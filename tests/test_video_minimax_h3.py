# -*- coding: utf-8 -*-
import base64
import io
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
    @staticmethod
    def _image(fmt="PNG", size=(256, 256)):
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", size, (40, 80, 120)).save(output, fmt)
        mime = "jpeg" if fmt == "JPEG" else fmt.lower()
        return "data:image/%s;base64,%s" % (
            mime, base64.b64encode(output.getvalue()).decode("ascii")
        )

    def test_reference_request_and_20_percent_markup(self):
        image = self._image()
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

    def test_credential_probe_reuses_the_accepted_task_list_endpoint(self):
        with patch.object(video_minimax_h3, "_request_json", return_value={}) as request:
            self.assertTrue(video_minimax_h3.check_credentials("test-only-secret", opener=object()))
        self.assertEqual("GET", request.call_args.args[1])
        self.assertEqual(
            "/v2/query/video_generation?page_num=1&page_size=1",
            request.call_args.args[2],
        )
        self.assertEqual("test-only-secret", request.call_args.kwargs["api_key"])

    def test_create_once_then_resume_only_queries(self):
        image = self._image()
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

    def test_jpeg_reference_is_normalized_to_clean_png(self):
        body = video_minimax_h3.build_request(
            "人物走进电梯", [self._image("JPEG", (257, 455))], duration=5
        )
        normalized = body["content"][1]["image_url"]["url"]
        self.assertTrue(normalized.startswith("data:image/png;base64,"))

    def test_invalid_image_and_provider_2013_are_user_readable(self):
        corrupt = "data:image/jpeg;base64," + base64.b64encode(b"not-jpeg").decode()
        with self.assertRaisesRegex(ValueError, "无法识别"):
            video_minimax_h3.build_request("人物走进电梯", [corrupt], duration=5)
        self.assertEqual(
            "麦克视频参考图无法识别，请重新上传 JPG 或 PNG 图片",
            video_minimax_h3._human_error(400, "media metadata is invalid (2013)"),
        )

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

    def test_shared_video_download_exhaustion_is_not_wrapped_as_transient(self):
        rendered = {
            "request_id": "h3-task-1", "source_video_url": "https://cdn.example/h3.mp4",
            "model": "MiniMax-H3", "duration": 5, "ratio": "9:16",
            "resolution": "2k", "provider": "minimax_h3_cn",
        }
        exhausted = video.CompletedVideoDownloadError("bounded download exhausted")
        with patch.object(video, "get_resumable_grok_request", return_value=None), \
                patch.object(video.provider_keys, "claim_candidate", return_value={"id": "mm-key", "secret": "secret"}), \
                patch.object(video.provider_keys, "set_health"), \
                patch.object(video, "update_video_asset_phase"), \
                patch.object(video_minimax_h3, "generate", return_value=rendered), \
                patch.object(video, "_download_xiaole_video", side_effect=exhausted):
            with self.assertRaises(video.CompletedVideoDownloadError) as caught:
                video.gen_xiaole_video({
                    "_job_id": 9000999, "channel": "minimax", "prompt": "actor opens door",
                    "model": "MiniMax-H3", "duration": 5, "ratio": "9:16",
                    "resolution": "2k", "reference_images": [],
                })
        self.assertIs(caught.exception, exhausted)

    def test_ui_has_separate_people_story_entry(self):
        html = (ROOT / "site" / "workbench" / "video.html").read_text(encoding="utf-8")
        self.assertIn('data-function="minimax"', html)
        self.assertIn("麦克视频", html)
        self.assertNotIn("MiniMax H3", html)
        self.assertIn("不是动作模仿", html)
        self.assertIn("setupXiaoleRefPanel('minimax', minimaxRefData, 5)", html)
        self.assertIn("p['video.minimax_h3.768p']||6", html)
        self.assertIn("xlPayload.model='MiniMax-H3'", html)
        self.assertNotIn("xlPayload.model='MiniMax-Hailuo-2.3'", html)


if __name__ == "__main__":
    unittest.main()
