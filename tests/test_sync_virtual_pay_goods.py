import unittest
from unittest.mock import call, patch

from server import sync_virtual_pay_goods as sync_goods


class SyncVirtualPayGoodsTests(unittest.TestCase):
    def test_goods_name_removes_characters_rejected_by_wechat(self):
        self.assertEqual(sync_goods.goods_name("1000 点"), "1000点")
        self.assertEqual(sync_goods.goods_name("黄雀点数 / Pro"), "黄雀点数Pro")
        self.assertEqual(sync_goods.goods_name("黄雀·点数_5000"), "黄雀·点数_5000")

    def test_upload_and_publish_are_submitted_one_item_at_a_time(self):
        products = [
            {"product_id": "hq_1000", "title": "1000 点", "price_fen": 9900},
            {"product_id": "hq_2000", "title": "2000 点", "price_fen": 19900},
            {"product_id": "hq_5000", "title": "5000 点", "price_fen": 49900},
        ]
        with patch.object(sync_goods.vpay, "is_configured", return_value=True), \
             patch.object(sync_goods.vpay, "products", return_value=products), \
             patch.object(sync_goods.vpay, "pay_env", return_value=0), \
             patch.object(sync_goods.vpay, "_xpay") as xpay, \
             patch.object(sync_goods, "wait_for") as wait_for:
            sync_goods.main()

        upload_calls = xpay.call_args_list[:3]
        publish_calls = xpay.call_args_list[3:]
        self.assertEqual(len(upload_calls), 3)
        self.assertEqual(len(publish_calls), 3)
        self.assertTrue(all(len(entry.args[1]["upload_item"]) == 1 for entry in upload_calls))
        self.assertTrue(all(len(entry.args[1]["publish_item"]) == 1 for entry in publish_calls))
        self.assertEqual(
            [entry.args[1]["upload_item"][0]["name"] for entry in upload_calls],
            ["1000点", "2000点", "5000点"],
        )
        self.assertEqual(
            wait_for.call_args_list,
            [
                call("/xpay/query_upload_goods", "upload_item"),
                call("/xpay/query_upload_goods", "upload_item"),
                call("/xpay/query_upload_goods", "upload_item"),
                call("/xpay/query_publish_goods", "publish_item"),
                call("/xpay/query_publish_goods", "publish_item"),
                call("/xpay/query_publish_goods", "publish_item"),
            ],
        )

    def test_rate_limit_is_retried_before_querying_status(self):
        item = {"id": "hq_custom", "name": "自定义点数", "price": 100}
        rate_limit = sync_goods.vpay.VirtualPayError(
            "频率限制",
            "xpay_failed",
            {"errcode": 45009},
        )
        with patch.object(sync_goods.vpay, "pay_env", return_value=0), \
             patch.object(sync_goods.vpay, "_xpay", side_effect=[rate_limit, {}]) as xpay, \
             patch.object(sync_goods, "wait_for") as wait_for, \
             patch.object(sync_goods.time, "sleep") as sleep:
            sync_goods.submit_one_by_one("/start", "/query", "upload_item", [item])

        self.assertEqual(xpay.call_count, 2)
        self.assertEqual(xpay.call_args_list[0], call("/start", {"upload_item": [item], "env": 0}))
        self.assertEqual(xpay.call_args_list[1], call("/start", {"upload_item": [item], "env": 0}))
        sleep.assert_called_once_with(10)
        wait_for.assert_called_once_with("/query", "upload_item")


if __name__ == "__main__":
    unittest.main()
