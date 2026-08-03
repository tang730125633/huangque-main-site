"""PR-F recovery scans for refunds and expired worker leases."""

import time
from contextlib import closing
import sqlite3

from . import short_drama_lipsync_jobs


def release_expired_leases(
    db_factory, *, now=None, limit=64, job_id=None
):
    now = int(time.time()) if now is None else int(now)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        job_filter = " AND id=?" if job_id else ""
        args = [now]
        if job_id:
            args.append(str(job_id))
        args.append(max(1, int(limit)))
        rows = conn.execute(
            "SELECT id,state,lease_token,lease_expires_at "
            "FROM short_drama_lipsync_jobs "
            "WHERE state IN ('queued','running','cancel_pending') "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at<=? "
            + job_filter +
            " ORDER BY lease_expires_at LIMIT ?",
            tuple(args),
        ).fetchall()
        released = []
        for row in rows:
            changed = conn.execute(
                "UPDATE short_drama_lipsync_jobs SET lease_token=NULL,"
                "lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE id=? AND state=? AND lease_token=? "
                "AND lease_expires_at=? AND lease_expires_at<=?",
                (
                    now, row["id"], row["state"], row["lease_token"],
                    row["lease_expires_at"], now,
                ),
            ).rowcount
            if changed:
                released.append(row["id"])
        conn.commit()
    return released


def run(db_factory, ledger, *, now=None, limit=64):
    return {
        "released_leases": release_expired_leases(
            db_factory, now=now, limit=limit
        ),
        "refunded_attempts": short_drama_lipsync_jobs.reconcile_refunds(
            db_factory, ledger, limit=limit, now=now
        ),
    }
