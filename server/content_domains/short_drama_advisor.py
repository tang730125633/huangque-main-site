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
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import egress, provider_keys


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
_USER_ACTIVE = {}
_GLOBAL_ACTIVE = 0
_AUDIT_LOG = logging.getLogger("short_drama.advisor.usage")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_INPUT_TOKENS = 16000
_MAX_OUTPUT_TOKENS = 1200
_MODEL_PRICES = {
    # xAI prices per 1M tokens: $0.30 input / $0.50 output.
    "grok-3-mini": (300000, 500000),
    "grok-3-mini-latest": (300000, 500000),
    "grok-3-mini-beta": (300000, 500000),
    "grok-3-mini-fast": (300000, 500000),
    "grok-3-mini-fast-latest": (300000, 500000),
    "grok-3-mini-fast-beta": (300000, 500000),
}

_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_advisor_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  period_day INTEGER NOT NULL,
  reserved_microusd INTEGER NOT NULL,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  status TEXT NOT NULL DEFAULT 'reserved',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_advisor_usage_user_time
  ON short_drama_advisor_usage(username, created_at);
CREATE INDEX IF NOT EXISTS idx_short_drama_advisor_usage_day
  ON short_drama_advisor_usage(period_day, status);
"""


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
        "user_daily_requests": _positive_env("SHORT_DRAMA_ADVISOR_USER_DAILY_REQUESTS", 60),
        "global_daily_budget_microusd": _positive_env(
            "SHORT_DRAMA_ADVISOR_GLOBAL_DAILY_BUDGET_MICROUSD", 1000000
        ),
        "user_concurrency": _positive_env("SHORT_DRAMA_ADVISOR_USER_CONCURRENCY", 1),
        "global_concurrency": _positive_env("SHORT_DRAMA_ADVISOR_GLOBAL_CONCURRENCY", 4),
    }


def init_db(db_factory):
    with closing(db_factory()) as conn:
        conn.executescript(_USAGE_SCHEMA)
        conn.commit()


def _shanghai_day_bounds(timestamp):
    local = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(_SHANGHAI)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp()), int(midnight.timestamp()) + 86400


def _reserve_usage(db_factory, username, request_hash, now, limits, reserve):
    if not callable(db_factory):
        raise AdvisorError(
            "advisor_usage_store_unavailable", "创作助手额度服务暂时不可用", 503
        )
    timestamp = int(now)
    period_day, next_day = _shanghai_day_bounds(timestamp)
    try:
        with closing(db_factory()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            window_count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_advisor_usage "
                "WHERE username=? AND created_at>=?",
                (username, timestamp - limits["window_seconds"]),
            ).fetchone()[0]
            if window_count >= limits["requests_per_window"]:
                raise AdvisorError(
                    "advisor_rate_limited",
                    "创作助手免费额度已达当前时间窗口上限，请稍后再试",
                    429,
                )
            daily_count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_advisor_usage "
                "WHERE username=? AND created_at>=? AND created_at<?",
                (username, period_day, next_day),
            ).fetchone()[0]
            if daily_count >= limits["user_daily_requests"]:
                raise AdvisorError(
                    "advisor_daily_quota_exhausted",
                    "创作助手今日免费额度已用完，请明日再试",
                    429,
                )
            reserved = conn.execute(
                "SELECT COALESCE(SUM(reserved_microusd),0) "
                "FROM short_drama_advisor_usage WHERE created_at>=? AND created_at<?",
                (period_day, next_day),
            ).fetchone()[0]
            if reserved + reserve > limits["global_daily_budget_microusd"]:
                raise AdvisorError(
                    "advisor_global_budget_exhausted",
                    "创作助手今日全局费用预算已用完",
                    503,
                )
            cursor = conn.execute(
                "INSERT INTO short_drama_advisor_usage"
                "(username,request_hash,period_day,reserved_microusd,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (username, request_hash, period_day, reserve, "reserved", timestamp, timestamp),
            )
            usage_id = cursor.lastrowid
            conn.commit()
    except AdvisorError:
        raise
    except Exception as error:
        _AUDIT_LOG.exception("advisor_usage_reservation_failed username=%s", username)
        raise AdvisorError(
            "advisor_usage_store_unavailable", "创作助手额度服务暂时不可用", 503
        ) from error
    return {
        "id": usage_id, "username": username, "db_factory": db_factory,
        "reserve_microusd": reserve,
    }


def _acquire_usage(username, db_factory, request_hash, reserve, model, now=None):
    global _GLOBAL_ACTIVE
    username = str(username or "").strip()
    if not username:
        raise AdvisorError("advisor_identity_required", "无法确认创作助手使用账号", 401)
    now = time.time() if now is None else float(now)
    limits = _usage_limits()
    with _USAGE_LOCK:
        if _USER_ACTIVE.get(username, 0) >= limits["user_concurrency"]:
            raise AdvisorError(
                "advisor_user_busy", "当前账号已有创作助手请求处理中", 429
            )
        if _GLOBAL_ACTIVE >= limits["global_concurrency"]:
            raise AdvisorError(
                "advisor_capacity_reached", "创作助手当前繁忙，请稍后再试", 429
            )
        _USER_ACTIVE[username] = _USER_ACTIVE.get(username, 0) + 1
        _GLOBAL_ACTIVE += 1
    try:
        ticket = _reserve_usage(
            db_factory, username, request_hash, now, limits, reserve
        )
        ticket["model"] = model
        return ticket
    except Exception:
        _release_usage({"username": username})
        raise


def _release_usage(ticket):
    global _GLOBAL_ACTIVE
    username = ticket.get("username") if isinstance(ticket, dict) else ticket
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
        _USER_ACTIVE.clear()
        _GLOBAL_ACTIVE = 0


def _finalize_usage(ticket, outcome, provider_usage=None):
    if not isinstance(ticket, dict) or not ticket.get("id"):
        return
    usage = provider_usage if isinstance(provider_usage, dict) else {}
    prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
    completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens")
    actual_cost = ticket.get("reserve_microusd")
    if outcome == "succeeded" and prompt_tokens is not None and completion_tokens is not None:
        try:
            actual_cost = _token_cost(
                ticket.get("model"), prompt_tokens, completion_tokens
            )
        except AdvisorError:
            _AUDIT_LOG.warning(
                "advisor_provider_usage_invalid usage_id=%s", ticket.get("id")
            )
            prompt_tokens = None
            completion_tokens = None
    try:
        with closing(ticket["db_factory"]()) as conn:
            conn.execute(
                "UPDATE short_drama_advisor_usage SET status=?,reserved_microusd=?,prompt_tokens=?,"
                "completion_tokens=?,updated_at=? WHERE id=? AND status='reserved'",
                (
                    str(outcome or "failed")[:32],
                    actual_cost,
                    int(prompt_tokens) if prompt_tokens is not None else None,
                    int(completion_tokens) if completion_tokens is not None else None,
                    int(time.time()),
                    ticket["id"],
                ),
            )
            conn.commit()
    except Exception:
        _AUDIT_LOG.exception(
            "advisor_usage_finalize_failed usage_id=%s", ticket.get("id")
        )


def _provider_config():
    base = str(
        os.getenv("SHORT_DRAMA_ADVISOR_API_BASE")
        or os.getenv("XAI_API_BASE")
        or ""
    ).strip().rstrip("/")
    model = str(os.getenv("SHORT_DRAMA_ADVISOR_MODEL") or "grok-3-mini").strip()
    if not base:
        raise AdvisorError(
            "advisor_provider_not_configured",
            "\u524d\u7f6e\u521b\u4f5c\u52a9\u624b Provider \u5c1a\u672a\u914d\u7f6e",
            503,
        )
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/chat/completions", model


def _token_cost(model, prompt_tokens, completion_tokens):
    prices = _MODEL_PRICES.get(str(model or ""))
    if prices is None:
        raise AdvisorError(
            "advisor_provider_pricing_unavailable",
            "创作助手模型尚未配置可信计费价格",
            503,
        )
    try:
        prompt_tokens = int(prompt_tokens)
        completion_tokens = int(completion_tokens)
    except (TypeError, ValueError) as error:
        raise AdvisorError(
            "advisor_provider_usage_invalid", "创作助手计费信息无效", 502
        ) from error
    if prompt_tokens < 0 or completion_tokens < 0:
        raise AdvisorError(
            "advisor_provider_usage_invalid", "创作助手计费信息无效", 502
        )
    numerator = prompt_tokens * prices[0] + completion_tokens * prices[1]
    return (numerator + 999999) // 1000000


def _max_output_tokens():
    return min(
        _MAX_OUTPUT_TOKENS,
        _positive_env("SHORT_DRAMA_ADVISOR_MAX_TOKENS", _MAX_OUTPUT_TOKENS),
    )


def _provider_opener():
    """Reuse the same preflight-selected xAI egress route as official video."""
    proxy = egress.preferred_proxy()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        ).open
    return urllib.request.build_opener().open


def _claim_provider_candidate(attempted_ids):
    try:
        candidate = provider_keys.claim_candidate("xai")
    except provider_keys.KeyStoreUnavailable as error:
        raise AdvisorError(
            "advisor_provider_not_configured",
            "\u524d\u7f6e\u521b\u4f5c\u52a9\u624b Provider \u5c1a\u672a\u914d\u7f6e",
            503,
        ) from error
    if not candidate or candidate.get("id") in attempted_ids:
        if attempted_ids:
            raise AdvisorError(
                "advisor_provider_failed",
                "\u521b\u4f5c\u52a9\u624b\u6682\u65f6\u4e0d\u53ef\u7528\uff08xAI \u5bc6\u94a5\u5747\u5df2\u5931\u6548\uff09",
                502,
            )
        raise AdvisorError(
            "advisor_provider_not_configured",
            "\u524d\u7f6e\u521b\u4f5c\u52a9\u624b Provider \u5c1a\u672a\u914d\u7f6e",
            503,
        )
    return candidate


def _set_candidate_health(candidate, ok, latency_ms=None, error=""):
    try:
        provider_keys.set_health(
            candidate["id"], ok, latency_ms, str(error or "")[:180]
        )
    except Exception:
        _AUDIT_LOG.exception(
            "advisor_provider_key_health_write_failed key_id=%s",
            candidate.get("id"),
        )


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


def _prepare_provider_request(body):
    request_body = _clean_body(body)
    if not request_body["user_message"]:
        raise AdvisorError("message_required", "\u8bf7\u8f93\u5165\u60f3\u6cd5\u6216\u95ee\u9898")
    url, model = _provider_config()
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
    max_tokens = _max_output_tokens()
    user_content = json.dumps(request_body, ensure_ascii=False)
    # One UTF-8 byte per token plus message-envelope slack is a conservative
    # upper bound without relying on a provider-specific tokenizer.
    input_tokens = (
        len(system.encode("utf-8")) + len(user_content.encode("utf-8")) + 256
    )
    if input_tokens > _MAX_INPUT_TOKENS:
        raise AdvisorError(
            "advisor_input_too_large", "创作助手输入内容过长，请精简后重试", 413
        )
    payload = json.dumps({
        "model": model,
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    }, ensure_ascii=False).encode("utf-8")
    return {
        "request_body": request_body,
        "url": url,
        "model": model,
        "payload": payload,
        "reserve_microusd": _token_cost(
            model, _MAX_INPUT_TOKENS, _MAX_OUTPUT_TOKENS
        ),
    }


def _advise_provider(body, opener=None, prepared=None):
    prepared = prepared or _prepare_provider_request(body)
    request_body = prepared["request_body"]
    url = prepared["url"]
    payload = prepared["payload"]
    attempted_ids = set()
    request_open = opener or _provider_opener()
    while True:
        candidate = _claim_provider_candidate(attempted_ids)
        attempted_ids.add(candidate["id"])
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": "Bearer " + candidate["secret"],
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with request_open(request, timeout=45) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            latency_ms = int((time.monotonic() - started) * 1000)
            if error.code in (401, 403):
                _set_candidate_health(
                    candidate, False, latency_ms, "HTTP %s" % error.code
                )
                continue
            raise AdvisorError(
                "advisor_provider_failed",
                "\u521b\u4f5c\u52a9\u624b\u6682\u65f6\u4e0d\u53ef\u7528\uff08HTTP %s\uff09" % error.code,
                502,
            ) from error
        except (OSError, ValueError) as error:
            raise AdvisorError(
                "advisor_provider_failed", "\u521b\u4f5c\u52a9\u624b\u6682\u65f6\u4e0d\u53ef\u7528", 502
            ) from error
        _set_candidate_health(
            candidate, True, int((time.monotonic() - started) * 1000), ""
        )
        break
    choices = result.get("choices") or []
    content = (((choices[0] if choices else {}).get("message") or {}).get("content") or "")
    normalized = _normalize(_json_content(content), request_body["understanding"])
    normalized["_provider_usage"] = result.get("usage") or {}
    return normalized


def advise(body, opener=None, username=None, db_factory=None):
    request_body = _clean_body(body)
    if not request_body["user_message"]:
        raise AdvisorError("message_required", "请输入想法或问题")
    prepared = _prepare_provider_request(request_body)
    request_hash = hashlib.sha256(
        json.dumps(request_body, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    # Internal deterministic normalization tests may omit username; HTTP never does.
    ticket = (
        _acquire_usage(
            username, db_factory, request_hash,
            prepared["reserve_microusd"], prepared["model"],
        )
        if username is not None else None
    )
    started = time.monotonic()
    outcome = "failed"
    provider_usage = {}
    try:
        result = _advise_provider(request_body, opener=opener, prepared=prepared)
        provider_usage = result.pop("_provider_usage", {})
        outcome = "succeeded"
        return result
    finally:
        _finalize_usage(ticket, outcome, provider_usage)
        _release_usage(ticket)
        _AUDIT_LOG.info(
            "advisor_usage username=%s request_hash=%s outcome=%s duration_ms=%d",
            ticket or "internal", request_hash, outcome,
            int((time.monotonic() - started) * 1000),
        )
