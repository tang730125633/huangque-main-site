import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import video_seedance


class _Response:
    def __init__(self, body):
        self.body = io.BytesIO(json.dumps(body).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body.read()


def _http_error(code, detail):
    return urllib.error.HTTPError(
        "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks",
        code,
        "error",
        {},
        io.BytesIO(json.dumps({"error": {"message": detail}}).encode("utf-8")),
    )


class SeedanceVideoTests(unittest.TestCase):
    def test_reference_payload_rejects_local_and_malformed_asset_urls(self):
        for value in (
                "data:image/png;base64,AAAA", "file:///tmp/ref.png",
                "asset://reference/2", "asset://asset-a/b", "https:///missing-host"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "公网 URL|asset://"):
                    video_seedance._reference_item(value)

    def test_error_summary_redacts_signed_url_query_credentials(self):
        raw = (
            "failed to read https://bucket.example/path/ref.png?"
            "q-signature=top-secret&q-key-time=1; retry"
        )
        cleaned = video_seedance._safe_text(raw)
        self.assertIn("https://bucket.example/path/ref.png?[REDACTED]", cleaned)
        self.assertNotIn("top-secret", cleaned)
        self.assertNotIn("q-key-time", cleaned)

    def test_generate_sends_official_payload_once_then_returns_video_url(self):
        events = []

        class Opener:
            def __init__(self):
                self.calls = []

            def open(self, request, timeout=None):
                self.calls.append((request, timeout))
                events.append(request.get_method())
                if request.get_method() == "POST":
                    return _Response({"id": "cgt-1", "status": "queued"})
                return _Response(
                    {
                        "id": "cgt-1",
                        "model": video_seedance.SEEDANCE_MODEL,
                        "status": "succeeded",
                        "content": {"video_url": "https://cdn.example/out.mp4"},
                        "duration": 8,
                        "ratio": "16:9",
                        "resolution": "1080p",
                        "generate_audio": True,
                        "usage": {"completion_tokens": 123456},
                    }
                )

        opener = Opener()

        def heartbeat(job_id, phase, **fields):
            events.append("heartbeat")
            self.assertEqual(job_id, 9)
            self.assertEqual(fields["provider_video_id"], "cgt-1")

        with patch.object(video_seedance, "ARK_API_KEY", "test-key"), patch.object(
            video_seedance, "_opener", return_value=opener
        ):
            result = video_seedance.generate(
                prompt="镜头缓慢推进",
                duration=8,
                ratio="16:9",
                resolution="1080p",
                generate_audio=True,
                reference_images=[
                    "https://img.example/one.jpg",
                    "asset://asset-2",
                ],
                job_id=9,
                heartbeat=heartbeat,
                now=lambda: 0,
                sleep=lambda _delay: None,
            )

        self.assertEqual(events[:3], ["POST", "heartbeat", "GET"])
        posts = [
            request
            for request, _timeout in opener.calls
            if request.get_method() == "POST"
        ]
        self.assertEqual(len(posts), 1)
        body = json.loads(posts[0].data)
        self.assertEqual(body["model"], "doubao-seedance-2-0-260128")
        self.assertEqual(body["resolution"], "1080p")
        self.assertEqual(body["ratio"], "16:9")
        self.assertEqual(body["duration"], 8)
        self.assertIs(body["generate_audio"], True)
        self.assertEqual(
            [item.get("role") for item in body["content"][1:]],
            ["reference_image", "reference_image"],
        )
        self.assertEqual(result["source_video_url"], "https://cdn.example/out.mp4")
        self.assertEqual(result["completion_tokens"], 123456)

    def test_fast_is_explicit_and_never_accepts_1080p(self):
        with self.assertRaisesRegex(ValueError, "Fast 仅支持 480p 或 720p"):
            video_seedance._build_payload(
                video_seedance.SEEDANCE_FAST_MODEL,
                "demo",
                5,
                "9:16",
                "1080p",
                False,
            )
        payload = video_seedance._build_payload(
            video_seedance.SEEDANCE_FAST_MODEL,
            "demo",
            5,
            "9:16",
            "720p",
            False,
        )
        self.assertEqual(payload["model"], video_seedance.SEEDANCE_FAST_MODEL)

    def test_submit_network_error_is_unknown_and_not_retried(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("connection reset")
        with patch.object(video_seedance, "ARK_API_KEY", "test-key"), patch.object(
            video_seedance, "_opener", return_value=opener
        ):
            with self.assertRaisesRegex(
                video_seedance.CreateOutcomeUnknown, "请勿重复提交"
            ):
                video_seedance.generate(prompt="demo")
        self.assertEqual(opener.open.call_count, 1)

    def test_all_submit_5xx_are_unknown_and_not_retried(self):
        for code in (501, 599):
            with self.subTest(code=code):
                opener = Mock()
                opener.open.side_effect = _http_error(code, "temporary")
                with patch.object(
                    video_seedance, "ARK_API_KEY", "test-key"
                ), patch.object(video_seedance, "_opener", return_value=opener):
                    with self.assertRaises(video_seedance.CreateOutcomeUnknown):
                        video_seedance.generate(prompt="demo")
                self.assertEqual(opener.open.call_count, 1)

    def test_transient_poll_retries_get_without_second_post(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"id": "cgt-2"}),
            _http_error(503, "temporary"),
            _Response(
                {
                    "id": "cgt-2",
                    "status": "succeeded",
                    "content": {"video_url": "https://cdn.example/two.mp4"},
                }
            ),
        ]
        sleeps = []
        with patch.object(video_seedance, "ARK_API_KEY", "test-key"), patch.object(
            video_seedance, "_opener", return_value=opener
        ):
            result = video_seedance.generate(
                prompt="demo", now=lambda: 0, sleep=sleeps.append
            )
        methods = [
            call.args[0].get_method() for call in opener.open.call_args_list
        ]
        self.assertEqual(methods, ["POST", "GET", "GET"])
        self.assertIn(5, sleeps)
        self.assertEqual(result["request_id"], "cgt-2")

    def test_terminal_failure_is_human_readable(self):
        opener = Mock()
        opener.open.return_value = _Response(
            {
                "id": "cgt-3",
                "status": "failed",
                "error": {"message": "InputTextSensitiveContentDetected"},
            }
        )
        with patch.object(video_seedance, "ARK_API_KEY", "test-key"), patch.object(
            video_seedance, "_opener", return_value=opener
        ):
            with self.assertRaisesRegex(RuntimeError, "安全审核"):
                video_seedance.resume(
                    "cgt-3", now=lambda: 0, sleep=lambda _delay: None
                )
        self.assertEqual(
            [call.args[0].get_method() for call in opener.open.call_args_list],
            ["GET"],
        )

    def test_terminal_failure_never_exposes_pooled_api_key(self):
        secret = "Pooled-Seedance-Key"
        opener = Mock()
        opener.open.return_value = _Response(
            {
                "id": "cgt-secret",
                "status": "failed",
                "error": {"message": "provider rejected token=" + secret.swapcase()},
            }
        )
        with patch.object(video_seedance, "_opener", return_value=opener):
            with self.assertRaises(video_seedance.SeedanceProviderFailed) as caught:
                video_seedance.resume(
                    "cgt-secret", api_key=secret, now=lambda: 0,
                    sleep=lambda _delay: None,
                )
        self.assertNotIn(secret.lower(), str(caught.exception).lower())
        self.assertIn("***", str(caught.exception))

    def test_http_error_never_exposes_api_key(self):
        secret = "super-secret-key"
        opener = Mock()
        opener.open.side_effect = _http_error(
            400, "bad request token=" + secret
        )
        with patch.object(video_seedance, "ARK_API_KEY", secret), patch.object(
            video_seedance, "_opener", return_value=opener
        ):
            with self.assertRaises(RuntimeError) as caught:
                video_seedance.generate(prompt="demo")
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("***", str(caught.exception))

    def test_http_error_redacts_api_key_case_insensitively(self):
        secret = "CaseSensitive-Key"
        opener = Mock()
        opener.open.side_effect = _http_error(
            403, "invalid api key: " + secret.swapcase()
        )
        with patch.object(video_seedance, "ARK_API_KEY", secret), patch.object(
            video_seedance, "_opener", return_value=opener
        ):
            with self.assertRaises(video_seedance.SeedanceCredentialRejected) as caught:
                video_seedance.generate(prompt="demo")
        self.assertNotIn(secret.lower(), str(caught.exception).lower())
        self.assertIn("***", str(caught.exception))

    def test_common_provider_errors_are_translated(self):
        cases = [
            (401, "invalid api key", "鉴权失败"),
            (402, "insufficient balance", "余额不足"),
            (429, "rate limit", "并发繁忙"),
            (400, "InputImageSensitiveContentDetected", "安全审核"),
            (403, "model permission denied", "模型未开通"),
        ]
        for code, detail, expected in cases:
            with self.subTest(code=code):
                self.assertIn(expected, video_seedance._human_error(code, detail))

    def test_only_explicit_credentials_are_marked_for_key_isolation(self):
        cases = [
            (401, "token invalid", video_seedance.SeedanceCredentialRejected, "token invalid"),
            (402, "insufficient balance", video_seedance.SeedanceRejected, "insufficient balance"),
            (402, "insufficient balance; authentication required", video_seedance.SeedanceRejected, "authentication required"),
            (403, "model permission denied", video_seedance.SeedanceRejected, "model permission denied"),
            (403, "unauthorized model", video_seedance.SeedanceRejected, "unauthorized model"),
            (403, "content policy rejected", video_seedance.SeedanceRejected, "content policy rejected"),
            (403, "invalid api key", video_seedance.SeedanceCredentialRejected, "invalid api key"),
            (429, "rate limit", video_seedance.SeedanceRejected, "rate limit"),
        ]
        for code, detail, expected, summary in cases:
            with self.subTest(code=code, detail=detail):
                opener = Mock()
                opener.open.side_effect = _http_error(code, detail)
                with patch.object(video_seedance, "ARK_API_KEY", "test-key"), patch.object(
                    video_seedance, "_opener", return_value=opener
                ):
                    with self.assertRaises(video_seedance.SeedanceRejected) as caught:
                        video_seedance.generate(prompt="demo")
                self.assertIs(type(caught.exception), expected)
                self.assertIn(summary, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
