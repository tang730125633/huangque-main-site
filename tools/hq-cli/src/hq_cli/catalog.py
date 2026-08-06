"""Machine-readable Huangque main-site capability contract."""

from urllib.parse import urlencode


ENVIRONMENTS = {"main": "https://huangquechuanmei.com"}


def _schema(properties=None, required=None):
    return {
        "type": "object", "additionalProperties": False,
        "required": required or [], "properties": properties or {},
    }


RUN_OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema", "cli_version", "capability", "result", "next_actions"],
    "properties": {
        "schema": {"type": "string", "const": "hq.run/v1"},
        "cli_version": {"type": "string"},
        "capability": {"type": "string"},
        "result": {"type": "object"},
        "next_actions": {"type": "array", "items": {"type": "string"}},
    },
}


def _navigation(identifier, name, path, description, fields=None, target_auth="account_for_actions"):
    return {
        "id": identifier, "name": name, "kind": "navigation", "availability": "available",
        "runnable": True, "description": description, "input_schema": _schema(fields),
        "output_schema": RUN_OUTPUT_SCHEMA, "requires_auth": False, "required_scope": None,
        "target_auth": target_auth, "side_effect": "navigation", "confirmation_required": False,
        "cost": {"kind": "none", "detail": "导航不会调用 AI 或扣点。"},
        "deep_link": {"path": path, "query_fields": sorted((fields or {}).keys())},
        "api_action": None,
        "next_actions": ["在已登录浏览器中打开返回的黄雀主站链接。"],
    }


def _api(identifier, name, action, description, fields=None, required=None, scope="profile:read",
         side_effect="read", confirmation=False, cost=None):
    return {
        "id": identifier, "name": name, "kind": "api", "availability": "available",
        "runnable": True, "description": description, "input_schema": _schema(fields, required),
        "output_schema": RUN_OUTPUT_SCHEMA, "requires_auth": True, "required_scope": scope,
        "target_auth": "hq_device_authorization", "side_effect": side_effect,
        "confirmation_required": confirmation, "cost": cost or {"kind": "none"},
        "deep_link": None, "api_action": action,
        "next_actions": ["结果只包含当前已授权黄雀账号的数据。"],
    }


def _upload(identifier, name, description, scope):
    capability = _api(
        identifier, name, None, description, scope=scope,
        side_effect="upload", confirmation=True,
    )
    capability["kind"] = "upload"
    capability["file_input"] = {
        "argument": "--file", "path": "absolute", "maxBytes": 10 * 1024 * 1024,
        "mimeTypes": ["image/jpeg", "image/png", "image/webp"],
        "accountActiveMaxFiles": 8, "accountActiveMaxBytes": 60 * 1024 * 1024,
    }
    return capability


