# -*- coding: utf-8 -*-
"""内容爬取计费：主爬取 3 点，提取文案 6 点。

## 前端两个动作共用一个 collect 接口，靠 want 区分

    主爬取（内容爬取）  POST /api/gen/collect  want=['comments'] 或 ['video']  →  3 点
    提取文案            POST /api/gen/collect  want=['transcript']              →  6 点

生产 Nginx 实际路由命中 LeadGen 服务，基础价是 3 点；提取文案加收 3 点。

## 前后端必须一致

前端从统一定价接口读取，不再单独维护一份价格。
"""
import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

points = importlib.import_module("content_domains.points")
HTML = (ROOT / "site/workbench/collect.html").read_text(encoding="utf-8")


class MainCrawlIs3Tests(unittest.TestCase):
    def test_comments_crawl_is_3(self):
        self.assertEqual(points.cost_of("collect", {"want": ["comments"]}), 3)

    def test_video_download_crawl_is_3(self):
        """仅下载视频（want=['video']）也是主爬取，3 点。"""
        self.assertEqual(points.cost_of("collect", {"want": ["video"]}), 3)

    def test_empty_want_defaults_to_main_crawl(self):
        """没给 want 时按主爬取算 —— 绝不能因为字段缺失就白送（回落到 0）。"""
        self.assertEqual(points.cost_of("collect", {}), 3)


class TranscriptStays6Tests(unittest.TestCase):
    def test_transcript_extract_is_6(self):
        """提取文案保留 6 点，别跟着主爬取一起涨。"""
        self.assertEqual(points.cost_of("collect", {"want": ["transcript"]}), 6)

    def test_transcript_mixed_in_still_6(self):
        """want 里只要含 transcript 就按 6（和改动前 `'transcript' in want` 的语义一致）。"""
        self.assertEqual(points.cost_of("collect", {"want": ["comments", "transcript"]}), 6)


class FrontendMatchesBackendTests(unittest.TestCase):
    """价钱是用户下单前唯一看得到的数，写错就是明码标错价。"""

    def test_the_crawl_note_shows_default(self):
        self.assertIn("约 3 点", HTML)

    def test_both_note_states_show_it(self):
        """colNote 有两态，都从统一基础价变量显示。"""
        self.assertIn("collectBasePoints=p['collect.base']", HTML)
        self.assertEqual(HTML.count("'+collectBasePoints+' 点"), 2)

    def test_transcript_button_still_says_6(self):
        """提取文案按钮的「约 6 点」不能被误改。"""
        self.assertIn("约 6 点", HTML)


if __name__ == "__main__":
    unittest.main()
