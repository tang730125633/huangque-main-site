import json
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "site/workbench/inspirations.json"
ASSET_DIR = ROOT / "site/assets/inspirations"
PLACEHOLDER_PROMPT = "主标题区、卖点卡片、产品或服务视觉中心、预约信息占位"


class InspirationDiversityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = json.loads(DATA_PATH.read_text(encoding="utf-8"))

    def test_gallery_keeps_curated_range_and_contiguous_ids(self):
        self.assertGreaterEqual(len(self.items), 60)
        self.assertEqual(sorted(item["id"] for item in self.items), list(range(1, len(self.items) + 1)))
        self.assertGreaterEqual(len({item["category"] for item in self.items}), 11)

    def test_template_placeholders_are_not_published(self):
        for item in self.items:
            self.assertIsNone(re.search(r"灵感\d+$", item["title"]), item["title"])
            self.assertNotIn(PLACEHOLDER_PROMPT, item["prompt"])

    def test_every_webp_is_referenced_once_and_valid(self):
        referenced = [pathlib.PurePosixPath(item["image"]).name for item in self.items]
        self.assertEqual(len(referenced), len(set(referenced)))
        self.assertEqual(set(referenced), {path.name for path in ASSET_DIR.glob("*.webp")})
        for name in referenced:
            header = (ASSET_DIR / name).read_bytes()[:12]
            self.assertEqual(header[:4], b"RIFF", name)
            self.assertEqual(header[8:12], b"WEBP", name)

    def test_new_vertical_cases_have_real_images_and_reusable_prompts(self):
        additions = [item for item in self.items if 59 <= item["id"] <= 62]
        self.assertEqual({item["category"] for item in additions}, {"皮肤管理封面", "医美科普配图"})
        for item in additions:
            self.assertEqual(item["model"], "gpt")
            self.assertGreater(len(item["prompt"]), 100)
            self.assertGreater((ASSET_DIR / pathlib.PurePosixPath(item["image"]).name).stat().st_size, 80_000)

    def test_moments_cases_are_curated_first_with_reusable_prompts(self):
        additions = self.items[:18]
        self.assertEqual([item["id"] for item in additions], list(range(63, 81)))
        self.assertTrue(all(item["prompt"].strip() for item in additions))


if __name__ == "__main__":
    unittest.main()
