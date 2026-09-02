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


def _download(identifier, name, description, fields, required, scope):
    capability = _api(
        identifier, name, None, description, fields, required, scope,
        side_effect="download",
    )
    capability["kind"] = "download"
    capability["next_actions"] = [
        "Use one explicit absolute --output path; existing files are never overwritten.",
    ]
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
     {"prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
      "engine": {"type": "string", "enum": ["openai", "seedream", "xiaole", "banana"]},
      "inspiration": {"type": "integer", "minimum": 1000000, "maximum": 9223372036854775807}}, "account_for_actions"),
    ("video", "视频工作台", "/workbench/video", "进入视频工作台；只预填，不提交生成。",
     {"prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
      "channel": {"type": "string", "enum": ["grok", "micro", "omni", "minimax", "sora"]},
      "inspiration": {"type": "integer", "minimum": 1000000, "maximum": 9223372036854775807}}, "account_for_actions"),
    ("text-video", "文案成片", "/workbench/text-video", "进入文案成片页；页面入口不会直接提交生成。",
     None, "account_for_actions"),
    ("matrix-template", "模板成片", "/workbench/matrix-template.html", "进入模板成片页；页面入口不会直接提交生成。",
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
CAPABILITIES["director-capability"] = _api(
    "director-capability", "编导能力契约", "director-capability",
    "读取编导工作流、状态、权限、计费和当前可执行动作。", scope="director:read")
REQUEST_ID = {"type": "string", "pattern": "^[A-Za-z0-9._:-]{8,128}$"}
CAPABILITIES["director-chat"] = _api(
    "director-chat", "编导顾客助手", "director-chat",
    "向编导顾客助手提交一轮可追踪对话；返回零点数异步任务。",
    {
        "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
        "session_id": STRING_ID,
        "page_revision": {"type": "integer", "minimum": 1},
        "page_context": {"type": "object"},
        "history": {"type": "array", "maxItems": 12, "items": {"type": "object"}},
        "source_page": {"type": "string", "maxLength": 80},
        "request_id": REQUEST_ID,
    }, ["prompt", "session_id", "page_revision", "page_context", "request_id"],
    "director:write", "external_ai", True,
    {"kind": "external_ai", "points": 0, "detail": "不扣用户点数，但会调用编导模型并创建零点数任务。"})
CAPABILITIES["director-chat"]["next_actions"] = [
    "只用 task 轮询返回的 job_id；若出现 production_offer，先展示费用并等待用户确认。",
]
CAPABILITIES["director-produce"] = _api(
    "director-produce", "确认编导生产单", "director-produce",
    "确认同一轮编导助手返回的冻结脚本生产单。",
    {
        "offer_id": {"type": "string", "pattern": "^director-production-[A-Za-z0-9_-]{16,64}$"},
        "input": {"type": "object"},
        "expected_cost": {"type": "integer", "minimum": 0},
        "plan_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "quote_token": {"type": "string", "minLength": 20, "maxLength": 4096},
    }, ["offer_id", "input", "expected_cost", "plan_digest", "quote_token"],
    "director:generate", "write", True)
CAPABILITIES["director-produce"]["next_actions"] = [
    "只确认用户已经看到并同意的 production_offer；提交后只轮询返回的原 job_id。",
]
DIRECTOR_WORKFLOW_ID = {"type": "string", "pattern": "^dw_[0-9a-f]{32}$"}
DIRECTOR_STORYBOARD = {
    "type": "array", "minItems": 1, "maxItems": 60,
    "items": {"type": "object"},
}
CAPABILITIES["director-workflows"] = _api(
    "director-workflows", "编导工作流列表", "director-workflows",
    "读取本人持久化编导工作流。",
    {"limit": {"type": "integer", "minimum": 1, "maximum": 50},
     "offset": {"type": "integer", "minimum": 0, "maximum": 2000}},
    scope="director:read")
CAPABILITIES["director-workflow-create"] = _api(
    "director-workflow-create", "创建编导工作流", "director-workflow-create",
    "从本人已完成脚本/拆解任务或显式分镜创建持久工作流。",
    {"title": {"type": "string", "minLength": 1, "maxLength": 120},
     "source_job_id": {"type": "integer", "minimum": 1},
     "storyboard": DIRECTOR_STORYBOARD, "request_id": REQUEST_ID},
    ["title", "request_id"], "director:write", "write", True)
CAPABILITIES["director-workflow-create"]["input_schema"]["oneOf"] = [
    {"required": ["source_job_id"]}, {"required": ["storyboard"]},
]
CAPABILITIES["director-workflow"] = _api(
    "director-workflow", "编导工作流", "director-workflow",
    "读取本人一个工作流、当前 revision 与结构化分镜。",
    {"workflow_id": DIRECTOR_WORKFLOW_ID}, ["workflow_id"], "director:read")
CAPABILITIES["director-storyboard-update"] = _api(
    "director-storyboard-update", "更新编导分镜", "director-storyboard-update",
    "按 revision 保存本人结构化分镜，冲突时拒绝覆盖。",
    {"workflow_id": DIRECTOR_WORKFLOW_ID, "revision": {"type": "integer", "minimum": 1},
     "storyboard": DIRECTOR_STORYBOARD},
    ["workflow_id", "revision", "storyboard"], "director:write", "write", True)
CAPABILITIES["director-storyboard-export"] = _api(
    "director-storyboard-export", "导出编导分镜", "director-storyboard-export",
    "读取本人工作流并返回可保存的 Markdown 分镜。",
    {"workflow_id": DIRECTOR_WORKFLOW_ID}, ["workflow_id"], "director:read")
PLAN_DIGEST = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
CAPABILITIES["director-production-plan"] = _api(
    "director-production-plan", "编导生产计划", "director-production-plan",
    "把当前分镜 revision 冻结为一个图片或视频生产方案。",
    {"workflow_id": DIRECTOR_WORKFLOW_ID,
     "output_kind": {"type": "string", "enum": ["image", "video"]},
     "options": {"type": "object"}},
    ["workflow_id", "output_kind", "options"], "director:write", "write", True)
CAPABILITIES["director-production-start"] = _api(
    "director-production-start", "启动编导生产", "director-production-start",
    "先报价；确认后按冻结 plan_digest 启动一次底层图片或视频任务。",
    {"workflow_id": DIRECTOR_WORKFLOW_ID, "plan_digest": PLAN_DIGEST, "request_id": REQUEST_ID},
    ["workflow_id", "plan_digest", "request_id"], "director:generate", "paid", True,
    {"kind": "server_quote", "unit": "points", "confirmation": "same input + quote_token + --confirm"})
CAPABILITIES["director-production-status"] = _api(
    "director-production-status", "编导生产状态", "director-production-status",
    "读取本人工作流最近一次生产及底层 Job 状态。",
    {"workflow_id": DIRECTOR_WORKFLOW_ID}, ["workflow_id"], "director:read")
CAPABILITIES["director-production-recover"] = _api(
    "director-production-recover", "恢复编导生产", "director-production-recover",
    "只重放原 request_id 的结果未知提交，不创建新的付费意图。",
    {"workflow_id": DIRECTOR_WORKFLOW_ID, "plan_digest": PLAN_DIGEST, "request_id": REQUEST_ID},
    ["workflow_id", "plan_digest", "request_id"], "director:recover", "write", True)
CAPABILITIES["director-remake-plan"] = _api(
    "director-remake-plan", "编导同款复刻计划", "director-remake-plan",
    "把当前分镜冻结为电影化身、Grok 或 Seedance 复刻方案。",
    {"workflow_id": DIRECTOR_WORKFLOW_ID,
     "mode": {"type": "string", "enum": ["cinematic", "grok", "micro"]},
     "instruction": {"type": "string", "minLength": 1, "maxLength": 2000},
     "options": {"type": "object"}},
    ["workflow_id", "mode", "instruction", "options"], "director:write", "write", True)
CAPABILITIES["director-remake-start"] = _api(
    "director-remake-start", "启动编导同款复刻", "director-remake-start",
    "先报价；确认后按冻结 plan_digest 启动一次复刻任务。",
    {"workflow_id": DIRECTOR_WORKFLOW_ID, "plan_digest": PLAN_DIGEST, "request_id": REQUEST_ID},
    ["workflow_id", "plan_digest", "request_id"], "director:generate", "paid", True,
    {"kind": "server_quote", "unit": "points", "confirmation": "same input + quote_token + --confirm"})
CAPABILITIES["director-remake-status"] = _api(
    "director-remake-status", "编导同款复刻状态", "director-remake-status",
    "读取本人工作流最近一次复刻及底层 Job 状态。",
    {"workflow_id": DIRECTOR_WORKFLOW_ID}, ["workflow_id"], "director:read")
CAPABILITIES["director-remake-recover"] = _api(
    "director-remake-recover", "恢复编导同款复刻", "director-remake-recover",
    "只重放原 request_id 的结果未知提交，不创建新的付费意图。",
    {"workflow_id": DIRECTOR_WORKFLOW_ID, "plan_digest": PLAN_DIGEST, "request_id": REQUEST_ID},
    ["workflow_id", "plan_digest", "request_id"], "director:recover", "write", True)

DH_RUN_ID = {"type": "string", "pattern": "^dh-run-[A-Za-z0-9._:-]{8,128}$"}
DH_REQUEST_ID = {"type": "string", "pattern": "^[A-Za-z0-9._:-]{8,128}$"}
DH_UPLOAD_ID = {"type": "string", "pattern": "^img_[0-9a-f]{32}$"}
DH_AUDIO_UPLOAD_ID = {"type": "string", "pattern": "^dha_[0-9a-f]{32}$"}
DH_PLAN_FIELDS = {
    "script": {"type": "string", "minLength": 12, "maxLength": 6000},
    "narration_mode": {"type": "string", "enum": ["text", "audio"]},
    "audio_upload_id": DH_AUDIO_UPLOAD_ID,
    "allow_ai_materials": {"type": "boolean"},
    "customer_upload_ids": {"type": "array", "maxItems": 12, "uniqueItems": True,
                            "items": DH_UPLOAD_ID},
}
CAPABILITIES["digital-human-oneclick-capability"] = _api(
    "digital-human-oneclick-capability", "数字人一键生成能力",
    "digital-human-oneclick-capability", "读取普通模式可用形象、音色、素材限制、价格与供应商状态。",
    scope="digital-human-oneclick:read")
CAPABILITIES["digital-human-oneclick-plan"] = _api(
    "digital-human-oneclick-plan", "规划数字人一键生成", "digital-human-oneclick-plan",
    "按文案或本人完整录音生成冻结时间轴、场景与素材需求，并返回 plan_digest。",
    DH_PLAN_FIELDS, ["narration_mode"], "digital-human-oneclick:read")
CAPABILITIES["digital-human-oneclick-plan"]["input_schema"]["oneOf"] = [
    {"required": ["script"]}, {"required": ["audio_upload_id"]},
]
CAPABILITIES["digital-human-oneclick-consent"] = _api(
    "digital-human-oneclick-consent", "确认数字人授权", "digital-human-oneclick-consent",
    "保存与 plan_digest 绑定的本人照片、声音复刻和 AI 素材授权。",
    {
        "confirmed": {"type": "boolean", "const": True},
        "consent_version": {"type": "string", "const": "digital-human-material-v3"},
        "purpose": {"type": "string", "const": "digital_human_material_v3"},
        "run_id": DH_RUN_ID, "plan_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "script": DH_PLAN_FIELDS["script"], "photo_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "voice_mode": {"type": "string", "enum": ["existing", "audio"]},
        "voice_ref": {"type": "string", "maxLength": 180},
        "voice_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        **DH_PLAN_FIELDS,
    }, ["confirmed", "consent_version", "purpose", "run_id", "plan_digest", "photo_sha256",
        "voice_mode", "voice_ref", "narration_mode"],
    "digital-human-oneclick:write", "write", True)
CAPABILITIES["digital-human-oneclick-start"] = _api(
    "digital-human-oneclick-start", "启动数字人一键生成", "digital-human-oneclick-start",
    "先返回服务端报价；确认后以相同输入、quote_token 和 request_id 启动一次可恢复运行。",
    {
        "request_id": DH_REQUEST_ID, "consent_token": {"type": "string", "minLength": 32, "maxLength": 512},
        "plan_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "portrait_upload_id": DH_UPLOAD_ID, "voice_key": {"type": "string", "maxLength": 180},
        **DH_PLAN_FIELDS,
    }, ["request_id", "consent_token", "plan_digest", "narration_mode", "allow_ai_materials",
        "customer_upload_ids", "portrait_upload_id"],
    "digital-human-oneclick:generate", "paid", True,
    {"kind": "server_quote", "unit": "points", "confirmation": "same input + quote_token + --confirm"})
CAPABILITIES["digital-human-oneclick-status"] = _api(
    "digital-human-oneclick-status", "数字人运行状态", "digital-human-oneclick-status",
    "查询原 run_id 的子任务、扣点、退款、失败原因和成片地址。",
    {"run_id": DH_RUN_ID}, ["run_id"], "digital-human-oneclick:read")
for _identifier, _name, _description in (
    ("digital-human-oneclick-recover", "恢复数字人运行", "仅恢复原运行中可安全恢复的失败步骤。"),
    ("digital-human-oneclick-abandon", "放弃数字人运行", "停止后续恢复并保留已有任务和账务审计。"),
):
    CAPABILITIES[_identifier] = _api(
        _identifier, _name, _identifier, _description,
        {"run_id": DH_RUN_ID, "request_id": DH_REQUEST_ID}, ["run_id", "request_id"],
        "digital-human-oneclick:write", "write", True)
CAPABILITIES["digital-human-oneclick-history"] = _api(
    "digital-human-oneclick-history", "数字人成片历史", "digital-human-oneclick-history",
    "读取当前账号的数字人成片历史。",
    {"limit": {"type": "integer", "minimum": 1, "maximum": 50},
     "offset": {"type": "integer", "minimum": 0, "maximum": 2000}},
    scope="digital-human-oneclick:read")
CAPABILITIES["digital-ip-projects"] = _api(
    "digital-ip-projects", "数字化 IP 项目列表", "digital-ip-projects", "读取当前账号的数字化 IP 项目。",
    scope="ip12:read")
CAPABILITIES["digital-ip-project"] = _api(
    "digital-ip-project", "数字化 IP 项目", "digital-ip-project", "读取当前账号的一个数字化 IP 项目。",
    {"project_id": STRING_ID}, ["project_id"], "ip12:read")
CAPABILITIES["digital-ip-report"] = _api(
    "digital-ip-report", "数字化 IP 报告", "digital-ip-report", "读取一个数字化 IP 项目已经保存的报告；不会重新生成。",
    {"project_id": STRING_ID}, ["project_id"], "ip12:read")
CAPABILITIES["digital-ip-create"] = _api(
    "digital-ip-create", "创建数字化 IP 项目", "digital-ip-create", "在当前账号创建一个数字 IP 项目。",
    {"title": {"type": "string", "minLength": 1, "maxLength": 80}}, ["title"], "ip12:write", "write", True)
CAPABILITIES["digital-ip-update"] = _api(
    "digital-ip-update", "更新数字化 IP 项目", "digital-ip-update", "按 revision 更新一个本人数字 IP 项目的标题。",
    {"project_id": STRING_ID,
     "revision": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807},
     "title": {"type": "string", "minLength": 1, "maxLength": 80}},
    ["project_id", "revision", "title"], "ip12:write", "write", True)
