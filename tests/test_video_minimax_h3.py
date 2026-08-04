# -*- coding: utf-8 -*-
import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import points, video, video_minimax_h3  # noqa: E402


class MiniMaxH3VideoTests(unittest.TestCase):
    def test_reference_request_and_20_percent_markup(self):
        image = "data:image/png;base64," + base64.b64encode(b"png").decode()
        body = video_minimax_h3.build_request(
            "第1张参考图仅作为人物身份参考", [image], "9:16", 15, "768p"
        )
        self.assertEqual(body["model"], "MiniMax-H3")
        self.assertEqual(body["resolution"], "768P")
        self.assertEqual(body["content"][1]["role"], "reference_image")
        with patch("content_domains.points.pricing.get_price", return_value=6):
            self.assertEqual(points.cost_of("xiaole_video", {
                "channel": "minimax", "duration": 15, "resolution": "768p",
            }), 90)

    def test_create_once_then_resume_only_queries(self):
        image = "data:image/png;base64," + base64.b64encode(b"png").decode()
        succeeded = {"task": {
            "status": "succeeded", "content": {"url": "https://cdn.example/h3.mp4"},
            "duration": 5, "ratio": "9:16",
        }}
        calls = []

        def request(_opener, method, path, body=None, timeout=90, api_key=None):
            calls.append((method, path))
            return {"task_id": "h3-task-1"} if method == "POST" else succeeded

        with patch.object(video_minimax_h3, "_request_json", side_effect=request), \
                patch.object(video_minimax_h3, "_opener", return_value=object()):
            created = video_minimax_h3.generate(
                "人物走进电梯", [image], duration=5, api_key="secret", sleep=lambda _s: None
            )
            resumed = video_minimax_h3.resume(
                "h3-task-1", duration=5, api_key="secret", sleep=lambda _s: None
            )
        self.assertEqual(created["source_video_url"], "https://cdn.example/h3.mp4")
        self.assertEqual(resumed["request_id"], "h3-task-1")
        self.assertEqual([method for method, _path in calls], ["POST", "GET", "GET"])

    def test_shared_video_job_uses_minimax_adapter(self):
        rendered = {
            "request_id": "h3-task-1", "source_video_url": "https://cdn.example/h3.mp4",
            "model": "MiniMax-H3", "duration": 15, "ratio": "9:16",
            "resolution": "768p", "provider": "minimax_h3_cn",
        }
        with patch.object(video, "get_resumable_grok_request", return_value=None), \
                patch.object(video.provider_keys, "claim_candidate", return_value={"id": "mm-key", "secret": "secret"}), \
                patch.object(video.provider_keys, "set_health"), \
                patch.object(video, "update_video_asset_phase"), \
                patch.object(video_minimax_h3, "generate", return_value=rendered) as generate, \
                patch.object(video, "_download_xiaole_video", return_value="video/h3.mp4"), \
                patch.object(video, "_extract_first_frame_cover", return_value=None), \
                patch.object(video, "public_url", return_value="https://cos.example/h3.mp4"):
            result = video.gen_xiaole_video({
                "_job_id": 8, "channel": "minimax", "prompt": "人物走进电梯",
                "model": "MiniMax-H3", "duration": 15, "ratio": "9:16",
                "resolution": "768p", "reference_images": ["data:image/png;base64,cG5n"],
            })
        generate.assert_called_once()
        self.assertEqual(result["provider_video_id"], "h3-task-1")
        self.assertEqual(result["provider"], "minimax_h3_cn")

    def test_ui_has_separate_people_story_entry(self):
        html = (ROOT / "site" / "workbench" / "video.html").read_text(encoding="utf-8")
        self.assertIn('data-function="minimax"', html)
        self.assertIn("麦克视频", html)
        self.assertNotIn("MiniMax H3", html)
        self.assertIn("不是动作模仿", html)
        self.assertIn("setupXiaoleRefPanel('minimax', minimaxRefData, 5)", html)
        self.assertIn("p['video.minimax_h3.768p']||6", html)


if __name__ == "__main__":
    unittest.main()
