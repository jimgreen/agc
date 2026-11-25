"""AGC SQLite 持久化数据访问层。

默认存储路径：``D:\\codex\\workspace\\realtime\\qinling\\sqldb\\agc\\agc.sqlite3``
（可通过 ``AGC_DB`` 环境变量或构造参数覆盖）。

所有表结构由 ``agc_schema.open_realtime_database`` 自动初始化并同步至
RTDB 原生结构目录（``rtdb_schema_*``），支持算法后台与 Web 服务并发访问。
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agc_datasources import DEFAULT_AGC_DB
from agc_schema import open_realtime_database


DEFAULT_BACKEND_RUNTIME_SETTINGS: Dict[str, Any] = {
    "backend_refresh_seconds": 1.0,
    "frame_age_limit_seconds": 30.0,
    "scada_command_timeout_seconds": 5.0,
    "trend_retention_points": 50000,
    "auto_start_on_boot": False,
}

DEFAULT_WEB_RUNTIME_SETTINGS: Dict[str, Any] = {
    "frontend_refresh_seconds": 1.5,
    "frontend_request_timeout_seconds": 15.0,
    "runtime_log_page_size": 20,
    "runtime_log_cache_limit": 300,
    "diagram_flow_electric_threshold_kw": 0.1,
    "diagram_flow_hydrogen_threshold_nm3_h": 0.1,
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _finite_or_none(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _finite_or_zero(value: Any) -> float:
    num = _finite_or_none(value)
    return num if num is not None else 0.0


class AGCRealtimeStore:
    """面向秦岭实时 AGC 的 SQLite 持久化存储。"""

    def __init__(self, path: str | Path = DEFAULT_AGC_DB) -> None:
        self.lock = threading.RLock()
        self.path, self.conn = open_realtime_database(Path(path))

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ------------------------------------------------------------------
    # 控制参数与运行参数配置
    # ------------------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.conn.execute(
                "SELECT value_json FROM agc_control_settings WHERE setting_key=?",
                (str(key),),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> str:
        now = _now_iso()
        encoded = json.dumps(value, ensure_ascii=False)
        with self.lock:
            self.conn.execute(
                """INSERT INTO agc_control_settings(setting_key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(setting_key) DO UPDATE SET
                     value_json=excluded.value_json,
                     updated_at=excluded.updated_at""",
                (str(key), encoded, now),
            )
            self.conn.commit()
        return now

    def get_all_settings(self) -> Dict[str, Any]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT setting_key, value_json, updated_at FROM agc_control_settings"
            ).fetchall()
        result: Dict[str, Any] = {}
        latest_updated = ""
        for r in rows:
            try:
                result[str(r["setting_key"])] = json.loads(r["value_json"])
            except json.JSONDecodeError:
                result[str(r["setting_key"])] = r["value_json"]
            if str(r["updated_at"]) > latest_updated:
                latest_updated = str(r["updated_at"])
        result["_updated_at"] = latest_updated
        return result

    def load_control_config(self) -> Dict[str, Any]:
        """读取已保存的 AGC 算法控制参数、策略预设与控制模式。"""
        saved = self.get_setting("control_parameters", {})
        mode = self.get_setting("control_mode", {})
        preset = self.get_setting("strategy_preset", "standard")
        return {
            "parameters": saved if isinstance(saved, dict) else {},
            "loop_mode": str((mode or {}).get("loop_mode", "open")) if isinstance(mode, dict) else "open",
            "auto_enabled": bool((mode or {}).get("auto_enabled", False)) if isinstance(mode, dict) else False,
            "preset": str(preset or "standard"),
        }

    def save_control_parameters(self, payload: Mapping[str, Any], preset: str = "custom") -> str:
        ts = self.set_setting("control_parameters", dict(payload))
        self.set_setting("strategy_preset", str(preset or "custom"))
        return ts

    def save_control_mode(self, loop_mode: str, auto_enabled: bool) -> str:
        return self.set_setting(
            "control_mode",
            {
                "loop_mode": "closed" if loop_mode == "closed" else "open",
                "auto_enabled": bool(auto_enabled),
            },
        )

    def load_runtime_settings(self) -> Dict[str, Any]:
        backend = dict(DEFAULT_BACKEND_RUNTIME_SETTINGS)
        saved_backend = self.get_setting("backend_runtime_settings", {})
        if isinstance(saved_backend, dict):
            backend.update(saved_backend)

        web = dict(DEFAULT_WEB_RUNTIME_SETTINGS)
        saved_web = self.get_setting("web_runtime_settings", {})
        if isinstance(saved_web, dict):
            web.update(saved_web)

        updated_at = self.get_setting("runtime_settings_updated_at", "")
        return {
            "backend": backend,
            "web": web,
            "updated_at": str(updated_at or ""),
        }

    def save_runtime_settings(
        self,
        *,
        backend: Optional[Mapping[str, Any]] = None,
        web: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = _now_iso()
        if backend is not None:
            merged_b = dict(DEFAULT_BACKEND_RUNTIME_SETTINGS)
            merged_b.update(backend)
            self.set_setting("backend_runtime_settings", merged_b)
        if web is not None:
            merged_w = dict(DEFAULT_WEB_RUNTIME_SETTINGS)
            merged_w.update(web)
            self.set_setting("web_runtime_settings", merged_w)
        self.set_setting("runtime_settings_updated_at", now)
        return self.load_runtime_settings()

    # ------------------------------------------------------------------
    # 控制策略快照、逐台设备策略与综合趋势曲线写入
    # ------------------------------------------------------------------

    def latest_cycle(self) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(cycle), 0) AS c FROM agc_strategy_snapshots"
            ).fetchone()
        return int(row["c"]) if row else 0

    def record_strategy_cycle(
        self,
        *,
        plan: Mapping[str, Any],
        loop_mode: str,
        auto_enabled: bool,
        sample_timestamp: str,
        environment: Mapping[str, Any],
        dispatch_results: Sequence[Mapping[str, Any]] = (),
        summary_text: str = "",
        epoch_ms: Optional[int] = None,
    ) -> int:
        """在单个 SQLite 事务中原子写入策略快照、设备策略明细与趋势曲线点。"""
        recorded_at = _now_iso()
        time_label = _now_hms()
        ts_ms = int(epoch_ms if epoch_ms is not None else round(time.time() * 1000))
        metrics = plan.get("metrics") if isinstance(plan.get("metrics"), Mapping) else {}
        dq = plan.get("dataQuality") if isinstance(plan.get("dataQuality"), Mapping) else {}
        command_rows = list(plan.get("commandRows") or [])
        decision_detail = list(plan.get("decisionDetail") or [])
        warnings = list(plan.get("warnings") or [])

        # 将闭环下发回执按 (dev_type, dev_name, set_type) 映射
        dispatch_map: Dict[Tuple[str, str, str], Mapping[str, Any]] = {}
        dispatched_count = 0
        for item in dispatch_results:
            key = (
                str(item.get("dev_type", "")),
                str(item.get("dev_name", "")),
                str(item.get("set_type", "")),
            )
            dispatch_map[key] = item
            if item.get("status") == "dispatched":
                dispatched_count += 1

        changed_count = sum(1 for r in command_rows if bool(r.get("strategyTargetChanged")))
        if not summary_text:
            summary_text = str(decision_detail[0]) if decision_detail else (
                f"第 {{cycle}} 周期｜{dq.get('status', 'ok')}｜策略 {len(command_rows)} 条（变动 {changed_count} 条）"
            )

        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT COALESCE(MAX(cycle), 0) AS c FROM agc_strategy_snapshots"
                ).fetchone()
                cycle = int(row["c"] if row else 0) + 1
                final_summary = summary_text.replace("{cycle}", str(cycle))

                # 1. 写入策略快照
                self.conn.execute(
                    """INSERT INTO agc_strategy_snapshots(
                        cycle, recorded_at, sample_timestamp, loop_mode, auto_enabled,
                        data_quality, dispatch_allowed, command_count, changed_count, dispatched_count,
                        load_kw, renewable_current_kw, renewable_target_kw, renewable_max_available_kw,
                        storage_current_kw, storage_target_kw, storage_soc,
                        diesel_current_kw, diesel_target_kw,
                        acdc_current_kw, acdc_target_kw,
                        electrolyzer_current_kw, electrolyzer_target_kw,
                        fuel_cell_current_kw, fuel_cell_target_kw,
                        hydrogen_soc, hydrogen_pressure_mpa,
                        wind_speed, solar_irradiance,
                        summary_text, metrics_json, decision_detail_json, warnings_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        cycle,
                        recorded_at,
                        str(sample_timestamp or recorded_at),
                        "closed" if loop_mode == "closed" else "open",
                        1 if auto_enabled else 0,
                        str(dq.get("status", "ok")),
                        1 if dq.get("dispatchAllowed") else 0,
                        len(command_rows),
                        changed_count,
                        dispatched_count,
                        _finite_or_zero(metrics.get("loadKw")),
                        _finite_or_zero(metrics.get("totalRenewableCurrentKw", metrics.get("renewableCurrent"))),
                        _finite_or_zero(metrics.get("totalRenewableTargetKw", metrics.get("renewableTarget"))),
                        _finite_or_zero(metrics.get("totalRenewableMaxAvailableKw", metrics.get("availableRenewable"))),
                        _finite_or_zero(metrics.get("totalStorageCurrentKw", metrics.get("storageCurrent"))),
                        _finite_or_zero(metrics.get("totalStorageTargetKw", metrics.get("storageTarget"))),
                        _finite_or_none(metrics.get("totalStorageSoc", metrics.get("storageSoc"))),
                        _finite_or_zero(metrics.get("totalDieselCurrentKw", metrics.get("dieselCurrentKw"))),
                        _finite_or_zero(metrics.get("totalDieselTargetKw", metrics.get("dieselTargetKw"))),
                        _finite_or_zero(metrics.get("acdcCurrentKw")),
                        _finite_or_zero(metrics.get("acdcTargetKw")),
                        _finite_or_zero(metrics.get("electrolyzerCurrentKw")),
                        _finite_or_zero(metrics.get("electrolyzerTargetKw")),
                        _finite_or_zero(metrics.get("fuelCellCurrentKw")),
                        _finite_or_zero(metrics.get("fuelCellTargetKw")),
                        _finite_or_none(metrics.get("hydrogenStorageSoc")),
                        _finite_or_none(metrics.get("hydrogenStoragePressureMpa")),
                        _finite_or_none(environment.get("wind_speed")),
                        _finite_or_none(environment.get("solar_irradiance")),
                        final_summary,
                        json.dumps(metrics, ensure_ascii=False, default=str),
                        json.dumps(decision_detail, ensure_ascii=False, default=str),
                        json.dumps(warnings, ensure_ascii=False, default=str),
                    ),
                )

                # 2. 写入逐台设备策略行
                cmd_tuples = []
                for crow in command_rows:
                    dev_type = str(crow.get("dev_type", ""))
                    dev_name = str(crow.get("dev_name", ""))
                    set_type = str(crow.get("set_type", ""))
                    cur_kw = _finite_or_none(crow.get("currentKw"))
                    tgt_kw = _finite_or_none(crow.get("targetKw", crow.get("commandKw")))
                    avail_kw = _finite_or_none(crow.get("weatherAvailableKw", crow.get("availableKw", crow.get("capacityKw"))))
                    delta_kw = (
                        round(tgt_kw - cur_kw, 4)
                        if (cur_kw is not None and tgt_kw is not None)
                        else None
                    )
                    dres = dispatch_map.get((dev_type, dev_name, set_type))
                    dev_ctrl_state = str(crow.get("deviceControlState") or "closed").strip().lower()
                    if dres:
                        d_status = str(dres.get("status", "calculated"))
                        d_msg = str(dres.get("message", ""))
                    elif dev_ctrl_state == "open":
                        d_status = "device_open"
                        d_msg = "设备开环（不参与决策/不下发）"
                    else:
                        d_status = "open_loop_only" if loop_mode != "closed" else "calculated"
                        d_msg = ""
                    cmd_tuples.append(
                        (
                            cycle,
                            recorded_at,
                            str(crow.get("category", "")),
                            dev_type,
                            dev_name,
                            str(crow.get("connectionSide", "")),
                            str(crow.get("mode", "")),
                            1 if crow.get("online", True) else 0,
                            1 if crow.get("commandable", False) else 0,
                            set_type,
                            cur_kw,
                            avail_kw,
                            tgt_kw,
                            delta_kw,
                            _finite_or_none(crow.get("soc")),
                            1 if crow.get("strategyTargetChanged") else 0,
                            str(crow.get("statusLabel", "")),
                            d_status,
                            d_msg,
                            json.dumps(crow, ensure_ascii=False, default=str),
                        )
                    )
                if cmd_tuples:
                    self.conn.executemany(
                        """INSERT INTO agc_strategy_commands(
                            cycle, recorded_at, category, dev_type, dev_name,
                            connection_side, control_mode, online, commandable, set_type,
                            current_kw, available_kw, target_kw, delta_kw, soc,
                            changed, status_label, dispatch_status, dispatch_message, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        cmd_tuples,
                    )

                # 3. 写入综合趋势曲线点
                ac_ren_cur = _finite_or_none(metrics.get("acRenewableCurrentKw"))
                ac_ren_tgt = _finite_or_none(metrics.get("acRenewableTargetKw"))
                dc_ren_cur = _finite_or_none(metrics.get("dcRenewableCurrentKw"))
                dc_ren_tgt = _finite_or_none(metrics.get("dcRenewableTargetKw"))
                wind_cur = _finite_or_none(metrics.get("totalWindCurrentKw"))
                if wind_cur is None:
                    wind_cur = _finite_or_zero(metrics.get("acWindCurrentKw")) + _finite_or_zero(metrics.get("dcWindCurrentKw"))
                wind_tgt = _finite_or_none(metrics.get("totalWindTargetKw"))
                if wind_tgt is None:
                    wind_tgt = _finite_or_zero(metrics.get("acWindTargetKw")) + _finite_or_zero(metrics.get("dcWindTargetKw"))
                pv_cur = _finite_or_none(metrics.get("totalPvCurrentKw"))
                if pv_cur is None:
                    pv_cur = _finite_or_zero(metrics.get("acPvCurrentKw")) + _finite_or_zero(metrics.get("dcPvCurrentKw"))
                pv_tgt = _finite_or_none(metrics.get("totalPvTargetKw"))
                if pv_tgt is None:
                    pv_tgt = _finite_or_zero(metrics.get("acPvTargetKw")) + _finite_or_zero(metrics.get("dcPvTargetKw"))

                tot_ren_cur = _finite_or_none(metrics.get("totalRenewableCurrentKw"))
                if tot_ren_cur is None:
                    tot_ren_cur = _finite_or_zero(ac_ren_cur) + _finite_or_zero(dc_ren_cur)
                tot_ren_tgt = _finite_or_none(metrics.get("totalRenewableTargetKw"))
                if tot_ren_tgt is None:
                    tot_ren_tgt = _finite_or_zero(ac_ren_tgt) + _finite_or_zero(dc_ren_tgt)
                tot_ren_max = _finite_or_none(metrics.get("totalRenewableMaxAvailableKw"))
                if tot_ren_max is None:
                    tot_ren_max = _finite_or_zero(metrics.get("acRenewableMaxAvailableKw")) + _finite_or_zero(metrics.get("dcRenewableMaxAvailableKw"))

                tot_st_cur = _finite_or_none(metrics.get("totalStorageCurrentKw"))
                if tot_st_cur is None:
                    tot_st_cur = _finite_or_zero(metrics.get("acStorageCurrentKw")) + _finite_or_zero(metrics.get("dcStorageCurrentKw"))
                tot_st_tgt = _finite_or_none(metrics.get("totalStorageTargetKw"))
                if tot_st_tgt is None:
                    tot_st_tgt = _finite_or_zero(metrics.get("acStorageTargetKw")) + _finite_or_zero(metrics.get("dcStorageTargetKw"))

                self.conn.execute(
                    """INSERT INTO agc_trend_points(
                        cycle, recorded_at, epoch_ms, time_label,
                        load_kw, ac_load_kw, dc_load_kw,
                        renewable_current_kw, renewable_target_kw, renewable_max_available_kw,
                        ac_renewable_current_kw, ac_renewable_target_kw,
                        dc_renewable_current_kw, dc_renewable_target_kw,
                        wind_current_kw, wind_target_kw, pv_current_kw, pv_target_kw,
                        storage_current_kw, storage_target_kw,
                        ac_storage_current_kw, ac_storage_target_kw,
                        dc_storage_current_kw, dc_storage_target_kw,
                        storage_soc, ac_storage_soc, dc_storage_soc,
                        diesel_current_kw, diesel_target_kw, diesel_min_kw,
                        acdc_current_kw, acdc_target_kw,
                        electrolyzer_current_kw, electrolyzer_target_kw,
                        fuel_cell_current_kw, fuel_cell_target_kw,
                        hydrogen_soc, hydrogen_pressure_mpa,
                        wind_speed, solar_irradiance
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        cycle,
                        recorded_at,
                        ts_ms,
                        time_label,
                        _finite_or_none(metrics.get("loadKw")),
                        _finite_or_none(metrics.get("acLoadKw")),
                        _finite_or_none(metrics.get("dcLoadKw")),
                        tot_ren_cur,
                        tot_ren_tgt,
                        tot_ren_max,
                        ac_ren_cur,
                        ac_ren_tgt,
                        dc_ren_cur,
                        dc_ren_tgt,
                        wind_cur,
                        wind_tgt,
                        pv_cur,
                        pv_tgt,
                        tot_st_cur,
                        tot_st_tgt,
                        _finite_or_none(metrics.get("acStorageCurrentKw")),
                        _finite_or_none(metrics.get("acStorageTargetKw")),
                        _finite_or_none(metrics.get("dcStorageCurrentKw")),
                        _finite_or_none(metrics.get("dcStorageTargetKw")),
                        _finite_or_none(metrics.get("totalStorageSoc", metrics.get("storageSoc"))),
                        _finite_or_none(metrics.get("acStorageSoc")),
                        _finite_or_none(metrics.get("dcStorageSoc")),
                        _finite_or_none(metrics.get("totalDieselCurrentKw", metrics.get("dieselCurrentKw"))),
                        _finite_or_none(metrics.get("totalDieselTargetKw", metrics.get("dieselTargetKw"))),
                        _finite_or_none(metrics.get("totalDieselMinKw", metrics.get("dieselMinKw"))),
                        _finite_or_none(metrics.get("acdcCurrentKw")),
                        _finite_or_none(metrics.get("acdcTargetKw")),
                        _finite_or_none(metrics.get("electrolyzerCurrentKw")),
                        _finite_or_none(metrics.get("electrolyzerTargetKw")),
                        _finite_or_none(metrics.get("fuelCellCurrentKw")),
                        _finite_or_none(metrics.get("fuelCellTargetKw")),
                        _finite_or_none(metrics.get("hydrogenStorageSoc")),
                        _finite_or_none(metrics.get("hydrogenStoragePressureMpa")),
                        _finite_or_none(environment.get("wind_speed")),
                        _finite_or_none(environment.get("solar_irradiance")),
                    ),
                )

                self.conn.commit()
                return cycle
            except BaseException:
                self.conn.rollback()
                raise

    # ------------------------------------------------------------------
    # 日志与指令审计写入
    # ------------------------------------------------------------------

    def append_control_log(
        self,
        *,
        level: str = "info",
        event_type: str = "cycle",
        cycle: int = 0,
        subject: str = "",
        summary: str = "",
        detail: str = "",
    ) -> int:
        now = _now_iso()
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO agc_control_logs(logged_at, level, event_type, cycle, subject, summary, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (now, str(level), str(event_type), int(cycle), str(subject), str(summary), str(detail)),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def append_runtime_log(
        self,
        *,
        level: str = "info",
        log_type: str = "system",
        target: str = "",
        result: str = "",
        summary: str = "",
    ) -> int:
        now = _now_iso()
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO agc_runtime_logs(logged_at, level, log_type, target, result, summary)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (now, str(level), str(log_type), str(target), str(result), str(summary)),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def append_command_records(self, records: Sequence[Mapping[str, Any]]) -> List[int]:
        if not records:
            return []
        now = _now_iso()
        now_ms = int(round(time.time() * 1000))
        inserted_ids: List[int] = []
        with self.lock:
            for r in records:
                cur = self.conn.execute(
                    """INSERT INTO agc_command_records(
                        issued_at, epoch_ms, cycle, origin, kind,
                        dev_type, dev_name, set_type, measured_value, command_value,
                        rtu_id, pnt_no, status, message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(r.get("issued_at") or now),
                        int(r.get("epoch_ms") or now_ms),
                        int(r.get("cycle") or 0),
                        str(r.get("origin") or "automatic"),
                        str(r.get("kind") or "yt"),
                        str(r.get("dev_type") or ""),
                        str(r.get("dev_name") or ""),
                        str(r.get("set_type") or ""),
                        _finite_or_none(r.get("measured_value")),
                        _finite_or_none(r.get("command_value")),
                        r.get("rtu_id"),
                        r.get("pnt_no"),
                        str(r.get("status") or "calculated"),
                        str(r.get("message") or ""),
                    ),
                )
                inserted_ids.append(int(cur.lastrowid or 0))
            self.conn.commit()
        return inserted_ids

    def update_command_record_status(
        self,
        record_id: int,
        *,
        status: str,
        message: str,
    ) -> None:
        """供独立回执监听线程异步回填指令审计终态（如 pending -> dispatched/failed/timeout）。"""
        if int(record_id or 0) <= 0:
            return
        with self.lock:
            self.conn.execute(
                "UPDATE agc_command_records SET status = ?, message = ? WHERE id = ?",
                (str(status), str(message), int(record_id)),
            )
            self.conn.commit()

    def update_strategy_command_dispatch_status(
        self,
        *,
        cycle: int,
        dev_type: str,
        dev_name: str,
        set_type: str,
        dispatch_status: str,
        dispatch_message: str,
    ) -> None:
        """同步回填指定周期策略表内对应设备的下发回执状态与快照成功计数。"""
        if int(cycle or 0) <= 0:
            return
        with self.lock:
            self.conn.execute(
                """UPDATE agc_strategy_commands
                   SET dispatch_status = ?, dispatch_message = ?
                   WHERE cycle = ? AND dev_type = ? AND dev_name = ? AND set_type = ?""",
                (
                    str(dispatch_status),
                    str(dispatch_message),
                    int(cycle),
                    str(dev_type),
                    str(dev_name),
                    str(set_type),
                ),
            )
            row = self.conn.execute(
                """SELECT COUNT(*) AS cnt FROM agc_strategy_commands
                   WHERE cycle = ? AND dispatch_status = 'dispatched'""",
                (int(cycle),),
            ).fetchone()
            ok_cnt = int(row["cnt"] if row else 0)
            self.conn.execute(
                "UPDATE agc_strategy_snapshots SET dispatched_count = ? WHERE cycle = ?",
                (ok_cnt, int(cycle)),
            )
            self.conn.commit()

    # ------------------------------------------------------------------
    # 查询接口（面向 Web HMI 曲线、日志、历史策略、指令追踪）
    # ------------------------------------------------------------------

    def get_latest_strategy(self) -> Optional[Dict[str, Any]]:
        with self.lock:
            snap = self.conn.execute(
                "SELECT * FROM agc_strategy_snapshots ORDER BY cycle DESC LIMIT 1"
            ).fetchone()
            if snap is None:
                return None
            cycle = int(snap["cycle"])
            cmd_rows = self.conn.execute(
                "SELECT * FROM agc_strategy_commands WHERE cycle=? ORDER BY id ASC",
                (cycle,),
            ).fetchall()
        return {
            "cycle": cycle,
            "recorded_at": snap["recorded_at"],
            "sample_timestamp": snap["sample_timestamp"],
            "loop_mode": snap["loop_mode"],
            "auto_enabled": bool(snap["auto_enabled"]),
            "data_quality": snap["data_quality"],
            "dispatch_allowed": bool(snap["dispatch_allowed"]),
            "command_count": snap["command_count"],
            "changed_count": snap["changed_count"],
            "dispatched_count": snap["dispatched_count"],
            "summary_text": snap["summary_text"],
            "metrics": json.loads(snap["metrics_json"] or "{}"),
            "decision_detail": json.loads(snap["decision_detail_json"] or "[]"),
            "warnings": json.loads(snap["warnings_json"] or "[]"),
            "command_rows": [
                {
                    "category": r["category"],
                    "dev_type": r["dev_type"],
                    "dev_name": r["dev_name"],
                    "connection_side": r["connection_side"],
                    "control_mode": r["control_mode"],
                    "device_control_state": (
                        json.loads(r["raw_json"] or "{}").get("deviceControlState", "closed")
                        if r["raw_json"]
                        else "closed"
                    ),
                    "online": bool(r["online"]),
                    "commandable": bool(r["commandable"]),
                    "set_type": r["set_type"],
                    "current_kw": r["current_kw"],
                    "available_kw": r["available_kw"],
                    "target_kw": r["target_kw"],
                    "delta_kw": r["delta_kw"],
                    "soc": r["soc"],
                    "changed": bool(r["changed"]),
                    "status_label": r["status_label"],
                    "dispatch_status": r["dispatch_status"],
                    "dispatch_message": r["dispatch_message"],
                }
                for r in cmd_rows
            ],
        }

    def query_trend(
        self,
        *,
        window_minutes: int = 60,
        end_epoch_ms: Optional[int] = None,
        limit: int = 600,
    ) -> Dict[str, Any]:
        """查询综合功率与 SOC 趋势曲线，支持时间窗与翻页锚点。"""
        win_ms = max(1, int(window_minutes)) * 60 * 1000
        with self.lock:
            bounds = self.conn.execute(
                "SELECT MIN(epoch_ms) AS min_ms, MAX(epoch_ms) AS max_ms, COUNT(*) AS total FROM agc_trend_points"
            ).fetchone()
            total = int(bounds["total"] or 0)
            if total == 0:
                return {"points": [], "total": 0, "min_epoch_ms": None, "max_epoch_ms": None, "start_epoch_ms": None, "end_epoch_ms": None}
            max_ms = int(bounds["max_ms"])
            min_ms = int(bounds["min_ms"])
            anchor_end = int(end_epoch_ms) if end_epoch_ms is not None else max_ms
            anchor_start = anchor_end - win_ms
            rows = self.conn.execute(
                """SELECT * FROM agc_trend_points
                   WHERE epoch_ms BETWEEN ? AND ?
                   ORDER BY epoch_ms ASC""",
                (anchor_start, anchor_end),
            ).fetchall()
            if not rows and end_epoch_ms is None:
                rows = self.conn.execute(
                    "SELECT * FROM agc_trend_points ORDER BY epoch_ms DESC LIMIT ?",
                    (max(10, int(limit)),),
                ).fetchall()
                rows = list(reversed(rows))

        # 若点数超出 limit，做均匀抽稀保留首尾
        if len(rows) > limit and limit >= 10:
            step = (len(rows) - 1) / float(limit - 1)
            sampled = [rows[int(round(i * step))] for i in range(limit)]
        else:
            sampled = rows

        return {
            "points": [dict(r) for r in sampled],
            "total": total,
            "min_epoch_ms": min_ms,
            "max_epoch_ms": max_ms,
            "start_epoch_ms": anchor_start,
            "end_epoch_ms": anchor_end,
            "has_earlier": anchor_start > min_ms,
            "has_later": anchor_end < max_ms,
        }

    def query_control_logs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        level: str = "",
        keyword: str = "",
    ) -> Dict[str, Any]:
        page = max(1, int(page))
        page_size = max(5, min(200, int(page_size)))
        clauses: List[str] = []
        params: List[Any] = []
        if level and level != "all":
            clauses.append("level = ?")
            params.append(level)
        if keyword.strip():
            kw = f"%{keyword.strip()}%"
            clauses.append("(subject LIKE ? OR summary LIKE ? OR detail LIKE ?)")
            params.extend([kw, kw, kw])
        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.lock:
            total = int(
                self.conn.execute(
                    f"SELECT COUNT(*) AS c FROM agc_control_logs{where_sql}", params
                ).fetchone()["c"]
            )
            offset = (page - 1) * page_size
            rows = self.conn.execute(
                f"SELECT * FROM agc_control_logs{where_sql} ORDER BY seq DESC LIMIT ? OFFSET ?",
                [*params, page_size, offset],
            ).fetchall()
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, math.ceil(total / page_size)) if total else 1,
        }

    def query_runtime_logs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        log_type: str = "",
        only_alert: bool = False,
        keyword: str = "",
    ) -> Dict[str, Any]:
        page = max(1, int(page))
        page_size = max(5, min(200, int(page_size)))
        clauses: List[str] = []
        params: List[Any] = []
        if log_type and log_type != "all":
            clauses.append("log_type = ?")
            params.append(log_type)
        if only_alert:
            clauses.append("level IN ('warn', 'warning', 'error', 'critical')")
        if keyword.strip():
            kw = f"%{keyword.strip()}%"
            clauses.append("(target LIKE ? OR result LIKE ? OR summary LIKE ?)")
            params.extend([kw, kw, kw])
        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.lock:
            total = int(
                self.conn.execute(
                    f"SELECT COUNT(*) AS c FROM agc_runtime_logs{where_sql}", params
                ).fetchone()["c"]
            )
            types = [
                str(r["log_type"])
                for r in self.conn.execute(
                    "SELECT DISTINCT log_type FROM agc_runtime_logs ORDER BY log_type"
                ).fetchall()
            ]
            offset = (page - 1) * page_size
            rows = self.conn.execute(
                f"SELECT * FROM agc_runtime_logs{where_sql} ORDER BY seq DESC LIMIT ? OFFSET ?",
                [*params, page_size, offset],
            ).fetchall()
        return {
            "items": [dict(r) for r in rows],
            "types": types,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, math.ceil(total / page_size)) if total else 1,
        }

    def clear_runtime_logs(self) -> int:
        with self.lock:
            cur = self.conn.execute("DELETE FROM agc_runtime_logs")
            self.conn.commit()
            return int(cur.rowcount or 0)

    def clear_control_logs(self) -> int:
        with self.lock:
            cur = self.conn.execute("DELETE FROM agc_control_logs")
            self.conn.commit()
            return int(cur.rowcount or 0)

    def query_command_records(
        self,
        *,
        kind: str = "",
        origin: str = "",
        dev_type: str = "",
        dev_name: str = "",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if kind and kind != "all":
            clauses.append("kind = ?")
            params.append(kind)
        if origin and origin != "all":
            clauses.append("origin = ?")
            params.append(origin)
        if dev_type and dev_type != "all":
            clauses.append("dev_type = ?")
            params.append(dev_type)
        if dev_name:
            clauses.append("dev_name = ?")
            params.append(dev_name)
        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.lock:
            rows = self.conn.execute(
                f"SELECT * FROM agc_command_records{where_sql} ORDER BY id DESC LIMIT ?",
                [*params, max(1, min(2000, int(limit)))],
            ).fetchall()
        return [dict(r) for r in rows]

    def query_command_trace(
        self,
        *,
        dev_type: str,
        dev_name: str,
        set_type: str,
        window_minutes: int = 60,
    ) -> List[Dict[str, Any]]:
        """查询单条设备指令的实时值与控制值跟踪曲线。"""
        cutoff = int(round(time.time() * 1000)) - max(1, int(window_minutes)) * 60 * 1000
        with self.lock:
            rows = self.conn.execute(
                """SELECT issued_at, epoch_ms, origin, measured_value, command_value, status
                   FROM agc_command_records
                   WHERE dev_type=? AND dev_name=? AND set_type=? AND epoch_ms >= ?
                   ORDER BY epoch_ms ASC LIMIT 600""",
                (dev_type, dev_name, set_type, cutoff),
            ).fetchall()
            if not rows:
                rows = self.conn.execute(
                    """SELECT issued_at, epoch_ms, origin, measured_value, command_value, status
                       FROM agc_command_records
                       WHERE dev_type=? AND dev_name=? AND set_type=?
                       ORDER BY epoch_ms DESC LIMIT 120""",
                    (dev_type, dev_name, set_type),
                ).fetchall()
                rows = list(reversed(rows))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 受控设备控制配置（上下限/步长/死区/闭环-跟踪-开环状态）与 96 点预设曲线
    # ------------------------------------------------------------------

    def load_device_configs(self) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
        """读取全部已持久化的受控设备控制配置，返回 {(dev_type, dev_name, set_type): cfg}。"""
        with self.lock:
            rows = self.conn.execute(
                """SELECT dev_type, dev_name, set_type, control_state,
                          p_min, p_max, control_step, control_deadband,
                          curve_interp, updated_at
                   FROM agc_device_configs
                   ORDER BY dev_type, dev_name, set_type"""
            ).fetchall()
        out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for r in rows:
            state = str(r["control_state"] or "closed").strip().lower()
            if state not in {"closed", "track", "open"}:
                state = "closed"
            interp = str(r["curve_interp"] or "step").strip().lower()
            if interp not in {"step", "linear"}:
                interp = "step"
            key = (str(r["dev_type"]), str(r["dev_name"]), str(r["set_type"] or "p_set"))
            out[key] = {
                "dev_type": key[0],
                "dev_name": key[1],
                "set_type": key[2],
                "control_state": state,
                "p_min": _finite_or_none(r["p_min"]),
                "p_max": _finite_or_none(r["p_max"]),
                "control_step": _finite_or_none(r["control_step"]),
                "control_deadband": max(0.0, _finite_or_zero(r["control_deadband"])),
                "curve_interp": interp,
                "updated_at": str(r["updated_at"] or ""),
            }
        return out

    def save_device_configs(self, items: Sequence[Mapping[str, Any]]) -> Tuple[int, str]:
        """事务批量保存受控设备控制配置（参数非法时整体回滚）。"""
        now = _now_iso()
        normalized: List[Tuple[Any, ...]] = []
        for raw in items:
            d_type = str(raw.get("dev_type") or "").strip()
            d_name = str(raw.get("dev_name") or "").strip()
            s_type = str(raw.get("set_type") or "p_set").strip()
            if not d_type or not d_name or not s_type:
                raise ValueError("设备配置缺少 dev_type / dev_name / set_type")
            state = str(raw.get("control_state") or "closed").strip().lower()
            if state not in {"closed", "track", "open"}:
                raise ValueError(f"{d_name} 的控制状态无效：{state}（仅允许 closed / track / open）")
            p_min_raw = raw.get("p_min")
            p_max_raw = raw.get("p_max")
            step_raw = raw.get("control_step")
            dead_raw = raw.get("control_deadband", 0.0)
            p_min = None if (p_min_raw is None or str(p_min_raw).strip() == "") else _finite_or_none(p_min_raw)
            p_max = None if (p_max_raw is None or str(p_max_raw).strip() == "") else _finite_or_none(p_max_raw)
            step = None if (step_raw is None or str(step_raw).strip() == "") else _finite_or_none(step_raw)
            dead = 0.0 if (dead_raw is None or str(dead_raw).strip() == "") else _finite_or_none(dead_raw)
            if p_min_raw not in (None, "") and p_min is None:
                raise ValueError(f"{d_name} 下限必须为有效数值")
            if p_max_raw not in (None, "") and p_max is None:
                raise ValueError(f"{d_name} 上限必须为有效数值")
            if step_raw not in (None, "") and (step is None or step < 0.0):
                raise ValueError(f"{d_name} 控制步长必须为非负数值")
            if dead is None or dead < 0.0:
                raise ValueError(f"{d_name} 控制死区必须为非负数值")
            if p_min is not None and p_max is not None and p_min > p_max + 1e-9:
                raise ValueError(f"{d_name} 下限 ({p_min}) 不能大于上限 ({p_max})")
            interp = str(raw.get("curve_interp") or "step").strip().lower()
            if interp not in {"step", "linear"}:
                interp = "step"
            normalized.append((d_type, d_name, s_type, state, p_min, p_max, step, float(dead), interp, now))

        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.executemany(
                    """INSERT INTO agc_device_configs(
                           dev_type, dev_name, set_type, control_state,
                           p_min, p_max, control_step, control_deadband,
                           curve_interp, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(dev_type, dev_name, set_type) DO UPDATE SET
                           control_state=excluded.control_state,
                           p_min=excluded.p_min,
                           p_max=excluded.p_max,
                           control_step=excluded.control_step,
                           control_deadband=excluded.control_deadband,
                           curve_interp=excluded.curve_interp,
                           updated_at=excluded.updated_at""",
                    normalized,
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return len(normalized), now

    def reset_device_configs(
        self,
        *,
        dev_type: str = "",
        dev_name: str = "",
        set_type: str = "",
        reset_curves: bool = False,
    ) -> int:
        """恢复单个或全部设备的控制配置为系统默认状态。"""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                if dev_type and dev_name and set_type:
                    cur = self.conn.execute(
                        "DELETE FROM agc_device_configs WHERE dev_type=? AND dev_name=? AND set_type=?",
                        (dev_type, dev_name, set_type),
                    )
                    if reset_curves:
                        self.conn.execute(
                            "DELETE FROM agc_device_curves WHERE dev_type=? AND dev_name=? AND set_type=?",
                            (dev_type, dev_name, set_type),
                        )
                else:
                    cur = self.conn.execute("DELETE FROM agc_device_configs")
                    if reset_curves:
                        self.conn.execute("DELETE FROM agc_device_curves")
                self.conn.commit()
                return int(cur.rowcount or 0)
            except Exception:
                self.conn.rollback()
                raise

    def load_device_curve(
        self,
        dev_type: str,
        dev_name: str,
        set_type: str = "p_set",
    ) -> Optional[List[float]]:
        """读取单台受控设备的 96 点预设曲线；若未配置或点数不完整返回 None。"""
        with self.lock:
            rows = self.conn.execute(
                """SELECT point_index, target_value
                   FROM agc_device_curves
                   WHERE dev_type=? AND dev_name=? AND set_type=?
                   ORDER BY point_index ASC""",
                (dev_type, dev_name, set_type),
            ).fetchall()
        if len(rows) != 96:
            return None
        pts: List[float] = [0.0] * 96
        for r in rows:
            idx = int(r["point_index"])
            if 0 <= idx < 96:
                pts[idx] = _finite_or_zero(r["target_value"])
        return pts

    def load_all_device_curves(self) -> Dict[Tuple[str, str, str], List[float]]:
        """一次性加载全部已保存的完整 96 点预设曲线（用于控制周期零延迟评估）。"""
        with self.lock:
            rows = self.conn.execute(
                """SELECT dev_type, dev_name, set_type, point_index, target_value
                   FROM agc_device_curves
                   ORDER BY dev_type, dev_name, set_type, point_index ASC"""
            ).fetchall()
        grouped: Dict[Tuple[str, str, str], Dict[int, float]] = {}
        for r in rows:
            key = (str(r["dev_type"]), str(r["dev_name"]), str(r["set_type"] or "p_set"))
            idx = int(r["point_index"])
            if 0 <= idx < 96:
                grouped.setdefault(key, {})[idx] = _finite_or_zero(r["target_value"])
        out: Dict[Tuple[str, str, str], List[float]] = {}
        for key, pts_map in grouped.items():
            if len(pts_map) == 96:
                out[key] = [pts_map[i] for i in range(96)]
        return out

    def save_device_curve(
        self,
        *,
        dev_type: str,
        dev_name: str,
        set_type: str = "p_set",
        points: Sequence[Any],
        curve_interp: Optional[str] = None,
    ) -> str:
        """原子保存指定受控设备的 96 点预设曲线（校验 96 个有限实数）。"""
        d_type = str(dev_type or "").strip()
        d_name = str(dev_name or "").strip()
        s_type = str(set_type or "p_set").strip()
        if not d_type or not d_name or not s_type:
            raise ValueError("保存预设曲线缺少 dev_type / dev_name / set_type")
        if len(points) != 96:
            raise ValueError(f"预设曲线必须包含 96 个采样点（当前 {len(points)} 个）")
        cleaned: List[float] = []
        for idx, raw_v in enumerate(points):
            v = _finite_or_none(raw_v)
            if v is None:
                raise ValueError(f"预设曲线第 {idx + 1} 点数值无效")
            cleaned.append(float(v))
        now = _now_iso()
        rows_to_write = [
            (d_type, d_name, s_type, i, i * 15, cleaned[i], now)
            for i in range(96)
        ]
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.executemany(
                    """INSERT INTO agc_device_curves(
                           dev_type, dev_name, set_type, point_index, minute_of_day, target_value, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(dev_type, dev_name, set_type, point_index) DO UPDATE SET
                           minute_of_day=excluded.minute_of_day,
                           target_value=excluded.target_value,
                           updated_at=excluded.updated_at""",
                    rows_to_write,
                )
                if curve_interp in {"step", "linear"}:
                    self.conn.execute(
                        """INSERT INTO agc_device_configs(
                               dev_type, dev_name, set_type, control_state,
                               p_min, p_max, control_step, control_deadband,
                               curve_interp, updated_at
                           ) VALUES (?, ?, ?, 'closed', NULL, NULL, NULL, 0, ?, ?)
                           ON CONFLICT(dev_type, dev_name, set_type) DO UPDATE SET
                               curve_interp=excluded.curve_interp,
                               updated_at=excluded.updated_at""",
                        (d_type, d_name, s_type, curve_interp, now),
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return now
