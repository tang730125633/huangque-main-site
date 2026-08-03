"""Paid lipsync attempt/job state machine, leases, billing and recovery."""

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

from .short_drama_lipsync_snapshot import canonical_json
from . import (
    short_drama_lipsync_media,
    short_drama_lipsync_observability,
    short_drama_lipsync_versions,
)


LEASE_SECONDS = 45
ACTIVE_STATES = ("prepared", "queued", "running", "cancel_pending")
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "manual_review")
RETRYABLE_ATTEMPT_STATES = ("charged", "linked")
REFUND_SAFE_JOB_STATES = ("failed", "cancelled")


class LipsyncJobError(ValueError):
    def __init__(self, code, message, *, status=400, retry_after_ms=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after_ms = retry_after_ms


class LipsyncResultRetryableError(RuntimeError):
    pass


class LipsyncResultManualReviewError(RuntimeError):
    pass


def jobs_enabled():
    return str(os.environ.get(
        "HQ_SHORT_DRAMA_LIPSYNC_JOBS_ENABLED", "0"
    )).strip() == "1"


def billing_enabled():
    return str(os.environ.get(
        "HQ_SHORT_DRAMA_LIPSYNC_BILLING_ENABLED", "0"
    )).strip() == "1"


def _request_hash(payload):
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _decode(value):
    try:
        result = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _public(conn, job_id, *, replayed=False):
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT job.*,attempt.state AS attempt_state,attempt.cost,"
        "attempt.points_left,attempt.charge_ref,attempt.refund_ref,"
        "attempt.terminal_json FROM short_drama_lipsync_jobs job "
        "JOIN short_drama_lipsync_attempts attempt ON attempt.id=job.attempt_id "
        "WHERE job.id=?",
        (job_id,),
    ).fetchone()
    if not row:
        raise LookupError("lipsync job does not exist")
    item = dict(row)
    item["result"] = _decode(item.pop("result_json"))
    item["error"] = _decode(item.pop("error_json"))
    item["terminal"] = _decode(item.pop("terminal_json"))
    item["replayed"] = bool(replayed)
    state = str(item.get("state") or "")
    attempt_state = str(item.get("attempt_state") or "")
    item["allowed_actions"] = {
        "retry": (
            state in {"prepared", "queued"}
            and attempt_state in {"charged", "linked"}
        ),
        "cancel": state in {
            "prepared", "queued", "running", "cancel_pending"
        },
        "refresh": True,
        "reconcile": attempt_state in {
            "accepted", "charged", "linked", "refund_pending"
        },
    }
    item["terminal_state"] = bool(
        state in TERMINAL_STATES
        and attempt_state not in {"refund_pending", "manual_review"}
    )
    for key in ("lease_token", "lease_owner"):
        item.pop(key, None)
    return item


