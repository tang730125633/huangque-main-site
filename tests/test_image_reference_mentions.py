# -*- coding: utf-8 -*-
import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

TEST_ROOT = tempfile.TemporaryDirectory()
os.environ["CONTENT_OUT"] = TEST_ROOT.name
os.environ["CONTENT_JOB_DB"] = str(Path(TEST_ROOT.name) / "jobs.db")

from content_domains import image, image_mentions
import imggen_api


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32
B64 = base64.b64encode(PNG).decode("ascii")


class MentionContractTests(unittest.TestCase):
    def test_aliases_resolve_and_missing_index_fails(self):
        self.assertEqual(
            "让 第1张参考图 穿上 第2张参考图 的衣服",
            image_mentions.resolve_image_mentions("让 @图片1 穿上 @图2 的衣服", 2),
        )
        self.assertEqual(
            "use <IMAGE_1> and <IMAGE_2>",
            image_mentions.resolve_image_mentions("use @图片1 and @图2", 2, "xai"),
        )
        self.assertEqual(
            "use <IMAGE_REF_0> and <IMAGE_REF_1>",
            image_mentions.resolve_image_mentions("use @图片1 and @图2", 2, "omni"),
        )
        with self.assertRaisesRegex(ValueError, "@图片3"):
            image_mentions.validate_image_mentions("参考 @图片3", 2)
        with self.assertRaisesRegex(ValueError, "编号从1开始"):
            image_mentions.validate_image_mentions("参考 @图片0", 2)

    def test_banana_accepts_14_and_builds_all_parts(self):
        payload = imggen_api.validate_banana_payload({
            "prompt": "参考 @图片14", "reference_images": [B64] * 14,
        })
        body = imggen_api._build_banana_body(
            image_mentions.resolve_image_mentions(payload["prompt"], 14),
            "1:1", payload["reference_images"], "1K",
        )
        parts = body["contents"][0]["parts"]
        self.assertEqual(14, len([part for part in parts if "inlineData" in part]))
        self.assertEqual("参考 第14张参考图", parts[-1]["text"])

    def test_openai_multi_edit_uses_array_multipart_field(self):
        captured = {}

        def dispatch(_provider, path, body, content_type, *_args, **_kwargs):
            captured.update(path=path, body=body, content_type=content_type)
            return {"data": [{"b64_json": B64}]}

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(image, "OUT_DIR", Path(tmp)), \
                mock.patch.object(image, "_dispatch_gpt", side_effect=dispatch), \
                mock.patch.object(image, "public_url", return_value="https://example.test/out.png"):
            result = image.gen_image({
                "prompt": "combine @图片1 and @图片2",
                "reference_images": [B64, B64],
            })
        self.assertEqual("/v1/images/edits", captured["path"])
        self.assertEqual(2, captured["body"].count(b'name="image[]"'))
        self.assertIn(b"combine \xe7\xac\xac1\xe5\xbc\xa0\xe5\x8f\x82\xe8\x80\x83\xe5\x9b\xbe", captured["body"])
        self.assertEqual("combine @图片1 and @图片2", result["prompt"])

    def test_workbenches_expose_numbered_reference_controls(self):
        banana = (ROOT / "site/workbench/banana.html").read_text(encoding="utf-8")
        video = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")
        self.assertIn("REF_LIMITS={banana:14,gpt:16,seedream:10,xiaole:4", banana)
        self.assertIn("bp.reference_images=refImages.map", banana)
        self.assertIn("点图片编号插入 @图片N", banana)
        self.assertIn("function insertImageMention(textarea,index)", video)
        self.assertIn("function grokRefLimit(){return GROK_REF_MAX;}", video)
        self.assertIn("setupXiaoleRefPanel('micro', microRefData, 9)", video)
        self.assertIn("setupXiaoleRefPanel('omni', omniRefData, 6)", video)


if __name__ == "__main__":
    unittest.main()
