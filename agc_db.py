"""SQLite data access used by the AGC calculation backend.

The Web HMI owns the same schema, while this module keeps the calculation
process independent from the HTTP server.  Both processes can therefore read
and write the configured shared SQLite database without importing one another.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

if __package__:
    from .agc_schema import open_database
else:
    from agc_schema import open_database


DEFAULT_DB_PATH = Path(os.environ.get("AGC_DB", r"D:\codex\sqldb\realtime\agc\agc.db"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class AGCDataStore:
    """Persistence adapter for calculation-side AGC state."""

    DEVICE_TABLES = (
        ("wind", "wind_generator"),
        ("pv", "pv_generator"),
        ("storage", "estorage"),
        ("diesel", "diesel_generator"),
        ("load", "energyconsumer"),
    )

    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self.lock = threading.RLock()
        self.path, self.conn = open_database(path)

    @classmethod
    def for_model(cls, model_path: Optional[Path] = None, path: Optional[Path] = None) -> Optional["AGCDataStore"]:
        """Open the configured DB when available, creating it for explicit paths."""
        target = Path(path) if path is not None else DEFAULT_DB_PATH
        if path is None and not target.exists() and model_path is None:
            return None
        return cls(target)

    def close(self) -> None:
        self.conn.close()

    def latest_cycle(self) -> int:
        row = self._fetchone("SELECT COALESCE(MAX(cycle), 0) AS cycle FROM control_cycles")
        return _integer(row["cycle"] if row else 0)

    def latest_cycle_row(self) -> Optional[sqlite3.Row]:
        """Return the newest persisted control cycle for cross-process sync."""
        return self._fetchone("SELECT * FROM control_cycles ORDER BY cycle DESC LIMIT 1")

    def latest_samples(self) -> List[sqlite3.Row]:
        """Return one newest device sample per device."""
        return self._fetchall(
            """SELECT s.* FROM device_samples s
               JOIN (SELECT kind, device_id, MAX(cycle) AS max_cycle
                     FROM device_samples GROUP BY kind, device_id) latest
                 ON latest.kind=s.kind AND latest.device_id=s.device_id AND latest.max_cycle=s.cycle
               ORDER BY s.kind, s.device_id"""
        )

    def _fetchall(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def _fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchone()

    @staticmethod
    def _device_kind(device: Any) -> str:
        name = type(device).__name__
        return {
            "wind_generator": "wind",
            "pv_generator": "pv",
            "estorage": "storage",
            "diesel_generator": "diesel",
            "energyconsumer": "load",
        }.get(name, name)

    def _iter_devices(self, model: Any):
        for kind, table in self.DEVICE_TABLES:
            for device in getattr(model, table, []) or []:
                yield kind, device

    @staticmethod
    def _cast(old: Any, value: Any) -> Any:
        if isinstance(old, int) and not isinstance(old, bool):
            return _integer(value, old)
        if isinstance(old, float):
            return _number(value, old)
        return value

    def _save_device(self, kind: str, device: Any) -> None:
        now = _now()
        self.conn.execute(
            """INSERT INTO devices(kind, device_id, name, control_state, run_status, p_min, p_max,
                 control_step, control_deadband, preset_power, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(kind, device_id) DO UPDATE SET name=excluded.name,
                 control_state=excluded.control_state, run_status=excluded.run_status,
                 p_min=excluded.p_min, p_max=excluded.p_max, control_step=excluded.control_step,
                 control_deadband=excluded.control_deadband, preset_power=excluded.preset_power,
                 updated_at=excluded.updated_at""",
            (
                kind, str(getattr(device, "id", "")), str(getattr(device, "name", "")),
                str(getattr(device, "agc_control_state", "remote")), _integer(getattr(device, "run_stat", 0)),
                _number(getattr(device, "p_min", 0)), _number(getattr(device, "p_max", 0)),
                _number(getattr(device, "p_step", 0)), _number(getattr(device, "p_dead", 0)),
                _number(getattr(device, "agc_preset_power", getattr(device, "p_cur", 0))), now,
            ),
        )

    def _save_curve(self, kind: str, device: Any) -> None:
        curve = list(getattr(device, "agc_preset_curve", []))
        if len(curve) != 96:
            curve = [_number(getattr(device, "agc_preset_power", getattr(device, "p_cur", 0)))] * 96
            device.agc_preset_curve = curve
        now = _now()
        self.conn.execute("DELETE FROM device_curves WHERE kind=? AND device_id=?", (kind, str(getattr(device, "id", ""))))
        self.conn.executemany(
            "INSERT INTO device_curves(kind, device_id, point_index, minute_of_day, power, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(kind, str(getattr(device, "id", "")), index, index * 15, _number(value), now) for index, value in enumerate(curve)],
        )

    def _settings(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for row in self._fetchall("SELECT name, value, value_type FROM system_settings"):
            value: Any = row["value"]
            if row["value_type"] == "int":
                value = _integer(value)
            elif row["value_type"] == "float":
                value = _number(value)
            result[str(row["name"])] = value
        return result

    def apply_model(self, model: Any) -> None:
        """Load DB configuration into an E-file model and seed missing rows."""
        settings = self._settings()
        for para in getattr(model, "para", []) or []:
            name = str(getattr(para, "name", ""))
            if name in settings:
                para.value = self._cast(getattr(para, "value", ""), settings[name])
        if settings.get("CONTROL_MODE") in {"open_loop", "closed_loop"}:
            model.agc_control_mode = settings["CONTROL_MODE"]

        for kind, device in self._iter_devices(model):
            device_id = str(getattr(device, "id", ""))
            row = self._fetchone("SELECT * FROM devices WHERE kind=? AND device_id=?", (kind, device_id))
            if row is not None:
                device.agc_control_state = str(row["control_state"] or "remote")
                device.run_stat = _integer(row["run_status"], _integer(getattr(device, "run_stat", 0)))
                for field, column in (("p_min", "p_min"), ("p_max", "p_max"), ("p_step", "control_step"), ("p_dead", "control_deadband")):
                    if hasattr(device, field):
                        setattr(device, field, _number(row[column], _number(getattr(device, field, 0))))
                device.agc_control_step = _number(getattr(device, "p_step", 0))
                device.agc_control_deadband = _number(getattr(device, "p_dead", 0))
                device.agc_preset_power = _number(row["preset_power"], _number(getattr(device, "p_cur", 0)))
            else:
                device.agc_control_state = str(getattr(device, "agc_control_state", "remote"))
                device.agc_preset_power = _number(getattr(device, "p_cur", 0))
                self._save_device(kind, device)

            curve_rows = self._fetchall("SELECT power FROM device_curves WHERE kind=? AND device_id=? ORDER BY point_index", (kind, device_id))
            curve = [_number(item["power"]) for item in curve_rows]
            if len(curve) == 96:
                device.agc_preset_curve = curve
            else:
                device.agc_preset_curve = [_number(getattr(device, "agc_preset_power", 0))] * 96
                self._save_curve(kind, device)
        self.conn.commit()

    def apply_latest_runtime(self, model: Any) -> None:
        """Apply latest DB telemetry/commands to the in-memory calculation model."""
        rows = self._fetchall(
            """SELECT s.* FROM device_samples s
               JOIN (SELECT kind, device_id, MAX(cycle) AS max_cycle FROM device_samples GROUP BY kind, device_id) latest
               ON latest.kind=s.kind AND latest.device_id=s.device_id AND latest.max_cycle=s.cycle
               """
        )
        by_key = {(str(row["kind"]), str(row["device_id"])): row for row in rows}
        for kind, device in self._iter_devices(model):
            row = by_key.get((kind, str(getattr(device, "id", ""))))
            if row is None:
                continue
            device.p_cur = _number(row["realtime_power"], _number(getattr(device, "p_cur", 0)))
            device.p_ctrl = _number(row["command_power"], _number(getattr(device, "p_ctrl", device.p_cur)))
            device.run_stat = _integer(row["run_status"], _integer(getattr(device, "run_stat", 0)))

    def _latest_environment(self, model: Any) -> Optional[sqlite3.Row]:
        return self._fetchone("SELECT * FROM control_cycles ORDER BY cycle DESC LIMIT 1")

    def apply_latest_environment(self, model: Any) -> None:
        row = self._latest_environment(model)
        if row is None:
            return
        values = {"wind_speed": row["wind_speed"], "solor_irrad": row["solar_irradiance"], "env_temp": row["env_temperature"]}
        env_map = {str(item.name): item for item in getattr(model, "env_para", []) or []}
        yc_map = {str(item.id): item for item in getattr(model, "yc", []) or []}
        for name, value in values.items():
            env = env_map.get(name)
            yc = yc_map.get(str(getattr(env, "yc_id", ""))) if env else None
            if yc is not None:
                yc.value = value

    def record_cycle(self, model: Any, config: Any) -> None:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                # A Web worker and a standalone calculation process may write at the
                # same time.  Keep the SQLite cycle key monotonic so neither process
                # silently replaces the other process' history point.
                cycle = max(_integer(getattr(config, "control_count", 0)), self.latest_cycle() + 1)
                recorded_at = _now()
                devices = list(self._iter_devices(model))
                groups = {"wind": [], "pv": [], "storage": [], "diesel": [], "load": []}
                for kind, device in devices:
                    groups[kind].append(device)
                power = lambda device: _number(getattr(device, "p_cur", getattr(device, "p", 0)))
                renewable = sum(power(d) for d in groups["wind"] + groups["pv"])
                storage = sum(power(d) for d in groups["storage"])
                diesel = sum(power(d) for d in groups["diesel"])
                load = sum(power(d) for d in groups["load"])
                wind_speed = _number(getattr(getattr(model, "wind_speed", None), "value", 0))
                irradiance = _number(getattr(getattr(model, "solor_irrad", None), "value", 0))
                env_temp = _number(getattr(getattr(model, "env_temp", None), "value", 0))
                raw_mode = str(getattr(model, "agc_control_mode", "closed_loop"))
                control_mode = "open_loop" if raw_mode.startswith("open_loop") else "closed_loop"
                self.conn.execute(
                    """INSERT INTO control_cycles(cycle, recorded_at, control_mode, running,
                         load_power, renewable_power, storage_power, diesel_power, balance_power,
                         wind_speed, solar_irradiance, env_temperature)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (cycle, recorded_at, control_mode, int(bool(getattr(config, "is_running", True))), load, renewable, storage, diesel, renewable + storage + diesel - load, wind_speed, irradiance, env_temp),
                )
                self.conn.executemany(
                    """INSERT INTO device_samples(cycle, recorded_at, kind, device_id, name,
                         control_state, run_status, realtime_power, command_power, preset_power,
                         control_step, control_deadband)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (cycle, recorded_at, kind, str(getattr(device, "id", "")), str(getattr(device, "name", "")), str(getattr(device, "agc_control_state", "remote")), _integer(getattr(device, "run_stat", 0)), power(device), _number(getattr(device, "p_ctrl", power(device))), _number(getattr(device, "agc_preset_power", 0)), _number(getattr(device, "p_step", 0)), _number(getattr(device, "p_dead", 0)))
                        for kind, device in devices
                    ],
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise



class SQLiteLogHandler(logging.Handler):
    """Forward AGC logger records into the shared event log table."""

    def __init__(self, store: AGCDataStore) -> None:
        super().__init__(logging.INFO)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self.store.lock:
                self.store.conn.execute(
                    "INSERT INTO event_logs(logged_at, level, message) VALUES (?, ?, ?)",
                    (
                        datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
                        record.levelname,
                        self.format(record),
                    ),
                )
                self.store.conn.commit()
        except Exception:
            self.handleError(record)