def prepare(db_factory, actor, owner, payload, idempotency_key, *, now=None):
    if not jobs_enabled():
        raise LipsyncJobError(
            "lipsync_jobs_disabled", "口型付费任务尚未启用", status=503
        )
    if not isinstance(payload, dict) or set(payload) != {
        "project_id", "shot_id", "quote_id", "expected_input_hash"
    }:
        raise LipsyncJobError("invalid_request", "口型任务请求字段不正确")
    key = str(idempotency_key or "").strip()
    if not key:
        raise LipsyncJobError(
            "idempotency_key_required", "必须提供 Idempotency-Key"
        )
    now = int(time.time()) if now is None else int(now)
    identity = {
        "project_id": str(payload["project_id"]),
        "shot_id": str(payload["shot_id"]),
        "quote_id": str(payload["quote_id"]),
        "expected_input_hash": str(payload["expected_input_hash"]),
    }
    request_hash = _request_hash(identity)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT job.id,attempt.request_hash FROM "
            "short_drama_lipsync_attempts attempt "
            "JOIN short_drama_lipsync_jobs job ON job.attempt_id=attempt.id "
            "WHERE attempt.actor_username=? AND attempt.idempotency_key=?",
            (actor, key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise LipsyncJobError(
                    "idempotency_conflict",
                    "同一 Idempotency-Key 不能用于不同口型任务", status=409,
                )
            conn.commit()
            return _public(conn, existing["id"], replayed=True)
        quote = conn.execute(
            "SELECT * FROM short_drama_lipsync_quotes WHERE id=?",
            (identity["quote_id"],),
        ).fetchone()
        if not quote or quote["actor_username"] != actor:
            raise LipsyncJobError("quote_not_found", "口型报价不存在", status=404)
        if (
            quote["owner_username"] != owner
            or quote["project_id"] != identity["project_id"]
            or quote["shot_id"] != identity["shot_id"]
        ):
            raise LipsyncJobError("quote_mismatch", "口型报价不属于当前镜头", status=409)
        if quote["status"] == "consumed":
            raise LipsyncJobError("quote_consumed", "口型报价已使用", status=409)
        if quote["status"] != "issued" or int(quote["expires_at"]) <= now:
            raise LipsyncJobError("quote_expired", "口型报价已过期", status=409)
        if quote["input_hash"] != identity["expected_input_hash"]:
            raise LipsyncJobError(
                "dependency_changed", "口型依赖已经变化", status=409
            )
        cost_data = _decode(quote["cost_json"])
        cost = int(cost_data.get("points") or 0)
        if cost <= 0 or not billing_enabled():
            raise LipsyncJobError(
                "lipsync_billing_disabled", "口型付费账本尚未启用", status=503
            )
        attempt_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        charge_key = "short-drama-lipsync:%s:%s" % (actor, key)
        conn.execute(
            "INSERT INTO short_drama_lipsync_attempts "
            "(id,actor_username,owner_username,quote_id,idempotency_key,"
            "request_hash,charge_key,refund_key,cost,state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'accepted',?,?)",
            (
                attempt_id, actor, owner, quote["id"], key, request_hash,
                charge_key, charge_key + ":refund", cost, now, now,
            ),
        )
        conn.execute(
            "INSERT INTO short_drama_lipsync_jobs "
            "(id,attempt_id,project_id,shot_id,input_hash,provider,state,"
            "next_poll_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,'prepared',?,?,?)",
            (
                job_id, attempt_id, quote["project_id"], quote["shot_id"],
                quote["input_hash"], quote["provider"], now, now, now,
            ),
        )
        conn.execute(
            "UPDATE short_drama_lipsync_quotes SET status='consumed' "
            "WHERE id=? AND status='issued'",
            (quote["id"],),
        )
        conn.commit()
        return _public(conn, job_id)


def charge(db_factory, job_id, ledger, *, now=None):
    now = int(time.time()) if now is None else int(now)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT job.id AS job_id,attempt.id AS attempt_id,"
            "attempt.actor_username,attempt.cost,attempt.charge_key,"
            "attempt.state,attempt.points_left "
            "FROM short_drama_lipsync_jobs job "
            "JOIN short_drama_lipsync_attempts attempt ON attempt.id=job.attempt_id "
            "WHERE job.id=?",
            (job_id,),
        ).fetchone()
    if not row:
        raise LookupError("lipsync job does not exist")
    if row["state"] in ("charged", "linked", "settled"):
        with closing(db_factory()) as conn:
            return _public(conn, job_id, replayed=True)
    if row["state"] != "accepted":
        raise LipsyncJobError("charge_terminal", "口型扣点状态不可继续", status=409)
    try:
        points_left = ledger.deduct(
            row["actor_username"], int(row["cost"]), "短剧口型生成",
            row["charge_key"],
        )
        charge_ref = row["charge_key"]
    except Exception as error:
        transaction = ledger.lookup(row["charge_key"])
        if not transaction and int(getattr(error, "status", 0) or 0) == 402:
            raise LipsyncJobError(
                "insufficient_points",
                str(getattr(error, "detail", None) or "项目点数不足"),
                status=402,
            ) from error
        if not transaction:
            raise LipsyncJobError(
                "charge_unknown", "扣点结果暂时无法确认", status=503,
                retry_after_ms=3000,
            ) from error
        points_left = transaction.get("points")
        charge_ref = transaction.get("id") or row["charge_key"]
    with closing(db_factory()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE short_drama_lipsync_attempts SET state='charged',"
            "charge_ref=?,points_left=?,updated_at=? "
            "WHERE id=? AND state='accepted'",
            (str(charge_ref), points_left, now, row["attempt_id"]),
        ).rowcount
        if changed:
            conn.execute(
                "UPDATE short_drama_lipsync_jobs SET state='queued',"
                "next_poll_at=?,updated_at=? WHERE id=? AND state='prepared'",
                (now, now, job_id),
            )
        conn.commit()
        return _public(conn, job_id, replayed=not bool(changed))


