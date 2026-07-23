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
import importlib, io, os, shutil, sys, tempfile, time, unittest
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
    """下载受总预算约束，且【流式落盘】——内存恒定，不再把整段视频攒在内存里。"""

    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.tikhub = importlib.import_module("tikhub")
        self._orig_opener = self.tikhub._OPENER
        self.tmp = tempfile.mkdtemp(prefix="hqdl-")

    def tearDown(self):
        self.tikhub._OPENER = self._orig_opener
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dest(self):
        return os.path.join(self.tmp, "v.mp4")

    def _stub_opener(self, response):
        class _O:
            def open(self, req, timeout=None):
                return response
        self.tikhub._OPENER = _O()

    def test_download_writes_to_file(self):
        self._stub_opener(_FakeResponse(b"x" * 1000))
        n = self.tikhub.download_to_file("http://cdn/v.mp4", time.time() + 30, self._dest())
        self.assertEqual(n, 1000)
        self.assertEqual(os.path.getsize(self._dest()), 1000)

    def test_rejects_oversize_by_content_length(self):
        """Content-Length 预检：下载前就否掉，省掉整段无用等待。"""
        self._stub_opener(_FakeResponse(b"", {"Content-Length": "999999999"}))
        with self.assertRaises(ValueError) as ctx:
            self.tikhub.download_to_file("http://cdn/v.mp4", time.time() + 30, self._dest(), max_bytes=1024)
        self.assertIn("超过上限", str(ctx.exception))

    def test_rejects_oversize_while_streaming(self):
        """CDN 不给 Content-Length 时，边下边数，超限即停。"""
        self._stub_opener(_FakeResponse(b"x" * 100000))
        with self.assertRaises(ValueError):
            self.tikhub.download_to_file("http://cdn/v.mp4", time.time() + 30, self._dest(), max_bytes=4096)

    def test_deadline_already_expired(self):
        self._stub_opener(_FakeResponse(b"x"))
        with self.assertRaises(TimeoutError):
            self.tikhub.download_to_file("http://cdn/v.mp4", time.time() - 1, self._dest())

    def test_deadline_exceeded_midstream(self):
        """核心回归：慢 CDN 每块都拖时间，到点必须放弃，而不是无限续命。"""
        self._stub_opener(_FakeResponse(b"x" * 1000000, chunk_delay=0.05))
        t0 = time.time()
        with self.assertRaises(TimeoutError) as ctx:
            self.tikhub.download_to_file("http://cdn/v.mp4", time.time() + 0.2, self._dest())
        self.assertLess(time.time() - t0, 2.0, "超预算后仍在继续下载")
        self.assertIn("预算", str(ctx.exception))

    def test_fallback_returns_original_url_and_does_not_raise(self):
        """转存失败必须回退原链接，绝不中断采集。"""
        lg = importlib.import_module("leadgen_api")

        class _Boom:
            def open(self, req, timeout=None):
                raise OSError("The read operation timed out")

        self.tikhub._OPENER = _Boom()
        import content_domains.cos as cos
        enabled, cos.enabled = cos.enabled, lambda: True
        try:
            out = lg.public_url_from_remote("http://cdn/v.mp4", "collect/douyin/1.mp4", "video/mp4")
            self.assertEqual(out, "http://cdn/v.mp4")
        finally:
            cos.enabled = enabled


