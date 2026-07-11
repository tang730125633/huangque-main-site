import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import video_xai


class _Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class XaiVideoTests(unittest.TestCase):
    def test_create_payload_and_poll_result(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"request_id": "rid-1"}),
            _Response({"status": "pending"}),
            _Response({"status": "done", "model": "grok-imagine-video", "video": {
                "url": "https://vidgen.x.ai/v.mp4", "duration": 5,
            }}),
        ]
        clock = iter([0, 0, 1, 2, 3])
        heartbeat = Mock()
        with patch.object(video_xai, "XAI_API_KEY", "test-key"), \
             patch.object(video_xai, "_opener", return_value=opener), \
             patch.object(video_xai, "XAI_VIDEO_POLL_INTERVAL", 0):
            result = video_xai.generate(
                "grok-imagine-video", "demo", 5, "9:16", "720p",
                image_url="https://cos.example/ref.jpg", job_id=7,
                heartbeat=heartbeat, now=lambda: next(clock), sleep=lambda _: None,
            )
        create_req = opener.open.call_args_list[0].args[0]
        payload = json.loads(create_req.data)
        self.assertEqual(create_req.full_url, "https://api.x.ai/v1/videos/generations")
        self.assertEqual(payload["image"], {"url": "https://cos.example/ref.jpg"})
        self.assertEqual(payload["duration"], 5)
        self.assertEqual(payload["aspect_ratio"], "9:16")
        self.assertEqual(result["source_video_url"], "https://vidgen.x.ai/v.mp4")
        self.assertEqual(opener.open.call_count, 3)
        heartbeat.assert_called()

    def test_create_network_failure_is_not_retried(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("reset")
        with patch.object(video_xai, "XAI_API_KEY", "test-key"), \
             patch.object(video_xai, "_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "网络异常"):
                video_xai.generate("grok-imagine-video", "demo", 5, "16:9", "480p")
        self.assertEqual(opener.open.call_count, 1)

    def test_http_402_has_actionable_message(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://api.x.ai/v1/videos/generations", 402, "payment", {},
            io.BytesIO(b'{"error":"insufficient credits"}'),
        )
        with patch.object(video_xai, "XAI_API_KEY", "test-key"), \
             patch.object(video_xai, "_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "余额不足"):
                video_xai.generate("grok-imagine-video", "demo", 5, "16:9", "480p")


if __name__ == "__main__":
    unittest.main()
