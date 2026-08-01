# -*- coding: utf-8 -*-
"""HeyGen 429 必须退避重试 —— 但也【只有】429 能重试。

## 2026-07-12 实测（20 路同时提交：10 口播 + 10 剧情视频）

    提交完成: 成功 13/20  429 = 7  用时 11s
    错误码 `rate_limit_exceeded`，原文「please reduce the RATE to call this api」
    7 个 429 全部在 1.1 秒内【瞬间】返回
    而被接受的 13 条【全部成功出片】

两个关键推论：

1. **我们撞的是「速率」墙，不是「并发」墙。** 官方文档说的 Max Concurrent Video Jobs = 10
   根本没拦我们 —— 13 条同时生成全部成功。真正的限制是「一瞬间发太多请求」。
   （10 路同时提交时零 429；20 路就掐掉 7 个。）

2. **429 是唯一可以安全重发的失败。** 请求被瞬间拒绝、未被处理、【未计费】。
   而超时 / RST / 5xx 绝不能重发 —— HeyGen 提交即扣 credit，那些失败发生在请求
   已经送达之后，视频可能已经在生成、钱已经花了。
   （同一条纪律见 HeyGenBilledError、egress.post_json 的 _pre_delivery_failure、
   以及 image._seedream_post。这是全站第四次遇到同一个形状的问题。）

不重试的后果：一次突发就把用户的任务判死退点、白等几分钟 —— 而被拒的那些，
退避几秒重发几乎必成（20 路里 13 路本来就过了）。
"""
import importlib
import sys
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")


def _http_error(code, body=b"{}"):
    return urllib.error.HTTPError("https://api.heygen.com/v3/videos", code, "err", {}, BytesIO(body))


class ErrorClassificationTests(unittest.TestCase):
    """429 要单独成一类，不能和其它错误一起被 RuntimeError 一把抓。"""

    def _request(self, err):
        with patch.object(video, "HEYGEN_API_KEY", "k"), \
             patch.object(video, "HEYGEN_API_BASE", "https://relay.test/v3"), \
             patch.object(video.urllib.request, "urlopen", side_effect=err):
            return video._heygen_request_json("POST", "/videos")

    def test_429_becomes_its_own_exception(self):
        with self.assertRaises(video.HeyGenRateLimited):
            self._request(_http_error(429, b'{"error":{"code":"rate_limit_exceeded"}}'))

    def test_other_http_errors_stay_generic(self):
        for code in (400, 402, 500):
            with self.subTest(code=code):
                with self.assertRaises(RuntimeError) as ctx:
                    self._request(_http_error(code))
                self.assertNotIsInstance(ctx.exception, video.HeyGenRateLimited,
                                         "HTTP %d 不是限流，重发它可能会再扣一次钱" % code)


