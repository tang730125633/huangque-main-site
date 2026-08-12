import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.content_domains import browser_qa


class AdminBrowserE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server.admin_api as admin_api
        cls.admin = admin_api

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = self.admin.ADMIN_DB
        self.admin.ADMIN_DB = Path(self.tmp.name) / "admin.db"
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            connection.execute("""CREATE TABLE admin_e2e_runs(
                run_id TEXT PRIMARY KEY,batch_id TEXT DEFAULT '',operation_id TEXT,
                username TEXT DEFAULT '',status TEXT,job_id INTEGER,
                acceptance_id TEXT DEFAULT '',evidence_json TEXT DEFAULT '{}',
                cost INTEGER DEFAULT 0,points_before INTEGER,points_after INTEGER,
                transaction_key TEXT DEFAULT '',error TEXT DEFAULT '',created_by TEXT,
                created_at INTEGER,updated_at INTEGER)""")
            connection.commit()

    def tearDown(self):
        self.admin.ADMIN_DB = self.old_db
        self.tmp.cleanup()

    def test_private_contract_and_exact_payload_guard(self):
        payload = {
            "source_page": "banana", "model": "nb2", "quality": "std",
            "count": 1, "prompt": "qa",
            "reference_images": ["data:image/png;base64," + "x" * 40],
        }
        browser_qa.validate_nb2_reference_payload(payload, "qa")
        with self.assertRaisesRegex(browser_qa.BrowserQAError, "纳米香蕉 2"):
            browser_qa.validate_nb2_reference_payload(dict(payload, model="pro"), "qa")
        public = self.admin.function_registry.list_pages()
        mode = next(mode for page in public for feature in page["functions"]
                    for mode in feature["modes"]
                    if mode["key"] == "image.banana.nb2.reference")
        self.assertTrue(mode["validation"]["browser_supported"])
        self.assertEqual(len(mode["validation"]["browser_checks"]), 6)
        self.assertNotIn("qa-serum.png", json.dumps(mode, ensure_ascii=False))
        self.assertNotIn("huangquechuanmei.com", json.dumps(mode, ensure_ascii=False))

    def test_start_is_background_only_and_never_serializes_tokens(self):
        session = {"token": "private-account-token", "account": {
            "username": "qa-dedicated", "points": 1000, "membership_active": True,
        }}
        runner = self.admin.function_registry.e2e_runner("image.banana.nb2.reference")
        prepared = {
            "runner": runner, "endpoint": "/api/gen/banana", "cost": 10,
            "payload": {"prompt": "qa"},
        }
        thread = SimpleNamespace(start=lambda: None)
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "_e2e_prepare_operation", return_value=prepared), \
             patch.object(self.admin.threading, "Thread", return_value=thread):
            run = self.admin.start_browser_e2e_run(
                "root", "private-admin-token", "image.banana.nb2.reference"
            )
        self.assertEqual(run["status"], "browser_running")
        self.assertEqual(run["evidence"]["browser"]["total"], 6)
        self.assertNotIn("private-account-token", json.dumps(run))
        self.assertNotIn("private-admin-token", json.dumps(run))

    def test_one_browser_submit_can_bind_only_one_job(self):
        run_id = self.admin._insert_e2e_run(
            "root", "image.banana.nb2.reference", status="browser_running"
        )
        account = {"username": "qa-dedicated", "points": 1000}
        result = {
            "job_id": 77, "actual_cost": 10, "points_after": 990,
            "idempotency_key": "image-one",
            "request_sha256": "abc",
        }
        self.admin._browser_job_accepted(run_id, account, "/api/gen/banana", result)
        self.admin._browser_job_accepted(run_id, account, "/api/gen/banana", result)
        with self.assertRaisesRegex(ValueError, "两个不同 job_id"):
            self.admin._browser_job_accepted(
                run_id, account, "/api/gen/banana", dict(result, job_id=78)
            )
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            row = connection.execute(
                "SELECT status,job_id,cost,points_before,points_after,evidence_json FROM admin_e2e_runs"
            ).fetchone()
        self.assertEqual(row[:5], ("queued", 77, 10, 1000, 990))
        self.assertEqual(json.loads(row[5])["browser"]["job_id"], 77)

    def test_unknown_submit_is_not_retried_or_marked_green(self):
        run_id = self.admin._insert_e2e_run(
            "root", "image.banana.nb2.reference", status="browser_running"
        )
        self.admin._e2e_project_evidence(run_id, browser={"status": "running"})
        uncertain = type("Uncertain", (RuntimeError,), {})
        module = SimpleNamespace(
            BrowserSubmitUncertain=uncertain,
            run_nb2_reference_journey=lambda **kwargs: (_ for _ in ()).throw(
                uncertain("response missing")
            ),
        )
        session = {"token": "account", "account": {"username": "qa", "points": 100}}
        prepared = {"runner": {"browser": {
            "origin": "https://huangquechuanmei.com", "fixture": "qa-serum.png",
        }}, "endpoint": "/api/gen/banana", "payload": {"prompt": "qa"}, "cost": 10}
        with patch.object(self.admin, "browser_qa", module):
            self.admin._run_browser_e2e(run_id, "admin", session, prepared)
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            row = connection.execute(
                "SELECT status,error,evidence_json FROM admin_e2e_runs"
            ).fetchone()
        self.assertEqual(row[0], "unknown")
        self.assertIn("禁止自动重试", row[1])
        self.assertEqual(json.loads(row[2])["browser"]["status"], "unknown")

    def test_admin_requires_browser_six_and_backend_eight(self):
        source = (Path(__file__).resolve().parents[1] / "site/admin/index.html").read_text()
        self.assertIn("/api/admin/e2e/browser/run", source)
        self.assertIn("完整旅程已验收 · 6/6 + 8/8", source)
        self.assertIn("browser.job_id===run.job_id", source)


if __name__ == "__main__":
    unittest.main()
