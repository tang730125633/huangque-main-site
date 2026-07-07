import os
import json
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
from http.server import ThreadingHTTPServer


class AuthPointsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")

        import importlib
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.INTERNAL_TOKEN = "test-internal-token"
        self.auth.init_db()
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute(
                "INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change) "
                "VALUES('fang','h','s','fang',10,'member',0)"
            )
            c.commit()
        finally:
            c.close()

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def test_deduct_is_atomic_and_rejects_insufficient_points(self):
        points, err = self.auth.deduct_points("fang", 7)
        self.assertIsNone(err)
        self.assertEqual(points["points"], 3)

        points, err = self.auth.deduct_points("fang", 4)
        self.assertIsNone(points)
        self.assertEqual(err, "insufficient")
        self.assertEqual(self.auth.get_points_row("fang")["points"], 3)

    def test_refund_adds_points(self):
        points, err = self.auth.refund_points("fang", 5)
        self.assertIsNone(err)
        self.assertEqual(points["points"], 15)

    def test_concurrent_deduct_never_overdraws(self):
        results = []
        lock = threading.Lock()

        def worker():
            points, err = self.auth.deduct_points("fang", 1)
            with lock:
                results.append((points, err))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ok = [r for r in results if r[1] is None]
        insufficient = [r for r in results if r[1] == "insufficient"]
        self.assertEqual(len(ok), 10)
        self.assertEqual(len(insufficient), 10)
        self.assertEqual(self.auth.get_points_row("fang")["points"], 0)

    def test_http_points_endpoints_require_internal_token(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/api/auth/points?username=fang", timeout=3)
            self.assertEqual(ctx.exception.code, 403)

            req = urllib.request.Request(
                base + "/api/auth/points/deduct",
                data=json.dumps({"username": "fang", "amount": 4}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-HQ-Internal-Token": "test-internal-token",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())
            self.assertEqual(data["points"], 6)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_login_sets_http_only_cookie_without_plaintext_token_body(self):
        self.auth.create_user("cookie_user", "secret123", 5)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            req = urllib.request.Request(
                base + "/api/auth/login",
                data=json.dumps({"username": "cookie_user", "password": "secret123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())
                cookie = r.headers.get("Set-Cookie") or ""

            self.assertNotIn("token", data)
            self.assertEqual(data["user"]["username"], "cookie_user")
            self.assertIn("HttpOnly", cookie)
            self.assertIn(self.auth.AUTH_COOKIE_NAME + "=", cookie)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