def get(db_factory, owner, job_id):
    with closing(db_factory()) as conn:
        row = conn.execute(
            "SELECT 1 FROM short_drama_lipsync_jobs job "
            "JOIN short_drama_lipsync_attempts attempt ON attempt.id=job.attempt_id "
            "WHERE job.id=? AND attempt.owner_username=?",
            (job_id, owner),
        ).fetchone()
        if not row:
            raise LookupError("lipsync job does not exist")
        return _public(conn, job_id)


def acquire_lease(db_factory, job_id, worker, *, now=None):
    now = int(time.time()) if now is None else int(now)
    token = uuid.uuid4().hex
    with closing(db_factory()) as conn:
        changed = conn.execute(
            "UPDATE short_drama_lipsync_jobs SET lease_token=?,lease_owner=?,"
            "lease_expires_at=?,heartbeat_at=?,updated_at=? WHERE id=? "
            "AND state IN ('queued','running','cancel_pending') "
            "AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
            (token, worker, now + LEASE_SECONDS, now, now, job_id, now),
        ).rowcount
        conn.commit()
    return token if changed else None


def heartbeat(db_factory, job_id, token, *, now=None):
    now = int(time.time()) if now is None else int(now)
    with closing(db_factory()) as conn:
        changed = conn.execute(
            "UPDATE short_drama_lipsync_jobs SET heartbeat_at=?,"
            "lease_expires_at=?,updated_at=? WHERE id=? AND lease_token=?",
            (now, now + LEASE_SECONDS, now, job_id, token),
        ).rowcount
        conn.commit()
    return bool(changed)


def release_lease(db_factory, job_id, token, *, now=None):
    """Yield a live job between durable polling iterations."""
    now = int(time.time()) if now is None else int(now)
    with closing(db_factory()) as conn:
        changed = conn.execute(
            "UPDATE short_drama_lipsync_jobs SET lease_token=NULL,"
            "lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
            "WHERE id=? AND lease_token=? "
            "AND state IN ('queued','running','cancel_pending')",
            (now, job_id, token),
        ).rowcount
        conn.commit()
    return bool(changed)


