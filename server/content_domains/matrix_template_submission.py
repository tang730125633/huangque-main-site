"""Durable charge/job handoff for idempotent paid submissions.

The table name is retained for backwards compatibility with the first caller,
matrix-template video.  New callers freeze their job kind in the same attempt
record before contacting Auth.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import time
import uuid

from . import jobs_store, submission_idempotency


LEASE_SECONDS = 120


class AttemptError(RuntimeError):
    pass


class AttemptConflict(AttemptError):
    pass


class AttemptInProgress(AttemptError):
    pass


class AttemptRecoveryPending(AttemptError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _row(row):
    if not row:
        return None
    value = dict(row)
    value["input"] = json.loads(value.pop("input_json") or "{}")
    value["response"] = json.loads(value.pop("response_json") or "{}")
    return value


def ensure_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS matrix_template_submission_attempts(
        username TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        idem_key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        input_json TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'matrix_template_video',
        cost INTEGER NOT NULL,
        charge_key TEXT NOT NULL UNIQUE,
        refund_key TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL,
        points_left INTEGER,
        job_id INTEGER UNIQUE,
        response_json TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '',
        lease_token TEXT NOT NULL DEFAULT '',
        lease_until INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY(username,endpoint,idem_key)
    )""")
    columns = {
        row["name"] for row in connection.execute(
            "PRAGMA table_info(matrix_template_submission_attempts)"
        ).fetchall()
    }
    if "kind" not in columns:
        connection.execute(
            "ALTER TABLE matrix_template_submission_attempts "
            "ADD COLUMN kind TEXT NOT NULL DEFAULT 'matrix_template_video'"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_matrix_template_attempt_recovery "
        "ON matrix_template_submission_attempts(state,lease_until,updated_at)"
    )


def transaction_keys(username, endpoint, idem_key):
    charge = "job-charge:%s:%s:%s" % (username, endpoint, idem_key)
    refund = "job-charge-refund:" + hashlib.sha256(charge.encode("utf-8")).hexdigest()
    return charge, refund


def get(db_factory, username, endpoint, idem_key):
    with closing(db_factory()) as connection:
        ensure_table(connection)
        row = connection.execute(
            "SELECT * FROM matrix_template_submission_attempts "
            "WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, idem_key),
        ).fetchone()
    return _row(row)


def recoverable(db_factory, limit=100, now=None):
    now = int(time.time() if now is None else now)
    with closing(db_factory()) as connection:
        ensure_table(connection)
        rows = connection.execute(
            """SELECT username,endpoint,idem_key
               FROM matrix_template_submission_attempts
               WHERE state IN ('prepared','charging','charged','refund_pending')
                 AND (lease_token='' OR lease_until<=?)
               ORDER BY updated_at ASC,created_at ASC LIMIT ?""",
            (now, max(1, min(500, int(limit or 100)))),
        ).fetchall()
    return [dict(row) for row in rows]


def prepare(db_factory, username, endpoint, idem_key, body, cost, now=None,
            kind="matrix_template_video"):
    now = int(time.time() if now is None else now)
    cost = int(cost or 0)
    kind = str(kind or "").strip()
    if cost <= 0:
        raise ValueError("invalid frozen cost")
    if not kind:
        raise ValueError("invalid frozen job kind")
    digest = _hash(body)
    encoded = _json(body)
    charge_key, refund_key = transaction_keys(username, endpoint, idem_key)
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        ensure_table(connection)
        claim = connection.execute(
            "SELECT request_hash FROM submission_idempotency "
            "WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, idem_key),
        ).fetchone()
        if not claim or claim["request_hash"] != digest:
            connection.rollback()
            raise AttemptConflict("submission idempotency claim is missing or changed")
        connection.execute(
            """INSERT OR IGNORE INTO matrix_template_submission_attempts(
               username,endpoint,idem_key,request_hash,input_json,kind,cost,
               charge_key,refund_key,state,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'prepared',?,?)""",
            (username, endpoint, idem_key, digest, encoded, kind, cost,
             charge_key, refund_key, now, now),
        )
        row = connection.execute(
            "SELECT * FROM matrix_template_submission_attempts "
            "WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, idem_key),
        ).fetchone()
        if (
            not row or row["request_hash"] != digest
            or row["input_json"] != encoded or int(row["cost"]) != cost
            or row["kind"] != kind
            or row["charge_key"] != charge_key or row["refund_key"] != refund_key
        ):
            connection.rollback()
            raise AttemptConflict("submission attempt is bound to different input")
        connection.commit()
    return _row(row)


