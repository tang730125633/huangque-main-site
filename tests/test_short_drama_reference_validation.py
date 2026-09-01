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

from content_domains import egress, short_drama
from content_domains import short_drama_reference_validation as validation


def response(has_character, framing_sufficient):
    return {
        "candidates": [{
            "content": {"parts": [{"text": json.dumps({
                "has_character": has_character,
                "framing_sufficient": framing_sufficient,
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

    def test_accepts_clear_human_animated_animal_robot_and_monster_characters(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch.object(
                egress, "post_json_idempotent", return_value=response(True, True),
        ) as post:
            result = validation.validate_character_reference(
                b"\x89PNG\r\n\x1a\npixels", "image/png",
            )

        self.assertEqual(
            {"has_character": True, "framing_sufficient": True}, result,
        )
        request_body = json.loads(post.call_args.args[3].decode("utf-8"))
        instruction = request_body["contents"][0]["parts"][1]["text"]
        for allowed in ("真人", "二维动漫", "插画", "3D卡通", "动物", "机器人", "怪兽"):
            with self.subTest(allowed=allowed):
                self.assertIn(allowed, instruction)

    def test_rejects_image_without_character_with_exact_message(self):
        with self.assertRaisesRegex(ValueError, "^请上传清晰的角色图片$"):
            self.validate_with(response(False, False))

    def test_rejects_character_without_sufficient_identity_features(self):
        with self.assertRaisesRegex(
                ValueError, "^请上传主体清晰、特征完整的角色参考图$"):
            self.validate_with(response(True, False))

    def test_provider_failure_has_temporary_detection_message(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch.object(
                egress, "post_json_idempotent", side_effect=TimeoutError(),
        ):
            with self.assertRaisesRegex(ValueError, "^角色图片检测暂时不可用，请稍后重新检测$"):
                validation.validate_character_reference(
                    b"\x89PNG\r\n\x1a\npixels", "image/png",
                )

    def test_rate_limit_and_auth_failures_have_distinct_messages(self):
        cases = (
            (429, "角色检测服务繁忙，请稍后重新检测"),
            (401, "角色检测服务配置异常，请联系管理员"),
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


class ShortDramaCharacterReferencePromptTests(unittest.TestCase):
    def test_prompt_supports_non_human_visual_identity(self):
        prompt = short_drama._character_reference_prompt({
            "name": "齿轮守卫",
            "identity_text": "蒸汽朋克机器人",
            "personality": "沉稳忠诚",
            "appearance_prompt": "铜制外壳，独眼蓝光，左肩齿轮徽记",
            "wardrobe_prompt": "深红披风，黑铁护肩",
        })

        self.assertNotIn("电影写实", prompt)
        self.assertIn("动物、机器人、怪兽或奇幻生物", prompt)
        self.assertIn("轮廓、材质、配色、纹理、标志性特征和装备", prompt)
        self.assertIn("正面全身、侧面全身、背面全身", prompt)


if __name__ == "__main__":
    unittest.main()
