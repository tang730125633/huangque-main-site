import importlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer


class InviteAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")
        import server.auth_server as auth_server
        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.INTERNAL_TOKEN = "test-internal"
        self.auth.INVITE_HASH_SECRET = "test-invite-secret"
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.init_db()
        self.auth.create_user("admin", "secret123", 0, "admin")
        self.auth.create_user("inviter", "secret123")
        c = self.connect()
        code = self.auth.invites.ensure_user_code(c, self.user_id("inviter", c))["code"]
        c.commit(); c.close()
        result, err = self.auth.register_account("invitee", "secret123", "被邀请用户", code)
        self.assertIsNone(err)
        self.assertTrue(result["invite_bound"])

    def tearDown(self):
        os.environ.pop("HQ_TEST_AUTH_DB", None)
        self.tmp.cleanup()

    def connect(self):
        c = sqlite3.connect(self.auth.DB)
        c.row_factory = sqlite3.Row
        return c

    def user_id(self, username, c=None):
        own = c is None
        c = c or self.connect()
        try:
            return c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
        finally:
            if own:
                c.close()

    def test_config_stats_filters_actions_and_audit(self):
        c = self.connect()
        try:
            config = self.auth.invites.admin_update_config(c, {
                "name": "暑期邀请", "status": "enabled", "code_required": True,
                "daily_invite_limit": 88,
            }, self.user_id("admin", c))
            self.assertEqual(config["daily_invite_limit"], 88)
            self.assertEqual(config["code_required"], 1)
            stats = self.auth.invites.admin_stats(c, 7)
            self.assertEqual(stats["total"], 1)
            data = self.auth.invites.admin_relations(c, {"invitee": "invitee"})
            self.assertEqual(data["total"], 1)
            relation_id = data["items"][0]["id"]
            self.auth.invites.admin_relation_action(c, relation_id, "ban", "批量注册复核", self.user_id("admin", c))
            c.commit()
            row = c.execute("SELECT account_status FROM users WHERE username='invitee'").fetchone()
            self.assertEqual(row[0], "banned")
            self.auth.invites.admin_relation_action(c, relation_id, "unban", "确认正常", self.user_id("admin", c))
            self.auth.invites.admin_relation_action(c, relation_id, "invalidate", "测试无效", self.user_id("admin", c))
            self.auth.invites.admin_relation_action(c, relation_id, "restore", "", self.user_id("admin", c))
            c.commit()
            self.assertGreaterEqual(len(self.auth.invites.admin_audit(c)), 5)
        finally:
            c.close()

    def test_xlsx_export_contains_chinese_and_is_valid_zip(self):
        c = self.connect()
        try:
            body = self.auth.invites.export_relations_xlsx(c)
        finally:
            c.close()
        self.assertTrue(body.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(body)) as book:
            self.assertIn("xl/worksheets/sheet1.xml", book.namelist())
            sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("邀请人账号", sheet)
            self.assertIn("被邀请用户", sheet)

    def test_admin_http_endpoints_require_admin_and_internal_token(self):
        token = self.auth.issue_token("admin")
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        headers = {"Authorization": "Bearer " + token, "X-HQ-Internal-Token": "test-internal"}
        try:
            req = urllib.request.Request(base + "/api/auth/admin/invite/stats", headers=headers)
            stats = json.loads(urllib.request.urlopen(req, timeout=3).read())
            self.assertEqual(stats["total"], 1)
            payload = json.dumps({"name": "接口活动", "status": "enabled", "code_required": False,
                                  "daily_invite_limit": 60}).encode()
            req = urllib.request.Request(base + "/api/auth/admin/invite/config", data=payload,
                                         headers={**headers, "Content-Type": "application/json"}, method="PUT")
            saved = json.loads(urllib.request.urlopen(req, timeout=3).read())
            self.assertEqual(saved["config"]["name"], "接口活动")
            req = urllib.request.Request(base + "/api/auth/admin/invite/export.xlsx", headers=headers)
            response = urllib.request.urlopen(req, timeout=3)
            self.assertEqual(response.headers.get_content_type(),
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.assertTrue(response.read().startswith(b"PK"))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
