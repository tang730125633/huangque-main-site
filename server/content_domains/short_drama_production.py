"""Persistence helpers for short-drama production assets and jobs."""

import json
import hashlib
import sqlite3
import time
import uuid

from . import short_drama_voice


ASSET_TYPES = {"still"}
JOB_KINDS = {"still"}
PRODUCTION_STAGES = {
    "stills_review", "voice_review", "video_review", "assembly_review", "completed",
}
STILL_REQUEST_FIELDS = {
    "project_id", "revision", "shot_id", "prompt", "mode", "count",
}
STILL_SUBMISSION_FIELDS = STILL_REQUEST_FIELDS | {"quote_token"}
QUOTE_TTL_SECONDS = 300


_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_assets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  type TEXT NOT NULL CHECK (type IN ('still')),
  current_version INTEGER,
  locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0,1)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(project_id, shot_id, type)
);
CREATE TABLE IF NOT EXISTS short_drama_asset_versions (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES short_drama_assets(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  job_id INTEGER NOT NULL,
  url TEXT NOT NULL,
  file TEXT NOT NULL DEFAULT '',
  prompt TEXT NOT NULL,
  ratio TEXT NOT NULL CHECK (ratio IN ('9:16','16:9')),
  cost INTEGER NOT NULL DEFAULT 0 CHECK (cost >= 0),
  status TEXT NOT NULL CHECK (status IN ('done','failed')),
  created_at INTEGER NOT NULL,
  UNIQUE(asset_id, version),
  UNIQUE(asset_id, job_id, url)
);
CREATE TABLE IF NOT EXISTS short_drama_production_jobs (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('still')),
  job_id INTEGER NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL,
  quoted_cost INTEGER NOT NULL CHECK (quoted_cost >= 0),
  status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed')),
  error TEXT NOT NULL DEFAULT '',
  refunded INTEGER NOT NULL DEFAULT 0 CHECK (refunded IN (0,1,2)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, kind, idempotency_key)
);
CREATE TABLE IF NOT EXISTS short_drama_still_quotes (
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  request_hash TEXT NOT NULL,
  cost INTEGER NOT NULL CHECK (cost >= 0),
  expires_at INTEGER NOT NULL,
  consumed_idempotency_key TEXT,
  consumed_job_id INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_still_quotes_lookup
  ON short_drama_still_quotes(username, project_id, shot_id, expires_at);
CREATE TABLE IF NOT EXISTS short_drama_charge_attempts (
  charge_key TEXT PRIMARY KEY,
  refund_key TEXT NOT NULL UNIQUE,
  username TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  quote_token TEXT NOT NULL REFERENCES short_drama_still_quotes(token),
  cost INTEGER NOT NULL CHECK (cost >= 0),
  image_payload_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN
    ('accepted','charged','linked','refund_pending','refunded','failed')),
  points_left INTEGER,
  job_id INTEGER,
  terminal_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, endpoint, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_charge_attempts_operation
  ON short_drama_charge_attempts(username, project_id, shot_id, request_hash, state);
CREATE TRIGGER IF NOT EXISTS short_drama_assets_project_shot_on_insert
BEFORE INSERT ON short_drama_assets
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'short drama asset shot must belong to project');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_assets_project_shot_on_update
BEFORE UPDATE OF project_id, shot_id ON short_drama_assets
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'short drama asset shot must belong to project');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_production_jobs_project_shot_on_insert
BEFORE INSERT ON short_drama_production_jobs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'short drama production job shot must belong to project');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_production_jobs_project_shot_on_update
BEFORE UPDATE OF project_id, shot_id ON short_drama_production_jobs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'short drama production job shot must belong to project');
END;
"""


class ChargeAttemptInProgress(Exception):
    pass


def init_db(db_factory):
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(short_drama_production_jobs)")}
        if "error" not in columns:
            conn.execute("ALTER TABLE short_drama_production_jobs ADD COLUMN error TEXT NOT NULL DEFAULT ''")
        if "refunded" not in columns:
            conn.execute("ALTER TABLE short_drama_production_jobs ADD COLUMN refunded INTEGER NOT NULL DEFAULT 0")
        version_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(short_drama_asset_versions)")
        }
        if "file" not in version_columns:
            conn.execute(
                "ALTER TABLE short_drama_asset_versions "
                "ADD COLUMN file TEXT NOT NULL DEFAULT ''"
            )
        conn.commit()
    finally:
        conn.close()


def normalize_still_request(body, require_quote=False):
    expected = STILL_SUBMISSION_FIELDS if require_quote else STILL_REQUEST_FIELDS
    if not isinstance(body, dict) or set(body) != expected:
        if require_quote and isinstance(body, dict) and set(body) == STILL_REQUEST_FIELDS:
            raise ValueError("关键帧 quote 必填")
        raise ValueError("关键帧请求字段不正确")
    if (not isinstance(body["mode"], str)
            or body["mode"] not in {"single", "retry", "batch"}
            or type(body["count"]) is not int or body["count"] != 2):
        raise ValueError("关键帧每次必须生成 2 张候选图")
    if (not isinstance(body["project_id"], str) or not body["project_id"].strip()
            or not isinstance(body["shot_id"], str) or not body["shot_id"].strip()):
        raise ValueError("关键帧项目或分镜 ID 无效")
    if type(body["revision"]) is not int or body["revision"] < 1:
        raise ValueError("项目版本无效")
    if not isinstance(body["prompt"], str):
        raise ValueError("关键帧提示词无效")
    request = dict(body)
    for field in ("project_id", "shot_id", "prompt"):
        request[field] = request[field].strip()
    descriptor = {
        "kind": "short-drama-still",
        "project_id": request["project_id"],
        "revision": request["revision"],
        "shot_id": request["shot_id"],
        "prompt": request["prompt"],
        "mode": request["mode"],
        "count": request["count"],
        "provider": "seedream",
        "variant": "std",
        "quality": "hd",
    }
    if require_quote:
        if (not isinstance(body["quote_token"], str)
                or not body["quote_token"].strip()):
            raise ValueError("关键帧 quote 无效")
        request["quote_token"] = body["quote_token"].strip()
        descriptor["quote_token"] = request["quote_token"]
    return request, descriptor


def _descriptor_hash(descriptor):
    canonical = json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _quote_request_hash(descriptor):
    return _descriptor_hash({
        key: value for key, value in descriptor.items() if key != "quote_token"
    })


def charge_transaction_keys(username, endpoint, idempotency_key):
    canonical = json.dumps(
        [str(username), str(endpoint), str(idempotency_key)],
        ensure_ascii=False, separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "still-charge:" + digest, "still-refund:" + digest


def _attempt_dict(row):
    if not row:
        return None
    item = dict(row)
    try:
        item["image_payload"] = json.loads(item.pop("image_payload_json"))
    except (TypeError, ValueError):
        raise RuntimeError("stored still charge payload is invalid")
    raw_terminal = item.pop("terminal_json", None)
    item["terminal_response"] = json.loads(raw_terminal) if raw_terminal else None
    item["cost"] = int(item["cost"])
    if item.get("points_left") is not None:
        item["points_left"] = int(item["points_left"])
    if item.get("job_id") is not None:
        item["job_id"] = int(item["job_id"])
    return item


def get_charge_attempt(db_factory, username, idempotency_key):
    conn = db_factory()
    conn.row_factory = sqlite3.Row
    try:
        return _attempt_dict(conn.execute(
            "SELECT * FROM short_drama_charge_attempts "
            "WHERE username=? AND idempotency_key=?",
            (username, idempotency_key),
        ).fetchone())
    finally:
        conn.close()


def accept_charge_attempt(db_factory, *, username, endpoint, idempotency_key, prepared):
    """Durably accept consent and consume its quote before crossing into Auth."""
    charge_key, refund_key = charge_transaction_keys(username, endpoint, idempotency_key)
    project_id = prepared["project"]["id"]
    shot_id = prepared["shot"]["id"]
    quote_token = prepared["quote_token"]
    request_hash = prepared["request_hash"]
    cost = int(prepared["quoted_cost"])
    now = int(time.time())
    conn = db_factory()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM short_drama_charge_attempts "
            "WHERE username=? AND endpoint=? AND idempotency_key=?",
            (username, endpoint, idempotency_key),
        ).fetchone()
        if existing:
            conn.rollback()
            return _attempt_dict(existing)
        project = conn.execute(
            "SELECT stage FROM short_drama_projects WHERE id=? AND deleted=0",
            (project_id,),
        ).fetchone()
        if not project or project["stage"] != "stills_review":
            raise ValueError("当前短剧阶段不能接受关键帧扣点")
        unresolved = conn.execute(
            "SELECT idempotency_key FROM short_drama_charge_attempts "
            "WHERE username=? AND project_id=? AND shot_id=? AND request_hash=? "
            "AND state IN ('accepted','charged','refund_pending') LIMIT 1",
            (username, project_id, shot_id, request_hash),
        ).fetchone()
        if unresolved:
            conn.rollback()
            raise ChargeAttemptInProgress("same still operation is still reconciling")
        consumed = conn.execute(
            "UPDATE short_drama_still_quotes SET consumed_idempotency_key=? "
            "WHERE token=? AND username=? AND project_id=? AND shot_id=? "
            "AND request_hash=? AND cost=? AND expires_at>=? "
            "AND consumed_idempotency_key IS NULL",
            (idempotency_key, quote_token, username, project_id, shot_id,
             request_hash, cost, now),
        )
        if consumed.rowcount != 1:
            conn.rollback()
            raise ValueError("关键帧 quote 已过期、已使用或与请求不匹配")
        conn.execute(
            "INSERT INTO short_drama_charge_attempts "
            "(charge_key,refund_key,username,endpoint,idempotency_key,request_hash,"
            "project_id,shot_id,quote_token,cost,image_payload_json,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'accepted',?,?)",
            (charge_key, refund_key, username, endpoint, idempotency_key, request_hash,
             project_id, shot_id, quote_token, cost,
             json.dumps(prepared["image_payload"], ensure_ascii=False), now, now),
        )
        conn.commit()
        return get_charge_attempt(db_factory, username, idempotency_key)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def mark_attempt_charged(db_factory, username, idempotency_key, points_left):
    conn = db_factory()
    try:
        conn.execute(
            "UPDATE short_drama_charge_attempts SET state='charged',points_left=?,updated_at=? "
            "WHERE username=? AND idempotency_key=? AND state IN ('accepted','charged')",
            (int(points_left), int(time.time()), username, idempotency_key),
        )
        conn.commit()
    finally:
        conn.close()
    return get_charge_attempt(db_factory, username, idempotency_key)


def mark_attempt_refund_pending(db_factory, username, idempotency_key, response, job_id=None):
    payload = json.dumps(response, ensure_ascii=False)
    conn = db_factory()
    try:
        conn.execute(
            "UPDATE short_drama_charge_attempts SET state='refund_pending',terminal_json=?,"
            "job_id=COALESCE(?,job_id),updated_at=? WHERE username=? AND idempotency_key=? "
            "AND state IN ('accepted','charged','linked','refund_pending')",
            (payload, job_id, int(time.time()), username, idempotency_key),
        )
        conn.commit()
    finally:
        conn.close()
    return get_charge_attempt(db_factory, username, idempotency_key)


def mark_attempt_failed(db_factory, username, idempotency_key, response):
    """Persist a definitive no-charge Auth rejection as a replayable terminal result."""
    payload = json.dumps(response, ensure_ascii=False)
    conn = db_factory()
    try:
        conn.execute(
            "UPDATE short_drama_charge_attempts SET state='failed',terminal_json=?,updated_at=? "
            "WHERE username=? AND idempotency_key=? AND state='accepted'",
            (payload, int(time.time()), username, idempotency_key),
        )
        conn.commit()
    finally:
        conn.close()
    return get_charge_attempt(db_factory, username, idempotency_key)


def recover_attempt_response(db_factory, username, idempotency_key):
    attempt = get_charge_attempt(db_factory, username, idempotency_key)
    return attempt.get("terminal_response") if attempt else None


def reconcile_attempt_refund(db_factory, points_domain, attempt):
    """Replay the attempt's one stable Auth compensation transaction."""
    if not attempt or attempt.get("state") not in {"refund_pending", "refunded"}:
        return attempt
    if attempt["state"] == "refund_pending":
        try:
            points_domain.refund_points(
                attempt["username"], attempt["cost"], "short-drama still compensation",
                transaction_key=attempt["refund_key"],
            )
        except Exception:
            return attempt
        attempt = mark_attempt_refunded(
            db_factory, attempt["username"], attempt["idempotency_key"],
        )
    return attempt


