"""AGC Web HMI server.

The server intentionally uses only Python's standard library so it can run in
the same environment as the existing AGC controller.  It keeps the controller
model in memory, exposes a small JSON API, and serves the browser HMI from
``web/public``.
"""

from __future__ import annotations

import json
import mimetypes
import os
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


WEB_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = WEB_DIR / "public"
DATA_DIR = WEB_DIR / "data"
MODEL_FILE = Path(os.environ.get("AGC_MODEL", AGC_DIR / "model_real.e"))
PORT = int(os.environ.get("AGC_WEB_PORT", "8765"))


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


def _json_value(value: Any) -> Any:
    """Convert model values into JSON-safe scalar values."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        self.do_one_ctrl, self.agc_ctrl_stg_check, self.do_renew_gen_fur_calc = (
            agc_ctrl._load_control_functions()
        )
        self.history: List[Dict[str, Any]] = self._load_history()
        self.command_history: List[Dict[str, Any]] = self._load_command_history()
        self._apply_saved_settings()
        self._load_device_modes()
        self.worker = threading.Thread(target=self._worker_loop, name="agc-web-loop", daemon=True)
        self.worker.start()

    # ---------- persistence and control loop ----------

    def _load_history(self) -> List[Dict[str, Any]]:
        if not self.history_path.exists():
            return []
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            return data[-1000:] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_history(self) -> None:
        temp_path = self.history_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(self.history[-1000:], ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.history_path)

    def _load_command_history(self) -> List[Dict[str, Any]]:
        if not self.command_history_path.exists():
            return []
        try:
            data = json.loads(self.command_history_path.read_text(encoding="utf-8"))
            return data[-5000:] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_command_history(self) -> None:
        temp_path = self.command_history_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self.command_history[-5000:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.command_history_path)

    def _apply_saved_settings(self) -> None:
        if not self.settings_path.exists():
            return
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        params = data.get("params", {}) if isinstance(data, dict) else {}
        saved_mode = data.get("control_mode") if isinstance(data, dict) else None
        if saved_mode in {"open_loop", "closed_loop"}:
            self.control_mode = saved_mode
        for para in getattr(self.model, "para", []):
            if para.name in params:
                para.value = self._cast_parameter(para.value, params[para.name])
        self._refresh_periods()

    def _all_devices(self) -> Iterable[Any]:
        for table_name in ("wind_generator", "pv_generator", "estorage", "diesel_generator", "energyconsumer"):
            yield from getattr(self.model, table_name, []) or []

    def _load_device_modes(self) -> None:
        saved: Dict[str, Any] = {}
        if self.device_modes_path.exists():
            try:
                payload = json.loads(self.device_modes_path.read_text(encoding="utf-8"))
                saved = payload if isinstance(payload, dict) else {}
            except (OSError, ValueError):
                saved = {}
        for device in self._all_devices():
            key = self._device_key(device)
            item = saved.get(key, {}) if isinstance(saved.get(key, {}), dict) else {}
            state = str(item.get("control_state", "remote")).lower()
            device.agc_control_state = state if state in {"remote", "local"} else "remote"
            for field in ("p_step", "p_dead", "p_min", "p_max", "run_stat"):
                if field in item:
                    old = getattr(device, field, 0)
                    setattr(device, field, self._cast_parameter(old, item[field]))
            device.agc_control_step = _number(getattr(device, "p_step", 0.0))
            device.agc_control_deadband = _number(getattr(device, "p_dead", 0.0))
            device.agc_preset_power = _number(item.get("preset_power", getattr(device, "p_cur", 0.0)))
            curve = item.get("preset_curve")
            if not isinstance(curve, list) or len(curve) not in {24, 96}:
                curve = [device.agc_preset_power] * 96
            elif len(curve) == 24:
                # Migrate earlier hourly presets to the new 15-minute format.
                curve = [value for value in curve for _ in range(4)]
            device.agc_preset_curve = [_number(value, device.agc_preset_power) for value in curve]

    @staticmethod
    def _device_key(device: Any) -> str:
        return f"{type(device).__name__}:{getattr(device, 'id', '')}"

    def _save_device_modes(self) -> None:
        payload = {}
        for device in self._all_devices():
            payload[self._device_key(device)] = {
                "control_state": str(getattr(device, "agc_control_state", "remote")),
                "p_step": _json_value(getattr(device, "p_step", 0.0)),
                "p_dead": _json_value(getattr(device, "p_dead", 0.0)),
                "p_min": _json_value(getattr(device, "p_min", 0.0)),
                "p_max": _json_value(getattr(device, "p_max", 0.0)),
                "run_stat": _json_value(getattr(device, "run_stat", 0)),
                "preset_power": _number(getattr(device, "agc_preset_power", 0.0)),
                "preset_curve": list(getattr(device, "agc_preset_curve", [])),
            }
        temp_path = self.device_modes_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.device_modes_path)

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
            device.p_ctrl = target
            if hasattr(device, "p_set"):
                device.p_set = target
            device.agc_preset_power = target
            self.config.logger.info("本地曲线控制 %s point=%s p_ctrl=%.3f", getattr(device, "name", ""), point, target)

    def set_control_mode(self, mode: str) -> Dict[str, Any]:
        mode = str(mode or "").lower()
        if mode not in {"open_loop", "closed_loop"}:
            raise ValueError("控制模式必须是 open_loop 或 closed_loop")
        with self.lock:
            self.control_mode = mode
            self._save_settings()
            self.config.logger.info("Web 控制模式切换为：%s", "闭环" if mode == "closed_loop" else "开环")
            return self._snapshot()

    def _save_settings(self) -> None:
        values = {str(item.name): _json_value(item.value) for item in getattr(self.model, "para", [])}
        temp_path = self.settings_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps({"updated_at": _utc_now(), "control_mode": self.control_mode, "params": values}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.settings_path)

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
            self._save_history()
            self._save_command_history()
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

    def device_command(self, payload: Dict[str, Any]) -> Dict[str, Any]:
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
                target.agc_preset_curve = [_number(item) for item in value]
                now = datetime.now()
                target.agc_preset_power = target.agc_preset_curve[now.hour * 4 + now.minute // 15]
            elif field == "preset_power":
                target.agc_preset_power = _number(value, _number(getattr(target, "p_cur", 0.0)))
                target.agc_preset_curve = [target.agc_preset_power] * 96
            else:
                actual_field = {
                    "control_step": "p_step",
                    "control_deadband": "p_dead",
                }.get(field, field)
                old = getattr(target, actual_field, 0)
                if field in {"control_step", "control_deadband", "p_min", "p_max"}:
                    converted = _number(value, _number(old))
                elif field == "run_stat":
                    converted = _integer(value, _integer(old))
                else:
                    converted = self._cast_parameter(old, value)
                setattr(target, actual_field, converted)
                if field == "control_step":
                    target.agc_control_step = _number(value)
                elif field == "control_deadband":
                    target.agc_control_deadband = _number(value)
            self._save_device_modes()
            self.config.logger.info("设备指令 %s/%s %s=%s", kind, item_id, field, value)
            return self._snapshot()

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
                }
                for row in self.command_history
                if f"{row.get('kind')}:{row.get('id')}" == device_key
            ][-240:]
            return {
                "device": self._device_parameters(kind, device),
                "history": history,
                "commands": history[-120:],
            }

    # ---------- JSON views ----------

    def _device(self, kind: str, item: Any) -> Dict[str, Any]:
        soc = getattr(item, "soc_cur", None)
        if soc is not None:
            soc = _number(soc)
            soc = soc / 100 if soc > 1 else soc
        state = str(getattr(item, "agc_control_state", "remote"))
        effective_power = getattr(item, "p_ctrl", getattr(item, "p_cur", getattr(item, "p", 0))) if state == "local" else getattr(item, "p_cur", getattr(item, "p", 0))
        return {
            "kind": kind,
            "id": _json_value(getattr(item, "id", "")),
            "name": str(getattr(item, "name", f"{kind}-{getattr(item, 'id', '')}")),
            "status": _integer(getattr(item, "run_stat", 0)),
            "power": round(_number(effective_power), 3),
            "maxPower": round(_number(getattr(item, "p_max", 0)), 3),
            "soc": None if soc is None else round(soc, 4),
            "step": round(_number(getattr(item, "p_step", 0)), 3),
            "controlState": state,
            "presetPower": round(_number(getattr(item, "agc_preset_power", 0.0)), 3),
            "presetCurve": list(getattr(item, "agc_preset_curve", [])),
        }

    def devices(self) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for kind, name in (
            ("wind", "wind_generator"),
            ("pv", "pv_generator"),
            ("storage", "estorage"),
            ("diesel", "diesel_generator"),
            ("load", "energyconsumer"),
        ):
            result.extend(self._device(kind, item) for item in getattr(self.model, name, []))
        return result

    def _snapshot(self, include_history: bool = True) -> Dict[str, Any]:
        devices = self.devices()
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
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        keyword = keyword.lower().strip()
        level = level.upper().strip()
        result = []
        for line in reversed(lines):
            if keyword and keyword not in line.lower():
                continue
            if level and f" {level} " not in line:
                continue
            parts = line.split(" - ", 2)
            result.append(
                {
                    "timestamp": parts[0] if parts else "",
                    "level": parts[1] if len(parts) > 1 else "INFO",
                    "message": parts[2] if len(parts) > 2 else line,
                }
            )
            if len(result) >= max(1, min(limit, 1000)):
                break
        return result

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
            self._send_json(STATE._snapshot())
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
            self._send_json({"items": STATE.history[-limit:]})
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
            rows = STATE.command_history
            if kind:
                rows = [row for row in rows if row.get("kind") == kind]
            if item_id:
                rows = [row for row in rows if str(row.get("id")) == str(item_id)]
            self._send_json({"items": rows[-limit:]})
            return
        if parsed.path == "/api/health":
            self._send_json({"ok": True, "time": _utc_now()})
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
                    result = STATE.run_cycle()
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
