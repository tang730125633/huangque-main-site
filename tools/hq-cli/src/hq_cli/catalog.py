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
        "api_action": None, "website_modes": [],
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
        "deep_link": None, "api_action": action, "website_modes": [],
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
        "accountActiveMaxFiles": 20, "accountActiveMaxBytes": 96 * 1024 * 1024,
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
    ("text-video", "文案成片", "/workbench/text-video", "进入文案成片页；页面入口不会直接提交生成。",
     None, "account_for_actions"),
    ("short-drama", "短剧创作", "/workbench/short-drama", "进入短剧创作页；页面入口不会直接创建项目或生成素材。",
     None, "account_for_actions"),
    ("one-click-video", "一键成片", "/workbench/one-click-video", "用一个已完成的视频资产进入一键成片工作台。",
     {"source_asset_id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}}, "account_for_actions"),
    ("audio", "音频工作台", "/workbench/audio", "进入音频工作台；只预填，不提交生成。",
     {"prompt": {"type": "string", "minLength": 1, "maxLength": 1000}}, "account_for_actions"),
    ("script", "文案", "/workbench/script", "进入文案编导工作台。", None, "account_for_actions"),
    ("canvas", "画布", "/workbench/canvas", "进入创作画布。", None, "account_for_actions"),
    ("assets-page", "资产页", "/workbench/assets", "进入我的资产。", None, "account_for_data"),
    ("pricing-page", "点数价格", "/workbench/pricing", "进入点数价格页；只查看，不会提交任务。", None, "none"),
    ("invite", "邀请中心", "/workbench/invite", "进入当前账号的邀请中心。", None, "account_for_data_or_actions"),
    ("recharge", "会员与点数", "/workbench/recharge", "进入会员与点数页面；不会创建订单或付款。", None, "account_for_data_or_actions"),
    ("bots", "Bot 矩阵", "/workbench/bots", "进入 Bot 矩阵页；不会创建或配置 Bot。", None, "account_for_data_or_actions"),
    ("tutorials", "教程", "/workbench/tutorials", "进入教程中心。", None, "none"),
    ("settings", "设置", "/workbench/settings", "进入账号设置。", None, "account_for_data_or_actions"),
):
    CAPABILITIES[item[0]] = _navigation(*item)

CAPABILITIES["account"] = _api(
    "account", "账号资料", "account", "读取当前授权账号、会员、点数和授权范围。")
CAPABILITIES["channels"] = _api(
    "channels", "渠道目录", "channels", "按当前授权账号读取黄雀全部真实 API 渠道、前端功能映射和 CLI 调用入口。")
CAPABILITIES["channels"]["next_actions"] = [
    "根据 access、capabilities、selector/selectors 选择可直接调用的能力；registered 表示已登记但尚无独立执行入口。",
]
CAPABILITIES["digital-ip-projects"] = _api(
    "digital-ip-projects", "数字化 IP 项目列表", "digital-ip-projects", "读取当前账号的数字化 IP 项目。",
    scope="ip12:read")
CAPABILITIES["digital-ip-project"] = _api(
    "digital-ip-project", "数字化 IP 项目", "digital-ip-project", "读取当前账号的一个数字化 IP 项目。",
    {"project_id": STRING_ID}, ["project_id"], "ip12:read")
CAPABILITIES["digital-ip-report"] = _api(
    "digital-ip-report", "数字化 IP 报告", "digital-ip-report", "读取一个数字化 IP 项目已经保存的报告；不会重新生成。",
    {"project_id": STRING_ID}, ["project_id"], "ip12:read")
for identifier, name, description in (
    ("text-video-capability", "文案成片可用状态", "读取文案成片功能开关和可用状态。"),
    ("text-video-templates", "文案成片模板", "读取文案成片可用模板。"),
    ("text-video-styles", "文案成片样式", "读取文案成片可用样式。"),
    ("text-video-voices", "文案成片音色", "读取文案成片可用音色。"),
):
    CAPABILITIES[identifier] = _api(identifier, name, identifier, description, scope="assets:read")
CAPABILITIES["pricing"] = _api(
    "pricing", "点数价格", "pricing", "读取主站当前点数价格目录。", scope="profile:read")
CAPABILITIES["inspiration-catalog"] = _api(
    "inspiration-catalog", "灵感案例", "inspiration-catalog", "读取主站当前公开的灵感案例。",
    scope="inspiration:read")
CAPABILITIES["inspiration-likes"] = _api(
    "inspiration-likes", "灵感收藏状态", "inspiration-likes", "读取灵感案例的收藏数和当前账号已收藏案例。",
    scope="inspiration:read")
CAPABILITIES["inspiration-like"] = _api(
    "inspiration-like", "收藏灵感案例", "inspiration-like", "收藏或取消收藏一个公开灵感案例。",
    {"id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807},
     "favorite": {"type": "boolean"}}, ["id", "favorite"], "inspiration:write", "write", True)
CAPABILITIES["leads-crm"] = _api(
    "leads-crm", "获客跟进列表", "leads-crm", "读取当前账号保存的客户跟进记录。",
    {"lead_ids": {"type": "array", "maxItems": 100,
                  "items": {"type": "string", "pattern": "^[0-9a-f]{16,40}$",
                            "minLength": 16, "maxLength": 40}}}, scope="leads:read")
CAPABILITIES["leads-crm-upsert"] = _api(
    "leads-crm-upsert", "保存客户跟进", "leads-crm-upsert", "新增或更新当前账号的一条客户跟进记录。",
    {"lead_id": {"type": "string", "pattern": "^[0-9a-f]{16,40}$", "minLength": 16, "maxLength": 40},
     "intent": {"type": "string", "enum": ["高意向", "咨询", "价格敏感", "围观"]},
     "follow_status": {"type": "string", "enum": ["待跟进", "跟进中", "已加微", "已成交", "无效"]},
     "follow_note": {"type": "string", "maxLength": 300}},
    ["lead_id"], "leads:write", "write", True)
CAPABILITIES["video-avatars"] = _api(
    "video-avatars", "数字人形象", "video-avatars", "读取当前账号可用的数字人形象。",
    {"limit": LIMIT}, scope="assets:read")
CAPABILITIES["audio-slots"] = _api(
    "audio-slots", "声音克隆槽位", "audio-slots", "读取当前账号的声音克隆槽位、状态和当前价格。",
    scope="assets:read")
CAPABILITIES["short-drama-projects"] = _api(
    "short-drama-projects", "短剧项目列表", "short-drama-projects", "读取当前账号可访问的短剧项目。",
    {"page": {"type": "integer", "minimum": 1, "maximum": 100000},
     "page_size": {"type": "integer", "minimum": 1, "maximum": 50}}, scope="short-drama:read")
for identifier, name, description in (
    ("short-drama-project", "短剧项目", "读取当前账号可访问的一个短剧项目。"),
    ("short-drama-conversation", "短剧创作对话", "读取一个短剧项目的创作对话与已保存脚本。"),
    ("short-drama-preflight", "短剧开拍检查", "读取一个短剧项目已经保存的开拍检查结果；不会重新生成。"),
):
    CAPABILITIES[identifier] = _api(
        identifier, name, identifier, description,
        {"project_id": {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                        "minLength": 36, "maxLength": 36}},
        ["project_id"], "short-drama:read")
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
CAPABILITIES["video-upload"] = _upload(
    "video-upload", "上传生成参考视频",
    "把一个本地 MP4、MOV 或 WebM 流式上传为本人短期私有 upload_id；不扣点，不返回公开素材地址。",
    "assets:upload",
)
CAPABILITIES["video-upload"]["file_input"] = {
    "argument": "--file", "path": "absolute", "maxBytes": 32 * 1024 * 1024,
    "mimeTypes": ["video/mp4", "video/quicktime", "video/webm"],
    "accountActiveMaxFiles": 6, "accountActiveMaxBytes": 96 * 1024 * 1024,
}
CAPABILITIES["video-upload"]["next_actions"] = [
    "把返回的 upload_id 写入电影化身或经典换装动作的 reference_video_upload_ids / person_video_upload_id。",
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
    "provider": {"type": "string", "enum": ["openai", "xiaole", "seedream", "banana"]},
    "ratio": {"type": "string", "enum": ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]},
    "quality": {"type": "string", "enum": ["std", "hd"]},
    "count": {"type": "integer", "minimum": 1, "maximum": 4},
    "variant": {"type": "string", "enum": ["std", "pro"]},
    "model": {"type": "string", "enum": ["nb2", "pro"]},
    "image_upload_id": {"type": "string", "minLength": 36, "maxLength": 36},
    "mask_upload_id": {"type": "string", "minLength": 36, "maxLength": 36},
    "reference_upload_ids": {"type": "array", "maxItems": 16,
                             "items": {"type": "string", "minLength": 36, "maxLength": 36}},
}
VIDEO_CHANNEL_RULES = {
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
    for channel, rule in VIDEO_CHANNEL_RULES.items():
        clauses.append({
            "if": {"properties": {"channel": {"const": channel}}, "required": ["channel"]},
            "then": _video_channel_then(rule),
        })
    clauses.append({
        "if": {"not": {"required": ["channel"]}},
        "then": _video_channel_then(VIDEO_CHANNEL_RULES["grok"]),
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
    for model, resolutions in VIDEO_CHANNEL_RULES["sora"]["model_resolutions"].items():
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


VIDEO_FIELDS = {
    "prompt": {"type": "string", "minLength": 1, "maxLength": 2000,
               "description": "可用 @图片1、@图片2 按 reference_upload_ids 顺序引用"},
    "channel": {"type": "string", "enum": ["grok", "micro", "omni", "minimax", "sora"]},
    "ratio": {"type": "string", "enum": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "adaptive"]},
    "duration": {"type": "integer", "minimum": 1, "maximum": 15},
    "seconds": {"type": "integer", "enum": [4, 8, 12]},
    "resolution": {"type": "string", "enum": ["480p", "720p", "1024p", "1080p", "2k"]},
    "model": {"type": "string", "enum": ["grok-imagine-video", "grok-imagine-video-1.5", "sora-2", "sora-2-pro"]},
    "generate_audio": {"type": "boolean"},
    "reference_upload_ids": {"type": "array", "minItems": 1, "maxItems": 9,
                             "items": {"type": "string", "minLength": 36, "maxLength": 36}},
}
AUDIO_FIELDS = {
    "text": {"type": "string", "minLength": 1, "maxLength": 1000},
    "voice": {"type": "string", "minLength": 1, "maxLength": 128},
    "speed": {"type": "number", "minimum": 0.5, "maximum": 2},
    "pitch": {"type": "integer", "minimum": -12, "maximum": 12},
    "volume": {"type": "integer", "minimum": -50, "maximum": 100},
}
COLLECT_URL = {
    "type": "string", "minLength": 8, "maxLength": 2048,
    "pattern": "^https?://(?:[^/?#@]+\\.)?(?:douyin\\.com|iesdouyin\\.com|xiaohongshu\\.com|xhslink\\.com|xhslink\\.cn)(?::(?:80|443))?(?:[/?#].*)?$",
    "description": "抖音或小红书的公开内容链接；不接受口令、账号密码、本机路径或其他站点 URL",
}
LEADS_FIELDS = {
    "keyword": {"type": "string", "minLength": 1, "maxLength": 120},
    "platforms": {
        "type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True,
        "items": {"type": "string", "enum": ["douyin", "xhs", "channels"],
                  "minLength": 3, "maxLength": 8},
    },
    "count": {"type": "integer", "minimum": 1, "maximum": 30},
    "pages": {"type": "integer", "minimum": 1, "maximum": 3},
    "channels_targets": {
        "type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "maxLength": 120},
    },
}

AVATAR_ID = {
    "type": "integer", "minimum": 1, "maximum": 9223372036854775807,
    "description": "当前账号已有且已就绪的数字人形象 ID",
}
IMAGE_UPLOAD_ID = {"type": "string", "minLength": 36, "maxLength": 36}
VIDEO_UPLOAD_ID = {"type": "string", "minLength": 36, "maxLength": 36}
TALKING_VIDEO_FIELDS = {
    "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1", "4:5", "5:4"]},
    "motion": {"type": "string", "enum": ["low", "medium", "high"]},
    "subtitle": {"type": "boolean"},
    "subtitle_style": {"type": "string", "enum": ["white", "variety", "bar"]},
    "subtitle_position": {"type": "string", "enum": ["top", "upper", "center", "lower", "bottom"]},
}
VIDEO_LIPSYNC_FIELDS = {
    "video_asset_id": {
        "type": "integer", "minimum": 1, "maximum": 9223372036854775807,
        "description": "当前账号已完成的原视频资产 ID",
    },
    "audio_asset_id": {
        "type": "integer", "minimum": 1, "maximum": 9223372036854775807,
        "description": "当前账号已有的口播音频资产 ID",
    },
    "quality": {"type": "string", "enum": ["speed", "precision"]},
    "dynamic_duration": {"type": "boolean"},
}
DIGITAL_IP_TEXT_FIELDS = {
    "avatar_id": AVATAR_ID,
    "text": {"type": "string", "minLength": 1, "maxLength": 1000},
    "voice": {"type": "string", "minLength": 1, "maxLength": 128},
    **TALKING_VIDEO_FIELDS,
}
DIGITAL_IP_AUDIO_FIELDS = {
    "avatar_id": AVATAR_ID,
    "audio_file": {
        "type": "string", "minLength": 1, "maxLength": 500,
        "description": "从当前账号资产结果取得的 audio_file；不是 URL 或本机路径",
    },
    **TALKING_VIDEO_FIELDS,
}
DIGITAL_IP_BATCH_FIELDS = {
    "avatars": {
        "type": "array", "minItems": 2, "maxItems": 5,
        "items": _schema({
            "avatar_id": AVATAR_ID,
            "label": {"type": "string", "minLength": 1, "maxLength": 60},
        }, ["avatar_id"]),
    },
    "text": {"type": "string", "minLength": 1, "maxLength": 1000},
    "voice": {"type": "string", "minLength": 1, "maxLength": 128},
    **TALKING_VIDEO_FIELDS,
}
CINEMATIC_OPEN_FIELDS = {
    "avatar_id": AVATAR_ID,
    "avatar_ids": {
        "type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True,
        "items": AVATAR_ID,
    },
    "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
    "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1"]},
    "duration": {"type": "integer", "minimum": 4, "maximum": 15},
    "enhance_prompt": {"type": "boolean"},
    "reference_image_upload_ids": {
        "type": "array", "minItems": 1, "maxItems": 8, "items": IMAGE_UPLOAD_ID,
        "description": "形象和参考图共用 9 张额度：1/2/3 个形象最多再传 8/7/6 张参考图",
    },
    "reference_video_upload_ids": {
        "type": "array", "minItems": 1, "maxItems": 3, "items": VIDEO_UPLOAD_ID,
    },
}
CINEMATIC_MOTION_FIELDS = {
    "avatar_id": AVATAR_ID,
    "reference_video_upload_ids": {
        "type": "array", "minItems": 1, "maxItems": 1, "items": VIDEO_UPLOAD_ID,
    },
    "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1"]},
}
TRYON_FAST_FIELDS = {
    "person_image_upload_id": IMAGE_UPLOAD_ID,
    "clothes_upload_id": IMAGE_UPLOAD_ID,
    "seconds": {"type": "integer", "minimum": 5, "maximum": 15},
}
TRYON_CLASSIC_FIELDS = {
    "person_video_upload_id": VIDEO_UPLOAD_ID,
    "clothes_upload_id": IMAGE_UPLOAD_ID,
    "background_upload_id": IMAGE_UPLOAD_ID,
    "seconds": {"type": "integer", "minimum": 1, "maximum": 6},
}

