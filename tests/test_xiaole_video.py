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
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_demo.mp4"):
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
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_demo.mp4"):
            ok = self.video.gen_xiaole_video({"channel": "grok", "prompt": "demo", "ratio": "1:1"})
            fallback = self.video.gen_xiaole_video({"channel": "grok", "prompt": "demo", "ratio": "2:3"})

        self.assertEqual(ok["ratio"], "1:1")
        self.assertEqual(calls[0][2]["input"]["size"], "1024x1024")
        self.assertEqual(fallback["ratio"], "9:16")
        self.assertEqual(calls[2][2]["input"]["size"], "720x1280")
        self.assertNotIn("aspect_ratio", calls[0][2]["input"])
        self.assertNotIn("aspect_ratio", calls[2][2]["input"])


if __name__ == "__main__":
    unittest.main()
