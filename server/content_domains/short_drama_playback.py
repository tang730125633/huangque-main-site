"""PR-D single-file playback bundles and zero-cost remux jobs."""

import json
import sqlite3
import time
import uuid
from contextlib import closing

from . import short_drama_playback_hashes as hashes


class PlaybackError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_playback_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version >= 1),
  source_version_id TEXT NOT NULL
    REFERENCES short_drama_composition_versions(id) ON DELETE RESTRICT,
  timeline_version_id TEXT NOT NULL
    REFERENCES short_drama_timeline_versions(id) ON DELETE RESTRICT,
  media_hash TEXT NOT NULL,
  subtitle_hash TEXT NOT NULL,
  bundle_hash TEXT NOT NULL,
  media_file TEXT NOT NULL,
  media_url TEXT NOT NULL,
  subtitle_file TEXT NOT NULL,
  subtitle_url TEXT NOT NULL,
  duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
  subtitle_delivery TEXT NOT NULL DEFAULT 'external_vtt',
  status TEXT NOT NULL CHECK (status IN ('ready','stale','failed')),
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(project_id, version),
  UNIQUE(project_id, bundle_hash)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_playback_versions_project
  ON short_drama_playback_versions(project_id, version DESC);

CREATE TABLE IF NOT EXISTS short_drama_playback_current (
  project_id TEXT PRIMARY KEY REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  version_id TEXT NOT NULL REFERENCES short_drama_playback_versions(id),
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS short_drama_playback_jobs (
  id TEXT PRIMARY KEY,
  actor TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  source_version_id TEXT NOT NULL,
  timeline_version_id TEXT NOT NULL,
  job_id TEXT NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('queued','running','succeeded','failed')
  ),
  error_code TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  finished_at INTEGER,
  UNIQUE(actor, idempotency_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_short_drama_playback_jobs_active
  ON short_drama_playback_jobs(project_id)
  WHERE status IN ('queued','running');
"""


def init_db(db_factory):
    with closing(db_factory()) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()


def _json(value, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed


def _version(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "version": row["version"],
        "source_version_id": row["source_version_id"],
        "timeline_version_id": row["timeline_version_id"],
        "media_hash": row["media_hash"],
        "subtitle_hash": row["subtitle_hash"],
        "bundle_hash": row["bundle_hash"],
        "media_url": row["media_url"],
        "subtitle_url": row["subtitle_url"],
        "duration_ms": row["duration_ms"],
        "subtitle_delivery": row["subtitle_delivery"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def _job(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "job_id": int(row["job_id"]),
        "project_id": row["project_id"],
        "source_version_id": row["source_version_id"],
        "timeline_version_id": row["timeline_version_id"],
        "status": row["status"],
        "cost": 0,
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
    }


def _source_context(conn, project, source_version_id=None):
    from . import short_drama_assembly, short_drama_timeline

    project_id = project["id"]
    source = conn.execute(
        "SELECT version.* FROM short_drama_composition_versions version "
        "LEFT JOIN short_drama_compositions composition "
        "ON composition.project_id=version.project_id "
        "WHERE version.project_id=? AND version.kind='preview' "
        "AND version.status='succeeded' AND ("
        "?='' AND version.version=composition.current_preview_version "
        "OR version.id=?) ORDER BY version.version DESC LIMIT 1",
        (project_id, source_version_id or "", source_version_id or ""),
    ).fetchone()
    if not source:
        raise PlaybackError(
            "playback_source_missing", "没有可用于播放包的 720p 预览版本", 409
        )
    config = _json(source["config_json"], {})
    delivery = str(
        (config.get("subtitle") or {}).get("delivery") or "burned_ass"
    )
    if delivery != "external_vtt":
        raise PlaybackError(
            "legacy_burned_subtitle",
            "该历史预览已烧录字幕，需重新生成 720p 预览后才能开关字幕",
            409,
        )
    authoritative = short_drama_timeline._authoritative_source(conn, project)
    timeline = short_drama_timeline._current(
        conn, project_id, authoritative
    )
    if not timeline:
        raise PlaybackError(
            "timeline_not_ready", "主时间线尚未确认，不能创建播放包", 409
        )
    if timeline["effective_status"] != "ready":
        raise PlaybackError(
            "timeline_stale",
            "主时间线已失效，请重新确认后再创建播放包",
            409,
        )
    assembly = short_drama_assembly.build_assembly_snapshot(conn, project)
    if (
        not assembly.get("input_hash")
        or source["input_hash"] != assembly["input_hash"]
    ):
        raise PlaybackError(
            "playback_source_stale",
            "所选预览与当前音画字幕输入不一致，请重新生成预览",
            409,
        )
    source_identity = short_drama_assembly._source_identity(
        project,
        short_drama_assembly._collect_sources(conn, project_id),
    )
    return source, timeline, source_identity


def _context_is_current(
    conn, project, source, timeline, source_identity
):
    from . import short_drama_assembly, short_drama_timeline

    current_source = conn.execute(
        "SELECT id,input_hash,status FROM short_drama_composition_versions "
        "WHERE id=? AND project_id=? AND kind='preview'",
        (source["id"], project["id"]),
    ).fetchone()
    if (
        not current_source
        or current_source["status"] != "succeeded"
        or current_source["input_hash"] != source["input_hash"]
    ):
        return False
    if short_drama_assembly._source_identity(
        project,
        short_drama_assembly._collect_sources(conn, project["id"]),
    ) != source_identity:
        return False
    authoritative = short_drama_timeline._authoritative_source(conn, project)
    current_timeline = short_drama_timeline._current(
        conn, project["id"], authoritative
    )
    return bool(
        current_timeline
        and current_timeline["effective_status"] == "ready"
        and current_timeline["id"] == timeline["id"]
        and current_timeline["timeline_hash"] == timeline["timeline_hash"]
        and current_timeline["source_hashes"] == timeline["source_hashes"]
    )


def _request_identity(source, timeline):
    source_hashes = dict(timeline.get("source_hashes") or {})
    cues = list(timeline.get("subtitle_cues") or [])
    media_identity = {
        "source_version_id": source["id"],
        "input_hash": source["input_hash"],
        "duration_ms": source["duration_ms"],
        "video_codec": source["video_codec"],
        "audio_codec": source["audio_codec"],
    }
    subtitle_identity = {
        "timeline_version_id": timeline["id"],
        "timeline_hash": timeline["timeline_hash"],
        "subtitle_cues": cues,
    }
    media_hash = hashes.media_hash(
        composition_input_hash=source["input_hash"],
        master_audio_hash=source_hashes.get("master_audio_hash", ""),
        ratio=source["width"] and source["height"]
        and f"{source['width']}:{source['height']}" or "",
        profile="short_drama_preview_v1",
    )
    subtitle_hash = hashes.subtitle_hash(
        alignment_version=timeline["id"],
        transcript_hash=source_hashes.get("transcript_hash", ""),
        timeline_hash=timeline["timeline_hash"],
        cues=cues,
    )
    return {
        "media_identity": media_identity,
        "subtitle_identity": subtitle_identity,
        "media_hash": media_hash,
        "subtitle_hash": subtitle_hash,
        "bundle_hash": hashes.bundle_hash(media_hash, subtitle_hash),
    }


def reconcile_job(db_factory, job_id):
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        linked = conn.execute(
            "SELECT * FROM short_drama_playback_jobs WHERE job_id=?",
            (str(job_id),),
        ).fetchone()
        generic = conn.execute(
            "SELECT status,result,error FROM jobs "
            "WHERE id=? AND kind='short_drama_remux'",
            (job_id,),
        ).fetchone()
        if not linked or not generic or linked["status"] in {"succeeded", "failed"}:
            conn.commit()
            return False
        if generic["status"] == "done":
            result = _json(generic["result"], {})
            required = {
                "media_file", "media_url", "subtitle_file", "subtitle_url",
                "duration_ms", "media_hash", "subtitle_hash", "bundle_hash",
            }
            if not required.issubset(result):
                conn.execute(
                    "UPDATE short_drama_playback_jobs SET status='failed',"
                    "error_code='remux_result_invalid',"
                    "error_message='轻量重封装结果字段不完整',updated_at=?,"
                    "finished_at=? WHERE job_id=?",
                    (now, now, str(job_id)),
                )
                conn.commit()
                return True
            if required.issubset(result):
                existing = conn.execute(
                    "SELECT * FROM short_drama_playback_versions "
                    "WHERE project_id=? AND bundle_hash=?",
                    (linked["project_id"], result["bundle_hash"]),
                ).fetchone()
                if existing:
                    version_id = existing["id"]
                else:
                    number = int(conn.execute(
                        "SELECT COALESCE(MAX(version),0)+1 "
                        "FROM short_drama_playback_versions WHERE project_id=?",
                        (linked["project_id"],),
                    ).fetchone()[0])
                    version_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO short_drama_playback_versions "
                        "(id,project_id,version,source_version_id,"
                        "timeline_version_id,media_hash,subtitle_hash,bundle_hash,"
                        "media_file,media_url,subtitle_file,subtitle_url,"
                        "duration_ms,subtitle_delivery,status,created_by,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'external_vtt',"
                        "'ready',?,?)",
                        (
                            version_id, linked["project_id"], number,
                            linked["source_version_id"],
                            linked["timeline_version_id"],
                            result["media_hash"], result["subtitle_hash"],
                            result["bundle_hash"], result["media_file"],
                            result["media_url"], result["subtitle_file"],
                            result["subtitle_url"], result["duration_ms"],
                            linked["actor"], now,
                        ),
                    )
                conn.execute(
                    "INSERT INTO short_drama_playback_current "
                    "(project_id,version_id,updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(project_id) DO UPDATE SET "
                    "version_id=excluded.version_id,updated_at=excluded.updated_at",
                    (linked["project_id"], version_id, now),
                )
                conn.execute(
                    "UPDATE short_drama_playback_jobs SET status='succeeded',"
                    "updated_at=?,finished_at=? WHERE job_id=?",
                    (now, now, str(job_id)),
                )
                conn.commit()
                return True
        if generic["status"] == "error":
            conn.execute(
                "UPDATE short_drama_playback_jobs SET status='failed',"
                "error_code='remux_failed',error_message=?,updated_at=?,"
                "finished_at=? WHERE job_id=?",
                (str(generic["error"] or "轻量重封装失败")[:300],
                 now, now, str(job_id)),
            )
            conn.commit()
            return True
        if generic["status"] == "running" and linked["status"] == "queued":
            conn.execute(
                "UPDATE short_drama_playback_jobs SET status='running',"
                "updated_at=? WHERE job_id=?",
                (now, str(job_id)),
            )
        conn.commit()
        return False


def get_snapshot(db_factory, owner, project_id):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        project = conn.execute(
            "SELECT id FROM short_drama_projects WHERE id=? AND username=? "
            "AND deleted=0", (project_id, owner),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        active_ids = [
            int(row["job_id"]) for row in conn.execute(
                "SELECT job_id FROM short_drama_playback_jobs "
                "WHERE project_id=? AND status IN ('queued','running')",
                (project_id,),
            )
        ]
    for job_id in active_ids:
        reconcile_job(db_factory, job_id)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT version.* FROM short_drama_playback_versions version "
            "WHERE version.project_id=? ORDER BY version.version DESC",
            (project_id,),
        ).fetchall()
        current = conn.execute(
            "SELECT version.* FROM short_drama_playback_current current "
            "JOIN short_drama_playback_versions version "
            "ON version.id=current.version_id WHERE current.project_id=?",
            (project_id,),
        ).fetchone()
        active = conn.execute(
            "SELECT * FROM short_drama_playback_jobs WHERE project_id=? "
            "AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        legacy = conn.execute(
            "SELECT id,version FROM short_drama_composition_versions "
            "WHERE project_id=? AND kind='preview' AND status='succeeded' "
            "ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return {
            "contract_version": hashes.CONTRACT_VERSION,
            "project_id": project_id,
            "current_version": _version(current),
            "versions": [_version(row) for row in rows],
            "active_job": _job(active),
            "legacy_fallback": (
                {"source_version_id": legacy["id"], "version": legacy["version"]}
                if legacy and not rows else None
            ),
            "subtitle_toggle_supported": bool(current),
            "cost": 0,
        }


def create_remux_job(
    db_factory, actor, owner, body, idempotency_key, enqueue=None
):
    if not isinstance(body, dict) or set(body) - {
        "project_id", "source_version_id"
    }:
        raise ValueError("重封装请求字段不正确")
    project_id = str(body.get("project_id") or "").strip()
    source_version_id = str(body.get("source_version_id") or "").strip()
    key = str(idempotency_key or "").strip()
    if not project_id or not key or len(key) > 180:
        raise ValueError("项目 ID 或 Idempotency-Key 无效")
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? "
            "AND deleted=0", (project_id, owner),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        source, timeline, source_identity = _source_context(
            conn, project, source_version_id
        )
        identity = _request_identity(source, timeline)
        request_hash = hashes.canonical_hash({
            "project_id": project_id,
            "source_version_id": source["id"],
            "timeline_version_id": timeline["id"],
            "bundle_hash": identity["bundle_hash"],
        })
        cues = list(timeline.get("subtitle_cues") or [])
        payload = {
            "mode": "short_drama_remux",
            "project_id": project_id,
            "source_version_id": source["id"],
            "source_file": source["file"],
            "duration_ms": source["duration_ms"],
            "timeline_version_id": timeline["id"],
            "subtitle_cues": cues,
            **identity,
        }
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM short_drama_playback_jobs "
            "WHERE actor=? AND idempotency_key=?",
            (actor, key),
        ).fetchone()
        if existing:
            conn.commit()
            if existing["request_hash"] != request_hash:
                raise PlaybackError(
                    "idempotency_conflict",
                    "同一 Idempotency-Key 不能用于不同重封装请求", 409
                )
            return {**_job(existing), "replayed": True}
        locked_project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? "
            "AND deleted=0", (project_id, owner),
        ).fetchone()
        if (
            not locked_project
            or not _context_is_current(
                conn, locked_project, source, timeline, source_identity
            )
        ):
            conn.rollback()
            raise PlaybackError(
                "playback_source_stale",
                "预览或主时间线已更新，请刷新后重新创建播放包",
                409,
            )
        ready = conn.execute(
            "SELECT * FROM short_drama_playback_versions "
            "WHERE project_id=? AND bundle_hash=?",
            (project_id, identity["bundle_hash"]),
        ).fetchone()
        if ready:
            conn.commit()
            return {
                "project_id": project_id, "status": "succeeded",
                "version": _version(ready), "cost": 0, "replayed": True,
            }
        active = conn.execute(
            "SELECT job_id FROM short_drama_playback_jobs "
            "WHERE project_id=? AND status IN ('queued','running')",
            (project_id,),
        ).fetchone()
        if active:
            conn.rollback()
            raise PlaybackError(
                "active_remux_job", "该项目已有重封装任务正在处理", 409
            )
        cursor = conn.execute(
            "INSERT INTO jobs(kind,username,cost,status,payload,created_at,"
            "updated_at,owner) VALUES "
            "('short_drama_remux',?,0,'pending',?,?,?,'content')",
            (actor, json.dumps(payload, ensure_ascii=False), now, now),
        )
        job_id = int(cursor.lastrowid)
        row_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO short_drama_playback_jobs "
            "(id,actor,project_id,source_version_id,timeline_version_id,"
            "job_id,idempotency_key,request_hash,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?, 'queued',?,?)",
            (
                row_id, actor, project_id, source["id"], timeline["id"],
                str(job_id), key, request_hash, now, now,
            ),
        )
        conn.commit()
    if callable(enqueue):
        enqueue(job_id, "short_drama_remux")
    return {
        "id": row_id, "project_id": project_id, "job_id": job_id,
        "status": "queued", "cost": 0, "replayed": False,
    }


def get_job(db_factory, owner, project_id, job_id):
    reconcile_job(db_factory, job_id)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT job.* FROM short_drama_playback_jobs job "
            "JOIN short_drama_projects project ON project.id=job.project_id "
            "WHERE job.job_id=? AND job.project_id=? AND project.username=? "
            "AND project.deleted=0",
            (str(job_id), project_id, owner),
        ).fetchone()
        if not row:
            raise LookupError("重封装任务不存在")
        return _job(row)


def select_version(db_factory, actor, owner, body):
    if not isinstance(body, dict) or set(body) != {"project_id", "version_id"}:
        raise ValueError("播放版本选择字段不正确")
    project_id = str(body.get("project_id") or "").strip()
    version_id = str(body.get("version_id") or "").strip()
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT id FROM short_drama_projects WHERE id=? AND username=? "
            "AND deleted=0", (project_id, owner),
        ).fetchone()
        version = conn.execute(
            "SELECT * FROM short_drama_playback_versions "
            "WHERE id=? AND project_id=? AND status='ready'",
            (version_id, project_id),
        ).fetchone()
        if not project or not version:
            conn.rollback()
            raise LookupError("播放版本不存在")
        conn.execute(
            "INSERT INTO short_drama_playback_current "
            "(project_id,version_id,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET "
            "version_id=excluded.version_id,updated_at=excluded.updated_at",
            (project_id, version_id, now),
        )
        conn.execute(
            "INSERT INTO short_drama_timeline_audit "
            "(id,project_id,version_id,actor,action,details_json,created_at) "
            "VALUES (?,?,?,?, 'select_playback_version',?,?)",
            (
                uuid.uuid4().hex, project_id, version["timeline_version_id"],
                actor, json.dumps({"playback_version_id": version_id}), now,
            ),
        )
        conn.commit()
        return _version(version)