for identifier, name, fields, required in (
    ("image-generate", "图片生成", IMAGE_FIELDS, ["prompt"]),
    ("video-generate", "视频生成", VIDEO_FIELDS, ["prompt"]),
    ("video-lipsync", "原视频口型同步", VIDEO_LIPSYNC_FIELDS,
     ["video_asset_id", "audio_asset_id"]),
    ("audio-generate", "音频生成", AUDIO_FIELDS, ["text"]),
    ("digital-ip-text-generate", "数字IP单条文案生成", DIGITAL_IP_TEXT_FIELDS,
     ["avatar_id", "text", "voice"]),
    ("digital-ip-audio-generate", "数字IP本人资产音频生成", DIGITAL_IP_AUDIO_FIELDS,
     ["avatar_id", "audio_file"]),
    ("digital-ip-batch-generate", "数字IP批量文案生成", DIGITAL_IP_BATCH_FIELDS,
     ["avatars", "text", "voice"]),
    ("cinematic-open-generate", "电影化身开放式生成", CINEMATIC_OPEN_FIELDS,
     ["prompt"]),
    ("cinematic-motion-generate", "电影化身动作模仿", CINEMATIC_MOTION_FIELDS,
     ["avatar_id", "reference_video_upload_ids"]),
    ("tryon-fast-generate", "快速换装", TRYON_FAST_FIELDS,
     ["person_image_upload_id", "clothes_upload_id"]),
    ("tryon-classic-generate", "经典换装", TRYON_CLASSIC_FIELDS,
     ["person_video_upload_id"]),
):
    CAPABILITIES[identifier] = _api(
        identifier, name, identifier,
        "先返回服务器报价；只有用相同参数、quote_token 和 --confirm 重试才会扣点并提交任务。",
        fields, required, "generation:quote", "paid", True,
        {"kind": "server_quote", "unit": "points", "confirmation": "quote_token + --confirm"},
    )

