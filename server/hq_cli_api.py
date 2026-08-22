"""Scoped device authorization and fixed action plans for the Huangque CLI."""

import base64
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


PUBLIC_ORIGIN = os.environ.get("HQ_CLI_PUBLIC_ORIGIN", "https://huangquechuanmei.com").strip().rstrip("/")
DEVICE_TTL = 10 * 60
TOKEN_TTL = 8 * 60 * 60
POLL_INTERVAL = 3
BRIDGE_TOKEN_TTL = 60
QUOTE_TTL = 5 * 60
ACTION_REQUEST_TTL = 30 * 24 * 60 * 60
ACTION_INFLIGHT_TTL = 10 * 60
CLI_CHAT_REQUESTS_PER_MINUTE = 6
CONTENT_BASE = "http://127.0.0.1:8096"
LEADGEN_BASE = "http://127.0.0.1:8100"
IMGGEN_BASE = "http://127.0.0.1:8101"
HERMES_BASE = "http://127.0.0.1:3102"
ADMIN_BASE = "http://127.0.0.1:8098"

SCOPES = {
    "profile:read": "读取账号公开资料与点数",
    "ip12:read": "读取本人 IP12 项目与报告",
    "ip12:write": "创建本人 IP12 项目",
    "ip12:chat": "向本人 IP12 项目提交回答并调用 AI 教练",
    "prompt:optimize": "把提示词发送给黄雀 AI 优化",
    "canvas:read": "读取本人可访问的画布",
    "canvas:write": "创建本人画布",
    "canvas:agent": "把画布快照发送给 AI 生成可确认的操作方案",
    "canvas:edit": "经确认后编辑本人有编辑权限的画布",
    "assets:upload": "上传本人生成所需的临时参考图",
    "tasks:read": "读取本人任务状态与点数流水",
    "assets:read": "读取本人资产与音色",
    "assets:write": "收藏资产并管理本人资产标签",
    "inspiration:read": "读取灵感案例与本人收藏状态",
    "inspiration:write": "经确认后收藏或取消收藏灵感案例",
    "leads:read": "读取本人线索跟进记录",
    "leads:write": "经确认后更新本人线索跟进记录",
    "short-drama:read": "读取本人短剧项目与生产准备状态",
    "generation:quote": "查询生成、采集或获客任务所需点数",
    "generation:submit": "经二次确认后提交付费任务并扣点",
    "video-compose:read": "读取本人一键成片项目",
    "video-compose:write": "经确认后创建、分析、审核或渲染本人一键成片项目",
    "digital-presenter:read": "读取本人画布中的数字人口播项目",
    "digital-presenter:write": "经确认后创建或更新本人画布中的数字人口播项目",
}
DEFAULT_SCOPES = tuple(SCOPES)
CHANNEL_CATALOG = (
    {"id": "xai", "provider": "xAI API", "category": "视频生成", "features": ["果肉视频生成"],
     "access": "direct", "capabilities": ["video-generate"], "selector": {"channel": "grok"}},
    {"id": "openai", "provider": "OpenAI API", "category": "图片 / 视频", "features": ["黄雀引擎 2", "Sora 2"],
     "access": "mixed", "capabilities": ["image-generate", "video-generate"], "selector": {},
     "selectors": [{"capability": "image-generate", "input": {"provider": "openai"}},
                   {"capability": "video-generate", "input": {"channel": "sora"}}]},
    {"id": "gemini", "provider": "Google Gemini API", "category": "图片 / 视频", "features": ["纳米香蕉", "Omni 视频"],
     "access": "mixed", "capabilities": ["image-generate", "video-generate"], "selector": {},
     "selectors": [{"capability": "image-generate", "input": {"provider": "banana"}},
                   {"capability": "video-generate", "input": {"channel": "omni"}}]},
    {"id": "seedance", "provider": "火山方舟 API", "category": "图片 / 视频", "features": ["Seedream", "Seedance 视频"],
     "access": "direct", "capabilities": ["image-generate", "video-generate"], "selector": {"provider": "seedream", "channel": "micro"}},
    {"id": "minimax", "provider": "MetaSo MiniMax API", "category": "视频生成", "features": ["麦克视频"],
     "access": "direct", "capabilities": ["video-generate"], "selector": {"channel": "minimax"}},
    {"id": "zelong", "provider": "小乐 AI API", "category": "图片生成", "features": ["黄雀引擎 2 备用线路"],
     "access": "routed", "capabilities": ["image-generate"], "selector": {"provider": "xiaole"}},
    {"id": "zelong2", "provider": "泽龙 API", "category": "图片生成", "features": ["泽龙 2 备用线路（维护中）"],
     "access": "registered", "capabilities": [], "selector": {}},
    {"id": "heygen", "provider": "HeyGen API", "category": "数字化 IP / 视频", "features": ["电影化身", "数字人口播", "数字人形象"],
     "access": "managed", "capabilities": [
         "digital-presenter-capability", "digital-presenter-create",
         "digital-ip-text-generate", "digital-ip-batch-generate", "digital-ip-audio-generate",
         "cinematic-open-generate", "cinematic-motion-generate",
     ], "selector": {}},
    {"id": "heygen_relay", "provider": "HeyGen 中转 API", "category": "数字化 IP / 视频", "features": ["中转与下载兜底"],
     "access": "routed", "capabilities": ["tasks", "assets"], "selector": {}},
    {"id": "xiaolevideo", "provider": "小乐视频 API", "category": "图片 / 视频", "features": ["果肉生图", "历史兼容线路"],
     "access": "routed", "capabilities": ["image-generate", "video-generate"], "selector": {}},
    {"id": "runninghub", "provider": "RunningHub API", "category": "视频处理", "features": ["换装换背景 · 线路一"],
     "access": "managed", "capabilities": ["tryon-classic-generate"], "selector": {}},
    {"id": "wavespeed", "provider": "WaveSpeed API", "category": "视频处理", "features": ["换装换背景 · 线路二", "Seedance AI 超清"],
     "access": "managed", "capabilities": ["tryon-fast-generate"], "selector": {}},
    {"id": "cosyvoice", "provider": "阿里百炼 API", "category": "音频生成", "features": ["公共音色", "声音克隆"],
     "access": "direct", "capabilities": ["voices", "audio-generate"], "selector": {}},
    {"id": "tikhub", "provider": "TikHub API", "category": "内容采集 / 获客", "features": ["抖音 / 小红书 / 视频号", "评论与线索"],
     "access": "mixed", "capabilities": [
         "collect", "collect-content", "collect-video", "collect-transcript", "collect-search",
         "leads", "leads-generate",
     ], "selector": {}},
    {"id": "zhipu", "provider": "智谱视觉 API", "category": "内容分析", "features": ["链接分镜拆解"],
     "access": "managed", "capabilities": [], "selector": {}},
    {"id": "cos", "provider": "腾讯云 COS", "category": "基础设施", "features": ["生成结果存储", "参考素材与成片存储"],
     "access": "managed", "capabilities": ["image-upload", "assets"], "selector": {}},
)
CONFIRMATION_ACTIONS = frozenset({
    "ip12-create", "ip12-message", "prompt-optimize", "canvas-create", "canvas-ops",
    "asset-favorite", "asset-tags", "video-compose-create", "video-compose-analyze",
    "video-compose-review", "video-compose-render", "digital-presenter-create",
    "digital-presenter-update",
    "inspiration-like", "leads-crm-upsert",
})

# This is the public contract shared by the CLI, the first-party HTTP bridge,
# and IP12.  `action_plan` remains the only validator and route builder; the
# catalog deliberately describes inputs without exposing private upstream URLs
# or provider credentials.
_ACTION_INPUTS = {
    "account": (), "channels": (), "pricing": (),
    "text-video-capability": (), "text-video-templates": (),
    "text-video-styles": (), "text-video-voices": (),
    "inspiration-catalog": (), "inspiration-likes": (),
    "inspiration-like": ("id", "favorite"),
    "leads-crm": ("lead_ids",), "leads-crm-upsert": ("lead_id", "intent", "follow_status", "follow_note"),
    "collect-content": ("url",), "collect-video": ("url",), "collect-transcript": ("url",),
    "collect-search": ("platform", "keyword", "page"), "leads-generate": ("url", "platform", "pages", "channels_targets"),
    "video-avatars": ("limit",), "audio-slots": (),
    "short-drama-projects": ("page", "page_size"),
    "short-drama-project": ("project_id",), "short-drama-conversation": ("project_id",),
    "short-drama-preflight": ("project_id",),
    "digital-ip-projects": (), "digital-ip-project": ("project_id",), "digital-ip-report": ("project_id",),
    "ip12-projects": (), "ip12-project": ("project_id",), "ip12-report": ("project_id",),
    "ip12-create": ("title",), "ip12-message": ("project_id", "message", "request_id"),
    "prompt-optimize": ("prompt", "kind"),
    "canvas-list": ("limit", "offset"), "canvas-get": ("board_id",),
    "canvas-create": ("name", "prompt"), "canvas-agent-plan": ("prompt", "project_id", "snapshot_digest", "scope", "nodes", "edges", "selected_node_ids", "history"),
    "canvas-ops": ("board_id", "expected_version", "op_id", "ops"),
    "tasks": ("days", "kind", "page", "page_size"), "task": ("job_id",),
    "assets": ("kind", "limit", "offset"), "voices": (),
    "asset-favorite": ("kind", "key", "favorite"), "asset-tags": ("kind", "key", "tags"),
    "video-compose-projects": (), "video-compose-project": ("project_id",),
    "video-compose-create": ("source_asset_id",),
    "video-compose-analyze": ("project_id", "expected_revision"),
    "video-compose-review": ("project_id", "expected_revision", "decisions"),
    "video-compose-render": ("project_id", "expected_revision"),
    "digital-presenter-capability": (), "digital-presenter-project": ("board_id", "project_id"),
    "digital-presenter-create": ("board_id", "request_id", "title", "script_text", "ratio", "resolution", "voice_key", "target_duration"),
    "digital-presenter-update": ("board_id", "project_id", "revision", "title", "script_text", "ratio", "resolution", "voice_key", "target_duration"),
    "image-generate": ("prompt", "provider", "ratio", "quality", "count", "variant", "model", "image_upload_id", "mask_upload_id", "reference_upload_ids"),
    "video-generate": ("prompt", "channel", "ratio", "duration", "seconds", "resolution", "model", "generate_audio", "reference_upload_ids"),
    "video-lipsync": ("video_asset_id", "audio_asset_id", "quality", "dynamic_duration"),
    "audio-generate": ("text", "voice", "speed", "pitch", "volume"),
    "digital-ip-text-generate": ("avatar_id", "image_upload_id", "text", "voice", "ratio", "motion", "subtitle", "subtitle_style", "subtitle_position"),
    "digital-ip-batch-generate": ("avatars", "text", "voice", "ratio", "motion", "subtitle", "subtitle_style", "subtitle_position"),
    "digital-ip-audio-generate": ("avatar_id", "image_upload_id", "audio_file", "audio_upload_id", "ratio", "motion", "subtitle", "subtitle_style", "subtitle_position"),
    "cinematic-open-generate": ("avatar_id", "avatar_ids", "prompt", "ratio", "duration", "enhance_prompt", "reference_image_upload_ids", "reference_video_upload_ids"),
    "cinematic-motion-generate": ("avatar_id", "reference_video_upload_ids", "ratio"),
    "tryon-fast-generate": ("person_image_upload_id", "clothes_upload_id", "seconds"),
    "tryon-classic-generate": ("person_video_upload_id", "clothes_upload_id", "background_upload_id", "seconds"),
}

_ACTION_PURPOSES = {
    "account": "读取当前黄雀账号与点数", "channels": "读取可用渠道", "pricing": "读取实时价格",
    "ip12-projects": "读取本人 IP12 项目", "ip12-project": "读取本人 IP12 项目详情",
    "ip12-report": "读取本人 IP12 报告", "canvas-list": "读取本人画布", "canvas-get": "读取本人画布详情",
    "tasks": "读取本人任务记录", "task": "读取本人任务详情", "assets": "读取本人资产", "voices": "读取可用音色",
    "image-generate": "生成图片", "video-generate": "生成视频", "video-lipsync": "让本人原视频匹配新口播音频",
    "audio-generate": "生成音频",
    "canvas-agent-plan": "为画布生成可确认的操作方案", "canvas-ops": "写入本人画布操作",
}

# Discovery is descriptive only.  `action_plan` below remains the sole input
# validator and route builder.  Keep this subset explicit because agents need
# usable media/Canvas schemas, not the old field-type-only placeholder.
_ID_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 160}
_INT_ID_SCHEMA = {"type": "integer", "minimum": 1, "maximum": 2**63 - 1}
_IMAGE_UPLOAD_SCHEMA = {"type": "string", "pattern": "^img_[0-9a-f]{32}$"}
_VIDEO_UPLOAD_SCHEMA = {"type": "string", "pattern": "^vid_[0-9a-f]{32}$"}
_AUDIO_UPLOAD_SCHEMA = {"type": "string", "pattern": "^aud_[0-9a-f]{32}$"}

