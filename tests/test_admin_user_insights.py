import http.cookiejar
import importlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


class AuthUserInsightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.INTERNAL_TOKEN = "test-internal-token"
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.init_db()
        self.auth.create_user("admin", "secret123", 0, "admin")
        self.auth.create_user("alice", "secret123", 100, "member")
        self.auth.create_user("bob", "secret123", 20, "member")
        with sqlite3.connect(self.auth.DB) as c:
            c.execute(
                """INSERT INTO recharge_orders(
                       order_id,username,amount,points,status,created_at,reviewed_at,order_type
                   ) VALUES('R1','alice',499,1000,'approved',10,11,'membership_experience')"""
            )
            c.execute(
                """INSERT INTO recharge_orders(
                       order_id,username,amount,points,status,created_at,order_type
                   ) VALUES('R2','alice',20,200,'pending',12,'points')"""
            )
            c.execute(
                """INSERT INTO virtual_pay_orders(
                       order_id,username,openid,package_id,product_id,amount_fen,points,env,
                       status,created_at,paid_at,credited_at,order_type
                   ) VALUES('V1','alice','o','p','x',9900,100,0,'credited',20,21,22,'points')"""
            )
            c.execute(
                """INSERT INTO virtual_pay_orders(
                       order_id,username,openid,package_id,product_id,amount_fen,points,env,
                       status,created_at,order_type
                   ) VALUES('V2','alice','o','p','x',500,5,0,'created',23,'points')"""
            )
            c.execute(
                """INSERT INTO points_audit(
                       who_admin,username,delta,before_points,after_points,reason,created_at
                   ) VALUES('system','alice',-10,110,100,'job:image',30)"""
            )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def client(self, username):
        jar = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar),
        )
        self.post(client, "/api/auth/login", {"username": username, "password": "secret123"})
        return client

    def post(self, client, path, payload, internal=False):
        headers = {"Content-Type": "application/json"}
        if internal:
            headers["X-HQ-Internal-Token"] = "test-internal-token"
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(), headers=headers, method="POST",
        )
        with client.open(req, timeout=3) as response:
            return json.loads(response.read())

    def get(self, client, path):
        with client.open(self.base + path, timeout=3) as response:
            return json.loads(response.read())

    def test_payment_and_ledger_summary_is_exact_user_only(self):
        data = self.auth.admin_user_insights("alice")
        self.assertEqual(data["user"]["username"], "alice")
        self.assertEqual(data["payments"]["order_count"], 4)
        self.assertEqual(data["payments"]["paid_order_count"], 2)
        self.assertEqual(data["payments"]["paid_amount_fen"], 59800)
        self.assertEqual(data["payments"]["pending_count"], 2)
        self.assertEqual(data["ledger"]["summary"]["debits"], 10)
        self.assertIsNone(self.auth.admin_user_insights("missing"))

    def test_notification_admin_boundary_and_user_isolation(self):
        admin = self.client("admin")
        alice = self.client("alice")
        bob = self.client("bob")
        with self.assertRaises(urllib.error.HTTPError) as no_internal:
            self.post(admin, "/api/auth/admin/notifications", {
                "username": "alice", "title": "抱歉声明", "detail": "任务已恢复。",
            })
        self.assertEqual(no_internal.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as not_admin:
            self.post(alice, "/api/auth/admin/notifications", {
                "username": "alice", "title": "伪造", "detail": "不应成功",
            }, internal=True)
        self.assertEqual(not_admin.exception.code, 403)

        sent = self.post(admin, "/api/auth/admin/notifications", {
            "username": "alice", "title": "抱歉声明", "detail": "任务已恢复。",
            "created_by": "alice",
        }, internal=True)
        self.assertTrue(sent["ok"])
        self.assertNotIn("created_by", sent["notification"])
        with sqlite3.connect(self.auth.DB) as c:
            actor = c.execute("SELECT created_by FROM user_notifications").fetchone()[0]
        self.assertEqual(actor, "admin")
        self.assertEqual(self.get(alice, "/api/auth/notifications")["items"][0]["title"], "抱歉声明")
        self.assertEqual(self.get(bob, "/api/auth/notifications")["items"], [])

    def test_notification_validation_does_not_silently_truncate(self):
        notice, err = self.auth.create_user_notification("alice", "x" * 81, "正文", "admin")
        self.assertIsNone(notice)
        self.assertEqual(err, "title_too_long")
        notice, err = self.auth.create_user_notification("alice", "标题", "x" * 1001, "admin")
        self.assertIsNone(notice)
        self.assertEqual(err, "detail_too_long")

    def test_password_reset_requires_admin_and_revokes_existing_sessions(self):
        admin = self.client("admin")
        alice = self.client("alice")
        with self.assertRaises(urllib.error.HTTPError) as no_internal:
            self.post(admin, "/api/auth/admin/password/reset", {
                "username": "alice", "new_password": "temporary456",
            })
        self.assertEqual(no_internal.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as not_admin:
            self.post(alice, "/api/auth/admin/password/reset", {
                "username": "alice", "new_password": "temporary456",
            }, internal=True)
        self.assertEqual(not_admin.exception.code, 403)

        reset = self.post(admin, "/api/auth/admin/password/reset", {
            "username": "alice", "new_password": "temporary456",
        }, internal=True)
        self.assertTrue(reset["reset"]["must_change"])
        with self.assertRaises(urllib.error.HTTPError) as revoked:
            self.get(alice, "/api/auth/me")
        self.assertEqual(revoked.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as old_password:
            self.post(alice, "/api/auth/login", {"username": "alice", "password": "secret123"})
        self.assertEqual(old_password.exception.code, 401)
        relogin = self.post(alice, "/api/auth/login", {
            "username": "alice", "password": "temporary456",
        })
        self.assertTrue(relogin["user"]["must_change"])


class AdminTaskInsightsTests(unittest.TestCase):
    def setUp(self):
        import server.admin_api as admin_api

        self.admin = admin_api
        self.old_db = admin_api.JOB_DB
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.path = Path(path)
        self.admin.JOB_DB = self.path
        with sqlite3.connect(self.path) as c:
            c.execute(
                """CREATE TABLE jobs(
                       id INTEGER PRIMARY KEY,kind TEXT,username TEXT,cost INTEGER,status TEXT,
                       payload TEXT,created_at INTEGER,updated_at INTEGER)"""
            )
            c.executemany(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                [
                    (1, "xiaole_video", "alice", 20, "done", '{"channel":"micro","model":"seedance"}', 10, 20),
                    (2, "xiaole_video", "alice", 30, "error", '{"channel":"omni","model":"omni"}', 30, 40),
                    (3, "image", "alice", 8, "pending", '{"provider":"seedream","model":"seedream"}', 50, 50),
                    (4, "image", "bob", 8, "done", '{"provider":"openai"}', 60, 70),
                ],
            )

    def tearDown(self):
        self.admin.JOB_DB = self.old_db
        self.path.unlink(missing_ok=True)

    def test_cumulative_status_channel_model_and_recent_are_separated(self):
        data = self.admin.user_job_insights("alice")
        self.assertEqual((data["total"], data["done"], data["error"], data["running"]), (3, 1, 1, 1))
        self.assertEqual(data["success_rate"], 0.5)
        self.assertEqual({x["name"] for x in data["by_channel"]}, {"micro", "omni", "seedream"})
        self.assertEqual({x["name"] for x in data["by_model"]}, {"seedance", "omni", "seedream"})
        self.assertEqual([x["id"] for x in data["recent"]], [3, 2, 1])
        self.assertEqual(self.admin._job_payload('{"channel":"micro"}')["channel"], "micro")


class AdminUserInsightsFrontendTests(unittest.TestCase):
    def test_admin_and_notification_center_are_wired(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "site/admin/index.html").read_text(encoding="utf-8")
        shell = (root / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
        for marker in (
            'id="userDetailBox"', 'data-act="detail"', 'data-act="notice"',
            "/api/admin/users/detail?username=", "/api/admin/users/notification",
            "noticeSending", 'id="detailPassword"', 'type="password" minlength="6" maxlength="128"',
            "/api/admin/users/password/reset",
        ):
            self.assertIn(marker, html)
        self.assertIn("/api/auth/notifications?limit=50", shell)
        self.assertIn("server-notice-", shell)
        self.assertIn("escapeHtml(x.title)", shell)


if __name__ == "__main__":
    unittest.main()
