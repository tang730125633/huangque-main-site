import base64
import io
import os
import random
import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