def retry_attempt_refunds(db_factory, points_domain, limit=100):
    """Sweep attempt-owned compensation, including attempts without a job row."""
    conn = db_factory()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM short_drama_charge_attempts WHERE state='refund_pending' "
            "ORDER BY updated_at,charge_key LIMIT ?", (max(1, int(limit or 100)),),
        ).fetchall()
        attempts = [_attempt_dict(row) for row in rows]
    finally:
        conn.close()
    recovered = 0
    for attempt in attempts:
        if reconcile_attempt_refund(db_factory, points_domain, attempt)["state"] == "refunded":
            recovered += 1
    return recovered


def refund_pending_response():
    return {
        "detail": "still generation refund is still being reconciled",
        "code": "refund_pending", "retryable": True, "retry_after_ms": 1000,
    }


def record_attempt_job(db_factory, username, idempotency_key, job_id, *, connection):
    """Atomically bind the already-authorized attempt, job and replay response."""
    connection.row_factory = sqlite3.Row
    attempt = connection.execute(
        "SELECT * FROM short_drama_charge_attempts WHERE username=? AND idempotency_key=?",
        (username, idempotency_key),
    ).fetchone()
    if not attempt or attempt["state"] not in {"charged", "linked"}:
        raise ValueError("still charge attempt is not ready for a job")
    job = connection.execute(
        "SELECT username,kind,cost,status FROM jobs WHERE id=?", (job_id,),
    ).fetchone()
    if (not job or job["username"] != username or job["kind"] != "image"
            or job["status"] != "pending" or int(job["cost"] or 0) != int(attempt["cost"])):
        raise ValueError("still job does not match its accepted charge")
    now = int(time.time())
    connection.execute(
        "UPDATE short_drama_still_quotes SET consumed_job_id=? "
        "WHERE token=? AND username=? AND consumed_idempotency_key=? "
        "AND (consumed_job_id IS NULL OR consumed_job_id=?)",
        (job_id, attempt["quote_token"], username, idempotency_key, job_id),
    )
    connection.execute(
        "INSERT INTO short_drama_production_jobs "
        "(id,username,project_id,shot_id,kind,job_id,idempotency_key,quoted_cost,status,created_at,updated_at) "
        "VALUES(?,?,?,?, 'still',?,?,?,'pending',?,?)",
        (str(uuid.uuid4()), username, attempt["project_id"], attempt["shot_id"],
         job_id, idempotency_key, int(attempt["cost"]), now, now),
    )
    response = {
        "job_id": job_id, "cost": int(attempt["cost"]),
        "points_left": int(attempt["points_left"]),
        "project_id": attempt["project_id"], "shot_id": attempt["shot_id"],
    }
    connection.execute(
        "UPDATE short_drama_charge_attempts SET state='linked',job_id=?,terminal_json=?,updated_at=? "
        "WHERE charge_key=? AND state='charged'",
        (job_id, json.dumps(response, ensure_ascii=False), now, attempt["charge_key"]),
    )
    return response


