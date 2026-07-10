# -*- coding: utf-8 -*-
"""#6 修作图失败率：base64 padding 容错 + 中转出图瞬时失败退避重试。"""
import base64
import importlib
import sys
import unittest
import urllib.error
from pathlib import Path


def _load_image_module():
    server_dir = str(Path(__file__).resolve().parents[1] / "server")
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)
    return importlib.import_module("content_domains.image")


image = _load_image_module()


class CleanB64Tests(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(image._clean_b64(""))
        self.assertIsNone(image._clean_b64(None))
        self.assertIsNone(image._clean_b64("   "))

    def test_strips_data_url_prefix(self):
        raw = base64.b64encode(b"abc").decode()
        cleaned = image._clean_b64("data:image/png;base64," + raw)
        self.assertEqual(base64.b64decode(cleaned), b"abc")

    def test_strips_whitespace_and_newlines(self):
        raw = base64.b64encode(b"hello world").decode()
        noisy = raw[:4] + "\n  " + raw[4:] + "\t"
        self.assertEqual(base64.b64decode(image._clean_b64(noisy)), b"hello world")

    def test_fixes_missing_padding(self):
        # "abcd" → "YWJjZA=="；去掉尾部 padding 后应被补回，不再抛 Incorrect padding
        stripped = base64.b64encode(b"abcd").decode().rstrip("=")
        self.assertNotIn("=", stripped)
        cleaned = image._clean_b64(stripped)
        self.assertEqual(base64.b64decode(cleaned), b"abcd")

    def test_wellformed_passthrough(self):
        raw = base64.b64encode(b"payload-bytes").decode()
        self.assertEqual(image._clean_b64(raw), raw)


class IsTransientTests(unittest.TestCase):
    def _http(self, code):
        return urllib.error.HTTPError("http://x", code, "err", {}, None)

    def test_retryable_http_codes(self):
        for code in (429, 500, 502, 503, 504):
            self.assertTrue(image._is_transient(self._http(code)), code)

    def test_client_errors_not_retryable(self):
        for code in (400, 401, 403, 404, 422):
            self.assertFalse(image._is_transient(self._http(code)), code)

    def test_urlerror_and_timeouts_retryable(self):
        self.assertTrue(image._is_transient(urllib.error.URLError("timed out")))
        self.assertTrue(image._is_transient(TimeoutError("read timeout")))
        self.assertTrue(image._is_transient(ConnectionError("connection reset")))

    def test_value_error_not_retryable(self):
        # 泽龙2 号池全失败抛 ValueError，不能被 _retry 二次放大
        self.assertFalse(image._is_transient(ValueError("泽龙2号池全部失败")))


class RetryTests(unittest.TestCase):
    def setUp(self):
        self._sleep = image.time.sleep
        image.time.sleep = lambda *_: None  # 别在测试里真睡

    def tearDown(self):
        image.time.sleep = self._sleep

    def test_transient_then_success(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("timed out")
            return {"ok": True}

        self.assertEqual(image._retry(flaky), {"ok": True})
        self.assertEqual(calls["n"], 2)

    def test_non_transient_no_retry(self):
        calls = {"n": 0}

        def bad():
            calls["n"] += 1
            raise ValueError("内容审核拒绝")

        with self.assertRaises(ValueError):
            image._retry(bad)
        self.assertEqual(calls["n"], 1)  # 只试一次，不重试

    def test_exhausts_and_raises_last(self):
        calls = {"n": 0}

        def always():
            calls["n"] += 1
            raise urllib.error.URLError("down")

        with self.assertRaises(urllib.error.URLError):
            image._retry(always, tries=2)
        self.assertEqual(calls["n"], 2)  # tries 上限内不再多试


class DispatchRetryTests(unittest.TestCase):
    """确认重试真接在中转路径上：zelong 单发 + zelong2 号池。"""

    def setUp(self):
        self._post = image._post
        self._sleep = image.time.sleep
        image.time.sleep = lambda *_: None

    def tearDown(self):
        image._post = self._post
        image.time.sleep = self._sleep

    def test_zelong_dispatch_retries_transient(self):
        calls = {"n": 0}

        def fake_post(path, data, ctype, base=None, key=None, proxy=True):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError("http://x", 503, "bad gateway", {}, None)
            return {"data": [{"b64_json": "x"}]}

        image._post = fake_post
        out = image._dispatch_gpt("zelong", "/v1/images/generations", b"{}", "application/json",
                                  "https://relay", "k", False)
        self.assertEqual(out, {"data": [{"b64_json": "x"}]})
        self.assertEqual(calls["n"], 2)

    def test_zelong2_pool_retries_within_account(self):
        calls = {"n": 0}

        def fake_post(path, data, ctype, base=None, key=None, proxy=True):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("connection reset")
            return {"data": [{"b64_json": "y"}]}

        image._post = fake_post
        image.os.environ["ZELONG2_KEYS"] = "poolkey"
        try:
            out = image._post_zelong2("/v1/images/generations", b"{}", "application/json")
        finally:
            image.os.environ.pop("ZELONG2_KEYS", None)
        self.assertEqual(out, {"data": [{"b64_json": "y"}]})
        self.assertEqual(calls["n"], 2)  # 同一号内重试成功，未耗尽号池


if __name__ == "__main__":
    unittest.main()
