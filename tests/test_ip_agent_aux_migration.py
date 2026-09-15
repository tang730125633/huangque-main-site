import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from server.db import postgres

PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_ip_agent_aux.py"
SPEC = importlib.util.spec_from_file_location("migrate_ip_agent_aux", PATH)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class IPAgentAuxMigrationTest(unittest.TestCase):
    def tearDown(self):
        postgres.close_pool()

    def test_scan_excludes_v4_and_counts_profile_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "owned.json").write_text(json.dumps({
                "profile": {"name": "A"}, "report": {"status": "ready"}
            }), encoding="utf-8")
            (root / "empty.json").write_text(json.dumps({
                "profile": {}, "report": {}
            }), encoding="utf-8")
            (root / "v4-owned.json").write_text(json.dumps({
                "main": [{"role": "user", "content": "ignored"}]
            }), encoding="utf-8")
            items = migration.scan(root)
            report = migration.summary(items)
            self.assertEqual(report["sessions"], 2)
            self.assertEqual(report["profiles"], 1)
            self.assertEqual(report["reports"], 1)
            self.assertEqual(len(report["source_checksum"]), 64)

    @unittest.skipUnless(os.environ.get("HQ_DATABASE_URL"), "PostgreSQL integration URL not configured")
    def test_apply_is_idempotent_and_audited(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ci-aux.json").write_text(json.dumps({
                "profile": {"name": "A"}, "report": {"status": "ready"}
            }), encoding="utf-8")
            items = migration.scan(root)
            first = migration.apply(items, root, "ci-test-sha")
            second = migration.apply(items, root, "ci-test-sha")
            self.assertEqual((first["inserted"], first["skipped"]), (1, 0))
            self.assertEqual((second["inserted"], second["skipped"]), (0, 1))
            with postgres.connection() as conn:
                row = conn.execute(
                    "SELECT snapshot FROM agent.session_aux WHERE session_id = 'ci-aux'"
                ).fetchone()
                self.assertEqual(row["snapshot"]["report"]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
