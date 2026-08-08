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
        self.old = self.admin.ADMIN_DB, self.admin.JOB_DB, self.admin.ASSET_DB, self.admin.CONTENT_OUT
        self.admin.ADMIN_DB = root / "admin.db"
        self.admin.JOB_DB = root / "jobs.db"
        self.admin.ASSET_DB = root / "assets.db"
        self.admin.CONTENT_OUT = root / "content_out"
        self.admin.CONTENT_OUT.mkdir()
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            connection.execute("""CREATE TABLE admin_e2e_runs(
                run_id TEXT PRIMARY KEY,operation_id TEXT,username TEXT DEFAULT '',status TEXT,
                job_id INTEGER,cost INTEGER DEFAULT 0,points_before INTEGER,points_after INTEGER,
                transaction_key TEXT DEFAULT '',error TEXT DEFAULT '',created_by TEXT,
                created_at INTEGER,updated_at INTEGER)""")
            connection.commit()

    def tearDown(self):
        self.admin.ADMIN_DB, self.admin.JOB_DB, self.admin.ASSET_DB, self.admin.CONTENT_OUT = self.old
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
            with self.assertRaisesRegex(ValueError, "已有一条测试旅程"):
                self.admin.start_e2e_run(
                    "root", "admin-token", "video.digital_ip.audio"
                )

    def test_fixture_reader_rejects_paths_outside_private_directory(self):
        with self.assertRaisesRegex(ValueError, "名称无效"):
            self.admin._fixture_data_url("@fixture/../admin_api.py")

    def test_cinematic_open_uses_one_ready_qa_avatar_and_four_second_quote(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "_content_e2e_get", return_value={"items": [
                 {"id": 41, "status": "ready"}, {"id": 42, "status": "ready"},
             ]}), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 120)), \
             patch.object(self.admin, "_content_e2e_request", return_value={
                 "job_id": 88, "cost": 120, "points_left": 380,
             }) as submit:
            run = self.admin.start_e2e_run(
                "root", "admin-token", "video.cinematic.open"
            )
        payload = submit.call_args.args[2]
        self.assertEqual(submit.call_args.args[:2], ("/api/gen/cinematic", "short-lived-secret"))
        self.assertEqual(payload["avatar_ids"], [41])
        self.assertEqual(payload["cine_mode"], "open")
        self.assertEqual(payload["duration"], 4)
        self.assertEqual(run["job_id"], 88)
        self.assertNotIn("short-lived-secret", json.dumps(run))

    def test_cinematic_open_fails_before_submission_without_ready_avatar(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "_content_e2e_get", return_value={"items": []}), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 120)), \
             patch.object(self.admin, "_content_e2e_request") as submit:
            with self.assertRaisesRegex(ValueError, "尚未登记已就绪"):
                self.admin.start_e2e_run(
                    "root", "admin-token", "video.cinematic.open"
                )
        submit.assert_not_called()

    def test_preflight_quotes_fixture_without_creating_paid_task_or_exposing_bytes(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 30)), \
             patch.object(self.admin, "_content_e2e_request") as submit:
            result = self.admin.e2e_preflight(
                "admin-token", "video.digital_ip.text.single"
            )
        self.assertTrue(result["ready"])
        self.assertEqual((result["cost"], result["points"]), (30, 500))
        self.assertIn("清晰度：720p", result["parameters"])
        self.assertNotIn("short-lived-secret", json.dumps(result))
        self.assertNotIn("data:image", json.dumps(result))
        submit.assert_not_called()

    def test_preflight_blocks_every_mode_while_another_journey_is_running(self):
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            connection.execute(
                "INSERT INTO admin_e2e_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("active-run", "video.omni.text", "qa-dedicated", "running", 77,
                 30, 500, 470, "ledger-77", "", "root", 1, 2),
            )
            connection.commit()
        with patch.object(self.admin, "_e2e_job_evidence", return_value={
                "status": "running", "provider_task_id": "provider-77",
                "completed": False, "delivery_verified": False,
                "billing_state": "in_flight", "artifact_check": "not_recorded",
                "error": "",
        }):
            result = self.admin.e2e_preflight("admin-token", "video.grok.text")
        self.assertFalse(result["ready"])
        self.assertIn("另一条测试旅程", result["blocker"])

    def test_grok_image_uses_matching_product_fixture_and_supported_resolution(self):
        runner = self.admin.function_registry.e2e_runner("video.grok.image")
        payload = self.admin._e2e_payload("video.grok.image", runner)
        self.assertEqual(payload["resolution"], "720p")
        self.assertEqual(len(payload["reference_images"]), 1)
        self.assertTrue(payload["reference_images"][0].startswith("data:image/png;base64,"))
        self.assertIn("精华瓶", payload["prompt"])

    def test_cinematic_motion_uses_private_video_and_server_quote(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "_content_e2e_get", return_value={"items": [
                 {"id": 41, "status": "ready"},
             ]}), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 90)), \
             patch.object(self.admin, "_content_e2e_post", return_value={"cost": 90}) as quote, \
             patch.object(self.admin, "_content_e2e_request", return_value={
                 "job_id": 89, "cost": 90, "points_left": 410,
             }) as submit:
            ready = self.admin.e2e_preflight("admin-token", "video.cinematic.motion")
            run = self.admin.start_e2e_run("root", "admin-token", "video.cinematic.motion")
        self.assertTrue(ready["ready"])
        self.assertIn("时长：随参考视频", ready["parameters"])
        self.assertEqual(quote.call_args.args[0], "/api/gen/cinematic/quote")
        payload = submit.call_args.args[2]
        self.assertEqual(payload["cine_mode"], "motion")
        self.assertEqual(payload["avatar_ids"], [41])
        self.assertTrue(payload["reference_video_data"].startswith("data:video/"))
        self.assertEqual(run["cost"], 90)

    def test_task_card_and_e2e_share_delivery_and_ledger_evidence(self):
        (self.admin.CONTENT_OUT / "result.mp4").write_bytes(b"video")
        with closing(sqlite3.connect(self.admin.JOB_DB)) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY,status TEXT,cost INTEGER,refunded INTEGER,error TEXT,
                created_at INTEGER,updated_at INTEGER,result TEXT)""")
            connection.execute(
                "INSERT INTO jobs VALUES(77,'done',30,0,'',1,2,?)",
                (json.dumps({"video_file": "result.mp4", "video_url": "https://cdn.example/result.mp4",
                             "provider_video_id": "provider-77"}),),
            )
            connection.commit()
        with closing(sqlite3.connect(self.admin.ASSET_DB)) as connection:
            connection.execute("""CREATE TABLE video_assets(
                job_id INTEGER,phase TEXT,provider_video_id TEXT,video_file TEXT,
                video_url TEXT,status TEXT,error TEXT,updated_at INTEGER)""")
            connection.execute(
                "INSERT INTO video_assets VALUES(77,'completed','provider-77','result.mp4',"
                "'https://cdn.example/result.mp4','done','',2)"
            )
            connection.commit()
        row = {
            "run_id": "run-77", "operation_id": "video.digital_ip.text.single",
            "username": "qa-dedicated", "status": "completed", "job_id": 77,
            "cost": 30, "points_before": 500, "points_after": 470,
            "transaction_key": "ledger-77", "error": "", "created_by": "root",
            "created_at": 1, "updated_at": 2,
        }
        ledger = {"username": "qa-dedicated", "delta": -30, "after_points": 470}
        with patch.object(self.admin.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"h264\n")), \
             patch.object(self.admin, "points_domain", SimpleNamespace(get_points_transaction=lambda key: ledger)):
            evidence = self.admin._e2e_job_evidence(77)
            run = self.admin._public_e2e_run(row)
        self.assertTrue(evidence["delivery_verified"])
        self.assertEqual(evidence["artifact_check"], "decodable")
        self.assertEqual(next(stage for stage in run["stages"] if stage["key"] == "delivery")["state"], "passed")
        self.assertEqual(next(stage for stage in run["stages"] if stage["key"] == "billing")["detail"], "扣点流水一致")
        self.assertNotIn("transaction_key", run)

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
