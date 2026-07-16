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


def _http_error(code, body=b'{}'):
    return urllib.error.HTTPError(
        "https://api.x.ai/v1/videos/rid-1", code, "error", {}, io.BytesIO(body)
    )


class XaiVideoTests(unittest.TestCase):
    def test_request_id_is_persisted_before_first_poll(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"request_id": "rid-early"}),
            _Response({"status": "done", "video": {
                "url": "https://vidgen.x.ai/v.mp4",
            }}),
        ]
        heartbeat = Mock()
        clock = iter([0, 0, 1])
        with patch.object(video_xai, "XAI_API_KEY", "test-key"), \
             patch.object(video_xai, "_opener", return_value=opener):
            video_xai.generate(
                "grok-imagine-video", "demo", 5, "9:16", "720p",
                job_id=7, heartbeat=heartbeat,
                now=lambda: next(clock), sleep=lambda _: None,
            )
        first = heartbeat.call_args_list[0]
        self.assertEqual(first.args[:2], (7, "xai_pending"))
        self.assertEqual(first.kwargs["provider_video_id"], "rid-early")

    def test_poll_retries_503_without_second_create(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"request_id": "rid-1"}),
            _http_error(503),
            _Response({"status": "pending"}),
            _Response({"status": "done", "video": {
                "url": "https://vidgen.x.ai/v.mp4", "duration": 5,
            }}),
        ]
        clock = iter([0, 0, 1, 2, 3, 4, 5])
        sleeps = []
        with patch.object(video_xai, "XAI_API_KEY", "test-key"), \
             patch.object(video_xai, "_opener", return_value=opener):
            result = video_xai.generate(
                "grok-imagine-video", "demo", 5, "9:16", "720p",
                now=lambda: next(clock), sleep=sleeps.append,
            )
        self.assertEqual(result["request_id"], "rid-1")
        self.assertEqual(opener.open.call_count, 4)
        self.assertEqual(sleeps[0], 5)
        create_calls = [
            call for call in opener.open.call_args_list
            if call.args[0].get_method() == "POST"
        ]
        self.assertEqual(len(create_calls), 1)

    def test_resume_polls_existing_id_without_post(self):
        opener = Mock()
        opener.open.return_value = _Response({"status": "done", "video": {
            "url": "https://vidgen.x.ai/resumed.mp4", "duration": 10,
        }})
        clock = iter([0, 0, 1])
        with patch.object(video_xai, "XAI_API_KEY", "test-key"), \
             patch.object(video_xai, "_opener", return_value=opener):
            result = video_xai.resume(
                "rid-existing", "grok-imagine-video", 10,
                now=lambda: next(clock), sleep=lambda _: None,
            )
        self.assertEqual(result["request_id"], "rid-existing")
        req = opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertTrue(req.full_url.endswith("/videos/rid-existing"))

    def test_poll_does_not_retry_terminal_400(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"request_id": "rid-1"}), _http_error(400)
        ]
        with patch.object(video_xai, "XAI_API_KEY", "test-key"), \
             patch.object(video_xai, "_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                video_xai.generate(
                    "grok-imagine-video", "demo", 5, "9:16", "720p"
                )
        self.assertEqual(opener.open.call_count, 2)

    def test_edit_payload_uses_official_edits_endpoint(self):
        opener = Mock()
        opener.open.side_effect = [_Response({"request_id": "edit-1"}), _Response({
            "status": "done", "video": {"url": "https://vidgen.x.ai/edit.mp4", "duration": 7.5}})]
        clock = iter([0, 0, 1])
        with patch.object(video_xai, "XAI_API_KEY", "test-key"), patch.object(video_xai, "_opener", return_value=opener):
            result = video_xai.edit("grok-imagine-video", "replace clothes", "https://cos.example/source.mp4", 7.5,
                                    now=lambda: next(clock), sleep=lambda _: None)
        req = opener.open.call_args_list[0].args[0]
        self.assertEqual(req.full_url, "https://api.x.ai/v1/videos/edits")
        self.assertEqual(json.loads(req.data), {"model": "grok-imagine-video", "prompt": "replace clothes",
                                                "video": {"url": "https://cos.example/source.mp4"}})
        self.assertEqual(result["duration"], 7.5)

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
