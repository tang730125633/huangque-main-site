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
import errno
import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
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
HEYGEN_DIRECT_PROXY = os.environ.get("HEYGEN_DIRECT_PROXY", "").strip()
HEYGEN_PROXY_FALLBACK = (EGRESS_FALLBACK or os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:7897").strip()

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


def heygen_proxy():
    """HeyGen 直连与后台检测共用的专属出境选择。"""
    return HEYGEN_DIRECT_PROXY or preferred_proxy(HEYGEN_PROXY_FALLBACK)


def _channel_usable(proxy):
    """发请求前先看这档的代理端口通不通。通不通都判不出来时（如没写端口）就当可用，
    交给真实请求去判——宁可多发一次，也不要因为探测能力不足而白白跳过一整档。"""
    if not proxy:
        return True                      # 直连档，无需探测
    parts = urllib.parse.urlsplit(proxy)
    if not parts.hostname or not parts.port:
        return True
    return _proxy_reachable(proxy)


_PRE_DELIVERY_ERRNOS = {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH}


def _pre_delivery_failure(e):
    """这个异常能否证明「请求还没送到上游」？只有能证明时，换通道重发才是安全的。

    出图 POST 是**非幂等**的：请求一旦抵达 OpenAI/Google，就可能已经出图并计费。
    此时换通道重发 = 再出一张 + 再计一次费，用户还只拿到一张。

    - HTTPError        → 上游已应答，肯定送达了。不换。
    - 超时(任何形态)    → **歧义**：可能连不上，也可能正在出图。宁可失败退点，让用户自己决定重试。
    - ConnectionReset  → 歧义（可能握手期被 RST，也可能请求发出后被切）。不换。
    - DNS 解析失败 / 连接被拒 / 主机不可达 / TLS 握手失败 → 应用层数据从未发出。可以换。
    """
    if isinstance(e, urllib.error.HTTPError):
        return False
    if isinstance(e, (TimeoutError, socket.timeout)):
        return False
    if isinstance(e, urllib.error.URLError):
        reason = e.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return False
        if isinstance(reason, ConnectionResetError):
            return False
        if isinstance(reason, socket.gaierror):
            return True
        if isinstance(reason, ConnectionRefusedError):
            return True
        if isinstance(reason, ssl.SSLError):
            return True              # 握手阶段失败，应用层字节未发送
        if isinstance(reason, OSError) and reason.errno in _PRE_DELIVERY_ERRNOS:
            return True
    return False


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
    """按优先级链发 POST，返回解析后的 JSON dict。

    ⚠ 出图 POST 是**非幂等**的：请求一旦抵达上游就可能已经出图并计费。所以降级不是
    「失败就换下一档重发」，而是分两步：

      1. 发请求**之前**探这档代理通不通；不通就跳过（一个字节都没发出，绝对安全）
      2. 请求发出后再失败，只有能证明「未送达」（DNS/连接被拒/主机不可达/TLS 握手失败）
         才换下一档。超时、连接被重置、HTTPError 一律**直接抛出**——它们都可能意味着
         上游已经收到并出了图，换通道重发会再计一次费。

    原实现对任何异常都降级，超时时会把同一张图付两遍甚至三遍钱。

    official_base 走代理档，heygen_base 走直连兜底档。data 为已编码字节，headers 含鉴权。
    log 为可选的一元函数（如 lambda m: print(m, flush=True)）。
    """
    last = None
    for label, base, proxy, timeout in channels(official_base, heygen_base):
        if not _channel_usable(proxy):
            last = last or urllib.error.URLError(ConnectionRefusedError("代理 %s 不可达" % proxy))
            if log:
                log("[egress] %s 代理不可达，跳过该档（未发出任何请求）" % label)
            continue
        req = urllib.request.Request(base + path, data=data, headers=headers, method="POST")
        try:
            with _opener(proxy).open(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            if not _pre_delivery_failure(e):
                if log:
                    log("[egress] %s via %s 失败，且请求可能已送达上游（换通道会重复计费），"
                        "直接失败退点: %s" % (path, label, str(e)[:120]))
                raise
            if label != "heygen" and log:
                log("[egress] %s via %s 连接阶段失败(未送达)，降级下一档: %s" % (path, label, str(e)[:120]))
    raise last if last is not None else RuntimeError("egress: 无可用通道")


def post_json_idempotent(official_base, heygen_base, path, data, headers, log=None,
                         max_attempts=2):
    """POST an idempotent analysis request, retrying a slow/broken read once.

    Unlike image generation, chat analysis can be safely repeated from the
    user's perspective: the paid site job is created and charged only once.
    Prefer the next configured route; with a single route, retry it once.
    """
    available = channels(official_base, heygen_base)
    if not available:
        raise RuntimeError("egress: 无可用通道")
    attempts = []
    for channel in available:
        if _channel_usable(channel[2]):
            attempts.append(channel)
    if not attempts:
        raise RuntimeError("egress: 无可用通道")
    while len(attempts) < max(1, int(max_attempts or 1)):
        attempts.append(attempts[-1])

    last = None
    for number, (label, base, proxy, timeout) in enumerate(
            attempts[:max(1, int(max_attempts or 1))], 1):
        request = urllib.request.Request(
            base + path, data=data, headers=headers, method="POST",
        )
        try:
            with _opener(proxy).open(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            last = error
            if 400 <= int(getattr(error, "code", 0) or 0) < 500:
                raise
        except Exception as error:
            last = error
        if log:
            log(
                "[egress] idempotent %s attempt %d/%d via %s failed: %s"
                % (path, number, max_attempts, label, str(last)[:120])
            )
    raise last if last is not None else RuntimeError("egress: 请求失败")


def _read_image_stream(response, expected=1):
    """读取 Images API SSE；连接中断时保留已收到的最后一张有效渐进图。"""
    completed, partial = [], []
    try:
        while True:
            line = response.readline()
            if not line:
                break
            if not line.startswith(b"data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == b"[DONE]":
                continue
            event = json.loads(raw)
            image = event.get("b64_json") or event.get("partial_image_b64")
            if not image:
                continue
            if str(event.get("type") or "").endswith(".completed"):
                completed.append(image)
                if len(completed) >= max(1, int(expected or 1)):
                    return {"data": [{"b64_json": value} for value in completed]}
            else:
                partial.append(image)
    except (http.client.RemoteDisconnected, http.client.IncompleteRead,
            urllib.error.URLError, TimeoutError, ConnectionError):
        if completed:
            return {"data": [{"b64_json": value} for value in completed], "stream_incomplete": True}
        if partial:
            return {"data": [{"b64_json": partial[-1]}], "stream_incomplete": True}
        raise
    images = completed or (partial[-1:] if partial else [])
    if images:
        result = {"data": [{"b64_json": value} for value in images]}
        if len(completed) < max(1, int(expected or 1)):
            result["stream_incomplete"] = True
        return result
    raise ValueError("图片流结束但未返回有效图片")


def post_image_json(official_base, heygen_base, path, data, headers, log=None):
    """官方通道使用 SSE 防长生成期间空闲断连；中转兜底保持原 JSON 协议。"""
    original = json.loads(data)
    expected = max(1, int(original.get("n") or 1))
    stream_body = dict(original)
    stream_body.update({"stream": True, "partial_images": 1})
    stream_data = json.dumps(stream_body, ensure_ascii=False).encode()
    last = None
    for label, base, proxy, timeout in channels(official_base, heygen_base):
        if not _channel_usable(proxy):
            last = last or urllib.error.URLError(ConnectionRefusedError("代理 %s 不可达" % proxy))
            if log:
                log("[egress] %s 代理不可达，跳过该档（未发出任何请求）" % label)
            continue
        is_official = label != "heygen"
        req_headers = dict(headers)
        if is_official:
            req_headers["Accept"] = "text/event-stream"
        req = urllib.request.Request(base + path, data=stream_data if is_official else data,
                                     headers=req_headers, method="POST")
        try:
            with _opener(proxy).open(req, timeout=timeout) as response:
                if is_official:
                    result = _read_image_stream(response, expected)
                    if result.get("stream_incomplete") and log:
                        log("[egress] 图片流提前结束，已保留最后一张有效渐进图")
                    return result
                return json.loads(response.read())
        except Exception as error:
            last = error
            if not _pre_delivery_failure(error):
                if log:
                    log("[egress] %s via %s 失败，且请求可能已送达上游，直接失败退点: %s" %
                        (path, label, str(error)[:120]))
                raise
            if label != "heygen" and log:
                log("[egress] %s via %s 连接阶段失败(未送达)，降级下一档: %s" %
                    (path, label, str(error)[:120]))
    raise last if last is not None else RuntimeError("egress: 无可用通道")
