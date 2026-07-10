# -*- coding: utf-8 -*-
import http.client
import os
import threading
import time
import urllib.error

from .core import (
    OPENAI_BASE, OPENAI_KEY, OUT_DIR, SIZES, ZELONG2_BASE, ZELONG2_KEY,
    ZELONG_BASE, ZELONG_KEY, _NOPROXY, _multipart, _post,
    base64, json, public_url, urllib, uuid,
)
from .video import XIAOLEVIDEO_API_KEY, _xiaole_request

# gpt 引擎出境优先级：VPS 隧道 → mihomo → heygen（见 egress.py）。官方 OpenAI 直连地址：
OPENAI_OFFICIAL_BASE = os.environ.get("OPENAI_OFFICIAL_BASE", "https://api.openai.com").rstrip("/")

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
SEEDREAM_HD_PIXELS = 9400000    # 高清档目标像素（实测 4688x2000 / 4096x4096 均可，未触上限）
SEEDREAM_MAX_N = 2              # 数量上限，须与 points.cost_of 的 cap 一致，否则按 N 扣点却只出 2 张

_ZELONG2_POOL_LOCK = threading.Lock()
_ZELONG2_POOL_NEXT = 0

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
    errors = []
    for idx, account in enumerate(_zelong2_attempts(), 1):
        try:
            return _retry(lambda: _post(path, data, ctype, base=account["base"], key=account["key"], proxy=False))
        except Exception as e:
            errors.append("#%d %s: %s" % (idx, account["base"], str(e)[:160]))
            print("[zelong2-pool] attempt failed %s" % errors[-1], flush=True)
    raise ValueError("泽龙2号池全部失败: " + " | ".join(errors))

def _gen_image_xiaole(prompt, ratio, quality, count, img):
    """果肉生图渠道(xiaolevideo.cn，与果肉/豆姐视频同账号)：gpt-image-2 文生图/图生图。
    统一 generations API：创建 → 轮询 → 落盘，与 video.py 的 generate_xiaole_video 同一套模式。"""
    if not XIAOLEVIDEO_API_KEY:
        raise ValueError("果肉生图未配置（XIAOLEVIDEO_API_KEY）")
    resolution = "2k" if quality == "high" else "1k"
    input_d = {"prompt": prompt, "mode": ("image_to_image" if img else "text_to_image"),
               "resolution": resolution, "aspect_ratio": ratio, "quality": quality, "n": count}
    if img:
        input_d["reference_images"] = [{"type": "base64", "value": img}]
    create = _xiaole_request("POST", "/api/v1/generations", {"model": "gpt-image-2", "input": input_d})
    if create.get("code") not in (200, 0, None):
        raise ValueError("出图创建失败: %s" % str(create.get("message"))[:200])
    data = create.get("data") or {}
    rid = data.get("request_id") or data.get("task_id")
    status_url = data.get("status_url") or (("/api/v1/generations/" + str(rid)) if rid else "")
    if not status_url:
        raise ValueError("渠道未返回任务ID")
    deadline = time.time() + 300   # 果肉生图实测~239s近死线,放宽防误超时(前端banana轮询容忍900s #331)
    images = None
    while time.time() < deadline:
        st = _xiaole_request("GET", status_url, timeout=30)
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
    return {"type": "image", "mode": ("img2img" if img else "text2img"), "provider": "xiaole",
            "count": len(files_out), "file": files_out[0], "url": urls[0],
            "files": files_out, "urls": urls, "ratio": ratio, "prompt": prompt}

def _dispatch_gpt(provider, path, body, ct, base, key, proxy):
    """gpt 家族出图分发。openai(官方) 走出境优先级链 VPS→mihomo→heygen；泽龙系维持原样。"""
    if provider == "zelong2":
        return _post_zelong2(path, body, ct)
    if provider == "zelong":
        return _retry(lambda: _post(path, body, ct, base=base, key=key, proxy=proxy))
    # provider == "openai"：优先自建出境直连官方，超时/报错降级，最终兜底 heygen(=OPENAI_BASE)。
    # 未配 EGRESS_* 时 egress 链里只剩 heygen 一档，等于改动前的老行为。
    from content_domains import egress
    return egress.post_json(OPENAI_OFFICIAL_BASE, OPENAI_BASE, path, body,
                            {"Authorization": "Bearer " + OPENAI_KEY, "Content-Type": ct},
                            log=lambda m: print(m, flush=True))


