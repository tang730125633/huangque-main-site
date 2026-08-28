"""Public, provider-neutral contract for Director CLI workflow coverage.

The contract is intentionally descriptive in the first rollout.  It gives the
web app, the public CLI and CI one authoritative inventory without exposing
provider URLs, credentials, or pretending that planned actions are runnable.
Executable actions remain registered by :mod:`hq_cli_api` only when their
server-owned orchestration is implemented.
"""

import copy


CONTRACT_VERSION = "director-workflow-contract-v1"

WORKFLOW_STATES = (
    "draft",
    "quoted",
    "confirmed",
    "queued",
    "running",
    "needs_attention",
    "recoverable",
    "completed",
    "failed",
    "refund_pending",
    "refunded",
    "abandoned",
)

TERMINAL_STATES = ("completed", "failed", "refunded", "abandoned")

STATE_TRANSITIONS = {
    "draft": ("quoted", "abandoned"),
    "quoted": ("confirmed", "draft", "abandoned"),
    "confirmed": ("queued", "recoverable", "failed", "refund_pending"),
    "queued": ("running", "needs_attention", "recoverable", "failed", "refund_pending"),
    "running": ("needs_attention", "recoverable", "completed", "failed", "refund_pending"),
    "needs_attention": ("queued", "running", "recoverable", "failed", "abandoned"),
    "recoverable": ("queued", "running", "completed", "failed", "refund_pending", "abandoned"),
    "refund_pending": ("refunded",),
    "completed": (),
    "failed": (),
    "refunded": (),
    "abandoned": (),
}

SCOPE_CONTRACT = {
    "director:read": "读取本人编导能力、工作流、结果和资产",
    "director:write": "经确认后保存本人脚本、分镜与编导方案",
    "director:generate": "经报价和二次确认后提交本人编导付费生成",
    "director:recover": "恢复本人可恢复的编导任务，不创建重复付费任务",
    "digital-human-oneclick:read": "读取本人数字人一键生成方案、运行状态与历史",
    "digital-human-oneclick:write": "经确认后上传本人素材并保存授权与方案",
    "digital-human-oneclick:generate": "经报价和二次确认后运行本人数字人一键生成",
}


def _action(identifier, group, scope, operations, endpoints, *, billing="free",
            availability="planned", transport="action", description=""):
    return {
        "id": identifier,
        "group": group,
        "description": description,
        "required_scope": scope,
        "website_operations": list(operations),
        "server_endpoints": list(endpoints),
        "billing": billing,
        "confirmation_required": billing == "quote_then_confirm" or scope.endswith(
            (":write", ":recover")
        ),
        "idempotency_required": billing == "quote_then_confirm" or scope.endswith(
            (":write", ":recover")
        ),
        "availability": availability,
        "transport": transport,
    }


