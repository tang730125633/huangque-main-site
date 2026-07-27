import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
os.environ.setdefault("CONTENT_BASE", tempfile.mkdtemp())

from content_domains import (
    cos,
    feature_flags,
    startup_recovery,
    video,
    video_seedance,
    wavespeed,
)


class SeedanceUpscaleTests(unittest.TestCase):
    def _body(self, **changes):
        body = {
            "channel": "micro",
            "prompt": "paper bird",
            "model": video_seedance.SEEDANCE_MODEL,
            "duration": 4,
            "ratio": "16:9",
            "resolution": "480p",
            "generate_audio": True,
            "upscale": True,
        }
        body.update(changes)
        return body

    def test_validation_rejects_unsafe_upscale_combinations_before_charge(self):
        enabled = patch.object(feature_flags, "is_enabled", return_value=True)
        with enabled, patch.object(video, "SEEDANCE_UPSCALE_ENABLED", True), \
             patch.object(video_seedance, "available", return_value=True), \
             patch.object(wavespeed, "available", return_value=True), \
             patch.object(cos, "enabled", return_value=True):
            cleaned = video.validate_xiaole_video_payload(
                self._body(upscale_prediction_id="attacker-controlled")
            )
            self.assertTrue(cleaned["upscale"])
            self.assertNotIn("upscale_prediction_id", cleaned)
            with self.assertRaisesRegex(ValueError, "必须先生成 480p"):
                video.validate_xiaole_video_payload(
                    self._body(resolution="1080p")
                )
            with self.assertRaisesRegex(ValueError, "必须为布尔值"):
                video.validate_xiaole_video_payload(self._body(upscale="yes"))
        with patch.object(feature_flags, "is_enabled", return_value=True), \
             patch.object(video, "SEEDANCE_UPSCALE_ENABLED", True), \
             patch.object(video_seedance, "available", return_value=True), \
             patch.object(wavespeed, "available", return_value=False):
            with self.assertRaisesRegex(ValueError, "暂未配置"):
                video.validate_xiaole_video_payload(self._body())

    def test_standard_1080p_crops_to_fill_without_black_bars(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "upscaled.mp4"
            source.write_bytes(b"input")
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b"output")

            with patch.object(video, "_resolve_out_file", return_value=source), \
                 patch.object(video.subprocess, "run", side_effect=fake_run):
                video._normalize_seedance_upscale_video(
                    "video/upscaled.mp4", "16:9")

        video_filter = commands[0][commands[0].index("-vf") + 1]
        self.assertIn("force_original_aspect_ratio=increase", video_filter)
        self.assertIn("crop=1920:1080", video_filter)
        self.assertNotIn("pad=", video_filter)

    def test_wavespeed_submit_once_and_resume_only_gets(self):
        completed = {
            "data": {
                "status": "completed",
                "outputs": ["https://1.1.1.1/upscaled.mp4"],
            }
        }
        submitted = []
        with patch.object(wavespeed, "WAVESPEED_KEY", "test-key"), \
             patch.object(
                 wavespeed,
                 "_ws_req",
                 side_effect=[
                     {"code": 200, "data": {"id": "pred-1"}},
                     completed,
                 ],
             ) as request:
            result = wavespeed.run_seedvr2(
                "https://cos.example/source.mp4",
                on_submitted=submitted.append,
                now=lambda: 0,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(submitted, ["pred-1"])
        self.assertEqual(result["prediction_id"], "pred-1")
        self.assertEqual(
            [call.args[0] for call in request.call_args_list], ["POST", "GET"]
        )

        with patch.object(wavespeed, "WAVESPEED_KEY", "test-key"), \
             patch.object(wavespeed, "_ws_req", return_value=completed) as request:
            wavespeed.run_seedvr2(
                prediction_id="pred-1",
                now=lambda: 0,
                sleep=lambda _seconds: None,
            )
        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET"])
        with patch.object(wavespeed, "WAVESPEED_KEY", "test-key"), \
             patch.object(
                 wavespeed,
                 "_ws_req",
                 return_value={"code": 401, "message": "unauthorized"},
             ):
            with self.assertRaises(wavespeed.WaveSpeedQueryUnavailable):
                wavespeed.run_seedvr2(
                    prediction_id="pred-1",
                    now=lambda: 0,
                    sleep=lambda _seconds: None,
                )
        with patch.object(wavespeed, "WAVESPEED_KEY", "test-key"), \
             patch.object(wavespeed, "_ws_req", return_value={
                 "code": 200,
                 "data": {"status": "completed", "outputs": ["file:///etc/passwd"]},
             }):
            with self.assertRaises(wavespeed.WaveSpeedProviderFailed):
                wavespeed.run_seedvr2(
                    prediction_id="pred-1",
                    now=lambda: 0,
                    sleep=lambda _seconds: None,
                )
        with patch.object(wavespeed, "WAVESPEED_KEY", "test-key"), \
             patch.object(wavespeed, "_ws_req", return_value={
                 "code": 500, "message": "unknown",
             }):
            with self.assertRaises(wavespeed.WaveSpeedCreateOutcomeUnknown):
                wavespeed.run_seedvr2(
                    "https://cos.example/source.mp4",
                    now=lambda: 0,
                    sleep=lambda _seconds: None,
                )
        with patch.object(wavespeed, "WAVESPEED_KEY", "test-key"), \
             patch.object(wavespeed, "_ws_req", return_value={
                 "code": 408, "message": "unknown",
             }):
            with self.assertRaises(wavespeed.WaveSpeedCreateOutcomeUnknown):
                wavespeed.run_seedvr2(
                    "https://cos.example/source.mp4",
                    now=lambda: 0,
                    sleep=lambda _seconds: None,
                )
        self.assertFalse(video._is_public_http_url("http://127.0.0.1/private"))
        self.assertFalse(video._is_public_http_url("http://2130706433/private"))
        self.assertFalse(video._is_public_http_url("http://0x7f000001/private"))

    def test_prediction_id_is_persisted_in_existing_job_payload(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE jobs("
                "id INTEGER PRIMARY KEY,payload TEXT,status TEXT,updated_at INTEGER)"
            )
            connection.execute(
                "INSERT INTO jobs VALUES(7,?, 'running',0)",
                (json.dumps({"channel": "micro", "upscale": True}),),
            )
            connection.commit()
            connection.close()

            def db():
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                return conn

            with patch.object(video, "jdb", db):
                video._persist_seedance_upscale_prediction(7, "pred-7")
            connection = db()
            payload = json.loads(
                connection.execute(
                    "SELECT payload FROM jobs WHERE id=7"
                ).fetchone()["payload"]
            )
            connection.close()
            self.assertEqual(payload["upscale_prediction_id"], "pred-7")
        finally:
            os.unlink(path)

    def test_startup_never_requeues_second_submit_without_persisted_id(self):
        asset_handle, asset_path = tempfile.mkstemp(suffix=".db")
        job_handle, job_path = tempfile.mkstemp(suffix=".db")
        os.close(asset_handle)
        os.close(job_handle)
        try:
            asset = sqlite3.connect(asset_path)
            asset.execute(
                "CREATE TABLE video_assets("
                "job_id INTEGER,provider_video_id TEXT,model TEXT,phase TEXT,"
                "status TEXT,resolution TEXT,ratio TEXT)"
            )
            asset.execute(
                "INSERT INTO video_assets VALUES("
                "7,'seed-1',?,'seedance_upscale_submitting',"
                "'running','480p','16:9')",
                (video_seedance.SEEDANCE_MODEL,),
            )
            asset.commit()
            asset.close()

            jobs = sqlite3.connect(job_path)
            jobs.execute(
                "CREATE TABLE jobs("
                "id INTEGER,username TEXT,cost INTEGER,kind TEXT,status TEXT,"
                "owner TEXT,payload TEXT)"
            )
            jobs.execute(
                "INSERT INTO jobs VALUES("
                "7,'tester',120,'xiaole_video','running','content',?)",
                (json.dumps({"channel": "micro", "upscale": True}),),
            )
            jobs.commit()
            jobs.close()

            def db(path):
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                return conn

            requeue = Mock(return_value=True)
            terminal = Mock(return_value=True)
            with patch.object(video, "adb", side_effect=lambda: db(asset_path)), \
                 patch.object(video, "jdb", side_effect=lambda: db(job_path)):
                handled = startup_recovery.reclaim_orphaned_running(
                    jdb=lambda: db(job_path),
                    service_owner="content",
                    domains=lambda: (None, None, video),
                    set_terminal=terminal,
                    refund_once=Mock(),
                    mark_video_asset_failed=Mock(),
                    requeue_job=requeue,
                    logger=lambda *_args, **_kwargs: None,
                )
            self.assertEqual(handled, 0)
            requeue.assert_not_called()
            terminal.assert_not_called()

            asset = db(asset_path)
            asset.execute(
                "UPDATE video_assets SET phase=? WHERE job_id=7",
                ("seedance_upscale_recovery_required",),
            )
            asset.commit()
            asset.close()
            jobs = db(job_path)
            jobs.execute(
                "UPDATE jobs SET payload=? WHERE id=7",
                (json.dumps({
                    "channel": "micro",
                    "upscale": True,
                    "upscale_prediction_id": "pred-1",
                }),),
            )
            jobs.commit()
            jobs.close()
            with patch.object(video, "adb", side_effect=lambda: db(asset_path)), \
                 patch.object(video, "jdb", side_effect=lambda: db(job_path)):
                handled = startup_recovery.reclaim_orphaned_running(
                    jdb=lambda: db(job_path),
                    service_owner="content",
                    domains=lambda: (None, None, video),
                    set_terminal=terminal,
                    refund_once=Mock(),
                    mark_video_asset_failed=Mock(),
                    requeue_job=requeue,
                    logger=lambda *_args, **_kwargs: None,
                )
            self.assertEqual(handled, 1)
            requeue.assert_called_once_with(7)
        finally:
            os.unlink(asset_path)
            os.unlink(job_path)

    def test_full_chain_returns_ai_1080p_and_preserves_original_audio(self):
        rendered = {
            "request_id": "seed-1",
            "model": video_seedance.SEEDANCE_MODEL,
            "source_video_url": "https://seed.example/source.mp4",
            "duration": 4,
            "resolution": "480p",
            "ratio": "16:9",
            "generate_audio": True,
        }

        def run_seedvr2(**kwargs):
            kwargs["on_submitted"]("pred-1")
            kwargs["heartbeat"](kwargs["job_id"], "seedance_upscale_running")
            return {
                "prediction_id": "pred-1",
                "source_video_url": "https://wave.example/upscaled.mp4",
            }

        payload = self._body(_job_id=7, _username="tester")
        with patch.object(video, "get_resumable_grok_request", return_value=None), \
             patch.object(video.provider_keys, "claim_candidate", return_value={
                 "id": "seedance-key", "secret": "test-key"
             }), \
             patch.object(video.provider_keys, "set_health"), \
             patch.object(video, "update_video_asset_phase") as phase, \
             patch.object(video_seedance, "generate", return_value=rendered), \
             patch.object(
                 video, "_download_xiaole_video",
                 side_effect=["video/source.mp4", "video/upscaled.mp4"],
             ), \
             patch.object(
                 wavespeed, "_material_url",
                 return_value="https://cos.example/video/source.mp4",
             ), \
             patch.object(video, "_extract_reference_audio", return_value="audio/source.m4a"), \
             patch.object(video, "_persist_seedance_upscale_prediction") as persist, \
             patch.object(wavespeed, "run_seedvr2", side_effect=run_seedvr2), \
             patch.object(
                 video, "_normalize_seedance_upscale_video",
                 return_value="video/upscaled.mp4",
             ), \
             patch.object(
                 video,
                 "_mux_seedance_upscale_audio",
                 return_value="video/final.mp4",
             ) as mux, \
             patch.object(video, "_extract_first_frame_cover", return_value=None):
            result = video.gen_xiaole_video(payload)

        persist.assert_called_once_with(7, "pred-1")
        mux.assert_called_once_with("video/upscaled.mp4", "audio/source.m4a")
        self.assertEqual(result["resolution"], "1080p")
        self.assertEqual(result["source_resolution"], "480p")
        self.assertTrue(result["upscale"])
        self.assertNotIn("upscale_prediction_id", result)
        self.assertEqual(payload["upscale_prediction_id"], "pred-1")
        self.assertIn(
            "seedance_upscale_submitting",
            [call.args[1] for call in phase.call_args_list],
        )

    def test_unknown_second_submission_is_frozen_without_requeue(self):
        requeue = Mock(return_value=True)
        with patch.object(video, "get_resumable_grok_request", return_value={
            "request_id": "seed-1",
            "provider": "seedance",
            "phase": "seedance_upscale_submitting",
        }), patch.object(video, "update_video_asset_phase") as phase:
            held = video.recover_paid_video_error(
                7,
                "xiaole_video",
                self._body(),
                wavespeed.WaveSpeedCreateOutcomeUnknown("lost"),
                requeue,
                force_requeue=True,
            )
        self.assertTrue(held)
        requeue.assert_not_called()
        phase.assert_called_once_with(
            7, "seedance_upscale_recovery_required", error="lost"
        )

        requeue.reset_mock()
        with patch.object(
            video, "get_resumable_grok_request", return_value={
                "request_id": "seed-1",
                "provider": "seedance",
                "phase": "seedance_downloading",
            }
        ), patch.object(video, "update_video_asset_phase"):
            self.assertTrue(
                video.recover_paid_video_error(
                    7,
                    "xiaole_video",
                    self._body(),
                    wavespeed.WaveSpeedTransientRead("COS unavailable"),
                    requeue,
                )
            )
        requeue.assert_called_once_with(7)

    def test_upscale_keeps_silent_choice_silent(self):
        rendered = {
            "request_id": "seed-1",
            "model": video_seedance.SEEDANCE_MODEL,
            "source_video_url": "https://seed.example/source.mp4",
            "duration": 4,
            "resolution": "480p",
            "ratio": "16:9",
            "generate_audio": False,
        }
        payload = self._body(
            _job_id=7, _username="tester", generate_audio=False
        )
        with patch.object(video, "get_resumable_grok_request", return_value=None), \
             patch.object(video.provider_keys, "claim_candidate", return_value={
                 "id": "seedance-key", "secret": "test-key"
             }), \
             patch.object(video.provider_keys, "set_health"), \
             patch.object(video, "update_video_asset_phase"), \
             patch.object(video_seedance, "generate", return_value=rendered), \
             patch.object(
                 video, "_download_xiaole_video",
                 side_effect=["video/source.mp4", "video/upscaled.mp4"],
             ), \
             patch.object(
                 wavespeed, "_material_url",
                 return_value="https://cos.example/source.mp4",
             ), \
             patch.object(video, "_extract_reference_audio") as extract_audio, \
             patch.object(video, "_persist_seedance_upscale_prediction"), \
             patch.object(wavespeed, "run_seedvr2", return_value={
                 "prediction_id": "pred-1",
                 "source_video_url": "https://wave.example/upscaled.mp4",
             }), \
             patch.object(
                 video, "_normalize_seedance_upscale_video",
                 return_value="video/upscaled.mp4",
             ), \
             patch.object(video, "_mux_seedance_upscale_audio") as mux, \
             patch.object(video, "_extract_first_frame_cover", return_value=None):
            result = video.gen_xiaole_video(payload)

        extract_audio.assert_not_called()
        mux.assert_not_called()
        self.assertFalse(result["generate_audio"])

    def test_known_second_id_requeues_transient_but_terminal_failure_refunds(self):
        recovery = {
            "request_id": "seed-1",
            "provider": "seedance",
            "phase": "seedance_upscale_running",
        }
        requeue = Mock(return_value=True)
        payload = self._body(upscale_prediction_id="pred-1")
        with patch.object(
            video, "get_resumable_grok_request", return_value=recovery
        ), patch.object(video, "update_video_asset_phase") as phase:
            held = video.recover_paid_video_error(
                7,
                "xiaole_video",
                payload,
                wavespeed.WaveSpeedTransientRead("temporary"),
                requeue,
            )
        self.assertTrue(held)
        requeue.assert_called_once_with(7)
        phase.assert_called_once_with(
            7, "seedance_upscale_retrying", error="temporary"
        )

        requeue.reset_mock()
        self.assertFalse(
            video.recover_paid_video_error(
                7,
                "xiaole_video",
                payload,
                wavespeed.WaveSpeedProviderFailed("failed"),
                requeue,
            )
        )
        requeue.assert_not_called()

        with patch.object(
            video, "get_resumable_grok_request", return_value=recovery
        ), patch.object(video, "update_video_asset_phase") as phase:
            self.assertTrue(
                video.recover_paid_video_error(
                    7,
                    "xiaole_video",
                    payload,
                    ValueError("local state unavailable"),
                    requeue,
                )
            )
        phase.assert_called_once_with(
            7,
            "seedance_upscale_recovery_required",
            error="local state unavailable",
        )

    def test_frontend_exposes_quality_choice_health_and_progress(self):
        html = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")
        for expected in (
            'data-seedance-upscale="true"',
            "xlPayload.upscale=selectedSeedanceUpscale",
            "seedanceUpscaleAvailable=d.seedance_upscale_enabled===true",
            "seedance_upscale_normalizing",
            "seedance_upscale_retrying",
            "AI 超清测试期暂不加点",
        ):
            self.assertIn(expected, html)
        source = (ROOT / "server/content_domains/video.py").read_text(
            encoding="utf-8"
        )
        mux = source.split("def _mux_seedance_upscale_audio", 1)[1].split(
            "\ndef ", 1
        )[0]
        self.assertIn("apad", mux)
        self.assertIn('"-shortest"', mux)


if __name__ == "__main__":
    unittest.main()
