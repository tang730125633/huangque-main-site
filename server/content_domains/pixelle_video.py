# -*- coding: utf-8 -*-
"""Pixelle text-to-video adapter for the authenticated content job pipeline."""

import json
import math
import os
import pathlib
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing
from decimal import Decimal, ROUND_HALF_UP

from .core import OUT_DIR, public_url
from . import audio as audio_domain
from . import feature_flags
from . import pixelle_talking_assets


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
_CAPACITY_BLOCKED_UNTIL = 0.0
_MAX_CAPTION_UNITS = 28
_MAX_CAPTION_CUES = 100
_CAPTION_SENTENCE_BOUNDARIES = frozenset("。！？!?；;：:\n")
_CAPTION_CLAUSE_BOUNDARIES = frozenset("，,、 ")

DEFAULT_STYLE = "realistic_commercial"
DEFAULT_PUBLIC_VOICE = "zh-CN-YunjianNeural"
DEFAULT_TALKING_RATIO = 0.3
MIN_TALKING_RATIO = 0.1
MAX_TALKING_RATIO = 0.5
_BASE_NARRATION_CHARS_PER_SECOND = 4.0
_PLAN_RATE_WINDOW_SECONDS = 60.0
_PLAN_RATE_MAX_REQUESTS = 6
_PLAN_RATE_REQUESTS = {}
_PLAN_RATE_LOCK = threading.Lock()
_PUBLIC_VOICE_RE = re.compile(r"^zh-CN(?:-[a-z]+)?-[A-Za-z]+Neural$")
_PUBLIC_VOICE_NAMES = {
    "zh-CN-XiaoxiaoNeural": "女声-温柔（晓晓）",
    "zh-CN-XiaoyiNeural": "女声-甜美（晓伊）",
    "zh-CN-YunjianNeural": "男声-专业（云健）",
    "zh-CN-YunxiNeural": "男声-磁性（云希）",
    "zh-CN-YunyangNeural": "男声-新闻（云扬）",
    "zh-CN-YunyeNeural": "男声-自然（云野）",
    "zh-CN-YunfengNeural": "男声-沉稳（云枫）",
    "zh-CN-liaoning-XiaobeiNeural": "女声-东北（晓北）",
}
_STYLE_COMMON_RESTRICTIONS = (
    "When people appear, depict contemporary Chinese or East Asian people with natural, "
    "varied facial features and realistic skin texture, unless the user text explicitly "
    "specifies another ethnicity, nationality, or region. "
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


def _sanitized_public_voices():
    response = _json_request("GET", "/api/voices/public", timeout=10)
    items = []
    for raw in response.get("items") or []:
        if not isinstance(raw, dict):
            continue
        voice_id = str(raw.get("id") or "").strip()
        locale = str(raw.get("locale") or "").strip()
        gender = str(raw.get("gender") or "").strip().lower()
        if (
            not _PUBLIC_VOICE_RE.fullmatch(voice_id)
            or locale != "zh-CN"
            or gender not in {"male", "female"}
        ):
            continue
        items.append({
            "id": "public:" + voice_id,
            "name": _PUBLIC_VOICE_NAMES.get(voice_id, voice_id),
            "scope": "public",
            "gender": gender,
            "locale": locale,
        })
    return items


def public_voices(username):
    items = _sanitized_public_voices()
    for voice in audio_domain.list_audio_voices(username):
        if not isinstance(voice, dict) or voice.get("scope") != "personal":
            continue
        voice_key = str(voice.get("voice_key") or "").strip()
        if not voice_key or len(voice_key) > 128:
            continue
        try:
            audio_domain.require_owned_ready_personal_voice(username, voice_key)
        except ValueError:
            continue
        item = {
            "id": "personal:" + voice_key,
            "name": str(voice.get("display_name") or "个人音色")[:40],
            "scope": "personal",
        }
        preview_url = str(voice.get("preview_url") or "").strip()
        if preview_url.startswith("/api/gen/file/"):
            item["preview_url"] = preview_url
        items.append(item)
    return items


def _fixed_segments(text):
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]


def _style_key(payload):
    style = str((payload or {}).get("style") or DEFAULT_STYLE).strip()
    if style not in STYLE_PRESETS_BY_KEY:
        raise ValueError("请选择有效的素材风格")
    return style


