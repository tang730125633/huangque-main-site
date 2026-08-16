# -*- coding: utf-8 -*-
"""HeyGen 轮询/下载遇瞬时网络抖动要重试 —— 但【只有幂等 GET】能重试，提交 POST 不行。

## 线上背景（#605）

egress 隧道一天 flap 5 次。每次抖动撞上一个正在轮询的任务，就把「已提交、成片已在
HeyGen 生成好」的任务判死、白烧一次提交费（cinematic 每条约 $7）。2026-07-12 单日 5 条
因此丢片（fang/qilin/yuelei，已手动 re-poll 全部挽回）。

根因：`_heygen_poll_video` / 下载对传输层网络错误【零重试】，而这一段是 `GET /videos/{id}`
+ 下载成片 —— 幂等、不计费、不改状态，本该重试。尤其「read timeout」发生在 r.read()
阶段，是 TimeoutError 而非 URLError，原来的 `except URLError` 根本没接住它。

## 两条不能混的纪律

1. **提交 POST /videos**：非 429 的失败绝不能重发（可能已计费）。网络错也不行——
   HeyGenNetworkError 是 RuntimeError 子类，`_heygen_retry_429` 不认它，照旧穿透 →
   HeyGenBilledError。本测试锁死这条不回归。
2. **轮询/下载 GET**：幂等、不计费 —— 瞬时网络错误退避重试，一次 SSL 抖动不该烧 $7。
"""
import ast
import importlib
import socket
import ssl
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


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _unwrapped_upload_lines(source):
    tree = ast.parse(source)
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    failures = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node.func) != "_heygen_upload_asset":
            continue
        current = parents.get(node)
        wrapped = False
        while current is not None:
            if (isinstance(current, ast.Call)
                    and _call_name(current.func) == "_heygen_retry_net"):
                wrapped = True
                break
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                break
            current = parents.get(current)
        if not wrapped:
            failures.append(node.lineno)
    return failures


def _http_error(code, body=b"{}"):
    return urllib.error.HTTPError("https://api.heygen.com/v3/videos", code, "err", {}, BytesIO(body))


