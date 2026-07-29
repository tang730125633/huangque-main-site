import base64
import io
import os
import random
import sys
import threading
import unittest
import urllib.parse
from unittest.mock import patch

try:
    from PIL import Image as PillowImage
except ImportError:
    PillowImage = None

ROOT = os.path.dirname(os.path.dirname(__file__))
if os.path.join(ROOT, "server") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "server"))

from content_domains import miniprogram_security as security


class MiniProgramSecurityTests(unittest.TestCase):
    def setUp(self):
        security._TOKEN_CACHE.update(value="", expires_at=0)

    def test_unconfigured_dev_environment_skips(self):
        with patch.dict(os.environ, {}, clear=True):
            security.check_payload({"prompt": "safe"})

    def test_payload_checks_text_and_data_images(self):
        png = base64.b64encode(b"png-bytes").decode()
        with patch.dict(os.environ, {"WX_MP_APPID": "a", "WX_MP_APPSECRET": "s"}, clear=True), \
             patch.object(security, "check_text") as check_text, \
             patch.object(security, "check_image") as check_image:
            security.check_payload({"prompt": "hello", "reference_image": "data:image/png;base64," + png})
        check_text.assert_called_once_with("hello")
        self.assertEqual(check_image.call_args.args[0], b"png-bytes")
        self.assertEqual(check_image.call_args.args[2], "image/png")

    def test_risky_result_is_rejected(self):
        with self.assertRaises(security.ContentRejected):
            security._check_result({"errcode": 87014, "errmsg": "risky content"})

    def test_invalid_token_error_raises_token_invalid(self):
        # 40001/40014/42001 走 _TokenInvalid，由 _with_token_retry 获取共享 token 重试，
        # 不再直接对用户报 503。
        for code in (40001, 40014, 42001):
            with self.assertRaises(security._TokenInvalid) as caught:
                security._check_result(
                    {"errcode": code, "errmsg": "bad token"}, token="used-token"
                )
            self.assertEqual(caught.exception.token, "used-token")

    def test_other_wechat_error_fails_closed(self):
        with self.assertRaises(security.SecurityUnavailable):
            security._check_result({"errcode": 40013, "errmsg": "invalid appid"})

    def test_image_media_size_error_is_not_reported_as_service_outage(self):
        with self.assertRaises(security.ContentRejected):
            security._check_result({"errcode": 40006, "errmsg": "invalid media size"}, image=True)

    def test_small_image_is_sent_unchanged(self):
        raw = b"small-image-bytes"
        review, content_type = security._prepare_image_for_security(raw, "image/webp")
        self.assertIs(review, raw)
        self.assertEqual(content_type, "image/webp")

    def test_large_image_uses_bounded_review_copy_without_pillow_dependency(self):
        class FakeImage:
            mode = "RGB"
            info = {}
            size = (1200, 1200)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def seek(self, _position):
                return None

            def convert(self, _mode):
                return self

            def load(self):
                return None

            def copy(self):
                return self

            def thumbnail(self, _size, _resampling):
                return None

            def save(self, output, **_kwargs):
                output.write(b"bounded-review-copy")

        class FakeImageModule:
            class Resampling:
                LANCZOS = object()

            @staticmethod
            def open(_source):
                return FakeImage()

        class FakeImageOps:
            @staticmethod
            def exif_transpose(image):
                return image

        raw = b"x" * (security._MAX_CHECK_IMAGE_BYTES + 1)
        with patch.object(security, "Image", FakeImageModule), \
             patch.object(security, "ImageOps", FakeImageOps):
            review, content_type = security._prepare_image_for_security(raw, "image/png")

        self.assertEqual(review, b"bounded-review-copy")
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(raw, b"x" * (security._MAX_CHECK_IMAGE_BYTES + 1))

    @unittest.skipIf(PillowImage is None, "Pillow is not installed")
    def test_large_image_gets_bounded_jpeg_review_copy(self):
        random_bytes = random.Random(7).randbytes(1200 * 1200 * 3)
        image = PillowImage.frombytes("RGB", (1200, 1200), random_bytes)
        original = io.BytesIO()
        image.save(original, format="JPEG", quality=100)
        raw = original.getvalue()
        self.assertGreater(len(raw), security._MAX_CHECK_IMAGE_BYTES)

        review, content_type = security._prepare_image_for_security(raw, "image/jpeg")

        self.assertEqual(content_type, "image/jpeg")
        self.assertLessEqual(len(review), security._MAX_CHECK_IMAGE_BYTES)
        self.assertEqual(original.getvalue(), raw)
        with PillowImage.open(io.BytesIO(review)) as checked:
            self.assertEqual(checked.format, "JPEG")

    def test_check_image_uploads_review_copy(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"errcode": 0, "errmsg": "ok"}'

        original = b"original-image-that-must-not-be-uploaded"
        review = b"bounded-review-copy"
        with patch.object(security, "_prepare_image_for_security", return_value=(review, "image/jpeg")), \
             patch.object(security, "access_token", return_value="token"), \
             patch.object(security.urllib.request, "urlopen", return_value=Response()) as urlopen:
            security.check_image(original, "upload.png", "image/png")

        request = urlopen.call_args.args[0]
        self.assertIn(review, request.data)
        self.assertNotIn(original, request.data)
        self.assertIn(b'filename="upload.jpg"', request.data)
        self.assertIn(b"Content-Type: image/jpeg", request.data)

    def test_access_token_is_cached(self):
        with patch.dict(os.environ, {"WX_MP_APPID": "a", "WX_MP_APPSECRET": "s"}, clear=True), \
             patch.object(security, "_json_request", return_value={"access_token": "tok", "expires_in": 7200}) as request:
            self.assertEqual(security.access_token(), "tok")
            self.assertEqual(security.access_token(), "tok")
        request.assert_called_once()