DIRECTOR_ACTIONS = (
    _action(
        "director-capability", "read", "director:read", (),
        ("CLI:/api/auth/cli/action",), availability="available",
        description="读取编导与数字人一键生成的完整 CLI 覆盖契约。",
    ),
    _action(
        "director-workflows", "read", "director:read", (),
        ("GET:/api/gen/director/workflows",),
        description="列出本人编导工作流。",
    ),
    _action(
        "director-workflow", "read", "director:read", (),
        ("GET:/api/gen/director/workflows/{workflow_id}",),
        description="读取一个本人编导工作流及当前 revision。",
    ),
    _action(
        "director-script-generate", "script", "director:write",
        ("script.write.spoken", "script.write.story", "script.write.recommend"),
        ("POST:/api/gen/copy",),
        description="生成可编辑的结构化脚本与分镜。",
    ),
    _action(
        "director-storyboard-update", "storyboard", "director:write", (),
        ("PUT:/api/gen/director/workflows/{workflow_id}/storyboard",),
        description="按 revision 保存结构化分镜，冲突时拒绝覆盖。",
    ),
    _action(
        "director-storyboard-export", "storyboard", "director:read", (),
        ("GET:/api/gen/director/workflows/{workflow_id}/storyboard/export",),
        description="导出本人结构化分镜。",
    ),
    _action(
        "director-breakdown-upload", "breakdown", "director:write",
        ("script.breakdown.local_image", "script.breakdown.local_video"),
        ("POST:/api/gen/breakdown/local-upload?media_type=image",
         "POST:/api/gen/breakdown/local-upload?media_type=video"),
        transport="dedicated_upload",
        description="上传本人本地图片或视频用于提示词反推。",
    ),
    _action(
        "director-breakdown", "breakdown", "director:write",
        ("script.breakdown.scenes", "script.breakdown.reverse"),
        ("POST:/api/gen/breakdown",),
        description="按 scenes 或 reverse_prompt 模式拆解链接或已上传素材。",
    ),
    _action(
        "director-scene-image-generate", "scene", "director:generate",
        ("script.output.image",), ("POST:/api/gen/image",),
        billing="quote_then_confirm",
        description="根据冻结的分镜生成单镜头图片。",
    ),
    _action(
        "director-scene-video-generate", "scene", "director:generate",
        ("script.output.video.story",), ("POST:/api/gen/xiaole_video",),
        billing="quote_then_confirm",
        description="根据冻结的分镜生成单镜头剧情视频。",
    ),
    _action(
        "director-scene-talking-generate", "scene", "director:generate",
        ("script.output.video.spoken", "script.output.video.recommend"),
        ("POST:/api/gen/script_to_video",), billing="quote_then_confirm",
        description="根据冻结的口播分镜生成人物口播镜头。",
    ),
    _action(
        "director-production-plan", "production", "director:read", (),
        ("POST:/api/gen/director/workflows/{workflow_id}/production/plan",),
        description="冻结输入并返回服务端权威生产方案与报价摘要。",
    ),
    _action(
        "director-production-start", "production", "director:generate",
        ("script.output.handoff",),
        ("POST:/api/gen/director/workflows/{workflow_id}/production/start",),
        billing="quote_then_confirm",
        description="用 quote_token、plan_digest 和 request_id 启动完整生产。",
    ),
    _action(
        "director-production-status", "production", "director:read", (),
        ("GET:/api/gen/director/workflows/{workflow_id}/production",),
        description="读取生产状态、子任务和账务状态。",
    ),
    _action(
        "director-production-recover", "production", "director:recover", (),
        ("POST:/api/gen/director/workflows/{workflow_id}/production/recover",),
        description="只恢复原 request_id 的可恢复生产，不重复扣点。",
    ),
    _action(
        "director-remake-plan", "remake", "director:read", (),
        ("POST:/api/gen/director/workflows/{workflow_id}/remake/plan",),
        description="根据反推结果冻结同款复刻方案。",
    ),
    _action(
        "director-remake-start", "remake", "director:generate",
        ("script.output.remake.cinematic", "script.output.remake.grok",
         "script.output.remake.micro"),
        ("POST:/api/gen/cinematic", "POST:/api/gen/xiaole_video"),
        billing="quote_then_confirm",
        description="启动电影化身、果肉或 Seedance 同款复刻。",
    ),
    _action(
        "director-remake-status", "remake", "director:read", (),
        ("GET:/api/gen/director/workflows/{workflow_id}/remake",),
        description="读取同款复刻状态与成品。",
    ),
    _action(
        "director-remake-recover", "remake", "director:recover", (),
        ("POST:/api/gen/director/workflows/{workflow_id}/remake/recover",),
        description="恢复原同款复刻任务。",
    ),
)


