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
import sqlite3
import threading
import time
from datetime import datetime, timezone
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
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
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
            self.conn.commit()

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self.lock:
            self.conn.executemany(sql, list(rows))
            self.conn.commit()

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
            self.conn.commit()

    def curve(self, kind: str, device_id: Any) -> List[float]:
        rows = self.fetchall("SELECT power FROM device_curves WHERE kind=? AND device_id=? ORDER BY point_index", (str(kind), str(device_id)))
        return [_number(row["power"]) for row in rows]

    def save_cycle(self, point: Dict[str, Any]) -> None:
        summary = point.get("summary", {})
        weather = point.get("weather", {})
        self.execute(
            """INSERT OR REPLACE INTO control_cycles(cycle, recorded_at, control_mode, running,
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
                """INSERT OR REPLACE INTO device_samples(cycle, recorded_at, kind, device_id, name,
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

    def latest_device_samples(self) -> List[sqlite3.Row]:
        return self.fetchall(
            """SELECT s.* FROM device_samples s
               JOIN (SELECT kind, device_id, MAX(cycle) AS max_cycle
                     FROM device_samples GROUP BY kind, device_id) latest
                 ON latest.kind=s.kind AND latest.device_id=s.device_id AND latest.max_cycle=s.cycle
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

    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.running = False
        self.control_mode = "closed_loop"
        self.cycle = 0
        self.gather_count = 0
        self.last_error = ""
        self.started_at = _utc_now()
        self.db = AGCDatabase(DB_PATH)
        self.settings_path = DATA_DIR / "settings.json"
        self.device_modes_path = DATA_DIR / "device_modes.json"
        self.history_path = DATA_DIR / "history.json"
        self.command_history_path = DATA_DIR / "command_history.json"
        self.log_path = DATA_DIR / "agc_web.log"
        self.model = agc_ctrl.load_model(MODEL_FILE)
        self.config = agc_ctrl.initialize_system(
            self.model,
            log_file=self.log_path,
            ctrl_output_file=DATA_DIR / "dev_ctrl.e",
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

    def _save_one_device(self, device: Any, kind: Optional[str] = None) -> None:
        kind = kind or self._device_kind(device)
        self.db.save_device({
            "kind": kind, "device_id": getattr(device, "id", ""), "name": getattr(device, "name", ""),
            "control_state": getattr(device, "agc_control_state", "remote"), "run_status": getattr(device, "run_stat", 0),
            "p_min": getattr(device, "p_min", 0), "p_max": getattr(device, "p_max", 0),
            "control_step": getattr(device, "p_step", 0), "control_deadband": getattr(device, "p_dead", 0),
            "preset_power": getattr(device, "agc_preset_power", 0),
        })
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
            kind = next(
                (label for label, table in (
                    ("wind", "wind_generator"), ("pv", "pv_generator"),
                    ("storage", "estorage"), ("diesel", "diesel_generator"),
                    ("load", "energyconsumer"),
                ) if device in (getattr(self.model, table, []) or [])),
                type(device).__name__,
            )
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
            "pMin": round(_number(getattr(item, "p_min", 0.0)), 3),
            "pMax": round(_number(getattr(item, "p_max", 0.0)), 3),
            "controlStep": round(_number(getattr(item, "p_step", getattr(item, "agc_control_step", 0.0))), 3),
            "controlDeadband": round(_number(getattr(item, "p_dead", getattr(item, "agc_control_deadband", 0.0))), 3),
            "presetPower": round(_number(getattr(item, "agc_preset_power", 0.0)), 3),
            "presetCurve": list(getattr(item, "agc_preset_curve", [])),
        }

    def _sync_from_database(self) -> Optional[sqlite3.Row]:
        """Refresh Web state from cycles written by another AGC process.

        The Web HMI and ``agc_ctrl.py`` can run as separate processes.  The
        calculation process therefore remains the source of telemetry and
        commands in SQLite; every state read mirrors the newest persisted
        cycle back into the Web model before rendering JSON.
        """
        latest = self.db.latest_cycle_row()
        if latest is None:
            return None
        persisted = self.db.history(1000)
        if persisted:
            self.history = persisted
            self.cycle = max(self.cycle, max(_integer(row.get("cycle", 0)) for row in persisted))
        mode = str(latest["control_mode"] or "")
        if mode in {"open_loop", "closed_loop"}:
            self.control_mode = mode
        for row in self.db.latest_device_samples():
            device = self._find_device(row["kind"], row["device_id"])
            if device is None:
                continue
            device.p_cur = _number(row["realtime_power"], _number(getattr(device, "p_cur", 0)))
            device.p_ctrl = _number(row["command_power"], _number(getattr(device, "p_ctrl", device.p_cur)))
            device.run_stat = _integer(row["run_status"], _integer(getattr(device, "run_stat", 0)))
            state = str(row["control_state"] or "remote")
            if state in {"remote", "local"}:
                device.agc_control_state = state
        env_map = {str(item.name): item for item in getattr(self.model, "env_para", []) or []}
        yc_map = {str(item.id): item for item in getattr(self.model, "yc", []) or []}
        for name, column in (("wind_speed", "wind_speed"), ("solor_irrad", "solar_irradiance"), ("env_temp", "env_temperature")):
            env = env_map.get(name)
            yc = yc_map.get(str(getattr(env, "yc_id", ""))) if env else None
            if yc is not None:
                yc.value = _number(latest[column], _number(yc.value))
        return latest

    def _apply_local_curves(self) -> None:
        now = datetime.now()
        point = now.hour * 4 + now.minute // 15
        for device in self._all_devices():
            if str(getattr(device, "agc_control_state", "remote")) != "local":
                continue
            curve = getattr(device, "agc_preset_curve", [])
            target = _number(curve[point], getattr(device, "agc_preset_power", 0.0)) if len(curve) >= 96 else _number(getattr(device, "agc_preset_power", 0.0))
            lower = _number(getattr(device, "p_min", 0.0))
            upper = _number(getattr(device, "p_max", max(target, lower)))
            if upper < lower:
                upper = max(target, lower)
            target = max(lower, min(upper, target))
            before = _number(getattr(device, "p_ctrl", getattr(device, "p_cur", 0.0)))
            before_set = _number(getattr(device, "p_set", before)) if hasattr(device, "p_set") else None
            device.p_ctrl = target
            if hasattr(device, "p_set"):
                device.p_set = target
            device.agc_preset_power = target
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
            point = self._snapshot(include_history=False)
            point["commands"] = self._record_commands(point["timestamp"])
            self.command_history.extend(point["commands"])
            self.command_history = self.command_history[-5000:]
            self.history.append(point)
            self.history = self.history[-1000:]
            self.db.save_cycle(point)
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

    def device_command(self, payload: Dict[str, Any], _return_snapshot: bool = True) -> Optional[Dict[str, Any]]:
        with self.lock:
            kind = str(payload.get("kind", ""))
            item_id = str(payload.get("id", ""))
            field = str(payload.get("field", "run_stat"))
            collections = {
                "wind": getattr(self.model, "wind_generator", []),
                "pv": getattr(self.model, "pv_generator", []),
                "storage": getattr(self.model, "estorage", []),
                "diesel": getattr(self.model, "diesel_generator", []),
                "load": getattr(self.model, "energyconsumer", []),
            }
            target = next(
                (item for item in collections.get(kind, []) if str(getattr(item, "id", "")) == item_id),
                None,
            )
            if target is None or field not in {
                "run_stat", "p_cur", "p_step", "p_dead", "control_step",
                "control_deadband", "p_min", "p_max", "control_state",
                "preset_power", "preset_curve",
            }:
                raise ValueError("设备或字段无效")
            value = payload.get("value")
            if field == "control_state":
                state = str(value).lower()
                if state not in {"remote", "local"}:
                    raise ValueError("受控状态必须是 remote 或 local")
                target.agc_control_state = state
            elif field == "preset_curve":
                if not isinstance(value, list) or len(value) != 96:
                    raise ValueError("默认功率曲线必须包含 96 个 15 分钟点")
                curve = [_number(item, float("nan")) for item in value]
                if any(not math.isfinite(item) for item in curve):
                    raise ValueError("默认功率曲线必须全部是有限数字")
                lower = _number(getattr(target, "p_min", 0.0))
                upper = _number(getattr(target, "p_max", 0.0))
                if upper > lower and any(item < lower or item > upper for item in curve):
                    raise ValueError(f"默认功率曲线必须位于 {lower:g} 至 {upper:g} MW 范围内")
                target.agc_preset_curve = curve
                now = datetime.now()
                target.agc_preset_power = target.agc_preset_curve[now.hour * 4 + now.minute // 15]
            elif field == "preset_power":
                converted = _number(value, float("nan"))
                if not math.isfinite(converted):
                    raise ValueError("默认功率必须是有限数字")
                lower = _number(getattr(target, "p_min", 0.0))
                upper = _number(getattr(target, "p_max", 0.0))
                if upper > lower and not lower <= converted <= upper:
                    raise ValueError(f"默认功率必须位于 {lower:g} 至 {upper:g} MW 范围内")
                target.agc_preset_power = converted
                target.agc_preset_curve = [converted] * 96
            else:
                actual_field = {
                    "control_step": "p_step",
                    "control_deadband": "p_dead",
                }.get(field, field)
                old = getattr(target, actual_field, 0)
                if field in {"control_step", "control_deadband", "p_min", "p_max"}:
                    converted = _number(value, float("nan"))
                    if not math.isfinite(converted):
                        raise ValueError(f"设备参数无效：{field}")
                    if field in {"control_step", "control_deadband"} and converted < 0:
                        raise ValueError(f"设备参数不能为负值：{field}")
                elif field == "run_stat":
                    converted = _integer(value, _integer(old))
                    if converted not in {0, 1}:
                        raise ValueError("运行状态只能是 0 或 1")
                else:
                    converted = self._cast_parameter(old, value)
                if field == "p_min" and converted > _number(getattr(target, "p_max", converted)):
                    raise ValueError("功率下限不能高于功率上限")
                if field == "p_max" and converted < _number(getattr(target, "p_min", converted)):
                    raise ValueError("功率上限不能低于功率下限")
                setattr(target, actual_field, converted)
                if field == "control_step":
                    target.agc_control_step = _number(value)
                elif field == "control_deadband":
                    target.agc_control_deadband = _number(value)
                if field == "run_stat" and hasattr(self.config, "send_device_run_state"):
                    if not self.config.send_device_run_state(target, converted):
                        setattr(target, actual_field, old)
                        raise ValueError("SCADA遥控下发失败，设备运行状态未修改")
            self._save_device_modes()
            self.config.logger.info("设备指令 %s/%s %s=%s", kind, item_id, field, value)
            return self._snapshot() if _return_snapshot else None

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

    def _device(self, kind: str, item: Any, include_curve: bool = True) -> Dict[str, Any]:
        soc = getattr(item, "soc_cur", None)
        if soc is not None:
            soc = _number(soc)
            soc = soc / 100 if soc > 1 else soc
        state = str(getattr(item, "agc_control_state", "remote"))
        effective_power = getattr(item, "p_ctrl", getattr(item, "p_cur", getattr(item, "p", 0))) if state == "local" else getattr(item, "p_cur", getattr(item, "p", 0))
        result = {
            "kind": kind,
            "id": _json_value(getattr(item, "id", "")),
            "name": str(getattr(item, "name", f"{kind}-{getattr(item, 'id', '')}")),
            "status": _integer(getattr(item, "run_stat", 0)),
            "power": round(_number(effective_power), 3),
            "maxPower": round(_number(getattr(item, "p_max", 0)), 3),
            "soc": None if soc is None else round(soc, 4),
            "minPower": round(_number(getattr(item, "p_min", 0)), 3),
            "step": round(_number(getattr(item, "p_step", 0)), 3),
            "controlState": state,
            "presetPower": round(_number(getattr(item, "agc_preset_power", 0.0)), 3),
        }
        if include_curve:
            result["presetCurve"] = list(getattr(item, "agc_preset_curve", []))
        return result

    def devices(self, include_curve: bool = True) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for kind, name in (
            ("wind", "wind_generator"),
            ("pv", "pv_generator"),
            ("storage", "estorage"),
            ("diesel", "diesel_generator"),
            ("load", "energyconsumer"),
        ):
            result.extend(self._device(kind, item, include_curve) for item in getattr(self.model, name, []))
        return result

    def _snapshot(self, include_history: bool = True, include_curves: bool = True) -> Dict[str, Any]:
        latest = self._sync_from_database()
        devices = self.devices(include_curves)
        renewable = sum(item["power"] for item in devices if item["kind"] in {"wind", "pv"})
        storage = sum(item["power"] for item in devices if item["kind"] == "storage")
        diesel = sum(item["power"] for item in devices if item["kind"] == "diesel")
        load = sum(item["power"] for item in devices if item["kind"] == "load")
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
            "devices": devices,
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
                "database": str(DB_PATH),
                "lastCycle": _integer(latest["cycle"], self.cycle) if latest else self.cycle,
                "lastCycleAt": str(latest["recorded_at"]) if latest else "",
                "running": bool(latest["running"]) if latest else self.running,
            },
        }
        if include_history:
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
                "simulation": self._snapshot(include_history=False)["weather"],
            }

    def logs(self, keyword: str = "", level: str = "", limit: int = 200) -> List[Dict[str, str]]:
        return self.db.logs(keyword, level, limit)

    def close(self) -> None:
        self.stop_event.set()


STATE = AGCState()


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
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            query = parse_qs(parsed.query)
            lite = query.get("lite", ["0"])[0].lower() in {"1", "true", "yes"}
            self._send_json(STATE._snapshot(include_history=not lite, include_curves=not lite))
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
            snapshot = STATE._snapshot(include_history=False)
            self._send_json({"ok": True, "time": _utc_now(), "database": str(DB_PATH), "tables": STATE.db.stats(), "scada": snapshot.get("scada", {})})
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
                kind = str(payload.get("kind", ""))
                item_id = payload.get("id", "")
                parameters = payload.get("parameters", {})
                if not isinstance(parameters, dict):
                    raise ValueError("设备参数必须是 JSON 对象")
                result = None
                for field, value in parameters.items():
                    result = STATE.device_command({"kind": kind, "id": item_id, "field": field, "value": value})
                self._send_json(result or STATE._snapshot())
                return
            self._error("接口不存在", HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._error(str(exc))
        except Exception as exc:  # keep API errors JSON-shaped for the HMI
            STATE.last_error = str(exc)
            self._error("服务器处理失败: " + str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> None:
    address = os.environ.get("AGC_WEB_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((address, PORT), HMIHandler)
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
