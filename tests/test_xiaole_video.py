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

    def test_generate_xiaole_video_omits_aspect_ratio(self):
        # 果肉/Grok 视频模型不支持 aspect_ratio（实测 HTTP 422），input 里绝不能带该字段(#367 回归修复)
        calls = []

        def fake_request(method, path, body=None, timeout=90):
            calls.append((method, path, body, timeout))
            if method == "POST":
                return {"code": 200, "data": {"request_id": "rid-1", "status_url": "/status/rid-1"}}
            return {"data": {"status": "completed", "output": {"videos": [{"url": "https://cdn.example/video.mp4"}]}}}

        with patch.object(self.video, "_xiaole_request", side_effect=fake_request), \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_demo.mp4"):
            result = self.video.generate_xiaole_video("Grok Image Video", "demo", ratio="16:9", prefix="grok")

        self.assertEqual(result["video_file"], "video/grok_demo.mp4")
        self.assertNotIn("aspect_ratio", calls[0][2]["input"])

    def test_gen_xiaole_video_rejects_unknown_ratio(self):
        with self.assertRaises(ValueError) as cm:
            self.video.gen_xiaole_video({"channel": "grok", "prompt": "demo", "ratio": "2:3"})
        self.assertIn("ratio", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
