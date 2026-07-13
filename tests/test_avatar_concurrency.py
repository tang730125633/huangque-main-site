# -*- coding: utf-8 -*-
"""建形象：并发 + 提交重试。

## 实测（2026-07-12，线上、真实 HeyGen 账号、无其它任务在飞）

    5 路并发   5/5 成功、0×429、零降速（就绪中位 19.7s vs 单条基线 19.8s）
    10 路并发  9/10 成功、0×429、HeyGen 侧照样不降速
               挂掉的那条是 TLS handshake timeout，另一条提交花了 57.7s（其余都 ~1.1s）
               → 瓶颈是【我们的出境隧道】，不是 HeyGen

    扣费：连建 6 个形象，plan_credit 和 api 两个池都是 **0 扣减** —— 建形象免费。

## 两条结论

1. **worker 从 1 调到 5。** 串行只有 144 个/小时；500 人集中建形象要排 3.5 小时，
   而建形象是电影化身的【入口】，堵在这里等于整个功能没法用。5 路 → 约 900 个/小时。

2. **提交要对瞬时网络错误重试。** 握手超时意味着请求根本没发出去，用户却看到
   「建形象失败」并被退了 5 点 —— 什么都没发生。

   之所以敢重试，是因为**建形象免费**：万一上一次其实已经送达，代价只是在 HeyGen 上多留
   一个孤儿形象，不是钱。视频的提交绝不能这么干（提交即计费，$7/条）。
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

core = importlib.import_module("content_domains.core")
video = importlib.import_module("content_domains.video")
VIDEO_SRC = (ROOT / "server/content_domains/video.py").read_text(encoding="utf-8")


class WorkerPoolTests(unittest.TestCase):
    def test_the_pool_is_no_longer_serial(self):
        self.assertGreaterEqual(core.AVATAR_JOB_WORKERS, 5,
                                "串行 144 个/小时 —— 500 人集中建形象要排 3.5 小时")

    def test_it_stays_within_the_measured_clean_headroom(self):
        """10 路时我们自己的隧道开始丢包（1/10 TLS 握手超时）。别拍脑袋往上加 ——
        隧道扩容之前，5 是实测零失败的档位。"""
        self.assertLessEqual(core.AVATAR_JOB_WORKERS, 5)

    def test_a_single_user_cannot_take_the_whole_pool(self):
        self.assertLess(core.MAX_USER_ACTIVE_AVATAR, core.AVATAR_JOB_WORKERS)


class SubmitRetryTests(unittest.TestCase):
    def test_the_avatar_submit_retries_transient_network_errors(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise video.HeyGenNetworkError("handshake timed out")
            return "ok"

        with patch.object(video.time, "sleep", lambda s: None):
            self.assertEqual(video._heygen_retry_net(flaky, "建形象"), "ok")
        self.assertEqual(len(calls), 3)

    def test_it_gives_up_and_raises_the_last_error(self):
        def always():
            raise video.HeyGenNetworkError("boom")

        with patch.object(video.time, "sleep", lambda s: None):
            with self.assertRaises(video.HeyGenNetworkError):
                video._heygen_retry_net(always, "建形象")

    def test_it_does_not_swallow_real_failures(self):
        """「照片里没有检测到人脸」这种是 HeyGen 明确的应答，重试多少次都一样 —— 不能盲重。"""
        def no_face():
            raise RuntimeError("No face detected")

        with self.assertRaises(RuntimeError):
            video._heygen_retry_net(no_face, "建形象")

    def test_both_the_upload_and_the_submit_are_wrapped(self):
        block = VIDEO_SRC.split("def gen_avatar")[1].split("def ")[0]
        self.assertIn('_heygen_retry_net(lambda: _heygen_upload_asset(fp, direct=True), "建形象传图")', block)
        self.assertIn('_heygen_retry_net(', block)
        self.assertIn("_heygen_create_photo_avatar", block)

    def test_video_submits_are_NOT_given_this_retry(self):
        """⚠️ 最要紧的一条。视频提交即计费（$7/条），重发 = 同一条片子付两次。
        _heygen_retry_net 只能出现在建形象里 —— 建形象免费，重发不花钱。"""
        for fn in ("generate_heygen_video_direct", "gen_cinematic", "generate_heygen_video"):
            block = VIDEO_SRC.split("def %s" % fn)[1].split("\ndef ")[0]
            self.assertNotIn("_heygen_retry_net", block,
                             "%s 用了 _heygen_retry_net —— 会把同一条视频付两次钱" % fn)

    def test_the_reason_it_is_safe_is_written_down(self):
        """下一个人一定会问「凭什么这里能重发」。答案得在代码里，不能只在 PR 描述里。"""
        doc = video._heygen_retry_net.__doc__
        self.assertIn("免费", doc)
        self.assertIn("HeyGenBilledError", doc)


if __name__ == "__main__":
    unittest.main()
