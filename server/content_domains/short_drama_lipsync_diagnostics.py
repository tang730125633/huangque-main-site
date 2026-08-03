"""Read-only, redacted production diagnostics for lipsync jobs and billing."""

from contextlib import closing
import hashlib
import json
import sqlite3
import time


def _hash(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _redaction_kind(key):
    lowered = str(key or "").lower()
    if lowered.endswith("_hash"):
        return "safe_hash"
    if (
        lowered in {
            "file", "path", "url", "object_key", "cover_key",
        }
        or lowered.endswith(("_file", "_path", "_url"))
    ):
        return "hash"
    if (
        lowered in {"username", "actor", "owner", "updated_by"}
        or lowered.endswith("_username")
        or lowered.startswith(("actor_", "owner_"))
    ):
        return "hash"
    if (
        any(marker in lowered for marker in (
            "token", "secret", "password", "credential",
            "authorization", "cookie",
        ))
        or lowered in {
            "api_key", "access_key", "charge_key", "refund_key",
            "idempotency_key",
        }
    ):
        return "redact"
    if (
        lowered.endswith("_json")
        or lowered in {"payload", "result", "error", "terminal"}
    ):
        return "redact"
    return "recurse"


def _redacted_value(value):
    if isinstance(value, dict):
        return _redact(value)
    if isinstance(value, (list, tuple)):
        return [_redacted_value(item) for item in value]
    return value


def _redact(row):
    result = {}
    for key, value in dict(row).items():
        kind = _redaction_kind(key)
        if kind == "redact":
            result[key] = "[REDACTED]"
        elif kind == "hash":
            result[key + "_hash"] = _hash(value)
        elif kind == "safe_hash":
            result[key] = value
        else:
            result[key] = _redacted_value(value)
    return result


def _rows(conn, sql, args, limit=100):
    return [
        _redact(row) for row in conn.execute(sql + " LIMIT ?", (*args, limit)).fetchall()
    ]


def query(db_factory, filters, *, actor="admin", limit=100):
    if not isinstance(filters, dict):
        filters = {}
    limit = max(1, min(int(limit or 100), 200))
    supported = {
        "project_id", "job_id", "attempt_id", "provider_job_id",
        "version_id", "trace_id",
    }
    selected = {
        key: str(filters.get(key) or "").strip()[:160]
        for key in supported if filters.get(key)
    }
    if not selected:
        raise ValueError("at least one diagnostic identifier is required")
    project_id = selected.get("project_id", "")
    job_id = selected.get("job_id", "")
    attempt_id = selected.get("attempt_id", "")
    provider_job_id = selected.get("provider_job_id", "")
    version_id = selected.get("version_id", "")
    trace_id = selected.get("trace_id", "")
    clauses = []
    args = []
    for key, value in (
        ("project_id", project_id), ("id", job_id),
        ("attempt_id", attempt_id), ("provider_job_id", provider_job_id),
    ):
        if value:
            clauses.append("%s=?" % key)
            args.append(value)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        jobs = _rows(
            conn,
            "SELECT * FROM short_drama_lipsync_jobs WHERE " + (
                " OR ".join(clauses) if clauses else "0"
            ) + " ORDER BY updated_at DESC",
            tuple(args), limit,
        )
        derived_job_ids = [row.get("id") for row in jobs]
        effective_job = job_id or next((item for item in derived_job_ids if item), "")
        attempts = _rows(
            conn,
            "SELECT * FROM short_drama_lipsync_attempts WHERE " + (
                "id=? OR id=(SELECT attempt_id FROM short_drama_lipsync_jobs "
                "WHERE id=?)" if attempt_id or effective_job else "0"
            ) + " ORDER BY updated_at DESC",
            (attempt_id, effective_job) if attempt_id or effective_job else (),
            limit,
        )
        versions = _rows(
            conn,
            "SELECT * FROM short_drama_lipsync_versions WHERE " + (
                "id=? OR job_id=?" if version_id or effective_job else "0"
            ) + " ORDER BY created_at DESC",
            (version_id, effective_job) if version_id or effective_job else (),
            limit,
        )
        events = _rows(
            conn,
            "SELECT * FROM short_drama_lipsync_events WHERE " + (
                "project_id=? OR job_id=? OR attempt_id=? OR version_id=? OR trace_id=?"
                if any((project_id, effective_job, attempt_id, version_id, trace_id))
                else "0"
            ) + " ORDER BY created_at DESC",
            (
                project_id, effective_job, attempt_id, version_id, trace_id
            ) if any((project_id, effective_job, attempt_id, version_id, trace_id)) else (),
            limit,
        )
        conn.execute(
            "INSERT INTO short_drama_lipsync_rollout_audit "
            "(id,action,actor,target,reason,incident_id,before_json,after_json,created_at) "
            "VALUES (lower(hex(randomblob(16))),'diagnostics.queried',?,?,?,?,?,?,?)",
            (
                str(actor or "admin")[:80], _hash(json.dumps(selected, sort_keys=True)),
                "production_diagnostics", "", "{}",
                json.dumps({"filters": sorted(selected)}, sort_keys=True),
                int(time.time()),
            ),
        )
        conn.commit()
    return {
        "filters": {key: _hash(value) for key, value in selected.items()},
        "jobs": jobs,
        "attempts": attempts,
        "versions": versions,
        "events": events,
    }