def mark_linked_attempt_failed(db_factory, username, idempotency_key, response):
    """Atomically make a linked job terminal and persist its refund intent."""
    now = int(time.time())
    payload = json.dumps(response, ensure_ascii=False)
    conn = db_factory()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        attempt = conn.execute(
            "SELECT job_id FROM short_drama_charge_attempts "
            "WHERE username=? AND idempotency_key=?",
            (username, idempotency_key),
        ).fetchone()
        job_id = int(attempt["job_id"]) if attempt and attempt["job_id"] else None
        if job_id:
            conn.execute(
                "UPDATE jobs SET status='error',error=?,refunded=CASE WHEN cost>0 THEN 2 ELSE refunded END,updated_at=? "
                "WHERE id=? AND status IN ('pending','running')",
                (str(response.get("detail") or "")[:300], now, job_id),
            )
            conn.execute(
                "UPDATE short_drama_production_jobs SET status='failed',error=?,refunded=2,updated_at=? "
                "WHERE job_id=?",
                (str(response.get("detail") or "")[:300], now, job_id),
            )
        conn.execute(
            "UPDATE short_drama_charge_attempts SET state='refund_pending',terminal_json=?,updated_at=? "
            "WHERE username=? AND idempotency_key=? AND state IN ('linked','refund_pending')",
            (payload, now, username, idempotency_key),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_charge_attempt(db_factory, username, idempotency_key)


def transition_linked_job_refund_pending(conn, job_id, error, *,
                                         from_states=("pending", "running", "done"),
                                         response=None):
    """Claim an attempt-backed job failure and durably hand refund ownership to its attempt.

    The caller owns the transaction.  No Auth call is made here: the attempt sweeper is
    deliberately the only component allowed to replay ``still-refund:*``.
    """
    conn.row_factory = sqlite3.Row
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='short_drama_charge_attempts'"
    ).fetchone():
        return None
    attempt = conn.execute(
        "SELECT username,idempotency_key,state FROM short_drama_charge_attempts "
        "WHERE job_id=? AND state IN ('linked','refund_pending')",
        (int(job_id),),
    ).fetchone()
    if not attempt:
        return None
    placeholders = ",".join("?" for _ in from_states)
    now = int(time.time())
    message = str(error or "still generation failed")[:300]
    claimed = conn.execute(
        "UPDATE jobs SET status='error',error=?,"
        "refunded=CASE WHEN COALESCE(cost,0)>0 THEN 2 ELSE refunded END,updated_at=? "
        "WHERE id=? AND status IN (%s)" % placeholders,
        (message, now, int(job_id), *tuple(from_states)),
    ).rowcount == 1
    if not claimed:
        return {"claimed": False, "attempt_owned": True}
    terminal = response or {
        "detail": message, "code": "still_generation_failed",
        "operation_terminal": True, "_http_status": 500,
    }
    conn.execute(
        "UPDATE short_drama_charge_attempts SET state='refund_pending',terminal_json=?,updated_at=? "
        "WHERE job_id=? AND state IN ('linked','refund_pending')",
        (json.dumps(terminal, ensure_ascii=False), now, int(job_id)),
    )
    conn.execute(
        "UPDATE short_drama_production_jobs SET status='failed',error=?,"
        "refunded=CASE WHEN quoted_cost>0 THEN 2 ELSE refunded END,updated_at=? WHERE job_id=?",
        (message, now, int(job_id)),
    )
    return {"claimed": claimed, "attempt_owned": True}


