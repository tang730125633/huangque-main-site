import importlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
video = importlib.import_module("content_domains.video")


class H3VideoImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "assets.db"
        with sqlite3.connect(self.db) as c:
            c.execute("""CREATE TABLE video_assets(
                id INTEGER PRIMARY KEY, job_id INTEGER UNIQUE, username TEXT NOT NULL, mode TEXT NOT NULL,
                image_file TEXT, audio_file TEXT, reference_video_file TEXT, video_file TEXT, video_url TEXT,
                text TEXT, voice_key TEXT, resolution TEXT, ratio TEXT, motion TEXT, phase TEXT,
                image_asset_id TEXT, audio_asset_id TEXT, reference_asset_id TEXT, provider_video_id TEXT,
                provider_key_id TEXT, provider_avatar_id TEXT, provider_avatar_group_id TEXT,
                source_video_url TEXT, background_file TEXT, tryon_mode TEXT, model TEXT,
                status TEXT NOT NULL, error TEXT, created_at INTEGER, updated_at INTEGER)""")

    def tearDown(self):
        self.tmp.cleanup()

    def connect_assets(self):
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        return connection

    def test_imports_valid_h3_mp4_as_user_asset(self):
        raw = b"\x00\x00\x00\x18ftypmp42" + b"x" * 32
        probe = subprocess.CompletedProcess([], 0, json.dumps({
            "streams": [{"width": 1280, "height": 736}], "format": {"duration": "15.083333"}
        }), "")
        with patch.object(video, "VIDEO_OUT_DIR", self.root / "video"), \
             patch.object(video, "adb", side_effect=self.connect_assets), \
             patch.object(video, "public_url", return_value="https://cdn.example/h3.mp4"), \
             patch.object(video.subprocess, "run", return_value=probe):
            asset = video.import_h3_video_asset("qa-user", raw, "video/mp4", "迟到的信")
        self.assertEqual(asset["mode"], "h3_import")
        self.assertEqual(asset["status"], "done")
        self.assertEqual(asset["duration"], 15.083333)
        self.assertTrue((self.root / asset["video_file"]).is_file())

    def test_rejects_non_mp4_before_writing(self):
        with self.assertRaisesRegex(ValueError, "有效的 MP4"):
            video.import_h3_video_asset("qa-user", b"not-a-video", "video/mp4")


if __name__ == "__main__":
    unittest.main()
