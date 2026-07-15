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


class VideoResolutionValidationTests(unittest.TestCase):
    def test_talking_rejects_unverified_4k(self):
        with self.assertRaisesRegex(ValueError, "720p、1080p"):
            video.validate_video_payload({"mode": "text", "image_data": PNG, "text": "hi",
                                          "voice": "v", "resolution": "4k"})

    def test_cinematic_is_where_1080p_lives_now(self):
        out = video.validate_cinematic_payload({
            "avatar_ids": [1], "prompt": "海边跳舞", "resolution": "1080p", "ratio": "9:16"})
        self.assertEqual("1080p", out["resolution"])


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
    def test_talking_resolution_is_fixed_at_1080p(self):
        # 口播不再给选分辨率（kongli 2026-07-15），固定 1080p：选择器移除、常量 1080p、后端缺省也 1080p。
        self.assertNotIn('data-resolution=', VIDEO_HTML)
        self.assertIn("selectedResolution='1080p'", VIDEO_HTML)

    def test_legacy_motion_ui_is_gone(self):
        # 老版动作模仿已下线：tab、面板、提交入口都不该再出现
        self.assertNotIn('data-function="motion"', VIDEO_HTML)
        self.assertNotIn('id="motionPanel"', VIDEO_HTML)
        self.assertNotIn("submitMotionVideo", VIDEO_HTML)

    def test_tryon_duration_is_selected_not_hardcoded(self):
        for seconds in (3, 5, 6, 10, 15):
            self.assertIn('data-tryon-seconds="%d"' % seconds, VIDEO_HTML)
        self.assertGreaterEqual(VIDEO_HTML.count("seconds:selectedTryonSeconds"), 2)
        self.assertNotIn("seconds:6", VIDEO_HTML)

    def test_xiaole_and_tryon_show_cost_and_match_backend(self):
        """果肉/豆姐/欧米(30 点/秒) 与 试衣(25/40) 都要在前端显示预估价，且与后端实扣对上。"""
        import re
        from content_domains import points
        # 前端 XIAOLE_RATE 必须与后端 xiaole_video 单价一致
        m = re.search(r"var XIAOLE_RATE=(\d+)", VIDEO_HTML)
        self.assertTrue(m, "video.html 没有 XIAOLE_RATE")
        self.assertEqual(int(m.group(1)), points.cost_of("xiaole_video", {"duration": 1}),
                         "前端果肉单价与后端 30 点/秒不一致")
        # 四个成本提示元素都在
        for eid in ("grokCostNote", "microCostNote", "omniCostNote", "tryonCostNote"):
            self.assertIn('id="%s"' % eid, VIDEO_HTML, eid)
        # 试衣两档与后端一致：换背景需衣服图+背景图都有 → 40，否则 25
        self.assertIn("换装+换背景 40 点", VIDEO_HTML)
        self.assertIn("换装 25 点", VIDEO_HTML)
        self.assertEqual(points.cost_of("tryon", {"clothes_data": "x", "background_data": "y"}), 40)
        self.assertEqual(points.cost_of("tryon", {"clothes_data": "x"}), 25)

    def test_hidden_output_shadow_ui_is_removed(self):
        for stale_id in ("outputThumb", "outputMotion", "outputRatio", "outputDuration", "outputPreview"):
            self.assertNotIn('id="%s"' % stale_id, VIDEO_HTML)
        self.assertIn("renderVideoHistoryState('正在读取生成记录...'", VIDEO_HTML)
        self.assertIn("renderVideoHistoryState('生成记录加载失败',true)", VIDEO_HTML)
        self.assertIn("videoModeTag(x)", VIDEO_HTML)


if __name__ == "__main__":
    unittest.main()
