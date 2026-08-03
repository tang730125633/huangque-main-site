import base64
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch


class GeminiOmniTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server = str(Path(__file__).resolve().parents[1] / "server")
        if server not in sys.path:
            sys.path.insert(0, server)
        from content_domains import video_gemini_omni
        cls.omni = video_gemini_omni

    def image(self):
        return "data:image/png;base64," + base64.b64encode(b"png-data").decode()

    def test_request_uses_official_model_controls_and_prompt_duration(self):
        body = self.omni.build_request(
            "护肤品缓缓旋转", [self.image()], "9:16", 8, "uri"
        )
        self.assertEqual(body["model"], "gemini-omni-flash-preview")
        self.assertEqual(body["response_format"], {
            "type": "video", "aspect_ratio": "9:16",
            "duration": "8s", "delivery": "uri",
        })
        self.assertEqual(
            body["generation_config"]["video_config"]["task"], "image_to_video"
        )
        self.assertIn("[0-8s]", body["input"][-1]["text"])
        self.assertTrue(body["background"])
        self.assertTrue(body["store"])

    def test_text_and_multi_reference_tasks(self):
        text = self.omni.build_request("demo", [], "16:9", 3, "inline")
        refs = self.omni.build_request(
            "demo", [self.image(), self.image()], "16:9", 10, "inline"
        )
        self.assertIsInstance(text["input"], str)
        self.assertEqual(
            text["generation_config"]["video_config"]["task"], "text_to_video"
        )
        self.assertEqual(
            refs["generation_config"]["video_config"]["task"],
            "reference_to_video",
        )
        with self.assertRaisesRegex(ValueError, "最多支持 6 张"):
            self.omni.build_request(
                "demo", [self.image()] * 7, "16:9", 6, "inline"
            )

    def test_parameter_boundaries(self):
        for ratio in ("1:1", "4:5"):
            with self.assertRaisesRegex(ValueError, "比例仅支持"):
                self.omni.build_request("demo", aspect_ratio=ratio)
        for seconds in (2, 11, True):
            with self.assertRaisesRegex(ValueError, "3-10"):
                self.omni.build_request("demo", duration=seconds)
        with self.assertRaisesRegex(ValueError, "JPEG、PNG 或 WebP"):
            self.omni.build_request(
                "demo", ["data:image/gif;base64,R0lGODlh"], duration=3
            )

    def test_generate_accepts_inline_video_and_reports_measured_duration(self):
        payload = {
            "id": "v1_demo",
            "steps": [{
                "type": "model_output",
                "content": [{
                    "type": "video",
                    "mime_type": "video/mp4",
                    "data": base64.b64encode(b"fake-mp4").decode(),
                }],
            }],
        }
        with patch.object(self.omni, "_opener"), \
             patch.object(self.omni, "_request_json", return_value=payload), \
             patch.object(self.omni, "_probe_duration", return_value=7.25):
            result = self.omni.generate("demo", duration=7, delivery="inline")
        self.assertEqual(result["video_bytes"], b"fake-mp4")
        self.assertEqual(result["duration"], 7.25)
        self.assertTrue(result["duration_is_measured"])
        self.assertEqual(result["interaction_id"], "v1_demo")

    def test_generate_uses_configured_gemini_gateway(self):
        payload = {
            "id": "v1_gateway",
            "status": "completed",
            "steps": [{
                "type": "model_output",
                "content": [{
                    "type": "video",
                    "mime_type": "video/mp4",
                    "data": base64.b64encode(b"fake-mp4").decode(),
                }],
            }],
        }
        with patch.object(self.omni, "API_BASE", "https://gateway.example/gemini"), \
             patch.object(self.omni, "_opener"), \
             patch.object(self.omni, "_request_json", return_value=payload) as request, \
             patch.object(self.omni, "_probe_duration", return_value=3):
            self.omni.generate("demo", duration=3, delivery="inline")
        self.assertEqual(
            request.call_args.args[2],
            "https://gateway.example/gemini/v1beta/interactions",
        )

    def test_generate_polls_and_downloads_uri(self):
        uri = (
            "https://generativelanguage.googleapis.com/v1beta/"
            "files/file-1:download?alt=media"
        )
        payload = {
            "id": "v1_uri",
            "steps": [{
                "type": "model_output",
                "content": [{"type": "video", "mime_type": "video/mp4", "uri": uri}],
            }],
        }
        with patch.object(self.omni, "_opener"), \
             patch.object(self.omni, "_request_json", return_value=payload), \
             patch.object(self.omni, "_poll_file") as poll, \
             patch.object(self.omni, "_download_uri", return_value=b"video") as dl, \
             patch.object(self.omni, "_probe_duration", return_value=None):
            result = self.omni.generate("demo", duration=5)
        poll.assert_called_once()
        dl.assert_called_once()
        self.assertEqual(result["source_video_url"], uri)
        self.assertEqual(result["duration"], 5)
        self.assertFalse(result["duration_is_measured"])

    def test_resume_only_gets_existing_interaction(self):
        payload = {
            "id": "v1_existing",
            "status": "completed",
            "steps": [{
                "type": "model_output",
                "content": [{
                    "type": "video",
                    "mime_type": "video/mp4",
                    "data": base64.b64encode(b"existing-video").decode(),
                }],
            }],
        }
        with patch.object(self.omni, "_opener"), \
             patch.object(self.omni, "_request_json", return_value=payload) as request, \
             patch.object(self.omni, "_probe_duration", return_value=4):
            result = self.omni.resume("v1_existing", duration=4)
        self.assertEqual(request.call_args.args[1], "GET")
        self.assertEqual(result["request_id"], "v1_existing")

    def test_create_network_unknown_is_single_shot_and_does_not_leak_key(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("connection reset")
        with patch.object(self.omni, "GEMINI_API_KEY", "super-secret-key"):
            with self.assertRaises(
                    self.omni.GeminiOmniCreateOutcomeUnknown) as raised:
                self.omni._request_json(
                    opener, "POST",
                    "https://generativelanguage.googleapis.com/v1beta/interactions",
                    {"model": self.omni.MODEL},
                )
        self.assertEqual(opener.open.call_count, 1)
        self.assertNotIn("super-secret-key", str(raised.exception))
        self.assertIn("禁止自动重发", str(raised.exception))

    def test_all_create_5xx_are_unknown_and_not_refundable_rejection(self):
        for code in (501, 503, 599):
            with self.subTest(code=code):
                opener = Mock()
                opener.open.side_effect = urllib.error.HTTPError(
                    "https://generativelanguage.googleapis.com/v1beta/interactions",
                    code, "unavailable", {},
                    io.BytesIO(b'{"error":{"message":"busy"}}'),
                )
                with patch.object(self.omni, "GEMINI_API_KEY", "test-key"):
                    with self.assertRaises(
                        self.omni.GeminiOmniCreateOutcomeUnknown
                    ):
                        self.omni._request_json(
                            opener, "POST",
                            "https://generativelanguage.googleapis.com/v1beta/interactions",
                            {"model": self.omni.MODEL},
                        )
                self.assertEqual(opener.open.call_count, 1)

    def test_http_errors_are_human_readable_and_redacted(self):
        body = json.dumps({"error": {"message": "bad super-secret-key"}}).encode()
        error = urllib.error.HTTPError(
            "https://example.invalid", 403, "forbidden", {},
            io.BytesIO(body),
        )
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(self.omni, "GEMINI_API_KEY", "super-secret-key"):
            with self.assertRaises(self.omni.GeminiOmniRejected) as raised:
                self.omni._request_json(
                    opener, "POST",
                    "https://generativelanguage.googleapis.com/v1beta/interactions",
                    {"model": self.omni.MODEL},
                )
        self.assertIn("付费项目、API Key 和调用地区", str(raised.exception))
        self.assertNotIn("super-secret-key", str(raised.exception))

    def test_get_4xx_is_not_misclassified_as_terminal_provider_failure(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://generativelanguage.googleapis.com/v1beta/interactions/v1",
            403, "forbidden", {}, io.BytesIO(b'{"error":{"message":"denied"}}'),
        )
        with patch.object(self.omni, "GEMINI_API_KEY", "test-key"):
            with self.assertRaisesRegex(RuntimeError, "查询无法继续") as raised:
                self.omni._request_json(
                    opener, "GET",
                    "https://generativelanguage.googleapis.com/v1beta/interactions/v1",
                )
        self.assertNotIsInstance(raised.exception, self.omni.GeminiOmniRejected)
        self.assertNotIsInstance(
            raised.exception, self.omni.GeminiOmniProviderFailed
        )

    def test_rejects_non_google_download_uri(self):
        with self.assertRaisesRegex(RuntimeError, "非官方视频地址"):
            self.omni._file_name("https://attacker.example/files/steal:download")

    def test_official_file_download_uses_configured_gateway(self):
        uri = (
            "https://generativelanguage.googleapis.com/v1beta/"
            "files/file-1:download?alt=media"
        )
        with patch.object(self.omni, "API_BASE", "https://gateway.example/gemini"):
            self.assertEqual(
                self.omni._file_request_url(uri),
                "https://gateway.example/gemini/v1beta/"
                "files/file-1:download?alt=media",
            )

    def test_poll_file_retries_only_get(self):
        uri = (
            "https://generativelanguage.googleapis.com/v1beta/"
            "files/file-1:download?alt=media"
        )
        states = [
            self.omni.GeminiOmniTransientRead("temporary"),
            {"state": "PROCESSING"},
            {"state": "ACTIVE"},
        ]
        clock = [0]

        def now():
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(self.omni, "_request_json", side_effect=states):
            heartbeat = Mock()
            self.omni._poll_file(
                Mock(), uri, now=now, sleep=sleep, interaction_id="v1_demo",
                heartbeat=heartbeat,
            )
        self.assertEqual(clock[0], self.omni.POLL_INTERVAL * 2)
        self.assertTrue(
            all(call.args[1].startswith("omni_file_")
                for call in heartbeat.call_args_list)
        )

    def test_download_retry_exhaustion_stays_resumable(self):
        opener = Mock()
        with patch.object(
            self.omni, "_request",
            side_effect=self.omni.GeminiOmniTransientRead("temporary"),
        ):
            with self.assertRaises(self.omni.GeminiOmniTransientRead):
                self.omni._download_uri(
                    opener,
                    "https://generativelanguage.googleapis.com/v1beta/"
                    "files/file-1:download?alt=media",
                    sleep=lambda _seconds: None,
                )


if __name__ == "__main__":
    unittest.main()
