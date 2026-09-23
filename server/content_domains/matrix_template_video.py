"""Production-site bridge to the isolated matrix template generation service."""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing

from .core import OUT_DIR, public_url
from . import feature_flags, matrix_template_semantics, matrix_text_controls, pricing


FEATURE_KEY = "matrix_template_video"
# 目录完整性**看组成，不看总数**。
#
# 原来只有一个写死的数字白名单 TRANSITION_TEMPLATE_COUNTS = {2,15,19,20,22,24}，
# 而且在三处各写了一份（这里 / 中转器 relay.py 的 /health / 代码注释），三份还不一致
# ——中转器那份漏了 24，模板数真到 24 时它会返回 503，而主站认为 24 合法，**造成假性故障**。
# 每加/减一个模板都得同步改这几处，漏一处整条渠道就被误判成「未就绪」，
# 用户收到「生成渠道正在繁忙或维护」。2026-09-11 磊哥下线两个老模板（22→20）时就差点踩到。
#
# 现在总数由**组成**推导：17 个 ref + 1 个九宫格 + 2 个 fixed-skill 是骨架，
# 老模板可有可无。以后加减模板不用再改数字。
TRANSITION_TEMPLATE_COUNTS = frozenset({2, 15, 19, 20, 22, 24})  # 历史过渡态，保留兼容
LEGACY_TEMPLATE_IDS = ("full-overlay-bold", "poster-split")
REFERENCE_TEMPLATE_RE = re.compile(r"ref-[0-9]{2}-[a-z0-9-]{1,48}\Z")
REFERENCE_TEMPLATE_COUNT = 17
NINE_GRID_TEMPLATE_ID = "nine-grid-reveal"
NINE_GRID_VARIANT = "nine-grid"
TRIPLE_STRIP_TEMPLATE_ID = "triple-strip-shutter"
TRIPLE_STRIP_VARIANT = "triple-strip"
YELLOW_BANNER_TEMPLATE_ID = "yellow-banner-zoom"
YELLOW_BANNER_VARIANT = "yellow-banner"
FAN_WHIP_TEMPLATE_ID = "fan-whip-static"
FAN_WHIP_VARIANT = "fan-whip"
BRUSH_PANEL_TEMPLATE_ID = "brush-panel-transitions"
BRUSH_PANEL_VARIANT = "brush-panel"
INSET_FLIP_TEMPLATE_ID = "inset-flip-whip"
FIXED_OPENING_TEMPLATE_ID = "fixed-opening-whip"
BILINGUAL_TEMPLATE_ID = "bilingual-stagger-salon"
MOTION_V3_TEMPLATE_IDS = (INSET_FLIP_TEMPLATE_ID, FIXED_OPENING_TEMPLATE_ID)
FIXED_SKILL_TEMPLATE_IDS = (
    TRIPLE_STRIP_TEMPLATE_ID, YELLOW_BANNER_TEMPLATE_ID,
    FAN_WHIP_TEMPLATE_ID, BRUSH_PANEL_TEMPLATE_ID,
)
# 完整目录的合法总数由**组成**推导，不再手写：骨架 + 0~2 个老模板（老模板开关在上游
# legacy_templates_enabled）。加了新模板这里自动跟上，不需要改代码。
_SKELETON_COUNT = REFERENCE_TEMPLATE_COUNT + 1 + len(FIXED_SKILL_TEMPLATE_IDS)
COMPLETE_TEMPLATE_COUNTS = frozenset(
    range(_SKELETON_COUNT, _SKELETON_COUNT + len(LEGACY_TEMPLATE_IDS) + 1)
)


def _healthy_template_count(count) -> bool:
    """The generation service owns catalog composition and size."""
    try:
        count = int(count or 0)
    except (TypeError, ValueError):
        return False
    return 1 <= count <= 1_000


def _catalog_is_complete(templates) -> bool:
    return bool(list(templates or []))


FIXED_SKILL_TEMPLATE_CONTRACTS = {
    INSET_FLIP_TEMPLATE_ID: {"variant":"inset-flip", "duration":443/30, "required_visuals":7},
    FIXED_OPENING_TEMPLATE_ID: {"variant":"fixed-opening", "duration":17.3, "required_visuals":4},
    TRIPLE_STRIP_TEMPLATE_ID: {
        "variant": TRIPLE_STRIP_VARIANT,
        "duration": 17.6,
        "required_visuals": 8,
    },
    YELLOW_BANNER_TEMPLATE_ID: {
        "variant": YELLOW_BANNER_VARIANT,
        "duration": 302 / 30,
        "required_visuals": 3,
    },
    FAN_WHIP_TEMPLATE_ID: {
        "variant": FAN_WHIP_VARIANT,
        "duration": 377 / 30,
        "required_visuals": 5,
    },
    BRUSH_PANEL_TEMPLATE_ID: {
        "variant": BRUSH_PANEL_VARIANT,
        "duration": 15.133333,
        "required_visuals": 7,
    },
}
API_URL = os.environ.get("MATRIX_TEMPLATE_API_URL", "http://127.0.0.1:8112").rstrip("/")
API_TOKEN = os.environ.get("MATRIX_TEMPLATE_API_TOKEN", "").strip()
JOB_TIMEOUT = max(60, min(1800, int(os.environ.get("MATRIX_TEMPLATE_JOB_TIMEOUT", "1200"))))
TOTAL_TIMEOUT = max(300, min(1800, int(os.environ.get(
    "MATRIX_TEMPLATE_TOTAL_TIMEOUT", "1200"
))))
POLL_INTERVAL = max(1, min(10, int(os.environ.get("MATRIX_TEMPLATE_POLL_INTERVAL", "3"))))
MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_VOICEOVER_BYTES = 32 * 1024 * 1024
MAX_VOICEOVER_SECONDS = 600.0
MAX_VOICEOVER_TEXT_LENGTH = 120
DEFAULT_VOICEOVER_BGM_VOLUME = 0.2
VOICEOVER_CACHE_RETENTION_SECONDS = 24 * 60 * 60
VOICEOVER_MUX_TIMEOUT = 180
MATERIAL_POLICY_SHARED = "shared"
MATERIAL_POLICY_OWNED_PUBLIC = "owned_public"
# 素材范围（2026-09-17 老板定调）：一次性邀请码注册的账号只能用公网素材，
# 绝不使用公司素材（飞书群聊导入等）。其余账号候选载荷与历史逐字节一致。
MATERIAL_SCOPE_PUBLIC_ONLY = "public_only"
# 模板参数微调（2026-09-21，合同 v1）：只有渲染侧声明 tunable 的模板（首发
# ref-05-changsha-white-red）可调，其余模板 tunable=false 且收到 overrides 一律
# 明确拒绝（绝不静默忽略）。字段名/范围/默认值的单一起源是渲染侧 overrides_schema；
# 这里的常量只是主站的同值校验与规范化。
OVERRIDES_CONTRACT_VERSION = 1
OVERRIDE_FIELDS = (
    "title_scale", "title_offset_y", "cta_scale", "cta_offset_y",
    "accent_color", "media_focus",
)
OVERRIDE_DEFAULTS = {
    "title_scale": 1.0, "title_offset_y": 0, "cta_scale": 1.0, "cta_offset_y": 0,
}
OVERRIDE_SCALE_MIN = 0.85
OVERRIDE_SCALE_MAX = 1.10
OVERRIDE_OFFSET_MIN = -60
OVERRIDE_OFFSET_MAX = 60
ACCENT_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}\Z")
MEDIA_FOCUS_SLOT_LIMIT = 21
_OVERRIDE_LABELS = {
    "title_scale": "标题缩放", "title_offset_y": "标题上下位置",
    "cta_scale": "行动文案缩放", "cta_offset_y": "行动文案上下位置",
    "accent_color": "强调色", "media_focus": "画面焦点",
}
# 预览（matrix-template-preview）：只读对比，不登记正式作品、不扣点、不发完成通知。
# 走既有任务凭据 + task 查询，预览文件一律经主站代理回放，绝不裸暴露渲染侧地址。
PREVIEW_KIND = "matrix_template_preview"
# 预览只接受单条生成字段（合同 §3.2）：批量/口播/时长一律明确拒绝，绝不静默忽略。
PREVIEW_FIELDS = frozenset({
    "top_text", "bottom_text", "template_id", "font_family", "user_materials",
    "bgm", "template_revision", "overrides",
})
_PREVIEW_UNSUPPORTED_LABELS = {
    "voiceover": "口播配音（预览只对比画面与文字排版）",
    "bgm_volume": "背景音乐音量",
    "duration": "时长（由渲染端按素材自动定稿）",
    "batch_id": "批量任务参数",
    "batch_index": "批量任务参数",
    "batch_size": "批量任务参数",
    "mode": "其他生成模式",
    "preview_id": "预览标识",
}
PREVIEW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
PREVIEW_TIMEOUT = max(60, min(1800, int(os.environ.get(
    "MATRIX_TEMPLATE_PREVIEW_TIMEOUT", "900"
))))
PREVIEW_DEFAULT_TTL_SECONDS = 30 * 60
PREVIEW_FRAME_LIMIT = 24
PREVIEW_MAX_ACTIVE_PER_USER = max(1, min(5, int(os.environ.get(
    "MATRIX_TEMPLATE_PREVIEW_MAX_ACTIVE", "2"
))))
_USER_MATERIAL_TYPES = {"image", "video"}
_USER_MATERIAL_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_CACHE = {
    "at": 0.0,
    "templates": [],
    "fonts": [],
    "controls": {},
    "max_batch_size": 1,
    "engine_concurrency": {"ffmpeg": 1, "hyperframes": 1},
}
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_VOICEOVER_CACHE_LOCKS = tuple(threading.Lock() for _ in range(32))
_VOICEOVER_SYNTH_LOCK = threading.Lock()


class MatrixTemplateHTTPError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = int(status)


class MatrixTemplateProviderFailed(RuntimeError):
    """The provider reached an authoritative failed terminal state."""


def _validated_base():
    parsed = urllib.parse.urlsplit(API_URL)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme not in ({"http", "https"} if loopback else {"https"})
        or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment
    ):
        raise RuntimeError("模板成片服务地址配置无效")
    return parsed


