# -*- coding: utf-8 -*-
"""作图出境优先级链：新 VPS 隧道 → mihomo(法兰克福) → heygen 中转。

背景：作图三引擎(nb2/pro/gpt)原本全走 heygen.zelong.vip 共享中转，拥塞时慢到分钟级、
gpt 失败率 40%。改为优先走自建出境直连官方 API，前一档超时/报错就自动降到下一档。

优先级（按顺序尝试，第一个成功即返回）：
  1. EGRESS_PROXY          首选：新 VPS Reality 隧道的本地 http 代理，如 http://127.0.0.1:10809
  2. EGRESS_PROXY_FALLBACK 备选：现有 mihomo(法兰克福)，如 http://127.0.0.1:7897
  3. heygen 中转           兜底：GEMINI_BASE/OPENAI_BASE，直连（绕过进程级 HTTPS_PROXY）

安全默认：EGRESS_PROXY 与 EGRESS_PROXY_FALLBACK 都未配置时，链里只剩 heygen 一档，
等于改动前的老行为——所以合并本模块零风险，真正切换靠部署时设这两个环境变量。

官方模型名与 heygen 通用（gpt-image-2 自 2026-04 起为官方有效模型），无需按档改名。

只有「通道级失败」(连不上/超时/HTTP 错误码) 触发降级；HTTP 200 直接返回，业务结果
（如内容审核没出图）由调用方判断——那是官方 API 的决定，换通道也一样，不该白降级。
"""
import json
import os
import socket
import threading
import time
import urllib.parse
import urllib.request

EGRESS_PRIMARY = os.environ.get("EGRESS_PROXY", "").strip()
EGRESS_FALLBACK = os.environ.get("EGRESS_PROXY_FALLBACK", "").strip()
# 每个代理档的超时秒数。默认 210 覆盖 gpt-image-2 实测 ~174s；连接被 RST 会秒级抛错快速降级，
# 这个超时只在「请求正常推进但慢」时才生效，不影响掉线快速回落。
EGRESS_TIMEOUT = int(os.environ.get("EGRESS_TIMEOUT", "210") or 210)
# 首选(VPS隧道)可单独放宽超时：gpt-image-2 正常出图就要 ~174s，210s 稍有抖动就误判超时、
# 白白降级到 mihomo。给首选更长余量（默认回落到 EGRESS_TIMEOUT，不设即老行为）。三档总和
# 需压在 reaper image 900s 宽限内：如首选 300 + mihomo 210 + heygen 300 = 810s，安全。
EGRESS_PRIMARY_TIMEOUT = int(os.environ.get("EGRESS_PRIMARY_TIMEOUT", str(EGRESS_TIMEOUT)) or EGRESS_TIMEOUT)
HEYGEN_TIMEOUT = int(os.environ.get("EGRESS_HEYGEN_TIMEOUT", "300") or 300)

# 直连 opener：ProxyHandler({}) 显式清空，绕过 content.env 里进程级的 HTTP(S)_PROXY，
# 保证 heygen 兜底那一档确实直连、不会误走进 mihomo。
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _opener(proxy):
    if not proxy:
        return _DIRECT
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


# ===== 视频侧（口播 / 动作模仿）的通道选择 =====
# 与作图的 post_json 链不同：视频的 create 请求是**非幂等**的（发出去就可能已生成一条片并计费），
# 所以绝不能「失败了换条通道重发」。这里只在**发请求之前**用一次本地 TCP 探活选定通道：
# 隧道进程可达就走隧道，不可达就走备选（mihomo）。HeyGen 直连整体失败时，仍由 video.py
# 既有的「回退泽龙中转」兜底 —— 那一层是业务级降级，不是同一请求的重发。
#
# ⚠ 隧道带宽实测被上游整形在 ~1.3 MB/s（10 Mbps）：4 并发流总吞吐不变、每流均分为 1/4。
# 走隧道会给视频下载套上这个天花板（mihomo 4 并发可到 ~11 MB/s）。这是已知取舍，等 VPS 升级套餐。
EGRESS_PROBE_TTL = int(os.environ.get("EGRESS_PROBE_TTL", "30") or 30)
_probe = {"at": 0.0, "ok": False}
_probe_lock = threading.Lock()


def _proxy_reachable(proxy, timeout=0.5):
    """本地 TCP 连一下代理端口。隧道客户端是本机 xray，连不上说明它没在跑。"""
    try:
        parts = urllib.parse.urlsplit(proxy)
        if not parts.hostname or not parts.port:
            return False
        with socket.create_connection((parts.hostname, parts.port), timeout=timeout):
            return True
    except Exception:
        return False


def tunnel_alive(now=None):
    """隧道是否可用。带 TTL 缓存，避免每个请求都探一次。"""
    if not EGRESS_PRIMARY:
        return False
    now = time.time() if now is None else now
    with _probe_lock:
        if now - _probe["at"] > EGRESS_PROBE_TTL:
            ok = _proxy_reachable(EGRESS_PRIMARY)
            if ok != _probe["ok"]:
                print("[egress] 隧道 %s %s" % (EGRESS_PRIMARY, "恢复可用" if ok else "不可达，改走备选"), flush=True)
            _probe["ok"] = ok
            _probe["at"] = now
        return _probe["ok"]


def preferred_proxy(fallback=""):
    """隧道优先、探活失败退回备选。返回代理 URL；都没有则返回 ''（即直连/进程级代理）。"""
    if tunnel_alive():
        return EGRESS_PRIMARY
    return (fallback or EGRESS_FALLBACK or "").strip()


def channels(official_base, heygen_base):
    """返回 [(标签, base, proxy或None, 超时), ...] 优先级链。未配代理时只剩 heygen，即老行为。"""
    ch = []
    if EGRESS_PRIMARY:
        ch.append(("vps", official_base, EGRESS_PRIMARY, EGRESS_PRIMARY_TIMEOUT))
    if EGRESS_FALLBACK:
        ch.append(("mihomo", official_base, EGRESS_FALLBACK, EGRESS_TIMEOUT))
    ch.append(("heygen", heygen_base, None, HEYGEN_TIMEOUT))
    return ch


def post_json(official_base, heygen_base, path, data, headers, log=None):
    """按优先级链发 POST，返回解析后的 JSON dict。前一档超时/报错降到下一档；全部失败抛最后一个异常。

    official_base 走代理档，heygen_base 走直连兜底档。data 为已编码字节，headers 含鉴权。
    log 为可选的一元函数（如 lambda m: print(m, flush=True)），用于记降级过程。
    """
    last = None
    for label, base, proxy, timeout in channels(official_base, heygen_base):
        req = urllib.request.Request(base + path, data=data, headers=headers, method="POST")
        try:
            with _opener(proxy).open(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:  # 连接错误/超时/HTTPError 都降级到下一档
            last = e
            if label != "heygen" and log:
                log("[egress] %s via %s 失败，降级下一档: %s" % (path, label, str(e)[:120]))
    raise last if last is not None else RuntimeError("egress: 无可用通道")
