import base64
import os
import sys
import unittest
from unittest.mock import patch

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

    def test_access_token_is_cached(self):
        with patch.dict(os.environ, {"WX_MP_APPID": "a", "WX_MP_APPSECRET": "s"}, clear=True), \
             patch.object(security, "_json_request", return_value={"access_token": "tok", "expires_in": 7200}) as request:
            self.assertEqual(security.access_token(), "tok")
            self.assertEqual(security.access_token(), "tok")
        request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