def fail_linked_job(db_factory, job_id, error, *, from_states=("pending", "running", "done"),
                    response=None):
    """Atomically persist terminal job state and attempt-owned refund intent."""
    conn = db_factory()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = transition_linked_job_refund_pending(
            conn, job_id, error, from_states=from_states, response=response,
        )
        if result is None:
            conn.rollback()
            return None
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_attempt_refunded(db_factory, username, idempotency_key):
    conn = db_factory()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT job_id FROM short_drama_charge_attempts WHERE username=? AND idempotency_key=?",
            (username, idempotency_key),
        ).fetchone()
        job_id = int(row[0]) if row and row[0] else None
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_charge_attempts SET state='refunded',updated_at=? "
            "WHERE username=? AND idempotency_key=? AND state IN ('refund_pending','refunded')",
            (now, username, idempotency_key),
        )
        if job_id:
            conn.execute("UPDATE jobs SET refunded=1,updated_at=? WHERE id=? AND refunded=2",
                         (now, job_id))
            conn.execute(
                "UPDATE short_drama_production_jobs SET refunded=1,updated_at=? WHERE job_id=?",
                (now, job_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_charge_attempt(db_factory, username, idempotency_key)


def _authorized_project(conn, username, project_id, access=None, *, write=False):
    project = conn.execute(
        "SELECT * FROM short_drama_projects WHERE id=? AND deleted=0", (project_id,)
    ).fetchone()
    if not project:
        raise LookupError("短剧项目不存在")
    if not project["board_id"]:
        if project["username"] != username:
            raise LookupError("short drama project does not exist")
        return dict(project)
    access = access if isinstance(access, dict) else {}
    board_id = str(access.get("board_id") or "").strip()
    role = str(access.get("role") or "").strip().lower()
    if board_id != project["board_id"] or role not in {"owner", "editor", "viewer"}:
        raise LookupError("短剧项目不存在")
    if write and role not in {"owner", "editor"}:
        raise PermissionError("当前协作角色没有编辑权限")
    return dict(project)


def _project_references(conn, project, shot, *, include_internal=False):
    try:
        character_keys = json.loads(shot.get("character_keys_json") or "[]")
    except (TypeError, ValueError):
        character_keys = []
    if not isinstance(character_keys, list):
        character_keys = []
    keys = [item for item in character_keys if isinstance(item, str) and item]
    references = []
    if keys:
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            "SELECT id, character_key, name, source_type, avatar_id "
            "FROM short_drama_characters WHERE project_id=? AND character_key IN (%s) "
            "ORDER BY sort_order, id" % placeholders,
            (project["id"], *keys),
        ).fetchall()
        for row in rows:
            references.append({
                "type": "character", "id": row["id"], "name": row["name"],
                "source_type": row["source_type"], "source_id": row["avatar_id"] or "",
            })
    previous = conn.execute(
        "SELECT v.id, v.url, v.file, v.ratio, s.shot_key FROM short_drama_shots current "
        "JOIN short_drama_shots s ON s.project_id=current.project_id "
        "AND s.sort_order<current.sort_order "
        "JOIN short_drama_assets a ON a.project_id=s.project_id AND a.shot_id=s.id "
        "AND a.type='still' AND a.locked=1 "
        "JOIN short_drama_asset_versions v ON v.asset_id=a.id AND v.version=a.current_version "
        "WHERE current.id=? AND current.project_id=? AND v.status='done' "
        "ORDER BY s.sort_order DESC, s.id DESC LIMIT 1",
        (shot["id"], project["id"]),
    ).fetchone()
    if previous:
        if previous["ratio"] != project["ratio"]:
            raise ValueError("上一镜头连续性参考比例与项目不一致")
        continuity = {
            "type": "continuity", "id": previous["id"],
            "name": "上一镜头 %s 已锁定关键帧" % previous["shot_key"],
            "url": previous["url"], "ratio": previous["ratio"],
        }
        if include_internal and previous["file"]:
            continuity["file"] = previous["file"]
        references.append(continuity)
    return references


def prepare_still_submission(db_factory, username, body, *, require_quote=False,
                             idempotency_key="", now=None, access=None):
    body, descriptor = normalize_still_request(body, require_quote=require_quote)

    conn = db_factory()
    conn.row_factory = sqlite3.Row
    try:
        project = _authorized_project(
            conn, username, body["project_id"], access, write=True
        )
        if int(project["revision"]) != body["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
        if project["stage"] != "stills_review":
            raise ValueError("当前短剧阶段不能生成关键帧")
        shot = conn.execute(
            "SELECT s.*, COALESCE(a.locked, 0) AS still_locked "
            "FROM short_drama_shots s "
            "LEFT JOIN short_drama_assets a "
            "ON a.project_id=s.project_id AND a.shot_id=s.id AND a.type='still' "
            "WHERE s.id=? AND s.project_id=?",
            (body["shot_id"], project["id"]),
        ).fetchone()
        if not shot:
            raise ValueError("关键帧分镜不属于当前项目")
        shot = dict(shot)
        if body["mode"] == "batch" and bool(shot.pop("still_locked")):
            raise ValueError("批量生成已跳过锁定的关键帧")
        shot.pop("still_locked", None)
        references = _project_references(conn, project, shot, include_internal=True)
        quoted_cost = None
        if require_quote:
            quote = conn.execute(
                "SELECT username, project_id, shot_id, request_hash, cost, expires_at, "
                "consumed_idempotency_key FROM short_drama_still_quotes WHERE token=?",
                (body["quote_token"],),
            ).fetchone()
            current_time = int(time.time()) if now is None else int(now)
            if (not quote or quote["username"] != username
                    or quote["project_id"] != project["id"]
                    or quote["shot_id"] != shot["id"]
                    or quote["request_hash"] != _quote_request_hash(descriptor)
                    or int(quote["expires_at"]) < current_time
                    or (quote["consumed_idempotency_key"] is not None
                        and quote["consumed_idempotency_key"] != idempotency_key)):
                raise ValueError("关键帧 quote 无效、已过期或与请求不匹配")
            quoted_cost = int(quote["cost"])
    finally:
        conn.close()

    from . import image as image_domain
    image_payload = image_domain.validate_image_payload({
        "provider": "seedream",
        "variant": "std",
        "quality": "hd",
        "prompt": body["prompt"],
        "ratio": project["ratio"],
        "count": 2,
        "short_drama_references": references,
    })
    shot["references"] = references
    return {
        "project": project, "shot": shot, "image_payload": image_payload,
        "quote_token": body.get("quote_token"), "quoted_cost": quoted_cost,
        "request_hash": _quote_request_hash(descriptor),
    }


def check_production_budget(db_factory, username, project_id, quoted_cost, access=None):
    if type(quoted_cost) is not int or quoted_cost < 0:
        raise ValueError("关键帧报价无效")
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        project = _authorized_project(conn, username, project_id, access, write=True)
        point_budget, spent_points, stage = (
            project["point_budget"], project["spent_points"], project["stage"]
        )
        if stage != "stills_review":
            raise ValueError("当前短剧阶段不能生成关键帧")
        point_budget = int(point_budget)
        if point_budget == 0:
            return
        reserved = conn.execute(
            "SELECT COALESCE(SUM(p.quoted_cost), 0) "
            "FROM short_drama_production_jobs p "
            "JOIN jobs j ON j.id=p.job_id AND j.username=p.username AND j.kind='image' "
            "WHERE p.project_id=? "
            "AND p.status IN ('pending','running') "
            "AND j.status IN ('pending','running','done')",
            (project_id,),
        ).fetchone()[0]
        attempt_reserved = conn.execute(
            "SELECT COALESCE(SUM(a.cost),0) FROM short_drama_charge_attempts a "
            "WHERE a.project_id=? AND a.state IN ('accepted','charged','refund_pending') "
            "AND (a.job_id IS NULL OR (a.state='refund_pending' AND NOT EXISTS ("
            "SELECT 1 FROM short_drama_production_jobs p WHERE p.job_id=a.job_id "
            "AND p.status IN ('pending','running'))))",
            (project_id,),
        ).fetchone()[0]
        reserved = int(reserved or 0) + int(attempt_reserved or 0)
        spent_points = int(spent_points)
        if spent_points + reserved + quoted_cost > point_budget:
            from .short_drama import PointBudgetExceeded
            raise PointBudgetExceeded(
                "短剧点数预算不足：已用 %d 点、已预留 %d 点、本次 %d 点、预算 %d 点" %
                (spent_points, reserved, quoted_cost, point_budget)
            )
    finally:
        conn.close()


def prepare_still_quote(db_factory, username, body, cost_of, access=None):
    prepared = prepare_still_submission(db_factory, username, body, access=access)
    cost = int(cost_of("image", prepared["image_payload"]))
    if cost < 0:
        raise ValueError("关键帧报价无效")
    check_production_budget(db_factory, username, prepared["project"]["id"], cost, access)
    token = uuid.uuid4().hex
    now = int(time.time())
    expires_at = now + QUOTE_TTL_SECONDS
    _request, descriptor = normalize_still_request(body)
    conn = db_factory()
    try:
        conn.execute(
            "INSERT INTO short_drama_still_quotes "
            "(token, username, project_id, shot_id, request_hash, cost, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (token, username, prepared["project"]["id"], prepared["shot"]["id"],
             _descriptor_hash(descriptor), cost, expires_at, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"cost": cost, "count": 2, "kind": "still",
            "quote_token": token, "expires_at": expires_at}


def record_submitted_job(db_factory, *, username, project_id, shot_id, job_id,
                         idempotency_key, quoted_cost, quote_token=None,
                         request_hash=None, connection=None, access=None):
    if (type(job_id) is not int or job_id < 1 or type(quoted_cost) is not int
            or quoted_cost < 0 or not isinstance(idempotency_key, str)
            or not idempotency_key):
        raise ValueError("关键帧任务关联参数无效")
    owns_connection = connection is None
    conn = db_factory() if owns_connection else connection
    conn.row_factory = sqlite3.Row
    try:
        if owns_connection:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
        project = _authorized_project(conn, username, project_id, access, write=True)
        if project["stage"] != "stills_review":
            raise ValueError("当前短剧阶段不能生成关键帧")
        if not conn.execute(
            "SELECT 1 FROM short_drama_shots WHERE id=? AND project_id=?",
            (shot_id, project_id),
        ).fetchone():
            raise ValueError("关键帧分镜不属于当前项目")
        job = conn.execute(
            "SELECT username, kind, cost, status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if (not job or job["username"] != username or job["kind"] != "image"
                or job["status"] != "pending" or int(job["cost"] or 0) != quoted_cost):
            raise ValueError("关键帧任务不属于当前用户或状态无效")
        existing = conn.execute(
            "SELECT p.id, p.project_id, p.shot_id, j.status AS job_status "
            "FROM short_drama_production_jobs p "
            "LEFT JOIN jobs j ON j.id=p.job_id AND j.username=p.username "
            "WHERE p.username=? AND p.kind='still' AND p.idempotency_key=?",
            (username, idempotency_key),
        ).fetchone()
        if existing:
            if (existing["project_id"] != project_id or existing["shot_id"] != shot_id
                    or existing["job_status"] not in {"error", "failed"}):
                raise ValueError("关键帧幂等键已关联其他任务")
            conn.execute(
                "DELETE FROM short_drama_production_jobs WHERE id=?", (existing["id"],)
            )
        now = int(time.time())
        if quote_token is not None:
            consumed = conn.execute(
                "UPDATE short_drama_still_quotes SET consumed_idempotency_key=?, consumed_job_id=? "
                "WHERE token=? AND username=? AND project_id=? AND shot_id=? "
                "AND request_hash=? AND cost=? AND expires_at>=? "
                "AND consumed_idempotency_key IS NULL",
                (idempotency_key, job_id, quote_token, username, project_id, shot_id,
                 request_hash, quoted_cost, now),
            )
            if consumed.rowcount != 1:
                raise ValueError("关键帧 quote 已过期、已使用或与请求不匹配")
        conn.execute(
            "INSERT INTO short_drama_production_jobs "
            "(id, username, project_id, shot_id, kind, job_id, idempotency_key, "
            "quoted_cost, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'still', ?, ?, ?, 'pending', ?, ?)",
            (str(uuid.uuid4()), username, project_id, shot_id, job_id,
             idempotency_key, quoted_cost, now, now),
        )
        if owns_connection:
            conn.commit()
    except Exception:
        if owns_connection:
            conn.rollback()
        raise
    finally:
        if owns_connection:
            conn.close()


def submitted_job_callback(db_factory, **association):
    return lambda connection, job_id: record_submitted_job(
        db_factory, job_id=job_id, connection=connection, **association
    )


def recover_submitted_response(db_factory, username, idempotency_key):
    """Recover an accepted job after a crash before the HTTP claim was completed."""
    conn = db_factory()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT p.project_id, p.shot_id, p.job_id, p.quoted_cost "
            "FROM short_drama_production_jobs p "
            "JOIN jobs j ON j.id=p.job_id AND j.username=p.username AND j.kind='image' "
            "WHERE p.username=? AND p.kind='still' AND p.idempotency_key=?",
            (username, idempotency_key),
        ).fetchone()
        if not row:
            return None
        return {
            "job_id": int(row["job_id"]), "cost": int(row["quoted_cost"]),
            "project_id": row["project_id"], "shot_id": row["shot_id"],
        }
    finally:
        conn.close()


def consume_failed_quote(db_factory, username, quote_token, idempotency_key):
    """Permanently consume a quote whose charge attempt reached compensation."""
    conn = db_factory()
    try:
        conn.execute(
            "UPDATE short_drama_still_quotes SET consumed_idempotency_key=? "
            "WHERE token=? AND username=? AND consumed_idempotency_key IS NULL",
            (idempotency_key, quote_token, username),
        )
        conn.commit()
    finally:
        conn.close()


def ensure_asset_slots(conn, project_id):
    now = int(time.time())
    shot_ids = conn.execute(
        "SELECT id FROM short_drama_shots WHERE project_id=? ORDER BY sort_order, id",
        (project_id,),
    ).fetchall()
    for (shot_id,) in shot_ids:
        conn.execute(
            "INSERT OR IGNORE INTO short_drama_assets "
            "(id, project_id, shot_id, type, created_at, updated_at) VALUES (?, ?, ?, 'still', ?, ?)",
            (str(uuid.uuid4()), project_id, shot_id, now, now),
        )


def _json_object(raw, error_message):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        raise ValueError(error_message)
    if not isinstance(value, dict):
        raise ValueError(error_message)
    return value


def _trusted_result_files(result, urls):
    candidates = result.get("files")
    if not isinstance(candidates, list):
        candidates = []
    if not candidates and result.get("file"):
        candidates = [result["file"]]
    from . import image as image_domain
    return [
        image_domain._trusted_short_drama_file(
            candidates[index] if index < len(candidates) else ""
        ) or image_domain._trusted_short_drama_file(url, file_url=True)
        for index, url in enumerate(urls)
    ]


def reconcile_jobs(conn, username, project_id):
    job_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    has_job_error = "error" in job_columns
    has_job_refunded = "refunded" in job_columns
    error_expr = "j.error" if has_job_error else "''"
    refunded_expr = "j.refunded" if has_job_refunded else "0"
    rows = conn.execute(
        "SELECT p.id, p.shot_id, p.job_id, p.status, p.quoted_cost, "
        "j.status, j.cost, j.payload, j.result, p.error, p.refunded, "
        + error_expr + ", " + refunded_expr + " "
        "FROM short_drama_production_jobs p "
        "JOIN jobs j ON j.id=p.job_id AND j.username=p.username AND j.kind='image' "
        "WHERE p.project_id=? ORDER BY p.created_at, p.id",
        (project_id,),
    ).fetchall()
    now = int(time.time())
    for (link_id, shot_id, job_id, link_status, quoted_cost, job_status, cost,
         payload_json, result_json, link_error, link_refunded, job_error,
         job_refunded) in rows:
        status = job_status if job_status in {"pending", "running", "done", "failed"} else "failed"
        conn.execute(
            "UPDATE short_drama_production_jobs SET status=?, error=?, refunded=?, updated_at=? WHERE id=?",
            (status, str(job_error or link_error or "")[:300],
             int(job_refunded or link_refunded or 0), now, link_id),
        )
        if status != "done":
            continue
        savepoint = "reconcile_%s" % str(job_id).replace("-", "_")
        conn.execute("SAVEPOINT " + savepoint)
        try:
            payload = _json_object(payload_json, "关键帧任务参数无效")
            result = _json_object(result_json, "关键帧任务结果无效")
            project_ratio = conn.execute(
                "SELECT ratio FROM short_drama_projects WHERE id=?", (project_id,)
            ).fetchone()[0]
            if result.get("ratio") != project_ratio or payload.get("ratio") != project_ratio:
                raise ValueError("关键帧任务比例与项目不一致")
            urls = result.get("urls") or ([result.get("url")] if result.get("url") else [])
            if (not isinstance(urls, list) or len(urls) != 2
                    or any(not isinstance(url, str) or not url for url in urls)
                    or len(set(urls)) != 2):
                raise ValueError("关键帧任务必须返回 2 张候选图")
            local_files = _trusted_result_files(result, urls)
            prompt = payload.get("prompt") or ""
            if not isinstance(prompt, str):
                raise ValueError("关键帧任务参数无效")
            asset_id = conn.execute(
                "SELECT id FROM short_drama_assets WHERE project_id=? AND shot_id=? AND type='still'",
                (project_id, shot_id),
            ).fetchone()[0]
            existing_archive = conn.execute(
                "SELECT COUNT(*) FROM short_drama_asset_versions WHERE asset_id=? AND job_id=?",
                (asset_id, job_id),
            ).fetchone()[0]
            if int(existing_archive) not in {0, 2}:
                raise ValueError("关键帧任务归档不完整")
            next_version = int(conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_asset_versions WHERE asset_id=?",
                (asset_id,),
            ).fetchone()[0])
            if int(existing_archive) == 0:
                for offset, url in enumerate(urls):
                    conn.execute(
                        "INSERT OR IGNORE INTO short_drama_asset_versions "
                        "(id, asset_id, version, job_id, url, file, prompt, ratio, cost, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'done', ?)",
                        (str(uuid.uuid4()), asset_id, next_version + offset, job_id, url,
                         local_files[offset],
                         prompt, project_ratio, int(cost or 0), now),
                    )
            for url, local_file in zip(urls, local_files):
                if local_file:
                    conn.execute(
                        "UPDATE short_drama_asset_versions SET file=? "
                        "WHERE asset_id=? AND job_id=? AND url=? AND file=''",
                        (local_file, asset_id, job_id, url),
                    )
            archived = conn.execute(
                "SELECT COUNT(*), MIN(version) FROM short_drama_asset_versions WHERE asset_id=? AND job_id=?",
                (asset_id, job_id),
            ).fetchone()
            if int(archived[0]) != 2:
                raise ValueError("关键帧任务必须完整归档 2 张候选图")
            if link_status in {"pending", "running"}:
                conn.execute(
                    "UPDATE short_drama_projects SET spent_points=spent_points+? WHERE id=?",
                    (int(quoted_cost or 0), project_id),
                )
            conn.execute(
                "UPDATE short_drama_assets SET current_version=COALESCE(current_version, ?), updated_at=? WHERE id=?",
                (int(archived[1]), now, asset_id),
            )
            conn.execute("RELEASE " + savepoint)
        except Exception as error:
            conn.execute("ROLLBACK TO " + savepoint)
            conn.execute("RELEASE " + savepoint)
            message = str(error)[:300]
            if has_job_error and has_job_refunded:
                attempt_failure = transition_linked_job_refund_pending(
                    conn, job_id, message, from_states=("done",),
                )
                if attempt_failure is None:
                    conn.execute(
                        "UPDATE jobs SET status='error', error=?, refunded=CASE WHEN cost>0 AND refunded=0 THEN 2 ELSE refunded END WHERE id=?",
                        (message, job_id),
                    )
                refund_state = 2 if int(cost or 0) > 0 else 0
            else:
                refund_state = 0
            conn.execute(
                "UPDATE short_drama_production_jobs SET status='failed', error=?, refunded=?, updated_at=? WHERE id=?",
                (message, refund_state, now, link_id),
            )


