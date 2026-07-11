import sys
import unittest
from pathlib import Path
from unittest.mock import patch


class XiaoleVideoTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        from content_domains import video
        self.video = video

    def test_generate_xiaole_video_sends_size_without_aspect_ratio(self):
        calls = []

        def fake_request(method, path, body=None, timeout=90):
            calls.append((method, path, body, timeout))
            if method == "POST":
                return {"code": 200, "data": {"request_id": "rid-1", "status_url": "/status/rid-1"}}
            return {"data": {"status": "completed", "output": {"videos": [{"url": "https://cdn.example/video.mp4"}]}}}

        with patch.object(self.video, "_xiaole_request", side_effect=fake_request), \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_demo.mp4"), \
             patch.object(self.video, "GROK_VIDEO_PROVIDER", "xiaole"):
            result = self.video.generate_xiaole_video("Grok Image Video", "demo", size="1280x720", prefix="grok")

        self.assertEqual(result["video_file"], "video/grok_demo.mp4")
        self.assertEqual(calls[0][2]["input"]["size"], "1280x720")
        self.assertNotIn("aspect_ratio", calls[0][2]["input"])

    def test_gen_xiaole_video_maps_ratio_to_size_and_defaults_unknown_ratio(self):
        calls = []

        def fake_request(method, path, body=None, timeout=90):
            calls.append((method, path, body, timeout))
            if method == "POST":
                return {"code": 200, "data": {"request_id": "rid-1", "status_url": "/status/rid-1"}}
            return {"data": {"status": "completed", "output": {"videos": [{"url": "https://cdn.example/video.mp4"}]}}}

        with patch.object(self.video, "_xiaole_request", side_effect=fake_request), \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_demo.mp4"), \
             patch.object(self.video, "GROK_VIDEO_PROVIDER", "xiaole"):
            ok = self.video.gen_xiaole_video({"channel": "grok", "prompt": "demo", "ratio": "1:1"})
            fallback = self.video.gen_xiaole_video({"channel": "grok", "prompt": "demo", "ratio": "2:3"})

        self.assertEqual(ok["ratio"], "1:1")
        self.assertEqual(calls[0][2]["input"]["size"], "1024x1024")
        self.assertEqual(fallback["ratio"], "9:16")
        self.assertEqual(calls[2][2]["input"]["size"], "720x1280")
        self.assertNotIn("aspect_ratio", calls[0][2]["input"])
        self.assertNotIn("aspect_ratio", calls[2][2]["input"])

    def test_xiaole_ratio_channel_error_matches_supplier_size_message(self):
        self.assertTrue(self.video._is_xiaole_ratio_channel_error(
            '视频接口失败: HTTP 404 {"code":404,"message":"当前模型暂无支持该视频参数的可用渠道：渠道不支持当前视频尺寸"}'
        ))

    def test_generate_xiaole_video_normalizes_supplier_size_error(self):
        with patch.object(
            self.video,
            "_xiaole_request",
            side_effect=RuntimeError('视频接口失败: HTTP 404 {"code":404,"message":"当前模型暂无支持该视频参数的可用渠道：渠道不支持当前视频尺寸"}')
        ):
            with self.assertRaisesRegex(RuntimeError, "当前仅部分比例可用，请优先尝试 16:9（横屏）"):
                self.video.generate_xiaole_video("Grok Image Video", "demo", size="720x1280", prefix="grok")

    def test_validate_official_grok_parameters(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            body = self.video.validate_xiaole_video_payload({
                "channel": "grok", "prompt": "cinematic demo", "ratio": "2:3",
                "duration": 15, "resolution": "720p", "model": "grok-imagine-video",
            })
        self.assertEqual(body["ratio"], "2:3")
        self.assertEqual(body["duration"], 15)

    def test_validate_official_grok_rejects_multiple_references(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            with self.assertRaisesRegex(ValueError, "最多支持1张"):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "cinematic demo",
                    "reference_images": ["https://a/1.jpg", "https://a/2.jpg"],
                })

    def test_validate_video_15_requires_reference(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            with self.assertRaisesRegex(ValueError, "仅支持图生视频"):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "cinematic demo",
                    "model": "grok-imagine-video-1.5",
                })

    def test_gen_grok_official_preserves_result_contract(self):
        fake = {
            "request_id": "xai-1", "model": "grok-imagine-video",
            "source_video_url": "https://vidgen.x.ai/demo.mp4", "duration": 10,
        }
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate", return_value=fake) as generate, \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_xai_demo.mp4"), \
             patch.object(self.video, "_extract_first_frame_cover", return_value="video/grok_xai_demo_cover.jpg"), \
             patch.object(self.video, "public_url", return_value="https://cos.example/cover.jpg"):
            result = self.video.gen_xiaole_video({
                "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
            })
        self.assertEqual(result["video_file"], "video/grok_xai_demo.mp4")
        self.assertEqual(result["provider_video_id"], "xai-1")
        self.assertEqual(result["model"], "grok-imagine-video")
        self.assertEqual(result["duration"], 10)
        generate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
