"""Regression checks for the SQLite structures consumed by RTDB."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agc_schema import apply_schema, open_database


class EmbeddedSchemaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def open(self, name="agc.db"):
        path, connection = open_database(self.directory / name)
        self.addCleanup(connection.close)
        return path, connection

    def test_new_database_registers_business_tables_and_composite_keys(self):
        path, connection = self.open()
        self.assertEqual(path, self.directory / "agc.db")
        self.assertEqual(connection.execute(
            "SELECT storage_format FROM rtdb_schema_database WHERE version=1"
        ).fetchone()[0], "native")
        registry = connection.execute(
            "SELECT table_name FROM rtdb_schema_tables ORDER BY table_name"
        )
        self.assertEqual([row[0] for row in registry], [
            "control_cycles", "device_curves", "device_samples", "devices",
            "event_logs", "system_settings",
        ])
        keys = connection.execute(
            "SELECT eng_name FROM rtdb_schema_columns "
            "WHERE table_name='device_curves' AND primary_key=1 ORDER BY ordinal"
        )
        self.assertEqual([row[0] for row in keys], ["kind", "device_id", "point_index"])
        data_type, has_default, default_json = connection.execute(
            "SELECT data_type, has_default, default_json FROM rtdb_schema_columns "
            "WHERE table_name='devices' AND eng_name='control_state'"
        ).fetchone()
        self.assertEqual((data_type, has_default, json.loads(default_json)), ("str", 1, "remote"))

    def test_schema_change_rebuilds_definition_and_preserves_native_rows(self):
        _, connection = self.open()
        apply_schema(connection, "agc", "CREATE TABLE extra(id INTEGER PRIMARY KEY);")
        with connection:
            connection.execute("INSERT INTO extra(id) VALUES (42)")
        fingerprint = connection.execute(
            "SELECT fingerprint FROM rtdb_schema_tables WHERE table_name='extra'"
        ).fetchone()[0]
        apply_schema(connection, "agc", "ALTER TABLE extra ADD COLUMN voltage REAL DEFAULT 1.25;")
        self.assertEqual(tuple(connection.execute("SELECT id, voltage FROM extra").fetchone()),
                         (42, 1.25))
        updated_fingerprint = connection.execute(
            "SELECT fingerprint FROM rtdb_schema_tables WHERE table_name='extra'"
        ).fetchone()[0]
        self.assertNotEqual(updated_fingerprint, fingerprint)
        data_type, default_json = connection.execute(
            "SELECT data_type, default_json FROM rtdb_schema_columns "
            "WHERE table_name='extra' AND eng_name='voltage'"
        ).fetchone()
        self.assertEqual((data_type, json.loads(default_json)), ("double", 1.25))
        apply_schema(connection, "agc", "ALTER TABLE extra RENAME TO extra_renamed;")
        self.assertIsNone(connection.execute(
            "SELECT table_name FROM rtdb_schema_tables WHERE table_name='extra'"
        ).fetchone())
        self.assertEqual(tuple(connection.execute("SELECT id, voltage FROM extra_renamed").fetchone()),
                         (42, 1.25))
        apply_schema(connection, "agc", "DROP TABLE extra_renamed;")
        self.assertIsNone(connection.execute(
            "SELECT eng_name FROM rtdb_schema_columns WHERE table_name='extra_renamed'"
        ).fetchone())

    def test_schema_change_with_sql_error_rolls_back_ddl_and_registry(self):
        _, connection = self.open()
        before = [tuple(row) for row in connection.execute("SELECT * FROM rtdb_schema_tables ORDER BY table_name")]
        with self.assertRaises(sqlite3.DatabaseError):
            apply_schema(connection, "agc", """
                CREATE TABLE must_rollback(id INTEGER PRIMARY KEY);
                ALTER TABLE devices ADD COLUMN kind TEXT;
            """)
        self.assertFalse(connection.in_transaction)
        self.assertIsNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE name='must_rollback'"
        ).fetchone())
        after = [tuple(row) for row in connection.execute("SELECT * FROM rtdb_schema_tables ORDER BY table_name")]
        self.assertEqual(after, before)

    def test_schema_registration_error_rolls_back_new_table_and_prior_ddl(self):
        _, connection = self.open()
        with self.assertRaisesRegex(ValueError, "缺少主键"):
            apply_schema(connection, "agc", """
                ALTER TABLE devices ADD COLUMN voltage REAL;
                CREATE TABLE invalid_table(voltage REAL);
            """)
        self.assertNotIn("voltage", [row[1] for row in connection.execute("PRAGMA table_info(devices)")])
        self.assertIsNone(connection.execute(
            "SELECT table_name FROM rtdb_schema_tables WHERE table_name='invalid_table'"
        ).fetchone())
        self.assertIsNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE name='invalid_table'"
        ).fetchone())

    def test_migration_cannot_commit_ddl_before_catalog_sync(self):
        _, connection = self.open()
        with self.assertRaisesRegex(ValueError, "事务控制"):
            apply_schema(connection, "agc", """
                CREATE TABLE partial(id INTEGER PRIMARY KEY);
                /* comment with a semicolon; */ COMMIT;
                CREATE TABLE invalid_table(voltage REAL);
            """)
        self.assertIsNone(connection.execute(
            "SELECT name FROM sqlite_schema WHERE name='partial'"
        ).fetchone())

    def test_native_data_remains_live_without_copying_business_tables(self):
        path, connection = self.open()
        with connection:
            connection.execute("INSERT INTO event_logs(logged_at,level,message) VALUES ('now','INFO','AGC')")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
            self.assertEqual(reader.execute("SELECT message FROM event_logs").fetchone()[0], "AGC")
            with connection:
                connection.execute("UPDATE event_logs SET message='changed'")
            self.assertEqual(reader.execute("SELECT message FROM event_logs").fetchone()[0], "changed")
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM event_logs").fetchone()[0], 1)
            self.assertIsNone(reader.execute(
                "SELECT name FROM sqlite_master WHERE name GLOB 'rtdb_data_*' OR name GLOB 'rtdb_metadata_*'"
            ).fetchone())

    def test_existing_db_is_upgraded_in_place_and_reopen_keeps_existing_definition(self):
        legacy = self.directory / "agc.db"
        with closing(sqlite3.connect(legacy)) as source, source:
            schema = Path(__file__).resolve().parents[1] / "web" / "agc_schema.sql"
            source.executescript(schema.read_text(encoding="utf-8"))
            source.execute("INSERT INTO event_logs(logged_at,level,message) VALUES ('old','INFO','legacy')")
        path, connection = self.open(legacy.name)
        self.assertEqual(path, legacy)
        self.assertEqual(connection.execute("SELECT message FROM event_logs").fetchone()[0], "legacy")
        with connection:
            connection.execute("UPDATE event_logs SET message='new'")
        before = [tuple(row) for row in connection.execute(
            "SELECT * FROM rtdb_schema_columns ORDER BY table_name, ordinal"
        )]

        reopened_path, reopened = self.open(legacy.name)
        self.assertEqual(reopened_path, path)
        self.assertEqual([row[0] for row in reopened.execute("SELECT message FROM event_logs")], ["new"])
        after = [tuple(row) for row in reopened.execute("SELECT * FROM rtdb_schema_columns ORDER BY table_name, ordinal")]
        self.assertEqual(after, before)
        self.assertEqual(reopened.total_changes, 0)
        self.assertFalse(legacy.with_suffix(".sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
