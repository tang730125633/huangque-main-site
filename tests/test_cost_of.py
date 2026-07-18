import importlib
import sys
import unittest
from pathlib import Path


class CostOfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.points = importlib.import_module("content_domains.points")

    def test_script_to_video_talking_estimates_by_text_length(self):
        """一键成片口播按文案字数估秒预扣：20 字 ≈ 5 秒 → 50 点"""
        scenes = [{"line": "一二三四五六七八九十一二三四五六七八九十", "scene": "画面"}]
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": scenes, "style": "口播"}), 50)

    def test_script_to_video_talking_has_minimum_hold(self):
        """口播预扣保底 10 点（1 秒）"""
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": [{"line": "短"}], "style": "种草"}), 10)

    def test_script_to_video_drama_stays_flat(self):
        """剧情走 grok 不按秒，保持 COST 表固定 20 点"""
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": [{"scene": "画面", "line": ""}], "style": "剧情"}), 20)

    def test_breakdown_batch_progressive_pricing(self):
        """批量拆解：首条 8 点，每多一条 +4，封顶 5 条 = 24 点"""
        self.assertEqual(self.points.cost_of("breakdown", {"urls": ["https://a.test/1"]}), 8)
        self.assertEqual(
            self.points.cost_of("breakdown", {"urls": ["https://a.test/1", "https://a.test/2", "https://a.test/3"]}), 16)
        self.assertEqual(
            self.points.cost_of("breakdown", {"urls": ["https://a.test/%d" % i for i in range(6)]}), 24)

    def test_breakdown_single_url_stays_8(self):
        self.assertEqual(self.points.cost_of("breakdown", {"url": "https://a.test/1"}), 8)


if __name__ == "__main__":
    unittest.main()