class DownloadOnceTests(unittest.TestCase):
    """采集流程原来把同一个 play_url 下两次：一次转存 COS、一次 ASR。

    线上 job 1354：5.1MB 的文件，第一次(转存)耗时 130s 且读超时失败，第二次(ASR)只花 20.5s。
    现在下载一次落盘、路径复用给 ASR，且临时文件必须被删干净。
    """

    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.lg = importlib.import_module("leadgen_api")
        self.downloads = []
        self._orig_dl = self.lg._download_with_retry
        self._orig_store = self.lg.store_video_file
        self._orig_cos = self.lg._cos_enabled

        def _fake_dl(url, key, dest):
            self.downloads.append(url)
            with open(dest, "wb") as f:
                f.write(b"MP4DATA")
            return 7

        self.lg._download_with_retry = _fake_dl
        self.lg.store_video_file = lambda path, key, ct=None: "https://cos/%s" % key
        self.lg._cos_enabled = lambda: True

    def tearDown(self):
        self.lg._download_with_retry = self._orig_dl
        self.lg.store_video_file = self._orig_store
        self.lg._cos_enabled = self._orig_cos

    def test_keep_file_returns_path_and_downloads_once(self):
        url, path = self.lg.fetch_and_store("http://cdn/v.mp4", "collect/douyin/1.mp4", "video/mp4", keep_file=True)
        try:
            self.assertEqual(url, "https://cos/collect/douyin/1.mp4")
            self.assertTrue(os.path.isfile(path), "keep_file=True 时文件必须留给调用方")
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"MP4DATA")
            self.assertEqual(len(self.downloads), 1, "同一个 URL 只该下载一次")
        finally:
            os.unlink(path)

    def test_without_keep_file_temp_is_removed(self):
        url, path = self.lg.fetch_and_store("http://cdn/v.mp4", "k", "video/mp4", keep_file=False)
        self.assertEqual(url, "https://cos/k")
        self.assertIsNone(path, "不需要文件时不该把路径漏给调用方")

    def test_no_cos_no_file_means_no_download(self):
        """既不转存也不做 ASR，就别白下一遍视频。"""
        self.lg._cos_enabled = lambda: False
        url, path = self.lg.fetch_and_store("http://cdn/v.mp4", "k", "video/mp4", keep_file=False)
        self.assertEqual(url, "http://cdn/v.mp4")
        self.assertIsNone(path)
        self.assertEqual(self.downloads, [], "不该发起下载")

    def test_cos_disabled_but_asr_needed_still_downloads(self):
        self.lg._cos_enabled = lambda: False
        url, path = self.lg.fetch_and_store("http://cdn/v.mp4", "k", "video/mp4", keep_file=True)
        try:
            self.assertEqual(url, "http://cdn/v.mp4", "没转存就回退原链接")
            self.assertTrue(os.path.isfile(path), "ASR 仍需要文件")
            self.assertEqual(len(self.downloads), 1)
        finally:
            os.unlink(path)

    def test_download_failure_falls_back_and_cleans_up(self):
        self.lg._download_with_retry = lambda url, key, dest: 0
        url, path = self.lg.fetch_and_store("http://cdn/v.mp4", "k", "video/mp4", keep_file=True)
        self.assertEqual(url, "http://cdn/v.mp4")
        self.assertIsNone(path, "拿不到文件时 ASR 会自己再下一次，不能给它半截文件")

    def test_cos_upload_failure_falls_back_but_keeps_file(self):
        """转存失败不该连累 ASR —— 文件已经在磁盘上了。"""
        self.lg.store_video_file = lambda path, key, ct=None: None
        url, path = self.lg.fetch_and_store("http://cdn/v.mp4", "k", "video/mp4", keep_file=True)
        try:
            self.assertEqual(url, "http://cdn/v.mp4")
            self.assertTrue(os.path.isfile(path))
        finally:
            os.unlink(path)

    def test_temp_file_removed_when_not_kept(self):
        """keep_file=False 的路径必须删临时文件，否则 /tmp 会被 100MB 的视频塞爆。"""
        seen = {}

        def _spy_dl(url, key, dest):
            seen["path"] = dest
            with open(dest, "wb") as f:
                f.write(b"X")
            return 1

        self.lg._download_with_retry = _spy_dl
        self.lg.fetch_and_store("http://cdn/v.mp4", "k", "video/mp4", keep_file=False)
        self.assertFalse(os.path.exists(seen["path"]), "临时文件没删")

    def test_public_url_from_remote_keeps_old_signature(self):
        self.assertEqual(self.lg.public_url_from_remote("http://cdn/v.mp4", "k", "video/mp4"), "https://cos/k")


class TranscriptReuseTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.tikhub = importlib.import_module("tikhub")
        self.downloads = []
        self.tmp = tempfile.mkdtemp(prefix="hqasr-")
        self._orig_dl = self.tikhub.download_to_file
        self._orig_whisper = self.tikhub._whisper
        self._orig_http_get = self.tikhub._http_get

        def _fake_dl(url, deadline, dest, **kw):
            self.downloads.append(url)
            with open(dest, "wb") as f:
                f.write(b"DOWNLOADED")
            return 10

        def _fake_whisper(path, filename="v.mp4"):
            with open(path, "rb") as f:
                return "文案:" + f.read().decode()

        self.tikhub.download_to_file = _fake_dl
        self.tikhub._whisper = _fake_whisper

    def tearDown(self):
        self.tikhub.download_to_file = self._orig_dl
        self.tikhub._whisper = self._orig_whisper
        self.tikhub._http_get = self._orig_http_get
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _existing_file(self, content=b"REUSED"):
        path = os.path.join(self.tmp, "reuse.mp4")
        with open(path, "wb") as f:
            f.write(content)
        return path

    def test_reuses_given_file_without_downloading(self):
        r = self.tikhub.transcript({"platform": "douyin", "play_url": "http://cdn/v.mp4"},
                                   video_path=self._existing_file())
        self.assertEqual(r, {"text": "文案:REUSED", "source": "asr"})
        self.assertEqual(self.downloads, [], "给了文件还去下载，等于没修")

    def test_given_file_is_not_deleted_by_transcript(self):
        """文件归调用方(gen_collect 的 finally)删，transcript 不许动它。"""
        path = self._existing_file()
        self.tikhub.transcript({"platform": "douyin", "play_url": "x"}, video_path=path)
        self.assertTrue(os.path.isfile(path))

    def test_downloads_when_no_file_given(self):
        r = self.tikhub.transcript({"platform": "douyin", "play_url": "http://cdn/v.mp4"})
        self.assertEqual(r["text"], "文案:DOWNLOADED")
        self.assertEqual(self.downloads, ["http://cdn/v.mp4"])

    def test_self_downloaded_temp_is_cleaned_up(self):
        """transcript 自己下的文件，自己删。"""
        seen = {}

        def _spy(url, deadline, dest, **kw):
            seen["path"] = dest
            with open(dest, "wb") as f:
                f.write(b"X")
            return 1

        self.tikhub.download_to_file = _spy
        self.tikhub.transcript({"platform": "douyin", "play_url": "http://cdn/v.mp4"})
        self.assertFalse(os.path.exists(seen["path"]), "自己下的临时文件没删")

    def test_channels_still_skipped_even_with_file(self):
        self.assertIsNone(self.tikhub.transcript({"platform": "channels", "play_url": "x"},
                                                 video_path=self._existing_file()))

    def test_subtitle_wins_over_file(self):
        """小红书有官方字幕就别跑 ASR —— 字幕是白送的、更准。"""
        srt = "1\n00:00:01,000 --> 00:00:02,000\n你好\n".encode("utf-8")
        self.tikhub._http_get = lambda url, **kw: srt
        r = self.tikhub.transcript({"platform": "xhs", "subtitle_url": "http://x/s.srt"},
                                   video_path=self._existing_file())
        self.assertEqual(r["source"], "subtitle")
        self.assertEqual(self.downloads, [], "有字幕就不该下视频")


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
        def _f(path, u, a, reason="", transaction_key=""):
            call = (path, u, a, reason, transaction_key) if transaction_key else (path, u, a, reason)
            self.auth_calls.append(call)
            return status, (data or {})
        self.lg._auth_points = _f

    # --- 核心回归：扣点走 /deduct，退点走 /refund，且金额一律非负 ---
    def test_refund_uses_refund_endpoint(self):
        self._auth(200, {"points": 9})
        self.assertTrue(self.lg.add_points("u", 6))
        self.assertEqual(self.auth_calls, [("/api/auth/points/refund", "u", 6, "")])
        self.assertEqual(self.direct_calls, [], "auth 成功时不该直写 users.db")

    def test_deduct_uses_deduct_endpoint_with_positive_amount(self):
        self._auth(200, {"points": 3})
        self.assertTrue(self.lg.add_points("u", -6))
        self.assertEqual(self.auth_calls, [("/api/auth/points/deduct", "u", 6, "")],
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

    def test_keyed_refund_never_direct_writes_after_lost_response(self):
        self._auth(500, {"detail": "response lost"})
        self.assertFalse(self.lg.add_points("u", 6, "job#42", "job-refund:42"))
        self.assertEqual(self.auth_calls, [
            ("/api/auth/points/refund", "u", 6, "job#42", "job-refund:42")
        ])
        self.assertEqual(self.direct_calls, [])

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


class CollectImageCosTests(unittest.TestCase):
    """采集封面 + 图文图片转存 COS 保永久 —— 抖音 douyinpic / 小红书 xhscdn 的 sign 链带时效会过期。"""

    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        os.environ.setdefault("CONTENT_BASE", tempfile.mkdtemp(prefix="hqcol-"))
        self.leads = importlib.import_module("content_domains.leads")
        self._orig = self.leads.public_url_from_remote
        # 假 COS：任何远端链 → 固定 COS 前缀 + key，便于断言 key 拼装
        self.leads.public_url_from_remote = lambda url, key, ct=None: "https://cos/%s" % key

    def tearDown(self):
        self.leads.public_url_from_remote = self._orig

    def test_remote_image_is_transcoded(self):
        got = self.leads._collect_cos_image(
            "https://p3-pc-sign.douyinpic.com/x~tplv.jpeg", "douyin", "vid123", "cover")
        self.assertEqual(got, "https://cos/collect/douyin/cover_vid123.jpg")

    def test_already_cos_is_not_reuploaded(self):
        u = "https://huangque-media.cos.ap-guangzhou.myqcloud.com/collect/douyin/x.jpg"
        self.assertEqual(self.leads._collect_cos_image(u, "douyin", "v", "cover"), u)

    def test_empty_or_non_http_passthrough(self):
        self.assertEqual(self.leads._collect_cos_image("", "douyin", "v", "cover"), "")
        self.assertIsNone(self.leads._collect_cos_image(None, "douyin", "v", "cover"))
        self.assertEqual(self.leads._collect_cos_image("data:image/png;base64,AA", "xhs", "v", "img0"),
                         "data:image/png;base64,AA")

    def test_id_is_sanitized_into_key(self):
        got = self.leads._collect_cos_image("http://cdn/a.jpg", "xhs", "a/b c?d", "img0")
        self.assertEqual(got, "https://cos/collect/xhs/img0_abcd.jpg")

    def test_transcode_failure_falls_back_to_original(self):
        """public_url_from_remote 自身 fail-open：转存失败/COS 未启用 → 原样返回，绝不中断采集。"""
        self.leads.public_url_from_remote = lambda url, key, ct=None: url
        u = "http://p3-pc-sign.douyinpic.com/x.jpg"
        self.assertEqual(self.leads._collect_cos_image(u, "douyin", "v", "cover"), u)


if __name__ == "__main__":
    unittest.main()
