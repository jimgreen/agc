"""秦岭微电网模型库、实时库（scdfe_d）、SVG 接线图与四遥点表适配器。

职责：
1. 只读解析 ``devices.sqlite3``（兼容 RTDB ``rtdb_data_*`` 与原生表及 ``.e`` 文件回退），
   构造 AGC 算法引擎与 Web HMI 所需的标准模型定义与设备列表。
2. 只读轮询 ``scdfe_d.sqlite3``（WAL 友好），实时采集交直流发电、储能 SOC、负荷、
   变流器、氢能系统、断路器与气象量（风速、辐照度），生成标准快照与测点列表。
3. 解析 SVG 接线图路径（传入 ``qinling.e`` 时自动关联同目录 ``qinling.svg``）。
4. 加载 ``point_catalog.json``（及 ``scdfe_c.sqlite3`` 回退）建立四遥控制点映射，
   支持闭环或人工确认后向 ``scdfe_c`` 指令端点安全下发 YT/YK 指令。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from scada_interface import ScadaCommandClient, ScadaHttpClient, load_http_client

DEFAULT_MODEL_DB = Path(
    os.environ.get(
        "AGC_MODEL_DB",
        r"D:\codex\workspace\realtime\qinling\sqldb\model\devices.sqlite3",
    )
)
DEFAULT_SCADA_DB = Path(
    os.environ.get(
        "AGC_SCADA_DB",
        r"D:\codex\workspace\realtime\qinling\sqldb\scada\scdfe_d.sqlite3",
    )
)
DEFAULT_SVG_PATH = Path(
    os.environ.get(
        "AGC_SVG_PATH",
        r"D:\codex\workspace\realtime\qinling\model\qinling.e",
    )
)
DEFAULT_AGC_DB = Path(
    os.environ.get(
        "AGC_DB",
        r"D:\codex\workspace\realtime\qinling\sqldb\agc\agc.sqlite3",
    )
)


# 物理设备主表及域归属
PRIMARY_DEVICE_TABLES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("ACGenerator", ("AC",)),
    ("DCGenerator", ("DC",)),
    ("ACLoad", ("AC",)),
    ("DCLoad", ("DC",)),
    ("DCACConverter", ("AC", "DC")),
    ("DCDCConverter", ("DC",)),
    ("ACBreak", ("AC",)),
    ("DCBreak", ("DC",)),
    ("ACBranch", ("AC",)),
    ("DCBranch", ("DC",)),
    ("ACZeroBranch", ("AC",)),
    ("ACRealBs", ("AC",)),
    ("DCRealBs", ("DC",)),
    ("AcE2Hydro", ("AC", "HYDRO")),
    ("Hydro2DcE", ("DC", "HYDRO")),
    ("HydroStorage", ("HYDRO",)),
    ("HydroSource", ("HYDRO",)),
    ("HydroLoad", ("HYDRO",)),
    ("HydroValve", ("HYDRO",)),
    ("HydroCompressor", ("HYDRO",)),
)

# 储能与新能源扩展参数表
EXTENSION_LINK_TABLES: Tuple[Tuple[str, str, str], ...] = (
    ("ACWindGen", "ACGenerator", "idx_acgenerator"),
    ("ACPVGen", "ACGenerator", "idx_acgenerator"),
    ("ACStorageGen", "ACGenerator", "idx_acgenerator"),
    ("ACDieselGen", "ACGenerator", "idx_acgenerator"),
    ("DCPVGen", "DCGenerator", "idx_dcgenerator"),
    ("DCStorageGen", "DCGenerator", "idx_dcgenerator"),
)

# 控制字段名到点表 metric 的映射关系
SET_TYPE_TO_METRICS: Dict[str, Tuple[str, ...]] = {
    "p_set": ("p_set",),
    "q_set": ("q_set",),
    "v_set": ("v_set",),
    "i_set": ("i_set",),
    "p_ac_set": ("p_ac_set", "p_set"),
    "q_ac_set": ("q_ac_set", "q_set"),
    "p_dc_set": ("p_dc_set", "p_set"),
    "v_ac_set": ("v_ac_set",),
    "v_dc_set": ("v_dc_set",),
    "run_stat": ("start_stop", "run"),
    "closed_status": ("open_close", "start_stop"),
    "flow_set": ("flow_set", "p_set"),
}


@dataclass(frozen=True)
class ScadaControlPoint:
    """单个四遥控制点映射定义。"""

    kind: str  # "yt" 或 "yk"
    rtu_id: int
    pnt_no: int
    model_table: str
    model_idx: int
    device_name: str
    dev_type: str
    metric: str
    name: str
    unit: str = ""
    eng_min: Optional[float] = None
    eng_max: Optional[float] = None


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _int_or_default(value: Any, default: int = 0) -> int:
    num = _finite(value)
    if num is None:
        return default
    return int(round(num))


def _sqlite_ro_connect(path: Path, timeout: float = 5.0) -> sqlite3.Connection:
    """以只读 URI 方式连接 SQLite（兼容 WAL 并发读写，绝不写入目标库）。"""
    resolved = Path(path).resolve()
    uri = f"{resolved.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        pass
    return conn


def resolve_svg_file(svg_or_e_path: str | Path) -> Tuple[Optional[Path], Optional[Path]]:
    """解析接线图 SVG 路径与关联的 .e 模型文件路径。

    若用户传入的是 ``qinling.e``，自动在同目录寻找同名 ``qinling.svg``；
    若传入 ``.svg``，同时记录同目录同名 ``.e``（若存在）。
    """
    raw = Path(svg_or_e_path)
    svg_path: Optional[Path] = None
    e_path: Optional[Path] = None
    suffix = raw.suffix.lower()
    if suffix == ".svg":
        if raw.is_file():
            svg_path = raw
        candidate_e = raw.with_suffix(".e")
        if candidate_e.is_file():
            e_path = candidate_e
    elif suffix == ".e":
        if raw.is_file():
            e_path = raw
        candidate_svg = raw.with_suffix(".svg")
        if candidate_svg.is_file():
            svg_path = candidate_svg
    else:
        if raw.is_file():
            svg_path = raw
        elif raw.with_suffix(".svg").is_file():
            svg_path = raw.with_suffix(".svg")
        if raw.with_suffix(".e").is_file():
            e_path = raw.with_suffix(".e")
    return svg_path, e_path


def _parse_e_file_tables(e_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """轻量解析标准 E-file（如 qinling.e）为表行字典，用作数据库缺失时的回退。"""
    tables: Dict[str, List[Dict[str, Any]]] = {}
    if not e_path.is_file():
        return tables
    text = e_path.read_text(encoding="utf-8", errors="ignore")
    current_block = ""
    headers: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("<") and line.endswith(">"):
            tag = line[1:-1].strip()
            if tag.startswith("/"):
                current_block = ""
                headers = []
            else:
                current_block = tag.split()[0]
                headers = []
                tables.setdefault(current_block, [])
            continue
        if not current_block:
            continue
        if line.startswith("@"):
            headers = line[1:].strip().split()
        elif line.startswith("#") and headers:
            parts = line[1:].strip().split()
            row: Dict[str, Any] = {}
            for i, col in enumerate(headers):
                val_raw = parts[i] if i < len(parts) else ""
                row[col] = _coerce_scalar(val_raw)
            tables[current_block].append(row)
    return tables


def _coerce_scalar(raw: str) -> Any:
    text = str(raw).strip()
    if text == "":
        return ""
    if re.fullmatch(r"[+-]?\d+", text):
        try:
            return int(text)
        except ValueError:
            return text
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?", text):
        try:
            return float(text)
        except ValueError:
            return text
    return text


def load_model_tables(model_db: Path, fallback_e_path: Optional[Path] = None) -> Dict[str, List[Dict[str, Any]]]:
    """只读读取 ``devices.sqlite3`` 中的全部模型表（支持 RTDB ``rtdb_data_*`` 布局与原生表）。"""
    if model_db.is_file():
        conn = _sqlite_ro_connect(model_db)
        try:
            cur = conn.cursor()
            has_rtdb = cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rtdb_schema_tables'"
            ).fetchone()
            tables: Dict[str, List[Dict[str, Any]]] = {}
            if has_rtdb:
                t_rows = cur.execute(
                    "SELECT table_name FROM rtdb_schema_tables ORDER BY ordinal"
                ).fetchall()
                for (tname,) in t_rows:
                    cols = [
                        row[0]
                        for row in cur.execute(
                            "SELECT eng_name FROM rtdb_schema_columns WHERE table_name=? ORDER BY ordinal",
                            (tname,),
                        ).fetchall()
                    ]
                    phys = f"rtdb_data_{tname}"
                    exists_phys = cur.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (phys,),
                    ).fetchone()
                    if exists_phys:
                        rows = cur.execute(f'SELECT * FROM "{phys}" ORDER BY _pos').fetchall()
                        parsed: List[Dict[str, Any]] = []
                        for r in rows:
                            # rtdb_data_* 首列为 _pos
                            vals = list(r)[1:]
                            parsed.append(
                                {cols[i]: vals[i] for i in range(min(len(cols), len(vals)))}
                            )
                        tables[tname] = parsed
                    else:
                        exists_native = cur.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (tname,),
                        ).fetchone()
                        if exists_native:
                            rows = cur.execute(f'SELECT * FROM "{tname}"').fetchall()
                            tables[tname] = [dict(r) for r in rows]
            else:
                t_rows = cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
                for (tname,) in t_rows:
                    rows = cur.execute(f'SELECT * FROM "{tname}"').fetchall()
                    tables[tname] = [dict(r) for r in rows]
            if tables:
                return tables
        finally:
            conn.close()

    if fallback_e_path is not None and fallback_e_path.is_file():
        return _parse_e_file_tables(fallback_e_path)
    return {}


def load_control_point_catalog(scada_db: Path) -> Tuple[List[ScadaControlPoint], Dict[str, Any]]:
    """加载四遥控制点目录（优先同目录 ``point_catalog.json``，缺省回退 ``scdfe_c.sqlite3``）。"""
    scada_dir = Path(scada_db).resolve().parent
    catalog_path = scada_dir / "point_catalog.json"
    points: List[ScadaControlPoint] = []
    meta: Dict[str, Any] = {"source": "", "yt_count": 0, "yk_count": 0}
    if catalog_path.is_file():
        try:
            payload = json.loads(catalog_path.read_text(encoding="utf-8"))
            for item in payload.get("points", []) or []:
                kind = str(item.get("kind", "")).strip().lower()
                if kind not in {"yt", "yk"}:
                    continue
                points.append(
                    ScadaControlPoint(
                        kind=kind,
                        rtu_id=int(item.get("rtu_id", 1)),
                        pnt_no=int(item.get("pnt_no", 0)),
                        model_table=str(item.get("model_table", "")),
                        model_idx=_int_or_default(item.get("model_idx", 0), 0),
                        device_name=str(item.get("device_name", "")),
                        dev_type=str(item.get("dev_type", "")),
                        metric=str(item.get("metric", "")),
                        name=str(item.get("name", "")),
                        unit=str(item.get("unit", "")),
                        eng_min=_finite(item.get("eng_min")),
                        eng_max=_finite(item.get("eng_max")),
                    )
                )
            meta["source"] = str(catalog_path)
            meta["yt_count"] = sum(1 for p in points if p.kind == "yt")
            meta["yk_count"] = sum(1 for p in points if p.kind == "yk")
            return points, meta
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    # 若无 point_catalog.json，优先从同级 fes 目录、其次同目录只读解析 scdfe_c.sqlite3
    fes_scdfe_c = scada_dir.parent / "fes" / "scdfe_c.sqlite3"
    scdfe_c = fes_scdfe_c if fes_scdfe_c.is_file() else (scada_dir / "scdfe_c.sqlite3")
    if scdfe_c.is_file():
        try:
            conn = _sqlite_ro_connect(scdfe_c)
            try:
                for kind, tbl in (("yt", "fes_yt_define"), ("yk", "fes_yk_define")):
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
                    ).fetchone()
                    if not exists:
                        continue
                    for r in conn.execute(f'SELECT rtu_id, pnt_no, name FROM "{tbl}"').fetchall():
                        raw_name = str(r["name"] or "")
                        dev_name = raw_name.split("/")[0].strip() if "/" in raw_name else raw_name
                        metric = "start_stop" if kind == "yk" else "p_set"
                        if "无功" in raw_name:
                            metric = "q_set"
                        elif "电压" in raw_name:
                            metric = "v_set"
                        elif "交流" in raw_name and "有功" in raw_name:
                            metric = "p_ac_set"
                        elif "直流" in raw_name and "有功" in raw_name:
                            metric = "p_dc_set"
                        points.append(
                            ScadaControlPoint(
                                kind=kind,
                                rtu_id=int(r["rtu_id"]),
                                pnt_no=int(r["pnt_no"]),
                                model_table="",
                                model_idx=0,
                                device_name=dev_name,
                                dev_type="",
                                metric=metric,
                                name=raw_name,
                            )
                        )
                meta["source"] = str(scdfe_c)
                meta["yt_count"] = sum(1 for p in points if p.kind == "yt")
                meta["yk_count"] = sum(1 for p in points if p.kind == "yk")
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return points, meta


def _resolve_command_endpoint(scada_db: Path, explicit: Optional[Path] = None) -> Optional[Path]:
    if explicit is not None and Path(explicit).is_file():
        return Path(explicit)
    env_path = str(os.environ.get("AGC_SCADA_COMMAND_ENDPOINT", "") or "").strip()
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    scada_dir = Path(scada_db).resolve().parent
    fes_dir = scada_dir.parent / "fes"
    for base_dir in (fes_dir, scada_dir):
        for name in (
            "scdfe_c.sqlite3.scdfe_command.json",
            f"{Path(scada_db).name}.scdfe_command.json",
        ):
            cand = base_dir / name
            if cand.is_file():
                return cand
    return None


class QinlingDataAdapter:
    """秦岭微电网模型、实时库、SVG 与四遥控制统一适配器。"""

    def __init__(
        self,
        model_db: str | Path = DEFAULT_MODEL_DB,
        scada_db: str | Path = DEFAULT_SCADA_DB,
        svg_path: str | Path = DEFAULT_SVG_PATH,
        command_endpoint: Optional[str | Path] = None,
        command_sender: Optional[Callable[[str, int, int, Any], bool]] = None,
    ) -> None:
        self.model_db = Path(model_db)
        self.scada_db = Path(scada_db)
        self.svg_input_path = Path(svg_path)
        self.svg_file, self.e_file = resolve_svg_file(self.svg_input_path)
        self.command_endpoint = _resolve_command_endpoint(
            self.scada_db, Path(command_endpoint) if command_endpoint else None
        )
        self._custom_sender = command_sender
        self._http_client: Optional[ScadaHttpClient] = load_http_client()
        self._cmd_client: Optional[ScadaCommandClient] = (
            ScadaCommandClient(self.command_endpoint)
            if self.command_endpoint is not None
            else None
        )

        self.model_tables: Dict[str, List[Dict[str, Any]]] = {}
        self.model_meta: Dict[str, Any] = {}
        self.control_points: List[ScadaControlPoint] = []
        self.control_point_meta: Dict[str, Any] = {}
        self._control_index: Dict[Tuple[str, str, str], ScadaControlPoint] = {}
        self._control_name_index: Dict[Tuple[str, str], ScadaControlPoint] = {}
        self._svg_text: str = ""
        self._svg_signature: str = ""
        self._definition_revision: int = 1

        self.reload_static()

    def reload_static(self) -> None:
        """重新加载模型静态表、SVG 接线图与四遥点表。"""
        self.svg_file, self.e_file = resolve_svg_file(self.svg_input_path)
        self.model_tables = load_model_tables(self.model_db, self.e_file)
        model_rows = self.model_tables.get("Model", [])
        first = model_rows[0] if model_rows else {}
        self.model_meta = {
            "id": str(first.get("name") or "qinling"),
            "name": str(first.get("name") or "qinling"),
            "path": str(first.get("path") or ""),
            "p_base": _finite(first.get("p_base")) or 100.0,
            "u_unit": str(first.get("u_unit") or "V"),
            "p_unit": str(first.get("p_unit") or "kW"),
            "i_unit": str(first.get("i_unit") or "A"),
            "model_db": str(self.model_db),
            "scada_db": str(self.scada_db),
            "svg_file": str(self.svg_file or self.svg_input_path),
        }

        if self.svg_file is not None and self.svg_file.is_file():
            self._svg_text = self.svg_file.read_text(encoding="utf-8", errors="ignore")
            self._svg_signature = hashlib.sha256(self._svg_text.encode("utf-8")).hexdigest()[:16]
        else:
            self._svg_text = ""
            self._svg_signature = ""

        self.control_points, self.control_point_meta = load_control_point_catalog(self.scada_db)
        self._control_index.clear()
        self._control_name_index.clear()
        for pt in self.control_points:
            if pt.model_table and pt.device_name and pt.metric:
                self._control_index[(pt.model_table, pt.device_name, pt.metric)] = pt
            if pt.device_name and pt.metric:
                self._control_name_index.setdefault((pt.device_name, pt.metric), pt)
        self._definition_revision += 1

    @property
    def svg_text(self) -> str:
        return self._svg_text

    @property
    def definition_revision(self) -> int:
        return self._definition_revision

    def resolve_control_point(
        self,
        dev_type: str,
        dev_name: str,
        set_type: str,
    ) -> Optional[ScadaControlPoint]:
        """将算法/人工指令目标解析为 SCADA 真实四遥点。"""
        metrics = SET_TYPE_TO_METRICS.get(set_type, (set_type,))
        # 对 AcE2Hydro / Hydro2DcE 自动映射到其电气侧实体（ACLoad / DCGenerator）
        candidate_targets: List[Tuple[str, str]] = [(dev_type, dev_name)]
        if dev_type == "AcE2Hydro":
            for row in self.model_tables.get("AcE2Hydro", []):
                if str(row.get("name", "")) == dev_name:
                    load_idx = _int_or_default(row.get("idx_ac_load_t1"), -1)
                    for lrow in self.model_tables.get("ACLoad", []):
                        if _int_or_default(lrow.get("idx"), -2) == load_idx:
                            candidate_targets.append(("ACLoad", str(lrow.get("name", ""))))
        elif dev_type == "Hydro2DcE":
            for row in self.model_tables.get("Hydro2DcE", []):
                if str(row.get("name", "")) == dev_name:
                    gen_idx = _int_or_default(row.get("idx_dc_unit_t1"), -1)
                    for grow in self.model_tables.get("DCGenerator", []):
                        if _int_or_default(grow.get("idx"), -2) == gen_idx:
                            candidate_targets.append(("DCGenerator", str(grow.get("name", ""))))

        for tbl, name in candidate_targets:
            for metric in metrics:
                pt = self._control_index.get((tbl, name, metric))
                if pt is not None:
                    return pt
                pt = self._control_name_index.get((name, metric))
                if pt is not None:
                    return pt
        return None

    def dispatch_scada_command(
        self,
        dev_type: str,
        dev_name: str,
        set_type: str,
        value: Any,
    ) -> Tuple[bool, str, Optional[ScadaControlPoint]]:
        """通过配置好的四遥通道下发单条 YT/YK 指令。"""
        pt = self.resolve_control_point(dev_type, dev_name, set_type)
        if pt is None:
            return False, f"未在点表中找到控制点映射: {dev_type}.{dev_name}.{set_type}", None
        try:
            if self._custom_sender is not None:
                ok = bool(self._custom_sender(pt.kind, pt.rtu_id, pt.pnt_no, value))
                return ok, "已提交(自定义通道)" if ok else "自定义通道拒绝", pt
            if self._cmd_client is not None:
                ok = self._cmd_client.send(pt.kind, pt.rtu_id, pt.pnt_no, value)
                return bool(ok), "已提交至 SCADA 指令服务" if ok else "SCADA 指令服务拒绝", pt
            if self._http_client is not None:
                ok = self._http_client.send(pt.kind, pt.rtu_id, pt.pnt_no, value)
                return bool(ok), "已提交至 SCADA HTTP 接口" if ok else "SCADA HTTP 接口拒绝", pt
            return False, "未配置 SCADA 指令端点（scdfe_command.json / HTTP）", pt
        except Exception as exc:
            return False, f"指令下发异常: {exc}", pt

    def _read_scdfe_tables(self) -> Tuple[Dict[str, List[Dict[str, Any]]], Optional[float], str]:
        """只读抓取 ``scdfe_d.sqlite3`` 全部实时表。"""
        if not self.scada_db.is_file():
            return {}, None, f"实时数据库不存在: {self.scada_db}"
        try:
            conn = _sqlite_ro_connect(self.scada_db, timeout=5.0)
            try:
                cur = conn.cursor()
                t_rows = cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'rtdb_%' ORDER BY name"
                ).fetchall()
                result: Dict[str, List[Dict[str, Any]]] = {}
                latest_ms: Optional[float] = None
                for (tname,) in t_rows:
                    rows = [dict(r) for r in cur.execute(f'SELECT * FROM "{tname}"').fetchall()]
                    result[tname] = rows
                    for r in rows:
                        for k, v in r.items():
                            if k.endswith("_time") and isinstance(v, (int, float)) and v > 1e11:
                                if latest_ms is None or float(v) > latest_ms:
                                    latest_ms = float(v)
                return result, latest_ms, ""
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return {}, None, f"读取实时库失败: {exc}"

    def _build_control_definitions(self) -> List[Dict[str, Any]]:
        """生成控制引擎和前端控制指令页使用的 control 定义列表。"""
        rows: List[Dict[str, Any]] = []
        # 1. 从模型主表生成标准可控设备的控制定义
        for block, _domains in PRIMARY_DEVICE_TABLES:
            for item in self.model_tables.get(block, []):
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                kind = str(item.get("dev_type", "")).strip()
                if block in {"ACGenerator", "DCGenerator"}:
                    p_max = _finite(item.get("p_max"))
                    p_min = _finite(item.get("p_min"))
                    rated = _finite(item.get("rated_capacity")) or 0.0
                    if (p_max is None or p_max == 0.0) and rated > 0:
                        p_max = rated
                        if "storage" in kind:
                            p_min = -rated
                    self._append_control_def(rows, block, name, "p_set", "kW", p_min, p_max)
                    self._append_control_def(rows, block, name, "run_stat", "", 0.0, 1.0)
                elif block == "DCACConverter":
                    cap = _finite(item.get("rated_capacity")) or 0.0
                    ac_max = _finite(item.get("ac_p_max"))
                    ac_min = _finite(item.get("ac_p_min"))
                    dc_max = _finite(item.get("dc_p_max"))
                    dc_min = _finite(item.get("dc_p_min"))
                    if ac_max is None or (ac_max == 0 and cap > 0):
                        ac_max, ac_min = cap, -cap
                    if dc_max is None or (dc_max == 0 and cap > 0):
                        dc_max, dc_min = cap, -cap
                    self._append_control_def(rows, block, name, "p_ac_set", "kW", ac_min, ac_max)
                    self._append_control_def(rows, block, name, "p_dc_set", "kW", dc_min, dc_max)
                    self._append_control_def(rows, block, name, "run_stat", "", 0.0, 1.0)
                elif block == "DCDCConverter":
                    cap = _finite(item.get("rated_capacity")) or 0.0
                    self._append_control_def(rows, block, name, "p_set", "kW", -cap, cap)
                    self._append_control_def(rows, block, name, "run_stat", "", 0.0, 1.0)
                elif block in {"ACLoad", "DCLoad"}:
                    p_max = _finite(item.get("p_max")) or _finite(item.get("rated_capacity")) or 0.0
                    self._append_control_def(rows, block, name, "p_set", "kW", 0.0, p_max if p_max > 0 else None)
                    self._append_control_def(rows, block, name, "run_stat", "", 0.0, 1.0)
                elif block in {"ACBreak", "DCBreak"}:
                    self._append_control_def(rows, block, name, "closed_status", "", 0.0, 1.0)
                elif block in {"AcE2Hydro", "Hydro2DcE"}:
                    self._append_control_def(rows, block, name, "p_set", "kW", 0.0, None)
                    self._append_control_def(rows, block, name, "run_stat", "", 0.0, 1.0)
        return rows

    def _append_control_def(
        self,
        rows: List[Dict[str, Any]],
        dev_type: str,
        dev_name: str,
        set_type: str,
        unit: str,
        min_val: Optional[float],
        max_val: Optional[float],
    ) -> None:
        pt = self.resolve_control_point(dev_type, dev_name, set_type)
        rows.append(
            {
                "dev_type": dev_type,
                "dev_name": dev_name,
                "set_type": set_type,
                "unit": unit,
                "min": min_val,
                "max": max_val,
                "kind": pt.kind if pt else ("yk" if set_type in {"run_stat", "closed_status"} else "yt"),
                "rtu_id": pt.rtu_id if pt else None,
                "pnt_no": pt.pnt_no if pt else None,
                "point_name": pt.name if pt else f"{dev_name} / {set_type}",
                "scada_mapped": pt is not None,
            }
        )

    def read_snapshot(self) -> Tuple[Dict[str, Any], float, str]:
        """读取当前秦岭实时快照，返回 ``(snapshot, age_seconds, error_text)``。"""
        now_dt = datetime.now()
        now_iso = now_dt.isoformat(timespec="seconds")
        now_hms = now_dt.strftime("%H:%M:%S")
        now_epoch = time.time()

        scada_tables, latest_ms, scada_error = self._read_scdfe_tables()

        # 按 (table, idx) 与 (table, name) 建立快速索引
        scada_by_idx: Dict[Tuple[str, int], Dict[str, Any]] = {}
        scada_by_name: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for tname, trows in scada_tables.items():
            for r in trows:
                idx = _int_or_default(r.get("idx"), -1)
                name = str(r.get("name") or "").strip()
                if idx >= 0:
                    scada_by_idx[(tname, idx)] = r
                if name:
                    scada_by_name[(tname, name)] = r

        # 计算储能 SOC 映射（从 ACStorageGen / DCStorageGen 映射到对应 ACGenerator / DCGenerator idx）
        ac_soc_by_gen_idx: Dict[int, Tuple[float, Optional[int], Optional[int]]] = {}
        for ext in self.model_tables.get("ACStorageGen", []):
            ext_idx = _int_or_default(ext.get("idx"), -1)
            gen_idx = _int_or_default(ext.get("idx_acgenerator"), -1)
            srow = scada_by_idx.get(("ACStorageGen", ext_idx))
            if srow is not None and _finite(srow.get("soc")) is not None:
                soc_val = float(srow["soc"])
                if soc_val > 1.0:
                    soc_val = soc_val / 100.0
                ac_soc_by_gen_idx[gen_idx] = (
                    soc_val,
                    srow.get("soc_rtu"),
                    srow.get("soc_pnt"),
                )

        dc_soc_by_gen_idx: Dict[int, Tuple[float, Optional[int], Optional[int]]] = {}
        for ext in self.model_tables.get("DCStorageGen", []):
            ext_idx = _int_or_default(ext.get("idx"), -1)
            gen_idx = _int_or_default(ext.get("idx_dcgenerator"), -1)
            srow = scada_by_idx.get(("DCStorageGen", ext_idx))
            if srow is not None and _finite(srow.get("soc")) is not None:
                soc_val = float(srow["soc"])
                if soc_val > 1.0:
                    soc_val = soc_val / 100.0
                dc_soc_by_gen_idx[gen_idx] = (
                    soc_val,
                    srow.get("soc_rtu"),
                    srow.get("soc_pnt"),
                )

        # 构造 devices、device_states 与 measurements.scada
        devices: List[Dict[str, Any]] = []
        device_states: List[Dict[str, Any]] = []
        scada_meas: List[Dict[str, Any]] = []
        meas_defs: List[Dict[str, Any]] = []

        def add_meas(
            dev_type: str,
            dev_name: str,
            meas_type: str,
            value: Any,
            unit: str = "",
            srow: Optional[Mapping[str, Any]] = None,
            field_prefix: str = "",
            valid: Optional[int] = None,
        ) -> None:
            num = _finite(value)
            qual = None
            rtu_id = None
            pnt_no = None
            ts_ms = None
            if srow is not None and field_prefix:
                qual = srow.get(f"{field_prefix}_qual")
                rtu_id = srow.get(f"{field_prefix}_rtu")
                pnt_no = srow.get(f"{field_prefix}_pnt")
                ts_ms = srow.get(f"{field_prefix}_time")
            is_valid = 1 if (num is not None and (valid is None or valid == 1)) else 0
            ts_str = (
                datetime.fromtimestamp(float(ts_ms) / 1000.0).isoformat(timespec="seconds")
                if isinstance(ts_ms, (int, float)) and ts_ms > 1e11
                else now_iso
            )
            scada_meas.append(
                {
                    "dev_type": dev_type,
                    "dev_name": dev_name,
                    "meas_type": meas_type,
                    "value": num,
                    "valid": is_valid,
                    "quality": 0 if qual is None else int(qual),
                    "unit": unit,
                    "timestamp": ts_str,
                    "rtu_id": rtu_id,
                    "pnt_no": pnt_no,
                }
            )
            meas_defs.append(
                {
                    "dev_type": dev_type,
                    "dev_name": dev_name,
                    "meas_type": meas_type,
                    "unit": unit,
                    "enabled": 1,
                }
            )

        # 汇总气象量（风速、辐照度、气温）
        wind_speeds = [
            float(r["wind_speed"])
            for r in scada_tables.get("ACWindGen", [])
            if _finite(r.get("wind_speed")) is not None
        ]
        irradiances = [
            float(r["irradiance"])
            for r in (scada_tables.get("ACPVGen", []) + scada_tables.get("DCPVGen", []))
            if _finite(r.get("irradiance")) is not None
        ]
        if wind_speeds:
            ws_avg = sum(wind_speeds) / len(wind_speeds)
            add_meas("Environment", "weather", "WIND_SPEED", ws_avg, "m/s")
        if irradiances:
            irr_avg = sum(irradiances) / len(irradiances)
            add_meas("Environment", "weather", "SOLAR_IRRADIANCE", irr_avg, "W/m2")
        # 秦岭环境温度如实测无值则提供标准 25°C 参考（算法用做 PV 温度修正）
        module_temps = [
            float(r["module_temp"])
            for r in (scada_tables.get("ACPVGen", []) + scada_tables.get("DCPVGen", []))
            if _finite(r.get("module_temp")) is not None
        ]
        air_temp = (sum(module_temps) / len(module_temps)) if module_temps else 25.0
        add_meas("Environment", "weather", "AIR_TEMP", air_temp, "°C")

        # 氢能系统索引（用于 AcE2Hydro / Hydro2DcE 聚合）
        ac_load_scada_by_idx = {
            _int_or_default(r.get("idx"), -1): r for r in scada_tables.get("ACLoad", [])
        }
        dc_gen_scada_by_idx = {
            _int_or_default(r.get("idx"), -1): r for r in scada_tables.get("DCGenerator", [])
        }
        hydro_src_scada_by_idx = {
            _int_or_default(r.get("idx"), -1): r for r in scada_tables.get("HydroSource", [])
        }
        hydro_load_scada_by_idx = {
            _int_or_default(r.get("idx"), -1): r for r in scada_tables.get("HydroLoad", [])
        }

        for block, domains in PRIMARY_DEVICE_TABLES:
            for item in self.model_tables.get(block, []):
                idx = _int_or_default(item.get("idx"), 0)
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                srow = scada_by_idx.get((block, idx)) or scada_by_name.get((block, name))
                raw_copy = dict(item)
                dev_kind = str(item.get("dev_type") or "").strip()

                # 控制模式
                if block == "DCACConverter":
                    ac_ctrl = str(item.get("ac_control_type") or "").strip().upper()
                    dc_ctrl = str(item.get("dc_control_type") or "").strip().upper()
                    mode = ac_ctrl if ac_ctrl and ac_ctrl != "NONE" else dc_ctrl
                elif block == "DCDCConverter":
                    i_ctrl = str(item.get("i_control_type") or "").strip().upper()
                    j_ctrl = str(item.get("j_control_type") or "").strip().upper()
                    mode = i_ctrl if i_ctrl and i_ctrl != "NONE" else j_ctrl
                else:
                    mode = str(item.get("control_type") or "").strip().upper()

                # 实时运行态与开关位
                run_val = item.get("run_stat", 1)
                if srow is not None and srow.get("run") is not None:
                    run_val = _int_or_default(srow.get("run"), 1)
                elif srow is not None and srow.get("position") is not None:
                    run_val = _int_or_default(srow.get("position"), 1)
                run_stat = _int_or_default(run_val, 1)

                closed_status = _int_or_default(item.get("status", 1), 1)
                if srow is not None and srow.get("position") is not None:
                    closed_status = _int_or_default(srow.get("position"), closed_status)

                # 初始 set_values
                set_values: Dict[str, Any] = {}
                for k in ("p_set", "q_set", "v_set", "i_set", "p_ac_set", "q_ac_set", "p_dc_set", "v_ac_set", "v_dc_set", "flow_set"):
                    if k in item and _finite(item.get(k)) is not None:
                        set_values[k] = float(item[k])

                dev_record: Dict[str, Any] = {
                    "dev_type": block,
                    "model_block": block,
                    "idx": idx,
                    "dev_name": name,
                    "name": name,
                    "device_kind": dev_kind,
                    "mode": mode,
                    "control_type": mode,
                    "run_stat": run_stat,
                    "status": closed_status if block in {"ACBreak", "DCBreak"} else run_stat,
                    "closed_status": closed_status,
                    "rated_capacity": _finite(item.get("rated_capacity")) or 0.0,
                    "terminal_domains": list(domains),
                    "set_values": set_values,
                    "raw": raw_copy,
                }

                # 挂载储能/储氢 SOC 及各类实时量测
                if block == "ACGenerator":
                    if idx in ac_soc_by_gen_idx:
                        soc_v, soc_rtu, soc_pnt = ac_soc_by_gen_idx[idx]
                        dev_record["soc_curr"] = soc_v
                        scada_meas.append(
                            {
                                "dev_type": block,
                                "dev_name": name,
                                "meas_type": "SOC",
                                "value": soc_v,
                                "valid": 1,
                                "quality": 0,
                                "unit": "",
                                "timestamp": now_iso,
                                "rtu_id": soc_rtu,
                                "pnt_no": soc_pnt,
                            }
                        )
                    if srow is not None:
                        add_meas(block, name, "P_GEN", srow.get("p"), "kW", srow, "p")
                        add_meas(block, name, "Q_GEN", srow.get("q"), "kvar", srow, "q")
                        add_meas(block, name, "UAB", srow.get("uab"), "V", srow, "uab")
                        add_meas(block, name, "IA", srow.get("ia"), "A", srow, "ia")
                        add_meas(block, name, "RUN_STAT", run_stat, "", srow, "run")

                elif block == "DCGenerator":
                    if idx in dc_soc_by_gen_idx:
                        soc_v, soc_rtu, soc_pnt = dc_soc_by_gen_idx[idx]
                        dev_record["soc_curr"] = soc_v
                        scada_meas.append(
                            {
                                "dev_type": block,
                                "dev_name": name,
                                "meas_type": "SOC",
                                "value": soc_v,
                                "valid": 1,
                                "quality": 0,
                                "unit": "",
                                "timestamp": now_iso,
                                "rtu_id": soc_rtu,
                                "pnt_no": soc_pnt,
                            }
                        )
                    if srow is not None:
                        add_meas(block, name, "P_GEN", srow.get("p"), "kW", srow, "p")
                        add_meas(block, name, "U", srow.get("u"), "V", srow, "u")
                        add_meas(block, name, "I", srow.get("i"), "A", srow, "i")
                        add_meas(block, name, "RUN_STAT", run_stat, "", srow, "run")

                elif block == "ACLoad":
                    if srow is not None:
                        add_meas(block, name, "P_LOAD", srow.get("p"), "kW", srow, "p")
                        add_meas(block, name, "Q_LOAD", srow.get("q"), "kvar", srow, "q")
                        add_meas(block, name, "UAB", srow.get("uab"), "V", srow, "uab")
                        add_meas(block, name, "IA", srow.get("ia"), "A", srow, "ia")
                        add_meas(block, name, "RUN_STAT", run_stat, "", srow, "run")

                elif block == "DCLoad":
                    if srow is not None:
                        add_meas(block, name, "P_LOAD", srow.get("p"), "kW", srow, "p")
                        add_meas(block, name, "U", srow.get("u"), "V", srow, "u")
                        add_meas(block, name, "I", srow.get("i"), "A", srow, "i")
                        add_meas(block, name, "RUN_STAT", run_stat, "", srow, "run")

                elif block == "DCACConverter":
                    if srow is not None:
                        add_meas(block, name, "P_AC", srow.get("ac_p"), "kW", srow, "ac_p")
                        add_meas(block, name, "Q_AC", srow.get("ac_q"), "kvar", srow, "ac_q")
                        add_meas(block, name, "P_DC", srow.get("dc_p"), "kW", srow, "dc_p")
                        add_meas(block, name, "U_AC", srow.get("ac_uab"), "V", srow, "ac_uab")
                        add_meas(block, name, "U_DC", srow.get("dc_u"), "V", srow, "dc_u")
                        add_meas(block, name, "RUN_STAT", run_stat, "", srow, "run")

                elif block == "DCDCConverter":
                    if srow is not None:
                        add_meas(block, name, "P_I", srow.get("i_p"), "kW", srow, "i_p")
                        add_meas(block, name, "P_J", srow.get("j_p"), "kW", srow, "j_p")
                        add_meas(block, name, "U_I", srow.get("i_u"), "V", srow, "i_u")
                        add_meas(block, name, "U_J", srow.get("j_u"), "V", srow, "j_u")
                        add_meas(block, name, "RUN_STAT", run_stat, "", srow, "run")

                elif block in {"ACBreak", "DCBreak"}:
                    if srow is not None:
                        add_meas(block, name, "CLOSED_STATUS", closed_status, "", srow, "position")
                        i_field = "ia" if block == "ACBreak" else "i"
                        add_meas(block, name, "IA" if block == "ACBreak" else "I", srow.get(i_field), "A", srow, i_field)

                elif block in {"ACBranch", "ACZeroBranch"}:
                    if srow is not None:
                        add_meas(block, name, "P_I", srow.get("i_p"), "kW", srow, "i_p")
                        add_meas(block, name, "Q_I", srow.get("i_q"), "kvar", srow, "i_q")
                        add_meas(block, name, "IA", srow.get("i_ia"), "A", srow, "i_ia")
                        add_meas(block, name, "RUN_STAT", run_stat, "", srow, "run")

                elif block == "DCBranch":
                    if srow is not None:
                        add_meas(block, name, "P_I", srow.get("i_p"), "kW", srow, "i_p")
                        add_meas(block, name, "P_J", srow.get("j_p"), "kW", srow, "j_p")
                        add_meas(block, name, "I", srow.get("i_i"), "A", srow, "i_i")
                        add_meas(block, name, "RUN_STAT", run_stat, "", srow, "run")

                elif block == "AcE2Hydro":
                    load_srow = ac_load_scada_by_idx.get(_int_or_default(item.get("idx_ac_load_t1"), -1))
                    hsrc_srow = hydro_src_scada_by_idx.get(_int_or_default(item.get("idx_h2_unit_t2"), -1))
                    p_val = srow.get("p") if (srow and _finite(srow.get("p")) is not None) else (load_srow.get("p") if load_srow else None)
                    fl_val = srow.get("flow") if (srow and _finite(srow.get("flow")) is not None) else (hsrc_srow.get("flow") if hsrc_srow else None)
                    pr_val = srow.get("pressure") if (srow and _finite(srow.get("pressure")) is not None) else (hsrc_srow.get("pressure") if hsrc_srow else None)
                    add_meas(block, name, "P_LOAD", p_val, "kW", load_srow or srow, "p")
                    add_meas(block, name, "FLOW", fl_val, "Nm3/h", hsrc_srow or srow, "flow")
                    add_meas(block, name, "PRESSURE", pr_val, "MPa", hsrc_srow or srow, "pressure")
                    add_meas(block, name, "RUN_STAT", run_stat, "", srow, "run")

                elif block == "Hydro2DcE":
                    gen_srow = dc_gen_scada_by_idx.get(_int_or_default(item.get("idx_dc_unit_t1"), -1))
                    hld_srow = hydro_load_scada_by_idx.get(_int_or_default(item.get("idx_h2_load_t2"), -1))
                    p_val = srow.get("p") if (srow and _finite(srow.get("p")) is not None) else (gen_srow.get("p") if gen_srow else None)
                    fl_val = srow.get("flow") if (srow and _finite(srow.get("flow")) is not None) else (hld_srow.get("flow") if hld_srow else None)
                    pr_val = srow.get("pressure") if (srow and _finite(srow.get("pressure")) is not None) else (hld_srow.get("pressure") if hld_srow else None)
                    add_meas(block, name, "P_GEN", p_val, "kW", gen_srow or srow, "p")
                    add_meas(block, name, "FLOW", fl_val, "Nm3/h", hld_srow or srow, "flow")
                    add_meas(block, name, "PRESSURE", pr_val, "MPa", hld_srow or srow, "pressure")
                    add_meas(block, name, "RUN_STAT", run_stat, "", srow, "run")

                elif block == "HydroStorage":
                    if srow is not None:
                        soc_v = _finite(srow.get("soc"))
                        if soc_v is not None and soc_v > 1.0:
                            soc_v = soc_v / 100.0
                        if soc_v is not None:
                            dev_record["soc_curr"] = soc_v
                        add_meas(block, name, "SOC", soc_v, "", srow, "soc")
                        add_meas(block, name, "PRESSURE", srow.get("pressure"), "MPa", srow, "pressure")
                        add_meas(block, name, "FLOW", srow.get("flow"), "Nm3/h", srow, "flow")
                        add_meas(block, name, "RUN_STAT", run_stat, "", srow, "run")

                elif block in {"HydroSource", "HydroLoad"}:
                    if srow is not None:
                        add_meas(block, name, "PRESSURE", srow.get("pressure"), "MPa", srow, "pressure")
                        add_meas(block, name, "FLOW", srow.get("flow"), "Nm3/h", srow, "flow")
                        add_meas(block, name, "RUN_STAT", run_stat, "", srow, "run")

                devices.append(dev_record)
                device_states.append(
                    {
                        "dev_type": block,
                        "dev_name": name,
                        "run_stat": run_stat,
                        "dead_island": 0,
                    }
                )

        # 补充节点电压量测（便于接线图与量测页展示）
        for node_tbl, u_col in (("ACNode", "uab"), ("DCNode", "u")):
            for r in scada_tables.get(node_tbl, []):
                nname = str(r.get("name") or "").strip()
                if not nname:
                    continue
                if _finite(r.get(u_col)) is not None:
                    add_meas(node_tbl, nname, "UAB" if u_col == "uab" else "U", r.get(u_col), "V", r, u_col)

        # 数据陈旧度计算
        if latest_ms is not None:
            age_seconds = max(0.0, now_epoch - (latest_ms / 1000.0))
            sample_iso = datetime.fromtimestamp(latest_ms / 1000.0).isoformat(timespec="seconds")
        else:
            age_seconds = 0.0 if not scada_error else 9999.0
            sample_iso = now_iso

        minute_of_day = now_dt.hour * 60 + now_dt.minute + now_dt.second / 60.0
        control_defs = self._build_control_definitions()
        set_value_rows = [
            row for row in control_defs if row.get("set_type") not in {"run_stat", "closed_status"}
        ]
        run_stat_rows = [
            row for row in control_defs if row.get("set_type") in {"run_stat", "closed_status"}
        ]

        # 为每台设备补充可用的 set_types 便于前端与引擎检索
        set_types_by_dev: Dict[Tuple[str, str], List[str]] = {}
        for crow in set_value_rows:
            set_types_by_dev.setdefault(
                (str(crow["dev_type"]), str(crow["dev_name"])), []
            ).append(str(crow["set_type"]))
        for dev in devices:
            dev["set_types"] = set_types_by_dev.get(
                (str(dev["dev_type"]), str(dev["dev_name"])), []
            )

        snapshot: Dict[str, Any] = {
            "model": dict(self.model_meta),
            "clock": {
                "time": now_hms,
                "timestamp": now_iso,
                "sample_timestamp": sample_iso,
                "absolute_minute": round(minute_of_day, 4),
                "minute": int(minute_of_day),
                "step_minutes": 1.0,
                "speed": 1.0,
                "running": True,
                "state": "running" if not scada_error else "degraded",
            },
            "measurement_clock": {
                "timestamp": sample_iso,
                "epoch_ms": latest_ms,
                "age_seconds": round(age_seconds, 3),
            },
            "devices": devices,
            "device_states": device_states,
            "device_parameters": {k: [dict(row) for row in v] for k, v in self.model_tables.items()},
            "definitions": {
                "model": {
                    k: {"rows": [dict(row) for row in v]}
                    for k, v in self.model_tables.items()
                },
                "control": {
                    "SetValue": {"rows": set_value_rows},
                    "RunStat": {"rows": run_stat_rows},
                },
                "control_list": control_defs,
                "measurement": meas_defs,
            },
            "measurements": {
                "scada": scada_meas,
                "definitions": meas_defs,
            },
            "settings": {
                "modes": [],
            },
            "static_meta": {
                "definition_revision": self._definition_revision,
                "diagram": {
                    "path": str(self.svg_file or ""),
                    "signature": self._svg_signature,
                    "available": bool(self._svg_text),
                },
                "scada_command": {
                    "endpoint": str(self.command_endpoint or ""),
                    "available": bool(self._cmd_client or self._http_client or self._custom_sender),
                    "yt_count": self.control_point_meta.get("yt_count", 0),
                    "yk_count": self.control_point_meta.get("yk_count", 0),
                },
            },
            "environment": {
                "wind_speed": round(sum(wind_speeds) / len(wind_speeds), 3) if wind_speeds else None,
                "solar_irradiance": round(sum(irradiances) / len(irradiances), 3) if irradiances else None,
                "air_temperature": round(air_temp, 2),
                "sample_timestamp": sample_iso,
            },
        }
        return snapshot, age_seconds, scada_error
