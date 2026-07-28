# -*- coding: utf-8 -*-
import base64
import http.client
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))
from content_domains import breakdown
try:
    from PIL import Image
except ImportError:
    Image = None


class BreakdownFollowupTests(unittest.TestCase):
    def test_tikhub_millisecond_duration_is_normalized_to_seconds(self):
        self.assertAlmostEqual(
            breakdown._normalize_duration_seconds(10034), 10.034, places=3
        )
        self.assertEqual(breakdown._normalize_duration_seconds(23), 23)
        self.assertEqual(
            breakdown._normalize_duration_seconds(None, fallback=30), 30
        )

    def test_link_reverse_uses_downloaded_media_duration_for_frames(self):
        fake_tikhub = mock.Mock()
        fake_tikhub.detail.return_value = {
            "play_url": "https://cdn.example/video.mp4",
            "duration": 10034,
            "title": "duration unit regression",
        }
        fake_tikhub.download_to_file.return_value = None
        fake_tikhub.transcript.return_value = []
        with mock.patch.dict(sys.modules, {"tikhub": fake_tikhub}), \
             mock.patch.object(breakdown, "_probe_duration", return_value=10.034), \
             mock.patch.object(
                 breakdown, "_extract_frames", return_value=(None, ["frame.jpg"])
             ) as extracted, \
             mock.patch.object(
                 breakdown, "_reverse_from_frames",
                 return_value={"type": "breakdown_reverse"},
             ) as reversed_from_frames:
            result = breakdown._do_breakdown(
                {"mode": "reverse_prompt", "_job_id": 7},
                {"platform": "douyin", "id": "123", "note_type": "video"},
                "https://www.douyin.com/video/123",
            )

        self.assertEqual(result["type"], "breakdown_reverse")
        self.assertEqual(extracted.call_args.args[1:], (4, 10.034))
        self.assertEqual(reversed_from_frames.call_args.args[-1], 10.034)
        self.assertEqual(
            reversed_from_frames.call_args.kwargs["script_text"],
            "",
        )

    def test_reverse_prompt_uses_duration_transcript_and_timeline_sections(self):
        captured = {}

        def fake_chat(system_message, user_message, frames, temp=0.7):
            captured.update(
                system=system_message,
                user=user_message,
                frames=frames,
            )
            return json.dumps({
                "prompt": (
                    "[00:00-00:03] 主体从画面左侧进入。\n"
                    "[00:03-00:07] 镜头跟随主体向前移动。"
                )
            }, ensure_ascii=False)

        with mock.patch.object(
            breakdown, "_chat_multimodal", side_effect=fake_chat
        ):
            result = breakdown._reverse_from_frames(
                {},
                ["frame-1.jpg", "frame-2.jpg"],
                title="测试视频",
                platform="douyin",
                duration=7,
                script_text="[0s-3s] 开场口播",
            )

        self.assertIn("[00:00-00:03]", result["prompt"])
        for expected in (
            "总时长：7.0 秒",
            "[0s-3s] 开场口播",
            "连续、不重叠、无空档",
            "每段用 80-160 字",
            "动作起点、连续过程、终点",
            "各段之间用换行分隔",
        ):
            self.assertIn(expected, captured["user"])
        self.assertIn("不臆造", captured["system"])

    def test_download_timeout_refreshes_detail_and_retries_once(self):
        fake_tikhub = mock.Mock()
        stale = {
            "play_url": "https://stale.example/video.mp4",
            "duration": 10034,
        }
        fresh = {
            "play_url": "https://fresh.example/video.mp4",
            "duration": 10,
        }
        fake_tikhub.download_to_file.side_effect = [
            TimeoutError("下载超过预算（已下载 1.0MB）"),
            None,
        ]
        fake_tikhub.detail.return_value = fresh

        result = breakdown._download_breakdown_video(
            fake_tikhub,
            {"platform": "douyin", "id": "123", "note_type": "video"},
            stale,
            "target.mp4",
        )

        self.assertIs(result, fresh)
        self.assertEqual(fake_tikhub.download_to_file.call_count, 2)
        self.assertEqual(
            fake_tikhub.download_to_file.call_args_list[0].args[0],
            stale["play_url"],
        )
        self.assertEqual(
            fake_tikhub.download_to_file.call_args_list[1].args[0],
            fresh["play_url"],
        )
        fake_tikhub.detail.assert_called_once_with(
            "douyin", "123", "video", fresh=True
        )

    def test_missing_cached_play_url_refreshes_detail_before_download(self):
        fake_tikhub = mock.Mock()
        stale = {"duration": 10034}
        fresh = {
            "play_url": "https://fresh.example/video.mp4",
            "duration": 10,
        }
        fake_tikhub.detail.return_value = fresh

        result = breakdown._download_breakdown_video(
            fake_tikhub,
            {"platform": "douyin", "id": "123", "note_type": "video"},
            stale,
            "target.mp4",
        )

        self.assertIs(result, fresh)
        fake_tikhub.detail.assert_called_once_with(
            "douyin", "123", "video", fresh=True
        )
        fake_tikhub.download_to_file.assert_called_once()
        self.assertEqual(
            fake_tikhub.download_to_file.call_args.args[0],
            fresh["play_url"],
        )

    def test_image_post_without_play_url_is_rejected_without_refresh(self):
        fake_tikhub = mock.Mock()

        with self.assertRaises(ValueError):
            breakdown._download_breakdown_video(
                fake_tikhub,
                {"platform": "xiaohongshu", "id": "123", "note_type": "image"},
                {"images": ["https://cdn.example/image.jpg"]},
                "target.mp4",
            )

        fake_tikhub.detail.assert_not_called()
        fake_tikhub.download_to_file.assert_not_called()

    def test_incomplete_read_refreshes_detail_and_retries_once(self):
        fake_tikhub = mock.Mock()
        stale = {"play_url": "https://stale.example/video.mp4"}
        fresh = {"play_url": "https://fresh.example/video.mp4"}
        fake_tikhub.detail.return_value = fresh
        fake_tikhub.download_to_file.side_effect = [
            http.client.IncompleteRead(b"partial", 100),
            None,
        ]

        result = breakdown._download_breakdown_video(
            fake_tikhub,
            {"platform": "douyin", "id": "123", "note_type": "video"},
            stale,
            "target.mp4",
        )

        self.assertIs(result, fresh)
        self.assertEqual(fake_tikhub.download_to_file.call_count, 2)
        fake_tikhub.detail.assert_called_once_with(
            "douyin", "123", "video", fresh=True
        )

    def test_download_size_error_is_not_retried(self):
        fake_tikhub = mock.Mock()
        fake_tikhub.download_to_file.side_effect = ValueError(
            "file exceeds 26MB limit"
        )

        with self.assertRaisesRegex(ValueError, "26MB"):
            breakdown._download_breakdown_video(
                fake_tikhub,
                {"platform": "douyin", "id": "123", "note_type": "video"},
                {"play_url": "https://cdn.example/video.mp4"},
                "target.mp4",
            )

        fake_tikhub.detail.assert_not_called()
        fake_tikhub.download_to_file.assert_called_once()

    def test_download_retry_stays_bounded_and_returns_clear_error(self):
        fake_tikhub = mock.Mock()
        detail = {"play_url": "https://cdn.example/video.mp4"}
        fake_tikhub.detail.return_value = detail
        fake_tikhub.download_to_file.side_effect = TimeoutError(
            "下载超过预算（已下载 1.0MB）"
        )

        with self.assertRaisesRegex(TimeoutError, "刷新地址后重试仍失败"):
            breakdown._download_breakdown_video(
                fake_tikhub,
                {"platform": "douyin", "id": "123", "note_type": "video"},
                detail,
                "target.mp4",
            )

        self.assertEqual(fake_tikhub.download_to_file.call_count, 2)
        fake_tikhub.detail.assert_called_once_with(
            "douyin", "123", "video", fresh=True
        )

    def test_extract_frames_rejects_empty_visual_input(self):
        first = tempfile.mkdtemp()
        second = tempfile.mkdtemp()
        with mock.patch.object(
            breakdown.tempfile, "mkdtemp", side_effect=[first, second]
        ), mock.patch.object(
            breakdown.subprocess, "run", return_value=mock.Mock()
        ):
            with self.assertRaisesRegex(ValueError, "关键帧"):
                breakdown._extract_frames("video.mp4", count=4, duration=10)

    def test_parser_recovers_json_from_unclosed_code_fence(self):
        parsed = breakdown._parse_breakdown_json(
            '```json\n{"scenes":[{"scene":"完整镜头","line":""}]}'
        )
        self.assertEqual(parsed["scenes"][0]["scene"], "完整镜头")

    def test_validation_rejects_empty_and_placeholder_scenes(self):
        with self.assertRaisesRegex(ValueError, "为空"):
            breakdown._validate_scene_breakdown({"scenes": []})
        with self.assertRaisesRegex(ValueError, "占位"):
            breakdown._validate_scene_breakdown(
                {"scenes": [{"scene": "具体画面", "line": ""}]}
            )

    def test_breakdown_retries_malformed_model_output(self):
        valid = json.dumps(
            {"scenes": [{"scene": "人物从桌边起身走向窗前，镜头缓慢跟随", "line": ""}]},
            ensure_ascii=False,
        )
        with mock.patch.object(
            breakdown, "_chat_multimodal", side_effect=["not json", valid]
        ) as chat:
            result = breakdown._request_breakdown_result("system", "user", "context", [])
        self.assertEqual(len(result["scenes"]), 1)
        self.assertEqual(chat.call_count, 2)

    def test_multimodal_timeout_is_localized_after_safe_retries(self):
        with mock.patch.object(breakdown, "ZHIPU_API_KEY", "secret-test-key"), \
             mock.patch.object(
                 breakdown.egress, "post_json_idempotent",
                 side_effect=TimeoutError("The read operation timed out"),
             ):
            with self.assertRaisesRegex(RuntimeError, "AI 分析响应超时"):
                breakdown._chat_multimodal("system", "user", [])

    def test_frame_thumbnails_are_embedded_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            image = pathlib.Path(directory) / "frame.jpg"
            image.write_bytes(b"\xff\xd8\xff\xd9")
            thumbs = breakdown._frame_thumbnails([str(image)])
        self.assertEqual(thumbs, [])

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_large_source_is_resized_and_bounded_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "large.png"
            Image.new("RGB", (3000, 2000), (35, 120, 210)).save(source, "PNG")
            original_size = source.stat().st_size
            thumbs = breakdown._frame_thumbnails([str(source)])

        self.assertEqual(len(thumbs), 1)
        self.assertTrue(thumbs[0].startswith("data:image/jpeg;base64,"))
        raw = base64.b64decode(thumbs[0].split(",", 1)[1])
        self.assertLessEqual(len(raw), breakdown._THUMBNAIL_MAX_BYTES)
        self.assertNotEqual(len(raw), original_size)
        with Image.open(io.BytesIO(raw)) as thumbnail:
            self.assertLessEqual(max(thumbnail.size), breakdown._THUMBNAIL_MAX_EDGE)


if __name__ == "__main__":
    unittest.main()