_HANDOFF_ORDER = {
    "missing_locked_still": 0,
    "active_job": 1,
    "refund_pending": 2,
    "charge_attempt_pending": 3,
    "ledger_inconsistent": 4,
}

_HANDOFF_MESSAGES = {
    "missing_locked_still": "请先为每个镜头锁定一张有效关键帧",
    "active_job": "仍有关键帧生成任务处理中，请等待完成",
    "refund_pending": "仍有关键帧退款待确认，请等待账本收口",
    "charge_attempt_pending": "仍有关键帧扣点记录处理中，请稍后重试",
    "ledger_inconsistent": "关键帧账本关联异常，请刷新后重试",
}


def _blocker(code, shot_id=None):
    item = {"code": code, "message": _HANDOFF_MESSAGES[code]}
    if shot_id:
        item["shot_id"] = shot_id
    return item


def build_phase_two_handoff(conn, project_id, ratio):
    blockers = []
    shot_rows = conn.execute(
        "SELECT s.id FROM short_drama_shots s WHERE s.project_id=? "
        "AND NOT EXISTS (SELECT 1 FROM short_drama_assets a "
        "JOIN short_drama_asset_versions v "
        "ON v.asset_id=a.id AND v.version=a.current_version "
        "WHERE a.project_id=s.project_id AND a.shot_id=s.id AND a.type='still' "
        "AND a.locked=1 AND v.status='done' AND v.ratio=?) "
        "ORDER BY s.sort_order,s.id",
        (project_id, ratio),
    ).fetchall()
    if not conn.execute(
        "SELECT 1 FROM short_drama_shots WHERE project_id=? LIMIT 1", (project_id,),
    ).fetchone():
        blockers.append(_blocker("missing_locked_still"))
    blockers.extend(_blocker("missing_locked_still", row[0]) for row in shot_rows)

    for row in conn.execute(
        "SELECT shot_id,status,refunded FROM short_drama_production_jobs "
        "WHERE project_id=? ORDER BY shot_id,job_id", (project_id,),
    ).fetchall():
        if row[1] in {"pending", "running"}:
            blockers.append(_blocker("active_job", row[0]))
        if int(row[2] or 0) == 2:
            blockers.append(_blocker("refund_pending", row[0]))

    for row in conn.execute(
        "SELECT shot_id,state FROM short_drama_charge_attempts "
        "WHERE project_id=? AND state IN ('accepted','charged','refund_pending') "
        "ORDER BY shot_id,created_at,charge_key", (project_id,),
    ).fetchall():
        code = "refund_pending" if row[1] == "refund_pending" else "charge_attempt_pending"
        blockers.append(_blocker(code, row[0]))

    for row in conn.execute(
        "SELECT p.shot_id FROM short_drama_production_jobs p "
        "LEFT JOIN jobs j ON j.id=p.job_id AND j.username=p.username AND j.kind='image' "
        "WHERE p.project_id=? AND j.id IS NULL ORDER BY p.shot_id,p.job_id",
        (project_id,),
    ).fetchall():
        blockers.append(_blocker("ledger_inconsistent", row[0]))

    unique = {(item["code"], item.get("shot_id")): item for item in blockers}
    blockers = sorted(unique.values(), key=lambda item: (
        _HANDOFF_ORDER[item["code"]], item.get("shot_id", ""), item["message"],
    ))
    return {"blocked": bool(blockers), "blockers": blockers}


