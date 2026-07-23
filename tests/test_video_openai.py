import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import video_openai


class _Response:
    def __init__(self, body=b"", headers=None):
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
        self._body = io.BytesIO(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self._body.read(size)


class _InterruptingResponse(_Response):
    def __init__(self, body):
        super().__init__(body)
        self._read_count = 0

    def read(self, size=-1):
        self._read_count += 1
        if self._read_count == 2:
            raise OSError("connection reset during download")
        return self._body.read(min(size, 7))


def _http_error(code, detail="error"):
    return urllib.error.HTTPError(
        "https://api.openai.com/v1/videos",
        code,
        "error",
        {},
        io.BytesIO(json.dumps({"error": {"message": detail}}).encode("utf-8")),
    )


def _headers(request):
    return {key.lower(): value for key, value in request.header_items()}


class OpenAIVideoAdapterTests(unittest.TestCase):
    def test_cross_origin_redirect_never_forwards_api_credentials(self):
        request = urllib.request.Request(
            "https://api.openai.com/v1/videos/video_1/content",
            headers={
                "Authorization": "Bearer secret",
                "Proxy-Authorization": "Basic proxy-secret",
                "Accept": "video/mp4",
            },
            method="GET",
        )
        redirected = video_openai._SafeRedirectHandler().redirect_request(
            request, None, 302, "Found", {},
            "https://download.example/video.mp4?signature=ok",
        )
        headers = {key.lower(): value for key, value in redirected.header_items()}
        self.assertNotIn("authorization", headers)
        self.assertNotIn("proxy-authorization", headers)
        self.assertEqual(headers.get("accept"), "video/mp4")

    def test_base_accepts_urls_with_or_without_v1(self):
        with patch.object(video_openai, "OPENAI_BASE", "https://relay.example/openai"):
            self.assertEqual(
                video_openai._api_url("/videos"),
                "https://relay.example/openai/v1/videos",
            )
        with patch.object(video_openai, "OPENAI_BASE", "https://relay.example/openai/v1/"):
            self.assertEqual(
                video_openai._api_url("/videos"),
                "https://relay.example/openai/v1/videos",
            )

    def test_generate_posts_twelve_second_json_once_persists_id_then_polls(self):
        events = []

        class Opener:
            def __init__(self):
                self.calls = []

            def open(self, request, timeout=None):
                self.calls.append((request, timeout))
                events.append(request.get_method())
                if request.get_method() == "POST":
                    return _Response({
                        "id": "video_early",
                        "status": "queued",
                        "model": "sora-2-pro",
                        "seconds": "12",
                        "size": "1280x720",
                    })
                return _Response({
                    "id": "video_early",
                    "status": "completed",
                    "model": "sora-2-pro",
                    "seconds": "12",
                    "size": "1280x720",
                })

        opener = Opener()

        def heartbeat(job_id, phase, **fields):
            events.append("heartbeat")
            self.assertEqual(job_id, 17)
            self.assertEqual(fields["provider_video_id"], "video_early")

        with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
             patch.object(video_openai, "OPENAI_BASE", "https://api.openai.com"), \
             patch.object(video_openai, "_opener", return_value=opener):
            result = video_openai.generate(
                "sora-2-pro",
                "demo",
                12,
                "1280x720",
                job_id=17,
                heartbeat=heartbeat,
                now=lambda: 0,
                sleep=lambda _delay: None,
            )

        self.assertEqual(events[:3], ["POST", "heartbeat", "GET"])
        posts = [call for call in opener.calls if call[0].get_method() == "POST"]
        self.assertEqual(len(posts), 1)
        request = posts[0][0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/videos")
        self.assertEqual(
            json.loads(request.data),
            {
                "model": "sora-2-pro",
                "prompt": "demo",
                "seconds": "12",
                "size": "1280x720",
            },
        )
        headers = _headers(request)
        self.assertEqual(headers["authorization"], "Bearer test-key")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertNotIn("idempotency-key", headers)
        self.assertEqual(result["video_id"], "video_early")
        self.assertEqual(result["model"], "sora-2-pro")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["seconds"], "12")
        self.assertEqual(result["size"], "1280x720")

    def test_provider_id_persistence_failure_stops_before_first_get(self):
        opener = Mock()
        opener.open.return_value = _Response({"id": "video_paid", "status": "queued"})
        with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
             patch.object(video_openai, "_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "asset db unavailable"):
                video_openai.generate(
                    "sora-2", "demo", 4, "720x1280", job_id=18,
                    heartbeat=Mock(side_effect=RuntimeError("asset db unavailable")),
                )
        self.assertEqual(opener.open.call_count, 1)

    def test_transient_get_retries_without_second_post(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"id": "video_1", "status": "queued"}),
            _http_error(503, "temporary outage"),
            _Response({"id": "video_1", "status": "in_progress"}),
            _Response({"id": "video_1", "status": "completed"}),
        ]
        sleeps = []
        heartbeat = Mock()
        with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
             patch.object(video_openai, "OPENAI_VIDEO_POLL_INTERVAL", 0), \
             patch.object(video_openai, "_opener", return_value=opener):
            result = video_openai.generate(
                "sora-2", "demo", "4", "1280x720",
                job_id=3, heartbeat=heartbeat,
                now=lambda: 0, sleep=sleeps.append,
            )

        methods = [call.args[0].get_method() for call in opener.open.call_args_list]
        self.assertEqual(methods.count("POST"), 1)
        self.assertEqual(methods.count("GET"), 3)
        self.assertIn(5, sleeps)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any(call.args[1] == "sora_retrying" for call in heartbeat.call_args_list))

    def test_invalid_get_json_retries_without_second_post(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"id": "video_1", "status": "queued"}),
            _Response(b"<html>temporary relay error</html>"),
            _Response({"id": "video_1", "status": "completed"}),
        ]
        with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
             patch.object(video_openai, "TRANSIENT_BACKOFF", (0,)), \
             patch.object(video_openai, "_opener", return_value=opener):
            result = video_openai.generate(
                "sora-2", "demo", "4", "1280x720",
                now=lambda: 0, sleep=lambda _delay: None,
            )
        methods = [call.args[0].get_method() for call in opener.open.call_args_list]
        self.assertEqual(methods, ["POST", "GET", "GET"])
        self.assertEqual(result["status"], "completed")

    def test_resume_only_uses_get(self):
        opener = Mock()
        opener.open.return_value = _Response({
            "id": "video_existing",
            "status": "completed",
            "model": "sora-2",
            "seconds": "4",
            "size": "1280x720",
        })
        with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
             patch.object(video_openai, "_opener", return_value=opener):
            result = video_openai.resume(
                "video_existing", "sora-2", "4", "1280x720",
                now=lambda: 0, sleep=lambda _delay: None,
            )
        self.assertEqual(result["video_id"], "video_existing")
        self.assertTrue(all(
            call.args[0].get_method() == "GET"
            for call in opener.open.call_args_list
        ))

    def test_create_transport_failure_is_never_retried(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("reset")
        with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
            patch.object(video_openai, "_opener", return_value=opener):
            with self.assertRaisesRegex(video_openai.CreateOutcomeUnknown, "网络异常"):
                video_openai.generate("sora-2", "demo", "4", "1280x720", job_id=1)
        self.assertEqual(opener.open.call_count, 1)

    def test_create_response_without_id_is_treated_as_unknown(self):
        opener = Mock()
        opener.open.return_value = _Response({"status": "queued"})
        with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
             patch.object(video_openai, "_opener", return_value=opener):
            with self.assertRaises(video_openai.CreateOutcomeUnknown):
                video_openai.generate("sora-2", "demo", "4", "1280x720", job_id=1)
        self.assertEqual(opener.open.call_count, 1)

    def test_http_errors_are_actionable(self):
        cases = (
            (401, video_openai.CreateRejected, "鉴权失败"),
            (403, video_openai.CreateRejected, "鉴权失败"),
            (402, video_openai.CreateRejected, "余额不足"),
            (429, video_openai.CreateRejected, "限流"),
            (503, video_openai.CreateOutcomeUnknown, "暂时不可用"),
        )
        for code, exception, message in cases:
            with self.subTest(code=code):
                opener = Mock()
                opener.open.side_effect = _http_error(code, "provider detail")
                with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
                    patch.object(video_openai, "_opener", return_value=opener):
                    with self.assertRaises(exception) as raised:
                        video_openai.generate("sora-2", "demo", "4", "1280x720", job_id=1)
                self.assertIn(message, str(raised.exception))
                self.assertIn("provider detail", str(raised.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_failed_status_exposes_provider_message(self):
        opener = Mock()
        opener.open.return_value = _Response({
            "id": "video_failed",
            "status": "failed",
            "error": {"message": "content policy rejected"},
        })
        with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
             patch.object(video_openai, "_opener", return_value=opener):
            with self.assertRaisesRegex(video_openai.ProviderVideoFailed, "content policy rejected"):
                video_openai.resume(
                    "video_failed", "sora-2", "4", "1280x720",
                    now=lambda: 0, sleep=lambda _delay: None,
                )


class OpenAIVideoDownloadTests(unittest.TestCase):
    MP4 = b"\x00\x00\x00\x18ftypmp42" + b"video-data" * 20

    def test_download_is_authenticated_streamed_and_atomic(self):
        opener = Mock()
        opener.open.return_value = _Response(
            self.MP4, {"Content-Length": str(len(self.MP4))}
        )
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "result.mp4"
            destination.write_bytes(b"old")
            with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
                 patch.object(video_openai, "OPENAI_BASE", "https://relay.example/v1"), \
                 patch.object(video_openai, "_DOWNLOAD_CHUNK_BYTES", 7), \
                 patch.object(video_openai, "_opener", return_value=opener):
                returned = video_openai.download_content(
                    "video/needs quoting", destination, max_bytes=len(self.MP4) + 1
                )

            self.assertEqual(returned, str(destination))
            self.assertEqual(destination.read_bytes(), self.MP4)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(
                request.full_url,
                "https://relay.example/v1/videos/video%2Fneeds%20quoting/content",
            )
            self.assertEqual(_headers(request)["authorization"], "Bearer test-key")

    def test_invalid_mp4_never_replaces_existing_file(self):
        opener = Mock()
        opener.open.return_value = _Response(b"not-an-mp4")
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "result.mp4"
            destination.write_bytes(b"known-good")
            with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
                 patch.object(video_openai, "_opener", return_value=opener):
                with self.assertRaisesRegex(ValueError, "ftyp"):
                    video_openai.download_content("video_bad", destination, max_bytes=100)
            self.assertEqual(destination.read_bytes(), b"known-good")
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_content_length_over_limit_is_rejected_before_writing(self):
        opener = Mock()
        opener.open.return_value = _Response(
            self.MP4, {"Content-Length": str(len(self.MP4))}
        )
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "result.mp4"
            destination.write_bytes(b"known-good")
            with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
                 patch.object(video_openai, "_opener", return_value=opener):
                with self.assertRaisesRegex(ValueError, "文件过大"):
                    video_openai.download_content("video_large", destination, max_bytes=8)
            self.assertEqual(destination.read_bytes(), b"known-good")

    def test_streaming_limit_is_enforced_without_content_length(self):
        opener = Mock()
        opener.open.return_value = _Response(self.MP4)
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "result.mp4"
            destination.write_bytes(b"known-good")
            with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
                 patch.object(video_openai, "_DOWNLOAD_CHUNK_BYTES", 5), \
                 patch.object(video_openai, "_opener", return_value=opener):
                with self.assertRaisesRegex(ValueError, "超过限制"):
                    video_openai.download_content("video_large", destination, max_bytes=20)
            self.assertEqual(destination.read_bytes(), b"known-good")
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_download_retries_first_transient_get_failure_then_succeeds(self):
        transient_errors = (
            ("http_503", lambda: _http_error(503, "temporary outage")),
            ("network", lambda: urllib.error.URLError("connection reset")),
        )
        for label, error_factory in transient_errors:
            with self.subTest(error=label), tempfile.TemporaryDirectory() as tmp:
                opener = Mock()
                opener.open.side_effect = [
                    error_factory(),
                    _Response(self.MP4),
                ]
                destination = Path(tmp) / "result.mp4"
                with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
                     patch.object(video_openai, "DOWNLOAD_TRANSIENT_BACKOFF", (0,)), \
                     patch.object(video_openai.time, "sleep") as sleep, \
                     patch.object(video_openai, "_opener", return_value=opener):
                    video_openai.download_content(
                        "video_retry", destination, max_bytes=len(self.MP4) + 1
                    )

                self.assertEqual(destination.read_bytes(), self.MP4)
                self.assertEqual(opener.open.call_count, 2)
                self.assertTrue(all(
                    call.args[0].get_method() == "GET"
                    for call in opener.open.call_args_list
                ))
                sleep.assert_called_once_with(0)

    def test_download_restarts_after_midstream_network_interruption(self):
        opener = Mock()
        opener.open.side_effect = [
            _InterruptingResponse(self.MP4),
            _Response(self.MP4),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "result.mp4"
            destination.write_bytes(b"known-good")
            with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
                 patch.object(video_openai, "DOWNLOAD_TRANSIENT_BACKOFF", (0,)), \
                 patch.object(video_openai.time, "sleep"), \
                 patch.object(video_openai, "_opener", return_value=opener):
                video_openai.download_content(
                    "video_interrupted", destination, max_bytes=len(self.MP4) + 1
                )

            self.assertEqual(destination.read_bytes(), self.MP4)
            self.assertEqual(opener.open.call_count, 2)
            self.assertTrue(all(
                call.args[0].get_method() == "GET"
                for call in opener.open.call_args_list
            ))
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_download_transient_retries_are_bounded(self):
        opener = Mock()
        opener.open.side_effect = [
            _http_error(503, "still unavailable"),
            urllib.error.URLError("connection reset"),
            _http_error(503, "still unavailable"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "result.mp4"
            destination.write_bytes(b"known-good")
            with patch.object(video_openai, "OPENAI_API_KEY", "test-key"), \
                 patch.object(video_openai, "DOWNLOAD_TRANSIENT_BACKOFF", (0, 0)), \
                 patch.object(video_openai.time, "sleep") as sleep, \
                 patch.object(video_openai, "_opener", return_value=opener):
                with self.assertRaisesRegex(
                    video_openai.TransientOpenAIError,
                    "重试耗尽.*3 次 GET",
                ):
                    video_openai.download_content(
                        "video_exhausted", destination, max_bytes=len(self.MP4) + 1
                    )

            self.assertEqual(destination.read_bytes(), b"known-good")
            self.assertEqual(opener.open.call_count, 3)
            self.assertTrue(all(
                call.args[0].get_method() == "GET"
                for call in opener.open.call_args_list
            ))
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
