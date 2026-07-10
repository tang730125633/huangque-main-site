import io
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import video


class HeyGenErrorTest(unittest.TestCase):
    def test_http_and_network_errors_are_readable_and_logged(self):
        cases = [
            (urllib.error.HTTPError("url", 403, "Forbidden", {}, io.BytesIO(b'{"error":"quota"}')), "HTTP 403", "[heygen] FAIL POST /videos"),
            (urllib.error.URLError("timed out"), "HeyGen接口网络失败: timed out", "network timed out"),
        ]
        for error, message, log in cases:
            with self.subTest(message=message), patch.object(video, "HEYGEN_API_KEY", "test-key"), \
                    patch.object(video.urllib.request, "urlopen", side_effect=error):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaisesRegex(RuntimeError, message):
                    video._heygen_request_json("POST", "/videos")
                self.assertIn(log, output.getvalue())

    def test_missing_key_fails_before_processing_payload(self):
        with patch.object(video, "HEYGEN_API_KEY", ""):
            with self.assertRaisesRegex(ValueError, "视频生成服务未配置"):
                video.gen_video({"mode": "text"})

    def test_avatar_poll_does_not_hide_request_error(self):
        error = RuntimeError("HeyGen接口失败: HTTP 401 invalid key")
        with patch.object(video, "_heygen_request_json", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                video._heygen_wait_photo_avatar("avatar-id")

    def test_provider_failure_is_logged_with_detail(self):
        result = {"data": {"status": "failed", "error": "quota exceeded"}}
        with patch.object(video, "_heygen_request_json", return_value=result):
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "quota exceeded"):
                video._heygen_poll_video("video-id")
            self.assertIn("[heygen] FAIL GET /videos/video-id", output.getvalue())

    def test_motion_duration_comes_from_reference_video(self):
        generated = {
            "video_file": "video/result.mp4",
            "video_id": "video-id",
            "avatar_item_id": "avatar-id",
            "avatar_group_id": "group-id",
        }
        with patch.object(video, "HEYGEN_API_KEY", "test-key"), \
                patch.object(video, "_save_data_file", side_effect=["image/input.jpg", "video/reference.mp4"]), \
                patch.object(video, "_motion_reference_duration", return_value=17.2), \
                patch.object(video, "update_video_asset_phase"), \
                patch.object(video, "record_video_avatar", return_value={"id": 7}), \
                patch.object(video, "public_url", return_value="https://example.test/result.mp4"), \
                patch.object(video, "generate_heygen_motion_video", return_value=generated) as create:
            result = video.gen_video({
                "mode": "motion",
                "image_data": "data:image/jpeg;base64,AA==",
                "reference_video_data": "data:video/mp4;base64,AA==",
                "duration": 10,
                "line": "1",
                "_username": "tester",
            })

        self.assertEqual(create.call_args.args[4], 18)
        self.assertEqual(result["duration"], 17.2)

    def test_motion_duration_rejects_provider_overflow(self):
        with patch.object(video, "_probe_video_duration", return_value=30.1):
            with self.assertRaisesRegex(ValueError, "线路一 HeyGen最长 30 秒"):
                video._motion_reference_duration("video/reference.mp4", "1")
        with patch.object(video, "_probe_video_duration", return_value=120.1):
            with self.assertRaisesRegex(ValueError, "线路二 WaveSpeed最长 120 秒"):
                video._motion_reference_duration("video/reference.mp4", "2")


if __name__ == "__main__":
    unittest.main()
