# -*- coding: utf-8 -*-
import importlib
import json
import pathlib
import sys
import unittest
from unittest import mock


SERVER = pathlib.Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

text = importlib.import_module("content_domains.text")

REF_IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


class DirectorScriptDetailTests(unittest.TestCase):
    def test_reference_images_use_multimodal_channel_and_inherit_requirements(self):
        director_result = json.dumps({
            "scenes": [{
                "dur": "3s", "scene": "手持产品面向镜头", "line": "自然介绍",
                "shot": "中景正面机位", "camera": "固定", "lighting": "左侧自然光",
                "audio": "安静室内", "transition": "硬切",
            }],
        }, ensure_ascii=False)
        with mock.patch.object(
            text, "_director_chat_multimodal", return_value=director_result
        ) as multimodal, mock.patch.object(text, "_director_chat") as plain:
            result = text.gen_copy({
                "prompt": "介绍产品",
                "format": "script",
                "style": "口播",
                "dur": "30s",
                "platform": "抖音",
                "reference_images": [REF_IMAGE],
            })

        self.assertEqual("script", result["mode"])
        multimodal.assert_called_once()
        plain.assert_not_called()
        usermsg = multimodal.call_args.args[1]
        self.assertIn("参考图使用要求", usermsg)
        self.assertEqual([REF_IMAGE], multimodal.call_args.args[2])

    def test_sanitize_keeps_new_fields_and_replaces_banned_words(self):
        scenes = [{
            "dur": "3s",
            "scene": "超强质感的产品特写",
            "line": "超强效果",
            "shot": "超强近景",
            "camera": "超强推镜",
            "lighting": "超强侧光",
            "audio": "超强音效",
            "transition": "超强转场",
        }]
        cleaned = text.sanitize_script_scenes(scenes, "介绍产品")

        self.assertEqual(1, len(cleaned))
        item = cleaned[0]
        for field in ("scene", "line", "shot", "camera", "lighting", "audio", "transition"):
            self.assertIn(field, item)
            self.assertNotIn("超强", item[field])
            self.assertIn("良好", item[field])
        self.assertEqual("3s", item["dur"])

    def test_validate_rejects_fifth_reference_image_and_non_data_url(self):
        with self.assertRaisesRegex(ValueError, "最多 4 张"):
            text.validate_copy_payload({
                "prompt": "介绍产品",
                "reference_images": [REF_IMAGE] * 5,
            })
        with self.assertRaisesRegex(ValueError, "仅支持上传的图片文件"):
            text.validate_copy_payload({
                "prompt": "介绍产品",
                "reference_images": ["https://example.com/a.png"],
            })
        body = text.validate_copy_payload({
            "prompt": "介绍产品",
            "reference_images": [REF_IMAGE],
        })
        self.assertEqual([REF_IMAGE], body["reference_images"])

    def test_script_branch_parses_seven_field_scenes(self):
        director_result = json.dumps({
            "scenes": [{
                "dur": "3s",
                "scene": "产品置于窗边桌面上，主播右手入画拿起",
                "line": "先给大家看外观",
                "shot": "近景平视",
                "camera": "缓慢前推",
                "lighting": "左侧暖光",
                "audio": "翻页声",
                "transition": "顺动作硬切",
            }],
        }, ensure_ascii=False)
        with mock.patch.object(
            text, "_director_chat", return_value=director_result
        ) as director, mock.patch.object(text, "_director_chat_multimodal") as multimodal:
            result = text.gen_copy({
                "prompt": "介绍产品",
                "format": "script",
                "style": "口播",
                "dur": "30s",
                "platform": "抖音",
            })

        director.assert_called_once()
        multimodal.assert_not_called()
        scene = result["scenes"][0]
        for field in ("scene", "line", "shot", "camera", "lighting", "audio", "transition"):
            self.assertIn(field, scene)
        self.assertEqual("近景平视", scene["shot"])
        self.assertEqual("顺动作硬切", scene["transition"])

    def test_script_prompt_requests_seven_fields_and_200_300_chars(self):
        with mock.patch.object(
            text, "_director_chat", return_value='{"scenes":[{"dur":"3s","scene":"x","line":"y"}]}'
        ) as director:
            text.gen_copy({
                "prompt": "介绍产品",
                "format": "script",
            })

        usermsg = director.call_args.args[1]
        for field in ('"shot"', '"camera"', '"lighting"', '"audio"', '"transition"'):
            self.assertIn(field, usermsg)
        self.assertIn("200-300 字", usermsg)
        self.assertNotIn("80-140 字", usermsg)


if __name__ == "__main__":
    unittest.main()
