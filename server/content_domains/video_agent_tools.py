"""Allowlisted HQ CLI tools and explicit paid-action confirmation state."""

import json
import re
import secrets
import time
import hashlib
from contextlib import closing

from . import hq_cli_executor, submission_idempotency


MAX_TOOL_RESULT_BYTES = 24 * 1024
MAX_PENDING_TTL = 600
MIN_PENDING_TTL = 30

# 对账安全期：覆盖 CLI 子进程超时(35s) + 鉴权代理提交超时(30s) 的最坏在途。
# 安全期内绝不把"暂无幂等记录"收敛为未提交，也绝不放行同输入重新报价。
RECONCILE_SAFETY_SECONDS = 90

# 能力 → 内容服务任务 kind：陈旧 processing 对账时按任务账本收敛。
_CAPABILITY_JOB_KINDS = {
    "video-generate": ("sora_video", "xiaole_video"),
    "digital-ip-text-generate": ("video",),
    "cinematic-open-generate": ("cinematic",),
    "cinematic-motion-generate": ("cinematic",),
    "tryon-fast-generate": ("tryon",),
    "tryon-classic-generate": ("tryon",),
}

# 能力 → 内容服务幂等端点。对账必须同时绑定真实提交端点，避免同一
# idem_key 在其他写接口留下的响应被误认成当前付费视频提交。
_CAPABILITY_ENDPOINTS = {
    "video-generate": ("/api/gen/sora_video", "/api/gen/xiaole_video"),
    "digital-ip-text-generate": ("/api/gen/video",),
    "cinematic-open-generate": ("/api/gen/cinematic",),
    "cinematic-motion-generate": ("/api/gen/cinematic",),
    "tryon-fast-generate": ("/api/gen/tryon",),
    "tryon-classic-generate": ("/api/gen/tryon",),
}

# 渠道规则只有一份正本：部署的 hq_cli.catalog。Agent 报价工具的
# 视频参数校验直接由它推导，杜绝与真实 CLI 合同漂移（契约测试守护）。
try:
    _CLI_CATALOG = hq_cli_executor.load_cli_catalog()
    VIDEO_CHANNEL_RULES = _CLI_CATALOG.VIDEO_CHANNEL_RULES
    _VIDEO_RATIO_ENUM = sorted({
        value for rule in VIDEO_CHANNEL_RULES.values() for value in rule["ratios"]
    })
    _VIDEO_RESOLUTION_ENUM = sorted({
        value for rule in VIDEO_CHANNEL_RULES.values() for value in rule["resolutions"]
    })
except hq_cli_executor.CLIExecutionError:
    VIDEO_CHANNEL_RULES = None
    _VIDEO_RATIO_ENUM = ["1:1", "16:9", "2:3", "21:9", "3:2", "3:4", "4:3", "9:16", "adaptive"]
    _VIDEO_RESOLUTION_ENUM = ["480p", "720p", "1024p", "1080p", "2k"]


class ToolError(ValueError):
    def __init__(self, code, message, status=422, *, unknown_outcome=False,
                 pending_action=None):
        super().__init__(message)
        self.code = str(code or "tool_failed")
        self.status = int(status)
        self.unknown_outcome = bool(unknown_outcome)
        self.pending_action = pending_action if isinstance(pending_action, dict) else None


def _object(properties=None, required=None):
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


LIMIT = {"type": "integer", "minimum": 1, "maximum": 120}
AVATAR_ID = {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}
UPLOAD_ID = {"type": "string", "minLength": 36, "maxLength": 36}


