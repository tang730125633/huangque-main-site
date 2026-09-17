"""一次性邀请码批量功能测试（2026-09-17 老板发首批 100 人需求）。

覆盖：
1. admin 批量生成接口：数量校验、inviter_username 归属、返回码列表。
2. 一次性码：注册绑定成功即作废，同码二次注册 409 code_used。
3. 永久码不受影响：一人一 active 码、可反复注册绑定。
4. 唯一索引语义：同一 inviter 可持多个 active 一次性码 + 一个 active 永久码。
"""

import importlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


class InviteBulkCodesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.pop("HQ_MEMBERSHIP_ENFORCEMENT_ENABLED", None)
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.path.join(self.tmp.name, "users.db")
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.INVITE_HASH_SECRET = "test-invite-secret"
        self.auth.INTERNAL_TOKEN = "test-internal-token"
        self.auth.REGISTER_MAX = 200
        self.auth.REGISTER_WINDOW = 120
        self.auth.REGISTER_IP_MAX = 400
        self.auth.REGISTER_IP_WINDOW = 60
        self.auth.REGISTER_HITS.clear()
        self.auth.init_db()
        self.auth.create_user("admin", "unit-test-admin-pass", 10)
        self.auth.create_user("boss", "boss123", 10)
        now = int(time.time())
        conn = sqlite3.connect(self.auth.DB)
        conn.execute("UPDATE users SET role='admin' WHERE username='admin'")
        conn.execute(
            """UPDATE users SET membership_tier='experience',
                      membership_started_at=?, membership_expires_at=?
                WHERE username='boss'""",
            (now, now + 365 * 86400),
        )
        conn.execute(
            "UPDATE invite_campaigns SET code_required=1, daily_invite_limit=500"
        )
        conn.commit()
        conn.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _post(self, path, body, headers=None):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        # 本机系统代理会把 127.0.0.1 请求拦成 502，测试必须直连。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=15) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw or b"{}"), dict(resp.headers)
                except ValueError:
                    return resp.status, {}, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"{}"), dict(exc.headers)
            except ValueError:
                return exc.code, {}, dict(exc.headers)

    def _admin_cookie(self):
        _, _, headers = self._post("/api/auth/login", {
            "username": "admin", "password": "unit-test-admin-pass",
        })
        cookie = headers.get("Set-Cookie") or ""
        return cookie.split(";")[0]

    def _admin_headers(self):
        return {
            "Cookie": self._admin_cookie(),
            "X-HQ-Internal-Token": "test-internal-token",
        }

    def test_bulk_create_and_single_use(self):
        status, body, _ = self._post(
            "/api/auth/admin/invite/codes",
            {"count": 3, "batch_label": "第一批", "inviter_username": "boss"},
            self._admin_headers(),
        )
        self.assertEqual(status, 200, body)
        codes = body["batch"]["codes"]
        self.assertEqual(len(codes), 3)
        conn = sqlite3.connect(self.auth.DB)
        boss_id = int(conn.execute(
            "SELECT id FROM users WHERE username='boss'").fetchone()[0])
        conn.close()
        self.assertEqual(body["batch"]["inviter_user_id"], boss_id)
        self.assertEqual(body["batch"]["batch_label"], "第一批")
        for code in codes:
            self.assertEqual(len(code), 6)

        # 一次性码：第一次注册绑定成功
        status, body, _ = self._post("/api/auth/register", {
            "username": "user-a", "password": "secret123",
            "invite_code": codes[0], "invite_source": "web_manual",
        })
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("invite_bound"))

        # 同码第二次注册：已作废
        status, body, _ = self._post("/api/auth/register", {
            "username": "user-b", "password": "secret123",
            "invite_code": codes[0], "invite_source": "web_manual",
        })
        self.assertEqual(status, 409, body)
        self.assertEqual(body.get("code"), "code_used")

        # 其余码不受影响
        status, body, _ = self._post("/api/auth/register", {
            "username": "user-c", "password": "secret123",
            "invite_code": codes[1], "invite_source": "web_manual",
        })
        self.assertEqual(status, 200, body)

    def test_permanent_code_unaffected(self):
        import server.invites as invites

        conn = sqlite3.connect(self.auth.DB)
        conn.row_factory = sqlite3.Row
        boss_id = int(conn.execute(
            "SELECT id FROM users WHERE username='boss'").fetchone()["id"])
        row = invites.ensure_user_code(conn, boss_id)  # boss 的永久码
        conn.commit()
        conn.close()
        perm = row["code"]

        # 永久码可反复绑定不同用户
        for name in ("user-x", "user-y"):
            status, body, _ = self._post("/api/auth/register", {
                "username": name, "password": "secret123",
                "invite_code": perm, "invite_source": "web_manual",
            })
            self.assertEqual(status, 200, body)

        conn = sqlite3.connect(self.auth.DB)
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM invite_codes WHERE code=?", (perm,))]
        conn.close()
        self.assertEqual(statuses, ["active"])

    def test_count_validation(self):
        status, body, _ = self._post(
            "/api/auth/admin/invite/codes",
            {"count": 0, "inviter_username": "boss"},
            self._admin_headers(),
        )
        self.assertEqual(status, 400, body)
        status, body, _ = self._post(
            "/api/auth/admin/invite/codes",
            {"count": 1001, "inviter_username": "boss"},
            self._admin_headers(),
        )
        self.assertEqual(status, 400, body)
        # 非 admin 不可用
        _, _, headers = self._post("/api/auth/login", {
            "username": "boss", "password": "boss123",
        })
        cookie = (headers.get("Set-Cookie") or "").split(";")[0]
        status, body, _ = self._post(
            "/api/auth/admin/invite/codes",
            {"count": 3, "inviter_username": "boss"},
            {"Cookie": cookie, "X-HQ-Internal-Token": "test-internal-token"},
        )
        self.assertEqual(status, 403, body)


if __name__ == "__main__":
    unittest.main()
