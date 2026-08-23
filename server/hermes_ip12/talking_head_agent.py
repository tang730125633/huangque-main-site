"""Bounded specialist contract for the first IP12 talking-head work."""

import json


AGENT_ID = "talking_head_video_agent"
AGENT_NAME = "口播短视频 Agent"
CONTRACT_VERSION = "1.0.0"
ORCHESTRATOR_ID = "ip12_master_agent"
SUPPORTED_ACTIONS = {"digital-ip-text-generate", "digital-ip-audio-generate"}

_STAGES = {
    "blocked_prerequisite": "collecting_materials",
    "draft": "awaiting_quote",
    "stale": "replanning",
    "quoted": "awaiting_confirmation",
    "submitting": "submitting_once",
    "queued": "generating",
    "running": "generating",
    "done": "delivered",
    "failed": "failed",
    "refund_pending": "failed",
    "refunded": "failed",
}
_NEXT_ACTIONS = {
    "collecting_materials": "补齐形象与声音素材",
    "awaiting_quote": "获取实时报价",
    "replanning": "按最新文案重新规划并报价",
    "awaiting_confirmation": "等待用户确认报价",
    "submitting_once": "恢复同一条确认请求",
    "generating": "查询原任务并等待成品",
    "delivered": "请用户试听试看并说明修改意见",
    "failed": "说明失败与退款状态后决定是否重试",
}


def supports(action):
    return str(action or "") in SUPPORTED_ACTIONS


def capability_gate(state, source=None):
    state = state if isinstance(state, dict) else {}
    completed = set(state.get("completed_modules") or [])
    missing = ["模块 %s" % module for module in range(1, 7) if module not in completed]
    if (state.get("foundation_report") or {}).get("status") != "confirmed":
        missing.append("确认模块 1–4 报告")
    script = str((source or {}).get("script") or "").strip()
    if not script:
        missing.append("确认一篇口播文案")
    elif len(script) > 1000:
        missing.append("将口播文案控制在 1000 字以内")
    return {
        "id": "talking-head-first-work",
        "status": "locked" if missing else "unlocked",
        "missing": missing,
    }


def _profile_text(state):
    profile = (state or {}).get("ip_profile") or {}
    return json.dumps(profile, ensure_ascii=False, sort_keys=True)[:12000]


def plan(state, source, action="digital-ip-text-generate", user_options=None):
    if not supports(action):
        raise ValueError("口播短视频 Agent 不支持该能力")
    gate = capability_gate(state, source)
    if gate["status"] != "unlocked":
        return {"ok": False, "agent_id": AGENT_ID, "gate": gate}
    profile_text = _profile_text(state)
    calm = any(token in profile_text for token in (
        "温和", "耐心", "亲切", "陪伴", "去焦虑", "专业但不生硬",
    ))
    defaults = {
        "ratio": "9:16",
        "motion": "low" if calm else "medium",
        "subtitle": True,
        "subtitle_style": "white",
        "subtitle_position": "lower",
    }
    defaults.update(user_options or {})
    return {
        "ok": True,
        "agent_id": AGENT_ID,
        "agent_name": AGENT_NAME,
        "contract_version": CONTRACT_VERSION,
        "orchestrator_id": ORCHESTRATOR_ID,
        "objective": "把第一篇已确认口播文案制作成可信、自然、可发布的短视频",
        "gate": gate,
        "capability": action,
        "recommended_options": defaults,
        "option_schema": {
            "type": "object",
            "properties": {
                "ratio": {"type": "string"},
                "motion": {"type": "string"},
                "subtitle": {"type": "boolean"},
                "subtitle_style": {"type": "string"},
                "subtitle_position": {"type": "string"},
            },
        },
        "brief": {
            "reason": "由口播短视频 Agent 基于已确认定位、人设、价值主张和当前文案规划",
            "audience": "当前 IP 已确认的目标受众",
            "goal": "完成第一件可试听、可观看、可继续修改的数字人口播作品",
            "delivery_style": "温和自然、信息清楚、不过度表演" if calm else "自然可信、重点清楚、节奏稳定",
        },
        "quality_bar": [
            "形象和声音均来自当前账号已确认素材",
            "画面、声音和字幕能够正常播放",
            "文案与当前确认版本一致",
            "生成任务只在报价确认后提交一次",
            "成品、任务号和修改意见写回当前 Project",
        ],
    }


def new_delegation(production_id, specialist_plan):
    return {
        "delegation_id": "delegate_" + str(production_id).removeprefix("prod_"),
        "agent_id": AGENT_ID,
        "agent_name": AGENT_NAME,
        "contract_version": CONTRACT_VERSION,
        "orchestrator_id": ORCHESTRATOR_ID,
        "objective": specialist_plan["objective"],
        "stage": "collecting_materials",
        "status": "active",
        "next_action": _NEXT_ACTIONS["collecting_materials"],
        "quality_bar": list(specialist_plan.get("quality_bar") or []),
    }


def sync_production(record):
    specialist = record.get("specialist_agent") if isinstance(record, dict) else None
    if not isinstance(specialist, dict) or specialist.get("agent_id") != AGENT_ID:
        return False
    stage = _STAGES.get(str(record.get("status") or ""), "planning")
    status = (
        "completed" if stage == "delivered"
        else "failed" if stage == "failed"
        else "waiting_user" if stage in {"collecting_materials", "awaiting_confirmation"}
        else "running"
    )
    changed = any((
        specialist.get("stage") != stage,
        specialist.get("status") != status,
        specialist.get("next_action") != _NEXT_ACTIONS.get(stage, "继续规划当前作品"),
    ))
    specialist.update(
        stage=stage,
        status=status,
        next_action=_NEXT_ACTIONS.get(stage, "继续规划当前作品"),
    )
    return changed


def sync_project(conversation):
    records = [
        record for record in (conversation.get("productions") or {}).values()
        if isinstance(record, dict)
        and isinstance(record.get("specialist_agent"), dict)
        and record["specialist_agent"].get("agent_id") == AGENT_ID
    ]
    if not records:
        return False
    for record in records:
        sync_production(record)
    latest = records[-1]
    specialist = latest["specialist_agent"]
    terminal = specialist["status"] in {"completed", "failed"}
    runtime = {
        "orchestrator_id": ORCHESTRATOR_ID,
        "active_delegation_id": None if terminal else specialist["delegation_id"],
        "last_delegation_id": specialist["delegation_id"],
        "specialist_agent_id": AGENT_ID,
        "phase": specialist["stage"],
        "next_action": specialist["next_action"],
    }
    changed = conversation.get("agent_runtime") != runtime
    conversation["agent_runtime"] = runtime
    return changed