def _query_dicts(conn, query, params=()):
    cursor = conn.execute(query, params)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def build_production_snapshot(conn, project, username):
    project_id = project["id"]
    shots = _query_dicts(
        conn,
        "SELECT id, shot_key, sort_order, duration, image_prompt, character_keys_json "
        "FROM short_drama_shots WHERE project_id=? ORDER BY sort_order, id",
        (project_id,),
    )
    assets = {
        item["shot_id"]: item for item in _query_dicts(
            conn,
            "SELECT id, shot_id, current_version, locked FROM short_drama_assets "
            "WHERE project_id=? AND type='still' ORDER BY shot_id, id",
            (project_id,),
        )
    }
    versions_by_asset = {}
    for version in _query_dicts(
        conn,
        "SELECT v.id, v.asset_id, v.version, v.job_id, v.url, v.prompt, v.ratio, "
        "v.cost, v.status, v.created_at "
        "FROM short_drama_asset_versions v "
        "JOIN short_drama_assets a ON a.id=v.asset_id "
        "JOIN short_drama_shots s ON s.id=a.shot_id "
        "WHERE a.project_id=? AND a.type='still' "
        "ORDER BY s.sort_order, s.id, v.version, v.id",
        (project_id,),
    ):
        versions_by_asset.setdefault(version.pop("asset_id"), []).append(version)
    latest_jobs = {}
    latest_job_shots = set()
    reserved_points = 0
    for job in _query_dicts(
        conn,
        "SELECT p.id, p.shot_id, p.job_id, p.kind, p.status, p.quoted_cost, "
        "p.error, p.refunded "
        "FROM short_drama_production_jobs p "
        "JOIN jobs j ON j.id=p.job_id AND j.username=p.username AND j.kind='image' "
        "WHERE p.project_id=? "
        "ORDER BY p.job_id DESC",
        (project_id,),
    ):
        if job["status"] in {"pending", "running"}:
            reserved_points += int(job["quoted_cost"])
        refund_value = int(job.get("refunded") or 0)
        job["refunded"] = bool(refund_value == 1)
        job["refund_pending"] = bool(refund_value == 2)
        shot_id = job.pop("shot_id")
        if shot_id not in latest_job_shots:
            latest_job_shots.add(shot_id)
            if job["status"] != "done":
                latest_jobs[shot_id] = job

    reserved_points += int(conn.execute(
        "SELECT COALESCE(SUM(a.cost),0) FROM short_drama_charge_attempts a "
        "WHERE a.project_id=? AND a.state IN ('accepted','charged','refund_pending') "
        "AND (a.job_id IS NULL OR (a.state='refund_pending' AND NOT EXISTS ("
        "SELECT 1 FROM short_drama_production_jobs p WHERE p.job_id=a.job_id "
        "AND p.status IN ('pending','running'))))",
        (project_id,),
    ).fetchone()[0] or 0)

    shot_items = []
    for shot in shots:
        asset = assets[shot["id"]]
        shot_row = dict(shot)
        references = _project_references(conn, project, shot_row)
        shot_items.append({
            "id": shot["id"],
            "shot_key": shot["shot_key"],
            "sort_order": int(shot["sort_order"]),
            "duration": int(shot["duration"]),
            "image_prompt": shot["image_prompt"],
            "references": references,
            "still": {
                "asset_id": asset["id"],
                "current_version": (
                    None if asset["current_version"] is None else int(asset["current_version"])
                ),
                "locked": bool(asset["locked"]),
                "versions": versions_by_asset.get(asset["id"], []),
                "job": latest_jobs.get(shot["id"]),
            },
        })
    handoff = build_phase_two_handoff(conn, project_id, project["ratio"])
    return {
        "project_id": project_id,
        "revision": int(project["revision"]),
        "stage": project["stage"],
        "ratio": project["ratio"],
        "point_budget": int(project["point_budget"]),
        "spent_points": int(project["spent_points"]),
        "reserved_points": reserved_points,
        "handoff_blocked": handoff["blocked"],
        "handoff_blockers": handoff["blockers"],
        "shots": shot_items,
    }


