# -*- coding: utf-8 -*-
import base64
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))
from content_domains import breakdown
try:
    from PIL import Image
except ImportError:
    Image = None


class BreakdownFollowupTests(unittest.TestCase):
    def test_parser_recovers_json_from_unclosed_code_fence(self):
        parsed = breakdown._parse_breakdown_json(
            '```json\n{"scenes":[{"scene":"完整镜头","line":""}]}'
        )
        self.assertEqual(parsed["scenes"][0]["scene"], "完整镜头")

    def test_validation_rejects_empty_and_placeholder_scenes(self):
        with self.assertRaisesRegex(ValueError, "为空"):
            breakdown._validate_scene_breakdown({"scenes": []})
        with self.assertRaisesRegex(ValueError, "占位"):
            breakdown._validate_scene_breakdown(
                {"scenes": [{"scene": "具体画面", "line": ""}]}
            )

    def test_breakdown_retries_malformed_model_output(self):
        valid = json.dumps(
            {"scenes": [{"scene": "人物从桌边起身走向窗前，镜头缓慢跟随", "line": ""}]},
            ensure_ascii=False,
        )
        with mock.patch.object(
            breakdown, "_chat_multimodal", side_effect=["not json", valid]
        ) as chat:
            result = breakdown._request_breakdown_result("system", "user", "context", [])
        self.assertEqual(len(result["scenes"]), 1)
        self.assertEqual(chat.call_count, 2)

    def test_multimodal_timeout_is_localized_after_safe_retries(self):
        with mock.patch.object(
            breakdown.egress, "post_json_idempotent",
            side_effect=TimeoutError("The read operation timed out"),
        ):
            with self.assertRaisesRegex(RuntimeError, "AI 分析响应超时"):
                breakdown._chat_multimodal("system", "user", [])

    def test_frame_thumbnails_are_embedded_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            image = pathlib.Path(directory) / "frame.jpg"
            image.write_bytes(b"\xff\xd8\xff\xd9")
            thumbs = breakdown._frame_thumbnails([str(image)])
        self.assertEqual(thumbs, [])

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_large_source_is_resized_and_bounded_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "large.png"
            Image.new("RGB", (3000, 2000), (35, 120, 210)).save(source, "PNG")
            original_size = source.stat().st_size
            thumbs = breakdown._frame_thumbnails([str(source)])

        self.assertEqual(len(thumbs), 1)
        self.assertTrue(thumbs[0].startswith("data:image/jpeg;base64,"))
        raw = base64.b64decode(thumbs[0].split(",", 1)[1])
        self.assertLessEqual(len(raw), breakdown._THUMBNAIL_MAX_BYTES)
        self.assertNotEqual(len(raw), original_size)
        with Image.open(io.BytesIO(raw)) as thumbnail:
            self.assertLessEqual(max(thumbnail.size), breakdown._THUMBNAIL_MAX_EDGE)


if __name__ == "__main__":
    unittest.main()
