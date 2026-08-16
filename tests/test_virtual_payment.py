import base64
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
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
        "WX_MESSAGE_PUSH_TOKEN",
        "WX_MESSAGE_PUSH_AES_KEY",
        "HQ_MINIPROGRAM_PAYMENTS_ENABLED",
        "HQ_MEMBERSHIP_ENFORCEMENT_ENABLED",
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
            "WX_MESSAGE_PUSH_TOKEN": "push-token",
            "WX_MESSAGE_PUSH_AES_KEY": base64.b64encode(b"K" * 32).decode().rstrip("="),
        })

        import importlib
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.wechat_vpay._TOKEN_CACHE.update(value="", expires_at=0)
        self.auth.DB = os.path.join(self.tmp.name, "users.db")
        self.auth.init_db()
        self.auth.create_user("buyer", "secret123", 5)

    def tearDown(self):
        self.auth.wechat_vpay._TOKEN_CACHE.update(value="", expires_at=0)
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

    def test_default_virtual_goods_match_current_wechat_published_prices(self):
        os.environ.pop("WX_VIRTUAL_PAY_PRODUCTS_JSON", None)
        products = self.auth.wechat_vpay.products()
        self.assertEqual(
            [(item["product_id"], item["price_fen"]) for item in products[:3]],
            [
                ("hq_points_1000", 9900),
                ("hq_points_2000", 19900),
                ("hq_points_5000", 49900),
            ],
        )

    def test_membership_virtual_good_is_always_available_at_499_yuan(self):
        product = self.auth.wechat_vpay.product_by_id("membership_experience")
        renewal = self.auth.wechat_vpay.product_by_id("membership_experience_renewal")

        self.assertEqual(product["product_id"], "hq_member_exp_1y")
        self.assertEqual(product["price_fen"], 49900)
        self.assertEqual(product["points"], 1000)
        self.assertEqual(product["order_type"], "membership_experience")
        self.assertEqual(renewal["price_fen"], 49900)
        self.assertEqual(renewal["points"], 0)
        self.assertEqual(renewal["order_type"], "membership_experience_renewal")
        self.assertNotIn(
            "membership_experience",
            [item["id"] for item in self.auth.public_virtual_pay_packages("experience")],
        )

    def test_miniprogram_payment_switch_defaults_on_and_accepts_off_values(self):
        os.environ.pop("HQ_MINIPROGRAM_PAYMENTS_ENABLED", None)
        self.assertTrue(self.auth.miniprogram_payments_enabled())
        for value in ("0", "false", "FALSE", "no", "off"):
            os.environ["HQ_MINIPROGRAM_PAYMENTS_ENABLED"] = value
            self.assertFalse(self.auth.miniprogram_payments_enabled())

    def test_disabled_switch_blocks_virtual_order_before_wechat_calls(self):
        os.environ["HQ_MINIPROGRAM_PAYMENTS_ENABLED"] = "0"
        with patch.object(self.auth.wechat_vpay, "code_to_session") as code_to_session:
            result, err = self.auth.create_virtual_pay_order(
                "buyer", "test_pack", "wx-code"
            )
        self.assertIsNone(result)
        self.assertEqual(err, "payment_disabled")
        code_to_session.assert_not_called()

    def test_disabled_switch_blocks_both_miniprogram_order_routes(self):
        os.environ["HQ_MINIPROGRAM_PAYMENTS_ENABLED"] = "0"
        for path in ("/api/auth/virtual-pay/order", "/api/auth/wxpay/jsapi"):
            sent = []
            handler = self.auth.H.__new__(self.auth.H)
            handler.path = path
            handler._user = lambda: {"username": "buyer"}
            handler._send = lambda status, payload, extra_headers=None: sent.append(
                (status, payload)
            )
            handler._body = lambda: self.fail("disabled route must not read request body")

            handler.do_POST()

            self.assertEqual(sent, [(503, {
                "detail": "小程序支付功能暂时关闭",
                "code": "payment_disabled",
            })])

    def test_disabled_switch_hides_virtual_payment_packages(self):
        os.environ["HQ_MINIPROGRAM_PAYMENTS_ENABLED"] = "0"
        sent = []
        handler = self.auth.H.__new__(self.auth.H)
        handler.path = "/api/auth/virtual-pay/packages"
        handler._user = lambda: {"username": "buyer"}
        handler._require_membership = lambda row: True
        handler._send = lambda status, payload, extra_headers=None: sent.append(
            (status, payload)
        )

        handler.do_GET()

        status, payload = sent[0]
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["items"], [])
        self.assertIsNone(payload["custom"])

    def test_delivery_notification_includes_pay_signature(self):
        with patch.object(self.auth.wechat_vpay, "access_token", return_value="wx-token"), \
             patch.object(self.auth.wechat_vpay, "_json_request", return_value={}) as request:
            self.auth.wechat_vpay.notify_provide_goods("HQ1", 0)

        url, body = request.call_args.args[:2]
        self.assertIn("/xpay/notify_provide_goods?", url)
        self.assertIn("pay_sig=", url)
        self.assertEqual(json.loads(body), {"order_id": "HQ1", "env": 0})

    def test_access_token_uses_shared_stable_endpoint_and_cache(self):
        client = self.auth.wechat_vpay
        response = {"access_token": "stable-token", "expires_in": 7200}

        with patch.object(client, "_json_request", return_value=response) as request:
            self.assertEqual(client.access_token(), "stable-token")
            self.assertEqual(client.access_token(), "stable-token")

        request.assert_called_once()
        url, raw = request.call_args.args
        self.assertEqual(url, client.API_BASE + "/cgi-bin/stable_token")
        self.assertEqual(json.loads(raw), {
            "grant_type": "client_credential",
            "appid": "wx-test",
            "secret": "test-secret",
            "force_refresh": False,
        })

    def test_production_python_has_no_legacy_token_endpoint(self):
        server_dir = Path(__file__).resolve().parents[1] / "server"
        legacy = "/cgi-bin/" + "token"
        offenders = [
            str(path.relative_to(server_dir))
            for path in server_dir.rglob("*.py")
            if legacy in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])

    def test_create_order_returns_only_client_payment_fields_without_binding_openid(self):
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
            self.assertIsNone(c.execute(
                "SELECT wx_openid FROM users WHERE username='buyer'"
            ).fetchone()[0])
        finally:
            c.close()

    def test_nonmember_can_create_membership_virtual_order(self):
        os.environ["HQ_MEMBERSHIP_ENFORCEMENT_ENABLED"] = "1"
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            result, err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )

        self.assertIsNone(err)
        self.assertEqual(result["order"]["order_type"], "membership_experience")
        self.assertEqual(result["order"]["amount_fen"], 49900)
        self.assertEqual(result["order"]["points"], 1000)
        sign_data = json.loads(result["payment"]["signData"])
        self.assertEqual(sign_data["productId"], "hq_member_exp_1y")
        self.assertEqual(sign_data["goodsPrice"], 49900)

    def test_active_member_cannot_create_membership_virtual_order(self):
        now = 1800000000
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute(
                """UPDATE users SET membership_tier='experience',
                          membership_started_at=?,membership_expires_at=?
                     WHERE username='buyer'""",
                (now, now + self.auth.MEMBERSHIP_YEAR_SECONDS),
            )
            c.commit()
        finally:
            c.close()

        with patch("server.auth_server.time.time", return_value=now), patch.object(
            self.auth.wechat_vpay, "code_to_session"
        ) as code_to_session:
            result, err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )

        self.assertIsNone(result)
        self.assertEqual(err, "membership_already_owned")
        code_to_session.assert_not_called()

    def test_active_experience_member_can_credit_repeated_renewal_orders_without_points(self):
        now = 1800000000
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute(
                "UPDATE users SET membership_tier='experience',membership_started_at=?,membership_expires_at=? WHERE username='buyer'",
                (now - 10, now + 100),
            )
            c.commit()
        finally:
            c.close()
        expiry = now + 100
        for index in (1, 2):
            with patch("server.auth_server.time.time", return_value=now + index), patch.object(
                self.auth.wechat_vpay, "code_to_session",
                return_value={"openid": "openid-buyer", "session_key": "session-key"},
            ):
                result, err = self.auth.create_virtual_pay_order(
                    "buyer", "membership_experience_renewal", "wx-code",
                )
            self.assertIsNone(err)
            self.assertEqual(result["order"]["points"], 0)
            order_id = result["order"]["order_id"]
            with patch.object(self.auth.wechat_vpay, "notify_provide_goods", return_value={}), patch(
                "server.auth_server.time.time", return_value=now + index,
            ):
                order, err = self.auth.confirm_virtual_pay_order("buyer", order_id, verified_wx_order={
                    "order_id": order_id, "status": 2, "order_fee": 49900,
                    "paid_time": now + index, "wx_order_id": "wx-renew-%s" % index,
                    "wxpay_order_id": "wxpay-renew-%s" % index,
                })
            self.assertIsNone(err)
            self.assertEqual(order["status"], "credited")
            expiry += self.auth.MEMBERSHIP_YEAR_SECONDS
            c = sqlite3.connect(self.auth.DB)
            try:
                self.assertEqual(c.execute(
                    "SELECT membership_expires_at FROM users WHERE username='buyer'"
                ).fetchone()[0], expiry)
            finally:
                c.close()
        self.assertEqual(self.auth.get_points_row("buyer")["points"], 5)

    def test_user_cannot_create_a_second_open_membership_order(self):
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            first, first_err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )

        self.assertIsNone(first_err)
        self.assertEqual(first["order"]["status"], "created")

        with patch.object(
            self.auth.wechat_vpay,
            "query_order",
            return_value={"order": {"order_id": first["order"]["order_id"], "status": 1}},
        ) as query_order, patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ) as code_to_session:
            second, second_err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )

        self.assertIsNone(second)
        self.assertEqual(second_err, "membership_order_exists")
        query_order.assert_called_once()
        code_to_session.assert_not_called()
        c = sqlite3.connect(self.auth.DB)
        try:
            self.assertEqual(
                c.execute(
                    """SELECT COUNT(*) FROM virtual_pay_orders
                         WHERE username='buyer' AND order_type='membership_experience'"""
                ).fetchone()[0],
                1,
            )
        finally:
            c.close()

    def test_wechat_closed_membership_order_is_retired_before_retry(self):
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            first, first_err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )
        self.assertIsNone(first_err)
        first_order_id = first["order"]["order_id"]

        with patch.object(
            self.auth.wechat_vpay,
            "query_order",
            return_value={"order": {"order_id": first_order_id, "status": 6}},
        ), patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            second, second_err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )

        self.assertIsNone(second_err)
        self.assertNotEqual(second["order"]["order_id"], first_order_id)
        c = sqlite3.connect(self.auth.DB)
        try:
            first_status, first_error = c.execute(
                "SELECT status,last_error FROM virtual_pay_orders WHERE order_id=?",
                (first_order_id,),
            ).fetchone()
            self.assertEqual(first_status, "failed")
            self.assertIn("已关闭", first_error)
            self.assertEqual(
                c.execute(
                    """SELECT COUNT(*) FROM virtual_pay_orders
                         WHERE username='buyer' AND order_type='membership_experience'"""
                ).fetchone()[0],
                2,
            )
        finally:
            c.close()

    def test_paid_membership_order_is_fulfilled_when_user_retries_purchase(self):
        now = 1800000000
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ), patch("server.auth_server.time.time", return_value=now):
            first, first_err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )
        self.assertIsNone(first_err)
        order_id = first["order"]["order_id"]
        wx_order = {
            "order_id": order_id,
            "status": 2,
            "order_fee": 49900,
            "paid_time": now,
            "wx_order_id": "wx-recovered-membership",
            "wxpay_order_id": "wxpay-recovered-membership",
        }

        with patch.object(
            self.auth.wechat_vpay, "query_order", return_value={"order": wx_order}
        ) as query_order, patch.object(
            self.auth.wechat_vpay, "notify_provide_goods", return_value={}
        ), patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ) as code_to_session, patch("server.auth_server.time.time", return_value=now):
            second, second_err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )

        self.assertIsNone(second)
        self.assertEqual(second_err, "membership_already_active")
        query_order.assert_called_once()
        code_to_session.assert_not_called()
        self.assertEqual(self.auth.get_points_row("buyer")["points"], 1005)
        c = sqlite3.connect(self.auth.DB)
        try:
            status = c.execute(
                "SELECT status FROM virtual_pay_orders WHERE order_id=?", (order_id,)
            ).fetchone()[0]
            tier = c.execute(
                "SELECT membership_tier FROM users WHERE username='buyer'"
            ).fetchone()[0]
            self.assertEqual(status, "credited")
            self.assertEqual(tier, "experience")
            self.assertEqual(
                c.execute(
                    """SELECT COUNT(*) FROM virtual_pay_orders
                         WHERE username='buyer' AND order_type='membership_experience'"""
                ).fetchone()[0],
                1,
            )
        finally:
            c.close()

    def test_incomplete_wechat_query_never_closes_membership_order(self):
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            first, first_err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )
        self.assertIsNone(first_err)

        with patch.object(
            self.auth.wechat_vpay,
            "query_order",
            side_effect=({"order": {}}, {"order": {"status": "unknown"}}),
        ) as query_order, patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ) as code_to_session:
            attempts = [
                self.auth.create_virtual_pay_order(
                    "buyer", "membership_experience", "wx-code"
                )
                for _ in range(2)
            ]

        self.assertEqual(
            attempts,
            [(None, "membership_order_exists"), (None, "membership_order_exists")],
        )
        self.assertEqual(query_order.call_count, 2)
        code_to_session.assert_not_called()
        c = sqlite3.connect(self.auth.DB)
        try:
            self.assertEqual(
                c.execute(
                    "SELECT status FROM virtual_pay_orders WHERE order_id=?",
                    (first["order"]["order_id"],),
                ).fetchone()[0],
                "created",
            )
        finally:
            c.close()

    def test_expired_paid_membership_order_cannot_be_renewed(self):
        now = 1800000000
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute(
                """INSERT INTO virtual_pay_orders(
                     order_id,username,openid,package_id,product_id,amount_fen,points,env,status,
                     created_at,list_amount_fen,pricing_tier,discount_bps,order_type
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "HQ-OLD-MEMBERSHIP", "buyer", "openid-buyer", "membership_experience",
                    "hq_member_exp_1y", 49900, 1000, 0, "credited", now - 100,
                    49900, "", 10000, "membership_experience",
                ),
            )
            c.execute(
                """UPDATE users SET wx_openid=?,membership_tier='experience',
                          membership_started_at=?,membership_expires_at=?
                     WHERE username='buyer'""",
                ("openid-buyer", now - 200, now - 1),
            )
            c.commit()
        finally:
            c.close()

        with patch("server.auth_server.time.time", return_value=now), patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ) as code_to_session:
            result, err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )

        self.assertIsNone(result)
        self.assertEqual(err, "membership_already_owned")
        code_to_session.assert_not_called()

    def test_create_order_allows_wechat_payer_used_by_another_account(self):
        self.auth.create_user("owner", "secret123", 0)
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute("UPDATE users SET wx_openid=? WHERE username=?", ("openid-owner", "owner"))
            c.commit()
        finally:
            c.close()

        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-owner", "session_key": "session-key"},
        ):
            result, err = self.auth.create_virtual_pay_order("buyer", "test_pack", "wx-code")

        self.assertIsNone(err)
        c = sqlite3.connect(self.auth.DB)
        try:
            self.assertIsNone(c.execute(
                "SELECT wx_openid FROM users WHERE username='buyer'"
            ).fetchone()[0])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM virtual_pay_orders").fetchone()[0], 1)
            self.assertEqual(c.execute(
                "SELECT openid FROM virtual_pay_orders"
            ).fetchone()[0], "openid-owner")
        finally:
            c.close()

    def test_create_order_allows_account_with_a_different_legacy_wechat_binding(self):
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute(
                "UPDATE users SET wx_openid=? WHERE username=?",
                ("openid-legacy", "buyer"),
            )
            c.commit()
        finally:
            c.close()

        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-current", "session_key": "session-key"},
        ):
            result, err = self.auth.create_virtual_pay_order("buyer", "test_pack", "wx-code")

        self.assertIsNone(err)
        c = sqlite3.connect(self.auth.DB)
        try:
            self.assertEqual(c.execute(
                "SELECT openid FROM virtual_pay_orders"
            ).fetchone()[0], "openid-current")
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

    def test_paid_membership_order_grants_all_benefits_exactly_once(self):
        now = 1800000000
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ), patch("server.auth_server.time.time", return_value=now):
            result, err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )
        self.assertIsNone(err)
        order_id = result["order"]["order_id"]
        wx_result = {
            "order": {
                "order_id": order_id,
                "status": 2,
                "order_fee": 49900,
                "paid_time": now,
                "wx_order_id": "wx-membership-1",
                "wxpay_order_id": "wxpay-membership-1",
            },
        }

        with patch.object(self.auth.wechat_vpay, "query_order", return_value=wx_result) as query, \
             patch.object(self.auth.wechat_vpay, "notify_provide_goods", return_value={}), \
             patch("server.auth_server.time.time", return_value=now):
            first, first_err = self.auth.confirm_virtual_pay_order("buyer", order_id)
            second, second_err = self.auth.confirm_virtual_pay_order("buyer", order_id)

        self.assertIsNone(first_err)
        self.assertIsNone(second_err)
        self.assertEqual(first["status"], "credited")
        self.assertEqual(second["status"], "credited")
        self.assertEqual(query.call_count, 1)
        user = self.auth.get_points_row("buyer")
        self.assertEqual(user["points"], 1005)

        c = sqlite3.connect(self.auth.DB)
        try:
            c.row_factory = sqlite3.Row
            membership = c.execute(
                "SELECT membership_tier,membership_expires_at FROM users WHERE username='buyer'"
            ).fetchone()
            self.assertEqual(membership["membership_tier"], "experience")
            self.assertEqual(
                membership["membership_expires_at"], now + self.auth.MEMBERSHIP_YEAR_SECONDS
            )
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM membership_voice_slot_entitlements WHERE username='buyer'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM membership_audit WHERE username='buyer'"
                ).fetchone()[0],
                1,
            )
        finally:
            c.close()

    def test_background_reconcile_routes_created_membership_order_through_confirm(self):
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            result, err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )
        self.assertIsNone(err)
        order_id = result["order"]["order_id"]
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute(
                "UPDATE virtual_pay_orders SET created_at=created_at-? WHERE order_id=?",
                (self.auth.VIRTUAL_PAY_RECONCILE_MIN_AGE_SECONDS, order_id),
            )
            c.commit()
        finally:
            c.close()

        with patch.object(
            self.auth,
            "confirm_virtual_pay_order",
            return_value=({"status": "credited"}, None),
        ) as confirm:
            stats = self.auth.reconcile_created_virtual_pay_orders()

        confirm.assert_called_once_with("buyer", order_id)
        self.assertEqual(stats, {
            "checked": 1,
            "credited": 1,
            "terminal": 0,
            "pending": 0,
            "errors": 0,
        })

    def test_membership_refund_reverses_first_purchase_points_and_membership(self):
        now = 1800000000
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ), patch("server.auth_server.time.time", return_value=now):
            result, err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )
        self.assertIsNone(err)
        order_id = result["order"]["order_id"]
        with patch.object(self.auth.wechat_vpay, "query_order", return_value={
            "order": {
                "order_id": order_id,
                "status": 2,
                "order_fee": 49900,
                "paid_time": now,
                "wx_order_id": "wx-membership-refund",
                "wxpay_order_id": "wxpay-membership-refund",
            },
        }), patch.object(self.auth.wechat_vpay, "notify_provide_goods", return_value={}), \
             patch("server.auth_server.time.time", return_value=now):
            _, confirm_err = self.auth.confirm_virtual_pay_order("buyer", order_id)
        self.assertIsNone(confirm_err)

        response = self.auth.process_virtual_pay_message({
            "Event": "xpay_refund_notify",
            "pay_order_id": "wx-membership-refund",
        })

        self.assertEqual(response["errcode"], 0)
        self.assertEqual(response["order"]["status"], "refunded")
        self.assertEqual(self.auth.get_points_row("buyer")["points"], 5)

        with patch.object(self.auth.wechat_vpay, "query_order") as query_order:
            confirmed, confirm_err = self.auth.confirm_virtual_pay_order("buyer", order_id)
            delivery_response = self.auth.process_virtual_pay_message({
                "Event": "xpay_goods_deliver_notify",
                "pay_order_id": "wx-membership-refund",
            })

        self.assertIsNone(confirm_err)
        self.assertEqual(confirmed["status"], "refunded")
        self.assertEqual(delivery_response["errcode"], 0)
        query_order.assert_not_called()
        self.assertEqual(self.auth.get_points_row("buyer")["points"], 5)
        c = sqlite3.connect(self.auth.DB)
        try:
            tier = c.execute(
                "SELECT membership_tier FROM users WHERE username='buyer'"
            ).fetchone()[0]
            self.assertEqual(tier, "")
        finally:
            c.close()

    def test_failed_membership_order_never_recovers_from_late_callbacks(self):
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            result, err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )
        self.assertIsNone(err)
        order_id = result["order"]["order_id"]
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute(
                "UPDATE virtual_pay_orders SET status='failed',last_error='订单已关闭' WHERE order_id=?",
                (order_id,),
            )
            c.commit()
        finally:
            c.close()

        with patch.object(self.auth.wechat_vpay, "query_order") as query_order:
            confirmed, confirm_err = self.auth.confirm_virtual_pay_order("buyer", order_id)
            delivered = self.auth.process_virtual_pay_message({
                "Event": "xpay_goods_deliver_notify",
                "order_id": order_id,
            })

        self.assertIsNone(confirm_err)
        self.assertEqual(confirmed["status"], "failed")
        self.assertEqual(delivered["errcode"], 0)
        query_order.assert_not_called()
        self.assertEqual(self.auth.get_points_row("buyer")["points"], 5)

    def test_membership_refund_failure_blocks_another_purchase(self):
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            result, err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )
        self.assertIsNone(err)
        order_id = result["order"]["order_id"]

        with patch.object(self.auth.wechat_vpay, "query_order", return_value={
            "order": {"order_id": order_id, "status": 7, "order_fee": 49900}
        }):
            confirmed, confirm_err = self.auth.confirm_virtual_pay_order("buyer", order_id)

        self.assertEqual(confirm_err, "not_paid")
        self.assertEqual(confirmed["status"], "refund_review")
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ) as code_to_session:
            second, second_err = self.auth.create_virtual_pay_order(
                "buyer", "membership_experience", "wx-code"
            )

        self.assertIsNone(second)
        self.assertEqual(second_err, "membership_order_exists")
        code_to_session.assert_not_called()
        self.assertEqual(self.auth.get_points_row("buyer")["points"], 5)

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
            "price_fen_per_list_yuan": 100,
            "membership_tier": "",
            "discount_bps": 10000,
        })

    def test_three_membership_tiers_price_three_virtual_packages_on_server(self):
        products = [
            {
                "id": "points_1000",
                "product_id": "hq_points_1000",
                "title": "1000 points",
                "price_fen": 10000,
                "points": 1000,
            },
            {
                "id": "points_2000",
                "product_id": "hq_points_2000",
                "title": "2000 points",
                "price_fen": 20000,
                "points": 2000,
            },
            {
                "id": "points_5000",
                "product_id": "hq_points_5000",
                "title": "5000 points",
                "price_fen": 50000,
                "points": 5000,
            },
        ]
        os.environ["WX_VIRTUAL_PAY_PRODUCTS_JSON"] = json.dumps(products)
        expected = {
            "experience": [10000, 20000, 50000],
            "partner": [7500, 15000, 37500],
            "initiator": [5500, 11000, 27500],
        }
        now = 1800000000
        for tier, prices in expected.items():
            username = "buyer_" + tier
            self.auth.create_user(username, "secret123", 0)
            c = sqlite3.connect(self.auth.DB)
            try:
                c.execute(
                    """UPDATE users
                          SET membership_tier=?,membership_started_at=?,membership_expires_at=?
                        WHERE username=?""",
                    (tier, now, now + self.auth.MEMBERSHIP_YEAR_SECONDS, username),
                )
                c.commit()
            finally:
                c.close()

            quotes = self.auth.public_virtual_pay_packages(tier)
            self.assertEqual([item["price_fen"] for item in quotes], prices)
            for product, expected_fen in zip(products, prices):
                with self.subTest(tier=tier, package=product["id"]):
                    with patch.object(
                        self.auth.wechat_vpay,
                        "code_to_session",
                        return_value={
                            "openid": "openid-" + tier,
                            "session_key": "session-key",
                        },
                    ):
                        result, err = self.auth.create_virtual_pay_order(
                            username, product["id"], "wx-code",
                        )
                    self.assertIsNone(err)
                    self.assertEqual(result["order"]["amount_fen"], expected_fen)
                    self.assertEqual(result["order"]["points"], product["points"])
                    self.assertEqual(result["order"]["pricing_tier"], tier)
                    sign_data = json.loads(result["payment"]["signData"])
                    self.assertEqual(sign_data["goodsPrice"], product["price_fen"])
                    if expected_fen == product["price_fen"]:
                        self.assertNotIn("activitySellingPrice", sign_data)
                    else:
                        self.assertEqual(sign_data["activitySellingPrice"], expected_fen)

    def test_secure_message_push_round_trip_and_signature_check(self):
        message = {
            "Event": "xpay_subscribe_ios_refund_query_notify",
            "pay_order_id": "wx-order-1",
        }
        ciphertext = self.auth.wechat_vpay.encrypt_message(json.dumps(message))
        query = {"timestamp": ["1784200000"], "nonce": ["nonce-1"]}
        query["msg_signature"] = [self.auth.wechat_vpay.message_signature(
            "push-token", "1784200000", "nonce-1", ciphertext
        )]
        decoded, encrypted = self.auth.wechat_vpay.decode_message_push(
            query, {"Encrypt": ciphertext}
        )
        self.assertTrue(encrypted)
        self.assertEqual(decoded, message)

        encoded = self.auth.wechat_vpay.encode_message_push({"result_code": 0}, True)
        self.assertEqual(json.loads(self.auth.wechat_vpay.decrypt_message(encoded["Encrypt"])), {
            "result_code": 0,
        })
        self.assertEqual(
            encoded["MsgSignature"],
            self.auth.wechat_vpay.message_signature(
                "push-token", encoded["TimeStamp"], encoded["Nonce"], encoded["Encrypt"]
            ),
        )

        bad_query = dict(query)
        bad_query["msg_signature"] = ["bad"]
        with self.assertRaises(self.auth.wechat_vpay.MessagePushError):
            self.auth.wechat_vpay.decode_message_push(bad_query, {"Encrypt": ciphertext})

    def _create_and_credit_order(self):
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            result, err = self.auth.create_virtual_pay_order("buyer", "test_pack", "wx-code")
        self.assertIsNone(err)
        order_id = result["order"]["order_id"]
        with patch.object(self.auth.wechat_vpay, "query_order", return_value={
            "order": {
                "order_id": order_id,
                "status": 2,
                "order_fee": 1,
                "paid_time": 1784200000,
                "wx_order_id": "wx-order-refund",
                "wxpay_order_id": "wxpay-refund",
            },
        }), patch.object(self.auth.wechat_vpay, "notify_provide_goods", return_value={}):
            _, confirm_err = self.auth.confirm_virtual_pay_order("buyer", order_id)
        self.assertIsNone(confirm_err)
        return order_id

    def test_ios_refund_query_uses_local_delivery_evidence(self):
        self._create_and_credit_order()
        response = self.auth.process_virtual_pay_message({
            "Event": "xpay_subscribe_ios_refund_query_notify",
            "pay_order_id": "wx-order-refund",
        })
        self.assertEqual(response["result_code"], 1)
        self.assertIn("已发放", response["evidence"])

        missing = self.auth.process_virtual_pay_message({
            "Event": "xpay_subscribe_ios_refund_query_notify",
            "pay_order_id": "missing",
        })
        self.assertEqual(missing["result_code"], 0)

    def test_refund_notification_reverses_points_exactly_once(self):
        order_id = self._create_and_credit_order()
        self.assertEqual(self.auth.get_points_row("buyer")["points"], 15)
        event = {"Event": "xpay_refund_notify", "pay_order_id": "wx-order-refund"}
        first = self.auth.process_virtual_pay_message(event)
        second = self.auth.process_virtual_pay_message(event)

        self.assertEqual(first["errcode"], 0)
        self.assertEqual(second["errcode"], 0)
        self.assertEqual(self.auth.get_points_row("buyer")["points"], 5)
        c = sqlite3.connect(self.auth.DB)
        try:
            self.assertEqual(
                c.execute("SELECT status FROM virtual_pay_orders WHERE order_id=?", (order_id,)).fetchone()[0],
                "refunded",
            )
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM points_audit WHERE reason=?",
                    ("微信虚拟支付退款: " + order_id,),
                ).fetchone()[0],
                1,
            )
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
