import importlib
import sys
import unittest
from pathlib import Path


class ScriptToVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.script_to_video = importlib.import_module("content_domains.script_to_video")
        cls.video = importlib.import_module("content_domains.video")

    def setUp(self):
        self.orig_gen_video = self.video.gen_video
        self.orig_gen_xiaole_video = self.video.gen_xiaole_video
        self.orig_get_video_avatar = getattr(self.video, "get_video_avatar", None)
        self.orig_get_first_avatar = self.script_to_video._get_first_avatar

    def tearDown(self):
        self.video.gen_video = self.orig_gen_video
        self.video.gen_xiaole_video = self.orig_gen_xiaole_video
        if self.orig_get_video_avatar is not None:
            self.video.get_video_avatar = self.orig_get_video_avatar
        self.script_to_video._get_first_avatar = self.orig_get_first_avatar

    def test_drama_style_routes_to_grok_pipeline(self):
        calls = {}

        def fake_gen_xiaole_video(payload):
            calls["payload"] = payload
            return {"video_url": "https://example.test/drama.mp4"}

        self.video.gen_xiaole_video = fake_gen_xiaole_video
        self.script_to_video._get_first_avatar = lambda username: self.fail("剧情模式不应读取数字人形象")

        result = self.script_to_video.gen_script_to_video({
            "_username": "fang",
            "_job_id": 7,
            "style": "剧情",
            "scenes": [{"scene": "女生走进门店"}, {"scene": "镜头推近展示产品"}],
        })

        self.assertEqual(calls["payload"]["channel"], "grok")
        self.assertEqual(calls["payload"]["_username"], "fang")
        self.assertIn("女生走进门店", calls["payload"]["prompt"])
        self.assertIn("电影质感", calls["payload"]["prompt"])
        self.assertEqual(result["pipeline"], "grok")
        self.assertEqual(result["scene_count"], 2)

    def test_talking_style_uses_selected_avatar_id(self):
        calls = {}

        def fake_get_video_avatar(username, avatar_id):
            calls["avatar_lookup"] = (username, avatar_id)
            return {"id": avatar_id}

        def fake_gen_video(payload):
            calls["payload"] = payload
            return {"video_url": "https://example.test/talking.mp4"}

        self.video.get_video_avatar = fake_get_video_avatar
        self.video.gen_video = fake_gen_video
        self.script_to_video._get_first_avatar = lambda username: self.fail("显式 avatar_id 时不应回退到首个形象")

        result = self.script_to_video.gen_script_to_video({
            "_username": "fang",
            "_job_id": 8,
            "style": "种草",
            "avatar_id": "42",
            "scenes": [{"line": "第一句"}, {"line": "第二句"}],
        })

        self.assertEqual(calls["avatar_lookup"], ("fang", "42"))
        self.assertEqual(calls["payload"]["avatar_id"], "42")
        self.assertEqual(calls["payload"]["text"], "第一句\n\n第二句")
        self.assertEqual(result["pipeline"], "talking")
        self.assertEqual(result["type"], "script_to_video")


if __name__ == "__main__":
    unittest.main()
