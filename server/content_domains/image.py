# -*- coding: utf-8 -*-
import http.client
import os
import random
import threading
import time
import urllib.error

from .core import (
    OPENAI_BASE, OPENAI_KEY, OUT_DIR, SIZES, ZELONG2_BASE, ZELONG2_KEY,
    ZELONG_BASE, ZELONG_KEY, _NOPROXY, _multipart, _post,
    base64, json, public_url, urllib, uuid,
)
from .video import XIAOLEVIDEO_API_KEY, _image_bytes_look_valid, _xiaole_request

# gpt 引擎出境优先级：VPS 隧道 → mihomo → heygen（见 egress.py）。官方 OpenAI 直连地址：
OPENAI_OFFICIAL_BASE = os.environ.get("OPENAI_OFFICIAL_BASE", "https://api.openai.com").rstrip("/")
IMAGE_REF_MAX_BYTES = max(1, int(os.environ.get("IMAGE_REF_MAX_BYTES", str(10 * 1024 * 1024)) or (10 * 1024 * 1024)))
# 提示词上限：Ark 实测吃得下 2 万字，2000 是没必要的收紧；8000 对长场景描述够用。
IMAGE_PROMPT_MAX_CHARS = max(1, int(os.environ.get("IMAGE_PROMPT_MAX_CHARS", "8000") or 8000))
# count 上限取各引擎里最大的 MAX_N（gpt 4；seedream 2 由引擎自己再 cap）。
IMAGE_MAX_COUNT = max(1, int(os.environ.get("IMAGE_MAX_COUNT", "4") or 4))
XIAOLE_IMAGE_MAX_REF = max(1, int(os.environ.get("XIAOLE_IMAGE_MAX_REF", "4") or 4))
XIAOLE_IMAGE_REF_TOTAL_MAX_BYTES = max(IMAGE_REF_MAX_BYTES, int(os.environ.get(
    "XIAOLE_IMAGE_REF_TOTAL_MAX_BYTES", str(28 * 1024 * 1024)) or (28 * 1024 * 1024)))

# ===== Seedream（火山方舟 Ark）=====
# 火山在国内，服务器直连即可：不走 VPS 隧道、不走 mihomo、不走 heygen 中转。
# ⚠ 进程级 HTTPS_PROXY 指向 mihomo(法兰克福)，所以调用必须 proxy=False 显式绕过，否则请求绕地球一圈。
# 下面这些默认值全部由线上实测确认（见 tests/test_seedream.py 头注）：model id 取自本账号 /models。
ARK_BASE = os.environ.get("ARK_BASE", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
ARK_API_KEY = os.environ.get("ARK_API_KEY", "")
SEEDREAM_MODELS = {
    "std": os.environ.get("ARK_SEEDREAM_MODEL", "doubao-seedream-5-0-260128"),
    "pro": os.environ.get("ARK_SEEDREAM_PRO_MODEL", "doubao-seedream-5-0-pro-260628"),
}
SEEDREAM_MIN_PIXELS = 3686400   # 实测硬下限：低于此 Ark 返回 400「image size must be at least 3686400 pixels」
SEEDREAM_HD_PIXELS = 9400000    # 高清档目标像素（标准版实测 4688x2000 / 4096x4096 均可）
# 像素上限按型号不同 —— Pro 的窗口窄得多。线上实测：
#   pro: 4556800 px(1600x2848) → 200 ；4629248 px(1712x2704) → 400「image area must be at most 4624220 pixels」
#   std: 9400320 px(2304x4080) → 200 ；16777216 px(4096x4096) → 200
# 高清档若不按型号夹逼，Pro 会拿到 9.4M 像素而必然 400 —— 线上 3 单 Pro 高清就是这么挂的（已退点）。
SEEDREAM_MAX_PIXELS = {"std": 16777216, "pro": 4624220}
SEEDREAM_MAX_N = 2              # 数量上限，须与 points.cost_of 的 cap 一致，否则按 N 扣点却只出 2 张
SEEDREAM_MAX_REF_BYTES = 10 * 1024 * 1024   # 参考图上限，与 imggen_api 的 MAX_IMAGE_BYTES 对齐

_ZELONG2_POOL_LOCK = threading.Lock()
_ZELONG2_POOL_NEXT = 0

# 出图死线。必须留在 reaper KIND_GRACE["image"]=900s 之内（否则任务被判超时退点），
# 前端 banana 轮询也容忍 900s（#331）。600s 给两档慢引擎足够余量、又不越界。
#   xiaole : 轮询循环的总死线，天然受控
#   zelong2: 号池的**总**死线（不是单次 timeout），见 _post_zelong2
XIAOLE_IMG_DEADLINE = int(os.environ.get("XIAOLE_IMG_DEADLINE", "600") or 600)
ZELONG2_DEADLINE = int(os.environ.get("ZELONG2_DEADLINE", "600") or 600)
_MIN_ATTEMPT_SECONDS = 5        # 剩余预算不足这么多秒就别再发请求了

# ===== 果肉渠道抗压 =====
# 50 齐点压测（2026-07-19 报告）：20 条失败里 17 条是上游按 API Key 熔断
# 「当前 API Key 媒体任务过多」。Key 与果肉/豆姐视频共用（限额实测 ~10 个媒体任务），
# 图像侧并发闸收紧到 5，给视频留余量，从源头少触发熔断；拿不到闸的任务在 worker 里
# 排队等（worker 池本来就只有 10），总比创建被 429 当场判死退点强。
XIAOLE_IMG_MAX_CONCURRENCY = max(1, int(os.environ.get("XIAOLE_IMG_MAX_CONCURRENCY", "5") or 5))
# 创建调用限流重试总预算：与 seedream 429 同一逻辑——只有限流能确定任务未创建未计费，
# 重试绝对安全；其余错误照旧立刻失败退点。300s 退避 + 600s 轮询贴 reaper image 900s 红线，
# 极端排队会被 reaper 判超时退点（不丢钱只是白等），可接受。
XIAOLE_IMG_CREATE_MAX_WAIT = max(0, int(os.environ.get("XIAOLE_IMG_CREATE_MAX_WAIT", "300") or 300))
_XIAOLE_IMG_SEM = threading.BoundedSemaphore(XIAOLE_IMG_MAX_CONCURRENCY)

def _xiaole_rate_limited(text, code=None):
    """限流判定：HTTP 429、body code 报错、「媒体任务过多」都覆盖。宁可漏判不重试，
    不可误判重试——非限流错误重试可能重复创建=重复计费（同 _seedream_post 的纪律）。"""
    t = str(text or "")
    tl = t.lower()
    return (str(code).strip() == "429") or ("429" in t) or ("过多" in t) or ("限流" in t) \
        or ("too many" in tl) or ("rate limit" in tl)

def _clean_b64(value):
    """归一化前端传来的图片 base64：剥离 data: 前缀、去空白/换行、补齐 padding。
    个别前端路径(剪贴板/反推回填)会带换行或缺 padding，裸 base64.b64decode 抛
    「Incorrect padding」→ 图生图整单失败退点(#6)。空值返回 None。"""
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    raw = "".join(raw.split())
    return raw + "=" * (-len(raw) % 4)

_RETRYABLE_HTTP = {429, 500, 502, 503, 504}

def _is_transient(e):
    """连接层/限流/网关类错误 → 可重试；4xx(内容审核/参数)不重试。"""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in _RETRYABLE_HTTP
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError))

