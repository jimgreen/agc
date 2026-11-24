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


if __name__ == "__main__":
    unittest.main()
