# -*- coding: utf-8 -*-
"""leadgen_api 的两处修复：

1. COS 转存的总预算（#11）
   线上 23 次转存失败全部是 "The read operation timed out"。原实现 tikhub._http_get(timeout=120)
   的 timeout 只管单次 socket 读，慢 CDN 上 read 会反复续命；再加盲目重试 2 次，最坏在转存上
   耗 240s+，把整个 collect 任务顶过 reaper 判死线 → 判死退点 → worker 又写回 done。
   现在改成分块读 + 每块检查总预算，超预算立即放弃且不再重试。

2. 退点走 auth 服务（#9）
   原 add_points 直接 UPDATE users.db，没有事务、不进 points_audit，collect/leads 的退点在
   审计里完全隐形。改为调 auth 的 refund 接口；auth 不可用时回退直写 —— 宁可少一条审计，
   也不能把用户的点吞了。
"""
import importlib, io, sys, time, unittest
from pathlib import Path


class _FakeResponse(io.BytesIO):
    """够用的 urlopen 返回体替身：支持 with、.headers、.read(n)。"""

    def __init__(self, data, headers=None, chunk_delay=0.0):
        super().__init__(data)
        self.headers = headers or {}
        self._delay = chunk_delay

    def read(self, n=-1):
        if self._delay:
            time.sleep(self._delay)
        return super().read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class CosBudgetTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.lg = importlib.import_module("leadgen_api")
        self.tikhub = importlib.import_module("tikhub")
        self._orig_opener = self.tikhub._OPENER

    def tearDown(self):
        self.tikhub._OPENER = self._orig_opener

    def _stub_opener(self, response):
        class _O:
            def open(self, req, timeout=None):
                return response
        self.tikhub._OPENER = _O()

    def test_fetch_success(self):
        self._stub_opener(_FakeResponse(b"x" * 1000))
        data = self.lg._fetch_within_budget("http://cdn/v.mp4", time.time() + 30)
        self.assertEqual(len(data), 1000)

    def test_rejects_oversize_by_content_length(self):
        """Content-Length 预检：下载前就否掉，省掉整段无用等待。"""
        big = str(self.lg.COS_FETCH_MAX_BYTES + 1)
        self._stub_opener(_FakeResponse(b"", {"Content-Length": big}))
        with self.assertRaises(ValueError) as ctx:
            self.lg._fetch_within_budget("http://cdn/v.mp4", time.time() + 30)
        self.assertIn("超过上限", str(ctx.exception))

    def test_rejects_oversize_while_streaming(self):
        """CDN 不给 Content-Length 时，边下边数，超限即停。"""
        self.lg.COS_FETCH_MAX_BYTES, orig = 4096, self.lg.COS_FETCH_MAX_BYTES
        try:
            self._stub_opener(_FakeResponse(b"x" * 100000))
            with self.assertRaises(ValueError):
                self.lg._fetch_within_budget("http://cdn/v.mp4", time.time() + 30)
        finally:
            self.lg.COS_FETCH_MAX_BYTES = orig

    def test_deadline_already_expired(self):
        self._stub_opener(_FakeResponse(b"x"))
        with self.assertRaises(TimeoutError):
            self.lg._fetch_within_budget("http://cdn/v.mp4", time.time() - 1)

    def test_deadline_exceeded_midstream(self):
        """核心回归：慢 CDN 每块都拖时间，到点必须放弃，而不是无限续命。"""
        self._stub_opener(_FakeResponse(b"x" * 1000000, chunk_delay=0.05))
        t0 = time.time()
        with self.assertRaises(TimeoutError) as ctx:
            self.lg._fetch_within_budget("http://cdn/v.mp4", time.time() + 0.2)
        self.assertLess(time.time() - t0, 2.0, "超预算后仍在继续下载")
        self.assertIn("预算", str(ctx.exception))

    def test_fallback_returns_original_url_and_does_not_raise(self):
        """转存失败必须回退原链接，绝不中断采集。"""
        class _Boom:
            def open(self, req, timeout=None):
                raise OSError("The read operation timed out")
        self.tikhub._OPENER = _Boom()
        orig_cos = sys.modules.get("content_domains.cos")
        import content_domains.cos as cos
        enabled, cos.enabled = cos.enabled, lambda: True
        try:
            out = self.lg.public_url_from_remote("http://cdn/v.mp4", "collect/douyin/1.mp4", "video/mp4")
            self.assertEqual(out, "http://cdn/v.mp4")
        finally:
            cos.enabled = enabled
            if orig_cos is not None:
                sys.modules["content_domains.cos"] = orig_cos


