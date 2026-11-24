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
    QinlingDataAdapter,
)
from agc_engine import (
    RenewableControlSettings,
    calculate_renewable_control_plan,
    prepare_resource_topology_static_context,
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

    def close(self) -> None:
        self.stop_background_loop()
        self.store.close()

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

        plan = calculate_renewable_control_plan(
            snapshot,
            settings,
            data_source="remote",
            snapshot_age_seconds=age_seconds,
            topology_static_context=topo_ctx,
            runtime_memory=runtime_memory,
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

        # 仅在闭环模式且数据质量允许时真正下发变动指令
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
                ok, msg, pt = self.adapter.dispatch_scada_command(d_type, d_name, s_type, s_val)
                st = "dispatched" if ok else "failed"
                dispatch_results.append(
                    {
                        "dev_type": d_type,
                        "dev_name": d_name,
                        "set_type": s_type,
                        "status": st,
                        "message": msg,
                    }
                )
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
            for rcmd in run_commands_to_send:
                d_type = str(rcmd.get("dev_type", ""))
                d_name = str(rcmd.get("dev_name", ""))
                s_val = int(bool(rcmd.get("run_stat", 1)))
                ok, msg, pt = self.adapter.dispatch_scada_command(d_type, d_name, "run_stat", s_val)
                st = "dispatched" if ok else "failed"
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
        self.store.append_command_records(audit_records)

        dispatched_ok = sum(1 for r in dispatch_results if r["status"] == "dispatched")
        dispatched_fail = sum(1 for r in dispatch_results if r["status"] == "failed")
        level = "warn" if (dispatched_fail > 0 or stale_blocked or dq.get("status") != "ok") else "info"
        trigger_cn = "自动周期" if trigger == "auto" else "单次计算"
        mode_cn = "闭环" if loop_mode == "closed" else "开环"
        self.store.append_control_log(
            level=level,
            event_type=trigger,
            cycle=cycle,
            subject=f"周期 #{cycle}（{trigger_cn} · {mode_cn}）",
            summary=f"{summary_line}｜下发成功 {dispatched_ok}，失败 {dispatched_fail}",
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

        ok, msg, pt = self.adapter.dispatch_scada_command(dev_type, dev_name, set_type, parsed_val)
        status = "dispatched" if ok else "failed"
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
        }
        self.store.append_command_records([record])
        with self.lock:
            self._latest_manual_commands[(dev_type, dev_name, set_type)] = record
        self.store.append_runtime_log(
            level="info" if ok else "warn",
            log_type="manual_command",
            target=f"{dev_name} ({set_type})",
            result="已下发" if ok else "失败",
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
            active = cmd_val is not None and row_status in {"dispatched", "calculated", "open_loop_only"}
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
            "yk": yk_rows,
            "yt": yt_rows,
            "recent_records": recent_records[:100],
        }
