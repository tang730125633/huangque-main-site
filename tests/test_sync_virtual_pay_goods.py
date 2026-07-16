import unittest
from unittest.mock import call, patch

from server import sync_virtual_pay_goods as sync_goods


class SyncVirtualPayGoodsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
