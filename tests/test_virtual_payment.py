import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class VirtualPaymentTests(unittest.TestCase):
    ENV_KEYS = (
        "WX_MP_APPID",
        "WX_MP_APPSECRET",
        "WX_VIRTUAL_PAY_ENV",
        "WX_VIRTUAL_PAY_OFFER_ID",
        "WX_VIRTUAL_PAY_APP_KEY_PROD",
        "WX_VIRTUAL_PAY_APP_KEY_SANDBOX",
        "WX_VIRTUAL_PAY_PRODUCTS_JSON",
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        os.environ.update({
            "WX_MP_APPID": "wx-test",
            "WX_MP_APPSECRET": "test-secret",
            "WX_VIRTUAL_PAY_ENV": "0",
            "WX_VIRTUAL_PAY_OFFER_ID": "offer-test",
            "WX_VIRTUAL_PAY_APP_KEY_PROD": "prod-app-key",
            "WX_VIRTUAL_PAY_PRODUCTS_JSON": (
                '[{"id":"test_pack","product_id":"hq_test_pack","title":"测试包",'
                '"price_fen":1,"points":10,"recommended":true}]'
            ),
        })

        import importlib
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.path.join(self.tmp.name, "users.db")
        self.auth.init_db()
        self.auth.create_user("buyer", "secret123", 5)

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_signatures_match_official_hmac_shape(self):
        body = '{"openid":"o1","env":0,"order_id":"HQ1"}'
        expected = self.auth.wechat_vpay._hmac_hex("prod-app-key", "/xpay/query_order&" + body)
        self.assertEqual(
            self.auth.wechat_vpay.calc_pay_sig("/xpay/query_order", body, "prod-app-key"),
            expected,
        )

    def test_create_order_returns_only_client_payment_fields_and_binds_openid(self):
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            result, err = self.auth.create_virtual_pay_order("buyer", "test_pack", "wx-code")

        self.assertIsNone(err)
        self.assertEqual(result["order"]["amount_fen"], 1)
        self.assertEqual(result["order"]["points"], 10)
        self.assertEqual(set(result["payment"]), {"mode", "signData", "paySig", "signature"})
        self.assertNotIn("session-key", str(result))
        self.assertNotIn("prod-app-key", str(result))

        c = sqlite3.connect(self.auth.DB)
        try:
            self.assertEqual(c.execute("SELECT wx_openid FROM users WHERE username='buyer'").fetchone()[0], "openid-buyer")
        finally:
            c.close()

    def test_paid_order_credits_points_exactly_once(self):
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            result, err = self.auth.create_virtual_pay_order("buyer", "test_pack", "wx-code")
        self.assertIsNone(err)
        order_id = result["order"]["order_id"]
        wx_result = {
            "errcode": 0,
            "order": {
                "order_id": order_id,
                "status": 2,
                "order_fee": 1,
                "paid_fee": 1,
                "paid_time": 1784200000,
                "wx_order_id": "wx-order-1",
                "wxpay_order_id": "wxpay-1",
            },
        }
        with patch.object(self.auth.wechat_vpay, "query_order", return_value=wx_result) as query, \
             patch.object(self.auth.wechat_vpay, "notify_provide_goods", return_value={}):
            first, first_err = self.auth.confirm_virtual_pay_order("buyer", order_id)
            second, second_err = self.auth.confirm_virtual_pay_order("buyer", order_id)

        self.assertIsNone(first_err)
        self.assertIsNone(second_err)
        self.assertEqual(first["status"], "credited")
        self.assertEqual(second["status"], "credited")
        self.assertEqual(self.auth.get_points_row("buyer")["points"], 15)
        self.assertEqual(query.call_count, 1)

        c = sqlite3.connect(self.auth.DB)
        try:
            audits = c.execute(
                "SELECT COUNT(*) FROM points_audit WHERE username='buyer' AND reason LIKE '微信虚拟支付:%'"
            ).fetchone()[0]
            self.assertEqual(audits, 1)
        finally:
            c.close()

    def test_amount_mismatch_never_credits_points(self):
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            result, _ = self.auth.create_virtual_pay_order("buyer", "test_pack", "wx-code")
        order_id = result["order"]["order_id"]
        with patch.object(self.auth.wechat_vpay, "query_order", return_value={
            "errcode": 0,
            "order": {"order_id": order_id, "status": 2, "order_fee": 2},
        }):
            confirmed, err = self.auth.confirm_virtual_pay_order("buyer", order_id)
        self.assertIsNone(confirmed)
        self.assertEqual(err, "amount_mismatch")
        self.assertEqual(self.auth.get_points_row("buyer")["points"], 5)


if __name__ == "__main__":
    unittest.main()