class NetworkErrorClassificationTests(unittest.TestCase):
    """传输层瞬时错误要归成 HeyGenNetworkError（可被幂等 GET 重试）；HTTP 状态错误不算。"""

    def _request(self, err):
        with patch.object(video, "HEYGEN_API_KEY", "k"), \
             patch.object(video, "HEYGEN_API_BASE", "https://relay.test/v3"), \
             patch.object(video.urllib.request, "urlopen", side_effect=err):
            return video._heygen_request_json("GET", "/videos/x")

    def test_url_error_becomes_network_error(self):
        with self.assertRaises(video.HeyGenNetworkError):
            self._request(urllib.error.URLError(ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")))

    def test_read_timeout_becomes_network_error(self):
        """read timeout 是 TimeoutError（socket.timeout），不是 URLError ——
        原来的 except URLError 漏了它，会裸抛「The read operation timed out」判死任务。"""
        with self.assertRaises(video.HeyGenNetworkError):
            self._request(socket.timeout("The read operation timed out"))
        with self.assertRaises(video.HeyGenNetworkError):
            self._request(TimeoutError("The read operation timed out"))

    def test_connection_reset_becomes_network_error(self):
        with self.assertRaises(video.HeyGenNetworkError):
            self._request(ConnectionResetError("RST"))

    def test_network_error_is_still_a_runtimeerror(self):
        """必须是 RuntimeError 子类 —— 否则提交路径原有的 except RuntimeError/Exception 会漏接，
        且 _heygen_retry_429 的语义会变。"""
        self.assertTrue(issubclass(video.HeyGenNetworkError, RuntimeError))

    def test_http_5xx_is_not_a_network_error(self):
        """HTTP 5xx 是上游明确响应，不是传输层抖动 —— 不能被当成可安全重试的网络错误。"""
        for code in (400, 500, 503):
            with self.subTest(code=code):
                with self.assertRaises(RuntimeError) as ctx:
                    self._request(_http_error(code))
                self.assertNotIsInstance(ctx.exception, video.HeyGenNetworkError,
                                         "HTTP %d 被误判成可重试网络错误" % code)

    def test_429_still_wins_over_network(self):
        with patch.object(video, "HEYGEN_API_KEY", "k"), \
             patch.object(video, "HEYGEN_API_BASE", "https://relay.test/v3"), \
             patch.object(video.urllib.request, "urlopen",
                          side_effect=_http_error(429, b'{"error":{"code":"rate_limit_exceeded"}}')):
            with self.assertRaises(video.HeyGenRateLimited):
                video._heygen_request_json("POST", "/videos")


class OfficialHeyGenProxyIsolationTests(unittest.TestCase):
    """官方 HeyGen 走专用代理；认证和自定义中转不再依赖或污染全局代理。"""

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"data":{"ok":true}}'

    class _Opener:
        def __init__(self):
            self.calls = []

        def open(self, req, timeout=0):
            self.calls.append((req.full_url, timeout))
            return OfficialHeyGenProxyIsolationTests._Response()

    def test_official_base_uses_dedicated_proxy_opener(self):
        opener = self._Opener()
        with patch.object(video, "HEYGEN_API_KEY", "k"), \
             patch.object(video, "HEYGEN_API_BASE", "https://api.heygen.com/v3"), \
             patch.object(video, "_heygen_direct_opener", return_value=opener), \
             patch.object(video.urllib.request, "urlopen",
                          side_effect=AssertionError("官方 HeyGen 不应裸直连")):
            result = video._heygen_request_json("GET", "/avatars", timeout=12)
        self.assertTrue(result["data"]["ok"])
        self.assertEqual(opener.calls, [("https://api.heygen.com/v3/avatars", 12)])

    def test_custom_relay_keeps_original_transport(self):
        response = self._Response()
        with patch.object(video, "HEYGEN_API_KEY", "k"), \
             patch.object(video, "HEYGEN_API_BASE", "https://relay.internal/v3"), \
             patch.object(video, "_heygen_direct_opener",
                          side_effect=AssertionError("自定义中转不应走 HeyGen 专用代理")), \
             patch.object(video.urllib.request, "urlopen", return_value=response) as urlopen:
            result = video._heygen_request_json("GET", "/avatars", timeout=12)
        self.assertTrue(result["data"]["ok"])
        self.assertEqual(urlopen.call_count, 1)

    def test_lookalike_domains_are_not_treated_as_official(self):
        for base in (
            "https://api.heygen.com.attacker.test/v3",
            "https://heygen.com.attacker.test/v3",
            "https://notheygen.com/v3",
        ):
            with self.subTest(base=base):
                self.assertFalse(video._heygen_official_base(base))


class SubmitStillNeverRetriesNetworkTests(unittest.TestCase):
    """提交 POST 遇网络错 = 可能已计费，绝不能重发 —— 这条纪律不许因本次改动松动。"""

    def test_retry_429_does_not_retry_a_network_error(self):
        calls = []

        def net_fail():
            calls.append(1)
            raise video.HeyGenNetworkError("HeyGen接口网络失败: The read operation timed out")

        with patch.object(video.time, "sleep"):
            with self.assertRaises(video.HeyGenNetworkError):
                video._heygen_retry_429(net_fail, "提交")
        self.assertEqual(len(calls), 1, "提交的网络错被重发了 —— 可能已计费的请求绝不能重来")


class PollRetriesTransientNetworkTests(unittest.TestCase):
    """轮询是幂等 GET：瞬时抖动退避重试，网络恢复就正常出片。"""

    def _fake_clock(self):
        clock = [1000.0]
        return clock

    def test_poll_retries_until_network_recovers(self):
        clock = self._fake_clock()
        completed = {"data": {"status": "completed", "video_url": "https://x/y.mp4", "duration": 10}}
        seq = [video.HeyGenNetworkError("SSL EOF"),
               video.HeyGenNetworkError("read timeout"),
               completed]

        def req(*a, **k):
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch.object(video, "_heygen_request_json", side_effect=req), \
             patch.object(video.time, "time", lambda: clock[0]), \
             patch.object(video.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            info = video._heygen_poll_video("vid1", direct=True, deadline_s=600)
        self.assertEqual(info["video_url"], "https://x/y.mp4")
        self.assertEqual(seq, [], "应当把前两次网络抖动都重试掉，第三次拿到 completed")

    def test_poll_does_not_retry_a_real_provider_failure(self):
        """provider 明确 status=failed 是真失败（不是网络抖动）—— 立即判失败、退点，不重试。"""
        clock = self._fake_clock()
        calls = []

        def req(*a, **k):
            calls.append(1)
            return {"data": {
                "status": "failed", "video_url": None, "provider_video_id": "secret-id",
                "failure_message": "Your content was flagged by our moderation system. Please try different images or prompts.",
            }}

        with patch.object(video, "_heygen_request_json", side_effect=req), \
             patch.object(video.time, "time", lambda: clock[0]), \
             patch.object(video.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            with self.assertRaisesRegex(RuntimeError, "内容审核未通过，请更换人物图片、参考视频或提示词") as ctx:
                video._heygen_poll_video("vid1", direct=True, deadline_s=600)
        self.assertEqual(len(calls), 1, "真失败被反复重试了")
        self.assertNotIn("secret-id", str(ctx.exception), "用户错误不应暴露整段上游响应")

    def test_poll_gives_up_after_deadline_if_network_never_recovers(self):
        """网络一直不恢复：deadline 是总上限，不会无限转，最终超时抛出。"""
        clock = self._fake_clock()

        def req(*a, **k):
            raise video.HeyGenNetworkError("always down")

        with patch.object(video, "_heygen_request_json", side_effect=req), \
             patch.object(video.time, "time", lambda: clock[0]), \
             patch.object(video.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            with self.assertRaises(TimeoutError):
                video._heygen_poll_video("vid1", direct=True, deadline_s=30)


class DownloadRetriesTransientNetworkTests(unittest.TestCase):
    """下载成片同样是幂等 GET：瞬时网络错误退避重试。"""

    def test_read_retry_succeeds_after_transient_failures(self):
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"VIDEOBYTES"

        attempts = []

        def open_fn():
            attempts.append(1)
            if len(attempts) < 3:
                raise TimeoutError("The read operation timed out")
            return _Resp()

        with patch.object(video.time, "sleep"):
            data = video._heygen_read_retry(open_fn, "下载")
        self.assertEqual(data, b"VIDEOBYTES")
        self.assertEqual(len(attempts), 3)

    def test_read_retry_raises_network_error_after_budget(self):
        def open_fn():
            raise ConnectionResetError("RST")

        with patch.object(video.time, "sleep"), \
             patch.object(video, "HEYGEN_NET_RETRIES", 3):
            with self.assertRaises(video.HeyGenNetworkError):
                video._heygen_read_retry(open_fn, "下载")


if __name__ == "__main__":
    unittest.main()


class AvatarWaitAndUploadResilienceTests(unittest.TestCase):
    """建形象的「等就绪」轮询 + 各处素材上传，也要对瞬时网络错误重试(#611 只包了上传/创建，
    漏了 wait poll → yuanzhi 的 read timeout 死在这；cinematic/口播素材上传也没包 → fang 死在这)。"""

    def test_wait_photo_avatar_retries_transient_network(self):
        """look 状态轮询遇网络抖动要重试，恢复后正常返回；不能一次 read timeout 就判死建形象。"""
        clock = [1000.0]
        seq = [video.HeyGenNetworkError("read timeout"),
               ("processing", None),
               ("completed", None)]

        def look(*a, **k):
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch.object(video, "_heygen_look_status", side_effect=look), \
             patch.object(video.time, "time", lambda: clock[0]), \
             patch.object(video.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            self.assertTrue(video._heygen_wait_photo_avatar("look1", direct=True))
        self.assertEqual(seq, [], "网络抖动那次应被重试掉，最终拿到 completed")

    def test_wait_photo_avatar_still_raises_non_network_errors(self):
        """401/配额等 HTTP 错误仍要直接上抛，不能被「继续轮询」掩盖成超时。"""
        def look(*a, **k):
            raise RuntimeError("HeyGen接口失败: HTTP 401")

        clock = [1000.0]
        with patch.object(video, "_heygen_look_status", side_effect=look), \
             patch.object(video.time, "time", lambda: clock[0]), \
             patch.object(video.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            with self.assertRaises(RuntimeError):
                video._heygen_wait_photo_avatar("look1", direct=True)

    def test_all_uploads_are_retry_wrapped(self):
        """所有 _heygen_upload_asset 调用点都要包 _heygen_retry_net(上传不计费、重试安全)。
        漏一个，隧道抖动打在那次上传上就白失败一条。"""
        src = Path(video.__file__).read_text(encoding="utf-8")
        self.assertEqual(
            _unwrapped_upload_lines(src), [],
            "存在未包 _heygen_retry_net 的上传调用",
        )

    def test_upload_retry_guard_rejects_a_real_unwrapped_call(self):
        source = (
            "def upload(path):\n"
            "    return _heygen_upload_asset(path)\n"
        )
        self.assertEqual(_unwrapped_upload_lines(source), [2])