def select_asset(db_factory, username, body, access=None):
    required_fields = {"project_id", "revision", "asset_id", "version", "lock"}
    if not isinstance(body, dict) or set(body) != required_fields:
        raise ValueError("asset selection request fields are invalid")
    if (type(body["project_id"]) is not str or not body["project_id"].strip()
            or type(body["asset_id"]) is not str or not body["asset_id"].strip()):
        raise ValueError("asset selection identifiers are invalid")
    if (type(body["revision"]) is not int or body["revision"] < 1
            or type(body["version"]) is not int or body["version"] < 1):
        raise ValueError("asset version is invalid")
    if type(body["lock"]) is not bool:
        raise ValueError("asset lock state is invalid")

    project_id = body["project_id"].strip()
    asset_id = body["asset_id"].strip()
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _authorized_project(conn, username, project_id, access, write=True)
        row = conn.execute(
            "SELECT p.revision, p.stage "
            "FROM short_drama_assets a "
            "JOIN short_drama_projects p ON p.id=a.project_id "
            "JOIN short_drama_shots s ON s.id=a.shot_id AND s.project_id=a.project_id "
            "JOIN short_drama_asset_versions v "
            "ON v.asset_id=a.id AND v.version=? "
            "WHERE a.id=? AND a.project_id=? AND a.type='still' "
            "AND p.deleted=0 AND v.status='done' AND v.ratio=p.ratio",
            (body["version"], asset_id, project_id),
        ).fetchone()
        if not row:
            raise LookupError("asset version does not exist")
        if int(row[0]) != body["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        if row[1] != "stills_review":
            raise ValueError("assets cannot be selected in the current stage")
        now = int(time.time())
        updated = conn.execute(
            "UPDATE short_drama_assets SET current_version=?, locked=?, updated_at=? "
            "WHERE id=? AND project_id=? AND type='still'",
            (body["version"], int(body["lock"]), now, asset_id, project_id),
        )
        if updated.rowcount != 1:
            raise LookupError("asset version does not exist")
        cur = conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1, updated_at=? "
            "WHERE id=? AND revision=? "
            "AND stage='stills_review' AND deleted=0",
            (now, project_id, body["revision"]),
        )
        if cur.rowcount != 1:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_production(db_factory, username, project_id, access=access)


