"""秦岭微电网 AGC 算法后台、持久化存储与 Web HMI 自动化测试。

全部测试在临时目录内运行，使用隔离 SQLite 数据库与模拟四遥通道，
不修改真实模型/实时库，也不向现场 SCADA 服务发送任何指令。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from agc_datasources import QinlingDataAdapter, resolve_svg_file
from agc_schema import open_realtime_database
from agc_service import AGCRealtimeService
from agc_store import AGCRealtimeStore
from web.agc_server import create_server


MINI_MODEL_E = """<Model>
@ path name p_base u_unit p_unit i_unit
# test qinling_mini 100 V kW A
</Model>

<ACNode>
@ idx name vbase run_stat
# 1 AC母线节点 380 1
# 2 交流风电节点 380 1
# 3 交流储能节点 380 1
# 4 交流柴发节点 380 1
# 5 交流负荷节点 380 1
</ACNode>

<ACRealBs>
@ idx name dev_type node run_stat v_max v_min
# 1 交流母线-1 ac-bus-vertical 1 1 1.1 0.9
</ACRealBs>

<ACBreak>
@ idx name dev_type i_node j_node status run_stat rated_capacity i_max
# 1 开关-风电 ac-box-breaker 1 2 1 1 0 0
# 2 开关-储能 ac-box-breaker 1 3 1 1 0 0
# 3 开关-柴发 ac-box-breaker 1 4 1 1 0 0
# 4 开关-负荷 ac-box-breaker 1 5 1 1 0 0
</ACBreak>

<ACGenerator>
@ idx name dev_type node control_type p_set p_max p_min q_set q_max q_min v_set alpha run_stat rated_capacity rated_voltage v_max v_min
# 1 交流风电-1 ac-wind-source 2 PQ 20 50 0 0 0 0 380 1.0 1 50 380 1.1 0.9
# 2 交流储能-1 ac-storage 3 PQ 10 100 -100 0 0 0 380 1.0 1 100 380 1.1 0.9
# 3 交流柴发-1 ac-diesel-source 4 PH 120 300 70 0 0 0 380 1.0 1 300 380 1.1 0.9
</ACGenerator>

<ACLoad>
@ idx name dev_type node p_set p_max p_min q_max q_min run_stat rated_capacity pbase pv0 pv1 pv2 qbase qv0 qv1 qv2 v_max v_min
# 1 交流负荷-1 ac-load 5 150 200 0 0 0 1 200 150 1 0 0 0 1 0 0 1.1 0.9
</ACLoad>

<ACWindGen>
@ idx idx_acgenerator wind_turbine_model cut_in_wind_speed rated_wind_speed cut_out_wind_speed rotor_diameter hub_height
# 1 1 WT-50 3 12 25 20 30
</ACWindGen>

<ACStorageGen>
@ idx idx_acgenerator storage_technology battery_rack_count energy_capacity charge_discharge_efficiency max_charge_power max_discharge_power state_of_charge soc_upper_limit soc_lower_limit
# 1 2 lithium 10 200 90 100 100 50 90 0.2
</ACStorageGen>

<ACDieselGen>
@ idx idx_acgenerator diesel_unit_model fuel_grade specific_fuel_consumption fuel_tank_capacity rated_speed start_time
# 1 3 DG-300 0# 200 20 1500 10
</ACDieselGen>
"""

MINI_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600">
<g id="ACGenerator_Layer">
  <use id="ACGenerator-1" name="交流风电-1" x="100" y="100" width="80" height="60"/>
  <use id="ACGenerator-2" name="交流储能-1" x="300" y="100" width="80" height="60"/>
  <use id="ACGenerator-3" name="交流柴发-1" x="500" y="100" width="80" height="60"/>
</g>
</svg>"""


