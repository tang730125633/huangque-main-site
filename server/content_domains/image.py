# -*- coding: utf-8 -*-
import os
import threading

from .core import (
    OPENAI_BASE, OPENAI_KEY, OUT_DIR, SIZES, ZELONG2_BASE, ZELONG2_KEY,
    ZELONG_BASE, ZELONG_KEY, _NOPROXY, _multipart, _post,
    base64, json, public_url, urllib, uuid,
)

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

def gen_image(payload):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    ratio = payload.get("ratio") or "1:1"
    size  = SIZES.get(ratio, "1024x1024")
    img   = payload.get("image")   # base64(无 data: 前缀) — 上传参考图 → 图生图 / 局部修改
    mask  = payload.get("mask")    # base64 — 蒙版(透明处=要重绘的区域) → 局部修改
    quality = "high" if (payload.get("quality") or "hd") == "hd" else "medium"  # 标准=medium/高清=high
    provider = (payload.get("provider") or "openai").strip().lower()
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
