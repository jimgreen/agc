"""Keep AGC's physical SQLite tables and RTDB's native catalog in sync."""

from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3


SCHEMA_PATH = Path(__file__).resolve().parent / "web" / "agc_schema.sql"
REALTIME_SCHEMA_PATH = Path(__file__).resolve().parent / "web" / "agc_realtime_schema.sql"
IDENTIFIER = re.compile(r"[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*")
NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
TEXT = re.compile(r"'(?:[^']|'')*'")
TRANSACTION_SQL = re.compile(
    r"(?:[\s;\ufeff]|--[^\n]*(?:\n|$)|/\*.*?\*/)*"
    r"(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b",
    re.IGNORECASE | re.DOTALL,
)
TABLE_LABELS = {
    "system_settings": "系统参数",
    "devices": "设备配置",
    "device_curves": "默认功率曲线",
    "control_cycles": "控制周期",
    "device_samples": "设备采样",
    "event_logs": "运行日志",
    "agc_control_settings": "AGC控制参数配置",
    "agc_strategy_snapshots": "AGC控制策略快照",
    "agc_strategy_commands": "AGC设备控制策略明细",
    "agc_trend_points": "AGC综合趋势曲线",
    "agc_control_logs": "AGC控制决策日志",
    "agc_command_records": "AGC四遥指令记录",
    "agc_runtime_logs": "AGC系统运行日志",
    "agc_device_configs": "AGC受控设备控制配置",
    "agc_device_curves": "AGC受控设备96点预设曲线",
}


def _ensure_catalog(connection, database_name):
    existing = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='rtdb_schema_database'"
    ).fetchone()
    if existing:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(rtdb_schema_database)")}
        marker = None
        if "storage_format" in columns:
            marker = connection.execute(
                "SELECT storage_format FROM rtdb_schema_database WHERE version=1"
            ).fetchone()
        if marker is None or marker[0] != "native":
            raise ValueError("AGC 无法修改由 RTDB 管理的数据库结构")
    else:
        connection.execute("""
            CREATE TABLE rtdb_schema_database (
                version INTEGER PRIMARY KEY CHECK(version=1),
                eng_name TEXT NOT NULL, chn_name TEXT NOT NULL,
                storage_format TEXT NOT NULL DEFAULT 'native'
            )
        """)
        name = database_name if IDENTIFIER.fullmatch(database_name) else "agc"
        connection.execute(
            "INSERT INTO rtdb_schema_database VALUES (1, ?, 'AGC 自动发电控制', 'native')",
            (name,),
        )
    connection.execute("""
        CREATE TABLE IF NOT EXISTS rtdb_schema_tables (
            table_name TEXT PRIMARY KEY, chn_name TEXT NOT NULL,
            ordinal INTEGER NOT NULL, fingerprint TEXT NOT NULL, indexes_json TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS rtdb_schema_columns (
            table_name TEXT NOT NULL, ordinal INTEGER NOT NULL,
            eng_name TEXT NOT NULL, chn_name TEXT NOT NULL, data_type TEXT NOT NULL,
            primary_key INTEGER NOT NULL CHECK(primary_key IN (0,1)),
            disp_type TEXT NOT NULL, disp_menu TEXT NOT NULL,
            has_default INTEGER NOT NULL CHECK(has_default IN (0,1)), default_json TEXT,
            PRIMARY KEY(table_name, ordinal), UNIQUE(table_name, eng_name),
            FOREIGN KEY(table_name) REFERENCES rtdb_schema_tables(table_name) ON DELETE CASCADE
        )
    """)


def _column_type(declaration):
    declaration = declaration.upper()
    if "INT" in declaration:
        return "int"
    if any(part in declaration for part in ("CHAR", "CLOB", "TEXT")):
        return "str"
    if "BLOB" in declaration or not declaration:
        return "blob"
    return "double"


def _literal_default(sql, data_type):
    if sql is None:
        return None
    if data_type == "str" and TEXT.fullmatch(sql):
        return sql[1:-1].replace("''", "'")
    if data_type in {"int", "double"} and re.fullmatch(r"[+-]?\d+", sql):
        value = int(sql)
        if -(2**63) <= value < 2**63:
            return value
    if data_type == "double" and NUMBER.fullmatch(sql):
        value = float(sql)
        if math.isfinite(value):
            return value
    # SQLite retains expression defaults independently of the RTDB catalog.
    return None


