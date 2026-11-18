"""AGC Web HMI server.

The server intentionally uses only Python's standard library so it can run in
the same environment as the existing AGC controller.  It keeps the controller
model in memory, exposes a small JSON API, and serves the browser HMI from
``web/public``.
"""

from __future__ import annotations

import json
import math
import mimetypes
import os
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone
from contextlib import contextmanager
from types import SimpleNamespace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse

import sys


AGC_DIR = Path(__file__).resolve().parents[1]
if str(AGC_DIR) not in sys.path:
    sys.path.insert(0, str(AGC_DIR))

import agc_ctrl  # noqa: E402  (the project is intentionally stdlib-only)
from agc_db import DEFAULT_DB_PATH  # noqa: E402


WEB_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = WEB_DIR / "public"
DATA_DIR = WEB_DIR / "data"
DB_PATH = Path(os.environ.get("AGC_DB", DEFAULT_DB_PATH))
MODEL_FILE = Path(os.environ.get("AGC_MODEL", AGC_DIR / "model_real.e"))
PORT = int(os.environ.get("AGC_WEB_PORT", "8765"))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _json_value(value: Any) -> Any:
    """Convert model values into JSON-safe scalar values."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AGCDatabase:
    """SQLite persistence used by the Web HMI and its JSON APIs."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._transaction_depth = 0
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        schema = (WEB_DIR / "agc_schema.sql").read_text(encoding="utf-8")
        with self.lock:
            self.conn.executescript(schema)
            self.conn.commit()

    @staticmethod
    def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
        try:
            return row[key]
        except (IndexError, KeyError):
            return default

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self.lock:
            return list(self.conn.execute(sql, tuple(params)).fetchall())

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self.lock:
            self.conn.execute(sql, tuple(params))
            if not self._transaction_depth:
                self.conn.commit()

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self.lock:
            self.conn.executemany(sql, list(rows))
            if not self._transaction_depth:
                self.conn.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            outer = self._transaction_depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._transaction_depth += 1
            try:
                yield
                if outer:
                    self.conn.commit()
            except Exception:
                if outer:
                    self.conn.rollback()
                raise
            finally:
                self._transaction_depth -= 1

    def settings(self) -> Dict[str, Any]:
        rows = self.fetchall("SELECT name, value, value_type FROM system_settings ORDER BY name")
        output: Dict[str, Any] = {}
        for row in rows:
            value = row["value"]
            if row["value_type"] == "int":
                value = _integer(value, 0)
            elif row["value_type"] == "float":
                value = _number(value)
            output[str(row["name"])] = value
        return output

    def save_settings(self, values: Dict[str, Any], descriptions: Optional[Dict[str, str]] = None, control_mode: str = "closed_loop") -> None:
        now = _utc_now()
        descriptions = descriptions or {}
        rows = []
        for name, value in values.items():
            value_type = "int" if isinstance(value, int) and not isinstance(value, bool) else "float" if isinstance(value, float) else "text"
            rows.append((str(name), str(value), value_type, descriptions.get(str(name), ""), now))
        rows.append(("CONTROL_MODE", str(control_mode), "text", "Web HMI 控制模式", now))
        self.executemany(
            """INSERT INTO system_settings(name, value, value_type, description, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET value=excluded.value, value_type=excluded.value_type,
                 description=excluded.description, updated_at=excluded.updated_at""",
            rows,
        )

    def save_device(self, row: Dict[str, Any]) -> None:
        now = _utc_now()
        self.execute(
            """INSERT INTO devices(kind, device_id, name, control_state, run_status, p_min, p_max,
                 control_step, control_deadband, preset_power, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(kind, device_id) DO UPDATE SET name=excluded.name,
                 control_state=excluded.control_state, run_status=excluded.run_status,
                 p_min=excluded.p_min, p_max=excluded.p_max, control_step=excluded.control_step,
                 control_deadband=excluded.control_deadband, preset_power=excluded.preset_power,
                 updated_at=excluded.updated_at""",
            (
                str(row.get("kind", "")), str(row.get("device_id", "")), str(row.get("name", "")),
                str(row.get("control_state", "remote")), _integer(row.get("run_status", 0)),
                _number(row.get("p_min", 0)), _number(row.get("p_max", 0)),
                _number(row.get("control_step", 0)), _number(row.get("control_deadband", 0)),
                _number(row.get("preset_power", 0)), now,
            ),
        )

    def device_rows(self) -> List[sqlite3.Row]:
        return self.fetchall("SELECT * FROM devices ORDER BY kind, device_id")

    def save_curve(self, kind: str, device_id: Any, curve: List[float]) -> None:
        now = _utc_now()
        with self.lock:
            self.conn.execute("DELETE FROM device_curves WHERE kind=? AND device_id=?", (str(kind), str(device_id)))
            self.conn.executemany(
                "INSERT INTO device_curves(kind, device_id, point_index, minute_of_day, power, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(str(kind), str(device_id), index, index * 15, _number(value), now) for index, value in enumerate(curve[:96])],
            )
            if not self._transaction_depth:
                self.conn.commit()

    def curve(self, kind: str, device_id: Any) -> List[float]:
        rows = self.fetchall("SELECT power FROM device_curves WHERE kind=? AND device_id=? ORDER BY point_index", (str(kind), str(device_id)))
        return [_number(row["power"]) for row in rows]

    def save_cycle(self, point: Dict[str, Any]) -> None:
        with self.transaction():
            latest = self.latest_cycle_row()
            point["cycle"] = max(_integer(point.get("cycle", 0)), (latest["cycle"] + 1) if latest else 1)
            for row in point.get("commands", []):
                row["cycle"] = point["cycle"]
            self._insert_cycle(point)

    def _insert_cycle(self, point: Dict[str, Any]) -> None:
        summary = point.get("summary", {})
        weather = point.get("weather", {})
        self.execute(
            """INSERT INTO control_cycles(cycle, recorded_at, control_mode, running,
                 load_power, renewable_power, storage_power, diesel_power, balance_power,
                 wind_speed, solar_irradiance, env_temperature)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _integer(point.get("cycle", 0)), str(point.get("timestamp", _utc_now())),
                str(point.get("controlMode", "closed_loop")), int(bool(point.get("running", False))),
                _number(summary.get("load")), _number(summary.get("renewable")), _number(summary.get("storage")),
                _number(summary.get("diesel")), _number(summary.get("balance")),
                _number(weather.get("wind_speed")), _number(weather.get("solor_irrad")), _number(weather.get("env_temp")),
            ),
        )
        commands = point.get("commands", [])
        if commands:
            self.executemany(
                """INSERT INTO device_samples(cycle, recorded_at, kind, device_id, name,
                     control_state, run_status, realtime_power, command_power, preset_power,
                     control_step, control_deadband)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        _integer(row.get("cycle", point.get("cycle", 0))), str(row.get("timestamp", point.get("timestamp", _utc_now()))),
                        str(row.get("kind", "")), str(row.get("id", "")), str(row.get("name", "")),
                        str(row.get("controlState", "remote")), _integer(row.get("runStatus", 0)),
                        _number(row.get("realtimePower")), _number(row.get("commandPower")), _number(row.get("presetPower")),
                        _number(row.get("controlStep")), _number(row.get("controlDeadband")),
                    ) for row in commands
                ],
            )

    def history(self, limit: int = 1000) -> List[Dict[str, Any]]:
        rows = self.fetchall("SELECT * FROM control_cycles ORDER BY cycle DESC LIMIT ?", (max(1, min(limit, 5000)),))
        output = []
        for row in reversed(rows):
            output.append({
                "timestamp": row["recorded_at"], "cycle": row["cycle"], "controlMode": row["control_mode"], "running": bool(row["running"]),
                "summary": {"load": row["load_power"], "renewable": row["renewable_power"], "storage": row["storage_power"], "diesel": row["diesel_power"], "balance": row["balance_power"]},
                "weather": {"wind_speed": row["wind_speed"], "solor_irrad": row["solar_irradiance"], "env_temp": row["env_temperature"]},
            })
        return output

    def latest_cycle_row(self) -> Optional[sqlite3.Row]:
        return self.fetchone("SELECT * FROM control_cycles ORDER BY cycle DESC LIMIT 1")

    def query_history(self, start="", end="", before=0, anchor=None, limit=240):
        """Stable cycle cursor plus UTC-aware time range; end is exclusive."""
        def parse_time(value):
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError()
                return parsed.astimezone(timezone.utc)
            except (ValueError, TypeError):
                raise ValueError("查询时间须为包含时区的 ISO 时间") from None
        start_time, end_time = parse_time(start), parse_time(end)
        if start_time and end_time and start_time >= end_time:
            raise ValueError("结束时间必须晚于开始时间")
        limit = max(1, min(int(limit), 10000))
        with self.lock:
            if anchor is None:
                latest = self.latest_cycle_row()
                anchor = int(latest["cycle"]) if latest else 0
            clauses, params = ["cycle<=?"], [int(anchor)]
            for value, operator in ((start_time, ">="), (end_time, "<")):
                if value:
                    clauses.append(f"julianday(recorded_at){operator}julianday(?)")
                    params.append(value.isoformat())
            where = " AND ".join(clauses)
            stats = dict(self.fetchone(f"""SELECT COUNT(*) AS count, AVG(load_power) AS avgLoad,
                AVG(renewable_power) AS avgRenewable, MAX(load_power) AS peakLoad,
                MAX(ABS(balance_power)) AS maxAbsBalance FROM control_cycles WHERE {where}""", params))
            if before:
                clauses.append("cycle<?")
                params.append(int(before))
            rows = self.fetchall(f"SELECT * FROM control_cycles WHERE {' AND '.join(clauses)} ORDER BY cycle DESC LIMIT ?", [*params, limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [{"timestamp": row["recorded_at"], "cycle": row["cycle"],
                  "controlMode": row["control_mode"], "running": bool(row["running"]),
                  "summary": {name: row[column] for name, column in (
                      ("load", "load_power"), ("renewable", "renewable_power"),
                      ("storage", "storage_power"), ("diesel", "diesel_power"), ("balance", "balance_power"))}}
                 for row in reversed(rows)]
        return {"items": items, "statistics": stats, "anchor": int(anchor),
                "hasMore": has_more, "nextBefore": rows[-1]["cycle"] if has_more else None,
                "range": {"start": start, "end": end}, "limit": limit}

    def latest_device_samples(self) -> List[sqlite3.Row]:
        return self.fetchall(
            """SELECT s.* FROM devices d JOIN device_samples s
                 ON s.id=(SELECT sample.id FROM device_samples sample
                          WHERE sample.kind=d.kind AND sample.device_id=d.device_id
                          ORDER BY sample.cycle DESC LIMIT 1)
               ORDER BY s.kind, s.device_id"""
        )

    def commands(self, kind: str = "", device_id: Any = "", limit: int = 5000) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if kind:
            clauses.append("kind=?"); params.append(str(kind))
        if device_id != "":
            clauses.append("device_id=?"); params.append(str(device_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(limit, 10000)))
        rows = self.fetchall(f"SELECT * FROM device_samples{where} ORDER BY cycle DESC, id DESC LIMIT ?", params)
        return [
            {"timestamp": row["recorded_at"], "cycle": row["cycle"], "kind": row["kind"], "id": row["device_id"], "name": row["name"], "controlState": row["control_state"], "runStatus": row["run_status"], "realtimePower": row["realtime_power"], "commandPower": row["command_power"], "presetPower": row["preset_power"], "controlStep": row["control_step"], "controlDeadband": row["control_deadband"]}
            for row in reversed(rows)
        ]

    def logs(self, keyword: str = "", level: str = "", limit: int = 300) -> List[Dict[str, str]]:
        clauses, params = [], []
        if keyword:
            clauses.append("message LIKE ?"); params.append(f"%{keyword}%")
        if level:
            clauses.append("level=?"); params.append(level.upper())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(limit, 5000)))
        rows = self.fetchall(f"SELECT logged_at, level, message FROM event_logs{where} ORDER BY id DESC LIMIT ?", params)
        return [{"timestamp": row["logged_at"], "level": row["level"], "message": row["message"]} for row in rows]

    def save_event(self, logged_at: str, level: str, message: str) -> None:
        self.execute("INSERT INTO event_logs(logged_at, level, message) VALUES (?, ?, ?)", (logged_at, level, message))

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def stats(self) -> Dict[str, Any]:
        names = ("system_settings", "devices", "device_curves", "control_cycles", "device_samples", "event_logs")
        return {name: int(self.fetchone(f"SELECT COUNT(*) AS total FROM {name}")["total"]) for name in names}


