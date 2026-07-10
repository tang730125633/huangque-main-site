import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fix_video_first_frame_covers.py"
SPEC = importlib.util.spec_from_file_location("fix_video_first_frame_covers", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VideoCoverBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.asset_db = self.root / "audio_assets.db"
        self.content_out = self.root / "content_out"
        (self.content_out / "video").mkdir(parents=True)
        with sqlite3.connect(self.asset_db) as conn:
            conn.execute(
                """CREATE TABLE video_assets(
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER,
                    video_file TEXT,
                    image_file TEXT,
                    status TEXT,
                    deleted INTEGER DEFAULT 0,
                    created_at INTEGER,
                    updated_at INTEGER
                )"""
            )
            conn.executemany(
                """INSERT INTO video_assets
                   (id, job_id, video_file, image_file, status, deleted, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                [
                    (1, 101, "video/one.mp4", None, "done", 0, 3, 3),
                    (2, 102, "video/two.mp4", "video/two_cover.jpg", "done", 0, 2, 2),
                    (3, 103, "video/deleted.mp4", None, "done", 1, 1, 1),
                    (4, 104, "video/running.mp4", None, "running", 0, 4, 4),
                ],
            )
        (self.content_out / "video" / "one.mp4").write_bytes(b"video")

    def tearDown(self):
        self.tmp.cleanup()

    def test_find_missing_reads_video_assets_database(self):
        rows = MODULE.find_missing(self.asset_db)
        self.assertEqual([(1, 101, "video/one.mp4")], rows)

    def test_dry_run_never_invokes_ffmpeg_or_writes_database(self):
        def unexpected_runner(*args, **kwargs):
            raise AssertionError("dry-run invoked ffmpeg")

        stats = MODULE.repair(
            self.asset_db,
            self.content_out,
            dry_run=True,
            runner=unexpected_runner,
        )
        self.assertEqual(1, stats["ready"])
        with sqlite3.connect(self.asset_db) as conn:
            value = conn.execute("SELECT image_file FROM video_assets WHERE id=1").fetchone()[0]
        self.assertIsNone(value)
        self.assertFalse((self.content_out / "video" / "one_cover.jpg").exists())

    def test_backfill_is_idempotent(self):
        def fake_ffmpeg(command, **kwargs):
            Path(command[-1]).write_bytes(b"jpeg")

        stats = MODULE.repair(
            self.asset_db,
            self.content_out,
            sleep_seconds=0,
            runner=fake_ffmpeg,
        )
        self.assertEqual(1, stats["fixed"])
        with sqlite3.connect(self.asset_db) as conn:
            value = conn.execute("SELECT image_file FROM video_assets WHERE id=1").fetchone()[0]
        self.assertEqual("video/one_cover.jpg", value)
        self.assertEqual(0, MODULE.repair(
            self.asset_db,
            self.content_out,
            sleep_seconds=0,
            runner=fake_ffmpeg,
        )["candidates"])

    def test_rejects_path_outside_content_out(self):
        self.assertIsNone(MODULE.resolve_video_path(self.content_out, "../outside.mp4"))


if __name__ == "__main__":
    unittest.main()
