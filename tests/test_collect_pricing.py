# -*- coding: utf-8 -*-
"""内容爬取计费：主爬取 30 点，提取文案 6 点（kongli 2026-07-15）。

## 前端两个动作共用一个 collect 接口，靠 want 区分

    主爬取（内容爬取）  POST /api/gen/collect  want=['comments'] 或 ['video']  → 30 点
    提取文案            POST /api/gen/collect  want=['transcript']              →  6 点

原来主爬取是 3 点。改成 30（kongli 2026-07-15）。提取文案保留 6 点不变 ——
它前端标着「约 6 点」，别顺手一起涨了。

## 前后端必须一致

主爬取以前不显示成本（3 点、藏着），现在 30 点是 10 倍，藏着 = 用户被静默扣 30 点，
正是 leads 那条注释警告的「消耗点数对不上」。collect.html 的 colNote 已补「约 30 点」，
静态那处和 setMode 里 JS 动态设的那处都要有。
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


class MainCrawlIs30Tests(unittest.TestCase):
    def test_comments_crawl_is_30(self):
        self.assertEqual(points.cost_of("collect", {"want": ["comments"]}), 30)

    def test_video_download_crawl_is_30(self):
        """仅下载视频（want=['video']）也是主爬取，30 点。"""
        self.assertEqual(points.cost_of("collect", {"want": ["video"]}), 30)

    def test_empty_want_defaults_to_main_crawl(self):
        """没给 want 时按主爬取算 —— 绝不能因为字段缺失就白送（回落到 0）。"""
        self.assertEqual(points.cost_of("collect", {}), 30)


class TranscriptStays6Tests(unittest.TestCase):
    def test_transcript_extract_is_6(self):
        """提取文案保留 6 点，别跟着主爬取一起涨。"""
        self.assertEqual(points.cost_of("collect", {"want": ["transcript"]}), 6)

    def test_transcript_mixed_in_still_6(self):
        """want 里只要含 transcript 就按 6（和改动前 `'transcript' in want` 的语义一致）。"""
        self.assertEqual(points.cost_of("collect", {"want": ["comments", "transcript"]}), 6)


class FrontendMatchesBackendTests(unittest.TestCase):
    """价钱是用户下单前唯一看得到的数，写错就是明码标错价。"""

    def test_the_crawl_note_shows_30(self):
        self.assertIn("约 30 点", HTML)

    def test_both_note_states_show_it(self):
        """colNote 有两态：普通爬取 / 仅下载。两态都是主爬取，都该显示 30。"""
        # 静态 HTML 那处
        self.assertIn("文案 + 评论默认提取 · 约 30 点", HTML)
        # setMode 里 JS 动态设的那处（download 分支）
        self.assertIn("仅解析并下载视频本身 · 不提取评论/文案 · 约 30 点", HTML)

    def test_transcript_button_still_says_6(self):
        """提取文案按钮的「约 6 点」不能被误改。"""
        self.assertIn("约 6 点", HTML)


if __name__ == "__main__":
    unittest.main()
