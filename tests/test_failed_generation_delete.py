import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import core  # noqa: E402


class FailedGenerationDeleteTests(unittest.TestCase):
    def test_only_owned_failed_image_and_video_jobs_can_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_job_db, old_audio_db = core.JOB_DB, core.AUDIO_DB
            core.JOB_DB, core.AUDIO_DB = str(Path(tmp) / "jobs.db"), str(Path(tmp) / "assets.db")
            try:
                with sqlite3.connect(core.JOB_DB) as db:
                    db.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY,kind TEXT,username TEXT,status TEXT,deleted INTEGER DEFAULT 0,updated_at INTEGER)")
                    db.executemany("INSERT INTO jobs(id,kind,username,status) VALUES(?,?,?,?)", [
                        (1, "image", "tang", "error"),
                        (2, "video", "tang", "error"),
                        (3, "image", "tang", "done"),
                    ])
                with sqlite3.connect(core.AUDIO_DB) as db:
                    db.execute("CREATE TABLE video_assets(job_id INTEGER,username TEXT,status TEXT,updated_at INTEGER)")
                    db.execute("INSERT INTO video_assets(job_id,username,status) VALUES(2,'tang','failed')")

                core.delete_failed_job("tang", 1)
                core.delete_failed_job("tang", 2)
                with self.assertRaises(ValueError):
                    core.delete_failed_job("tang", 3)
                with self.assertRaises(LookupError):
                    core.delete_failed_job("other", 1)

                with sqlite3.connect(core.JOB_DB) as db:
                    self.assertEqual(db.execute("SELECT deleted FROM jobs WHERE id=1").fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT deleted FROM jobs WHERE id=2").fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT deleted FROM jobs WHERE id=3").fetchone()[0], 0)
                with sqlite3.connect(core.AUDIO_DB) as db:
                    self.assertEqual(db.execute("SELECT status FROM video_assets WHERE job_id=2").fetchone()[0], "deleted")
            finally:
                core.JOB_DB, core.AUDIO_DB = old_job_db, old_audio_db

    def test_image_and_video_pages_offer_confirmed_delete_for_failed_records(self):
        banana = (ROOT / "site/workbench/banana.html").read_text(encoding="utf-8")
        video = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")
        for page in (banana, video):
            self.assertIn("/api/gen/job/delete", page)
            self.assertIn("确定删除这条失败记录吗？", page)


if __name__ == "__main__":
    unittest.main()
