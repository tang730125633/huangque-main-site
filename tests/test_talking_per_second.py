# -*- coding: utf-8 -*-
"""口播(video kind)按秒计费 + 跑完结算（kongli 2026-07-15）。

口播成片时长 ≈ 音频时长，10 点/秒 × 输出时长。扣点时机：
- audio 模式：ffprobe 上传/引用音频，扣点前拿到精确时长 → 扣准，无需结算。
- text 模式：TTS 在 job 里才跑，扣点时按文本长度【偏保守】估算预扣，跑完由 run_job 按成片
  真实时长（HeyGen 返回的 duration）结算 —— **多退少不补**，绝不二次扣点。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import points, video


class TalkingPerSecondTests(unittest.TestCase):
    def test_rate_is_ten(self):
        self.assertEqual(video.TALKING_RATE_PER_SEC, 10)

    def test_text_mode_estimates_from_length(self):
        # 4 字/秒（偏保守，估长→预扣偏高→跑完退差）：45 字 → 11.25s → ceil 12 → 120 点
        self.assertEqual(points.cost_of("video", {"mode": "text", "text": "一" * 45}), 120)

    def test_text_mode_is_at_least_one_second(self):
        self.assertEqual(points.cost_of("video", {"mode": "text", "text": "嗨"}), 10)

    def test_audio_mode_falls_back_when_unprobeable(self):
        # 探不到音频时长 → 兜底 10s → 100 点（真实时长在跑完由结算修正）
        self.assertEqual(points.cost_of("video", {"mode": "audio"}), 100)


class TalkingSettleTests(unittest.TestCase):
    def test_actual_cost_is_ten_per_output_second(self):
        self.assertEqual(video.talking_actual_cost({"duration": 8}), 80)      # 8 × 10
        self.assertEqual(video.talking_actual_cost({"duration": 7.2}), 80)    # 向上取整 8 → 80
        self.assertEqual(video.talking_actual_cost({"seconds": 3}), 30)       # 也认 seconds

    def test_actual_cost_none_when_duration_missing(self):
        """拿不到成片时长 → 不结算，保留预扣（宁可不退，也不乱退）。"""
        self.assertIsNone(video.talking_actual_cost({}))
        self.assertIsNone(video.talking_actual_cost({"duration": 0}))
        self.assertIsNone(video.talking_actual_cost({"duration": "x"}))

    def test_settle_is_wired_after_done_and_only_refunds_overcharge(self):
        core_src = (Path(video.__file__).with_name("core.py")).read_text(encoding="utf-8")
        block = core_src.split('if not _set_terminal(job_id, "done", result=result):')[1][:900]
        self.assertIn('kind == "video"', block)
        self.assertIn("talking_actual_cost", block)
        self.assertIn("safe_refund_points", block)
        # ⚠️ 多退少不补：只有预扣 > 实际才退，绝不在结算里二次扣点
        self.assertIn("> actual", block)
        self.assertNotIn("deduct", block)


class TalkingFrontendTests(unittest.TestCase):
    """前端口播单价必须与后端一致，且说清是「按实际时长结算」，别再显示旧的固定价。"""

    VIDEO_HTML = (Path(__file__).resolve().parents[1] / "site/workbench/video.html").read_text(encoding="utf-8")

    def test_frontend_rate_matches_backend(self):
        import re
        m = re.search(r"var TALKING_RATE=(\d+)", self.VIDEO_HTML)
        self.assertTrue(m, "video.html 里没有 TALKING_RATE")
        self.assertEqual(int(m.group(1)), video.TALKING_RATE_PER_SEC, "前端口播单价与后端 TALKING_RATE_PER_SEC 不一致")

    def test_frontend_shows_settle_wording_and_drops_old_flat_price(self):
        self.assertIn("按实际时长结算", self.VIDEO_HTML)
        self.assertNotIn("预计消耗 20 点", self.VIDEO_HTML)


if __name__ == "__main__":
    unittest.main()
