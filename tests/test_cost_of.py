import importlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
        """剧情复用 Grok 1.0 720p 的统一价格：12 点/秒。"""
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": [{"scene": "画面", "line": ""}], "style": "剧情"}), 120)
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": [{"scene": "画面"}], "style": "剧情", "duration": 3}), 36)
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": [{"scene": "画面"}], "style": "剧情", "duration": 15}), 180)
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": [{"scene": "画面"}], "style": "剧情", "duration": 99}), 180)

    def test_script_to_video_drama_bad_duration_falls_back_to_10s(self):
        """时长字段非法时按默认 10s 计价，不允许 cost_of 抛异常打穿提交链路"""
        self.assertEqual(
            self.points.cost_of("script_to_video", {"scenes": [{"scene": "画面"}], "style": "剧情", "duration": "abc"}), 120)

    def test_script_to_video_drama_forwards_model_pricing(self):
        self.assertEqual(
            self.points.cost_of("script_to_video", {
                "scenes": [{"scene": "画面"}], "style": "剧情", "duration": 10,
                "model": "grok-imagine-video-1.5", "resolution": "1080p",
            }),
            self.points.cost_of("xiaole_video", {
                "channel": "grok", "duration": 10,
                "model": "grok-imagine-video-1.5", "resolution": "1080p",
            }),
        )

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

    def test_breakdown_partial_refund_is_persistent_idempotent_and_retryable(self):
        core = importlib.import_module("content_domains.core")
        original_db = core.JOB_DB
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            core.JOB_DB = str(Path(directory) / "jobs.db")
            try:
                with sqlite3.connect(core.JOB_DB) as connection:
                    connection.execute(
                        "CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT)")
                    connection.execute("INSERT INTO jobs(id,status) VALUES(99,'running')")
                    connection.commit()
                result = {
                    "type": "breakdown_batch", "total": 5,
                    "errors": [{"url": "a"}, {"url": "b"}],
                }
                self.assertTrue(
                    self.points.prepare_breakdown_batch_refund("fang", 100, result, 99))
                with sqlite3.connect(core.JOB_DB) as connection:
                    connection.execute("UPDATE jobs SET status='done' WHERE id=99")
                    connection.commit()
                calls = []
                def flaky(username, amount, reason="", transaction_key=""):
                    calls.append((username, amount, transaction_key))
                    if len(calls) == 1:
                        raise RuntimeError("auth unavailable")
                    return 1040
                with mock.patch.object(self.points, "refund_points", side_effect=flaky):
                    self.assertEqual("pending", self.points.reconcile_breakdown_refund(99))
                    self.assertEqual(1, self.points.retry_breakdown_refunds())
                    self.assertEqual("refunded", self.points.reconcile_breakdown_refund(99))
                self.assertEqual(2, len(calls))
                self.assertEqual(40, calls[0][1])
                self.assertEqual(calls[0][2], calls[1][2])
                self.assertEqual(
                    "breakdown-partial-refund:99:fang", calls[0][2])
                with sqlite3.connect(core.JOB_DB) as connection:
                    state, attempts = connection.execute(
                        "SELECT state,attempts FROM breakdown_partial_refunds WHERE job_id=99"
                    ).fetchone()
                self.assertEqual(("refunded", 2), (state, attempts))
            finally:
                core.JOB_DB = original_db

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