class RefundAuditTests(unittest.TestCase):
    """add_points 同时被扣点(负 delta)和退点(正 delta)调用。

    auth 的 /deduct 与 /refund 都校验 `amount >= 0`（auth_server.py），所以必须按符号
    分流到不同端点并传绝对值。第一版把两者都路由到 /refund，导致每次扣点都拿到 400
    然后回退直写 —— 扣点依然绕过 points_audit，且热路径上多一次注定失败的 HTTP 往返。
    最初的测试只覆盖了正数 delta，所以没抓到。
    """

    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.lg = importlib.import_module("leadgen_api")
        self._orig_auth = self.lg._auth_points
        self._orig_direct = self.lg._add_points_direct
        self.direct_calls = []
        self.lg._add_points_direct = lambda u, d: (self.direct_calls.append((u, d)), True)[1]
        self.auth_calls = []

    def tearDown(self):
        self.lg._auth_points = self._orig_auth
        self.lg._add_points_direct = self._orig_direct

    def _auth(self, status, data=None):
        def _f(path, u, a):
            self.auth_calls.append((path, u, a))
            return status, (data or {})
        self.lg._auth_points = _f

    # --- 核心回归：扣点走 /deduct，退点走 /refund，且金额一律非负 ---
    def test_refund_uses_refund_endpoint(self):
        self._auth(200, {"points": 9})
        self.assertTrue(self.lg.add_points("u", 6))
        self.assertEqual(self.auth_calls, [("/api/auth/points/refund", "u", 6)])
        self.assertEqual(self.direct_calls, [], "auth 成功时不该直写 users.db")

    def test_deduct_uses_deduct_endpoint_with_positive_amount(self):
        self._auth(200, {"points": 3})
        self.assertTrue(self.lg.add_points("u", -6))
        self.assertEqual(self.auth_calls, [("/api/auth/points/deduct", "u", 6)],
                         "扣点必须走 /deduct 且传绝对值；传负数会被 auth 以 400 拒绝")
        self.assertEqual(self.direct_calls, [])

    def test_insufficient_points_does_not_fall_back(self):
        """402 是业务结论不是故障：回退直写等于绕过 auth 的余额校验硬扣。"""
        self._auth(402, {"detail": "点数不足"})
        self.assertFalse(self.lg.add_points("u", -6))
        self.assertEqual(self.direct_calls, [], "余额不足时绝不能直写扣点")

    def test_zero_delta_is_noop(self):
        self._auth(500)
        self.assertTrue(self.lg.add_points("u", 0))
        self.assertEqual(self.auth_calls, [])

    # --- auth 故障时的兜底：宁可审计缺一条，也不能吞用户的点 ---
    def test_falls_back_to_direct_write_when_auth_fails(self):
        self._auth(500, {"detail": "HQ_INTERNAL_TOKEN 未配置"})
        self.assertTrue(self.lg.add_points("u", 6))
        self.assertEqual(self.direct_calls, [("u", 6)])

    def test_deduct_falls_back_on_auth_outage(self):
        self._auth(500, {"detail": "points update failed"})
        self.assertTrue(self.lg.add_points("u", -6))
        self.assertEqual(self.direct_calls, [("u", -6)])

    def test_falls_back_on_http_error(self):
        self._auth(403, {"detail": "forbidden"})
        self.assertTrue(self.lg.add_points("u", 6))
        self.assertEqual(self.direct_calls, [("u", 6)])

    def test_auth_points_without_token_short_circuits(self):
        token, self.lg.INTERNAL_TOKEN = self.lg.INTERNAL_TOKEN, ""
        try:
            status, data = self.lg._auth_points("/api/auth/points/refund", "u", 6)
            self.assertEqual(status, 500)
            self.assertIn("HQ_INTERNAL_TOKEN", data["detail"])
        finally:
            self.lg.INTERNAL_TOKEN = token


