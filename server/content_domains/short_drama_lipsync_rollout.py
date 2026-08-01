"""Fail-closed rollout and provider policy for production lipsync traffic."""

from contextlib import closing
import hashlib
import json
import os
import sqlite3
import time

from . import feature_flags


FEATURE = "short_drama_lipsync_v1"
BLOCKED_WHEN_PAUSED = {
    "quote", "create", "retry", "select", "lock",
    "preview", "final_quote", "export", "completion",
}
DEFAULT_CONFIG = {
    "enabled": False,
    "kill_switch": True,
    "percentage": 0,
    "allow_users": [],
    "allow_projects": [],
    "deny_users": [],
    "deny_projects": [],
    "provider_policy": {"allowed": [], "weights": {}},
    "config_version": 0,
    "provider_policy_version": 0,
}
MANUAL_REFUND_SAFE_JOB_STATES = {"failed", "cancelled"}
MANUAL_REFUNDABLE_ATTEMPT_STATES = {
    "charged", "linked", "failed", "refund_pending",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_lipsync_rollout_configs (
  feature TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
  kill_switch INTEGER NOT NULL DEFAULT 1 CHECK (kill_switch IN (0,1)),
  percentage INTEGER NOT NULL DEFAULT 0 CHECK (percentage BETWEEN 0 AND 100),
  allow_users_json TEXT NOT NULL DEFAULT '[]',
  allow_projects_json TEXT NOT NULL DEFAULT '[]',
  deny_users_json TEXT NOT NULL DEFAULT '[]',
  deny_projects_json TEXT NOT NULL DEFAULT '[]',
  provider_policy_json TEXT NOT NULL DEFAULT '{}',
  config_version INTEGER NOT NULL DEFAULT 1,
  provider_policy_version INTEGER NOT NULL DEFAULT 1,
  updated_by TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS short_drama_lipsync_rollout_decisions (
  project_id TEXT PRIMARY KEY,
  actor_hash TEXT NOT NULL,
  eligible INTEGER NOT NULL CHECK (eligible IN (0,1)),
  cohort TEXT NOT NULL,
  reason TEXT NOT NULL,
  config_version INTEGER NOT NULL,
  provider_policy_version INTEGER NOT NULL,
  decision_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS short_drama_lipsync_provider_controls (
  provider TEXT PRIMARY KEY,
  paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  version INTEGER NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  incident_id TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS short_drama_lipsync_rollout_audit (
  id TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  actor TEXT NOT NULL,
  target TEXT NOT NULL,
  reason TEXT NOT NULL,
  incident_id TEXT NOT NULL DEFAULT '',
  before_json TEXT NOT NULL,
  after_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lipsync_rollout_audit_created
  ON short_drama_lipsync_rollout_audit(created_at DESC);
"""


class RolloutError(RuntimeError):
    def __init__(self, code, message, *, status=503, decision=None):
        super().__init__(message)
        self.code = str(code)
        self.status = int(status)
        self.decision = dict(decision or {})


def init_db(db_factory):
    with closing(db_factory()) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def _json(value, default):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _stable_list(value, name):
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError("%s must be a list with at most 1000 items" % name)
    result = []
    for item in value:
        item = str(item or "").strip()
        if item and item not in result:
            result.append(item[:160])
    return sorted(result)


def _provider_policy(value):
    if not isinstance(value, dict):
        raise ValueError("provider_policy must be an object")
    allowed = _stable_list(value.get("allowed") or [], "provider_policy.allowed")
    raw_weights = value.get("weights") or {}
    if not isinstance(raw_weights, dict):
        raise ValueError("provider_policy.weights must be an object")
    weights = {}
    for provider, weight in raw_weights.items():
        provider = str(provider or "").strip()
        if not provider:
            continue
        try:
            weight = int(weight)
        except (TypeError, ValueError):
            raise ValueError("provider weight must be an integer")
        if weight < 0 or weight > 10000:
            raise ValueError("provider weight is out of range")
        weights[provider[:80]] = weight
    if allowed and any(provider not in allowed for provider in weights):
        raise ValueError("provider weight is not in the allowed set")
    return {"allowed": allowed, "weights": dict(sorted(weights.items()))}


def _row_config(row):
    if not row:
        return dict(DEFAULT_CONFIG)
    return {
        "enabled": bool(row["enabled"]),
        "kill_switch": bool(row["kill_switch"]),
        "percentage": int(row["percentage"]),
        "allow_users": _json(row["allow_users_json"], []),
        "allow_projects": _json(row["allow_projects_json"], []),
        "deny_users": _json(row["deny_users_json"], []),
        "deny_projects": _json(row["deny_projects_json"], []),
        "provider_policy": _json(row["provider_policy_json"], {}),
        "config_version": int(row["config_version"]),
        "provider_policy_version": int(row["provider_policy_version"]),
        "updated_by": row["updated_by"],
        "reason": row["reason"],
        "updated_at": int(row["updated_at"]),
    }


def get_config(db_factory):
    try:
        with closing(db_factory()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM short_drama_lipsync_rollout_configs WHERE feature=?",
                (FEATURE,),
            ).fetchone()
        return _row_config(row)
    except Exception:
        return dict(DEFAULT_CONFIG)


def _audit(conn, action, actor, target, reason, before, after, *, incident_id="", now):
    conn.execute(
        "INSERT INTO short_drama_lipsync_rollout_audit "
        "(id,action,actor,target,reason,incident_id,before_json,after_json,created_at) "
        "VALUES (lower(hex(randomblob(16))),?,?,?,?,?,?,?,?)",
        (
            action, actor, target, reason, incident_id,
            _canonical(before), _canonical(after), now,
        ),
    )


def request_manual_refund(
    db_factory, actor, attempt_id, reason, *, incident_id="", now=None
):
    """CAS one safely terminated job into targeted refund recovery."""
    actor = str(actor or "admin").strip()[:80]
    attempt_id = str(attempt_id or "").strip()
    reason = str(reason or "").strip()[:300]
    incident_id = str(incident_id or "").strip()[:120]
    if not attempt_id or not reason:
        raise ValueError("attempt_id and reason are required")
    now = int(time.time()) if now is None else int(now)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempt.state AS attempt_state,attempt.refund_ref,"
            "job.id AS job_id,job.state AS job_state "
            "FROM short_drama_lipsync_attempts attempt "
            "LEFT JOIN short_drama_lipsync_jobs job "
            "ON job.attempt_id=attempt.id WHERE attempt.id=?",
            (attempt_id,),
        ).fetchone()
        if not row:
            raise RolloutError(
                "attempt_not_found", "口型任务扣点记录不存在", status=404
            )
        before = dict(row)
        if row["job_state"] not in MANUAL_REFUND_SAFE_JOB_STATES:
            raise RolloutError(
                "refund_job_not_terminal",
                "活动中或结果不确定的口型任务必须先安全取消并完成对账",
                status=409,
            )
        if row["attempt_state"] == "refunded":
            conn.commit()
            return {
                **before, "attempt_id": attempt_id,
                "state": "refunded", "replayed": True,
            }
        if row["attempt_state"] not in MANUAL_REFUNDABLE_ATTEMPT_STATES:
            raise RolloutError(
                "attempt_not_refundable", "当前扣点状态不可手工退款",
                status=409,
            )
        if row["attempt_state"] == "refund_pending":
            conn.commit()
            return {
                **before, "attempt_id": attempt_id,
                "state": "refund_pending", "replayed": True,
            }
        changed = conn.execute(
            "UPDATE short_drama_lipsync_attempts "
            "SET state='refund_pending',updated_at=? "
            "WHERE id=? AND state=? AND EXISTS ("
            "SELECT 1 FROM short_drama_lipsync_jobs job "
            "WHERE job.attempt_id=short_drama_lipsync_attempts.id "
            "AND job.id=? AND job.state=?)",
            (
                now, attempt_id, row["attempt_state"],
                row["job_id"], row["job_state"],
            ),
        ).rowcount
        if changed != 1:
            raise RolloutError(
                "refund_state_conflict",
                "任务或扣点状态已变化，请刷新后重试",
                status=409,
            )
        after = {
            **before, "attempt_state": "refund_pending",
        }
        _audit(
            conn, "attempt.refund_requested", actor, attempt_id, reason,
            before, after, incident_id=incident_id, now=now,
        )
        conn.commit()
    return {
        **after, "attempt_id": attempt_id,
        "state": "refund_pending", "replayed": False,
    }


def set_config(db_factory, actor, payload, *, expected_version=None, now=None):
    if not isinstance(payload, dict):
        raise ValueError("rollout payload must be an object")
    reason = str(payload.get("reason") or "").strip()[:300]
    if not reason:
        raise ValueError("reason is required")
    now = int(time.time()) if now is None else int(now)
    enabled = bool(payload.get("enabled"))
    kill_switch = bool(payload.get("kill_switch"))
    try:
        percentage = int(payload.get("percentage") or 0)
    except (TypeError, ValueError):
        raise ValueError("percentage must be an integer")
    if percentage < 0 or percentage > 100:
        raise ValueError("percentage must be between 0 and 100")
    cleaned = {
        "enabled": enabled,
        "kill_switch": kill_switch,
        "percentage": percentage,
        "allow_users": _stable_list(payload.get("allow_users") or [], "allow_users"),
        "allow_projects": _stable_list(
            payload.get("allow_projects") or [], "allow_projects"
        ),
        "deny_users": _stable_list(payload.get("deny_users") or [], "deny_users"),
        "deny_projects": _stable_list(
            payload.get("deny_projects") or [], "deny_projects"
        ),
        "provider_policy": _provider_policy(payload.get("provider_policy") or {}),
    }
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        current_row = conn.execute(
            "SELECT * FROM short_drama_lipsync_rollout_configs WHERE feature=?",
            (FEATURE,),
        ).fetchone()
        before = _row_config(current_row)
        current_version = int(before.get("config_version") or 0)
        if expected_version is not None and int(expected_version) != current_version:
            raise RolloutError(
                "rollout_revision_conflict",
                "灰度配置已更新，请刷新后重试",
                status=409,
            )
        config_version = current_version + 1
        policy_changed = cleaned["provider_policy"] != before.get("provider_policy")
        policy_version = int(before.get("provider_policy_version") or 0)
        if policy_changed or policy_version == 0:
            policy_version += 1
        conn.execute(
            "INSERT INTO short_drama_lipsync_rollout_configs "
            "(feature,enabled,kill_switch,percentage,allow_users_json,"
            "allow_projects_json,deny_users_json,deny_projects_json,"
            "provider_policy_json,config_version,provider_policy_version,"
            "updated_by,reason,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(feature) DO UPDATE SET enabled=excluded.enabled,"
            "kill_switch=excluded.kill_switch,percentage=excluded.percentage,"
            "allow_users_json=excluded.allow_users_json,"
            "allow_projects_json=excluded.allow_projects_json,"
            "deny_users_json=excluded.deny_users_json,"
            "deny_projects_json=excluded.deny_projects_json,"
            "provider_policy_json=excluded.provider_policy_json,"
            "config_version=excluded.config_version,"
            "provider_policy_version=excluded.provider_policy_version,"
            "updated_by=excluded.updated_by,reason=excluded.reason,"
            "updated_at=excluded.updated_at",
            (
                FEATURE, int(enabled), int(kill_switch), percentage,
                _canonical(cleaned["allow_users"]),
                _canonical(cleaned["allow_projects"]),
                _canonical(cleaned["deny_users"]),
                _canonical(cleaned["deny_projects"]),
                _canonical(cleaned["provider_policy"]),
                config_version, policy_version, str(actor or "admin")[:80],
                reason, now, now,
            ),
        )
        after = dict(cleaned)
        after.update({
            "config_version": config_version,
            "provider_policy_version": policy_version,
        })
        _audit(
            conn, "rollout.updated", str(actor or "admin")[:80], FEATURE,
            reason, before, after, now=now,
        )
        conn.commit()
    return get_config(db_factory)


def set_provider_paused(
    db_factory, actor, provider, paused, reason, *,
    incident_id="", now=None,
):
    provider = str(provider or "").strip()[:80]
    reason = str(reason or "").strip()[:300]
    incident_id = str(incident_id or "").strip()[:120]
    if not provider or not reason:
        raise ValueError("provider and reason are required")
    now = int(time.time()) if now is None else int(now)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM short_drama_lipsync_provider_controls WHERE provider=?",
            (provider,),
        ).fetchone()
        before = dict(row) if row else {}
        version = int(before.get("version") or 0) + 1
        after = {
            "provider": provider, "paused": bool(paused), "version": version,
            "reason": reason, "incident_id": incident_id,
        }
        conn.execute(
            "INSERT INTO short_drama_lipsync_provider_controls "
            "(provider,paused,version,actor,reason,incident_id,updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
            "paused=excluded.paused,version=excluded.version,actor=excluded.actor,"
            "reason=excluded.reason,incident_id=excluded.incident_id,"
            "updated_at=excluded.updated_at",
            (
                provider, int(bool(paused)), version, str(actor or "admin")[:80],
                reason, incident_id, now,
            ),
        )
        _audit(
            conn, "provider.paused" if paused else "provider.resumed",
            str(actor or "admin")[:80], provider, reason, before, after,
            incident_id=incident_id, now=now,
        )
        conn.commit()
    return after


def provider_controls(db_factory):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM short_drama_lipsync_provider_controls ORDER BY provider"
        ).fetchall()
    return [{**dict(row), "paused": bool(row["paused"])} for row in rows]


def _paused_providers(db_factory):
    try:
        return {
            row["provider"] for row in provider_controls(db_factory)
            if row["paused"]
        }
    except Exception:
        return set()


def _decision_payload(eligible, cohort, reason, config, operation):
    return {
        "feature": FEATURE,
        "eligible": bool(eligible),
        "cohort": str(cohort),
        "reason": str(reason),
        "config_version": int(config.get("config_version") or 0),
        "provider_policy_version": int(
            config.get("provider_policy_version") or 0
        ),
        "operation": str(operation or "read"),
        "provider_policy": dict(config.get("provider_policy") or {}),
    }


def _emit_decision(db_factory, actor, project_id, decision):
    try:
        from . import short_drama_lipsync_observability as observability
        observability.emit(
            db_factory,
            "lipsync.rollout.decision",
            severity="info",
            project_id=project_id,
            actor=actor,
            cohort=decision["cohort"],
            config_version=decision["config_version"],
            detail={
                "eligible": decision["eligible"],
                "reason": decision["reason"],
                "operation": decision["operation"],
                "provider_policy_version": decision["provider_policy_version"],
            },
        )
    except Exception:
        pass


def evaluate(
    db_factory, actor, project_id="", *, operation="read",
    flag_enabled=None, now=None,
):
    actor = str(actor or "").strip()[:160]
    project_id = str(project_id or "").strip()[:160]
    operation = str(operation or "read").strip()
    now = int(time.time()) if now is None else int(now)
    try:
        flag_on = (
            feature_flags.is_enabled(FEATURE)
            if flag_enabled is None else bool(flag_enabled)
        )
    except Exception:
        flag_on = False
    config = get_config(db_factory)
    if not flag_on:
        decision = _decision_payload(
            False, "legacy", "feature_disabled", config, operation
        )
    elif not config.get("enabled"):
        decision = _decision_payload(
            False, "legacy", "rollout_disabled", config, operation
        )
    elif config.get("kill_switch") and operation in BLOCKED_WHEN_PAUSED:
        decision = _decision_payload(
            False, "paused", "kill_switch", config, operation
        )
    elif actor in config.get("deny_users", []) or project_id in config.get(
        "deny_projects", []
    ):
        decision = _decision_payload(
            False, "denied", "denylist", config, operation
        )
    elif actor in config.get("allow_users", []) or project_id in config.get(
        "allow_projects", []
    ):
        decision = _decision_payload(
            True, "internal", "allowlist", config, operation
        )
    else:
        pinned = None
        if project_id:
            try:
                with closing(db_factory()) as conn:
                    conn.row_factory = sqlite3.Row
                    pinned = conn.execute(
                        "SELECT * FROM short_drama_lipsync_rollout_decisions "
                        "WHERE project_id=?",
                        (project_id,),
                    ).fetchone()
            except Exception:
                pinned = None
        if pinned:
            decision = _decision_payload(
                bool(pinned["eligible"]), pinned["cohort"],
                "pinned_decision", config, operation,
            )
            decision["config_version"] = int(pinned["config_version"])
            decision["provider_policy_version"] = int(
                pinned["provider_policy_version"]
            )
        else:
            secret = str(os.environ.get("HQ_SHORT_DRAMA_ROLLOUT_SECRET") or "")
            if not secret:
                decision = _decision_payload(
                    False, "legacy", "server_secret_missing", config, operation
                )
            else:
                identity = project_id or actor
                bucket = int(_hash("%s:%s" % (secret, identity))[:8], 16) % 100
                eligible = bucket < int(config.get("percentage") or 0)
                percentage = int(config.get("percentage") or 0)
                cohort = str(percentage) if eligible and percentage else "legacy"
                decision = _decision_payload(
                    eligible, cohort, "percentage", config, operation
                )
                if project_id:
                    try:
                        decision_hash = _hash(_canonical({
                            "project_id": project_id,
                            "actor_hash": _hash(actor),
                            "eligible": eligible,
                            "cohort": cohort,
                            "config_version": decision["config_version"],
                        }))
                        with closing(db_factory()) as conn:
                            conn.execute(
                                "INSERT OR IGNORE INTO "
                                "short_drama_lipsync_rollout_decisions "
                                "(project_id,actor_hash,eligible,cohort,reason,"
                                "config_version,provider_policy_version,"
                                "decision_hash,created_at,updated_at) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (
                                    project_id, _hash(actor), int(eligible),
                                    cohort, "percentage",
                                    decision["config_version"],
                                    decision["provider_policy_version"],
                                    decision_hash, now, now,
                                ),
                            )
                            conn.commit()
                    except Exception:
                        decision = _decision_payload(
                            False, "legacy", "decision_store_failed",
                            config, operation,
                        )
    policy = dict(decision.get("provider_policy") or {})
    paused = _paused_providers(db_factory)
    policy["paused"] = sorted(paused)
    allowed = list(policy.get("allowed") or [])
    policy["effective_allowed"] = [
        item for item in allowed if item not in paused
    ]
    decision["provider_policy"] = policy
    _emit_decision(db_factory, actor, project_id, decision)
    return decision


def require(db_factory, actor, project_id="", *, operation, provider=""):
    decision = evaluate(db_factory, actor, project_id, operation=operation)
    if not decision["eligible"]:
        code = (
            "rollout_paused"
            if decision["reason"] == "kill_switch"
            else "feature_disabled"
        )
        raise RolloutError(
            code, "真实口型功能当前未向该项目开放",
            status=503, decision=decision,
        )
    provider = str(provider or "").strip()
    if provider:
        policy = decision.get("provider_policy") or {}
        allowed = list(policy.get("effective_allowed") or [])
        configured = list(policy.get("allowed") or [])
        if provider in set(policy.get("paused") or []):
            raise RolloutError(
                "provider_paused", "当前口型 Provider 已暂停接收新任务",
                status=503, decision=decision,
            )
        if configured and provider not in allowed:
            raise RolloutError(
                "provider_not_allowed", "当前口型 Provider 不在生产允许集中",
                status=422, decision=decision,
            )
    return decision


def project_has_lipsync(db_factory, project_id):
    try:
        with closing(db_factory()) as conn:
            row = conn.execute(
                "SELECT 1 FROM short_drama_lipsync_current WHERE project_id=? "
                "UNION SELECT 1 FROM short_drama_lipsync_assembly_plans "
                "WHERE project_id=? LIMIT 1",
                (project_id, project_id),
            ).fetchone()
        return bool(row)
    except Exception:
        return False


def quote_provider(db_factory, quote_id):
    try:
        with closing(db_factory()) as conn:
            row = conn.execute(
                "SELECT provider FROM short_drama_lipsync_quotes WHERE id=?",
                (str(quote_id or ""),),
            ).fetchone()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""


def job_provider(db_factory, job_id):
    try:
        with closing(db_factory()) as conn:
            row = conn.execute(
                "SELECT provider FROM short_drama_lipsync_jobs WHERE id=?",
                (str(job_id or ""),),
            ).fetchone()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""


def require_project_operation(db_factory, actor, project_id, *, operation):
    if not project_has_lipsync(db_factory, project_id):
        return {
            "eligible": False, "cohort": "legacy",
            "reason": "legacy_project", "operation": operation,
        }
    return require(db_factory, actor, project_id, operation=operation)
