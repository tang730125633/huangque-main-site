# -*- coding: utf-8 -*-
"""果肉生图（xiaole 渠道）抗压修复的回归测试。

背景（2026-07-19 主站 50 齐点压测报告）：50 提交 30 落库仅 10 成功，
20 条失败里 17 条是上游按 API Key 熔断「当前 API Key 媒体任务过多」。
当时 `_xiaole_request` 只有 5 次 429 退避（~120s），扛不住持续数分钟的整批饱和；
轮询 GET 一次瞬时错误就杀死已在飞（已计费）的任务。

修复（server/content_domains/image.py）：
  1. 创建调用限流专项重试（预算 XIAOLE_IMG_CREATE_MAX_WAIT，只重试限流）；
  2. 进程级并发闸 _XIAOLE_IMG_SEM（默认 5，Key 与果肉/豆姐视频共用）；
  3. 轮询瞬时错误连续 5 次才放弃。

下列用例全部 mock `_xiaole_request`，不打真实上游。
"""
import base64
import importlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

image = importlib.import_module("content_domains.image")

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n-fake").decode("ascii")
CREATE_OK = {"code": 200, "data": {"request_id": "r1", "status_url": "/api/v1/generations/r1"}}
POLL_OK = {"code": 200, "data": {"status": "succeeded", "output": {"images": [{"b64_json": PNG_B64}]}}}
RATE_LIMIT_MSG = "当前 API Key 媒体任务过多，请稍后再试"


def _fake_request(calls, post_results, get_results):
    """生成 _xiaole_request 替身：按队列依次返回/抛错，记录调用。"""
    def fake(method, path, body=None, timeout=90, retry_deadline=None):
        calls.append((method, path))
        queue = post_results if method == "POST" else get_results
        item = queue.pop(0) if queue else POLL_OK
        if isinstance(item, Exception):
            raise item
        return item
    return fake


class XiaoleImageRetryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._now = 0.0

        def advance(seconds):
            self._now += max(0.0, float(seconds))

        patches = [
            patch.object(image, "XIAOLEVIDEO_API_KEY", "test-key"),
            patch.object(image, "OUT_DIR", Path(self._tmp.name)),
            patch.object(image.time, "sleep", advance),
            patch.object(image.time, "monotonic", lambda: self._now),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, post_results, get_results=None):
        calls = []
        fake = _fake_request(calls, list(post_results), list(get_results or [POLL_OK]))
        with patch.object(image, "_xiaole_request", fake):
            result = image._gen_image_xiaole_locked("一只猫", "1:1", "high", 1, None)
        return result, calls

    def test_http_429_retried_then_succeeds(self):
        """HTTP 429（RuntimeError）被重试，第三次创建成功出图。"""
        result, calls = self._run([
            RuntimeError("视频接口失败: HTTP 429 %s" % RATE_LIMIT_MSG),
            RuntimeError("视频接口失败: HTTP 429 %s" % RATE_LIMIT_MSG),
            CREATE_OK,
        ])
        self.assertEqual(result["provider"], "xiaole")
        self.assertEqual([m for m, _ in calls].count("POST"), 3)

    def test_body_code_rate_limit_retried(self):
        """HTTP 200 的 body code=429 即使消息不含限流字样也会重试。"""
        result, calls = self._run([
            {"code": 429, "message": "busy"},
            CREATE_OK,
        ])
        self.assertEqual(result["provider"], "xiaole")
        self.assertEqual([m for m, _ in calls].count("POST"), 2)

    def test_create_budget_counts_time_spent_inside_request(self):
        """外层预算按墙钟计时，不会漏掉 _xiaole_request 内部消耗的时间。"""
        calls = []

        def slow_rate_limit(method, path, body=None, timeout=90, retry_deadline=None):
            calls.append((method, path))
            self._now += 120
            raise RuntimeError("视频接口失败: HTTP 429 busy")

        with patch.object(image, "XIAOLE_IMG_CREATE_MAX_WAIT", 300), \
             patch.object(image.random, "random", return_value=0.5), \
             patch.object(image, "_xiaole_request", slow_rate_limit):
            with self.assertRaisesRegex(ValueError, "限流"):
                image._gen_image_xiaole_locked("一只猫", "1:1", "high", 1, None)

        self.assertLessEqual([m for m, _ in calls].count("POST"), 2)
        self.assertLessEqual(self._now, 300)

    def test_non_rate_limit_error_not_retried(self):
        """内容审核/参数类错误绝不重试（重发=重复计费），一次即抛。"""
        with self.assertRaises(ValueError) as ctx:
            self._run([{"code": 400, "message": "内容审核未通过"}])
        self.assertIn("出图创建失败", str(ctx.exception))

    def test_rate_limit_budget_exhausted(self):
        """持续限流超过预算 → 放弃并给「限流」人话（走失败退点，不会死等）。"""
        with patch.object(image, "XIAOLE_IMG_CREATE_MAX_WAIT", 25):
            with self.assertRaises(ValueError) as ctx:
                self._run([RuntimeError("视频接口失败: HTTP 429 %s" % RATE_LIMIT_MSG)] * 100)
        self.assertIn("限流", str(ctx.exception))

    def test_poll_tolerates_transient_errors(self):
        """轮询 GET 瞬时失败不杀死在飞任务，恢复后照常出图。"""
        result, _ = self._run([CREATE_OK], [
            RuntimeError("视频接口网络异常: SSL 握手超时"),
            RuntimeError("视频接口失败: HTTP 502 bad gateway"),
            POLL_OK,
        ])
        self.assertEqual(result["provider"], "xiaole")
        self.assertEqual(result["count"], 1)

    def test_poll_gives_up_after_consecutive_errors(self):
        """轮询连续 5 次失败才放弃（避免单边网络故障无限占 worker）。"""
        with self.assertRaises(ValueError) as ctx:
            self._run([CREATE_OK], [RuntimeError("视频接口网络异常: down")] * 10)
        self.assertIn("状态查询连续失败", str(ctx.exception))

    def test_semaphore_caps_upstream_concurrency(self):
        """并发闸：8 线程同时进 _gen_image_xiaole，上游在飞峰值不超过闸值。
        用 Event 同步而不是 sleep：前 2 个线程都进入临界区后才放行，峰值必然打满闸值。"""
        gate = threading.BoundedSemaphore(2)
        state = {"cur": 0, "peak": 0}
        lock = threading.Lock()
        both_inside = threading.Event()

        def stub_locked(*_args):
            with lock:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
                if state["cur"] >= 2:
                    both_inside.set()
            both_inside.wait(timeout=5)
            with lock:
                state["cur"] -= 1
            return {"provider": "xiaole"}

        with patch.object(image, "_XIAOLE_IMG_SEM", gate), \
             patch.object(image, "_gen_image_xiaole_locked", stub_locked):
            threads = [threading.Thread(target=image._gen_image_xiaole,
                                        args=("p", "1:1", "high", 1, None)) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(state["peak"], 2)


if __name__ == "__main__":
    unittest.main()
