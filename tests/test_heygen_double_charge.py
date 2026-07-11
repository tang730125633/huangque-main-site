# -*- coding: utf-8 -*-
"""HeyGen 提交后失败不得回退重发 —— 否则同一条视频付两次钱。

2026-07-11 用「生成前后读钱包」实测（这是唯一可信的上游单价测法，文档和除法都骗过我）：

    ① 生成前     钱包 $15.15   quota 909
    ③ 提交生成后 钱包 $8.15    quota 489     ← 提交那一刻就扣了 $7.00
    出片时不再扣。

即 cinematic_avatar = $7.00/条，**提交即计费**。而「回退泽龙中转」转发的是同一个 HeyGen
账号（见 generate_heygen_motion_video 的注释），所以回退不是换供应商，是拿同一份素材
再提交一次 —— 同一条视频付两次 $7。

原代码两处 fallback 都是 `except Exception` 一把抓，不区分失败发生在提交前还是提交后：
轮询超时、下载失败、网络抖动，统统触发重发。而实测生成要 392~511s，原死线却只有 510s
（照着早已废弃的 reaper 600s 算的，reaper 现在是 2400s）—— 于是线上频繁出现
「motion直连失败,回退泽龙中转」，每一次都是一条视频付两遍。

这与 egress.post_json 里早已立下的非幂等纪律是同一条：只有「投递前」的失败才可以换通道重试。
"""
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")


class DeadlineTests(unittest.TestCase):
    def test_motion_deadline_covers_measured_generation_time(self):
        # 实测 10 路并发下生成最长 511s；死线必须明显高于它，否则擦线就回退重发。
        self.assertGreaterEqual(video.HEYGEN_MOTION_DEADLINE, 1000)

    def test_motion_deadline_stays_within_reaper_grace(self):
        # 上传(≤240s) + 生成(≤死线) + 下载 必须仍在 reaper 的 motion 宽限 2400s 内，
        # 否则 reaper 会在直连还没轮询完时就把任务判超时退点（既扣了 $7 又退了点）。
        self.assertLess(240 + video.HEYGEN_MOTION_DEADLINE + 60, 2400)


class _Billed(Exception):
    """测试替身：模拟「提交成功之后」发生的失败（轮询超时/下载失败）。"""


class MotionFallbackTests(unittest.TestCase):
    """提交后失败 → 必须原样抛出，绝不能再走一次 impl（那就是再扣 $7）。"""

    def _run(self, impl_side_effect):
        calls = []

        def fake_impl(*a, **kw):
            calls.append(kw.get("direct"))
            return impl_side_effect(kw.get("direct"))

        with patch.object(video, "_generate_heygen_motion_video_impl", fake_impl), \
             patch.object(video, "_HEYGEN_DIRECT", True), \
             patch.object(video, "HEYGEN_API_KEY", "k"):
            try:
                out = video.generate_heygen_motion_video("i.jpg", "r.mp4", "720p", "9:16", 13)
                return calls, out, None
            except Exception as e:
                return calls, None, e

    def test_billed_failure_is_not_retried_on_relay(self):
        def impl(direct):
            if direct:
                raise video.HeyGenBilledError("已提交(已扣费)，轮询超时")
            raise AssertionError("提交后失败竟然回退了中转 —— 同一条视频会再付一次 $7")

        calls, out, err = self._run(impl)

        self.assertEqual(calls, [True], "直连提交后失败，绝不能再调一次 impl")
        self.assertIsInstance(err, video.HeyGenBilledError)

    def test_pre_submit_failure_still_falls_back(self):
        # 提交前失败（上传挂了、建 avatar 挂了）没有计费，回退中转是安全且必要的。
        def impl(direct):
            if direct:
                raise RuntimeError("上传素材失败：连接被拒")
            return {"video_file": "video/ok.mp4", "provider": "heygen_zelong"}

        calls, out, err = self._run(impl)

        self.assertIsNone(err)
        self.assertEqual(calls, [True, False], "提交前失败应当回退中转")
        self.assertEqual(out["provider"], "heygen_zelong")


class TalkingFallbackTests(unittest.TestCase):
    """口播是同一个形状的 bug（_heygen_create_video 之后才失败也会重发）。"""

    def test_billed_failure_is_not_retried_on_relay(self):
        calls = []

        def fake_direct(*a, **kw):
            calls.append("direct")
            raise video.HeyGenBilledError("口播已提交(已计费)，下载失败")

        # 回退分支若被走到，会去碰真实文件/网络；这里用哨兵确保它压根不该被走到
        def boom(*a, **kw):
            raise AssertionError("口播提交后失败竟然回退了中转 —— 会再付一次")

        with patch.object(video, "generate_heygen_video_direct", fake_direct), \
             patch.object(video, "_resolve_out_file", boom), \
             patch.object(video, "_HEYGEN_DIRECT", True), \
             patch.object(video, "HEYGEN_API_KEY", "k"):
            with self.assertRaises(video.HeyGenBilledError):
                video.generate_heygen_video("i.jpg", "a.mp3", "1080p", "9:16", "medium")

        self.assertEqual(calls, ["direct"])


if __name__ == "__main__":
    unittest.main()