# Keep discovery and executable planning on one channel matrix.  The public
# CLI carries an identical table and the cross-package contract test prevents
# the two distributions from drifting apart.
_VIDEO_CHANNEL_RULES = {
    "grok": {
        "ratios": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"],
        "duration": [1, 15], "seconds": [],
        "resolutions": ["480p", "720p"],
        "models": ["grok-imagine-video", "grok-imagine-video-1.5"],
        "reference_max": 7, "generate_audio": False,
        "default_ratio": "16:9", "default_duration": 10,
        "default_resolution": "720p", "default_model": "grok-imagine-video",
        "reference_resolutions": ["720p"],
        "reference_required_models": ["grok-imagine-video-1.5"],
    },
    "micro": {
        "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"],
        "duration": [4, 15], "seconds": [],
        "resolutions": ["480p", "720p", "1080p"], "models": [],
        "reference_max": 9, "generate_audio": True,
        "default_ratio": "9:16", "default_duration": 5,
        "default_resolution": "720p", "default_model": "",
    },
    "omni": {
        "ratios": ["9:16", "16:9"], "duration": [3, 10], "seconds": [],
        "resolutions": ["720p"], "models": [],
        "reference_max": 6, "generate_audio": False,
        "default_ratio": "16:9", "default_duration": 5,
        "default_resolution": "720p", "default_model": "",
    },
    "minimax": {
        "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"],
        "duration": [4, 15], "seconds": [],
        "resolutions": ["2k"], "models": [],
        "reference_max": 5, "generate_audio": False,
        "default_ratio": "9:16", "default_duration": 5,
        "default_resolution": "2k", "default_model": "",
    },
    "sora": {
        "ratios": ["9:16", "16:9"], "duration": [], "seconds": [4, 8, 12],
        "resolutions": ["720p", "1024p", "1080p"],
        "models": ["sora-2", "sora-2-pro"],
        "reference_max": 1, "generate_audio": False,
        "default_ratio": "9:16", "default_seconds": 4,
        "default_resolution": "720p", "default_model": "sora-2",
        "model_resolutions": {
            "sora-2": ["720p"],
            "sora-2-pro": ["720p", "1024p", "1080p"],
        },
    },
}


def _video_channel_then(rule):
    properties = {
        "ratio": {"enum": list(rule["ratios"])},
        "resolution": {"enum": list(rule["resolutions"])},
        "reference_upload_ids": {
            "type": "array", "minItems": 1,
            "maxItems": int(rule["reference_max"]),
        },
    }
    forbidden = []
    if rule["duration"]:
        properties["duration"] = {
            "type": "integer", "minimum": rule["duration"][0],
            "maximum": rule["duration"][1],
        }
        forbidden.append("seconds")
    else:
        properties["seconds"] = {"type": "integer", "enum": list(rule["seconds"])}
        forbidden.append("duration")
    if rule["models"]:
        properties["model"] = {"type": "string", "enum": list(rule["models"])}
    else:
        forbidden.append("model")
    if not rule["generate_audio"]:
        forbidden.append("generate_audio")
    result = {"properties": properties}
    if forbidden:
        result["not"] = {"anyOf": [{"required": [field]} for field in forbidden]}
    return result


def _video_channel_schema():
    clauses = []
    for channel, rule in _VIDEO_CHANNEL_RULES.items():
        clauses.append({
            "if": {"properties": {"channel": {"const": channel}}, "required": ["channel"]},
            "then": _video_channel_then(rule),
        })
    clauses.append({
        "if": {"not": {"required": ["channel"]}},
        "then": _video_channel_then(_VIDEO_CHANNEL_RULES["grok"]),
    })
    grok_selector = {"anyOf": [
        {"not": {"required": ["channel"]}},
        {"properties": {"channel": {"const": "grok"}}, "required": ["channel"]},
    ]}
    clauses.append({
        "if": {"allOf": [grok_selector, {"required": ["reference_upload_ids"]}]},
        "then": {"properties": {"resolution": {"enum": ["720p"]}}},
    })
    clauses.append({
        "if": {"allOf": [grok_selector, {
            "properties": {"model": {"const": "grok-imagine-video-1.5"}},
            "required": ["model"],
        }]},
        "then": {
            "required": ["reference_upload_ids"],
            "properties": {"resolution": {"enum": ["720p"]}},
        },
    })
    sora_selector = {
        "properties": {"channel": {"const": "sora"}}, "required": ["channel"],
    }
    for model, resolutions in _VIDEO_CHANNEL_RULES["sora"]["model_resolutions"].items():
        clauses.append({
            "if": {"allOf": [sora_selector, {
                "properties": {"model": {"const": model}}, "required": ["model"],
            }]},
            "then": {"properties": {"resolution": {"enum": list(resolutions)}}},
        })
    clauses.append({
        "if": {"allOf": [sora_selector, {"not": {"required": ["model"]}}]},
        "then": {"properties": {"resolution": {"enum": ["720p"]}}},
    })
    return clauses


_MEDIA_SCHEMAS = {
    "image-generate": {
        "required": ["prompt"], "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
            "provider": {"type": "string", "enum": ["openai", "xiaole", "seedream", "banana"]},
            "ratio": {"type": "string", "enum": ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]},
            "quality": {"type": "string", "enum": ["std", "hd"]},
            "count": {"type": "integer", "minimum": 1, "maximum": 4},
            "variant": {"type": "string", "enum": ["std", "pro"]},
            "model": {"type": "string", "enum": ["nb2", "pro"]},
            "image_upload_id": _IMAGE_UPLOAD_SCHEMA, "mask_upload_id": _IMAGE_UPLOAD_SCHEMA,
            "reference_upload_ids": {"type": "array", "minItems": 1, "maxItems": 16,
                                     "items": _IMAGE_UPLOAD_SCHEMA},
        },
        "constraints": [
            "provider-specific reference_upload_ids limit: openai=16, seedream=10, xiaole=4, banana=14",
            "image_upload_id and reference_upload_ids are mutually exclusive",
            "mask_upload_id requires image_upload_id, provider=openai, and count=1",
            "model is only for banana; variant is only for seedream; banana count is 1, 2, or 4",
        ],
    },
    "video-generate": {
        "required": ["prompt"], "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
            "channel": {"type": "string", "enum": ["grok", "micro", "omni", "minimax", "sora"]},
            "ratio": {"type": "string", "enum": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "adaptive"]},
            "duration": {"type": "integer", "minimum": 1, "maximum": 15},
            "seconds": {"type": "integer", "enum": [4, 8, 12]},
            "resolution": {"type": "string", "enum": ["480p", "720p", "1024p", "1080p", "2k"]},
            "model": {"type": "string", "enum": ["grok-imagine-video", "grok-imagine-video-1.5", "sora-2", "sora-2-pro"]},
            "generate_audio": {"type": "boolean"},
            "reference_upload_ids": {"type": "array", "minItems": 1, "maxItems": 9,
                                     "items": _IMAGE_UPLOAD_SCHEMA},
        },
        "allOf": _video_channel_schema(),
        "x-hq-channel-rules": _VIDEO_CHANNEL_RULES,
        "constraints": [
            "reference_upload_ids limit: grok=7, micro=9, omni=6, minimax=5",
            "channel=minimax accepts only resolution=2k for new tasks",
            "resolution=2k is only valid when channel=minimax",
            "channel-specific ratio, duration/seconds, resolution, model, and reference rules are machine-readable in input_schema.allOf",
            "sora uses model=sora-2|sora-2-pro, seconds=4|8|12, ratio=9:16|16:9, resolution=720p|1024p|1080p, and exactly one reference when supplied",
            "sora rejects duration and generate_audio; seconds is only for sora; model is otherwise only for grok",
            "generate_audio is only a boolean for micro",
        ],
    },
    "video-lipsync": {
        "required": ["video_asset_id", "audio_asset_id"],
        "properties": {
            "video_asset_id": _INT_ID_SCHEMA,
            "audio_asset_id": _INT_ID_SCHEMA,
            "quality": {"type": "string", "enum": ["speed", "precision"]},
            "dynamic_duration": {"type": "boolean"},
        },
        "constraints": [
            "video_asset_id and audio_asset_id must be completed assets owned by this account",
            "quality defaults to speed; precision costs twice as many points",
            "dynamic_duration defaults to false to preserve the source performance timing",
            "the source video must be 1-300 seconds",
        ],
    },
    "audio-generate": {
        "required": ["text"], "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 1000},
            "voice": {"type": "string", "minLength": 1, "maxLength": 128},
            "speed": {"type": "number", "minimum": 0.5, "maximum": 2},
            "pitch": {"type": "integer", "minimum": -12, "maximum": 12},
            "volume": {"type": "integer", "minimum": -50, "maximum": 100},
        }, "constraints": ["speed is rounded to one decimal place"],
    },
    "canvas-create": {
        "required": ["name"], "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 48},
            "prompt": {"type": "string", "maxLength": 2000},
        }, "constraints": ["a non-empty prompt becomes the first text node"],
    },
    "canvas-agent-plan": {
        "required": ["prompt", "project_id", "snapshot_digest", "scope", "nodes", "edges", "selected_node_ids"],
        "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
            "project_id": {"type": "string", "pattern": "^(local|collab):[A-Za-z0-9_-]{1,120}$"},
            "snapshot_digest": {"type": "string", "pattern": "^[a-f0-9]{8,32}$"},
            "scope": {"type": "string", "enum": ["local", "collab"]},
            "nodes": {"type": "array", "maxItems": 60}, "edges": {"type": "array", "maxItems": 120},
            "selected_node_ids": {"type": "array", "maxItems": 30}, "history": {"type": "array", "maxItems": 10},
            "page_context": {"type": "object"}, "ip12_context": {"type": "object"},
        },
        "constraints": [
            "project_id prefix must match scope; nodes and edges may not reference unknown or duplicate node IDs",
            "node text is capped at 30,000 characters total; history is at most 10 user/assistant messages",
            "page_context is only the Huangque Canvas page; media data, blob URLs, and base64 are rejected",
        ],
    },
    "canvas-ops": {
        "required": ["board_id", "base_version", "op_id", "ops"], "properties": {
            "board_id": _ID_SCHEMA, "base_version": _INT_ID_SCHEMA,
            "op_id": {"type": "string", "pattern": "^hqcli-[A-Za-z0-9_-]{11,122}$"},
            "ops": {"type": "array", "minItems": 1, "maxItems": 12},
        },
        "constraints": [
            "only node.create, node.patch, and edge.create are accepted",
            "created node types are text, gen, or video; coordinates are 0-100000",
            "deletion, board replacement, generated output, membership, and script operations are rejected",
        ],
    },
}

