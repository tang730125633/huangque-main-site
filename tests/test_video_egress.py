# -*- coding: utf-8 -*-
"""口播 / 动作模仿的出境通道：VPS 隧道优先、mihomo 备选。

红线（本文件的核心不变量）：**通道在发请求之前选定，绝不对已发出的非幂等请求换通道重发。**
HeyGen 的 create-video 和 WaveSpeed 的 POST 提交都会真的生成一条片并计费；换通道重发
= 出两条片 + 计两次费。备选只在「发之前探到隧道不可达」时生效。

线上实测（2026-07-10，5 轮交错）：
  隧道   下行中位 1.04 MB/s；4 并发合计仍 1.30 MB/s，每流均分 0.33 → 上游整形，硬顶 ~10 Mbps
  mihomo 下行中位 5.31 MB/s；4 并发合计 11.0+ MB/s → 无硬顶
  TCP 重传率 ~0%（ping 显示的 8% 丢包是 ICMP 降级假象，不是真丢包）
走隧道会给视频下载套上 ~1.3 MB/s 天花板，这是明知的取舍（等 VPS 升级带宽）。
"""
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))


def _reload_egress(primary="", fallback=""):
    with patch.dict(os.environ, {"EGRESS_PROXY": primary, "EGRESS_PROXY_FALLBACK": fallback}, clear=False):
        import content_domains.egress as egress
        return importlib.reload(egress)


class PreferredProxyTests(unittest.TestCase):
    def test_tunnel_used_when_reachable(self):
        eg = _reload_egress(primary="http://127.0.0.1:10809", fallback="http://127.0.0.1:7897")
        with patch.object(eg, "_proxy_reachable", return_value=True):
            self.assertEqual(eg.preferred_proxy(), "http://127.0.0.1:10809")

    def test_falls_back_when_tunnel_process_down(self):
        """探活失败 → 走 mihomo。这是『发请求前』的选择，不是失败后的重发。"""
        eg = _reload_egress(primary="http://127.0.0.1:10809", fallback="http://127.0.0.1:7897")
        with patch.object(eg, "_proxy_reachable", return_value=False):
            self.assertEqual(eg.preferred_proxy(), "http://127.0.0.1:7897")

    def test_explicit_fallback_argument_wins_over_env(self):
        eg = _reload_egress(primary="http://127.0.0.1:10809", fallback="http://127.0.0.1:7897")
        with patch.object(eg, "_proxy_reachable", return_value=False):
            self.assertEqual(eg.preferred_proxy("http://other:1"), "http://other:1")

    def test_no_tunnel_configured_is_old_behavior(self):
        """未配 EGRESS_PROXY → 不探活、直接用备选（改动前老行为）。"""
        eg = _reload_egress(primary="", fallback="http://127.0.0.1:7897")
        with patch.object(eg, "_proxy_reachable", side_effect=AssertionError("不该探活")):
            self.assertEqual(eg.preferred_proxy(), "http://127.0.0.1:7897")

    def test_nothing_configured_returns_empty(self):
        eg = _reload_egress(primary="", fallback="")
        self.assertEqual(eg.preferred_proxy(), "")

    def test_probe_is_cached_by_ttl(self):
        """每个请求都探一次会拖慢；TTL 内只探一次。"""
        eg = _reload_egress(primary="http://127.0.0.1:10809", fallback="http://f:1")
        with patch.object(eg, "_proxy_reachable", return_value=True) as probe:
            eg.tunnel_alive(now=1000.0)
            eg.tunnel_alive(now=1000.0 + eg.EGRESS_PROBE_TTL - 1)
            self.assertEqual(probe.call_count, 1)
            eg.tunnel_alive(now=1000.0 + eg.EGRESS_PROBE_TTL + 1)
            self.assertEqual(probe.call_count, 2)

    def test_unparseable_proxy_is_not_reachable(self):
        eg = _reload_egress()
        for bad in ("", "not-a-url", "http://nohost", "http://h"):
            self.assertFalse(eg._proxy_reachable(bad))


