"""Read models and persistence contracts for short-drama assembly."""

import json
import sqlite3
from contextlib import closing


ASSEMBLY_STAGES = {"assembly_review", "completed"}
COMPOSITION_KINDS = {"preview", "final"}

DEFAULT_ASSEMBLY_CONFIG = {
    "subtitle": {
        "enabled": True,
        "preset": "white_outline",
        "position": "bottom",
    },
    "bgm": {
        "asset_id": None,
        "volume": 0.18,
        "fade_in_ms": 500,
        "fade_out_ms": 800,
    },
    "profiles": {
        "preview": "short_drama_preview_v1",
        "final": "short_drama_final_v1",
    },
}

_BLOCKER_MESSAGES = {
    "missing_locked_voice_shot": "镜头尚未锁定配音与字幕",
    "missing_locked_video_shot": "镜头尚无已确认的电影化身视频版本",
    "active_composition_job": "项目仍有合成任务处理中",
    "preview_missing": "尚未生成可用预览版本",
    "final_missing": "尚未生成可用正式成片",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_compositions (
  project_id TEXT PRIMARY KEY
    REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  assembly_revision INTEGER NOT NULL DEFAULT 1
    CHECK (assembly_revision >= 1),
  config_json TEXT NOT NULL DEFAULT '{}',
  current_preview_version INTEGER,
  current_final_version INTEGER,
  preview_locked INTEGER NOT NULL DEFAULT 0
    CHECK (preview_locked IN (0,1)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS short_drama_composition_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL
    REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('preview','final')),
  version INTEGER NOT NULL CHECK (version >= 1),
  job_id TEXT NOT NULL UNIQUE,
  input_hash TEXT NOT NULL,
  config_json TEXT NOT NULL,
  file TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  cover_file TEXT NOT NULL DEFAULT '',
  duration_ms INTEGER CHECK (
    duration_ms IS NULL OR (typeof(duration_ms)='integer' AND duration_ms > 0)
  ),
  width INTEGER CHECK (
    width IS NULL OR (typeof(width)='integer' AND width > 0)
  ),
  height INTEGER CHECK (
    height IS NULL OR (typeof(height)='integer' AND height > 0)
  ),
  fps REAL CHECK (fps IS NULL OR fps > 0),
  video_codec TEXT NOT NULL DEFAULT '',
  audio_codec TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL
    CHECK (status IN ('rendering','succeeded','failed','stale')),
  global_video_asset_id INTEGER UNIQUE,
  created_at INTEGER NOT NULL,
  UNIQUE(project_id, kind, version)
);

CREATE TABLE IF NOT EXISTS short_drama_composition_jobs (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  project_id TEXT NOT NULL
    REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('preview','final')),
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('queued','running','succeeded','failed')
  ),
  phase TEXT NOT NULL DEFAULT 'queued',
  progress INTEGER NOT NULL DEFAULT 0
    CHECK (progress BETWEEN 0 AND 100),
  error_code TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  attempt_count INTEGER NOT NULL DEFAULT 0
    CHECK (attempt_count >= 0),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  finished_at INTEGER,
  UNIQUE(username, kind, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_short_drama_composition_versions_project
  ON short_drama_composition_versions(project_id, kind, version DESC);
CREATE INDEX IF NOT EXISTS idx_short_drama_composition_jobs_project
  ON short_drama_composition_jobs(username, project_id, status, updated_at DESC);
"""


def init_db(db_factory):
    with closing(db_factory()) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.commit()


def _json_value(value, default):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _merge_config(value):
    saved = _json_value(value, {}) if value is not None else {}
    config = {
        "subtitle": dict(DEFAULT_ASSEMBLY_CONFIG["subtitle"]),
        "bgm": dict(DEFAULT_ASSEMBLY_CONFIG["bgm"]),
        "profiles": dict(DEFAULT_ASSEMBLY_CONFIG["profiles"]),
    }
    for section in config:
        candidate = saved.get(section)
        if isinstance(candidate, dict):
            config[section].update(candidate)
    return config


def _blocker(code, shot_id=None):
    item = {"code": code, "message": _BLOCKER_MESSAGES[code]}
    if shot_id is not None:
        item["shot_id"] = shot_id
    return item


def _version_snapshot(row):
    return {
        "id": row["id"],
        "kind": row["kind"],
        "version": row["version"],
        "job_id": row["job_id"],
        "input_hash": row["input_hash"],
        "config": _json_value(row["config_json"], {}),
        "url": row["url"],
        "duration_ms": row["duration_ms"],
        "width": row["width"],
        "height": row["height"],
        "fps": row["fps"],
        "video_codec": row["video_codec"],
        "audio_codec": row["audio_codec"],
        "status": row["status"],
        "global_video_asset_id": row["global_video_asset_id"],
        "created_at": row["created_at"],
    }


def _active_job_snapshot(row):
    if row is None:
        return None
    return {
        "job_id": row["job_id"],
        "kind": row["kind"],
        "status": row["status"],
        "phase": row["phase"],
        "progress": row["progress"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "attempt_count": row["attempt_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
    }


def build_assembly_snapshot(conn, project):
    """Build the D-0 read model without creating or mutating a workspace row."""
    conn.row_factory = sqlite3.Row
    composition = conn.execute(
        "SELECT * FROM short_drama_compositions WHERE project_id=?",
        (project["id"],),
    ).fetchone()
    voice_states = {
        row["shot_id"]: bool(row["locked"])
        for row in conn.execute(
            "SELECT shot_id,locked FROM short_drama_voice_shots "
            "WHERE project_id=?",
            (project["id"],),
        )
    }
    blockers = []
    shots = []
    for shot in conn.execute(
        "SELECT id,shot_key,sort_order,duration "
        "FROM short_drama_shots WHERE project_id=? ORDER BY sort_order,id",
        (project["id"],),
    ):
        shot_blockers = []
        voice_locked = voice_states.get(shot["id"], False)
        if not voice_locked:
            shot_blockers.append(
                _blocker("missing_locked_voice_shot", shot["id"])
            )
        # C-3 owns the confirmed video-version source. D-0 deliberately
        # publishes the blocker contract without guessing at that future table.
        shot_blockers.append(
            _blocker("missing_locked_video_shot", shot["id"])
        )
        blockers.extend(shot_blockers)
        shots.append({
            "id": shot["id"],
            "shot_key": shot["shot_key"],
            "sort_order": shot["sort_order"],
            "duration": shot["duration"],
            "voice": {
                "locked": voice_locked,
                "status": "ready" if voice_locked else "blocked",
            },
            "video": {
                "confirmed": False,
                "status": "pending_c3",
                "current_version": None,
            },
            "ready": False,
            "blockers": shot_blockers,
        })
    active_job = conn.execute(
        "SELECT * FROM short_drama_composition_jobs "
        "WHERE project_id=? AND status IN ('queued','running') "
        "ORDER BY updated_at DESC,job_id DESC LIMIT 1",
        (project["id"],),
    ).fetchone()
    if active_job:
        blockers.append(_blocker("active_composition_job"))
    versions = [
        _version_snapshot(row)
        for row in conn.execute(
            "SELECT * FROM short_drama_composition_versions "
            "WHERE project_id=? ORDER BY created_at DESC,kind,version DESC",
            (project["id"],),
        )
    ]
    preview_versions = [
        item for item in versions
        if item["kind"] == "preview" and item["status"] == "succeeded"
    ]
    final_versions = [
        item for item in versions
        if item["kind"] == "final" and item["status"] == "succeeded"
    ]
    if not preview_versions:
        blockers.append(_blocker("preview_missing"))
    if not final_versions:
        blockers.append(_blocker("final_missing"))
    current_preview = (
        composition["current_preview_version"] if composition else None
    )
    current_final = (
        composition["current_final_version"] if composition else None
    )
    preview_locked = bool(composition["preview_locked"]) if composition else False
    readiness_blockers = [
        item for item in blockers
        if item["code"] in {
            "missing_locked_voice_shot",
            "missing_locked_video_shot",
            "active_composition_job",
        }
    ]
    return {
        "project_id": project["id"],
        "revision": project["revision"],
        "stage": project["stage"],
        "ratio": project["ratio"],
        "target_duration": project["target_duration"],
        "assembly_revision": (
            composition["assembly_revision"] if composition else 1
        ),
        "config": _merge_config(
            composition["config_json"] if composition else None
        ),
        "current_preview_version": current_preview,
        "current_final_version": current_final,
        "preview_locked": preview_locked,
        "implementation_status": "contract_only",
        "rendering_enabled": False,
        "shots": shots,
        "versions": versions,
        "active_job": _active_job_snapshot(active_job),
        "readiness": {
            "ready": not readiness_blockers,
            "blockers": readiness_blockers,
        },
        "actions": {
            "can_save_config": False,
            "can_preview": False,
            "can_lock_preview": False,
            "can_export": False,
            "can_confirm": False,
        },
        "blockers": blockers,
    }


def get_assembly_workspace(db_factory, username, project_id):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        project = conn.execute(
            "SELECT * FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if project["stage"] not in ASSEMBLY_STAGES:
            raise ValueError("短剧项目尚未进入合成阶段")
        return build_assembly_snapshot(conn, project)
