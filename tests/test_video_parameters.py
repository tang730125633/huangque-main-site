# -*- coding: utf-8 -*-
import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import video, wavespeed


def _data_url(mime, raw):
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


PNG = _data_url("image/png", b"\x89PNG\r\n\x1a\n" + b"x" * 32)
MP4 = _data_url("video/mp4", b"\x00\x00\x00\x18ftypmp42" + b"x" * 32)
VIDEO_HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")
CORE_SRC = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")


class MotionParameterValidationTests(unittest.TestCase):
    def _payload(self, **extra):
        payload = {
            "mode": "motion",
            "image_data": PNG,
            "reference_video_data": MP4,
        }
        payload.update(extra)
        return payload

    def test_motion_defaults_to_real_720p_and_drops_client_duration(self):
        result = video.validate_video_payload(self._payload(duration=10))
        self.assertEqual("720p", result["resolution"])
        self.assertNotIn("duration", result)

    def test_motion_no_longer_carries_a_line(self):
        # 动作模仿已去线路化（原线路一 HeyGen 拆成了独立的「AI 剧情视频」）。
        # 老前端可能仍带 line，忽略但不能写进 payload —— 否则会混淆历史记录的分桶。
        result = video.validate_video_payload(self._payload(line="1"))
        self.assertNotIn("line", result)

    def test_motion_rejects_fake_1080p_and_says_where_to_get_it(self):
        """绝不悄悄降分辨率（本文件的核心意图，防的是历史上的「假 1080p」bug）。

        动作模仿走 WaveSpeed，它只有 720p。用户要 1080p 就必须去「AI 剧情视频」——
        报错要把去处说清楚，而不是默默给一个 720p 的片子。
        """
        with self.assertRaisesRegex(ValueError, "剧情视频"):
            video.validate_video_payload(self._payload(resolution="1080p"))

    def test_cinematic_is_where_1080p_lives_now(self):
        out = video.validate_cinematic_payload({
            "avatar_ids": [1], "prompt": "海边跳舞", "resolution": "1080p", "ratio": "9:16"})
        self.assertEqual("1080p", out["resolution"])

    def test_unverified_4k_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "720p、1080p"):
            video.validate_video_payload(self._payload(resolution="4k"))

    def test_wavespeed_never_silently_downscales(self):
        with self.assertRaisesRegex(ValueError, "仅支持 720p"):
            wavespeed.generate_motion("image.jpg", "reference.mp4", "1080p")


class TryonParameterValidationTests(unittest.TestCase):
    def test_line2_accepts_real_duration_range(self):
        for seconds in (5, 6, 10, 15):
            with self.subTest(seconds=seconds):
                result = video.validate_tryon_payload({
                    "line": "2",
                    "person_image_data": PNG,
                    "clothes_data": PNG,
                    "seconds": seconds,
                })
                self.assertEqual(seconds, result["seconds"])

    def test_line2_rejects_out_of_range_and_background(self):
        for seconds in (4, 16):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(ValueError, "5-15 秒"):
                video.validate_tryon_payload({
                    "line": "2", "person_image_data": PNG, "clothes_data": PNG,
                    "seconds": seconds,
                })
        with self.assertRaisesRegex(ValueError, "线路二不支持换背景"):
            video.validate_tryon_payload({
                "line": "2", "person_image_data": PNG, "clothes_data": PNG,
                "background_data": PNG, "seconds": 6,
            })

    def test_line1_matches_six_second_input_cap(self):
        for seconds in (1, 3, 5, 6):
            with self.subTest(seconds=seconds):
                result = video.validate_tryon_payload({
                    "line": "1",
                    "person_video_data": MP4,
                    "clothes_data": PNG,
                    "seconds": seconds,
                })
                self.assertEqual(seconds, result["seconds"])
        with self.assertRaisesRegex(ValueError, "1-6 秒"):
            video.validate_tryon_payload({
                "line": "1", "person_video_data": MP4, "clothes_data": PNG,
                "seconds": 7,
            })

    def test_line2_result_keeps_selected_duration(self):
        with patch.object(video, "_save_data_file", side_effect=["person.png", "cloth.png"]), \
                patch.object(video, "update_video_asset_phase"), \
                patch.object(wavespeed, "available", return_value=True), \
                patch.object(wavespeed, "generate_tryon",
                             return_value={"video_file": "video/out.mp4", "video_url": "/out.mp4"}) as generate:
            result = video.gen_tryon({
                "line": "2", "person_image_data": "person", "clothes_data": "cloth",
                "seconds": 10,
            })
        self.assertEqual(10, generate.call_args.args[2])
        self.assertEqual(10, result["duration"])
        self.assertEqual(10, result["seconds"])

    def test_tryon_is_validated_before_points_are_deducted(self):
        self.assertIn('elif kind == "tryon":', CORE_SRC)
        self.assertIn("body = video_domain.validate_tryon_payload(body)", CORE_SRC)
        self.assertLess(
            CORE_SRC.index("body = video_domain.validate_tryon_payload(body)"),
            CORE_SRC.index("cost = points_domain.cost_of(kind, body)"),
        )


class VideoParameterUiTests(unittest.TestCase):
    def test_only_supported_talking_resolutions_are_offered(self):
        self.assertIn('data-resolution="720p"', VIDEO_HTML)
        self.assertIn('data-resolution="1080p"', VIDEO_HTML)
        self.assertNotIn('data-resolution="4k"', VIDEO_HTML)

    def test_motion_resolution_is_locked_to_720p_and_submitted(self):
        """动作模仿已去线路化：只走 WaveSpeed，固定 720p。

        原来这条守的是「按线路切换分辨率」，但线路一(HeyGen)的能力已经拆成了独立的
        「AI 剧情视频」。核心意图不变——绝不悄悄降分辨率：1080p 的按钮是 disabled 的，
        点不动，而且 title 里告诉用户 1080p 去哪儿找。
        """
        self.assertIn('data-motion-resolution="720p"', VIDEO_HTML)
        self.assertIn('data-motion-resolution="1080p" disabled', VIDEO_HTML)
        self.assertIn("resolution:selectedMotionResolution", VIDEO_HTML)
        self.assertIn("selectedMotionResolution='720p';", VIDEO_HTML)
        self.assertNotIn("selectedMotionLine", VIDEO_HTML)
        self.assertNotIn("duration:10", VIDEO_HTML)

    def test_tryon_duration_is_selected_not_hardcoded(self):
        for seconds in (3, 5, 6, 10, 15):
            self.assertIn('data-tryon-seconds="%d"' % seconds, VIDEO_HTML)
        self.assertGreaterEqual(VIDEO_HTML.count("seconds:selectedTryonSeconds"), 2)
        self.assertNotIn("seconds:6", VIDEO_HTML)

    def test_hidden_output_shadow_ui_is_removed(self):
        for stale_id in ("outputThumb", "outputMotion", "outputRatio", "outputDuration", "outputPreview"):
            self.assertNotIn('id="%s"' % stale_id, VIDEO_HTML)
        self.assertIn("renderVideoHistoryState('正在读取生成记录...'", VIDEO_HTML)
        self.assertIn("renderVideoHistoryState('生成记录加载失败',true)", VIDEO_HTML)
        self.assertIn("videoModeTag(x)", VIDEO_HTML)


if __name__ == "__main__":
    unittest.main()