def _speech_rate(payload):
    raw_value = (payload or {}).get("speech_rate")
    if raw_value is None:
        return 1.0
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise ValueError("请选择 0.5-2.0 之间的语速值")
    value = float(raw_value)
    if not math.isfinite(value) or value < 0.5 or value > 2.0:
        raise ValueError("请选择 0.5-2.0 之间的语速值")
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _freeze_voice(body, username):
    selection = str(body.get("voice") or ("public:" + DEFAULT_PUBLIC_VOICE)).strip()
    scope, separator, value = selection.partition(":")
    if not separator or scope not in {"public", "personal"} or not value:
        raise ValueError("请选择有效的配音音色")
    if scope == "public":
        if username:
            allowed = {
                item["id"].split(":", 1)[1]
                for item in public_voices(username)
                if item.get("scope") == "public"
            }
            if value not in allowed:
                raise ValueError("请选择有效的配音音色")
        elif value != DEFAULT_PUBLIC_VOICE and not _PUBLIC_VOICE_RE.fullmatch(value):
            raise ValueError("请选择有效的配音音色")
        return {"voice_scope": "public", "voice_id": value}
    audio_domain.require_owned_ready_personal_voice(username, value)
    return {"voice_scope": "personal", "voice_key": value}


def prepare_payload(payload, username=""):
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
    style = _style_key(body)
    scene_count = 5 if mode == "generate" else len(segments)
    prepared = {
        "pipeline": "pixelle",
        "provider": "pixelle",
        "source_page": "text-video" if body.get("source_page") == "text-video" else "script",
        "text": text,
        "mode": mode,
        "template": template,
        "style": style,
        "speech_rate": _speech_rate(body),
        "n_scenes": scene_count,
        "scenes": [{"line": line} for line in segments],
    }
    prepared.update(_freeze_voice(body, username))
    prepared["talking_material"] = _prepare_talking_material(
        body.get("talking_material"), prepared, username)
    return prepared