CAPABILITIES["digital-ip-delete"] = _api(
    "digital-ip-delete", "删除数字化 IP 项目", "digital-ip-delete", "删除一个本人数字 IP 项目；删除前应先读取并核对目标。",
    {"project_id": STRING_ID,
     "revision": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}},
    ["project_id", "revision"], "ip12:write", "delete", True)
for identifier, name, description in (
    ("text-video-capability", "文案成片可用状态", "读取文案成片功能开关和可用状态。"),
    ("text-video-templates", "文案成片模板", "读取文案成片可用模板。"),
    ("text-video-styles", "文案成片样式", "读取文案成片可用样式。"),
    ("text-video-voices", "文案成片音色", "读取文案成片可用音色。"),
    ("matrix-template-capability", "模板成片可用状态", "读取模板成片功能开关和生成服务状态。"),
    ("matrix-template-templates", "模板成片模板", "读取模板成片可用视觉模板。"),
):
    CAPABILITIES[identifier] = _api(identifier, name, identifier, description, scope="assets:read")
CAPABILITIES["pricing"] = _api(
    "pricing", "点数价格", "pricing", "读取主站当前点数价格目录。", scope="profile:read")
CAPABILITIES["dl"] = _download(
    "dl", "无水印下载", "把黄雀已返回的受支持视频或图片地址下载到一个明确的本地文件。",
    {
        "url": {"type": "string", "minLength": 8, "maxLength": 4096, "pattern": "^https?://.*$"},
        "name": {"type": "string", "minLength": 1, "maxLength": 40},
        "decode_key": {"type": "string", "maxLength": 4096},
    }, ["url"], "assets:read",
)
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
CAPABILITIES["leads-delete"] = _api(
    "leads-delete", "删除获客跟进", "leads-delete", "永久删除当前账号的客户跟进记录；删除前应先读取并核对目标。",
    {"lead_ids": {"type": "array", "minItems": 1, "maxItems": 100, "uniqueItems": True,
                  "items": {"type": "string", "pattern": "^[0-9a-f]{16,40}$"}}},
    ["lead_ids"], "leads:write", "delete", True)
