# -*- coding: utf-8 -*-
"""BUG-0010: AI 作图数量应支持离散选项 1、2、4。"""
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BANANA = (ROOT / "site" / "workbench" / "banana.html").read_text(encoding="utf-8")
IMGGEN = (ROOT / "server" / "imggen_api.py").read_text(encoding="utf-8")


class ImageCountFourTests(unittest.TestCase):
    def test_banana_models_offer_four_images(self):
        limits = re.search(r"var ENGINE_MAXN=\{([^;]+)\};", BANANA).group(1).replace(" ", "")
        self.assertIn("banana:4", limits)

    def test_count_control_uses_discrete_one_two_four_options(self):
        self.assertIn("varCOUNT_OPTIONS=[1,2,4]", BANANA.replace(" ", ""))
        self.assertIn("stepCount(-1)", BANANA)
        self.assertIn("stepCount(1)", BANANA)

    def test_banana_api_accepts_only_one_two_or_four(self):
        self.assertIn("count not in {1, 2, 4}", IMGGEN)
        self.assertGreaterEqual(IMGGEN.count("1、2 或 4"), 2)


if __name__ == "__main__":
    unittest.main()
