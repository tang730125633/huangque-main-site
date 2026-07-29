# -*- coding: utf-8 -*-
"""作图出境优先级链 content_domains/egress.py —— VPS隧道 → mihomo → heygen 降级。

守的不变量：
1. 默认安全：未配 EGRESS_* 时只走 heygen 一档（= 改动前老行为），合并零风险
2. 优先级顺序：VPS 隧道优先，其次 mihomo，最后 heygen
3. 前档超时/报错自动降级到下一档；某一档成功即返回、不再往下
4. 全部失败时抛出最后一个异常，不静默吞
5. 官方档走各自代理、heygen 档直连（不同 base + 不同 opener）
"""
import os
import socket
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))


def _reload_egress(primary="", fallback="", timeout="210", primary_timeout=None):
    import importlib
    env = {"EGRESS_PROXY": primary, "EGRESS_PROXY_FALLBACK": fallback, "EGRESS_TIMEOUT": timeout}
    # 未显式给 primary_timeout 时删掉该键，验证「回落到 EGRESS_TIMEOUT」的默认语义
    if primary_timeout is None:
        os.environ.pop("EGRESS_PRIMARY_TIMEOUT", None)
    else:
        env["EGRESS_PRIMARY_TIMEOUT"] = primary_timeout
    with patch.dict(os.environ, env, clear=False):
        import content_domains.egress as egress
        return importlib.reload(egress)


class ChannelOrderTests(unittest.TestCase):
    def test_default_off_only_heygen(self):
        """未配任何代理 → 链里只有 heygen，即老行为。"""
        eg = _reload_egress(primary="", fallback="")
        ch = eg.channels("https://official", "https://heygen")
        self.assertEqual([c[0] for c in ch], ["heygen"])
        self.assertEqual(ch[0][1], "https://heygen")
        self.assertIsNone(ch[0][2])  # heygen 直连，无代理

    def test_full_chain_order(self):
        """两个代理都配 → VPS 优先、mihomo 次之、heygen 兜底。"""
        eg = _reload_egress(primary="http://127.0.0.1:10809", fallback="http://127.0.0.1:7897")
        ch = eg.channels("https://official", "https://heygen")
        self.assertEqual([c[0] for c in ch], ["vps", "mihomo", "heygen"])
        self.assertEqual(ch[0][2], "http://127.0.0.1:10809")   # vps 走首选代理
        self.assertEqual(ch[1][2], "http://127.0.0.1:7897")    # mihomo 走备选代理
        self.assertIsNone(ch[2][2])                            # heygen 直连
        self.assertEqual(ch[0][1], "https://official")         # 代理档打官方
        self.assertEqual(ch[2][1], "https://heygen")           # 兜底档打 heygen

    def test_only_primary_configured(self):
        eg = _reload_egress(primary="http://127.0.0.1:10809", fallback="")
        ch = eg.channels("https://official", "https://heygen")
        self.assertEqual([c[0] for c in ch], ["vps", "heygen"])


class TimeoutTests(unittest.TestCase):
    """每档超时。通道元组为 (标签, base, proxy, 超时)，索引 3 是超时秒数。"""

    def test_primary_timeout_defaults_to_egress_timeout(self):
        """未设 EGRESS_PRIMARY_TIMEOUT → 首选沿用 EGRESS_TIMEOUT（老行为，不变）。"""
        eg = _reload_egress(primary="http://p1", fallback="http://p2", timeout="210")
        ch = eg.channels("https://official", "https://heygen")
        by = {c[0]: c[3] for c in ch}
        self.assertEqual(by["vps"], 210)
        self.assertEqual(by["mihomo"], 210)

    def test_primary_timeout_override_only_affects_vps(self):
        """设 EGRESS_PRIMARY_TIMEOUT=300 → 只放宽首选，mihomo/heygen 不受影响。"""
        eg = _reload_egress(primary="http://p1", fallback="http://p2", timeout="210", primary_timeout="300")
        ch = eg.channels("https://official", "https://heygen")
        by = {c[0]: c[3] for c in ch}
        self.assertEqual(by["vps"], 300)      # 首选放宽
        self.assertEqual(by["mihomo"], 210)   # 备选不动
        self.assertEqual(by["heygen"], 300)   # 兜底不动（EGRESS_HEYGEN_TIMEOUT 默认）

    def test_chain_total_stays_within_reaper_grace(self):
        """三档超时之和必须 < reaper image 900s 宽限，否则会边降级边被误判超时退点。"""
        eg = _reload_egress(primary="http://p1", fallback="http://p2", timeout="210", primary_timeout="300")
        ch = eg.channels("https://official", "https://heygen")
        self.assertLess(sum(c[3] for c in ch), 900)