def _retry(fn, tries=2, base_delay=1.5):
    """瞬时失败退避重试(#6)：中转出图原来无重试，上游断连/503/504/read timeout 直接
    算失败退点。只重试 _is_transient；单发 _post 最多 2 次(300s×2 仍在 reaper image
    900s 宽限内)。泽龙2 号池抛 ValueError(非瞬时)，不会被这里二次放大。"""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1 or not _is_transient(e):
                raise
            print("[img-retry] 第%d次瞬时失败，退避重试: %s" % (i + 1, str(e)[:120]), flush=True)
            time.sleep(base_delay * (i + 1))

def _split_env_list(value):
    return [v.strip() for v in str(value or "").replace("\n", ",").replace(";", ",").split(",") if v.strip()]

def _zelong2_accounts():
    keys = _split_env_list(os.environ.get("ZELONG2_KEYS", ""))
    if ZELONG2_KEY and ZELONG2_KEY not in keys:
        keys.insert(0, ZELONG2_KEY)
    bases = _split_env_list(os.environ.get("ZELONG2_BASES", ""))
    return [{"key": key, "base": bases[i] if i < len(bases) else ZELONG2_BASE} for i, key in enumerate(keys)]

def _zelong2_attempts():
    global _ZELONG2_POOL_NEXT
    accounts = _zelong2_accounts()
    if not accounts:
        return []
    with _ZELONG2_POOL_LOCK:
        start = _ZELONG2_POOL_NEXT % len(accounts)
        _ZELONG2_POOL_NEXT += 1
    return accounts[start:] + accounts[:start]

