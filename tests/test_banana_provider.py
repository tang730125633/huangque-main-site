import base64
import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import banana_provider, image, points


PNG_2X2 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGMU0bBhYGBg"
    "YgADAAWiAHylyrQdAAAAAElFTkSuQmCC"
)
JPEG_2X2 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8U"
    "HRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgN"
    "DRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
    "MjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQF"
    "BgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEI"
    "I0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNk"
    "ZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLD"
    "xMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEB"
    "AQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJB"
    "UQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZH"
    "SElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaan"
    "qKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oA"
    "DAMBAAIRAxEAPwDyOiiiuw5D/9k="
)
WEBP_2X2 = "UklGRh4AAABXRUJQVlA4TBEAAAAvAUAAAAdQlFKUp/+BiOh/AAA="


class BananaProviderTests(unittest.TestCase):
    def test_accepts_fourteen_references_and_builds_inline_parts_before_prompt(self):
        payload = banana_provider.validate_payload({
            "prompt": "cinematic frame",
            "model": "nb2",
            "quality": "hd",
            "ratio": "9:16",
            "count": 2,
            "images": [PNG_2X2] * 14,
        })
        body = banana_provider.build_request_body(
            payload["prompt"], payload["ratio"], payload["images"], "2K"
        )
        parts = body["contents"][0]["parts"]
        self.assertEqual(15, len(parts))
        self.assertTrue(all("inlineData" in part for part in parts[:14]))
        self.assertEqual({"text": "cinematic frame"}, parts[-1])

    def test_rejects_more_than_fourteen_references(self):
        with self.assertRaisesRegex(ValueError, "at most 14"):
            banana_provider.validate_payload({
                "prompt": "x", "images": [PNG_2X2] * 15,
            })

    def test_infers_real_mime_and_rejects_invalid_or_mismatched_images(self):
        payload = banana_provider.validate_payload({
            "prompt": "preserve identity",
            "images": [PNG_2X2, JPEG_2X2, WEBP_2X2],
        })
        self.assertEqual(
            ["image/png", "image/jpeg", "image/webp"],
            [item["mime_type"] for item in payload["images"]],
        )
        with self.assertRaisesRegex(ValueError, "cannot be decoded"):
            banana_provider.validate_payload({
                "prompt": "x",
                "images": [base64.b64encode(b"not-an-image").decode("ascii")],
            })
        with self.assertRaisesRegex(ValueError, "does not match"):
            banana_provider.validate_payload({
                "prompt": "x",
                "images": [{"data": JPEG_2X2, "mime_type": "image/png"}],
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
            "images": [PNG_2X2],
        })
        sent = generate.call_args.args[0]
        self.assertEqual(1, len(sent["images"]))
        self.assertEqual(PNG_2X2, sent["images"][0]["data"])

    @mock.patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}, clear=False)
    @mock.patch("content_domains.egress.post_json")
    def test_generation_returns_provider_model_and_reference_metadata(self, post_json):
        post_json.return_value = {
            "candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/png", "data": PNG_2X2}}
            ]}}]
        }
        with tempfile.TemporaryDirectory() as folder:
            result = banana_provider.generate({
                "prompt": "cinematic frame", "model": "nb2", "quality": "std",
                "ratio": "1:1", "count": 1, "images": [PNG_2X2],
            }, Path(folder), lambda name, _mime: "/file/" + name)
        self.assertEqual("banana", result["provider"])
        self.assertEqual("gemini-3.1-flash-image", result["model"])
        self.assertEqual(1, result["reference_count"])
        self.assertEqual("1K", result["image_size"])
        self.assertIsInstance(post_json.call_args.args[3], bytes)


if __name__ == "__main__":
    unittest.main()
