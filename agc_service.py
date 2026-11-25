"""秦岭 AGC 实时控制决策核心服务与后台调度器。

职责：
- 周期只读采样 ``scdfe_d.sqlite3``，维护最新快照与测点滚动跟踪历史；
- 托管 ``RenewableControlSettings``、策略预设、储能充放电降额曲线及开环/闭环状态；
- 按控制周期或单次计算触发 ``calculate_renewable_control_plan``，将控制参数、
  计算策略、设备明细、综合趋势曲线、控制日志与指令审计统一持久化至 ``agc.sqlite3``；
- 闭环模式及人工确认遥控/遥调经由 ``QinlingDataAdapter`` 向 SCADA 四遥通道下发。
"""

from __future__ import annotations

import copy
import math
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from agc_datasources import (
    DEFAULT_AGC_DB,
    DEFAULT_MODEL_DB,
    DEFAULT_SCADA_DB,
    DEFAULT_SVG_PATH,
    SCDFE_D_CTRL_STATUS_DISABLED,
    SCDFE_D_CTRL_STATUS_FAILED,
    SCDFE_D_CTRL_STATUS_IDLE,
    SCDFE_D_CTRL_STATUS_PENDING,
    SCDFE_D_CTRL_STATUS_SUCCESS,
    SCDFE_D_CTRL_STATUS_UNCONFIGURED,
    QinlingDataAdapter,
)
from agc_engine import (
    RenewableControlSettings,
    calculate_renewable_control_plan,
    evaluate_device_curve,
    prepare_resource_topology_static_context,
    resolve_snapshot_minute_of_day,
)
from agc_store import (
    AGCRealtimeStore,
    DEFAULT_BACKEND_RUNTIME_SETTINGS,
    DEFAULT_WEB_RUNTIME_SETTINGS,
)