def _post_zelong2(path, data, ctype):
    """泽龙2 号池：逐个账号尝试，**整体压在 ZELONG2_DEADLINE 内**。

    不能简单把单次 timeout 放宽——号池要遍历 N 个账号、每个账号还被 _retry 重试 2 次，
    最坏耗时是 N×2×timeout，会冲破 reaper image 的 900s 宽限（任务被判超时退点）。
    改为总死线：每次实际发请求时按「剩余预算」当 timeout，预算耗尽就不再试下一个号。
    """
    errors = []
    deadline = time.time() + ZELONG2_DEADLINE
    for idx, account in enumerate(_zelong2_attempts(), 1):
        if deadline - time.time() <= _MIN_ATTEMPT_SECONDS:
            errors.append("#%d 总死线 %ds 已耗尽，不再尝试后续号" % (idx, ZELONG2_DEADLINE))
            print("[zelong2-pool] %s" % errors[-1], flush=True)
            break

        def _attempt(acc=account):
            # 每次（含 _retry 的重试）都重算剩余预算，保证总耗时不超过 deadline
            remain = deadline - time.time()
            if remain <= _MIN_ATTEMPT_SECONDS:
                raise ValueError("泽龙2 总死线 %ds 已耗尽" % ZELONG2_DEADLINE)
            return _post(path, data, ctype, base=acc["base"], key=acc["key"],
                         proxy=False, timeout=int(remain))

        try:
            return _retry(_attempt)
        except Exception as e:
            errors.append("#%d %s: %s" % (idx, account["base"], str(e)[:160]))
            print("[zelong2-pool] attempt failed %s" % errors[-1], flush=True)
    raise ValueError("泽龙2号池全部失败: " + " | ".join(errors))

def _decode_image_b64(value, field):
    """\u5148\u6309 #505 \u7684\u89c4\u5219\u6e05\u6d17\uff0c\u518d\u4e25\u683c\u6821\u9a8c\u3002

    \u4e0d\u80fd\u76f4\u63a5 b64decode(validate=True)\uff1a\u526a\u8d34\u677f\u7c98\u8d34(#483 \u7684 Ctrl/\u2318V)\u4f1a\u5e26\u4e2d\u95f4\u6362\u884c\u3001
    \u53cd\u63a8\u56de\u586b/\u7075\u611f\u8ddf\u521b\u4f1a\u7f3a\u5c3e\u90e8 padding \u2014\u2014 \u8fd9\u4e9b\u90fd\u662f\u5408\u6cd5\u7684\u524d\u7aef\u4ea7\u7269\uff0c#505 \u4e13\u95e8\u7528
    _clean_b64 \u4fee\u8fc7\u3002\u6e05\u6d17\u4e4b\u540e\u518d validate=True\uff0c\u65e2\u6321\u5f97\u4f4f\u771f\u5783\u573e\uff0c\u53c8\u4e0d\u8bef\u4f24\u5b83\u4eec\u3002
    """
    value = _clean_b64(value)          # \u5265 data: \u524d\u7f00 + \u53bb\u6240\u6709\u7a7a\u767d/\u6362\u884c + \u8865 padding
    if not value:
        return "", b""
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:
        raise ValueError("%s \u5fc5\u987b\u662f\u5408\u6cd5 base64" % field)
    if len(raw) > IMAGE_REF_MAX_BYTES:
        mb = IMAGE_REF_MAX_BYTES // 1024 // 1024
        raise ValueError("%s \u8d85\u8fc7 %dMB\uff0c\u8bf7\u5148\u538b\u7f29\u56fe\u7247\u518d\u4e0a\u4f20" % (field, mb))
    return value, raw

