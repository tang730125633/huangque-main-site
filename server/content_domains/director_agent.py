# -*- coding: utf-8 -*-
"""Context-aware guide and customer-confirmed Director production Agent."""

import hashlib
import json
import os
import re
import secrets
import threading
import time

from . import director_cli
from . import submission_idempotency


MAX_ACTIONS = 6
MAX_HISTORY = 10
MODEL = os.environ.get("DIRECTOR_AGENT_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"
REASONING_EFFORT = os.environ.get("DIRECTOR_AGENT_REASONING_EFFORT", "low").strip() or "low"
API_BASE = os.environ.get("DIRECTOR_AGENT_API_BASE", "").strip() or None
API_KEY = os.environ.get("DIRECTOR_AGENT_API_KEY", "").strip() or None


def _post(*args, **kwargs):
    """Import the shared HTTP client lazily so registry startup stays optional."""
    from . import core
    return core._post(*args, **kwargs)


def provider_config(fallback_base=None, fallback_key=None):
    """Resolve one endpoint/key pair without crossing credential scopes.

    A dedicated endpoint is usable only with its dedicated key. Without that
    endpoint the Agent uses the global pair and ignores a stray dedicated key.
    """
    if API_BASE:
        return (API_BASE, API_KEY) if API_KEY else None
    global_key = str(fallback_key or "").strip()
    if not global_key:
        return None
    return (str(fallback_base or "https://api.openai.com").strip(), global_key)


def is_available(fallback_key=None, fallback_base=None):
    """Require both a scope-safe model pair and the local read-only HQ CLI."""
    try:
        from . import director_conversation
    except (ImportError, SyntaxError):
        return False
    return (provider_config(fallback_base, fallback_key) is not None
            and callable(director_conversation.converse) and director_cli.is_available())


def _env_positive_int(name, default):
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except Exception:
        value = default
    return max(1, value)


RATE_LIMIT_PER_MINUTE = _env_positive_int("DIRECTOR_AGENT_RATE_LIMIT_PER_MINUTE", 12)
DAILY_LIMIT = _env_positive_int("DIRECTOR_AGENT_DAILY_LIMIT", 120)
OFFER_TTL_SECONDS = max(
    60, min(3600, _env_positive_int("DIRECTOR_AGENT_OFFER_TTL_SECONDS", 900))
)
CONFIRM_SCRIPT_PROMPT = "确认生成"


class DirectorOfferError(ValueError):
    """A terminal customer-confirmation error safe to return to the browser."""

    def __init__(self, message, code="director_offer_invalid", status=400):
        super().__init__(message)
        self.code = code
        self.status = status

SCRIPT_MODES = {"write", "script_to_video", "breakdown"}
DIGITAL_HUMAN_MODES = {"photo", "video"}
DIGITAL_HUMAN_GUIDE_CONTRACT = "digital-human-oneclick-guide-v1"
MODES = SCRIPT_MODES | DIGITAL_HUMAN_MODES
BREAKDOWN_TOOLS = {"scenes", "reverse_prompt"}
STAGES = {
    "understand", "script", "breakdown", "assets", "video",
    "setup", "voice", "production", "result",
}
FIELD_LIMITS = {
    "topic": 1000,
    "selling_points": 2000,
    "breakdown_url": 2000,
    "digital_human_script": 6000,
}
FIELD_NAMES = set(FIELD_LIMITS)
OPTION_VALUES = {
    "style": {"口播", "剧情", "种草"},
    "duration": {"15s", "30s", "60s"},
    "platform": {"抖音", "小红书", "视频号"},
    "breakdown_tool": BREAKDOWN_TOOLS,
    "narration_mode": {"text", "audio"},
    "precision_template": {
        "viral-talking-head-v1", "professional-explainer-v1",
        "clean-talking-v1",
    },
}
OPTION_NAMES = set(OPTION_VALUES)
FOCUS_TARGETS = {
    "topic", "selling_points", "generate_script", "breakdown_url",
    "analyze_breakdown", "generate_video", "generate_audio", "export_script",
    "photo_upload", "voice_source", "voice_upload", "customer_materials",
    "full_audio_upload", "photo_authorization", "analyze_plan",
    "generate_photo_video", "video_upload", "precision_authorization",
    "analyze_voice", "generate_precision_video",
}
NAV_TARGETS = {
    "script", "digital_human", "ip12", "assets", "audio", "video", "canvas",
}
PAGE_ACTION_SCOPE = {
    "script": {
        "fill_field": {"topic", "selling_points", "breakdown_url"},
        "choose_option": {"style", "duration", "platform", "breakdown_tool"},
        "switch_mode": SCRIPT_MODES,
        "focus": {
            "topic", "selling_points", "generate_script", "breakdown_url",
            "analyze_breakdown", "generate_video", "generate_audio", "export_script",
        },
    },
    "digital_human_oneclick": {
        "fill_field": {"digital_human_script"},
        "choose_option": {"narration_mode", "precision_template"},
        "switch_mode": DIGITAL_HUMAN_MODES,
        "focus": {
            "photo_upload", "voice_source", "voice_upload", "customer_materials",
            "full_audio_upload", "photo_authorization", "analyze_plan",
            "generate_photo_video", "video_upload", "precision_authorization",
            "analyze_voice", "generate_precision_video",
        },
    },
}
MEDIA_MARKERS = ("data:image/", "data:video/", ";base64,", "blob:")
BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{512,}={0,2}(?![A-Za-z0-9+/_=-])")


def _schema(properties):
    return {
        "type": "object", "additionalProperties": False,
        "properties": properties, "required": list(properties),
    }


DIRECTOR_AGENT_SCHEMA = _schema({
    "content": {"type": "string", "maxLength": 5000},
    "stage": {"type": "string", "enum": sorted(STAGES)},
    "actions": {"type": "array", "maxItems": MAX_ACTIONS, "items": {"anyOf": [
        _schema({
            "type": {"type": "string", "const": "fill_field"},
            "field": {"type": "string", "enum": sorted(FIELD_NAMES)},
            "value": {"type": "string", "maxLength": 6000},
            "label": {"type": "string", "maxLength": 80},
        }),
        _schema({
            "type": {"type": "string", "const": "choose_option"},
            "field": {"type": "string", "enum": sorted(OPTION_NAMES)},
            "value": {"type": "string", "maxLength": 160},
            "label": {"type": "string", "maxLength": 80},
        }),
        _schema({
            "type": {"type": "string", "const": "switch_mode"},
            "mode": {"type": "string", "enum": sorted(MODES)},
            "label": {"type": "string", "maxLength": 80},
        }),
        _schema({
            "type": {"type": "string", "const": "focus"},
            "target": {"type": "string", "enum": sorted(FOCUS_TARGETS)},
            "label": {"type": "string", "maxLength": 80},
        }),
        _schema({
            "type": {"type": "string", "const": "navigate"},
            "target": {"type": "string", "enum": sorted(NAV_TARGETS)},
            "label": {"type": "string", "maxLength": 80},
        }),
    ]}},
    "warnings": {
        "type": "array", "maxItems": 8,
        "items": {"type": "string", "maxLength": 300},
    },
    "offer_production": {"type": "boolean"},
})


def _text(value, limit, field):
    value = str(value or "").strip()
    if len(value) > limit:
        raise ValueError("%s超过长度限制" % field)
    return value


def _contains_media(value):
    raw = json.dumps(value, ensure_ascii=False).lower()
    return any(marker in raw for marker in MEDIA_MARKERS) or bool(BASE64_RE.search(raw))


def _local_day_bounds(now):
    stamp = time.localtime(now)
    start = int(time.mktime((stamp.tm_year, stamp.tm_mon, stamp.tm_mday,
                             0, 0, 0, 0, 0, -1)))
    next_start = int(time.mktime((stamp.tm_year, stamp.tm_mon, stamp.tm_mday + 1,
                                  0, 0, 0, 0, 0, -1)))
    return start, next_start


def _submission_limit_snapshot(db_factory, username, now=None):
    """Return a public 429 body when an authenticated account exceeds its quota."""
    username = _text(username, 160, "认证账号")
    if not username:
        raise ValueError("编导助手缺少认证账号")
    now = int(time.time() if now is None else now)
    day_start, next_day_start = _local_day_bounds(now)
    window_start = min(day_start, now - 60)
    connection = db_factory()
    try:
        usage_row = connection.execute(
            """SELECT
                   COALESCE(SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END),0)
               FROM jobs
               WHERE username=? AND kind='director_agent' AND created_at>=?""",
            (now - 60, day_start, username, window_start),
        ).fetchone()
    finally:
        connection.close()
    minute_count = int(usage_row[0] if usage_row else 0)
    daily_count = int(usage_row[1] if usage_row else 0)
    if minute_count >= RATE_LIMIT_PER_MINUTE:
        return {
            "detail": "编导助手回复过于频繁，请一分钟后再试",
            "code": "director_agent_rate_limited",
            "retry_after_ms": 60000,
            "limit": RATE_LIMIT_PER_MINUTE,
        }
    if daily_count >= DAILY_LIMIT:
        return {
            "detail": "今天的编导助手次数已用完，请明天继续",
            "code": "director_agent_daily_limit",
            "retry_after_ms": max(1000, (next_day_start - now) * 1000),
            "limit": DAILY_LIMIT,
        }
    return None


def accept_chat_job(db_factory, username, payload, owner, endpoint,
                    idempotency_key, points_left=0, max_active_jobs=None,
                    now=None):
    """Atomically claim one chat request and create its zero-cost job.

    Tang main uses the compact submission_idempotency schema.  Keeping the
    claim, quota check, job insert, and replay response in one transaction
    makes retries safe without widening that shared database contract.
    """
    username = _text(username, 160, "\u8ba4\u8bc1\u8d26\u53f7")
    if not username:
        raise ValueError("\u7f16\u5bfc\u52a9\u624b\u7f3a\u5c11\u8ba4\u8bc1\u8d26\u53f7")
    endpoint = _text(endpoint, 200, "\u63d0\u4ea4\u7aef\u70b9")
    idempotency_key = submission_idempotency.clean_key(idempotency_key)
    if not endpoint or not idempotency_key:
        raise ValueError("\u7f16\u5bfc\u52a9\u624b\u63d0\u4ea4\u5fc5\u987b\u63d0\u4f9b Idempotency-Key")
    now = int(time.time() if now is None else now)
    day_start, next_day_start = _local_day_bounds(now)
    window_start = min(day_start, now - 60)
    request_hash = hashlib.sha256(
        _canonical(payload).encode("utf-8")
    ).hexdigest()
    connection = db_factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        submission_idempotency.ensure_table(connection)
        existing = connection.execute(
            "SELECT request_hash,response_json FROM submission_idempotency "
            "WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, idempotency_key),
        ).fetchone()
        if existing:
            if str(existing["request_hash"]) != request_hash:
                connection.rollback()
                return "conflict", None
            if existing["response_json"]:
                response = json.loads(existing["response_json"])
                connection.commit()
                return "replay", response
            connection.commit()
            return "processing", None
        usage_row = connection.execute(
            """SELECT
                   COALESCE(SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END),0)
               FROM jobs
               WHERE username=? AND kind='director_agent' AND created_at>=?""",
            (now - 60, day_start, username, window_start),
        ).fetchone()
        minute_count = int(usage_row[0] if usage_row else 0)
        daily_count = int(usage_row[1] if usage_row else 0)
        limit_hit = None
        if minute_count >= RATE_LIMIT_PER_MINUTE:
            limit_hit = {
                "detail": "\u7f16\u5bfc\u52a9\u624b\u56de\u590d\u8fc7\u4e8e\u9891\u7e41\uff0c\u8bf7\u4e00\u5206\u949f\u540e\u518d\u8bd5",
                "code": "director_agent_rate_limited",
                "retry_after_ms": 60000,
                "limit": RATE_LIMIT_PER_MINUTE,
            }
        elif daily_count >= DAILY_LIMIT:
            limit_hit = {
                "detail": "\u4eca\u5929\u7684\u7f16\u5bfc\u52a9\u624b\u6b21\u6570\u5df2\u7528\u5b8c\uff0c\u8bf7\u660e\u5929\u7ee7\u7eed",
                "code": "director_agent_daily_limit",
                "retry_after_ms": max(1000, (next_day_start - now) * 1000),
                "limit": DAILY_LIMIT,
            }
        if not limit_hit and max_active_jobs is not None:
            active_row = connection.execute(
                """SELECT COUNT(*) FROM jobs
                   WHERE username=? AND status IN ('pending','running')
                     AND COALESCE(deleted,0)=0""",
                (username,),
            ).fetchone()
            active_jobs = int(active_row[0] if active_row else 0)
            if active_jobs >= int(max_active_jobs):
                limit_hit = {
                    "detail": "\u60a8\u6709 %d \u4e2a\u4efb\u52a1\u6b63\u5728\u6392\u961f\u751f\u6210\uff0c\u5b8c\u6210\u540e\u518d\u63d0\u4ea4" % active_jobs,
                    "code": "active_job_cap",
                    "active_jobs": active_jobs,
                    "max_active_jobs": int(max_active_jobs),
                    "retry_after_ms": 4000,
                    "need": 0,
                }
        if limit_hit:
            connection.rollback()
            return "limited", limit_hit
        cursor = connection.execute(
            """INSERT INTO jobs(kind,username,cost,payload,created_at,updated_at,owner)
               VALUES('director_agent',?,?,?,?,?,?)""",
            (username, 0, json.dumps(payload, ensure_ascii=False), now, now, owner),
        )
        job_id = int(cursor.lastrowid)
        response = {
            "job_id": job_id, "cost": 0, "points_left": int(points_left),
        }
        connection.execute(
            "INSERT INTO submission_idempotency"
            "(username,endpoint,idem_key,request_hash,response_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (username, endpoint, idempotency_key, request_hash,
             json.dumps(response, ensure_ascii=False), now, now),
        )
        connection.commit()
        return "new", response
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