class _FakeResp:
    def __init__(self, payload):
        self._b = payload
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.eg = _reload_egress(primary="http://p1", fallback="http://p2")

    def _run(self, side_effects):
        """side_effects: 每个 opener.open 调用依次的行为（异常实例=失败，bytes=成功返回体）。"""
        calls = []

        class _Opener:
            def __init__(self, tag):
                self.tag = tag
            def open(self, req, timeout=None):
                calls.append((self.tag, req.full_url, timeout))
                eff = side_effects[len(calls) - 1]
                if isinstance(eff, Exception):
                    raise eff
                return _FakeResp(eff)

        def fake_opener(proxy):
            return _Opener("direct" if not proxy else proxy)

        with patch.object(self.eg, "_opener", side_effect=fake_opener):
            try:
                out = self.eg.post_json("https://official", "https://heygen", "/gen", b"{}",
                                        {"Content-Type": "application/json"})
                return out, calls, None
            except Exception as e:
                return None, calls, e

    def test_primary_success_stops_early(self):
        out, calls, err = self._run([b'{"ok":1}'])
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(len(calls), 1)                       # 首档成功就停
        self.assertEqual(calls[0][0], "http://p1")            # 走的是 VPS 代理
        self.assertTrue(calls[0][1].startswith("https://official"))

    @staticmethod
    def _refused(msg="连接被拒"):
        return urllib.error.URLError(ConnectionRefusedError(msg))

    def test_fallback_to_mihomo_on_pre_delivery_failure(self):
        """连接被拒 = 一个字节都没发出去 → 换通道是安全的。"""
        out, calls, err = self._run([self._refused("vps"), b'{"ok":2}'])
        self.assertEqual(out, {"ok": 2})
        self.assertEqual([c[0] for c in calls], ["http://p1", "http://p2"])  # VPS→mihomo

    def test_all_proxies_refused_then_heygen(self):
        out, calls, err = self._run([self._refused("vps"), self._refused("mihomo"), b'{"ok":3}'])
        self.assertEqual(out, {"ok": 3})
        self.assertEqual([c[0] for c in calls], ["http://p1", "http://p2", "direct"])
        self.assertTrue(calls[2][1].startswith("https://heygen"))            # 兜底打 heygen 且直连

    def test_all_channels_fail_raises_last(self):
        boom = ValueError("heygen 也挂")
        out, calls, err = self._run([self._refused("a"), self._refused("b"), boom])
        self.assertIsNone(out)
        self.assertIs(err, boom)                              # 抛最后一个异常，不静默
        self.assertEqual(len(calls), 3)

    # ===== 非幂等保护：请求可能已送达上游时，绝不换通道重发（会重复出图 + 重复计费） =====

    def test_timeout_does_not_fail_over(self):
        """超时是歧义的：可能连不上，也可能上游正在出图。重发 = 再出一张 + 再计一次费。"""
        out, calls, err = self._run([TimeoutError("vps 超时"), b'{"ok":2}'])
        self.assertIsNone(out)
        self.assertIsInstance(err, TimeoutError)
        self.assertEqual(len(calls), 1)                        # 只发了一次，没有降级

    def test_url_error_wrapping_timeout_does_not_fail_over(self):
        out, calls, err = self._run([urllib.error.URLError(TimeoutError("read timeout")), b'{"ok":2}'])
        self.assertIsNone(out)
        self.assertEqual(len(calls), 1)

    def test_connection_reset_does_not_fail_over(self):
        """RST 可能发生在请求发出之后，无法证明未送达。"""
        out, calls, err = self._run([urllib.error.URLError(ConnectionResetError("RST")), b'{"ok":2}'])
        self.assertIsNone(out)
        self.assertEqual(len(calls), 1)

    def test_http_error_does_not_fail_over(self):
        """上游已经应答，肯定送达了。"""
        import io
        e = urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO(b"{}"))
        out, calls, err = self._run([e, b'{"ok":2}'])
        self.assertIsNone(out)
        self.assertEqual(len(calls), 1)

    def test_dns_failure_fails_over(self):
        out, calls, err = self._run([urllib.error.URLError(socket.gaierror("no dns")), b'{"ok":2}'])
        self.assertEqual(out, {"ok": 2})
        self.assertEqual(len(calls), 2)

    def test_tls_handshake_failure_fails_over(self):
        import ssl as _ssl
        out, calls, err = self._run([urllib.error.URLError(_ssl.SSLError("handshake")), b'{"ok":2}'])
        self.assertEqual(out, {"ok": 2})
        self.assertEqual(len(calls), 2)

    def test_http_200_no_business_data_still_returns(self):
        """HTTP 200 但业务没出图 → 直接返回，不降级（换通道也没用，由调用方判断）。"""
        out, calls, err = self._run([b'{"data":[]}'])
        self.assertEqual(out, {"data": []})
        self.assertEqual(len(calls), 1)

    def test_idempotent_analysis_retries_timeout_on_next_channel(self):
        calls = []

        class _Opener:
            def __init__(self, tag):
                self.tag = tag
            def open(self, req, timeout=None):
                calls.append(self.tag)
                if len(calls) == 1:
                    raise TimeoutError("read timed out")
                return _FakeResp(b'{"ok":2}')

        with patch.object(self.eg, "_channel_usable", return_value=True), \
             patch.object(
                 self.eg, "_opener",
                 side_effect=lambda proxy: _Opener("direct" if not proxy else proxy),
             ):
            result = self.eg.post_json_idempotent(
                "https://official", "https://heygen", "/chat", b"{}", {},
                max_attempts=2,
            )
        self.assertEqual(result, {"ok": 2})
        self.assertEqual(calls, ["http://p1", "http://p2"])

    def test_idempotent_analysis_retries_only_route_once(self):
        eg = _reload_egress(primary="", fallback="")
        calls = []

        class _Opener:
            def open(self, req, timeout=None):
                calls.append(req.full_url)
                if len(calls) == 1:
                    raise TimeoutError("read timed out")
                return _FakeResp(b'{"ok":3}')

        with patch.object(eg, "_opener", return_value=_Opener()):
            result = eg.post_json_idempotent(
                "https://official", "https://heygen", "/chat", b"{}", {},
                max_attempts=2,
            )
        self.assertEqual(result, {"ok": 3})
        self.assertEqual(len(calls), 2)