class StableTokenTests(unittest.TestCase):
    """稳定版 token + 40001 共享恢复（20260727 双机互打 40001 事故的根治）。

    旧版 /cgi-bin/token 每签发新 token 即让其他实例缓存 token 失效；稳定版
    /cgi-bin/stable_token 在 force_refresh=false 时多实例共享同一 token。
    """

    def setUp(self):
        security._TOKEN_CACHE.update(value="", expires_at=0)
        self.calls = []

        def fake_json_request(url, payload=None, headers=None, timeout=15):
            self.calls.append((url, dict(payload or {})))
            if "/cgi-bin/stable_token" in url:
                force = (payload or {}).get("force_refresh")
                return {"access_token": "tok-force" if force else "tok-shared",
                        "expires_in": 7200}
            if "msg_sec_check" in url:
                code = self.check_results.pop(0) if self.check_results else 0
                return {"errcode": code, "errmsg": "stub-%s" % code}
            raise AssertionError("unexpected url: " + url)

        self.fake_json_request = fake_json_request
        self.check_results = []
        self.env = patch.dict(os.environ, {"WX_MP_APPID": "a", "WX_MP_APPSECRET": "s"}, clear=True)
        self.env.start()
        self.req = patch.object(security, "_json_request", side_effect=fake_json_request)
        self.req.start()

    def tearDown(self):
        self.req.stop()
        self.env.stop()
        security._TOKEN_CACHE.update(value="", expires_at=0)

    def _token_payloads(self):
        return [p for url, p in self.calls if "/cgi-bin/stable_token" in url]

    def _check_count(self):
        return sum(1 for url, _ in self.calls if "msg_sec_check" in url)

    def test_token_uses_stable_endpoint_without_force_refresh(self):
        self.assertEqual(security.access_token(), "tok-shared")
        payloads = self._token_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["force_refresh"], False)
        self.assertEqual(payloads[0]["grant_type"], "client_credential")

    def test_check_text_recovers_from_40001_with_shared_stable_token(self):
        security._TOKEN_CACHE.update(
            value="tok-old", expires_at=int(security.time.time()) + 7200
        )
        self.check_results = [40001, 0]
        security.check_text("今天天气不错")
        self.assertEqual(
            [p["force_refresh"] for p in self._token_payloads()],
            [False],
        )
        self.assertEqual(self._check_count(), 2)

    def test_check_text_double_token_failure_becomes_unavailable(self):
        security._TOKEN_CACHE.update(
            value="tok-old", expires_at=int(security.time.time()) + 7200
        )
        self.check_results = [40001, 40014]
        with self.assertRaises(security.SecurityUnavailable):
            security.check_text("今天天气不错")
        self.assertEqual(self._check_count(), 2)

    def test_87014_rejected_without_retry(self):
        self.check_results = [87014]
        with self.assertRaises(security.ContentRejected):
            security.check_text("违规内容")
        self.assertEqual(self._check_count(), 1)

    def test_healthy_check_reuses_cached_token(self):
        self.check_results = [0, 0]
        security.check_text("第一段文本")
        security.check_text("第二段文本")
        self.assertEqual(len(self._token_payloads()), 1)
        self.assertEqual(self._check_count(), 2)

    def test_stale_failure_cannot_clear_a_newer_cached_token(self):
        security._TOKEN_CACHE.update(value="tok-new", expires_at=int(security.time.time()) + 7200)
        with patch.object(security, "_json_request") as request:
            self.assertEqual(security._refresh_invalid_token("tok-old"), "tok-new")
        request.assert_not_called()
        self.assertEqual(security._TOKEN_CACHE["value"], "tok-new")

    def test_concurrent_invalid_calls_share_one_refresh(self):
        security._TOKEN_CACHE.update(value="tok-old", expires_at=int(security.time.time()) + 7200)
        barrier = threading.Barrier(2)
        calls_lock = threading.Lock()
        token_requests = []

        def concurrent_request(url, payload=None, headers=None, timeout=15):
            if "/cgi-bin/stable_token" in url:
                with calls_lock:
                    token_requests.append(bool((payload or {}).get("force_refresh")))
                return {"access_token": "tok-new", "expires_in": 7200}
            if "msg_sec_check" in url:
                token = urllib.parse.parse_qs(
                    urllib.parse.urlparse(url).query
                )["access_token"][0]
                if token == "tok-old":
                    barrier.wait(timeout=3)
                    return {"errcode": 40001, "errmsg": "expired"}
                return {"errcode": 0, "errmsg": "ok"}
            raise AssertionError("unexpected url: " + url)

        errors = []

        def run_check():
            try:
                security.check_text("并发安全检测")
            except Exception as exc:
                errors.append(exc)

        with patch.object(security, "_json_request", side_effect=concurrent_request):
            workers = [threading.Thread(target=run_check) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5)

        self.assertFalse(errors)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(token_requests, [False])
        self.assertEqual(security._TOKEN_CACHE["value"], "tok-new")

    def test_same_rejected_shared_token_never_forces_cross_instance_refresh(self):
        security._TOKEN_CACHE.update(
            value="tok-shared", expires_at=int(security.time.time()) + 7200
        )
        self.check_results = [40001]

        with self.assertRaises(security.SecurityUnavailable):
            security.check_text("平台仍返回旧 token")

        self.assertEqual(
            [p["force_refresh"] for p in self._token_payloads()],
            [False],
        )
        self.assertEqual(security._TOKEN_CACHE["value"], "")


if __name__ == "__main__":
    unittest.main()