def confirm_stage(db_factory, username, body, access=None):
    if not isinstance(body, dict) or set(body) != {"project_id", "revision", "stage"}:
        raise ValueError("production stage confirmation fields are invalid")
    if type(body["project_id"]) is not str or not body["project_id"].strip():
        raise ValueError("project identifier is invalid")
    if type(body["revision"]) is not int or body["revision"] < 1:
        raise ValueError("project revision is invalid")
    if type(body["stage"]) is not str or body["stage"] != "stills_review":
        raise ValueError("only the stills review stage can be confirmed")

    project_id = body["project_id"].strip()
    blocked_message = None
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _authorized_project(conn, username, project_id, access, write=True)
        if int(project["revision"]) != body["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        if project["stage"] != "stills_review":
            raise ValueError("short drama stages cannot be skipped")

        reconcile_jobs(conn, username, project_id)
        handoff = build_phase_two_handoff(conn, project_id, project["ratio"])
        if handoff["blocked"]:
            blocked_message = handoff["blockers"][0]["message"]
        else:
            short_drama_voice.ensure_voice_workspace(
                conn, project_id, allowed_stages={"stills_review"}
            )

            cur = conn.execute(
                "UPDATE short_drama_projects "
                "SET stage='voice_review', revision=revision+1, updated_at=? "
                "WHERE id=? AND revision=? "
                "AND stage='stills_review' AND deleted=0",
                (int(time.time()), project_id, body["revision"]),
            )
            if cur.rowcount != 1:
                from .short_drama import RevisionConflict
                raise RevisionConflict("project was updated; refresh and retry")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if blocked_message is not None:
        raise ValueError(blocked_message)
    return get_production(db_factory, username, project_id, access=access)


def get_production(db_factory, username, project_id, access=None):
    conn = db_factory()
    conn.row_factory = sqlite3.Row
    try:
        project = _authorized_project(conn, username, project_id, access, write=False)
        if project["stage"] not in PRODUCTION_STAGES:
            raise ValueError("短剧项目尚未进入素材制作")
        ensure_asset_slots(conn, project_id)
        reconcile_jobs(conn, username, project_id)
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND deleted=0", (project_id,)
        ).fetchone()
        snapshot = build_production_snapshot(conn, dict(project), username)
        conn.commit()
        return snapshot
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