for identifier, name, fields, required in (
    ("collect-content", "采集内容与评论", {"url": COLLECT_URL}, ["url"]),
    ("collect-video", "采集原视频", {"url": COLLECT_URL}, ["url"]),
    ("collect-transcript", "提取口播文案", {"url": COLLECT_URL}, ["url"]),
    ("collect-search", "搜索平台内容", {
        "platform": {"type": "string", "enum": ["douyin", "xhs"]},
        "keyword": {"type": "string", "minLength": 1, "maxLength": 120},
        "page": {"type": "integer", "minimum": 1, "maximum": 50},
    }, ["platform", "keyword"]),
    ("leads-generate", "生成获客名单", LEADS_FIELDS, ["platforms"]),
):
    CAPABILITIES[identifier] = _api(
        identifier, name, identifier,
        "先返回服务器报价；只有用相同参数、quote_token 和 --confirm 重试才会扣点并提交任务。",
        fields, required, "generation:quote", "paid", True,
        {"kind": "server_quote", "unit": "points", "confirmation": "quote_token + --confirm"},
    )
    CAPABILITIES[identifier]["next_actions"] = [
        "确认提交后只用 task 轮询返回的 job_id；不要重复提交相同任务。",
    ]

CAPABILITIES["leads-generate"]["input_schema"]["anyOf"] = [
    {"required": ["keyword"]}, {"required": ["channels_targets"]},
]
CAPABILITIES["leads-generate"]["constraints"] = [
    "platforms 包含 douyin 或 xhs 时必须提供 keyword",
    "platforms 包含 channels 时必须提供 channels_targets",
]