class ChannelPreflightTests(unittest.TestCase):
    """代理不可达时，整档跳过且一个字节都不发——最安全的降级。"""

    def test_unreachable_primary_is_skipped_without_sending(self):
        eg = _reload_egress(primary="http://127.0.0.1:10809", fallback="http://127.0.0.1:7897")
        calls = []

        class _Op:
            def __init__(self, tag): self.tag = tag
            def open(self, req, timeout=None):
                calls.append(self.tag)
                return _FakeResp(b'{"ok":1}')

        with patch.object(eg, "_proxy_reachable", side_effect=lambda p, **kw: "7897" in p), \
             patch.object(eg, "_opener", side_effect=lambda p: _Op(p or "direct")):
            out = eg.post_json("https://official", "https://heygen", "/g", b"{}", {})
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(calls, ["http://127.0.0.1:7897"])     # 隧道档整档跳过，未发请求

    def test_direct_channel_never_probed(self):
        eg = _reload_egress(primary="", fallback="")
        with patch.object(eg, "_proxy_reachable", side_effect=AssertionError("直连档不该探测")):
            self.assertTrue(eg._channel_usable(None))

    def test_proxy_without_port_is_assumed_usable(self):
        """探测不出来就别跳过整档，交给真实请求判。"""
        eg = _reload_egress()
        with patch.object(eg, "_proxy_reachable", side_effect=AssertionError("不该探测")):
            self.assertTrue(eg._channel_usable("http://p1"))


if __name__ == "__main__":
    unittest.main()