def _prepare_talking_material(raw, prepared, username):
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return {"enabled": False}
    if not username:
        raise ValueError("口播视频素材缺少有效用户")

    plan_id = str(raw.get("plan_id") or "").strip()
    source_hash = str(raw.get("source_hash") or "").strip().lower()
    plan = pixelle_talking_assets.get_plan(username, plan_id)
    if source_hash != str(plan.get("source_hash") or ""):
        raise ValueError("分镜方案摘要不匹配")
    if plan.get("status") != "active" or plan.get("job_id") is not None:
        raise ValueError("分镜方案已经用于其他任务")
    source = dict(plan.get("source") or {})
    frozen_voice = (
        ("public", str(source.get("voice_id") or ""))
        if source.get("voice_scope") == "public"
        else ("personal", str(source.get("voice_key") or ""))
    )
    current_voice = (
        ("public", str(prepared.get("voice_id") or ""))
        if prepared.get("voice_scope") == "public"
        else ("personal", str(prepared.get("voice_key") or ""))
    )
    comparable = (
        ("text", prepared.get("text"), source.get("text")),
        ("mode", prepared.get("mode"), source.get("mode")),
        ("template", prepared.get("template"), source.get("template")),
        ("style", prepared.get("style"), source.get("style")),
        ("speech_rate", prepared.get("speech_rate"), source.get("speech_rate")),
        ("voice", current_voice, frozen_voice),
    )
    if any(current != frozen for _name, current, frozen in comparable):
        raise ValueError("提交内容与已确认的分镜方案不一致")

    ratio = _talking_ratio(raw)
    frozen_ratio = _talking_ratio({"ratio": source.get("ratio")})
    if ratio != frozen_ratio:
        raise ValueError("提交内容与已确认的分镜方案不一致")
    raw_scenes = raw.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise ValueError("请明确选择至少一个口播分镜")
    plan_scenes = list(plan.get("scenes") or [])
    by_id = {str(item.get("scene_id") or ""): item for item in plan_scenes}
    selections = {}
    for item in raw_scenes:
        if not isinstance(item, dict) or not isinstance(item.get("enabled"), bool):
            raise ValueError("口播分镜选择无效")
        scene_id = str(item.get("scene_id") or "").strip()
        if scene_id not in by_id or scene_id in selections:
            raise ValueError("口播分镜不属于当前方案")
        if not item["enabled"]:
            selections[scene_id] = None
            continue
        selection = {"scene_id": scene_id, "enabled": True}
        override = str(item.get("avatar_asset_id") or "").strip()
        if override:
            selection["avatar_asset_id"] = override
        selections[scene_id] = selection
    selected = [selections[scene_id] for scene_id in by_id
                if selections.get(scene_id) is not None]
    if not selected:
        raise ValueError("请明确选择至少一个口播分镜")

    default_avatar = str(raw.get("default_avatar_asset_id") or "").strip()
    asset_ids = [default_avatar]
    asset_ids.extend(
        item["avatar_asset_id"] for item in selected
        if item.get("avatar_asset_id")
    )
    unique_asset_ids = list(dict.fromkeys(asset_ids))
    if not default_avatar:
        raise ValueError("请上传默认人物形象图片")
    for asset_id in unique_asset_ids:
        avatar = pixelle_talking_assets.read_avatar(username, asset_id)
        if (not isinstance(avatar, dict)
                or avatar.get("asset_id", asset_id) != asset_id
                or not re.fullmatch(r"image/(?:png|jpeg|webp)",
                                    str(avatar.get("mime") or ""))
                or not re.fullmatch(r"[0-9a-f]{64}",
                                    str(avatar.get("sha256") or ""))
                or not isinstance(avatar.get("data"), bytes)):
            raise LookupError("人物图片不存在或已失效")
    frozen_lines = []
    for item in plan_scenes:
        scene_id = str(item.get("scene_id") or "")
        text = str(item.get("text") or "").strip()
        if not scene_id or not text:
            raise ValueError("分镜方案内容无效")
        frozen_lines.append({"line": text, "scene_id": scene_id})
    prepared.update({
        "text": "\n\n".join(item["line"] for item in frozen_lines),
        "mode": "fixed",
        "n_scenes": len(frozen_lines),
        "scenes": frozen_lines,
        "source_page": ("text-video" if source.get("source_page") == "text-video"
                        else "script"),
    })
    normalized = {
        "enabled": True,
        "plan_id": plan_id,
        "source_hash": source_hash,
        "ratio": frozen_ratio,
        "default_avatar_asset_id": default_avatar,
        "scenes": selected,
    }
    return normalized


def paid_plan_association(payload, username):
    talking = (payload or {}).get("talking_material")
    if not isinstance(talking, dict) or talking.get("enabled") is not True:
        return None
    plan_id = talking["plan_id"]
    source_hash = talking["source_hash"]

    def associate(connection, job_id):
        pixelle_talking_assets.consume_and_bind_paid_plan(
            connection, username, plan_id, source_hash, job_id)

    return associate


def check_plan_rate_limit(username, now=None):
    """Apply a small owner-scoped guard to the unbilled narration planner."""
    owner = str(username or "").strip()
    if not owner:
        raise ValueError("missing user")
    stamp = float(time.monotonic() if now is None else now)
    with _PLAN_RATE_LOCK:
        recent = [item for item in _PLAN_RATE_REQUESTS.get(owner, [])
                  if stamp - item < _PLAN_RATE_WINDOW_SECONDS]
        if len(recent) >= _PLAN_RATE_MAX_REQUESTS:
            raise RuntimeError("planning_rate_limited")
        recent.append(stamp)
        _PLAN_RATE_REQUESTS[owner] = recent