def _create_with_lease_heartbeat(
    db_factory, job_id, token, provider, request, idempotency_key
):
    """Keep ownership alive while a remote create request is blocking."""
    stopped = threading.Event()

    def keep_alive():
        while not stopped.wait(max(1, LEASE_SECONDS // 3)):
            if not heartbeat(db_factory, job_id, token):
                return

    keeper = threading.Thread(
        target=keep_alive,
        name="lipsync-create-heartbeat",
        daemon=True,
    )
    keeper.start()
    try:
        return provider.create_job(request, idempotency_key)
    finally:
        stopped.set()
        keeper.join(timeout=1)


def process_once(
    db_factory, job_id, provider, token, *, now=None, request_builder=None
):
    now = int(time.time()) if now is None else int(now)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        job = conn.execute(
            "SELECT job.*,attempt.id AS billing_attempt_id,"
            "attempt.idempotency_key FROM short_drama_lipsync_jobs job "
            "JOIN short_drama_lipsync_attempts attempt ON attempt.id=job.attempt_id "
            "WHERE job.id=? AND job.lease_token=?",
            (job_id, token),
        ).fetchone()
    if not job:
        raise LipsyncJobError("lease_lost", "口型任务租约已失效", status=409)
    heartbeat(db_factory, job_id, token, now=now)
    if (
        job["state"] == "cancel_pending"
        and not job["provider_job_id"]
        and not job["provider_create_started_at"]
    ):
        return _finish_job(db_factory, job_id, token, "cancelled", now=now)
    if job["state"] == "cancel_pending" and job["provider_job_id"]:
        if not getattr(provider, "supports_cancel", False):
            return _manual_review(
                db_factory, job_id, token, "provider_cancel_unsupported",
                RuntimeError("provider cancellation is not supported"), now,
            )
        try:
            provider.cancel_job(job["provider_job_id"])
        except Exception:
            heartbeat(db_factory, job_id, token, now=now)
            with closing(db_factory()) as conn:
                conn.execute(
                    "UPDATE short_drama_lipsync_jobs SET next_poll_at=?,"
                    "updated_at=? WHERE id=? AND lease_token=? "
                    "AND state='cancel_pending'",
                    (now + 5, now, job_id, token),
                )
                conn.commit()
                return _public(conn, job_id)
        return _finish_job(db_factory, job_id, token, "cancelled", now=now)
    if not job["provider_job_id"]:
        with closing(db_factory()) as conn:
            changed = conn.execute(
                "UPDATE short_drama_lipsync_jobs SET state=CASE "
                "WHEN state='queued' THEN 'running' ELSE state END,"
                "provider_create_started_at=COALESCE("
                "provider_create_started_at,?),heartbeat_at=?,updated_at=? "
                "WHERE id=? AND lease_token=? "
                "AND state IN ('queued','running','cancel_pending')",
                (now, now, now, job_id, token),
            ).rowcount
            conn.commit()
        if not changed:
            raise LipsyncJobError("lease_lost", "口型任务租约已失效", status=409)
        try:
            provider_request = {
                "project_id": job["project_id"],
                "shot_id": job["shot_id"],
                "input_hash": job["input_hash"],
            }
            if callable(request_builder):
                provider_request = request_builder(job_id, provider_request)
            if not isinstance(provider_request, dict):
                raise RuntimeError("lipsync request builder returned invalid data")
            created = _create_with_lease_heartbeat(
                db_factory, job_id, token, provider,
                provider_request,
                job["idempotency_key"],
            )
        except Exception as error:
            outcome_unknown = (
                isinstance(error, TimeoutError)
                or bool(getattr(error, "outcome_unknown", False))
            )
            if not outcome_unknown:
                return fail_job(
                    db_factory, job_id, token,
                    "provider_create_failed", error, now=now,
                )
            with closing(db_factory()) as conn:
                conn.execute(
                    "UPDATE short_drama_lipsync_jobs SET next_poll_at=?,"
                    "error_json=?,updated_at=? WHERE id=? AND lease_token=?",
                    (
                        now + 5,
                        canonical_json({
                            "code": "provider_create_unknown",
                            "detail": str(error)[:220],
                        }),
                        now, job_id, token,
                    ),
                )
                conn.commit()
                return _public(conn, job_id)
        provider_job_id = str((created or {}).get("job_id") or "")
        if not provider_job_id:
            return _manual_review(
                db_factory, job_id, token, "provider_job_id_missing",
                RuntimeError("provider did not return job id"), now,
            )
        with closing(db_factory()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE short_drama_lipsync_jobs SET provider_job_id=?,"
                "state=CASE WHEN state='cancel_pending' THEN state "
                "ELSE 'running' END,heartbeat_at=?,next_poll_at=?,"
                "error_json='{}',updated_at=? WHERE id=? "
                "AND provider_job_id IS NULL "
                "AND state IN ('running','cancel_pending')",
                (provider_job_id, now, now + 2, now, job_id),
            ).rowcount
            if changed:
                conn.execute(
                    "UPDATE short_drama_lipsync_attempts SET state='linked',"
                    "updated_at=? WHERE id=? AND state='charged'",
                    (now, job["attempt_id"]),
                )
            conn.commit()
            current = _public(conn, job_id)
        if current["state"] == "cancel_pending":
            if not getattr(provider, "supports_cancel", False):
                return _manual_review(
                    db_factory, job_id, token, "provider_cancel_unsupported",
                    RuntimeError("provider cancellation is not supported"), now,
                )
            try:
                provider.cancel_job(provider_job_id)
            except Exception:
                return current
            return _finish_job(
                db_factory, job_id, token, "cancelled", now=now
            )
        return current
    status = provider.get_job(job["provider_job_id"]) or {}
    provider_state = str(status.get("status") or "unknown").lower()
    if provider_state in ("queued", "running", "unknown"):
        with closing(db_factory()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_jobs SET state='running',"
                "progress=?,poll_count=poll_count+1,next_poll_at=?,"
                "heartbeat_at=?,updated_at=? WHERE id=? AND lease_token=?",
                (
                    max(0, min(99, int(status.get("progress") or 0))),
                    now + 2, now, now, job_id, token,
                ),
            )
            conn.commit()
            return _public(conn, job_id)
    if provider_state == "succeeded":
        with closing(db_factory()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_jobs SET state='running',"
                "progress=95,result_json=?,poll_count=poll_count+1,"
                "next_poll_at=NULL,heartbeat_at=?,updated_at=? "
                "WHERE id=? AND lease_token=?",
                (
                    canonical_json({
                        "provider_status": provider_state,
                        "result_ready": True,
                    }),
                    now, now, job_id, token,
                ),
            )
            conn.commit()
            return _public(conn, job_id)
    if provider_state == "cancelled":
        return _finish_job(db_factory, job_id, token, "cancelled", now=now)
    return _finish_job(
        db_factory, job_id, token, "failed",
        error={"code": "provider_failed", "provider_status": provider_state},
        now=now,
    )


def _finish_job(db_factory, job_id, token, state, *, result=None, error=None, now):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE short_drama_lipsync_jobs SET state=?,progress=?,"
            "result_json=?,error_json=?,lease_token=NULL,lease_owner=NULL,"
            "lease_expires_at=NULL,updated_at=? WHERE id=? AND lease_token=? "
            "AND state NOT IN ('succeeded','failed','cancelled','manual_review')",
            (
                state, 100 if state == "succeeded" else 0,
                canonical_json(result or {}), canonical_json(error or {}),
                now, job_id, token,
            ),
        ).rowcount
        if changed:
            attempt_id = conn.execute(
                "SELECT attempt_id FROM short_drama_lipsync_jobs WHERE id=?",
                (job_id,),
            ).fetchone()[0]
            attempt_state = (
                "settled" if state == "succeeded"
                else "manual_review" if state == "manual_review"
                else "refund_pending"
            )
            conn.execute(
                "UPDATE short_drama_lipsync_attempts SET state=?,updated_at=? "
                "WHERE id=? AND state IN ('charged','linked')",
                (attempt_state, now, attempt_id),
            )
        conn.commit()
        return _public(conn, job_id, replayed=not bool(changed))


def _manual_review(db_factory, job_id, token, code, error, now):
    return _finish_job(
        db_factory, job_id, token, "manual_review",
        error={"code": code, "detail": str(error)[:220]}, now=now,
    )


def fail_job(db_factory, job_id, token, code, error, *, now=None):
    now = int(time.time()) if now is None else int(now)
    return _finish_job(
        db_factory, job_id, token, "failed",
        error={"code": str(code), "detail": str(error)[:220]},
        now=now,
    )


def manual_review_job(db_factory, job_id, token, code, error, *, now=None):
    now = int(time.time()) if now is None else int(now)
    return _manual_review(
        db_factory, job_id, token, str(code), error, now
    )


def defer_result(
    db_factory, job_id, token, code, error, *,
    max_attempts=8, now=None,
):
    """Persist a retryable result-stage error without refunding the user."""
    now = int(time.time()) if now is None else int(now)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT result_retry_count FROM short_drama_lipsync_jobs "
            "WHERE id=? AND lease_token=? AND state='running'",
            (job_id, token),
        ).fetchone()
        if not row:
            raise LipsyncJobError(
                "lease_lost", "口型任务租约已失效", status=409
            )
        attempts = int(row["result_retry_count"] or 0) + 1
        conn.execute(
            "UPDATE short_drama_lipsync_jobs SET result_retry_count=?,"
            "error_json=?,next_poll_at=?,heartbeat_at=?,updated_at=? "
            "WHERE id=? AND lease_token=? AND state='running'",
            (
                attempts,
                canonical_json({
                    "code": str(code),
                    "detail": str(error)[:220],
                    "retryable": True,
                    "attempt": attempts,
                }),
                now + min(300, 2 ** min(attempts, 8)),
                now, now, job_id, token,
            ),
        )
        conn.commit()
    if attempts >= max(1, int(max_attempts)):
        return manual_review_job(
            db_factory, job_id, token,
            "result_retry_exhausted", error, now=now,
        )
    with closing(db_factory()) as conn:
        return _public(conn, job_id)


