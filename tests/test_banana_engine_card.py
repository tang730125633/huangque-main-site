# -*- coding: utf-8 -*-
"""作图页：Nano Banana 2 与 Pro 合并为一张引擎卡 + 卡下型号子选择。

守的不变量：
1. 只有一张 `data-engine="banana"` 卡，nb2/pro 不再是独立引擎卡
2. 型号在 `#bananaVariantRow` 里选（data-variant=nb2|pro）
3. **两个型号点数不同**（标10/高14 vs 标18/高26）——切型号必须重算点数，
   这点和 Seedream 不同（Seedream 两档同价，忘了重算也看不出来）
4. 提交时 body 带 model=<型号>，仍打 /api/gen/banana（后端未改）
5. 老深链 ?engine=nb2 / ?engine=pro 必须继续工作 —— script.html 至今硬编码 &engine=nb2
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANANA = (ROOT / "site" / "workbench" / "banana.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "site" / "workbench" / "script.html").read_text(encoding="utf-8")


class EngineCardTests(unittest.TestCase):
    def test_single_banana_card(self):
        self.assertEqual(BANANA.count('data-engine="banana"'), 1)

    def test_old_nb2_and_pro_cards_are_gone(self):
        self.assertNotIn('data-engine="nb2"', BANANA)
        self.assertNotIn('data-engine="pro"', BANANA)

    def test_other_engine_cards_untouched(self):
        for eng in ("gpt", "seedream", "xiaole", "zelong2"):
            self.assertIn('data-engine="%s"' % eng, BANANA)

    def test_cost_span_keyed_by_card_not_variant(self):
        self.assertIn('data-engine-cost="banana"', BANANA)
        self.assertNotIn('data-engine-cost="nb2"', BANANA)
        self.assertNotIn('data-engine-cost="pro"', BANANA)


class VariantRowTests(unittest.TestCase):
    def test_variant_row_exists_with_both_models(self):
        row = BANANA[BANANA.index('id="bananaVariantRow"'):]
        row = row[:row.index("</div>\n      <!--")] if "</div>\n      <!--" in row else row[:2000]
        self.assertIn('data-variant="nb2"', row)
        self.assertIn('data-variant="pro"', row)

    def test_variant_row_visibility_bound_to_banana_engine(self):
        self.assertRegex(BANANA, r"bananaVariantRow'\);\s*\n\s*if\(bv\) bv\.style\.display = \(e==='banana'\)")

    def test_seedream_row_still_bound_to_seedream(self):
        self.assertRegex(BANANA, r"if\(sv\) sv\.style\.display = \(e==='seedream'\)")


class PointsRecalculationTests(unittest.TestCase):
    """nb2/pro 点数不同，这是本次合并最容易漏的坑。"""

    def test_costbase_keeps_distinct_prices(self):
        m = re.search(r"var COSTBASE=\{([^;]+)\};", BANANA).group(1)
        self.assertIn("nb2:{std:10,hd:14}", m.replace(" ", ""))
        self.assertIn("pro:{std:18,hd:26}", m.replace(" ", ""))

    def test_cost_key_resolves_banana_to_variant(self):
        self.assertRegex(BANANA, r"function costKey\(eng\)\{ return eng==='banana' \? bananaVariant : eng; \}")

    def test_engine_cost_uses_cost_key(self):
        self.assertRegex(BANANA, r"function engineCost\(eng\)\{ var t=COSTBASE\[costKey\(eng\)\]")

    def test_variant_click_recalculates_cost(self):
        """切 Banana 2 → Pro，点数 14→26，按钮与提示必须立刻更新。"""
        fn = BANANA[BANANA.index("function bindVariantRow"):]
        fn = fn[:fn.index("bindVariantRow('bananaVariantRow'")]
        self.assertIn("updateCost()", fn)

    def test_select_engine_recalculates_cost(self):
        """切引擎(如 gpt→banana)也要重算：banana 的价格取决于当前型号。"""
        start = BANANA.index("function selectEngine(e)")
        end = BANANA.index("c.onclick=function(){ selectEngine(", start)   # selectEngine 之后的绑定行
        self.assertIn("updateCost()", BANANA[start:end])


class SubmitTests(unittest.TestCase):
    def test_submits_variant_as_model_to_banana_endpoint(self):
        block = BANANA[BANANA.index("gen.onclick=function()"):]
        block = block[:block.index("// 最近作品")] if "// 最近作品" in block else block[:2500]
        self.assertIn("bp.model=bananaVariant", block)
        self.assertIn("'/api/gen/banana'", block)
        self.assertNotIn("bp.model=engine", block)   # 老写法：engine 曾等于 nb2/pro

    def test_engine_max_count_defined_for_banana(self):
        m = re.search(r"var ENGINE_MAXN=\{([^;]+)\};", BANANA).group(1).replace(" ", "")
        self.assertIn("banana:2", m)


class DeepLinkCompatTests(unittest.TestCase):
    """script.html 的「转视频」按钮将画面描述传入 video.html（通过 ?prompt=）。
    banana.html 仍映射 legacy engine 值（nb2/pro）到合卡后的 selectEngineByKey。"""

    def test_script_page_handoff_points_to_video(self):
        self.assertIn("video.html", SCRIPT)
        self.assertIn("data-to-video", SCRIPT)

    def test_banana_maps_legacy_engine_values(self):
        fn = BANANA[BANANA.index("function selectEngineByKey"):]
        fn = fn[:fn.index("\n  }") + 4]
        self.assertIn("key==='nb2'", fn)
        self.assertIn("key==='pro'", fn)
        self.assertIn("selectEngine('banana')", fn)
        self.assertIn("bananaVariant=key", fn)

    def test_deep_link_handler_uses_the_mapper(self):
        self.assertRegex(BANANA, r"var pe=ip\.get\('engine'\); if\(pe\) selectEngineByKey\(pe\);")


if __name__ == "__main__":
    unittest.main()
