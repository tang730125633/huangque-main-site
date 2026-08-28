# -*- coding: utf-8 -*-

import base64
import importlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
video = importlib.import_module("content_domains.video")


class _AssetConnection:
    def __init__(self, row):
        self.row = row

    def execute(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        return self.row

    def close(self):
        return None


class VideoPrecisionLipsyncTests(unittest.TestCase):
    def test_owned_video_asset_query_includes_mode_for_voice_sample_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = pathlib.Path(directory) / "audio_assets.db"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE video_assets ("
                "id INTEGER PRIMARY KEY, job_id TEXT, username TEXT, mode TEXT, "
                "video_file TEXT, video_url TEXT, resolution TEXT, ratio TEXT, status TEXT)"
            )
            connection.execute(
                "INSERT INTO video_assets VALUES (17, 'job-17', 'fang', "
                "'lipsync_source', 'video/owned.mp4', '', '1080x1920', '9:16', 'done')"
            )
            connection.commit()
            connection.close()

            def open_database():
                opened = sqlite3.connect(database)
                opened.row_factory = sqlite3.Row
                return opened

            with mock.patch.object(video, "adb", side_effect=open_database):
                asset = video.get_video_asset("fang", 17)

        self.assertIsNotNone(asset)
        self.assertEqual("lipsync_source", asset["mode"])

    def test_nginx_accepts_only_the_declared_100mb_upload_route(self):
        for relative in (
            "server/nginx-huangquechuanmei.conf",
            "deploy/nginx-huangquechuanmei.conf",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            start = source.index("location = /api/gen/video/lipsync-import")
            end = source.index("\n    }", start)
            location = source[start:end]
            self.assertIn("client_max_body_size 100m;", location)
            self.assertIn("limit_conn hq_cli_upload_conn 2;", location)
            self.assertIn('proxy_set_header X-HQ-Internal-Token "";', location)

    def test_import_rejects_empty_oversize_and_non_mp4_content(self):
        with self.assertRaisesRegex(ValueError, "不能为空"):
            video.import_lipsync_source_video("fang", b"")
        with mock.patch.object(video, "VIDEO_IMPORT_MAX_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "不能超过"):
                video.import_lipsync_source_video("fang", b"x" * 9)
        with self.assertRaisesRegex(ValueError, "仅支持 MP4"):
            video.import_lipsync_source_video(
                "fang", b"\x00\x00\x00\x18ftypisom", "video/webm"
            )
        with self.assertRaisesRegex(ValueError, "有效的 MP4"):
            video.import_lipsync_source_video("fang", b"not-an-mp4-file")

    def test_import_records_a_private_owned_source_asset(self):
        raw = b"\x00\x00\x00\x18ftypisom" + b"x" * 128
        probe = SimpleNamespace(returncode=0, stdout=json.dumps({
            "streams": [{"width": 1080, "height": 1920, "r_frame_rate": "25/1"}],
            "format": {"duration": "12.5"},
        }), stderr="")
        recorded = {}
        row = {
            "id": 17, "mode": "lipsync_source", "status": "done",
            "video_file": "video/owned.mp4", "video_url": "/media/private/owned",
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(video, "VIDEO_OUT_DIR", pathlib.Path(directory)), \
                mock.patch.object(video.subprocess, "run", return_value=probe), \
                mock.patch.object(video, "public_url", return_value="/media/private/owned") as url, \
                mock.patch.object(video, "record_video_asset",
                                  side_effect=lambda _job, user, data: recorded.update(
                                      {"username": user, "asset": data})), \
                mock.patch.object(video, "adb", return_value=_AssetConnection(row)):
            asset = video.import_lipsync_source_video(
                "fang", raw, title=" 本人 口播视频 "
            )

        self.assertEqual("fang", recorded["username"])
        self.assertEqual("lipsync_source", recorded["asset"]["mode"])
        self.assertEqual("9:16", recorded["asset"]["ratio"])
        self.assertEqual(12.5, asset["duration"])
        url.assert_called_once_with(mock.ANY, "video/mp4", private=True)

    def test_voice_sample_is_owned_bounded_hashed_and_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "owned.mp4"
            source.write_bytes(b"owned-video")

            def run(command, **_kwargs):
                if command[0] == "ffmpeg":
                    pathlib.Path(command[-1]).write_bytes(b"ID3" + b"v" * 600)
                    self.assertIn("-t", command)
                    self.assertEqual("60", command[command.index("-t") + 1])
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout="12.345\n", stderr="")

            with mock.patch.object(video, "AUDIO_OUT_DIR", root), \
                    mock.patch.object(video, "get_video_asset", return_value={
                        "id": 17, "mode": "lipsync_source",
                        "video_file": "video/owned.mp4",
                    }), \
                    mock.patch.object(video, "_resolve_out_file", return_value=source), \
                    mock.patch.object(video, "_user_owns_output_file", return_value=True), \
                    mock.patch.object(video.subprocess, "run", side_effect=run):
                sample = video.extract_lipsync_voice_sample("fang", 17)

            self.assertEqual(17, sample["video_asset_id"])
            self.assertEqual("mp3", sample["audio_format"])
            self.assertEqual(12.345, sample["duration"])
            self.assertEqual(
                video.hashlib.sha256(source.read_bytes()).hexdigest(),
                sample["video_sha256"],
            )
            self.assertGreater(len(base64.b64decode(sample["audio"])), 256)
            self.assertEqual([], list(root.glob(".lipsync-voice-sample-*.mp3")))

    def test_voice_sample_rejects_cross_account_non_source_and_silence(self):
        with mock.patch.object(video, "get_video_asset", return_value=None):
            with self.assertRaisesRegex(ValueError, "不属于当前账号"):
                video.extract_lipsync_voice_sample("other", 17)
        with mock.patch.object(video, "get_video_asset", return_value={
                "id": 18, "mode": "text", "video_file": "video/other.mp4"}):
            with self.assertRaisesRegex(ValueError, "本人上传"):
                video.extract_lipsync_voice_sample("fang", 18)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "silent.mp4"
            source.write_bytes(b"video")
            with mock.patch.object(video, "AUDIO_OUT_DIR", root), \
                    mock.patch.object(video, "get_video_asset", return_value={
                        "id": 19, "mode": "lipsync_source",
                        "video_file": "video/silent.mp4",
                    }), \
                    mock.patch.object(video, "_resolve_out_file", return_value=source), \
                    mock.patch.object(video, "_user_owns_output_file", return_value=True), \
                    mock.patch.object(video.subprocess, "run", return_value=SimpleNamespace(
                        returncode=1, stdout="", stderr="no audio")):
                with self.assertRaisesRegex(ValueError, "没有可用人声"):
                    video.extract_lipsync_voice_sample("fang", 19)
            self.assertEqual([], list(root.glob(".lipsync-voice-sample-*.mp3")))

    def test_hardened_validator_accepts_only_verified_consent_metadata(self):
        payload = {
            "mode": "lipsync", "video_asset_id": 7, "audio_asset_id": 8,
            "lipsync_mode": "precision", "dynamic_duration": True,
            "digital_human_pipeline": "digital_human_video_voice",
            "digital_human_stage": "precision", "digital_human_consent_id": "consent-1",
            "clone_attempt_id": "attempt-1",
        }
        patches = (
            mock.patch.object(video, "get_video_asset", return_value={
                "video_file": "video/owned.mp4", "ratio": "9:16", "resolution": "1080x1920"}),
            mock.patch.object(video, "get_audio_asset", return_value={"file": "audio/owned.mp3"}),
            mock.patch.object(video, "_resolve_out_file", return_value=ROOT / "owned.mp4"),
            mock.patch.object(video, "_user_owns_output_file", return_value=True),
            mock.patch.object(video, "_normalize_audio_file_ref", return_value="audio/owned.mp3"),
            mock.patch.object(video, "_probe_video_duration", return_value=12.0),
        )
        for patcher in patches:
            patcher.start()
        try:
            cleaned = video.validate_video_payload(payload, "fang")
            with self.assertRaisesRegex(ValueError, "不支持参数"):
                video.validate_video_payload(dict(payload, text="not allowed"), "fang")
        finally:
            for patcher in reversed(patches):
                patcher.stop()
        self.assertEqual("consent-1", cleaned["digital_human_consent_id"])
        self.assertEqual("attempt-1", cleaned["clone_attempt_id"])
        self.assertEqual("precision", cleaned["lipsync_mode"])

    def test_browser_submits_the_narrow_lipsync_contract(self):
        source = (ROOT / "site/workbench/digital-human-unified.js").read_text(
            encoding="utf-8"
        )
        call = source[source.index("function generateLipsync"):source.index(
            "function editDecisions"
        )]
        self.assertIn("lipsync_mode:'precision'", call)
        self.assertIn("dynamic_duration:true", call)
        self.assertNotIn("text:text", call)
        self.assertNotIn("resolution:'1080p'", call)


if __name__ == "__main__":
    unittest.main()
