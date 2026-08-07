import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class AdminE2ERunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server.admin_api as admin_api
        cls.admin = admin_api

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old = self.admin.ADMIN_DB, self.admin.JOB_DB, self.admin.ASSET_DB
        self.admin.ADMIN_DB = root / "admin.db"
        self.admin.JOB_DB = root / "jobs.db"
        self.admin.ASSET_DB = root / "assets.db"
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            connection.execute("""CREATE TABLE admin_e2e_runs(
                run_id TEXT PRIMARY KEY,operation_id TEXT,username TEXT DEFAULT '',status TEXT,
                job_id INTEGER,cost INTEGER DEFAULT 0,points_before INTEGER,points_after INTEGER,
                transaction_key TEXT DEFAULT '',error TEXT DEFAULT '',created_by TEXT,
                created_at INTEGER,updated_at INTEGER)""")
            connection.commit()

    def tearDown(self):
        self.admin.ADMIN_DB, self.admin.JOB_DB, self.admin.ASSET_DB = self.old
        self.tmp.cleanup()

    def test_server_runs_once_without_exposing_fixture_or_account_token(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 30)), \
             patch.object(self.admin, "_content_e2e_request", return_value={
                 "job_id": 77, "cost": 30, "points_left": 470,
             }) as submit:
            run = self.admin.start_e2e_run(
                "root", "admin-token", "video.digital_ip.text.single"
            )
            self.assertEqual(run["username"], "qa-dedicated")
            self.assertEqual(run["job_id"], 77)
            payload = submit.call_args.args[2]
            self.assertTrue(payload["image_data"].startswith("data:image/"))
            self.assertNotIn("short-lived-secret", json.dumps(run))
            self.assertNotIn("image_data", json.dumps(run))
            with self.assertRaisesRegex(ValueError, "已有测试任务"):
                self.admin.start_e2e_run(
                    "root", "admin-token", "video.digital_ip.text.single"
                )

    def test_fixture_reader_rejects_paths_outside_private_directory(self):
        with self.assertRaisesRegex(ValueError, "名称无效"):
            self.admin._fixture_data_url("@fixture/../admin_api.py")

    def test_release_moves_fixtures_to_private_runtime_and_removes_public_copy(self):
        root = Path(__file__).resolve().parents[1]
        ship = (root / "ship").read_text(encoding="utf-8")
        drift = (root / "scripts/drift_sentinel.py").read_text(encoding="utf-8")
        self.assertIn("server/qa_fixtures/*", ship)
        self.assertIn("workbench/assets/qa/$(basename", ship)
        self.assertIn("QA_FIXTURES_RUNTIME", drift)


class AuthE2ESessionTests(unittest.TestCase):
    def setUp(self):
        import server.auth_server as auth_server
        self.auth = auth_server
        self.tmp = tempfile.TemporaryDirectory()
        self.old = self.auth.DB, self.auth.INTERNAL_TOKEN, self.auth.E2E_TEST_USERNAME
        self.auth.DB = str(Path(self.tmp.name) / "users.db")
        self.auth.INTERNAL_TOKEN = "internal-test-token"
        self.auth.E2E_TEST_USERNAME = "qa-dedicated"
        self.auth.init_db()
        self.auth.create_user("root", "secret1", 0, "admin")
        self.auth.create_user("qa-dedicated", "secret2", 500, "member")
        with closing(self.auth.db()) as connection:
            connection.execute("UPDATE users SET must_change=0 WHERE username IN ('root','qa-dedicated')")
            connection.commit()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.auth.DB, self.auth.INTERNAL_TOKEN, self.auth.E2E_TEST_USERNAME = self.old
        self.tmp.cleanup()

    def test_only_admin_internal_call_can_issue_dedicated_account_session(self):
        admin_token = self.auth.issue_token("root")
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/auth/admin/e2e/session" % self.server.server_address[1],
            data=b"{}", method="POST", headers={
                "Authorization": "Bearer " + admin_token,
                "X-HQ-Internal-Token": self.auth.INTERNAL_TOKEN,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            data = json.loads(response.read())
        self.assertEqual(data["account"]["username"], "qa-dedicated")
        with closing(self.auth.db()) as connection:
            row = connection.execute(
                "SELECT username,scope FROM tokens WHERE token=?", (data["token"],)
            ).fetchone()
        self.assertEqual((row["username"], row["scope"]), ("qa-dedicated", "account"))
        blocked = urllib.request.Request(
            request.full_url, data=b"{}", method="POST",
            headers={"Authorization": "Bearer " + admin_token},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(blocked, timeout=3)
        self.assertEqual(caught.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
