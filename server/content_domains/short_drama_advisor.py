"""Stateless semantic advisor used before a short-drama project is created."""

import json
import hashlib
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request


ALLOWED_FIELDS = {
    "topic", "protagonist", "conflict", "emotion", "ending", "audience", "style",
}
ALLOWED_INTENTS = {
    "answer", "question", "ask_recommendation", "modify", "negate", "undo",
    "confirm", "unknown",
}
ALLOWED_OPERATIONS = {"set", "clear", "keep"}
ALLOWED_STATUSES = {"confirmed", "inferred", "suggested", "conflicted", "removed"}
ALLOWED_NEXT_ACTIONS = {"ask", "recommend", "confirm", "continue", "undo", "clarify"}

_USAGE_LOCK = threading.Lock()
_USER_WINDOWS = {}
_USER_ACTIVE = {}
_GLOBAL_ACTIVE = 0
_AUDIT_LOG = logging.getLogger("short_drama.advisor.usage")


class AdvisorError(ValueError):
    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code = code
        self.status = status


def _positive_env(name, default):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def _usage_limits():
    return {
        "window_seconds": _positive_env("SHORT_DRAMA_ADVISOR_WINDOW_SECONDS", 300),
        "requests_per_window": _positive_env("SHORT_DRAMA_ADVISOR_REQUESTS_PER_WINDOW", 12),
        "user_concurrency": _positive_env("SHORT_DRAMA_ADVISOR_USER_CONCURRENCY", 1),
        "global_concurrency": _positive_env("SHORT_DRAMA_ADVISOR_GLOBAL_CONCURRENCY", 4),
    }


def _acquire_usage(username, now=None):
    global _GLOBAL_ACTIVE
    username = str(username or "").strip()
    if not username:
        raise AdvisorError("advisor_identity_required", "无法确认创作助手使用账号", 401)
    now = time.time() if now is None else float(now)
    limits = _usage_limits()
    with _USAGE_LOCK:
        window = [
            value for value in _USER_WINDOWS.get(username, [])
            if now - value < limits["window_seconds"]
        ]
        _USER_WINDOWS[username] = window
        if len(window) >= limits["requests_per_window"]:
            raise AdvisorError(
                "advisor_rate_limited", "创作助手免费额度已达当前时间窗口上限，请稍后再试", 429
            )
        if _USER_ACTIVE.get(username, 0) >= limits["user_concurrency"]:
            raise AdvisorError(
                "advisor_user_busy", "当前账号已有创作助手请求处理中", 429
            )
        if _GLOBAL_ACTIVE >= limits["global_concurrency"]:
            raise AdvisorError(
                "advisor_capacity_reached", "创作助手当前繁忙，请稍后再试", 429
            )
        window.append(now)
        _USER_WINDOWS[username] = window
        _USER_ACTIVE[username] = _USER_ACTIVE.get(username, 0) + 1
        _GLOBAL_ACTIVE += 1
    return username


def _release_usage(username):
    global _GLOBAL_ACTIVE
    if not username:
        return
    with _USAGE_LOCK:
        active = max(0, _USER_ACTIVE.get(username, 0) - 1)
        if active:
            _USER_ACTIVE[username] = active
        else:
            _USER_ACTIVE.pop(username, None)
        _GLOBAL_ACTIVE = max(0, _GLOBAL_ACTIVE - 1)


def _reset_usage_for_tests():
    global _GLOBAL_ACTIVE
    with _USAGE_LOCK:
        _USER_WINDOWS.clear()
        _USER_ACTIVE.clear()
        _GLOBAL_ACTIVE = 0


def _provider_config():
    base = str(
        os.getenv("SHORT_DRAMA_ADVISOR_API_BASE")
        or os.getenv("XAI_API_BASE")
        or ""
    ).strip().rstrip("/")
    key = str(
        os.getenv("SHORT_DRAMA_ADVISOR_API_KEY")
        or os.getenv("XAI_API_KEY")
        or ""
    ).strip()
    model = str(os.getenv("SHORT_DRAMA_ADVISOR_MODEL") or "grok-3-mini").strip()
    if not base or not key:
        raise AdvisorError(
            "advisor_provider_not_configured",
            "\u524d\u7f6e\u521b\u4f5c\u52a9\u624b Provider \u5c1a\u672a\u914d\u7f6e",
            503,
        )
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/chat/completions", key, model