def _request(method, path, body=None, *, request_id="", timeout=30):
    if not API_TOKEN:
        raise RuntimeError("模板成片服务凭证未配置")
    _validated_base()
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Authorization": "Bearer " + API_TOKEN}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if request_id:
        headers["X-Request-Id"] = request_id
    request = urllib.request.Request(API_URL + path, data=data, headers=headers, method=method)
    try:
        with _NO_PROXY.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            value = json.loads(exc.read())
            detail = value.get("detail") or value.get("error")
        except Exception:
            detail = None
        raise MatrixTemplateHTTPError(
            exc.code, str(detail or "模板成片生成服务请求失败")
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("模板成片生成服务连接失败") from exc


def availability(force=False):
    enabled = feature_flags.is_enabled(FEATURE_KEY)
    if not enabled:
        return {"enabled": False, "ready": False, "available": False}
    try:
        health = _request("GET", "/health", timeout=5)
        ready = (
            health.get("ok") is True
            and _healthy_template_count(health.get("templates"))
        )
    except Exception:
        ready = False
    return {"enabled": True, "ready": ready, "available": ready}


def require_available():
    feature_flags.require_enabled(FEATURE_KEY)
    if not availability().get("ready"):
        raise feature_flags.FeatureDisabled("模板成片服务暂不可用，请稍后重试")


_SEMANTIC_CONTRACTS = {
    "v01": {
        "top1": (70, 400, 996, 2), "top2": (64, 400, 996, 2),
        "top3": (52, 900, 996, 2), "bottom2": (74, 400, 848, 2),
    },
    "v02": {
        "top1": (86, 400, 996, 2), "top2": (62, 400, 996, 4),
        "bottom2": (78, 400, 996, 2),
    },
    "v03": {
        "top1": (86, 900, 996, 2), "top2": (62, 400, 996, 4),
        "bottom2": (78, 900, 996, 2),
    },
    "v04": {
        "top1": (88, 900, 996, 2), "top2": (72, 900, 996, 2),
        "top3": (48, 900, 948, 2), "bottom2": (80, 900, 996, 2),
    },
    "v05": {
        "top1": (102, 900, 996, 2), "top2": (104, 900, 996, 2),
        "top3": (68, 900, 996, 2), "bottom2": (70, 900, 862, 2),
    },
    "v06": {
        "top1": (104, 900, 996, 2), "top2": (76, 900, 996, 2),
        "top3": (60, 900, 996, 2), "bottom2": (76, 900, 924, 2),
    },
    "v07": {
        "top1": (118, 900, 996, 2), "top2": (82, 900, 996, 2),
        "top3": (51, 900, 996, 2), "bottom1": (57, 900, 996, 2),
        "bottom2": (86, 900, 996, 2),
    },
    "v08": {
        "top1": (92, 900, 996, 2), "top2": (62, 900, 996, 2),
        "top3": (54, 900, 996, 2), "bottom2": (64, 400, 948, 2),
    },
    "v09": {
        "top1": (88, 400, 996, 2), "top2": (50, 700, 996, 4),
        "bottom2": (66, 400, 996, 2),
    },
    "v10": {
        "top1": (85, 400, 996, 2), "top2": (78, 400, 996, 2),
        "top3": (65, 800, 996, 2), "bottom2": (80, 400, 970, 2),
    },
    "v11": {
        "top1": (86, 900, 996, 2), "top2": (80, 800, 996, 2),
        "top3": (54, 800, 996, 2), "bottom2": (76, 400, 996, 2),
    },
    "v12": {
        "top1": (80, 400, 996, 2), "top2": (62, 400, 996, 2),
        "top3": (70, 400, 996, 2), "bottom2": (62, 400, 996, 2),
    },
    "v13": {
        "top1": (76, 900, 996, 2), "top2": (68, 900, 996, 4),
        "bottom2": (80, 900, 996, 2),
    },
    "v14": {
        "top1": (72, 400, 996, 2), "top2": (50, 800, 996, 4),
        "bottom2": (64, 400, 996, 2),
    },
    "v15": {
        "top1": (80, 900, 996, 2), "top2": (64, 900, 996, 4),
        "bottom2": (92, 900, 996, 2),
    },
    "v16": {
        "top1": (80, 400, 996, 2), "top2": (68, 400, 996, 2),
        "top3": (52, 900, 996, 2), "bottom2": (70, 400, 996, 2),
    },
    "v17": {
        "top1": (74, 900, 996, 2), "top2": (64, 900, 996, 2),
        "top3": (118, 900, 996, 2), "bottom2": (84, 900, 996, 2),
    },
    NINE_GRID_VARIANT: {
        "top1": (82, 900, 800, 4), "top2": (82, 900, 800, 4),
        "bottom2": (58, 900, 930, 4),
    },
    TRIPLE_STRIP_VARIANT: {
        "top1": (64, 900, 732, 2), "top2": (38, 900, 738, 3),
        "bottom2": (26, 750, 620, 4),
    },
    YELLOW_BANNER_VARIANT: {
        "top1": (50, 900, 804, 2), "top2": (38, 900, 900, 2),
        "top3": (38, 900, 900, 2), "bottom2": (32, 750, 787, 4),
    },
    FAN_WHIP_VARIANT: {
        "top1": (54, 900, 996, 2), "top2": (46, 900, 996, 2),
        "top3": (38, 900, 996, 2), "bottom2": (42, 900, 996, 4),
    },
    BRUSH_PANEL_VARIANT: {
        "top1": (54, 900, 996, 2), "top2": (46, 900, 996, 2),
        "top3": (38, 900, 996, 2), "bottom2": (42, 900, 996, 4),
    },
}
_SEMANTIC_CONTRACT_TRANSITIONS = {
    "v07": ({
        "top1": (104, 900, 996, 2), "top2": (68, 900, 996, 2),
        "top3": (62, 900, 996, 2), "bottom2": (84, 900, 996, 2),
    }, _SEMANTIC_CONTRACTS["v07"]),
}
_ALL_REFERENCE_VARIANTS = {
    f"v{index:02d}" for index in range(1, 18)
}
_SEMANTIC_LAYER_TRANSITIONS = {
    ("v01", "bottom2"): {
        (74, 400, 848, 2),
        (74, 400, 944, 2),
    },
    ("v04", "bottom2"): {
        (60, 900, 996, 2),
        (80, 900, 996, 2),
    },
    ("v06", "top1"): {
        (104, 900, 996, 2),
        (86, 900, 996, 2),
    },
    ("v08", "top1"): {
        (92, 900, 996, 2),
        (86, 900, 996, 2),
    },
    ("v09", "top1"): {
        (78, 400, 996, 2),
        (88, 400, 996, 2),
    },
    ("v10", "top1"): {
        (70, 400, 996, 2),
        (85, 400, 996, 2),
    },
    ("v10", "top3"): {
        (54, 800, 996, 2),
        (65, 800, 996, 2),
    },
    ("v12", "top1"): {
        (72, 400, 996, 2),
        (80, 400, 996, 2),
    },
    ("v12", "top3"): {
        (50, 400, 996, 2),
        (70, 400, 996, 2),
    },
    ("v15", "bottom2"): {
        (92, 900, 996, 2),
        (82, 900, 996, 2),
    },
    ("v16", "top1"): {
        (48, 400, 996, 2),
        (80, 400, 996, 2),
    },
}


def _tunable_controls(raw):
    """Parse the renderer-owned tunable contract; fail closed, never fabricate."""
    revision = str(raw.get("template_revision") or "").strip().lower()
    schema = raw.get("overrides_schema")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", revision)
        or not isinstance(schema, dict)
        or not schema
    ):
        return None
    return {
        "tunable": True,
        "template_revision": revision,
        "overrides_schema": json.loads(json.dumps(schema, ensure_ascii=False)),
    }


def _override_schema_items(schema):
    """Yield (field, definition) for the two compact schema shapes we accept."""
    properties = schema.get("properties")
    source = properties if isinstance(properties, dict) else schema
    for key, item in source.items():
        if key in OVERRIDE_FIELDS and isinstance(item, dict):
            yield key, item


def _override_schema_defaults(schema):
    """Echo only the defaults the renderer explicitly declares."""
    return {
        key: json.loads(json.dumps(item["default"], ensure_ascii=False))
        for key, item in _override_schema_items(schema) if "default" in item
    }


def _override_schema_slots(schema):
    """Echo the declared media_focus slot bounds/说明 without inventing numbers."""
    for key, item in _override_schema_items(schema):
        if key != "media_focus":
            continue
        slots = {}
        for field in ("maxItems", "max_slots", "slots", "slot_max"):
            value = item.get(field)
            if (
                not isinstance(value, bool) and isinstance(value, int)
                and 0 < value <= MEDIA_FOCUS_SLOT_LIMIT
            ):
                slots["max"] = value
                break
        note = str(item.get("note") or item.get("description") or "").strip()
        if note:
            slots["note"] = note[:200]
        return slots
    return {}


def _semantic_contract(value, variant):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "version", "max_width_px", "layers",
    }:
        raise RuntimeError("HyperFrames 语义排版能力无效")
    layers = value.get("layers")
    expected_max_width = value.get("max_width_px")
    allowed_layers = {"top1", "top2", "top3", "bottom1", "bottom2"}
    if (
        value.get("version") != 1
        or isinstance(expected_max_width, bool)
        or not isinstance(expected_max_width, int)
        or not 64 <= expected_max_width <= 4096
        or not isinstance(layers, dict)
        or not {"top1", "top2", "bottom2"}.issubset(layers)
        or not set(layers).issubset(allowed_layers)
    ):
        raise RuntimeError("HyperFrames 语义排版能力无效")
    normalized = {}
    for layer, item in layers.items():
        if not isinstance(item, dict) or set(item) != {
            "font_size_px", "font_weight", "max_width_px", "max_lines",
        }:
            raise RuntimeError("HyperFrames 语义排版能力无效")
        font_size = item.get("font_size_px")
        font_weight = item.get("font_weight")
        max_width = item.get("max_width_px")
        max_lines = item.get("max_lines")
        if (
            any(isinstance(item, bool) for item in (
                font_size, font_weight, max_width, max_lines,
            ))
            or not all(isinstance(item, int) for item in (
                font_size, font_weight, max_width, max_lines,
            ))
            or not 8 <= font_size <= 512
            or not 100 <= font_weight <= 1000
            or not 16 <= max_width <= 4096
            or max_width > expected_max_width
            or not 1 <= max_lines <= 12
        ):
            raise RuntimeError("HyperFrames 语义排版能力无效")
        normalized[layer] = dict(item)
    return {
        "version": 1,
        "max_width_px": expected_max_width,
        "layers": normalized,
    }


def _refresh_catalog(force=False):
    now = time.monotonic()
    if force or now - _CACHE["at"] > 30:
        response = _request("GET", "/v1/templates", timeout=10)
        try:
            max_batch_size = int(response.get("max_batch_size") or 1)
            raw_engine_concurrency = response.get("engine_concurrency") or {}
            if not isinstance(raw_engine_concurrency, dict):
                raise TypeError("engine concurrency must be an object")
            engine_concurrency = {
                "ffmpeg": int(
                    raw_engine_concurrency.get("ffmpeg") or max_batch_size
                ),
                "hyperframes": int(
                    raw_engine_concurrency.get("hyperframes")
                    or response.get("hyperframes_concurrency") or 1
                ),
            }
        except (TypeError, ValueError) as exc:
            raise RuntimeError("模板批量能力无效") from exc
        if (
            not 1 <= max_batch_size <= 5
            or any(
                not 1 <= value <= max_batch_size
                for value in engine_concurrency.values()
            )
        ):
            raise RuntimeError("模板批量能力无效")
        raw_templates = response.get("templates")
        if not isinstance(raw_templates, list):
            raise RuntimeError("模板目录无效")
        templates = []
        seen_template_ids = set()
        tunable_controls = {}
        text_controls = {}
        for raw in raw_templates:
            if not isinstance(raw, dict):
                continue
            template_id = str(raw.get("id") or "")
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", template_id)
                or template_id in seen_template_ids
            ):
                continue
            engine = str(raw.get("engine") or "ffmpeg")
            font_selectable = raw.get("font_selectable") is not False
            font_mode = str(raw.get("font_mode") or (
                "selectable" if font_selectable else "template_locked"
            ))
            variant = str(raw.get("variant") or "")
            try:
                semantic_layout = _semantic_contract(
                    raw.get("semantic_layout"), variant,
                )
            except RuntimeError:
                continue
            if engine not in {"ffmpeg", "hyperframes"}:
                continue
            if font_mode not in {"selectable", "template_locked"}:
                continue
            if engine == "hyperframes" and (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", variant)
                or semantic_layout is None
            ):
                continue
            template = {
                "id": template_id,
                "name": str(raw.get("name") or template_id)[:40],
                "description": str(raw.get("description") or "")[:160],
                "tags": [str(item)[:20] for item in (raw.get("tags") or [])[:8]],
                "engine": engine,
                "font_mode": font_mode,
                "font_selectable": font_selectable,
                "variant": variant,
            }
            if semantic_layout is not None:
                template["semantic_layout"] = semantic_layout
            for key in (
                "duration_mode", "required_visuals",
                "required_visuals_max", "bgm_mode", "bgm_optional",
                "fixed_duration_seconds",
                "requires_voiceover", "copy_mode", "narration_contract_version",
            ):
                if key in raw:
                    template[key] = raw[key]
            # 画面位窗口（渲染侧每个画面位实际播放的源片长度范围，秒）。调用方
            # （Agent 运行时 / 工作台）据此挑够长的本人镜头——4.608 秒的镜头配
            # health-team-hook 的 4.867 秒画面位会被渲染端拒（2026-09-23 实锤），
            # 窗口不暴露出去，调用方只能试错。非法值一概不放进公开目录。
            clip_range = raw.get("clip_duration_range_seconds")
            if isinstance(clip_range, (list, tuple)) and len(clip_range) == 2:
                try:
                    clip_low, clip_high = (
                        float(clip_range[0]), float(clip_range[1]),
                    )
                except (TypeError, ValueError):
                    clip_low = clip_high = None
                if (
                    clip_low is not None and clip_high is not None
                    and not isinstance(clip_range[0], bool)
                    and not isinstance(clip_range[1], bool)
                    and math.isfinite(clip_low) and math.isfinite(clip_high)
                    and 0 < clip_low <= clip_high <= 600
                ):
                    template["clip_duration_range_seconds"] = [
                        clip_low, clip_high,
                    ]
            duration_mode = template.get("duration_mode")
            required_visuals = template.get("required_visuals")
            required_visuals_max = template.get("required_visuals_max")
            fixed_duration = template.get("fixed_duration_seconds")
            if (
                duration_mode not in {
                    None, "fixed", "fixed_12", "narration",
                    "random_integer_7_15", "random_integer_8_15",
                }
                or (
                    required_visuals is not None
                    and (
                        isinstance(required_visuals, bool)
                        or not isinstance(required_visuals, int)
                        or not 1 <= required_visuals <= 21
                    )
                )
                or (
                    required_visuals_max is not None
                    and (
                        isinstance(required_visuals_max, bool)
                        or not isinstance(required_visuals_max, int)
                        or not 1 <= required_visuals_max <= (22 if template_id == BILINGUAL_TEMPLATE_ID else 21)
                        or (
                            isinstance(required_visuals, int)
                            and required_visuals_max < required_visuals
                        )
                    )
                )
                or (
                    duration_mode == "fixed"
                    and (
                        isinstance(fixed_duration, bool)
                        or not isinstance(fixed_duration, (int, float))
                        or not math.isfinite(float(fixed_duration))
                        or not 1 <= float(fixed_duration) <= 600
                    )
                )
                or (
                    "bgm_optional" in template
                    and not isinstance(template["bgm_optional"], bool)
                )
            ):
                continue
            if duration_mode == "narration" and (
                template_id != BILINGUAL_TEMPLATE_ID or template.get("requires_voiceover") is not True
                or template.get("copy_mode") != "bilingual_titles" or template.get("narration_contract_version") != 1
            ):
                continue
            accepted_media_types = raw.get("accepted_media_types")
            if accepted_media_types is not None:
                if (
                    not isinstance(accepted_media_types, list)
                    or not accepted_media_types
                    or len(accepted_media_types) > 2
                    or any(item not in {"image", "video"}
                           for item in accepted_media_types)
                ):
                    continue
                template["accepted_media_types"] = list(dict.fromkeys(
                    accepted_media_types
                ))
            # 轻量标记进公开目录（tunable / template_revision）；完整 overrides_schema
            # 只放在按需读取的 controls 里，避免每次目录响应都塞 22 份大 schema。
            controls = _tunable_controls(raw) if raw.get("tunable") is True else None
            template["tunable"] = controls is not None
            text_definition = matrix_text_controls.parse_controls(raw.get("text_controls"))
            if text_definition is not None:
                text_controls[template_id] = text_definition
                template["text_tunable"] = True
                template["text_revision"] = text_definition["text_revision"]
            if controls is not None:
                template["template_revision"] = controls["template_revision"]
                tunable_controls[template_id] = controls
            templates.append(template)
            seen_template_ids.add(template_id)
        if not _catalog_is_complete(templates):
            raise RuntimeError("模板目录无可用项")
        fonts = [{"value": "", "label": "自动搭配", "source": "automatic"}]
        seen = {""}
        for raw in response.get("fonts") or []:
            if not isinstance(raw, dict):
                continue
            value = str(raw.get("value") or "").strip()
            if not value or value in seen or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,79}", value
            ):
                continue
            source = str(raw.get("source") or "")
            if source not in {"bundled", "private"}:
                continue
            fonts.append({
                "value": value,
                "label": str(raw.get("label") or value)[:40],
                "source": source,
            })
            seen.add(value)
        _CACHE.update({
            "at": now,
            "templates": templates,
            "fonts": fonts,
            "controls": tunable_controls,
            "text_controls": text_controls,
            "max_batch_size": max_batch_size,
            "engine_concurrency": engine_concurrency,
        })


