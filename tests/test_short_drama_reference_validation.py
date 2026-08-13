import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import egress, short_drama_reference_validation as validation


def response(has_real_person, visible_extent):
    return {
        "candidates": [{
            "content": {"parts": [{"text": json.dumps({
                "has_real_person": has_real_person,
                "visible_extent": visible_extent,
            })}]},
        }],
    }


class ShortDramaReferenceValidationTests(unittest.TestCase):
    def validate_with(self, provider_response):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch.object(
                egress, "post_json_idempotent", return_value=provider_response,
        ):
            return validation.validate_character_reference(
                b"\x89PNG\r\n\x1a\npixels", "image/png",
            )

    def test_accepts_real_person_visible_to_waist_or_more(self):
        self.assertEqual(
            "half_body",
            self.validate_with(response(True, "half_body"))["visible_extent"],
        )
        self.assertEqual(
            "full_body",
            self.validate_with(response(True, "full_body"))["visible_extent"],
        )

    def test_rejects_non_person_with_exact_message(self):
        with self.assertRaisesRegex(ValueError, "^请上传人物图$"):
            self.validate_with(response(False, "full_body"))

    def test_rejects_headshot_with_specific_extent_message(self):
        for provider_response in (
                response(True, "head_only"), response(True, "upper_body")):
            with self.subTest(provider_response=provider_response):
                with self.assertRaisesRegex(
                        ValueError, "^请上传至少包含半身的人物图$"):
                    self.validate_with(provider_response)

    def test_provider_failure_has_temporary_detection_message(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch.object(
                egress, "post_json_idempotent", side_effect=TimeoutError(),
        ):
            with self.assertRaisesRegex(ValueError, "^人物图片检测暂时不可用，请稍后重新检测$"):
                validation.validate_character_reference(
                    b"\x89PNG\r\n\x1a\npixels", "image/png",
                )

    def test_rate_limit_and_auth_failures_have_distinct_messages(self):
        cases = (
            (429, "人物检测服务繁忙，请稍后重新检测"),
            (401, "人物检测服务配置异常，请联系管理员"),
        )
        for status, expected in cases:
            error = urllib.error.HTTPError(
                "https://example.test", status, "upstream error", {}, None,
            )
            with self.subTest(status=status), patch.dict(
                    os.environ, {"GEMINI_API_KEY": "test-key"}), patch.object(
                    egress, "post_json_idempotent", side_effect=error,
            ):
                with self.assertRaisesRegex(ValueError, "^%s$" % expected):
                    validation.validate_character_reference(
                        b"\x89PNG\r\n\x1a\npixels", "image/png",
                    )


if __name__ == "__main__":
    unittest.main()
