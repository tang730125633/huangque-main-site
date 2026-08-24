"""Account-scoped read model for the IP12 master Agent."""

import copy
import re


SCHEMA = "ip12.project-memory/v1"
HISTORY_LIMIT = 50
PREFERENCE_KEYS = {"communication_style", "response_length", "tone", "interaction_preference"}
ACTIVE_PRODUCTION_STATUSES = {"blocked_prerequisite", "draft", "quoted", "submitting", "queued", "running", "refund_pending"}


def _text(value, limit):
    return str(value or "").strip()[:limit]


def _fact_map(values):
    result = {}
    for key, item in (values or {}).items():
        item = item if isinstance(item, dict) else {"value": item}
        value = item.get("value")
        if value in (None, "", [], {}):
            continue
        result[str(key)] = {
            "value": copy.deepcopy(value) if not isinstance(value, str) else _text(value, 1200),
            "evidence": _text(item.get("evidence_quote"), 500),
        }
    return result


def _content_topics(project):
    pack = ((project.get("deliverables") or {}).get("6") or {})
    result = []
    for category in pack.get("categories") or []:
        for topic in category.get("topics") or []:
            versions = topic.get("versions") or []
            current = versions[-1] if versions else {}
            result.append({
                "category_id": _text(category.get("id"), 120),
                "topic_id": _text(topic.get("id"), 120),
                "category": _text(category.get("name"), 160),
                "title": _text(topic.get("title"), 240),
                "version": int(current.get("version") or 0),
                "status": _text(topic.get("status"), 60),
                "excerpt": _text(current.get("content"), 420),
            })
    return result


def _content_target(project):
    target = project.get("active_content_target") if isinstance(project.get("active_content_target"), dict) else {}
    return {
        "category_id": _text(target.get("category_id"), 120),
        "topic_id": _text(target.get("topic_id"), 120),
    }


def resolve_content_reference(memory, message):
    """Resolve stable object references; never infer a target from insertion order."""
    topics = memory.get("content_topics") or []
    compact = "".join(str(message or "").lower().split())
    title_matches = [
        item for item in topics
        if item.get("title") and "".join(str(item["title"]).lower().split()) in compact
    ]
    if len(title_matches) == 1:
        return {key: title_matches[0][key] for key in ("category_id", "topic_id")}
    ordinal = re.search(r"第([一二三123])篇", compact)
    if ordinal:
        index = {"一": 1, "二": 2, "三": 3}.get(ordinal.group(1), int(ordinal.group(1)) if ordinal.group(1).isdigit() else 0)
        if 0 < index <= len(topics):
            return {key: topics[index - 1][key] for key in ("category_id", "topic_id")}
    active = memory.get("active_content_target") or {}
    if active.get("category_id") and active.get("topic_id") and re.search(r"(?:这个|这篇|该篇|刚才那个)", compact):
        return {"category_id": active["category_id"], "topic_id": active["topic_id"]}
    if len(topics) == 1:
        return {key: topics[0][key] for key in ("category_id", "topic_id")}
    return None


def _confirmed_outputs(profile):
    result = []
    for key, item in sorted((profile.get("confirmed_outputs") or {}).items()):
        if not isinstance(item, dict):
            continue
        result.append({
            "id": str(key),
            "module": int(item.get("module") or 0),
            "step": int(item.get("step") or 0),
            "title": _text(item.get("title"), 200),
            "excerpt": _text(item.get("content"), 260),
        })
    return result[-18:]


def _productions(project):
    result = []
    for record in (project.get("productions") or {}).values():
        if not isinstance(record, dict):
            continue
        specialist = record.get("specialist_agent") if isinstance(record.get("specialist_agent"), dict) else {}
        quote = record.get("quote") if isinstance(record.get("quote"), dict) else {}
        result.append({
            "production_id": _text(record.get("id"), 100),
            "action": _text(record.get("action"), 100),
            "family": _text(record.get("capability_family"), 40),
            "status": _text(record.get("status"), 60),
            "created_at": _text(record.get("created_at"), 40),
            "updated_at": _text(record.get("updated_at"), 40),
            "job_present": bool(record.get("job_id")),
            "confirmation_present": bool(record.get("confirmation_id")),
            "selected_fields": sorted(str(key) for key, value in (record.get("options") or {}).items()
                                      if value not in (None, "", [], {})),
            "quote": {
                "cost": quote.get("cost"),
                "points": quote.get("points"),
                "expires_at": quote.get("expires_at"),
            },
            "specialist": {
                "agent_id": _text(specialist.get("agent_id"), 100),
                "stage": _text(specialist.get("stage"), 60),
                "next_action": _text(specialist.get("next_action"), 200),
            },
        })
    return result[-8:]


def _recent_messages(project):
    result = []
    for item in (project.get("messages") or [])[-14:]:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        content = _text(item.get("content"), 1000)
        if content:
            result.append({"role": item["role"], "content": content})
    return result


def _pending(state):
    pending = state.get("pending") if isinstance(state.get("pending"), dict) else None
    if not pending:
        return None
    return {
        "id": _text(pending.get("id"), 120),
        "status": _text(pending.get("status"), 60),
        "checkpoint": int(pending.get("checkpoint") or 0),
        "choices": [
            {"choice_id": _text(item.get("choice_id"), 120), "title": _text(item.get("title"), 180)}
            for item in (pending.get("choices") or [])[:5] if isinstance(item, dict)
        ],
    }