_PRODUCTION_ID_RE = re.compile(r"^director-production-[A-Za-z0-9_-]{16,64}$")
_PRODUCTION_LOCKS = {}
_PRODUCTION_LOCKS_GUARD = threading.Lock()


def _production_lock(username, offer_id):
    key = username + "\0" + offer_id
    with _PRODUCTION_LOCKS_GUARD:
        lock = _PRODUCTION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PRODUCTION_LOCKS[key] = lock
        return lock


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _ensure_production_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS director_cli_productions(
        username TEXT NOT NULL,
        offer_id TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        input_json TEXT NOT NULL,
        expected_cost INTEGER NOT NULL,
        quoted_cost INTEGER,
        quote_token TEXT,
        quote_expires_at INTEGER,
        state TEXT NOT NULL,
        job_id INTEGER,
        points_left INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        last_error TEXT,
        PRIMARY KEY(username, offer_id)
    )""")


def _ensure_offer_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS director_agent_offers(
        username TEXT NOT NULL,
        offer_id TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        expected_cost INTEGER NOT NULL,
        token_hash TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        confirmed_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY(username, offer_id)
    )""")


def _ensure_pending_plan_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS director_agent_pending_plans(
        username TEXT NOT NULL,
        session_id TEXT NOT NULL,
        offer_id TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        input_json TEXT NOT NULL,
        expected_cost INTEGER NOT NULL,
        page_revision TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY(username, session_id),
        UNIQUE(username, offer_id)
    )""")


def _pending_plan_digest(session_id, offer_id, cli_input, expected_cost,
                         page_revision):
    return hashlib.sha256(_canonical({
        "session_id": session_id,
        "offer_id": offer_id,
        "input": cli_input,
        "expected_cost": int(expected_cost),
        "page_revision": page_revision,
    }).encode("utf-8")).hexdigest()


def _save_pending_plan(db_factory, username, session_id, plan, now=None):
    """Durably bind one prepared, non-billable plan to this chat session."""
    stamp = int(time.time() if now is None else now)
    cli_input = plan["input"]
    request_json = _canonical(cli_input)
    input_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
    digest = _pending_plan_digest(
        session_id, plan["offer_id"], cli_input, plan["expected_cost"],
        plan["page_revision"],
    )
    expires_at = stamp + OFFER_TTL_SECONDS
    connection = db_factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_pending_plan_table(connection)
        connection.execute(
            """INSERT INTO director_agent_pending_plans(
               username,session_id,offer_id,plan_digest,input_hash,input_json,
               expected_cost,page_revision,expires_at,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(username,session_id) DO UPDATE SET
                 offer_id=excluded.offer_id,
                 plan_digest=excluded.plan_digest,
                 input_hash=excluded.input_hash,
                 input_json=excluded.input_json,
                 expected_cost=excluded.expected_cost,
                 page_revision=excluded.page_revision,
                 expires_at=excluded.expires_at,
                 created_at=excluded.created_at,
                 updated_at=excluded.updated_at""",
            (username, session_id, plan["offer_id"], digest, input_hash,
             request_json, int(plan["expected_cost"]), plan["page_revision"],
             expires_at, stamp, stamp),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    saved = dict(plan)
    saved.update(plan_digest=digest, expires_at=expires_at)
    return saved


def _pending_plan_confirmation(db_factory, username, request, now=None):
    """Resolve the exact confirmation command against one durable plan."""
    stamp = int(time.time() if now is None else now)
    connection = db_factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_pending_plan_table(connection)
        row = connection.execute(
            "SELECT * FROM director_agent_pending_plans "
            "WHERE username=? AND session_id=?",
            (username, request["session_id"]),
        ).fetchone()
        if not row:
            connection.commit()
            return {"state": "missing"}
        try:
            cli_input = json.loads(row["input_json"])
        except (TypeError, ValueError):
            cli_input = None
        valid_input = (
            isinstance(cli_input, dict)
            and set(cli_input) == {
                "request_id", "topic", "selling_points", "style",
                "duration", "platform",
            }
            and cli_input.get("request_id") == row["offer_id"]
        )
        expected_digest = (_pending_plan_digest(
            request["session_id"], row["offer_id"], cli_input,
            row["expected_cost"], row["page_revision"],
        ) if valid_input else "")
        if (not valid_input or row["plan_digest"] != expected_digest
                or hashlib.sha256(row["input_json"].encode("utf-8")).hexdigest()
                != row["input_hash"]):
            connection.execute(
                "DELETE FROM director_agent_pending_plans "
                "WHERE username=? AND session_id=?",
                (username, request["session_id"]),
            )
            connection.commit()
            return {"state": "invalid"}
        if int(row["expires_at"]) <= stamp:
            connection.execute(
                "DELETE FROM director_agent_pending_plans "
                "WHERE username=? AND session_id=?",
                (username, request["session_id"]),
            )
            connection.commit()
            return {"state": "expired"}
        context = request["page_context"]
        current_input = {
            "request_id": row["offer_id"],
            "topic": context.get("topic") or "",
            "selling_points": context.get("selling_points") or "",
            "style": context.get("style") or "",
            "duration": context.get("duration") or "",
            "platform": context.get("platform") or "",
        }
        current_hash = hashlib.sha256(
            _canonical(current_input).encode("utf-8")
        ).hexdigest()
        parameters_valid = (
            context.get("mode") == "write"
            and bool(current_input["topic"])
            and all(current_input[name] in OPTION_VALUES[name]
                    for name in ("style", "duration", "platform"))
        )
        if (request["page_revision"] != row["page_revision"]
                or not parameters_valid or current_hash != row["input_hash"]):
            connection.execute(
                "DELETE FROM director_agent_pending_plans "
                "WHERE username=? AND session_id=?",
                (username, request["session_id"]),
            )
            connection.commit()
            return {"state": "changed"}
        if not director_cli.production_is_available():
            connection.commit()
            return {"state": "unavailable"}
        current_cost = _copy_cost(cli_input)
        if current_cost <= 0:
            connection.commit()
            return {"state": "unavailable"}
        if current_cost != int(row["expected_cost"]):
            new_digest = _pending_plan_digest(
                request["session_id"], row["offer_id"], cli_input,
                current_cost, row["page_revision"],
            )
            connection.execute(
                """UPDATE director_agent_pending_plans
                   SET expected_cost=?,plan_digest=?,expires_at=?,updated_at=?
                   WHERE username=? AND session_id=? AND plan_digest=?""",
                (current_cost, new_digest, stamp + OFFER_TTL_SECONDS, stamp,
                 username, request["session_id"], row["plan_digest"]),
            )
            connection.commit()
            return {"state": "price_changed", "expected_cost": current_cost}
        connection.commit()
        return {
            "state": "ready",
            "offer": {
                "offer_id": row["offer_id"], "kind": "script",
                "expected_cost": int(row["expected_cost"]),
                "requires_confirmation": True,
                "page_revision": row["page_revision"],
                "input": cli_input,
                "summary": {
                    "topic": cli_input["topic"],
                    "style": cli_input["style"],
                    "duration": cli_input["duration"],
                    "platform": cli_input["platform"],
                },
            },
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _token_hash(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _issue_production_offer(db_factory, username, offer, page_revision, now=None):
    """Persist one server-issued confirmation capability for the rendered card."""
    stamp = int(time.time() if now is None else now)
    token = secrets.token_urlsafe(32)
    input_hash = hashlib.sha256(
        _canonical(offer["input"]).encode("utf-8")
    ).hexdigest()
    plan_digest = hashlib.sha256(_canonical({
        "page_revision": page_revision,
        "input": offer["input"],
        "summary": offer["summary"],
    }).encode("utf-8")).hexdigest()
    expires_at = stamp + OFFER_TTL_SECONDS
    connection = db_factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_offer_table(connection)
        connection.execute(
            """INSERT INTO director_agent_offers(
               username,offer_id,plan_digest,input_hash,expected_cost,
               token_hash,expires_at,confirmed_at,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,NULL,?,?)
               ON CONFLICT(username,offer_id) DO UPDATE SET
                 plan_digest=excluded.plan_digest,
                 input_hash=excluded.input_hash,
                 expected_cost=excluded.expected_cost,
                 token_hash=excluded.token_hash,
                 expires_at=excluded.expires_at,
                 confirmed_at=NULL,
                 updated_at=excluded.updated_at""",
            (username, offer["offer_id"], plan_digest, input_hash,
             int(offer["expected_cost"]), _token_hash(token), expires_at,
             stamp, stamp),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    issued = dict(offer)
    issued.update({
        "plan_digest": plan_digest,
        "quote_token": token,
        "expires_at": expires_at,
    })
    return issued


def _claim_production_offer(connection, username, offer_id, cli_input,
                            expected_cost, plan_digest, quote_token, stamp,
                            before_attempt_insert=None):
    """Validate the offer and create its durable attempt in one transaction."""
    input_hash = hashlib.sha256(_canonical(cli_input).encode("utf-8")).hexdigest()
    request_json = _canonical(cli_input)
    _ensure_offer_table(connection)
    _ensure_production_table(connection)
    offer = connection.execute(
        "SELECT * FROM director_agent_offers WHERE username=? AND offer_id=?",
        (username, offer_id),
    ).fetchone()
    if (not offer or offer["plan_digest"] != plan_digest
            or offer["input_hash"] != input_hash
            or int(offer["expected_cost"]) != int(expected_cost)
            or not secrets.compare_digest(
                str(offer["token_hash"]), _token_hash(quote_token))):
        raise DirectorOfferError(
            "生产确认单不是服务器签发或内容已经变化",
            "director_offer_invalid",
        )
    attempt = connection.execute(
        "SELECT * FROM director_cli_productions WHERE username=? AND offer_id=?",
        (username, offer_id),
    ).fetchone()
    if attempt and attempt["request_hash"] != input_hash:
        raise DirectorOfferError(
            "同一生产单不能用于不同内容",
            "production_idempotency_conflict", 409,
        )
    if (int(offer["expires_at"]) <= stamp
            and (not offer["confirmed_at"] or not attempt)):
        raise DirectorOfferError(
            "生产确认单已过期，请重新让编导助手报价",
            "director_offer_expired",
        )
    if not attempt:
        connection.execute(
            """UPDATE director_agent_offers SET confirmed_at=?,updated_at=?
               WHERE username=? AND offer_id=? AND token_hash=?""",
            (stamp, stamp, username, offer_id, offer["token_hash"]),
        )
        if before_attempt_insert is not None:
            before_attempt_insert()
        connection.execute(
            """INSERT INTO director_cli_productions(
               username,offer_id,request_hash,input_json,expected_cost,
               state,created_at,updated_at
               ) VALUES(?,?,?,?,?,'preparing',?,?)""",
            (username, offer_id, input_hash, request_json,
             expected_cost, stamp, stamp),
        )
        attempt = connection.execute(
            "SELECT * FROM director_cli_productions WHERE username=? AND offer_id=?",
            (username, offer_id),
        ).fetchone()
    elif not offer["confirmed_at"]:
        connection.execute(
            """UPDATE director_agent_offers SET confirmed_at=?,updated_at=?
               WHERE username=? AND offer_id=? AND token_hash=?""",
            (stamp, stamp, username, offer_id, offer["token_hash"]),
        )
    return attempt


def _rotate_production_offer(db_factory, username, offer_id, plan_digest,
                             cli_input, expected_cost, old_quote_token,
                             now=None):
    """Expire the clicked card and refresh the pending plan after repricing."""
    stamp = int(time.time() if now is None else now)
    input_hash = hashlib.sha256(_canonical(cli_input).encode("utf-8")).hexdigest()
    connection = db_factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_offer_table(connection)
        _ensure_pending_plan_table(connection)
        cursor = connection.execute(
            """UPDATE director_agent_offers
               SET expected_cost=?,expires_at=?,confirmed_at=NULL,updated_at=?
               WHERE username=? AND offer_id=? AND plan_digest=? AND input_hash=?
                 AND token_hash=? AND expires_at>?""",
            (int(expected_cost), stamp, stamp,
             username, offer_id, plan_digest, input_hash,
             _token_hash(old_quote_token), stamp),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise DirectorOfferError(
                "生产确认单已被另一个请求刷新，请使用最新确认单",
                "director_offer_refreshed", 409,
            )
        pending = connection.execute(
            "SELECT * FROM director_agent_pending_plans "
            "WHERE username=? AND offer_id=? AND input_hash=?",
            (username, offer_id, input_hash),
        ).fetchone()
        if pending:
            pending_digest = _pending_plan_digest(
                pending["session_id"], offer_id, cli_input, expected_cost,
                pending["page_revision"],
            )
            connection.execute(
                """UPDATE director_agent_pending_plans
                   SET expected_cost=?,plan_digest=?,expires_at=?,updated_at=?
                   WHERE username=? AND session_id=? AND plan_digest=?""",
                (int(expected_cost), pending_digest,
                 stamp + OFFER_TTL_SECONDS, stamp, username,
                 pending["session_id"], pending["plan_digest"]),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"pending_updated": bool(pending)}


def _normalize_production_request(value):
    if (not isinstance(value, dict)
            or set(value) != {"offer_id", "input", "expected_cost",
                              "plan_digest", "quote_token"}):
        raise ValueError("生产确认参数无效")
    offer_id = _text(value.get("offer_id"), 96, "生产单号")
    if not _PRODUCTION_ID_RE.fullmatch(offer_id):
        raise ValueError("生产单号无效")
    raw = value.get("input")
    allowed = {"request_id", "topic", "selling_points", "style", "duration", "platform"}
    if not isinstance(raw, dict) or set(raw) != allowed:
        raise ValueError("生产内容格式无效")
    if raw.get("request_id") != offer_id:
        raise ValueError("生产单与请求标识不一致")
    cli_input = {
        "request_id": offer_id,
        "topic": _text(raw.get("topic"), 1000, "选题"),
        "selling_points": _text(raw.get("selling_points"), 2000, "核心卖点"),
        "style": _text(raw.get("style"), 40, "风格"),
        "duration": _text(raw.get("duration"), 20, "时长"),
        "platform": _text(raw.get("platform"), 40, "平台"),
    }
    if not cli_input["topic"]:
        raise ValueError("请先确认脚本选题")
    for field in ("style", "duration", "platform"):
        if cli_input[field] not in OPTION_VALUES[field]:
            raise ValueError("生产选项无效")
    expected_cost = value.get("expected_cost")
    if (isinstance(expected_cost, bool) or not isinstance(expected_cost, int)
            or not 1 <= expected_cost <= 10000):
        raise ValueError("预期点数无效")
    plan_digest = _text(value.get("plan_digest"), 64, "生产方案摘要")
    quote_token = _text(value.get("quote_token"), 4096, "生产确认凭证")
    if not re.fullmatch(r"[a-f0-9]{64}", plan_digest):
        raise ValueError("生产方案摘要无效")
    if not re.fullmatch(r"[A-Za-z0-9._-]{20,4096}", quote_token):
        raise ValueError("生产确认凭证无效")
    return offer_id, cli_input, expected_cost, plan_digest, quote_token


def produce_script(db_factory, username, value, now=None, before_link=None,
                   before_attempt_insert=None):
    """Quote and confirm one durable CLI production request.

    ``client_request_id`` is also the content-service Idempotency-Key, so a
    retry after an expired quote or a lost HTTP response still resolves the
    original paid job instead of creating another one.
    """
    username = _text(username, 160, "认证账号")
    if not username:
        raise ValueError("编导生产缺少认证账号")
    offer_id, cli_input, expected_cost, plan_digest, quote_token = (
        _normalize_production_request(value)
    )
    confirmation_token = quote_token
    request_hash = hashlib.sha256(_canonical(cli_input).encode("utf-8")).hexdigest()
    lock = _production_lock(username, offer_id)
    with lock:
        stamp = int(time.time() if now is None else now)
        connection = db_factory()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _claim_production_offer(
                connection, username, offer_id, cli_input, expected_cost,
                plan_digest, quote_token, stamp,
                before_attempt_insert=before_attempt_insert,
            )
            if row and row["state"] == "linked" and row["job_id"]:
                job_id = int(row["job_id"])
                linked = connection.execute(
                    "SELECT id,username,kind FROM jobs WHERE id=?", (job_id,),
                ).fetchone()
                if (not linked or linked["username"] != username
                        or linked["kind"] != "copy"):
                    raise RuntimeError("linked Director CLI job is invalid")
                connection.commit()
                return 200, {
                    "job_id": job_id, "cost": int(row["quoted_cost"] or expected_cost),
                    "points_left": row["points_left"], "recovered": True,
                }
            connection.execute(
                "UPDATE director_cli_productions SET expected_cost=?,updated_at=? "
                "WHERE username=? AND offer_id=?",
                (expected_cost, stamp, username, offer_id),
            )
            quote_token = str(row["quote_token"] or "")
            quoted_cost = int(row["quoted_cost"] or 0)
            quote_expires_at = int(row["quote_expires_at"] or 0)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        if not quote_token or quote_expires_at <= stamp + 3:
            try:
                quote = director_cli.quote_script(username, cli_input)
            except director_cli.DirectorCLIError as error:
                return 502, {
                    "detail": str(error), "code": "director_cli_quote_failed",
                    "retry_after_ms": 1500,
                }
            quote_token = quote["quote_token"]
            quoted_cost = int(quote["cost"])
            quote_expires_at = stamp + int(quote["expires_in"])
            points = quote.get("points")
            connection = db_factory()
            try:
                _ensure_production_table(connection)
                connection.execute(
                    """UPDATE director_cli_productions
                       SET quote_token=?,quoted_cost=?,quote_expires_at=?,
                           state='quoted',points_left=?,updated_at=?,last_error=NULL
                       WHERE username=? AND offer_id=? AND request_hash=?""",
                    (quote_token, quoted_cost, quote_expires_at, points, stamp,
                     username, offer_id, request_hash),
                )
                connection.commit()
            finally:
                connection.close()
        else:
            points = row["points_left"]

        if quoted_cost != expected_cost:
            try:
                refreshed = _rotate_production_offer(
                    db_factory, username, offer_id, plan_digest, cli_input,
                    quoted_cost, confirmation_token, now=stamp,
                )
            except DirectorOfferError as error:
                return error.status, {
                    "detail": str(error), "code": error.code,
                }
            return 409, {
                "detail": "生成价格已变化，原确认已失效，请重新回复确认生成",
                "code": "production_price_changed", "quoted_cost": expected_cost,
                "current_cost": quoted_cost, "points": points,
                "requires_new_text_confirmation": True,
                "pending_plan_updated": bool(refreshed["pending_updated"]),
            }

        connection = db_factory()
        try:
            _ensure_production_table(connection)
            connection.execute(
                "UPDATE director_cli_productions SET state='submitting',updated_at=? "
                "WHERE username=? AND offer_id=? AND request_hash=?",
                (stamp, username, offer_id, request_hash),
            )
            connection.commit()
        finally:
            connection.close()
        try:
            submitted = director_cli.confirm_script(
                username, cli_input, quote_token,
            )
        except director_cli.DirectorCLIError as error:
            connection = db_factory()
            try:
                _ensure_production_table(connection)
                if error.code == "quote_expired":
                    connection.execute(
                        """UPDATE director_cli_productions
                           SET quote_token=NULL,quoted_cost=NULL,quote_expires_at=NULL,
                               state='preparing',last_error=?,updated_at=?
                           WHERE username=? AND offer_id=?""",
                        (str(error)[:220], int(time.time()), username, offer_id),
                    )
                else:
                    connection.execute(
                        "UPDATE director_cli_productions SET last_error=?,updated_at=? "
                        "WHERE username=? AND offer_id=?",
                        (str(error)[:220], int(time.time()), username, offer_id),
                    )
                connection.commit()
            finally:
                connection.close()
            if not error.retryable:
                return error.status, {
                    "detail": str(error), "code": error.code,
                }
            return 502, {
                "detail": str(error),
                "code": ("director_cli_quote_expired" if error.code == "quote_expired"
                         else "director_cli_submit_retryable"),
                "retry_after_ms": 1500,
            }
        job_id = int(submitted["job_id"])
        if before_link is not None:
            before_link(job_id)
        connection = db_factory()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_production_table(connection)
            linked = connection.execute(
                "SELECT id,username,kind FROM jobs WHERE id=?", (job_id,),
            ).fetchone()
            if (not linked or linked["username"] != username or linked["kind"] != "copy"):
                raise RuntimeError("Director CLI returned an invalid customer job")
            connection.execute(
                """UPDATE director_cli_productions
                   SET state='linked',job_id=?,points_left=?,updated_at=?,last_error=NULL
                   WHERE username=? AND offer_id=? AND request_hash=?""",
                (job_id, submitted.get("points_left"), int(time.time()),
                 username, offer_id, request_hash),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return 200, {
            "job_id": job_id, "cost": quoted_cost,
            "points_left": submitted.get("points_left"), "recovered": False,
        }


def _active_job_status(value):
    status = _text(value, 24, "任务状态")
    if status not in {"idle", "pending", "running", "completed", "failed"}:
        raise ValueError("任务状态无效")
    return status


def _script_page_context(value):
    allowed = {
        "page", "path", "mode", "topic", "selling_points", "style",
        "duration", "platform", "has_script", "scene_count", "has_breakdown",
        "breakdown_scene_count", "breakdown_url", "breakdown_tool",
        "has_reverse_prompt", "active_job_status",
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("页面上下文格式无效")
    if value.get("path") not in {
        "/workbench/script", "/workbench/script.html",
    }:
        raise ValueError("页面上下文不属于黄雀编导")
    mode = _text(value.get("mode"), 16, "编导模式")
    if mode not in SCRIPT_MODES:
        raise ValueError("编导模式无效")
    breakdown_tool = _text(value.get("breakdown_tool") or "scenes", 24, "拆解工具")
    if breakdown_tool not in BREAKDOWN_TOOLS:
        raise ValueError("拆解工具无效")
    for name in ("has_script", "has_breakdown"):
        if not isinstance(value.get(name), bool):
            raise ValueError("页面状态格式无效")
    has_reverse_prompt = value.get("has_reverse_prompt", False)
    if not isinstance(has_reverse_prompt, bool):
        raise ValueError("反推结果状态格式无效")
    counts = {}
    for name in ("scene_count", "breakdown_scene_count"):
        count = value.get(name)
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 100:
            raise ValueError("分镜数量无效")
        counts[name] = count
    return {
        "page": "script", "path": value["path"], "mode": mode,
        "topic": _text(value.get("topic"), 1000, "选题"),
        "selling_points": _text(value.get("selling_points"), 2000, "核心卖点"),
        "style": _text(value.get("style"), 40, "风格"),
        "duration": _text(value.get("duration"), 20, "时长"),
        "platform": _text(value.get("platform"), 40, "平台"),
        "has_script": value["has_script"], "scene_count": counts["scene_count"],
        "has_breakdown": value["has_breakdown"],
        "breakdown_scene_count": counts["breakdown_scene_count"],
        "breakdown_url": _text(value.get("breakdown_url"), 2000, "拆解链接"),
        "breakdown_tool": breakdown_tool,
        "has_reverse_prompt": has_reverse_prompt,
        "active_job_status": _active_job_status(value.get("active_job_status")),
    }


def _digital_human_page_context(value):
    allowed = {
        "page", "path", "mode", "narration_mode", "script_text",
        "script_length", "has_portrait", "has_video_source", "has_voice_source",
        "has_drive_audio", "customer_material_count", "consent_confirmed",
        "precision_template", "has_result", "active_job_status", "guide_contract",
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("页面上下文格式无效")
    if value.get("path") not in {
        "/workbench/digital-human-oneclick",
        "/workbench/digital-human-oneclick.html",
    }:
        raise ValueError("页面上下文不属于数字人一键生成")
    mode = _text(value.get("mode"), 16, "数字人模式")
    if mode not in DIGITAL_HUMAN_MODES:
        raise ValueError("数字人模式无效")
    narration_mode = _text(value.get("narration_mode") or "text", 16, "口播驱动方式")
    if narration_mode not in {"text", "audio"}:
        raise ValueError("口播驱动方式无效")
    script_text = _text(value.get("script_text"), 6000, "数字人口播文案")
    script_length = value.get("script_length")
    if (isinstance(script_length, bool) or not isinstance(script_length, int)
            or not 0 <= script_length <= 6000):
        raise ValueError("数字人口播文案长度无效")
    material_count = value.get("customer_material_count")
    if (isinstance(material_count, bool) or not isinstance(material_count, int)
            or not 0 <= material_count <= 6):
        raise ValueError("客户参考图数量无效")
    for name in (
        "has_portrait", "has_video_source", "has_voice_source",
        "has_drive_audio", "consent_confirmed", "has_result",
    ):
        if not isinstance(value.get(name), bool):
            raise ValueError("数字人页面状态格式无效")
    template = _text(value.get("precision_template"), 40, "Precision 模板")
    if template and template not in OPTION_VALUES["precision_template"]:
        raise ValueError("Precision 模板无效")
    guide_contract = _text(value.get("guide_contract"), 80, "数字人引导契约")
    if guide_contract != DIGITAL_HUMAN_GUIDE_CONTRACT:
        raise ValueError("数字人引导契约无效")
    return {
        "page": "digital_human_oneclick", "path": value["path"],
        "guide_contract": DIGITAL_HUMAN_GUIDE_CONTRACT, "mode": mode,
        "narration_mode": narration_mode, "script_text": script_text,
        "script_length": len(script_text), "has_portrait": value["has_portrait"],
        "has_video_source": value["has_video_source"],
        "has_voice_source": value["has_voice_source"],
        "has_drive_audio": value["has_drive_audio"],
        "customer_material_count": material_count,
        "consent_confirmed": value["consent_confirmed"],
        "precision_template": template, "has_result": value["has_result"],
        "active_job_status": _active_job_status(value.get("active_job_status")),
    }


def _page_context(value):
    if not isinstance(value, dict):
        raise ValueError("页面上下文格式无效")
    if value.get("page") == "script":
        return _script_page_context(value)
    if value.get("page") == "digital_human_oneclick":
        return _digital_human_page_context(value)
    raise ValueError("页面上下文不属于黄雀编导")


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    allowed = {
        "prompt", "session_id", "page_revision", "page_context", "history",
        "source_page", "provider", "quoted_cost", "qa_operation_id", "qa_run_id",
    }
    if set(payload) - allowed:
        raise ValueError("请求包含不支持的字段")
    prompt = _text(payload.get("prompt"), 6000, "问题")
    session_id = _text(payload.get("session_id"), 80, "会话标识")
    revision = _text(payload.get("page_revision"), 32, "页面版本")
    if not prompt:
        raise ValueError("请输入想了解的问题")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", session_id):
        raise ValueError("会话标识无效")
    if not re.fullmatch(r"[a-f0-9]{8,32}", revision):
        raise ValueError("页面版本无效")
    if payload.get("provider") not in (None, "", "openai_responses"):
        raise ValueError("模型渠道无效")
    history = payload.get("history") or []
    if not isinstance(history, list) or len(history) > MAX_HISTORY:
        raise ValueError("Agent 历史消息超过限制")
    clean_history = []
    for item in history:
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise ValueError("Agent 历史消息格式无效")
        if item.get("role") not in {"user", "assistant"}:
            raise ValueError("Agent 历史消息角色无效")
        content = _text(item.get("content"), 2000, "历史消息")
        if content:
            clean_history.append({"role": item["role"], "content": content})
    page_context = _page_context(payload.get("page_context"))
    source_page = page_context["page"]
    if payload.get("source_page") not in (None, "", source_page):
        raise ValueError("页面来源无效")
    cleaned = {
        "prompt": prompt, "session_id": session_id, "page_revision": revision,
        "page_context": page_context,
        "history": clean_history, "source_page": source_page,
        "provider": "openai_responses", "quoted_cost": payload.get("quoted_cost", 0),
    }
    for name in ("qa_operation_id", "qa_run_id"):
        if payload.get(name):
            cleaned[name] = _text(payload[name], 120, "质检标识")
    if cleaned["quoted_cost"] != 0:
        raise ValueError("编导助手当前为免费功能")
    if _contains_media(cleaned):
        raise ValueError("Agent 上下文不能包含媒体数据或 Blob 地址")
    return cleaned


def _responses_chat(request):
    # Keep the existing caller/monkeypatch seam and authenticated job contract.
    from . import core, director_conversation
    provider = provider_config(core.OPENAI_BASE, core.OPENAI_KEY)
    if provider is None:
        raise ValueError("编导助手暂未配置模型服务，请稍后再试")
    api_base, api_key = provider
    protocol = os.environ.get("DIRECTOR_AGENT_API_PROTOCOL", "auto").strip()
    if protocol == "auto":
        protocol = "chat_completions" if MODEL.lower().startswith("deepseek") else "responses"

    def post(path, data, ctype, timeout):
        return _post(path, data, ctype, base=api_base, key=api_key, timeout=timeout)

    return director_conversation.converse(
        request, post=post, model=MODEL, protocol=protocol,
        reasoning_effort=REASONING_EFFORT,
        action_schema=DIRECTOR_AGENT_SCHEMA["properties"]["actions"],
        page_guide=director_cli.page_guide,
    )


def _ensure_page_action_allowed(page, action):
    if action.get("type") == "navigate":
        return
    scope = PAGE_ACTION_SCOPE.get(page) or {}
    kind = action.get("type")
    key = {
        "fill_field": "field", "choose_option": "field",
        "switch_mode": "mode", "focus": "target",
    }.get(kind)
    if not key or action.get(key) not in scope.get(kind, set()):
        raise ValueError("Agent 动作不属于当前页面")


_SCRIPT_TOPIC_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"(?:主题|选题)(?:是|为|定为)(?P<topic>[^，,。；;！？!?]{1,300})"
    r"[，,].{0,30}(?:分镜脚本|分镜|脚本)",
    r"关于(?P<topic>[^，,。；;！？!?]{1,300}?)(?:的)?"
    r"(?:分镜脚本|分镜|脚本)",
    r"以(?P<topic>[^，,。；;！？!?]{1,300}?)(?:作为主题|作为选题|为主题|为选题|为题)"
    r".{0,20}(?:分镜脚本|分镜|脚本)",
    r"(?:围绕|以)(?P<topic>[^，,。；;！？!?]{1,300}?)"
    r"(?:[，,]\s*)?(?:来)?(?:生成|制作|写|做).{0,20}(?:分镜脚本|分镜|脚本)",
    r"(?:给|为)(?!(?:我|咱们|咱俩|自己|大家|大伙|您)"
    r"(?:来|生成|制作|写|做))(?P<topic>[^，,。；;！？!?]{1,300}?)"
    r"(?:来|生成|制作|写|做).{0,20}(?:分镜脚本|分镜|脚本)",
    r"围绕(?P<topic>[^，,。；;！？!?]{1,300}?)(?:的)"
    r"(?:分镜脚本|分镜|脚本)",
    r"按(?P<topic>[^，,。；;！？!?]{1,300}?)(?:这个|该)?选题"
    r"(?:生成|制作|写|做).{0,20}(?:分镜脚本|分镜|脚本)",
    r"(?:做|写|生成|制作|来)(?:一份|一个|个)"
    r"(?P<topic>[^，,。；;！？!?]{1,300}?)(?:的)?"
    r"(?:分镜脚本|分镜|脚本)",
))
_SCRIPT_INVALID_TOPICS = {
    "我", "你", "您", "他", "她", "它", "我们", "咱们", "你们", "他们",
    "自己", "大家", "这个", "那个", "这", "那", "该主题", "该选题",
    "这个主题", "这个选题", "上述主题", "上述选题", "刚才主题", "刚才选题",
    "刚才的主题", "刚才的选题", "前面主题", "前面选题", "前面的主题",
    "前面的选题", "本人", "大伙", "那个主题", "那个选题", "前文主题",
    "前文选题", "刚才说的主题", "刚才说的选题", "分镜", "脚本", "分镜脚本",
    "这个方向", "那个方向", "上面的选题", "上面的主题", "之前的选题",
    "之前的主题", "上述", "前文", "刚才说的", "刚才的", "上面的",
    "前面的", "之前的",
}
_SCRIPT_INVALID_TOPIC_REFERENCE = re.compile(
    r"^(?:我|你|您|他|她|它|我们|咱们|咱俩|你们|他们|大家|大伙|本人|自己|"
    r"这|那|此|该|上述|上面|上次|前述|前面|前文|之前|刚才)"
    r"(?:这|那)?(?:次|个|些)?(?:的|说的)?"
    r"(?:主题|选题|话题|内容|方向|方案)?$"
)
_SCRIPT_TOPIC_MODIFIER_ONLY = re.compile(
    r"^(?:\d+(?:\.\d+)?(?:秒|分钟|分|小时)(?:内|左右)?(?:的)?|"
    r"(?:抖音|快手|视频号|小红书|B站|微博)(?:平台|用)?(?:的)?|"
    r"(?:口播|剧情|测评|种草|专业|幽默)(?:风格)?(?:的)?)$",
    re.IGNORECASE,
)


def _explicit_script_production_request(request):
    """Only the customer's exact current-turn command may open a card."""
    return (
        request["page_context"]["page"] == "script"
        and str(request.get("prompt") or "").strip() == CONFIRM_SCRIPT_PROMPT
    )


def _script_topic_from_prompt(request):
    prompt = str(request.get("prompt") or "").strip()
    for pattern in _SCRIPT_TOPIC_PATTERNS:
        match = pattern.search(prompt)
        if not match:
            continue
        topic = match.group("topic").strip(" \t\r\n，,。；;！？!?：:‘’“”\"'")
        topic = re.sub(r"(?:这个|该)(?:主题|选题)$", "", topic).strip()
        topic = re.sub(r"(?:主题|选题)$", "", topic).strip()
        if (topic and topic not in _SCRIPT_INVALID_TOPICS
                and not _SCRIPT_INVALID_TOPIC_REFERENCE.fullmatch(topic)
                and not _SCRIPT_TOPIC_MODIFIER_ONLY.fullmatch(topic)):
            return _text(topic, FIELD_LIMITS["topic"], "选题")
        return ""
    return ""


def _client_page_revision(page_context):
    """Match script-agent.js digest(JSON.stringify(page_context))."""
    serialized = json.dumps(
        page_context, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    )
    value = 2166136261
    encoded = serialized.encode("utf-16-le", errors="surrogatepass")
    for offset in range(0, len(encoded), 2):
        value ^= encoded[offset] | (encoded[offset + 1] << 8)
        value = (value * 16777619) & 0xffffffff
    return "%08x" % value


def _effective_script_context(request, actions, *, force_write=False,
                              topic_fallback=""):
    effective = dict(request["page_context"])
    for action in actions:
        if (action["type"] == "fill_field"
                and action["field"] in {"topic", "selling_points"}):
            effective[action["field"]] = action["value"]
        elif (action["type"] == "choose_option"
                and action["field"] in {"style", "duration", "platform"}):
            effective[action["field"]] = action["value"]
        elif action["type"] == "switch_mode":
            effective["mode"] = action["mode"]
        elif action["type"] == "navigate":
            return None
    if force_write:
        effective["mode"] = "write"
    if not effective.get("topic") and topic_fallback:
        effective["topic"] = topic_fallback
    return effective


def _copy_cost(cli_input):
    from . import points
    copy_payload = {
        "prompt": cli_input["topic"] + (("\n卖点：" + cli_input["selling_points"])
                                         if cli_input["selling_points"] else ""),
        "format": "script", "style": cli_input["style"],
        "dur": cli_input["duration"], "platform": cli_input["platform"],
        "ctype": "分镜脚本", "source_page": "script",
        "client_request_id": cli_input["request_id"],
    }
    return int(points.cost_of("copy", copy_payload))


def _script_production_offer(request, actions, requested, *, force_write=False,
                             topic_fallback=""):
    """Build a non-billable plan; this function never authorizes production."""
    if not requested or request["page_context"]["page"] != "script":
        return None
    effective = _effective_script_context(
        request, actions, force_write=force_write,
        topic_fallback=topic_fallback,
    )
    if effective is None:
        return None
    if effective.get("mode") != "write" or not effective.get("topic"):
        return None
    if not director_cli.production_is_available():
        return None
    for field, fallback in (("style", "口播"), ("duration", "30s"), ("platform", "抖音")):
        if effective.get(field) not in OPTION_VALUES[field]:
            effective[field] = fallback
    seed = "%s\0%s\0%s\0%s" % (
        request["_username"], request.get("_job_id"), request["session_id"],
        _canonical({key: effective.get(key) for key in (
            "topic", "selling_points", "style", "duration", "platform",
        )}),
    )
    offer_id = "director-production-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    cli_input = {
        "request_id": offer_id,
        "topic": effective["topic"],
        "selling_points": effective.get("selling_points") or "",
        "style": effective["style"],
        "duration": effective["duration"],
        "platform": effective["platform"],
    }
    cost = _copy_cost(cli_input)
    if cost <= 0:
        return None
    return {
        "offer_id": offer_id, "kind": "script", "expected_cost": cost,
        "input": cli_input,
        "summary": {
            "topic": cli_input["topic"], "style": cli_input["style"],
            "duration": cli_input["duration"], "platform": cli_input["platform"],
        },
        "requires_confirmation": True,
        "page_revision": _client_page_revision(effective),
    }


