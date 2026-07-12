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


class AuthProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")

        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.init_db()
        self.auth.create_user("profile_user", "secret123", 19, "member")
        self.auth.create_user("friend_user", "secret123", 7, "member")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self._post("/api/auth/login", {"username": "profile_user", "password": "secret123"})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def _post(self, path, payload, client=None):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with (client or self.client).open(req, timeout=3) as response:
            return json.loads(response.read())

    def _get(self, path, client=None):
        with (client or self.client).open(self.base + path, timeout=3) as response:
            return json.loads(response.read())

    def test_profile_updates_display_name_only(self):
        data = self._post("/api/auth/profile", {
            "display_name": "  新昵称  ", "role": "admin", "points": 999999,
        })
        self.assertTrue(data["ok"])
        self.assertEqual(data["user"]["name"], "新昵称")
        self.assertEqual(data["user"]["role"], "member")
        self.assertEqual(data["user"]["points"], 19)
        c = sqlite3.connect(self.auth.DB)
        try:
            row = c.execute(
                "SELECT display_name,role,points FROM users WHERE username='profile_user'"
            ).fetchone()
        finally:
            c.close()
        self.assertEqual(row, ("新昵称", "member", 19))

    def test_me_returns_stable_fixed_account_id(self):
        first = self._get("/api/auth/me")["user"]["account_id"]
        second = self._get("/api/auth/me")["user"]["account_id"]
        self.assertEqual(first, second)
        self.assertEqual(len(first), self.auth.ACCOUNT_ID_LENGTH)
        self.assertTrue(first.startswith(self.auth.ACCOUNT_ID_PREFIX))

    def test_profile_cannot_modify_account_id(self):
        before = self._get("/api/auth/me")["user"]["account_id"]
        data = self._post("/api/auth/profile", {
            "display_name": "Visible Name",
            "account_id": "HQAAAAAA",
        })
        self.assertEqual(data["user"]["account_id"], before)
        after = self._get("/api/auth/me")["user"]["account_id"]
        self.assertEqual(after, before)

    def test_init_db_backfills_missing_account_ids(self):
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute("UPDATE users SET account_id=NULL WHERE username='profile_user'")
            c.commit()
        finally:
            c.close()
        self.auth.init_db()
        data = self._get("/api/auth/me")
        self.assertEqual(len(data["user"]["account_id"]), self.auth.ACCOUNT_ID_LENGTH)

    def test_add_friend_by_account_id(self):
        c = sqlite3.connect(self.auth.DB)
        try:
            account_id = c.execute(
                "SELECT account_id FROM users WHERE username='friend_user'"
            ).fetchone()[0]
        finally:
            c.close()
        data = self._post("/api/auth/friends/add", {"account_id": account_id.lower()})
        self.assertTrue(data["ok"])
        self.assertEqual(data["friends"][0]["username"], "friend_user")
        listed = self._get("/api/auth/friends")
        self.assertEqual(listed["friends"][0]["account_id"], account_id)

    def test_add_friend_rejects_self_and_duplicate(self):
        me = self._get("/api/auth/me")["user"]["account_id"]
        with self.assertRaises(urllib.error.HTTPError) as self_ctx:
            self._post("/api/auth/friends/add", {"account_id": me})
        self.assertEqual(self_ctx.exception.code, 400)
        c = sqlite3.connect(self.auth.DB)
        try:
            account_id = c.execute(
                "SELECT account_id FROM users WHERE username='friend_user'"
            ).fetchone()[0]
        finally:
            c.close()
        self._post("/api/auth/friends/add", {"account_id": account_id})
        with self.assertRaises(urllib.error.HTTPError) as dup_ctx:
            self._post("/api/auth/friends/add", {"account_id": account_id})
        self.assertEqual(dup_ctx.exception.code, 409)

    def test_profile_rejects_empty_and_long_names(self):
        for name in ("   ", "a" * 33):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/auth/profile", {"display_name": name})
            self.assertEqual(ctx.exception.code, 400)

    def test_profile_requires_login(self):
        anonymous = urllib.request.build_opener()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/auth/profile", {"display_name": "访客"}, anonymous)
        self.assertEqual(ctx.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