_SPECS = {
    "hq_get_account": {
        "capability": "account", "scope": "profile:read", "mode": "read",
        "description": "读取当前账号、会员、点数和本次授权范围。不会修改数据。",
        "parameters": _object(), "title": "账号信息",
    },
    "hq_list_channels": {
        "capability": "channels", "scope": "profile:read", "mode": "read",
        "description": "读取当前账号可用的黄雀能力目录。不会修改数据。",
        "parameters": _object(), "title": "可用能力",
    },
    "hq_get_pricing": {
        "capability": "pricing", "scope": "profile:read", "mode": "read",
        "description": "读取黄雀当前点数价格目录。不会提交生成。",
        "parameters": _object(), "title": "价格目录",
    },
    "hq_list_video_avatars": {
        "capability": "video-avatars", "scope": "assets:read", "mode": "read",
        "description": "读取当前账号已就绪的数字人形象。",
        "parameters": _object({"limit": LIMIT}), "title": "数字人形象",
    },
    "hq_list_voices": {
        "capability": "voices", "scope": "assets:read", "mode": "read",
        "description": "读取当前账号可用的公共和个人音色。",
        "parameters": _object(), "title": "可用音色",
    },
    "hq_list_assets": {
        "capability": "assets", "scope": "assets:read", "mode": "read",
        "description": "按类型读取当前账号素材；只返回已有资产元数据。",
        "parameters": _object({
            "kind": {"type": "string", "enum": ["image", "audio", "video", "copy", "collect", "leads", "breakdown"]},
            "limit": LIMIT,
            "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
        }, ["kind"]), "title": "素材列表",
    },
    "hq_list_tasks": {
        "capability": "tasks", "scope": "tasks:read", "mode": "read",
        "description": "读取当前账号最近生成任务、状态、扣点和退款结果。",
        "parameters": _object({
            "days": {"type": "integer", "minimum": 1, "maximum": 365},
            "kind": {"type": "string", "maxLength": 32},
            "page": {"type": "integer", "minimum": 1, "maximum": 100000},
            "page_size": {"type": "integer", "minimum": 5, "maximum": 50},
        }), "title": "任务列表",
    },
    "hq_get_task": {
        "capability": "task", "scope": "tasks:read", "mode": "read",
        "description": "按任务号读取当前账号的一项生成任务。",
        "parameters": _object({"job_id": AVATAR_ID}, ["job_id"]), "title": "任务详情",
    },
    "hq_quote_video_generate": {
        "capability": "video-generate", "scope": "generation:quote", "mode": "quote",
        "description": "根据文字与可选参考图取得视频生成报价；只报价，不扣点、不提交。",
        "parameters": _object({
            "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
            "channel": {"type": "string", "enum": sorted(VIDEO_CHANNEL_RULES) if VIDEO_CHANNEL_RULES else ["grok", "micro", "omni", "minimax", "sora"]},
            "ratio": {"type": "string", "enum": _VIDEO_RATIO_ENUM},
            "duration": {"type": "integer", "minimum": 1, "maximum": 15},
            "seconds": {"type": "integer", "enum": [4, 8, 12]},
            "resolution": {"type": "string", "enum": _VIDEO_RESOLUTION_ENUM},
            "model": {"type": "string", "enum": ["grok-imagine-video", "grok-imagine-video-1.5", "sora-2", "sora-2-pro"]},
            "generate_audio": {"type": "boolean"},
            "reference_upload_ids": {"type": "array", "minItems": 1, "maxItems": 9, "items": UPLOAD_ID},
        }, ["prompt"]), "title": "自由生成视频",
        # 与 hq_cli.catalog.VIDEO_CHANNEL_RULES 同一份数据；_parse_arguments
        # 用它与真实 CLI 完全一致地校验渠道约束（21:9/adaptive/2k 等）。
        "channel_rules": VIDEO_CHANNEL_RULES,
    },
    "hq_quote_talking_video": {
        "capability": "digital-ip-text-generate", "scope": "generation:quote", "mode": "quote",
        "description": "为一个数字人和一段文案取得口播视频报价；只报价，不扣点、不提交。",
        "parameters": _object({
            "avatar_id": AVATAR_ID,
            "image_upload_id": UPLOAD_ID,
            "text": {"type": "string", "minLength": 1, "maxLength": 1000},
            "voice": {"type": "string", "minLength": 1, "maxLength": 128},
            "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1", "4:5", "5:4"]},
            "motion": {"type": "string", "enum": ["low", "medium", "high"]},
            "subtitle": {"type": "boolean"},
            "subtitle_style": {"type": "string", "enum": ["white", "variety", "bar"]},
            "subtitle_position": {"type": "string", "enum": ["top", "upper", "center", "lower", "bottom"]},
        }, ["text", "voice"]), "title": "数字人口播",
        "one_of": ("avatar_id", "image_upload_id"),
    },
    "hq_quote_story_video": {
        "capability": "cinematic-open-generate", "scope": "generation:quote", "mode": "quote",
        "description": "用已有电影化身和剧情描述取得开放式视频报价；只报价，不扣点、不提交。",
        "parameters": _object({
            "avatar_id": AVATAR_ID,
            "avatar_ids": {"type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True, "items": AVATAR_ID},
            "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
            "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1"]},
            "duration": {"type": "integer", "minimum": 4, "maximum": 15},
            "enhance_prompt": {"type": "boolean"},
            "reference_image_upload_ids": {"type": "array", "minItems": 1, "maxItems": 8, "items": UPLOAD_ID},
            "reference_video_upload_ids": {"type": "array", "minItems": 1, "maxItems": 3, "items": UPLOAD_ID},
        }, ["prompt"]), "title": "剧情故事",
        "one_of": ("avatar_id", "avatar_ids"),
    },
    "hq_quote_motion_video": {
        "capability": "cinematic-motion-generate", "scope": "generation:quote", "mode": "quote",
        "description": "用一个数字人和一段参考视频取得动作模仿报价；只报价，不扣点、不提交。",
        "parameters": _object({
            "avatar_id": AVATAR_ID,
            "reference_video_upload_ids": {"type": "array", "minItems": 1, "maxItems": 1, "items": UPLOAD_ID},
            "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1"]},
        }, ["avatar_id", "reference_video_upload_ids"]), "title": "动作模仿",
    },
    "hq_quote_tryon_fast_video": {
        "capability": "tryon-fast-generate", "scope": "generation:quote", "mode": "quote",
        "description": "用人物图片和服装图片取得快速换装视频报价；只报价，不扣点、不提交。",
        "parameters": _object({
            "person_image_upload_id": UPLOAD_ID, "clothes_upload_id": UPLOAD_ID,
            "seconds": {"type": "integer", "minimum": 5, "maximum": 15},
        }, ["person_image_upload_id", "clothes_upload_id"]), "title": "快速换装视频",
    },
    "hq_quote_tryon_classic_video": {
        "capability": "tryon-classic-generate", "scope": "generation:quote", "mode": "quote",
        "description": "用人物视频和服装或背景图片取得经典换装换背景报价；只报价，不扣点、不提交。",
        "parameters": _object({
            "person_video_upload_id": UPLOAD_ID, "clothes_upload_id": UPLOAD_ID,
            "background_upload_id": UPLOAD_ID,
            "seconds": {"type": "integer", "minimum": 1, "maximum": 6},
        }, ["person_video_upload_id"]), "title": "换装换背景视频",
        "at_least_one": ("clothes_upload_id", "background_upload_id"),
    },
}


# Publish the same conditional requirements that the runtime enforces.  These
# clauses are part of the model-visible tool contract, not merely local checks.
for _spec in _SPECS.values():
    if _spec.get("one_of"):
        _spec["parameters"]["oneOf"] = [
            {"required": [field]} for field in _spec["one_of"]
        ]
    if _spec.get("at_least_one"):
        _spec["parameters"]["anyOf"] = [
            {"required": [field]} for field in _spec["at_least_one"]
        ]


TOOL_DEFINITIONS = [
    {
        "type": "function", "name": name,
        "description": spec["description"],
        "parameters": spec["parameters"], "strict": True,
    }
    for name, spec in _SPECS.items()
]


_PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS video_agent_pending_actions(
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    capability TEXT NOT NULL,
    input_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    quote_token TEXT NOT NULL,
    cost INTEGER NOT NULL DEFAULT 0,
    points INTEGER,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    idempotency_key TEXT,
    submission_key TEXT,
    payload_json TEXT,
    result_json TEXT,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_video_agent_pending_user
ON video_agent_pending_actions(username, created_at DESC);
"""

_PENDING_UNIQUE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_video_agent_pending_live_input
ON video_agent_pending_actions(username, capability, input_hash)
WHERE status IN ('awaiting_confirmation','confirming','result_unknown')
"""


# 报价热路径每次都会调用 ensure_tables；建表/迁移/去重扫描幂等且昂贵，
# 每个进程对同一 db_factory 只执行一次。总是执行的剩余部分只有
# CREATE IF NOT EXISTS 与索引存在性检查，代价恒定。
_PENDING_DDL_DONE = set()


def ensure_tables(db_factory, recover_confirming=False):
    if not callable(db_factory):
        raise ToolError("pending_store_unavailable", "视频操作确认服务暂时不可用", 503)
    try:
        with closing(db_factory()) as conn:
            conn.executescript(_PENDING_SCHEMA)
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(video_agent_pending_actions)"
            ).fetchall()}
            if "submission_key" not in columns:
                conn.execute(
                    "ALTER TABLE video_agent_pending_actions "
                    "ADD COLUMN submission_key TEXT"
                )
            if "payload_json" not in columns:
                conn.execute(
                    "ALTER TABLE video_agent_pending_actions "
                    "ADD COLUMN payload_json TEXT"
                )
            marker = id(db_factory)
            if marker not in _PENDING_DDL_DONE:
                # Pre-fingerprint builds keyed cards by the model's raw JSON. An
                # unsubmitted legacy quote is safe to retire and re-quote; legacy
                # confirming/unknown cards stay as hard barriers below.
                conn.execute(
                    "UPDATE video_agent_pending_actions "
                    "SET status='cancelled',quote_token='',error_code='legacy_fingerprint_migrated' "
                    "WHERE status='awaiting_confirmation' AND instr(input_hash,':')=0"
                )
                _PENDING_DDL_DONE.add(marker)
                if len(_PENDING_DDL_DONE) > 8:
                    _PENDING_DDL_DONE.clear()
            index_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='idx_video_agent_pending_live_input'"
            ).fetchone()
            index_sql = str(index_row[0] or "") if index_row else ""
            if not index_row or "result_unknown" not in index_sql:
                conn.execute("DROP INDEX IF EXISTS idx_video_agent_pending_live_input")
                # A developer database may contain duplicate cards created by
                # an earlier build. Keep the newest one and retire older cards
                # before adding the invariant instead of making startup fail.
                live = conn.execute(
                    "SELECT id,username,capability,input_hash "
                    "FROM video_agent_pending_actions "
                    "WHERE status IN ('awaiting_confirmation','confirming','result_unknown') "
                    "ORDER BY CASE status WHEN 'result_unknown' THEN 0 "
                    "WHEN 'confirming' THEN 1 ELSE 2 END,created_at DESC,rowid DESC"
                ).fetchall()
                seen = set()
                for row in live:
                    identity = (row[1], row[2], row[3])
                    if identity in seen:
                        conn.execute(
                            "UPDATE video_agent_pending_actions "
                            "SET status='cancelled',quote_token='',error_code=? WHERE id=?",
                            ("duplicate_pending_migrated", row[0]),
                        )
                    else:
                        seen.add(identity)
                conn.execute(_PENDING_UNIQUE_INDEX_SQL)
            if recover_confirming:
                conn.execute(
                    "UPDATE video_agent_pending_actions "
                    "SET status='result_unknown',quote_token='',"
                    "error_code='interrupted_confirmation',updated_at=? "
                    "WHERE status='confirming'",
                    (int(time.time()),),
                )
            # Quote credentials are useful only while a card is awaiting a
            # click or actively being submitted. Scrub tokens left by older
            # builds as soon as any process opens the store.
            conn.execute(
                "UPDATE video_agent_pending_actions SET quote_token='' "
                "WHERE status NOT IN ('awaiting_confirmation','confirming') "
                "AND quote_token<>''"
            )
            conn.commit()
    except Exception as error:
        raise ToolError("pending_store_unavailable", "视频操作确认服务暂时不可用", 503) from error