STRING_ID = {"type": "string", "minLength": 1, "maxLength": 160}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 120}
CANVAS_AGENT_NODE_ID = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"}
CANVAS_OP_NODE_ID = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"}
CANVAS_AGENT_FIELDS = {
    "prompt": {"type": "string", "minLength": 1, "maxLength": 2000,
               "description": "可用 @图片1、@图片2 按 reference_upload_ids 顺序引用"},
    "project_id": {"type": "string", "pattern": "^(local|collab):[A-Za-z0-9_-]{1,120}$"},
    "snapshot_digest": {"type": "string", "pattern": "^[a-f0-9]{8,32}$"},
    "scope": {"type": "string", "enum": ["local", "collab"]},
    "nodes": {"type": "array", "maxItems": 60, "items": _schema({
        "id": CANVAS_AGENT_NODE_ID,
        "type": {"type": "string", "enum": ["text", "image", "reverse", "gen", "video", "shortDrama"]},
        "title": {"type": "string", "maxLength": 120},
        "content": {"type": "string", "maxLength": 5000},
        "selected": {"type": "boolean"},
    }, ["id", "type", "title", "content", "selected"])},
    "edges": {"type": "array", "maxItems": 120, "items": _schema({
        "from_node_id": CANVAS_AGENT_NODE_ID, "to_node_id": CANVAS_AGENT_NODE_ID,
    }, ["from_node_id", "to_node_id"])},
    "selected_node_ids": {"type": "array", "maxItems": 30, "items": CANVAS_AGENT_NODE_ID},
    "history": {"type": "array", "maxItems": 10, "items": _schema({
        "role": {"type": "string", "enum": ["user", "assistant"]},
        "content": {"type": "string", "maxLength": 2000},
    }, ["role", "content"])},
}
CANVAS_PARAMS = _schema({
    "title": {"type": "string", "maxLength": 120},
    "text": {"type": "string", "maxLength": 5000},
})
CANVAS_PARAMS["minProperties"] = 1
CANVAS_CREATE_PARAMS = _schema({
    "title": {"type": "string", "maxLength": 120},
    "text": {"type": "string", "minLength": 1, "maxLength": 5000},
}, ["text"])
CANVAS_ENDPOINT = _schema({
    "node": CANVAS_OP_NODE_ID, "port": {"type": "string", "enum": ["prompt", "image"]},
}, ["node", "port"])
CANVAS_OPS = {
    "type": "array", "minItems": 1, "maxItems": 12, "items": {"oneOf": [
        _schema({
            "type": {"type": "string", "const": "node.create"},
            "node": _schema({
                "id": CANVAS_OP_NODE_ID, "type": {"type": "string", "enum": ["text", "gen", "video"]},
                "x": {"type": "number", "minimum": 0, "maximum": 100000},
                "y": {"type": "number", "minimum": 0, "maximum": 100000}, "params": CANVAS_CREATE_PARAMS,
            }, ["id", "type", "x", "y", "params"]),
        }, ["type", "node"]),
        _schema({
            "type": {"type": "string", "const": "node.patch"}, "id": CANVAS_OP_NODE_ID,
            "fields": {**_schema({
                "x": {"type": "number", "minimum": 0, "maximum": 100000},
                "y": {"type": "number", "minimum": 0, "maximum": 100000}, "params": CANVAS_PARAMS,
            }), "minProperties": 1},
        }, ["type", "id", "fields"]),
        _schema({
            "type": {"type": "string", "const": "edge.create"},
            "edge": _schema({"from": CANVAS_ENDPOINT, "to": CANVAS_ENDPOINT}, ["from", "to"]),
        }, ["type", "edge"]),
    ]},
}

