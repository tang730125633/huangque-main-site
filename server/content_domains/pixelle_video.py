# -*- coding: utf-8 -*-
"""Pixelle text-to-video adapter for the authenticated content job pipeline."""

import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .core import OUT_DIR, public_url
from . import feature_flags


def _env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


PIXELLE_API_URL = os.environ.get("PIXELLE_API_URL", "http://127.0.0.1:8103").rstrip("/")
PIXELLE_MEDIA_WORKFLOW = os.environ.get(
    "PIXELLE_MEDIA_WORKFLOW", "runninghub/image_flux.json"
).strip()
PIXELLE_VIDEO_WORKFLOW = os.environ.get(
    "PIXELLE_VIDEO_WORKFLOW", "runninghub/video_wan2.1_fusionx.json"
).strip()
PIXELLE_TTS_WORKFLOW = os.environ.get(
    "PIXELLE_TTS_WORKFLOW", "selfhost/tts_edge.json"
).strip()
PIXELLE_JOB_TIMEOUT = _env_int("PIXELLE_JOB_TIMEOUT", 900, 60, 900)
PIXELLE_POLL_INTERVAL = _env_int("PIXELLE_POLL_INTERVAL", 3, 1, 15)
PIXELLE_MAX_VIDEO_BYTES = _env_int(
    "PIXELLE_MAX_VIDEO_BYTES", 512 * 1024 * 1024, 10 * 1024 * 1024, 1024 * 1024 * 1024
)
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DEFAULT_STYLE = "realistic_commercial"
_STYLE_COMMON_RESTRICTIONS = (
    "No watermark, no logo, no garbled or unreadable text, "
    "no malformed people, objects, hands, or anatomy."
)
STYLE_PRESETS = (
    {
        "key": "realistic_commercial",
        "name": "写实商业",
        "prompt_prefix": (
            "Photorealistic commercial advertising, authentic people or products, "
            "modern business environment, natural professional lighting, credible "
            "editorial composition, realistic materials and balanced colors. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "cinematic",
        "name": "电影质感",
        "prompt_prefix": (
            "Cinematic visual storytelling, motivated film lighting, narrative composition, "
            "shallow depth of field, restrained color grading, realistic texture, subtle film grain. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "future_tech",
        "name": "科技未来",
        "prompt_prefix": (
            "Premium near-future AI visual design, clean advanced spaces, precise blue and cyan "
            "accent lighting, refined interface motifs, crisp geometry, high-end technology campaign. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "healing_fresh",
        "name": "治愈清新",
        "prompt_prefix": (
            "Bright healing lifestyle visual, soft natural daylight, gentle low-saturation palette, "
            "airy everyday setting, calm composition, light and optimistic atmosphere. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "chinese_illustration",
        "name": "国风插画",
        "prompt_prefix": (
            "Contemporary Chinese illustration, elegant ink wash and fine brush textures, intentional "
            "negative space, refined oriental palette, poetic layered composition, delicate paper texture. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "cartoon_3d",
        "name": "3D 卡通",
        "prompt_prefix": (
            "Polished 3D cartoon scene, appealing rounded characters or objects, soft tactile materials, "
            "bright studio lighting, expressive but coherent forms, premium animated-film rendering. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "retro_film",
        "name": "复古胶片",
        "prompt_prefix": (
            "Documentary retro film aesthetic, organic analog grain, soft highlight roll-off, nostalgic "
            "muted colors, natural candid composition, authentic period camera texture. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "minimal_line",
        "name": "极简线稿",
        "prompt_prefix": (
            "Minimal line-art illustration, precise clean strokes, limited color palette, generous negative "
            "space, editorial infographic composition, clear visual hierarchy, refined paper-white ground. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "medical_beauty",
        "name": "医美高级感",
        "prompt_prefix": (
            "Premium medical-aesthetics campaign, immaculate contemporary clinic, soft clean lighting, "
            "natural skin texture, restrained luxury, trustworthy professional mood, elegant neutral palette. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "ecommerce_product",
        "name": "电商产品广告",
        "prompt_prefix": (
            "High-conversion ecommerce product advertising, unmistakable hero product, controlled studio "
            "lighting, crisp material details, clean background, premium commercial composition and depth. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
)
STYLE_PRESETS_BY_KEY = {item["key"]: item for item in STYLE_PRESETS}


class PixelleTransientError(RuntimeError):
    """A temporary transport failure that is safe to retry while polling."""

_PORTRAIT_ILLUSTRATION_NAMES = {
    "image_blur_card.html": "玻璃模糊卡片",
    "image_book.html": "书页阅读",
    "image_cartoon.html": "卡通插画",
    "image_default.html": "标准图文",
    "image_elegant.html": "优雅杂志",
    "image_excerpt.html": "摘录卡片",
    "image_fashion_vintage.html": "复古时尚",
    "image_full.html": "全屏沉浸",
    "image_healing.html": "治愈清新",
    "image_health_preservation.html": "养生科普",
    "image_life_insights.html": "人生感悟",
    "image_life_insights_light.html": "明亮感悟",
    "image_long_text.html": "长文排版",
    "image_modern.html": "现代简约",
    "image_neon.html": "霓虹科技",
    "image_psychology_card.html": "心理卡片",
    "image_purple.html": "紫色质感",
    "image_satirical_cartoon.html": "讽刺漫画",
    "image_simple_black.html": "极简黑色",
    "image_simple_line_drawing.html": "线稿插画",
}
_LANDSCAPE_ILLUSTRATION_NAMES = {
    "image_book.html": "横版书页",
    "image_film.html": "电影叙事",
    "image_full.html": "横版沉浸",
    "image_ultrawide_minimal.html": "超宽极简",
    "image_wide_darktech.html": "暗黑科技",
}
_PORTRAIT_VIDEO_NAMES = {
    "video_default.html": "动态图文",
    "video_healing.html": "治愈动态",
}
_PNG_PREVIEWS = {
    "1080x1920/image_blur_card.html",
    "1080x1920/image_cartoon.html",
    "1080x1920/video_default.html",
    "1080x1920/video_healing.html",
}


def _template_record(size, filename, name, kind):
    width, height = (int(value) for value in size.split("x", 1))
    key = size + "/" + filename
    extension = ".png" if key in _PNG_PREVIEWS else ".jpg"
    return {
        "key": key,
        "name": name,
        "width": width,
        "height": height,
        "kind": kind,
        "orientation": "portrait" if height > width else "landscape",
        "preview_url": "../assets/pixelle-templates/%s/%s%s" % (
            size,
            pathlib.Path(filename).stem,
            extension,
        ),
    }


TEMPLATES = tuple(
    [
        _template_record("1080x1920", filename, name, "illustration")
        for filename, name in _PORTRAIT_ILLUSTRATION_NAMES.items()
    ]
    + [
        _template_record("1920x1080", filename, name, "illustration")
        for filename, name in _LANDSCAPE_ILLUSTRATION_NAMES.items()
    ]
    + [
        _template_record("1080x1920", filename, name, "video")
        for filename, name in _PORTRAIT_VIDEO_NAMES.items()
    ]
)
TEMPLATES_BY_KEY = {item["key"]: item for item in TEMPLATES}
TEMPLATE_KEYS = {item["key"] for item in TEMPLATES}
FEATURE_KEY = "pixelle_text_video"
_HEALTH_CACHE = {"checked_at": 0.0, "ready": False}
_HEALTH_TTL = 5


def public_templates():
    return [dict(item) for item in TEMPLATES]


def public_styles():
    return [
        {"key": item["key"], "name": item["name"]}
        for item in STYLE_PRESETS
    ]


def _fixed_segments(text):
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]


def prepare_payload(payload):
    body = dict(payload or {})
    raw_text = str(body.get("text") or "")
    mode = str(body.get("mode") or "generate").strip()
    if mode not in {"generate", "fixed"}:
        raise ValueError("请选择主题创作或完整文案")
    if mode == "fixed":
        segments = _fixed_segments(raw_text)
        text = "\n\n".join(segments)
    else:
        text = re.sub(r"\s+", " ", raw_text).strip()
        segments = [text] if text else []
    if len(text) < 2:
        raise ValueError("请输入至少 2 个字的主题或文案")
    if len(text) > 1000:
        raise ValueError("文案不能超过 1000 个字")
    if mode == "fixed" and len(segments) > 20:
        raise ValueError("完整文案最多支持 20 个段落，请合并后再提交")
    template = str(body.get("template") or "1080x1920/image_default.html").strip()
    if template not in TEMPLATE_KEYS:
        raise ValueError("请选择有效的视频模板")
    style = str(body.get("style") or DEFAULT_STYLE).strip()
    if style not in STYLE_PRESETS_BY_KEY:
        raise ValueError("请选择有效的素材风格")
    scene_count = 5 if mode == "generate" else len(segments)
    return {
        "pipeline": "pixelle",
        "text": text,
        "mode": mode,
        "template": template,
        "style": style,
        "n_scenes": scene_count,
        "scenes": [{"line": line} for line in segments],
    }


def _json_request(method, path, payload=None, timeout=30):
    body = None if payload is None else json.dumps(
        payload, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(
        PIXELLE_API_URL + path,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method=method,
    )
    try:
        with _NO_PROXY.open(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read() or b"{}")
            detail = error.get("detail") or error.get("message")
        except Exception:
            detail = ""
        raise RuntimeError(
            "视频生成服务请求失败（HTTP %s）：%s" % (exc.code, detail or "未知错误")
        )
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise PixelleTransientError(
            "无法连接视频生成服务：%s" % str(reason)[:160]
        )


def availability(force=False):
    enabled = feature_flags.is_enabled(FEATURE_KEY)
    if not enabled:
        return {"enabled": False, "ready": False, "available": False}
    now = time.monotonic()
    if force or now - _HEALTH_CACHE["checked_at"] > _HEALTH_TTL:
        ready = False
        try:
            health = _json_request("GET", "/health", timeout=3)
            ready = str(health.get("status") or "").lower() == "healthy"
        except Exception:
            ready = False
        _HEALTH_CACHE.update({"checked_at": now, "ready": ready})
    ready = bool(_HEALTH_CACHE["ready"])
    return {"enabled": True, "ready": ready, "available": ready}


def require_available():
    feature_flags.require_enabled(FEATURE_KEY)
    if not availability().get("ready"):
        raise feature_flags.FeatureDisabled("文案成片服务暂不可用，请稍后重试")


def _prepared_text(text, mode):
    if mode == "fixed" and re.search(r"[\u3400-\u9fff]", text):
        return text
    label = "主题" if mode == "generate" else "原始文案"
    return (
        "请只使用简体中文创作全部旁白、标题和屏幕文案，不要输出英文旁白或英文字幕。"
        "保持原意，语言自然、适合中文短视频口播。\n%s：%s" % (label, text)
    )


def _submit(payload):
    template = TEMPLATES_BY_KEY[payload["template"]]
    media_workflow = (
        PIXELLE_VIDEO_WORKFLOW
        if template["kind"] == "video"
        else PIXELLE_MEDIA_WORKFLOW
    )
    response = _json_request("POST", "/api/video/generate/async", {
        "text": _prepared_text(payload["text"], payload["mode"]),
        "mode": payload["mode"],
        "n_scenes": payload["n_scenes"],
        "frame_template": payload["template"],
        "media_workflow": media_workflow,
        "tts_workflow": PIXELLE_TTS_WORKFLOW,
        "video_fps": 30,
        "bgm_volume": 0.18,
    })
    task_id = str(response.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("视频生成服务没有返回任务 ID")
    return task_id


def _wait(task_id):
    deadline = time.monotonic() + PIXELLE_JOB_TIMEOUT
    while time.monotonic() < deadline:
        try:
            task = _json_request(
                "GET", "/api/tasks/" + urllib.parse.quote(task_id, safe=""), timeout=20
            )
        except PixelleTransientError:
            time.sleep(PIXELLE_POLL_INTERVAL)
            continue
        status = str(task.get("status") or "pending").lower()
        if status == "completed":
            return dict(task.get("result") or {})
        if status in {"failed", "cancelled"}:
            raise RuntimeError(str(task.get("error") or "视频生成失败")[:300])
        time.sleep(PIXELLE_POLL_INTERVAL)
    raise RuntimeError("视频生成超时，请稍后重试")


def _safe_upstream_video_url(value):
    base = urllib.parse.urlsplit(PIXELLE_API_URL)
    base_prefix = base.path.rstrip("/")
    parsed = urllib.parse.urlsplit(str(value or ""))
    if not parsed.scheme and not parsed.netloc:
        relative_path = "/" + str(value).lstrip("/")
        if base_prefix and relative_path.startswith(base_prefix + "/"):
            resolved_path = relative_path
        else:
            resolved_path = base_prefix + relative_path
        parsed = urllib.parse.urlsplit(urllib.parse.urlunsplit((
            base.scheme,
            base.netloc,
            resolved_path,
            "",
            "",
        )))
    if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
        raise RuntimeError("视频生成服务返回了无效的文件地址")
    prefixed_files = base_prefix + "/api/files/"
    if parsed.path.startswith(prefixed_files):
        return urllib.parse.urlunsplit(parsed)
    if parsed.path.startswith("/api/files/"):
        parsed = parsed._replace(path=base_prefix + parsed.path)
        return urllib.parse.urlunsplit(parsed)
    else:
        raise RuntimeError("视频生成服务返回了无效的文件路径")


def _download_video(url, job_id):
    relative = pathlib.Path("video") / (
        "pixelle_%s.mp4" % (str(job_id or uuid.uuid4().hex)[:64])
    )
    target = OUT_DIR / relative
    temporary = target.with_suffix(".mp4.part")
    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with _NO_PROXY.open(url, timeout=180) as response, temporary.open("wb") as handle:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > PIXELLE_MAX_VIDEO_BYTES:
                raise RuntimeError("生成视频超过允许的文件大小")
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > PIXELLE_MAX_VIDEO_BYTES:
                    raise RuntimeError("生成视频超过允许的文件大小")
                handle.write(chunk)
        with temporary.open("rb") as handle:
            header = handle.read(64)
        if total < 1024 or b"ftyp" not in header:
            raise RuntimeError("视频生成服务返回的文件无效")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return relative.as_posix(), total


def generate(payload):
    task_id = _submit(payload)
    result = _wait(task_id)
    source_url = _safe_upstream_video_url(result.get("video_url"))
    video_file, file_size = _download_video(source_url, payload.get("_job_id"))
    return {
        "type": "script_to_video",
        "pipeline": "pixelle",
        "video_file": video_file,
        "video_url": public_url(video_file, "video/mp4", private=True),
        "duration": round(float(result.get("duration") or 0), 3),
        "scene_count": int(result.get("scene_count") or payload.get("n_scenes") or 1),
        "template": payload["template"],
        "input_mode": payload["mode"],
        "file_size": int(result.get("file_size") or file_size),
        "upstream_task_id": task_id,
    }