def _available_assets(active, voice_ui):
    fields = set((active or {}).get("selected_fields") or [])
    return {
        "avatar_ready": bool(fields.intersection({"avatar_id", "image_upload_id"})),
        "voice_ready": bool(fields.intersection({"voice", "audio_upload_id"}))
        or str((voice_ui or {}).get("status") or "") in {"complete", "ready"},
    }


def build(project, state, capability_gates=None):
    """Build a compact snapshot from one already-authorized Project."""
    project = project if isinstance(project, dict) else {}
    state = state if isinstance(state, dict) else {}
    profile = state.get("ip_profile") if isinstance(state.get("ip_profile"), dict) else {}
    voice_ui = project.get("voice_clone_ui") if isinstance(project.get("voice_clone_ui"), dict) else {}
    productions = _productions(project)
    active_id = _text(project.get("active_production_id"), 100)
    active = next((item for item in productions if item["production_id"] == active_id), None)
    active_record = (project.get("productions") or {}).get(active_id) if active_id else None
    active_run = (project.get("agent_runs") or {}).get(
        str((active_record or {}).get("agent_run_id") or "")
    ) if isinstance(active_record, dict) else None
    candidates = [item for item in productions if item["status"] in ACTIVE_PRODUCTION_STATUSES]
    return {
        "schema": SCHEMA,
        "project_id": _text(project.get("id"), 100),
        "title": _text(project.get("title"), 160),
        "revision": int(state.get("revision") or 0),
        "workflow": {
            "current_module": int(state.get("current_module") or 1),
            "module_step": int(state.get("module_step") or 0),
            "completed_modules": [int(value) for value in (state.get("completed_modules") or [])
                                  if isinstance(value, int)],
            "pending": _pending(state),
            "foundation_status": _text((state.get("foundation_report") or {}).get("status"), 60),
        },
        "facts": _fact_map(profile.get("facts")),
        "preferences": _fact_map(profile.get("preferences")),
        "confirmed_outputs": _confirmed_outputs(profile),
        "content_topics": _content_topics(project),
        "active_content_target": _content_target(project),
        "voice_clone": {
            "status": _text(voice_ui.get("status"), 40),
            "voice_name": _text(voice_ui.get("voice_name"), 80),
            "slot_id": _text(voice_ui.get("slot_id"), 120),
        },
        "available_assets": _available_assets(active, voice_ui),
        "productions": productions,
        "active_production": copy.deepcopy(active),
        "active_agent_run": {
            key: _text((active_run or {}).get(key), 200)
            for key in ("agent_id", "status", "awaiting", "next_action")
        } if isinstance(active_run, dict) else None,
        "active_production_candidates": copy.deepcopy(candidates),
        "capability_gates": copy.deepcopy(capability_gates or []),
        "recent_messages": _recent_messages(project),
    }


def public_decision(decision):
    decision = decision if isinstance(decision, dict) else {}
    safe_token = lambda value, limit: re.sub(r"[^A-Za-z0-9_.:\-]", "", str(value or ""))[:limit]
    return {
        "intent": safe_token(decision.get("intent"), 40),
        "delegate_to": safe_token(decision.get("delegate_to"), 80),
        "tool": safe_token(decision.get("tool"), 80),
        "awaiting": safe_token(decision.get("awaiting"), 40),
        "confidence": decision.get("confidence"),
        "reason_codes": [safe_token(item, 80) for item in (decision.get("reason_codes") or [])[:8]],
        "memory_evidence": [
            {
                "source": safe_token(item.get("source"), 80),
                "ref": safe_token(item.get("ref"), 160),
            }
            for item in (decision.get("memory_evidence") or [])[:8]
            if isinstance(item, dict)
        ],
        "tool_policy": safe_token(decision.get("tool_policy"), 40),
        "payment_policy": copy.deepcopy(decision.get("payment_policy") or {}),
        "references": {
            key: safe_token((decision.get("references") or {}).get(key), 120)
            for key in ("production_id", "category_id", "topic_id")
        },
    }


def validated_preference_updates(decision, user_message):
    """Accept only explicit, evidenced preferences from the current user turn."""
    raw_message = str(user_message or "")
    compact_message = "".join(raw_message.split())
    accepted = []
    for item in (decision.get("memory_updates") or [])[:4]:
        if not isinstance(item, dict) or item.get("kind") != "preference":
            continue
        key = str(item.get("key") or "")
        value = _text(item.get("value"), 300)
        evidence = _text(item.get("evidence_quote"), 300)
        confidence = item.get("confidence")
        if key not in PREFERENCE_KEYS or not value or not evidence:
            continue
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or confidence < 0.85:
            continue
        if evidence not in raw_message and "".join(evidence.split()) not in compact_message:
            continue
        accepted.append({"key": key, "value": value, "evidence_quote": evidence})
    return accepted


def apply_preference_updates(state, updates):
    if not updates:
        return 0
    profile = state.setdefault("ip_profile", {})
    preferences = profile.setdefault("preferences", {})
    for item in updates:
        preferences[item["key"]] = {
            "value": item["value"],
            "evidence_quote": item["evidence_quote"],
        }
    return len(updates)


def record_decision(project, decision, request_id, revision, recorded_at):
    """Store no raw user text; this telemetry must remain best-effort."""
    event = {
        "request_id": _text(request_id, 80),
        "project_revision": int(revision or 0),
        "recorded_at": _text(recorded_at, 40),
        **public_decision(decision),
    }
    current = project.get("semantic_master") if isinstance(project.get("semantic_master"), dict) else {}
    history = list(current.get("history") or [])[-(HISTORY_LIMIT - 1):] + [event]
    project["semantic_master"] = {
        "schema": "ip12.semantic-master-telemetry/v1",
        "latest": event,
        "history": history,
    }
    return event