def finalize_result(
    db_factory, job_id, provider, token, *, work_dir, output_root, probe,
    remux=None, now=None,
):
    """Download, validate, publish, version and settle as one recoverable step."""
    now = int(time.time()) if now is None else int(now)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT job.*,quote.media_spec_json,quote.cost_json,"
            "quote.face_target_json,quote.provider_capability_version "
            "FROM short_drama_lipsync_jobs job "
            "JOIN short_drama_lipsync_attempts attempt ON attempt.id=job.attempt_id "
            "JOIN short_drama_lipsync_quotes quote ON quote.id=attempt.quote_id "
            "WHERE job.id=? AND job.lease_token=?",
            (job_id, token),
        ).fetchone()
    if not row:
        raise LipsyncJobError("lease_lost", "口型任务租约已失效", status=409)
    result = _decode(row["result_json"])
    if (
        row["state"] != "running"
        or not row["provider_job_id"]
        or not result.get("result_ready")
    ):
        raise LipsyncJobError(
            "result_not_ready", "Provider 结果尚未就绪", status=409
        )
    work_dir = Path(work_dir) / str(job_id)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise LipsyncResultRetryableError(
            "result work directory is unavailable"
        ) from error
    downloaded = work_dir / "provider-result"
    heartbeat(db_factory, job_id, token, now=now)
    if not downloaded.is_file():
        partial = work_dir / "provider-result.partial"
        complete = work_dir / "provider-result.complete"
        if not (partial.is_file() and complete.is_file()):
            try:
                if partial.exists():
                    partial.unlink()
                if complete.exists():
                    complete.unlink()
            except OSError:
                pass
            try:
                provider.fetch_result(row["provider_job_id"], str(partial))
                if not partial.is_file() or partial.stat().st_size <= 0:
                    raise RuntimeError(
                        "provider did not write a complete result"
                    )
                with open(partial, "r+b") as result_file:
                    os.fsync(result_file.fileno())
                with open(complete, "wb") as marker:
                    marker.write(b"complete")
                    marker.flush()
                    os.fsync(marker.fileno())
            except Exception as error:
                try:
                    if partial.exists():
                        partial.unlink()
                    if complete.exists():
                        complete.unlink()
                except OSError:
                    pass
                if getattr(provider, "supports_result_refetch", False):
                    raise LipsyncResultRetryableError(
                        "provider result fetch failed"
                    ) from error
                raise LipsyncResultManualReviewError(
                    "provider result cannot be fetched again safely"
                ) from error
        try:
            os.replace(str(partial), str(downloaded))
            complete.unlink()
        except OSError as error:
            raise LipsyncResultRetryableError(
                "provider result cache publication failed"
            ) from error
    artifact = short_drama_lipsync_media.accept_and_publish(
        job_id=job_id,
        project_id=row["project_id"],
        shot_id=row["shot_id"],
        provider=row["provider"],
        source_file=downloaded,
        output_root=output_root,
        expected_spec=_decode(row["media_spec_json"]),
        probe=probe,
        remux=remux,
    )
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT state,lease_token FROM short_drama_lipsync_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if (
            not current or current["state"] != "running"
            or current["lease_token"] != token
        ):
            raise LipsyncJobError("lease_lost", "口型任务租约已失效", status=409)
        conn.execute(
            "UPDATE short_drama_lipsync_jobs SET state='succeeded' "
            "WHERE id=? AND lease_token=? AND state='running'",
            (job_id, token),
        )
        version = short_drama_lipsync_versions.publish(
            conn,
            job_id=job_id,
            artifact=artifact,
            dependency_hashes={
                "input_hash": row["input_hash"],
                "face_target": _decode(row["face_target_json"]),
            },
            cost=_decode(row["cost_json"]),
            model_version=getattr(provider, "model_version", "")
            or row["provider_capability_version"],
            now=now,
        )
        conn.execute(
            "UPDATE short_drama_lipsync_jobs SET state='succeeded',progress=100,"
            "result_json=?,error_json='{}',lease_token=NULL,lease_owner=NULL,"
            "lease_expires_at=NULL,updated_at=? WHERE id=? AND lease_token=?",
            (
                canonical_json({
                    "provider_status": "succeeded",
                    "version_id": version["id"],
                    "file": version["file"],
                    "file_hash": version["file_hash"],
                }),
                now, job_id, token,
            ),
        )
        conn.execute(
            "UPDATE short_drama_lipsync_attempts SET state='settled',"
            "updated_at=? WHERE id=? AND state='linked'",
            (now, row["attempt_id"]),
        )
        conn.commit()
        return _public(conn, job_id)