DIGITAL_HUMAN_ONECLICK_ACTIONS = (
    _action(
        "digital-human-oneclick-capability", "oneclick", "digital-human-oneclick:read", (),
        ("GET:/api/gen/digital-human-v2/capability",),
        description="读取数字人一键生成能力与限制。",
    ),
    _action(
        "digital-human-oneclick-plan", "oneclick", "digital-human-oneclick:read", (),
        ("POST:/api/gen/digital-human-v2/plan",),
        description="按文案或完整录音生成冻结时间轴方案。",
    ),
    _action(
        "digital-human-oneclick-consent", "oneclick", "digital-human-oneclick:write", (),
        ("POST:/api/gen/digital-human-v2/consent",),
        description="保存与 plan_digest 绑定的照片、声音和 AI 素材授权。",
    ),
    _action(
        "digital-human-oneclick-audio-upload", "oneclick", "digital-human-oneclick:write", (),
        ("POST:/api/gen/digital-human-v2/audio-upload",), transport="dedicated_upload",
        description="上传本人完整口播录音并生成安全切片。",
    ),
    _action(
        "digital-human-oneclick-material-upload", "oneclick", "digital-human-oneclick:write", (),
        ("POST:/api/gen/script_to_video/material-upload",), transport="dedicated_upload",
        description="上传本人顾客素材并绑定运行。",
    ),
    _action(
        "digital-human-oneclick-start", "oneclick", "digital-human-oneclick:generate", (),
        ("POST:/api/gen/digital-human-v2/runs",), billing="quote_then_confirm",
        description="由服务端编排声音、主画面、数字人、合成与归档。",
    ),
    _action(
        "digital-human-oneclick-status", "oneclick", "digital-human-oneclick:read", (),
        ("GET:/api/gen/digital-human-v2/runs/{run_id}",),
        description="读取完整运行、子任务、失败原因和账务状态。",
    ),
    _action(
        "digital-human-oneclick-recover", "oneclick", "digital-human-oneclick:write", (),
        ("POST:/api/gen/digital-human-v2/runs/{run_id}/recover",),
        description="恢复原运行的可恢复步骤，不创建重复付费任务。",
    ),
    _action(
        "digital-human-oneclick-abandon", "oneclick", "digital-human-oneclick:write", (),
        ("POST:/api/gen/digital-human-v2/runs/{run_id}/abandon",),
        description="放弃未完成运行；已扣点步骤仍按账务终态处理。",
    ),
    _action(
        "digital-human-oneclick-history", "oneclick", "digital-human-oneclick:read", (),
        ("GET:/api/gen/digital-human-v2/history",),
        description="读取本人数字人一键生成历史。",
    ),
    _action(
        "digital-human-oneclick-material-resolve", "oneclick", "digital-human-oneclick:write", (),
        ("POST:/api/gen/digital-human-v2/material-resolve",),
        description="按冻结方案解析本人素材或允许的 AI 补图来源。",
    ),
)


ALL_ACTIONS = DIRECTOR_ACTIONS + DIGITAL_HUMAN_ONECLICK_ACTIONS

WORKFLOW_INVARIANTS = (
    "浏览器和 CLI 必须调用同一个服务端编排器；浏览器 JavaScript 不得成为权威工作流。",
    "付费动作必须先报价，再以相同输入、quote_token、plan_digest 和唯一 request_id 确认一次。",
    "quote_token 必须绑定账号、规范化输入、冻结方案、价格和过期时间。",
    "响应不确定或任务失败时只能按原 workflow_id/run_id/request_id 查询或恢复，禁止重复提交。",
    "所有读写必须校验当前账号所有权；返回结果不得暴露上游凭据或私有内部地址。",
    "扣点、任务绑定、退款等待和退款完成必须有持久账本并可在重启后恢复。",
)


def capability_document():
    """Return a copy-safe response for authenticated discovery callers."""
    actions = copy.deepcopy(ALL_ACTIONS)
    counts = {
        "available": sum(item["availability"] == "available" for item in actions),
        "planned": sum(item["availability"] == "planned" for item in actions),
        "total": len(actions),
    }
    return {
        "contract_version": CONTRACT_VERSION,
        "workflow_states": list(WORKFLOW_STATES),
        "terminal_states": list(TERMINAL_STATES),
        "state_transitions": {key: list(value) for key, value in STATE_TRANSITIONS.items()},
        "scopes": copy.deepcopy(SCOPE_CONTRACT),
        "actions": actions,
        "counts": counts,
        "invariants": list(WORKFLOW_INVARIANTS),
    }