def _clean_body(body):
    if not isinstance(body, dict):
        raise AdvisorError("request_invalid", "\u8bf7\u6c42\u4f53\u5fc5\u987b\u662f JSON \u5bf9\u8c61")
    messages = body.get("messages") or []
    if not isinstance(messages, list) or len(messages) > 20:
        raise AdvisorError("messages_invalid", "\u8bbf\u8c08\u6d88\u606f\u683c\u5f0f\u65e0\u6548")
    cleaned = []
    for item in messages:
        value = str(item or "").strip()
        if value:
            cleaned.append(value[:600])
    understanding = body.get("understanding") or {}
    if not isinstance(understanding, dict):
        understanding = {}
    understanding = {
        key: str(value or "").strip()[:500]
        for key, value in understanding.items()
        if key in ALLOWED_FIELDS
    }
    expected_field = str(body.get("expected_field") or "").strip()
    if expected_field not in ALLOWED_FIELDS:
        expected_field = ""
    field_states = body.get("field_states") or {}
    if not isinstance(field_states, dict):
        field_states = {}
    field_states = {
        key: {
            "status": str((value or {}).get("status") or "")[:30],
            "confidence": (value or {}).get("confidence"),
            "evidence": str((value or {}).get("evidence") or "")[:200],
        }
        for key, value in field_states.items()
        if key in ALLOWED_FIELDS and isinstance(value, dict)
    }
    recommendation_context = body.get("recommendation_context") or {}
    if not isinstance(recommendation_context, dict):
        recommendation_context = {}
    recommendation_field = str(recommendation_context.get("field") or "").strip()
    if recommendation_field not in ALLOWED_FIELDS:
        recommendation_field = ""
    raw_options = recommendation_context.get("options") or []
    if not isinstance(raw_options, list):
        raw_options = []
    recommendation_options = [
        str(item or "").strip()[:160]
        for item in raw_options[:3]
        if str(item or "").strip()
    ]
    try:
        selected_index = int(recommendation_context.get("selected_index") or 0)
    except (TypeError, ValueError):
        selected_index = 0
    if selected_index < 0 or selected_index > len(recommendation_options):
        selected_index = 0
    selected_value = str(recommendation_context.get("selected_value") or "").strip()[:160]
    if selected_index and recommendation_options:
        selected_value = recommendation_options[selected_index - 1]
    return {
        "messages": cleaned,
        "understanding": understanding,
        "expected_field": expected_field,
        "field_states": field_states,
        "recommendation_context": {
            "field": recommendation_field,
            "options": recommendation_options,
            "selected_index": selected_index,
            "selected_value": selected_value,
        },
        "user_message": str(body.get("user_message") or "").strip()[:600],
    }


def _json_content(value):
    value = str(value or "").strip()
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I)
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as error:
        raise AdvisorError(
            "advisor_response_invalid", "\u521b\u4f5c\u52a9\u624b\u8fd4\u56de\u683c\u5f0f\u65e0\u6548", 502
        ) from error
    if not isinstance(result, dict):
        raise AdvisorError(
            "advisor_response_invalid", "\u521b\u4f5c\u52a9\u624b\u8fd4\u56de\u683c\u5f0f\u65e0\u6548", 502
        )
    return result


