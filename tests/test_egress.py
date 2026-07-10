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
import sys
import unittest
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

    def test_fallback_to_mihomo_then_success(self):
        out, calls, err = self._run([TimeoutError("vps 超时"), b'{"ok":2}'])
        self.assertEqual(out, {"ok": 2})
        self.assertEqual([c[0] for c in calls], ["http://p1", "http://p2"])  # VPS→mihomo

    def test_all_proxies_fail_then_heygen(self):
        out, calls, err = self._run([OSError("vps 断"), OSError("mihomo 断"), b'{"ok":3}'])
        self.assertEqual(out, {"ok": 3})
        self.assertEqual([c[0] for c in calls], ["http://p1", "http://p2", "direct"])
        self.assertTrue(calls[2][1].startswith("https://heygen"))            # 兜底打 heygen 且直连

    def test_all_channels_fail_raises_last(self):
        boom = ValueError("heygen 也挂")
        out, calls, err = self._run([OSError("a"), OSError("b"), boom])
        self.assertIsNone(out)
        self.assertIs(err, boom)                              # 抛最后一个异常，不静默
        self.assertEqual(len(calls), 3)

    def test_http_200_no_business_data_still_returns(self):
        """HTTP 200 但业务没出图 → 直接返回，不降级（换通道也没用，由调用方判断）。"""
        out, calls, err = self._run([b'{"data":[]}'])
        self.assertEqual(out, {"data": []})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