_TALKING_FIELDS = {
    "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1", "4:5", "5:4"]},
    "motion": {"type": "string", "enum": ["low", "medium", "high"]}, "subtitle": {"type": "boolean"},
    "subtitle_style": {"type": "string", "enum": ["white", "variety", "bar"]},
    "subtitle_position": {"type": "string", "enum": ["top", "upper", "center", "lower", "bottom"]},
}
_MEDIA_SCHEMAS.update({
    "canvas-list": {"required": [], "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
    }, "constraints": []},
    "canvas-get": {"required": ["board_id"], "properties": {"board_id": _ID_SCHEMA}, "constraints": []},
    "digital-ip-text-generate": {"required": ["text", "voice"], "properties": {
        "avatar_id": _INT_ID_SCHEMA, "image_upload_id": {**_IMAGE_UPLOAD_SCHEMA, "title": "人物照片"},
        "text": {"type": "string", "minLength": 1, "maxLength": 1000},
        "voice": {"type": "string", "minLength": 1, "maxLength": 128}, **_TALKING_FIELDS},
        "constraints": ["provide one ready account avatar_id or one private image_upload_id; output is fixed at 1080p"]},
    "digital-ip-audio-generate": {"required": [], "properties": {
        "avatar_id": _INT_ID_SCHEMA, "image_upload_id": {**_IMAGE_UPLOAD_SCHEMA, "title": "人物照片"},
        "audio_file": {"type": "string", "minLength": 1, "maxLength": 500},
        "audio_upload_id": {**_AUDIO_UPLOAD_SCHEMA, "title": "本人口播音频"}, **_TALKING_FIELDS},
        "constraints": ["provide one avatar_id or image_upload_id and one owned audio_file or audio_upload_id; output is fixed at 1080p"]},
    "digital-ip-batch-generate": {"required": ["avatars", "text", "voice"], "properties": {
        "avatars": {"type": "array", "minItems": 2, "maxItems": 5, "uniqueItems": True, "items": {
            "type": "object", "additionalProperties": False, "required": ["avatar_id"], "properties": {
                "avatar_id": _INT_ID_SCHEMA, "label": {"type": "string", "minLength": 1, "maxLength": 60},
            }}},
        "text": {"type": "string", "minLength": 1, "maxLength": 1000},
        "voice": {"type": "string", "minLength": 1, "maxLength": 128}, **_TALKING_FIELDS},
        "constraints": ["avatars contains 2-5 distinct ready account avatar IDs, each optionally labelled up to 60 characters; output is fixed at 1080p"]},
    "cinematic-open-generate": {"required": ["prompt"], "properties": {
        "avatar_id": _INT_ID_SCHEMA, "avatar_ids": {"type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True, "items": _INT_ID_SCHEMA},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
        "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1"]},
        "duration": {"type": "integer", "minimum": 4, "maximum": 15}, "enhance_prompt": {"type": "boolean"},
        "reference_image_upload_ids": {"type": "array", "minItems": 1, "maxItems": 8, "items": _IMAGE_UPLOAD_SCHEMA},
        "reference_video_upload_ids": {"type": "array", "minItems": 1, "maxItems": 3, "items": _VIDEO_UPLOAD_SCHEMA},
    }, "constraints": ["provide avatar_id or avatar_ids, never both; avatars and image references share 9 slots; output is fixed at 720p"]},
    "cinematic-motion-generate": {"required": ["avatar_id", "reference_video_upload_ids"], "properties": {
        "avatar_id": _INT_ID_SCHEMA, "reference_video_upload_ids": {"type": "array", "minItems": 1, "maxItems": 1, "items": _VIDEO_UPLOAD_SCHEMA},
        "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1"]},
    }, "constraints": ["avatar_id must be a ready cinematic avatar; output is fixed at 720p"]},
    "tryon-fast-generate": {"required": ["person_image_upload_id", "clothes_upload_id"], "properties": {
        "person_image_upload_id": {**_IMAGE_UPLOAD_SCHEMA, "title": "人物图片"},
        "clothes_upload_id": {**_IMAGE_UPLOAD_SCHEMA, "title": "服装图片"},
        "seconds": {"type": "integer", "minimum": 5, "maximum": 15},
    }, "constraints": ["both uploads are private images owned by this account; seconds defaults to 6"]},
    "tryon-classic-generate": {"required": ["person_video_upload_id"], "properties": {
        "person_video_upload_id": {**_VIDEO_UPLOAD_SCHEMA, "title": "人物视频"},
        "clothes_upload_id": {**_IMAGE_UPLOAD_SCHEMA, "title": "服装图片"},
        "background_upload_id": {**_IMAGE_UPLOAD_SCHEMA, "title": "背景图片"},
        "seconds": {"type": "integer", "minimum": 1, "maximum": 6},
    }, "constraints": ["provide clothes_upload_id, background_upload_id, or both; seconds defaults to 6"]},
    "video-avatars": {"required": [], "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 120},
    }, "constraints": []},
    "video-compose-projects": {"required": [], "properties": {}, "constraints": []},
    "video-compose-project": {"required": ["project_id"], "properties": {
        "project_id": {"type": "string", "pattern": "^compose_[0-9a-f]{32}$"},
    }, "constraints": []},
    "video-compose-create": {"required": ["source_asset_id"], "properties": {
        "source_asset_id": _INT_ID_SCHEMA,
    }, "constraints": ["source_asset_id must be a completed video asset owned by this account"]},
    "video-compose-analyze": {"required": ["project_id", "expected_revision"], "properties": {
        "project_id": {"type": "string", "pattern": "^compose_[0-9a-f]{32}$"}, "expected_revision": _INT_ID_SCHEMA,
    }, "constraints": ["analysis is non-destructive and uses the expected current project revision"]},
    "video-compose-review": {"required": ["project_id", "expected_revision", "decisions"], "properties": {
        "project_id": {"type": "string", "pattern": "^compose_[0-9a-f]{32}$"}, "expected_revision": _INT_ID_SCHEMA,
        "decisions": {"type": "object", "minProperties": 1, "maxProperties": 200,
                      "additionalProperties": {"type": "string", "enum": ["keep", "remove"]}},
    }, "constraints": []},
    "video-compose-render": {"required": ["project_id", "expected_revision"], "properties": {
        "project_id": {"type": "string", "pattern": "^compose_[0-9a-f]{32}$"}, "expected_revision": _INT_ID_SCHEMA,
    }, "constraints": ["render uses the confirmed EDL for this project revision"]},
    "digital-presenter-capability": {"required": [], "properties": {}, "constraints": []},
    "digital-presenter-project": {"required": ["board_id", "project_id"], "properties": {
        "board_id": _ID_SCHEMA, "project_id": {"type": "string", "pattern": "^dp_[0-9a-f]{32}$"},
    }, "constraints": []},
    "digital-presenter-create": {"required": ["board_id", "request_id"], "properties": {
        "board_id": _ID_SCHEMA, "request_id": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{8,128}$"},
        "title": {"type": "string", "minLength": 1, "maxLength": 80}, "script_text": {"type": "string", "maxLength": 20000},
        "ratio": {"type": "string", "enum": ["9:16", "16:9"]}, "resolution": {"type": "string", "enum": ["1080p"]},
        "voice_key": {"type": "string", "maxLength": 200}, "target_duration": {"type": "integer", "minimum": 30, "maximum": 180},
    }, "constraints": ["request_id is the idempotency key"]},
    "digital-presenter-update": {"required": ["board_id", "project_id", "revision"], "properties": {
        "board_id": _ID_SCHEMA, "project_id": {"type": "string", "pattern": "^dp_[0-9a-f]{32}$"}, "revision": _INT_ID_SCHEMA,
        "title": {"type": "string", "minLength": 1, "maxLength": 80}, "script_text": {"type": "string", "maxLength": 20000},
        "ratio": {"type": "string", "enum": ["9:16", "16:9"]}, "resolution": {"type": "string", "enum": ["1080p"]},
        "voice_key": {"type": "string", "maxLength": 200}, "target_duration": {"type": "integer", "minimum": 30, "maximum": 180},
    }, "constraints": ["provide at least one editable field and the current revision"]},
})

_FAMILIES = {
    "image-upload": "image", "image-generate": "image", "audio-slots": "audio", "voices": "audio", "audio-generate": "audio",
    "video-upload": "video", "video-avatars": "video", "video-generate": "video", "video-lipsync": "video", "digital-ip-text-generate": "video",
    "digital-ip-batch-generate": "video", "digital-ip-audio-generate": "video", "cinematic-open-generate": "video",
    "cinematic-motion-generate": "video", "tryon-fast-generate": "video", "tryon-classic-generate": "video",
    "video-compose-projects": "video", "video-compose-project": "video", "video-compose-create": "video",
    "video-compose-analyze": "video", "video-compose-review": "video", "video-compose-render": "video",
    "text-video-capability": "video", "text-video-templates": "video", "text-video-styles": "video", "text-video-voices": "video",
    "canvas-list": "canvas", "canvas-get": "canvas", "canvas-create": "canvas", "canvas-agent-plan": "canvas",
    "canvas-ops": "canvas", "digital-presenter-capability": "canvas", "digital-presenter-project": "canvas",
    "digital-presenter-create": "canvas", "digital-presenter-update": "canvas",
}
_ACTION_FEATURE_GATES = {
    "audio-generate": ("audio",), "canvas-agent-plan": ("canvas_agent",),
    "video-lipsync": ("video",), "digital-ip-text-generate": ("video",), "digital-ip-batch-generate": ("video",),
    "digital-ip-audio-generate": ("video",), "cinematic-open-generate": ("cinematic",),
    "cinematic-motion-generate": ("cinematic",), "tryon-fast-generate": ("tryon",),
    "tryon-classic-generate": ("tryon",), "digital-presenter-capability": ("digital_presenter",),
    "digital-presenter-project": ("digital_presenter",), "digital-presenter-create": ("digital_presenter",),
    "digital-presenter-update": ("digital_presenter",),
}
_OPTION_FEATURE_GATES = {
    ("image-generate", "provider"): {"openai": ("image",), "seedream": ("image",), "xiaole": ("image", "image_xiaole"), "banana": ("image", "banana")},
    ("video-generate", "channel"): {"grok": ("grok_video",), "micro": ("seedance_video",), "omni": ("omni_video",), "minimax": ("minimax_h3_video",), "sora": ("sora_video",)},
}
CATALOG_FEATURE_FLAGS = tuple(sorted({flag for flags in (*_ACTION_FEATURE_GATES.values(), *(
    gates for options in _OPTION_FEATURE_GATES.values() for gates in options.values())) for flag in flags}))

_GENERATION_ACTIONS = frozenset({
    "collect-content", "collect-video", "collect-transcript", "collect-search", "leads-generate",
    "canvas-agent-plan", "image-generate", "video-generate", "video-lipsync", "audio-generate",
    "digital-ip-text-generate", "digital-ip-batch-generate", "digital-ip-audio-generate",
    "cinematic-open-generate", "cinematic-motion-generate", "tryon-fast-generate", "tryon-classic-generate",
})


def _catalog_type(field):
    if field in {"id", "avatar_id", "job_id", "limit", "offset", "page", "page_size", "days", "count", "duration", "seconds", "expected_version", "expected_revision", "revision", "source_asset_id", "target_duration", "pitch", "volume"}:
        return "integer"
    if field in {"favorite", "subtitle", "enhance_prompt", "generate_audio"}:
        return "boolean"
    if field.endswith("ids") or field in {"ops", "nodes", "edges", "history", "avatars", "lead_ids", "tags"}:
        return "array"
    if field in {"decisions", "channels_targets"}:
        return "object"
    if field == "speed":
        return "number"
    return "string"


def _catalog_route(action):
    if action.startswith("canvas-") or action.startswith("digital-presenter-"):
        return "/workbench/canvas"
    if action.startswith("image-"):
        return "/workbench/banana"
    if action.startswith("audio-") or action == "voices":
        return "/workbench/audio"
    if action.startswith("text-video-"):
        return "/workbench/text-video"
    if action.startswith("video-compose-"):
        return "/workbench/one-click-video"
    if action.startswith("digital-ip-"):
        return "/workbench/digital-ip"
    if action.startswith(("video-", "cinematic-", "tryon-")):
        return "/workbench/video"
    if action.startswith("ip12-"):
        return "/workbench/ip12/"
    if action.startswith("short-drama-"):
        return "/workbench/short-drama"
    return ""


def _catalog_entry(action, fields):
    generation = action in _GENERATION_ACTIONS
    external_effect = generation or action in CONFIRMATION_ACTIONS
    details = _MEDIA_SCHEMAS.get(action, {})
    schema = {
        "type": "object", "additionalProperties": False,
        "required": list(details.get("required", ())),
        "properties": details.get("properties") or {field: {"type": _catalog_type(field)} for field in fields},
    }
    for keyword in ("allOf", "anyOf", "oneOf", "x-hq-channel-rules"):
        if keyword in details:
            schema[keyword] = details[keyword]
    result_type = "quote" if generation else ("account" if action == "account" else "json")
    return {
        "action": action,
        "purpose": _ACTION_PURPOSES.get(action, "执行黄雀已登记能力：" + action),
        "input_schema": schema,
        "constraints": list(details.get("constraints", ())),
        "billing": "quote_then_confirm" if generation else "free",
        "external_effect": external_effect,
        "confirmation_required": generation or action in CONFIRMATION_ACTIONS,
        "risk": "production" if generation else ("write" if external_effect else "read"),
        "result_type": result_type, "result": {"kind": result_type},
        "ui_route": _catalog_route(action),
        "transport": {"kind": "action", "supports": ["internal_action", "controlled_shell"]},
        "availability": {"status": "available", "feature_flags": [], "disabled_feature_flags": []},
    }


def _upload_catalog_entry(action, family, max_bytes, mime_types, max_files):
    label = {"image": "图片", "video": "视频", "audio": "音频"}[family]
    return {
        "action": action, "family": family, "purpose": "上传本人生成所需的临时参考" + label,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["file"], "properties": {
            "file": {"type": "file", "path": "absolute", "maxBytes": max_bytes, "mimeTypes": mime_types},
        }},
        "constraints": ["requires explicit confirmation", "uploads are private to the current account"],
        "billing": "free", "external_effect": True, "confirmation_required": True, "risk": "write",
        "result_type": "upload", "result": {"kind": "upload_id"}, "ui_route": _catalog_route(action),
        "transport": {"kind": "dedicated_upload", "supports": ["dedicated_upload"], "account_active_max_files": max_files},
        "availability": {"status": "available", "feature_flags": [], "disabled_feature_flags": []},
    }


ACTION_CATALOG = tuple(_catalog_entry(action, fields) for action, fields in _ACTION_INPUTS.items()) + (
    _upload_catalog_entry("image-upload", "image", 10 * 1024 * 1024,
                          ["image/jpeg", "image/png", "image/webp"], 20),
    _upload_catalog_entry("video-upload", "video", 32 * 1024 * 1024,
                          ["video/mp4", "video/quicktime", "video/webm"], 6),
)
for _catalog_item in ACTION_CATALOG:
    if _catalog_item["action"] in _FAMILIES:
        _catalog_item["family"] = _FAMILIES[_catalog_item["action"]]
ACTION_CATALOG_MAP = {item["action"]: item for item in ACTION_CATALOG if item["transport"]["kind"] == "action"}
ACTION_CATALOG_VERSION = "hq-action-catalog-v2"


def action_catalog(feature_states=None):
    """Return a copy-safe, provider-secret-free catalog for first-party callers."""
    feature_states = feature_states or {}
    actions = json.loads(json.dumps(ACTION_CATALOG, ensure_ascii=False))
    for item in actions:
        action = item["action"]
        required = _ACTION_FEATURE_GATES.get(action, ())
        disabled = [flag for flag in required if not feature_states.get(flag, True)]
        availability = item["availability"]
        availability["feature_flags"] = list(required)
        availability["disabled_feature_flags"] = disabled
        if disabled:
            availability["status"] = "disabled"
        for (option_action, field), options in _OPTION_FEATURE_GATES.items():
            if action != option_action:
                continue
            available = [option for option, flags in options.items() if all(feature_states.get(flag, True) for flag in flags)]
            unavailable = {option: [flag for flag in flags if not feature_states.get(flag, True)]
                           for option, flags in options.items() if option not in available}
            item["input_schema"]["properties"][field]["enum"] = available
            availability["available_%s" % field] = available
            availability["disabled_%s" % field] = unavailable
            if not available:
                availability["status"] = "disabled"
    return {
        "version": ACTION_CATALOG_VERSION,
        "actions": actions,
    }

