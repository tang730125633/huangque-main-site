# -*- coding: utf-8 -*-
"""果肉(xiaole) / 泽龙2(zelong2) 出图死线。

背景：xiaole hd 图生图实测稳定 ~300s，原来死线正好也是 300s —— 用户 15544499908
近 10 次失败里 6 次是「出图超时」。放宽到 600s。

守的不变量：
1. 两档死线都 < reaper KIND_GRACE["image"]=900s（否则放宽了死线，任务照样被 reaper 判超时退点）
2. zelong2 是**总**死线，不是单次 timeout —— 号池 N 个账号 × _retry 2 次，
   若只放宽单次 timeout，最坏耗时 N×2×timeout 会冲破 900s
3. 每次实际发请求都按剩余预算收紧 timeout；预算耗尽不再试下一个号
4. 都可由 env 覆盖
"""
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

image = importlib.import_module("content_domains.image")
core = importlib.import_module("content_domains.core")


class DeadlineBoundsTests(unittest.TestCase):
    def test_both_deadlines_are_600_by_default(self):
        self.assertEqual(600, image.XIAOLE_IMG_DEADLINE)
        self.assertEqual(600, image.ZELONG2_DEADLINE)

    def test_deadlines_stay_within_reaper_image_grace(self):
        """放宽死线不能越过 reaper 宽限，否则只是把「出图超时」换成「生成超时自动结束」。"""
        grace = core.KIND_GRACE["image"]
        self.assertLess(image.XIAOLE_IMG_DEADLINE, grace)
        self.assertLess(image.ZELONG2_DEADLINE, grace)

    def test_env_overridable(self):
        with patch.dict(os.environ, {"XIAOLE_IMG_DEADLINE": "420", "ZELONG2_DEADLINE": "450"}):
            reloaded = importlib.reload(image)
            self.assertEqual(420, reloaded.XIAOLE_IMG_DEADLINE)
            self.assertEqual(450, reloaded.ZELONG2_DEADLINE)
        importlib.reload(image)   # 还原默认，避免污染其它用例


class Zelong2TotalDeadlineTests(unittest.TestCase):
    """号池的总死线：不管几个号、每个号重试几次，总耗时受 ZELONG2_DEADLINE 约束。"""

    def setUp(self):
        self.accounts = [{"base": "b%d" % i, "key": "k%d" % i} for i in range(3)]

    def test_timeout_passed_is_remaining_budget_not_fixed_300(self):
        seen = {}

        def fake_post(*_a, **kw):
            seen["timeout"] = kw.get("timeout")
            return {"ok": 1}

        with patch.object(image, "_zelong2_attempts", lambda: self.accounts), \
             patch.object(image, "_post", fake_post):
            self.assertEqual({"ok": 1}, image._post_zelong2("/p", b"{}", "application/json"))

        self.assertIsNotNone(seen["timeout"], "必须显式传 timeout")
        self.assertLessEqual(seen["timeout"], image.ZELONG2_DEADLINE)
        self.assertGreater(seen["timeout"], image.ZELONG2_DEADLINE - 30, "首次尝试应几乎拿到全部预算")

    def test_exhausted_budget_stops_trying_more_accounts(self):
        """预算耗尽后不再尝试后续号 —— 否则 N 个号会把耗时叠到 reaper 之外。"""
        calls = []
        clock = {"t": 1000.0}

        def fake_time():
            return clock["t"]

        def fake_post(*_a, **_kw):
            calls.append(1)
            clock["t"] += 400.0            # 每次尝试烧掉 400s
            raise ValueError("非瞬时错误")  # ValueError 不被 _retry 重试

        with patch.object(image, "_zelong2_attempts", lambda: self.accounts), \
             patch.object(image, "_post", fake_post), \
             patch.object(image.time, "time", fake_time), \
             patch.object(image.time, "sleep", lambda _s: None):
            with self.assertRaises(ValueError):
                image._post_zelong2("/p", b"{}", "application/json")

        # 600s 预算：第1个号烧400s，第2个号开始时只剩200s（>5s，可试）再烧400s → 预算负，
        # 第3个号必须被跳过。所以总尝试次数 < 账号数。
        self.assertLess(len(calls), len(self.accounts), "预算耗尽后不应继续试后面的号")

    def test_error_message_mentions_deadline_when_exhausted(self):
        clock = {"t": 1000.0}

        def fake_post(*_a, **_kw):
            clock["t"] += 700.0            # 一次就烧光 600s 预算
            raise ValueError("boom")

        with patch.object(image, "_zelong2_attempts", lambda: self.accounts), \
             patch.object(image, "_post", fake_post), \
             patch.object(image.time, "time", lambda: clock["t"]), \
             patch.object(image.time, "sleep", lambda _s: None):
            with self.assertRaises(ValueError) as cm:
                image._post_zelong2("/p", b"{}", "application/json")
        self.assertIn("死线", str(cm.exception))


class PostTimeoutParamTests(unittest.TestCase):
    def test_core_post_accepts_timeout_and_defaults_to_300(self):
        import inspect
        sig = inspect.signature(core._post)
        self.assertIn("timeout", sig.parameters)
        self.assertEqual(300, sig.parameters["timeout"].default, "默认必须保持 300，不改其它引擎行为")


if __name__ == "__main__":
    unittest.main()
