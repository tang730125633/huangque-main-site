import base64
import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import banana_provider, image, points


PNG_1X1 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8"
    b"\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND"
    b"\xaeB`\x82"
).decode("ascii")


class BananaProviderTests(unittest.TestCase):
    def test_accepts_five_references_and_builds_inline_parts_before_prompt(self):
        payload = banana_provider.validate_payload({
            "prompt": "cinematic frame",
            "model": "nb2",
            "quality": "hd",
            "ratio": "9:16",
            "count": 2,
            "images": [PNG_1X1] * 5,
        })
        body = banana_provider.build_request_body(
            payload["prompt"], payload["ratio"], payload["images"], "2K"
        )
        parts = body["contents"][0]["parts"]
        self.assertEqual(6, len(parts))
        self.assertTrue(all("inlineData" in part for part in parts[:5]))
        self.assertEqual({"text": "cinematic frame"}, parts[-1])

    def test_rejects_more_than_five_references(self):
        with self.assertRaisesRegex(ValueError, "at most 5"):
            banana_provider.validate_payload({
                "prompt": "x", "images": [PNG_1X1] * 6,
            })

    def test_short_drama_hd_batch_cost_is_seventy_points(self):
        self.assertEqual(70, points.cost_of("image", {
            "provider": "banana", "model": "nb2",
            "quality": "hd", "count": 2,
        }))

    def test_four_images_are_charged_as_four_images(self):
        self.assertEqual(140, points.cost_of("image", {
            "provider": "banana", "model": "nb2",
            "quality": "hd", "count": 4,
        }))

    @mock.patch("content_domains.banana_provider.generate")
    def test_content_image_route_preserves_client_reference_images(self, generate):
        generate.return_value = {"provider": "banana", "file": "result.png"}
        image.gen_image({
            "provider": "banana", "model": "nb2", "quality": "std",
            "prompt": "preserve identity", "ratio": "1:1", "count": 1,
            "images": [PNG_1X1],
        })
        sent = generate.call_args.args[0]
        self.assertEqual(1, len(sent["images"]))
        self.assertEqual(PNG_1X1, sent["images"][0]["data"])

    @mock.patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}, clear=False)
    @mock.patch("content_domains.egress.post_json")
    def test_generation_returns_provider_model_and_reference_metadata(self, post_json):
        post_json.return_value = {
            "candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/png", "data": PNG_1X1}}
            ]}}]
        }
        with tempfile.TemporaryDirectory() as folder:
            result = banana_provider.generate({
                "prompt": "cinematic frame", "model": "nb2", "quality": "std",
                "ratio": "1:1", "count": 1, "images": [PNG_1X1],
            }, Path(folder), lambda name, _mime: "/file/" + name)
        self.assertEqual("banana", result["provider"])
        self.assertEqual("gemini-3.1-flash-image", result["model"])
        self.assertEqual(1, result["reference_count"])
        self.assertEqual("1K", result["image_size"])
        self.assertIsInstance(post_json.call_args.args[3], bytes)


if __name__ == "__main__":
    unittest.main()