CAPABILITIES["cinematic-open-generate"]["input_schema"]["oneOf"] = [
    {"required": ["avatar_id"]}, {"required": ["avatar_ids"]},
]
CAPABILITIES["tryon-classic-generate"]["input_schema"]["anyOf"] = [
    {"required": ["clothes_upload_id"]}, {"required": ["background_upload_id"]},
]

CAPABILITIES["image-generate"]["constraints"] = [
    "image_upload_id and reference_upload_ids are mutually exclusive",
    "reference_upload_ids limits: openai=16, seedream=10, xiaole=4, banana=14",
    "provider=banana supports model=nb2|pro, count=1|2|4, and ratios 1:1|2:3|3:2|3:4|4:3|4:5|5:4|9:16|16:9|21:9",
    "model is only for provider=banana; variant is only for provider=seedream",
    "mask_upload_id requires image_upload_id, provider=openai, PNG mask, and count=1",
]
CAPABILITIES["video-generate"]["input_schema"]["allOf"] = _video_channel_schema()
CAPABILITIES["video-generate"]["input_schema"]["x-hq-channel-rules"] = VIDEO_CHANNEL_RULES

CAPABILITIES["video-generate"]["constraints"] = [
    "reference_upload_ids limits: grok=7, micro=9, omni=6, minimax=5",
    "channel=minimax accepts only resolution=2k for new tasks",
    "resolution=2k is only valid when channel=minimax",
    "channel-specific ratio, duration/seconds, resolution, model, and reference rules are machine-readable in input_schema.allOf",
    "channel=sora uses model=sora-2|sora-2-pro, seconds=4|8|12, ratio=9:16|16:9, resolution=720p|1024p|1080p, and at most one reference image",
    "channel=sora does not accept duration or generate_audio; seconds is only for sora",
    "@图片N references the Nth item in reference_upload_ids",
]
CAPABILITIES["video-lipsync"]["constraints"] = [
    "video_asset_id and audio_asset_id must be completed assets owned by the current account",
    "quality defaults to speed; precision costs twice as many points",
    "dynamic_duration defaults to false to preserve the source performance timing",
    "the source video must be 1-300 seconds",
]
CAPABILITIES["digital-ip-text-generate"]["constraints"] = [
    "avatar_id must identify a ready avatar owned by the current account",
    "this capability submits exactly one avatar and one script; batch input is not accepted",
    "output resolution is fixed by the main site at 1080p",
]
CAPABILITIES["digital-ip-audio-generate"]["constraints"] = [
    "avatar_id must identify a ready avatar owned by the current account",
    "audio_file must be copied from the current account's assets result and must be mp3|wav|m4a",
    "URLs, local paths, audio uploads, and base64 audio are not accepted",
    "output resolution is fixed by the main site at 1080p",
]
CAPABILITIES["digital-ip-batch-generate"]["constraints"] = [
    "avatars must contain 2-5 distinct ready avatar_id values owned by the current account",
    "each avatar item may include a 1-60 character label",
    "all avatars share the same text, voice, ratio, motion, and subtitle settings",
    "output resolution is fixed by the main site at 1080p",
]
CAPABILITIES["cinematic-open-generate"]["constraints"] = [
    "provide either avatar_id or 1-3 distinct avatar_ids owned by the current account, never both",
    "avatar looks and reference_image_upload_ids share 9 image slots: 1 avatar allows 8 references, 2 allow 7, and 3 allow 6",
    "reference_video_upload_ids accepts 1-3 private video uploads when present",
    "duration defaults to 10 seconds and is limited to 4-15 seconds",
    "output resolution is fixed by the main site at 720p",
]
CAPABILITIES["cinematic-motion-generate"]["constraints"] = [
    "avatar_id must identify one ready cinematic avatar owned by the current account",
    "reference_video_upload_ids must contain exactly one private video upload",
    "output resolution is fixed by the main site at 720p",
]
CAPABILITIES["tryon-fast-generate"]["constraints"] = [
    "person_image_upload_id and clothes_upload_id must be private image uploads owned by the current account",
    "seconds defaults to 6 and is limited to 5-15 seconds",
]
CAPABILITIES["tryon-classic-generate"]["constraints"] = [
    "person_video_upload_id must be one private video upload owned by the current account",
    "provide clothes_upload_id, background_upload_id, or both",
    "seconds defaults to 6 and is limited to 1-6 seconds",
]

