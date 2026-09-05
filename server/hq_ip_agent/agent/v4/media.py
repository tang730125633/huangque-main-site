"""采集成果图片的下载与本地化。

小红书等 CDN 直链带签名、防盗链，页面直连常 403 或链接过期——
用户看到的是「只给了链接」甚至「贴了图但看不到」。
这里把图片下载到本地目录，由 /api/v4/media/ 提供，浏览器里长期可见。
"""
from __future__ import annotations

import logging
import os
import urllib.request

from . import observability

log = logging.getLogger("hq.v4.media")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_MAX_BYTES = 12 * 1024 * 1024
_EXT_BY_MIME = {"image/jpeg": ".jpg", "image/png": ".png",
                "image/webp": ".webp", "image/gif": ".gif"}
_IMAGE_KEYS = ("images", "image_urls", "imgs")


def extract_image_urls(content, limit: int = 12) -> list:
    """从采集任务结果里递归找 images/image_urls/imgs 键，返回去重后的 http URL 列表。

    只认这些键，不扫所有 http 字符串（评论头像等也是 http URL，不能混进来）。
    列表项既可能是字符串，也可能是带 url 字段的对象（如 {index, url}）。"""
    found: list = []
    if isinstance(content, dict):
        for k, v in content.items():
            if k in _IMAGE_KEYS and isinstance(v, list):
                for x in v:
                    if isinstance(x, str) and x.startswith("http"):
                        found.append(x)
                    elif isinstance(x, dict) and isinstance(x.get("url"), str) \
                            and x["url"].startswith("http"):
                        found.append(x["url"])
            found.extend(extract_image_urls(v, limit))
    elif isinstance(content, list):
        for v in content:
            found.extend(extract_image_urls(v, limit))
    seen: set = set()
    out: list = []
    for u in found:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= limit:
            break
    return out


def _sniff_ext(data: bytes, ctype: str) -> str:
    ext = _EXT_BY_MIME.get((ctype or "").split(";")[0].strip().lower())
    if ext:
        return ext
    # 魔数兜底（CDN 可能不给正确 Content-Type）
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ""


def download_images(urls: list, sid: str, job_id, root: str) -> list:
    """把远程图片下载到 root/<sid>/<job_id>/，返回本地文件名列表（不含目录）。
    单张失败不影响整批。"""
    dest = os.path.join(root, str(sid), str(job_id))
    os.makedirs(dest, exist_ok=True)
    local: list = []
    for i, u in enumerate(urls):
        try:
            req = urllib.request.Request(u, headers={
                "User-Agent": _UA,
                "Referer": "https://www.xiaohongshu.com/",
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read(_MAX_BYTES + 1)
                if len(data) > _MAX_BYTES:
                    continue
                ctype = resp.headers.get("Content-Type") or ""
            ext = _sniff_ext(data, ctype)
            if not ext:
                log.warning("无法识别图片类型，跳过：%s", u[:100], extra=observability.ctx())
                continue
            name = "img_%02d%s" % (i + 1, ext)
            with open(os.path.join(dest, name), "wb") as f:
                f.write(data)
            local.append(name)
        except Exception as err:  # 单张失败不拖垮整批
            log.warning("下载失败 %s: %s", u[:100], err, extra=observability.ctx())
    return local