def request_cancel(db_factory, owner, job_id, *, now=None):
    now = int(time.time()) if now is None else int(now)
    with closing(db_factory()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT job.state FROM short_drama_lipsync_jobs job "
            "JOIN short_drama_lipsync_attempts attempt ON attempt.id=job.attempt_id "
            "WHERE job.id=? AND attempt.owner_username=?",
            (job_id, owner),
        ).fetchone()
        if not row:
            raise LookupError("lipsync job does not exist")
        if row[0] in TERMINAL_STATES:
            conn.commit()
            return _public(conn, job_id, replayed=True)
        conn.execute(
            "UPDATE short_drama_lipsync_jobs SET state='cancel_pending',"
            "updated_at=? WHERE id=?",
            (now, job_id),
        )
        conn.commit()
        return _public(conn, job_id)


def retry(db_factory, owner, job_id, *, now=None):
    now = int(time.time()) if now is None else int(now)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT job.state AS job_state,job.provider_job_id,"
            "attempt.state AS attempt_state,job.error_json "
            "FROM short_drama_lipsync_jobs job "
            "JOIN short_drama_lipsync_attempts attempt ON attempt.id=job.attempt_id "
            "WHERE job.id=? AND attempt.owner_username=?",
            (job_id, owner),
        ).fetchone()
        if not row:
            raise LookupError("lipsync job does not exist")
        if row["job_state"] not in ("failed", "manual_review"):
            raise LipsyncJobError("retry_not_allowed", "当前任务不可重试", status=409)
        if row["attempt_state"] not in RETRYABLE_ATTEMPT_STATES:
            raise LipsyncJobError(
                "retry_billing_not_active",
                "任务已进入退款或人工审核，不可重试",
                status=409,
            )
        if row["provider_job_id"]:
            raise LipsyncJobError(
                "retry_requires_reconcile", "已关联 Provider 的任务必须先对账",
                status=409,
            )
        if _decode(row["error_json"]).get("code") == "provider_create_unknown":
            raise LipsyncJobError(
                "retry_requires_reconcile",
                "Provider 建单结果未知，必须人工对账后再处理", status=409,
            )
        changed = conn.execute(
            "UPDATE short_drama_lipsync_jobs SET state='queued',error_json='{}',"
            "next_poll_at=?,updated_at=? "
            "WHERE id=? AND state=? AND provider_job_id IS NULL AND EXISTS ("
            "SELECT 1 FROM short_drama_lipsync_attempts attempt "
            "WHERE attempt.id=short_drama_lipsync_jobs.attempt_id "
            "AND attempt.owner_username=? AND attempt.state=?)",
            (
                now, now, job_id, row["job_state"], owner,
                row["attempt_state"],
            ),
        ).rowcount
        if changed != 1:
            raise LipsyncJobError(
                "retry_state_conflict",
                "任务或扣点状态已变化，请刷新后重试",
                status=409,
            )
        conn.commit()
        return _public(conn, job_id)


