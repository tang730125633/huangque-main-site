import importlib
import sys
import unittest
from pathlib import Path


class CostOfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.points = importlib.import_module("content_domains.points")

    def test_script_to_video_talking_estimates_by_text_length(self):
        """一键成片口播沿用主站当前每 30 秒 30 点的预扣规则。"""
        scenes = [{"line": "一二三四五六七八九十一二三四五六七八九十", "scene": "画面"}]
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": scenes, "style": "口播"}), 30)

    def test_script_to_video_talking_has_minimum_hold(self):
        """口播预扣保底一档 30 点。"""
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": [{"line": "短"}], "style": "种草"}), 30)

    def test_script_to_video_preflights_talking_and_missing_static_materials(self):
        body = {
            "scenes": [{"line": "短", "scene": "枇杷树"}],
            "style": "口播",
            "material_plan": [
                {"source": "asset"},
                {"source": "generate"},
                {"source": "generate"},
            ],
            "material_generate_count": 2,
        }
        self.assertEqual(self.points.cost_of("script_to_video", body), 70)
        self.assertEqual(body["cost_breakdown"], {
            "talking": 30,
            "material_images": 40,
            "material_generate_count": 2,
            "material_reused_count": 1,
            "total": 70,
        })

    def test_script_to_video_drama_aligns_with_xiaole_per_second(self):
        """剧情与 xiaole_video 同价：30 点/秒 × 时长（默认 10s，上限 15s），不再固定 20 点"""
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": [{"scene": "画面", "line": ""}], "style": "剧情"}), 300)
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": [{"scene": "画面"}], "style": "剧情", "duration": 3}), 90)
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": [{"scene": "画面"}], "style": "剧情", "duration": 15}), 450)
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": [{"scene": "画面"}], "style": "剧情", "duration": 99}), 450)

    def test_script_to_video_drama_bad_duration_falls_back_to_10s(self):
        """时长字段非法时按默认 10s 计价，不允许 cost_of 抛异常打穿提交链路"""
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": [{"scene": "画面"}], "style": "剧情", "duration": "abc"}), 300)

    def test_breakdown_batch_refund_rules(self):
        """批量拆解退点：全灭全退；部分失败每条退 20；无失败/无费用/坏参数不退"""
        self.assertEqual(self.points.breakdown_batch_refund(100, 5, 5), 100)  # 全灭全退
        self.assertEqual(self.points.breakdown_batch_refund(100, 5, 2), 40)   # 部分失败 20×2
        self.assertEqual(self.points.breakdown_batch_refund(60, 3, 1), 20)    # 3 条灭 1 条退 20
        self.assertEqual(self.points.breakdown_batch_refund(20, 1, 1), 20)    # 单条全灭全退
        self.assertEqual(self.points.breakdown_batch_refund(100, 5, 9), 100)  # failed>total 仍按全灭
        self.assertEqual(self.points.breakdown_batch_refund(100, 5, 0), 0)    # 无失败不退
        self.assertEqual(self.points.breakdown_batch_refund(0, 5, 5), 0)     # 无费用不退
        self.assertEqual(self.points.breakdown_batch_refund("x", 5, 5), 0)   # 坏参数不抛异常

    def test_settle_breakdown_batch_refunds_only_failed_urls(self):
        """结算钩子：只对 breakdown_batch 退点；单条拆解/无失败/坏结果一律不动"""
        calls = []
        orig = self.points.safe_refund_points
        self.points.safe_refund_points = lambda u, a, r="": calls.append((u, a)) or a
        try:
            self.points.settle_breakdown_batch(
                "fang", 100, {"type": "breakdown_batch", "total": 5, "errors": [{"url": "a"}, {"url": "b"}]}, 99)
            self.assertEqual(calls, [("fang", 40)])
            calls.clear()
            self.points.settle_breakdown_batch("fang", 24, {"type": "breakdown_batch", "total": 5, "errors": []}, 100)
            self.points.settle_breakdown_batch("fang", 8, {"type": "breakdown"}, 101)   # 单条不在这结算
            self.points.settle_breakdown_batch("fang", 8, None, 102)
            self.assertEqual(calls, [])
        finally:
            self.points.safe_refund_points = orig

    def test_breakdown_batch_fixed_per_link_pricing(self):
        """批量拆解：每个有效链接 20 点，封顶 5 条 = 100 点"""
        self.assertEqual(self.points.cost_of("breakdown", {"urls": ["https://a.test/1"]}), 20)
        self.assertEqual(
            self.points.cost_of("breakdown", {"urls": ["https://a.test/1", "https://a.test/2", "https://a.test/3"]}), 60)
        self.assertEqual(
            self.points.cost_of("breakdown", {"urls": ["https://a.test/%d" % i for i in range(6)]}), 100)

    def test_breakdown_single_url_is_20(self):
        self.assertEqual(self.points.cost_of("breakdown", {"url": "https://a.test/1"}), 20)


if __name__ == "__main__":
    unittest.main()
