"""Pure shadow decision contract for the IP12 master Agent trial."""

import re


SCHEMA = "ip12.master-decision/v1"
MODE = "shadow"
MASTER_AGENT_ID = "ip12_master_agent"
HISTORY_LIMIT = 50
_CONTINUE_RE = re.compile(r"^(?:嗯|哦|好|好的|可以|行|ok|继续|然后呢|下一步|接下来呢|怎么办)[的啊呀吧嘛呢，。!！?？\s]*$", re.I)
_STATUS_RE = re.compile(
    r"(?:现在|当前)?(?:做到哪|进行到哪|到什么阶段|进度|进展如何|状态|什么情况|完成了吗|怎么样了)"
    r"|(?:能|可以|是否).{0,6}(?:看到|读取|知道).{0,8}(?:音色|声音|形象|素材|作品|任务)"
)


def _latest_production(project):
    records = [record for record in (project.get("productions") or {}).values()
               if isinstance(record, dict)]
    return records[-1] if records else None


def _active_context(project):
    record = _latest_production(project) or {}
    specialist = record.get("specialist_agent") if isinstance(record.get("specialist_agent"), dict) else {}
    return {
        "production_id": str(record.get("id") or ""),
        "production_status": str(record.get("status") or ""),
        "delegation_id": str(specialist.get("delegation_id") or ""),
        "specialist_agent_id": str(specialist.get("agent_id") or ""),
    }


def _decision(kind, execution_route, next_action, *, delegate_to="", awaiting="", reasons=None, context=None):
    return {
        "schema": SCHEMA,
        "mode": MODE,
        "master_agent_id": MASTER_AGENT_ID,
        "decision": kind,
        "execution_route": execution_route,
        "delegate_to": delegate_to or None,
        "awaiting": awaiting or None,
        "next_action": next_action,
        "reason_codes": list(reasons or []),
        "context": context or {},
    }


def decide(project, state, user_message, signals):
    """Return one bounded decision without mutating Project or calling tools."""
    project = project if isinstance(project, dict) else {}
    state = state if isinstance(state, dict) else {}
    signals = signals if isinstance(signals, dict) else {}
    route = str(signals.get("legacy_route") or "model_turn")
    action_type = str(signals.get("action_type") or "")
    production_action = str(signals.get("production_action") or "")
    context = _active_context(project)
    message = re.sub(r"\s+", "", str(user_message or ""))

    if context["production_id"] and (
        _CONTINUE_RE.fullmatch(message) or _STATUS_RE.search(message)
    ):
        status = context["production_status"]
        if status == "quoted":
            return _decision("await_confirmation", "master_resume", "展示原报价并等待确认或修改",
                             awaiting="quote_confirmation", reasons=["active_quote"], context=context)
        if status in {"submitting", "queued", "running"}:
            return _decision("resume_task", "master_resume", "查询原任务并继续等待",
                             reasons=["active_async_task"], context=context)
        if status == "done":
            return _decision("request_feedback", "master_resume", "展示原成品并询问修改意见",
                             awaiting="revision_feedback", reasons=["delivered_work"], context=context)
        if status in {"draft", "blocked_prerequisite", "stale"}:
            return _decision("resume_delegation", "master_resume", "继续补齐当前作品的下一项",
                             delegate_to=context["specialist_agent_id"], reasons=["active_delegation"], context=context)

    if route == "action_turn":
        return _decision("execute_confirmed_action", route, "执行当前已确认的 IP12 动作",
                         reasons=[action_type or "harness_action"], context=context)
    if route == "production_turn":
        delegate = "talking_head_video_agent" if production_action == "digital-ip-text-generate" else ""
        return _decision("delegate", route, "由专业 Agent 规划并返回一个下一步",
                         delegate_to=delegate, reasons=[production_action or "production_intent"], context=context)
    if route == "content_revision_turn":
        return _decision("revise_content", route, "保留原版本并生成一个新版本",
                         reasons=["explicit_revision"], context=context)
    if route == "foundation_revision_turn":
        return _decision("revise_foundation", route, "按批注更新报告草稿",
                         reasons=["foundation_review"], context=context)

    completed = set(state.get("completed_modules") or [])
    if len(completed.intersection(range(1, 7))) < 6:
        return _decision("continue_ip12", route, "每轮只推进当前模块的一个关键缺口",
                         reasons=["modules_incomplete"], context=context)
    return _decision("answer_or_clarify", route, "回答当前问题，必要时只追问一个缺口",
                     reasons=["no_execution_intent"], context=context)


def record_shadow(project, decision, legacy_route, request_id, revision, recorded_at):
    """Persist a redacted comparison read model; never store user text."""
    shadow = project.get("master_agent_shadow") if isinstance(project.get("master_agent_shadow"), dict) else {}
    metrics = shadow.get("metrics") if isinstance(shadow.get("metrics"), dict) else {}
    aligned = decision.get("execution_route") == legacy_route
    event = {
        "request_id": str(request_id or "")[:80],
        "project_revision": int(revision or 0),
        "decision": decision.get("decision"),
        "execution_route": decision.get("execution_route"),
        "legacy_route": legacy_route,
        "delegate_to": decision.get("delegate_to"),
        "awaiting": decision.get("awaiting"),
        "next_action": decision.get("next_action"),
        "reason_codes": list(decision.get("reason_codes") or []),
        "aligned": aligned,
        "recorded_at": str(recorded_at),
    }
    history = list(shadow.get("history") or [])[-(HISTORY_LIMIT - 1):] + [event]
    total = int(metrics.get("total") or 0) + 1
    aligned_count = int(metrics.get("aligned") or 0) + int(aligned)
    project["master_agent_shadow"] = {
        "schema": SCHEMA,
        "mode": MODE,
        "latest": event,
        "metrics": {"total": total, "aligned": aligned_count, "mismatched": total - aligned_count},
        "history": history,
    }
    return event