def public_templates(force=False):
    _refresh_catalog(force)
    return [dict(item) for item in _CACHE["templates"]]


def public_fonts(force=False):
    _refresh_catalog(force)
    return [dict(item) for item in _CACHE["fonts"]]


def public_batch_capability(force=False):
    _refresh_catalog(force)
    return {
        "max_batch_size": int(_CACHE["max_batch_size"]),
        "engine_concurrency": dict(_CACHE["engine_concurrency"]),
    }


def public_template_controls(template_id, force=False):
    """Read-only tunable contract for one template (matrix-template-controls).

    The renderer owns tunable/overrides_schema; this view never invents a
    field.  A template without a usable tunable contract reports tunable=false.
    """
    _refresh_catalog(force)
    cleaned = str(template_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", cleaned):
        raise ValueError("请选择有效模板")
    template = next(
        (item for item in _CACHE["templates"] if item["id"] == cleaned), None,
    )
    if template is None:
        raise ValueError("请选择有效模板")
    result = {
        "template_id": cleaned,
        "name": str(template.get("name") or cleaned),
        "tunable": bool(template.get("tunable")),
    }
    controls = _CACHE["controls"].get(cleaned) if result["tunable"] else None
    text_definition = _CACHE.get("text_controls", {}).get(cleaned)
    if text_definition:
        result["text_tunable"] = True
        result["text_controls"] = json.loads(json.dumps(text_definition, ensure_ascii=False))
    if not controls:
        result["tunable"] = False
        result["note"] = ("支持 text_overrides 逐层文字微调；未传字段保留模板默认值。" if text_definition
                          else "该模板暂不支持参数微调，文案、素材与字体按模板默认执行。")
        return result
    schema = controls["overrides_schema"]
    result["template_revision"] = controls["template_revision"]
    result["overrides_schema"] = json.loads(json.dumps(schema, ensure_ascii=False))
    defaults = _override_schema_defaults(schema)
    if defaults:
        result["defaults"] = defaults
    slots = _override_schema_slots(schema)
    if slots:
        result["slots"] = slots
    return result


def _override_number(value, label, minimum, maximum, *, digits=4):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{label}需要是 {minimum}-{maximum} 之间的数字")
    normalized = float(value)
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{label}需要是 {minimum}-{maximum} 之间的数字")
    return round(normalized, digits)


def normalize_overrides(value, *, max_slots=None):
    """Validate and canonicalize one overrides object (contract v1 vocabulary).

    Empty/absent overrides stay absent so the legacy path keeps byte-identical
    payloads.  Every rejection is explicit: unknown keys, bool-as-number, NaN,
    out-of-range values and bad colours never pass silently.
    """
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("模板参数需要是对象")
    unknown = sorted(set(value) - set(OVERRIDE_FIELDS))
    if unknown:
        raise ValueError("模板参数不支持字段：" + unknown[0])
    result = {}
    if "title_scale" in value:
        result["title_scale"] = _override_number(
            value["title_scale"], _OVERRIDE_LABELS["title_scale"],
            OVERRIDE_SCALE_MIN, OVERRIDE_SCALE_MAX,
        )
    if "title_offset_y" in value:
        offset = value["title_offset_y"]
        if (
            isinstance(offset, bool) or not isinstance(offset, int)
            or not OVERRIDE_OFFSET_MIN <= offset <= OVERRIDE_OFFSET_MAX
        ):
            raise ValueError(
                f"{_OVERRIDE_LABELS['title_offset_y']}需要是 "
                f"{OVERRIDE_OFFSET_MIN}-{OVERRIDE_OFFSET_MAX} 之间的整数像素"
            )
        result["title_offset_y"] = int(offset)
    if "cta_scale" in value:
        result["cta_scale"] = _override_number(
            value["cta_scale"], _OVERRIDE_LABELS["cta_scale"],
            OVERRIDE_SCALE_MIN, OVERRIDE_SCALE_MAX,
        )
    if "cta_offset_y" in value:
        offset = value["cta_offset_y"]
        if (
            isinstance(offset, bool) or not isinstance(offset, int)
            or not OVERRIDE_OFFSET_MIN <= offset <= OVERRIDE_OFFSET_MAX
        ):
            raise ValueError(
                f"{_OVERRIDE_LABELS['cta_offset_y']}需要是 "
                f"{OVERRIDE_OFFSET_MIN}-{OVERRIDE_OFFSET_MAX} 之间的整数像素"
            )
        result["cta_offset_y"] = int(offset)
    if "accent_color" in value:
        color = value["accent_color"]
        if not isinstance(color, str) or not ACCENT_COLOR_RE.fullmatch(color):
            raise ValueError("强调色需要是 #RRGGBB 格式的十六进制颜色")
        result["accent_color"] = color.upper()
    if "media_focus" in value:
        focuses = value["media_focus"]
        if not isinstance(focuses, list) or not 1 <= len(focuses) <= MEDIA_FOCUS_SLOT_LIMIT:
            raise ValueError("画面焦点需要是 1-21 项的数组")
        slot_limit = int(max_slots) if isinstance(max_slots, int) and not isinstance(
            max_slots, bool
        ) and 0 < max_slots <= MEDIA_FOCUS_SLOT_LIMIT else MEDIA_FOCUS_SLOT_LIMIT
        normalized_focus = []
        seen_slots = set()
        for item in focuses:
            if not isinstance(item, dict) or set(item) != {"slot", "x", "y"}:
                raise ValueError("画面焦点每项需要包含 slot、x、y")
            slot = item["slot"]
            if (
                isinstance(slot, bool) or not isinstance(slot, int)
                or not 1 <= slot <= slot_limit
            ):
                raise ValueError(f"画面焦点槽位需要是 1-{slot_limit} 之间的整数")
            if slot in seen_slots:
                raise ValueError("画面焦点槽位不能重复")
            seen_slots.add(slot)
            normalized_focus.append({
                "slot": int(slot),
                "x": _override_number(item["x"], "焦点横向位置", 0, 1),
                "y": _override_number(item["y"], "焦点纵向位置", 0, 1),
            })
        result["media_focus"] = sorted(
            normalized_focus, key=lambda item: item["slot"],
        )
    return {key: result[key] for key in OVERRIDE_FIELDS if key in result}


def _overrides_agree(sent, echoed):
    """True when the renderer echo keeps every value we sent (defaults may add)."""
    return all(echoed.get(key) == value for key, value in sent.items())


def _effective_overrides(echoed, sent):
    """Contract-fixed defaults filled in, renderer-declared values kept."""
    effective = dict(OVERRIDE_DEFAULTS)
    effective.update(echoed)
    # Renderer emits [] for unchanged focus; omit this no-op in the strict
    # persisted contract so worker revalidation accepts the same prepared job.
    if effective.get("media_focus") == []:
        effective.pop("media_focus")
    return {key: effective[key] for key in OVERRIDE_FIELDS if key in effective}


def _normalize_voiceover(value, username, *, allow_legacy_text=False):
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise ValueError("配音设置无效")
    allowed = {
        "text", "voice", "speed", "pitch", "volume", "delivery",
        "voice_scope", "voice_version",
    }
    if set(value) - allowed:
        raise ValueError("配音设置包含无效字段")
    if (
        not allow_legacy_text
        and len(str(value.get("text") or "").strip()) > MAX_VOICEOVER_TEXT_LENGTH
    ):
        raise ValueError("口播文案需要控制在 120 字以内")
    username = str(username or "").strip()
    if not username:
        raise ValueError("无法确认配音音色归属")
    from . import audio as audio_domain
    feature_flags.require_enabled("audio")
    if not audio_domain.cosyvoice.enabled():
        raise feature_flags.FeatureDisabled("配音服务暂不可用，请稍后重试")
    normalized = audio_domain.validate_audio_payload(dict(value), username)
    requested_scope = str(value.get("voice_scope") or "").strip()
    if requested_scope and requested_scope != normalized.get("voice_scope"):
        raise ValueError("配音音色归属已变化，请重新选择")
    if normalized.get("voice_scope") == "personal":
        audio_domain.require_owned_ready_personal_voice(
            username, normalized["voice"],
        )
    provider_voice = audio_domain.resolve_audio_provider_voice(
        username, normalized["voice"],
    )
    voice_version = hashlib.sha256(
        str(provider_voice).encode("utf-8"),
    ).hexdigest()
    requested_version = str(value.get("voice_version") or "").strip()
    if requested_version and requested_version != voice_version:
        raise ValueError("配音音色版本已变化，请重新选择")
    result = {
        key: normalized[key]
        for key in (
            "text", "voice", "speed", "pitch", "volume", "delivery",
            "voice_scope",
        )
        if key in normalized
    }
    result["voice_version"] = voice_version
    return result


def _normalize_bgm_volume(value):
    if isinstance(value, bool):
        raise ValueError("背景音乐音量需要设置为 0-100%")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("背景音乐音量需要设置为 0-100%") from exc
    if not math.isfinite(normalized) or not 0 <= normalized <= 1:
        raise ValueError("背景音乐音量需要设置为 0-100%")
    return round(normalized, 3)


def shared_materials_allowed(user):
    """Only staff or an explicit opaque account grant may use Yuelei materials."""
    if not isinstance(user, dict):
        return False
    if str(user.get("role") or "").strip().lower() == "admin":
        return True
    grants = {
        value.strip() for value in os.environ.get(
            "MATRIX_SHARED_MATERIAL_ACCOUNT_IDS", ""
        ).split(",") if value.strip()
    }
    return str(user.get("account_id") or "").strip() in grants


def public_only_materials(user):
    """一次性邀请码注册的账号：模板成片只能用公网素材（2026-09-17 老板定调）。

    管理员与未绑定一次性码的账号（含永久码注册的老账号）路径完全不变。判定只
    依据 auth /me 透出的 single_use_invite 标记；标记缺失（auth 尚未升级或查询
    异常时 fail-open）按不受限处理，保护绝大多数账号不受影响。
    """
    if not isinstance(user, dict):
        return False
    if str(user.get("role") or "").strip().lower() == "admin":
        return False
    return bool(user.get("single_use_invite"))


def _normalize_user_materials(value, *, trusted_frozen=False):
    if value in (None, "", []):
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ValueError("本人素材需要包含 1-20 个图片或视频")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("本人素材条目格式不正确")
        allowed = (
            {"sha256", "media_type", "clip_start_seconds"}
            if trusted_frozen else
            {"upload_id", "media_type", "clip_start_seconds"}
        )
        if set(item) - allowed:
            raise ValueError("本人素材包含无效字段")
        media_type = str(item.get("media_type") or "").strip().lower()
        if media_type not in _USER_MATERIAL_TYPES:
            raise ValueError("本人素材只支持图片或视频")
        record = {"media_type": media_type}
        if trusted_frozen:
            sha256 = str(item.get("sha256") or "").strip().lower()
            if not _USER_MATERIAL_SHA_RE.fullmatch(sha256):
                raise ValueError("本人素材校验值无效")
            record["sha256"] = sha256
        else:
            upload_id = str(item.get("upload_id") or "").strip().lower()
            if not upload_id:
                raise ValueError("请先从 Agent 或我的资产上传图片或视频")
            record["upload_id"] = upload_id
        start = item.get("clip_start_seconds")
        if start is not None:
            if (
                media_type != "video" or isinstance(start, bool)
                or not isinstance(start, (int, float))
                or not math.isfinite(float(start)) or not 0 <= float(start) <= 3600
            ):
                raise ValueError("视频素材起始时间无效")
            record["clip_start_seconds"] = round(float(start), 3)
        result.append(record)
    return result


def _read_user_upload(item, username):
    from . import cli_uploads
    handle, size, meta = cli_uploads.open_upload(
        item["media_type"], item["upload_id"], username,
    )
    mime = str((meta or {}).get("mime") or "").strip().lower()
    if mime not in {
        "image/png", "image/jpeg", "image/webp",
        "video/mp4", "video/quicktime",
    }:
        handle.close()
        raise ValueError("本人素材文件格式不支持")
    if (
        (item["media_type"] == "image" and not mime.startswith("image/"))
        or (item["media_type"] == "video" and not mime.startswith("video/"))
    ):
        handle.close()
        raise ValueError("本人素材类型与文件 MIME 不一致")
    return handle, size, mime, str(meta["sha256"])


def _upload_user_asset(stream, length, sha256, content_type, timeout=3600):
    request = urllib.request.Request(
        API_URL + "/v1/user-assets", data=stream,
        headers={
            "Authorization": "Bearer " + API_TOKEN,
            "Content-Type": content_type,
            "X-HQ-Asset-Sha256": sha256,
            "Content-Length": str(length),
        },
        method="POST",
    )
    try:
        with _NO_PROXY.open(request, timeout=timeout):
            return True
    except urllib.error.HTTPError as exc:
        raise RuntimeError("本人素材暂时无法用于模板成片") from exc


def _resolve_user_materials(
        value, username, *, trusted_frozen=False, video_only=False):
    materials = _normalize_user_materials(
        value, trusted_frozen=trusted_frozen,
    )
    if materials and video_only and any(
            item["media_type"] != "video" for item in materials):
        raise ValueError(
            "固定节奏动效模板目前只支持视频素材，"
            "请上传视频或改用 ref 模板"
        )
    if not materials or trusted_frozen:
        return materials
    resolved = []
    for item in materials:
        try:
            handle, length, content_type, sha256 = _read_user_upload(item, username)
        except Exception as exc:
            raise ValueError("本人素材不存在、已过期或不属于当前账号，请重新上传") from exc
        if not length:
            handle.close()
            raise ValueError("本人素材内容为空，请重新上传")
        with handle:
            _upload_user_asset(handle, length, sha256, content_type)
        record = {"sha256": sha256, "media_type": item["media_type"]}
        if "clip_start_seconds" in item:
            record["clip_start_seconds"] = item["clip_start_seconds"]
        resolved.append(record)
    return resolved


def _tunable_template_controls(template):
    """Renderer-declared tunable contract of one catalog template, or None."""
    if not isinstance(template, dict) or template.get("tunable") is not True:
        return None
    controls = _CACHE["controls"].get(str(template.get("id") or ""))
    return controls if isinstance(controls, dict) else None


def _override_slot_limit(template, schema):
    """How many focus slots one media_focus array may address."""
    limits = []
    declared = _override_schema_slots(schema).get("max")
    for candidate in (declared, template.get("required_visuals_max")):
        if (
            not isinstance(candidate, bool) and isinstance(candidate, int)
            and 0 < candidate <= MEDIA_FOCUS_SLOT_LIMIT
        ):
            limits.append(candidate)
    return min(limits) if limits else MEDIA_FOCUS_SLOT_LIMIT


def _preview_lookup_record(username, preview_id, preview_lookup=None):
    """Read one owner-scoped preview record (injectable for unit tests)."""
    if callable(preview_lookup):
        return preview_lookup(username, preview_id)
    if not username:
        return None
    try:
        from .core import jdb
        from . import matrix_template_submission
        return matrix_template_submission.get_preview(jdb, username, preview_id)
    except Exception as exc:
        print(
            "[matrix-template-preview] 预览记录暂不可读 id=%s: %s"
            % (preview_id, str(exc)[:160]), flush=True,
        )
        return None


def _resolve_template_tuning(body, template, username, preview_lookup=None):
    """Validate template_revision/overrides/preview_id for one request.

    Returns (overrides, template_revision, preview_record).  The legacy path
    (no fine-tune fields) returns empty values so today's payloads stay
    byte-identical.  Tunable-only enforcement and an explicitly rejected batch
    extension live here because every submission path funnels through
    validate_payload.
    """
    revision = str(body.get("template_revision") or "").strip().lower()
    overrides_present = body.get("overrides") not in (None, {})
    preview_id = str(body.get("preview_id") or "").strip()
    batch = bool(
        body.get("batch_id") or body.get("batch_index") is not None
        or body.get("batch_size") is not None
    )
    if not (overrides_present or revision or preview_id):
        return {}, "", None
    if batch:
        raise ValueError("批量生成暂不支持模板参数微调，请改用单条生成")
    if revision and not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ValueError("模板版本无效，请重新读取模板可调范围")
    controls = _tunable_template_controls(template)
    if controls is None:
        raise ValueError(
            "当前模板暂不支持参数微调，请选择支持微调的模板"
        )
    if not revision:
        raise ValueError("参数微调需要同时提供 template_revision")
    if revision != controls["template_revision"]:
        raise ValueError("模板样式已更新，请重新预览后再提交")
    overrides = (
        normalize_overrides(
            body.get("overrides"),
            max_slots=_override_slot_limit(template, controls["overrides_schema"]),
        )
        if overrides_present else {}
    )
    record = None
    if preview_id:
        if not PREVIEW_ID_RE.fullmatch(preview_id):
            raise ValueError("预览标识无效，请重新预览")
        record = _preview_lookup_record(username, preview_id, preview_lookup)
        if not isinstance(record, dict):
            raise ValueError("预览已过期或不属于当前账号，请重新预览")
        if str(record.get("preview_id") or "") != preview_id:
            raise ValueError("预览已过期或不属于当前账号，请重新预览")
        if str(record.get("template_id") or "") != str(template.get("id") or ""):
            raise ValueError("这次预览的是其他模板，请重新预览")
        if str(record.get("template_revision") or "") != controls["template_revision"]:
            raise ValueError("模板样式已更新，请重新预览")
        frozen_overrides = record.get("overrides")
        if not isinstance(frozen_overrides, dict):
            frozen_overrides = {}
        if overrides:
            # 提交载荷可能是渲染端补齐默认值后的生效版（执行期重放存过的载荷）；
            # 只要预览冻结的发送值全部保留就算同一份输入（合同 §3.5）。
            if not (
                overrides == frozen_overrides
                or _overrides_agree(frozen_overrides, overrides)
            ):
                raise ValueError("预览参数与本次提交不一致，请重新预览或沿用预览参数")
        overrides = frozen_overrides
    return overrides, revision, record


def _preview_materials_match(resolved, frozen):
    """True when resolved uploads are exactly the previewed frozen materials."""
    def shape(items):
        return [
            {
                key: item.get(key) for key in
                ("sha256", "media_type", "clip_start_seconds")
                if item.get(key) is not None
            }
            for item in items if isinstance(item, dict)
        ]
    return bool(frozen) and shape(resolved) == shape(frozen)


def preview_input_fingerprint(payload):
    """Owner-independent identity of one preview's input (text + media + params)."""
    if not isinstance(payload, dict):
        raise ValueError("预览参数无效")
    identity = {
        "top_text": " ".join(str(payload.get("top_text") or "").split()),
        "bottom_text": " ".join(str(payload.get("bottom_text") or "").split()),
        "template_id": str(payload.get("template_id") or ""),
        "font_family": str(payload.get("font_family") or ""),
        "material_policy": str(payload.get("material_policy") or ""),
        "bgm": bool(payload.get("bgm")),
        "user_materials": [
            {
                key: item.get(key) for key in
                ("sha256", "media_type", "clip_start_seconds")
                if item.get(key) is not None
            }
            for item in (payload.get("user_materials") or [])
            if isinstance(item, dict)
        ] if isinstance(payload.get("user_materials"), list) else [],
        "template_revision": str(payload.get("template_revision") or ""),
        "overrides": (
            payload.get("overrides")
            if isinstance(payload.get("overrides"), dict) else {}
        ),
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_payload(
        raw, username="", *, trusted_semantic_layout=None,
        trusted_frozen_execution=False, allow_shared_materials=None,
        public_only_materials=False, for_preview=False, preview_lookup=None):
    if isinstance(raw, dict) and raw.get("mode") == "timeline":
        from . import timeline_compose
        return timeline_compose.validate_payload(raw, username)
    require_available()
    body = dict(raw or {})
    top = " ".join(str(body.get("top_text") or "").split())
    bottom = " ".join(str(body.get("bottom_text") or "").split())
    if not 2 <= len(top) <= 60:
        raise ValueError("顶部标题需要 2-60 个字符")
    if not 2 <= len(bottom) <= 80:
        raise ValueError("底部行动文案需要 2-80 个字符")
    templates = public_templates()
    template_id = str(
        body.get("template_id")
        or (templates[0]["id"] if templates else "")
    )
    template = next(
        (item for item in templates if item["id"] == template_id), None
    )
    if template is None:
        raise ValueError("请选择有效模板")
    # 参数微调（合同 v1）：只有渲染侧声明 tunable 的模板接受 template_revision/
    # overrides/preview_id；其余模板与旧路径完全一致（不产生任何新字段）。
    overrides, template_revision, preview_record = _resolve_template_tuning(
        body, template, username, preview_lookup,
    )
    text_style = matrix_text_controls.normalize_request(body, _CACHE.get("text_controls", {}).get(template_id))
    if for_preview and text_style:
        raise ValueError("逐层文字微调目前用于正式生成，不支持旧版双版本预览")
    font_family = str(body.get("font_family") or "").strip()
    font_selectable = template.get("font_selectable") is not False
    if (
        font_selectable and font_family
        and font_family not in {item["value"] for item in public_fonts()}
    ):
        raise ValueError("请选择当前可用字体")
    voiceover = _normalize_voiceover(
        body.get("voiceover"), username,
        allow_legacy_text=trusted_frozen_execution,
    )
    if template_id == BILINGUAL_TEMPLATE_ID:
        if not voiceover:
            raise ValueError("双语字幕模板必须启用口播配音")
        from . import matrix_bilingual
        if not trusted_frozen_execution:
            matrix_bilingual.ensure_ready()
    bgm = body.get("bgm", False if voiceover else True)
    if not isinstance(bgm, bool):
        raise ValueError("背景音乐设置无效")
    if template_id == BILINGUAL_TEMPLATE_ID and bgm:
        raise ValueError("双语字幕模板按原版规则不使用背景音乐")
    if (
        template.get("bgm_mode") == "bound"
        and template.get("bgm_optional") is not True
        and not bgm
    ):
        raise ValueError("当前模板的背景音乐不可关闭")
    bgm_volume = None
    if "bgm_volume" in body:
        if not voiceover or not bgm:
            raise ValueError("背景音乐音量仅用于已开启背景音乐的口播")
        bgm_volume = _normalize_bgm_volume(body["bgm_volume"])
    elif voiceover and bgm:
        bgm_volume = DEFAULT_VOICEOVER_BGM_VOLUME
    fixed_duration = None
    if template_id == BILINGUAL_TEMPLATE_ID:
        fixed_duration = 8.0  # Admission only; execution uses measured narration.
    if template.get("duration_mode") == "fixed_12":
        fixed_duration = 12.0
    elif template.get("duration_mode") == "fixed":
        value = template.get("fixed_duration_seconds")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 8 <= float(value) <= 20
        ):
            raise RuntimeError("固定模板时长合同无效")
        fixed_duration = float(value)
    duration = body.get("duration")
    if duration not in (None, ""):
        try:
            duration = float(duration)
        except (TypeError, ValueError) as exc:
            raise ValueError("视频时长设置无效") from exc
        if fixed_duration is not None:
            if abs(duration - fixed_duration) > 0.001:
                raise ValueError(
                    f"当前模板时长固定为 {fixed_duration:g} 秒"
                )
        elif duration < 8 or duration > 15:
            raise ValueError("视频时长需要 8-15 秒")
    else:
        duration = None
    if fixed_duration is not None:
        duration = fixed_duration
    material_durations = None
    auto_materials = False
    if template_id in (*MOTION_V3_TEMPLATE_IDS, BILINGUAL_TEMPLATE_ID) and not trusted_frozen_execution:
        from . import matrix_template_account_media as account_media
        if not body.get("user_materials"):
            body["user_materials"], material_durations = account_media.initial(template_id, username)
            auto_materials = True
        elif template_id == BILINGUAL_TEMPLATE_ID:
            explicit = _normalize_user_materials(body["user_materials"])
            material_durations = account_media.explicit_durations(explicit, username)
    raw_user_materials = body.get("user_materials")
    user_material_count = (
        len(raw_user_materials) if isinstance(raw_user_materials, list) else 0
    )
    if not user_material_count and preview_record is not None:
        # 带 preview_id 且未重传素材：沿用预览冻结的同一批素材（合同 §3.5）。
        user_material_count = len([
            item for item in (preview_record.get("materials") or [])
            if isinstance(item, dict)
        ])
    maximum_visuals = template.get("required_visuals_max")
    if (
        1 <= user_material_count <= 20
        and not isinstance(maximum_visuals, bool)
        and isinstance(maximum_visuals, int)
        and user_material_count > maximum_visuals
    ):
        raise ValueError(
            f"当前模板最多使用 {maximum_visuals} 份素材，"
            "请减少素材或改用更多画面位的模板"
        )
    if (
        user_material_count
        and (
            template.get("duration_mode") in {
                "random_integer_7_15", "random_integer_8_15",
            }
            or (
                template.get("duration_mode") is None
                and REFERENCE_TEMPLATE_RE.fullmatch(template_id)
            )
        )
    ):
        duration = max(float(duration or 8), user_material_count * 3.0)
    accepted_media_types = template.get("accepted_media_types")
    video_only = (
        "image" not in accepted_media_types
        if isinstance(accepted_media_types, list)
        else template.get("duration_mode") in {"fixed", "fixed_12", "narration"}
    )
    user_materials = _resolve_user_materials(
        body.get("user_materials"), username,
        trusted_frozen=trusted_frozen_execution,
        video_only=video_only,
    )
    if preview_record is not None:
        frozen_materials = [
            dict(item) for item in (preview_record.get("materials") or [])
            if isinstance(item, dict)
        ]
        if user_materials is None:
            if not frozen_materials:
                raise ValueError("预览记录缺少素材信息，请重新预览")
            user_materials = frozen_materials
        elif _preview_materials_match(user_materials, frozen_materials):
            user_materials = frozen_materials
        else:
            raise ValueError("这次提交的素材与预览不一致，请重新预览")
    font_applied = font_family if (font_family and font_selectable) else ""
    # 素材策略（2026-09-12 生产实锤 #8482 + #8629 合并定稿）：
    # 带本人素材 -> owned_public：渲染端只在该策略下接受本人素材（ref-* 最多 3 份、
    # 1~2 份 Pexels 补齐）；带素材仍判 shared 会先过 preflight 再被 /v1/jobs 拒
    # （「需要 5 个」，#8482）。
    # 无本人素材 -> shared：一律走本地素材库，绝不绕去 Pexels 公网（#8629 因此
    # 偶发失败；老板定调「不要再让他走公网了，我们优先走素材库」）。
    material_policy = (
        MATERIAL_POLICY_OWNED_PUBLIC if user_materials
        else MATERIAL_POLICY_SHARED
    )
    if preview_record is not None:
        # 预览与提交必须证明是同一份输入（合同 §3.5：完整输入摘要不含时长，
        # 时长/运动种子来自预览冻结的 prepared）。
        expected_fingerprint = str(preview_record.get("fingerprint") or "")
        actual_fingerprint = preview_input_fingerprint({
            "top_text": top, "bottom_text": bottom, "template_id": template_id,
            "font_family": font_applied, "material_policy": material_policy,
            "bgm": bgm, "user_materials": user_materials,
            "template_revision": template_revision, "overrides": overrides,
        })
        if expected_fingerprint and expected_fingerprint != actual_fingerprint:
            raise ValueError("本次提交与预览输入不一致，请重新预览")
    candidate = {
        "top_text": top, "bottom_text": bottom,
        "template_id": template_id, "bgm": bgm, "duration": duration,
    }
    candidate.update(text_style)
    if overrides:
        candidate["overrides"] = overrides
    if template_revision:
        candidate["template_revision"] = template_revision
    if allow_shared_materials is not None or template_id in (*MOTION_V3_TEMPLATE_IDS, BILINGUAL_TEMPLATE_ID):
        candidate["material_policy"] = material_policy
    # 素材范围（2026-09-17）：受限账号（一次性邀请码注册）只允许公网素材；
    # 标记只在受限时出现，其余账号的候选载荷与历史逐字节一致。
    if public_only_materials:
        # 2026-09-17 临时硬闸（主 Agent）：渲染节点侧的公网过滤尚未全量部署，实测受限账号
        # 仍会拿到公司素材（job 9303 实锤）。节点全部升级前，受限账号在入口直接明确报错，绝不生成。
        raise ValueError("当前可用公共素材不足，暂时无法生成，请稍后重试")
        candidate["material_scope"] = MATERIAL_SCOPE_PUBLIC_ONLY
    if user_materials:
        candidate["user_materials"] = user_materials
    if font_family and font_selectable:
        candidate["font_family"] = font_family
    semantic_contract = template.get("semantic_layout")
    semantic_contract = matrix_text_controls.semantic_contract(
        semantic_contract, text_style.get("text_overrides"),
        _CACHE.get("text_controls", {}).get(template_id),
    )
    if (
        template.get("engine") == "hyperframes"
        and semantic_contract is None
    ):
        raise ValueError(
            "模板 AI 断句能力不可用，视频任务未创建且未扣点"
        )
    batch_id = str(body.get("batch_id") or "").strip().lower()
    batch_index = body.get("batch_index")
    batch_size = body.get("batch_size")
    if batch_id or batch_index is not None or batch_size is not None:
        if (
            not re.fullmatch(r"[0-9a-f]{32}", batch_id)
            or isinstance(batch_index, bool) or not isinstance(batch_index, int)
            or isinstance(batch_size, bool) or not isinstance(batch_size, int)
            or not 1 <= batch_index <= batch_size <= 5
        ):
            raise ValueError("批量任务参数无效")
        candidate.update({
            "batch_id": batch_id,
            "batch_index": batch_index,
            "batch_size": batch_size,
        })
    response = None
    if semantic_contract is not None:
        def validate_semantic_layout(semantic_layout):
            candidate["semantic_layout"] = semantic_layout
            try:
                value = _request("POST", "/v1/preflight", candidate, timeout=10)
                payload = value.get("payload") if isinstance(value, dict) else None
                echoed = (
                    payload.get("semantic_layout")
                    if isinstance(payload, dict) else None
                )
                if isinstance(echoed, dict):
                    echoed = dict(echoed)

                def safe_break_echo(key):
                    values = echoed.get(key)
                    original = semantic_layout.get(key)
                    return (
                        isinstance(values, list)
                        and isinstance(original, list)
                        and all(
                            not isinstance(item, bool) and isinstance(item, int)
                            for item in values
                        )
                        and values == sorted(set(values))
                        and set(values).issubset(original)
                    )

                if (
                    not isinstance(echoed, dict)
                    or set(echoed) != set(semantic_layout)
                    or any(
                        echoed.get(key) != semantic_layout.get(key)
                        for key in (
                            "version", "model", "source_sha256", "top1_end",
                        )
                    )
                    or not all(safe_break_echo(key) for key in (
                        "top_break_after", "bottom_break_after",
                    ))
                    or (
                        echoed["top1_end"] != len(top) - 1
                        and echoed["top1_end"] not in echoed["top_break_after"]
                    )
                ):
                    return False, "生成端回显的 semantic_layout 关键字段不一致"
                normalized = echoed != semantic_layout
                original_counts = (
                    len(semantic_layout["top_break_after"]),
                    len(semantic_layout["bottom_break_after"]),
                )
                # Cache and submit the font-authoritative subset from generation.
                semantic_layout.clear()
                semantic_layout.update(echoed)
                if normalized:
                    print(
                        "[matrix-template-semantic-normalized] "
                        f"template={template_id} breaks="
                        f"{original_counts[0]}->{len(echoed['top_break_after'])},"
                        f"{original_counts[1]}->{len(echoed['bottom_break_after'])}",
                        flush=True,
                    )
                return True, value
            except MatrixTemplateHTTPError as exc:
                if exc.status == 400 and (
                    "语义" in str(exc) or "完整词组" in str(exc)
                ):
                    return False, str(exc)
                raise
            except RuntimeError as exc:
                raise feature_flags.FeatureDisabled(
                    "模板成片服务暂不可用，请稍后重试"
                ) from exc

        try:
            if trusted_semantic_layout is not None:
                semantic_layout = dict(trusted_semantic_layout)
                accepted, response = validate_semantic_layout(semantic_layout)
                if not accepted:
                    raise RuntimeError(str(response or "语义排版校验失败"))
            else:
                semantic_layout, response = matrix_template_semantics.resolve(
                    top, bottom, template_id, semantic_contract,
                    validate_semantic_layout,
                )
            candidate["semantic_layout"] = semantic_layout
        except MatrixTemplateHTTPError as exc:
            if exc.status in (400, 409):
                raise ValueError(str(exc)) from exc
            raise feature_flags.FeatureDisabled(
                "模板成片服务暂不可用，请稍后重试"
            ) from exc
        except RuntimeError as exc:
            print(
                "[matrix-template-semantic-rejected] "
                f"template={template_id} reason={str(exc)[:240]}",
                flush=True,
            )
            raise ValueError(
                (
                    "AI 断句结果未通过生成校验，"
                    "视频任务失败并将自动退点"
                    if trusted_semantic_layout is not None else
                    "AI 断句失败，视频任务未创建且未扣点，请重试"
                )
            ) from exc
    if for_preview:
        # 预览不做独立预检：渲染侧 /v1/preview-jobs 自己冻结 prepared
        # （素材顺序/切片/总时长/运动种子）并跑真实渲染（合同 §3.2）。
        # 但 AI 语义断句仍是渲染侧必需字段，已在上面解析进 candidate 随请求转发。
        result = dict(candidate)
        if result.get("duration") is None:
            result.pop("duration", None)
        return result
    if response is None:
        try:
            response = _request("POST", "/v1/preflight", candidate, timeout=10)
        except MatrixTemplateHTTPError as exc:
            if exc.status in (400, 409):
                raise ValueError(str(exc)) from exc
            raise feature_flags.FeatureDisabled(
                "模板成片服务暂不可用，请稍后重试"
            ) from exc
        except RuntimeError as exc:
            raise feature_flags.FeatureDisabled(
                "模板成片服务暂不可用，请稍后重试"
            ) from exc
    payload = response.get("payload") if isinstance(response, dict) else None
    if not isinstance(payload, dict):
        raise RuntimeError("模板成片预检结果无效")
    expected = set(candidate)
    allowed_extra = {"effective_overrides"} if "overrides" in candidate else set()
    if set(payload) - expected - allowed_extra:
        raise RuntimeError("模板成片预检结果无效")
    missing = expected - set(payload)
    if missing - {"overrides"} or ("overrides" in missing and "effective_overrides" not in payload):
        raise RuntimeError("模板成片预检结果无效")
    for key, value in candidate.items():
        # duration 由渲染端定稿；overrides 允许渲染端回显补齐默认值后再逐字段核对。
        if key in {"duration", "overrides"}:
            continue
        if payload.get(key) != value:
            raise RuntimeError("模板成片预检参数不一致")
    effective_overrides = None
    if "overrides" in candidate:
        echoed = payload.get("overrides")
        if not isinstance(echoed, dict):
            echoed = payload.get("effective_overrides")
        if not isinstance(echoed, dict) or not _overrides_agree(
                candidate["overrides"], echoed):
            raise RuntimeError("模板成片预检参数不一致")
        effective_overrides = _effective_overrides(echoed, candidate["overrides"])
    authoritative_duration = payload.get("duration")
    if (isinstance(authoritative_duration, bool)
            or not isinstance(authoritative_duration, (int, float))
            or not math.isfinite(float(authoritative_duration))
            or (
                fixed_duration is not None
                and abs(float(authoritative_duration) - fixed_duration) > 0.001
            )
            or (
                fixed_duration is None
                and not 8 <= float(authoritative_duration) <= 15
            )):
        raise RuntimeError("模板成片预检时长无效")
    result = dict(payload, duration=float(authoritative_duration))
    if material_durations is not None:
        result["_matrix_material_durations"] = material_durations
        result["_matrix_auto_materials"] = auto_materials
    result.pop("effective_overrides", None)
    if preview_record is not None:
        # 带 preview_id 的正式任务必须按预览冻结的发送版参数转发：渲染端以
        # overrides 参与输入摘要核对并复用 prepared（合同 §3.5）。生效版
        # （默认值补齐）已存在预览记录里供展示，不覆盖发送版。
        result["overrides"] = (
            candidate["overrides"] if "overrides" in candidate else {}
        )
    elif effective_overrides is not None:
        # 生效值规范化后回显（合同 §1）：默认值补齐，渲染端回显为准。
        result["overrides"] = effective_overrides
    if template_revision:
        result["template_revision"] = template_revision
    if preview_record is not None:
        # preview_id 进正式提交（渲染端据此复用 prepared）；摘要随载荷冻结，
        # 让幂等身份与「预览时冻结的那一份 prepared」绑定（不发给渲染端）。
        result["preview_id"] = str(
            preview_record.get("preview_id")
            or body.get("preview_id") or ""
        ).strip()
        prepared_digest = str(preview_record.get("prepared_digest") or "")
        if prepared_digest:
            result["_prepared_digest"] = prepared_digest
    if voiceover:
        result["voiceover"] = voiceover
        if bgm:
            result["bgm_volume"] = bgm_volume
    return result


def _safe_file_url(value):
    base = _validated_base()
    parsed = urllib.parse.urlsplit(str(value or ""))
    if parsed.scheme or parsed.netloc:
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            raise RuntimeError("模板成片服务返回了无效文件地址")
        return urllib.parse.urlunsplit(parsed)
    path = "/" + str(value or "").lstrip("/")
    prefix = base.path.rstrip("/")
    return urllib.parse.urlunsplit((base.scheme, base.netloc, prefix + path, "", ""))


def _remaining_budget(deadline_at, message="模板成片生成超时"):
    from . import task_termination
    task_termination.check()
    remaining = float(deadline_at) - time.time()
    if remaining <= 0:
        raise RuntimeError(message)
    return remaining


def _media_probe(path, timeout=30):
    from .task_termination import run_process
    try:
        completed = run_process(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height,duration,pix_fmt,color_primaries,color_transfer,color_space,color_range",
                "-of", "json", str(path),
            ],
            check=True, capture_output=True, text=True,
            timeout=max(0.1, min(30.0, float(timeout))),
        )
        value = json.loads(completed.stdout or "{}")
        duration = float((value.get("format") or {}).get("duration") or 0)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        raise RuntimeError("配音文件无法读取") from exc
    if not 0.1 <= duration <= MAX_VOICEOVER_SECONDS:
        raise RuntimeError("配音时长无效或超过 10 分钟")
    return value.get("streams") or [], duration