def normalize_model_result(raw, request):
    raw = str(raw or "").strip()
    required_fields = {"content", "stage", "actions", "warnings"}
    data = None
    decoder = json.JSONDecoder(strict=False)
    for match in re.finditer(r"\{", raw):
        try:
            candidate, _ = decoder.raw_decode(raw[match.start():])
        except (TypeError, ValueError):
            continue
        if isinstance(candidate, dict) and required_fields.issubset(candidate):
            data = candidate
            break
    if data is None:
        raise ValueError("编导助手返回格式无效，请重试")
    if (not isinstance(data, dict) or not required_fields.issubset(data)
            or set(data) - (required_fields | {"offer_production"})):
        raise ValueError("编导助手返回了不支持的字段")
    content = _text(data.get("content"), 5000, "Agent 回答")
    stage = _text(data.get("stage"), 20, "当前阶段")
    actions, warnings = data.get("actions"), data.get("warnings")
    if not content or stage not in STAGES:
        raise ValueError("编导助手回答或阶段无效")
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        raise ValueError("编导助手操作数量超过限制")
    if not isinstance(warnings, list) or len(warnings) > 8:
        raise ValueError("编导助手提醒数量超过限制")
    model_offer_requested = data.get("offer_production", False)
    if not isinstance(model_offer_requested, bool):
        raise ValueError("编导助手生产意图无效")
    prepare_requested = bool(
        model_offer_requested
        and request["page_context"]["page"] == "script"
        and not _explicit_script_production_request(request)
    )
    normalized = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise ValueError("编导助手动作格式无效")
        kind = action.get("type")
        item = {"id": "action_%d" % (index + 1), "type": kind}
        if kind == "fill_field":
            if set(action) != {"type", "field", "value", "label"} or action.get("field") not in FIELD_NAMES:
                raise ValueError("预填动作无效")
            value = _text(
                action.get("value"), FIELD_LIMITS[action["field"]], "预填内容")
            if not value:
                raise ValueError("预填内容不能为空")
            item.update(field=action["field"], value=value,
                        label=_text(action.get("label"), 80, "动作名称") or "填入页面")
        elif kind == "choose_option":
            if set(action) != {"type", "field", "value", "label"} or action.get("field") not in OPTION_NAMES:
                raise ValueError("选项动作无效")
            value = _text(action.get("value"), 160, "选项值")
            allowed_values = OPTION_VALUES[action["field"]]
            if value not in allowed_values:
                raise ValueError("选项值无效")
            item.update(field=action["field"], value=value,
                        label=_text(action.get("label"), 80, "动作名称") or "选择选项")
        elif kind == "switch_mode":
            if set(action) != {"type", "mode", "label"} or action.get("mode") not in MODES:
                raise ValueError("切换模式动作无效")
            item.update(mode=action["mode"], label=_text(action.get("label"), 80, "动作名称") or "切换模式")
        elif kind == "focus":
            if set(action) != {"type", "target", "label"} or action.get("target") not in FOCUS_TARGETS:
                raise ValueError("聚焦动作无效")
            item.update(target=action["target"], label=_text(action.get("label"), 80, "动作名称") or "查看这里")
        elif kind == "navigate":
            if set(action) != {"type", "target", "label"} or action.get("target") not in NAV_TARGETS:
                raise ValueError("站内引导动作无效")
            item.update(target=action["target"], label=_text(action.get("label"), 80, "动作名称") or "前往下一步")
        else:
            raise ValueError("编导助手返回了不允许的动作")
        _ensure_page_action_allowed(request["page_context"]["page"], item)
        normalized.append(item)
    topic_fallback = ""
    if prepare_requested:
        prompt_topic = _script_topic_from_prompt(request)
        trusted_topic = prompt_topic or request["page_context"].get("topic") or ""
        normalized = [
            item for item in normalized
            if item["type"] != "navigate"
            and not (item["type"] == "switch_mode" and item["mode"] != "write")
            and not (item["type"] == "fill_field" and item["field"] == "topic")
        ]
        topic_fallback = trusted_topic
        if prompt_topic:
            normalized.insert(0, {
                "id": "action_0", "type": "fill_field", "field": "topic",
                "value": prompt_topic, "label": "填入选题",
            })
            normalized = normalized[:MAX_ACTIONS]
        if (request["page_context"].get("mode") != "write"
                and not any(item["type"] == "switch_mode" for item in normalized)
                and len(normalized) < MAX_ACTIONS):
            normalized.append({
                "id": "action_0", "type": "switch_mode", "mode": "write",
                "label": "切换到写脚本",
            })
        for index, item in enumerate(normalized):
            item["id"] = "action_%d" % (index + 1)
    if any(item["type"] == "navigate" for item in normalized):
        if len(normalized) != 1:
            raise ValueError("站内跳转必须作为独立动作，不能与页面修改同时执行")
    warnings = [_text(item, 300, "Agent 提醒") for item in warnings]
    prepared_plan = _script_production_offer(
        request, normalized, prepare_requested,
        force_write=prepare_requested,
        topic_fallback=topic_fallback,
    )
    if prepare_requested and prepared_plan is None:
        has_topic = bool(request["page_context"].get("topic") or topic_fallback or any(
            item["type"] == "fill_field" and item["field"] == "topic"
            for item in normalized
        ))
        if not has_topic:
            content = "请告诉我这次要生成的分镜脚本主题。"
            warnings.append("还缺少本次分镜脚本的选题。")
        elif not director_cli.production_is_available():
            content = "编导 CLI 生产暂时不可用，请稍后再试。"
            warnings.append("编导 CLI 生产依赖当前不可用。")
        else:
            warnings.append("待确认方案暂未就绪，请补充本次分镜脚本的选题。")
    if prepared_plan is not None:
        summary = prepared_plan["summary"]
        content = (
            "方案已准备好：选题“%s”，规格为 %s · %s · %s，预计扣除 %d 点。"
            "确认信息无误后，请回复：%s。"
            % (summary["topic"], summary["platform"], summary["style"],
               summary["duration"], int(prepared_plan["expected_cost"]),
               CONFIRM_SCRIPT_PROMPT)
        )
    seed = request["session_id"] + request["page_revision"] + raw
    result = {
        "type": "director_agent", "content": content,
        "plan": {
            "plan_id": "plan_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16],
            "page_revision": request["page_revision"], "stage": stage,
            "content": content, "actions": normalized,
            "warnings": [item for item in warnings if item][:8],
            "requires_confirmation": False,
        },
    }
    if prepared_plan is not None:
        result["_pending_production_plan"] = prepared_plan
    return result


