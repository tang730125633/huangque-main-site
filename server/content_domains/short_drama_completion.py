"""D-6 delivery readiness, atomic completion and immutable snapshots."""

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing

from . import short_drama_assembly_lipsync

CONFIRM_ENDPOINT = "/api/gen/short-drama/completion/confirm"
ACTIVE_JOB_STATES = {
    "pending", "queued", "running", "recovering", "uploading",
    "archiving", "submitted", "downloading", "metadata_pending",
}
UNSETTLED_ATTEMPT_STATES = {
    "accepted", "charged", "linked", "refund_pending",
}


class CompletionError(RuntimeError):
    def __init__(self, code, message, status=409, blockers=None):
        super().__init__(message)
        self.code = code
        self.status = int(status)
        self.blockers = list(blockers or [])


class ProjectCompleted(CompletionError):
    def __init__(self, message="项目已经完成并永久只读"):
        super().__init__("project_completed", message, 409)


class CompletionDisabled(CompletionError):
    def __init__(self):
        super().__init__("completion_disabled", "完成确认入口尚未开放", 503)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_completions (
  completion_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL UNIQUE
    REFERENCES short_drama_projects(id) ON DELETE RESTRICT,
  owner_username TEXT NOT NULL,
  completed_by TEXT NOT NULL,
  base_revision INTEGER NOT NULL,
  completed_revision INTEGER NOT NULL,
  final_version_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  delivery_hash TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  completed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS short_drama_completion_attempts (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  project_id TEXT NOT NULL
    REFERENCES short_drama_projects(id) ON DELETE RESTRICT,
  endpoint TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('started','committed','failed')),
  completion_id TEXT,
  response_json TEXT,
  error_code TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(actor_username, endpoint, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_completion_attempts_project
  ON short_drama_completion_attempts(project_id,state,updated_at);

CREATE TABLE IF NOT EXISTS short_drama_audit_events (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  event_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  actor_username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  board_id TEXT,
  entity_id TEXT,
  request_hash TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_audit_project
  ON short_drama_audit_events(project_id,created_at DESC);

CREATE TABLE IF NOT EXISTS short_drama_completion_migration_runs (
  run_id TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK(state IN ('applied','rolled_back')),
  actor_username TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS short_drama_completion_migration_items (
  run_id TEXT NOT NULL REFERENCES short_drama_completion_migration_runs(run_id)
    ON DELETE RESTRICT,
  project_id TEXT NOT NULL,
  completion_id TEXT NOT NULL,
  project_revision INTEGER NOT NULL,
  previous_completion_id TEXT,
  previous_completed_at INTEGER,
  previous_completed_by TEXT,
  state TEXT NOT NULL CHECK(state IN ('applied','rolled_back')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(run_id,project_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_short_drama_completion_migration_item
  ON short_drama_completion_migration_items(completion_id);
"""


_BLOCKER_MESSAGES = {
    "completion_stage_invalid": "项目尚未进入成片确认阶段",
    "final_version_missing": "缺少当前正式成片版本",
    "final_version_stale": "正式成片版本与项目当前指针不一致",
    "final_asset_not_ready": "正式视频或封面尚未完成归档",
    "media_verification_failed": "正式成片媒体规格或哈希校验未通过",
    "required_lock_missing": "角色、画面、配音字幕或视频锁定链不完整",
    "active_job": "项目仍有生成、上传或归档任务处理中",
    "billing_unsettled": "项目仍有扣点、退款或预留点数未结清",
    "forbidden": "仅项目 owner 可以确认完成",
}


_BLOCKER_MESSAGES.update({
    "lipsync_manifest_invalid": "口型合成证据清单格式无效",
    "lipsync_manifest_mismatch":
        "口型合成证据清单与不可变素材清单不一致",
})


def enabled():
    value = str(os.getenv("HQ_SHORT_DRAMA_COMPLETION_ENABLED", "0")).lower()
    return value in {"1", "true", "yes", "on"}


def reject_legacy_completion():
    """Reject legacy completion only after the D-6 rollout is enabled."""
    if enabled():
        raise CompletionError(
            "completion_required",
            "请使用 D-6 原子完成确认流程推进 completed",
            409,
        )
    return False


def _connection(db_factory):
    conn = db_factory()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_exists(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def _columns(conn, table):
    if not _table_exists(conn, table):
        return set()
    return {
        row[1] for row in conn.execute(
            "PRAGMA table_info(%s)" % table
        ).fetchall()
    }


def _json(value, default):
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return default
    return result if isinstance(result, type(default)) else default


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _blocker(code, domain, entity_id=None, recommended_action=None):
    return {
        "code": code,
        "domain": domain,
        "entity_id": entity_id,
        "message": _BLOCKER_MESSAGES[code],
        "recommended_action": recommended_action or "刷新交付检查后重试",
    }


def init_db(db_factory):
    with closing(_connection(db_factory)) as conn:
        conn.executescript(_SCHEMA)
        project_columns = _columns(conn, "short_drama_projects")
        for name, declaration in {
            "completion_id": "TEXT",
            "completed_at": "INTEGER",
            "completed_by": "TEXT",
        }.items():
            if name not in project_columns:
                conn.execute(
                    "ALTER TABLE short_drama_projects ADD COLUMN %s %s"
                    % (name, declaration)
                )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_short_drama_projects_completion_id "
            "ON short_drama_projects(completion_id) "
            "WHERE completion_id IS NOT NULL"
        )
        conn.commit()


def assert_project_writable(conn, project_id, owner_username=None):
    params = [str(project_id or "")]
    query = (
        "SELECT stage,completion_id FROM short_drama_projects "
        "WHERE id=? AND deleted=0"
    )
    if owner_username is not None:
        query += " AND username=?"
        params.append(owner_username)
    row = conn.execute(query, tuple(params)).fetchone()
    if not row:
        raise LookupError("短剧项目不存在")
    if row["stage"] == "completed" or row["completion_id"]:
        raise ProjectCompleted()
    return row


def _lock_summary(conn, project_id):
    shot_count = int(conn.execute(
        "SELECT COUNT(*) FROM short_drama_shots WHERE project_id=?",
        (project_id,),
    ).fetchone()[0])

    def count(table, where):
        if not _table_exists(conn, table):
            return 0
        return int(conn.execute(
            "SELECT COUNT(*) FROM %s WHERE project_id=? AND %s" % (table, where),
            (project_id,),
        ).fetchone()[0])

    return {
        "shot_count": shot_count,
        "still_locked": count(
            "short_drama_assets", "locked=1 AND current_version IS NOT NULL"
        ),
        "voice_locked": count("short_drama_voice_shots", "locked=1"),
        "video_locked": count(
            "short_drama_video_shots", "locked=1 AND current_version IS NOT NULL"
        ),
    }


def _active_jobs(conn, project_id):
    checks = (
        ("short_drama_production_jobs", "status"),
        ("short_drama_voice_jobs", "status"),
        ("short_drama_video_jobs", "status"),
        ("short_drama_composition_jobs", "status"),
    )
    found = []
    for table, status_column in checks:
        if not _table_exists(conn, table):
            continue
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        query = (
            "SELECT id,%s AS status FROM %s WHERE project_id=? "
            "AND %s IN (%s) LIMIT 5"
            % (status_column, table, status_column, placeholders)
        )
        for row in conn.execute(
            query, (project_id,) + tuple(sorted(ACTIVE_JOB_STATES))
        ):
            found.append({
                "table": table,
                "id": str(row["id"]),
                "status": str(row["status"]),
            })
    return found


def _unsettled_attempts(conn, project_id):
    tables = (
        "short_drama_charge_attempts",
        "short_drama_voice_charge_attempts",
        "short_drama_video_charge_attempts",
        "short_drama_final_attempts",
        "short_drama_provider_shot_attempts",
    )
    found = []
    for table in tables:
        if not _table_exists(conn, table) or "state" not in _columns(conn, table):
            continue
        placeholders = ",".join("?" for _ in UNSETTLED_ATTEMPT_STATES)
        rows = conn.execute(
            "SELECT state FROM %s WHERE project_id=? AND state IN (%s) LIMIT 5"
            % (table, placeholders),
            (project_id,) + tuple(sorted(UNSETTLED_ATTEMPT_STATES)),
        ).fetchall()
        found.extend({"table": table, "state": row["state"]} for row in rows)
    return found


def _completion_row(conn, project_id):
    return conn.execute(
        "SELECT * FROM short_drama_completions WHERE project_id=?",
        (project_id,),
    ).fetchone()


def _completion_payload(row, replayed=False):
    if not row:
        return None
    snapshot = _json(row["snapshot_json"], {})
    return {
        "completion_id": row["completion_id"],
        "project_id": row["project_id"],
        "stage": "completed",
        "revision": row["completed_revision"],
        "base_revision": row["base_revision"],
        "final_version_id": row["final_version_id"],
        "asset_id": row["asset_id"],
        "delivery_hash": row["delivery_hash"],
        "completed_by": row["completed_by"],
        "completed_at": row["completed_at"],
        "snapshot": snapshot,
        "replayed": bool(replayed),
    }


def _readiness_from_conn(
    conn, actor_username, owner_username, project_id, point_usage=None, now=None
):
    now = int(now or time.time())
    project = conn.execute(
        "SELECT * FROM short_drama_projects WHERE id=? AND username=? "
        "AND deleted=0",
        (project_id, owner_username),
    ).fetchone()
    if not project:
        raise LookupError("短剧项目不存在")
    completion = _completion_row(conn, project_id)
    if project["stage"] == "completed" and completion:
        return {
            "project_id": project_id,
            "revision": int(project["revision"]),
            "stage": "completed",
            "feature_enabled": enabled(),
            "ready": True,
            "blockers": [],
            "delivery_hash": completion["delivery_hash"],
            "checked_at": now,
            "completion": _completion_payload(completion),
        }

    blockers = []
    if project["stage"] != "assembly_review":
        blockers.append(_blocker(
            "completion_stage_invalid", "project", project_id,
            "返回当前阶段并完成前序验收",
        ))
    if actor_username != owner_username:
        blockers.append(_blocker(
            "forbidden", "permission", actor_username, "联系项目 owner 确认完成"
        ))

    composition = conn.execute(
        "SELECT * FROM short_drama_compositions WHERE project_id=?",
        (project_id,),
    ).fetchone()
    version = None
    if composition and composition["current_final_version"]:
        version = conn.execute(
            "SELECT * FROM short_drama_composition_versions "
            "WHERE project_id=? AND kind='final' AND version=? "
            "AND status='succeeded'",
            (project_id, composition["current_final_version"]),
        ).fetchone()
    if not version:
        blockers.append(_blocker(
            "final_version_missing", "version", None, "返回 D-4 生成正式成片"
        ))

    asset = None
    if version:
        asset = conn.execute(
            "SELECT * FROM short_drama_final_assets "
            "WHERE project_id=? AND composition_version_id=? "
            "AND archive_status='ready' AND deleted=0",
            (project_id, version["id"]),
        ).fetchone()
    if not asset:
        blockers.append(_blocker(
            "final_asset_not_ready", "asset", None, "等待 D-4 归档恢复完成"
        ))

    if version and asset:
        expected = (
            (1080, 1920) if project["ratio"] == "9:16" else (1920, 1080)
        )
        media_valid = (
            (int(asset["width"]), int(asset["height"])) == expected
            and int(asset["size"] or 0) > 0
            and bool(str(asset["sha256"] or ""))
            and bool(str(asset["object_key"] or ""))
            and bool(str(asset["cover_key"] or ""))
            and int(asset["duration_ms"] or 0) > 0
            and abs(
                int(asset["duration_ms"]) - int(project["target_duration"]) * 1000
            ) <= 1000
        )
        version_sha = str(version["sha256"] or "") if "sha256" in version.keys() else ""
        version_size = int(version["size"] or 0) if "size" in version.keys() else 0
        if version_sha and version_sha != str(asset["sha256"]):
            media_valid = False
        if version_size and version_size != int(asset["size"]):
            media_valid = False
        if not media_valid:
            blockers.append(_blocker(
                "media_verification_failed", "media", asset["id"],
                "返回 D-4 重新验证或归档正式成片",
            ))

    locks = _lock_summary(conn, project_id)
    if (
        locks["shot_count"] <= 0
        or locks["still_locked"] != locks["shot_count"]
        or locks["voice_locked"] != locks["shot_count"]
        or locks["video_locked"] != locks["shot_count"]
    ):
        blockers.append(_blocker(
            "required_lock_missing", "locks", project_id,
            "返回对应阶段完成全部镜头锁定",
        ))

    active_jobs = _active_jobs(conn, project_id)
    if active_jobs:
        blockers.append(_blocker(
            "active_job", "jobs", active_jobs[0]["id"], "等待活动任务进入终态"
        ))

    unsettled = _unsettled_attempts(conn, project_id)
    usage = (
        point_usage(conn, project_id)
        if callable(point_usage)
        else {"spent_points": int(project["spent_points"] or 0), "reserved_points": 0}
    )
    billing = {
        "spent_points": max(0, int(usage.get("spent_points") or 0)),
        "reserved_points": max(0, int(usage.get("reserved_points") or 0)),
        "unsettled_attempts": len(unsettled),
    }
    if billing["reserved_points"] or unsettled:
        blockers.append(_blocker(
            "billing_unsettled", "billing", project_id,
            "等待扣点或退款恢复完成后重新检查",
        ))

    lipsync_plan = None
    version_plan_hash = (
        str(version["plan_hash"] or "")
        if version is not None and "plan_hash" in version.keys() else ""
    )
    version_manifest = (
        _json(version["manifest_json"], {})
        if version is not None and "manifest_json" in version.keys() else {}
    )
    manifest_plan_hash = str(version_manifest.get("plan_hash") or "")
    if (
        short_drama_assembly_lipsync.completion_gate_enabled()
        or version_plan_hash
        or manifest_plan_hash
    ):
        try:
            lipsync_plan = short_drama_assembly_lipsync.load_plan(
                conn, project, require=True
            )
        except short_drama_assembly_lipsync.LipsyncAssemblyBlocked as error:
            blockers.append(_blocker(
                error.code, "lipsync", project_id, str(error)
            ))
        else:
            if version is not None:
                try:
                    short_drama_assembly_lipsync.validate_composition_manifest(
                        version["manifest_json"],
                        stored_manifest_hash=version["manifest_hash"],
                        persisted_plan_hash=version["plan_hash"],
                        expected_kind="final",
                        expected_project_id=project_id,
                        expected_input_hash=version["input_hash"],
                        plan=lipsync_plan,
                    )
                except (
                    short_drama_assembly_lipsync.LipsyncAssemblyBlocked
                ) as error:
                    blockers.append(_blocker(
                        error.code, "lipsync", project_id, str(error)
                    ))
    stable = {
        "project": {
            "id": project["id"],
            "revision": int(project["revision"]),
            "stage": project["stage"],
            "ratio": project["ratio"],
            "target_duration": int(project["target_duration"]),
            "board_id": project["board_id"],
        },
        "composition": {
            "assembly_revision": int(composition["assembly_revision"])
            if composition else None,
            "current_final_version": int(composition["current_final_version"])
            if composition and composition["current_final_version"] else None,
            "config": _json(composition["config_json"], {}) if composition else {},
        },
        "final_version": {
            key: version[key]
            for key in (
                "id", "version", "job_id", "input_hash", "duration_ms",
                "width", "height", "fps", "video_codec", "audio_codec",
                "sha256", "size",
                "plan_hash", "manifest_hash",
            )
            if version is not None and key in version.keys()
        },
        "asset": {
            key: asset[key]
            for key in (
                "id", "composition_version_id", "job_id", "object_key",
                "cover_key", "mime", "size", "sha256", "width", "height",
                "fps", "duration_ms", "video_codec", "audio_codec",
                "archive_status",
            )
            if asset is not None and key in asset.keys()
        },
        "locks": locks,
        "billing": billing,
        "lipsync": {
            "plan_hash": (
                lipsync_plan.get("plan_hash") if lipsync_plan else ""
            ),
            "dependency_hash": (
                lipsync_plan.get("dependency_hash") if lipsync_plan else ""
            ),
            "selected_sources": [
                {
                    key: item.get(key)
                    for key in (
                        "shot_id", "version_id", "version", "job_id",
                        "attempt_id", "provider", "model_version", "input_hash",
                        "file_hash", "dependency_hashes", "media_spec", "cost",
                        "locked_at", "locked_by",
                    )
                }
                for item in (
                    lipsync_plan.get("selected_sources") if lipsync_plan else []
                )
            ],
        },
    }
    return {
        "project_id": project_id,
        "revision": int(project["revision"]),
        "stage": project["stage"],
        "feature_enabled": enabled(),
        "ready": not blockers,
        "blockers": blockers,
        "delivery_hash": _digest(stable),
        "checked_at": now,
        "expires_at": now + 300,
        "final_version": stable["final_version"],
        "asset": stable["asset"],
        "locks": locks,
        "billing": billing,
        "_snapshot": stable,
    }


def readiness(
    db_factory, actor_username, owner_username, project_id,
    point_usage=None, now=None
):
    with closing(_connection(db_factory)) as conn:
        result = _readiness_from_conn(
            conn, actor_username, owner_username, project_id,
            point_usage=point_usage, now=now,
        )
    result.pop("_snapshot", None)
    return result


def get_completion(db_factory, owner_username, project_id):
    with closing(_connection(db_factory)) as conn:
        project = conn.execute(
            "SELECT id,stage,completion_id FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, owner_username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        row = _completion_row(conn, project_id)
        if not row:
            if project["stage"] == "completed":
                raise CompletionError(
                    "legacy_completion_pending_migration",
                    "历史完成项目尚未生成不可变交付快照，请先完成迁移或人工复核",
                    409,
                    [{
                        "code": "legacy_completion_pending_migration",
                        "domain": "completion",
                        "entity_id": project_id,
                        "message": "历史完成项目缺少不可变交付快照",
                        "recommended_action":
                            "运行历史完成项目迁移并处理 manual_review",
                    }],
                )
            raise CompletionError(
                "completion_not_found", "项目尚未完成交付确认", 404
            )
        return _completion_payload(row)


def confirm(
    db_factory, actor_username, owner_username, board_id, body,
    idempotency_key, point_usage=None, now=None, failure_hook=None
):
    if not enabled():
        raise CompletionDisabled()
    if not isinstance(body, dict) or set(body) != {
        "project_id", "revision", "final_version_id", "asset_id",
        "delivery_hash", "acknowledged",
    }:
        raise CompletionError("invalid_request", "确认完成请求字段不正确", 400)
    if body.get("acknowledged") is not True:
        raise CompletionError(
            "acknowledgement_required", "请先确认不可逆交付声明", 400
        )
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise CompletionError("idempotency_required", "缺少幂等键", 400)
    idempotency_key = idempotency_key.strip()[:160]
    project_id = str(body.get("project_id") or "")
    request_hash = _digest({
        "actor": actor_username,
        "owner": owner_username,
        "body": body,
    })
    timestamp = int(now or time.time())

    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            previous = conn.execute(
                "SELECT * FROM short_drama_completion_attempts "
                "WHERE actor_username=? AND endpoint=? AND idempotency_key=?",
                (actor_username, CONFIRM_ENDPOINT, idempotency_key),
            ).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise CompletionError(
                        "idempotency_conflict",
                        "同一幂等键不能用于不同确认请求",
                        409,
                    )
                if previous["state"] == "committed" and previous["response_json"]:
                    response = _json(previous["response_json"], {})
                    response["replayed"] = True
                    conn.commit()
                    return response

            if actor_username != owner_username:
                raise CompletionError(
                    "forbidden", "仅项目 owner 可以确认完成", 403
                )
            existing_completion = _completion_row(conn, project_id)
            if previous is None and existing_completion:
                conn.commit()
                return _completion_payload(existing_completion, replayed=True)
            if previous is None:
                conn.execute(
                    "INSERT INTO short_drama_completion_attempts "
                    "(id,actor_username,owner_username,project_id,endpoint,"
                    "idempotency_key,request_hash,state,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()), actor_username, owner_username,
                        project_id, CONFIRM_ENDPOINT, idempotency_key,
                        request_hash, "started", timestamp, timestamp,
                    ),
                )
            if callable(failure_hook):
                failure_hook("attempt_started")

            report = _readiness_from_conn(
                conn, actor_username, owner_username, project_id,
                point_usage=point_usage, now=timestamp,
            )
            if report["stage"] == "completed":
                completion = _completion_row(conn, project_id)
                response = _completion_payload(completion, replayed=True)
                conn.execute(
                    "UPDATE short_drama_completion_attempts SET state='committed',"
                    "completion_id=?,response_json=?,updated_at=? "
                    "WHERE actor_username=? AND endpoint=? AND idempotency_key=?",
                    (
                        response["completion_id"], _canonical(response), timestamp,
                        actor_username, CONFIRM_ENDPOINT, idempotency_key,
                    ),
                )
                conn.commit()
                return response
            if not report["ready"]:
                raise CompletionError(
                    report["blockers"][0]["code"],
                    "项目尚未满足完成条件",
                    409,
                    report["blockers"],
                )
            if callable(failure_hook):
                failure_hook("readiness_passed")
            if int(body.get("revision") or 0) != report["revision"]:
                raise CompletionError(
                    "revision_conflict", "项目版本已经变化", 409
                )
            if str(body.get("delivery_hash") or "") != report["delivery_hash"]:
                raise CompletionError(
                    "delivery_changed", "交付内容已经变化，请重新检查", 409
                )
            if str(body.get("final_version_id") or "") != str(
                report["final_version"].get("id") or ""
            ):
                raise CompletionError(
                    "delivery_changed", "正式成片版本已经变化", 409
                )
            if str(body.get("asset_id") or "") != str(
                report["asset"].get("id") or ""
            ):
                raise CompletionError(
                    "asset_changed", "正式资产已经变化", 409
                )

            completion_id = str(uuid.uuid4())
            completed_revision = report["revision"] + 1
            snapshot = dict(report["_snapshot"])
            snapshot["completion"] = {
                "completion_id": completion_id,
                "base_revision": report["revision"],
                "completed_revision": completed_revision,
                "completed_by": actor_username,
                "completed_at": timestamp,
                "delivery_hash": report["delivery_hash"],
            }
            conn.execute(
                "INSERT INTO short_drama_completions "
                "(completion_id,project_id,owner_username,completed_by,"
                "base_revision,completed_revision,final_version_id,asset_id,"
                "delivery_hash,snapshot_json,completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    completion_id, project_id, owner_username, actor_username,
                    report["revision"], completed_revision,
                    str(report["final_version"]["id"]),
                    str(report["asset"]["id"]), report["delivery_hash"],
                    _canonical(snapshot), timestamp,
                ),
            )
            if callable(failure_hook):
                failure_hook("snapshot_written")
            event_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO short_drama_audit_events "
                "(id,project_id,event_key,event_type,actor_username,"
                "owner_username,board_id,entity_id,request_hash,detail_json,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id, project_id, "completion:" + completion_id,
                    "completion_committed", actor_username, owner_username,
                    str(board_id or "") or None, completion_id, request_hash,
                    _canonical({
                        "base_revision": report["revision"],
                        "completed_revision": completed_revision,
                        "asset_id": report["asset"]["id"],
                        "final_version_id": report["final_version"]["id"],
                    }),
                    timestamp,
                ),
            )
            if callable(failure_hook):
                failure_hook("audit_written")
            cursor = conn.execute(
                "UPDATE short_drama_projects SET stage='completed',"
                "revision=?,completion_id=?,completed_at=?,completed_by=?,"
                "updated_at=? WHERE id=? AND username=? AND revision=? "
                "AND stage='assembly_review' AND completion_id IS NULL "
                "AND deleted=0",
                (
                    completed_revision, completion_id, timestamp,
                    actor_username, timestamp, project_id, owner_username,
                    report["revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise CompletionError(
                    "revision_conflict", "项目状态已经变化", 409
                )
            completion = _completion_row(conn, project_id)
            response = _completion_payload(completion)
            conn.execute(
                "UPDATE short_drama_completion_attempts SET state='committed',"
                "completion_id=?,response_json=?,updated_at=? "
                "WHERE actor_username=? AND endpoint=? AND idempotency_key=?",
                (
                    completion_id, _canonical(response), timestamp,
                    actor_username, CONFIRM_ENDPOINT, idempotency_key,
                ),
            )
            if callable(failure_hook):
                failure_hook("before_commit")
            conn.commit()
            return response
        except Exception:
            conn.rollback()
            raise


def _legacy_completion_evidence(conn, project):
    """Return authoritative legacy evidence or a manual-review reason."""
    project_id = project["id"]
    required_tables = {
        "short_drama_compositions",
        "short_drama_composition_versions",
        "short_drama_final_assets",
        "short_drama_final_attempts",
    }
    if any(not _table_exists(conn, table) for table in required_tables):
        return None, "legacy_schema_incomplete"
    composition = conn.execute(
        "SELECT * FROM short_drama_compositions WHERE project_id=?",
        (project_id,),
    ).fetchone()
    if not composition or not composition["current_final_version"]:
        return None, "legacy_final_version_missing"
    version = conn.execute(
        "SELECT * FROM short_drama_composition_versions "
        "WHERE project_id=? AND kind='final' AND version=? "
        "AND status='succeeded'",
        (project_id, composition["current_final_version"]),
    ).fetchone()
    if not version or not str(version["job_id"] or ""):
        return None, "legacy_final_version_missing"
    assets = conn.execute(
        "SELECT * FROM short_drama_final_assets "
        "WHERE project_id=? AND composition_version_id=? "
        "AND archive_status='ready' AND deleted=0",
        (project_id, version["id"]),
    ).fetchall()
    if len(assets) != 1:
        return None, "legacy_final_asset_invalid"
    asset = assets[0]
    expected = (1080, 1920) if project["ratio"] == "9:16" else (1920, 1080)
    media_valid = (
        str(asset["owner_username"] or "") == str(project["username"])
        and str(asset["job_id"] or "") == str(version["job_id"])
        and (int(asset["width"]), int(asset["height"])) == expected
        and int(asset["size"] or 0) > 0
        and len(str(asset["sha256"] or "")) == 64
        and bool(str(asset["object_key"] or ""))
        and bool(str(asset["cover_key"] or ""))
        and int(asset["duration_ms"] or 0) > 0
        and abs(
            int(asset["duration_ms"])
            - int(project["target_duration"]) * 1000
        ) <= 1000
    )
    if "sha256" in version.keys() and str(version["sha256"] or ""):
        media_valid = (
            media_valid
            and str(version["sha256"]) == str(asset["sha256"])
        )
    if "size" in version.keys() and version["size"] is not None:
        media_valid = media_valid and int(version["size"]) == int(asset["size"])
    if not media_valid:
        return None, "legacy_final_asset_invalid"
    attempts = conn.execute(
        "SELECT * FROM short_drama_final_attempts "
        "WHERE project_id=? AND state='archived' AND job_id=? AND asset_id=?",
        (project_id, str(version["job_id"]), str(asset["id"])),
    ).fetchall()
    if len(attempts) != 1:
        return None, "legacy_final_attempt_missing"
    if _active_jobs(conn, project_id):
        return None, "legacy_active_job"
    if _unsettled_attempts(conn, project_id):
        return None, "legacy_billing_unsettled"
    attempt = attempts[0]
    evidence = {
        "project": {
            "id": project_id,
            "revision": int(project["revision"]),
            "stage": "completed",
            "ratio": project["ratio"],
            "target_duration": int(project["target_duration"]),
            "board_id": project["board_id"],
            "legacy_completed_at": int(project["updated_at"] or 0),
        },
        "composition": {
            "assembly_revision": int(composition["assembly_revision"]),
            "current_final_version": int(composition["current_final_version"]),
            "config": _json(composition["config_json"], {}),
        },
        "final_version": {
            key: version[key]
            for key in (
                "id", "version", "job_id", "input_hash", "duration_ms",
                "width", "height", "fps", "video_codec", "audio_codec",
                "sha256", "size",
            )
            if key in version.keys()
        },
        "asset": {
            key: asset[key]
            for key in (
                "id", "composition_version_id", "job_id", "object_key",
                "cover_key", "mime", "size", "sha256", "width", "height",
                "fps", "duration_ms", "video_codec", "audio_codec",
                "archive_status",
            )
            if key in asset.keys()
        },
        "legacy_evidence": {
            "source": "assembly_confirm",
            "source_endpoint": "/api/gen/short-drama/assembly/confirm",
            "evidence_version": 1,
            "final_attempt_id": attempt["id"],
            "final_attempt_state": attempt["state"],
        },
    }
    return evidence, None


def migrate_legacy_completions(
    db_factory, limit=64, apply=False, now=None,
    actor_username="system:legacy-completion-migration",
    run_id=None,
):
    """Backfill only legacy completed projects with a complete evidence chain."""
    timestamp = int(now or time.time())
    migration_run_id = str(run_id or uuid.uuid4()) if apply else None
    with closing(_connection(db_factory)) as conn:
        if not _table_exists(conn, "short_drama_completions"):
            raise CompletionError(
                "completion_schema_missing",
                "D-6 数据表尚未初始化，不能迁移历史完成项目",
                503,
            )
        if apply:
            conn.execute("BEGIN IMMEDIATE")
            existing_run = conn.execute(
                "SELECT state,report_json FROM "
                "short_drama_completion_migration_runs WHERE run_id=?",
                (migration_run_id,),
            ).fetchone()
            if existing_run:
                if existing_run["state"] != "applied":
                    raise CompletionError(
                        "migration_run_closed",
                        "该迁移批次已经回滚，不能使用相同 run_id 重做",
                        409,
                    )
                report = _json(existing_run["report_json"], {})
                report["replayed"] = True
                conn.commit()
                return report
            conn.execute(
                "INSERT INTO short_drama_completion_migration_runs "
                "(run_id,state,actor_username,report_json,created_at,updated_at) "
                "VALUES (?,'applied',?,'{}',?,?)",
                (migration_run_id, actor_username, timestamp, timestamp),
            )
        projects = conn.execute(
            "SELECT * FROM short_drama_projects "
            "WHERE stage='completed' AND completion_id IS NULL "
            "AND deleted=0 ORDER BY updated_at,id LIMIT ?",
            (max(1, min(1000, int(limit or 64))),),
        ).fetchall()
        report = {
            "dry_run": not bool(apply),
            "run_id": migration_run_id,
            "state": "dry_run" if not apply else "applied",
            "replayed": False,
            "scanned": len(projects),
            "eligible": 0,
            "migrated": 0,
            "manual_review": [],
        }
        for project in projects:
            existing = _completion_row(conn, project["id"])
            if existing:
                report["manual_review"].append({
                    "project_id": project["id"],
                    "code": "legacy_snapshot_pointer_mismatch",
                })
                continue
            evidence, reason = _legacy_completion_evidence(conn, project)
            if reason:
                report["manual_review"].append({
                    "project_id": project["id"], "code": reason,
                })
                continue
            report["eligible"] += 1
            if not apply:
                continue
            completion_id = str(uuid.uuid4())
            completed_revision = int(project["revision"])
            base_revision = max(1, completed_revision - 1)
            delivery_hash = _digest(evidence)
            completed_at = int(project["updated_at"] or timestamp)
            snapshot = dict(evidence)
            snapshot["completion"] = {
                "completion_id": completion_id,
                "base_revision": base_revision,
                "completed_revision": completed_revision,
                "completed_by": project["username"],
                "completed_at": completed_at,
                "delivery_hash": delivery_hash,
                "legacy_migration": True,
                "migration_run_id": migration_run_id,
                "migrated_at": timestamp,
                "migrated_by": actor_username,
            }
            conn.execute(
                "INSERT INTO short_drama_completions "
                "(completion_id,project_id,owner_username,completed_by,"
                "base_revision,completed_revision,final_version_id,asset_id,"
                "delivery_hash,snapshot_json,completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    completion_id, project["id"], project["username"],
                    project["username"], base_revision, completed_revision,
                    str(evidence["final_version"]["id"]),
                    str(evidence["asset"]["id"]), delivery_hash,
                    _canonical(snapshot), completed_at,
                ),
            )
            request_hash = _digest({
                "project_id": project["id"],
                "completion_id": completion_id,
                "delivery_hash": delivery_hash,
            })
            conn.execute(
                "INSERT INTO short_drama_audit_events "
                "(id,project_id,event_key,event_type,actor_username,"
                "owner_username,board_id,entity_id,request_hash,detail_json,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), project["id"],
                    "legacy-completion:" + completion_id,
                    "legacy_completion_migrated", actor_username,
                    project["username"], project["board_id"], completion_id,
                    request_hash, _canonical({
                        "legacy_migration": True,
                        "migration_run_id": migration_run_id,
                        "source_endpoint":
                            "/api/gen/short-drama/assembly/confirm",
                        "final_version_id": evidence["final_version"]["id"],
                        "asset_id": evidence["asset"]["id"],
                    }), timestamp,
                ),
            )
            updated = conn.execute(
                "UPDATE short_drama_projects SET completion_id=?,"
                "completed_at=COALESCE(completed_at,?),"
                "completed_by=COALESCE(completed_by,?) "
                "WHERE id=? AND stage='completed' AND completion_id IS NULL",
                (
                    completion_id, completed_at, project["username"],
                    project["id"],
                ),
            )
            if updated.rowcount != 1:
                raise CompletionError(
                    "legacy_migration_conflict",
                    "历史完成项目在迁移期间发生变化",
                    409,
                )
            conn.execute(
                "INSERT INTO short_drama_completion_migration_items "
                "(run_id,project_id,completion_id,project_revision,"
                "previous_completion_id,previous_completed_at,"
                "previous_completed_by,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,'applied',?,?)",
                (
                    migration_run_id, project["id"], completion_id,
                    int(project["revision"]), project["completion_id"],
                    project["completed_at"], project["completed_by"],
                    timestamp, timestamp,
                ),
            )
            report["migrated"] += 1
        if apply:
            conn.execute(
                "UPDATE short_drama_completion_migration_runs "
                "SET report_json=?,updated_at=? WHERE run_id=?",
                (_canonical(report), timestamp, migration_run_id),
            )
            conn.commit()
        return report


def verify_legacy_completions(db_factory, run_id=None, now=None):
    """Verify completion pointers, snapshots and an optional migration run."""
    timestamp = int(now or time.time())
    issues = []
    with closing(_connection(db_factory)) as conn:
        rows = conn.execute(
            "SELECT p.id,p.completion_id,c.completion_id AS snapshot_id "
            "FROM short_drama_projects p "
            "LEFT JOIN short_drama_completions c ON c.project_id=p.id "
            "WHERE p.stage='completed' AND (p.completion_id IS NULL "
            "OR c.completion_id IS NULL OR p.completion_id<>c.completion_id) "
            "ORDER BY p.updated_at,p.id"
        ).fetchall()
        for row in rows:
            issues.append({
                "project_id": row["id"],
                "code": "completed_snapshot_mismatch",
            })
        orphaned = conn.execute(
            "SELECT c.project_id FROM short_drama_completions c "
            "LEFT JOIN short_drama_projects p ON p.id=c.project_id "
            "WHERE p.id IS NULL OR p.stage<>'completed' "
            "OR p.completion_id IS NULL OR p.completion_id<>c.completion_id "
            "ORDER BY c.project_id"
        ).fetchall()
        for row in orphaned:
            issues.append({
                "project_id": row["project_id"],
                "code": "snapshot_project_mismatch",
            })
        run = None
        item_count = 0
        if run_id:
            run = conn.execute(
                "SELECT state FROM short_drama_completion_migration_runs "
                "WHERE run_id=?", (str(run_id),),
            ).fetchone()
            if not run:
                issues.append({
                    "project_id": None, "code": "migration_run_not_found",
                })
            else:
                item_count = int(conn.execute(
                    "SELECT COUNT(*) FROM "
                    "short_drama_completion_migration_items "
                    "WHERE run_id=? AND state='applied'",
                    (str(run_id),),
                ).fetchone()[0])
                if run["state"] != "applied":
                    issues.append({
                        "project_id": None, "code": "migration_run_not_applied",
                    })
    return {
        "ok": not issues,
        "checked_at": timestamp,
        "run_id": str(run_id) if run_id else None,
        "run_state": run["state"] if run else None,
        "applied_items": item_count,
        "issues": issues,
    }


def rollback_legacy_completions(
    db_factory, run_id, now=None,
    actor_username="system:legacy-completion-migration",
):
    """Atomically roll back one applied migration batch without changing stage."""
    migration_run_id = str(run_id or "").strip()
    if not migration_run_id:
        raise CompletionError("migration_run_required", "缺少迁移 run_id", 400)
    timestamp = int(now or time.time())
    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute(
            "SELECT state,report_json FROM short_drama_completion_migration_runs "
            "WHERE run_id=?", (migration_run_id,),
        ).fetchone()
        if not run:
            raise CompletionError(
                "migration_run_not_found", "迁移批次不存在", 404,
            )
        if run["state"] == "rolled_back":
            report = _json(run["report_json"], {})
            report["replayed"] = True
            conn.commit()
            return report
        items = conn.execute(
            "SELECT * FROM short_drama_completion_migration_items "
            "WHERE run_id=? AND state='applied' ORDER BY project_id",
            (migration_run_id,),
        ).fetchall()
        blockers = []
        for item in items:
            project = conn.execute(
                "SELECT stage,revision,completion_id FROM short_drama_projects "
                "WHERE id=?", (item["project_id"],),
            ).fetchone()
            completion = conn.execute(
                "SELECT completion_id,snapshot_json FROM short_drama_completions "
                "WHERE project_id=?", (item["project_id"],),
            ).fetchone()
            snapshot = _json(completion["snapshot_json"], {}) if completion else {}
            migration = snapshot.get("completion") or {}
            if (
                not project
                or project["stage"] != "completed"
                or int(project["revision"]) != int(item["project_revision"])
                or project["completion_id"] != item["completion_id"]
                or not completion
                or completion["completion_id"] != item["completion_id"]
                or migration.get("migration_run_id") != migration_run_id
            ):
                blockers.append({
                    "project_id": item["project_id"],
                    "code": "migration_rollback_conflict",
                })
        if blockers:
            conn.rollback()
            raise CompletionError(
                "migration_rollback_blocked",
                "迁移后数据已经变化，不能自动回滚",
                409,
                blockers,
            )
        for item in items:
            updated = conn.execute(
                "UPDATE short_drama_projects SET completion_id=?,"
                "completed_at=?,completed_by=? WHERE id=? "
                "AND stage='completed' AND revision=? AND completion_id=?",
                (
                    item["previous_completion_id"],
                    item["previous_completed_at"],
                    item["previous_completed_by"], item["project_id"],
                    item["project_revision"], item["completion_id"],
                ),
            )
            if updated.rowcount != 1:
                raise CompletionError(
                    "migration_rollback_conflict",
                    "回滚期间项目状态发生变化",
                    409,
                )
            conn.execute(
                "DELETE FROM short_drama_completions "
                "WHERE project_id=? AND completion_id=?",
                (item["project_id"], item["completion_id"]),
            )
            conn.execute(
                "UPDATE short_drama_completion_migration_items "
                "SET state='rolled_back',updated_at=? "
                "WHERE run_id=? AND project_id=?",
                (timestamp, migration_run_id, item["project_id"]),
            )
            conn.execute(
                "INSERT INTO short_drama_audit_events "
                "(id,project_id,event_key,event_type,actor_username,"
                "owner_username,entity_id,detail_json,created_at) "
                "SELECT ?,p.id,?,'legacy_completion_migration_rolled_back',"
                "?,p.username,?, ?,? FROM short_drama_projects p WHERE p.id=?",
                (
                    str(uuid.uuid4()),
                    "legacy-completion-rollback:%s:%s" % (
                        migration_run_id, item["project_id"],
                    ),
                    actor_username, migration_run_id,
                    _canonical({
                        "migration_run_id": migration_run_id,
                        "completion_id": item["completion_id"],
                    }),
                    timestamp, item["project_id"],
                ),
            )
        report = {
            "run_id": migration_run_id,
            "state": "rolled_back",
            "rolled_back": len(items),
            "replayed": False,
        }
        conn.execute(
            "UPDATE short_drama_completion_migration_runs "
            "SET state='rolled_back',report_json=?,updated_at=? WHERE run_id=?",
            (_canonical(report), timestamp, migration_run_id),
        )
        conn.commit()
        return report


def reconcile_attempts(db_factory, limit=64):
    """Report incomplete attempts without inventing completion data."""
    with closing(_connection(db_factory)) as conn:
        rows = conn.execute(
            "SELECT id,project_id FROM short_drama_completion_attempts "
            "WHERE state='started' ORDER BY updated_at,id LIMIT ?",
            (max(1, min(1000, int(limit or 64))),),
        ).fetchall()
        recovered = 0
        inconsistent = 0
        for row in rows:
            completion = _completion_row(conn, row["project_id"])
            project = conn.execute(
                "SELECT stage,completion_id FROM short_drama_projects WHERE id=?",
                (row["project_id"],),
            ).fetchone()
            if completion and project and project["stage"] == "completed":
                response = _completion_payload(completion, replayed=True)
                conn.execute(
                    "UPDATE short_drama_completion_attempts SET state='committed',"
                    "completion_id=?,response_json=?,updated_at=? WHERE id=?",
                    (
                        completion["completion_id"], _canonical(response),
                        int(time.time()), row["id"],
                    ),
                )
                recovered += 1
            elif project and project["stage"] == "completed":
                inconsistent += 1
        conn.commit()
        return {
            "scanned": len(rows),
            "recovered": recovered,
            "inconsistent": inconsistent,
        }
