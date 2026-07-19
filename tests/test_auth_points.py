import os
import json
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch


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

    def test_wechat_transaction_can_only_approve_one_recharge_order(self):
        first, first_err = self.auth.create_recharge_order("fang", 99, 1000, "微信扫码充值")
        second, second_err = self.auth.create_recharge_order("fang", 99, 1000, "微信扫码充值")
        self.assertIsNone(first_err)
        self.assertIsNone(second_err)

        _, approve_err = self.auth.review_recharge_order(
            "wxpay", first["order_id"], "approve", "paid",
            transaction_id="wx-transaction-1", pay_channel="wxpay",
        )
        duplicate, duplicate_err = self.auth.review_recharge_order(
            "wxpay", second["order_id"], "approve", "paid",
            transaction_id="wx-transaction-1", pay_channel="wxpay",
        )

        self.assertIsNone(approve_err)
        self.assertIsNone(duplicate)
        self.assertEqual(duplicate_err, "transaction_in_use")
        self.assertEqual(self.auth.get_points_row("fang")["points"], 1010)
        c = sqlite3.connect(self.auth.DB)
        try:
            row = c.execute(
                "SELECT transaction_id,pay_channel FROM recharge_orders WHERE order_id=?",
                (first["order_id"],),
            ).fetchone()
            self.assertEqual(row, ("wx-transaction-1", "wxpay"))
        finally:
            c.close()

    def test_wechat_callback_identity_must_match_merchant_and_app(self):
        import server.wxpay as wxpay

        expected = {"appid": "wx-huangque", "mchid": "merchant-huangque"}
        with patch.object(wxpay, "_config", return_value=expected):
            self.assertTrue(wxpay.payment_identity_matches(expected))
            self.assertFalse(wxpay.payment_identity_matches({
                "appid": "wx-other", "mchid": "merchant-huangque",
            }))
            self.assertFalse(wxpay.payment_identity_matches({
                "appid": "wx-huangque", "mchid": "merchant-other",
            }))

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

    def test_miniprogram_login_returns_token_usable_as_bearer(self):
        self.auth.create_user("mp_user", "secret123", 5)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            req = urllib.request.Request(
                base + "/api/auth/miniprogram-login",
                data=json.dumps({"username": "mp_user", "password": "secret123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())

            self.assertIn("token", data)
            self.assertEqual(data["user"]["username"], "mp_user")

            req2 = urllib.request.Request(
                base + "/api/auth/me",
                headers={"Authorization": "Bearer " + data["token"]},
            )
            with urllib.request.urlopen(req2, timeout=3) as r:
                me_data = json.loads(r.read())
            self.assertEqual(me_data["user"]["username"], "mp_user")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_miniprogram_register_returns_token_and_creates_user(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            req = urllib.request.Request(
                base + "/api/auth/miniprogram-register",
                data=json.dumps({"username": "mp_new", "password": "secret123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())

            self.assertIn("token", data)
            self.assertEqual(data["user"]["username"], "mp_new")
            self.assertEqual(data["user"]["points"], self.auth.NEW_USER_TRIAL_POINTS)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