def _confirmation_result(request, content, *, production_offer=None,
                         warnings=None):
    seed = request["session_id"] + request["page_revision"] + content
    result = {
        "type": "director_agent", "content": content,
        "plan": {
            "plan_id": "plan_" + hashlib.sha256(
                seed.encode("utf-8")
            ).hexdigest()[:16],
            "page_revision": request["page_revision"],
            "stage": "production", "content": content, "actions": [],
            "warnings": list(warnings or [])[:8],
            "requires_confirmation": False,
        },
    }
    if production_offer is not None:
        result["production_offer"] = production_offer
    return result


def gen_director_agent(payload):
    internal = dict(payload or {})
    username = _text(internal.pop("_username", ""), 160, "认证账号")
    job_id = internal.pop("_job_id", None)
    if not username:
        raise ValueError("编导助手缺少认证账号")
    request = validate_payload(internal)
    request["_username"] = username
    request["_job_id"] = int(job_id) if job_id is not None else 0
    from . import core
    if _explicit_script_production_request(request):
        confirmation = _pending_plan_confirmation(core.jdb, username, request)
        state = confirmation["state"]
        if state == "ready":
            offer = _issue_production_offer(
                core.jdb, username, confirmation["offer"],
                request["page_revision"],
            )
            content = (
                "生产确认单已准备好，预计扣除 %d 点。请再次核对价格和参数，"
                "点击确认后才开始制作。"
                % int(offer["expected_cost"])
            )
            return _confirmation_result(
                request, content, production_offer=offer,
            )
        if state == "price_changed":
            return _confirmation_result(
                request,
                "预计价格已更新为 %d 点，原确认已失效。请核对后重新回复：%s。"
                % (int(confirmation["expected_cost"]), CONFIRM_SCRIPT_PROMPT),
                warnings=["价格变化后必须重新发送固定确认文字。"],
            )
        messages = {
            "missing": "当前会话没有待确认方案。请先告诉我选题、风格、时长和平台，我整理后再回复：确认生成。",
            "expired": "待确认方案已过期。请重新告诉我生产要求，我整理新方案后再回复：确认生成。",
            "changed": "页面参数或版本已经变化，原方案已失效。请让我重新整理方案后再回复：确认生成。",
            "invalid": "待确认方案校验失败，已安全失效。请重新告诉我生产要求。",
            "unavailable": "编导 CLI 生产暂时不可用，没有创建确认单，也不会扣点。请稍后重试。",
        }
        return _confirmation_result(
            request, messages[state], warnings=["没有创建生产确认单。"],
        )
    result = normalize_model_result(_responses_chat(request), request)
    pending_plan = result.pop("_pending_production_plan", None)
    if pending_plan is not None:
        _save_pending_plan(
            core.jdb, username, request["session_id"], pending_plan,
        )
    return result


HANDLERS = {"director_agent": gen_director_agent}
