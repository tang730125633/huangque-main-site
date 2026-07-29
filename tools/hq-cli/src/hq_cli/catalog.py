"""Fixed, public, read-only capability contract for HQ CLI V0.1."""

from urllib.parse import urlencode

ENVIRONMENTS = {
    "main": "https://huangquechuanmei.com",
    "zelong": "https://zelong.huangquechuanmei.com",
}

IMAGE_FIELDS = {
    "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
    "engine": {"type": "string", "enum": ["gpt", "banana", "nb2", "pro", "seedream", "xiaole", "zelong2"]},
}
VIDEO_FIELDS = {
    "prompt": {"type": "string", "minLength": 1, "maxLength": 1000},
    "channel": {"type": "string", "enum": ["grok", "micro", "omni"]},
}
AUDIO_FIELDS = {
    "prompt": {"type": "string", "minLength": 1, "maxLength": 1000},
    "speed": {"type": "number", "minimum": 0.5, "maximum": 2},
    "pitch": {"type": "integer", "minimum": -12, "maximum": 12},
    "volume": {"type": "integer", "minimum": -50, "maximum": 100},
}
ASSET_CATEGORIES = [
    "all", "image", "audio", "video", "avatar", "copy", "collect", "leads",
    "text2img", "img2img", "inpaint",
]


def _input_schema(properties=None, required=None):
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required or [],
        "properties": properties or {},
    }


RUN_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema", "cli_version", "url", "opened_browser", "next_actions"],
    "properties": {
        "schema": {"type": "string", "const": "hq.run/v1"},
        "cli_version": {"type": "string"},
        "url": {"type": "string"},
        "opened_browser": {"type": "boolean"},
        "next_actions": {"type": "array", "items": {"type": "string"}},
    },
}


def _navigation(identifier, name, path, description, fields=None, target_auth="account_for_actions"):
    return {
        "id": identifier,
        "name": name,
        "kind": "navigation",
        "availability": "available",
        "runnable": True,
        "description": description,
        "input_schema": _input_schema(fields),
        "output_schema": RUN_OUTPUT_SCHEMA,
        "requires_auth": False,
        "target_auth": target_auth,
        "side_effect": "navigation",
        "confirmation_required": False,
        "cost": {"kind": "none", "detail": "CLI navigation does not spend points or invoke product APIs."},
        "deep_link": {"path": path, "query_fields": sorted((fields or {}).keys())},
        "next_actions": ["Open the returned URL in a signed-in browser when the page requires an account."],
    }


_NAVIGATION = (
    ("login", "登录", "/login", "进入黄雀登录页。", None, "none"),
    ("inspiration", "灵感", "/workbench/inspiration", "浏览灵感设计工作台。", None, "none"),
    ("leads", "获客", "/workbench/leads", "进入平台获客工作台。", None, "account_for_data_or_actions"),
    ("collect", "采集", "/workbench/collect", "进入内容采集工作台。", None, "account_for_data_or_actions"),
    ("image", "图片", "/workbench/banana", "进入图片工作台；仅安全预填，不提交生成。", IMAGE_FIELDS, "account_for_actions"),
    ("video", "视频", "/workbench/video", "进入视频工作台；仅安全预填，不提交生成。", VIDEO_FIELDS, "account_for_actions"),
    ("audio", "音频", "/workbench/audio", "进入音频工作台；仅安全预填，不提交生成。", AUDIO_FIELDS, "account_for_actions"),
    ("script", "文案", "/workbench/script", "进入文案编导工作台。", None, "account_for_actions"),
    ("canvas", "画布", "/workbench/canvas", "进入创作画布；V0.1 不接受 board、collab 或 task 参数。", None, "account_for_actions"),
    ("assets", "资产", "/workbench/assets", "进入我的资产。", {"cat": {"type": "string", "enum": ASSET_CATEGORIES}}, "account_for_data"),
    ("invite", "邀请", "/workbench/invite", "进入邀请中心。", None, "account_for_data"),
    ("tutorials", "教程", "/workbench/tutorials", "进入教程视频。", None, "none"),
    ("settings", "设置", "/workbench/settings", "进入通用设置。", None, "account_for_data_or_actions"),
)

CAPABILITIES = {
    identifier: _navigation(identifier, name, path, description, fields, target_auth)
    for identifier, name, path, description, fields, target_auth in _NAVIGATION
}
CAPABILITIES["health"] = {
    "id": "health",
    "name": "健康说明",
    "kind": "read",
    "availability": "available",
    "runnable": True,
    "description": "返回固定匿名健康端点；doctor 才会进行固定官方 GET。",
    "input_schema": _input_schema(),
    "output_schema": RUN_OUTPUT_SCHEMA,
    "requires_auth": False,
    "target_auth": "none",
    "side_effect": "read",
    "confirmation_required": False,
    "cost": {"kind": "none"},
    "deep_link": {"path": "/api/gen/health", "query_fields": []},
    "next_actions": ["Run `hq doctor --environment <main|zelong>` for fixed anonymous health checks."],
}


def _planned(identifier, name, description, fields=None, required=None, cost=None, side_effect="paid"):
    return {
        "id": identifier,
        "name": name,
        "kind": "planned_auth",
        "availability": "planned_auth",
        "runnable": False,
        "description": description,
        "input_schema": _input_schema(fields, required),
        "output_schema": {"type": "object", "description": "V0.1 run rejects this capability with hq.error/v1."},
        "requires_auth": True,
        "side_effect": side_effect,
        "confirmation_required": True,
        "cost": cost or {"kind": "may_charge", "unit": "points"},
        "deep_link": None,
        "next_actions": ["This V0.1 CLI intentionally cannot run this account-bound action."],
    }


CAPABILITIES["image-generate"] = _planned(
    "image-generate", "图片生成", "需要登录、点数和账户授权；V0.1 仅公开 schema，不调用生成 API。",
    IMAGE_FIELDS, ["prompt", "engine"],
)
CAPABILITIES["video-generate"] = _planned(
    "video-generate", "视频生成", "需要登录、点数和账户授权；V0.1 不提交视频任务。",
    VIDEO_FIELDS, ["prompt", "channel"],
)
CAPABILITIES["audio-generate"] = _planned(
    "audio-generate", "音频生成", "需要登录、点数和账户授权；V0.1 不提交音频任务。",
    AUDIO_FIELDS, ["prompt"],
)
CAPABILITIES["ip12"] = _planned(
    "ip12", "IP12", "项目页可能创建项目，且公网入口依赖 Hermes 与登录；V0.1 禁止直接打开。",
    cost={"kind": "requires_product_review"},
    side_effect="write",
)
CAPABILITIES["ip12-report"] = _planned(
    "ip12-report", "IP12 报告", "报告需要项目权限；V0.1 禁止传入或打开 project 链接。",
    cost={"kind": "requires_product_review"},
    side_effect="external",
)


def capability_list():
    """Return the complete public contract in a single discovery response."""
    return list(CAPABILITIES.values())


def resolve_url(capability, environment, payload):
    """Resolve only a fixed official origin and declared, encoded query fields."""
    url = ENVIRONMENTS[environment] + capability["deep_link"]["path"]
    if payload:
        url += "?" + urlencode(payload)
    return url
