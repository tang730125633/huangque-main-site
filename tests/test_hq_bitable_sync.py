# -*- coding: utf-8 -*-
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

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

    def test_day_bounds_are_fixed_to_shanghai_timezone(self):
        start, end = SYNC.day_bounds("2026-07-31")

        self.assertEqual(start, 1785427200)
        self.assertEqual(end - start, 86400)
        self.assertEqual(SYNC.day_timestamp_ms("2026-07-31"), 1785427200000)

    def test_existing_records_are_updated_in_place_and_duplicates_are_removed(self):
        timestamp = SYNC.day_timestamp_ms("2026-07-31")
        calls = []

        def fake_api(_token, method, path, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return {
                    "data": {
                        "items": [
                            {
                                "record_id": "keep",
                                "fields": {
                                    "日期": timestamp,
                                    "大类": "作图",
                                    "功能": "作图 · 黄雀引擎 1",
                                    "渠道": "Seedream",
                                },
                            },
                            {
                                "record_id": "duplicate",
                                "fields": {
                                    "日期": timestamp,
                                    "大类": "作图",
                                    "功能": "作图 · 黄雀引擎 1",
                                    "渠道": "Seedream",
                                },
                            },
                            {
                                "record_id": "stale",
                                "fields": {
                                    "日期": timestamp,
                                    "功能": "重复旧功能",
                                },
                            },
                        ],
                        "has_more": False,
                    }
                }
            return {"data": {}}

        rows = [
            ["作图", "作图 · 黄雀引擎 1", "Seedream", 3, 1, 12],
            ["", "文案生成", "", 2, 0, 4],
        ]
        with mock.patch.object(SYNC, "api", side_effect=fake_api):
            SYNC.replace_day("token", "2026-07-31", rows)

        writes = set()
        for method, path, body in calls:
            if method != "POST":
                continue
            ids = tuple(
                item.get("record_id", "") if isinstance(item, dict) else item
                for item in body["records"]
            )
            writes.add((path.rsplit("/", 1)[-1], ids))
        self.assertIn(("batch_update", ("keep",)), writes)
        self.assertIn(("batch_create", ("",)), writes)
        self.assertIn(("batch_delete", ("duplicate", "stale")), writes)


if __name__ == "__main__":
    unittest.main()