def _validate(value, schema, path="arguments"):
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ToolError("tool_arguments_invalid", path + " 必须是对象")
        properties = schema.get("properties") or {}
        unknown = set(value) - set(properties)
        if unknown:
            raise ToolError("tool_arguments_invalid", path + " 包含未允许字段")
        for key in schema.get("required") or []:
            if key not in value:
                raise ToolError("tool_arguments_invalid", path + "." + key + " 为必填项")
        for key, item in value.items():
            _validate(item, properties[key], path + "." + key)
        return
    if expected == "string":
        if not isinstance(value, str):
            raise ToolError("tool_arguments_invalid", path + " 必须是字符串")
        if len(value) < int(schema.get("minLength", 0)) or len(value) > int(schema.get("maxLength", 1_000_000)):
            raise ToolError("tool_arguments_invalid", path + " 长度无效")
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            raise ToolError("tool_arguments_invalid", path + " 格式无效")
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError("tool_arguments_invalid", path + " 必须是整数")
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise ToolError("tool_arguments_invalid", path + " 超出范围")
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError("tool_arguments_invalid", path + " 必须是数字")
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise ToolError("tool_arguments_invalid", path + " 必须是布尔值")
    elif expected == "array":
        if not isinstance(value, list):
            raise ToolError("tool_arguments_invalid", path + " 必须是数组")
        if len(value) < int(schema.get("minItems", 0)) or len(value) > int(schema.get("maxItems", 10_000)):
            raise ToolError("tool_arguments_invalid", path + " 项目数量无效")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise ToolError("tool_arguments_invalid", path + " 不能包含重复项目")
        for index, item in enumerate(value):
            _validate(item, schema.get("items") or {}, "%s[%d]" % (path, index))
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError("tool_arguments_invalid", path + " 不是允许值")


def _validate_video_channel_rules(arguments, rules, path="arguments"):
    """Enforce the exact channel contract of the real CLI (catalog allOf).

    Mirrors hq_cli.catalog._video_channel_schema(): per-channel ratio /
    resolution / duration-or-seconds / model / generate_audio /
    reference_upload_ids rules, plus grok reference-resolution and sora
    model-resolution clauses.
    """
    if not rules:
        raise ToolError(
            "cli_not_installed", "黄雀 CLI 运行模块未部署，无法校验视频参数", 503
        )
    channel = str(arguments.get("channel") or "grok").strip().lower()
    rule = rules.get(channel)
    if not rule:
        raise ToolError("tool_arguments_invalid", path + ".channel 不是可用渠道")
    if "ratio" in arguments and arguments["ratio"] not in rule["ratios"]:
        raise ToolError(
            "tool_arguments_invalid",
            "%s.ratio 不适用于渠道 %s" % (path, channel),
        )
    if "resolution" in arguments and arguments["resolution"] not in rule["resolutions"]:
        raise ToolError(
            "tool_arguments_invalid",
            "%s.resolution 不适用于渠道 %s" % (path, channel),
        )
    if rule["duration"]:
        if "seconds" in arguments:
            raise ToolError(
                "tool_arguments_invalid",
                "%s.seconds 不适用于渠道 %s（该渠道使用 duration）" % (path, channel),
            )
        if "duration" in arguments and not (
            rule["duration"][0] <= arguments["duration"] <= rule["duration"][1]
        ):
            raise ToolError(
                "tool_arguments_invalid",
                "%s.duration 超出渠道 %s 的范围" % (path, channel),
            )
    else:
        if "duration" in arguments:
            raise ToolError(
                "tool_arguments_invalid",
                "%s.duration 不适用于渠道 %s（该渠道使用 seconds）" % (path, channel),
            )
        if "seconds" in arguments and arguments["seconds"] not in rule["seconds"]:
            raise ToolError(
                "tool_arguments_invalid",
                "%s.seconds 不适用于渠道 %s" % (path, channel),
            )
    if rule["models"]:
        if "model" in arguments and arguments["model"] not in rule["models"]:
            raise ToolError(
                "tool_arguments_invalid",
                "%s.model 不适用于渠道 %s" % (path, channel),
            )
    elif "model" in arguments:
        raise ToolError(
            "tool_arguments_invalid",
            "%s.model 不适用于渠道 %s" % (path, channel),
        )
    # 与真实 CLI 一致（cli.py）：只要字段出现就按渠道能力判定，
    # 显式 false 同样会被非 Micro 渠道拒绝。
    if not rule["generate_audio"] and "generate_audio" in arguments:
        raise ToolError(
            "tool_arguments_invalid",
            "%s.generate_audio 不适用于渠道 %s" % (path, channel),
        )
    if "reference_upload_ids" in arguments:
        if len(arguments["reference_upload_ids"]) > int(rule["reference_max"]):
            raise ToolError(
                "tool_arguments_invalid",
                "%s.reference_upload_ids 渠道 %s 最多 %d 张" % (
                    path, channel, int(rule["reference_max"]),
                ),
            )
    if channel == "grok":
        if (
            "reference_upload_ids" in arguments
            and "resolution" in arguments
            and arguments["resolution"] not in rule["reference_resolutions"]
        ):
            raise ToolError(
                "tool_arguments_invalid",
                "%s.resolution 带参考图时渠道 grok 仅支持 %s" % (
                    path, "、".join(rule["reference_resolutions"]),
                ),
            )
        if (
            arguments.get("model") == "grok-imagine-video-1.5"
            and "reference_upload_ids" not in arguments
        ):
            raise ToolError(
                "tool_arguments_invalid",
                "%s.model=grok-imagine-video-1.5 必须提供 reference_upload_ids" % path,
            )
    if channel == "sora":
        model = arguments.get("model")
        allowed = list(rule["model_resolutions"].get(model, ["720p"])) if model else ["720p"]
        if "resolution" in arguments and arguments["resolution"] not in allowed:
            raise ToolError(
                "tool_arguments_invalid",
                "%s.resolution 不适用于渠道 sora 的该模型" % path,
            )