def validate_image_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("\u8bf7\u6c42\u4f53\u5fc5\u987b\u662f JSON \u5bf9\u8c61")
    body = dict(payload)
    provider = str(body.get("provider") or "openai").strip().lower()
    if provider == "zelong2":
        raise ValueError("泽龙2生图渠道维护中，请使用 Seedream 或果肉生图")
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("\u63d0\u793a\u8bcd\u4e0d\u80fd\u4e3a\u7a7a")
    if len(prompt) > IMAGE_PROMPT_MAX_CHARS:
        raise ValueError("\u63d0\u793a\u8bcd\u4e0d\u80fd\u8d85\u8fc7 %d \u5b57" % IMAGE_PROMPT_MAX_CHARS)
    body["prompt"] = prompt
    # 魔数校验放在扣点前：base64 能解码但不是图片时，Ark 回的是 HTTP 500
    # 「service encountered an unexpected internal error」，用户会以为是我们的故障，
    # 而且那时点已经扣了（要等失败退点）。在这里拦住，所有引擎都受益且不扣点。
    if body.get("image"):
        body["image"], raw = _decode_image_b64(body.get("image"), "image")
        if raw and not _image_bytes_look_valid(raw):
            raise ValueError("参考图格式不支持，请使用 PNG / JPG / WebP")
    references = body.get("reference_images")
    if references is not None:
        if provider != "xiaole":
            raise ValueError("多图参考目前仅支持果肉生图")
        if not isinstance(references, list) or not references:
            raise ValueError("reference_images 必须是非空图片数组")
        if len(references) > XIAOLE_IMAGE_MAX_REF:
            raise ValueError("果肉生图最多支持 %d 张参考图" % XIAOLE_IMAGE_MAX_REF)
        clean_references, total_bytes = [], 0
        for index, value in enumerate(references, 1):
            clean, raw = _decode_image_b64(value, "第%d张参考图" % index)
            if not raw or not _image_bytes_look_valid(raw):
                raise ValueError("第%d张参考图格式不支持，请使用 PNG / JPG / WebP" % index)
            total_bytes += len(raw)
            clean_references.append(clean)
        if total_bytes > XIAOLE_IMAGE_REF_TOTAL_MAX_BYTES:
            raise ValueError("果肉生图的参考图合计不能超过 %dMB" % (XIAOLE_IMAGE_REF_TOTAL_MAX_BYTES // 1024 // 1024))
        body["reference_images"] = clean_references
    if body.get("mask"):
        body["mask"], raw = _decode_image_b64(body.get("mask"), "mask")
        if raw and not _image_bytes_look_valid(raw):
            raise ValueError("蒙版格式不支持，请使用 PNG")
    # count \u5fc5\u987b\u5939\u4f4f\u4e0a\u9650\uff1acost_of \u6309 count \u6263\u70b9\uff0ccount=100 \u5c31\u4f1a\u6263\u7206\u70b9\u3002
    # \u5404\u5f15\u64ce\u81ea\u5df1\u8fd8\u6709 MAX_N\uff08seedream 2 / gpt 4\uff09\uff0c\u8fd9\u91cc\u53ea\u6321\u79bb\u8c31\u503c\u3002
    try:
        count = int(body.get("count") or 1)
    except Exception:
        raise ValueError("count \u5fc5\u987b\u662f\u6b63\u6574\u6570")
    body["count"] = max(1, min(IMAGE_MAX_COUNT, count))
    return body

def _gen_image_xiaole(prompt, ratio, quality, count, img, references=None):
    """并发闸入口：同一时刻在上游飞的果肉图像任务不超过 XIAOLE_IMG_MAX_CONCURRENCY，
    超出的在 worker 里等闸（worker 池只有 10，排队深度天然有界）。"""
    with _XIAOLE_IMG_SEM:
        return _gen_image_xiaole_locked(prompt, ratio, quality, count, img, references)

def _gen_image_xiaole_locked(prompt, ratio, quality, count, img, references=None):
    """果肉生图渠道(xiaolevideo.cn，与果肉/豆姐视频同账号)：gpt-image-2 文生图/图生图。
    统一 generations API：创建 → 轮询 → 落盘，与 video.py 的 generate_xiaole_video 同一套模式。"""
    if not XIAOLEVIDEO_API_KEY:
        raise ValueError("果肉生图未配置（XIAOLEVIDEO_API_KEY）")
    resolution = "2k" if quality == "high" else "1k"
    refs = list(references or ([] if not img else [img]))
    input_d = {"prompt": prompt, "mode": ("image_to_image" if refs else "text_to_image"),
               "resolution": resolution, "aspect_ratio": ratio, "quality": quality, "n": count}
    if refs:
        input_d["reference_images"] = [{"type": "base64", "value": ref} for ref in refs]
    # 创建限流重试：_xiaole_request 自带的 5 次 429 退避(~120s)压测证明扛不住整批饱和
    # （上游 Key 熔断持续数分钟），这里在 XIAOLE_IMG_CREATE_MAX_WAIT 预算内继续等。
    # 只重试限流（任务未创建未计费，重发安全）；其余错误直接抛，走失败退点。
    create = None
    create_started = time.monotonic()
    create_deadline = create_started + XIAOLE_IMG_CREATE_MAX_WAIT
    attempts = 0
    while True:
        if attempts and time.monotonic() >= create_deadline:
            raise ValueError("果肉渠道繁忙（上游持续限流），请稍后重试")
        attempts += 1
        try:
            create = _xiaole_request(
                "POST", "/api/v1/generations", {"model": "gpt-image-2", "input": input_d},
                retry_deadline=create_deadline,
            )
            if create.get("code") in (200, 0, None):
                break
            msg = str(create.get("message"))[:200]
            if not _xiaole_rate_limited(msg, create.get("code")):
                raise ValueError("出图创建失败: %s" % msg)
        except RuntimeError as e:
            if not _xiaole_rate_limited(e):
                raise
        elapsed = max(0.0, time.monotonic() - create_started)
        remaining = max(0.0, create_deadline - time.monotonic())
        if remaining <= 0:
            raise ValueError("果肉渠道繁忙（上游持续限流），请稍后重试")
        delay = min(45.0, 10.0 + elapsed * 0.2) * (0.7 + random.random() * 0.6)  # 渐进退避+抖动，防齐点重试新洪峰
        delay = min(delay, remaining)
        print("[image] 果肉创建被限流，退避重试 等%.1fs(已耗时%.0f/%ds)" % (
            delay, elapsed, XIAOLE_IMG_CREATE_MAX_WAIT), flush=True)
        time.sleep(delay)
    data = create.get("data") or {}
    rid = data.get("request_id") or data.get("task_id")
    status_url = data.get("status_url") or (("/api/v1/generations/" + str(rid)) if rid else "")
    if not status_url:
        raise ValueError("渠道未返回任务ID")
    # 300s 太紧：hd 图生图(2k+参考图)实测稳定 ~300s，全站近7天成功任务中位193s、最大446s，
    # 失败任务中位315s —— 死线正好卡在实际耗时上。放宽到 600s（仍 < reaper 900s / 前端 900s）。
    deadline = time.time() + XIAOLE_IMG_DEADLINE
    images, poll_errors = None, 0
    while time.time() < deadline:
        try:
            st = _xiaole_request("GET", status_url, timeout=30)
            poll_errors = 0
        except Exception as e:
            # 轮询是幂等 GET：限流/抖动/网关类瞬时错误不该杀死已在飞的任务（任务已在
            # 上游计费，判死=白烧钱还退点）。连续 5 次(~25s)仍不通才放弃。
            poll_errors += 1
            if poll_errors >= 5:
                raise ValueError("出图状态查询连续失败: %s" % str(e)[:120])
            time.sleep(5)
            continue
        sdata = st.get("data") or {}
        status = str(sdata.get("status") or "").lower()
        if status == "succeeded":
            images = (sdata.get("output") or {}).get("images") or []
            break
        if status in ("failed", "error", "cancelled", "canceled"):
            err = sdata.get("error") or {}
            raise ValueError("出图失败: %s" % ((err.get("message") if isinstance(err, dict) else None) or str(err) or status))
        time.sleep(3)
    else:
        raise ValueError("出图超时")
    files_out, urls = [], []
    for item in (images or []):
        b64 = item.get("b64_json") if isinstance(item, dict) else None
        url = item.get("url") if isinstance(item, dict) else None
        fn = "img_%s.png" % uuid.uuid4().hex  # 不可猜键(#185)
        if b64:
            (OUT_DIR / fn).write_bytes(base64.b64decode(b64))
        elif url:
            with urllib.request.urlopen(url, timeout=120) as rr:
                (OUT_DIR / fn).write_bytes(rr.read())
        else:
            continue
        files_out.append(fn); urls.append(public_url(fn, "image/png"))
    if not files_out:
        raise ValueError("出图返回为空")
    return {"type": "image", "mode": ("img2img" if refs else "text2img"), "provider": "xiaole",
            "count": len(files_out), "file": files_out[0], "url": urls[0],
            "files": files_out, "urls": urls, "ratio": ratio, "prompt": prompt}

def _dispatch_gpt(provider, path, body, ct, base, key, proxy, streaming=False):
    """gpt 家族出图分发。openai(官方) 走出境优先级链 VPS→mihomo→heygen；泽龙系维持原样。"""
    if provider == "zelong2":
        return _post_zelong2(path, body, ct)
    if provider == "zelong":
        return _retry(lambda: _post(path, body, ct, base=base, key=key, proxy=proxy))
    # provider == "openai"：优先自建出境直连官方，超时/报错降级，最终兜底 heygen(=OPENAI_BASE)。
    # 未配 EGRESS_* 时 egress 链里只剩 heygen 一档，等于改动前的老行为。
    from content_domains import egress
    transport = egress.post_image_json if streaming else egress.post_json
    return transport(OPENAI_OFFICIAL_BASE, OPENAI_BASE, path, body,
                     {"Authorization": "Bearer " + OPENAI_KEY, "Content-Type": ct},
                     log=lambda m: print(m, flush=True))


def _seedream_size(ratio, quality, variant="std"):
    """比例 → 显式「宽x高」。Ark 不吃 1:1 这种比例串，只吃像素尺寸，且像素总数必须落在
    [SEEDREAM_MIN_PIXELS, 按型号的上限] 这个窗口里（实测 1024x1024、1152x2048 因太小被 400 拒；
    Pro 的上限只有 4624220，比标准版窄得多）。按目标像素反解并对齐到 16 的倍数，再夹逼进窗口。"""
    try:
        rw, rh = (int(x) for x in str(ratio).split(":", 1))
        if rw <= 0 or rh <= 0:
            raise ValueError
    except Exception:
        rw, rh = 9, 16
    # 未知型号取最保守（最小）的上限，宁可出图小一点也不要 400
    cap = SEEDREAM_MAX_PIXELS.get(variant) or min(SEEDREAM_MAX_PIXELS.values())
    target = SEEDREAM_HD_PIXELS if quality == "hd" else SEEDREAM_MIN_PIXELS
    target = max(SEEDREAM_MIN_PIXELS, min(target, cap))

    scale = (target / float(rw * rh)) ** 0.5
    w = max(16, int(round(rw * scale / 16.0)) * 16)
    h = max(16, int(round(rh * scale / 16.0)) * 16)
    for _ in range(64):                  # 取整误差 ~1.5%，窗口宽 ≥25%，几步内必收敛
        if w * h > cap and w > 32 and h > 32:
            w -= 16
            h -= 16
        elif w * h < SEEDREAM_MIN_PIXELS:
            w += 16
            h += 16
        else:
            break
    return "%dx%d" % (w, h)

def _seedream_check_ref(img):
    """本地先验参考图，把坏图挡在上游调用之前。

    实测：base64 能解码但不是图片时，Ark 返回的是 HTTP 500「The service encountered an
    unexpected internal error」而不是 400 —— 用户会看到「服务内部错误」，像是我们的故障。
    先在本地判魔数(PNG/JPEG/WebP)，给出人话错误，也省一次网络往返。
    Ark 本身对参考图很宽容：实测 5.4MB base64、3000x200 极端比例、JPEG 字节贴 png 标签都能过，
    所以这里只拦「确实是坏数据」和「大到离谱」。"""
    if not img:
        return
    try:
        raw = base64.b64decode(img)
    except Exception:
        raise ValueError("参考图不是合法的 base64")
    if len(raw) > SEEDREAM_MAX_REF_BYTES:
        raise ValueError("参考图太大，请压缩到 10MB 以内后重试")
    if not _image_bytes_look_valid(raw):
        raise ValueError("参考图格式不支持，请使用 PNG / JPG / WebP")

def _seedream_error(e):
    """Ark 的 HTTPError → 人话。内容审核类是业务失败（会走失败退点），不是系统故障。"""
    code = msg = ""
    try:
        err = (json.loads(e.read() or b"{}").get("error") or {})
        code = str(err.get("code") or "")
        msg = str(err.get("message") or "")[:180]
    except Exception:
        pass
    if "SensitiveContent" in code:      # Output/InputImageSensitiveContentDetected；官方对此不计费
        return ValueError("内容审核未通过，换个提示词或参考图再试")
    return ValueError("黄雀引擎 1 %s: %s" % (e.code, msg or code or "调用失败"))

def _seedream_fetch(url, tries=3):
    """直连下载出图结果。单独重试：下载失败不会重新生成图片，也就不会重复计费。
    IncompleteRead 不属于 _is_transient，这里单列 —— 大响应体断流实测会撞到它。"""
    last = None
    for i in range(tries):
        try:
            with _NOPROXY.open(url, timeout=120) as r:   # TOS 在国内，必须直连，不走 mihomo
                return r.read()
        except (urllib.error.URLError, http.client.IncompleteRead,
                TimeoutError, ConnectionError) as e:
            last = e
            if i < tries - 1:
                time.sleep(1.5 * (i + 1))
    raise ValueError("黄雀引擎 1 出图下载失败: %s" % str(last)[:120])

SEEDREAM_429_TRIES = int(os.environ.get("SEEDREAM_429_TRIES", "8") or 8)
SEEDREAM_429_MAX_WAIT = int(os.environ.get("SEEDREAM_429_MAX_WAIT", "700") or 700)  # 总退避预算，压在 reaper image 900s 内

def _seedream_post(fn, tries=None, max_wait=None):
    """生成请求的重试闸：**只重试 429**。

    通用 _retry 会重试 5xx/超时/URLError，但出图 POST 是非幂等的：那几种情况下
    Ark 很可能已经出图并计费，重发 = 再出一张 = 重复计费。只有 429(限流) 能确定
    请求被拒、未出图、未计费，重试才安全。其余一律直接抛出，走失败退点。
    下载(GET)是幂等的，重试见 _seedream_fetch。

    Ark 账号并发上限实测约 4~5：作图 worker 池更大时，10 路突发有 6 路会 429。
    退避到足够长以让前面的任务腾出并发槽，总退避压在 SEEDREAM_429_MAX_WAIT(<reaper 900s)。
    指数退避 3→6→12…上限 60s，并加 ±30% 抖动，避免 N 个 worker 同刻重试形成新洪峰。"""
    tries = SEEDREAM_429_TRIES if tries is None else tries
    max_wait = SEEDREAM_429_MAX_WAIT if max_wait is None else max_wait
    waited = 0.0
    for i in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            body = (e.read() or b"").decode("utf-8", "replace")
            # 两种 429 要分开：SetLimitExceeded=账号用量上限/安全体验模式，模型已被**暂停**——
            # 重试再久也没用，只会白占 worker(实测 246s×10 全失败)，立刻失败退点并给人话。
            if "SetLimitExceeded" in body or "Safe Experience" in body or "has been paused" in body:
                raise ValueError("黄雀引擎 1 当前用量上限已达到，请稍后重试或联系管理员")
            # 其余 429 = 瞬时并发/速率限制，请求被拒未出图未计费 → 安全重试。
            if i == tries - 1:
                raise ValueError("黄雀引擎 1 并发繁忙，请稍后重试")
            delay = min(60.0, 3.0 * (2 ** i)) * (0.7 + random.random() * 0.6)   # 指数退避 + 抖动
            if waited + delay > max_wait:      # 退避预算耗尽 → 别再等，直接抛(走失败退点)
                raise ValueError("黄雀引擎 1 并发繁忙，请稍后重试")
            waited += delay
            print("[seedream] 429 并发限流，退避重试(%d/%d) 等%.1fs" % (i + 1, tries, delay), flush=True)
            time.sleep(delay)

def _seedream_one(model, prompt, size, img):
    """出一张图，返回 PNG 字节。

    response_format 用 url 而非 b64_json：PNG 的 b64 响应体有 4~5MB，实测会 IncompleteRead，
    而 POST 是非幂等的（重试 = 重新出图 = 重复计费），不能靠重试硬扛。换成 url 后响应体很小，
    失败只需重试下载。output_format 必须显式写 png —— 不指定时 Ark 默认吐 JPEG，
    会和 .png 文件名 / image/png 的 Content-Type 对不上。"""
    body = {"model": model, "prompt": prompt, "size": size,
            "output_format": "png", "response_format": "url", "watermark": False}
    if img:
        # 实测：image 必须是 data URI；裸 base64 会被判成 URL 并报 400 invalid url specified。
        body["image"] = "data:image/png;base64," + img
    data = json.dumps(body, ensure_ascii=False).encode()
    try:
        d = _seedream_post(lambda: _post("/images/generations", data, "application/json",
                                        base=ARK_BASE, key=ARK_API_KEY, proxy=False))
    except urllib.error.HTTPError as e:
        raise _seedream_error(e)
    items = d.get("data") or []
    url = (items[0] or {}).get("url") if items else None
    if not url:
        raise ValueError("黄雀引擎 1 返回为空")
    return _seedream_fetch(url)

def _gen_image_seedream(prompt, ratio, quality, count, img, variant):
    """Seedream 5.0 / 5.0 Pro：文生图 + 图生图（同一端点，带 image 即图生图）。
    实测耗时(PNG 输出)：标准约 30~40s，Pro 约 85s —— Pro 慢一倍多，前端提示要分开写。
    单图 2~7MB。SEEDREAM_MAX_N=2 时 Pro 最坏约 170s，在 reaper image 900s 宽限内。"""
    if not ARK_API_KEY:
        raise ValueError("黄雀引擎 1 暂未配置，请联系管理员")
    _seedream_check_ref(img)     # 坏参考图会让 Ark 回 500，先在本地拦掉并说人话
    model = SEEDREAM_MODELS.get(variant) or SEEDREAM_MODELS["std"]
    size = _seedream_size(ratio, quality, variant)   # Pro 的像素上限低得多，必须按型号夹逼
    files_out, urls = [], []
    for _ in range(count):
        raw = _seedream_one(model, prompt, size, img)
        fn = "img_%s.png" % uuid.uuid4().hex   # 不可猜键(#185)
        (OUT_DIR / fn).write_bytes(raw)
        files_out.append(fn)
        urls.append(public_url(fn, "image/png"))
    if not files_out:
        raise ValueError("出图返回为空")
    return {"type": "image", "mode": ("img2img" if img else "text2img"), "provider": "seedream",
            "variant": variant, "model": model, "size": size, "count": len(files_out),
            "file": files_out[0], "url": urls[0], "files": files_out, "urls": urls,
            "ratio": ratio, "prompt": prompt}


def _trusted_short_drama_file(value, *, file_url=False):
    value = str(value or "").strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return ""
    if file_url:
        prefix = "/api/gen/file/"
        if not parsed.path.startswith(prefix):
            return ""
        relative = urllib.parse.unquote(parsed.path[len(prefix):])
    else:
        if parsed.path.startswith("/"):
            return ""
        relative = urllib.parse.unquote(parsed.path)
    if (not relative or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        return ""
    try:
        root = OUT_DIR.resolve()
        candidate = (OUT_DIR / relative).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        return ""
    try:
        if not candidate.is_file() or candidate.stat().st_size > IMAGE_REF_MAX_BYTES:
            return ""
        return candidate.relative_to(root).as_posix()
    except OSError:
        return ""


def _trusted_short_drama_continuity(url="", local_file=""):
    """Load only a validated local result; unsafe/missing input falls back to prompt."""
    relative = (
        _trusted_short_drama_file(local_file)
        or _trusted_short_drama_file(url, file_url=True)
    )
    if not relative:
        return None
    try:
        return (OUT_DIR.resolve() / relative).read_bytes()
    except OSError:
        return None


def gen_image(payload):
    payload = validate_image_payload(payload)
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    references = payload.get("short_drama_references")
    if isinstance(references, list):
        context = []
        continuity = None
        for reference in references:
            if not isinstance(reference, dict):
                continue
            ref_type = str(reference.get("type") or "")
            name = str(reference.get("name") or "").strip()
            if ref_type == "character" and name:
                context.append("character appearance: " + name)
            elif ref_type == "continuity":
                if name:
                    context.append("visual continuity: " + name)
                if continuity is None:
                    continuity = reference
        if context:
            prompt += "\nTrusted short-drama continuity context:\n" + "\n".join(context)
        if continuity is not None and not payload.get("image"):
            local_continuity = _trusted_short_drama_continuity(
                continuity.get("url"), continuity.get("file")
            )
            if local_continuity:
                payload["image"] = base64.b64encode(local_continuity).decode("ascii")
    payload["prompt"] = prompt
    ratio = payload.get("ratio") or "1:1"
    img   = _clean_b64(payload.get("image"))  # 参考图 → 图生图 / 局部修改；清洗防 padding 错(#6)
    mask  = _clean_b64(payload.get("mask"))   # 蒙版(透明处=要重绘的区域) → 局部修改
    quality = "high" if (payload.get("quality") or "hd") == "hd" else "medium"  # 标准=medium/高清=high
    provider = (payload.get("provider") or "openai").strip().lower()
    if provider == "zelong2":
        raise ValueError("泽龙2生图渠道维护中，请使用 Seedream 或果肉生图")
    if provider == "xiaole":
        count = 1 if mask else max(1, min(2, int(payload.get("count") or 1)))
        return _gen_image_xiaole(prompt, ratio, quality, count, img, payload.get("reference_images"))
    if provider == "seedream":
        if mask:
            raise ValueError("黄雀引擎 1 暂不支持局部修改（蒙版），请改用黄雀引擎 2")
        variant = "pro" if str(payload.get("variant") or "").strip().lower() == "pro" else "std"
        q = "hd" if (payload.get("quality") or "hd") == "hd" else "std"   # Seedream 按像素分档，不用 high/medium
        count = max(1, min(SEEDREAM_MAX_N, int(payload.get("count") or 1)))
        return _gen_image_seedream(prompt, ratio, q, count, img, variant)
    size  = SIZES.get(ratio, "1024x1024")
    if provider in {"zelong", "zelong2"}:
        if provider == "zelong2":
            base, key, provider_label = ZELONG2_BASE, ZELONG2_KEY, "泽龙2(chatgpt2api)"   # 专供生图号池
            if not _zelong2_accounts():
                raise ValueError(provider_label + "未配置 key")
        else:
            base, key, provider_label = ZELONG_BASE, ZELONG_KEY, "泽龙Ai(中转站)"
        proxy = False   # 国内中转/本方上游直连，不走代理
        if provider != "zelong2" and not key:
            raise ValueError(provider_label + "未配置 key")
        size = "1024x1024"   # 泽龙系图片渠道只支持 1024x1024；其它尺寸(9:16/16:9/auto)会 400 INVALID_IMAGE_SIZE，强制正方形保稳定出图
    else:
        base, key, proxy = OPENAI_BASE, OPENAI_KEY, True
    cap = 2 if provider in {"zelong", "zelong2"} else 4      # 中转出图慢，数量上限低
    count = 1 if mask else max(1, min(cap, int(payload.get("count") or 1)))  # 局部修改只出 1 张
    if img:
        files = [("image", "in.png", base64.b64decode(img))]
        if mask:
            files.append(("mask", "mask.png", base64.b64decode(mask)))
        body, ct = _multipart({"model": "gpt-image-2", "prompt": prompt, "size": size, "quality": quality, "n": str(count)}, files)
        d = _dispatch_gpt(provider, "/v1/images/edits", body, ct, base, key, proxy)
        mode = "inpaint" if mask else "img2img"
    else:
        body = json.dumps({"model": "gpt-image-2", "prompt": prompt, "size": size, "quality": quality, "n": count}).encode()
        d = _dispatch_gpt(provider, "/v1/images/generations", body, "application/json", base, key, proxy, streaming=True)
        mode = "text2img"
    files_out, urls = [], []
    for i, item in enumerate(d.get("data") or []):
        fn = "img_%s_%d.png" % (uuid.uuid4().hex, i)  # 不可猜键(#185)：杜绝时间戳猜测
        if item.get("b64_json"):
            (OUT_DIR / fn).write_bytes(base64.b64decode(item["b64_json"]))
        elif item.get("url"):                                # 部分中转返回 url 而非 b64
            opener = urllib.request.urlopen if proxy else _NOPROXY.open
            with opener(item["url"], timeout=120) as rr:
                (OUT_DIR / fn).write_bytes(rr.read())
        else:
            continue
        files_out.append(fn); urls.append(public_url(fn, "image/png"))
    if not files_out:
        raise ValueError("出图返回为空")
    return {"type": "image", "mode": mode, "provider": provider, "count": len(files_out),
            "file": files_out[0], "url": urls[0], "files": files_out, "urls": urls, "ratio": ratio, "prompt": prompt}

HANDLERS = {"image": gen_image}