def _claim(db_factory, username, endpoint, idem_key, now=None):
    now = int(time.time() if now is None else now)
    token = "matrix_attempt_" + uuid.uuid4().hex
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        ensure_table(connection)
        row = connection.execute(
            "SELECT * FROM matrix_template_submission_attempts "
            "WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, idem_key),
        ).fetchone()
        if not row:
            connection.rollback()
            return None
        if row["state"] in {"linked", "failed", "refunded"}:
            connection.commit()
            return _row(row)
        if row["lease_token"] and int(row["lease_until"] or 0) > now:
            connection.rollback()
            raise AttemptInProgress("submission attempt is already leased")
        changed = connection.execute(
            """UPDATE matrix_template_submission_attempts
               SET state=CASE WHEN state='prepared' THEN 'charging' ELSE state END,
                   lease_token=?,lease_until=?,updated_at=?
               WHERE username=? AND endpoint=? AND idem_key=?
                 AND state IN ('prepared','charging','charged','refund_pending')
                 AND (lease_token='' OR lease_until<=?)""",
            (token, now + LEASE_SECONDS, now, username, endpoint, idem_key, now),
        )
        if changed.rowcount != 1:
            connection.rollback()
            raise AttemptInProgress("submission attempt claim lost")
        connection.commit()
    value = get(db_factory, username, endpoint, idem_key)
    value["lease_token"] = token
    return value


def _lease_update(db_factory, attempt, assignments, values, states, now=None):
    now = int(time.time() if now is None else now)
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "UPDATE matrix_template_submission_attempts SET %s,updated_at=? "
            "WHERE username=? AND endpoint=? AND idem_key=? AND lease_token=? "
            "AND state IN (%s)" % (
                assignments, ",".join("?" for _ in states),
            ),
            tuple(values) + (now, attempt["username"], attempt["endpoint"],
                             attempt["idem_key"], attempt["lease_token"]) + tuple(states),
        )
        if changed.rowcount != 1:
            connection.rollback()
            raise AttemptInProgress("submission attempt lease was lost")
        connection.commit()
    return get(db_factory, attempt["username"], attempt["endpoint"], attempt["idem_key"])


def _release(db_factory, attempt, now=None):
    return _lease_update(
        db_factory, attempt, "lease_token='',lease_until=0", (),
        {"charging", "charged", "refund_pending"}, now,
    )


def _mark_charged(db_factory, attempt, points_left, now=None):
    return _lease_update(
        db_factory, attempt, "state='charged',points_left=?", (int(points_left),),
        {"charging"}, now,
    )


def _mark_failed(db_factory, attempt, response, error, now=None):
    return _lease_update(
        db_factory, attempt,
        "state='failed',response_json=?,error=?,lease_token='',lease_until=0",
        (_json(response), str(error or "")[:500]), {"charging"}, now,
    )


def _mark_refund_pending(db_factory, attempt, error, now=None):
    return _lease_update(
        db_factory, attempt, "state='refund_pending',error=?",
        (str(error or "")[:500],), {"charged"}, now,
    )


def _mark_refunded(db_factory, attempt, response, now=None):
    return _lease_update(
        db_factory, attempt,
        "state='refunded',response_json=?,lease_token='',lease_until=0",
        (_json(response),), {"refund_pending"}, now,
    )


def _confirmed_transaction(points, key, username, delta):
    reader = getattr(points, "get_points_transaction", None)
    transaction = reader(key) if callable(reader) else None
    if not transaction:
        return None
    if (
        str(transaction.get("username") or "") != username
        or int(transaction.get("delta") or 0) != int(delta)
    ):
        raise AttemptConflict("points transaction does not match submission attempt")
    return int(transaction.get("after_points") or 0)


