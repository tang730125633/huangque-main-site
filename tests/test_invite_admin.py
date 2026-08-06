import importlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
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
        now = int(time.time())
        c.execute(
            """UPDATE users SET membership_tier='experience',membership_started_at=?,membership_expires_at=?
                 WHERE username='inviter'""",
            (now, now + self.auth.MEMBERSHIP_YEAR_SECONDS),
        )
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
            c.execute("UPDATE users SET username='13800000031',display_name='13800000031' WHERE username='admin'")
            c.commit()
            audit = self.auth.invites.admin_audit(c)
            self.assertGreaterEqual(len(audit), 5)
            self.assertEqual(audit[0]["operator_account"], "138****0031")
            self.assertEqual(audit[0]["operator_name"], "138****0031")
            self.assertNotIn("operator_username", audit[0])
            raw_audit = c.execute("SELECT before_json,after_json FROM invite_admin_audit ORDER BY id DESC LIMIT 1").fetchone()
            self.assertNotIn("ip_hash", raw_audit["before_json"] + raw_audit["after_json"])
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
            self.assertIn("邀请人账号ID", sheet)
            self.assertIn("被邀请用户", sheet)
            self.assertNotIn(">inviter<", sheet)
            self.assertNotIn(">invitee<", sheet)

    def test_legacy_invite_table_gains_invalid_reason_column(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("""CREATE TABLE user_invites(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            inviter_user_id INTEGER NOT NULL,
            invitee_user_id INTEGER NOT NULL UNIQUE,
            invite_code TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'bound',
            risk_status TEXT NOT NULL DEFAULT 'normal',
            bound_at INTEGER NOT NULL,
            ip_hash TEXT,
            device_hash TEXT,
            updated_at INTEGER NOT NULL
        )""")
        self.auth.invites.init_schema(c)
        columns = {row["name"] for row in c.execute("PRAGMA table_info(user_invites)")}
        c.close()
        self.assertIn("invalid_reason", columns)

    def test_admin_http_endpoints_require_admin_and_internal_token(self):
        token = self.auth.issue_token("admin")
        c = self.connect()
        invitee_id = self.user_id("invitee", c)
        c.execute(
            "UPDATE users SET username='13800000031',display_name='13800000031' WHERE id=?",
            (invitee_id,),
        )
        c.commit(); c.close()
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        headers = {"Authorization": "Bearer " + token, "X-HQ-Internal-Token": "test-internal"}
        try:
            req = urllib.request.Request(base + "/api/auth/admin/invite/stats", headers=headers)
            stats = json.loads(urllib.request.urlopen(req, timeout=3).read())
            self.assertEqual(stats["total"], 1)
            req = urllib.request.Request(base + "/api/auth/admin/invite/journeys", headers=headers)
            journeys = json.loads(urllib.request.urlopen(req, timeout=3).read())
            self.assertEqual(journeys["summary"]["visited"], 0)
            req = urllib.request.Request(
                base + "/api/auth/admin/invite/network?search=13800000031", headers=headers,
            )
            network_search = json.loads(urllib.request.urlopen(req, timeout=3).read())
            self.assertEqual(network_search["items"][0]["username"], "138****0031")
            req = urllib.request.Request(
                base + "/api/auth/admin/invite/network?user_id=%s" % invitee_id, headers=headers,
            )
            network = json.loads(urllib.request.urlopen(req, timeout=3).read())
            self.assertEqual(network["root"]["username"], "138****0031")
            self.assertNotIn("13800000031", json.dumps(network, ensure_ascii=False))
            req = urllib.request.Request(base + "/api/auth/admin/invite/relations", headers=headers)
            relations = json.loads(urllib.request.urlopen(req, timeout=3).read())
            self.assertNotIn("inviter_username", relations["items"][0])
            self.assertNotIn("invitee_username", relations["items"][0])
            self.assertTrue(relations["items"][0]["invite_code"].endswith("••••"))
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
