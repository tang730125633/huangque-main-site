import importlib.util
import sqlite3
import time
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "check_short_drama_completion_integrity.py"
)
SPEC = importlib.util.spec_from_file_location("completion_integrity", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CompletionIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            CREATE TABLE short_drama_projects (
              id TEXT PRIMARY KEY, stage TEXT, completion_id TEXT
            );
            CREATE TABLE short_drama_completions (
              completion_id TEXT PRIMARY KEY, project_id TEXT, asset_id TEXT
            );
            CREATE TABLE short_drama_completion_attempts (
              id TEXT PRIMARY KEY, project_id TEXT, state TEXT, updated_at INTEGER
            );
            CREATE TABLE short_drama_final_assets (
              id TEXT PRIMARY KEY, project_id TEXT, archive_status TEXT, deleted INTEGER
            );
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_consistent_completion_is_clean(self):
        self.conn.execute(
            "INSERT INTO short_drama_projects VALUES ('p','completed','c')"
        )
        self.conn.execute(
            "INSERT INTO short_drama_completions VALUES ('c','p','a')"
        )
        self.conn.execute(
            "INSERT INTO short_drama_final_assets VALUES ('a','p','ready',0)"
        )
        result = MODULE.inspect_connection(self.conn, now=1000)
        self.assertTrue(result["ok"])
        self.assertEqual([], result["issues"])

    def test_reports_snapshot_asset_and_stale_attempt_problems(self):
        self.conn.execute(
            "INSERT INTO short_drama_projects VALUES ('p','completed',NULL)"
        )
        self.conn.execute(
            "INSERT INTO short_drama_completions VALUES ('c','orphan','missing')"
        )
        self.conn.execute(
            "INSERT INTO short_drama_completion_attempts VALUES (?,?,?,?)",
            ("attempt", "p", "started", int(time.time()) - 600),
        )
        result = MODULE.inspect_connection(
            self.conn, now=int(time.time()), stale_seconds=300
        )
        codes = {item["code"] for item in result["issues"]}
        self.assertFalse(result["ok"])
        self.assertTrue({
            "completed_snapshot_mismatch",
            "snapshot_project_mismatch",
            "delivery_asset_invalid",
            "completion_attempt_stale",
        }.issubset(codes))


if __name__ == "__main__":
    unittest.main()