class HeygenProxyTests(unittest.TestCase):
    """口播 + 动作模仿线路一。"""

    def _video(self):
        return importlib.import_module("content_domains.video")

    def test_explicit_env_override_wins(self):
        v = self._video()
        with patch.object(v, "_HEYGEN_DIRECT_PROXY", "http://explicit:1"):
            self.assertEqual(v._heygen_proxy(), "http://explicit:1")

    def test_delegates_to_egress_preferred_proxy(self):
        v = self._video()
        eg = importlib.import_module("content_domains.egress")
        with patch.object(v, "_HEYGEN_DIRECT_PROXY", ""), \
             patch.object(eg, "preferred_proxy", return_value="http://tunnel:1") as pp:
            self.assertEqual(v._heygen_proxy(), "http://tunnel:1")
        pp.assert_called_once_with(v._HEYGEN_PROXY_FALLBACK)

    def test_opener_carries_chosen_proxy(self):
        v = self._video()
        with patch.object(v, "_heygen_proxy", return_value="http://tunnel:1"):
            handlers = v._heygen_direct_opener().handlers
        proxies = [h.proxies for h in handlers if hasattr(h, "proxies")]
        self.assertIn({"http": "http://tunnel:1", "https": "http://tunnel:1"}, proxies)


class WavespeedProxyTests(unittest.TestCase):
    """动作模仿线路二（实际默认路径）+ 换装线路二（共用本模块）。"""

    def _ws(self):
        return importlib.import_module("content_domains.wavespeed")

    def test_explicit_env_override_wins(self):
        ws = self._ws()
        with patch.object(ws, "WAVESPEED_PROXY", "http://explicit:1"):
            handlers = ws._opener().handlers
        proxies = [h.proxies for h in handlers if hasattr(h, "proxies")]
        self.assertIn({"http": "http://explicit:1", "https": "http://explicit:1"}, proxies)

    def test_uses_egress_preferred_proxy(self):
        ws = self._ws()
        eg = importlib.import_module("content_domains.egress")
        with patch.object(ws, "WAVESPEED_PROXY", ""), \
             patch.object(eg, "preferred_proxy", return_value="http://tunnel:1"):
            handlers = ws._opener().handlers
        proxies = [h.proxies for h in handlers if hasattr(h, "proxies")]
        self.assertIn({"http": "http://tunnel:1", "https": "http://tunnel:1"}, proxies)

    def test_no_proxy_configured_keeps_env_proxy_behavior(self):
        """都没配时不能变成强制直连——那会改变原有行为。"""
        ws = self._ws()
        eg = importlib.import_module("content_domains.egress")
        with patch.object(ws, "WAVESPEED_PROXY", ""), \
             patch.object(eg, "preferred_proxy", return_value=""):
            handlers = ws._opener().handlers
        # build_opener() 自带的 ProxyHandler 会读环境变量；不能是显式清空的 {}
        explicit_empty = [h.proxies for h in handlers if hasattr(h, "proxies") and h.proxies == {}]
        self.assertEqual(explicit_empty, [])

    def test_result_download_goes_through_the_opener(self):
        """拉成片是最重的一腿，必须走选定通道，而不是裸 urlopen 继承环境变量。"""
        src = Path(importlib.import_module("content_domains.wavespeed").__file__).read_text(encoding="utf-8")
        body = src[src.index("def _download_to_lib"):src.index("def generate_tryon")]
        self.assertIn("_opener().open", body)
        self.assertNotIn("urllib.request.urlopen", body)

    def test_submit_goes_through_the_opener(self):
        src = Path(importlib.import_module("content_domains.wavespeed").__file__).read_text(encoding="utf-8")
        body = src[src.index("def _ws_req"):src.index("def _material_url")]
        self.assertIn("_opener().open", body)
        self.assertNotIn("urllib.request.urlopen", body)


class NoCrossChannelRetryTests(unittest.TestCase):
    """红线守卫：源码里不得出现『同一次提交在两条通道上各发一次』的重试。"""

    def test_wavespeed_submit_has_no_retry_loop(self):
        src = Path(importlib.import_module("content_domains.wavespeed").__file__).read_text(encoding="utf-8")
        body = src[src.index("def _ws_req"):src.index("def _material_url")]
        self.assertNotIn("for ", body)          # 无重试循环
        self.assertNotIn("preferred_proxy", body)  # 通道由 _opener 一次性选定，不在请求里换

    def test_preferred_proxy_never_iterates_channels(self):
        """preferred_proxy 只做一次选择。（作图的 egress.post_json 是另一套链式降级，
        它对非幂等 POST 的重发风险单独跟踪，不在本文件范围内。）"""
        eg = importlib.import_module("content_domains.egress")
        src = Path(eg.__file__).read_text(encoding="utf-8")
        start = src.index("def preferred_proxy")
        body = src[start:src.index("def channels", start)]
        self.assertNotIn("for ", body)
        self.assertNotIn("except", body)


if __name__ == "__main__":
    unittest.main()