_START_HITS = {}
_START_HITS_LOCK = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_UPLOAD_ID_RE = re.compile(r"^img_[0-9a-f]{32}$")
_VIDEO_UPLOAD_ID_RE = re.compile(r"^vid_[0-9a-f]{32}$")
_AUDIO_UPLOAD_ID_RE = re.compile(r"^aud_[0-9a-f]{32}$")
_CANVAS_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CANVAS_OP_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CANVAS_PROJECT_RE = re.compile(r"^(local|collab):[A-Za-z0-9_-]{1,120}$")
_CANVAS_OP_ID_RE = re.compile(r"^hqcli-[A-Za-z0-9_-]{11,122}$")
_VIDEO_COMPOSE_PROJECT_RE = re.compile(r"^compose_[0-9a-f]{32}$")
_VIDEO_COMPOSE_CANDIDATE_RE = re.compile(r"^candidate_[0-9a-f]{16}$")
_DIGITAL_PRESENTER_PROJECT_RE = re.compile(r"^dp_[0-9a-f]{32}$")
_LEAD_ID_RE = re.compile(r"^[0-9a-f]{16,40}$")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_CANVAS_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{512,}={0,2}(?![A-Za-z0-9+/_=-])")
IMAGE_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
IMAGE_UPLOAD_SLOTS = threading.BoundedSemaphore(2)
VIDEO_UPLOAD_MAX_BYTES = 32 * 1024 * 1024
VIDEO_UPLOAD_SLOTS = threading.BoundedSemaphore(2)
AUDIO_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
AUDIO_UPLOAD_SLOTS = threading.BoundedSemaphore(2)
_TASK_KINDS = {
    "", "image", "audio", "video", "xiaole_video", "copy", "collect", "collect_search", "leads",
    "tryon", "cinematic", "avatar", "breakdown", "script_to_video", "sora_video",
}


class CLIAPIError(Exception):
    def __init__(self, status, detail, code="invalid_request"):
        super().__init__(detail)
        self.status = int(status)
        self.detail = str(detail)
        self.code = str(code)


