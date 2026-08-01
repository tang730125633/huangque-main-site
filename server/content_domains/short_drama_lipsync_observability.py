"""Structured events, health metrics and alert evaluation for lipsync rollout."""

from contextlib import closing
import hashlib
import json
import sqlite3
import time


_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_lipsync_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_name TEXT NOT NULL,
  severity TEXT NOT NULL,
  project_id TEXT NOT NULL DEFAULT '',
  job_id TEXT NOT NULL DEFAULT '',
  attempt_id TEXT NOT NULL DEFAULT '',
  version_id TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT '',
  actor_hash TEXT NOT NULL DEFAULT '',
  cohort TEXT NOT NULL DEFAULT '',
  config_version INTEGER NOT NULL DEFAULT 0,
  trace_id TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lipsync_events_created
  ON short_drama_lipsync_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lipsync_events_project
  ON short_drama_lipsync_events(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lipsync_events_job
  ON short_drama_lipsync_events(job_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS lipsync_job_event_insert
AFTER INSERT ON short_drama_lipsync_jobs
BEGIN
  INSERT INTO short_drama_lipsync_events(
    event_name,severity,project_id,job_id,attempt_id,provider,detail_json,created_at
  ) VALUES(
    'lipsync.job.' || NEW.state,
    CASE WHEN NEW.state IN ('failed','manual_review') THEN 'error' ELSE 'info' END,
    NEW.project_id,NEW.id,NEW.attempt_id,NEW.provider,'{}',NEW.created_at
  );
END;

CREATE TRIGGER IF NOT EXISTS lipsync_job_event_state
AFTER UPDATE OF state ON short_drama_lipsync_jobs
WHEN OLD.state <> NEW.state
BEGIN
  INSERT INTO short_drama_lipsync_events(
    event_name,severity,project_id,job_id,attempt_id,provider,detail_json,created_at
  ) VALUES(
    'lipsync.job.' || NEW.state,
    CASE WHEN NEW.state IN ('failed','manual_review') THEN 'error' ELSE 'info' END,
    NEW.project_id,NEW.id,NEW.attempt_id,NEW.provider,
    json_object('from_state',OLD.state,'to_state',NEW.state),NEW.updated_at
  );
END;

CREATE TRIGGER IF NOT EXISTS lipsync_attempt_event_state
AFTER UPDATE OF state ON short_drama_lipsync_attempts
WHEN OLD.state <> NEW.state
BEGIN
  INSERT INTO short_drama_lipsync_events(
    event_name,severity,attempt_id,detail_json,created_at
  ) VALUES(
    'lipsync.billing.' || NEW.state,
    CASE WHEN NEW.state IN ('manual_review','refund_pending') THEN 'warning' ELSE 'info' END,
    NEW.id,json_object('from_state',OLD.state,'to_state',NEW.state),NEW.updated_at
  );
END;

CREATE TRIGGER IF NOT EXISTS lipsync_version_event_insert
AFTER INSERT ON short_drama_lipsync_versions
BEGIN
  INSERT INTO short_drama_lipsync_events(
    event_name,severity,project_id,job_id,version_id,provider,detail_json,created_at
  ) VALUES(
    'lipsync.version.created','info',NEW.project_id,NEW.job_id,NEW.id,
    NEW.provider,'{}',NEW.created_at
  );
END;
"""


def init_db(db_factory):
    with closing(db_factory()) as conn:
        try:
            conn.executescript(_SCHEMA)
        except sqlite3.OperationalError as exc:
            # The admin service may start before the content service has
            # installed the PR-F tables. The event table is still useful;
            # triggers are installed by the next content-domain init.
            if "no such table" not in str(exc).lower():
                raise
        conn.commit()


def _safe_detail(value):
    if not isinstance(value, dict):
        return {}
    blocked = {
        "authorization", "cookie", "token", "secret", "password",
        "api_key", "provider_key", "input_url", "output_url",
    }
    result = {}
    for key, item in value.items():
        name = str(key or "")[:80]
        lowered = name.lower()
        if lowered in blocked or any(part in lowered for part in ("token", "secret", "password")):
            result[name] = "[REDACTED]"
        elif isinstance(item, (str, int, float, bool)) or item is None:
            result[name] = item if not isinstance(item, str) else item[:500]
        elif isinstance(item, list):
            result[name] = item[:50]
        elif isinstance(item, dict):
            result[name] = _safe_detail(item)
    return result


def emit(
    db_factory, event_name, *, severity="info", project_id="", job_id="",
    attempt_id="", version_id="", provider="", actor="", cohort="",
    config_version=0, trace_id="", detail=None, now=None,
):
    now = int(time.time()) if now is None else int(now)
    actor_hash = (
        hashlib.sha256(str(actor).encode("utf-8")).hexdigest() if actor else ""
    )
    with closing(db_factory()) as conn:
        conn.execute(
            "INSERT INTO short_drama_lipsync_events "
            "(event_name,severity,project_id,job_id,attempt_id,version_id,"
            "provider,actor_hash,cohort,config_version,trace_id,detail_json,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(event_name or "")[:120], str(severity or "info")[:16],
                str(project_id or "")[:160], str(job_id or "")[:160],
                str(attempt_id or "")[:160], str(version_id or "")[:160],
                str(provider or "")[:80], actor_hash, str(cohort or "")[:40],
                int(config_version or 0), str(trace_id or "")[:160],
                json.dumps(_safe_detail(detail or {}), ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        conn.commit()


def _scalar(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return int(row[0] or 0) if row else 0


def health(db_factory, *, window_seconds=3600, now=None):
    now = int(time.time()) if now is None else int(now)
    cutoff = now - max(60, min(int(window_seconds), 86400 * 7))
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        total = _scalar(
            conn,
            "SELECT count(*) FROM short_drama_lipsync_jobs WHERE created_at>=?",
            (cutoff,),
        )
        succeeded = _scalar(
            conn,
            "SELECT count(*) FROM short_drama_lipsync_jobs "
            "WHERE created_at>=? AND state='succeeded'",
            (cutoff,),
        )
        failed = _scalar(
            conn,
            "SELECT count(*) FROM short_drama_lipsync_jobs "
            "WHERE created_at>=? AND state IN ('failed','cancelled')",
            (cutoff,),
        )
        active = _scalar(
            conn,
            "SELECT count(*) FROM short_drama_lipsync_jobs "
            "WHERE state IN ('prepared','queued','running','cancel_pending')",
        )
        unsettled = _scalar(
            conn,
            "SELECT count(*) FROM short_drama_lipsync_attempts "
            "WHERE state IN ('accepted','charged','linked','refund_pending')",
        )
        oldest = conn.execute(
            "SELECT min(updated_at) FROM short_drama_lipsync_jobs "
            "WHERE state IN ('prepared','queued','running','cancel_pending')"
        ).fetchone()
        oldest_age = max(0, now - int(oldest[0])) if oldest and oldest[0] else 0
        event_errors = _scalar(
            conn,
            "SELECT count(*) FROM short_drama_lipsync_events "
            "WHERE created_at>=? AND severity IN ('error','critical')",
            (cutoff,),
        )
    completed = succeeded + failed
    success_rate = round(succeeded / completed, 4) if completed else None
    alerts = []
    if completed >= 5 and success_rate is not None and success_rate < 0.8:
        alerts.append({"code": "lipsync_success_rate_low", "severity": "critical"})
    if oldest_age > 900:
        alerts.append({"code": "lipsync_queue_stalled", "severity": "critical"})
    if unsettled > 0:
        alerts.append({"code": "lipsync_unsettled_attempts", "severity": "warning"})
    return {
        "ok": not any(item["severity"] == "critical" for item in alerts),
        "window_seconds": max(60, min(int(window_seconds), 86400 * 7)),
        "metrics": {
            "submitted": total,
            "succeeded": succeeded,
            "failed": failed,
            "active": active,
            "unsettled_attempts": unsettled,
            "oldest_active_age_seconds": oldest_age,
            "error_events": event_errors,
            "success_rate": success_rate,
        },
        "alerts": alerts,
        "generated_at": now,
    }
