import base64
import unittest
from unittest.mock import patch

from server.content_domains import channel_manager, channel_runtime


class GeminiManagedAdapterTests(unittest.TestCase):
    def config(self):
        return {
            "adapter": "gemini_image",
            "model": "gemini-3.1-flash-image",
            "base_url": "https://generativelanguage.googleapis.com",
            "secret": "gemini-secret",
            "timeout": 30,
            "proxy": "",
        }

    def test_adapter_is_registered_as_reference_capable_image(self):
        self.assertEqual("image", channel_manager.ADAPTERS["gemini_image"]["kind"])
        self.assertTrue(channel_manager.ADAPTERS["gemini_image"]["references"])

    def test_request_uses_native_gemini_path_body_and_key_header(self):
        cfg = self.config()
        path, body, files = channel_runtime.build_generation_request(
            cfg, {"prompt": "画一只黄雀", "ratio": "9:16", "quality": "hd"}
        )
        self.assertEqual(
            "/v1beta/models/gemini-3.1-flash-image:generateContent", path
        )
        self.assertIsNone(files)
        self.assertEqual(["IMAGE"], body["generationConfig"]["responseModalities"])
        self.assertEqual("9:16", body["generationConfig"]["imageConfig"]["aspectRatio"])
        self.assertEqual("2K", body["generationConfig"]["imageConfig"]["imageSize"])

        pro = dict(cfg, model="gemini-3-pro-image")
        _path, pro_body, _files = channel_runtime.build_generation_request(
            pro, {"prompt": "画一只黄雀", "ratio": "1:1", "quality": "std"}
        )
        self.assertEqual("2K", pro_body["generationConfig"]["imageConfig"]["imageSize"])

        with patch.object(
            channel_runtime.safe_http,
            "request_json",
            return_value={"models": [{"name": "models/gemini-3.1-flash-image"}]},
        ) as request:
            result = channel_runtime.request(cfg, "GET", "/v1beta/models")
        self.assertIn("models", result)
        headers = request.call_args.kwargs["headers"]
        self.assertEqual("gemini-secret", headers["x-goog-api-key"])
        self.assertNotIn("Authorization", headers)

    def test_native_response_image_is_decoded(self):
        raw = b"fake-image-bytes"
        response = {
            "candidates": [{
                "content": {"parts": [{
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": base64.b64encode(raw).decode("ascii"),
                    }
                }]}
            }]
        }
        self.assertEqual(raw, channel_runtime._gemini_image_bytes(response))


if __name__ == "__main__":
    unittest.main()
