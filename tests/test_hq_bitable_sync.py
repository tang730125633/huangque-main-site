# -*- coding: utf-8 -*-
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/hq_bitable_sync_server.py"
SPEC = importlib.util.spec_from_file_location("hq_bitable_sync_server", SCRIPT)
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)


class FunctionDailySyncTests(unittest.TestCase):
    def test_all_functions_are_counted_without_losing_channel_breakdown(self):
        rows = [
            ("image", '{"model":"nb2"}', "done", 10, 0),
            ("image", '{"model":"nb2"}', "error", 10, 1),
            ("avatar", "{}", "done", 8, 0),
            ("collect", '{"url":"https://example.com"}', "error", 2, 0),
        ]
        got = {row[1]: row for row in SYNC.summarize(rows)}

        self.assertEqual(got["作图 · 纳米香蕉 2"], ["作图", "作图 · 纳米香蕉 2", "NanoBanana2", 1, 1, 10])
        self.assertEqual(got["创建数字人形象"], ["视频", "创建数字人形象", "", 1, 0, 8])
        self.assertEqual(got["内容爬取 · 贴链接"], ["", "内容爬取 · 贴链接", "", 0, 1, 2])

    def test_ship_and_drift_sentinel_track_the_runtime_script(self):
        ship = (ROOT / "ship").read_text(encoding="utf-8")
        sentinel = (ROOT / "scripts/drift_sentinel.py").read_text(encoding="utf-8")
        self.assertIn("scripts/hq_bitable_sync_server.py) dest=/home/ubuntu/; svc=\"\"", ship)
        self.assertIn(
            "'scripts/hq_bitable_sync_server.py': '/home/ubuntu/hq_bitable_sync_server.py'",
            sentinel,
        )

    def test_legacy_channel_filter_names_stay_compatible(self):
        self.assertEqual(SYNC.channel_name("tryon", {}), ("视频", "换装·线一HeyGen"))
        self.assertEqual(SYNC.channel_name("video", {"mode": "motion"}), ("视频", "动作模仿·线一HeyGen"))


if __name__ == "__main__":
    unittest.main()
