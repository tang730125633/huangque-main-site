# -*- coding: utf-8 -*-
"""视频生成死线：统一 15 分钟。

## 两层超时，顺序不能颠倒

    引擎自己的轮询死线（VIDEO_GEN_DEADLINE）   → 抛「生成超时」，退点，用户看得懂
    reaper 的宽限（VIDEO_REAPER_GRACE）        → 兜底：worker 整个卡死、连 updated_at 都不刷了

reaper 必须【后】于引擎死线触发。反过来的话，reaper 先把任务判死并退点，而 worker 还在轮询 ——
上游照样出片、照样收钱（HeyGen 是提交即计费），我们白付一次。

口播原来就是这个反过来的状态：中转轮询死线 1200s，reaper 对口播的宽限却只有 540s。

## 换装（tryon）不跟这 15 分钟走

线上实测线路一中位 909s、**p90 1612s（27 分钟）**。砍到 15 分钟会把超过一成的换装任务
判成失败。要改它，得先把那条链路本身提速。
"""
import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

core = importlib.import_module("content_domains.core")
video = importlib.import_module("content_domains.video")
wavespeed = importlib.import_module("content_domains.wavespeed")
CORE_SRC = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
VIDEO_SRC = (ROOT / "server/content_domains/video.py").read_text(encoding="utf-8")


class FifteenMinutesTests(unittest.TestCase):
    def test_the_deadline_is_fifteen_minutes(self):
        self.assertEqual(core.VIDEO_GEN_DEADLINE, 15 * 60)

    def test_every_video_engine_uses_it(self):
        """一个引擎漏了，它就还按自己那套超时 —— 用户看到的时长就不一致。"""
        self.assertEqual(video.HEYGEN_MOTION_DEADLINE, core.VIDEO_GEN_DEADLINE, "HeyGen（电影化身）")
        self.assertEqual(wavespeed.WS_DEADLINE, core.VIDEO_GEN_DEADLINE, "WaveSpeed（动作模仿）")
        # 口播：直连和中转两条路都要用它
        self.assertIn("_heygen_poll_video(video_id, direct=True, deadline_s=VIDEO_GEN_DEADLINE)", VIDEO_SRC)
        self.assertIn("_heygen_poll_video(video_id, deadline_s=VIDEO_GEN_DEADLINE)", VIDEO_SRC)

    def test_no_engine_still_carries_its_own_hardcoded_deadline(self):
        """回归：口播直连原来写死 450s、中转回落到 HEYGEN_TIMEOUT(1200s)。"""
        self.assertNotIn("deadline_s=450", VIDEO_SRC)
        self.assertNotIn("_heygen_poll_video(video_id)\n", VIDEO_SRC, "中转不能再回落到 HEYGEN_TIMEOUT")


class ReaperFiresAfterTheEngineTests(unittest.TestCase):
    def test_the_reaper_grace_is_strictly_longer(self):
        """顺序颠倒 = 我们白付一次上游的钱（HeyGen 提交即计费，$7/条）。"""
        self.assertGreater(core.VIDEO_REAPER_GRACE, core.VIDEO_GEN_DEADLINE,
                           "reaper 先杀，worker 还在跑：任务被判失败退点，上游照样出片照样收钱")

    def test_the_margin_covers_the_work_outside_the_poll_loop(self):
        """素材上传、成片下载、烧字幕、混 BGM —— 这些阶段 HeyGen 的轮询循环不刷 updated_at。"""
        self.assertGreaterEqual(core.VIDEO_REAPER_GRACE - core.VIDEO_GEN_DEADLINE, 300)

    def test_talking_and_motion_share_one_grace(self):
        """原来是两套数：motion 2400s（当年必回退泽龙中转时定的，去线路化后早就不需要），
        口播 540s（比中转自己的轮询死线 1200s 还短，会先杀）。"""
        self.assertIn("grace = VIDEO_REAPER_GRACE", CORE_SRC)
        self.assertNotIn('grace = 2400 if \'"mode":"motion"\'', CORE_SRC)

    def test_cinematic_grace_follows_the_same_rule(self):
        self.assertEqual(core.KIND_GRACE["cinematic"], core.VIDEO_REAPER_GRACE)


class TryonIsDeliberatelyExcludedTests(unittest.TestCase):
    def test_tryon_keeps_its_long_grace(self):
        """线上实测：线路一中位 909s、p90 1612s（27 分钟）。
        砍到 15 分钟 = 一成以上的换装任务被判失败。"""
        self.assertGreaterEqual(core.KIND_GRACE["tryon"], 2400)
        self.assertGreater(core.KIND_GRACE["tryon"], core.VIDEO_REAPER_GRACE)

    def test_the_reason_is_written_down(self):
        """下一个人看到「换装为什么不跟着 15 分钟」时，得能在代码里读到答案，
        而不是以为是漏改了。"""
        self.assertIn("p90 1612s", CORE_SRC)


if __name__ == "__main__":
    unittest.main()