def _pending_refund_candidate(db_factory, attempt_id):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT attempt.*,job.id AS job_id,job.state AS job_state,"
            "job.lease_token AS job_lease_token "
            "FROM short_drama_lipsync_attempts attempt "
            "LEFT JOIN short_drama_lipsync_jobs job "
            "ON job.attempt_id=attempt.id "
            "WHERE attempt.id=? AND attempt.state='refund_pending'",
            (str(attempt_id),),
        ).fetchone()


def _refund_candidate_is_safe(row):
    return bool(
        row
        and row["job_state"] in REFUND_SAFE_JOB_STATES
        and not row["job_lease_token"]
    )


def _emit_unsafe_refund_alert(db_factory, row, *, now):
    short_drama_lipsync_observability.emit(
        db_factory, "lipsync.refund.blocked_unsafe_job",
        severity="error",
        job_id=row["job_id"] or "",
        attempt_id=row["id"],
        actor=row["actor_username"],
        detail={
            "job_state": row["job_state"] or "missing",
            "has_lease": bool(row["job_lease_token"]),
        },
        now=now,
    )


def reconcile_refund_attempt(
    db_factory, ledger, attempt_id, *, now=None
):
    """Recover exactly one refund using its durable ledger idempotency key."""
    now = int(time.time()) if now is None else int(now)
    row = _pending_refund_candidate(db_factory, attempt_id)
    if not row:
        with closing(db_factory()) as conn:
            state = conn.execute(
                "SELECT state FROM short_drama_lipsync_attempts WHERE id=?",
                (str(attempt_id),),
            ).fetchone()
        return bool(state and state[0] == "refunded")
    if not _refund_candidate_is_safe(row):
        _emit_unsafe_refund_alert(db_factory, row, now=now)
        return False
    try:
        points_left = ledger.refund(
            row["actor_username"], int(row["cost"]),
            "短剧口型任务退款", row["refund_key"],
        )
        refund_ref = row["refund_key"]
    except Exception:
        transaction = ledger.lookup(row["refund_key"])
        if not transaction:
            return False
        points_left = transaction.get("points")
        refund_ref = transaction.get("id") or row["refund_key"]
    with closing(db_factory()) as conn:
        changed = conn.execute(
            "UPDATE short_drama_lipsync_attempts SET state='refunded',"
            "refund_ref=?,points_left=?,updated_at=? "
            "WHERE id=? AND state='refund_pending'",
            (str(refund_ref), points_left, now, row["id"]),
        ).rowcount
        state = (
            conn.execute(
                "SELECT state FROM short_drama_lipsync_attempts WHERE id=?",
                (row["id"],),
            ).fetchone()
            if not changed else None
        )
        conn.commit()
    return bool(changed or (state and state[0] == "refunded"))