for identifier, website_modes in {
    "image": ["banana", "openai", "seedream", "xiaole"],
    "image-upload": ["banana", "openai", "seedream", "xiaole"],
    "video-upload": ["cinematic", "tryon"],
    "image-generate": ["banana", "openai", "seedream", "xiaole"],
    "video": ["one_click", "digital_ip", "cinematic", "tryon", "grok", "sora", "minimax", "omni", "seedance"],
    "video-generate": ["grok", "sora", "minimax", "omni", "seedance"],
    "video-lipsync": ["digital_ip"],
    "digital-ip-text-generate": ["digital_ip"],
    "digital-ip-audio-generate": ["digital_ip"],
    "digital-ip-batch-generate": ["digital_ip"],
    "cinematic-open-generate": ["cinematic"],
    "cinematic-motion-generate": ["cinematic"],
    "tryon-fast-generate": ["tryon"],
    "tryon-classic-generate": ["tryon"],
    "audio": ["tts"], "voices": ["tts"], "audio-generate": ["tts"],
    "one-click-video": ["one_click"],
    "video-compose-projects": ["one_click"], "video-compose-project": ["one_click"],
    "video-compose-create": ["one_click"], "video-compose-analyze": ["one_click"],
    "video-compose-review": ["one_click"], "video-compose-render": ["one_click"],
    "canvas": ["agent", "image_node", "video_node", "digitalPresenter"],
    "canvas-agent-plan": ["agent"],
    "digital-presenter-capability": ["digitalPresenter"],
    "digital-presenter-project": ["digitalPresenter"],
    "digital-presenter-create": ["digitalPresenter"],
    "digital-presenter-update": ["digitalPresenter"],
    "text-video": ["text_video"], "text-video-capability": ["text_video"],
    "text-video-templates": ["text_video"], "text-video-styles": ["text_video"],
    "text-video-voices": ["text_video"],
    "short-drama": ["live_action"],
    "short-drama-projects": ["live_action"], "short-drama-project": ["live_action"],
    "short-drama-conversation": ["live_action"], "short-drama-preflight": ["live_action"],
    "inspiration-catalog": ["inspiration.browse"], "inspiration-likes": ["inspiration.like"],
    "inspiration-like": ["inspiration.like"],
    "collect": ["collect.content.comments", "collect.content.video", "collect.content.transcript", "collect.keyword.search"],
    "collect-content": ["collect.content.comments"],
    "collect-video": ["collect.content.video"],
    "collect-transcript": ["collect.content.transcript"],
    "collect-search": ["collect.keyword.search"],
    "leads": ["leads.keyword.search"], "leads-generate": ["leads.keyword.search"],
    "leads-crm": ["leads.crm.update"], "leads-crm-upsert": ["leads.crm.update"],
    "video-avatars": ["cinematic", "digital_ip", "live_action"], "audio-slots": ["tts"],
    "digital-ip-projects": ["digital_ip"], "digital-ip-project": ["digital_ip"],
    "digital-ip-report": ["digital_ip"],
    "pricing-page": ["pricing.catalog"], "pricing": ["pricing.catalog"],
    "invite": ["invite.dashboard", "invite.poster"],
    "recharge": ["recharge"], "bots": ["bots"],
}.items():
    CAPABILITIES[identifier]["website_modes"] = website_modes


def capability_list():
    return list(CAPABILITIES.values())


def resolve_url(capability, environment, payload):
    url = ENVIRONMENTS[environment] + capability["deep_link"]["path"]
    if payload:
        url += "?" + urlencode(payload)
    return url
