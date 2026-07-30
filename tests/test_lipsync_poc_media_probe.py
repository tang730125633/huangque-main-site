import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.lipsync_poc.metrics import media_probe
from tools.lipsync_poc.metrics.quality import (
    empty_human_review,
    media_contract_metrics,
)


class LipsyncPocMediaProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.media = Path(self.temp.name) / "sample.mp4"
        self.media.write_bytes(b"media")

    def tearDown(self):
        self.temp.cleanup()

    def test_command_is_shell_free_and_requests_streams(self):
        command = media_probe.build_ffprobe_command(self.media)
        self.assertIsInstance(command, list)
        self.assertIn("-show_streams", command)
        self.assertEqual(str(self.media), command[-1])

    def test_probe_normalizes_video_and_audio(self):
        payload = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 720,
                    "height": 1280,
                    "avg_frame_rate": "25/1",
                    "pix_fmt": "yuv420p",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 2,
                    "channel_layout": "stereo",
                },
            ],
            "format": {"duration": "5.04", "format_name": "mov,mp4"},
        }

        def runner(_command, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )

        report = media_probe.probe_media(self.media, runner=runner)
        self.assertEqual(5040, report["duration_ms"])
        self.assertEqual(25.0, report["video"]["fps"])
        self.assertEqual(48000, report["audio"]["sample_rate"])
        self.assertEqual(1, report["audio_stream_count"])

    def test_nonzero_exit_is_rejected(self):
        def runner(_command, **_kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="private")

        with self.assertRaises(media_probe.MediaProbeError):
            media_probe.probe_media(self.media, runner=runner)

    def test_invalid_json_is_rejected(self):
        def runner(_command, **_kwargs):
            return SimpleNamespace(returncode=0, stdout="{", stderr="")

        with self.assertRaises(media_probe.MediaProbeError):
            media_probe.probe_media(self.media, runner=runner)

    def test_contract_metrics_do_not_invent_human_scores(self):
        metrics = media_contract_metrics(
            {"duration_ms": 5000},
            {
                "duration_ms": 5070,
                "video_stream_count": 1,
                "audio_stream_count": 1,
                "video": {"width": 720, "height": 1280, "fps": 25},
            },
            {"width": 720, "height": 1280, "fps": 25},
        )
        self.assertEqual(70, metrics["duration_delta_ms"])
        self.assertTrue(metrics["resolution_matches"])
        self.assertEqual(1, metrics["output_audio_stream_count"])
        self.assertIsNone(empty_human_review()["lip_sync_score_1_to_5"])


if __name__ == "__main__":
    unittest.main()