def init_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS cli_device_grants(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_code_hash TEXT NOT NULL UNIQUE,
        user_code_hash TEXT NOT NULL UNIQUE,
        client_name TEXT NOT NULL,
        requested_scopes_json TEXT NOT NULL,
        approved_scopes_json TEXT,
        username TEXT,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        approved_at INTEGER,
        last_poll_at INTEGER NOT NULL DEFAULT 0,
        token_hash TEXT UNIQUE,
        token_expires_at INTEGER,
        revoked_at INTEGER
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_cli_grants_user ON cli_device_grants(username, token_expires_at)")
    connection.execute("""CREATE TABLE IF NOT EXISTS cli_action_requests(
        username TEXT NOT NULL,
        action TEXT NOT NULL,
        request_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        http_status INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY(username, action, request_id)
    )""")
    connection.execute("""CREATE INDEX IF NOT EXISTS idx_cli_action_active
        ON cli_action_requests(username, action, project_id, status, updated_at)""")


def _hash(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def begin_action_request(db_factory, username, action, request_id, project_id, request_hash, now=None):
    """Claim one persistent CLI action or describe the existing claim."""
    now = int(time.time() if now is None else now)
    connection = db_factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM cli_action_requests WHERE updated_at<?", (now - ACTION_REQUEST_TTL,))
        connection.execute(
            "UPDATE cli_action_requests SET status='uncertain',updated_at=? "
            "WHERE status='in_progress' AND updated_at<?",
            (now, now - ACTION_INFLIGHT_TTL),
        )
        row = connection.execute(
            "SELECT request_hash,status,http_status FROM cli_action_requests "
            "WHERE username=? AND action=? AND request_id=?",
            (username, action, request_id),
        ).fetchone()
        if row:
            connection.commit()
            if row["request_hash"] != request_hash:
                return "conflict", row["http_status"]
            return row["status"], row["http_status"]
        recent = connection.execute(
            "SELECT COUNT(*) FROM cli_action_requests WHERE username=? AND action=? AND created_at>=?",
            (username, action, now - 60),
        ).fetchone()[0]
        if int(recent) >= CLI_CHAT_REQUESTS_PER_MINUTE:
            connection.commit()
            return "rate_limited", None
        active = connection.execute(
            "SELECT status FROM cli_action_requests WHERE username=? AND action=? AND project_id=? "
            "AND (status='in_progress' OR (status='uncertain' AND updated_at>=?)) "
            "ORDER BY updated_at DESC LIMIT 1",
            (username, action, project_id, now - ACTION_INFLIGHT_TTL),
        ).fetchone()
        if active:
            connection.commit()
            return ("uncertain" if active["status"] == "uncertain" else "busy"), None
        connection.execute(
            "INSERT INTO cli_action_requests(username,action,request_id,project_id,request_hash,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'in_progress',?,?)",
            (username, action, request_id, project_id, request_hash, now, now),
        )
        connection.commit()
        return "new", None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finish_action_request(db_factory, username, action, request_id, http_status=None, uncertain=False, now=None):
    now = int(time.time() if now is None else now)
    connection = db_factory()
    try:
        connection.execute(
            "UPDATE cli_action_requests SET status=?,http_status=?,updated_at=? "
            "WHERE username=? AND action=? AND request_id=? AND status='in_progress'",
            ("uncertain" if uncertain else "completed", http_status, now, username, action, request_id),
        )
        connection.commit()
    finally:
        connection.close()


def _strict_object(value, allowed, required=()):
    if not isinstance(value, dict):
        raise CLIAPIError(400, "请求体必须是 JSON 对象")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise CLIAPIError(400, "不支持的参数：" + unknown[0])
    missing = [key for key in required if key not in value]
    if missing:
        raise CLIAPIError(400, "缺少参数：" + missing[0])
    return value


def _string(value, field, minimum=0, maximum=2000):
    if not isinstance(value, str):
        raise CLIAPIError(400, field + " 必须是字符串")
    value = value.strip()
    if len(value) < minimum or len(value) > maximum or any(ord(ch) < 32 for ch in value):
        raise CLIAPIError(400, field + " 长度或内容不合法")
    return value


def _integer(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CLIAPIError(400, "%s 必须是 %d-%d 的整数" % (field, minimum, maximum))
    return value


def _enum(value, field, choices):
    value = _string(value, field, 1, 80)
    if value not in choices:
        raise CLIAPIError(400, field + " 仅支持：" + "、".join(choices))
    return value


def _identifier(value, field):
    value = _string(value, field, 1, 160)
    if not _ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _upload_id(value, field):
    value = _string(value, field, 1, 64).lower()
    if not _UPLOAD_ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _video_upload_id(value, field):
    value = _string(value, field, 1, 64).lower()
    if not _VIDEO_UPLOAD_ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _audio_upload_id(value, field):
    value = _string(value, field, 1, 64).lower()
    if not _AUDIO_UPLOAD_ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _number(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CLIAPIError(400, field + " 必须是有限数字")
    if not minimum <= value <= maximum:
        raise CLIAPIError(400, field + " 超出允许范围")
    return value


def _canvas_node_id(value, field="节点标识"):
    value = _string(value, field, 1, 128)
    if not _CANVAS_NODE_ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _canvas_op_node_id(value, field="节点标识"):
    value = _string(value, field, 1, 64)
    if not _CANVAS_OP_NODE_ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _canvas_agent_payload(value):
    _strict_object(value, {
        "prompt", "project_id", "snapshot_digest", "scope", "nodes", "edges",
        "selected_node_ids", "history", "page_context", "ip12_context",
    }, ("prompt", "project_id", "snapshot_digest", "scope", "nodes", "edges", "selected_node_ids"))
    project_id = _string(value["project_id"], "project_id", 1, 128)
    scope = _enum(value["scope"], "scope", ("local", "collab"))
    if not _CANVAS_PROJECT_RE.fullmatch(project_id) or not project_id.startswith(scope + ":"):
        raise CLIAPIError(400, "project_id 与 scope 不匹配")
    digest = _string(value["snapshot_digest"], "snapshot_digest", 8, 32).lower()
    if not re.fullmatch(r"[a-f0-9]{8,32}", digest):
        raise CLIAPIError(400, "snapshot_digest 格式不合法")
    raw_nodes = value["nodes"]
    if not isinstance(raw_nodes, list) or len(raw_nodes) > 60:
        raise CLIAPIError(400, "nodes 必须是最多 60 项的数组")
    nodes, node_ids, total_content = [], set(), 0
    for raw in raw_nodes:
        _strict_object(raw, {"id", "type", "title", "content", "selected"},
                       ("id", "type", "title", "content", "selected"))
        node_id = _canvas_node_id(raw["id"])
        if node_id in node_ids:
            raise CLIAPIError(400, "nodes 包含重复节点")
        if not isinstance(raw["selected"], bool):
            raise CLIAPIError(400, "selected 必须是布尔值")
        node = {
            "id": node_id,
            "type": _enum(raw["type"], "节点类型", ("text", "image", "reverse", "gen", "video", "shortDrama")),
            "title": _string(raw["title"], "节点标题", 0, 120),
            "content": _string(raw["content"], "节点内容", 0, 5000),
            "selected": raw["selected"],
        }
        total_content += len(node["title"]) + len(node["content"])
        node_ids.add(node_id)
        nodes.append(node)
    if total_content > 30000:
        raise CLIAPIError(400, "画布上下文文本超过限制")
    selected = value["selected_node_ids"]
    if not isinstance(selected, list) or len(selected) > 30:
        raise CLIAPIError(400, "selected_node_ids 必须是最多 30 项的数组")
    selected = [_canvas_node_id(item, "选中节点标识") for item in selected]
    if len(selected) != len(set(selected)) or any(item not in node_ids for item in selected):
        raise CLIAPIError(400, "selected_node_ids 引用了无效节点")
    raw_edges = value["edges"]
    if not isinstance(raw_edges, list) or len(raw_edges) > 120:
        raise CLIAPIError(400, "edges 必须是最多 120 项的数组")
    edges = []
    for raw in raw_edges:
        _strict_object(raw, {"from_node_id", "to_node_id"}, ("from_node_id", "to_node_id"))
        source = _canvas_node_id(raw["from_node_id"], "连线起点")
        target = _canvas_node_id(raw["to_node_id"], "连线终点")
        if source == target or source not in node_ids or target not in node_ids:
            raise CLIAPIError(400, "edges 引用了无效节点")
        edges.append({"from_node_id": source, "to_node_id": target})
    raw_history = value.get("history", [])
    if not isinstance(raw_history, list) or len(raw_history) > 10:
        raise CLIAPIError(400, "history 必须是最多 10 项的数组")
    history = []
    for raw in raw_history:
        _strict_object(raw, {"role", "content"}, ("role", "content"))
        role = _enum(raw["role"], "历史角色", ("user", "assistant"))
        content = _string(raw["content"], "历史消息", 0, 2000)
        if content:
            history.append({"role": role, "content": content})
    page_context = value.get("page_context")
    if page_context is not None:
        _strict_object(page_context, {"page", "path", "title", "can_edit", "selected_count"},
                       ("page", "path", "title", "can_edit", "selected_count"))
        if page_context["page"] != "canvas" or page_context["path"] not in {
                "/workbench/canvas", "/workbench/canvas.html"}:
            raise CLIAPIError(400, "page_context 不属于黄雀画布")
        if not isinstance(page_context["can_edit"], bool):
            raise CLIAPIError(400, "page_context.can_edit 必须是布尔值")
        page_context = {
            "page": "canvas", "path": page_context["path"],
            "title": _string(page_context["title"], "page_context.title", 0, 120),
            "can_edit": page_context["can_edit"],
            "selected_count": _integer(page_context["selected_count"], "page_context.selected_count", 0, 30),
        }
    ip12_context = value.get("ip12_context")
    if ip12_context is not None:
        _strict_object(ip12_context, {"project_id", "title", "status", "foundation_status", "facts"},
                       ("project_id", "title", "status", "foundation_status", "facts"))
        raw_facts = ip12_context["facts"]
        if not isinstance(raw_facts, list) or len(raw_facts) > 20:
            raise CLIAPIError(400, "ip12_context.facts 必须是最多 20 项的数组")
        facts = []
        for raw in raw_facts:
            _strict_object(raw, {"label", "value"}, ("label", "value"))
            facts.append({
                "label": _string(raw["label"], "ip12_context.facts.label", 1, 80),
                "value": _string(raw["value"], "ip12_context.facts.value", 1, 800),
            })
        ip12_context = {
            "project_id": _identifier(ip12_context["project_id"], "ip12_context.project_id"),
            "title": _string(ip12_context["title"], "ip12_context.title", 0, 120),
            "status": _enum(ip12_context["status"], "ip12_context.status", ("draft", "candidate_ready", "confirmed")),
            "foundation_status": _enum(ip12_context["foundation_status"], "ip12_context.foundation_status",
                                       ("missing", "pending_confirmation", "confirmed", "stale", "legacy")),
            "facts": facts,
        }
    payload = {
        "prompt": _string(value["prompt"], "prompt", 1, 2000), "project_id": project_id,
        "snapshot_digest": digest, "scope": scope, "nodes": nodes, "edges": edges,
        "selected_node_ids": selected, "history": history,
    }
    if page_context is not None:
        payload["page_context"] = page_context
    if ip12_context is not None:
        payload["ip12_context"] = ip12_context
    raw = json.dumps(payload, ensure_ascii=False).lower()
    if any(marker in raw for marker in ("data:image/", "data:video/", ";base64,", "blob:")) or _CANVAS_BASE64_RE.search(raw):
        raise CLIAPIError(400, "画布上下文不能包含媒体数据或 Blob 地址")
    return payload


def _canvas_params(value, require_text=False):
    _strict_object(value, {"title", "text"}, ("text",) if require_text else ())
    if not value:
        raise CLIAPIError(400, "params 不能为空")
    params = {}
    if "title" in value:
        params["title"] = _string(value["title"], "params.title", 0, 120)
    if "text" in value:
        params["text"] = _string(value["text"], "params.text", 1 if require_text else 0, 5000)
    return params


def _canvas_ops_payload(value):
    _strict_object(value, {"board_id", "base_version", "op_id", "ops"},
                   ("board_id", "base_version", "op_id", "ops"))
    op_id = _string(value["op_id"], "op_id", 17, 128)
    if not _CANVAS_OP_ID_RE.fullmatch(op_id):
        raise CLIAPIError(400, "op_id 必须以 hqcli- 开头并包含足够的随机字符")
    raw_ops = value["ops"]
    if not isinstance(raw_ops, list) or not 1 <= len(raw_ops) <= 12:
        raise CLIAPIError(400, "ops 必须包含 1-12 项")
    ops = []
    for raw in raw_ops:
        if not isinstance(raw, dict):
            raise CLIAPIError(400, "画布操作必须是对象")
        kind = raw.get("type")
        if kind == "node.create":
            _strict_object(raw, {"type", "node"}, ("type", "node"))
            node = raw["node"]
            _strict_object(node, {"id", "type", "x", "y", "params"}, ("id", "type", "x", "y", "params"))
            ops.append({"type": kind, "node": {
                "id": _canvas_op_node_id(node["id"]),
                "type": _enum(node["type"], "node.type", ("text", "gen", "video")),
                "x": _number(node["x"], "node.x", 0, 100000),
                "y": _number(node["y"], "node.y", 0, 100000),
                "params": _canvas_params(node["params"], require_text=True),
            }})
        elif kind == "node.patch":
            _strict_object(raw, {"type", "id", "fields"}, ("type", "id", "fields"))
            fields = raw["fields"]
            _strict_object(fields, {"x", "y", "params"})
            if not fields:
                raise CLIAPIError(400, "node.patch fields 不能为空")
            clean = {}
            if "x" in fields:
                clean["x"] = _number(fields["x"], "fields.x", 0, 100000)
            if "y" in fields:
                clean["y"] = _number(fields["y"], "fields.y", 0, 100000)
            if "params" in fields:
                clean["params"] = _canvas_params(fields["params"])
            ops.append({"type": kind, "id": _canvas_op_node_id(raw["id"]), "fields": clean})
        elif kind == "edge.create":
            _strict_object(raw, {"type", "edge"}, ("type", "edge"))
            edge = raw["edge"]
            _strict_object(edge, {"from", "to"}, ("from", "to"))
            endpoints = {}
            for name in ("from", "to"):
                endpoint = edge[name]
                _strict_object(endpoint, {"node", "port"}, ("node", "port"))
                endpoints[name] = {
                    "node": _canvas_op_node_id(endpoint["node"], "edge.%s.node" % name),
                    "port": _enum(endpoint["port"], "edge.%s.port" % name, ("prompt", "image")),
                }
            if endpoints["from"]["node"] == endpoints["to"]["node"]:
                raise CLIAPIError(400, "画布连线不能形成自环")
            ops.append({"type": kind, "edge": endpoints})
        else:
            raise CLIAPIError(400, "CLI 不允许该画布操作")
    return {
        "board_id": _identifier(value["board_id"], "board_id"),
        "base_version": _integer(value["base_version"], "base_version", 1, 2**63 - 1),
        "op_id": op_id, "ops": ops,
    }


def _tags(value):
    if not isinstance(value, list) or len(value) > 8:
        raise CLIAPIError(400, "tags 必须是最多 8 项的数组")
    clean = []
    for item in value:
        tag = _string(item, "tags", 1, 24)
        if tag not in clean:
            clean.append(tag)
    return clean


def _normalize_scopes(value):
    if not isinstance(value, list) or not value or len(value) > len(SCOPES):
        raise CLIAPIError(400, "requested_scopes 必须是非空权限数组")
    scopes = []
    for item in value:
        if not isinstance(item, str) or item not in SCOPES:
            raise CLIAPIError(400, "包含未知权限范围")
        if item not in scopes:
            scopes.append(item)
    return scopes


def _allow_device_start(client_key, now):
    with _START_HITS_LOCK:
        hits = [stamp for stamp in _START_HITS.get(client_key, []) if now - stamp < 600]
        _START_HITS[client_key] = hits
        if len(hits) >= 10:
            return False
        hits.append(now)
        return True


def _user_code():
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def _normalize_user_code(value):
    raw = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if len(raw) != 8:
        raise CLIAPIError(400, "授权码格式不正确")
    return raw[:4] + "-" + raw[4:]


def start_device(db_factory, body, client_key, now=None):
    now = int(time.time() if now is None else now)
    _strict_object(body, {"client_name", "requested_scopes"}, ("client_name", "requested_scopes"))
    if not _allow_device_start(str(client_key or "unknown"), now):
        raise CLIAPIError(429, "授权请求过于频繁，请稍后重试", "rate_limited")
    client_name = _string(body["client_name"], "client_name", 1, 80)
    scopes = _normalize_scopes(body["requested_scopes"])
    for _ in range(16):
        device_code, user_code = secrets.token_urlsafe(32), _user_code()
        try:
            with db_factory() as connection:
                connection.execute(
                    """INSERT INTO cli_device_grants(
                       device_code_hash,user_code_hash,client_name,requested_scopes_json,status,created_at,expires_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (_hash(device_code), _hash(user_code), client_name,
                     json.dumps(scopes, separators=(",", ":")), "pending", now, now + DEVICE_TTL),
                )
            break
        except Exception as exc:
            if "UNIQUE" not in str(exc).upper():
                raise
    else:
        raise CLIAPIError(503, "暂时无法创建授权码，请重试")
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": PUBLIC_ORIGIN + "/workbench/device?user_code=" + urllib.parse.quote(user_code),
        "expires_in": DEVICE_TTL,
        "interval": POLL_INTERVAL,
        "scopes": scopes,
        "scope_details": [{"scope": scope, "description": SCOPES[scope]} for scope in scopes],
    }


def approve_device(db_factory, username, body, now=None):
    now = int(time.time() if now is None else now)
    _strict_object(body, {"user_code", "approve"}, ("user_code", "approve"))
    if not isinstance(body["approve"], bool):
        raise CLIAPIError(400, "approve 必须是布尔值")
    code_hash = _hash(_normalize_user_code(body["user_code"]))
    with db_factory() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM cli_device_grants WHERE user_code_hash=?", (code_hash,)).fetchone()
        if not row:
            raise CLIAPIError(404, "授权码不存在", "not_found")
        if int(row["expires_at"]) <= now:
            connection.execute("UPDATE cli_device_grants SET status='expired' WHERE id=?", (row["id"],))
            raise CLIAPIError(410, "授权码已过期", "expired_token")
        if row["status"] != "pending":
            if row["status"] == "approved" and row["username"] == username:
                return {"ok": True, "status": "approved"}
            raise CLIAPIError(409, "授权码已处理", "already_processed")
        status = "approved" if body["approve"] else "denied"
        scopes = row["requested_scopes_json"] if body["approve"] else None
        connection.execute(
            """UPDATE cli_device_grants
               SET status=?,username=?,approved_scopes_json=?,approved_at=? WHERE id=?""",
            (status, username, scopes, now, row["id"]),
        )
    return {"ok": True, "status": status}


def device_info(db_factory, body, now=None):
    now = int(time.time() if now is None else now)
    _strict_object(body, {"user_code"}, ("user_code",))
    code_hash = _hash(_normalize_user_code(body["user_code"]))
    with db_factory() as connection:
        row = connection.execute(
            "SELECT client_name,requested_scopes_json,status,expires_at FROM cli_device_grants WHERE user_code_hash=?",
            (code_hash,),
        ).fetchone()
    if not row:
        raise CLIAPIError(404, "授权码不存在", "not_found")
    status = row["status"]
    if status == "pending" and int(row["expires_at"]) <= now:
        status = "expired"
    try:
        scopes = json.loads(row["requested_scopes_json"] or "[]")
    except Exception:
        raise CLIAPIError(500, "授权请求数据无效", "invalid_grant")
    return {
        "client_name": row["client_name"], "status": status, "expires_at": int(row["expires_at"]),
        "scopes": scopes,
        "scope_details": [{"scope": scope, "description": SCOPES[scope]} for scope in scopes if scope in SCOPES],
    }


def poll_device(db_factory, body, now=None):
    now = int(time.time() if now is None else now)
    _strict_object(body, {"device_code"}, ("device_code",))
    device_code = _string(body["device_code"], "device_code", 20, 200)
    with db_factory() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM cli_device_grants WHERE device_code_hash=?", (_hash(device_code),)).fetchone()
        if not row:
            raise CLIAPIError(400, "设备授权请求无效", "invalid_grant")
        status = row["status"]
        if status in {"pending", "approved"} and int(row["expires_at"]) <= now:
            connection.execute("UPDATE cli_device_grants SET status='expired' WHERE id=?", (row["id"],))
            raise CLIAPIError(410, "设备授权已过期", "expired_token")
        if status == "approved":
            token = secrets.token_urlsafe(32)
            scopes = json.loads(row["approved_scopes_json"] or "[]")
            connection.execute(
                """UPDATE cli_device_grants SET status='issued',token_hash=?,token_expires_at=?,last_poll_at=?
                   WHERE id=? AND status='approved'""",
                (_hash(token), now + TOKEN_TTL, now, row["id"]),
            )
            return {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL, "scopes": scopes}
        if status == "pending":
            last_poll = int(row["last_poll_at"] or 0)
            if last_poll and now - last_poll < POLL_INTERVAL:
                raise CLIAPIError(429, "轮询过快，请按 interval 重试", "slow_down")
            connection.execute("UPDATE cli_device_grants SET last_poll_at=? WHERE id=?", (now, row["id"]),)
            raise CLIAPIError(202, "等待用户授权", "authorization_pending")
        if status == "denied":
            raise CLIAPIError(403, "用户拒绝了授权", "access_denied")
        if status == "expired":
            raise CLIAPIError(410, "设备授权已过期", "expired_token")
        raise CLIAPIError(409, "访问令牌已经签发，请重新登录", "already_issued")


def authenticate(db_factory, token, now=None):
    now = int(time.time() if now is None else now)
    token = str(token or "").strip()
    if not 20 <= len(token) <= 200:
        return None
    with db_factory() as connection:
        row = connection.execute(
            """SELECT u.*,g.approved_scopes_json AS cli_scopes,g.token_expires_at AS cli_expires_at
               FROM cli_device_grants g JOIN users u ON u.username=g.username
               WHERE g.token_hash=? AND g.status='issued' AND g.revoked_at IS NULL
                 AND g.token_expires_at>? AND COALESCE(u.account_status,'active')='active'""",
            (_hash(token), now),
        ).fetchone()
    if not row:
        return None
    try:
        scopes = tuple(json.loads(row["cli_scopes"] or "[]"))
    except Exception:
        return None
    return row, scopes


def revoke(db_factory, token, now=None):
    now = int(time.time() if now is None else now)
    token = str(token or "").strip()
    if not token:
        return False
    with db_factory() as connection:
        cursor = connection.execute(
            "UPDATE cli_device_grants SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
            (now, _hash(token)),
        )
    return cursor.rowcount > 0


def origin_allowed(origin):
    return bool(origin) and hmac.compare_digest(str(origin).strip().rstrip("/"), PUBLIC_ORIGIN)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def proxy_json(plan, web_token, internal_token=""):
    headers = {
        "Authorization": "Bearer " + web_token,
        "User-Agent": "huangque-auth-cli-gateway/1",
        "Accept": "application/json",
    }
    headers.update(plan.get("headers") or {})
    if plan.get("internal"):
        if not internal_token:
            raise CLIAPIError(503, "CLI 内部授权未配置", "not_configured")
        headers["X-HQ-Internal-Token"] = internal_token
    body = plan.get("body")
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        plan["base"] + plan["path"], data=data, headers=headers, method=plan.get("method", "GET"),
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=plan.get("timeout", 30)) as response:
            raw, status = response.read(2 * 1024 * 1024 + 1), response.getcode()
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(2 * 1024 * 1024 + 1), exc.code
    except (urllib.error.URLError, OSError) as exc:
        raise CLIAPIError(502, "黄雀业务服务暂时不可用：" + str(exc)[:120], "upstream_unavailable")
    if len(raw) > 2 * 1024 * 1024:
        raise CLIAPIError(502, "黄雀业务服务响应过大", "upstream_response_too_large")
    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        payload = {"detail": "黄雀业务服务返回了无效响应"}
        status = 502
    if isinstance(payload, dict) and "detail" not in payload and isinstance(payload.get("error"), str):
        payload = dict(payload, detail=payload["error"])
    return int(status), payload


def _proxy_media_upload(stream, length, web_token, internal_token, content_type, digest,
                        path, digest_header, label):
    display = {"image": "图片", "video": "视频", "audio": "音频"}[label]
    if not internal_token:
        raise CLIAPIError(503, "CLI 内部授权未配置", "not_configured")
    target = urllib.parse.urlsplit(CONTENT_BASE)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost"} or target.path not in {"", "/"}:
        raise CLIAPIError(503, "CLI %s上传目标配置不安全" % display, "not_configured")
    connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=60)
    try:
        connection.putrequest("POST", path, skip_accept_encoding=True)
        connection.putheader("Authorization", "Bearer " + web_token)
        connection.putheader("X-HQ-Internal-Token", internal_token)
        connection.putheader(digest_header, digest)
        connection.putheader("Content-Type", content_type)
        connection.putheader("Content-Length", str(length))
        connection.putheader("Accept", "application/json")
        connection.putheader("User-Agent", "huangque-auth-cli-upload/1")
        connection.endheaders()
        remaining = length
        while remaining:
            chunk = stream.read(min(64 * 1024, remaining))
            if not chunk:
                raise CLIAPIError(400, "%s上传不完整" % display, "invalid_%s_upload" % label)
            connection.send(chunk)
            remaining -= len(chunk)
        response = connection.getresponse()
        raw, status = response.read(2 * 1024 * 1024 + 1), response.status
    except CLIAPIError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise CLIAPIError(502, "%s上传服务暂时不可用：" % display + str(exc)[:120], "upstream_unavailable")
    finally:
        connection.close()
    if len(raw) > 2 * 1024 * 1024:
        raise CLIAPIError(502, "%s上传服务响应过大" % display, "upstream_response_too_large")
    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        raise CLIAPIError(502, "%s上传服务返回了无效响应" % display, "invalid_upstream_response")
    if not isinstance(payload, dict):
        raise CLIAPIError(502, "%s上传服务返回了无效响应" % display, "invalid_upstream_response")
    return int(status), payload


def proxy_image_upload(stream, length, web_token, internal_token, content_type, digest):
    return _proxy_media_upload(
        stream, length, web_token, internal_token, content_type, digest,
        "/api/gen/cli/image-upload", "X-HQ-Image-SHA256", "image",
    )


def proxy_video_upload(stream, length, web_token, internal_token, content_type, digest):
    return _proxy_media_upload(
        stream, length, web_token, internal_token, content_type, digest,
        "/api/gen/cli/video-upload", "X-HQ-Video-SHA256", "video",
    )


def proxy_audio_upload(stream, length, web_token, internal_token, content_type, digest):
    return _proxy_media_upload(
        stream, length, web_token, internal_token, content_type, digest,
        "/api/gen/cli/audio-upload", "X-HQ-Audio-SHA256", "audio",
    )


def _plan(scope, kind, **values):
    return {"scope": scope, "kind": kind, **values}


def _matched_string(value, field, pattern, maximum=160):
    value = _string(value, field, 1, maximum)
    if not pattern.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def validate_idempotency_key(value):
    """Validate a first-party idempotency key without exposing the regex."""
    return _matched_string(value, "idempotency_key", _IDEMPOTENCY_KEY_RE, 128)


def _video_compose_decisions(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 200:
        raise CLIAPIError(400, "decisions 必须是包含 1-200 项的对象")
    decisions = {}
    for candidate_id, decision in value.items():
        candidate_id = _matched_string(candidate_id, "候选片段 ID", _VIDEO_COMPOSE_CANDIDATE_RE)
        decisions[candidate_id] = _enum(decision, "剪辑决定", ("keep", "remove"))
    return decisions


def _digital_presenter_fields(value):
    fields = {}
    if "title" in value:
        fields["title"] = _string(value["title"], "title", 1, 80)
    if "script_text" in value:
        fields["script_text"] = _string(value["script_text"], "script_text", 0, 20000)
    if "ratio" in value:
        fields["ratio"] = _enum(value["ratio"], "ratio", ("9:16", "16:9"))
    if "resolution" in value:
        fields["resolution"] = _enum(value["resolution"], "resolution", ("1080p",))
    if "voice_key" in value:
        fields["voice_key"] = _string(value["voice_key"], "voice_key", 0, 200)
    if "target_duration" in value:
        fields["target_duration"] = _integer(value["target_duration"], "target_duration", 30, 180)
    return fields


def _collect_url(value):
    url = _string(value, "url", 1, 2048)
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise CLIAPIError(400, "url 格式不合法")
    allowed = ("douyin.com", "iesdouyin.com", "xiaohongshu.com", "xhslink.com", "xhslink.cn",
               "bilibili.com", "b23.tv")
    channels_share = (parsed.scheme == "https" and host == "weixin.qq.com" and port in (None, 443)
                      and parsed.path.startswith("/sph/") and parsed.path[len("/sph/"):].isalnum())
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or port not in (None, 80, 443)
            or not (channels_share or any(
                host == suffix or host.endswith("." + suffix) for suffix in allowed))):
        raise CLIAPIError(400, "url 仅支持抖音、小红书、视频号或 B 站公开链接")
    return url


def _leads_payload(value):
    _strict_object(value, {"keyword", "platforms", "count", "pages", "channels_targets"},
                   ("platforms",))
    raw_platforms = value["platforms"]
    if not isinstance(raw_platforms, list) or not 1 <= len(raw_platforms) <= 3:
        raise CLIAPIError(400, "platforms 必须是包含 1-3 项的平台数组")
    platforms = [_enum(item, "platforms", ("douyin", "xhs", "channels")) for item in raw_platforms]
    if len(platforms) != len(set(platforms)):
        raise CLIAPIError(400, "platforms 不能重复")
    keyword = _string(value.get("keyword", ""), "keyword", 0, 120)
    raw_targets = value.get("channels_targets", [])
    if not isinstance(raw_targets, list) or len(raw_targets) > 20:
        raise CLIAPIError(400, "channels_targets 必须是最多 20 项的数组")
    targets = [_string(item, "channels_targets", 1, 120) for item in raw_targets]
    if len(targets) != len(set(targets)):
        raise CLIAPIError(400, "channels_targets 不能重复")
    if any(platform != "channels" for platform in platforms) and not keyword:
        raise CLIAPIError(400, "抖音或小红书获客必须提供 keyword")
    if "channels" in platforms and not targets:
        raise CLIAPIError(400, "视频号获客必须提供 channels_targets")
    return {
        "keyword": keyword, "platforms": platforms,
        "count": _integer(value.get("count", 20), "count", 1, 30),
        "pages": _integer(value.get("pages", 1), "pages", 1, 3),
        "channels_targets": targets,
    }


def action_plan(action, value):
    if not isinstance(value, dict):
        raise CLIAPIError(400, "input 必须是 JSON 对象")
    if action not in ACTION_CATALOG_MAP:
        raise CLIAPIError(404, "未知 CLI 能力", "unknown_action")
    if action == "account":
        _strict_object(value, set())
        return _plan("profile:read", "account")
    if action == "channels":
        _strict_object(value, set())
        return _plan("profile:read", "channels")
    if action == "pricing":
        _strict_object(value, set())
        return _plan("profile:read", "proxy", base=CONTENT_BASE, path="/api/gen/pricing")
    if action in {
            "text-video-capability", "text-video-templates",
            "text-video-styles", "text-video-voices"}:
        _strict_object(value, set())
        suffix = action.removeprefix("text-video-")
        return _plan("assets:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/text-video/" + suffix)
    if action == "inspiration-catalog":
        _strict_object(value, set())
        return _plan("inspiration:read", "proxy", base=ADMIN_BASE,
                     path="/api/admin/public/inspirations")
    if action == "inspiration-likes":
        _strict_object(value, set())
        return _plan("inspiration:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/inspiration/likes")
    if action == "inspiration-like":
        _strict_object(value, {"id", "favorite"}, ("id", "favorite"))
        if not isinstance(value["favorite"], bool):
            raise CLIAPIError(400, "favorite 必须是布尔值")
        return _plan("inspiration:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/inspiration/like", method="POST",
                     body={"id": _integer(value["id"], "id", 1, 2**63 - 1),
                           "favorite": value["favorite"]})
    if action == "leads-crm":
        _strict_object(value, {"lead_ids"})
        lead_ids = value.get("lead_ids", [])
        if not isinstance(lead_ids, list) or len(lead_ids) > 100:
            raise CLIAPIError(400, "lead_ids 必须是最多 100 项的数组")
        lead_ids = [_matched_string(item, "lead_id", _LEAD_ID_RE, 40) for item in lead_ids]
        path = "/api/gen/leads/crm"
        if lead_ids:
            path += "?" + urllib.parse.urlencode({"ids": ",".join(dict.fromkeys(lead_ids))})
        return _plan("leads:read", "proxy", base=CONTENT_BASE, path=path)
    if action == "leads-crm-upsert":
        _strict_object(value, {"lead_id", "intent", "follow_status", "follow_note"}, ("lead_id",))
        body = {"lead_id": _matched_string(value["lead_id"], "lead_id", _LEAD_ID_RE, 40)}
        if "intent" in value:
            body["intent"] = _enum(value["intent"], "intent", ("高意向", "咨询", "价格敏感", "围观"))
        if "follow_status" in value:
            body["follow_status"] = _enum(
                value["follow_status"], "follow_status", ("待跟进", "跟进中", "已加微", "已成交", "无效"))
        if "follow_note" in value:
            body["follow_note"] = _string(value["follow_note"], "follow_note", 0, 300)
        return _plan("leads:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/leads/crm", method="POST", body=body)
    if action in {"collect-content", "collect-video", "collect-transcript"}:
        _strict_object(value, {"url"}, ("url",))
        want = {
            "collect-content": "comments",
            "collect-video": "video",
            "collect-transcript": "transcript",
        }[action]
        return _plan(
            "generation:quote", "generation", generation_kind="collect",
            endpoint="/api/gen/collect", submit_base=LEADGEN_BASE,
            payload={"url": _collect_url(value["url"]), "want": [want]},
        )
    if action == "collect-search":
        _strict_object(value, {"platform", "keyword", "page"}, ("platform", "keyword"))
        return _plan(
            "generation:quote", "generation", generation_kind="collect_search",
            endpoint="/api/gen/collect_search", submit_base=LEADGEN_BASE,
            payload={
                "platform": _enum(value["platform"], "platform", ("douyin", "xhs")),
                "keyword": _string(value["keyword"], "keyword", 1, 120),
                "page": _integer(value.get("page", 1), "page", 1, 50),
            },
        )
    if action == "leads-generate":
        return _plan(
            "generation:quote", "generation", generation_kind="leads",
            endpoint="/api/gen/leads", submit_base=LEADGEN_BASE,
            payload=_leads_payload(value),
        )
    if action == "video-avatars":
        _strict_object(value, {"limit"})
        limit = _integer(value.get("limit", 120), "limit", 1, 120)
        return _plan("assets:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/video/avatars?" + urllib.parse.urlencode({"limit": limit}))
    if action == "audio-slots":
        _strict_object(value, set())
        return _plan("assets:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/audio/slots?include_points=0")
    if action == "short-drama-projects":
        _strict_object(value, {"page", "page_size"})
        query = urllib.parse.urlencode({
            "page": _integer(value.get("page", 1), "page", 1, 100000),
            "page_size": _integer(value.get("page_size", 20), "page_size", 1, 50),
        })
        return _plan("short-drama:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/short-drama/projects?" + query)
    if action in {"short-drama-project", "short-drama-conversation", "short-drama-preflight"}:
        _strict_object(value, {"project_id"}, ("project_id",))
        project_id = urllib.parse.quote(_identifier(value["project_id"], "project_id"), safe="")
        suffix = action.removeprefix("short-drama-")
        if suffix == "project":
            path = "/api/gen/short-drama/project?id=" + project_id
        else:
            path = "/api/gen/short-drama/%s?project_id=%s" % (suffix, project_id)
        return _plan("short-drama:read", "proxy", base=CONTENT_BASE, path=path)
    if action == "digital-ip-projects":
        _strict_object(value, set())
        return _plan("ip12:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/digital-ip/projects")
    if action in {"digital-ip-project", "digital-ip-report"}:
        _strict_object(value, {"project_id"}, ("project_id",))
        project_id = _identifier(value["project_id"], "project_id")
        suffix = "/report" if action == "digital-ip-report" else ""
        return _plan("ip12:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/digital-ip/projects/"
                     + urllib.parse.quote(project_id, safe="") + suffix)
    if action == "ip12-projects":
        _strict_object(value, set())
        return _plan("ip12:read", "proxy", base=HERMES_BASE, path="/api/conversations")
    if action in {"ip12-project", "ip12-report"}:
        _strict_object(value, {"project_id"}, ("project_id",))
        project_id = _identifier(value["project_id"], "project_id")
        suffix = "/reports" if action == "ip12-report" else ""
        return _plan("ip12:read", "proxy", base=HERMES_BASE,
                     path="/api/conversations/" + urllib.parse.quote(project_id, safe="") + suffix)
    if action == "ip12-create":
        _strict_object(value, {"title"}, ("title",))
        title = _string(value["title"], "title", 1, 120)
        return _plan("ip12:write", "proxy", base=HERMES_BASE, path="/api/conversations",
                     method="POST", body={"title": title})
    if action == "ip12-message":
        _strict_object(value, {"project_id", "message", "request_id"}, ("project_id", "message", "request_id"))
        project_id = _identifier(value["project_id"], "project_id")
        message = value["message"]
        if (not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000
                or any(ord(ch) < 32 and ch not in "\r\n\t" for ch in message)):
            raise CLIAPIError(400, "message 长度或内容不合法")
        message = message.strip()
        request_id = _identifier(value["request_id"], "request_id")
        request_hash = _hash(json.dumps(
            {"project_id": project_id, "message": message}, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))
        return _plan("ip12:chat", "proxy", base=HERMES_BASE, path="/api/chat-complete",
                     method="POST", body={"conversation_id": project_id, "message": message}, timeout=290,
                     headers={"Idempotency-Key": request_id}, request_id=request_id,
                     project_id=project_id, request_hash=request_hash)
    if action == "prompt-optimize":
        _strict_object(value, {"prompt", "kind"}, ("prompt", "kind"))
        prompt = _string(value["prompt"], "prompt", 1, 2000)
        kind = _enum(value["kind"], "kind", ("image", "video"))
        return _plan("prompt:optimize", "proxy", base=IMGGEN_BASE, path="/api/gen/reverse", method="POST",
                     body={"action": "optimize", "prompt": prompt, "kind": kind}, timeout=90)
    if action == "canvas-list":
        _strict_object(value, {"limit", "offset"})
        limit = _integer(value.get("limit", 20), "limit", 1, 100)
        offset = _integer(value.get("offset", 0), "offset", 0, 100000)
        return _plan("canvas:read", "canvas-list", limit=limit, offset=offset)
    if action == "canvas-get":
        _strict_object(value, {"board_id"}, ("board_id",))
        return _plan("canvas:read", "canvas-get", board_id=_identifier(value["board_id"], "board_id"))
    if action == "canvas-create":
        _strict_object(value, {"name", "prompt"}, ("name",))
        name = _string(value["name"], "name", 1, 48)
        prompt = _string(value.get("prompt", ""), "prompt", 0, 2000)
        nodes = []
        if prompt:
            nodes.append({"id": "n1", "type": "text", "x": 80, "y": 80, "collapsed": False,
                          "params": {"text": prompt}, "outputs": {"prompt": prompt}, "image": None,
                          "state": "", "note": ""})
        data = {"nid": len(nodes), "runLabel": "就绪", "zoom": 1,
                "scroll": {"left": 0, "top": 0}, "edges": [], "nodes": nodes}
        return _plan("canvas:write", "canvas-create", name=name, data=data)
    if action == "canvas-agent-plan":
        payload = _canvas_agent_payload(value)
        headers = {}
        if payload["scope"] == "collab":
            headers["X-Canvas-Board-Id"] = payload["project_id"].split(":", 1)[1]
        return _plan(
            "canvas:agent", "generation", generation_kind="canvas_agent",
            endpoint="/api/gen/canvas_agent", quote_endpoint="/api/gen/canvas-agent/quote",
            quote_body={}, payload=payload, quoted_cost_field="quoted_cost", submit_headers=headers,
        )
    if action == "canvas-ops":
        payload = _canvas_ops_payload(value)
        return _plan("canvas:edit", "canvas-ops", board_id=payload.pop("board_id"), payload=payload)
    if action == "tasks":
        _strict_object(value, {"days", "kind", "page", "page_size"})
        days = _integer(value.get("days", 30), "days", 1, 365)
        page = _integer(value.get("page", 1), "page", 1, 100000)
        page_size = _integer(value.get("page_size", 20), "page_size", 5, 50)
        kind = _string(value.get("kind", ""), "kind", 0, 32)
        if kind not in _TASK_KINDS:
            raise CLIAPIError(400, "kind 不是可查询的任务类型")
        query = urllib.parse.urlencode({"days": days, "kind": kind, "page": page, "page_size": page_size})
        return _plan("tasks:read", "proxy", base=CONTENT_BASE, path="/api/gen/points/history?" + query)
    if action == "task":
        _strict_object(value, {"job_id"}, ("job_id",))
        job_id = _integer(value["job_id"], "job_id", 1, 2**63 - 1)
        return _plan("tasks:read", "proxy", base=CONTENT_BASE, path="/api/gen/job/%d" % job_id)
    if action == "assets":
        _strict_object(value, {"kind", "limit", "offset"}, ("kind",))
        kind = _enum(value["kind"], "kind", ("image", "audio", "video", "copy", "collect", "leads", "breakdown"))
        limit = _integer(value.get("limit", 60), "limit", 1, 120)
        offset = _integer(value.get("offset", 0), "offset", 0, 100000)
        if kind == "image":
            path = "/api/gen/history?" + urllib.parse.urlencode({"kind": "image", "limit": limit, "offset": offset})
        elif kind in {"audio", "video"}:
            path = "/api/gen/%s/assets?" % kind + urllib.parse.urlencode({"limit": limit, "offset": offset})
        else:
            path = "/api/gen/assets?" + urllib.parse.urlencode({"kind": kind, "limit": limit, "offset": offset})
        return _plan("assets:read", "proxy", base=CONTENT_BASE, path=path)
    if action == "voices":
        _strict_object(value, set())
        return _plan("assets:read", "proxy", base=CONTENT_BASE, path="/api/gen/audio/voices")
    if action == "asset-favorite":
        _strict_object(value, {"kind", "key", "favorite"}, ("kind", "key", "favorite"))
        if not isinstance(value["favorite"], bool):
            raise CLIAPIError(400, "favorite 必须是布尔值")
        body = {
            "kind": _enum(value["kind"], "kind", ("image", "audio", "video", "avatar", "copy", "collect", "leads", "breakdown")),
            "key": _string(value["key"], "key", 1, 500), "favorite": value["favorite"],
        }
        return _plan("assets:write", "proxy", base=CONTENT_BASE, path="/api/gen/asset/favorite",
                     method="POST", body=body)
    if action == "asset-tags":
        _strict_object(value, {"kind", "key", "tags"}, ("kind", "key", "tags"))
        body = {
            "kind": _enum(value["kind"], "kind", ("image", "audio", "video", "avatar", "copy", "collect", "leads", "breakdown")),
            "key": _string(value["key"], "key", 1, 500), "tags": _tags(value["tags"]),
        }
        return _plan("assets:write", "proxy", base=CONTENT_BASE, path="/api/gen/asset/tags",
                     method="POST", body=body)
    if action in {"video-compose-projects", "video-compose-project"}:
        allowed = {"project_id"} if action.endswith("project") else set()
        _strict_object(value, allowed, allowed)
        path = "/api/gen/video-compose/projects"
        if allowed:
            project_id = _matched_string(value["project_id"], "project_id", _VIDEO_COMPOSE_PROJECT_RE)
            path += "/" + project_id
        return _plan("video-compose:read", "proxy", base=CONTENT_BASE, path=path)
    if action == "video-compose-create":
        _strict_object(value, {"source_asset_id"}, ("source_asset_id",))
        body = {"source_asset_id": _integer(value["source_asset_id"], "source_asset_id", 1, 2**63 - 1)}
        return _plan("video-compose:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/video-compose/projects", method="POST", body=body)
    if action in {"video-compose-analyze", "video-compose-review", "video-compose-render"}:
        allowed = {"project_id", "expected_revision"}
        if action == "video-compose-review":
            allowed.add("decisions")
        _strict_object(value, allowed, tuple(allowed))
        project_id = _matched_string(value["project_id"], "project_id", _VIDEO_COMPOSE_PROJECT_RE)
        body = {"expected_revision": _integer(value["expected_revision"], "expected_revision", 1, 2**63 - 1)}
        suffix = {"video-compose-analyze": "analyze-source", "video-compose-review": "edit-decisions",
                  "video-compose-render": "render"}[action]
        if action == "video-compose-review":
            body["decisions"] = _video_compose_decisions(value["decisions"])
        return _plan("video-compose:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/video-compose/projects/%s/%s" % (project_id, suffix),
                     method="POST", body=body, timeout=300 if action != "video-compose-review" else 30)
    if action == "digital-presenter-capability":
        _strict_object(value, set())
        return _plan("digital-presenter:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/digital-presenter/capability")
    if action == "digital-presenter-project":
        _strict_object(value, {"board_id", "project_id"}, ("board_id", "project_id"))
        board_id = _identifier(value["board_id"], "board_id")
        project_id = _matched_string(value["project_id"], "project_id", _DIGITAL_PRESENTER_PROJECT_RE)
        return _plan("digital-presenter:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/digital-presenter/project?id=" + urllib.parse.quote(project_id),
                     headers={"X-Canvas-Board-Id": board_id})
    if action in {"digital-presenter-create", "digital-presenter-update"}:
        control = {"board_id", "request_id"} if action.endswith("create") else {"board_id", "project_id", "revision"}
        editable = {"title", "script_text", "ratio", "resolution", "voice_key", "target_duration"}
        _strict_object(value, control | editable, tuple(control))
        board_id = _identifier(value["board_id"], "board_id")
        body = _digital_presenter_fields(value)
        headers = {"X-Canvas-Board-Id": board_id}
        if action == "digital-presenter-create":
            headers["Idempotency-Key"] = _matched_string(
                value["request_id"], "request_id", _IDEMPOTENCY_KEY_RE, 128)
            path, method = "/api/gen/digital-presenter/projects", "POST"
        else:
            if not body:
                raise CLIAPIError(400, "数字人口播更新至少需要一个字段")
            body.update({
                "project_id": _matched_string(value["project_id"], "project_id", _DIGITAL_PRESENTER_PROJECT_RE),
                "revision": _integer(value["revision"], "revision", 1, 2**63 - 1),
            })
            path, method = "/api/gen/digital-presenter/project", "PUT"
        return _plan("digital-presenter:write", "proxy", base=CONTENT_BASE,
                     path=path, method=method, body=body, headers=headers)
    if action in {
            "image-generate", "video-generate", "audio-generate",
            "video-lipsync",
            "digital-ip-text-generate", "digital-ip-batch-generate", "digital-ip-audio-generate",
            "cinematic-open-generate", "cinematic-motion-generate",
            "tryon-fast-generate", "tryon-classic-generate"}:
        payload, generation_kind, endpoint = _generation_payload(action, value)
        return _plan("generation:quote", "generation", generation_kind=generation_kind,
                     endpoint=endpoint, payload=payload)
    raise CLIAPIError(404, "未知 CLI 能力", "unknown_action")


def _generation_payload(action, value):
    if action == "video-lipsync":
        _strict_object(
            value,
            {"video_asset_id", "audio_asset_id", "quality", "dynamic_duration"},
            ("video_asset_id", "audio_asset_id"),
        )
        dynamic_duration = value.get("dynamic_duration", False)
        if not isinstance(dynamic_duration, bool):
            raise CLIAPIError(400, "dynamic_duration 必须是布尔值")
        return {
            "mode": "lipsync",
            "video_asset_id": _integer(
                value["video_asset_id"], "video_asset_id", 1, 2**63 - 1),
            "audio_asset_id": _integer(
                value["audio_asset_id"], "audio_asset_id", 1, 2**63 - 1),
            "lipsync_mode": _enum(
                value.get("quality", "speed"), "quality", ("speed", "precision")),
            "dynamic_duration": dynamic_duration,
        }, "video", "/api/gen/video"
    if action in {"digital-ip-text-generate", "digital-ip-audio-generate"}:
        mode = "text" if action == "digital-ip-text-generate" else "audio"
        required = ("text", "voice") if mode == "text" else ()
        _strict_object(value, {
            "avatar_id", "image_upload_id", "text", "voice", "audio_file", "audio_upload_id", "ratio", "motion",
            "subtitle", "subtitle_style", "subtitle_position",
        }, required)
        if bool(value.get("avatar_id")) == bool(value.get("image_upload_id")):
            raise CLIAPIError(400, "avatar_id 与 image_upload_id 必须且只能提供一个")
        if mode == "audio" and bool(value.get("audio_file")) == bool(value.get("audio_upload_id")):
            raise CLIAPIError(400, "audio_file 与 audio_upload_id 必须且只能提供一个")
        subtitle = value.get("subtitle", False)
        if not isinstance(subtitle, bool):
            raise CLIAPIError(400, "subtitle 必须是布尔值")
        body = {
            "mode": mode,
            "resolution": "1080p",
            "ratio": _enum(value.get("ratio", "9:16"), "ratio", ("9:16", "16:9", "1:1", "4:5", "5:4")),
            "motion": _enum(value.get("motion", "medium"), "motion", ("low", "medium", "high")),
            "subtitle": subtitle,
            "subtitle_style": _enum(value.get("subtitle_style", "white"), "subtitle_style", ("white", "variety", "bar")),
            "subtitle_position": _enum(value.get("subtitle_position", "bottom"), "subtitle_position", ("top", "upper", "center", "lower", "bottom")),
        }
        if value.get("avatar_id") is not None:
            body["avatar_id"] = _integer(value["avatar_id"], "avatar_id", 1, 2**63 - 1)
        else:
            body["image_upload_id"] = _upload_id(value["image_upload_id"], "image_upload_id")
        if mode == "text":
            body["text"] = _string(value["text"], "text", 1, 1000)
            body["voice"] = _string(value["voice"], "voice", 1, 128)
        else:
            if value.get("audio_file") is not None:
                body["audio_file"] = _string(value["audio_file"], "audio_file", 1, 500)
            else:
                body["audio_upload_id"] = _audio_upload_id(value["audio_upload_id"], "audio_upload_id")
        return body, "video", "/api/gen/video"
    if action == "digital-ip-batch-generate":
        _strict_object(value, {
            "avatars", "text", "voice", "ratio", "motion", "subtitle",
            "subtitle_style", "subtitle_position",
        }, ("avatars", "text", "voice"))
        items = value["avatars"]
        if not isinstance(items, list) or not 2 <= len(items) <= 5:
            raise CLIAPIError(400, "avatars 必须包含 2-5 项")
        avatars, seen = [], set()
        for index, item in enumerate(items, 1):
            _strict_object(item, {"avatar_id", "label"}, ("avatar_id",))
            avatar_id = _integer(item["avatar_id"], "avatar_id", 1, 2**63 - 1)
            if avatar_id in seen:
                raise CLIAPIError(400, "avatars 不能包含重复形象")
            seen.add(avatar_id)
            avatars.append({
                "avatar_id": avatar_id,
                "label": _string(item.get("label", "形象 %d" % index), "label", 1, 60),
            })
        single_input = {key: value[key] for key in (
            "text", "voice", "ratio", "motion", "subtitle", "subtitle_style",
            "subtitle_position",
        ) if key in value}
        single_input["avatar_id"] = avatars[0]["avatar_id"]
        body = _generation_payload("digital-ip-text-generate", single_input)[0]
        body.pop("avatar_id")
        body["avatars"] = avatars
        return body, "video_batch", "/api/gen/video/batch"
    if action == "cinematic-open-generate":
        _strict_object(value, {
            "avatar_id", "avatar_ids", "prompt", "ratio", "duration", "enhance_prompt",
            "reference_image_upload_ids", "reference_video_upload_ids",
        }, ("prompt",))
        if value.get("avatar_id") is not None and value.get("avatar_ids") is not None:
            raise CLIAPIError(400, "avatar_id 与 avatar_ids 只能选一个")
        raw_ids = value.get("avatar_ids")
        if raw_ids is None and value.get("avatar_id") is not None:
            raw_ids = [value["avatar_id"]]
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 3:
            raise CLIAPIError(400, "avatar_id 或 avatar_ids 必须提供 1-3 个形象")
        avatar_ids = []
        for item in raw_ids:
            clean = _integer(item, "avatar_ids", 1, 2**63 - 1)
            if clean in avatar_ids:
                raise CLIAPIError(400, "avatar_ids 不能重复")
            avatar_ids.append(clean)
        enhance_prompt = value.get("enhance_prompt", False)
        if not isinstance(enhance_prompt, bool):
            raise CLIAPIError(400, "enhance_prompt 必须是布尔值")
        body = {
            "cine_mode": "open",
            "avatar_ids": avatar_ids,
            "prompt": _string(value["prompt"], "prompt", 1, 2000),
            "resolution": "720p",
            "ratio": _enum(value.get("ratio", "9:16"), "ratio", ("9:16", "16:9", "1:1")),
            "duration": _integer(value.get("duration", 10), "duration", 4, 15),
            "enhance_prompt": enhance_prompt,
        }
        image_ids = value.get("reference_image_upload_ids")
        if image_ids is not None:
            image_limit = 9 - len(avatar_ids)
            if not isinstance(image_ids, list) or not 1 <= len(image_ids) <= image_limit:
                raise CLIAPIError(
                    400,
                    "reference_image_upload_ids 必须包含 1-%d 项（与形象共用 9 张额度）" % image_limit,
                )
            body["reference_image_upload_ids"] = [
                _upload_id(item, "reference_image_upload_ids") for item in image_ids
            ]
        video_ids = value.get("reference_video_upload_ids")
        if video_ids is not None:
            if not isinstance(video_ids, list) or not 1 <= len(video_ids) <= 3:
                raise CLIAPIError(400, "reference_video_upload_ids 必须包含 1-3 项")
            body["reference_video_upload_ids"] = [
                _video_upload_id(item, "reference_video_upload_ids") for item in video_ids
            ]
        return body, "cinematic", "/api/gen/cinematic"
    if action == "cinematic-motion-generate":
        _strict_object(value, {"avatar_id", "reference_video_upload_ids", "ratio"},
                       ("avatar_id", "reference_video_upload_ids"))
        references = value["reference_video_upload_ids"]
        if not isinstance(references, list) or len(references) != 1:
            raise CLIAPIError(400, "动作模仿需要且只接受 1 个参考视频")
        return {
            "cine_mode": "motion",
            "avatar_ids": [_integer(value["avatar_id"], "avatar_id", 1, 2**63 - 1)],
            "reference_video_upload_ids": [
                _video_upload_id(references[0], "reference_video_upload_ids")],
            "resolution": "720p",
            "ratio": _enum(value.get("ratio", "9:16"), "ratio", ("9:16", "16:9", "1:1")),
        }, "cinematic", "/api/gen/cinematic"
    if action == "tryon-fast-generate":
        _strict_object(value, {"person_image_upload_id", "clothes_upload_id", "seconds"},
                       ("person_image_upload_id", "clothes_upload_id"))
        return {
            "line": "2",
            "person_image_upload_id": _upload_id(value["person_image_upload_id"], "person_image_upload_id"),
            "clothes_upload_id": _upload_id(value["clothes_upload_id"], "clothes_upload_id"),
            "seconds": _integer(value.get("seconds", 6), "seconds", 5, 15),
        }, "tryon", "/api/gen/tryon"
    if action == "tryon-classic-generate":
        _strict_object(value, {
            "person_video_upload_id", "clothes_upload_id", "background_upload_id", "seconds",
        }, ("person_video_upload_id",))
        if not value.get("clothes_upload_id") and not value.get("background_upload_id"):
            raise CLIAPIError(400, "经典换装至少需要衣服图或背景图")
        body = {
            "line": "1",
            "person_video_upload_id": _video_upload_id(value["person_video_upload_id"], "person_video_upload_id"),
            "seconds": _integer(value.get("seconds", 6), "seconds", 1, 6),
        }
        for field in ("clothes_upload_id", "background_upload_id"):
            if value.get(field):
                body[field] = _upload_id(value[field], field)
        return body, "tryon", "/api/gen/tryon"
    if action == "image-generate":
        _strict_object(value, {
            "prompt", "provider", "ratio", "quality", "count", "variant",
            "model", "image_upload_id", "mask_upload_id", "reference_upload_ids",
        }, ("prompt",))
        provider = _enum(
            value.get("provider", "openai"), "provider",
            ("openai", "xiaole", "seedream", "banana"),
        )
        ratio = _string(value.get("ratio", "1:1"), "ratio", 1, 8)
        ratios = (
            ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
            if provider == "banana" else ("1:1", "9:16", "16:9", "3:4")
        )
        if ratio not in ratios:
            raise CLIAPIError(400, "ratio 不支持该图片引擎")
        body = {
            "prompt": _string(value["prompt"], "prompt", 1, 2000),
            "provider": provider,
            "ratio": ratio,
            "quality": _enum(value.get("quality", "hd"), "quality", ("std", "hd")),
            "count": _integer(value.get("count", 1), "count", 1, 4),
            "source_page": "banana",
        }
        if provider == "banana":
            body["model"] = _enum(value.get("model", "nb2"), "model", ("nb2", "pro"))
            if body["count"] not in {1, 2, 4}:
                raise CLIAPIError(400, "Nano Banana count 仅支持 1、2、4")
        elif "model" in value:
            raise CLIAPIError(400, "model 仅用于 Nano Banana")
        if "variant" in value:
            if body["provider"] != "seedream":
                raise CLIAPIError(400, "variant 仅用于 seedream")
            body["variant"] = _enum(value["variant"], "variant", ("std", "pro"))
        if "image_upload_id" in value:
            body["image_upload_id"] = _upload_id(value["image_upload_id"], "image_upload_id")
        if "mask_upload_id" in value:
            body["mask_upload_id"] = _upload_id(value["mask_upload_id"], "mask_upload_id")
        if "reference_upload_ids" in value:
            references = value["reference_upload_ids"]
            limit = {"openai": 16, "seedream": 10, "xiaole": 4, "banana": 14}[body["provider"]]
            if not isinstance(references, list) or not 1 <= len(references) <= limit:
                raise CLIAPIError(400, "reference_upload_ids 必须包含 1-%d 项" % limit)
            body["reference_upload_ids"] = []
            for item in references:
                clean = _upload_id(item, "reference_upload_ids")
                if clean not in body["reference_upload_ids"]:
                    body["reference_upload_ids"].append(clean)
        if body.get("image_upload_id") and body.get("reference_upload_ids"):
            raise CLIAPIError(400, "单参考图和多参考图不能同时使用")
        if body.get("mask_upload_id") and not body.get("image_upload_id"):
            raise CLIAPIError(400, "蒙版必须同时提供 image_upload_id")
        if body.get("mask_upload_id") and body["provider"] != "openai":
            raise CLIAPIError(400, "蒙版局部修改仅支持 openai")
        if body.get("mask_upload_id") and body["count"] != 1:
            raise CLIAPIError(400, "蒙版局部修改 count 必须为 1")
        return body, "image", "/api/gen/image"
    if action == "video-generate":
        _strict_object(value, {
            "prompt", "channel", "ratio", "duration", "seconds", "resolution",
            "model", "generate_audio", "reference_upload_ids",
        }, ("prompt",))
        channel = _enum(value.get("channel", "grok"), "channel", ("grok", "micro", "omni", "minimax", "sora"))
        rule = _VIDEO_CHANNEL_RULES[channel]
        if channel == "sora":
            if "duration" in value or "generate_audio" in value:
                raise CLIAPIError(400, "Sora 使用 seconds，且不支持 generate_audio")
            model = _enum(value.get("model", rule["default_model"]), "model", tuple(rule["models"]))
            body = {
                "prompt": _string(value["prompt"], "prompt", 1, 2000),
                "channel": "sora",
                "model": model,
                "seconds": _integer(value.get("seconds", rule["default_seconds"]), "seconds", min(rule["seconds"]), max(rule["seconds"])),
                "ratio": _enum(value.get("ratio", rule["default_ratio"]), "ratio", tuple(rule["ratios"])),
                "resolution": _enum(value.get("resolution", rule["default_resolution"]), "resolution", tuple(rule["model_resolutions"][model])),
                "source_page": "video",
            }
            if body["seconds"] not in set(rule["seconds"]):
                raise CLIAPIError(400, "Sora seconds 仅支持 4、8、12")
            if "reference_upload_ids" in value:
                references = value["reference_upload_ids"]
                if not isinstance(references, list) or not 1 <= len(references) <= rule["reference_max"]:
                    raise CLIAPIError(400, "Sora reference_upload_ids 必须包含 1 项")
                body["reference_upload_ids"] = [_upload_id(references[0], "reference_upload_ids")]
            return body, "sora_video", "/api/gen/sora_video"
        if "seconds" in value:
            raise CLIAPIError(400, "seconds 仅用于 Sora")
        body = {
            "prompt": _string(value["prompt"], "prompt", 1, 2000),
            "channel": channel,
            "ratio": _enum(value.get("ratio", rule["default_ratio"]), "ratio", tuple(rule["ratios"])),
            "duration": _integer(value.get("duration", rule["default_duration"]), "duration", rule["duration"][0], rule["duration"][1]),
            "resolution": _enum(value.get("resolution", rule["default_resolution"]), "resolution", tuple(rule["resolutions"])),
            "source_page": "video",
        }
        selected_model = rule["default_model"]
        if "model" in value:
            if channel != "grok":
                raise CLIAPIError(400, "model 参数仅用于 grok")
            selected_model = _enum(value["model"], "model", tuple(rule["models"]))
            body["model"] = selected_model
        if "generate_audio" in value:
            if not isinstance(value["generate_audio"], bool) or channel != "micro":
                raise CLIAPIError(400, "generate_audio 仅用于 micro 且必须是布尔值")
            body["generate_audio"] = value["generate_audio"]
        if "reference_upload_ids" in value:
            references = value["reference_upload_ids"]
            limit = rule["reference_max"]
            if not isinstance(references, list) or not 1 <= len(references) <= limit:
                raise CLIAPIError(400, "reference_upload_ids 必须包含 1-%d 项" % limit)
            body["reference_upload_ids"] = []
            for item in references:
                clean = _upload_id(item, "reference_upload_ids")
                if clean not in body["reference_upload_ids"]:
                    body["reference_upload_ids"].append(clean)
        if channel == "grok":
            if selected_model in rule["reference_required_models"] and not body.get("reference_upload_ids"):
                raise CLIAPIError(400, "Grok Video 1.5 至少需要 1 张参考图")
            if body.get("reference_upload_ids") and body["resolution"] not in rule["reference_resolutions"]:
                raise CLIAPIError(400, "Grok 参考图生成仅支持 720p")
        return body, "xiaole_video", "/api/gen/xiaole_video"
    _strict_object(value, {"text", "voice", "speed", "pitch", "volume"}, ("text",))
    body = {"text": _string(value["text"], "text", 1, 1000), "source_page": "audio"}
    if "voice" in value:
        body["voice"] = _string(value["voice"], "voice", 1, 128)
    for field, default, minimum, maximum in (("pitch", 0, -12, 12), ("volume", 0, -50, 100)):
        body[field] = _integer(value.get(field, default), field, minimum, maximum)
    speed = value.get("speed", 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.5 <= float(speed) <= 2:
        raise CLIAPIError(400, "speed 必须是 0.5-2 的数字")
    body["speed"] = round(float(speed), 1)
    return body, "audio", "/api/gen/audio"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def issue_quote(secret, username, generation_kind, payload, cost, now=None):
    if not secret:
        raise CLIAPIError(503, "CLI 报价签名未配置", "not_configured")
    now = int(time.time() if now is None else now)
    cost = int(cost)
    if cost <= 0:
        raise CLIAPIError(502, "生成费用无效", "invalid_quote")
    claims = {
        "v": 1, "u": username, "k": generation_kind,
        "h": hashlib.sha256(_canonical(payload)).hexdigest(),
        "c": cost, "e": now + QUOTE_TTL, "n": secrets.token_hex(16),
    }
    encoded = base64.urlsafe_b64encode(_canonical(claims)).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return encoded + "." + signature, claims


def verify_quote(secret, token, username, generation_kind, payload, now=None):
    if not secret:
        raise CLIAPIError(503, "CLI 报价签名未配置", "not_configured")
    now = int(time.time() if now is None else now)
    try:
        encoded, signature = str(token or "").split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except Exception:
        raise CLIAPIError(400, "报价凭证无效，请重新报价", "invalid_quote")
    payload_hash = hashlib.sha256(_canonical(payload)).hexdigest()
    if (claims.get("v") != 1 or claims.get("u") != username or claims.get("k") != generation_kind
            or claims.get("h") != payload_hash or not isinstance(claims.get("c"), int)
            or not isinstance(claims.get("e"), int) or not isinstance(claims.get("n"), str)):
        raise CLIAPIError(409, "报价与当前账号或参数不匹配，请重新报价", "quote_mismatch")
    if claims["e"] <= now:
        raise CLIAPIError(409, "报价已过期，请重新报价", "quote_expired")
    return claims