CAPABILITIES["video-avatars"] = _api(
    "video-avatars", "数字人形象", "video-avatars", "读取当前账号可用的数字人形象。",
    {"limit": LIMIT}, scope="assets:read")
CAPABILITIES["audio-slots"] = _api(
    "audio-slots", "声音克隆槽位", "audio-slots", "读取当前账号的声音克隆槽位、状态和当前价格。",
    scope="assets:read")
CAPABILITIES["voice-clone-create"] = _api(
    "voice-clone-create", "创建声音克隆", "voice-clone-create", "用本人样音在选定槽位创建或重新录制个人克隆音色。",
    {"slot_id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_-]{1,87}$"},
     "name": {"type": "string", "minLength": 1, "maxLength": 40},
     "audio_upload_id": {"type": "string", "minLength": 36, "maxLength": 36}},
    ["slot_id", "name", "audio_upload_id"], "assets:write", "write", True)
CAPABILITIES["voice-clone-create"]["constraints"] = [
    "slot_id must come from audio-slots and belong to the current account",
    "audio_upload_id must come from audio-upload and belong to the current account",
    "the server normalizes up to 60 seconds of clear speech before creating or replacing the cloned voice",
    "for reliable cloning, use 30-60 seconds of continuous, clear, single-speaker speech; file duration alone does not guarantee enough effective speech",
    "long silence, music, and noise do not count as effective speech",
]
CAPABILITIES["voice-clone-create"]["next_actions"] = [
    "提交后只用 voice-clone-status 轮询原 slot_id，直到 ready 或 failed。",
    "若返回有效语音太短，重新上传30至60秒连续、清晰、单人说话的样音，再用新的 audio_upload_id 提交。",
]
CAPABILITIES["voice-clone-status"] = _api(
    "voice-clone-status", "声音克隆状态", "voice-clone-status", "读取一个本人声音克隆槽位的处理状态。",
    {"slot_id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_-]{1,87}$"}},
    ["slot_id"], "assets:read")
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
CAPABILITIES["short-drama-create"] = _api(
    "short-drama-create", "创建短剧项目", "short-drama-create", "在当前账号创建一个短剧项目；request_id 用于幂等重试。",
    {"title": {"type": "string", "minLength": 1, "maxLength": 80},
     "synopsis": {"type": "string", "minLength": 8, "maxLength": 4000},
     "ratio": {"type": "string", "enum": ["9:16", "16:9"]},
     "target_duration": {"type": "integer", "enum": [30, 45, 60]},
     "shot_count": {"type": "integer", "minimum": 6, "maximum": 10},
     "genre": {"type": "string", "maxLength": 40},
     "visual_style": {"type": "string", "maxLength": 80},
     "request_id": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{8,128}$"}},
    ["title", "synopsis", "ratio", "target_duration", "shot_count", "request_id"],
    "short-drama:write", "write", True)
CAPABILITIES["short-drama-delete"] = _api(
    "short-drama-delete", "删除短剧项目", "short-drama-delete", "删除一个本人短剧项目；删除前应先读取并核对目标。",
    {"project_id": {"type": "string", "minLength": 1, "maxLength": 160},
     "revision": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}},
    ["project_id", "revision"], "short-drama:write", "delete", True)
SD_INT = {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}
SD_PROVIDER_FIELDS = {
    "project_id": STRING_ID, "plan_id": STRING_ID, "shot_key": STRING_ID,
    "character_key": STRING_ID, "avatar_id": STRING_ID,
}
CAPABILITIES["short-drama-advisor"] = _api(
    "short-drama-advisor", "短剧立项顾问", "short-drama-advisor",
    "协商短剧主题、主角、冲突、情绪、结局、受众和风格。",
    {
        "messages": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 600}},
        "understanding": {"type": "object"},
        "expected_field": {"type": "string", "enum": ["", "topic", "protagonist", "conflict", "emotion", "ending", "audience", "style"]},
        "field_states": {"type": "object"},
        "recommendation_context": {"type": "object"},
        "user_message": {"type": "string", "minLength": 1, "maxLength": 600},
        "request_id": REQUEST_ID,
    }, ["user_message", "request_id"], "short-drama:write", "external_ai", True,
    {"kind": "external_ai", "points": 0, "detail": "不扣用户点数，受平台免费额度和速率限制。"})
CAPABILITIES["short-drama-character-reference-generate"] = _api(
    "short-drama-character-reference-generate", "生成短剧角色标准图",
    "short-drama-character-reference-generate",
    "先报价；确认后按当前项目版本和角色资料生成一张标准图。",
    {"project_id": STRING_ID, "revision": SD_INT, "character_key": STRING_ID},
    ["project_id", "revision", "character_key"], "generation:quote", "paid", True,
    {"kind": "server_quote", "unit": "points", "confirmation": "same input + quote_token + --confirm"})
CAPABILITIES["short-drama-character-reference-confirm"] = _api(
    "short-drama-character-reference-confirm", "确认短剧角色标准图",
    "short-drama-character-reference-confirm", "锁定已经完成并审核的角色标准图版本。",
    {"project_id": STRING_ID, "revision": SD_INT, "character_key": STRING_ID, "reference_version": SD_INT},
    ["project_id", "revision", "character_key", "reference_version"],
    "short-drama:write", "write", True)
CAPABILITIES["short-drama-preflight-plan"] = _api(
    "short-drama-preflight-plan", "生成短剧制作体检", "short-drama-preflight-plan",
    "根据锁定剧本生成制作方案与阻塞项。",
    {"project_id": STRING_ID, "conversation_revision": SD_INT,
     "quality_route": {"type": "string", "enum": ["quick_draft", "quality_first"]},
     "request_id": REQUEST_ID},
    ["project_id", "conversation_revision", "request_id"], "short-drama:write", "write", True)
CAPABILITIES["short-drama-preflight-confirm"] = _api(
    "short-drama-preflight-confirm", "确认短剧制作体检", "short-drama-preflight-confirm",
    "确认当前制作方案及系统要求人工接受的问题。",
    {"project_id": STRING_ID, "plan_id": STRING_ID, "plan_version": SD_INT,
     "accepted_issue_keys": {"type": "array", "maxItems": 100, "items": STRING_ID},
     "request_id": REQUEST_ID},
    ["project_id", "plan_id", "plan_version", "accepted_issue_keys", "request_id"],
    "short-drama:write", "write", True)
CAPABILITIES["short-drama-autodraft-preflight"] = _api(
    "short-drama-autodraft-preflight", "短剧单镜头生产预检",
    "short-drama-autodraft-preflight", "编译一个镜头的真实视频供应商请求，不扣点。",
    {**SD_PROVIDER_FIELDS, "execution": {"type": "object"}},
    ["project_id", "plan_id", "shot_key"], "short-drama:write", "write", True)
