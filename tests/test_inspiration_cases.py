import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import inspiration_cases as cases


class InspirationCasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.admin_db = root / "admin.db"
        self.jobs_db = root / "jobs.db"
        cases.init_db(self.admin_db)

    def tearDown(self):
        self.tmp.cleanup()

    def sample(self, **extra):
        data = {
            "title": "春日水光肌",
            "category": "医美焕肤",
            "tags": ["朋友圈", "写实"],
            "prompt": "自然光下的水润皮肤人像",
            "media_type": "image",
            "media_url": "https://cdn.example.com/case.webp",
            "cover_url": "https://cdn.example.com/case.webp",
            "target": "nb2",
            "featured": True,
            "sort_order": 10,
            "source_platform": "原创",
            "rights_status": "original",
        }
        data.update(extra)
        return data

    def test_publish_events_metrics_and_unpublish(self):
        draft = cases.save_case(self.admin_db, {"title": "待补素材"}, "tang")
        self.assertEqual(draft["status"], "draft")
        with self.assertRaisesRegex(ValueError, "分类"):
            cases.set_status(self.admin_db, draft["id"], "published", "tang")

        item = cases.save_case(self.admin_db, self.sample(), "tang", publish=True)
        public = cases.list_public(self.admin_db)["items"]
        self.assertEqual(public[0]["id"], cases.PUBLIC_ID_BASE + item["id"])
        self.assertTrue(public[0]["managed"])

        public_id = public[0]["id"]
        cases.record_events(self.admin_db, {"event": "impression", "ids": [public_id]})
        cases.record_events(self.admin_db, {"event": "click", "ids": [public_id]})

        with sqlite3.connect(self.jobs_db) as conn:
            conn.execute("CREATE TABLE jobs(cost INTEGER,status TEXT,payload TEXT,created_at INTEGER)")
            conn.execute(
                "INSERT INTO jobs VALUES(?,?,?,?)",
                (12, "done", json.dumps({"source_inspiration_id": public_id, "image": "x" * 5000}), int(time.time())),
            )
            conn.execute(
                "INSERT INTO jobs VALUES(?,?,?,?)",
                (12, "error", json.dumps({"source_inspiration_id": public_id}), int(time.time())),
            )
        admin_item = next(x for x in cases.list_admin(self.admin_db, self.jobs_db)["items"] if x["id"] == item["id"])
        self.assertEqual((admin_item["impressions"], admin_item["clicks"]), (1, 1))
        self.assertEqual((admin_item["success_count"], admin_item["points_spent"]), (1, 12))

        cases.set_status(self.admin_db, item["id"], "unpublished", "tang")
        self.assertEqual(cases.list_public(self.admin_db)["items"], [])

    def test_upload_checks_magic_and_uses_cos(self):
        payload = b"\x89PNG\r\n\x1a\n" + b"data"
        with mock.patch.object(cases.cos, "enabled", return_value=True), mock.patch.object(
            cases.cos, "put_file", return_value="https://cdn.example.com/upload.png"
        ) as put:
            result = cases.upload_media(io.BytesIO(payload), len(payload), "image/png", "image")
        self.assertEqual(result["media_type"], "image")
        self.assertTrue(result["url"].endswith("upload.png"))
        put.assert_called_once()
        with self.assertRaisesRegex(ValueError, "内容与格式"):
            with mock.patch.object(cases.cos, "enabled", return_value=True):
                cases.upload_media(io.BytesIO(b"not-png"), 7, "image/png", "image")

    def test_admin_gallery_and_generation_pages_are_wired(self):
        admin = (ROOT / "site/admin/index.html").read_text(encoding="utf-8")
        gallery = (ROOT / "site/workbench/inspiration.html").read_text(encoding="utf-8")
        banana = (ROOT / "site/workbench/banana.html").read_text(encoding="utf-8")
        video = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")
        nginx = (ROOT / "deploy/nginx-huangquechuanmei.conf").read_text(encoding="utf-8")
        self.assertIn('data-module-tab="inspirations"', admin)
        self.assertIn("/api/admin/inspirations/save", admin)
        self.assertIn("/api/admin/public/inspirations", gallery)
        self.assertIn("source_inspiration_id", banana)
        self.assertIn("source_inspiration_id", video)
        self.assertIn("client_max_body_size 200m", nginx)


if __name__ == "__main__":
    unittest.main()
