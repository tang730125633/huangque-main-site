import json
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

    def test_delivery_notification_includes_pay_signature(self):
        with patch.object(self.auth.wechat_vpay, "access_token", return_value="wx-token"), \
             patch.object(self.auth.wechat_vpay, "_json_request", return_value={}) as request:
            self.auth.wechat_vpay.notify_provide_goods("HQ1", 0)

        url, body = request.call_args.args[:2]
        self.assertIn("/xpay/notify_provide_goods?", url)
        self.assertIn("pay_sig=", url)
        self.assertEqual(json.loads(body), {"order_id": "HQ1", "env": 0})

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

    def test_custom_amount_uses_one_yuan_unit_goods_and_server_calculated_points(self):
        os.environ["WX_VIRTUAL_PAY_PRODUCTS_JSON"] = (
            '[{"id":"custom_points","product_id":"hq_points_custom","title":"自定义点数",'
            '"price_fen":100,"points":10,"custom_amount":true}]'
        )
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            result, err = self.auth.create_virtual_pay_order(
                "buyer", "custom_points", "wx-code", "25"
            )

        self.assertIsNone(err)
        self.assertEqual(result["order"]["amount_fen"], 2500)
        self.assertEqual(result["order"]["points"], 250)
        sign_data = json.loads(result["payment"]["signData"])
        self.assertEqual(sign_data["productId"], "hq_points_custom")
        self.assertEqual(sign_data["goodsPrice"], 100)
        self.assertEqual(sign_data["buyQuantity"], 25)
        self.assertEqual(sign_data["attach"], "points:250")

    def test_custom_amount_rejects_missing_decimal_boolean_and_out_of_range_values(self):
        os.environ["WX_VIRTUAL_PAY_PRODUCTS_JSON"] = (
            '[{"id":"custom_points","product_id":"hq_points_custom","title":"自定义点数",'
            '"price_fen":100,"points":10,"custom_amount":true}]'
        )
        invalid_values = (None, "", "0", "1.5", 1.5, 5001, True)
        with patch.object(self.auth.wechat_vpay, "code_to_session") as code_to_session:
            for value in invalid_values:
                result, err = self.auth.create_virtual_pay_order(
                    "buyer", "custom_points", "wx-code", value
                )
                self.assertIsNone(result)
                self.assertEqual(err, "invalid_custom_amount")
        code_to_session.assert_not_called()

    def test_fixed_package_never_trusts_forged_custom_amount(self):
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            result, err = self.auth.create_virtual_pay_order(
                "buyer", "test_pack", "wx-code", 5000
            )
        self.assertIsNone(err)
        self.assertEqual(result["order"]["amount_fen"], 1)
        self.assertEqual(result["order"]["points"], 10)
        self.assertEqual(json.loads(result["payment"]["signData"])["buyQuantity"], 1)

    def test_public_packages_separate_fixed_tiers_from_custom_configuration(self):
        os.environ["WX_VIRTUAL_PAY_PRODUCTS_JSON"] = (
            '[{"id":"fixed","product_id":"hq_fixed","title":"固定档",'
            '"price_fen":9900,"points":1000},'
            '{"id":"custom_points","product_id":"hq_points_custom","title":"自定义点数",'
            '"price_fen":100,"points":10,"custom_amount":true}]'
        )
        self.assertEqual([item["id"] for item in self.auth.public_virtual_pay_packages()], ["fixed"])
        self.assertEqual(self.auth.public_virtual_pay_custom(), {
            "package_id": "custom_points",
            "min_amount_yuan": 1,
            "max_amount_yuan": 5000,
            "points_per_yuan": 10,
        })


if __name__ == "__main__":
    unittest.main()