CAPABILITIES["short-drama-autodraft-quote"] = _api(
    "short-drama-autodraft-quote", "短剧单镜头报价", "short-drama-autodraft-quote",
    "为已预检的单镜头视频创建报价，不扣点。", SD_PROVIDER_FIELDS,
    ["project_id", "plan_id", "shot_key"], "short-drama:read")
CAPABILITIES["short-drama-autodraft-start"] = _api(
    "short-drama-autodraft-start", "启动短剧单镜头生产", "short-drama-autodraft-start",
    "确认原单镜头报价并启动一次可恢复任务。",
    {"project_id": STRING_ID, "quote_token": {"type": "string", "minLength": 1, "maxLength": 4096},
     "request_id": REQUEST_ID},
    ["project_id", "quote_token", "request_id"], "short-drama:write", "write", True)
CAPABILITIES["short-drama-autodraft-status"] = _api(
    "short-drama-autodraft-status", "短剧单镜头任务状态", "short-drama-autodraft-status",
    "读取原单镜头任务及扣点退款状态。",
    {"project_id": STRING_ID, "job_id": STRING_ID}, ["project_id", "job_id"], "short-drama:read")
CAPABILITIES["short-drama-delivery-quote"] = _api(
    "short-drama-delivery-quote", "短剧正式交付报价", "short-drama-delivery-quote",
    "对当前已验收精修版本建立正式交付报价。",
    {"project_id": STRING_ID, "version_id": STRING_ID}, ["project_id", "version_id"], "short-drama:read")
CAPABILITIES["short-drama-delivery-start"] = _api(
    "short-drama-delivery-start", "启动短剧正式交付", "short-drama-delivery-start",
    "确认原正式交付报价并启动一次可恢复任务。",
    {"project_id": STRING_ID, "quote_token": {"type": "string", "minLength": 1, "maxLength": 4096},
     "request_id": REQUEST_ID},
    ["project_id", "quote_token", "request_id"], "short-drama:write", "write", True)
CAPABILITIES["short-drama-delivery-status"] = _api(
    "short-drama-delivery-status", "短剧正式交付状态", "short-drama-delivery-status",
    "读取原正式交付任务及退款状态。",
    {"project_id": STRING_ID, "job_id": STRING_ID}, ["project_id", "job_id"], "short-drama:read")
for _identifier, _name, _description in (
    ("short-drama-completion-readiness", "短剧完成门禁", "读取完成交付前的锁、媒体、任务和账务阻塞项。"),
    ("short-drama-completion", "短剧完成快照", "读取已经确认的不可变完成快照。"),
):
    CAPABILITIES[_identifier] = _api(
        _identifier, _name, _identifier, _description,
        {"project_id": STRING_ID}, ["project_id"], "short-drama:read")
CAPABILITIES["short-drama-completion-confirm"] = _api(
    "short-drama-completion-confirm", "确认短剧完成", "short-drama-completion-confirm",
    "以最新 readiness 返回值确认不可逆交付。",
    {"project_id": STRING_ID, "revision": SD_INT, "final_version_id": STRING_ID,
     "asset_id": STRING_ID, "delivery_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
     "acknowledged": {"type": "boolean", "const": True}, "request_id": REQUEST_ID},
    ["project_id", "revision", "final_version_id", "asset_id", "delivery_hash", "acknowledged", "request_id"],
    "short-drama:write", "write", True)
CAPABILITIES["ip12-projects"] = _api(
    "ip12-projects", "IP12 项目列表", "ip12-projects", "读取当前账号在主站 Hermes IP12 中的全部诊断项目。", scope="ip12:read")
CAPABILITIES["ip12-project"] = _api(
    "ip12-project", "IP12 项目资料", "ip12-project", "读取一个本人 Hermes IP12 项目的基础资料、对话、模块进度与已存报告。",
    {"project_id": STRING_ID}, ["project_id"], "ip12:read")
CAPABILITIES["ip12-create"] = _api(
    "ip12-create", "创建 IP12 项目", "ip12-create", "在当前账号创建一个新的 IP12 项目。",
    {"title": {"type": "string", "minLength": 1, "maxLength": 120}}, ["title"], "ip12:write", "write", True)
CAPABILITIES["ip12-delete"] = _api(
    "ip12-delete", "删除 IP12 项目", "ip12-delete", "删除当前账号的一个 IP12 项目；删除前应先读取并核对目标。",
    {"project_id": STRING_ID}, ["project_id"], "ip12:write", "delete", True)