def _seedream_size(ratio, quality):
    """比例 → 显式「宽x高」。Ark 不吃 1:1 这种比例串，只吃像素尺寸，且有 3686400 像素硬下限
    （实测 1024x1024、1152x2048 都被 400 拒），所以按目标像素反解并对齐到 16 的倍数。"""
    try:
        rw, rh = (int(x) for x in str(ratio).split(":", 1))
        if rw <= 0 or rh <= 0:
            raise ValueError
    except Exception:
        rw, rh = 9, 16
    target = SEEDREAM_HD_PIXELS if quality == "hd" else SEEDREAM_MIN_PIXELS
    scale = (target / float(rw * rh)) ** 0.5
    w = max(16, int(round(rw * scale / 16.0)) * 16)
    h = max(16, int(round(rh * scale / 16.0)) * 16)
    while w * h < SEEDREAM_MIN_PIXELS:   # 向下取整可能压到线下，往上顶一格
        w += 16
        h += 16
    return "%dx%d" % (w, h)

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
    return ValueError("Seedream %s: %s" % (e.code, msg or code or "调用失败"))

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
    raise ValueError("Seedream 出图下载失败: %s" % str(last)[:120])

def _seedream_post(fn, tries=2, base_delay=2.0):
    """生成请求的重试闸：**只重试 429**。

    通用 _retry 会重试 5xx/超时/URLError，但出图 POST 是非幂等的：那几种情况下
    Ark 很可能已经出图并计费，重发 = 再出一张 = 重复计费。只有 429(限流) 能确定
    请求被拒、未出图，重试才安全。其余一律直接抛出，走失败退点由用户决定重试。
    下载(GET)是幂等的，重试见 _seedream_fetch。"""
    for i in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == tries - 1:
                raise
            print("[seedream] 429 限流，退避重试(%d)" % (i + 1), flush=True)
            time.sleep(base_delay * (i + 1))

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
        raise ValueError("Seedream 返回为空")
    return _seedream_fetch(url)

def _gen_image_seedream(prompt, ratio, quality, count, img, variant):
    """Seedream 5.0 / 5.0 Pro：文生图 + 图生图（同一端点，带 image 即图生图）。
    实测耗时(PNG 输出)：标准约 30~40s，Pro 约 85s —— Pro 慢一倍多，前端提示要分开写。
    单图 2~7MB。SEEDREAM_MAX_N=2 时 Pro 最坏约 170s，在 reaper image 900s 宽限内。"""
    if not ARK_API_KEY:
        raise ValueError("Seedream 未配置（ARK_API_KEY）")
    model = SEEDREAM_MODELS.get(variant) or SEEDREAM_MODELS["std"]
    size = _seedream_size(ratio, quality)
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


def gen_image(payload):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    ratio = payload.get("ratio") or "1:1"
    img   = _clean_b64(payload.get("image"))  # 参考图 → 图生图 / 局部修改；清洗防 padding 错(#6)
    mask  = _clean_b64(payload.get("mask"))   # 蒙版(透明处=要重绘的区域) → 局部修改
    quality = "high" if (payload.get("quality") or "hd") == "hd" else "medium"  # 标准=medium/高清=high
    provider = (payload.get("provider") or "openai").strip().lower()
    if provider == "xiaole":
        count = 1 if mask else max(1, min(2, int(payload.get("count") or 1)))
        return _gen_image_xiaole(prompt, ratio, quality, count, img)
    if provider == "seedream":
        if mask:
            raise ValueError("Seedream 暂不支持局部修改（蒙版），请改用 gpt-image-2")
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
        d = _dispatch_gpt(provider, "/v1/images/generations", body, "application/json", base, key, proxy)
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
