"""Lipsync domain orchestration for snapshots, quotes and paid PR-F jobs."""

import os
import sqlite3

from . import (
    short_drama_lipsync_quotes,
    short_drama_lipsync_jobs,
    short_drama_lipsync_snapshot,
    short_drama_lipsync_versions,
    short_drama_timeline,
)
from .short_drama_lipsync_schema import init_db


LipsyncQuoteError = short_drama_lipsync_quotes.LipsyncQuoteError
LipsyncJobError = short_drama_lipsync_jobs.LipsyncJobError
LipsyncVersionError = short_drama_lipsync_versions.LipsyncVersionError


def _require_mutations_enabled():
    if str(os.getenv(
        "HQ_SHORT_DRAMA_LIPSYNC_MUTATIONS_ENABLED", ""
    )).lower() not in ("1", "true", "yes", "on"):
        raise LipsyncVersionError(
            "lipsync_mutations_disabled",
            "口型工作区写操作尚未开放",
            status=503,
        )


def _project(conn, owner, project_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM short_drama_projects "
        "WHERE id=? AND username=? AND deleted=0",
        (project_id, owner),
    ).fetchone()
    if not row:
        raise LookupError("short drama project does not exist")
    return row


def _require_video_review(project):
    if str(project["stage"] or "") != "video_review":
        raise LipsyncVersionError(
            "project_stage_readonly",
            "当前项目阶段只能查看口型版本",
            status=409,
        )


def get_snapshot(db_factory, owner, project_id, *, can_write):
    conn = db_factory()
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        return short_drama_lipsync_snapshot.build_snapshot(
            conn, _project(conn, owner, project_id), can_write=can_write
        )
    finally:
        conn.close()


def create_quote(db_factory, actor, owner, payload, *, resolver=None):
    conn = db_factory()
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        project = _project(conn, owner, str(payload.get("project_id") or ""))
        snapshot = short_drama_lipsync_snapshot.build_snapshot(
            conn, project, can_write=True
        )
        # Snapshot synchronization may write. Release that transaction before
        # streaming large media files; create_quote opens its own short write
        # transaction after the immutable file fingerprints are ready.
        conn.commit()
        return short_drama_lipsync_quotes.create_quote(
            conn, actor=actor, owner=owner, payload=payload, snapshot=snapshot,
            resolver=resolver,
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def create_job(
    db_factory, actor, owner, payload, idempotency_key, *,
    deduct_points=None, refund_points=None, charge_lookup=None,
    provider_ready=None, enqueue=None,
):
    job = short_drama_lipsync_jobs.prepare(
        db_factory, actor, owner, payload, idempotency_key
    )
    if job["attempt_state"] != "accepted":
        return job
    if provider_ready is not None and (
        not callable(provider_ready) or not provider_ready(job["provider"])
    ):
        raise LipsyncJobError(
            "lipsync_worker_unavailable",
            "口型任务消费者或 Provider 尚未就绪",
            status=503,
        )
    ledger = short_drama_lipsync_jobs.CallbackLedger(
        deduct_points, refund_points, charge_lookup
    )
    charged = short_drama_lipsync_jobs.charge(
        db_factory, job["id"], ledger
    )
    if callable(enqueue):
        enqueue()
    return charged


def get_job(db_factory, owner, job_id):
    return short_drama_lipsync_jobs.get(db_factory, owner, job_id)


def job_project_id(db_factory, job_id):
    conn = db_factory()
    try:
        row = conn.execute(
            "SELECT project_id FROM short_drama_lipsync_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if not row:
            raise LookupError("lipsync job does not exist")
        return str(row[0])
    finally:
        conn.close()


def retry_job(db_factory, owner, job_id):
    return short_drama_lipsync_jobs.retry(db_factory, owner, job_id)


def cancel_job(db_factory, owner, job_id):
    return short_drama_lipsync_jobs.request_cancel(db_factory, owner, job_id)


def update_speakers(db_factory, owner, actor, payload, idempotency_key):
    """Reuse the authoritative PR-C timeline mutation instead of shadow state."""
    _require_mutations_enabled()
    return short_drama_timeline.save_lipsync_changes(
        db_factory, owner, actor, payload, idempotency_key
    )


def select_version(db_factory, owner, payload, version_id):
    _require_mutations_enabled()
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner, str(payload.get("project_id") or ""))
        _require_video_review(project)
        snapshot = short_drama_lipsync_snapshot.build_snapshot(
            conn, project, can_write=True
        )
        if int(payload.get("expected_revision") or 0) != int(project["revision"]):
            raise LipsyncVersionError(
                "revision_changed",
                "项目已被其他页面更新，请刷新后重试",
                status=409,
            )
        if snapshot["input_hash"] != str(
            payload.get("expected_input_hash") or ""
        ):
            raise LipsyncVersionError(
                "stale_snapshot",
                "口型依赖已经变化，请刷新后重新选择",
                status=409,
            )
        result = short_drama_lipsync_versions.select(
            conn,
            project_id=project["id"],
            version_id=version_id,
            expected_input_hash=snapshot["input_hash"],
            expected_revision=payload.get("expected_pointer_revision"),
        )
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def lock_version(db_factory, owner, actor, payload, version_id):
    _require_mutations_enabled()
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner, str(payload.get("project_id") or ""))
        _require_video_review(project)
        snapshot = short_drama_lipsync_snapshot.build_snapshot(
            conn, project, can_write=True
        )
        if int(payload.get("expected_revision") or 0) != int(project["revision"]):
            raise LipsyncVersionError(
                "revision_changed",
                "项目已被其他页面更新，请刷新后重试",
                status=409,
            )
        if snapshot["input_hash"] != str(
            payload.get("expected_input_hash") or ""
        ):
            raise LipsyncVersionError(
                "stale_snapshot",
                "口型依赖已经变化，请刷新后重新确认",
                status=409,
            )
        blockers = list(snapshot["blockers"])
        if blockers:
            raise LipsyncVersionError(
                "lipsync_not_ready",
                "口型版本尚未满足锁定条件",
                status=422,
                blockers=blockers,
            )
        result = short_drama_lipsync_versions.lock(
            conn,
            actor=actor,
            project_id=project["id"],
            version_id=version_id,
            expected_input_hash=snapshot["input_hash"],
            expected_revision=payload.get("expected_pointer_revision"),
        )
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