CAPABILITIES = {}
for item in (
    ("login", "网页登录", "/login", "进入黄雀主站登录页。", None, "none"),
    ("inspiration", "灵感", "/workbench/inspiration", "浏览灵感设计工作台。", None, "none"),
    ("leads", "获客", "/workbench/leads", "进入获客工作台。", None, "account_for_data_or_actions"),
    ("collect", "采集", "/workbench/collect", "进入内容采集工作台。", None, "account_for_data_or_actions"),
    ("image", "图片工作台", "/workbench/banana", "进入图片工作台；只预填，不提交生成。",
     {"prompt": {"type": "string", "minLength": 1, "maxLength": 2000}}, "account_for_actions"),
    ("video", "视频工作台", "/workbench/video", "进入视频工作台；只预填，不提交生成。",
     {"prompt": {"type": "string", "minLength": 1, "maxLength": 2000}}, "account_for_actions"),
    ("one-click-video", "一键成片", "/workbench/one-click-video", "用一个已完成的视频资产进入一键成片工作台。",
     {"source_asset_id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}}, "account_for_actions"),
    ("audio", "音频工作台", "/workbench/audio", "进入音频工作台；只预填，不提交生成。",
     {"prompt": {"type": "string", "minLength": 1, "maxLength": 1000}}, "account_for_actions"),
    ("script", "文案", "/workbench/script", "进入文案编导工作台。", None, "account_for_actions"),
    ("canvas", "画布", "/workbench/canvas", "进入创作画布。", None, "account_for_actions"),
    ("assets-page", "资产页", "/workbench/assets", "进入我的资产。", None, "account_for_data"),
    ("tutorials", "教程", "/workbench/tutorials", "进入教程中心。", None, "none"),
    ("settings", "设置", "/workbench/settings", "进入账号设置。", None, "account_for_data_or_actions"),
):
    CAPABILITIES[item[0]] = _navigation(*item)

CAPABILITIES["account"] = _api(
    "account", "账号资料", "account", "读取当前授权账号、会员、点数和授权范围。")
CAPABILITIES["channels"] = _api(
    "channels", "渠道目录", "channels", "按当前授权账号读取黄雀全部真实 API 渠道、前端功能映射和 CLI 调用入口。")
CAPABILITIES["channels"]["next_actions"] = [
    "根据 access、capabilities 和 selector 选择可直接调用的能力；registered 表示已登记但尚无独立执行入口。",
]
CAPABILITIES["ip12-projects"] = _api(
    "ip12-projects", "IP12 项目列表", "ip12-projects", "读取当前账号在主站 Hermes IP12 中的全部诊断项目。", scope="ip12:read")
CAPABILITIES["ip12-project"] = _api(
    "ip12-project", "IP12 项目资料", "ip12-project", "读取一个本人 Hermes IP12 项目的基础资料、对话、模块进度与已存报告。",
    {"project_id": STRING_ID}, ["project_id"], "ip12:read")
CAPABILITIES["ip12-create"] = _api(
    "ip12-create", "创建 IP12 项目", "ip12-create", "在当前账号创建一个新的 IP12 项目。",
    {"title": {"type": "string", "minLength": 1, "maxLength": 120}}, ["title"], "ip12:write", "write", True)
CAPABILITIES["ip12-report"] = _api(
    "ip12-report", "读取 IP12 报告", "ip12-report", "读取一个本人 Hermes IP12 项目已经保存的模块报告；不会重新生成报告。",
    {"project_id": STRING_ID}, ["project_id"], "ip12:read")
CAPABILITIES["ip12-message"] = _api(
    "ip12-message", "继续 IP12 对话", "ip12-message",
    "向本人 IP12 项目提交一轮回答并调用 AI 教练；request_id 必须每轮唯一，重试同一轮时保持不变。",
    {"project_id": STRING_ID, "message": {"type": "string", "minLength": 1, "maxLength": 4000},
     "request_id": STRING_ID},
    ["project_id", "message", "request_id"], "ip12:chat", "external_ai", True,
    {"kind": "external_ai", "points": 0, "detail": "不扣点，但会写入 IP12 项目并调用黄雀 AI。"})
CAPABILITIES["ip12-message"]["next_actions"] = [
    "网络超时后只可用完全相同的输入和 request_id 重试；若返回结果未知，先读取 IP12 项目。",
]
CAPABILITIES["prompt-optimize"] = _api(
    "prompt-optimize", "优化提示词", "prompt-optimize", "真实调用黄雀主站提示词优化服务。",
    {"prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
     "kind": {"type": "string", "enum": ["image", "video"]}},
    ["prompt", "kind"], "prompt:optimize", "external_ai", True,
    {"kind": "external_ai", "points": 0, "detail": "不扣用户点数，但会调用黄雀 AI。"})
CAPABILITIES["canvas-list"] = _api(
    "canvas-list", "画布列表", "canvas-list", "读取当前账号可访问的画布。",
    {"limit": {"type": "integer", "minimum": 1, "maximum": 100},
     "offset": {"type": "integer", "minimum": 0, "maximum": 100000}}, scope="canvas:read")
CAPABILITIES["canvas-get"] = _api(
    "canvas-get", "读取画布", "canvas-get", "读取一个本人可访问的画布。",
    {"board_id": STRING_ID}, ["board_id"], "canvas:read")
CAPABILITIES["canvas-create"] = _api(
    "canvas-create", "自动创建画布", "canvas-create", "创建协作画布；可用 prompt 自动放入第一个文本节点。",
    {"name": {"type": "string", "minLength": 1, "maxLength": 48},
     "prompt": {"type": "string", "minLength": 0, "maxLength": 2000}},
    ["name"], "canvas:write", "write", True)
CAPABILITIES["canvas-agent-plan"] = _api(
    "canvas-agent-plan", "画布 Agent 规划", "canvas-agent-plan",
    "把严格裁剪的画布文本快照发送给 AI；先报价，确认后扣点并返回可审核的操作方案，不自动修改画布。",
    CANVAS_AGENT_FIELDS,
    ["prompt", "project_id", "snapshot_digest", "scope", "nodes", "edges", "selected_node_ids"],
    "canvas:agent", "paid", True,
    {"kind": "server_quote", "unit": "points", "confirmation": "quote_token + --confirm"},
)
CAPABILITIES["canvas-agent-plan"]["next_actions"] = [
    "用 task 轮询 job_id；审核 result.plan.actions 后，再转换为 canvas-ops 并单独确认写入。",
]
CAPABILITIES["canvas-ops"] = _api(
    "canvas-ops", "写入画布操作", "canvas-ops",
    "向本人有编辑权限的协作画布提交最多 12 个非破坏性操作；不支持删除、整体覆盖或执行生成。",
    {
        "board_id": STRING_ID,
        "base_version": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807},
        "op_id": {"type": "string", "pattern": "^hqcli-[A-Za-z0-9_-]{11,122}$"},
        "ops": CANVAS_OPS,
    }, ["board_id", "base_version", "op_id", "ops"], "canvas:edit", "write", True,
)
CAPABILITIES["canvas-ops"]["constraints"] = [
    "Only node.create, node.patch, and edge.create are accepted",
    "Allowed created node types: text, gen, video",
    "Delete, generated outputs, board snapshots, members, and scripts are rejected",
]
CAPABILITIES["tasks"] = _api(
    "tasks", "任务列表", "tasks", "按账号读取生成任务、状态、扣点和退款结果。",
    {"days": {"type": "integer", "minimum": 1, "maximum": 365},
     "kind": {"type": "string", "maxLength": 32},
     "page": {"type": "integer", "minimum": 1, "maximum": 100000},
     "page_size": {"type": "integer", "minimum": 5, "maximum": 50}}, scope="tasks:read")
CAPABILITIES["task"] = _api(
    "task", "任务详情", "task", "按任务号读取当前账号的任务状态和结果。",
    {"job_id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}}, ["job_id"], "tasks:read")
CAPABILITIES["assets"] = _api(
    "assets", "资产列表", "assets", "读取当前账号的图片、音频、视频、文案、采集、获客或拆解资产。",
    {"kind": {"type": "string", "enum": ["image", "audio", "video", "copy", "collect", "leads", "breakdown"]},
     "limit": LIMIT, "offset": {"type": "integer", "minimum": 0, "maximum": 100000}},
    ["kind"], "assets:read")
CAPABILITIES["voices"] = _api(
    "voices", "可用音色", "voices", "读取当前账号可用于音频生成的公共与个人音色。", scope="assets:read")
CAPABILITIES["image-upload"] = _upload(
    "image-upload", "上传生成参考图",
    "把一张本地 PNG、JPG 或 WebP 流式上传为本人短期私有 upload_id；不扣点，不返回公开素材地址。",
    "assets:upload",
)
CAPABILITIES["image-upload"]["next_actions"] = [
    "把返回的 upload_id 写入 image-generate 的 image_upload_id、mask_upload_id 或 reference_upload_ids。",
]
ASSET_MARK_FIELDS = {
    "kind": {"type": "string", "enum": ["image", "audio", "video", "avatar", "copy", "collect", "leads", "breakdown"]},
    "key": {"type": "string", "minLength": 1, "maxLength": 500},
}
CAPABILITIES["asset-favorite"] = _api(
    "asset-favorite", "收藏资产", "asset-favorite", "收藏或取消收藏当前账号的一项资产。",
    {**ASSET_MARK_FIELDS, "favorite": {"type": "boolean"}}, ["kind", "key", "favorite"],
    "assets:write", "write", True)
CAPABILITIES["asset-tags"] = _api(
    "asset-tags", "管理资产标签", "asset-tags", "替换当前账号一项资产的标签；最多 8 个。",
    {**ASSET_MARK_FIELDS, "tags": {"type": "array", "maxItems": 8,
                                    "items": {"type": "string", "minLength": 1, "maxLength": 24}}},
    ["kind", "key", "tags"], "assets:write", "write", True)

COMPOSE_PROJECT_ID = {"type": "string", "pattern": "^compose_[0-9a-f]{32}$"}
DP_PROJECT_ID = {"type": "string", "pattern": "^dp_[0-9a-f]{32}$"}
REVISION = {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}
CAPABILITIES["video-compose-projects"] = _api(
    "video-compose-projects", "一键成片项目列表", "video-compose-projects", "读取本人一键成片项目。",
    scope="video-compose:read")
CAPABILITIES["video-compose-project"] = _api(
    "video-compose-project", "读取一键成片项目", "video-compose-project", "读取本人一个一键成片项目及当前版本。",
    {"project_id": COMPOSE_PROJECT_ID}, ["project_id"], "video-compose:read")
CAPABILITIES["video-compose-create"] = _api(
    "video-compose-create", "创建一键成片项目", "video-compose-create", "从本人已完成的视频资产创建一键成片项目。",
    {"source_asset_id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}},
    ["source_asset_id"], "video-compose:write", "write", True)
CAPABILITIES["video-compose-analyze"] = _api(
    "video-compose-analyze", "分析一键成片素材", "video-compose-analyze", "转写源视频并识别可审核的删减候选。",
    {"project_id": COMPOSE_PROJECT_ID, "expected_revision": REVISION},
    ["project_id", "expected_revision"], "video-compose:write", "external_ai", True)
CAPABILITIES["video-compose-review"] = _api(
    "video-compose-review", "确认一键成片剪辑", "video-compose-review", "精确确认全部候选片段保留或删除，生成非破坏性 EDL。",
    {"project_id": COMPOSE_PROJECT_ID, "expected_revision": REVISION,
     "decisions": {"type": "object", "minProperties": 1, "maxProperties": 200,
                   "additionalProperties": {"type": "string", "enum": ["keep", "remove"]}}},
    ["project_id", "expected_revision", "decisions"], "video-compose:write", "write", True)
CAPABILITIES["video-compose-render"] = _api(
    "video-compose-render", "渲染一键成片", "video-compose-render", "按已确认 EDL 使用主站默认模板渲染 MP4，并写入本人视频资产。",
    {"project_id": COMPOSE_PROJECT_ID, "expected_revision": REVISION},
    ["project_id", "expected_revision"], "video-compose:write", "write", True)

DP_FIELDS = {
    "title": {"type": "string", "minLength": 1, "maxLength": 80},
    "script_text": {"type": "string", "maxLength": 20000},
    "ratio": {"type": "string", "enum": ["9:16", "16:9"]},
    "resolution": {"type": "string", "enum": ["1080p"]},
    "voice_key": {"type": "string", "maxLength": 200},
    "target_duration": {"type": "integer", "minimum": 30, "maximum": 180},
}
CAPABILITIES["digital-presenter-capability"] = _api(
    "digital-presenter-capability", "数字人口播可用状态", "digital-presenter-capability", "读取主站数字人口播功能开关。",
    scope="digital-presenter:read")
CAPABILITIES["digital-presenter-project"] = _api(
    "digital-presenter-project", "读取数字人口播项目", "digital-presenter-project", "读取本人有访问权限的画布中的数字人口播项目。",
    {"board_id": STRING_ID, "project_id": DP_PROJECT_ID}, ["board_id", "project_id"], "digital-presenter:read")
CAPABILITIES["digital-presenter-create"] = _api(
    "digital-presenter-create", "创建数字人口播项目", "digital-presenter-create", "在本人有编辑权限的协作画布中创建数字人口播项目。",
    {"board_id": STRING_ID, "request_id": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{8,128}$"}, **DP_FIELDS},
    ["board_id", "request_id"], "digital-presenter:write", "write", True)
CAPABILITIES["digital-presenter-update"] = _api(
    "digital-presenter-update", "更新数字人口播项目", "digital-presenter-update", "按 revision 更新本人有编辑权限的数字人口播项目。",
    {"board_id": STRING_ID, "project_id": DP_PROJECT_ID, "revision": REVISION, **DP_FIELDS},
    ["board_id", "project_id", "revision"], "digital-presenter:write", "write", True)

IMAGE_FIELDS = {
    "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
    "provider": {"type": "string", "enum": ["openai", "xiaole", "seedream"]},
    "ratio": {"type": "string", "enum": ["1:1", "9:16", "16:9", "3:4"]},
    "quality": {"type": "string", "enum": ["std", "hd"]},
    "count": {"type": "integer", "minimum": 1, "maximum": 4},
    "variant": {"type": "string", "enum": ["std", "pro"]},
    "image_upload_id": {"type": "string", "minLength": 36, "maxLength": 36},
    "mask_upload_id": {"type": "string", "minLength": 36, "maxLength": 36},
    "reference_upload_ids": {"type": "array", "maxItems": 16,
                             "items": {"type": "string", "minLength": 36, "maxLength": 36}},
}
VIDEO_FIELDS = {
    "prompt": {"type": "string", "minLength": 1, "maxLength": 2000,
               "description": "可用 @图片1、@图片2 按 reference_upload_ids 顺序引用"},
    "channel": {"type": "string", "enum": ["grok", "micro", "omni", "minimax"]},
    "ratio": {"type": "string", "enum": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"]},
    "duration": {"type": "integer", "minimum": 1, "maximum": 15},
    "resolution": {"type": "string", "enum": ["480p", "720p", "768p", "1080p"]},
    "model": {"type": "string", "enum": ["grok-imagine-video", "grok-imagine-video-1.5"]},
    "generate_audio": {"type": "boolean"},
    "reference_upload_ids": {"type": "array", "maxItems": 9,
                             "items": {"type": "string", "minLength": 36, "maxLength": 36}},
}
AUDIO_FIELDS = {
    "text": {"type": "string", "minLength": 1, "maxLength": 1000},
    "voice": {"type": "string", "minLength": 1, "maxLength": 128},
    "speed": {"type": "number", "minimum": 0.5, "maximum": 2},
    "pitch": {"type": "integer", "minimum": -12, "maximum": 12},
    "volume": {"type": "integer", "minimum": -50, "maximum": 100},
}

for identifier, name, fields, required in (
    ("image-generate", "图片生成", IMAGE_FIELDS, ["prompt"]),
    ("video-generate", "视频生成", VIDEO_FIELDS, ["prompt"]),
    ("audio-generate", "音频生成", AUDIO_FIELDS, ["text"]),
):
    CAPABILITIES[identifier] = _api(
        identifier, name, identifier,
        "先返回服务器报价；只有用相同参数、quote_token 和 --confirm 重试才会扣点并提交任务。",
        fields, required, "generation:quote", "paid", True,
        {"kind": "server_quote", "unit": "points", "confirmation": "quote_token + --confirm"},
    )

CAPABILITIES["image-generate"]["constraints"] = [
    "image_upload_id and reference_upload_ids are mutually exclusive",
    "reference_upload_ids limits: openai=16, seedream=10, xiaole=4",
    "mask_upload_id requires image_upload_id, provider=openai, PNG mask, and count=1",
]
CAPABILITIES["video-generate"]["constraints"] = [
    "reference_upload_ids limits: grok=7, micro=9, omni=6, minimax=5",
    "@图片N references the Nth item in reference_upload_ids",
]


def capability_list():
    return list(CAPABILITIES.values())


def resolve_url(capability, environment, payload):
    url = ENVIRONMENTS[environment] + capability["deep_link"]["path"]
    if payload:
        url += "?" + urlencode(payload)
    return url