def _voiceover_cache_path(job_id, username, payload):
    fingerprint = hashlib.sha256(json.dumps(
        {"username": username, "voiceover": payload},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    batch_id = str(payload.get("_batch_id") or "")
    scope = batch_id if re.fullmatch(r"[0-9a-f]{32}", batch_id) else str(job_id)
    scope = re.sub(r"[^A-Za-z0-9_.-]", "-", scope)[:64] or "job"
    owner = hashlib.sha256(str(username).encode("utf-8")).hexdigest()[:12]
    root = OUT_DIR / ".matrix-template-voiceover"
    return root / f"{owner}-{scope}-{fingerprint[:24]}.mp3", fingerprint


def _cleanup_voiceover_cache(root, now=None):
    cutoff = float(time.time() if now is None else now) - VOICEOVER_CACHE_RETENTION_SECONDS
    try:
        for path in root.glob("*.mp3"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        return


def _owned_output_path(relative):
    value = str(relative or "").replace("\\", "/").lstrip("/")
    parts = [part for part in value.split("/") if part and part not in {".", ".."}]
    if not parts or len(parts) != len([part for part in value.split("/") if part]):
        raise RuntimeError("配音文件路径无效")
    root = OUT_DIR.resolve()
    path = OUT_DIR.joinpath(*parts).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("配音文件路径无效") from exc
    return path


def _prepare_voiceover_audio(job_id, username, voiceover, batch_id, deadline_at):
    if not voiceover:
        return None
    cache_payload = dict(voiceover, _batch_id=str(batch_id or ""))
    target, fingerprint = _voiceover_cache_path(
        job_id, str(username or ""), cache_payload,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.parent.chmod(0o700)
    except OSError:
        pass
    _cleanup_voiceover_cache(target.parent)
    lock = _VOICEOVER_CACHE_LOCKS[int(fingerprint[:8], 16) % len(_VOICEOVER_CACHE_LOCKS)]
    if not _persist_runtime(job_id, phase="synthesizing_voiceover"):
        raise RuntimeError("模板成片配音状态保存失败")
    if not lock.acquire(timeout=max(0.1, _remaining_budget(deadline_at))):
        raise RuntimeError("模板成片等待共享配音超时")
    try:
        if target.is_file() and 0 < target.stat().st_size <= MAX_VOICEOVER_BYTES:
            try:
                streams, duration = _media_probe(
                    target, timeout=_remaining_budget(deadline_at),
                )
                if any(item.get("codec_type") == "audio" for item in streams):
                    prepared = {"path": target, "duration": duration,
                                "fingerprint": fingerprint}
                else:
                    prepared = None
            except RuntimeError:
                prepared = None
            if prepared is None:
                target.unlink(missing_ok=True)
        else:
            prepared = None
        if prepared is None:
            _remaining_budget(deadline_at)
            from . import audio as audio_domain
            if not _VOICEOVER_SYNTH_LOCK.acquire(
                timeout=max(0.1, _remaining_budget(deadline_at)),
            ):
                raise RuntimeError("模板成片等待配音服务超时")
            try:
                expected_version = str(voiceover.get("voice_version") or "")
                if expected_version:
                    current_provider_voice = audio_domain.resolve_audio_provider_voice(
                        str(username or ""), voiceover["voice"],
                    )
                    current_version = hashlib.sha256(
                        str(current_provider_voice).encode("utf-8"),
                    ).hexdigest()
                    if current_version != expected_version:
                        raise ValueError("配音音色版本已变化，请重新选择")
                generated = audio_domain.gen_audio(
                    {**voiceover, "_username": str(username or "")},
                    publish=False,
                )
            finally:
                _VOICEOVER_SYNTH_LOCK.release()
            source = _owned_output_path(generated.get("file"))
            try:
                size = source.stat().st_size
                if not 0 < size <= MAX_VOICEOVER_BYTES:
                    raise RuntimeError("配音文件大小无效")
                streams, duration = _media_probe(
                    source, timeout=_remaining_budget(deadline_at),
                )
                if not any(item.get("codec_type") == "audio" for item in streams):
                    raise RuntimeError("配音文件缺少音轨")
                os.replace(source, target)
                try:
                    target.chmod(0o600)
                except OSError:
                    pass
                prepared = {"path": target, "duration": duration,
                            "fingerprint": fingerprint}
            finally:
                if source != target:
                    source.unlink(missing_ok=True)
    finally:
        lock.release()
    if not _persist_runtime(
        job_id, phase="voiceover_ready",
        voiceover_duration_ms=int(round(prepared["duration"] * 1000)),
        voiceover_fingerprint=fingerprint,
    ):
        raise RuntimeError("模板成片配音状态保存失败")
    return prepared


def _valid_template_video_codec(stream):
    if stream.get("color_transfer") in {"arib-std-b67", "smpte2084"}:
        return (stream.get("codec_name") == "hevc"
                and stream.get("pix_fmt") == "yuv420p10le"
                and stream.get("color_primaries") == "bt2020"
                and stream.get("color_space") == "bt2020nc")
    return stream.get("codec_name") == "h264"


def _mux_voiceover(
        video_file, voiceover, deadline_at, *, bgm=False,
        bgm_volume=DEFAULT_VOICEOVER_BGM_VOLUME, narration_duration=None):
    video = _owned_output_path(video_file)
    audio = pathlib.Path(voiceover["path"])
    duration = float(voiceover["duration"])
    if narration_duration is not None:
        if not duration <= float(narration_duration) <= duration + .64:
            raise MatrixTemplateProviderFailed("双语配音与视频时长不一致")
        duration = float(narration_duration)
    temporary = video.with_name(video.stem + ".voiceover.part.mp4")
    temporary.unlink(missing_ok=True)
    source_streams, source_duration = _media_probe(video, timeout=_remaining_budget(deadline_at))
    if narration_duration is not None and abs(source_duration-duration) > .04:
        raise MatrixTemplateProviderFailed("双语成片未按配音时间轴渲染，禁止循环或截短")
    source_video = next((s for s in source_streams if s.get("codec_type") == "video"), {})
    if not _valid_template_video_codec(source_video):
        raise MatrixTemplateProviderFailed("模板成片视频色彩格式无效")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        *([] if narration_duration is not None else ["-stream_loop", "-1"]),
        "-i", str(video), "-i", str(audio),
    ]
    if bgm:
        volume = _normalize_bgm_volume(bgm_volume)
        if not any(item.get("codec_type") == "audio" for item in source_streams):
            raise MatrixTemplateProviderFailed("模板成片背景音乐音轨缺失")
        # Keep audio finite before looping its samples; amix can deadlock when
        # sharing the endlessly looped video demuxer.
        command.extend(["-i", str(video)])
        samples = max(1, int(math.ceil(source_duration * 44100)))
        command.extend([
            "-filter_complex",
            (
                f"[2:a:0]aresample=44100,aloop=loop=-1:size={samples},"
                f"volume={volume:.6f},atrim=start=0:end={duration:.6f},"
                "asetpts=PTS-STARTPTS[bgm];"
                f"[1:a:0]atrim=start=0:end={duration:.6f},"
                "asetpts=PTS-STARTPTS[voice];"
                "[voice][bgm]amix=inputs=2:duration=longest:"
                "dropout_transition=0:normalize=0,"
                "alimiter=limit=0.95:level=false[aout]"
            ),
            "-map", "0:v:0", "-map", "[aout]",
        ])
    else:
        command.extend(["-map", "0:v:0", "-map", "1:a:0"])
        if narration_duration is not None:
            command.extend(["-af", "apad"])
    command.extend([
        "-map_metadata", "-1", "-sn", "-dn", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.6f}",
        "-movflags", "+faststart", str(temporary),
    ])
    try:
        remaining = _remaining_budget(deadline_at)
        from .task_termination import run_process
        run_process(
            command, check=True, capture_output=True,
            timeout=max(1.0, min(float(VOICEOVER_MUX_TIMEOUT), remaining)),
        )
        streams, actual_duration = _media_probe(
            temporary, timeout=_remaining_budget(deadline_at),
        )
        video_streams = [item for item in streams if item.get("codec_type") == "video"]
        audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
        audio_duration = float(audio_streams[0].get("duration") or actual_duration) if len(audio_streams) == 1 else 0
        output_size = temporary.stat().st_size
        if (
            len(video_streams) != 1 or len(audio_streams) != 1
            or not _valid_template_video_codec(video_streams[0])
            or any(video_streams[0].get(key) != source_video.get(key) for key in (
                "codec_name", "pix_fmt", "color_primaries", "color_transfer",
                "color_space", "color_range",
            ))
            or audio_streams[0].get("codec_name") != "aac"
            or (video_streams[0].get("width"), video_streams[0].get("height"))
                != (1080, 1920)
            # Packet copy can retain a few reordered video frames at the tail.
            # Keep narration timing strict, with a bounded container allowance.
            or abs(audio_duration - duration) > 0.12
            or abs(actual_duration - duration) > 0.25
            or not 1024 <= output_size <= MAX_VIDEO_BYTES
        ):
            raise MatrixTemplateProviderFailed("模板成片配音合成校验失败")
        _remaining_budget(deadline_at)
        os.replace(temporary, video)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("模板成片配音合成失败") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return round(actual_duration, 3), output_size


def _provider_payload(payload):
    return {
        key: value for key, value in payload.items()
        if not str(key).startswith("_") and key not in {"voiceover", "bgm_volume"}
    }


def _set_response_timeout(response, timeout):
    """Apply the current absolute budget to urllib's underlying socket."""
    stream = getattr(response, "fp", None)
    raw = getattr(stream, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is not None and hasattr(sock, "settimeout"):
        sock.settimeout(max(0.001, float(timeout)))


def _download(value, job_id, timeout=240, deadline_at=None):
    if deadline_at is None:
        deadline_at = time.time() + float(timeout)
    url = _safe_file_url(value)
    relative = pathlib.Path("video") / ("matrix_template_%s.mp4" % str(job_id)[:64])
    target = OUT_DIR / relative
    temporary = target.with_suffix(".mp4.part")
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + API_TOKEN})
    total = 0
    try:
        open_timeout = min(float(timeout), _remaining_budget(deadline_at))
        with _NO_PROXY.open(request, timeout=max(0.001, open_timeout)) as response, temporary.open("wb") as handle:
            read_chunk = getattr(response, "read1", None)
            if not callable(read_chunk):
                read_chunk = response.read
            # 终止检查（task_termination.check）每次都会**新开一个数据库连接**再关掉。
            # 原来每读一块（64KB）就调两次 —— 一个 33MB 的成片要开 1000+ 次连接；
            # 多条同时交付时数据库锁争抢会把这个循环整个拖慢：实测交付从 20 秒涨到
            # 200+ 秒，中转器侧日志是「读盘 0.01s、发送 200s」——不是文件或中转器慢，
            # 是读的这端卡住了，把中转器的发送也拖住（2026-09-12 定位）。
            # 终止检查不需要那么密，每秒一次足够；socket 超时仍然每块更新（不碰数据库）。
            checked_at = 0.0
            while True:
                now = time.time()
                if now - checked_at >= 1.0:
                    _remaining_budget(deadline_at)
                    checked_at = now
                remaining = min(float(timeout), deadline_at - now)
                if remaining <= 0:
                    raise RuntimeError("模板成片生成超时")
                _set_response_timeout(response, remaining)
                try:
                    chunk = read_chunk(64 * 1024)
                except (TimeoutError, OSError) as exc:
                    try:
                        _remaining_budget(deadline_at)
                    except RuntimeError as deadline_error:
                        raise deadline_error from exc
                    raise
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise RuntimeError("模板成片文件超过大小限制")
                handle.write(chunk)
        _remaining_budget(deadline_at)
        with temporary.open("rb") as handle:
            if total < 1024 or b"ftyp" not in handle.read(64):
                raise RuntimeError("模板成片文件无效")
        _remaining_budget(deadline_at)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return relative.as_posix(), total


def _runtime(job_id, kind=FEATURE_KEY):
    """Read the durable local lifecycle anchor for one matrix job."""
    from .core import jdb
    try:
        numeric_id = int(job_id)
    except (TypeError, ValueError):
        return {
            "created_at": int(time.time()), "payload": {},
            "trusted_execution": False,
        }
    try:
        with closing(jdb()) as connection:
            row = connection.execute(
                "SELECT created_at,payload FROM jobs WHERE id=? AND kind=?",
                (numeric_id, kind),
            ).fetchone()
    except Exception:
        return {
            "created_at": int(time.time()), "payload": {},
            "trusted_execution": False,
        }
    if not row:
        return {
            "created_at": int(time.time()), "payload": {},
            "trusted_execution": False,
        }
    trusted_execution = True
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
        trusted_execution = False
    if not isinstance(payload, dict):
        payload = {}
        trusted_execution = False
    return {
        "created_at": int(row["created_at"] or time.time()),
        "payload": payload,
        "trusted_execution": trusted_execution,
    }


def _execution_payload(value):
    if not isinstance(value, dict):
        return {}
    return {
        key: item for key, item in value.items()
        if not str(key).startswith("_")
    }


def _matches_trusted_execution(raw, lifecycle):
    stored = lifecycle.get("payload")
    return (
        lifecycle.get("trusted_execution") is True
        and isinstance(stored, dict)
        and _execution_payload(raw) == _execution_payload(stored)
    )


def _durable_runtime(job_id, kind=FEATURE_KEY):
    """Read recovery state without turning a database fault into no state."""
    from .core import jdb
    numeric_id = int(job_id)
    with closing(jdb()) as connection:
        row = connection.execute(
            "SELECT created_at,payload FROM jobs WHERE id=? AND kind=?",
            (numeric_id, kind),
        ).fetchone()
    if not row:
        raise RuntimeError("模板成片生命周期记录不存在")
    payload = json.loads(row["payload"] or "{}")
    if not isinstance(payload, dict):
        raise RuntimeError("模板成片生命周期记录无效")
    return {
        "created_at": int(row["created_at"] or time.time()),
        "payload": payload,
    }


def _persist_runtime(job_id, *, kind=FEATURE_KEY, **updates):
    """Persist provider identity/progress without changing the job state."""
    from .core import jdb
    try:
        numeric_id = int(job_id)
    except (TypeError, ValueError):
        return False
    now = int(time.time())
    try:
        with closing(jdb()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM jobs WHERE id=? AND kind=? "
                "AND status IN ('pending','running')",
                (numeric_id, kind),
            ).fetchone()
            if not row:
                connection.rollback()
                return False
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            runtime = payload.get("_matrix_runtime")
            if not isinstance(runtime, dict):
                runtime = {}
            runtime.update({key: value for key, value in updates.items() if value is not None})
            runtime["last_progress_at"] = now
            payload["_matrix_runtime"] = runtime
            changed = connection.execute(
                "UPDATE jobs SET payload=? WHERE id=? AND kind=? "
                "AND status IN ('pending','running')",
                (json.dumps(payload, ensure_ascii=False), numeric_id, kind),
            )
            connection.commit()
    except Exception:
        return False
    return changed.rowcount == 1


def public_lifecycle(row, now=None):
    """Return server-timed, non-sensitive lifecycle data for the owner."""
    now = int(time.time() if now is None else now)
    created_at = int(row["created_at"] or now)
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    runtime = payload.get("_matrix_runtime") if isinstance(payload, dict) else {}
    if not isinstance(runtime, dict):
        runtime = {}
    status = str(row["status"] or "")
    phase = str(runtime.get("phase") or (
        "queued" if status == "pending" else "starting"
    ))
    return {
        "phase": phase,
        "deadline_at": created_at + TOTAL_TIMEOUT,
        "elapsed_seconds": max(0, now - created_at),
        "last_progress_at": int(runtime.get("last_progress_at") or created_at),
        "provider_submitted": bool(runtime.get("provider_job_id")),
    }


def recover_worker_error(job_id, error, requeue=None):
    """Keep paid remote work recoverable until failure or expiry is certain."""
    lifecycle = _durable_runtime(job_id)
    deadline_at = int(lifecycle["created_at"]) + TOTAL_TIMEOUT
    if time.time() >= deadline_at or isinstance(
            error, MatrixTemplateProviderFailed):
        return False
    payload = lifecycle["payload"]
    runtime = payload.get("_matrix_runtime")
    if not isinstance(runtime, dict):
        return False
    provider_job_id = str(runtime.get("provider_job_id") or "")
    if provider_job_id:
        if not re.fullmatch(r"[0-9a-f]{32}", provider_job_id):
            raise RuntimeError("模板成片恢复信息无效")
        _persist_runtime(
            job_id, phase="provider_retrying", provider_status="unknown",
            last_error=str(error)[:300],
        )
        if requeue:
            requeue(job_id)
        return True
    phase = str(runtime.get("phase") or "")
    if phase in {"submitting", "submission_unknown"}:
        if isinstance(error, MatrixTemplateHTTPError) and error.status in {
                400, 401, 403, 404, 422}:
            return False
        _persist_runtime(
            job_id, phase="submission_unknown", provider_status="unknown",
            last_error=str(error)[:300], deadline_at=deadline_at,
        )
        return True
    return False


def generate(payload):
    from . import task_termination
    raw = dict(payload or {})
    job_id=raw.get('_job_id')
    if not str(job_id or '').isdigit() or raw.get('mode')=='timeline':
        return _generate(payload)
    from .core import jdb
    with task_termination.scope(int(job_id),jdb):
        return _generate(payload)


def _generate(payload):
    raw = dict(payload or {})
    if raw.get("mode") == "timeline":
        from . import timeline_compose
        return timeline_compose.generate(raw)
    local_job = str(raw.get("_job_id") or uuid.uuid4().hex)
    lifecycle = _runtime(local_job)
    deadline_at = int(lifecycle["created_at"]) + TOTAL_TIMEOUT
    _remaining_budget(deadline_at, "模板成片等待超时")
    stored_payload = lifecycle.get("payload")
    if not isinstance(stored_payload, dict):
        stored_payload = {}
    trusted_frozen_execution = _matches_trusted_execution(raw, lifecycle)
    runtime = stored_payload.get("_matrix_runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    remote_id = str(runtime.get("provider_job_id") or "")
    if remote_id and not re.fullmatch(r"[0-9a-f]{32}", remote_id):
        raise RuntimeError("模板成片恢复信息无效")

    if remote_id:
        payload = {
            key: value for key, value in stored_payload.items()
            if not str(key).startswith("_")
        }
    else:
        phase = str(runtime.get("phase") or "")
        legacy_exact_replay = (
            phase in {"submitting", "submission_unknown"}
            and "semantic_layout" not in stored_payload
        )
        text_style_exact_replay = (
            trusted_frozen_execution and stored_payload.get("text_overrides")
            and phase in {"submitting", "submission_unknown"}
        )
        if legacy_exact_replay or text_style_exact_replay:
            payload = {
                key: value for key, value in stored_payload.items()
                if not str(key).startswith("_")
            }
        else:
            validation_input = (
                _execution_payload(stored_payload)
                if trusted_frozen_execution else raw
            )
            payload = validate_payload(
                validation_input, str(raw.get("_username") or ""),
                trusted_semantic_layout=stored_payload.get("semantic_layout"),
                trusted_frozen_execution=trusted_frozen_execution,
                allow_shared_materials=(
                    None if "material_policy" not in stored_payload else
                    stored_payload.get("material_policy") == MATERIAL_POLICY_SHARED
                ),
                public_only_materials=(
                    stored_payload.get("material_scope")
                    == MATERIAL_SCOPE_PUBLIC_ONLY
                ),
            )
    voiceover = payload.get("voiceover")
    voiceover_audio = _prepare_voiceover_audio(
        local_job, str(raw.get("_username") or ""), voiceover,
        payload.get("batch_id"), deadline_at,
    )
    bilingual_plan = None
    if payload["template_id"] == BILINGUAL_TEMPLATE_ID:
        from . import matrix_bilingual
        from . import matrix_template_account_media as account_media
        if not voiceover_audio:
            raise RuntimeError("双语模板配音缺失")
        fingerprint = hashlib.sha256(voiceover_audio["path"].read_bytes()).hexdigest()
        bilingual_plan = runtime.get("bilingual_plan")
        bilingual_materials = runtime.get("bilingual_materials")
        if bilingual_plan is not None and bilingual_plan.get("audio_fingerprint") != fingerprint:
            raise RuntimeError("配音缓存已变化，禁止重复提交不同的字幕时间轴")
        if bilingual_plan is None:
            if remote_id or runtime.get("phase") in {"submitting", "submission_unknown"}:
                raise RuntimeError("双语任务缺少冻结的字幕时间轴，不能重新生成")
            bilingual_plan = matrix_bilingual.prepare(voiceover_audio,voiceover["text"],deadline_at)
            candidates = payload.get("user_materials") or []
            durations = stored_payload.get("_matrix_material_durations", payload.get("_matrix_material_durations"))
            automatic = stored_payload.get("_matrix_auto_materials", payload.get("_matrix_auto_materials", False))
            bilingual_materials = account_media.bilingual(candidates, durations, bilingual_plan["duration"], explicit=not automatic)
            bilingual_plan = dict(bilingual_plan, visual_count=len(bilingual_materials))
            if not _persist_runtime(local_job,phase="bilingual_ready",bilingual_plan=bilingual_plan,bilingual_materials=bilingual_materials):
                raise RuntimeError("双语字幕时间轴保存失败")
        if not bilingual_materials or len(bilingual_materials) != bilingual_plan.get("visual_count"):
            raise RuntimeError("双语任务缺少冻结的素材，禁止重复选择后提交")
        payload = dict(payload,narration_plan=bilingual_plan,duration=bilingual_plan["duration"],user_materials=bilingual_materials)
    if not remote_id:
        _remaining_budget(deadline_at)
        request_id = "matrix-template-" + re.sub(
            r"[^A-Za-z0-9_.:-]", "-", local_job
        )[:80]
        if not _persist_runtime(
            local_job, phase="submitting", deadline_at=deadline_at,
        ):
            raise RuntimeError("模板成片生命周期状态保存失败")
        remote = _request(
            "POST", "/v1/jobs", _provider_payload(payload),
            request_id=request_id,
            timeout=min(20, _remaining_budget(deadline_at)),
        )
        from .task_termination import provider_submitted
        provider_submitted(remote.get('job_id'))
        _remaining_budget(deadline_at)
        remote_id = str(remote.get("job_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", remote_id):
            raise RuntimeError("模板成片服务没有返回有效任务 ID")
        _persist_runtime(
            local_job, phase="provider_queued", provider_job_id=remote_id,
            provider_submitted_at=int(time.time()), provider_status="pending",
            deadline_at=deadline_at,
        )
    execution_deadline = min(time.monotonic() + JOB_TIMEOUT, time.monotonic() + max(
        0, deadline_at - time.time()
    ))
    last_status = ""
    while time.monotonic() < execution_deadline and time.time() < deadline_at:
        current = _request(
            "GET", "/v1/jobs/" + remote_id,
            timeout=min(20, _remaining_budget(deadline_at)),
        )
        _remaining_budget(deadline_at)
        status = str(current.get("status") or "")
        if status != last_status:
            _persist_runtime(
                local_job,
                phase="rendering" if status == "running" else "provider_queued",
                provider_status=status or "unknown",
            )
            last_status = status
        if status == "completed":
            result = current.get("result") or {}
            if payload.get("text_overrides") and (result.get("text_revision") != payload.get("text_revision")
                    or result.get("text_overrides") != payload["text_overrides"]):
                raise MatrixTemplateProviderFailed("生成结果没有匹配的文字微调参数，已阻止交付默认样式视频")
            _persist_runtime(local_job, phase="delivering", provider_status=status)
            remaining = deadline_at - time.time()
            if remaining <= 0:
                raise RuntimeError("模板成片生成超时")
            video_file, file_size = _download(
                result.get("file_url"), local_job, timeout=min(240, remaining),
                deadline_at=deadline_at,
            )
            final_duration = float(result.get("duration") or 0)
            if voiceover_audio:
                if not _persist_runtime(local_job, phase="muxing_voiceover"):
                    raise RuntimeError("模板成片配音状态保存失败")
                final_duration, file_size = _mux_voiceover(
                    video_file, voiceover_audio, deadline_at,
                    bgm=bool(payload.get("bgm")),
                    bgm_volume=payload.get(
                        "bgm_volume", DEFAULT_VOICEOVER_BGM_VOLUME,
                    ),
                    **({"narration_duration":bilingual_plan["duration"]} if bilingual_plan else {}),
                )
            response = {
                "type": "matrix_template_video",
                "mode": "matrix_template",
                "provider": "matrix-template",
                "provider_task_id": remote_id,
                "status": "done",
                "video_file": video_file,
                "video_url": public_url(video_file, "video/mp4", private=True),
                "duration": final_duration,
                "phase": "done",
                "resolution": "1080p",
                "ratio": "9:16",
                "width": int(result.get("width") or 1080),
                "height": int(result.get("height") or 1920),
                "template_id": result.get("template_id") or payload["template_id"],
                "font_selection": result.get("font_selection") or {},
                "font_files": result.get("font_files") or [],
                "file_size": file_size,
                "material_manifest": result.get("material_manifest") or [],
            }
            if isinstance(result.get("color_profile"), dict):
                response["color_profile"] = dict(result["color_profile"])
            if payload.get("text_overrides"):
                response["text_revision"] = result["text_revision"]
                response["text_overrides"] = result["text_overrides"]
            if voiceover:
                response["voiceover"] = {
                    "enabled": True,
                    "voice": voiceover["voice"],
                    "voice_scope": voiceover.get("voice_scope") or "",
                    "duration_ms": int(round(final_duration * 1000)),
                    "bgm": bool(payload.get("bgm")),
                }
                if payload.get("bgm"):
                    response["voiceover"]["bgm_volume"] = float(payload.get(
                        "bgm_volume", DEFAULT_VOICEOVER_BGM_VOLUME,
                    ))
            return response
        if status == "failed":
            raise MatrixTemplateProviderFailed(
                str(current.get("error") or "模板成片生成失败")[:500]
            )
        from .task_termination import sleep
        sleep(POLL_INTERVAL)
    raise RuntimeError("模板成片生成超时")


def public_preview_lifecycle(row, now=None):
    """Public lifecycle of one preview job (same shape as the video lifecycle)."""
    lifecycle = public_lifecycle(row, now)
    lifecycle["deadline_at"] = int(row["created_at"] or 0) + PREVIEW_TIMEOUT
    return lifecycle


def recover_preview_error(job_id, error, requeue=None):
    """Keep one preview recoverable until its own deadline or a clear failure."""
    # 渲染端已明确失败（如「素材数量不足」）时绝不重试：重试不会变好，只会把
    # 任务永远卡在 retrying，让轮询方（Agent）把步数耗光。与正式任务路径
    # recover_worker_error 的判据保持一致。
    if isinstance(error, MatrixTemplateProviderFailed):
        return False
    try:
        lifecycle = _durable_runtime(job_id, PREVIEW_KIND)
    except Exception:
        return False
    payload = lifecycle["payload"] if isinstance(lifecycle["payload"], dict) else {}
    runtime = payload.get("_matrix_runtime")
    if not isinstance(runtime, dict):
        return False
    deadline_at = int(lifecycle["created_at"]) + PREVIEW_TIMEOUT
    if time.time() >= deadline_at:
        return False
    preview_id = str(runtime.get("preview_id") or "")
    if preview_id and PREVIEW_ID_RE.fullmatch(preview_id):
        if not requeue:
            return False
        _persist_runtime(
            job_id, kind=PREVIEW_KIND, phase="preview_retrying",
            provider_status="unknown",
        )
        requeue(job_id)
        return True
    phase = str(runtime.get("phase") or "")
    if phase in {"submitting", "submission_unknown"}:
        if isinstance(error, MatrixTemplateHTTPError) and error.status in {
                400, 401, 403, 404, 422}:
            return False
        if not requeue:
            return False
        _persist_runtime(
            job_id, kind=PREVIEW_KIND, phase="submission_unknown",
            provider_status="unknown",
        )
        requeue(job_id)
        return True
    return False


_PREVIEW_FILE_LIMIT = 128 * 1024 * 1024


def _download_preview_file(url, relative, *, expect, deadline_at, timeout=180):
    """Stream one preview artifact into OUT_DIR with a magic-bytes check."""
    target = OUT_DIR / relative
    temporary = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        _safe_file_url(url), headers={"Authorization": "Bearer " + API_TOKEN},
    )
    total = 0
    try:
        open_timeout = min(float(timeout), _remaining_budget(deadline_at))
        with _NO_PROXY.open(
                request, timeout=max(0.001, open_timeout)) as response, \
                temporary.open("wb") as handle:
            checked_at = 0.0
            read_chunk = getattr(response, "read1", None)
            if not callable(read_chunk):
                read_chunk = response.read
            while True:
                now = time.time()
                if now - checked_at >= 1.0:
                    # _remaining_budget 每次都会新开数据库连接，按秒检查即可。
                    _remaining_budget(deadline_at)
                    checked_at = now
                remaining = min(float(timeout), deadline_at - now)
                if remaining <= 0:
                    raise RuntimeError("预览生成超时")
                _set_response_timeout(response, remaining)
                chunk = read_chunk(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _PREVIEW_FILE_LIMIT:
                    raise RuntimeError("预览文件超过大小限制")
                handle.write(chunk)
        with temporary.open("rb") as handle:
            head = handle.read(64)
        if expect == "jpeg":
            if total < 256 or not head.startswith(b"\xff\xd8\xff"):
                raise RuntimeError("预览关键帧无效")
        elif total < 1024 or b"ftyp" not in head:
            raise RuntimeError("预览视频无效")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return relative.as_posix(), total


def _preview_bundle(job_id, entry, label, deadline_at):
    """Download one version (MP4 + frames) and return main-site file paths."""
    if not isinstance(entry, dict):
        raise RuntimeError("预览结果缺少对比版本")
    video_url = str(entry.get("video_url") or "")
    if not video_url:
        raise RuntimeError("预览结果缺少视频地址")
    root = pathlib.Path("preview") / re.sub(r"[^A-Za-z0-9_.-]", "_", str(job_id))[:64]
    video_file, _ = _download_preview_file(
        video_url, root / (label + ".mp4"), expect="mp4",
        deadline_at=deadline_at,
    )
    frames = []
    raw_frames = entry.get("frames")
    if isinstance(raw_frames, list):
        for index, frame_url in enumerate(raw_frames[:PREVIEW_FRAME_LIMIT]):
            if not str(frame_url or "").strip():
                continue
            frame_file, _ = _download_preview_file(
                frame_url, root / ("%s_frame_%02d.jpg" % (label, index + 1)),
                expect="jpeg", deadline_at=deadline_at, timeout=60,
            )
            frames.append(frame_file)
    return {
        "video_file": video_file,
        "video_url": public_url(video_file, "video/mp4", private=True),
        "frames": frames,
        "frame_urls": [
            public_url(frame, "image/jpeg", private=True) for frame in frames
        ],
        "frame_count": len(frames),
    }


def _override_changes(overrides):
    """Plain-language summary of the applied parameters (effect values)."""
    if not isinstance(overrides, dict) or not overrides:
        return []
    changes = []
    title_scale = overrides.get("title_scale")
    if isinstance(title_scale, (int, float)) and not isinstance(title_scale, bool) \
            and abs(float(title_scale) - 1.0) > 1e-9:
        changes.append("标题缩放 %.2f×" % float(title_scale))
    cta_scale = overrides.get("cta_scale")
    if isinstance(cta_scale, (int, float)) and not isinstance(cta_scale, bool) \
            and abs(float(cta_scale) - 1.0) > 1e-9:
        changes.append("行动文案缩放 %.2f×" % float(cta_scale))
    for key, label in (("title_offset_y", "标题"), ("cta_offset_y", "行动文案")):
        value = overrides.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value:
            changes.append("%s%s %d px" % (label, "上移" if value < 0 else "下移", abs(value)))
    color = overrides.get("accent_color")
    if isinstance(color, str) and color:
        changes.append("强调色 " + color.upper())
    focuses = overrides.get("media_focus")
    if isinstance(focuses, list) and focuses:
        changes.append(
            "第 %s 画面焦点已移动"
            % "、".join(str(item.get("slot")) for item in focuses
                        if isinstance(item, dict))
        )
    return changes


def preview_payload(payload, username="", *, allow_shared_materials=None,
                    public_only_materials=False, preview_lookup=None):
    """Validate one matrix-template-preview request (single generation only)."""
    if not isinstance(payload, dict):
        raise ValueError("预览参数无效")
    unsupported = sorted(set(payload) - PREVIEW_FIELDS)
    if unsupported:
        label = _PREVIEW_UNSUPPORTED_LABELS.get(unsupported[0], unsupported[0])
        raise ValueError("预览不支持参数：" + label)
    if not str(payload.get("template_revision") or "").strip():
        raise ValueError("预览需要提供 template_revision，请先读取模板可调范围")
    materials = payload.get("user_materials") or []
    non_video = [
        str(item.get("media_type") or "") for item in materials
        if isinstance(item, dict) and str(item.get("media_type") or "") != "video"
    ]
    if non_video:
        # 两版对比预览在渲染服务本机执行，不支持图片转视频（正式出片才由节点
        # 转片）：平台层直接拒绝并给出路，避免渲染端 1 秒失败后被无限重试。
        raise ValueError("两版对比预览需要视频素材：请发 1~3 段视频，或图片直接正式出片（无需预览）")
    return validate_payload(
        payload, username,
        allow_shared_materials=allow_shared_materials,
        public_only_materials=public_only_materials,
        for_preview=True, preview_lookup=preview_lookup,
    )


def generate_preview(payload):
    """Private worker entry for matrix_template_preview (never a public kind)."""
    from . import task_termination
    raw = dict(payload or {})
    job_id = raw.get("_job_id")
    if not str(job_id or "").isdigit():
        return _generate_preview(payload)
    from .core import jdb
    with task_termination.scope(int(job_id), jdb):
        return _generate_preview(payload)


def _generate_preview(payload):
    from . import matrix_template_submission
    raw = dict(payload or {})
    local_job = str(raw.get("_job_id") or uuid.uuid4().hex)
    username = str(raw.get("_username") or "")
    frozen = {
        key: value for key, value in raw.items()
        if not str(key).startswith("_")
    }
    lifecycle = _runtime(local_job, PREVIEW_KIND)
    deadline_at = int(lifecycle["created_at"]) + PREVIEW_TIMEOUT
    _remaining_budget(deadline_at, "预览生成超时")
    stored = lifecycle.get("payload")
    runtime = (
        stored.get("_matrix_runtime") if isinstance(stored, dict) else None
    )
    if not isinstance(runtime, dict):
        runtime = {}
    preview_id = str(runtime.get("preview_id") or "")
    if preview_id and not PREVIEW_ID_RE.fullmatch(preview_id):
        raise RuntimeError("预览恢复信息无效")
    if not preview_id:
        if not _persist_runtime(
                local_job, kind=PREVIEW_KIND, phase="submitting",
                deadline_at=deadline_at):
            raise RuntimeError("预览生命周期状态保存失败")
        request_id = "matrix-template-preview-" + re.sub(
            r"[^A-Za-z0-9_.:-]", "-", local_job,
        )[:64]
        remote = _request(
            "POST", "/v1/preview-jobs", _provider_payload(frozen),
            request_id=request_id,
            timeout=min(20, _remaining_budget(deadline_at)),
        )
        preview_id = str(remote.get("preview_id") or "")
        if not PREVIEW_ID_RE.fullmatch(preview_id):
            raise RuntimeError("预览服务没有返回有效预览标识")
        from .task_termination import provider_submitted
        provider_submitted(preview_id)
        _remaining_budget(deadline_at)
        _persist_runtime(
            local_job, kind=PREVIEW_KIND, phase="preview_queued",
            preview_id=preview_id, provider_status="queued",
        )
    execution_deadline = time.monotonic() + min(
        PREVIEW_TIMEOUT, max(0.0, deadline_at - time.time()),
    )
    last_status = ""
    while time.monotonic() < execution_deadline and time.time() < deadline_at:
        current = _request(
            "GET", "/v1/preview-jobs/" + preview_id,
            timeout=min(20, _remaining_budget(deadline_at)),
        )
        _remaining_budget(deadline_at)
        status = str(current.get("status") or "")
        if status != last_status:
            _persist_runtime(
                local_job, kind=PREVIEW_KIND,
                phase="preview_rendering" if status == "rendering" else "preview_queued",
                preview_id=preview_id, provider_status=status or "unknown",
            )
            last_status = status
        if status == "ready":
            return _preview_result(
                local_job, username, frozen, current, preview_id, deadline_at,
            )
        if status in {"failed", "expired"}:
            reason = str(
                current.get("error") or current.get("reason")
                or "预览生成失败"
            )[:300]
            # 渲染端明确失败必须抛 ProviderFailed：recover_preview_error 只对
            # 这类错误立即终态不重试；抛普通 ValueError 会被无限 requeue 到
            # 15 分钟超时（2026-09-21 生产事故 9791 复现）。与正式任务路径一致。
            raise MatrixTemplateProviderFailed("预览未生成：" + reason)
        from .task_termination import sleep
        sleep(POLL_INTERVAL)
    raise RuntimeError("预览生成超时")


def _preview_result(job_id, username, frozen, current, preview_id, deadline_at):
    from . import matrix_template_submission
    _persist_runtime(job_id, kind=PREVIEW_KIND, phase="preview_delivering")
    resources = current.get("resources")
    if not isinstance(resources, dict):
        raise RuntimeError("预览结果不完整")
    default = _preview_bundle(
        job_id, resources.get("default"), "default", deadline_at,
    )
    candidate = _preview_bundle(
        job_id, resources.get("candidate"), "candidate", deadline_at,
    )
    prepared = current.get("prepared")
    prepared = prepared if isinstance(prepared, dict) else {}
    prepared_digest = str(prepared.get("digest") or "")
    expires_at = int(current.get("expires_at") or 0)
    if expires_at <= int(time.time()):
        expires_at = int(time.time()) + PREVIEW_DEFAULT_TTL_SECONDS
    echoed = current.get("effective_overrides")
    sent_overrides = frozen.get("overrides") if isinstance(
        frozen.get("overrides"), dict) else {}
    if not isinstance(echoed, dict):
        echoed = current.get("overrides")
    if isinstance(echoed, dict) and not _overrides_agree(sent_overrides, echoed):
        raise RuntimeError("预览回显参数不一致")
    effective_overrides = _effective_overrides(
        echoed if isinstance(echoed, dict) else {}, sent_overrides,
    )
    template_id = str(
        frozen.get("template_id") or current.get("template_id") or ""
    )
    template_revision = str(
        current.get("template_revision") or frozen.get("template_revision") or ""
    ).strip().lower()
    if not template_revision:
        raise RuntimeError("预览结果缺少模板版本")
    fingerprint = preview_input_fingerprint(frozen)
    stored = matrix_template_submission.record_preview(
        _jobs_db(), username=username, preview_id=preview_id,
        job_id=job_id if str(job_id).isdigit() else None,
        template_id=template_id, template_revision=template_revision,
        fingerprint=fingerprint, overrides=sent_overrides,
        effective_overrides=effective_overrides,
        materials=frozen.get("user_materials") or [],
        prepared_digest=prepared_digest, expires_at=expires_at,
    )
    matrix_template_submission.prune_previews(_jobs_db())
    response = {
        "type": "matrix_template_preview",
        "status": "done",
        "phase": "done",
        "preview_id": preview_id,
        "template_id": template_id,
        "template_revision": template_revision,
        "overrides": sent_overrides,
        "effective_overrides": effective_overrides,
        "changes": _override_changes(effective_overrides),
        "default": default,
        "candidate": candidate,
        "checks": current.get("checks") if isinstance(
            current.get("checks"), dict) else {},
        "prepared": {
            "digest": prepared_digest,
            "valid_until": int(prepared.get("valid_until") or expires_at),
        },
        "expires_at": expires_at,
        "record_expires_at": int(stored.get("expires_at") or expires_at),
        "font_selection": {},
    }
    return response


def _jobs_db():
    from .core import jdb
    return jdb


def cost(payload):
    if isinstance(payload, dict) and payload.get("mode") == "timeline":
        from . import timeline_compose
        return timeline_compose.cost(payload)
    return pricing.get_price("video.matrix_template")


HANDLERS = {"matrix_template_video": generate}
# 预览处理器不进 HANDLERS：registry 的公开列表决定 /api/gen/<kind> 路由与 /health caps，
# 五条测试锁定了这份清单，预览也必须没有公开直提路由（只能走 /api/gen/matrix-template/preview）。
PRIVATE_HANDLERS = {PREVIEW_KIND: generate_preview}
