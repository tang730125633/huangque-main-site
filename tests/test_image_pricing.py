# -*- coding: utf-8 -*-
"""作图的尺寸表与按引擎分档定价。

一、SIZES（gpt-image-2 的比例 → 像素）
   老表把 9:16 和 3:4 都映射成 1024x1536 —— 那是 2:3，两个按钮出的是同一张图。
   实测(2026-07-10)：gpt-image-2 唯一约束是「宽高都必须是 16 的倍数」
   （传 123x456 → "Width and height must both be divisible by 16"）。
   新尺寸已真实出图、读 PNG 头核对，/v1/images/generations 与 /v1/images/edits 都精确回显。

二、IMAGE_BASE_COST（1 点 = 0.1 元）
   gpt-image-2 按官方 $30.00/M image output token 实测（读 API 返回的 usage）：
       medium 1024x1024 = 1756 tok = $0.0527 ≈ ¥0.37
       medium 1152x2048 = 1413 tok = $0.0424 ≈ ¥0.30
       medium 1200x1600 = 1694 tok = $0.0508 ≈ ¥0.36
       high 恒为 medium 的 4.00 倍 → ¥1.20 ~ ¥1.50
   取最贵档定价避免倒挂：标准 4 点、高清 15 点（原来高清只收 12 点，1:1 与 3:4 是亏的）。
"""
import importlib
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

core = importlib.import_module("content_domains.core")
points = importlib.import_module("content_domains.points")
BANANA = (ROOT / "site" / "workbench" / "banana.html").read_text(encoding="utf-8")
IMGGEN_SRC = (ROOT / "server" / "imggen_api.py").read_text(encoding="utf-8")
image_domain = importlib.import_module("content_domains.image")

FRONTEND_RATIOS = ["1:1", "9:16", "16:9", "3:4"]


class ChannelShutdownTests(unittest.TestCase):
    def test_zelong2_is_rejected_before_points_are_deducted(self):
        with self.assertRaisesRegex(ValueError, "泽龙2生图渠道维护中"):
            image_domain.validate_image_payload({"provider": "zelong2", "prompt": "demo"})
        with self.assertRaisesRegex(ValueError, "泽龙2生图渠道维护中"):
            image_domain.gen_image({"provider": "zelong2", "prompt": "demo"})

    def test_zelong2_card_is_hidden(self):
        self.assertRegex(BANANA, r'data-engine="zelong2"[^>]*aria-hidden="true"[^>]*display:none')
        self.assertIn("location.hostname==='zelong.huangquechuanmei.com'", BANANA)


def _wh(size):
    w, h = (int(x) for x in size.split("x"))
    return w, h


class SizeTableTests(unittest.TestCase):
    def test_every_ratio_has_a_size(self):
        for r in FRONTEND_RATIOS:
            self.assertIn(r, core.SIZES)

    def test_dimensions_divisible_by_16(self):
        """gpt-image-2 的硬约束，实测报错原文如此。"""
        for r, s in core.SIZES.items():
            w, h = _wh(s)
            self.assertEqual((w % 16, h % 16), (0, 0), "%s → %s" % (r, s))

    def test_size_actually_matches_its_ratio(self):
        """老 bug：9:16 拿到的是 1024x1536（2:3）。"""
        for r, s in core.SIZES.items():
            rw, rh = (int(x) for x in r.split(":"))
            w, h = _wh(s)
            self.assertAlmostEqual(w / h, rw / rh, delta=0.01, msg="%s → %s" % (r, s))

    def test_portrait_ratios_are_no_longer_identical(self):
        """回归守卫：9:16 与 3:4 曾映射到同一个尺寸。"""
        self.assertNotEqual(core.SIZES["9:16"], core.SIZES["3:4"])

    def test_exact_sizes_verified_against_live_api(self):
        """这四个尺寸都真实出过图，PNG 头回显一致；edits 端点同样接受。"""
        self.assertEqual(core.SIZES["1:1"], "1024x1024")
        self.assertEqual(core.SIZES["9:16"], "1152x2048")
        self.assertEqual(core.SIZES["16:9"], "2048x1152")
        self.assertEqual(core.SIZES["3:4"], "1200x1600")


class GptPricingTests(unittest.TestCase):
    """按官方 token 单价折算，1 点 = 0.1 元。"""

    USD_PER_IMAGE_OUT_TOKEN = 30.0 / 1e6      # 官方定价页确认
    RATE = 7.1                                 # USD → CNY

    def _points(self, tokens):
        return tokens * self.USD_PER_IMAGE_OUT_TOKEN * self.RATE / 0.1

    def test_standard_tier_covers_every_ratio(self):
        """标准 4 点必须 ≥ 各比例的实测成本（最贵的是 1:1 的 1756 tok）。"""
        for tok in (1756, 1413, 1694):
            self.assertLessEqual(self._points(tok), points.IMAGE_BASE_COST["openai"]["std"] + 0.01)

    def test_hd_tier_covers_every_ratio(self):
        """高清 15 点必须 ≥ 各比例实测成本（high = medium × 4）。原来收 12 点，1:1 与 3:4 倒挂。"""
        for tok in (1756 * 4, 1413 * 4, 1694 * 4):
            self.assertLessEqual(self._points(tok), points.IMAGE_BASE_COST["openai"]["hd"] + 0.05)

    def test_old_hd_price_was_underwater(self):
        """守住修复的理由：12 点覆盖不了 1:1 高清（7024 tok ≈ 14.96 点）。"""
        self.assertGreater(self._points(7024), 12)