class DirectWriteFallbackTests(unittest.TestCase):
    """兜底直写必须保留余额校验：MAX(0, points+delta) 会把余额不足的用户硬扣到 0。"""

    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.lg = importlib.import_module("leadgen_api")
        import sqlite3, tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_db = self.lg.AUTH_DB
        self.lg.AUTH_DB = str(Path(self.tmp.name) / "users.db")
        c = sqlite3.connect(self.lg.AUTH_DB)
        c.execute("CREATE TABLE users(username TEXT PRIMARY KEY, points INTEGER)")
        c.execute("INSERT INTO users VALUES('u', 5)")
        c.commit(); c.close()

    def tearDown(self):
        self.lg.AUTH_DB = self._orig_db
        self.tmp.cleanup()

    def _points(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.lg.AUTH_DB)) as c:   # `with sqlite3.connect(...)` 只提交不关闭
            return c.execute("SELECT points FROM users WHERE username='u'").fetchone()[0]

    def test_direct_deduct_respects_balance(self):
        self.assertFalse(self.lg._add_points_direct("u", -9), "余额 5 扣 9 必须失败")
        self.assertEqual(self._points(), 5, "余额不足却被扣了")

    def test_direct_deduct_succeeds_within_balance(self):
        self.assertTrue(self.lg._add_points_direct("u", -5))
        self.assertEqual(self._points(), 0)

    def test_direct_refund_adds(self):
        self.assertTrue(self.lg._add_points_direct("u", 3))
        self.assertEqual(self._points(), 8)

    def test_unknown_user_reports_failure(self):
        self.assertFalse(self.lg._add_points_direct("nobody", 3))


class PermanentUrlTests(unittest.TestCase):
    """资产库要能区分「永久直链」和「会过期的第三方 CDN 直链」。"""

    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.store = importlib.import_module("content_domains.assets_store")

    def test_cos_url_is_permanent(self):
        self.assertTrue(self.store._is_permanent_url(
            "https://huangque-media-1435693839.cos.ap-guangzhou.myqcloud.com/collect/douyin/1.mp4"))

    def test_third_party_cdn_is_not_permanent(self):
        for u in ("https://v5-dy-ov-experiment.zjcdn.com/abc",     # 抖音
                  "https://sns-v11.rednotecdn.com/abc",            # 小红书
                  "https://wxapp.tc.qq.com/abc"):                  # 视频号
            self.assertFalse(self.store._is_permanent_url(u), u)

    def test_empty_url_is_not_permanent(self):
        self.assertFalse(self.store._is_permanent_url(""))
        self.assertFalse(self.store._is_permanent_url(None))

    def test_presigned_private_bucket_url_is_not_permanent(self):
        """COS_PUBLIC=0 时 cos.py 返回带签名的临时链接(默认 7 天)，host 同样是 myqcloud.com。
        只看域名会把它误判成永久，用户 7 天后点开是死链且全程无提示。"""
        signed = ("https://hq-1435693839.cos.ap-guangzhou.myqcloud.com/collect/douyin/1.mp4"
                  "?q-sign-algorithm=sha1&q-ak=AKID&q-sign-time=1&q-signature=abc")
        self.assertFalse(self.store._is_permanent_url(signed))

    def test_expires_style_signature_is_not_permanent(self):
        self.assertFalse(self.store._is_permanent_url(
            "https://hq.cos.ap-guangzhou.myqcloud.com/a.mp4?Expires=1783500000&Signature=xyz"))

    def test_host_match_is_suffix_not_substring(self):
        """原实现用子串包含，notmyqcloud.com.evil.net 会被判成永久。"""
        self.assertFalse(self.store._is_permanent_url("https://notmyqcloud.com.evil.net/a.mp4"))
        self.assertFalse(self.store._is_permanent_url("https://myqcloud.com.evil.net/a.mp4"))
        self.assertTrue(self.store._is_permanent_url("https://x.cos.ap-guangzhou.myqcloud.com/a.mp4"))

    def test_custom_cos_domain_from_env(self):
        import os, importlib
        old = os.environ.get("COS_DOMAIN")
        os.environ["COS_DOMAIN"] = "https://video.huangquechuanmei.com"
        try:
            self.assertTrue(self.store._is_permanent_url("https://video.huangquechuanmei.com/a.mp4"))
        finally:
            if old is None:
                os.environ.pop("COS_DOMAIN", None)
            else:
                os.environ["COS_DOMAIN"] = old

    def test_collect_meta_carries_permanent_flag(self):
        _, _, url, meta = self.store._project("collect", {
            "video": {"title": "t", "play_url": "https://v5-dy-ov-experiment.zjcdn.com/x.mp4"}})
        self.assertEqual(url, "https://v5-dy-ov-experiment.zjcdn.com/x.mp4")
        self.assertFalse(meta["permanent"])


if __name__ == "__main__":
    unittest.main()
