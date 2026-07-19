# -*- coding: utf-8 -*-
"""视频生成死线：视频引擎 15 分钟，电影化身 20 分钟。

## 两层超时，顺序不能颠倒

    引擎自己的轮询死线（VIDEO_GEN_DEADLINE / CINEMATIC_GEN_DEADLINE） → 抛「生成超时」，退点
    reaper 的宽限（*_REAPER_GRACE）                                   → 兜底：worker 整个卡死

reaper 必须【后】于引擎死线触发。反过来的话，reaper 先把任务判死并退点，而 worker 还在轮询 ——
上游照样出片、照样收钱（HeyGen 是提交即计费），我们白付一次。

口播原来就是这个反过来的状态：中转轮询死线 1200s，reaper 对口播的宽限却只有 540s。

## 电影化身单开一条 20 分钟（kongli 2026-07-14，原来跟全站 15 分钟走）

它是唯一「提交即扣费」的引擎（$7/条，收钱在提交那一刻，不是成功时）。别的引擎超时了顶多
白等；它超时了【钱已经花掉】。

线上真出现过：我们在 900s 判超时退点，而 HeyGen 那边其实已经 completed、片子都出来了 ——
结果片子被扔、$7 照付、用户还拿到一句「生成超时」。宁可多等 5 分钟。

⚠️ **改死线就必须让 reaper 宽限跟着走。** 代码里用 `CINEMATIC_GEN_DEADLINE + 300` 把不变式
钉死，不是两个常量各写各的字面量 —— 下面 ReaperFiresAfterTheEngineTests 守的就是这一条。

## 换装（tryon）不跟这些走

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
        """一个引擎漏了，它就还按自己那套超时 —— 用户看到的时长就不一致。

        电影化身【不在此列】：它单独一条 20 分钟，见 CinematicGetsTwentyMinutesTests。
        """
        self.assertEqual(wavespeed.WS_DEADLINE, core.VIDEO_GEN_DEADLINE, "WaveSpeed（果肉/豆姐/欧米）")
        # 口播：直连和中转两条路都要用它
        self.assertIn("_heygen_poll_video(video_id, direct=True, deadline_s=VIDEO_GEN_DEADLINE)", VIDEO_SRC)
        self.assertIn("_heygen_poll_video(video_id, deadline_s=VIDEO_GEN_DEADLINE)", VIDEO_SRC)


class CinematicGetsThirtyMinutesTests(unittest.TestCase):
    """动作模仿 + 开放式生成（都是 kind=cinematic）：30 分钟（kongli 2026-07-17，原 20 分钟）。"""

    def test_the_deadline_is_thirty_minutes(self):
        self.assertEqual(core.CINEMATIC_GEN_DEADLINE, 30 * 60)

    def test_the_poll_loop_actually_uses_it(self):
        """常量改了但轮询还用 VIDEO_GEN_DEADLINE = 白改。"""
        self.assertEqual(video.HEYGEN_MOTION_DEADLINE, core.CINEMATIC_GEN_DEADLINE)
        self.assertIn("deadline_s=HEYGEN_MOTION_DEADLINE", VIDEO_SRC)

    def test_it_is_longer_than_the_other_engines(self):
        self.assertGreater(core.CINEMATIC_GEN_DEADLINE, core.VIDEO_GEN_DEADLINE)

    def test_the_env_override_still_works(self):
        """线上要临时加裕量时，改 env 就行，不用发版。"""
        self.assertIn('_env_positive_int("HEYGEN_MOTION_DEADLINE", 1800)', CORE_SRC)

    def test_video_py_does_not_hardcode_its_own_number(self):
        """⚠️ 两边各写一个字面量 → 改了一边忘了另一边 → reaper 先杀。
        video.py 必须【引用】core 的常量。"""
        self.assertIn("HEYGEN_MOTION_DEADLINE = CINEMATIC_GEN_DEADLINE", VIDEO_SRC)

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

    def test_cinematic_grace_is_strictly_longer_than_its_own_deadline(self):
        """⚠️ 这条是整个改动最容易出事的地方。

        电影化身的死线抬到 1200s 之后，如果 reaper 的宽限还是老的 VIDEO_REAPER_GRACE(1200s)，
        两个数就【相等】了 —— reaper 会在引擎自己抛「生成超时」的同一秒把任务当死尸杀掉。
        用户拿到的是没头没脑的「生成超时自动结束」，而 HeyGen 那边 $7 已经花了。
        """
        self.assertEqual(core.KIND_GRACE["cinematic"], core.CINEMATIC_REAPER_GRACE)
        self.assertGreater(core.CINEMATIC_REAPER_GRACE, core.CINEMATIC_GEN_DEADLINE,
                           "reaper 会先于引擎死线杀掉还活着的任务 —— 钱花了，片子没了")
        self.assertGreaterEqual(core.CINEMATIC_REAPER_GRACE - core.CINEMATIC_GEN_DEADLINE, 300,
                                "留给轮询之外的上传/下载/合音轨 —— 那些阶段不刷 updated_at")

    def test_the_invariant_is_enforced_by_arithmetic_not_by_memory(self):
        """两个常量各写各的字面量，迟早有人只改一个。用加法把它钉死。"""
        self.assertIn("CINEMATIC_REAPER_GRACE = CINEMATIC_GEN_DEADLINE + 300", CORE_SRC)


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