CAPABILITIES["ip12-delete"]["next_actions"] = [
    "删除不可恢复；先用 ip12-project 读取核对 project_id，再以 --confirm 确认删除。",
]
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
CAPABILITIES["canvas-delete"] = _api(
    "canvas-delete", "删除画布", "canvas-delete", "删除本人创建的画布；只有所有者可删，删除不可恢复。",
    {"board_id": STRING_ID}, ["board_id"], "canvas:write", "delete", True)
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
CAPABILITIES["audio-upload"] = _upload(
    "audio-upload", "上传生成参考音频",
    "把一个本地 MP3、WAV、M4A、AAC 或 OGG 流式上传为本人短期私有 upload_id；不扣点，不返回公开素材地址。",
    "assets:upload",
)
CAPABILITIES["audio-upload"]["file_input"] = {
    "argument": "--file", "path": "absolute", "maxBytes": 10 * 1024 * 1024,
    "mimeTypes": ["audio/mpeg", "audio/wav", "audio/mp4", "audio/aac", "audio/ogg"],
    "accountActiveMaxFiles": 20, "accountActiveMaxBytes": 96 * 1024 * 1024,
}
CAPABILITIES["audio-upload"]["constraints"] = [
    "audio must contain a readable stream no longer than 300 seconds",
    "the upload is private to the current account; use result.expires_in as the authoritative lifetime",
]
CAPABILITIES["audio-upload"]["next_actions"] = [
    "把返回的 result.upload_id 作为 audio_upload_id 写入 voice-clone-create 或 digital-ip-audio-generate。",
]
CAPABILITIES["digital-human-oneclick-material-upload"] = _upload(
    "digital-human-oneclick-material-upload", "上传数字人顾客素材",
    "把本人明确指定的 PNG、JPG 或 WebP 私密上传到数字人一键生成素材区。",
    "digital-human-oneclick:write",
)
CAPABILITIES["digital-human-oneclick-material-upload"]["next_actions"] = [
    "把返回的 upload_id 加入 plan/start 的 customer_upload_ids；只能用于当前账号。",
]
CAPABILITIES["digital-human-oneclick-audio-upload"] = _upload(
    "digital-human-oneclick-audio-upload", "上传数字人完整录音",
    "把本人完整口播录音私密上传并安全切片；必须先生成一个稳定的 dh-run-* 标识。",
    "digital-human-oneclick:write",
)
CAPABILITIES["digital-human-oneclick-audio-upload"]["file_input"] = {
    "argument": "--file", "path": "absolute", "maxBytes": 30 * 1024 * 1024,
    "mimeTypes": ["audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/x-m4a", "audio/aac"],
    "requiredMetadata": {"--run-id": "^dh-run-[A-Za-z0-9._:-]{8,128}$"},
}
CAPABILITIES["digital-human-oneclick-audio-upload"]["next_actions"] = [
    "把返回的 audio_upload_id 与同一个 run_id 用于 plan、consent 和 start。",
]
CAPABILITIES["director-breakdown-upload"] = _upload(
    "director-breakdown-upload", "编导本地素材反推",
    "上传一张本地图片或一个本地视频，直接创建当前账号的付费提示词反推任务。",
    "director:generate",
)
CAPABILITIES["director-breakdown-upload"]["file_input"] = {
    "argument": "--file", "path": "absolute", "maxBytes": 200 * 1024 * 1024,
    "mimeTypes": ["image/jpeg", "image/png", "image/webp",
                  "video/mp4", "video/quicktime", "video/webm"],
    "imageMaxBytes": 20 * 1024 * 1024, "videoMaxSeconds": 120,
}
CAPABILITIES["director-breakdown-upload"]["side_effect"] = "paid"
CAPABILITIES["director-breakdown-upload"]["cost"] = {
    "kind": "server_quote", "unit": "points", "points_kind": "breakdown",
    "confirmation": "quote_token + --confirm + --expected-cost",
}
CAPABILITIES["director-breakdown-upload"]["transport"] = {
    "kind": "dedicated_upload",
    "quote_path": "/api/auth/cli/director-breakdown-quote",
    "quote_token_header": "X-HQ-Quote-Token",
    "expected_cost_header": "X-HQ-Expected-Cost",
    "idempotency_header": "Idempotency-Key",
}
CAPABILITIES["director-breakdown-upload"]["constraints"] = [
    "the unconfirmed call hashes the selected file locally and returns a server quote without uploading it",
    "confirmation must reuse the same file and quote_token and include --expected-cost from that quote",
    "the Idempotency-Key is stable for retries with the same quote_token",
    "images are limited to 20 MiB; videos are limited to 200 MiB and 120 seconds",
]
CAPABILITIES["director-breakdown-upload"]["next_actions"] = [
    "报价后审核 cost；确认时复用同一文件、quote_token 和 expected-cost。",
    "保存返回的 job_id 并只用 task 轮询；响应不确定时复用原 quote_token 重试，禁止重新报价。",
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
ASSET_DELETE_KINDS = ["image", "audio", "video", "copy", "collect", "leads", "breakdown"]
CAPABILITIES["asset-delete"] = _api(
    "asset-delete", "删除资产", "asset-delete",
    "永久删除当前账号自产资产；先用 assets 读取核对 kind 与 id/keys，删除不可恢复。",
    {
        "kind": {"type": "string", "enum": ASSET_DELETE_KINDS},
        "id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807},
        "keys": {"type": "array", "minItems": 1, "maxItems": 200, "uniqueItems": True,
                 "items": {"type": "string", "minLength": 1, "maxLength": 500}},
    },
    ["kind"], "assets:write", "delete", True)
CAPABILITIES["asset-delete"]["input_schema"]["oneOf"] = [
    {"required": ["id"]}, {"required": ["keys"]},
]
CAPABILITIES["asset-delete"]["constraints"] = [
    "provide exactly one of id (single delete) or keys (batch 1-200, same kind)",
    "id and keys must come from the assets read capability for the same kind",
    "only assets owned by the current account can be deleted; anything else is rejected",
    "avatar kind is not deletable through this capability",
    "deletion is irreversible and always requires --confirm",
]
CAPABILITIES["asset-delete"]["next_actions"] = [
    "删除不可恢复；先用 assets 读取核对 id/keys，再以 --confirm 确认删除。",
]

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
CAPABILITIES["video-compose-delete"] = _api(
    "video-compose-delete", "删除一键成片项目", "video-compose-delete", "删除一个本人一键成片项目；删除前应先读取并核对目标。",
    {"project_id": COMPOSE_PROJECT_ID, "expected_revision": REVISION},
    ["project_id", "expected_revision"], "video-compose:write", "delete", True)

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
CAPABILITIES["digital-presenter-delete"] = _api(
    "digital-presenter-delete", "删除数字人口播项目", "digital-presenter-delete", "按 revision 删除本人有编辑权限的数字人口播项目。",
    {"board_id": STRING_ID, "project_id": DP_PROJECT_ID, "revision": REVISION},
    ["board_id", "project_id", "revision"], "digital-presenter:write", "delete", True)

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
COLLECT_MEDIA_URL = {
    "type": "string", "minLength": 8, "maxLength": 2048,
    "pattern": "^(?:https?://(?:[^/?#@]+\\.)?(?:douyin\\.com|iesdouyin\\.com|xiaohongshu\\.com|xhslink\\.com|xhslink\\.cn|bilibili\\.com|b23\\.tv)(?::(?:80|443))?(?:[/?#].*)?|https://weixin\\.qq\\.com(?::443)?/sph/[A-Za-z0-9]+(?:[?#].*)?)$",
    "description": "抖音、小红书、视频号或 B 站的公开内容链接；视频号须使用 weixin.qq.com/sph/ 分享链接",
}
COLLECT_CONTENT_URL = {
    "type": "string", "minLength": 8, "maxLength": 2048,
    "pattern": "^(?:https?://(?:[^/?#@]+\\.)?(?:douyin\\.com|iesdouyin\\.com|xiaohongshu\\.com|xhslink\\.com|xhslink\\.cn|bilibili\\.com|b23\\.tv|x\\.com|twitter\\.com)(?::(?:80|443))?(?:[/?#].*)?|https://weixin\\.qq\\.com(?::443)?/sph/[A-Za-z0-9]+(?:[?#].*)?)$",
    "description": "抖音、小红书、视频号、B 站或 X 单帖公开链接；视频号须使用 weixin.qq.com/sph/ 分享链接",
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
AUDIO_UPLOAD_ID = {"type": "string", "minLength": 36, "maxLength": 36}
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
    "image_upload_id": IMAGE_UPLOAD_ID,
    "text": {"type": "string", "minLength": 1, "maxLength": 1000},
    "voice": {"type": "string", "minLength": 1, "maxLength": 128},
    **TALKING_VIDEO_FIELDS,
}
DIGITAL_IP_AUDIO_FIELDS = {
    "avatar_id": AVATAR_ID,
    "image_upload_id": IMAGE_UPLOAD_ID,
    "audio_file": {
        "type": "string", "minLength": 1, "maxLength": 500,
        "description": "从当前账号资产结果取得的 audio_file；不是 URL 或本机路径",
    },
    "audio_upload_id": AUDIO_UPLOAD_ID,
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
AVATAR_CREATE_FIELDS = {
    "image_data": {"type": "string", "minLength": 32, "maxLength": 12 * 1024 * 1024,
                   "description": "本人真人照片的 data URL（jpg/png/webp，正脸清晰、光线充足）"},
    "name": {"type": "string", "minLength": 1, "maxLength": 40},
}

TEXT_VIDEO_AVATAR_ID = {"type": "string", "pattern": "^local_avatar_[0-9a-f]{32}$"}
TEXT_VIDEO_PLAN_ID = {"type": "string", "pattern": "^talking_plan_[0-9a-f]{32}$"}
TEXT_VIDEO_SCENE_ID = {"type": "string", "pattern": "^scene_[0-9]{2}$"}
TEXT_VIDEO_FIELDS = {
    "text": {"type": "string", "minLength": 2, "maxLength": 1000},
    "template": {"type": "string", "minLength": 1, "maxLength": 240},
    "mode": {"type": "string", "enum": ["generate", "fixed"]},
    "style": {"type": "string", "minLength": 1, "maxLength": 80},
    "voice": {"type": "string", "minLength": 1, "maxLength": 200},
    "speech_rate": {"type": "number", "minimum": 0.5, "maximum": 2.0},
    "talking_material": _schema({
        "enabled": {"type": "boolean", "const": True},
        "plan_id": TEXT_VIDEO_PLAN_ID,
        "source_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "ratio": {"type": "number", "minimum": 0.1, "maximum": 0.5},
        "default_avatar_asset_id": TEXT_VIDEO_AVATAR_ID,
        "scenes": {"type": "array", "minItems": 1, "maxItems": 20, "items": _schema({
            "scene_id": TEXT_VIDEO_SCENE_ID, "enabled": {"type": "boolean"},
            "avatar_asset_id": TEXT_VIDEO_AVATAR_ID,
        }, ["scene_id", "enabled"])},
    }, ["enabled", "plan_id", "source_hash", "ratio", "default_avatar_asset_id", "scenes"]),
}
TEXT_VIDEO_PLAN_FIELDS = {
    key: value for key, value in TEXT_VIDEO_FIELDS.items() if key != "talking_material"
}
TEXT_VIDEO_PLAN_FIELDS["ratio"] = {"type": "number", "minimum": 0.1, "maximum": 0.5}

MATRIX_TEMPLATE_FIELDS = {
    "top_text": {"type": "string", "minLength": 2, "maxLength": 60},
    "bottom_text": {"type": "string", "minLength": 2, "maxLength": 80},
    "template_id": {"type": "string", "minLength": 1, "maxLength": 64,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
    "font_family": {"type": "string", "maxLength": 80},
}
MATRIX_TEMPLATE_BATCH_FIELDS = {
    **MATRIX_TEMPLATE_FIELDS,
    "count": {"type": "integer", "minimum": 2, "maximum": 5},
}

for identifier, name, fields, required in (
    ("image-generate", "图片生成", IMAGE_FIELDS, ["prompt"]),
    ("video-generate", "视频生成", VIDEO_FIELDS, ["prompt"]),
    ("video-lipsync", "原视频口型同步", VIDEO_LIPSYNC_FIELDS,
     ["video_asset_id", "audio_asset_id"]),
    ("audio-generate", "音频生成", AUDIO_FIELDS, ["text"]),
    ("text-video-generate", "文案成片生成", TEXT_VIDEO_FIELDS,
     ["text", "template", "style", "voice"]),
    ("matrix-template-generate", "模板成片生成", MATRIX_TEMPLATE_FIELDS,
     ["top_text", "bottom_text", "template_id"]),
    ("matrix-template-batch-generate", "模板成片批量生成", MATRIX_TEMPLATE_BATCH_FIELDS,
     ["top_text", "bottom_text", "template_id", "count"]),
    ("digital-ip-text-generate", "数字IP单条文案生成", DIGITAL_IP_TEXT_FIELDS,
     ["text", "voice"]),
    ("digital-ip-audio-generate", "数字IP本人资产音频生成", DIGITAL_IP_AUDIO_FIELDS,
     []),
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
    ("video-avatar-create", "创建数字人形象", AVATAR_CREATE_FIELDS,
     ["image_data"]),
):
    CAPABILITIES[identifier] = _api(
        identifier, name, identifier,
        "先返回服务器报价；只有用相同参数、quote_token 和 --confirm 重试才会扣点并提交任务。",
        fields, required, "generation:quote", "paid", True,
        {"kind": "server_quote", "unit": "points", "confirmation": "quote_token + --confirm"},
    )

DIRECTOR_SCRIPT_FIELDS = {
    "prompt": {"type": "string", "minLength": 1, "maxLength": 20000},
    "style": {"type": "string", "enum": ["spoken", "story", "recommend"]},
    "duration": {"type": "integer", "enum": [15, 30, 60]},
    "platform": {"type": "string", "enum": ["douyin", "xiaohongshu", "channels"]},
}
DIRECTOR_BREAKDOWN_FIELDS = {
    "url": {"type": "string", "minLength": 1, "maxLength": 2000},
    "urls": {"type": "array", "minItems": 1, "maxItems": 5,
             "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
    "mode": {"type": "string", "enum": ["scenes", "reverse_prompt"]},
}
DIRECTOR_SCENE_IMAGE_FIELDS = {
    "scenes": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "scene": {"type": "string", "maxLength": 2000},
            "line": {"type": "string", "maxLength": 2000},
            "dur": {"type": "number", "exclusiveMinimum": 0, "maximum": 180},
        },
    }},
    "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1", "4:5", "5:4"]},
    "quality": {"type": "string", "enum": ["standard", "hd"]},
}
DIRECTOR_SCENE_VIDEO_FIELDS = {
    "scenes": DIRECTOR_SCENE_IMAGE_FIELDS["scenes"],
    **{key: item for key, item in VIDEO_FIELDS.items() if key != "prompt"},
}
CAPABILITIES["video-avatar-create"]["constraints"] = [
    "image_data must be a jpg/png/webp data URL of the account holder's own portrait with a clear frontal face and good lighting",
    "creation costs points per avatar.create; a quote is returned first and points are deducted only after --confirm",
    "creations without a detectable face fail fast with a human-readable message; no points are deducted on failure",
]
CAPABILITIES["video-avatar-create"]["next_actions"] = [
    "提交后轮询 video-avatars 直到新形象 status 变为 ready（约 30 秒）。",
    "若提示未检测到人脸，换一张正脸清晰、光线充足的照片重新提交。",
]

