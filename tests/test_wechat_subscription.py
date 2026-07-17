import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class WechatSubscriptionTests(unittest.TestCase):
    ENV_KEYS = (
        "WX_MP_APPID",
        "WX_MP_APPSECRET",
        "WX_VIRTUAL_PAY_ENV",
        "WX_VIRTUAL_PAY_OFFER_ID",
        "WX_VIRTUAL_PAY_APP_KEY_PROD",
        "WX_VIRTUAL_PAY_PRODUCTS_JSON",
        "WX_SUBSCRIBE_TEMPLATES_JSON",
        "WX_SUBSCRIBE_MINIPROGRAM_STATE",
        "WX_VIRTUAL_PAY_RECONCILE_INTERVAL",
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
            "WX_VIRTUAL_PAY_RECONCILE_INTERVAL": "0",
            "WX_VIRTUAL_PAY_PRODUCTS_JSON": (
                '[{"id":"test_pack","product_id":"hq_test_pack","title":"测试包",'
                '"price_fen":1,"points":10}]'
            ),
            "WX_SUBSCRIBE_MINIPROGRAM_STATE": "formal",
            "WX_SUBSCRIBE_TEMPLATES_JSON": json.dumps({
                "work_complete": {
                    "template_id": "work-template",
                    "fields": {"thing1": "title", "phrase2": "status", "date3": "time"},
                },
                "recharge_credited": {
                    "template_id": "recharge-template",
                    "fields": {"number1": "points", "amount2": "amount", "date3": "time"},
                },
            }, ensure_ascii=False),
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

    def accept(self, event_type="work_complete"):
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            result, err = self.auth.record_subscription_choices(
                "buyer", {event_type: "accept"}, "wx-code"
            )
        self.assertIsNone(err)
        return result

    def test_config_exposes_template_ids_but_not_field_payload(self):
        config = self.auth.subscription_status("buyer")
        self.assertTrue(config["configured"])
        self.assertEqual(
            {item["event_type"] for item in config["events"]},
            {"work_complete", "recharge_credited"},
        )
        self.assertNotIn("fields", config["events"][0])

    def test_accept_adds_one_grant_and_success_consumes_once(self):
        result = self.accept()
        work = next(item for item in result["events"] if item["event_type"] == "work_complete")
        self.assertEqual(work["remaining"], 1)

        with patch.object(self.auth.wechat_subscribe, "send", return_value={"errcode": 0}) as send:
            first = self.auth.notify_work_complete("buyer", 42, "image")
            second = self.auth.notify_work_complete("buyer", 42, "image")

        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(send.call_count, 1)
        work = next(
            item for item in self.auth.subscription_status("buyer")["events"]
            if item["event_type"] == "work_complete"
        )
        self.assertEqual(work["remaining"], 0)

    def test_wechat_no_subscription_clears_stale_local_grants(self):
        self.accept()
        error = self.auth.wechat_subscribe.SubscribeMessageError(
            "user refuse to accept", "43101", {"errcode": 43101}
        )
        with patch.object(self.auth.wechat_subscribe, "send", side_effect=error):
            result = self.auth.notify_work_complete("buyer", 43, "video")
        self.assertEqual(result, {"status": "failed", "code": "43101"})
        work = next(
            item for item in self.auth.subscription_status("buyer")["events"]
            if item["event_type"] == "work_complete"
        )
        self.assertEqual(work["remaining"], 0)
        self.assertEqual(work["last_choice"], "expired")

    def test_background_reconcile_credits_paid_order_without_client_confirm(self):
        self.accept("recharge_credited")
        with patch.object(
            self.auth.wechat_vpay,
            "code_to_session",
            return_value={"openid": "openid-buyer", "session_key": "session-key"},
        ):
            created, err = self.auth.create_virtual_pay_order("buyer", "test_pack", "wx-code")
        self.assertIsNone(err)
        order_id = created["order"]["order_id"]
        with sqlite3.connect(self.auth.DB) as c:
            c.execute("UPDATE virtual_pay_orders SET reconcile_after=0 WHERE order_id=?", (order_id,))
        wx_result = {"order": {
            "order_id": order_id,
            "status": 2,
            "order_fee": 1,
            "paid_time": 1784200000,
        }}
        with patch.object(self.auth.wechat_vpay, "query_order", return_value=wx_result), \
             patch.object(self.auth.wechat_vpay, "notify_provide_goods", return_value={}), \
             patch.object(self.auth.wechat_subscribe, "send", return_value={"errcode": 0}):
            count = self.auth.reconcile_virtual_pay_orders_once()

        self.assertEqual(count, 1)
        self.assertEqual(self.auth.get_points_row("buyer")["points"], 15)
        self.assertEqual(created["order"]["status"], "created")
        with sqlite3.connect(self.auth.DB) as c:
            status = c.execute(
                "SELECT status FROM virtual_pay_orders WHERE order_id=?", (order_id,)
            ).fetchone()[0]
            delivery = c.execute(
                "SELECT status FROM wechat_subscription_deliveries WHERE business_id=?",
                ("order:" + order_id,),
            ).fetchone()[0]
        self.assertEqual(status, "credited")
        self.assertEqual(delivery, "sent")


if __name__ == "__main__":
    unittest.main()
