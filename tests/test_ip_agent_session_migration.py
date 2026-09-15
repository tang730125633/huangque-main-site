import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from server.db import postgres

PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_ip_agent_sessions.py"
SPEC = importlib.util.spec_from_file_location("migrate_ip_agent_sessions", PATH)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class IPAgentSessionMigrationTest(unittest.TestCase):
    def tearDown(self):
        postgres.close_pool()

    def test_scan_preserves_snapshot_and_reports_legacy_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "v4-owned.json").write_text(json.dumps({
                "owner": {"username": "u", "account_id": "a"},
                "main": [{"role": "user", "content": "hi"}],
                "widgets": [],
            }), encoding="utf-8")
            (root / "v4-legacy.json").write_text(json.dumps({
                "main": [], "widgets": []
            }), encoding="utf-8")
            sessions = migration.scan_sessions(root)
            report = migration.summary(sessions)
            self.assertEqual(report["sessions"], 2)
            self.assertEqual(report["messages"], 1)
            self.assertEqual(report["owners_missing"], 1)
            self.assertEqual(len(report["source_checksum"]), 64)

    @unittest.skipUnless(os.environ.get("HQ_DATABASE_URL"), "PostgreSQL integration URL not configured")
    def test_apply_is_idempotent_and_audited(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "v4-ci-session.json").write_text(json.dumps({
                "owner": {"username": "ci-user", "account_id": "ci-account"},
                "main": [{"role": "user", "content": "hello"}],
                "widgets": [],
            }), encoding="utf-8")
            sessions = migration.scan_sessions(root)
            first = migration.apply_sessions(sessions, root, "ci-test-sha")
            second = migration.apply_sessions(sessions, root, "ci-test-sha")
            self.assertEqual((first["inserted"], first["skipped"]), (1, 0))
            self.assertEqual((second["inserted"], second["skipped"]), (0, 1))
            with postgres.connection() as conn:
                row = conn.execute(
                    "SELECT owner_account_id, snapshot FROM agent.sessions "
                    "WHERE session_id = 'ci-session'"
                ).fetchone()
                self.assertEqual(row["owner_account_id"], "ci-account")
                self.assertEqual(row["snapshot"]["main"][0]["content"], "hello")
                audits = conn.execute(
                    "SELECT count(*) AS count FROM ops.data_migration_runs "
                    "WHERE domain = 'agent' AND state = 'verified'"
                ).fetchone()
                self.assertGreaterEqual(audits["count"], 2)


if __name__ == "__main__":
    unittest.main()
