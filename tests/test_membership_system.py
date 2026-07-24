import http.cookiejar
import importlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class MembershipSystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        self.old_enforcement = os.environ.get("HQ_MEMBERSHIP_ENFORCEMENT_ENABLED")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")
        os.environ.pop("HQ_MEMBERSHIP_ENFORCEMENT_ENABLED", None)
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.INTERNAL_TOKEN = "membership-test-token"
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.init_db()
        self.auth.create_user("buyer", "secret123", 20, "member")
        self.auth.create_user("admin", "secret123", 20, "admin")

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        if self.old_enforcement is None:
            os.environ.pop("HQ_MEMBERSHIP_ENFORCEMENT_ENABLED", None)
        else:
            os.environ["HQ_MEMBERSHIP_ENFORCEMENT_ENABLED"] = self.old_enforcement
        self.tmp.cleanup()

    def _row(self, username):
        c = self.auth.db()
        try:
            return c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        finally:
            c.close()

    def test_experience_order_is_fixed_499_and_activates_one_year_with_1000_points(self):
        self.assertEqual(
            self.auth.purchase_quote(499, "membership_experience"),
            (499, 1000, "membership_experience"),
        )
        self.assertIsNone(self.auth.purchase_quote(99, "membership_experience"))
        order, err = self.auth.create_recharge_order(
            "buyer", 499, 1000, "体验官", "membership_experience",
        )
        self.assertIsNone(err)
        before = int(time.time())
        approved, err = self.auth.review_recharge_order("admin", order["order_id"], "approve", "到账")
        self.assertIsNone(err)
        self.assertEqual(approved["order_type"], "membership_experience")
        row = self._row("buyer")
        self.assertEqual(row["points"], 1020)
        self.assertEqual(row["membership_tier"], "experience")
        self.assertGreaterEqual(row["membership_expires_at"], before + self.auth.MEMBERSHIP_YEAR_SECONDS)
        self.assertLessEqual(row["membership_expires_at"], int(time.time()) + self.auth.MEMBERSHIP_YEAR_SECONDS)
        c = self.auth.db()
        try:
            self.assertEqual(
                c.execute(
                    "SELECT username,source FROM membership_voice_slot_entitlements"
                ).fetchall()[0]["username"],
                "buyer",
            )
        finally:
            c.close()

    def test_admin_can_set_each_one_year_tier_and_cancel(self):
        now = 1800000000
        for tier in ("experience", "partner", "initiator"):
            user, err = self.auth.set_membership_admin("admin", "buyer", tier, "测试", now=now)
            self.assertIsNone(err)
            self.assertEqual(user["membership_tier"], tier)
            self.assertEqual(user["membership_expires_at"], now + self.auth.MEMBERSHIP_YEAR_SECONDS)
        user, err = self.auth.set_membership_admin("admin", "buyer", "", "取消", now=now)
        self.assertIsNone(err)
        self.assertFalse(user["membership_active"])
        c = self.auth.db()
        try:
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM membership_voice_slot_entitlements WHERE username='buyer'"
                ).fetchone()[0],
                1,
            )
        finally:
            c.close()

    def test_admin_membership_recharge_extends_same_tier_by_one_year(self):
        now = 1800000000
        first, err = self.auth.recharge_membership_admin(
            "admin", "buyer", "partner", "首次充值", request_id="recharge-first", now=now,
        )
        self.assertIsNone(err)
        first_expiry = now + self.auth.MEMBERSHIP_YEAR_SECONDS
        self.assertEqual(first["membership_tier"], "partner")
        self.assertEqual(first["membership_expires_at"], first_expiry)

        renewed, err = self.auth.recharge_membership_admin(
            "admin", "buyer", "partner", "同级续费", request_id="recharge-renew", now=now + 10,
        )
        self.assertIsNone(err)
        self.assertEqual(
            renewed["membership_expires_at"], first_expiry + self.auth.MEMBERSHIP_YEAR_SECONDS,
        )

        duplicate, err = self.auth.recharge_membership_admin(
            "admin", "buyer", "partner", "网络重试", request_id="recharge-renew", now=now + 20,
        )
        self.assertIsNone(err)
        self.assertTrue(duplicate["membership_recharge_duplicate"])
        self.assertEqual(duplicate["membership_expires_at"], renewed["membership_expires_at"])
        c = self.auth.db()
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM membership_recharge_records").fetchone()[0], 2)
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM membership_voice_slot_entitlements WHERE username='buyer'"
                ).fetchone()[0],
                1,
            )
        finally:
            c.close()

    def test_membership_and_slot_entitlement_rollback_together(self):
        with patch.object(
            self.auth,
            "_grant_membership_voice_slot_entitlement",
            side_effect=RuntimeError("entitlement write failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "entitlement"):
                self.auth.set_membership_admin(
                    "admin", "buyer", "partner", "transaction test", now=1800000000,
                )
        row = self._row("buyer")
        self.assertEqual(row["membership_tier"], "")
        c = self.auth.db()
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM membership_audit").fetchone()[0], 0)
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM membership_voice_slot_entitlements").fetchone()[0],
                0,
            )
        finally:
            c.close()

    def test_three_membership_tiers_quote_all_three_fixed_packages(self):
        expected = {
            "experience": {100: 100, 200: 200, 500: 500},
            "partner": {100: 75, 200: 150, 500: 375},
            "initiator": {100: 55, 200: 110, 500: 275},
        }
        for tier, prices in expected.items():
            for list_amount, pay_amount in prices.items():
                with self.subTest(tier=tier, list_amount=list_amount):
                    self.assertEqual(
                        self.auth.purchase_quote(
                            list_amount, "points", membership_tier=tier,
                        ),
                        (pay_amount, list_amount * 10, "points"),
                    )

    def test_discounted_wxpay_callback_amount_mismatch_never_credits(self):
        now = int(time.time())
        c = self.auth.db()
        try:
            c.execute(
                """UPDATE users
                      SET membership_tier='partner',membership_started_at=?,membership_expires_at=?
                    WHERE username='buyer'""",
                (now, now + self.auth.MEMBERSHIP_YEAR_SECONDS),
            )
            c.commit()
        finally:
            c.close()
        amount, points, _ = self.auth.purchase_quote(
            100, "points", membership_tier="partner",
        )
        order, err = self.auth.create_recharge_order(
            "buyer", amount, points, "partner discounted", "points",
            list_amount=100, pricing_tier="partner", discount_bps=7500,
        )
        self.assertIsNone(err)

        class FakeWxPay:
            @staticmethod
            def configured():
                return True

            @staticmethod
            def verify_notify(headers, raw):
                return True

            @staticmethod
            def decrypt_resource(resource):
                return {
                    "trade_state": "SUCCESS",
                    "out_trade_no": order["order_id"],
                    "transaction_id": "wx-wrong-amount",
                    "amount": {"total": 10000},
                }

            @staticmethod
            def payment_identity_matches(resource):
                return True

        self.auth.wxpay = FakeWxPay()
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:%d/api/auth/wxpay/notify" % server.server_address[1],
                data=b'{"resource":{}}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(self._row("buyer")["points"], 20)
            self.assertEqual(self.auth.get_recharge_order(order["order_id"])["status"], "pending")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_expired_membership_is_not_active(self):
        data = self.auth.membership_public("partner", 100, 200, now=201)
        self.assertFalse(data["membership_active"])
        self.assertEqual(data["membership_tier"], "")

    def test_http_blocks_nonmember_deduct_and_point_recharge_but_allows_membership_order(self):
        os.environ["HQ_MEMBERSHIP_ENFORCEMENT_ENABLED"] = "1"
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        jar = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            login = urllib.request.Request(
                base + "/api/auth/login",
                data=json.dumps({"username": "buyer", "password": "secret123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            client.open(login, timeout=3).close()

            def post(path, payload, headers=None):
                request = urllib.request.Request(
                    base + path,
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", **(headers or {})},
                    method="POST",
                )
                return client.open(request, timeout=3)

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                post("/api/auth/recharge/order", {"amount": 99, "product_type": "points"})
            self.assertEqual(ctx.exception.code, 403)

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                post(
                    "/api/auth/points/deduct",
                    {"username": "buyer", "amount": 1},
                    {"X-HQ-Internal-Token": "membership-test-token"},
                )
            self.assertEqual(ctx.exception.code, 403)

            with post(
                "/api/auth/recharge/order",
                {"amount": 499, "product_type": "membership_experience"},
            ) as response:
                data = json.loads(response.read())
            self.assertEqual(data["order"]["points"], 1000)
            self.assertEqual(data["order"]["order_type"], "membership_experience")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_enforcement_defaults_off_for_web_miniprogram_and_generation_paths(self):
        self.assertFalse(self.auth.membership_enforcement_enabled())

        class FakeWxPay:
            @staticmethod
            def configured():
                return True

            @staticmethod
            def jscode2session(code):
                return "openid-buyer"

            @staticmethod
            def create_jsapi(order_id, description, amount_fen, openid):
                return "prepay-test"

            @staticmethod
            def jsapi_pay_params(prepay_id):
                return {"prepay_id": prepay_id}

        self.auth.wxpay = FakeWxPay()
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        jar = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            login = urllib.request.Request(
                base + "/api/auth/login",
                data=json.dumps({"username": "buyer", "password": "secret123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            client.open(login, timeout=3).close()

            def post(path, payload, headers=None):
                request = urllib.request.Request(
                    base + path,
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", **(headers or {})},
                    method="POST",
                )
                with client.open(request, timeout=3) as response:
                    return response.status, json.loads(response.read())

            self.assertEqual(
                post("/api/auth/recharge/order", {"amount": 100, "product_type": "points"})[0],
                200,
            )
            self.assertEqual(
                post(
                    "/api/auth/wxpay/jsapi",
                    {"amount": 100, "product_type": "points", "js_code": "code"},
                )[0],
                200,
            )
            self.assertEqual(
                post(
                    "/api/auth/points/deduct",
                    {"username": "buyer", "amount": 1},
                    {"X-HQ-Internal-Token": "membership-test-token"},
                )[0],
                200,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_pages_expose_membership_controls_and_hide_point_area_by_default(self):
        recharge = (ROOT / "site" / "workbench" / "recharge.html").read_text(encoding="utf-8")
        admin = (ROOT / "site" / "admin" / "index.html").read_text(encoding="utf-8")
        self.assertIn("¥499 开通体验官 · 赠 1000 点", recharge)
        self.assertIn('id="pointsRechargeArea" hidden', recharge)
        self.assertIn("membership_experience", recharge)
        self.assertIn("体验官 / 合伙人 / 发起人 / 取消会员", admin)
        self.assertIn("/api/admin/membership/set", admin)
        self.assertIn("/api/admin/membership/recharge", admin)
        self.assertIn("/api/admin/membership/recharge/preview", admin)
        self.assertIn("membership_recharge_duplicate", admin)
        self.assertIn("充值会员", admin)
        self.assertIn("同等级续费从原到期日顺延一年", admin)
        self.assertIn("function membershipDisplayName(user)", recharge)
        self.assertIn("experience:'体验官',partner:'合伙人',initiator:'发起人'", recharge)
        self.assertNotIn("user.membership_name||'会员'", recharge)

    def test_changed_page_inline_javascript_parses(self):
        for relative in (("site", "workbench", "recharge.html"), ("site", "admin", "index.html")):
            html = ROOT.joinpath(*relative).read_text(encoding="utf-8")
            scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", html, re.I)
            checked = subprocess.run(
                ["node", "--check", "-"], input="\n".join(scripts), text=True,
                encoding="utf-8", capture_output=True, check=False,
            )
            self.assertEqual(checked.returncode, 0, "%s: %s" % (relative, checked.stderr))


if __name__ == "__main__":
    unittest.main()
