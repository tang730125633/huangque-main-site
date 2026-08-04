# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from content_domains import core, points, pricing, video
from server import auth_server, wechat_virtual_pay


class DynamicPricingTests(unittest.TestCase):
    def test_ship_deploys_shared_pricing_to_auth_and_content(self):
        ship = (ROOT / "ship").read_text(encoding="utf-8")
        self.assertIn("server/content_domains/pricing.py)", ship)
        self.assertIn('push_file "$f" /home/ubuntu/content-api/content_domains/', ship)

    def test_commerce_prices_drive_membership_quotes_and_virtual_goods(self):
        values = {
            "membership.experience.price_yuan": 399,
            "membership.experience.bonus_points": 900,
        }
        with patch.object(auth_server.pricing, "get_price", side_effect=lambda key: values[key]), \
                patch.object(wechat_virtual_pay.pricing, "get_price", side_effect=lambda key: values[key]):
            self.assertEqual(auth_server.purchase_quote(399, "membership_experience"), (399, 900, "membership_experience"))
            products = {item["id"]: item for item in wechat_virtual_pay.products()}
            self.assertEqual(products["membership_experience"]["price_fen"], 39900)
            self.assertEqual(products["membership_experience"]["points"], 900)

    def test_current_core_accepts_legacy_short_drama_runtime(self):
        calls = []

        class LegacyShortDrama:
            @staticmethod
            def dispatch_http(handler, method, db_factory, verify_token,
                              cost_of=None, mutation_lock=None,
                              canvas_access_resolver=None, voice_validator=None,
                              points_getter=None, generation_dependencies=None):
                calls.append(generation_dependencies)
                return True

        audio_domain, points_domain = object(), object()
        with patch.object(core, "_short_drama_domain", return_value=LegacyShortDrama), \
                patch.object(core, "_domains", return_value=(audio_domain, points_domain, object())), \
                patch.object(core, "_lipsync_worker_domain", side_effect=ImportError):
            self.assertTrue(core._dispatch_short_drama(
                object(), "POST", object(), object(), None,
                audio_asset_lookup=object(), lipsync_wake=object(),
            ))
            self.assertIsNone(core._lipsync_worker_attr("wake"))
        self.assertEqual((audio_domain, points_domain), calls[0][:2])

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
                self.assertEqual(public["invite.card_trial_reward"]["points"], 100)
                self.assertNotIn("updated_by", public["image.openai.std"])
                with self.assertRaises(ValueError):
                    pricing.set_price("image.openai.std", 0, "admin")
        finally:
            pricing.DB_PATH = old_path
            pricing.invalidate_cache()


if __name__ == "__main__":
    unittest.main()