def _build_fixture_env(root: Path) -> dict[str, Path]:
    model_dir = root / "model"
    scada_dir = root / "scada"
    agc_dir = root / "agc"
    model_dir.mkdir(parents=True)
    scada_dir.mkdir(parents=True)
    agc_dir.mkdir(parents=True)

    e_file = model_dir / "qinling.e"
    svg_file = model_dir / "qinling.svg"
    e_file.write_text(MINI_MODEL_E, encoding="utf-8")
    svg_file.write_text(MINI_SVG, encoding="utf-8")

    # 构造轻量 scdfe_d.sqlite3
    scada_db = scada_dir / "scdfe_d.sqlite3"
    conn = sqlite3.connect(scada_db)
    conn.executescript(
        """
        CREATE TABLE ACGenerator (idx INTEGER PRIMARY KEY, name TEXT, p REAL, q REAL, uab REAL, ia REAL, run INTEGER);
        INSERT INTO ACGenerator VALUES (1, '交流风电-1', 20.0, 0.0, 380.0, 30.0, 1);
        INSERT INTO ACGenerator VALUES (2, '交流储能-1', 10.0, 0.0, 380.0, 15.0, 1);
        INSERT INTO ACGenerator VALUES (3, '交流柴发-1', 120.0, 0.0, 380.0, 180.0, 1);
        CREATE TABLE ACLoad (idx INTEGER PRIMARY KEY, name TEXT, p REAL, q REAL, uab REAL, ia REAL, run INTEGER);
        INSERT INTO ACLoad VALUES (1, '交流负荷-1', 150.0, 0.0, 380.0, 220.0, 1);
        CREATE TABLE ACWindGen (idx INTEGER PRIMARY KEY, name TEXT, wind_speed REAL);
        INSERT INTO ACWindGen VALUES (1, '交流风电-1', 11.0);
        CREATE TABLE ACStorageGen (idx INTEGER PRIMARY KEY, name TEXT, soc REAL);
        INSERT INTO ACStorageGen VALUES (1, '交流储能-1', 0.65);
        CREATE TABLE ACBreak (idx INTEGER PRIMARY KEY, name TEXT, position INTEGER, ia REAL);
        INSERT INTO ACBreak VALUES (1, '开关-风电', 1, 30.0);
        INSERT INTO ACBreak VALUES (2, '开关-储能', 1, 15.0);
        INSERT INTO ACBreak VALUES (3, '开关-柴发', 1, 180.0);
        INSERT INTO ACBreak VALUES (4, '开关-负荷', 1, 220.0);
        """
    )
    conn.commit()
    conn.close()

    # 构造点表 point_catalog.json
    catalog = {
        "points": [
            {"kind": "yt", "rtu_id": 1, "pnt_no": 1, "model_table": "ACGenerator", "model_idx": 1, "device_name": "交流风电-1", "dev_type": "ac-wind-source", "metric": "p_set", "name": "交流风电-1 / 有功给定", "unit": "kW"},
            {"kind": "yk", "rtu_id": 1, "pnt_no": 1, "model_table": "ACGenerator", "model_idx": 1, "device_name": "交流风电-1", "dev_type": "ac-wind-source", "metric": "start_stop", "name": "交流风电-1 / 启停"},
            {"kind": "yt", "rtu_id": 1, "pnt_no": 2, "model_table": "ACGenerator", "model_idx": 2, "device_name": "交流储能-1", "dev_type": "ac-storage", "metric": "p_set", "name": "交流储能-1 / 有功给定", "unit": "kW"},
            {"kind": "yt", "rtu_id": 1, "pnt_no": 3, "model_table": "ACGenerator", "model_idx": 3, "device_name": "交流柴发-1", "dev_type": "ac-diesel-source", "metric": "p_set", "name": "交流柴发-1 / 有功给定", "unit": "kW"},
        ]
    }
    (scada_dir / "point_catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    return {
        "model_db": model_dir / "missing_devices.sqlite3",  # 触发 .e 回退加载
        "scada_db": scada_db,
        "svg_path": e_file,  # 传 .e 应自动关联 qinling.svg
        "agc_db": agc_dir / "agc.sqlite3",
    }


class AGCRealtimeSystemTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = _build_fixture_env(Path(self._tmp.name))

    def test_resolve_svg_from_e_path(self):
        svg_file, e_file = resolve_svg_file(self.env["svg_path"])
        self.assertIsNotNone(svg_file)
        self.assertEqual(svg_file.name, "qinling.svg")
        self.assertEqual(e_file.name, "qinling.e")

    def test_realtime_database_registers_rtdb_catalog(self):
        path, conn = open_realtime_database(self.env["agc_db"])
        self.addCleanup(conn.close)
        self.assertTrue(path.is_file())
        tables = [
            row[0]
            for row in conn.execute("SELECT table_name FROM rtdb_schema_tables ORDER BY table_name").fetchall()
        ]
        self.assertEqual(
            tables,
            [
                "agc_command_records",
                "agc_control_logs",
                "agc_control_settings",
                "agc_runtime_logs",
                "agc_strategy_commands",
                "agc_strategy_snapshots",
                "agc_trend_points",
            ],
        )

    def test_service_open_and_closed_loop_control_and_persistence(self):
        sent_commands = []

        def fake_sender(kind: str, rtu: int, pnt: int, val) -> bool:
            sent_commands.append((kind, rtu, pnt, val))
            return True

        svc = AGCRealtimeService(
            model_db=self.env["model_db"],
            scada_db=self.env["scada_db"],
            svg_path=self.env["svg_path"],
            agc_db=self.env["agc_db"],
            command_sender=fake_sender,
            start_background=False,
        )
        self.addCleanup(svc.close)

        # 1. 默认开环模式单次计算：应持久化策略与趋势，但绝不发四遥指令
        res_open = svc.run_control_cycle(trigger="manual")
        self.assertEqual(res_open["cycle"], 1)
        self.assertEqual(res_open["loop_mode"], "open")
        self.assertEqual(len(sent_commands), 0)
        self.assertGreater(res_open["latest"]["command_count"], 0)

        # 2. 更新控制参数与预设并校验持久化
        updated = svc.update_control_parameters({"interval_seconds": 3.0}, preset="aggressive_renewable")
        self.assertAlmostEqual(updated["settings"]["interval_seconds"], 3.0)
        self.assertEqual(updated["preset"], "aggressive_renewable")

        # 3. 切闭环并计算一次：应通过 fake_sender 下发变动指令并记入指令审计表
        svc.set_loop_mode("closed")
        res_closed = svc.run_control_cycle(trigger="manual")
        self.assertEqual(res_closed["cycle"], 2)
        self.assertGreaterEqual(len(sent_commands), 1)

        # 4. 校验趋势、控制日志与人工遥控/遥调审计
        trend = svc.store.query_trend(window_minutes=60)
        self.assertEqual(len(trend["points"]), 2)
        logs = svc.store.query_control_logs(page=1, page_size=20)
        self.assertGreaterEqual(logs["total"], 3)

        manual_res = svc.execute_manual_command(
            dev_type="ACGenerator", dev_name="交流风电-1", set_type="p_set", value=25.5
        )
        self.assertTrue(manual_res["ok"])
        self.assertEqual(sent_commands[-1], ("yt", 1, 1, 25.5))

    def test_http_server_routes_and_static_hmi(self):
        sent = []
        server, service = create_server(
            host="127.0.0.1",
            port=0,
            model_db=self.env["model_db"],
            scada_db=self.env["scada_db"],
            svg_path=self.env["svg_path"],
            agc_db=self.env["agc_db"],
            command_sender=lambda k, r, p, v: (sent.append((k, r, p, v)) or True),
            start_background=False,
        )
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(service.close)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        base = f"http://127.0.0.1:{port}"

        def get_json(path: str):
            with urlopen(f"{base}{path}", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                return json.loads(resp.read().decode("utf-8"))

        def post_json(path: str, payload: dict):
            req = Request(
                f"{base}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                return json.loads(resp.read().decode("utf-8"))

        health = get_json("/api/health")
        self.assertTrue(health["ok"])
        self.assertEqual(health["service"], "qinling-agc")

        snap = get_json("/api/snapshot")
        self.assertGreaterEqual(len(snap["devices"]), 3)

        calc = post_json("/api/agc/control", {"action": "calculate_once"})
        self.assertTrue(calc["ok"])
        self.assertEqual(calc["control"]["cycle"], 1)

        trend = get_json("/api/agc/trend?window=60")
        self.assertEqual(len(trend["points"]), 1)

        cmds = get_json("/api/commands")
        self.assertGreater(len(cmds["yt"]), 0)

        # 验证 SVG 与前端静态资源
        with urlopen(f"{base}/api/diagram/svg", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("<svg", resp.read().decode("utf-8"))

        for static_path in ("/", "/styles.css", "/js/00-i18n.js", "/js/03-agc-control.js", "/js/05-bootstrap.js"):
            with urlopen(f"{base}{static_path}", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertGreater(len(resp.read()), 100)

    def test_weather_near_zero_gate_without_max_available_cap(self):
        """验证不使用气象估算最大可发作为控制上限，且仅在有效风速/辐照接近 0 时禁止对应设备增发。"""
        from agc_engine.control_parts.planner import _apply_renewable_natural_upper_bounds
        from agc_engine import RenewableControlSettings

        settings = RenewableControlSettings()

        def make_rows():
            return [
                {
                    "dev_type": "ACGenerator",
                    "dev_name": "风机-1",
                    "technology": "wind",
                    "capacityKw": 100.0,
                    "currentKw": 20.0,
                    "planningCurrentKw": 20.0,
                    "commandable": True,
                    "weatherMeasurementAvailable": True,
                    "weatherAvailableKw": 5.0,  # 故意设极低的不准估算值
                },
                {
                    "dev_type": "DCGenerator",
                    "dev_name": "光伏-1",
                    "technology": "pv",
                    "capacityKw": 100.0,
                    "currentKw": 20.0,
                    "planningCurrentKw": 20.0,
                    "commandable": True,
                    "weatherMeasurementAvailable": True,
                    "weatherAvailableKw": 5.0,  # 故意设极低的不准估算值
                },
            ]

        # 1) 有效非零风速/辐照：不能以估算最大可发封顶，两者均不设 naturalUpperKw
        rows = make_rows()
        targets = {("ACGenerator", "风机-1"): 23.0, ("DCGenerator", "光伏-1"): 23.0}
        _apply_renewable_natural_upper_bounds(
            rows,
            targets,
            settings,
            runtime_memory=None,
            sim_time_seconds=10.0,
            wind_measurement_status={"available": True, "value": 4.0},
            solar_measurement_status={"available": True, "value": 120.0},
            diesel_guard_blocked=False,
        )
        self.assertIsNone(rows[0].get("naturalUpperKw"))
        self.assertIsNone(rows[1].get("naturalUpperKw"))
        self.assertAlmostEqual(targets[("ACGenerator", "风机-1")], 23.0)
        self.assertAlmostEqual(targets[("DCGenerator", "光伏-1")], 23.0)

        # 2) 有效风速接近 0：仅风机禁增发（<= currentKw），光伏仍可增发
        rows = make_rows()
        targets = {("ACGenerator", "风机-1"): 23.0, ("DCGenerator", "光伏-1"): 23.0}
        _apply_renewable_natural_upper_bounds(
            rows,
            targets,
            settings,
            runtime_memory=None,
            sim_time_seconds=12.0,
            wind_measurement_status={"available": True, "value": 0.2},
            solar_measurement_status={"available": True, "value": 300.0},
            diesel_guard_blocked=False,
        )
        self.assertAlmostEqual(rows[0].get("naturalUpperKw"), 20.0)
        self.assertEqual(rows[0].get("naturalUpperReason"), "weather_near_zero_wind")
        self.assertAlmostEqual(targets[("ACGenerator", "风机-1")], 20.0)
        self.assertIsNone(rows[1].get("naturalUpperKw"))
        self.assertAlmostEqual(targets[("DCGenerator", "光伏-1")], 23.0)

        # 3) 有效辐照接近 0：仅光伏禁增发（<= currentKw），风机仍可增发
        rows = make_rows()
        targets = {("ACGenerator", "风机-1"): 23.0, ("DCGenerator", "光伏-1"): 23.0}
        _apply_renewable_natural_upper_bounds(
            rows,
            targets,
            settings,
            runtime_memory=None,
            sim_time_seconds=14.0,
            wind_measurement_status={"available": True, "value": 6.0},
            solar_measurement_status={"available": True, "value": 1.5},
            diesel_guard_blocked=False,
        )
        self.assertIsNone(rows[0].get("naturalUpperKw"))
        self.assertAlmostEqual(targets[("ACGenerator", "风机-1")], 23.0)
        self.assertAlmostEqual(rows[1].get("naturalUpperKw"), 20.0)
        self.assertEqual(rows[1].get("naturalUpperReason"), "weather_near_zero_solar")
        self.assertAlmostEqual(targets[("DCGenerator", "光伏-1")], 20.0)

        # 4) 测点无效时，即使值为 0 也不误封锁增发
        rows = make_rows()
        targets = {("ACGenerator", "风机-1"): 23.0, ("DCGenerator", "光伏-1"): 23.0}
        _apply_renewable_natural_upper_bounds(
            rows,
            targets,
            settings,
            runtime_memory=None,
            sim_time_seconds=16.0,
            wind_measurement_status={"available": False, "value": 0.0},
            solar_measurement_status={"available": False, "value": 0.0},
            diesel_guard_blocked=False,
        )
        self.assertIsNone(rows[0].get("naturalUpperKw"))
        self.assertIsNone(rows[1].get("naturalUpperKw"))
        self.assertAlmostEqual(targets[("ACGenerator", "风机-1")], 23.0)
        self.assertAlmostEqual(targets[("DCGenerator", "光伏-1")], 23.0)

    def test_settings_normalized_idempotent_storage_step_ratio(self):
        """验证 normalized() 严格幂等，不会将默认跟网储能步长 0.03 误覆写为变流器步长 0.20。"""
        from agc_engine import RenewableControlSettings

        s1 = RenewableControlSettings().updated(
            {"step_coefficient": 0.02, "storage_step_ratio": 0.03, "diesel_step_ratio": 0.15}
        )
        s2 = s1.normalized()
        self.assertAlmostEqual(s1.storage_step_ratio, 0.03)
        self.assertAlmostEqual(s2.storage_step_ratio, 0.03)
        self.assertAlmostEqual(s2.converter_ramp_step_ratio, 0.20)
        self.assertAlmostEqual(s2.converter_step_ratio, 0.0)

    def test_renewable_no_conflicting_directions_in_island(self):
        """验证无环境干扰时同岛新能源机组不会一边涨功率一边降功率（禁止策略打架）。"""
        import math
        from agc_engine.renewable_optimization import _Variable, _solve_island

        # 构造复现场景：AC 侧因储能退出过充多出过剩功率迫使 AC 风电降功率，DC 侧本来有空间增发；
        # 启用同向约束后，岛内新能源不得出现“DC 涨、AC 降”的打架策略。
        vars_in = [
            _Variable(
                key=("ACGenerator", "交流风电-1"), kind="renewable",
                current_kw=16.0, lower_kw=14.5, upper_kw=17.5,
                side="AC", component_id="AC:1",
                renewable_available_kw=17.5, safety_lower_kw=0.0, safety_upper_kw=17.5,
                normal_step_kw=1.5,
            ),
            _Variable(
                key=("DCGenerator", "直流光伏-1"), kind="renewable",
                current_kw=10.0, lower_kw=8.5, upper_kw=11.5,
                side="DC", component_id="DC:1",
                renewable_available_kw=11.5, safety_lower_kw=0.0, safety_upper_kw=11.5,
                normal_step_kw=1.5,
            ),
            _Variable(
                key=("ACGenerator", "交流储能-1"), kind="storage",
                current_kw=-80.0, lower_kw=-30.0, upper_kw=-30.0,
                side="AC", component_id="AC:1",
                safety_lower_kw=-30.0, safety_upper_kw=95.0,
                requires_safety_correction=True, normal_step_kw=3.0,
            ),
            _Variable(
                key=("ACGenerator", "柴发-1"), kind="diesel",
                current_kw=200.0, lower_kw=155.0, upper_kw=245.0,
                side="AC", component_id="AC:1",
                safety_lower_kw=79.0, safety_upper_kw=291.0,
                normal_step_kw=45.0,
            ),
            _Variable(
                key=("DCGenerator", "直流储能-1"), kind="storage",
                current_kw=20.0, lower_kw=-57.0, upper_kw=57.0,
                side="DC", component_id="DC:1",
                safety_lower_kw=-57.0, safety_upper_kw=57.0,
                normal_step_kw=math.inf,
            ),
            _Variable(
                key=("DCACConverter", "变流器-1"), kind="converter",
                current_kw=-5.0, lower_kw=-65.0, upper_kw=0.0,
                ac_component_id="AC:1", dc_component_id="DC:1",
                ac_balance_coefficient=1.0, dc_balance_coefficient=-0.96,
                allocation_capacity_kw=300.0,
                safety_lower_kw=-300.0, safety_upper_kw=0.0,
                normal_step_kw=60.0, converter_efficiency=0.96,
                converter_base_dc_coefficient=-1.0,
            ),
        ]
        res = _solve_island(
            ("AC:1", "DC:1"),
            vars_in,
            renewable_curtailment_weight=1.0,
            diesel_output_weight=1.0,
            curtailment_square_weight=1e-6,
            adjustment_square_weight=1e-6,
            balance_delta_square_weight=1e4,
            balance_tolerance_kw=0.1,
            bound_tolerance_kw=0.1,
            optimization_ftol=1e-3,
            optimization_max_iterations=100,
        )
        self.assertTrue(res.success)
        ac_w_delta = res.target_by_device[("ACGenerator", "交流风电-1")] - 16.0
        dc_pv_delta = res.target_by_device[("DCGenerator", "直流光伏-1")] - 10.0
        # 绝不允许同时存在一个 > +0.05 且另一个 < -0.05 的方向打架
        self.assertFalse(ac_w_delta < -0.05 and dc_pv_delta > 0.05)
        self.assertFalse(ac_w_delta > 0.05 and dc_pv_delta < -0.05)
        self.assertLessEqual(res.max_balance_delta_kw, 0.1)

    def test_remote_all_load_measurements_invalid_fail_closed(self):
        """验证远端实时模式下全部在线负荷遥测失效时按 fail-closed 禁止闭环下发，恢复后自动放行。"""
        from agc_engine.control_parts.settings import _Quality, _load_boundary
        from agc_engine.control_parts.common import MeasurementValue

        snap = {
            "devices": [
                {
                    "dev_type": "ACLoad",
                    "name": "交流负荷-1",
                    "model_block": "ACLoad",
                    "raw": {"run_stat": 1, "p_set": 150.0},
                    "set_values": {"p_set": 150.0},
                }
            ]
        }
        q_lost = _Quality("remote", 0.5)
        val_lost = _load_boundary(snap, {}, q_lost)
        self.assertAlmostEqual(val_lost, 150.0)
        self.assertFalse(q_lost.payload()["dispatchAllowed"])
        self.assertFalse(q_lost.payload()["inputs"]["load"]["valid"])

        q_ok = _Quality("remote", 0.5)
        meas = {("ACLoad", "交流负荷-1", "P_LOAD"): MeasurementValue(182.5, "scada", {})}
        val_ok = _load_boundary(snap, meas, q_ok)
        self.assertAlmostEqual(val_ok, 182.5)
        self.assertTrue(q_ok.payload()["dispatchAllowed"])
        self.assertTrue(q_ok.payload()["inputs"]["load"]["valid"])

    def test_scdfe_d_direct_write_and_async_ack_watcher(self):
        """验证策略直写 scdfe_d.sqlite3 控制列，并由独立线程异步等待执行成功/失败/禁用/超时回执。"""
        import time

        # 为临时 scdfe_d.sqlite3 添加标准控制列组（对齐 scdfe_d 官方 9 列协议）
        conn = sqlite3.connect(self.env["scada_db"])
        conn.executescript(
            """
            ALTER TABLE ACGenerator ADD COLUMN p_set REAL;
            ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_time INTEGER;
            ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_status INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_ack_time INTEGER;
            ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_notuse INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_rtu INTEGER;
            ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_pnt INTEGER;
            ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_max REAL;
            ALTER TABLE ACGenerator ADD COLUMN p_set_cmd_min REAL;
            ALTER TABLE ACGenerator ADD COLUMN run_stat INTEGER;
            ALTER TABLE ACGenerator ADD COLUMN run_stat_cmd_time INTEGER;
            ALTER TABLE ACGenerator ADD COLUMN run_stat_cmd_status INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE ACGenerator ADD COLUMN run_stat_cmd_ack_time INTEGER;
            ALTER TABLE ACGenerator ADD COLUMN run_stat_cmd_notuse INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE ACGenerator ADD COLUMN run_stat_cmd_rtu INTEGER;
            ALTER TABLE ACGenerator ADD COLUMN run_stat_cmd_pnt INTEGER;
            UPDATE ACGenerator SET p_set_cmd_rtu=3, p_set_cmd_pnt=11, run_stat_cmd_rtu=3, run_stat_cmd_pnt=21 WHERE idx=1;
            UPDATE ACGenerator SET p_set_cmd_rtu=3, p_set_cmd_pnt=12, run_stat_cmd_rtu=3, run_stat_cmd_pnt=22 WHERE idx=2;
            UPDATE ACGenerator SET p_set_cmd_rtu=3, p_set_cmd_pnt=13, run_stat_cmd_rtu=3, run_stat_cmd_pnt=23, p_set_cmd_notuse=1 WHERE idx=3;
            """
        )
        conn.commit()
        conn.close()

        service = AGCRealtimeService(
            model_db=self.env["model_db"],
            scada_db=self.env["scada_db"],
            svg_path=self.env["svg_path"],
            agc_db=self.env["agc_db"],
            start_background=False,
            ack_timeout_seconds=0.8,
            ack_poll_interval_seconds=0.05,
        )
        self.addCleanup(service.close)

        # 确认独立回执线程已运行且控制点来自 scdfe_d.sqlite3（未连接 scdfe_c）
        self.assertIsNotNone(service._ack_watcher_thread)
        self.assertTrue(service._ack_watcher_thread.is_alive())
        self.assertEqual(service.adapter.control_point_meta.get("channel"), "scdfe_d")

        # 1) 人工写入 idx=1（期待回执成功）和 idx=2（期待回执失败）
        r1 = service.execute_manual_command(dev_type="ACGenerator", dev_name="交流风电-1", set_type="p_set", value=25.5)
        r2 = service.execute_manual_command(dev_type="ACGenerator", dev_name="交流储能-1", set_type="p_set", value=-15.0)
        # 2) idx=3 已设置 notuse=1，应直接拦截不写库
        r3 = service.execute_manual_command(dev_type="ACGenerator", dev_name="交流柴发-1", set_type="p_set", value=110.0)

        self.assertTrue(r1["ok"])
        self.assertEqual(r1["record"]["status"], "pending")
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["record"]["status"], "pending")
        self.assertFalse(r3["ok"])
        self.assertEqual(r3["record"]["status"], "disabled")

        # 验证 scdfe_d.sqlite3 宽表已被直写为 cmd_status=1 且值正确，idx=3 未被修改
        chk = sqlite3.connect(self.env["scada_db"])
        chk.row_factory = sqlite3.Row
        rows = {r["idx"]: dict(r) for r in chk.execute("SELECT idx, p_set, p_set_cmd_time, p_set_cmd_status FROM ACGenerator").fetchall()}
        self.assertAlmostEqual(rows[1]["p_set"], 25.5)
        self.assertEqual(rows[1]["p_set_cmd_status"], 1)
        self.assertAlmostEqual(rows[2]["p_set"], -15.0)
        self.assertEqual(rows[2]["p_set_cmd_status"], 1)
        self.assertIsNone(rows[3]["p_set"])

        # 模拟下游 scdfe_d 写回回执：idx=1 执行成功(2)，idx=2 执行失败(3)
        ack_ms = int(round(time.time() * 1000)) + 10
        chk.execute("UPDATE ACGenerator SET p_set_cmd_status=2, p_set_cmd_ack_time=? WHERE idx=1", (ack_ms,))
        chk.execute("UPDATE ACGenerator SET p_set_cmd_status=3, p_set_cmd_ack_time=? WHERE idx=2", (ack_ms,))
        chk.commit()
        chk.close()

        # 等待独立线程异步更新 agc_command_records
        deadline = time.monotonic() + 3.0
        st1 = st2 = "pending"
        while time.monotonic() < deadline:
            rec1 = service.store.conn.execute("SELECT status FROM agc_command_records WHERE id=?", (r1["record"]["id"],)).fetchone()
            rec2 = service.store.conn.execute("SELECT status FROM agc_command_records WHERE id=?", (r2["record"]["id"],)).fetchone()
            st1, st2 = rec1["status"], rec2["status"]
            if st1 != "pending" and st2 != "pending":
                break
            time.sleep(0.05)
        self.assertEqual(st1, "dispatched")
        self.assertEqual(st2, "failed")

        # 3) 验证超时场景：写入 idx=1 run_stat 后下游不回执，0.8s 后独立线程应标记为 timeout
        r_to = service.execute_manual_command(dev_type="ACGenerator", dev_name="交流风电-1", set_type="run_stat", value=1)
        self.assertEqual(r_to["record"]["status"], "pending")
        deadline = time.monotonic() + 3.0
        st_to = "pending"
        while time.monotonic() < deadline:
            rec_to = service.store.conn.execute("SELECT status FROM agc_command_records WHERE id=?", (r_to["record"]["id"],)).fetchone()
            st_to = rec_to["status"]
            if st_to != "pending":
                break
            time.sleep(0.05)
        self.assertEqual(st_to, "timeout")


if __name__ == "__main__":
    unittest.main()
