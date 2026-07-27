import base64
import io
import os
import random
import sys
import unittest
import urllib.error
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

    def test_other_wechat_error_fails_closed(self):
        with self.assertRaises(security.SecurityUnavailable):
            security._check_result({"errcode": 40001, "errmsg": "bad token"})

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
        url, payload = request.call_args.args
        self.assertEqual(url, security.API_BASE + "/cgi-bin/stable_token")
        self.assertEqual(payload, {
            "grant_type": "client_credential",
            "appid": "a",
            "secret": "s",
            "force_refresh": False,
        })

    def test_network_request_retries_twice(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"errcode": 0}'

        request = security.urllib.request.Request("https://example.test")
        with patch.object(
                security.urllib.request, "urlopen",
                side_effect=[urllib.error.URLError("one"), TimeoutError("two"), Response()]) as urlopen, \
             patch.object(security.time, "sleep") as sleep:
            self.assertEqual(security._request_json(request), {"errcode": 0})

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.2, 0.5])

    def test_text_check_refreshes_rejected_token_once(self):
        calls = []

        def request(url, payload=None, **_kwargs):
            calls.append((url, payload))
            if url.endswith("/cgi-bin/stable_token"):
                token = "new-token" if payload["force_refresh"] else "old-token"
                return {"access_token": token, "expires_in": 7200}
            if "old-token" in url:
                return {"errcode": 40001, "errmsg": "invalid credential"}
            return {"errcode": 0, "errmsg": "ok"}

        with patch.dict(os.environ, {"WX_MP_APPID": "a", "WX_MP_APPSECRET": "s"}, clear=True), \
             patch.object(security, "_json_request", side_effect=request):
            security.check_text("hello")

        stable_requests = [payload for url, payload in calls if url.endswith("/cgi-bin/stable_token")]
        self.assertEqual([payload["force_refresh"] for payload in stable_requests], [False, True])
        check_urls = [url for url, _payload in calls if "/wxa/msg_sec_check" in url]
        self.assertEqual(len(check_urls), 2)
        self.assertIn("old-token", check_urls[0])
        self.assertIn("new-token", check_urls[1])
        self.assertEqual(security._TOKEN_CACHE["value"], "new-token")

    def test_repeated_rejected_token_fails_closed_after_one_refresh(self):
        token_calls = []
        checks = []

        def token(force_refresh=False):
            token_calls.append(force_refresh)
            return "new-token" if force_refresh else "old-token"

        def request(url, _payload):
            checks.append(url)
            return {"errcode": 40014, "errmsg": "invalid access token"}

        with patch.object(security, "access_token", side_effect=token), \
             patch.object(security, "_json_request", side_effect=request), \
             patch.object(security, "_invalidate_access_token") as invalidate:
            with self.assertRaises(security.SecurityUnavailable):
                security.check_text("hello")

        self.assertEqual(token_calls, [False, True])
        self.assertEqual(len(checks), 2)
        invalidate.assert_called_once_with("old-token")

    def test_image_check_refreshes_rejected_token_once(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.body

        with patch.object(security, "access_token", side_effect=["old-token", "new-token"]) as token, \
             patch.object(security, "_invalidate_access_token") as invalidate, \
             patch.object(
                 security.urllib.request, "urlopen",
                 side_effect=[
                     Response(b'{"errcode":40001,"errmsg":"invalid credential"}'),
                     Response(b'{"errcode":0,"errmsg":"ok"}'),
                 ]) as urlopen:
            security.check_image(b"small", "upload.jpg", "image/jpeg")

        self.assertEqual(token.call_args_list[0].kwargs, {})
        self.assertEqual(token.call_args_list[1].kwargs, {"force_refresh": True})
        invalidate.assert_called_once_with("old-token")
        self.assertIn("old-token", urlopen.call_args_list[0].args[0].full_url)
        self.assertIn("new-token", urlopen.call_args_list[1].args[0].full_url)


if __name__ == "__main__":
    unittest.main()