def _parse_arguments(raw_arguments, spec):
    if isinstance(raw_arguments, str):
        try:
            value = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            raise ToolError("tool_arguments_invalid", "工具参数不是有效 JSON") from error
    else:
        value = raw_arguments
    _validate(value, spec["parameters"])
    if "channel_rules" in spec:
        # CLI 模块未部署时 rules 为 None：校验必然失败（fail-closed）。
        _validate_video_channel_rules(value, spec["channel_rules"])
    one_of = spec.get("one_of")
    if one_of and sum(1 for key in one_of if key in value) != 1:
        raise ToolError("tool_arguments_invalid", "必须且只能选择一个数字人字段")
    at_least_one = spec.get("at_least_one")
    if at_least_one and not any(key in value for key in at_least_one):
        raise ToolError("tool_arguments_invalid", "至少需要提供服装或背景素材")
    return value


_SENSITIVE_KEYS = {
    "authorization", "cookie", "set-cookie", "password", "credentials",
    "access_token", "refresh_token", "quote_token", "api_key", "secret",
}


# Tool results cross the DeepSeek trust boundary. Generic token redaction is not
# sufficient because private prompts, signed URLs, local paths, and upstream IDs
# often use otherwise harmless key names. Keep explicit capability allowlists.
_ACCOUNT_USER_FIELDS = {
    "points", "role", "membership_active", "membership_tier",
    "membership_name", "membership_started_at", "membership_expires_at",
    "must_change",
}
_CHANNEL_FIELDS = {
    "id", "key", "name", "label", "provider", "category", "access",
    "available", "enabled", "capabilities", "features", "selector",
}
_PRICING_FIELDS = {
    "id", "key", "name", "label", "description", "category", "kind",
    "channel", "model", "points", "cost", "unit", "duration", "seconds",
    "resolution", "active", "enabled",
}
_AVATAR_FIELDS = {"id", "name", "status", "created_at", "updated_at"}
_VOICE_FIELDS = {
    "id", "scope", "voice_key", "display_name", "slot_id", "status",
    "created_at", "updated_at",
}
_ASSET_FIELDS = {
    "id", "asset_id", "upload_id", "job_id", "kind", "type", "asset_kind",
    "name", "display_name", "voice_name", "status", "duration", "width",
    "height", "size", "created_at", "updated_at",
}
_TASK_FIELDS = {
    "id", "task_id", "job_id", "kind", "func", "status", "status_label",
    "phase", "progress", "cost", "amount", "points", "refunded",
    "created_at", "updated_at", "error_code", "asset_id",
}
_LIST_META_FIELDS = {
    "total", "count", "limit", "offset", "page", "page_size", "total_pages",
    "days", "kind", "points", "has_more",
}