def _definition(connection, table):
    if not IDENTIFIER.fullmatch(table):
        raise ValueError(f"无法登记 RTDB 表名：{table}")
    columns = []
    for _, name, declaration, _, default_sql, key_position in connection.execute(
        "SELECT * FROM pragma_table_info(?)", (table,)
    ):
        if not IDENTIFIER.fullmatch(name):
            raise ValueError(f"无法登记 RTDB 字段：{table}.{name}")
        data_type = _column_type(declaration)
        column = dict(eng_name=name, chn_name=name, data_type=data_type,
                      primary_key=bool(key_position), disp_type=data_type, disp_menu="")
        default = _literal_default(default_sql, data_type)
        if default is not None:
            column["default_value"] = default
        columns.append(column)
    if not any(column["primary_key"] for column in columns):
        raise ValueError(f"表 {table} 缺少主键，无法生成 RTDB 结构定义")
    return {"head": {"eng_name": table, "chn_name": TABLE_LABELS.get(table, table)}, "columns": columns}


def _sync_catalog(connection, database_name):
    _ensure_catalog(connection, database_name)
    tables = [
        row[0] for row in connection.execute("""
            SELECT name FROM sqlite_schema
            WHERE type='table' AND lower(name) NOT GLOB 'sqlite_*'
              AND lower(name) NOT GLOB 'rtdb_schema_*' ORDER BY name
        """)
    ]
    stored = {
        name: (ordinal, fingerprint)
        for name, ordinal, fingerprint in connection.execute(
            "SELECT table_name, ordinal, fingerprint FROM rtdb_schema_tables"
        )
    }
    for removed in stored.keys() - set(tables):
        connection.execute("DELETE FROM rtdb_schema_columns WHERE table_name=?", (removed,))
        connection.execute("DELETE FROM rtdb_schema_tables WHERE table_name=?", (removed,))
    for ordinal, table in enumerate(tables):
        definition = _definition(connection, table)
        fingerprint = sha256(json.dumps(definition, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        old_ordinal, old_fingerprint = stored.get(table, (None, None))
        if old_fingerprint == fingerprint:
            if old_ordinal != ordinal:
                connection.execute("UPDATE rtdb_schema_tables SET ordinal=? WHERE table_name=?", (ordinal, table))
            continue
        connection.execute("""
            INSERT INTO rtdb_schema_tables (table_name, chn_name, ordinal, fingerprint)
            VALUES (?, ?, ?, ?) ON CONFLICT(table_name) DO UPDATE SET
                chn_name=excluded.chn_name, ordinal=excluded.ordinal,
                fingerprint=excluded.fingerprint, indexes_json=NULL
        """, (table, definition["head"]["chn_name"], ordinal, fingerprint))
        connection.execute("DELETE FROM rtdb_schema_columns WHERE table_name=?", (table,))
        connection.executemany("""
            INSERT INTO rtdb_schema_columns (table_name, ordinal, eng_name, chn_name, data_type,
                primary_key, disp_type, disp_menu, has_default, default_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            (table, index, column["eng_name"], column["chn_name"], column["data_type"],
             int(column["primary_key"]), column["disp_type"], column["disp_menu"],
             int("default_value" in column),
             json.dumps(column["default_value"], ensure_ascii=False) if "default_value" in column else None)
            for index, column in enumerate(definition["columns"])
        ))


def _statements(script):
    start = 0
    for delimiter in re.finditer(";", script):
        end = delimiter.end()
        statement = script[start:end]
        if sqlite3.complete_statement(statement):
            yield statement
            start = end
    if script[start:].strip():
        yield script[start:]


def apply_schema(connection, database_name, script):
    """Apply the DDL and RTDB catalog together. The script must omit transaction control."""
    if connection.in_transaction:
        raise sqlite3.ProgrammingError("不能在已有事务中应用 AGC 表结构")
    try:
        # Execute individually because executescript can commit a pending transaction.
        connection.execute("BEGIN IMMEDIATE")
        for statement in _statements(script):
            if TRANSACTION_SQL.match(statement):
                raise ValueError("AGC 表结构脚本不能包含事务控制语句")
            connection.execute(statement)
        _sync_catalog(connection, database_name)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def open_database(path):
    """Open an AGC database and bring existing files up to date in place."""
    path = Path(path)
    script = SCHEMA_PATH.read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        apply_schema(connection, path.stem, script)
    except BaseException:
        connection.close()
        raise
    return path, connection


def open_realtime_database(path):
    """打开秦岭实时 AGC 数据库，应用最新控制参数、策略、曲线与日志表结构并同步 RTDB 目录。"""
    path = Path(path)
    script = REALTIME_SCHEMA_PATH.read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        apply_schema(connection, path.stem, script)
    except BaseException:
        connection.close()
        raise
    return path, connection