for identifier, name, fields, required in (
    ("director-script-generate", "编导脚本生成", DIRECTOR_SCRIPT_FIELDS, ["prompt"]),
    ("director-breakdown", "编导链接拆解", DIRECTOR_BREAKDOWN_FIELDS, []),
    ("director-scene-image-generate", "编导分镜图片生成", DIRECTOR_SCENE_IMAGE_FIELDS, ["scenes"]),
    ("director-scene-video-generate", "编导分镜视频生成", DIRECTOR_SCENE_VIDEO_FIELDS, ["scenes"]),
    ("director-scene-talking-generate", "编导口播镜头生成", TEXT_VIDEO_FIELDS,
     ["text", "template", "style", "voice"]),
    ("collect-content", "采集内容与评论", {"url": COLLECT_CONTENT_URL}, ["url"]),
    ("collect-video", "采集原视频", {"url": COLLECT_MEDIA_URL}, ["url"]),
    ("collect-transcript", "提取口播文案", {"url": COLLECT_MEDIA_URL}, ["url"]),
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
    if identifier.startswith("director-"):
        CAPABILITIES[identifier]["required_scope"] = "director:generate"
    CAPABILITIES[identifier]["next_actions"] = [
        "确认提交后只用 task 轮询返回的 job_id；不要重复提交相同任务。",
    ]

CAPABILITIES["director-breakdown"]["input_schema"]["oneOf"] = [
    {"required": ["url"]}, {"required": ["urls"]},
]
CAPABILITIES["director-scene-image-generate"]["constraints"] = [
    "至少一个 scene 必须包含非空画面描述",
    "先报价，再用完全相同的标准化输入确认一次",
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
CAPABILITIES["digital-ip-text-generate"]["input_schema"]["oneOf"] = [
    {"required": ["avatar_id"]}, {"required": ["image_upload_id"]},
]
CAPABILITIES["digital-ip-audio-generate"]["input_schema"]["oneOf"] = [
    {"required": ["avatar_id", "audio_file"]}, {"required": ["avatar_id", "audio_upload_id"]},
    {"required": ["image_upload_id", "audio_file"]}, {"required": ["image_upload_id", "audio_upload_id"]},
]
CAPABILITIES["text-video-generate"]["constraints"] = [
    "template, style, and voice must be selected from text-video-templates, text-video-styles, and text-video-voices",
    "mode defaults to generate; fixed preserves the supplied copy and automatically splits scenes",
    "the first call returns scene_count and cost_breakdown without submitting a paid task",
    "talking_material must come from text-video-plan and owner-scoped text-video-avatar-import results",
]
CAPABILITIES["text-video-generate"]["next_actions"] = [
    "核对 scene_count 和 cost_breakdown 后，用完全相同的输入、quote_token 与 --confirm 提交；拿到 job_id 后仅使用 task 轮询。",
]
CAPABILITIES["matrix-template-generate"]["constraints"] = [
    "template_id must be selected from matrix-template-templates",
    "font_family is optional and must be selected from matrix-template-templates fonts",
    "duration is calculated automatically and BGM is enabled by default",
    "the first call only quotes the fixed template-video cost",
]
CAPABILITIES["matrix-template-generate"]["next_actions"] = [
    "核对报价后，用完全相同的输入、quote_token 与 --confirm 提交；拿到 job_id 后仅使用 task 轮询。",
]
CAPABILITIES["matrix-template-batch-generate"]["constraints"] = [
    "template_id and optional font_family must be selected from matrix-template-templates",
    "count creates 2-5 independent jobs under one total quote and one confirmation",
    "duration is calculated automatically and BGM is enabled by default",
]
CAPABILITIES["matrix-template-batch-generate"]["next_actions"] = [
    "核对总价与 count 后，用完全相同的输入、quote_token 与 --confirm 提交；只轮询返回的 job_ids。",
]
CAPABILITIES["text-video-avatar-import"] = _api(
    "text-video-avatar-import", "导入口播人物", "text-video-avatar-import",
    "把本人 image-upload 的临时图片导入为文案成片人物资产。",
    {"image_upload_id": IMAGE_UPLOAD_ID}, ["image_upload_id"],
    "assets:upload", "write", True,
)
CAPABILITIES["text-video-avatar-import"]["next_actions"] = [
    "保存返回的 asset_id；可作为默认人物或某个口播分镜的 avatar_asset_id。",
]
CAPABILITIES["text-video-plan"] = _api(
    "text-video-plan", "规划口播分镜", "text-video-plan",
    "生成可确认的口播分镜方案。",
    TEXT_VIDEO_PLAN_FIELDS, ["text", "template", "style", "voice"],
    "generation:quote", "write", True,
)
CAPABILITIES["text-video-plan"]["next_actions"] = [
    "把 plan_id、source_hash、ratio、人物 asset_id 和逐分镜 enabled 组合为 text-video-generate.talking_material。",
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
    "channel=omni accepts resolution=720p, duration=3-10, ratio=9:16|16:9, and up to 6 JPEG/PNG/WebP references from image-upload",
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
    "provide exactly one of avatar_id or image_upload_id, and exactly one of audio_file or audio_upload_id",
    "audio_file must be copied from the current account's assets result and must be mp3|wav|m4a",
    "audio_upload_id must come from a private upload; URLs and local paths are not accepted",
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
    "audio-upload": ["tts", "digital_ip"],
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
    "video-compose-delete": ["one_click"],
    "canvas": ["agent", "image_node", "video_node", "digitalPresenter"],
    "canvas-delete": ["agent"],
    "canvas-agent-plan": ["agent"],
    "digital-presenter-capability": ["digitalPresenter"],
    "digital-presenter-project": ["digitalPresenter"],
    "digital-presenter-create": ["digitalPresenter"],
    "digital-presenter-update": ["digitalPresenter"],
    "digital-presenter-delete": ["digitalPresenter"],
    "text-video": ["text_video"], "text-video-capability": ["text_video"],
    "text-video-generate": ["text_video"], "text-video-avatar-import": ["text_video"],
    "text-video-plan": ["text_video"],
    "text-video-templates": ["text_video"], "text-video-styles": ["text_video"],
    "text-video-voices": ["text_video"],
    "matrix-template-capability": ["matrix_template.single"],
    "matrix-template": ["matrix_template.single"],
    "matrix-template-templates": ["matrix_template.single"],
    "matrix-template-generate": ["matrix_template.single"],
    "matrix-template-batch-generate": ["matrix_template.batch"],
    "short-drama": ["live_action"],
    "short-drama-create": ["live_action"], "short-drama-delete": ["live_action"],
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
    "leads-delete": ["leads.crm.update"],
    "video-avatars": ["cinematic", "digital_ip", "live_action"], "audio-slots": ["tts"],
    "voice-clone-create": ["tts"], "voice-clone-status": ["tts"],
    "digital-ip-projects": ["digital_ip"], "digital-ip-project": ["digital_ip"],
    "digital-ip-report": ["digital_ip"],
    "digital-ip-create": ["digital_ip"], "digital-ip-update": ["digital_ip"], "digital-ip-delete": ["digital_ip"],
    "pricing-page": ["pricing.catalog"], "pricing": ["pricing.catalog"],
    "invite": ["invite.dashboard", "invite.poster"],
    "recharge": ["recharge"], "bots": ["bots"],
}.items():
    CAPABILITIES[identifier]["website_modes"] = website_modes


_SITE_OPERATIONS = {
    "inspiration": ["inspiration.browse", "inspiration.like", "inspiration.handoff"],
    "leads": ["leads.keyword.search", "leads.crm.update"],
    "collect": ["collect.content.comments", "collect.content.video", "collect.content.transcript", "collect.keyword.search"],
    "banana": [
        "image.banana.nb2.text", "image.banana.nb2.reference", "image.banana.pro.text", "image.banana.pro.reference",
        "image.openai.text", "image.openai.reference", "image.openai.inpaint",
        "image.seedream.std.text", "image.seedream.std.reference", "image.seedream.pro.text", "image.seedream.pro.reference",
        "image.xiaole.text", "image.xiaole.reference", "image.prompt.optimize", "image.prompt.reverse",
    ],
    "video": [
        "video.one_click.compose", "video.digital_ip.text.single", "video.digital_ip.text.batch", "video.digital_ip.audio",
        "video.cinematic.motion", "video.cinematic.open", "video.tryon.fast", "video.tryon.classic",
        "video.grok.text", "video.grok.image", "video.sora.text", "video.sora.image", "video.minimax.image",
        "video.omni.text", "video.omni.image", "video.seedance.text", "video.seedance.image", "video.asset.import_h3",
    ],
    "audio": ["audio.tts.public", "audio.tts.personal"],
    "script": [
        "script.write.spoken", "script.write.story", "script.write.recommend",
        "script.breakdown.scenes", "script.breakdown.reverse", "script.breakdown.local_image", "script.breakdown.local_video",
        "script.output.video.story", "script.output.video.spoken", "script.output.video.recommend",
        "script.output.remake.cinematic", "script.output.remake.grok", "script.output.remake.micro",
        "script.output.image", "script.output.handoff",
    ],
    "text-video": ["text_video.topic", "text_video.fixed"],
    "matrix-template": ["matrix_template.single", "matrix_template.batch"],
    "short-drama": [
        "short_drama.live_action.script_planning", "short_drama.live_action.character_reference",
        "short_drama.live_action.shot_video", "short_drama.live_action.preview",
        "short_drama.live_action.delivery", "short_drama.autodraft.review",
    ],
    "canvas": [
        "canvas.agent.plan", "canvas.image.banana.nb2", "canvas.image.banana.pro", "canvas.image.openai",
        "canvas.image.zelong", "canvas.video.grok", "canvas.video.micro", "canvas.short_drama.node",
        "canvas.prompt.reverse", "canvas.local.edit",
    ],
    "assets": ["assets.audio.clone_vip", "assets.library.read", "assets.library.manage", "assets.voice.slot"],
    "pricing": ["pricing.catalog"], "invite": ["invite.dashboard", "invite.poster"],
    "tutorials": ["tutorials.playback"],
    "settings": ["settings.profile", "settings.preferences", "settings.friends", "settings.points", "settings.security"],
}
_SITE_NAVIGATION = {
    "inspiration": "inspiration", "leads": "leads", "collect": "collect", "banana": "image",
    "video": "video", "audio": "audio", "script": "script", "text-video": "text-video",
    "matrix-template": "matrix-template", "short-drama": "short-drama", "canvas": "canvas",
    "assets": "assets-page", "pricing": "pricing-page", "invite": "invite",
    "tutorials": "tutorials", "settings": "settings",
}
for _site_page, _site_operations in _SITE_OPERATIONS.items():
    CAPABILITIES[_SITE_NAVIGATION[_site_page]]["website_operations"] = list(_site_operations)


_AGENT_RESOURCES = {
    "ip12-": "ip12_project", "digital-ip-project": "digital_ip_project",
    "short-drama-": "short_drama_project", "video-compose-": "video_compose_project",
    "digital-presenter-": "digital_presenter", "canvas-": "canvas",
    "image-upload": "asset", "video-upload": "asset", "audio-upload": "asset", "asset": "asset", "assets": "asset",
    "task": "task", "voices": "voice",
    "audio-slots": "voice", "voice-clone-": "voice", "leads-crm": "lead",
    "inspiration-": "inspiration", "text-video-": "text_video",
}
_AGENT_RESOURCE_OVERRIDES = {
    "digital-ip-projects": "digital_ip_project",
    "digital-ip-project": "digital_ip_project",
    "digital-ip-report": "digital_ip_project",
    "digital-ip-create": "digital_ip_project",
    "digital-ip-update": "digital_ip_project",
    "digital-ip-delete": "digital_ip_project",
    "leads-crm": "lead",
    "leads-crm-upsert": "lead",
    "leads-delete": "lead",
}
_AGENT_OPERATIONS = {
    "ip12-projects": "list", "ip12-project": "get", "ip12-report": "get",
    "ip12-create": "create", "ip12-message": "update", "ip12-delete": "delete",
    "digital-ip-projects": "list", "digital-ip-project": "get", "digital-ip-report": "get",
    "short-drama-projects": "list", "short-drama-project": "get",
    "short-drama-conversation": "get", "short-drama-preflight": "get",
    "canvas-list": "list", "canvas-get": "get", "canvas-create": "create", "canvas-ops": "update",
    "tasks": "list", "task": "get", "assets": "list", "voices": "list",
    "asset-delete": "delete",
    "canvas-delete": "delete", "video-compose-delete": "delete",
    "digital-presenter-delete": "delete", "short-drama-create": "create",
    "short-drama-delete": "delete", "leads-delete": "delete",
    "digital-ip-create": "create", "digital-ip-update": "update", "digital-ip-delete": "delete",
    "voice-clone-create": "create", "voice-clone-status": "get",
    "video-avatars": "list", "audio-slots": "list", "leads-crm": "list",
    "leads-crm-upsert": "update", "inspiration-catalog": "list",
    "inspiration-likes": "list", "inspiration-like": "update",
}
_STANDARD_CRUD = ("list", "get", "create", "update", "delete")


def _agent_resource(identifier):
    if identifier in _AGENT_RESOURCE_OVERRIDES:
        return _AGENT_RESOURCE_OVERRIDES[identifier]
    for prefix, resource in _AGENT_RESOURCES.items():
        if identifier == prefix or identifier.startswith(prefix):
            return resource
    return identifier.split("-", 1)[0].replace("_", "-")


def _agent_operation(capability):
    identifier = capability["id"]
    if identifier in _AGENT_OPERATIONS:
        return _AGENT_OPERATIONS[identifier]
    if capability["kind"] == "navigation":
        return "navigate"
    if capability["kind"] == "upload":
        return "create"
    if capability["side_effect"] == "paid":
        return "execute"
    if capability["side_effect"] == "read":
        return "list" if identifier.endswith(("s", "-catalog", "-templates", "-styles")) else "get"
    if identifier.endswith(("-delete", "-remove")):
        return "delete"
    if identifier.endswith(("-create", "-import", "-plan")):
        return "create"
    if identifier.endswith(("-render", "-generate")):
        return "execute"
    return "update"


def _agent_input_source(name):
    if name == "job_id":
        return "从异步创建能力的 result.job_id 复制；之后只调用 task，不重新提交创建。"
    if name in {"project_id", "board_id", "source_asset_id", "video_asset_id", "audio_asset_id"}:
        return "先调用同资源的 list/get 能力，从本人可访问结果复制 ID。"
    if name.endswith("upload_id") or name.endswith("upload_ids"):
        family = {
            "audio_upload_id": "audio",
            "clothes_upload_id": "image",
            "image_upload_id": "image",
            "person_image_upload_id": "image",
            "person_video_upload_id": "video",
            "reference_video_upload_ids": "video",
        }.get(name)
        if family is None:
            family = "video" if "video" in name else "image"
        return "先调用 %s-upload，使用其本人私有 upload_id。" % family
    if name in {"avatar_id", "avatar_ids", "avatars"}:
        return "先调用 video-avatars，从 ready 形象复制 avatar_id。"
    if name == "slot_id":
        return "先调用 audio-slots，从当前账号可用槽位复制 slot_id。"
    if name in {"voice", "voice_key"}:
        return "先调用 voices，从可试听且 ready 的声音复制 voice_key。"
    if name in {"revision", "expected_revision", "expected_version"}:
        return "写入前重新读取目标，使用最新 revision/version；冲突后不得覆盖。"
    if name == "request_id":
        return "为新操作生成唯一 ID；响应不确定时必须复用同一 ID。"
    return "由用户明确提供，并按 input_schema 校验。"


def _required_input_names(schema):
    names = []
    if isinstance(schema, dict):
        for name in schema.get("required") or []:
            if name not in names:
                names.append(name)
        for key in ("oneOf", "anyOf", "allOf"):
            for child in schema.get(key) or []:
                for name in _required_input_names(child):
                    if name not in names:
                        names.append(name)
    return names


def _attach_agent_guidance():
    resource_operations = {}
    for capability in CAPABILITIES.values():
        resource = _agent_resource(capability["id"])
        operation = _agent_operation(capability)
        resource_operations.setdefault(resource, {}).setdefault(operation, []).append(capability["id"])
        capability["agent"] = {"resource": resource, "operation": operation}
    for capability in CAPABILITIES.values():
        agent = capability["agent"]
        operation = agent["operation"]
        required = _required_input_names(capability["input_schema"])
        preconditions = []
        if capability["requires_auth"]:
            preconditions.append("先运行 hq status；未授权时运行 hq login。")
        if capability.get("required_scope"):
            preconditions.append("授权必须包含 %s。" % capability["required_scope"])
        if capability["confirmation_required"]:
            preconditions.append("执行外部写入前必须显式传 --confirm。")
        if capability["side_effect"] == "paid":
            workflow = [
                "先用相同输入且不带 --confirm 获取服务器报价。",
                "向用户展示 cost、points 与具体扣点，得到明确同意。",
                "仅一次复用完全相同输入并传 --confirm --quote-token。",
                "拿到 job_id 后只调用 task 直到终态，并验证可用成品与账务。",
            ]
        else:
            workflow = ["运行 hq describe %s --json。" % capability["id"]]
            workflow.append("按 input_schema 运行一次；不要添加未声明字段。")
            if capability["confirmation_required"]:
                workflow.append("在执行写入前向用户说明影响并传 --confirm。")
        recovery = ["失败后先读取目标的最新状态，不盲目重复写入。"]
        if capability["side_effect"] == "paid":
            recovery = ["响应不确定时只查询原 job_id/request_id；禁止重新提交付费创建。"]
        elif operation in {"update", "delete"}:
            recovery.append("冲突或超时后重新 list/get；确认状态未变化前不要重试。")
        agent.update({
            "when_to_use": capability["description"],
            "preconditions": preconditions,
            "required_inputs": {name: _agent_input_source(name) for name in required},
            "workflow": workflow,
            "success_evidence": [
                "退出码为 0，且 JSON schema、capability 与目标一致。",
                "结果包含可复查的资源 ID、revision、job_id 或最终成品。",
            ],
            "recovery": recovery,
            "website_access": "navigate" if capability["kind"] == "navigation" else "direct",
            "website_operations": list(capability.get("website_operations") or []),
            "resource_operations": resource_operations[agent["resource"]],
            "missing_crud": [name for name in _STANDARD_CRUD if name not in resource_operations[agent["resource"]]],
        })


_attach_agent_guidance()

CAPABILITIES["voice-clone-create"]["agent"]["workflow"].append(
    "提交成功后只调用 voice-clone-status 查询原 slot_id；不要重复创建。"
)
CAPABILITIES["voice-clone-create"]["agent"]["recovery"].append(
    "若状态为 failed 且提示有效语音太短，上传新的30至60秒连续清晰单人语音，再用新的 audio_upload_id 发起新操作。"
)
CAPABILITIES["matrix-template-batch-generate"]["agent"]["workflow"][-1] = (
    "保存返回的全部 job_ids，之后只调用 task 查询这些原任务直到终态，并逐条验证成品与账务。"
)
CAPABILITIES["matrix-template-batch-generate"]["agent"]["recovery"] = [
    "部分成功或响应不确定时，先保留错误详情中的 jobs/job_ids；不要创建新批次。",
    "仅当返回 batch_result_pending 并明确要求恢复时，才用完全相同输入、原 quote_token 和 --confirm 重放一次。",
]
CAPABILITIES["leads-delete"]["agent"]["workflow"].insert(
    1, "先调用 leads-crm 读取并核对要永久删除的本人线索，再传这些 lead_ids 和 --confirm。"
)





def capability_list():
    return list(CAPABILITIES.values())


def resolve_url(capability, environment, payload):
    url = ENVIRONMENTS[environment] + capability["deep_link"]["path"]
    if payload:
        url += "?" + urlencode(payload)
    return url