def _safe_value(value, depth=0):
    if depth > 6:
        return "[已截断]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:60]:
            clean_key = str(key)[:100]
            lowered = clean_key.lower()
            if lowered in _SENSITIVE_KEYS or "token" in lowered or "password" in lowered or "secret" in lowered:
                continue
            result[clean_key] = _safe_value(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_safe_value(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return value[:1000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def _bounded_result(value):
    safe = _safe_value(value)
    encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_TOOL_RESULT_BYTES:
        return safe
    return {"truncated": True, "preview": encoded[:12000]}


def _project_confirmation_result(value):
    """Project a paid-submit response into the browser's minimal task handle."""
    if not isinstance(value, dict):
        return {}
    projected = {}
    job_id = value.get("job_id")
    if isinstance(job_id, int) and not isinstance(job_id, bool) and job_id > 0:
        projected["job_id"] = job_id
    elif isinstance(job_id, str) and re.fullmatch(r"[1-9][0-9]{0,18}", job_id):
        projected["job_id"] = job_id
    for key in ("cost", "points_left"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            projected[key] = item
    return projected


def _allowlisted_dict(value, fields):
    if not isinstance(value, dict):
        return {}
    return {
        key: _safe_value(value[key])
        for key in fields
        if key in value and value[key] is not None
    }


def _allowlisted_list(value, fields, limit=50):
    if not isinstance(value, list):
        return []
    return [_allowlisted_dict(item, fields) for item in value[:limit] if isinstance(item, dict)]


def _project_collection(value, item_fields, collection_keys=("items",)):
    value = value if isinstance(value, dict) else {}
    projected = _allowlisted_dict(value, _LIST_META_FIELDS)
    for key in collection_keys:
        if key in value:
            projected[key] = _allowlisted_list(value.get(key), item_fields)
    return projected


def _project_tool_result(tool_name, value):
    """Return the only fields that may be sent to the external model."""
    value = value if isinstance(value, dict) else {}
    if tool_name == "hq_get_account":
        projected = {
            "user": _allowlisted_dict(value.get("user"), _ACCOUNT_USER_FIELDS),
            "scopes": [
                str(item)[:80] for item in (value.get("scopes") or [])[:20]
                if isinstance(item, str)
            ],
        }
        if isinstance(value.get("expires_at"), (int, float)):
            projected["expires_at"] = value["expires_at"]
        return projected
    if tool_name == "hq_list_channels":
        return _project_collection(value, _CHANNEL_FIELDS, ("channels", "items"))
    if tool_name == "hq_get_pricing":
        return _project_collection(value, _PRICING_FIELDS)
    if tool_name == "hq_list_video_avatars":
        return _project_collection(value, _AVATAR_FIELDS)
    if tool_name == "hq_list_voices":
        return _project_collection(value, _VOICE_FIELDS)
    if tool_name == "hq_list_assets":
        return _project_collection(value, _ASSET_FIELDS)
    if tool_name == "hq_list_tasks":
        projected = _project_collection(value, _TASK_FIELDS)
        projected["kinds"] = _allowlisted_list(
            value.get("kinds"), {"kind", "label", "count"}, limit=50
        )
        return projected
    if tool_name == "hq_get_task":
        projected = _allowlisted_dict(value, _TASK_FIELDS)
        if isinstance(value.get("result"), dict):
            projected["result"] = _allowlisted_dict(value["result"], _TASK_FIELDS)
        return projected
    return {}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_FIELD_LABELS = {
    "prompt": "画面描述", "channel": "生成渠道", "ratio": "画幅",
    "duration": "时长（秒）", "seconds": "时长（秒）", "resolution": "分辨率",
    "model": "模型", "generate_audio": "生成配音",
    "reference_upload_ids": "参考图片", "avatar_id": "数字人形象",
    "avatar_ids": "电影化身", "text": "口播文案", "voice": "音色",
    "motion": "动作幅度", "subtitle": "字幕", "subtitle_style": "字幕样式",
    "subtitle_position": "字幕位置", "enhance_prompt": "增强提示词",
    "reference_image_upload_ids": "参考图片",
    "reference_video_upload_ids": "参考视频",
    "person_image_upload_id": "人物图片", "clothes_upload_id": "服装图片",
    "background_upload_id": "背景图片", "person_video_upload_id": "人物视频",
    "source_page": "来源页面", "line": "换装线路", "cine_mode": "电影模式",
    "quality": "质量", "count": "数量", "provider": "引擎", "kind": "类型",
    "mode": "模式", "lipsync_mode": "口型模式", "dynamic_duration": "动态时长",
    "video_asset_id": "原视频资产", "audio_asset_id": "音频资产",
    "image_upload_id": "图片上传", "mask_upload_id": "蒙版上传",
    "audio_upload_id": "音频上传", "audio_file": "音频资产",
    "style": "风格", "format": "格式", "platforms": "平台", "keyword": "关键词",
    "count_range": "数量范围", "url": "链接",
}


def _payload_summary(payload):
    """Deterministic summary of the SIGNED payload (server defaults included).

    Every field is shown whole (payloads are schema-bounded); defensive caps
    mark ``truncated`` so the confirmation card can still expand/recheck long
    text.  This is the exact input the quote fingerprint binds.
    """
    summary = []
    if not isinstance(payload, dict):
        return summary
    for key, raw in payload.items():
        label = _FIELD_LABELS.get(key, key)
        if isinstance(raw, list):
            value = [str(entry)[:300] for entry in raw[:24]]
            truncated = len(raw) > 24
        elif isinstance(raw, bool):
            value = "是" if raw else "否"
            truncated = False
        elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = raw
            truncated = False
        elif raw is None:
            value = ""
            truncated = False
        else:
            text = str(raw)
            truncated = len(text) > 3000
            value = text[:3000]
        summary.append({
            "key": key, "label": label, "value": value,
            "truncated": bool(truncated),
        })
    return summary


def _input_summary(tool_name, stored_input_json):
    """Build a deterministic, schema-driven summary of the stored input.

    Derived only from the server-persisted input that produced the quote
    fingerprint — never from model free text — so the confirmation card shows
    exactly what will be submitted.
    """
    spec = _SPECS.get(str(tool_name or "").strip())
    arguments = {}
    try:
        parsed = json.loads(stored_input_json or "{}")
        if isinstance(parsed, dict):
            arguments = parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        arguments = {}
    summary = []
    if spec:
        for key in spec["parameters"].get("properties", {}):
            if key not in arguments:
                continue
            raw = arguments[key]
            if isinstance(raw, list):
                display = "、".join(str(item)[:64] for item in raw[:6])
            elif isinstance(raw, bool):
                display = "是" if raw else "否"
            else:
                display = str(raw)[:160]
            summary.append({
                "key": key, "label": _FIELD_LABELS.get(key, key),
                "value": display,
            })
    return summary


def _safe_pending(row, result=None):
    stored_payload = None
    if "payload_json" in row.keys() and row["payload_json"]:
        try:
            parsed = json.loads(str(row["payload_json"]))
            if isinstance(parsed, dict):
                stored_payload = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            stored_payload = None
    if stored_payload is not None:
        input_summary = _payload_summary(stored_payload)
        input_source = "signed_payload"
    else:
        # 旧卡片没有落库 payload，回退到按原始工具参数重建的摘要。
        input_summary = _input_summary(
            str(row["tool_name"] if "tool_name" in row.keys() else ""),
            str(row["input_json"] or ""),
        )
        input_source = "tool_arguments"
    value = {
        "id": row["id"], "status": row["status"],
        "capability": str(row["capability"] or "")[:80],
        "title": row["title"] if "title" in row.keys() else _SPECS.get(row["tool_name"], {}).get("title", "视频生成"),
        "summary": "已取得报价，确认后才会扣点并提交生成",
        "cost": int(row["cost"] or 0),
        "points": int(row["points"]) if row["points"] is not None else None,
        "expires_at": int(row["expires_at"] or 0),
        # 确认卡只展示这份由服务器生成的确定性摘要；signed_payload 与
        # fingerprint 同源（落库时已自校验哈希），用户据此核对提交内容。
        "input": {
            "fingerprint": str(row["input_hash"] or "").strip()[:200],
            "summary": input_summary,
            "source": input_source,
        },
    }
    if result is not None:
        projected = _project_confirmation_result(result)
        if projected:
            value["result"] = projected
    return value


def _input_json(arguments):
    return _canonical(arguments)


def _quote_fingerprint(quote):
    value = str(quote.get("fingerprint") or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}:[0-9a-f]{64}", value):
        raise ToolError(
            "quote_response_invalid", "黄雀 CLI 未返回可校验的标准化报价标识", 502
        )
    return value


def _pending_quote_response(pending, *, reused):
    status = pending.get("status")
    if status == "awaiting_confirmation":
        return {
            "ok": True, "confirmation_required": True,
            "pending_action": pending, "reused": bool(reused),
        }
    if status == "confirming":
        return {
            "ok": False, "confirmation_required": False,
            "confirmation_in_progress": True, "pending_action": pending,
            "reused": bool(reused),
            "detail": "相同视频方案正在提交，请勿重复操作",
        }
    return {
        "ok": False, "confirmation_required": False,
        "result_unknown": True, "pending_action": pending,
        "reused": bool(reused),
        "detail": "相同视频方案存在结果未知的提交，请先在历史记录中核对，不能重新报价",
    }


def _reusable_pending(db_factory, username, capability, input_json, now):
    ensure_tables(db_factory)
    timestamp = int(now())
    try:
        with closing(db_factory()) as conn:
            conn.row_factory = __import__("sqlite3").Row
            conn.execute(
                "UPDATE video_agent_pending_actions "
                "SET status='expired',quote_token='',updated_at=? "
                "WHERE username=? AND capability=? AND input_json=? "
                "AND status='awaiting_confirmation' AND expires_at<=?",
                (timestamp, username, capability, input_json, timestamp),
            )
            row = conn.execute(
                "SELECT * FROM video_agent_pending_actions "
                "WHERE username=? AND capability=? AND input_json=? "
                "AND status IN ('awaiting_confirmation','confirming','result_unknown') "
                "ORDER BY created_at DESC LIMIT 1",
                (username, capability, input_json),
            ).fetchone()
            conn.commit()
        return _safe_pending(row) if row else None
    except Exception as error:
        raise ToolError("pending_store_unavailable", "视频操作确认服务暂时不可用", 503) from error


def _legacy_unknown_pending(db_factory, username, capability):
    """Fail closed for unknown/in-flight cards from raw-JSON-keyed builds."""
    ensure_tables(db_factory)
    try:
        with closing(db_factory()) as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT * FROM video_agent_pending_actions "
                "WHERE username=? AND capability=? "
                "AND status IN ('confirming','result_unknown') "
                "AND instr(input_hash,':')=0 "
                "ORDER BY CASE status WHEN 'result_unknown' THEN 0 ELSE 1 END,"
                "created_at DESC LIMIT 1",
                (username, capability),
            ).fetchone()
        return _safe_pending(row) if row else None
    except Exception as error:
        raise ToolError("pending_store_unavailable", "视频操作确认服务暂时不可用", 503) from error


def _store_quote(db_factory, username, tool_name, spec, arguments, quote, now):
    token = str(quote.get("quote_token") or "").strip()
    if not token:
        raise ToolError("quote_response_invalid", "黄雀 CLI 未返回有效报价", 502)
    digest = _quote_fingerprint(quote)
    # 报价真正绑定的是鉴权服务标准化后的 payload（含默认值），确认卡必须
    # 展示它。这里自校验 payload 与报价指纹同源，否则拒绝落卡（fail-closed）。
    payload = quote.get("payload")
    if not isinstance(payload, dict) or not payload:
        raise ToolError(
            "quote_response_invalid", "黄雀 CLI 报价未返回绑定的标准化参数", 502
        )
    payload_json = _canonical(payload)
    fingerprint_digest = digest.split(":", 1)[-1]
    if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != fingerprint_digest:
        raise ToolError(
            "quote_response_invalid", "黄雀 CLI 报价参数与报价指纹不匹配", 502
        )
    try:
        cost = max(0, int(quote.get("cost") or 0))
        points = quote.get("points")
        points = int(points) if points is not None else None
        expires_in = max(MIN_PENDING_TTL, min(MAX_PENDING_TTL, int(quote.get("expires_in") or 120)))
    except (TypeError, ValueError) as error:
        raise ToolError("quote_response_invalid", "黄雀 CLI 报价格式无效", 502) from error
    pending_id = "vpa_" + secrets.token_hex(16)
    input_json = _input_json(arguments)
    timestamp = int(now())
    expires_at = timestamp + expires_in
    ensure_tables(db_factory)
    try:
        with closing(db_factory()) as conn:
            conn.row_factory = __import__("sqlite3").Row
            conn.execute(
                "UPDATE video_agent_pending_actions "
                "SET status='expired',quote_token='',updated_at=? "
                "WHERE username=? AND capability=? AND input_hash=? "
                "AND status='awaiting_confirmation' AND expires_at<=?",
                (timestamp, username, spec["capability"], digest, timestamp),
            )
            existing = conn.execute(
                "SELECT * FROM video_agent_pending_actions "
                "WHERE username=? AND capability=? AND input_hash=? "
                "AND status IN ('awaiting_confirmation','confirming','result_unknown') "
                "ORDER BY created_at DESC LIMIT 1",
                (username, spec["capability"], digest),
            ).fetchone()
            if existing:
                conn.commit()
                return _safe_pending(existing)
            try:
                conn.execute(
                    "INSERT INTO video_agent_pending_actions"
                    "(id,username,tool_name,capability,input_json,input_hash,quote_token,cost,points,status,created_at,expires_at,updated_at,payload_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pending_id, username, tool_name, spec["capability"], input_json, digest,
                     token, cost, points, "awaiting_confirmation", timestamp, expires_at, timestamp,
                     payload_json),
                )
            except __import__("sqlite3").IntegrityError:
                existing = conn.execute(
                    "SELECT * FROM video_agent_pending_actions "
                    "WHERE username=? AND capability=? AND input_hash=? "
                    "AND status IN ('awaiting_confirmation','confirming','result_unknown') LIMIT 1",
                    (username, spec["capability"], digest),
                ).fetchone()
                if not existing:
                    raise
                conn.commit()
                return _safe_pending(existing)
            conn.commit()
            row = conn.execute("SELECT * FROM video_agent_pending_actions WHERE id=?", (pending_id,)).fetchone()
    except Exception as error:
        raise ToolError("pending_store_unavailable", "视频操作确认服务暂时不可用", 503) from error
    return _safe_pending(row)


class VideoAgentToolRuntime:
    def __init__(self, *, username, web_token, db_factory,
                 cli_execute=None, now=None, read_fallbacks=None):
        self.username = str(username or "").strip()
        self.web_token = str(web_token or "").strip()
        self.db_factory = db_factory
        self.cli_execute = cli_execute or hq_cli_executor.execute
        self.now = now or time.time
        self.read_fallbacks = dict(read_fallbacks or {})
        self.pending_actions = []
        self.activity = []

    def run(self, name, raw_arguments, timeout_seconds=None):
        spec = _SPECS.get(str(name or "").strip())
        if not spec:
            raise ToolError("tool_not_allowed", "模型请求了未开放的工具", 403)
        arguments = _parse_arguments(raw_arguments, spec)
        activity = {
            "tool": name, "title": spec["title"], "label": spec["title"],
            "side_effect": spec["mode"],
            "status": "running",
        }
        self.activity.append(activity)
        try:
            if spec["mode"] == "quote":
                input_json = _input_json(arguments)
                pending = _reusable_pending(
                    self.db_factory, self.username, spec["capability"], input_json, self.now
                )
                if not pending:
                    pending = _legacy_unknown_pending(
                        self.db_factory, self.username, spec["capability"]
                    )
                if pending:
                    if not any(item.get("id") == pending["id"] for item in self.pending_actions):
                        self.pending_actions.append(pending)
                    activity.update({
                        "status": (
                            "succeeded" if pending.get("status") == "awaiting_confirmation"
                            else "blocked"
                        ),
                        "pending_action_id": pending["id"],
                        "reused": True,
                    })
                    return _pending_quote_response(pending, reused=True)
            cli_kwargs = {
                "username": self.username, "web_token": self.web_token,
                "scopes": [spec["scope"]], "confirm": False,
            }
            if timeout_seconds is not None:
                cli_kwargs["timeout"] = max(1, min(35, float(timeout_seconds)))
            result = self.cli_execute(
                spec["capability"], arguments, **cli_kwargs
            )
            if spec["mode"] == "quote":
                pending = _store_quote(
                    self.db_factory, self.username, name, spec, arguments, result, self.now
                )
                if not any(item.get("id") == pending["id"] for item in self.pending_actions):
                    self.pending_actions.append(pending)
                activity.update({
                    "status": (
                        "succeeded" if pending.get("status") == "awaiting_confirmation"
                        else "blocked"
                    ),
                    "pending_action_id": pending["id"],
                })
                return _pending_quote_response(pending, reused=False)
            activity["status"] = "succeeded"
            return {
                "ok": True,
                "result": _bounded_result(_project_tool_result(name, result)),
            }
        except ToolError:
            activity["status"] = "failed"
            raise
        except hq_cli_executor.CLIExecutionError as error:
            fallback = self.read_fallbacks.get(name)
            if (
                spec["mode"] == "read"
                and error.code == "cli_auth_failed"
                and error.status == 404
                and callable(fallback)
            ):
                try:
                    result = fallback(dict(arguments))
                    if not isinstance(result, dict):
                        raise TypeError("local read fallback must return an object")
                    activity.update({"status": "succeeded", "fallback": "local_read"})
                    return {
                        "ok": True,
                        "result": _bounded_result(_project_tool_result(name, result)),
                    }
                except Exception as fallback_error:
                    activity["status"] = "failed"
                    raise ToolError(
                        "local_read_unavailable", "本地只读工具暂时不可用", 503
                    ) from fallback_error
            activity["status"] = "failed"
            raise ToolError(error.code, str(error), error.status,
                            unknown_outcome=error.unknown_outcome) from error
        except Exception as error:
            activity["status"] = "failed"
            raise ToolError("tool_execution_failed", "黄雀工具暂时不可用", 502) from error


def _read_pending_for_confirmation(db_factory, pending_id, username, idempotency_key, now):
    """Read a card without claiming; expires stale awaiting cards in place.

    Any crash after this step leaves the card ``awaiting_confirmation`` and
    re-confirmable — nothing has been claimed and no paid action can start.
    """
    ensure_tables(db_factory)
    timestamp = int(now())
    try:
        with closing(db_factory()) as conn:
            conn.row_factory = __import__("sqlite3").Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM video_agent_pending_actions WHERE id=? AND username=?",
                (pending_id, username),
            ).fetchone()
            if not row:
                raise ToolError("pending_action_not_found", "待确认操作不存在或已失效", 404)
            status = row["status"]
            if status == "submitted" and row["idempotency_key"] == idempotency_key:
                stored = json.loads(row["result_json"] or "{}")
                conn.commit()
                return row, stored, True
            if int(row["expires_at"] or 0) <= timestamp and status == "awaiting_confirmation":
                conn.execute(
                    "UPDATE video_agent_pending_actions "
                    "SET status='expired',quote_token='',updated_at=? WHERE id=?",
                    (timestamp, pending_id),
                )
                conn.commit()
                expired = dict(row)
                expired["status"] = "expired"
                raise ToolError(
                    "pending_action_expired", "报价已过期，请重新获取", 409,
                    pending_action=_safe_pending(expired),
                )
            if status != "awaiting_confirmation":
                raise ToolError(
                    "pending_action_unavailable", "该操作已处理，不能重复提交", 409,
                    pending_action=_safe_pending(row),
                )
            conn.commit()
            return row, None, False
    except ToolError:
        raise
    except Exception as error:
        raise ToolError("pending_store_unavailable", "视频操作确认服务暂时不可用", 503) from error


def _claim_pending_for_confirmation(db_factory, pending_id, username,
                                    idempotency_key, submission_key, now):
    """Atomically move awaiting→confirming AND persist the reconciliation key.

    One single UPDATE: a crash before it leaves the card re-confirmable; after
    it the card already carries ``submission_key``, so startup recovery can only
    produce a reconcilable ``result_unknown`` — the two-step claim/key window
    that could orphan cards without credentials is gone.
    """
    timestamp = int(now())
    try:
        with closing(db_factory()) as conn:
            conn.row_factory = __import__("sqlite3").Row
            conn.execute("BEGIN IMMEDIATE")
            claimed = conn.execute(
                "UPDATE video_agent_pending_actions "
                "SET status='confirming',idempotency_key=?,submission_key=?,updated_at=? "
                "WHERE id=? AND username=? AND status='awaiting_confirmation'",
                (idempotency_key, submission_key, timestamp, pending_id, username),
            )
            if claimed.rowcount != 1:
                # 并发双确认的败者或状态已变化：回滚后按只读路径给出准确错误。
                conn.rollback()
                return _read_pending_for_confirmation(
                    db_factory, pending_id, username, idempotency_key, now
                )
            row = conn.execute(
                "SELECT * FROM video_agent_pending_actions WHERE id=?",
                (pending_id,),
            ).fetchone()
            conn.commit()
            return row, None, False
    except ToolError:
        raise
    except Exception as error:
        raise ToolError("pending_store_unavailable", "视频操作确认服务暂时不可用", 503) from error


def confirm_pending_action(pending_id, idempotency_key, *, username, web_token,
                           db_factory, cli_execute=None, now=None, quote_claims=None):
    pending_id = str(pending_id or "").strip()
    idempotency_key = str(idempotency_key or "").strip()
    username = str(username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", idempotency_key):
        raise ToolError("idempotency_key_invalid", "确认请求幂等键格式无效", 400)
    if not re.fullmatch(r"vpa_[0-9a-f]{32}", pending_id):
        raise ToolError("pending_action_not_found", "待确认操作不存在或已失效", 404)
    now_fn = now or time.time
    row, stored, replayed = _read_pending_for_confirmation(
        db_factory, pending_id, username, idempotency_key, now_fn
    )
    if replayed:
        return _safe_pending(row, stored)
    # 先向鉴权服务核验报价凭证并取得确定性提交幂等键（hqcli-<nonce>，与
    # CLI 提交链路一致）。此时尚未领取卡片：核验失败零副作用，卡片仍是
    # awaiting_confirmation，无需任何回滚。
    claims_fn = quote_claims or hq_cli_executor.quote_claims
    try:
        claims = claims_fn(row["quote_token"])
        submission_key = "hqcli-" + str(claims["nonce"])
    except hq_cli_executor.CLIExecutionError as error:
        raise ToolError(error.code, str(error), error.status)
    row, stored, replayed = _claim_pending_for_confirmation(
        db_factory, pending_id, username, idempotency_key, submission_key, now_fn
    )
    if replayed:
        return _safe_pending(row, stored)
    executor = cli_execute or hq_cli_executor.execute
    arguments = json.loads(row["input_json"])
    try:
        result = executor(
            row["capability"], arguments,
            username=username, web_token=web_token,
            scopes=["generation:quote", "generation:submit"],
            confirm=True, quote_token=row["quote_token"],
        )
        safe_result = _project_confirmation_result(result)
        result_json = _canonical(safe_result)
        status = "submitted"
        error_code = None
    except hq_cli_executor.CLIExecutionError as error:
        status = "result_unknown" if error.unknown_outcome else "failed"
        error_code = error.code
        safe_result = None
        result_json = None
        failure = ToolError(error.code, str(error), error.status,
                            unknown_outcome=error.unknown_outcome)
    except Exception as error:
        status = "result_unknown"
        error_code = "confirmation_failed"
        safe_result = None
        result_json = None
        failure = ToolError(
            "confirmation_failed", "提交结果未知，请勿重复点击并前往历史记录核对", 502,
            unknown_outcome=True,
        )
    timestamp = int(now_fn())
    try:
        with closing(db_factory()) as conn:
            conn.execute(
                "UPDATE video_agent_pending_actions "
                "SET status=?,quote_token='',result_json=?,error_code=?,updated_at=? "
                "WHERE id=? AND username=? AND status='confirming' AND idempotency_key=?",
                (status, result_json, error_code, timestamp, pending_id, username, idempotency_key),
            )
            if conn.total_changes != 1:
                raise ToolError("pending_action_conflict", "待确认操作状态已变化", 409)
            conn.commit()
            conn.row_factory = __import__("sqlite3").Row
            updated = conn.execute("SELECT * FROM video_agent_pending_actions WHERE id=?", (pending_id,)).fetchone()
    except ToolError:
        raise
    except Exception as error:
        raise ToolError(
            "pending_store_unavailable", "生成可能已提交，但确认状态保存失败；请勿重复点击", 503,
            unknown_outcome=True,
        ) from error
    if status != "submitted":
        failure.pending_action = _safe_pending(updated)
        raise failure
    return _safe_pending(updated, safe_result)


def reconcile_pending_action(pending_id, *, username, db_factory, now=None):
    """Read-only reconciliation for cards stuck in ``result_unknown``.

    The confirm path persists the deterministic submission idempotency key
    (``hqcli-<nonce>``) in the SAME atomic update that claims the card, so
    every post-recovery unknown card carries credentials.  The content
    service's ``submission_idempotency`` table plus the jobs ledger then tell
    us, without any new side effect, whether the submission ever landed:

    * no record + outside safety window -> never submitted/charged -> failed
    * no record + inside safety window  -> may still be in flight -> keep unknown
    * record + response                  -> final outcome recorded -> submitted/failed
    * record, no response, inside window -> being processed -> keep unknown
    * record, no response, stale         -> converge via jobs ledger:
      matching job -> submitted, otherwise -> failed

    Cards from pre-fingerprint builds have no submission key and stay a hard
    barrier (fail closed); new confirmations always converge.
    """
    pending_id = str(pending_id or "").strip()
    username = str(username or "").strip()
    if not re.fullmatch(r"vpa_[0-9a-f]{32}", pending_id):
        raise ToolError("pending_action_not_found", "待确认操作不存在或已失效", 404)
    ensure_tables(db_factory)
    timestamp = int((now or time.time)())
    try:
        with closing(db_factory()) as conn:
            conn.row_factory = __import__("sqlite3").Row
            submission_idempotency.ensure_table(conn)
            row = conn.execute(
                "SELECT * FROM video_agent_pending_actions WHERE id=? AND username=?",
                (pending_id, username),
            ).fetchone()
            if not row:
                raise ToolError("pending_action_not_found", "待确认操作不存在或已失效", 404)
            if row["status"] != "result_unknown":
                return _safe_pending(row)
            key = str(row["submission_key"] or "").strip()
            if not key:
                raise ToolError(
                    "pending_reconcile_unavailable",
                    "该记录缺少可对账的提交凭证，请核对任务历史后重新报价", 409,
                    pending_action=_safe_pending(row),
                )
            endpoints = _CAPABILITY_ENDPOINTS.get(
                str(row["capability"] or "").strip()
            )
            if not endpoints:
                raise ToolError(
                    "pending_reconcile_unavailable",
                    "该记录的提交能力无法安全对账，请核对任务历史后重新报价", 409,
                    pending_action=_safe_pending(row),
                )
            endpoint_holes = ",".join("?" * len(endpoints))
            claim = conn.execute(
                "SELECT response_json,created_at,updated_at FROM submission_idempotency "
                "WHERE username=? AND idem_key=? AND endpoint IN (%s) " % endpoint_holes,
                (username, key) + tuple(endpoints),
            ).fetchone()
            if not claim:
                # 没有幂等记录不代表"从未提交"：被杀掉的 CLI 背后的认证代理
                # 请求可能仍在途。只有超过覆盖下游最大超时的安全期才允许
                # 收敛为 failed，否则保持阻断并提示稍后重试。
                if timestamp - int(row["updated_at"] or 0) < RECONCILE_SAFETY_SECONDS:
                    raise ToolError(
                        "pending_reconcile_in_flight",
                        "提交可能仍在途，请稍后重新对账", 409,
                        pending_action=_safe_pending(row),
                    )
                conn.execute(
                    "UPDATE video_agent_pending_actions "
                    "SET status='failed',quote_token='',"
                    "error_code='reconciled_never_submitted',updated_at=? "
                    "WHERE id=? AND username=? AND status='result_unknown'",
                    (timestamp, pending_id, username),
                )
                conn.commit()
                updated = conn.execute(
                    "SELECT * FROM video_agent_pending_actions WHERE id=?", (pending_id,)
                ).fetchone()
                return _safe_pending(updated)
            if not claim["response_json"]:
                # 有幂等记录但没有响应：提交曾被受理。安全期内保持未知；
                # 超过安全期后用任务账本收敛：有匹配的任务即 submitted，
                # 否则判定受理中断、从未创建任务（failed），绝不永久锁死。
                if timestamp - int(claim["updated_at"] or 0) < RECONCILE_SAFETY_SECONDS:
                    raise ToolError(
                        "pending_reconcile_in_flight",
                        "提交正在受理，请稍后重新对账", 409,
                        pending_action=_safe_pending(row),
                    )
                job_id = _find_submitted_job(conn, username, row)
                if job_id:
                    projected = {"job_id": int(job_id)}
                    status, error_code = "submitted", None
                    result_json = _canonical(projected)
                else:
                    status = "failed"
                    error_code = "reconciled_stale_processing"
                    result_json = None
                conn.execute(
                    "UPDATE video_agent_pending_actions "
                    "SET status=?,quote_token='',result_json=?,error_code=?,updated_at=? "
                    "WHERE id=? AND username=? AND status='result_unknown'",
                    (status, result_json, error_code, timestamp, pending_id, username),
                )
                conn.commit()
                updated = conn.execute(
                    "SELECT * FROM video_agent_pending_actions WHERE id=?", (pending_id,)
                ).fetchone()
                return _safe_pending(
                    updated, json.loads(result_json) if result_json else None
                )
            try:
                stored = json.loads(claim["response_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                stored = {}
            projected = _project_confirmation_result(stored)
            job_id = _find_submitted_job(conn, username, row)
            if projected and job_id:
                # 幂等响应只是结果载体；任务身份始终由 username、精确
                # submission_key 与该 capability 允许的 kind 三者共同证明。
                projected["job_id"] = int(job_id)
                status, error_code = "submitted", None
                result_json = _canonical(projected)
            else:
                status, error_code = "failed", "reconciled_submission_unverified"
                result_json = None
            conn.execute(
                "UPDATE video_agent_pending_actions "
                "SET status=?,quote_token='',result_json=?,error_code=?,updated_at=? "
                "WHERE id=? AND username=? AND status='result_unknown'",
                (status, result_json, error_code, timestamp, pending_id, username),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM video_agent_pending_actions WHERE id=?", (pending_id,)
            ).fetchone()
            return _safe_pending(
                updated, json.loads(result_json) if result_json else None
            )
    except ToolError:
        raise
    except Exception as error:
        raise ToolError("pending_store_unavailable", "视频操作确认服务暂时不可用", 503) from error


def _find_submitted_job(conn, username, row):
    """Look for the job a stale in-flight submission may have created.

    The task ledger must carry the exact idempotency key used by the CLI
    submission.  Account/time/cost/kind similarity is never identity: another
    concurrent paid request can have every one of those values in common.
    """
    kinds = _CAPABILITY_JOB_KINDS.get(str(row["capability"] or "").strip())
    submission_key = str(row["submission_key"] or "").strip()
    if not kinds or not submission_key:
        return None
    holes = ",".join("?" * len(kinds))
    job = conn.execute(
        "SELECT id FROM jobs WHERE username=? AND submission_key=? "
        "AND kind IN (%s) ORDER BY id DESC LIMIT 1" % holes,
        (username, submission_key) + tuple(kinds),
    ).fetchone()
    return int(job["id"]) if job else None
