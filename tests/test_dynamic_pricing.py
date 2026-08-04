# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from content_domains import points, pricing, video


class DynamicPricingTests(unittest.TestCase):
    def test_one_override_updates_billing_and_public_price(self):
        old_path = pricing.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pricing.DB_PATH = Path(tmp) / "pricing.db"
                pricing.invalidate_cache()
                self.assertEqual(points.cost_of("image", {"provider": "openai", "quality": "standard"}), 20)
                pricing.set_price("image.openai.std", 27, "admin")
                self.assertEqual(points.cost_of("image", {"provider": "openai", "quality": "standard"}), 27)
                talking = {"mode": "text", "text": "一" * 121}
                self.assertEqual(points.cost_of("video", talking), 60)
                pricing.set_price("video.talking.block", 50, "admin")
                self.assertEqual(video.talking_actual_cost({"duration": 30.1}, talking["_talking_block_points"]), 60)
                public = {x["key"]: x for x in pricing.public_catalog()["items"]}
                self.assertEqual(public["image.openai.std"]["points"], 27)
                self.assertNotIn("updated_by", public["image.openai.std"])
                with self.assertRaises(ValueError):
                    pricing.set_price("image.openai.std", 0, "admin")
        finally:
            pricing.DB_PATH = old_path
            pricing.invalidate_cache()


if __name__ == "__main__":
    unittest.main()