class RetryDisciplineTests(unittest.TestCase):
    def test_429_is_retried_until_it_succeeds(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise video.HeyGenRateLimited("429")
            return "vid1"

        with patch.object(video.time, "sleep"):
            self.assertEqual(video._heygen_retry_429(flaky, "测试"), "vid1")
        self.assertEqual(len(calls), 3)

    def test_a_timeout_is_never_retried(self):
        """超时【绝不能】重发 —— 请求可能已经送达，视频可能已经在生成、钱已经花了。

        这正是 HeyGen 的致命之处：提交即扣 credit。盲目重发 = 同一条视频付两次。
        """
        calls = []

        def times_out():
            calls.append(1)
            raise TimeoutError("The read operation timed out")

        with patch.object(video.time, "sleep"):
            with self.assertRaises(TimeoutError):
                video._heygen_retry_429(times_out, "测试")
        self.assertEqual(len(calls), 1, "超时被重发了 —— 可能已计费的请求绝不能重来")

    def test_a_500_is_never_retried(self):
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("HeyGen接口失败: HTTP 500")

        with patch.object(video.time, "sleep"):
            with self.assertRaises(RuntimeError):
                video._heygen_retry_429(boom, "测试")
        self.assertEqual(len(calls), 1)

    def test_gives_up_after_the_budget_and_reraises(self):
        def always429():
            raise video.HeyGenRateLimited("429")

        with patch.object(video.time, "sleep"):
            with self.assertRaises(video.HeyGenRateLimited):
                video._heygen_retry_429(always429, "测试")

    def test_retry_after_header_is_honoured(self):
        """HeyGen 在响应头里明确告诉我们该等多久 —— 听它的，比瞎猜指数退避准。

        官方文档：「Check the `Retry-After` response header for the number of seconds
        to wait before retrying.」
        """
        err = video.HeyGenRateLimited("429")
        err.retry_after = 30.0
        delays = []

        def always429():
            raise err

        with patch.object(video.time, "sleep", side_effect=delays.append):
            with self.assertRaises(video.HeyGenRateLimited):
                video._heygen_retry_429(always429, "测试")

        # 30s ± 抖动(0.7~1.3)，绝不能还是那个 2/4/8 的指数序列
        self.assertTrue(all(21 <= d <= 39 for d in delays), delays)

    def test_retry_after_is_still_jittered(self):
        """哪怕 Retry-After 给了确切秒数也要抖 —— 否则同一批被拒的 worker 会在同一刻
        一起重发，等于把突发原样搬到了退避之后，再撞一次 429。"""
        err = video.HeyGenRateLimited("429")
        err.retry_after = 30.0
        delays = []
        with patch.object(video.time, "sleep", side_effect=delays.append):
            try:
                video._heygen_retry_429(lambda: (_ for _ in ()).throw(err), "测试")
            except video.HeyGenRateLimited:
                pass
        self.assertGreater(len(set(delays)), 1, "Retry-After 被原样照抄，没有抖动")

    def test_header_is_parsed_from_the_response(self):
        err = _http_error(429, b'{"error":{"code":"rate_limit_exceeded"}}')
        err.headers = {"Retry-After": "12"}
        with patch.object(video, "HEYGEN_API_KEY", "k"), \
             patch.object(video, "HEYGEN_API_BASE", "https://relay.test/v3"), \
             patch.object(video.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(video.HeyGenRateLimited) as ctx:
                video._heygen_request_json("POST", "/videos")
        self.assertEqual(ctx.exception.retry_after, 12.0)

    def test_backoff_is_jittered(self):
        """不加抖动，同一批 worker 退避后又会撞在一起 —— 那正是 429 的成因。"""
        delays = []
        with patch.object(video.time, "sleep", side_effect=delays.append):
            try:
                video._heygen_retry_429(lambda: (_ for _ in ()).throw(video.HeyGenRateLimited("429")), "测试")
            except video.HeyGenRateLimited:
                pass
        self.assertGreater(len(delays), 1)
        self.assertNotEqual(len(set(delays)), 1, "退避时间完全相同 = 没有抖动")


class EveryCreateIsProtectedTests(unittest.TestCase):
    """每一处「建任务」的调用都要有 429 重试 —— 漏一处，那条路径就会被一次突发打死。"""

    def test_all_four_create_paths_retry_on_429(self):
        src = Path(video.__file__).read_text(encoding="utf-8")
        for label in ("口播直连", "口播中转", "剧情视频", "建形象"):
            self.assertIn('"%s")' % label, src)
        self.assertGreaterEqual(src.count("_heygen_retry_429("), 4)

    def test_polling_is_not_wrapped(self):
        """轮询是幂等的 GET，而且它发生在【已计费之后】——不该也不需要走 429 重试包装。

        （真要限流，轮询自己的循环会继续转；把它包进重试只会把语义搅浑。）
        """
        src = Path(video.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_heygen_retry_429(lambda: _heygen_poll_video", src)


if __name__ == "__main__":
    unittest.main()
