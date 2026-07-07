# -*- coding: utf-8 -*-
import os
import threading
import time

from .core import (
    OPENAI_BASE, OPENAI_KEY, OUT_DIR, SIZES, ZELONG2_BASE, ZELONG2_KEY,
    ZELONG_BASE, ZELONG_KEY, _NOPROXY, _multipart, _post,
    base64, json, public_url, urllib, uuid,
)
from .video import XIAOLEVIDEO_API_KEY, _xiaole_request

_ZELONG2_POOL_LOCK = threading.Lock()
_ZELONG2_POOL_NEXT = 0

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
            return _post(path, data, ctype, base=account["base"], key=account["key"], proxy=False)
        except Exception as e:
            errors.append("#%d %s: %s" % (idx, account["base"], str(e)[:160]))
            print("[zelong2-pool] attempt failed %s" % errors[-1], flush=True)
    raise ValueError("泽龙2号池全部失败: " + " | ".join(errors))

def _gen_image_xiaole(prompt, ratio, quality, count, img):
    """果肉生图渠道(xiaolevideo.cn，与果肉/微衣视频同账号)：gpt-image-2 文生图/图生图。
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

def gen_image(payload):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    ratio = payload.get("ratio") or "1:1"
    img   = payload.get("image")   # base64(无 data: 前缀) — 上传参考图 → 图生图 / 局部修改
    mask  = payload.get("mask")    # base64 — 蒙版(透明处=要重绘的区域) → 局部修改
    quality = "high" if (payload.get("quality") or "hd") == "hd" else "medium"  # 标准=medium/高清=high
    provider = (payload.get("provider") or "openai").strip().lower()
    if provider == "xiaole":
        count = 1 if mask else max(1, min(2, int(payload.get("count") or 1)))
        return _gen_image_xiaole(prompt, ratio, quality, count, img)
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
        d = _post_zelong2("/v1/images/edits", body, ct) if provider == "zelong2" else _post("/v1/images/edits", body, ct, base=base, key=key, proxy=proxy)
        mode = "inpaint" if mask else "img2img"
    else:
        body = json.dumps({"model": "gpt-image-2", "prompt": prompt, "size": size, "quality": quality, "n": count}).encode()
        d = _post_zelong2("/v1/images/generations", body, "application/json") if provider == "zelong2" else _post("/v1/images/generations", body, "application/json", base=base, key=key, proxy=proxy)
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
