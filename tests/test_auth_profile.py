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