# 策略预设方案（与学员台对齐）
STRATEGY_PRESETS: Dict[str, Dict[str, Any]] = {
    "standard": {},
    "aggressive_renewable": {
        "step_coefficient": 0.06,
        "storage_step_ratio": 0.05,
        "grid_forming_storage_protection_ratio": 0.04,
        "diesel_power_protection_ratio": 0.02,
        "soc_deadband": 0.04,
        "electrolyzer_storage_soc_start_minimum": 0.60,
        "optimization_renewable_curtailment_weight": 2.0,
        "optimization_diesel_output_weight": 1.2,
    },
    "conservative_protection": {
        "step_coefficient": 0.02,
        "storage_step_ratio": 0.02,
        "grid_forming_storage_protection_ratio": 0.08,
        "diesel_power_protection_ratio": 0.05,
        "soc_deadband": 0.08,
        "electrolyzer_storage_soc_start_minimum": 0.75,
        "electrolyzer_storage_soc_stop_maximum": 0.35,
        "fuel_cell_storage_soc_limit": 0.35,
        "optimization_renewable_curtailment_weight": 0.8,
        "optimization_diesel_output_weight": 1.0,
    },
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _settings_to_dict(settings: RenewableControlSettings) -> Dict[str, Any]:
    """将控制参数对象序列化为统一的 snake_case 字典（含储能充放电降额曲线）。"""
    out: Dict[str, Any] = {}
    for field_name in settings.__dataclass_fields__:
        val = getattr(settings, field_name)
        if val is None:
            continue
        if field_name in {"storage_charge_derating_curve", "storage_discharge_derating_curve"}:
            out[field_name] = [
                {"soc": float(pt[0]), "power_ratio": float(pt[1])}
                for pt in val
            ]
        else:
            out[field_name] = val
    return out


class AGCRealtimeService:
    """秦岭微电网 AGC 实时算法后台与 Web 服务共享控制器。"""

    def __init__(
        self,
        *,
        model_db: str | Path = DEFAULT_MODEL_DB,
        scada_db: str | Path = DEFAULT_SCADA_DB,
        svg_path: str | Path = DEFAULT_SVG_PATH,
        agc_db: str | Path = DEFAULT_AGC_DB,
        command_endpoint: Optional[str | Path] = None,
        command_sender: Optional[Callable[[str, int, int, Any], bool]] = None,
        start_background: bool = True,
        ack_timeout_seconds: float = 15.0,
        ack_poll_interval_seconds: float = 0.2,
    ) -> None:
        self.adapter = QinlingDataAdapter(
            model_db=model_db,
            scada_db=scada_db,
            svg_path=svg_path,
            command_endpoint=command_endpoint,
            command_sender=command_sender,
        )
        self.store = AGCRealtimeStore(agc_db)
        self.lock = threading.RLock()
        self.started_at = _now_iso()

        # 从 SQLite 恢复配置
        cfg = self.store.load_control_config()
        self.preset: str = cfg["preset"]
        self.loop_mode: str = "closed" if cfg["loop_mode"] == "closed" else "open"
        self.auto_enabled: bool = False
        self.settings: RenewableControlSettings = self._build_settings(cfg["parameters"])
        # 首次启动将默认控制参数落盘，保证数据库内配置完整可读
        if not cfg["parameters"]:
            self.store.save_control_parameters(_settings_to_dict(self.settings), preset=self.preset)

        self.runtime_settings: Dict[str, Any] = self.store.load_runtime_settings()
        if self.runtime_settings["backend"].get("auto_start_on_boot"):
            self.auto_enabled = bool(cfg["auto_enabled"])

        self._topology_ctx: Any = None
        self._runtime_memory: Dict[str, Any] = {}
        self._last_snapshot: Optional[Dict[str, Any]] = None
        self._last_age_seconds: float = 0.0
        self._last_scada_error: str = ""
        self._last_plan: Optional[Dict[str, Any]] = None
        self._last_cycle: int = self.store.latest_cycle()
        self._last_calculated_at: str = ""
        self._last_dispatched_at: str = ""
        self._status_text: str = "待命（可点击单次计算或启动自动控制）"
        self._last_control_monotonic: float = 0.0
        self._meas_history: Dict[Tuple[str, str, str], Deque[Dict[str, Any]]] = {}
        self._meas_history_limit: int = 600
        self._latest_manual_commands: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

        # 初始化时立即做一次只读采集（不写库不发令）
        self.refresh_telemetry()

        # 如果数据库已有历史策略，恢复最近一次展示
        persisted_latest = self.store.get_latest_strategy()
        if persisted_latest is not None:
            self._last_calculated_at = str(persisted_latest.get("recorded_at") or "")
            self._status_text = str(persisted_latest.get("summary_text") or self._status_text)

        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        # 独立指令回执监听线程状态（异步等待 scdfe_d 写回 *_cmd_status / *_cmd_ack_time）
        self._ack_timeout_seconds: float = max(0.5, float(ack_timeout_seconds))
        self._ack_poll_interval_seconds: float = max(0.05, float(ack_poll_interval_seconds))
        self._ack_lock = threading.Lock()
        self._ack_stop_event = threading.Event()
        self._ack_wake_event = threading.Event()
        self._pending_acks: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
        self._ack_watcher_thread: Optional[threading.Thread] = None
        self.start_ack_watcher()

        if start_background:
            self.start_background_loop()

    def _build_settings(self, raw_params: Mapping[str, Any]) -> RenewableControlSettings:
        base = RenewableControlSettings()
        if not raw_params:
            return base
        try:
            return base.updated(dict(raw_params))
        except Exception:
            return base

    def start_background_loop(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._background_loop,
            name="agc-realtime-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def stop_background_loop(self) -> None:
        self._stop_event.set()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3.0)

    def start_ack_watcher(self) -> None:
        """启动独立的指令回执等待守护线程（空闲休眠零开销，有在途指令时轮询 scdfe_d 回执状态）。"""
        if self._ack_watcher_thread is not None and self._ack_watcher_thread.is_alive():
            return
        self._ack_stop_event.clear()
        self._ack_watcher_thread = threading.Thread(
            target=self._ack_watcher_loop,
            name="agc-cmd-ack-watcher",
            daemon=True,
        )
        self._ack_watcher_thread.start()

    def stop_ack_watcher(self) -> None:
        self._ack_stop_event.set()
        self._ack_wake_event.set()
        if self._ack_watcher_thread is not None and self._ack_watcher_thread.is_alive():
            self._ack_watcher_thread.join(timeout=3.0)

    def close(self) -> None:
        self.stop_background_loop()
        self.stop_ack_watcher()
        self.store.close()

    def _register_pending_ack(self, entry: Dict[str, Any]) -> None:
        """将在途指令登记至独立回执监听队列；同点位旧在途指令标记为 superseded。"""
        slot = entry["slot"]
        replaced: Optional[Dict[str, Any]] = None
        with self._ack_lock:
            replaced = self._pending_acks.get(slot)
            self._pending_acks[slot] = entry
        if replaced is not None:
            self._finalize_ack_entry(
                replaced,
                final_status="superseded",
                final_msg="已被同点位更新指令覆盖",
                log_event=False,
            )
        self._ack_wake_event.set()

    def _finalize_ack_entry(
        self,
        entry: Mapping[str, Any],
        *,
        final_status: str,
        final_msg: str,
        log_event: bool = True,
    ) -> None:
        rec_id = int(entry.get("record_id") or 0)
        cycle = int(entry.get("cycle") or 0)
        origin = str(entry.get("origin") or "automatic")
        dev_type = str(entry.get("dev_type") or "")
        dev_name = str(entry.get("dev_name") or "")
        set_type = str(entry.get("set_type") or "")

        if rec_id > 0:
            try:
                self.store.update_command_record_status(
                    rec_id,
                    status=final_status,
                    message=final_msg,
                )
            except Exception:
                pass
        if origin == "automatic" and cycle > 0:
            try:
                self.store.update_strategy_command_dispatch_status(
                    cycle=cycle,
                    dev_type=dev_type,
                    dev_name=dev_name,
                    set_type=set_type,
                    dispatch_status=final_status,
                    dispatch_message=final_msg,
                )
            except Exception:
                pass
        elif origin == "manual":
            with self.lock:
                cur_rec = self._latest_manual_commands.get((dev_type, dev_name, set_type))
                if cur_rec is not None and int(cur_rec.get("cmd_time_ms") or 0) == int(entry.get("cmd_time_ms") or 0):
                    cur_rec["status"] = final_status
                    cur_rec["message"] = final_msg

        if final_status == "dispatched":
            with self.lock:
                self._last_dispatched_at = _now_iso()

        if log_event:
            is_ok = final_status == "dispatched"
            level = "info" if is_ok else "warn"
            try:
                self.store.append_runtime_log(
                    level=level,
                    log_type="command_ack",
                    target=f"{dev_name} ({set_type})",
                    result="执行成功" if is_ok else final_status,
                    summary=f"{'自动' if origin == 'automatic' else '人工'}指令={entry.get('value')}｜{final_msg}",
                )
                if not is_ok:
                    self.store.append_control_log(
                        level="warn",
                        event_type="command_ack",
                        cycle=cycle,
                        subject=f"指令回执异常：{dev_name} ({set_type})",
                        summary=final_msg,
                    )
            except Exception:
                pass

    def _ack_watcher_loop(self) -> None:
        """独立线程主循环：只读轮询 scdfe_d.sqlite3 中在途指令的返回状态并异步更新审计与策略表。"""
        while not self._ack_stop_event.is_set():
            with self._ack_lock:
                pending_items = dict(self._pending_acks)
            if not pending_items:
                self._ack_wake_event.clear()
                self._ack_wake_event.wait(timeout=1.0)
                continue

            try:
                states = self.adapter.read_scdfe_d_command_states(list(pending_items.keys()))
            except Exception:
                states = {}

            now_mono = time.monotonic()
            backend_cfg = self.runtime_settings.get("backend", {})
            cfg_timeout = backend_cfg.get(
                "command_ack_timeout_seconds",
                backend_cfg.get("scada_command_timeout_seconds", self._ack_timeout_seconds),
            )
            # 若构造时显式指定了更短的测试超时（<15s），优先保留构造值
            timeout_sec = max(
                0.5,
                float(self._ack_timeout_seconds if self._ack_timeout_seconds < 15.0 else cfg_timeout),
            )
            resolved: List[Tuple[Tuple[str, int, str], Dict[str, Any], str, str]] = []

            for slot, entry in pending_items.items():
                st = states.get(slot)
                expected_time = int(entry.get("cmd_time_ms") or 0)
                elapsed_ms = int(round((now_mono - float(entry.get("submitted_mono") or now_mono)) * 1000.0))

                if st is not None:
                    live_time = st.get("cmd_time")
                    live_status = int(st.get("cmd_status") or 0)
                    live_ack = st.get("cmd_ack_time")

                    # 若库内时标已经大于本条写入时标，说明已被更新指令覆写
                    if live_time is not None and expected_time > 0 and int(live_time) > expected_time:
                        resolved.append((slot, entry, "superseded", f"已被更新指令覆盖 (new_time={live_time})"))
                        continue

                    if live_status == SCDFE_D_CTRL_STATUS_SUCCESS:
                        ack_note = f"ack={live_ack}" if live_ack else f"{elapsed_ms}ms"
                        resolved.append((slot, entry, "dispatched", f"scdfe_d 回执：执行成功 ({ack_note})"))
                        continue
                    if live_status == SCDFE_D_CTRL_STATUS_FAILED:
                        resolved.append((slot, entry, "failed", f"scdfe_d 回执：执行失败 ({elapsed_ms}ms)"))
                        continue
                    if live_status == SCDFE_D_CTRL_STATUS_UNCONFIGURED:
                        resolved.append((slot, entry, "unconfigured", "scdfe_d 回执：控调点未配置有效 RTU/点号"))
                        continue
                    if live_status == SCDFE_D_CTRL_STATUS_DISABLED:
                        resolved.append((slot, entry, "disabled", "scdfe_d 回执：控制点已禁用闭锁"))
                        continue
                    if live_status == SCDFE_D_CTRL_STATUS_IDLE and live_ack is not None and live_ack >= expected_time:
                        resolved.append((slot, entry, "dispatched", f"scdfe_d 回执完成 (ack={live_ack})"))
                        continue

                if now_mono - float(entry.get("submitted_mono") or now_mono) >= timeout_sec:
                    resolved.append((slot, entry, "timeout", f"等待 scdfe_d 回执超时 ({timeout_sec:.1f}s)"))

            for slot, entry, final_status, final_msg in resolved:
                removed = False
                with self._ack_lock:
                    cur = self._pending_acks.get(slot)
                    if cur is entry:
                        self._pending_acks.pop(slot, None)
                        removed = True
                if removed:
                    self._finalize_ack_entry(
                        entry,
                        final_status=final_status,
                        final_msg=final_msg,
                        log_event=(final_status != "superseded"),
                    )

            self._ack_stop_event.wait(timeout=self._ack_poll_interval_seconds)

    def _background_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.refresh_telemetry()
                with self.lock:
                    auto_on = self.auto_enabled
                    interval = max(1.0, float(self.settings.interval_seconds))
                    last_run = self._last_control_monotonic
                    refresh_sec = max(
                        0.3,
                        float(self.runtime_settings["backend"].get("backend_refresh_seconds", 1.0)),
                    )
                now_mono = time.monotonic()
                if auto_on and (now_mono - last_run >= interval):
                    self.run_control_cycle(trigger="auto")
            except Exception as exc:
                self.store.append_runtime_log(
                    level="error",
                    log_type="backend",
                    target="AGC 后台循环",
                    result="异常",
                    summary=f"后台周期执行异常: {exc}",
                )
                refresh_sec = 2.0
            self._stop_event.wait(timeout=refresh_sec)

    # ------------------------------------------------------------------
    # 实时数据采集与测点历史
    # ------------------------------------------------------------------

    def refresh_telemetry(self) -> Dict[str, Any]:
        """只读拉取一次秦岭实时库，刷新内存快照与测点跟踪历史。"""
        snapshot, age_seconds, err = self.adapter.read_snapshot()
        epoch_ms = int(round(time.time() * 1000))
        time_label = snapshot.get("clock", {}).get("time", datetime.now().strftime("%H:%M:%S"))
        with self.lock:
            if self._topology_ctx is None:
                try:
                    self._topology_ctx = prepare_resource_topology_static_context(snapshot)
                except Exception:
                    self._topology_ctx = None
            self._last_snapshot = snapshot
            self._last_age_seconds = age_seconds
            self._last_scada_error = err
            for m in snapshot.get("measurements", {}).get("scada", []):
                if int(m.get("valid", 0)) != 1 or m.get("value") is None:
                    continue
                key = (str(m["dev_type"]), str(m["dev_name"]), str(m["meas_type"]))
                dq = self._meas_history.get(key)
                if dq is None:
                    dq = deque(maxlen=self._meas_history_limit)
                    self._meas_history[key] = dq
                dq.append({"t": epoch_ms, "label": time_label, "v": float(m["value"])})
        return snapshot

    def get_measurement_trace(
        self,
        dev_type: str,
        dev_name: str,
        meas_type: str,
        window_minutes: int = 60,
    ) -> List[Dict[str, Any]]:
        cutoff = int(round(time.time() * 1000)) - max(1, int(window_minutes)) * 60 * 1000
        with self.lock:
            dq = self._meas_history.get((dev_type, dev_name, meas_type))
            if not dq:
                return []
            pts = [p for p in dq if p["t"] >= cutoff]
            return pts if pts else list(dq)[-60:]

    # ------------------------------------------------------------------
    # 核心 AGC 控制决策周期计算与下发
    # ------------------------------------------------------------------

    def run_control_cycle(self, *, trigger: str = "manual") -> Dict[str, Any]:
        """执行一次完整的 AGC 控制决策计算，并在闭环允许时下发四遥指令。"""
        snapshot = self.refresh_telemetry()
        with self.lock:
            settings = self.settings
            loop_mode = self.loop_mode
            auto_enabled = self.auto_enabled
            age_seconds = self._last_age_seconds
            scada_err = self._last_scada_error
            frame_age_limit = float(
                self.runtime_settings["backend"].get("frame_age_limit_seconds", 30.0)
            )
            topo_ctx = self._topology_ctx
            runtime_memory = self._runtime_memory

        self._last_control_monotonic = time.monotonic()
        device_configs_map = self._load_effective_device_configs_map()

        plan = calculate_renewable_control_plan(
            snapshot,
            settings,
            data_source="remote",
            snapshot_age_seconds=age_seconds,
            topology_static_context=topo_ctx,
            runtime_memory=runtime_memory,
            device_configs=device_configs_map,
        )

        dq = plan.get("dataQuality") if isinstance(plan.get("dataQuality"), Mapping) else {}
        stale_blocked = age_seconds > frame_age_limit
        dispatch_allowed = bool(dq.get("dispatchAllowed")) and not stale_blocked and not scada_err

        command_rows = list(plan.get("commandRows") or [])
        commands_to_send = list(plan.get("commands") or [])
        run_commands_to_send = list(plan.get("runCommands") or [])

        # 建立设备实时值索引用于指令审计
        current_by_dev: Dict[Tuple[str, str], Optional[float]] = {
            (str(r.get("dev_type", "")), str(r.get("dev_name", ""))): _finite(r.get("currentKw"))
            for r in command_rows
        }
        changed_set = {
            (str(r.get("dev_type", "")), str(r.get("dev_name", "")), str(r.get("set_type", "")))
            for r in command_rows
            if bool(r.get("strategyTargetChanged"))
        }

        dispatch_results: List[Dict[str, Any]] = []
        audit_records: List[Dict[str, Any]] = []
        now_iso = _now_iso()
        now_ms = int(round(time.time() * 1000))

        # 仅在闭环模式且数据质量允许时写回 scdfe_d.sqlite3 并交由独立线程异步等待回执
        pending_to_watch: List[Tuple[int, Dict[str, Any]]] = []
        if loop_mode == "closed" and dispatch_allowed:
            for cmd in commands_to_send:
                d_type = str(cmd.get("dev_type", ""))
                d_name = str(cmd.get("dev_name", ""))
                s_type = str(cmd.get("set_type", "p_set"))
                s_val = _finite(cmd.get("set_value"))
                if s_val is None:
                    continue
                if (d_type, d_name, s_type) not in changed_set:
                    dispatch_results.append(
                        {
                            "dev_type": d_type,
                            "dev_name": d_name,
                            "set_type": s_type,
                            "status": "unchanged",
                            "message": "维持当前值",
                        }
                    )
                    continue
                ok, st, msg, pt, cmd_time_ms = self.adapter.write_scdfe_d_command(
                    d_type, d_name, s_type, s_val
                )
                dispatch_results.append(
                    {
                        "dev_type": d_type,
                        "dev_name": d_name,
                        "set_type": s_type,
                        "status": st,
                        "message": msg,
                    }
                )
                audit_idx = len(audit_records)
                audit_records.append(
                    {
                        "issued_at": now_iso,
                        "epoch_ms": now_ms,
                        "origin": "automatic",
                        "kind": pt.kind if pt else "yt",
                        "dev_type": d_type,
                        "dev_name": d_name,
                        "set_type": s_type,
                        "measured_value": current_by_dev.get((d_type, d_name)),
                        "command_value": s_val,
                        "rtu_id": pt.rtu_id if pt else None,
                        "pnt_no": pt.pnt_no if pt else None,
                        "status": st,
                        "message": msg,
                    }
                )
                if ok and st == "pending" and pt is not None and pt.scdfe_table and pt.scdfe_col:
                    pending_to_watch.append(
                        (
                            audit_idx,
                            {
                                "slot": (pt.scdfe_table, int(pt.scdfe_idx), pt.scdfe_col),
                                "origin": "automatic",
                                "kind": pt.kind,
                                "dev_type": d_type,
                                "dev_name": d_name,
                                "set_type": s_type,
                                "value": s_val,
                                "cmd_time_ms": int(cmd_time_ms or now_ms),
                                "submitted_mono": time.monotonic(),
                            },
                        )
                    )
            for rcmd in run_commands_to_send:
                d_type = str(rcmd.get("dev_type", ""))
                d_name = str(rcmd.get("dev_name", ""))
                s_val = int(bool(rcmd.get("run_stat", 1)))
                ok, st, msg, pt, cmd_time_ms = self.adapter.write_scdfe_d_command(
                    d_type, d_name, "run_stat", s_val
                )
                audit_idx = len(audit_records)
                audit_records.append(
                    {
                        "issued_at": now_iso,
                        "epoch_ms": now_ms,
                        "origin": "automatic",
                        "kind": "yk",
                        "dev_type": d_type,
                        "dev_name": d_name,
                        "set_type": "run_stat",
                        "measured_value": None,
                        "command_value": float(s_val),
                        "rtu_id": pt.rtu_id if pt else None,
                        "pnt_no": pt.pnt_no if pt else None,
                        "status": st,
                        "message": msg,
                    }
                )
                if ok and st == "pending" and pt is not None and pt.scdfe_table and pt.scdfe_col:
                    pending_to_watch.append(
                        (
                            audit_idx,
                            {
                                "slot": (pt.scdfe_table, int(pt.scdfe_idx), pt.scdfe_col),
                                "origin": "automatic",
                                "kind": "yk",
                                "dev_type": d_type,
                                "dev_name": d_name,
                                "set_type": "run_stat",
                                "value": s_val,
                                "cmd_time_ms": int(cmd_time_ms or now_ms),
                                "submitted_mono": time.monotonic(),
                            },
                        )
                    )
        else:
            # 开环模式或数据陈旧闭锁：仅记录策略计算值，不下发
            reason_msg = (
                "开环模式（仅计算不下发）"
                if loop_mode != "closed"
                else (
                    f"数据陈旧({age_seconds:.1f}s > {frame_age_limit:.0f}s)已闭锁"
                    if stale_blocked
                    else "数据质量受限，已闭锁闭环下发"
                )
            )
            for cmd in commands_to_send:
                d_type = str(cmd.get("dev_type", ""))
                d_name = str(cmd.get("dev_name", ""))
                s_type = str(cmd.get("set_type", "p_set"))
                s_val = _finite(cmd.get("set_value"))
                if (d_type, d_name, s_type) in changed_set:
                    dispatch_results.append(
                        {
                            "dev_type": d_type,
                            "dev_name": d_name,
                            "set_type": s_type,
                            "status": "open_loop_only" if loop_mode != "closed" else "blocked",
                            "message": reason_msg,
                        }
                    )
                    audit_records.append(
                        {
                            "issued_at": now_iso,
                            "epoch_ms": now_ms,
                            "origin": "automatic",
                            "kind": "yt",
                            "dev_type": d_type,
                            "dev_name": d_name,
                            "set_type": s_type,
                            "measured_value": current_by_dev.get((d_type, d_name)),
                            "command_value": s_val,
                            "rtu_id": None,
                            "pnt_no": None,
                            "status": "open_loop_only" if loop_mode != "closed" else "blocked",
                            "message": reason_msg,
                        }
                    )

        decision_detail = list(plan.get("decisionDetail") or [])
        summary_line = str(decision_detail[0]) if decision_detail else "控制策略计算完成"
        cycle = self.store.record_strategy_cycle(
            plan=plan,
            loop_mode=loop_mode,
            auto_enabled=auto_enabled,
            sample_timestamp=str(snapshot.get("measurement_clock", {}).get("timestamp") or now_iso),
            environment=snapshot.get("environment") or {},
            dispatch_results=dispatch_results,
            summary_text=summary_line,
            epoch_ms=now_ms,
        )
        for rec in audit_records:
            rec["cycle"] = cycle
        inserted_ids = self.store.append_command_records(audit_records)

        # 将已写入 scdfe_d 的待回执指令交由独立线程异步等待返回状态
        for audit_idx, entry in pending_to_watch:
            entry["cycle"] = cycle
            entry["record_id"] = inserted_ids[audit_idx] if audit_idx < len(inserted_ids) else 0
            self._register_pending_ack(entry)

        dispatched_ok = sum(1 for r in dispatch_results if r["status"] == "dispatched")
        dispatched_pending = sum(1 for r in dispatch_results if r["status"] == "pending")
        dispatched_fail = sum(
            1 for r in dispatch_results if r["status"] in {"failed", "disabled", "unconfigured", "timeout"}
        )
        level = "warn" if (dispatched_fail > 0 or stale_blocked or dq.get("status") != "ok") else "info"
        trigger_cn = "自动周期" if trigger == "auto" else "单次计算"
        mode_cn = "闭环" if loop_mode == "closed" else "开环"
        dispatch_brief = (
            f"写入 scdfe_d 待回执 {dispatched_pending}，成功 {dispatched_ok}，失败 {dispatched_fail}"
            if dispatched_pending > 0
            else f"下发成功 {dispatched_ok}，失败 {dispatched_fail}"
        )
        self.store.append_control_log(
            level=level,
            event_type=trigger,
            cycle=cycle,
            subject=f"周期 #{cycle}（{trigger_cn} · {mode_cn}）",
            summary=f"{summary_line}｜{dispatch_brief}",
            detail="\n".join(str(line) for line in decision_detail),
        )

        with self.lock:
            self._last_plan = plan
            self._last_cycle = cycle
            self._last_calculated_at = now_iso
            if dispatched_ok > 0:
                self._last_dispatched_at = now_iso
            self._status_text = summary_line

        return self.get_agc_control_payload()

    # ------------------------------------------------------------------
    # 控制参数、模式、启停与人工四遥操作
    # ------------------------------------------------------------------

    def set_auto_enabled(self, enabled: bool) -> Dict[str, Any]:
        with self.lock:
            self.auto_enabled = bool(enabled)
            self._status_text = "实时自动控制运行中" if self.auto_enabled else "实时自动控制已停止"
            self.store.save_control_mode(self.loop_mode, self.auto_enabled)
        self.store.append_control_log(
            level="info",
            event_type="state",
            cycle=self._last_cycle,
            subject="AGC 自动控制",
            summary="已启动自动控制循环" if enabled else "已停止自动控制循环",
        )
        self.store.append_runtime_log(
            level="info",
            log_type="agc_control",
            target="AGC 自动控制",
            result="运行中" if enabled else "已停止",
            summary="启动实时自动控制" if enabled else "停止实时自动控制",
        )
        if enabled:
            return self.run_control_cycle(trigger="auto")
        return self.get_agc_control_payload()

    def set_loop_mode(self, loop_mode: str) -> Dict[str, Any]:
        normalized = "closed" if str(loop_mode).strip().lower() == "closed" else "open"
        with self.lock:
            self.loop_mode = normalized
            self.store.save_control_mode(self.loop_mode, self.auto_enabled)
        label = "闭环控制（允许下发四遥）" if normalized == "closed" else "开环控制（仅计算不下发）"
        self.store.append_control_log(
            level="warn" if normalized == "closed" else "info",
            event_type="mode",
            cycle=self._last_cycle,
            subject="控制方式切换",
            summary=f"已切换为 {label}",
        )
        self.store.append_runtime_log(
            level="warn" if normalized == "closed" else "info",
            log_type="agc_control",
            target="控制方式",
            result="闭环" if normalized == "closed" else "开环",
            summary=f"切换为 {label}",
        )
        return self.get_agc_control_payload()

    def update_control_parameters(
        self,
        params: Mapping[str, Any],
        preset: str = "custom",
    ) -> Dict[str, Any]:
        """校验并持久化 AGC 控制参数（含储能充放电降额曲线）。"""
        with self.lock:
            if preset in STRATEGY_PRESETS and not params:
                merged = RenewableControlSettings().updated(STRATEGY_PRESETS[preset])
            else:
                merged = self.settings.updated(dict(params))
            merged.validate()
            self.settings = merged
            self.preset = str(preset or "custom")
            self.store.save_control_parameters(_settings_to_dict(merged), preset=self.preset)
        self.store.append_control_log(
            level="info",
            event_type="settings",
            cycle=self._last_cycle,
            subject="控制参数更新",
            summary=f"已保存控制参数（方案：{self.preset}，周期 {merged.interval_seconds}s）",
        )
        self.store.append_runtime_log(
            level="info",
            log_type="parameters",
            target="AGC 控制参数",
            result="已保存",
            summary=f"更新策略参数（方案 {self.preset}）",
        )
        return self.get_agc_control_payload()

    def update_runtime_settings(
        self,
        *,
        backend: Optional[Mapping[str, Any]] = None,
        web: Optional[Mapping[str, Any]] = None,
        reset_scope: str = "",
    ) -> Dict[str, Any]:
        if reset_scope == "backend":
            backend = dict(DEFAULT_BACKEND_RUNTIME_SETTINGS)
        elif reset_scope == "web":
            web = dict(DEFAULT_WEB_RUNTIME_SETTINGS)
        saved = self.store.save_runtime_settings(backend=backend, web=web)
        with self.lock:
            self.runtime_settings = saved
        self.store.append_runtime_log(
            level="info",
            log_type="parameters",
            target="运行参数",
            result="已保存",
            summary="更新后台/Web 运行参数",
        )
        return saved

    def execute_manual_command(
        self,
        *,
        dev_type: str,
        dev_name: str,
        set_type: str,
        value: Any,
    ) -> Dict[str, Any]:
        """执行人工遥控（YK）或遥调（YT）指令并写入审计数据库。"""
        is_yk = set_type in {"run_stat", "closed_status"}
        parsed_val: Any = int(bool(int(float(value)))) if is_yk else float(value)
        if not is_yk and not math.isfinite(parsed_val):
            raise ValueError("遥调控制值必须为有效数值")

        # 读取当前量测值
        measured_val: Optional[float] = None
        with self.lock:
            snap = self._last_snapshot or {}
            for m in snap.get("measurements", {}).get("scada", []):
                if m.get("dev_type") == dev_type and m.get("dev_name") == dev_name:
                    mt = str(m.get("meas_type", ""))
                    if (is_yk and mt in {"RUN_STAT", "CLOSED_STATUS"}) or (
                        not is_yk and mt in {"P_GEN", "P_LOAD", "P_AC", "P_DC"}
                    ):
                        measured_val = _finite(m.get("value"))
                        break

        ok, status, msg, pt, cmd_time_ms = self.adapter.write_scdfe_d_command(
            dev_type, dev_name, set_type, parsed_val
        )
        now_iso = _now_iso()
        now_ms = int(round(time.time() * 1000))
        record = {
            "issued_at": now_iso,
            "epoch_ms": now_ms,
            "cycle": self._last_cycle,
            "origin": "manual",
            "kind": pt.kind if pt else ("yk" if is_yk else "yt"),
            "dev_type": dev_type,
            "dev_name": dev_name,
            "set_type": set_type,
            "measured_value": measured_val,
            "command_value": float(parsed_val),
            "rtu_id": pt.rtu_id if pt else None,
            "pnt_no": pt.pnt_no if pt else None,
            "status": status,
            "message": msg,
            "cmd_time_ms": cmd_time_ms,
        }
        inserted_ids = self.store.append_command_records([record])
        rec_id = inserted_ids[0] if inserted_ids else 0
        record["id"] = rec_id
        with self.lock:
            self._latest_manual_commands[(dev_type, dev_name, set_type)] = record
        if ok and status == "pending" and pt is not None and pt.scdfe_table and pt.scdfe_col:
            self._register_pending_ack(
                {
                    "slot": (pt.scdfe_table, int(pt.scdfe_idx), pt.scdfe_col),
                    "record_id": rec_id,
                    "cycle": self._last_cycle,
                    "origin": "manual",
                    "kind": pt.kind,
                    "dev_type": dev_type,
                    "dev_name": dev_name,
                    "set_type": set_type,
                    "value": parsed_val,
                    "cmd_time_ms": int(cmd_time_ms or now_ms),
                    "submitted_mono": time.monotonic(),
                }
            )
        self.store.append_runtime_log(
            level="info" if ok else "warn",
            log_type="manual_command",
            target=f"{dev_name} ({set_type})",
            result="待回执" if status == "pending" else ("已下发" if ok else status),
            summary=f"人工{'遥控' if is_yk else '遥调'} = {parsed_val}｜{msg}",
        )
        return {"ok": ok, "record": record, "message": msg}

    # ------------------------------------------------------------------
    # 前端视图聚合载荷构造
    # ------------------------------------------------------------------

    def get_snapshot_payload(self, *, include_static: bool = False) -> Dict[str, Any]:
        with self.lock:
            snap = copy.deepcopy(self._last_snapshot) if self._last_snapshot else {}
            latest_strategy = self.store.get_latest_strategy()
            payload: Dict[str, Any] = {
                "model": snap.get("model", self.adapter.model_meta),
                "clock": snap.get("clock", {}),
                "measurement_clock": snap.get("measurement_clock", {}),
                "environment": snap.get("environment", {}),
                "devices": snap.get("devices", []),
                "device_states": snap.get("device_states", []),
                "measurements": snap.get("measurements", {"scada": []}),
                "static_meta": snap.get("static_meta", {}),
                "agc_status": {
                    "auto_enabled": self.auto_enabled,
                    "loop_mode": self.loop_mode,
                    "cycle": self._last_cycle,
                    "status_text": self._status_text,
                    "last_calculated_at": self._last_calculated_at,
                    "last_dispatched_at": self._last_dispatched_at,
                    "scada_error": self._last_scada_error,
                    "age_seconds": round(self._last_age_seconds, 2),
                    "agc_db": str(self.store.path),
                    "command_channel": "scdfe_d",
                    "pending_ack_count": len(self._pending_acks),
                },
                "latest_strategy": latest_strategy,
                "runtime_settings": self.runtime_settings,
            }
            if include_static:
                payload["device_parameters"] = snap.get("device_parameters", {})
                payload["definitions"] = {
                    "control_list": (snap.get("definitions") or {}).get("control_list", []),
                }
        return payload

    def _extract_island_diagnostics(self, strategy: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """提取拓扑孤岛优化与功率平衡诊断摘要，供人机状态灯展示。"""
        if not strategy:
            return {
                "island_count": 0,
                "all_success": True,
                "max_balance_delta_kw": 0.0,
                "solve_ms": 0.0,
                "status": "idle",
                "summary": "尚未计算",
                "islands": [],
            }
        metrics = strategy.get("metrics") if isinstance(strategy.get("metrics"), Mapping) else {}
        raw_islands = metrics.get("optimizationIslands")
        islands_out: List[Dict[str, Any]] = []
        max_delta = _finite(metrics.get("optimizationMaxBalanceResidualKw")) or 0.0
        all_ok = bool(metrics.get("optimizationAllSuccess", True))
        any_override = False
        if isinstance(raw_islands, list):
            for idx, isl in enumerate(raw_islands):
                if not isinstance(isl, Mapping):
                    continue
                st = str(isl.get("status") or "optimal")
                ok = bool(isl.get("success", True))
                d_kw = _finite(isl.get("maxBalanceDeltaKw", isl.get("max_balance_delta_kw"))) or 0.0
                max_delta = max(max_delta, abs(d_kw))
                if not ok:
                    all_ok = False
                if "override" in st or "safety" in st:
                    any_override = True
                islands_out.append(
                    {
                        "id": str(isl.get("islandId") or isl.get("island_id") or f"I{idx + 1}"),
                        "status": st,
                        "success": ok,
                        "device_count": int(isl.get("variableCount") or isl.get("variable_count") or 0),
                        "max_balance_delta_kw": round(abs(d_kw), 3),
                        "message": str(isl.get("message") or ""),
                    }
                )
        dq = str(strategy.get("data_quality") or "ok").lower()
        dispatch_ok = bool(strategy.get("dispatch_allowed", True))
        solve_sec = _finite(metrics.get("optimizationSolveSeconds")) or 0.0
        if dq != "ok" or not dispatch_ok or not all_ok:
            status = "danger"
        elif any_override or max_delta > 0.5:
            status = "warn"
        else:
            status = "ok"
        island_cnt = len(islands_out) or int(metrics.get("optimizationIslandCount") or 1)
        summary = f"{island_cnt} 岛 · 平衡偏差 {max_delta:.2f} kW"
        if dq != "ok" or not dispatch_ok:
            summary = f"闭锁 ({dq}) · {summary}"
        elif any_override:
            summary = f"安全放开 · {summary}"
        return {
            "island_count": island_cnt,
            "all_success": all_ok,
            "max_balance_delta_kw": round(max_delta, 3),
            "solve_ms": round(solve_sec * 1000.0, 1),
            "status": status,
            "summary": summary,
            "islands": islands_out,
        }

    def get_strategy_cycle_payload(self, cycle: Optional[int] = None) -> Dict[str, Any]:
        """查询指定历史周期（或最新周期）的控制策略快照与拓扑孤岛诊断。"""
        strategy = self.store.get_strategy_by_cycle(cycle)
        return {
            "ok": strategy is not None,
            "strategy": strategy,
            "island_diagnostics": self._extract_island_diagnostics(strategy),
        }

    def get_agc_control_payload(self) -> Dict[str, Any]:
        latest = self.store.get_latest_strategy()
        with self.lock:
            env = (self._last_snapshot or {}).get("environment", {})
            return {
                "auto_enabled": self.auto_enabled,
                "loop_mode": self.loop_mode,
                "preset": self.preset,
                "presets": list(STRATEGY_PRESETS.keys()),
                "settings": _settings_to_dict(self.settings),
                "cycle": self._last_cycle,
                "status_text": self._status_text,
                "last_calculated_at": self._last_calculated_at,
                "last_dispatched_at": self._last_dispatched_at,
                "environment": env,
                "age_seconds": round(self._last_age_seconds, 2),
                "scada_error": self._last_scada_error,
                "agc_db": str(self.store.path),
                "command_channel": "scdfe_d",
                "pending_ack_count": len(self._pending_acks),
                "island_diagnostics": self._extract_island_diagnostics(latest),
                "latest": latest,
            }

    def get_commands_payload(
        self,
        *,
        only_active: bool = False,
        origin: str = "all",
    ) -> Dict[str, Any]:
        with self.lock:
            snap = self._last_snapshot or {}
            control_list = list((snap.get("definitions") or {}).get("control_list") or [])
            meas_map: Dict[Tuple[str, str, str], float] = {}
            for m in snap.get("measurements", {}).get("scada", []):
                if int(m.get("valid", 0)) == 1 and m.get("value") is not None:
                    meas_map[(str(m["dev_type"]), str(m["dev_name"]), str(m["meas_type"]))] = float(m["value"])
        latest = self.store.get_latest_strategy()
        strat_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        if latest:
            for r in latest.get("command_rows") or []:
                strat_by_key[(str(r["dev_type"]), str(r["dev_name"]), str(r["set_type"]))] = r

        recent_records = self.store.query_command_records(limit=300)
        last_rec_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for rec in recent_records:
            key = (str(rec["dev_type"]), str(rec["dev_name"]), str(rec["set_type"]))
            if key not in last_rec_by_key:
                last_rec_by_key[key] = rec

        yk_rows: List[Dict[str, Any]] = []
        yt_rows: List[Dict[str, Any]] = []
        for item in control_list:
            d_type = str(item["dev_type"])
            d_name = str(item["dev_name"])
            s_type = str(item["set_type"])
            kind = str(item["kind"])
            srow = strat_by_key.get((d_type, d_name, s_type))
            lrec = last_rec_by_key.get((d_type, d_name, s_type))
            if kind == "yk":
                cur_val = meas_map.get((d_type, d_name, "CLOSED_STATUS"))
                if cur_val is None:
                    cur_val = meas_map.get((d_type, d_name, "RUN_STAT"))
            else:
                cur_val = (
                    meas_map.get((d_type, d_name, "P_AC" if s_type == "p_ac_set" else ("P_DC" if s_type == "p_dc_set" else "P_GEN")))
                    or meas_map.get((d_type, d_name, "P_LOAD"))
                )
            cmd_val = (
                lrec["command_value"]
                if lrec is not None
                else (srow["target_kw"] if srow is not None else None)
            )
            row_origin = lrec["origin"] if lrec is not None else ("automatic" if srow is not None else "")
            row_status = lrec["status"] if lrec is not None else (srow["dispatch_status"] if srow is not None else "idle")
            active = cmd_val is not None and row_status in {"pending", "dispatched", "calculated", "open_loop_only"}
            if only_active and not active:
                continue
            if origin != "all" and row_origin != origin:
                continue
            entry = {
                "dev_type": d_type,
                "dev_name": d_name,
                "set_type": s_type,
                "kind": kind,
                "point_name": item.get("point_name", ""),
                "unit": item.get("unit", ""),
                "min": item.get("min"),
                "max": item.get("max"),
                "rtu_id": item.get("rtu_id"),
                "pnt_no": item.get("pnt_no"),
                "scada_mapped": bool(item.get("scada_mapped")),
                "measured_value": cur_val,
                "command_value": cmd_val,
                "origin": row_origin or "-",
                "status": row_status,
                "issued_at": lrec["issued_at"] if lrec is not None else (latest["recorded_at"] if (latest and srow) else ""),
                "message": lrec["message"] if lrec is not None else (srow["status_label"] if srow is not None else ""),
                "active": active,
            }
            if kind == "yk":
                yk_rows.append(entry)
            else:
                yt_rows.append(entry)
        return {
            "command_channel": "scdfe_d",
            "pending_ack_count": len(self._pending_acks),
            "yk": yk_rows,
            "yt": yt_rows,
            "recent_records": recent_records[:100],
        }

    # ------------------------------------------------------------------
    # 受控设备控制配置（上下限/步长/死区/闭环-跟踪-开环）与 96 点预设曲线
    # ------------------------------------------------------------------

    def _load_effective_device_configs_map(self) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
        """加载并合并全部受控设备配置与 96 点预设曲线，供控制引擎消费。"""
        cfgs = self.store.load_device_configs()
        curves = self.store.load_all_device_curves()
        merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        all_keys = set(cfgs.keys()) | set(curves.keys())
        for key in all_keys:
            base = dict(
                cfgs.get(
                    key,
                    {
                        "dev_type": key[0],
                        "dev_name": key[1],
                        "set_type": key[2],
                        "control_state": "closed",
                        "p_min": None,
                        "p_max": None,
                        "control_step": None,
                        "control_deadband": 0.0,
                        "curve_interp": "step",
                        "updated_at": "",
                    },
                )
            )
            if key in curves:
                base["curve"] = curves[key]
            merged[key] = base
        return merged

    def _build_controlled_device_inventory(self) -> List[Dict[str, Any]]:
        """聚合模型、四遥点表与最近策略，列出全部 AGC 受控设备及模型默认边界/步长。"""
        with self.lock:
            snap = self._last_snapshot or {}
            settings = self.settings
        latest = self.store.get_latest_strategy()
        strat_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
        if latest:
            for r in latest.get("command_rows") or []:
                strat_by_key[(str(r.get("dev_type", "")), str(r.get("dev_name", "")))] = r

        meas_map: Dict[Tuple[str, str, str], float] = {}
        for m in snap.get("measurements", {}).get("scada", []):
            if int(m.get("valid", 0)) == 1 and m.get("value") is not None:
                meas_map[(str(m["dev_type"]), str(m["dev_name"]), str(m["meas_type"]))] = float(m["value"])

        ctrl_by_dev: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for item in (snap.get("definitions") or {}).get("control_list") or []:
            if item.get("kind") == "yt":
                ctrl_by_dev[(str(item["dev_type"]), str(item["dev_name"]), str(item["set_type"]))] = item

        dev_params = snap.get("device_parameters") or {}
        param_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
        param_by_idx: Dict[Tuple[str, int], Dict[str, Any]] = {}
        storage_ext_by_gen: Dict[Tuple[str, int], Dict[str, Any]] = {}
        internal_coupling_gen_idxs: set[Tuple[str, int]] = set()
        for blk, rows in dev_params.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                nm = str(row.get("name") or row.get("dev_name") or "").strip()
                r_idx = _finite(row.get("idx"))
                if nm:
                    param_by_key[(str(blk), nm)] = dict(row)
                if r_idx is not None:
                    param_by_idx[(str(blk), int(round(r_idx)))] = dict(row)
                if blk == "ACStorageGen" and _finite(row.get("idx_acgenerator")) is not None:
                    storage_ext_by_gen[("ACGenerator", int(round(float(row["idx_acgenerator"]))))] = dict(row)
                elif blk == "DCStorageGen" and _finite(row.get("idx_dcgenerator")) is not None:
                    storage_ext_by_gen[("DCGenerator", int(round(float(row["idx_dcgenerator"]))))] = dict(row)
                elif blk == "Hydro2DcE" and _finite(row.get("idx_dc_unit_t1")) is not None:
                    internal_coupling_gen_idxs.add(("DCGenerator", int(round(float(row["idx_dc_unit_t1"])))))
                elif blk == "Hydro2AcE" and _finite(row.get("idx_ac_unit_t1")) is not None:
                    internal_coupling_gen_idxs.add(("ACGenerator", int(round(float(row["idx_ac_unit_t1"])))))

        def _classify(dev_type: str, dev_name: str, sub_type: str, srow: Optional[Mapping[str, Any]]) -> Tuple[str, str]:
            if srow and srow.get("category"):
                cat = str(srow["category"])
                if "风" in cat:
                    return "wind", cat
                if "光" in cat:
                    return "pv", cat
                if "储" in cat:
                    return "storage", cat
                if "柴" in cat:
                    return "diesel", cat
                if "变流" in cat:
                    return "converter", cat
                if "氢" in cat or "燃" in cat:
                    return "hydrogen", cat
            st = (sub_type or "").lower()
            nm = dev_name
            if dev_type == "AcE2Hydro":
                return "hydrogen", "电制氢"
            if dev_type == "Hydro2DcE":
                return "hydrogen", "燃料电池"
            if dev_type == "DCACConverter":
                return "converter", "交直流变流器"
            if "wind" in st or "风" in nm:
                return "wind", "交流风电" if dev_type == "ACGenerator" else "直流风电"
            if "pv" in st or "光" in nm:
                return "pv", "交流光伏" if dev_type == "ACGenerator" else "直流光伏"
            if "storage" in st or "储" in nm:
                return "storage", "交流储能" if dev_type == "ACGenerator" else "直流储能"
            if "diesel" in st or "柴" in nm:
                return "diesel", "柴油发电"
            return "other", dev_type

        inventory: List[Dict[str, Any]] = []
        seen: set[Tuple[str, str, str]] = set()
        devices_list = snap.get("devices") or []
        dev_meta_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for d in devices_list:
            if not isinstance(d, Mapping):
                continue
            dt = str(d.get("dev_type") or d.get("type") or "").strip()
            dn = str(d.get("dev_name") or d.get("name") or "").strip()
            if dt and dn:
                dev_meta_map[(dt, dn)] = dict(d)

        # 收集候选受控设备：优先覆盖策略全量设备 + 模型中所有发电/储能/网侧变流器/氢能耦合设备
        candidate_keys: List[Tuple[str, str, str]] = []
        if latest:
            for r in latest.get("command_rows") or []:
                dt = str(r.get("dev_type") or "").strip()
                dn = str(r.get("dev_name") or "").strip()
                st = str(r.get("set_type") or ("p_ac_set" if dt == "DCACConverter" else "p_set")).strip()
                if dt and dn and st:
                    candidate_keys.append((dt, dn, st))

        for (dt, dn), meta in dev_meta_map.items():
            if dt in {"ACGenerator", "DCGenerator"}:
                prow_chk = param_by_key.get((dt, dn), {})
                chk_idx = _finite(prow_chk.get("idx"))
                if chk_idx is not None and (dt, int(round(chk_idx))) in internal_coupling_gen_idxs:
                    continue
                if "_直流设备端" in dn or "_交流设备端" in dn:
                    continue
                candidate_keys.append((dt, dn, "p_set"))
            elif dt == "DCACConverter" and ("DCAC" in dn or (dt, dn) in strat_by_key):
                candidate_keys.append((dt, dn, "p_ac_set"))
            elif dt in {"AcE2Hydro", "Hydro2DcE"}:
                candidate_keys.append((dt, dn, "p_set"))

        for dt, dn, st in candidate_keys:
            if (dt, dn, st) in seen:
                continue
            seen.add((dt, dn, st))
            srow = strat_by_key.get((dt, dn))
            meta = dev_meta_map.get((dt, dn), {})
            prow = param_by_key.get((dt, dn), {})
            ctrl_pt = ctrl_by_dev.get((dt, dn, st))
            sub_type = str(meta.get("sub_type") or meta.get("role") or "")
            group, category = _classify(dt, dn, sub_type, srow)

            # 实时量测值
            if dt == "DCACConverter":
                cur_val = meas_map.get((dt, dn, "P_AC" if st == "p_ac_set" else "P_DC"))
            elif dt == "AcE2Hydro":
                cur_val = meas_map.get((dt, dn, "P_LOAD"))
                if cur_val is None:
                    cur_val = meas_map.get((dt, dn, "P_GEN"))
            else:
                cur_val = meas_map.get((dt, dn, "P_GEN"))
                if cur_val is None:
                    cur_val = meas_map.get((dt, dn, "P_LOAD"))
            if cur_val is None and srow is not None:
                cur_val = _finite(srow.get("current_kw"))

            run_val = meas_map.get((dt, dn, "RUN_STAT"))
            online = bool(srow.get("online")) if srow is not None else (run_val is None or run_val > 0.5)

            # 推导模型默认上下限与默认步长（严格使用铭牌/模型额定参数，绝不混用瞬时气象估算值）
            def _pos_num(*vals: Any) -> Optional[float]:
                for v in vals:
                    fv = _finite(v)
                    if fv is not None and fv > 1e-9:
                        return fv
                return None

            r_idx_val = _finite(prow.get("idx"))
            ext_row = storage_ext_by_gen.get((dt, int(round(r_idx_val))), {}) if r_idx_val is not None else {}
            p_max_raw = _pos_num(
                prow.get("rated_capacity"),
                prow.get("p_max"),
                prow.get("ac_p_max"),
                prow.get("p_rated"),
                prow.get("sn"),
                ctrl_pt.get("max") if ctrl_pt else None,
            )
            p_min_raw = _finite(prow.get("p_min"))
            if p_min_raw is None:
                p_min_raw = _finite(prow.get("ac_p_min"))

            default_min: Optional[float] = None
            default_max: Optional[float] = None
            default_step: Optional[float] = None

            if group in {"wind", "pv"}:
                cap = max(0.0, p_max_raw or 0.0)
                default_min = 0.0
                default_max = round(cap, 3)
                default_step = round(max(0.0, settings.step_coefficient * cap), 3)
            elif group == "storage":
                max_dis = _pos_num(
                    ext_row.get("max_discharge_power"),
                    prow.get("p_dis_max"),
                    prow.get("p_max"),
                    prow.get("rated_capacity"),
                    p_max_raw,
                ) or 0.0
                max_ch = _pos_num(
                    ext_row.get("max_charge_power"),
                    prow.get("p_ch_max"),
                    abs(p_min_raw) if (p_min_raw is not None and p_min_raw < -1e-9) else None,
                    max_dis,
                ) or max_dis
                default_min = round(-abs(max_ch), 3)
                default_max = round(abs(max_dis), 3)
                default_step = round(max(0.0, settings.storage_step_ratio * max(abs(max_ch), abs(max_dis))), 3)
            elif group == "diesel":
                cap = max(0.0, _pos_num(prow.get("p_max"), prow.get("rated_capacity"), p_max_raw) or 0.0)
                lo = max(0.0, p_min_raw if p_min_raw is not None else 0.0)
                default_min = round(lo, 3)
                default_max = round(cap, 3)
                default_step = round(
                    max(0.0, settings.diesel_step_ratio * cap) if 0.0 < settings.diesel_step_ratio < 1.0 else 0.0,
                    3,
                )
            elif group == "converter":
                cap = abs(_pos_num(prow.get("ac_p_max"), prow.get("rated_capacity"), p_max_raw) or 0.0)
                default_min = round(-cap, 3)
                default_max = round(cap, 3)
                ratio = (
                    settings.converter_ramp_step_ratio
                    if settings.converter_ramp_step_ratio > 1e-9
                    else settings.converter_step_ratio
                )
                default_step = round(max(0.0, ratio * cap) if 0.0 < ratio < 1.0 else 0.0, 3)
            elif group == "hydrogen":
                # 从关联的电侧/氢侧端口设备读取氢能耦合设备额定功率；若模型未定义则返回 None，避免生成虚假 [0, 0] 上限
                linked_row: Dict[str, Any] = {}
                flow_power_cap: Optional[float] = None
                if dt == "AcE2Hydro":
                    if _finite(prow.get("idx_ac_load_t1")) is not None:
                        linked_row = param_by_idx.get(("ACLoad", int(round(float(prow["idx_ac_load_t1"])))), {})
                    if _finite(prow.get("idx_h2_unit_t2")) is not None:
                        h2_row = param_by_idx.get(("HydroSource", int(round(float(prow["idx_h2_unit_t2"])))), {})
                        f_max = _pos_num(h2_row.get("flow_max"))
                        coeff = _pos_num(prow.get("e2h_coeff"))
                        if f_max is not None and coeff is not None:
                            flow_power_cap = f_max / coeff
                elif dt == "Hydro2DcE":
                    if _finite(prow.get("idx_dc_unit_t1")) is not None:
                        linked_row = param_by_idx.get(("DCGenerator", int(round(float(prow["idx_dc_unit_t1"])))), {})
                    if _finite(prow.get("idx_h2_load_t2")) is not None:
                        h2_row = param_by_idx.get(("HydroLoad", int(round(float(prow["idx_h2_load_t2"])))), {})
                        f_max = _pos_num(h2_row.get("flow_max"))
                        coeff = _pos_num(prow.get("h2e_coeff"))
                        if f_max is not None and coeff is not None:
                            flow_power_cap = f_max * coeff
                cap = _pos_num(
                    p_max_raw,
                    linked_row.get("rated_capacity"),
                    linked_row.get("p_max"),
                    linked_row.get("p_load"),
                    flow_power_cap,
                    ctrl_pt.get("max") if ctrl_pt else None,
                )
                if cap is not None:
                    if dt == "AcE2Hydro":
                        default_min = round(cap * settings.electrolyzer_power_min_ratio, 3)
                        default_max = round(cap * settings.electrolyzer_power_max_ratio, 3)
                        default_step = round(cap * settings.electrolyzer_power_step_ratio, 3)
                    else:
                        default_min = round(cap * settings.fuel_cell_power_min_ratio, 3)
                        default_max = round(cap * settings.fuel_cell_power_max_ratio, 3)
                        default_step = round(cap * settings.fuel_cell_power_step_ratio, 3)
                else:
                    default_min = 0.0
                    default_max = None
                    default_step = None
            else:
                default_min = round(p_min_raw, 3) if p_min_raw is not None else 0.0
                default_max = round(p_max_raw, 3) if p_max_raw is not None else 0.0
                default_step = 0.0

            inventory.append(
                {
                    "group": group,
                    "category": category,
                    "dev_type": dt,
                    "dev_name": dn,
                    "set_type": st,
                    "unit": (ctrl_pt.get("unit") if ctrl_pt else "") or "kW",
                    "online": online,
                    "scada_mapped": bool(ctrl_pt and ctrl_pt.get("scada_mapped")),
                    "current_kw": round(cur_val, 3) if cur_val is not None else None,
                    "default_min": default_min,
                    "default_max": default_max,
                    "default_step": default_step,
                    "allow_negative": group in {"storage", "converter"},
                }
            )

        group_order = {"wind": 0, "pv": 1, "storage": 2, "diesel": 3, "converter": 4, "hydrogen": 5, "other": 6}
        inventory.sort(key=lambda r: (group_order.get(r["group"], 9), r["category"], r["dev_name"]))
        return inventory

    def get_device_configs_payload(self) -> Dict[str, Any]:
        """返回全部受控设备的当前配置、默认限值、当前时刻曲线现值及状态汇总。"""
        inventory = self._build_controlled_device_inventory()
        saved_cfgs = self.store.load_device_configs()
        saved_curves = self.store.load_all_device_curves()
        with self.lock:
            snap = self._last_snapshot or {}
        minute_of_day = resolve_snapshot_minute_of_day(snap)
        now_idx = int(minute_of_day // 15.0) % 96
        items: List[Dict[str, Any]] = []
        counts = {"total": 0, "closed": 0, "track": 0, "open": 0}

        for dev in inventory:
            key = (dev["dev_type"], dev["dev_name"], dev["set_type"])
            cfg = saved_cfgs.get(key)
            curve = saved_curves.get(key)
            state = str((cfg or {}).get("control_state") or "closed")
            interp = str((cfg or {}).get("curve_interp") or "step")
            curve_now = evaluate_device_curve(curve, minute_of_day, interp) if curve else None
            counts["total"] += 1
            counts[state] = counts.get(state, 0) + 1
            items.append(
                {
                    **dev,
                    "control_state": state,
                    "p_min": cfg["p_min"] if cfg else None,
                    "p_max": cfg["p_max"] if cfg else None,
                    "control_step": cfg["control_step"] if cfg else None,
                    "control_deadband": cfg["control_deadband"] if cfg else 0.0,
                    "curve_interp": interp,
                    "has_curve": curve is not None,
                    "curve_now_kw": round(curve_now, 3) if curve_now is not None else None,
                    "updated_at": cfg["updated_at"] if cfg else "",
                }
            )
        return {
            "items": items,
            "counts": counts,
            "minute_of_day": round(minute_of_day, 2),
            "current_slot_index": now_idx,
            "current_slot_label": f"{(now_idx * 15) // 60:02d}:{(now_idx * 15) % 60:02d}",
        }

    def save_device_configs(self, items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """校验并批量保存受控设备控制配置，同时写入审计日志。"""
        if not isinstance(items, Sequence) or not items:
            raise ValueError("待保存的设备配置列表不能为空")
        inv_map = {
            (d["dev_type"], d["dev_name"], d["set_type"]): d
            for d in self._build_controlled_device_inventory()
        }
        for raw in items:
            d_type = str(raw.get("dev_type") or "").strip()
            d_name = str(raw.get("dev_name") or "").strip()
            s_type = str(raw.get("set_type") or "p_set").strip()
            dev_info = inv_map.get((d_type, d_name, s_type))
            p_min = _finite(raw.get("p_min")) if raw.get("p_min") not in (None, "") else None
            p_max = _finite(raw.get("p_max")) if raw.get("p_max") not in (None, "") else None
            if dev_info and not dev_info.get("allow_negative"):
                if p_min is not None and p_min < -1e-9:
                    raise ValueError(f"{d_name} 下限不能为负值 ({p_min})")
                if p_max is not None and p_max < -1e-9:
                    raise ValueError(f"{d_name} 上限不能为负值 ({p_max})")
            if p_min is not None and p_max is not None and p_min > p_max + 1e-9:
                raise ValueError(f"{d_name} 下限 ({p_min}) 不能大于上限 ({p_max})")
        saved_count, ts = self.store.save_device_configs(items)
        self.store.append_control_log(
            level="info",
            event_type="device_config",
            cycle=self._last_cycle,
            subject="受控设备参数更新",
            summary=f"已保存 {saved_count} 台受控设备控制配置",
        )
        self.store.append_runtime_log(
            level="info",
            log_type="parameters",
            target="受控设备控制配置",
            result="已保存",
            summary=f"批量保存 {saved_count} 台设备上下限/步长/死区/控制状态",
        )
        payload = self.get_device_configs_payload()
        payload["saved_count"] = saved_count
        payload["updated_at"] = ts
        return payload

    def reset_device_configs(
        self,
        *,
        dev_type: str = "",
        dev_name: str = "",
        set_type: str = "",
        reset_curves: bool = False,
    ) -> Dict[str, Any]:
        """恢复单台或全部受控设备配置为默认闭环与模型默认边界。"""
        removed = self.store.reset_device_configs(
            dev_type=dev_type,
            dev_name=dev_name,
            set_type=set_type,
            reset_curves=reset_curves,
        )
        target_label = f"{dev_name} ({set_type})" if dev_name else "全部受控设备"
        self.store.append_control_log(
            level="warn",
            event_type="device_config",
            cycle=self._last_cycle,
            subject="受控设备配置恢复默认",
            summary=f"已重置 {target_label}（影响 {removed} 项，{'含曲线' if reset_curves else '保留曲线'}）",
        )
        self.store.append_runtime_log(
            level="warn",
            log_type="parameters",
            target=target_label,
            result="已恢复默认",
            summary=f"重置受控设备控制配置（{removed} 项）",
        )
        payload = self.get_device_configs_payload()
        payload["reset_count"] = removed
        return payload

    def get_device_curve_payload(
        self,
        *,
        dev_type: str,
        dev_name: str,
        set_type: str = "p_set",
    ) -> Dict[str, Any]:
        """读取单台受控设备的 96 点预设曲线（未配置时自动返回合理基线曲线）。"""
        d_type = str(dev_type or "").strip()
        d_name = str(dev_name or "").strip()
        s_type = str(set_type or "p_set").strip()
        if not d_type or not d_name:
            raise ValueError("缺少 dev_type 或 dev_name")
        all_cfgs = self.get_device_configs_payload()
        dev_info = next(
            (
                item
                for item in all_cfgs["items"]
                if item["dev_type"] == d_type and item["dev_name"] == d_name and item["set_type"] == s_type
            ),
            None,
        )
        saved = self.store.load_device_curve(d_type, d_name, s_type)
        has_saved = saved is not None
        if saved is None:
            # 未曾保存过曲线时：以当前实时值（或默认下限与上限中值）生成平滑基线
            base_val = 0.0
            if dev_info is not None:
                if dev_info.get("current_kw") is not None:
                    base_val = float(dev_info["current_kw"])
                elif dev_info.get("default_min") is not None and dev_info.get("default_max") is not None:
                    base_val = max(float(dev_info["default_min"]), 0.0)
                lo = dev_info.get("p_min") if dev_info.get("p_min") is not None else dev_info.get("default_min")
                hi = dev_info.get("p_max") if dev_info.get("p_max") is not None else dev_info.get("default_max")
                if lo is not None:
                    base_val = max(float(lo), base_val)
                if hi is not None:
                    base_val = min(float(hi), base_val)
            saved = [round(base_val, 3)] * 96

        labels = [f"{(i * 15) // 60:02d}:{(i * 15) % 60:02d}" for i in range(96)]
        return {
            "dev_type": d_type,
            "dev_name": d_name,
            "set_type": s_type,
            "has_saved": has_saved,
            "curve_interp": (dev_info or {}).get("curve_interp", "step"),
            "control_state": (dev_info or {}).get("control_state", "closed"),
            "current_kw": (dev_info or {}).get("current_kw"),
            "effective_min": (
                dev_info["p_min"]
                if (dev_info and dev_info.get("p_min") is not None)
                else (dev_info.get("default_min") if dev_info else None)
            ),
            "effective_max": (
                dev_info["p_max"]
                if (dev_info and dev_info.get("p_max") is not None)
                else (dev_info.get("default_max") if dev_info else None)
            ),
            "unit": (dev_info or {}).get("unit", "kW"),
            "current_slot_index": all_cfgs["current_slot_index"],
            "labels": labels,
            "points": saved,
        }

    def save_device_curve(
        self,
        *,
        dev_type: str,
        dev_name: str,
        set_type: str = "p_set",
        points: Sequence[Any],
        curve_interp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """保存受控设备 96 点预设曲线并记录审计日志。"""
        ts = self.store.save_device_curve(
            dev_type=dev_type,
            dev_name=dev_name,
            set_type=set_type,
            points=points,
            curve_interp=curve_interp,
        )
        self.store.append_control_log(
            level="info",
            event_type="device_curve",
            cycle=self._last_cycle,
            subject=f"预设曲线更新：{dev_name}",
            summary=f"已保存 {dev_name} ({set_type}) 96 点预设曲线（取点：{curve_interp or '保持原方式'}）",
        )
        self.store.append_runtime_log(
            level="info",
            log_type="parameters",
            target=f"{dev_name} ({set_type})",
            result="已保存",
            summary="更新 96 点预设跟踪曲线",
        )
        res = self.get_device_curve_payload(dev_type=dev_type, dev_name=dev_name, set_type=set_type)
        res["updated_at"] = ts
        return res

    def copy_device_curve_to_targets(
        self,
        *,
        source_name: str = "",
        points: Sequence[Any],
        curve_interp: Optional[str] = None,
        targets: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """将 96 点预设曲线批量复制应用至多台同类受控设备并记录审计日志。"""
        if not isinstance(targets, Sequence) or not targets:
            raise ValueError("目标设备列表不能为空")
        copied_names: List[str] = []
        last_ts = ""
        for tgt in targets:
            dt = str(tgt.get("dev_type") or "").strip()
            dn = str(tgt.get("dev_name") or "").strip()
            st = str(tgt.get("set_type") or "p_set").strip()
            if not dt or not dn:
                continue
            last_ts = self.store.save_device_curve(
                dev_type=dt,
                dev_name=dn,
                set_type=st,
                points=points,
                curve_interp=curve_interp,
            )
            copied_names.append(dn)
        if not copied_names:
            raise ValueError("未找到有效的目标设备")
        src_lbl = source_name or "当前曲线"
        self.store.append_control_log(
            level="info",
            event_type="device_curve",
            cycle=self._last_cycle,
            subject=f"批量复制预设曲线（{len(copied_names)} 台）",
            summary=f"已将 {src_lbl} 的 96 点曲线复制至：{', '.join(copied_names[:6])}{' 等' if len(copied_names) > 6 else ''}",
        )
        self.store.append_runtime_log(
            level="info",
            log_type="parameters",
            target=f"同类设备 ({len(copied_names)} 台)",
            result="已批量复制",
            summary=f"由 {src_lbl} 复制 96 点预设曲线",
        )
        payload = self.get_device_configs_payload()
        payload["copied_count"] = len(copied_names)
        payload["updated_at"] = last_ts
        return payload