class AGCState:
    """Thread-safe application state and the bridge to ``agc_ctrl``."""

    def __init__(self, database_path=DB_PATH, data_dir=DATA_DIR, start_worker=True) -> None:
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.running = False
        self.control_mode = "closed_loop"
        self.cycle = 0
        self.gather_count = 0
        self.last_error = ""
        self._synced_cycle = -1
        self.started_at = _utc_now()
        self.db = AGCDatabase(database_path)
        self.settings_path = data_dir / "settings.json"
        self.device_modes_path = data_dir / "device_modes.json"
        self.history_path = data_dir / "history.json"
        self.command_history_path = data_dir / "command_history.json"
        self.log_path = data_dir / "agc_web.log"
        self.model = agc_ctrl.load_model(MODEL_FILE)
        self.config = agc_ctrl.initialize_system(
            self.model,
            log_file=self.log_path,
            ctrl_output_file=data_dir / "dev_ctrl.e",
            db_path=database_path,
        )
        self.config.control_mode = self.control_mode
        self.do_one_ctrl, self.agc_ctrl_stg_check, self.do_renew_gen_fur_calc = (
            agc_ctrl._load_control_functions()
        )
        self.history: List[Dict[str, Any]] = self._load_history()
        self.command_history: List[Dict[str, Any]] = self._load_command_history()
        self.cycle = max((_integer(row.get("cycle", 0)) for row in self.history), default=0)
        self._apply_saved_settings()
        self._load_device_modes()
        self.worker = threading.Thread(target=self._worker_loop, name="agc-web-loop", daemon=True)
        if start_worker:
            self.worker.start()

    # ---------- persistence and control loop ----------

    def _load_history(self) -> List[Dict[str, Any]]:
        persisted = self.db.history(1000)
        if persisted:
            return persisted
        if not self.history_path.exists():
            return []
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            rows = data[-1000:] if isinstance(data, list) else []
            for row in rows:
                self.db.save_cycle(row)
            return rows
        except (OSError, ValueError):
            return []

    def _save_history(self) -> None:
        # Kept as a compatibility hook for callers from older Web HMI builds.
        # New cycles are persisted atomically through ``AGCDatabase.save_cycle``.
        return

    def _load_command_history(self) -> List[Dict[str, Any]]:
        persisted = self.db.commands(limit=5000)
        if persisted:
            return persisted
        if not self.command_history_path.exists():
            return []
        try:
            data = json.loads(self.command_history_path.read_text(encoding="utf-8"))
            rows = data[-5000:] if isinstance(data, list) else []
            if rows:
                self.db.executemany(
                    """INSERT OR REPLACE INTO device_samples(cycle, recorded_at, kind, device_id, name,
                         control_state, run_status, realtime_power, command_power, preset_power,
                         control_step, control_deadband)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(_integer(row.get("cycle", 0)), str(row.get("timestamp", _utc_now())), str(row.get("kind", "")), str(row.get("id", "")), str(row.get("name", "")), str(row.get("controlState", "remote")), _integer(row.get("runStatus", 0)), _number(row.get("realtimePower")), _number(row.get("commandPower")), _number(row.get("presetPower")), _number(row.get("controlStep")), _number(row.get("controlDeadband"))) for row in rows],
                )
            return rows
        except (OSError, ValueError):
            return []

    def _save_command_history(self) -> None:
        return

    def _apply_saved_settings(self) -> None:
        persisted = self.db.settings()
        data: Dict[str, Any] = {}
        if not persisted and self.settings_path.exists():
            try:
                data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            params = data.get("params", {}) if isinstance(data, dict) else {}
            descriptions = {str(item.name): str(getattr(item, "desc", "")) for item in getattr(self.model, "para", [])}
            if isinstance(params, dict):
                self.db.save_settings(params, descriptions, data.get("control_mode", "closed_loop"))
                persisted = self.db.settings()
        params = persisted
        saved_mode = params.pop("CONTROL_MODE", None)
        if saved_mode in {"open_loop", "closed_loop"}:
            self.control_mode = saved_mode
        self.config.control_mode = self.control_mode
        for para in getattr(self.model, "para", []):
            if para.name in params:
                para.value = self._cast_parameter(para.value, params[para.name])
        self._refresh_periods()

    def _all_devices(self) -> Iterable[Any]:
        for table_name in ("wind_generator", "pv_generator", "estorage", "diesel_generator", "energyconsumer"):
            yield from getattr(self.model, table_name, []) or []

    def _load_device_modes(self) -> None:
        saved: Dict[str, Any] = {}
        rows = self.db.device_rows()
        if rows:
            for row in rows:
                saved[f"{row['kind']}:{row['device_id']}"] = dict(row)
        elif self.device_modes_path.exists():
            try:
                payload = json.loads(self.device_modes_path.read_text(encoding="utf-8"))
                saved = payload if isinstance(payload, dict) else {}
            except (OSError, ValueError):
                saved = {}
        for device in self._all_devices():
            kind = self._device_kind(device)
            key = f"{kind}:{getattr(device, 'id', '')}"
            legacy_key = self._device_key(device)
            item = saved.get(key, saved.get(legacy_key, {})) if isinstance(saved.get(key, saved.get(legacy_key, {})), dict) else {}
            state = str(item.get("control_state", "remote")).lower()
            device.agc_control_state = state if state in {"remote", "local"} else "remote"
            for field, saved_field in (("p_step", "control_step"), ("p_dead", "control_deadband"), ("p_min", "p_min"), ("p_max", "p_max"), ("run_stat", "run_status")):
                if saved_field in item or field in item:
                    old = getattr(device, field, 0)
                    setattr(device, field, self._cast_parameter(old, item.get(saved_field, item.get(field))))
            device.agc_control_step = _number(getattr(device, "p_step", 0.0))
            device.agc_control_deadband = _number(getattr(device, "p_dead", 0.0))
            device.agc_preset_power = _number(item.get("preset_power", getattr(device, "p_cur", 0.0)))
            curve = item.get("preset_curve")
            if not isinstance(curve, list):
                curve = self.db.curve(kind, getattr(device, "id", ""))
            if not isinstance(curve, list) or len(curve) not in {24, 96}:
                curve = [device.agc_preset_power] * 96
            elif len(curve) == 24:
                # Migrate earlier hourly presets to the new 15-minute format.
                curve = [value for value in curve for _ in range(4)]
            device.agc_preset_curve = [_number(value, device.agc_preset_power) for value in curve]
            self._save_one_device(device, kind)

    @staticmethod
    def _device_key(device: Any) -> str:
        return f"{type(device).__name__}:{getattr(device, 'id', '')}"

    @staticmethod
    def _device_kind(device: Any) -> str:
        name = type(device).__name__
        return {"wind_generator": "wind", "pv_generator": "pv", "estorage": "storage", "diesel_generator": "diesel", "energyconsumer": "load"}.get(name, name)

    def _save_one_device(self, device: Any, kind: Optional[str] = None, save_curve: bool = True) -> None:
        kind = kind or self._device_kind(device)
        self.db.save_device({
            "kind": kind, "device_id": getattr(device, "id", ""), "name": getattr(device, "name", ""),
            "control_state": getattr(device, "agc_control_state", "remote"), "run_status": getattr(device, "run_stat", 0),
            "p_min": getattr(device, "p_min", 0), "p_max": getattr(device, "p_max", 0),
            "control_step": getattr(device, "p_step", 0), "control_deadband": getattr(device, "p_dead", 0),
            "preset_power": getattr(device, "agc_preset_power", 0),
        })
        if save_curve:
            self.db.save_curve(kind, getattr(device, "id", ""), list(getattr(device, "agc_preset_curve", [])))

    def _save_device_modes(self) -> None:
        for device in self._all_devices():
            self._save_one_device(device)

    @staticmethod
    def _control_kind_name(kind: str) -> str:
        return {
            "wind": "wind_generator",
            "pv": "pv_generator",
            "storage": "estorage",
            "diesel": "diesel_generator",
            "load": "energyconsumer",
        }.get(kind, "")

    def _find_device(self, kind: str, item_id: Any) -> Any:
        table = self._control_kind_name(kind)
        return next(
            (
                item
                for item in getattr(self.model, table, [])
                if str(getattr(item, "id", "")) == str(item_id)
            ),
            None,
        ) if table else None

    def _record_commands(self, timestamp: str) -> List[Dict[str, Any]]:
        commands: List[Dict[str, Any]] = []
        for device in self._all_devices():
            kind = self._device_kind(device)
            commands.append({
                "timestamp": timestamp,
                "cycle": self.cycle,
                "kind": kind,
                "id": _json_value(getattr(device, "id", "")),
                "name": str(getattr(device, "name", "")),
                "controlState": str(getattr(device, "agc_control_state", "remote")),
                "runStatus": _integer(getattr(device, "run_stat", 0)),
                "realtimePower": round(_number(getattr(device, "p_cur", getattr(device, "p", 0.0))), 3),
                "commandPower": round(_number(getattr(device, "p_ctrl", getattr(device, "p_cur", 0.0))), 3),
                "presetPower": round(_number(getattr(device, "agc_preset_power", 0.0)), 3),
                "controlStep": round(_number(getattr(device, "p_step", 0.0)), 3),
                "controlDeadband": round(_number(getattr(device, "p_dead", 0.0)), 3),
            })
        return commands

    def _device_parameters(self, kind: str, item: Any) -> Dict[str, Any]:
        return {
            "kind": kind,
            "id": _json_value(getattr(item, "id", "")),
            "name": str(getattr(item, "name", "")),
            "controlState": str(getattr(item, "agc_control_state", "remote")),
            "runStatus": _integer(getattr(item, "run_stat", 0)),
            "pMin": self._power_bounds(item)[0],
            "pMax": _number(self._power_bounds(item)[1]),
            "controlStep": round(_number(getattr(item, "p_step", getattr(item, "agc_control_step", 0.0))), 3),
            "controlDeadband": round(_number(getattr(item, "p_dead", getattr(item, "agc_control_deadband", 0.0))), 3),
            "presetPower": round(_number(getattr(item, "agc_preset_power", 0.0)), 3),
            "presetCurve": list(getattr(item, "agc_preset_curve", [])),
        }

    def _sync_from_database(self) -> Optional[sqlite3.Row]:
        """Refresh Web state from cycles written by another AGC process.

        The Web HMI and ``agc_ctrl.py`` can run as separate processes.  The
        calculation process therefore remains the source of telemetry and
        commands in SQLite. A new cycle refreshes telemetry once; historical
        control modes and device ownership never overwrite current settings.
        """
        latest = self.db.latest_cycle_row()
        if latest is None or latest["cycle"] <= self._synced_cycle:
            return latest
        persisted = self.db.history(240)
        if persisted:
            self.history = persisted
            self.cycle = max(self.cycle, max(_integer(row.get("cycle", 0)) for row in persisted))
        for row in self.db.latest_device_samples():
            device = self._find_device(row["kind"], row["device_id"])
            if device is None:
                continue
            device.p_cur = _number(row["realtime_power"], _number(getattr(device, "p_cur", 0)))
            device.p_ctrl = _number(row["command_power"], _number(getattr(device, "p_ctrl", device.p_cur)))
            device.run_stat = _integer(row["run_status"], _integer(getattr(device, "run_stat", 0)))
        env_map = {str(item.name): item for item in getattr(self.model, "env_para", []) or []}
        yc_map = {str(item.id): item for item in getattr(self.model, "yc", []) or []}
        for name, column in (("wind_speed", "wind_speed"), ("solor_irrad", "solar_irradiance"), ("env_temp", "env_temperature")):
            env = env_map.get(name)
            yc = yc_map.get(str(getattr(env, "yc_id", ""))) if env else None
            if yc is not None:
                yc.value = _number(latest[column], _number(yc.value))
        self._synced_cycle = latest["cycle"]
        return latest

    def _apply_local_curves(self) -> None:
        now = datetime.now()
        point = now.hour * 4 + now.minute // 15
        for device in self._all_devices():
            if str(getattr(device, "agc_control_state", "remote")) != "local":
                continue
            curve = getattr(device, "agc_preset_curve", [])
            target = _number(curve[point], getattr(device, "agc_preset_power", 0.0)) if len(curve) >= 96 else _number(getattr(device, "agc_preset_power", 0.0))
            lower, upper = self._power_bounds(device)
            before = _number(getattr(device, "p_ctrl", getattr(device, "p_cur", 0.0)))
            requested = target
            kind = self._device_kind(device)
            if not agc_ctrl._is_running(device):
                lower = upper = 0.0
            elif kind == "storage":
                lower = max(lower, -agc_ctrl._storage_soc_power_limit(self.model, device, charging=True))
                upper = min(upper, agc_ctrl._storage_soc_power_limit(self.model, device, charging=False))
            target = agc_ctrl._limit_control_step(self.model, device, target, kind)
            if abs(target - before) <= agc_ctrl._control_deadband(self.model, device, kind):
                target = before
            # Hard equipment constraints take precedence over ramp/deadband.
            target = max(lower, min(upper, target))
            before_set = _number(getattr(device, "p_set", before)) if hasattr(device, "p_set") else None
            device.p_ctrl = target
            if hasattr(device, "p_set"):
                device.p_set = target
            device.agc_preset_power = requested
            if self.control_mode == "closed_loop" and hasattr(self.config, "send_device_p_ctrl"):
                if not self.config.send_device_p_ctrl(device, target, self._device_kind(device)):
                    device.p_ctrl = before
                    if hasattr(device, "p_set"):
                        device.p_set = before_set
                    self.config.logger.warning("本地曲线 SCADA 遥调未确认 %s point=%s", getattr(device, "name", ""), point)
            self.config.logger.info("本地曲线控制 %s point=%s p_ctrl=%.3f", getattr(device, "name", ""), point, target)

    def set_control_mode(self, mode: str) -> Dict[str, Any]:
        mode = str(mode or "").lower()
        if mode not in {"open_loop", "closed_loop"}:
            raise ValueError("控制模式必须是 open_loop 或 closed_loop")
        with self.lock:
            self.control_mode = mode
            self.config.control_mode = mode
            self.config._last_scada_commands.clear()
            self._save_settings()
            self.config.logger.info("Web 控制模式切换为：%s", "闭环" if mode == "closed_loop" else "开环")
            return self._snapshot()

    def _save_settings(self) -> None:
        values = {str(item.name): _json_value(item.value) for item in getattr(self.model, "para", [])}
        descriptions = {str(item.name): str(getattr(item, "desc", "")) for item in getattr(self.model, "para", [])}
        self.db.save_settings(values, descriptions, self.control_mode)

    @staticmethod
    def _cast_parameter(old_value: Any, new_value: Any) -> Any:
        if isinstance(old_value, bool):
            return bool(new_value)
        if isinstance(old_value, int) and not isinstance(old_value, bool):
            return _integer(new_value, old_value)
        if isinstance(old_value, float):
            return _number(new_value, old_value)
        return str(new_value)

    def _refresh_periods(self) -> None:
        self.config.ctrl_trigger = max(1, _integer(self._parameter("CTRL_TRIGGER"), self.config.ctrl_trigger))
        self.config.gather_trigger = max(1, _integer(self._parameter("GATHER_TRIGGER"), self.config.gather_trigger))
        self.model.ctrl_trigger = self.config.ctrl_trigger
        self.model.gather_trigger = self.config.gather_trigger

    def _parameter(self, name: str, default: Any = None) -> Any:
        for item in getattr(self.model, "para", []):
            if str(getattr(item, "name", "")) == name:
                return getattr(item, "value", default)
        return default

    def _worker_loop(self) -> None:
        next_cycle = time.monotonic()
        while not self.stop_event.wait(0.2):
            with self.lock:
                is_running = self.running
                period = max(1, int(self.config.ctrl_trigger))
            if not is_running:
                next_cycle = time.monotonic()
                continue
            now = time.monotonic()
            if now < next_cycle:
                continue
            try:
                self.run_cycle()
            except Exception as exc:  # keep the HMI alive if plant integration fails
                with self.lock:
                    self.last_error = str(exc)
                self.config.logger.exception("Web 控制周期失败: %s", exc)
            next_cycle = time.monotonic() + period

    def run_cycle(self) -> Dict[str, Any]:
        with self.lock:
            self._sync_from_database()
            self.config.control_mode = self.control_mode
            # Gather the full plant first.  Local devices are then pinned to
            # their preset curve while the closed-loop strategy may adjust only
            # remote devices (agc_ctrl._set_p_ctrl enforces this guard).
            agc_ctrl.data_gather(self.config)
            self.do_renew_gen_fur_calc(
                self.config.logger,
                self.model,
                agc_ctrl._telemetry_value(getattr(self.model, "wind_speed", None)),
                agc_ctrl._telemetry_value(getattr(self.model, "solor_irrad", None)),
                agc_ctrl._telemetry_value(getattr(self.model, "env_temp", None)),
            )
            self.config.gather_count += 1
            self._apply_local_curves()
            if self.control_mode == "closed_loop":
                self.do_one_ctrl(self.config.logger, self.model)
                self.agc_ctrl_stg_check(self.config.logger, self.model)
                self.config.control_count += 1
            else:
                self.model.agc_control_mode = "open_loop_preset"
                self.config.logger.info("开环模式：跳过自动决策，仅执行本地预设曲线")
            self.cycle += 1
            self.gather_count = self.config.gather_count
            point = self._snapshot(include_history=False, include_curves=False, sync_database=False)
            point["commands"] = self._record_commands(point["timestamp"])
            self.db.save_cycle(point)
            self.cycle = point["cycle"]
            self._synced_cycle = self.cycle
            self.command_history.extend(point["commands"])
            self.command_history = self.command_history[-5000:]
            self.history.append({key: point[key] for key in ("timestamp", "cycle", "controlMode", "running", "summary", "weather")})
            self.history = self.history[-1000:]
            return point

    def set_running(self, running: bool) -> Dict[str, Any]:
        with self.lock:
            self.running = bool(running)
            self.config.is_running = self.running
            self.config.logger.info("Web 控制台：%s", "启动自动控制" if self.running else "暂停自动控制")
            return self._snapshot()

    def update_parameters(self, values: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            known = {str(item.name): item for item in getattr(self.model, "para", [])}
            changed = []
            for name, value in values.items():
                item = known.get(str(name))
                if item is None:
                    continue
                item.value = self._cast_parameter(item.value, value)
                changed.append(str(name))
            self._refresh_periods()
            self._save_settings()
            self.config.logger.info("参数配置已更新: %s", ", ".join(changed) or "无变化")
            return self.config_payload()

    def update_simulation(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """Update demo telemetry without changing the source E-file."""
        with self.lock:
            for name in ("wind_speed", "solor_irrad", "env_temp", "load_power"):
                if name in values and not math.isfinite(_number(values[name], float("nan"))):
                    raise ValueError(f"仿真遥测参数无效：{name}")
            env_map = {str(item.name): item for item in getattr(self.model, "env_para", [])}
            yc_map = {str(item.id): item for item in getattr(self.model, "yc", [])}
            for name in ("wind_speed", "solor_irrad", "env_temp"):
                if name in values and name in env_map:
                    yc = yc_map.get(str(getattr(env_map[name], "yc_id", "")))
                    if yc is not None:
                        yc.value = _number(values[name], _number(yc.value))
            if "load_power" in values:
                load_value = _number(values["load_power"])
                for load in getattr(self.model, "energyconsumer", []):
                    yc = yc_map.get(str(getattr(load, "p_yc_id", "")))
                    if yc is not None:
                        yc.value = load_value
            self.config.logger.info("仿真遥测已更新: %s", ", ".join(str(k) for k in values))
            return self._snapshot()

    @staticmethod
    def _power_bounds(device):
        if hasattr(device, "charge_p_max"):
            return (-abs(float(device.charge_p_max)), abs(float(device.dis_charge_p_max)))
        return (float(getattr(device, "p_min", 0)), float(getattr(device, "p_max", float("inf"))))

    def _prepare_device_config(self, target, parameters):
        if not isinstance(parameters, dict):
            raise ValueError("设备参数必须是 JSON 对象")
        allowed = {"run_stat", "p_cur", "p_step", "p_dead", "control_step", "control_deadband",
                   "p_min", "p_max", "control_state", "preset_power", "preset_curve"}
        if set(parameters) - allowed:
            raise ValueError("设备字段无效")
        if "preset_curve" in parameters and "preset_power" in parameters:
            raise ValueError("请分别保存曲线或默认功率")
        draft = SimpleNamespace(**vars(target))
        if hasattr(target, "charge_p_max"):
            bounds = self._power_bounds(target)
            for field, bound in zip(("p_min", "p_max"), bounds):
                if field in parameters and _number(parameters[field], float("nan")) != bound:
                    raise ValueError("储能边界由模型充放电额定功率确定")
        aliases = {"control_state": "agc_control_state", "control_step": "p_step", "control_deadband": "p_dead"}
        for field, value in parameters.items():
            if field in {"preset_power", "preset_curve"}:
                continue
            if field == "control_state":
                value = str(value).lower()
                if value not in {"remote", "local"}:
                    raise ValueError("受控状态必须是 remote 或 local")
            else:
                value = _number(value, float("nan"))
                if not math.isfinite(value):
                    raise ValueError(f"设备参数必须是有限数字：{field}")
                if field == "run_stat":
                    if value not in (0, 1):
                        raise ValueError("运行状态只能是 0 或 1")
                    value = int(value)
                if field in {"p_step", "p_dead", "control_step", "control_deadband"} and value < 0:
                    raise ValueError("控制步长和死区不能为负")
            setattr(draft, aliases.get(field, field), value)
        lower, upper = self._power_bounds(draft)
        if lower > upper:
            raise ValueError("功率下限不能高于功率上限")
        curve_changed = False
        if "preset_curve" in parameters:
            curve = parameters["preset_curve"]
            if not isinstance(curve, list) or len(curve) != 96:
                raise ValueError("默认功率曲线必须包含 96 个 15 分钟点")
            curve = [_number(v, float("nan")) for v in curve]
            curve_changed = curve != getattr(target, "agc_preset_curve", [])
        elif "preset_power" in parameters:
            value = _number(parameters["preset_power"], float("nan"))
            if not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f"默认功率必须是 {lower:g} 至 {upper:g} MW 内的有限数字")
            # Saving unchanged parameters must not flatten an existing day curve.
            curve_changed = value != getattr(target, "agc_preset_power", None)
            curve = [value] * 96 if curve_changed else list(getattr(target, "agc_preset_curve", [value] * 96))
        else:
            curve = list(getattr(target, "agc_preset_curve", []))
        if curve_changed or "preset_curve" in parameters or "p_min" in parameters or "p_max" in parameters:
            if any(not math.isfinite(v) or not lower <= v <= upper for v in curve):
                raise ValueError(f"已有或新曲线须位于 {lower:g} 至 {upper:g} MW 范围；请一并调整曲线")
        if curve_changed:
            draft.agc_preset_curve = curve
            now = datetime.now()
            draft.agc_preset_power = curve[now.hour * 4 + now.minute // 15]
        draft.agc_control_step = getattr(draft, "p_step", 0)
        draft.agc_control_deadband = getattr(draft, "p_dead", 0)
        return draft, curve_changed

    def device_config(self, payload, return_snapshot=True):
        with self.lock:
            self._sync_from_database()
            kind, item_id = str(payload.get("kind", "")), str(payload.get("id", ""))
            target = self._find_device(kind, item_id)
            if target is None:
                raise ValueError("设备不存在")
            draft, curve_changed = self._prepare_device_config(target, payload.get("parameters", {}))
            old = dict(vars(target))
            if draft.run_stat != getattr(target, "run_stat", None):
                self.config.control_mode = self.control_mode
                if not self.config.send_device_run_state(target, draft.run_stat):
                    raise ValueError("SCADA遥控下发失败，设备运行状态未修改")
            try:
                vars(target).update(vars(draft))
                with self.db.transaction():
                    self._save_one_device(target, kind, save_curve=curve_changed)
            except Exception:
                vars(target).clear()
                vars(target).update(old)
                raise
            self.config.logger.info("设备参数已更新 %s/%s: %s", kind, item_id, ", ".join(payload.get("parameters", {})))
            return self._snapshot() if return_snapshot else None

    def device_command(self, payload, _return_snapshot=True):
        return self.device_config({"kind": payload.get("kind"), "id": payload.get("id"),
            "parameters": {str(payload.get("field", "run_stat")): payload.get("value")}}, _return_snapshot)

    def device_batch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a bounded safe operation to multiple devices in one request."""
        field = str(payload.get("field", "control_state"))
        if field not in {"control_state", "preset_power"}:
            raise ValueError("批量操作仅支持受控状态或默认功率")
        value = payload.get("value")
        if field == "control_state" and str(value).lower() not in {"remote", "local"}:
            raise ValueError("受控状态必须是 remote 或 local")
        targets = payload.get("devices", payload.get("items", []))
        if not isinstance(targets, list) or not targets or len(targets) > 100:
            raise ValueError("批量设备数量必须在 1 到 100 之间")
        results = []
        with self.lock:
            for item in targets:
                if isinstance(item, dict):
                    kind, item_id = item.get("kind", ""), item.get("id", "")
                elif isinstance(item, str) and ":" in item:
                    kind, item_id = item.split(":", 1)
                else:
                    results.append({"id": str(item), "ok": False, "error": "设备标识无效"})
                    continue
                try:
                    self.device_command({"kind": kind, "id": item_id, "field": field, "value": value}, _return_snapshot=False)
                    results.append({"kind": str(kind), "id": str(item_id), "ok": True})
                except Exception as exc:
                    results.append({"kind": str(kind), "id": str(item_id), "ok": False, "error": str(exc)})
            snapshot = self._snapshot()
            snapshot["batchResults"] = results
            return snapshot

    def device_detail(self, kind: str, item_id: Any) -> Dict[str, Any]:
        with self.lock:
            self._sync_from_database()
            device = self._find_device(kind, item_id)
            if device is None:
                raise ValueError("设备不存在")
            device_key = f"{kind}:{item_id}"
            history = [
                {
                    "timestamp": row["timestamp"],
                    "cycle": row.get("cycle", 0),
                    "realtimePower": row.get("realtimePower", 0),
                    "commandPower": row.get("commandPower", 0),
                    "presetPower": row.get("presetPower", 0),
                    "controlState": row.get("controlState", "remote"),
                    "runStatus": row.get("runStatus", 0),
                    "controlStep": row.get("controlStep", 0),
                    "controlDeadband": row.get("controlDeadband", 0),
                }
                for row in self.db.commands(kind, item_id, 240)
            ]
            return {
                "device": self._device_parameters(kind, device),
                "history": history,
                "commands": history[-120:],
            }

    # ---------- JSON views ----------

    def _command_status_map(self) -> Dict[tuple, str]:
        """Index the newest YT result once for the device list render."""
        result: Dict[tuple, str] = {}
        for item in reversed(list(getattr(self.config, "command_results", []) or [])):
            if str(item.get("kind", "")) != "yt":
                continue
            key = (str(item.get("rtu", "")), str(item.get("pnt", "")))
            result.setdefault(key, "ok" if item.get("ok") else "error")
        return result

    def _device(self, kind: str, item: Any, include_curve: bool = True, command_status_map: Optional[Dict[tuple, str]] = None) -> Dict[str, Any]:
        soc = getattr(item, "soc_cur", None)
        if soc is not None:
            soc = _number(soc)
            soc = soc / 100 if soc > 1 else soc
        state = str(getattr(item, "agc_control_state", "remote"))
        effective_power = getattr(item, "p_cur", getattr(item, "p", 0))
        command_power = _number(getattr(item, "p_ctrl", effective_power), _number(effective_power))
        point = agc_ctrl._find_device_control_point(self.model, item)
        command_status = None
        if point is not None:
            key = (str(getattr(point, "rtu", "")), str(getattr(point, "pnt", "")))
            command_status = (command_status_map or {}).get(key)
        result = {
            "kind": kind,
            "id": _json_value(getattr(item, "id", "")),
            "name": str(getattr(item, "name", f"{kind}-{getattr(item, 'id', '')}")),
            "status": _integer(getattr(item, "run_stat", 0)),
            "power": round(_number(effective_power), 3),
            "commandPower": round(command_power, 3),
            "commandDiff": round(command_power - _number(effective_power), 3),
            "maxPower": _number(self._power_bounds(item)[1]),
            "soc": None if soc is None else round(soc, 4),
            "minPower": self._power_bounds(item)[0],
            "step": round(_number(getattr(item, "p_step", 0)), 3),
            "controlState": state,
            "presetPower": round(_number(getattr(item, "agc_preset_power", 0.0)), 3),
            "mapped": point is not None,
            "commandStatus": command_status or ("unconfigured" if point is None else "pending"),
        }
        if include_curve:
            result["presetCurve"] = list(getattr(item, "agc_preset_curve", []))
        return result

    def devices(self, include_curve: bool = True) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        command_status_map = self._command_status_map()
        for kind, name in (
            ("wind", "wind_generator"),
            ("pv", "pv_generator"),
            ("storage", "estorage"),
            ("diesel", "diesel_generator"),
            ("load", "energyconsumer"),
        ):
            result.extend(self._device(kind, item, include_curve, command_status_map) for item in getattr(self.model, name, []))
        return result

    def _snapshot(self, include_history=True, include_curves=True, sync_database=True, history_since=None, include_devices=True):
        with self.lock:
            return self._snapshot_locked(include_history, include_curves, sync_database, history_since, include_devices)

    def _snapshot_locked(self, include_history, include_curves, sync_database, history_since, include_devices=True):
        latest = self._sync_from_database() if sync_database else None
        device_view = self.devices(include_curves if include_devices else False)
        renewable = sum(item["power"] for item in device_view if item["kind"] in {"wind", "pv"})
        storage = sum(item["power"] for item in device_view if item["kind"] == "storage")
        diesel = sum(item["power"] for item in device_view if item["kind"] == "diesel")
        load = sum(item["power"] for item in device_view if item["kind"] == "load")
        balance = renewable + storage + diesel - load
        weather = {
            "wind_speed": round(_number(getattr(getattr(self.model, "wind_speed", None), "value", 0)), 3),
            "solor_irrad": round(_number(getattr(getattr(self.model, "solor_irrad", None), "value", 0)), 3),
            "env_temp": round(_number(getattr(getattr(self.model, "env_temp", None), "value", 0)), 3),
        }
        scada_results = list(getattr(self.config, "scada_data_results", []) or [])
        command_results = list(getattr(self.config, "command_results", []) or [])
        yc_results = [item for item in scada_results if item.get("kind") == "yc"]
        yx_results = [item for item in scada_results if item.get("kind") == "yx"]
        data: Dict[str, Any] = {
            "timestamp": _utc_now(),
            "running": self.running,
            "controlMode": self.control_mode,
            "cycle": self.cycle,
            "gatherCount": self.gather_count,
            "lastError": self.last_error,
            "period": int(self.config.ctrl_trigger),
            "summary": {
                "load": round(load, 3),
                "renewable": round(renewable, 3),
                "storage": round(storage, 3),
                "diesel": round(diesel, 3),
                "balance": round(balance, 3),
            },
            "weather": weather,
            "scada": {
                "source": str(getattr(self.config.fes_client, "source", "fallback")),
                "commandSource": str(getattr(self.config.fes_client, "command_source", "unconfigured")),
                "ycOk": sum(1 for item in yc_results if item.get("ok")),
                "ycTotal": len(yc_results),
                "yxOk": sum(1 for item in yx_results if item.get("ok")),
                "yxTotal": len(yx_results),
                "lastCommandOk": sum(1 for item in command_results[-100:] if item.get("ok")),
                "lastCommandTotal": len(command_results[-100:]),
            },
            "backend": {
                "source": "sqlite",
                "database": str(self.db.path),
                "lastCycle": _integer(latest["cycle"], self.cycle) if latest else self.cycle,
                "lastCycleAt": str(latest["recorded_at"]) if latest else "",
                "running": bool(latest["running"]) if latest else self.running,
            },
        }
        if include_devices:
            data["devices"] = device_view
        if include_history or (history_since is not None and history_since != self.cycle):
            data["history"] = self.history[-240:]
        return data

    def config_payload(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "model": str(MODEL_FILE),
                "parameters": [
                    {
                        "name": str(item.name),
                        "description": str(getattr(item, "desc", "")),
                        "value": _json_value(getattr(item, "value", "")),
                    }
                    for item in getattr(self.model, "para", [])
                ],
                "simulation": self._snapshot(include_history=False, include_curves=False)["weather"],
            }

    def analysis_payload(self):
        """Explain current measurements and limits without executing control."""
        with self.lock:
            latest = self._sync_from_database()
            acceptance = agc_ctrl.calculate_renewable_acceptance_space(self.model, remember=False)
            rows = []
            for kind, device in agc_ctrl._all_control_devices(self.model):
                measured = agc_ctrl._power_value(device, "p_cur", "p", default=0)
                command = agc_ctrl._current_control_value(device)
                lower, upper = self._power_bounds(device)
                step = agc_ctrl._control_step(self.model, device, "renew" if kind == "renewable" else kind)
                dead = agc_ctrl._control_deadband(self.model, device, kind)
                headroom = max(0, upper - max(measured, command, lower)) if kind == "renewable" else 0
                if not agc_ctrl._is_running(device):
                    reason = "停运"
                elif not agc_ctrl._is_remote_controlled(device):
                    reason = "本地曲线"
                elif kind != "renewable":
                    reason = "被动平衡" if kind in {"diesel", "storage"} else "负荷监视"
                elif headroom <= 0:
                    reason = "已达功率上限"
                elif agc_ctrl._probe_step(self.model, device, "renew") <= dead:
                    reason = "步长未越过死区"
                elif acceptance["acceptance_space"] <= 0:
                    reason = "无新增接纳空间"
                else:
                    reason = "具备试探条件"
                point = agc_ctrl._find_device_control_point(self.model, device)
                rows.append({"kind": kind, "id": str(getattr(device, "id", "")),
                    "name": str(getattr(device, "name", "")), "measured": measured, "command": command,
                    "gap": command - measured, "headroom": headroom, "step": step, "deadband": dead,
                    "reason": reason, "mapped": point is not None})
            renewable_rows = [row for row in rows if row["kind"] == "renewable"]
            tryable = [row for row in renewable_rows if row["reason"] == "具备试探条件"]
            limited = [row for row in renewable_rows if row["reason"] in {"已达功率上限", "无新增接纳空间", "步长未越过死区", "本地曲线", "停运"}]
            passive = [row for row in rows if row["kind"] in {"storage", "diesel"} and row["reason"] == "被动平衡"]
            strategy = "先对满足步长、死区和余量条件的远方新能源发出单步试探；柴发/构网储能保持当前指令，被动承担后续不平衡。"
            summary = {
                "renewableTryableCapacity": round(sum(row["headroom"] for row in tryable), 3),
                "renewableLimitedCapacity": round(sum(row["headroom"] for row in limited), 3),
                "unconfirmedProbeCapacity": round(sum(min(row["headroom"], max(0.0, row["step"])) for row in tryable), 3),
                "passiveBalanceCapacity": round(sum(abs(row["command"]) for row in passive), 3),
                "tryableCount": len(tryable), "limitedCount": len(limited), "passiveCount": len(passive),
            }
            return {"cycle": self.cycle, "sampleAt": latest["recorded_at"] if latest else None,
                "evaluatedAt": _utc_now(), "controlMode": self.control_mode, "running": self.running,
                "source": self.config.fes_client.source, "commandSource": self.config.fes_client.command_source,
                "acceptance": acceptance, "summary": summary, "strategy": strategy, "devices": rows,
                "note": "按当前测量与配置评估；不下发指令。新能源上涨仅为试探，不能据此降低柴发或构网储能功率。"}

    def logs(self, keyword: str = "", level: str = "", limit: int = 200) -> List[Dict[str, str]]:
        return self.db.logs(keyword, level, limit)

    def close(self) -> None:
        self.stop_event.set()
        if self.worker.is_alive():
            self.worker.join(timeout=10)
        if self.worker.is_alive():
            return
        for handler in self.config.logger.handlers[:]:
            handler.close()
            self.config.logger.removeHandler(handler)
        if self.config.data_store:
            self.config.data_store.close()
        self.db.close()


STATE = None  # Created only after the listening socket is acquired.


class HMIHandler(BaseHTTPRequestHandler):
    server_version = "AGC-HMI/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # The AGC log file is the source shown in the HMI; avoid duplicate HTTP noise.
        return

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": message}, status)

    def _body(self) -> Dict[str, Any]:
        length = _integer(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(max(0, min(length, 1_000_000)))
        if not raw:
            return {}
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._get()
        except ValueError as exc:
            self._error(str(exc))
        except Exception as exc:
            self._error("服务器处理失败: " + str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

    def _get(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            query = parse_qs(parsed.query)
            lite = query.get("lite", ["0"])[0].lower() in {"1", "true", "yes"}
            since = _integer(query.get("history_since", [-1])[0], -1) if "history_since" in query else None
            include_devices = query.get("devices", ["1"])[0].lower() not in {"0", "false", "no"}
            self._send_json(STATE._snapshot(include_history=not lite, include_curves=not lite, history_since=since, include_devices=include_devices))
            return
        if parsed.path == "/api/config":
            self._send_json(STATE.config_payload())
            return
        if parsed.path == "/api/logs":
            query = parse_qs(parsed.query)
            keyword = query.get("keyword", [""])[0]
            level = query.get("level", [""])[0]
            limit = _integer(query.get("limit", [200])[0], 200)
            self._send_json({"items": STATE.logs(keyword, level, limit)})
            return
        if parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            limit = max(1, min(1000, _integer(query.get("limit", [240])[0], 240)))
            self._send_json({"items": STATE.db.history(limit)})
            return
        if parsed.path == "/api/history-query":
            query = parse_qs(parsed.query)
            self._send_json(STATE.db.query_history(
                start=query.get("start", [""])[0], end=query.get("end", [""])[0],
                before=_integer(query.get("before", [0])[0]),
                anchor=_integer(query["anchor"][0]) if "anchor" in query else None,
                limit=_integer(query.get("limit", [240])[0], 240)))
            return
        if parsed.path == "/api/analysis":
            self._send_json(STATE.analysis_payload())
            return
        if parsed.path == "/api/device-detail":
            query = parse_qs(parsed.query)
            self._send_json(STATE.device_detail(query.get("kind", [""])[0], query.get("id", [""])[0]))
            return
        if parsed.path == "/api/commands":
            query = parse_qs(parsed.query)
            kind = query.get("kind", [""])[0]
            item_id = query.get("id", [""])[0]
            limit = max(1, min(1000, _integer(query.get("limit", [240])[0], 240)))
            self._send_json({"items": STATE.db.commands(kind, item_id, limit)})
            return
        if parsed.path == "/api/health":
            snapshot = STATE._snapshot(include_history=False, include_curves=False)
            self._send_json({"ok": True, "pid": os.getpid(), "startedAt": STATE.started_at, "time": _utc_now(), "database": str(STATE.db.path), "tables": STATE.db.stats(), "scada": snapshot.get("scada", {})})
            return
        self._serve_static(parsed.path)

    def _serve_static(self, path: str) -> None:
        relative = path.lstrip("/") or "index.html"
        candidate = (PUBLIC_DIR / relative).resolve()
        if PUBLIC_DIR not in candidate.parents and candidate != PUBLIC_DIR:
            self._error("路径无效", HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            candidate = PUBLIC_DIR / "index.html"
        content = candidate.read_bytes()
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self._body()
            if parsed.path == "/api/control":
                action = str(payload.get("action", ""))
                if action == "start":
                    result = STATE.set_running(True)
                elif action == "stop":
                    result = STATE.set_running(False)
                elif action == "step":
                    STATE.run_cycle()
                    result = STATE._snapshot()
                else:
                    raise ValueError("控制动作必须是 start、stop 或 step")
                self._send_json(result)
                return
            if parsed.path == "/api/mode":
                self._send_json(STATE.set_control_mode(payload.get("mode", "")))
                return
            if parsed.path == "/api/config":
                self._send_json(STATE.update_parameters(payload.get("parameters", payload)))
                return
            if parsed.path == "/api/simulation":
                self._send_json(STATE.update_simulation(payload))
                return
            if parsed.path == "/api/device-command":
                self._send_json(STATE.device_command(payload))
                return
            if parsed.path == "/api/device-batch":
                self._send_json(STATE.device_batch(payload))
                return
            if parsed.path == "/api/device-config":
                self._send_json(STATE.device_config(payload))
                return
            self._error("接口不存在", HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._error(str(exc))
        except Exception as exc:  # keep API errors JSON-shaped for the HMI
            STATE.last_error = str(exc)
            self._error("服务器处理失败: " + str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)


class AGCHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> None:
    global STATE
    address = os.environ.get("AGC_WEB_HOST", "127.0.0.1")
    server = AGCHTTPServer((address, PORT), HMIHandler)
    STATE = AGCState()
    print(f"AGC Web HMI: http://{address}:{PORT}")
    print(f"模型文件: {MODEL_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE.close()
        server.server_close()


if __name__ == "__main__":
    main()
