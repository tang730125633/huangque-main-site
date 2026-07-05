# -*- coding: utf-8 -*-
"""tikhub 分享文案解析的离线单测（不打网络）。

覆盖本次修复的核心：
  1. 从「链接后直接粘中文」的抖音分享文案里，抠出的 URL 不再被中文污染。
  2. dy_resolve 对 /video/<id>、纯 id、非抖音杂串 的判断（不触网的分支）。
  3. parse_link 对含链接 / 口令式无链接 / 小红书 / 视频号 的路由。
运行：python3 -m pytest tests/test_tikhub_parse.py   或   python3 tests/test_tikhub_parse.py
"""
import os, sys, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import tikhub


def test_extract_url_stops_at_cjk():
    # 链接后紧跟中文（无空格）——旧正则会把中文一起吞进 URL
    txt = "3.14 CQ:/ 复制打开抖音…https://v.douyin.com/iAbCdEf/…复制此链接，打开抖音搜索"
    url = tikhub._extract_url(txt)
    assert url == "https://v.douyin.com/iAbCdEf/", repr(url)
    assert "复制" not in url and "…" not in url


def test_extract_url_keeps_query():
    txt = "看看 https://www.iesdouyin.com/share/video/7654380745624879025/?region=US&mid=123 就这条"
    url = tikhub._extract_url(txt)
    assert url == "https://www.iesdouyin.com/share/video/7654380745624879025/?region=US&mid=123", repr(url)


def test_extract_url_none_for_kouling():
    # 口令式分享：没有 http 链接
    txt = "2.05 :8pm PxF:/ 07/24 e@o.DH  小婷婷在抖音记录美好生活20260607 - 抖音 复制此链接，打开Dou音搜索"
    assert tikhub._extract_url(txt) is None


def test_dy_resolve_from_video_url_offline():
    # 含 /video/<id>：纯正则命中，不触网
    assert tikhub.dy_resolve("https://www.douyin.com/video/7654380745624879025") == "7654380745624879025"


def test_dy_resolve_from_bare_id_offline():
    assert tikhub.dy_resolve("7654380745624879025") == "7654380745624879025"


def test_dy_resolve_none_for_junk_offline():
    # 非抖音链接的杂串：不再瞎丢给上游 get_aweme_id，直接 None（不触网）
    assert tikhub.dy_resolve("小婷婷在抖音记录美好生活20260607") is None
    assert tikhub.dy_resolve("随便一段没有链接也没有id的文字") is None


def test_parse_link_channels_routes_without_network():
    info = tikhub.parse_link("https://channels.weixin.qq.com/sph/ABCdef 看看这个视频号")
    assert info["platform"] == "channels"


def test_parse_link_douyin_video_url_offline():
    info = tikhub.parse_link("快看 https://www.douyin.com/video/7654380745624879025 这条")
    assert info["platform"] == "douyin"
    assert info["id"] == "7654380745624879025"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn(); passed += 1; print("ok  ", fn.__name__)
        except AssertionError as e:
            print("FAIL", fn.__name__, "->", e)
        except Exception as e:
            print("ERR ", fn.__name__, "->", repr(e)[:200])
    print("\n%d/%d passed" % (passed, len(fns)))
    sys.exit(0 if passed == len(fns) else 1)
