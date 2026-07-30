# -*- coding: utf-8 -*-
import json
import unittest
from pathlib import Path

from server.content_domains.history import expand_job_results


CORE = (Path(__file__).resolve().parents[1] / "server" / "content_domains" / "core.py").read_text(encoding="utf-8")


class ImageHistoryItemsTests(unittest.TestCase):
    def test_history_endpoint_uses_expanded_job_results(self):
        self.assertIn("items = history.expand_job_results(rows, lim, offset)", CORE)

    def test_expands_every_url_from_a_multi_image_job(self):
        rows = [
            {
                "id": 12,
                "created_at": 200,
                "result": json.dumps({
                    "url": "/first.png",
                    "urls": ["/first.png", "/second.png"],
                    "mode": "seedream",
                    "prompt": "demo",
                }),
            },
            {
                "id": 11,
                "created_at": 100,
                "result": json.dumps({"url": "/legacy.png", "mode": "gpt"}),
            },
        ]

        items = expand_job_results(rows, limit=9)

        self.assertEqual([item["url"] for item in items], [
            "/first.png", "/second.png", "/legacy.png"
        ])
        self.assertEqual([item["job_id"] for item in items], [12, 12, 11])

    def test_applies_limit_after_expanding_images(self):
        rows = [{
            "id": 12,
            "created_at": 200,
            "result": json.dumps({"urls": ["/1.png", "/2.png", "/3.png"]}),
        }]

        items = expand_job_results(rows, limit=2)

        self.assertEqual([item["url"] for item in items], ["/1.png", "/2.png"])

    def test_applies_offset_after_expanding_images(self):
        rows = [{
            "id": 12,
            "created_at": 200,
            "result": json.dumps({"urls": ["/1.png", "/2.png", "/3.png"]}),
        }]

        items = expand_job_results(rows, limit=2, offset=1)

        self.assertEqual([item["url"] for item in items], ["/2.png", "/3.png"])

    def test_ignores_empty_or_duplicate_urls_inside_one_job(self):
        rows = [{
            "id": 12,
            "created_at": 200,
            "result": json.dumps({
                "url": "/first.png",
                "urls": ["/first.png", "", "/first.png", "/second.png"],
            }),
        }]

        items = expand_job_results(rows, limit=9)

        self.assertEqual([item["url"] for item in items], ["/first.png", "/second.png"])

    def test_zero_limit_returns_no_items(self):
        rows = [{
            "id": 12,
            "created_at": 200,
            "result": json.dumps({"url": "/first.png"}),
        }]

        self.assertEqual(expand_job_results(rows, limit=0), [])

    def test_skips_non_object_results(self):
        rows = [{"id": 12, "created_at": 200, "result": "[]"}]

        self.assertEqual(expand_job_results(rows, limit=9), [])


if __name__ == "__main__":
    unittest.main()
