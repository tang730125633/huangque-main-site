import importlib
import hashlib
import io
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

    def test_streams_ten_minute_mov_into_owned_video_compose_asset(self):
        raw = b"\x00\x00\x00\x18ftypqt  " + b"x" * 64
        video_probe = subprocess.CompletedProcess([], 0, json.dumps({
            "streams": [{"width": 1080, "height": 1920}],
            "format": {"duration": "600.0"},
        }), "")
        audio_probe = subprocess.CompletedProcess([], 0, json.dumps({
            "streams": [{"codec_name": "aac"}],
        }), "")
        with patch.object(video, "VIDEO_OUT_DIR", self.root / "video"), \
             patch.object(video, "adb", side_effect=self.connect_assets), \
             patch.object(video, "public_url", return_value="https://cdn.example/source.mov"), \
             patch.object(video.subprocess, "run", side_effect=[video_probe, audio_probe]):
            asset = video.import_video_compose_source_asset(
                "qa-user", io.BytesIO(raw), len(raw), "video/quicktime", "第一条口播",
                hashlib.sha256(raw).hexdigest(),
            )
        self.assertEqual("video_compose_source", asset["mode"])
        self.assertEqual(600.0, asset["duration"])
        self.assertEqual("9:16", asset["ratio"])
        self.assertTrue((self.root / asset["video_file"]).is_file())

    def test_rejects_video_compose_source_without_audio(self):
        raw = b"\x00\x00\x00\x18ftypisom" + b"x" * 32
        video_probe = subprocess.CompletedProcess([], 0, json.dumps({
            "streams": [{"width": 1920, "height": 1080}],
            "format": {"duration": "30.0"},
        }), "")
        audio_probe = subprocess.CompletedProcess([], 0, json.dumps({"streams": []}), "")
        with patch.object(video, "VIDEO_OUT_DIR", self.root / "video"), \
             patch.object(video.subprocess, "run", side_effect=[video_probe, audio_probe]), \
             self.assertRaisesRegex(ValueError, "缺少音频流"):
            video.import_video_compose_source_asset(
                "qa-user", io.BytesIO(raw), len(raw), "video/mp4", "",
                hashlib.sha256(raw).hexdigest(),
            )

    def test_video_compose_import_requires_digest_and_cleans_final_file_on_db_error(self):
        raw = b"\x00\x00\x00\x18ftypisom" + b"x" * 32
        with patch.object(video, "VIDEO_OUT_DIR", self.root / "video"), \
             self.assertRaisesRegex(ValueError, "摘要无效"):
            video.import_video_compose_source_asset(
                "qa-user", io.BytesIO(raw), len(raw), "video/mp4",
            )

        video_probe = subprocess.CompletedProcess([], 0, json.dumps({
            "streams": [{"width": 1920, "height": 1080}],
            "format": {"duration": "30.0"},
        }), "")
        audio_probe = subprocess.CompletedProcess([], 0, json.dumps({
            "streams": [{"codec_name": "aac"}],
        }), "")
        with patch.object(video, "VIDEO_OUT_DIR", self.root / "video"), \
             patch.object(video.subprocess, "run", side_effect=[video_probe, audio_probe]), \
             patch.object(video, "public_url", return_value="https://cdn.example/source.mp4"), \
             patch.object(video, "record_video_asset", side_effect=sqlite3.OperationalError("locked")), \
             self.assertRaisesRegex(ValueError, "导入失败"):
            video.import_video_compose_source_asset(
                "qa-user", io.BytesIO(raw), len(raw), "video/mp4", "",
                hashlib.sha256(raw).hexdigest(),
            )
        self.assertEqual([], list((self.root / "video").glob("*")))

    def test_nginx_streams_two_gib_video_compose_import_routes(self):
        for relative in (
            "server/nginx-huangquechuanmei.conf",
            "deploy/nginx-huangquechuanmei.conf",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            for route in (
                "/api/gen/video-compose/import",
                "/api/auth/cli/video-compose-import",
            ):
                start = source.index("location = %s" % route)
                end = source.index("\n    }", start)
                location = source[start:end]
                self.assertIn("client_max_body_size 2048m;", location)
                self.assertIn("proxy_request_buffering off;", location)
                self.assertIn("limit_conn hq_cli_upload_conn 1;", location)


if __name__ == "__main__":
    unittest.main()
