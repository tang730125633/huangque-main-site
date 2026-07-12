# -*- coding: utf-8 -*-
"""剧情视频的时长「自适应」：跟随参考视频的实际长度。

用户既然上传了参考片段，成片就该和它一样长 —— 而不是被截断，也不是硬拖到某个固定秒数。

边界（都得处理，否则用户会拿到一个莫名其妙的时长）：
  * 没传参考视频 → 无从跟随（只有提示词，没有时间基准）→ 回落 10 秒
  * 探测失败（ffprobe 挂了 / 文件坏了）→ 回落 10 秒。时长是优化项，不该让整个任务失败
  * 超出 HeyGen 的 4~15 秒 → 夹住。超出范围它直接 400
  * 向上取整 —— 宁可多一帧，也别把参考片段的末尾截掉
"""
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")
HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")


class AutoDurationTests(unittest.TestCase):
    def _auto(self, secs, ref="video/ref.mp4"):
        with patch.object(video, "_probe_video_duration", return_value=secs):
            return video._cinematic_duration("auto", ref)

    def test_follows_the_reference_video(self):
        self.assertEqual(self._auto(8.0), 8)

    def test_rounds_up_so_the_tail_is_not_cut(self):
        # 8.2 秒的参考片段 → 9 秒。宁可多一帧，也别把末尾截掉。
        self.assertEqual(self._auto(8.2), 9)

    def test_clamps_into_heygens_range(self):
        lo, hi = video.CINEMATIC_DURATION_RANGE
        self.assertEqual(self._auto(120.0), hi, "超长的参考片段要夹到 15 秒，否则 HeyGen 直接 400")
        self.assertEqual(self._auto(1.0), lo, "太短的也要夹上来")

    def test_without_a_reference_it_falls_back(self):
        """只有提示词、没有参考视频时，「自适应」无从跟随 —— 回落默认值，不能报错。"""
        self.assertEqual(video._cinematic_duration("auto", None), video.CINEMATIC_AUTO_DURATION)
        self.assertEqual(video._cinematic_duration("", None), video.CINEMATIC_AUTO_DURATION)

    def test_a_broken_probe_falls_back_instead_of_failing_the_job(self):
        """ffprobe 挂了不该让整个任务失败 —— 时长是优化项，不是正确性前提。"""
        with patch.object(video, "_probe_video_duration", side_effect=RuntimeError("ffprobe 挂了")):
            self.assertEqual(video._cinematic_duration("auto", "video/ref.mp4"),
                             video.CINEMATIC_AUTO_DURATION)

    def test_an_explicit_duration_still_wins(self):
        with patch.object(video, "_probe_video_duration", return_value=8.0):
            self.assertEqual(video._cinematic_duration(15, "video/ref.mp4"), 15,
                             "用户明确选了固定秒数，就不该被参考视频覆盖")


class ValidationTests(unittest.TestCase):
    def _body(self, **kw):
        b = {"cine_mode": "open", "avatar_ids": [1], "prompt": "海边跳舞",
             "resolution": "720p", "ratio": "9:16"}
        b.update(kw)
        return b

    def test_auto_is_resolved_here_not_deferred_to_the_worker(self):
        """时长必须在校验阶段就变成确定的秒数 —— 因为扣点紧接着就发生。

        调用链是 validate → cost_of → 扣点 → 入队 → gen_cinematic，而点数 = 秒数 × 单价。
        留个 "auto" 给 worker 去解析，扣点这一刻就不知道该扣多少。
        """
        out = video.validate_cinematic_payload(self._body(duration="auto"))
        self.assertEqual(out["duration"], video.CINEMATIC_AUTO_DURATION, "没有参考视频 → 回落 10 秒")
        self.assertNotEqual(out["duration"], "auto")

    def test_missing_duration_means_auto(self):
        self.assertEqual(video.validate_cinematic_payload(self._body())["duration"],
                         video.CINEMATIC_AUTO_DURATION)

    def test_a_reference_video_is_probed_at_submit_time(self):
        """有参考视频时，「自适应」在这里就落盘 + 探测 —— 扣点前就知道成片几秒。"""
        with patch.object(video, "_is_valid_data_url", lambda *a: True), \
             patch.object(video, "_save_data_file", lambda *a, **k: "video/ref.mp4"), \
             patch.object(video, "_probe_video_duration", lambda f: 8.2):
            out = video.validate_cinematic_payload(
                self._body(duration="auto", reference_video_data="data:video/mp4;base64,AA"))
        self.assertEqual(out["duration"], 9, "8.2 秒 → 向上取整 9 秒")
        # 落盘后 payload 里存路径，不再存几十 MB 的 base64（jobs.payload 会被撑爆）
        self.assertEqual(out["reference_video_files"], ["video/ref.mp4"])
        self.assertNotIn("reference_video_data", out)

    def test_explicit_values_are_still_validated(self):
        self.assertEqual(video.validate_cinematic_payload(self._body(duration=12))["duration"], 12)
        for bad in (3, 16):
            with self.assertRaises(ValueError):
                video.validate_cinematic_payload(self._body(duration=bad))

    def test_garbage_is_rejected(self):
        with self.assertRaises(ValueError):
            video.validate_cinematic_payload(self._body(duration="随便"))


class UiTests(unittest.TestCase):
    def test_auto_is_offered_and_is_the_default(self):
        self.assertIn('data-cine-duration="auto"', HTML)
        self.assertIn('<button class="on" data-cine-duration="auto">自适应</button>', HTML)
        self.assertIn("selectedCineDuration='auto'", HTML)

    def test_auto_is_not_mangled_by_parseInt(self):
        # parseInt('auto') = NaN → 后端收到 null，白白丢掉用户的选择
        self.assertIn("v==='auto' ? 'auto' : parseInt(v,10)", HTML)

    def test_the_hint_explains_what_auto_does_without_a_reference(self):
        """用户选了自适应、没传参考片段，拿到 10 秒的片子会以为是 bug —— 必须提前说清楚。"""
        self.assertIn("未上传参考视频，将按 10 秒生成", HTML)
        # 参考视频现在可以传多个（开放式），跟随的是【第一个】
        self.assertIn("自适应：跟随第一个参考视频，成片约 '+cineSeconds()+' 秒", HTML)

    def test_the_hint_updates_when_a_reference_is_uploaded(self):
        # 参考素材的上传统一走 bindCineRefs（视频和图片共用一套逻辑）
        block = HTML.split("function bindCineRefs")[1].split("function renderCineRefs")[0]
        self.assertIn("updateCineDurationHint()", block)


if __name__ == "__main__":
    unittest.main()