class CostOfTests(unittest.TestCase):
    def test_gpt_uses_new_tiers(self):
        self.assertEqual(points.cost_of("image", {"provider": "openai", "quality": "std", "count": 1}), 20)
        self.assertEqual(points.cost_of("image", {"provider": "openai", "quality": "hd", "count": 1}), 35)

    def test_missing_provider_defaults_to_openai(self):
        """gen_image 的 provider 缺省就是 openai，扣点必须跟着走同一档。"""
        self.assertEqual(points.cost_of("image", {"quality": "hd", "count": 1}), 35)

    def test_other_engines_unchanged(self):
        for p in ("seedream", "zelong", "zelong2"):
            self.assertEqual(points.cost_of("image", {"provider": p, "quality": "std", "count": 1}), 8, p)
            self.assertEqual(points.cost_of("image", {"provider": p, "quality": "hd", "count": 1}), 12, p)
        self.assertEqual(points.cost_of("image", {"provider": "xiaole", "quality": "std", "count": 1}), 12)
        self.assertEqual(points.cost_of("image", {"provider": "xiaole", "quality": "hd", "count": 1}), 16)

    def test_unknown_provider_falls_back_to_default(self):
        self.assertEqual(points.cost_of("image", {"provider": "brand-new", "quality": "hd", "count": 1}), 12)

    def test_count_multiplies_and_caps_match_generator(self):
        self.assertEqual(points.cost_of("image", {"provider": "openai", "quality": "hd", "count": 4}), 35 * 4)
        self.assertEqual(points.cost_of("image", {"provider": "openai", "quality": "hd", "count": 9}), 35 * 4)
        for p in ("seedream", "zelong", "zelong2"):
            self.assertEqual(points.cost_of("image", {"provider": p, "quality": "hd", "count": 9}), 12 * 2, p)
        self.assertEqual(points.cost_of("image", {"provider": "xiaole", "quality": "hd", "count": 9}), 16 * 2)

    def test_mask_forces_single_image(self):
        self.assertEqual(points.cost_of("image", {"provider": "openai", "quality": "hd", "count": 4, "mask": "x"}), 35)


class FrontendBackendSyncTests(unittest.TestCase):
    """前端 COSTBASE 与后端 IMAGE_BASE_COST 必须逐字一致，否则按钮显示的点数与实际扣点不符。"""

    def _frontend_costbase(self):
        raw = re.search(r"var COSTBASE=\{(.+?)\};", BANANA).group(1)
        out = {}
        for eng, std, hd in re.findall(r"(\w+):\{std:(\d+),\s*hd:(\d+)\}", raw):
            out[eng] = {"std": int(std), "hd": int(hd)}
        return out

    # 前端引擎卡叫 gpt，后端 provider 叫 openai —— 同一个引擎两个名字，映射写死在这里。
    # seedream 不在这里：它按型号分价，见 test_seedream_variants_agree。
    BACKEND_TO_FRONTEND = {"openai": "gpt", "xiaole": "xiaole", "zelong2": "zelong2"}

    def test_shared_engines_agree(self):
        fe = self._frontend_costbase()
        for eng, be in points.IMAGE_BASE_COST.items():
            if eng == "zelong":
                continue          # 泽龙1 不在作图页引擎卡上
            key = self.BACKEND_TO_FRONTEND[eng]
            self.assertIn(key, fe, key)
            self.assertEqual(fe[key], be, "%s(前端 %s)" % (eng, key))

    def test_seedream_variants_agree(self):
        """Seedream 按型号分价（5.0标准/5.0pro）：前端 seedream_std/seedream_pro 必须与后端
        points.SEEDREAM_VARIANT_COST 逐字一致，否则切型号后显示与实扣对不上。"""
        fe = self._frontend_costbase()
        for variant in ("std", "pro"):
            key = "seedream_" + variant
            self.assertIn(key, fe, key)
            self.assertEqual(fe[key], points.SEEDREAM_VARIANT_COST[variant], key)
        self.assertEqual(points.SEEDREAM_VARIANT_COST["std"], {"std": 8, "hd": 12})
        self.assertEqual(points.SEEDREAM_VARIANT_COST["pro"], {"std": 15, "hd": 20})

    def test_gpt_price_updated_on_both_sides(self):
        self.assertEqual(self._frontend_costbase()["gpt"], {"std": 20, "hd": 35})
        self.assertEqual(points.IMAGE_BASE_COST["openai"], {"std": 20, "hd": 35})

    def _imggen_basecost(self):
        # Nano Banana(nb2/pro) 的实扣在 imggen_api.py（独立服务），不在 points.py。
        raw = re.search(r"BASE_COST\s*=\s*\{(.+?)\}\n", IMGGEN_SRC).group(1)
        out = {}
        for eng, std, hd in re.findall(r'"(\w+)":\s*\{"std":\s*(\d+),\s*"hd":\s*(\d+)\}', raw):
            out[eng] = {"std": int(std), "hd": int(hd)}
        return out

    def test_banana_nb2_pro_agree_front_and_back(self):
        """⚠ nb2/pro 的前端 COSTBASE 必须与 imggen_api.py 的 BASE_COST 逐字一致，
        否则作图卡显示的点数与实际扣点对不上（这条链路 points.py 那侧的一致性测试盖不到）。"""
        fe, be = self._frontend_costbase(), self._imggen_basecost()
        for eng in ("nb2", "pro"):
            self.assertIn(eng, be, "imggen_api.BASE_COST 缺 %s" % eng)
            self.assertIn(eng, fe, "前端 COSTBASE 缺 %s" % eng)
            self.assertEqual(fe[eng], be[eng], "%s 前后端点数不一致" % eng)

    def test_banana_prices_are_the_new_values(self):
        be = self._imggen_basecost()
        self.assertEqual(be["nb2"], {"std": 18, "hd": 35})
        self.assertEqual(be["pro"], {"std": 35, "hd": 44})


if __name__ == "__main__":
    unittest.main()