def _normalize(result, understanding=None):
    understanding = understanding or {}
    intent = str(result.get("intent") or "unknown").strip().lower()
    if intent not in ALLOWED_INTENTS:
        intent = "unknown"
    fields = result.get("extracted_fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    fields = {
        key: str(value or "").strip()[:500]
        for key, value in fields.items()
        if key in ALLOWED_FIELDS and str(value or "").strip()
    }
    try:
        confidence = float(result.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    updates = []
    raw_updates = result.get("field_updates") or []
    if isinstance(raw_updates, list):
        for raw in raw_updates[:10]:
            if not isinstance(raw, dict):
                continue
            field = str(raw.get("field") or "").strip()
            operation = str(raw.get("operation") or "set").strip().lower()
            if field not in ALLOWED_FIELDS or operation not in ALLOWED_OPERATIONS:
                continue
            value = str(raw.get("value") or "").strip()[:500]
            if operation == "set" and not value:
                continue
            try:
                update_confidence = float(raw.get("confidence") or result.get("confidence") or 0)
            except (TypeError, ValueError):
                update_confidence = 0.0
            update_confidence = max(0.0, min(1.0, update_confidence))
            status = str(raw.get("status") or "").strip().lower()
            if status not in ALLOWED_STATUSES:
                status = "confirmed" if update_confidence >= 0.8 else "inferred"
            if operation == "clear":
                status = "removed"
            updates.append({
                "field": field,
                "operation": operation,
                "value": value,
                "confidence": update_confidence,
                "evidence": str(raw.get("evidence") or "").strip()[:200],
                "status": status,
            })
    if not updates and intent in {"answer", "modify", "confirm"}:
        updates = [
            {
                "field": key,
                "operation": "set",
                "value": value,
                "confidence": confidence,
                "evidence": "",
                "status": "confirmed" if confidence >= 0.8 else "inferred",
            }
            for key, value in fields.items()
        ]
    conflicts = []
    raw_conflicts = result.get("conflicts") or []
    if isinstance(raw_conflicts, list):
        for raw in raw_conflicts[:10]:
            if not isinstance(raw, dict):
                continue
            field = str(raw.get("field") or "").strip()
            if field not in ALLOWED_FIELDS:
                continue
            conflicts.append({
                "field": field,
                "existing_value": str(raw.get("existing_value") or understanding.get(field) or "").strip()[:500],
                "proposed_value": str(raw.get("proposed_value") or "").strip()[:500],
                "reason": str(raw.get("reason") or "新说法与当前设定不一致").strip()[:300],
                "requires_confirmation": bool(raw.get("requires_confirmation", True)),
            })
    conflict_fields = {item["field"] for item in conflicts if item["requires_confirmation"]}
    for update in updates:
        if update["field"] in conflict_fields:
            update["status"] = "conflicted"
    next_action = str(result.get("next_action") or "").strip().lower()
    if next_action not in ALLOWED_NEXT_ACTIONS:
        next_action = "clarify" if conflicts else (
            "recommend" if intent == "ask_recommendation" else
            "undo" if intent == "undo" else
            "continue" if intent in {"answer", "modify", "negate", "confirm"} else "ask"
        )
    focus_field = str(result.get("focus_field") or "").strip()
    if focus_field not in ALLOWED_FIELDS:
        focus_field = ""
    quick = result.get("quick_replies") or []
    if not isinstance(quick, list):
        quick = []
    return {
        "intent": intent,
        "reply": str(result.get("reply") or "\u8bf7\u518d\u5177\u4f53\u8bf4\u4e00\u70b9\u3002")[:1000],
        "extracted_fields": fields,
        "field_updates": updates,
        "conflicts": conflicts,
        "missing_fields": [
            str(item) for item in (result.get("missing_fields") or [])
            if str(item) in ALLOWED_FIELDS
        ],
        "confidence": confidence,
        "quick_replies": [str(item)[:80] for item in quick[:4] if str(item).strip()],
        "recap": str(result.get("recap") or "").strip()[:1000],
        "next_action": next_action,
        "focus_field": focus_field,
        "understanding_summary": str(result.get("understanding_summary") or "").strip()[:1000],
        "mode": "ai",
        "degraded": False,
    }


def _advise_provider(body, opener=None):
    request_body = _clean_body(body)
    if not request_body["user_message"]:
        raise AdvisorError("message_required", "\u8bf7\u8f93\u5165\u60f3\u6cd5\u6216\u95ee\u9898")
    url, key, model = _provider_config()
    system = (
        "You are a Chinese short-drama interview assistant. Classify whether the user "
        "is answering, asking a question, requesting a recommendation, modifying a fact, "
        "negating/removing a fact, undoing the previous change, confirming the current facts, "
        "or unclear. Resolve references such as 'it', 'that one', and 'the previous setting' "
        "from the supplied understanding and conversation. Only extract facts the user explicitly supplied. Phrases such as "
        "'\u4f60\u89c9\u5f97\u5462', '\u5e2e\u6211\u63a8\u8350', and '\u4e0d\u77e5\u9053' must never be stored as business fields. "
        "When the user asks a question, answer it and provide 2-4 concrete options without "
        "advancing the expected field. Never turn a negated value into a positive fact. "
        "The request may contain recommendation_context with the exact numbered options shown "
        "to the user. Treat selected_index and selected_value as the resolved meaning of a terse "
        "numeric choice, and never ask what that number means when they are present. "
        "Extract every explicitly supplied field from one message, not only expected_field. "
        "For every requested change return field_updates, an array of objects with field, "
        "operation (set, clear, or keep), value, confidence, short verbatim evidence, and status. "
        "Status must be confirmed for explicit high-confidence user facts, inferred for uncertain "
        "interpretations, suggested only for assistant proposals, conflicted when two plausible "
        "interpretations need the user to choose, or removed after clear. Use clear "
        "when the user cancels a setting; use undo only when the user asks to undo the last change. "
        "Compare proposed updates with supplied understanding. Return conflicts only when the new "
        "message is genuinely ambiguous or cannot safely replace the current fact; an explicit phrase "
        "such as '改成' is a confirmed replacement, not an unresolved conflict. Ask at most one question, "
        "targeting the highest-impact missing fact. Return exactly one JSON object with keys intent, "
        "reply, recap, understanding_summary, extracted_fields, field_updates, conflicts, missing_fields, "
        "confidence, quick_replies, next_action, focus_field. intent must be answer, "
        "question, ask_recommendation, modify, negate, undo, confirm, or unknown. Fields may "
        "only be topic, protagonist, conflict, emotion, ending, audience, or style. recap must "
        "briefly state what changed, what stayed, and what remains uncertain. Reply in Chinese."
    )
    payload = json.dumps({
        "model": model,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(request_body, ensure_ascii=False)},
        ],
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with (opener or urllib.request.urlopen)(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise AdvisorError(
            "advisor_provider_failed",
            "\u521b\u4f5c\u52a9\u624b\u6682\u65f6\u4e0d\u53ef\u7528\uff08HTTP %s\uff09" % error.code,
            502,
        ) from error
    except (OSError, ValueError) as error:
        raise AdvisorError(
            "advisor_provider_failed", "\u521b\u4f5c\u52a9\u624b\u6682\u65f6\u4e0d\u53ef\u7528", 502
        ) from error
    choices = result.get("choices") or []
    content = (((choices[0] if choices else {}).get("message") or {}).get("content") or "")
    return _normalize(_json_content(content), request_body["understanding"])


def advise(body, opener=None, username=None):
    request_body = _clean_body(body)
    if not request_body["user_message"]:
        raise AdvisorError("message_required", "请输入想法或问题")
    # Internal deterministic tests may omit username; the authenticated HTTP route never does.
    ticket = _acquire_usage(username) if username is not None else None
    request_hash = hashlib.sha256(
        json.dumps(request_body, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    started = time.monotonic()
    outcome = "failed"
    try:
        result = _advise_provider(request_body, opener=opener)
        outcome = "succeeded"
        return result
    finally:
        _release_usage(ticket)
        _AUDIT_LOG.info(
            "advisor_usage username=%s request_hash=%s outcome=%s duration_ms=%d",
            ticket or "internal", request_hash, outcome,
            int((time.monotonic() - started) * 1000),
        )
