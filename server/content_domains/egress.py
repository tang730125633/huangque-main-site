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
import urllib.request

EGRESS_PRIMARY = os.environ.get("EGRESS_PROXY", "").strip()
EGRESS_FALLBACK = os.environ.get("EGRESS_PROXY_FALLBACK", "").strip()
# 每个代理档的超时秒数。默认 210 覆盖 gpt-image-2 实测 ~174s；连接被 RST 会秒级抛错快速降级，
# 这个超时只在「请求正常推进但慢」时才生效，不影响掉线快速回落。
EGRESS_TIMEOUT = int(os.environ.get("EGRESS_TIMEOUT", "210") or 210)
HEYGEN_TIMEOUT = int(os.environ.get("EGRESS_HEYGEN_TIMEOUT", "300") or 300)

# 直连 opener：ProxyHandler({}) 显式清空，绕过 content.env 里进程级的 HTTP(S)_PROXY，
# 保证 heygen 兜底那一档确实直连、不会误走进 mihomo。
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _opener(proxy):
    if not proxy:
        return _DIRECT
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def channels(official_base, heygen_base):
    """返回 [(标签, base, proxy或None, 超时), ...] 优先级链。未配代理时只剩 heygen，即老行为。"""
    ch = []
    if EGRESS_PRIMARY:
        ch.append(("vps", official_base, EGRESS_PRIMARY, EGRESS_TIMEOUT))
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