def _link_in_transaction(connection, attempt, job_id, response):
    changed = connection.execute(
        """UPDATE matrix_template_submission_attempts
           SET state='linked',job_id=?,response_json=?,lease_token='',lease_until=0,
               updated_at=?
           WHERE username=? AND endpoint=? AND idem_key=? AND state='charged'
             AND lease_token=? AND job_id IS NULL""",
        (int(job_id), _json(response), int(time.time()), attempt["username"],
         attempt["endpoint"], attempt["idem_key"], attempt["lease_token"]),
    )
    if changed.rowcount != 1:
        raise AttemptInProgress("submission attempt link lease was lost")
    submission_idempotency.accept_in_transaction(
        connection, attempt["username"], attempt["endpoint"], attempt["idem_key"],
        attempt["input"], response,
    )


def recover(db_factory, points, username, endpoint, idem_key, body=None, cost=None,
            owner="content", now=None, kind="matrix_template_video"):
    if body is not None:
        prepare(db_factory, username, endpoint, idem_key, body, cost, now, kind)
    attempt = _claim(db_factory, username, endpoint, idem_key, now)
    if not attempt:
        return None
    if attempt["state"] in {"linked", "failed", "refunded"}:
        if attempt.get("response"):
            submission_idempotency.complete(
                db_factory, username, endpoint, idem_key, attempt["response"],
            )
        return attempt
    if attempt["state"] == "charging":
        try:
            points_left = _confirmed_transaction(
                points, attempt["charge_key"], username, -int(attempt["cost"]),
            )
            if points_left is None:
                points_left = points.deduct_points(
                    username, attempt["cost"], "job:%s" % attempt["kind"],
                    transaction_key=attempt["charge_key"],
                )
            attempt = _mark_charged(db_factory, attempt, points_left, now)
            attempt["lease_token"] = attempt.get("lease_token") or ""
        except AttemptConflict:
            _release(db_factory, attempt, now)
            raise
        except Exception as exc:
            status = int(getattr(exc, "status", 0) or 0)
            if status in {402, 403}:
                response = {
                    "detail": str(getattr(exc, "detail", exc))[:220],
                    "code": (getattr(exc, "data", {}) or {}).get("code", "charge_rejected"),
                    "operation_terminal": True, "_http_status": status,
                }
                failed = _mark_failed(db_factory, attempt, response, response["detail"], now)
                submission_idempotency.complete(
                    db_factory, username, endpoint, idem_key, response,
                )
                return failed
            _release(db_factory, attempt, now)
            raise AttemptRecoveryPending("charge result is not confirmed") from exc
    if attempt["state"] == "charged":
        accepted = {
            "job_id": 0, "cost": int(attempt["cost"]),
            "points_left": int(attempt["points_left"]), "accepted": True,
        }
        try:
            job_id = jobs_store.create_job_after_charge(
                db_factory, attempt["kind"], username, attempt["cost"],
                attempt["input"], owner,
                before_commit=lambda connection, linked_job_id: (
                    accepted.update(job_id=int(linked_job_id)),
                    _link_in_transaction(connection, attempt, linked_job_id, accepted),
                ),
            )
            linked = get(db_factory, username, endpoint, idem_key)
            if int(linked.get("job_id") or 0) != int(job_id):
                raise AttemptConflict("linked job does not match created job")
            return linked
        except (AttemptError, SystemExit, KeyboardInterrupt):
            raise
        except Exception as exc:
            attempt = _mark_refund_pending(db_factory, attempt, exc, now)
    if attempt["state"] == "refund_pending":
        try:
            points_left = _confirmed_transaction(
                points, attempt["refund_key"], username, int(attempt["cost"]),
            )
            if points_left is None:
                points_left = points.refund_points(
                    username, attempt["cost"], "%s job create failed" % attempt["kind"],
                    transaction_key=attempt["refund_key"],
                )
            response = {
                "detail": "任务创建失败，点数已退回",
                "code": "job_create_failed", "operation_terminal": True,
                "points_left": int(points_left), "_http_status": 500,
            }
            refunded = _mark_refunded(db_factory, attempt, response, now)
            submission_idempotency.complete(
                db_factory, username, endpoint, idem_key, response,
            )
            return refunded
        except AttemptConflict:
            _release(db_factory, attempt, now)
            raise
        except Exception as exc:
            _release(db_factory, attempt, now)
            raise AttemptRecoveryPending("refund result is not confirmed") from exc
    raise AttemptConflict("unsupported submission attempt state")