def reconcile_refunds(db_factory, ledger, *, limit=64, now=None):
    now = int(time.time()) if now is None else int(now)
    with closing(db_factory()) as conn:
        rows = conn.execute(
            "SELECT id FROM short_drama_lipsync_attempts "
            "WHERE state='refund_pending' ORDER BY updated_at,id LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    result = []
    for row in rows:
        attempt_id = row[0]
        if reconcile_refund_attempt(
            db_factory, ledger, attempt_id, now=now
        ):
            result.append(attempt_id)
    return result


class PointsLedger:
    def __init__(self, points_domain):
        self.points = points_domain

    def deduct(self, username, amount, reason, key):
        return self.points.deduct_points(
            username, amount, reason, transaction_key=key
        )

    def refund(self, username, amount, reason, key):
        return self.points.refund_points(
            username, amount, reason, transaction_key=key
        )

    def lookup(self, key):
        return self.points.get_points_transaction(key)


class CallbackLedger:
    def __init__(self, deduct, refund, lookup):
        self._deduct = deduct
        self._refund = refund
        self._lookup = lookup

    def deduct(self, username, amount, reason, key):
        if not callable(self._deduct):
            raise RuntimeError("points deduction is unavailable")
        return self._deduct(
            username, amount, reason, transaction_key=key
        )

    def refund(self, username, amount, reason, key):
        if not callable(self._refund):
            raise RuntimeError("points refund is unavailable")
        return self._refund(
            username, amount, reason, transaction_key=key
        )

    def lookup(self, key):
        return self._lookup(key) if callable(self._lookup) else None