def _talking_ratio(payload):
    raw = (payload or {}).get("ratio", DEFAULT_TALKING_RATIO)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("口播比例必须在 10%-50% 之间")
    ratio = float(raw)
    if (not math.isfinite(ratio)
            or ratio < MIN_TALKING_RATIO
            or ratio > MAX_TALKING_RATIO):
        raise ValueError("口播比例必须在 10%-50% 之间")
    return float(Decimal(str(ratio)).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _recommended_scene_ids(scenes, ratio):
    """Mirror Pixelle's deterministic edge/center/non-adjacent selector."""
    if not scenes:
        return []
    target = max(1, round(len(scenes) * float(ratio)))
    target = min(target, len(scenes))
    center = (len(scenes) - 1) / 2.0
    selected = [0]
    if len(scenes) > 1:
        selected.append(len(scenes) - 1)

    def adjacent_to_selected_interior(index):
        return any(
            abs(index - selected_index) == 1
            for selected_index in selected
            if 0 < selected_index < len(scenes) - 1
        )

    interior = sorted(
        range(1, len(scenes) - 1),
        key=lambda index: (abs(index - center), index),
    )
    for index in interior:
        if len(selected) >= target:
            break
        if adjacent_to_selected_interior(index):
            continue
        selected.append(index)
    if len(selected) < target:
        for index in interior:
            if len(selected) >= target:
                break
            if index not in selected:
                selected.append(index)
    return [scenes[index]["scene_id"] for index in selected[:target]]


def _estimated_scene_duration(text, speech_rate):
    units = len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", str(text or "")))
    seconds = max(0.5, units / (_BASE_NARRATION_CHARS_PER_SECOND * speech_rate))
    return float(Decimal(str(seconds)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP))


def plan_talking_scenes(payload, username):
    """Freeze a non-billed narration plan for later explicit confirmation."""
    ratio = _talking_ratio(payload)
    prepared = prepare_payload(payload, username)
    if prepared["mode"] == "fixed":
        lines = _fixed_segments(prepared["text"])
    else:
        lines = _personal_narrations(prepared)
    if not lines:
        raise ValueError("分镜方案不能为空")

    scenes = []
    last_index = len(lines) - 1
    for index, line in enumerate(lines):
        role = "hook" if index == 0 else ("cta" if index == last_index else "body")
        scenes.append({
            "scene_id": "scene_%02d" % (index + 1),
            "text": line,
            "estimated_duration": _estimated_scene_duration(
                line, prepared["speech_rate"]),
            "role": role,
            "talking_recommended": False,
        })
    recommended = set(_recommended_scene_ids(scenes, ratio))
    for scene in scenes:
        scene["talking_recommended"] = scene["scene_id"] in recommended

    source = {
        "text": prepared["text"],
        "mode": prepared["mode"],
        "ratio": ratio,
        "template": prepared["template"],
        "style": prepared["style"],
        "speech_rate": prepared["speech_rate"],
        "source_page": prepared["source_page"],
        "voice_scope": prepared["voice_scope"],
    }
    if prepared["voice_scope"] == "public":
        source["voice_id"] = prepared["voice_id"]
    else:
        source["voice_key"] = prepared["voice_key"]
    plan = pixelle_talking_assets.create_plan(username, source, scenes)
    return {
        "plan_id": plan["plan_id"],
        "source_hash": plan["source_hash"],
        "scenes": plan["scenes"],
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
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        reason = getattr(exc, "reason", exc)
        raise PixelleTransientError(
            "无法连接视频生成服务：%s" % str(reason)[:160]
        )


def _binary_request(method, path, content, request_id, timeout=60):
    request = urllib.request.Request(
        PIXELLE_API_URL + path,
        data=content,
        headers={
            "Content-Type": "audio/mpeg",
            "X-Request-Id": request_id,
        },
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
            "个人配音上传失败（HTTP %s）：%s" % (exc.code, detail or "未知错误")
        )
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise PixelleTransientError("个人配音上传失败：%s" % str(reason)[:160])


def _asset_request(path, content, content_type, request_id, timeout=60):
    request = urllib.request.Request(
        PIXELLE_API_URL + path,
        data=content,
        headers={"Content-Type": content_type, "X-Request-Id": request_id},
        method="POST",
    )
    try:
        with _NO_PROXY.open(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read() or b"{}")
            detail = str(error.get("detail") or error.get("message") or "")[:160]
        except Exception:
            detail = ""
        if exc.code == 429 or exc.code >= 500:
            raise PixelleTransientError(
                "人物图片上传暂时失败（HTTP %s）" % exc.code) from exc
        if exc.code == 413:
            raise ValueError("人物图片超过视频生成服务限制") from exc
        raise ValueError(
            "人物图片被视频生成服务拒绝（HTTP %s）：%s" %
            (exc.code, detail or "参数无效")) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise PixelleTransientError("人物图片上传暂时失败") from exc


def _upload_avatar_asset(avatar, request_id):
    for attempt in range(3):
        try:
            response = _asset_request(
                "/api/avatar-assets", avatar["data"], avatar["mime"], request_id)
            asset_id = str(response.get("asset_id") or "").strip()
            if not re.fullmatch(r"avatar_[0-9a-f]{32}", asset_id):
                raise RuntimeError("人物图片上传未返回有效资源 ID")
            return asset_id
        except PixelleTransientError:
            if attempt >= 2:
                raise
            time.sleep(0.25 * (attempt + 1))
    raise RuntimeError("人物图片上传失败")


def _load_remote_avatar_map(job_id):
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return {}
    if job_id <= 0:
        return {}
    try:
        with closing(sqlite3.connect(
                pixelle_talking_assets.DB_PATH, timeout=30)) as connection:
            row = connection.execute(
                "SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    try:
        stored = json.loads(row[0] or "{}").get("_pixelle_remote_avatar_assets") or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(stored, dict):
        return {}
    return {
        digest: remote for digest, remote in stored.items()
        if re.fullmatch(r"[0-9a-f]{64}", str(digest or ""))
        and re.fullmatch(r"avatar_[0-9a-f]{32}", str(remote or ""))
    }


def _persist_remote_avatar_map(job_id, mapping):
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return
    valid = {
        str(digest): str(remote) for digest, remote in (mapping or {}).items()
        if re.fullmatch(r"[0-9a-f]{64}", str(digest or ""))
        and re.fullmatch(r"avatar_[0-9a-f]{32}", str(remote or ""))
    }
    if job_id <= 0 or not valid:
        return
    with closing(sqlite3.connect(
            pixelle_talking_assets.DB_PATH, timeout=30)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT payload FROM jobs WHERE id=? AND status IN ('pending','running')",
            (job_id,),
        ).fetchone()
        if not row:
            connection.rollback()
            return
        payload = json.loads(row[0] or "{}")
        existing = payload.get("_pixelle_remote_avatar_assets")
        if not isinstance(existing, dict):
            existing = {}
        existing.update(valid)
        payload["_pixelle_remote_avatar_assets"] = existing
        connection.execute(
            "UPDATE jobs SET payload=?,updated_at=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), int(time.time()), job_id),
        )
        connection.commit()


def _remote_talking_material(payload):
    talking = (payload or {}).get("talking_material")
    if not isinstance(talking, dict) or talking.get("enabled") is not True:
        return {"enabled": False}
    username = str(payload.get("_username") or "").strip()
    job_id = payload.get("_job_id")
    local_ids = [str(talking.get("default_avatar_asset_id") or "")]
    local_ids.extend(
        str(item.get("avatar_asset_id") or "")
        for item in talking.get("scenes") or []
        if isinstance(item, dict) and item.get("avatar_asset_id")
    )
    avatars = {
        asset_id: pixelle_talking_assets.read_avatar(username, asset_id)
        for asset_id in dict.fromkeys(local_ids)
    }
    remote_by_sha = _load_remote_avatar_map(job_id)
    local_to_remote = {}
    for asset_id, avatar in avatars.items():
        digest = str(avatar.get("sha256") or "")
        remote_id = remote_by_sha.get(digest)
        if not remote_id:
            request_id = "text-video-avatar-%s-%s" % (
                str(job_id or "pending")[:32], digest[:24])
            remote_id = _upload_avatar_asset(avatar, request_id)
            remote_by_sha[digest] = remote_id
            _persist_remote_avatar_map(job_id, {digest: remote_id})
        local_to_remote[asset_id] = remote_id

    scenes = []
    for item in talking.get("scenes") or []:
        result = {"scene_id": item["scene_id"], "enabled": True}
        override = str(item.get("avatar_asset_id") or "")
        result["avatar_asset_id"] = local_to_remote.get(override, "")
        scenes.append(result)
    return {
        "enabled": True,
        "ratio": float(talking["ratio"]),
        "default_avatar_asset_id": local_to_remote[local_ids[0]],
        "scenes": scenes,
    }


def availability(force=False):
    enabled = feature_flags.is_enabled(FEATURE_KEY)
    if not enabled:
        return {"enabled": False, "ready": False, "available": False}
    now = time.monotonic()
    if now < _CAPACITY_BLOCKED_UNTIL:
        return {"enabled": True, "ready": False, "available": False}
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


def _personal_narrations(payload):
    if payload["mode"] == "fixed":
        narrations = [
            str(scene.get("line") or "").strip()
            for scene in payload.get("scenes") or []
        ]
    else:
        response = _json_request("POST", "/api/content/narration", {
            "text": _prepared_text(payload["text"], payload["mode"]),
            "n_scenes": payload["n_scenes"],
            "min_words": 5,
            "max_words": 20,
        })
        narrations = response.get("narrations") or []
    cleaned = [str(item or "").strip() for item in narrations if str(item or "").strip()]
    if not cleaned or len(cleaned) > 20 or any(len(item) > 1000 for item in cleaned):
        raise RuntimeError("旁白分段生成失败")
    return cleaned


def _display_units(text):
    return sum(1 if ord(char) < 128 else 2 for char in text)


def _split_after_boundaries(text, boundaries):
    parts = []
    start = 0
    for index, char in enumerate(text):
        if char in boundaries:
            parts.append(text[start:index + 1])
            start = index + 1
    if start < len(text):
        parts.append(text[start:])
    return [part for part in parts if part]


def _hard_split_caption(text, max_units):
    parts = []
    current = []
    current_units = 0
    for char in text:
        char_units = _display_units(char)
        if current and current_units + char_units > max_units:
            parts.append("".join(current))
            current = []
            current_units = 0
        if char_units > max_units:
            raise ValueError("字幕宽度限制过小")
        current.append(char)
        current_units += char_units
    if current:
        parts.append("".join(current))

    for index in range(len(parts) - 1, 0, -1):
        pair = parts[index - 1] + parts[index]
        candidates = []
        for offset in range(1, len(pair)):
            left = pair[:offset]
            right = pair[offset:]
            left_units = _display_units(left)
            right_units = _display_units(right)
            if left_units <= max_units and right_units <= max_units:
                candidates.append((abs(left_units - right_units), offset, left, right))
        if candidates:
            _, _, parts[index - 1], parts[index] = min(candidates)
    return parts


def _pack_caption_fragments(fragments, max_units):
    packed = []
    current = ""
    for fragment in fragments:
        if current and _display_units(current + fragment) > max_units:
            packed.append(current)
            current = ""
        current += fragment
    if current:
        packed.append(current)
    return packed


def _split_caption_clause(text, max_units):
    if _display_units(text) <= max_units:
        return [text]
    clauses = _split_after_boundaries(text, _CAPTION_CLAUSE_BOUNDARIES)
    if len(clauses) == 1:
        return _hard_split_caption(text, max_units)
    bounded = []
    for clause in clauses:
        if _display_units(clause) <= max_units:
            bounded.append(clause)
        else:
            bounded.extend(_hard_split_caption(clause, max_units))

    return _pack_caption_fragments(bounded, max_units)


def _split_caption_text(text, max_units=_MAX_CAPTION_UNITS):
    if not isinstance(text, str) or not text:
        raise ValueError("字幕文本不能为空")
    if not isinstance(max_units, int) or max_units < 2:
        raise ValueError("字幕宽度限制无效")
    if _display_units(text) <= max_units:
        return [text]

    result = []
    for sentence in _split_after_boundaries(text, _CAPTION_SENTENCE_BOUNDARIES):
        result.extend(_split_caption_clause(sentence, max_units))
    result = _pack_caption_fragments(result, max_units)
    if "".join(result) != text:
        raise ValueError("字幕拆分改变了原文")
    if any(_display_units(part) > max_units for part in result):
        raise ValueError("字幕拆分后仍然过长")
    if len(result) > _MAX_CAPTION_CUES:
        raise ValueError("单个分镜字幕片段过多")
    return result


def _spoken_caption_units(text):
    return [char for char in str(text or "") if char.isalnum()]


def _caption_cues_from_word_timestamps(text, cue_texts, words, duration):
    """Map display-only cue boundaries onto real ASR word timing."""
    total_duration = float(duration)
    if total_duration <= 0 or not cue_texts:
        raise ValueError("字幕对齐音频时长无效")
    if "".join(cue_texts) != text:
        raise ValueError("字幕对齐文本与原文不一致")

    timed_units = []
    for word in words or []:
        value = str(word.get("text") or "").strip()
        start = word.get("start")
        end = word.get("end")
        if (
            not value
            or not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or end <= start
        ):
            continue
        units = _spoken_caption_units(value)
        if not units:
            continue
        span = float(end) - float(start)
        for index, _unit in enumerate(units):
            timed_units.append({
                "start": float(start) + span * index / len(units),
                "end": float(start) + span * (index + 1) / len(units),
            })

    known_count = len(_spoken_caption_units(text))
    if known_count <= 0 or len(timed_units) < len(cue_texts):
        raise ValueError("字幕对齐识别结果不足")

    boundaries = [0.0]
    consumed = 0
    for cue_text in cue_texts[:-1]:
        consumed += len(_spoken_caption_units(cue_text))
        token_index = round(consumed * len(timed_units) / known_count)
        token_index = max(1, min(len(timed_units) - 1, token_index))
        boundary = max(0.0, min(total_duration, timed_units[token_index]["start"]))
        if boundary <= boundaries[-1] + 0.04:
            raise ValueError("字幕对齐时间轴没有递增")
        boundaries.append(boundary)
    boundaries.append(total_duration)

    if any(end <= start for start, end in zip(boundaries, boundaries[1:])):
        raise ValueError("字幕对齐时间轴无效")
    return [
        {
            "text": cue_text,
            "start_time": round(boundaries[index], 3),
            "end_time": round(boundaries[index + 1], 3),
        }
        for index, cue_text in enumerate(cue_texts)
    ]


def _aligned_caption_cues(text, audio_content):
    """Align one continuous TTS result; fall back to display-only cues on ASR failure."""
    cue_texts = _split_caption_text(text)
    if len(cue_texts) == 1:
        return [{"text": cue_texts[0]}]

    token = uuid.uuid4().hex
    audio_path = OUT_DIR / ("caption-align-%s.mp3" % token)
    try:
        audio_path.write_bytes(audio_content)
        os.chmod(audio_path, 0o600)
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        duration = float(probe.stdout.strip())
        if duration <= 0:
            raise ValueError("字幕对齐音频时长无效")

        from . import video as video_domain
        with video_domain._whisper_sem:
            model = video_domain._get_whisper_model()
            segments, _ = model.transcribe(
                str(audio_path),
                language="zh",
                vad_filter=True,
                word_timestamps=True,
            )
            words = []
            for segment in segments:
                for word in getattr(segment, "words", None) or []:
                    words.append({
                        "text": str(getattr(word, "word", "") or ""),
                        "start": getattr(word, "start", None),
                        "end": getattr(word, "end", None),
                    })
        return _caption_cues_from_word_timestamps(
            text, cue_texts, words, duration
        )
    except Exception:
        return [{"text": cue_text} for cue_text in cue_texts]
    finally:
        try:
            audio_path.unlink(missing_ok=True)
        except OSError:
            pass


def _personal_narration_segments(payload):
    username = str(payload.get("_username") or "").strip()
    voice_key = str(payload.get("voice_key") or "").strip()
    if not username or not voice_key:
        raise ValueError("个人音色任务缺少用户或音色信息")
    segments = []
    for scene_index, text in enumerate(_personal_narrations(payload)):
        audio = audio_domain.synthesize_owned_voice_segment(
            username,
            voice_key,
            text,
            speed=payload.get("speech_rate", 1.0),
        )
        request_id = "text-video-%s-%d" % (
            payload.get("_job_id") or "pending",
            scene_index,
        )
        uploaded = _binary_request(
            "POST", "/api/audio-assets", audio["content"], request_id
        )
        asset_id = str(uploaded.get("asset_id") or "").strip()
        if not re.fullmatch(r"audio_[0-9a-f]{32}", asset_id):
            raise RuntimeError("个人配音上传未返回有效资源 ID")
        segments.append({
            "text": text,
            "audio_asset_id": asset_id,
            "caption_cues": _aligned_caption_cues(text, audio["content"]),
        })
    return segments


def _submit(payload):
    speech_rate = _speech_rate(payload)
    payload = dict(payload or {})
    payload["speech_rate"] = speech_rate
    template = TEMPLATES_BY_KEY[payload["template"]]
    style = STYLE_PRESETS_BY_KEY[_style_key(payload)]
    media_workflow = (
        PIXELLE_VIDEO_WORKFLOW
        if template["kind"] == "video"
        else PIXELLE_MEDIA_WORKFLOW
    )
    body = {
        "text": _prepared_text(payload["text"], payload["mode"]),
        "mode": payload["mode"],
        "n_scenes": payload["n_scenes"],
        "frame_template": payload["template"],
        "prompt_prefix": style["prompt_prefix"],
        "media_workflow": media_workflow,
        "tts_workflow": PIXELLE_TTS_WORKFLOW,
        "tts_speed": speech_rate,
        "video_fps": 30,
        "bgm_volume": 0.18,
    }
    remote_talking = _remote_talking_material(payload)
    if remote_talking.get("enabled"):
        body["talking_material"] = remote_talking
    if payload.get("voice_scope") == "personal":
        narration_segments = _personal_narration_segments(payload)
        body.update({
            "text": "\n\n".join(item["text"] for item in narration_segments),
            "mode": "fixed",
            "n_scenes": len(narration_segments),
            "narration_segments": narration_segments,
        })
        body.pop("tts_speed", None)
        body.pop("tts_workflow", None)
    else:
        body["voice_id"] = str(payload.get("voice_id") or DEFAULT_PUBLIC_VOICE)
    response = _json_request("POST", "/api/video/generate/async", body)
    task_id = str(response.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("视频生成服务没有返回任务 ID")
    return task_id


def _wait(task_id):
    global _CAPACITY_BLOCKED_UNTIL
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
            error = str(task.get("error") or "视频生成失败")[:300]
            if "TASK_QUEUE_MAXED" in error:
                _CAPACITY_BLOCKED_UNTIL = time.monotonic() + 60
            raise RuntimeError(error)
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


def _talking_warnings(result):
    candidates = []
    direct = result.get("talking_warnings")
    if isinstance(direct, list):
        candidates.extend(direct)
    frames = result.get("frames")
    if isinstance(frames, list):
        candidates.extend(
            {"scene_id": frame.get("scene_id"), "message": frame.get("talking_warning")}
            for frame in frames
            if isinstance(frame, dict) and frame.get("talking_warning")
        )

    warnings = []
    seen = set()
    for candidate in candidates:
        if isinstance(candidate, dict):
            scene_id = str(candidate.get("scene_id") or "scene").strip()
            message = candidate.get("message") or candidate.get("detail") \
                or candidate.get("reason") or candidate.get("warning")
        else:
            scene_id = "scene"
            message = candidate
        message = re.sub(r"[\x00-\x1f\x7f]+", " ", str(message or ""))
        message = " ".join(message.split())[:220]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", scene_id):
            scene_id = "scene"
        warning_key = (scene_id, message)
        if not message or warning_key in seen:
            continue
        warnings.append({"scene_id": scene_id, "message": message})
        seen.add(warning_key)
        if len(warnings) >= 20:
            break
    return warnings


def generate(payload):
    style = _style_key(payload)
    task_id = _submit(payload)
    result = _wait(task_id)
    source_url = _safe_upstream_video_url(result.get("video_url"))
    video_file, file_size = _download_video(source_url, payload.get("_job_id"))
    response = {
        "type": "script_to_video",
        "pipeline": "pixelle",
        "provider_task_id": task_id,
        "provider_video_id": task_id,
        "status": "done",
        "mode": payload["mode"],
        "video_file": video_file,
        "video_url": public_url(video_file, "video/mp4", private=True),
        "duration": round(float(result.get("duration") or 0), 3),
        "scene_count": int(result.get("scene_count") or payload.get("n_scenes") or 1),
        "template": payload["template"],
        "style": style,
        "input_mode": payload["mode"],
        "file_size": int(result.get("file_size") or file_size),
    }
    warnings = _talking_warnings(result)
    if warnings:
        response["talking_warnings"] = warnings
    return response
