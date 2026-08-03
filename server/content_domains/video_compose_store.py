# -*- coding: utf-8 -*-
"""Private SQLite store for non-destructive one-click video projects."""

import json
import os
import pathlib
import sqlite3
import time
import uuid
from contextlib import closing


BASE = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = os.environ.get("VIDEO_COMPOSE_DB", str(BASE / "video_compose.db"))
PROJECT_STATES = {
    "created", "transcribing", "analyzing_speech", "review_required",
    "review_confirmed", "building_clean_master", "clean_master_ready",
    "template_selection_required", "quoted", "confirmed", "rendering",
    "quality_checking", "storing", "completed", "failed", "refunded",
}


class ProjectNotFound(LookupError):
    pass


class RevisionConflict(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value, fallback):
    try:
        decoded = json.loads(value or "")
    except Exception:
        return fallback
    return decoded


def db():
    path = pathlib.Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    ensure_schema(connection)
    return connection


def ensure_schema(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS video_compose_projects(
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            source_asset_id INTEGER NOT NULL,
            source_revision TEXT NOT NULL,
            source_snapshot_json TEXT NOT NULL,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            transcript_version INTEGER NOT NULL DEFAULT 0,
            edit_decision_version INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER,
            transcript_hash TEXT,
            words_json TEXT,
            candidates_json TEXT,
            decisions_json TEXT,
            edl_json TEXT,
            template_id TEXT,
            template_version TEXT,
            render_input_json TEXT,
            clean_master_file TEXT,
            output_file TEXT,
            output_asset_id INTEGER,
            quality_json TEXT,
            error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_video_compose_owner_updated
            ON video_compose_projects(username,updated_at DESC);
        CREATE TABLE IF NOT EXISTS video_compose_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            username TEXT NOT NULL,
            event_type TEXT NOT NULL,
            revision INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(project_id) REFERENCES video_compose_projects(id)
        );
        CREATE INDEX IF NOT EXISTS idx_video_compose_events_project
            ON video_compose_events(project_id,id);
    """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(video_compose_projects)")}
    for name, declaration in {
        "template_id": "TEXT", "template_version": "TEXT", "render_input_json": "TEXT",
        "clean_master_file": "TEXT", "output_file": "TEXT", "output_asset_id": "INTEGER", "quality_json": "TEXT",
    }.items():
        if name not in columns:
            connection.execute("ALTER TABLE video_compose_projects ADD COLUMN %s %s" % (name, declaration))
    connection.commit()


def _public(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "source_asset_id": row["source_asset_id"],
        "source_revision": row["source_revision"],
        "source": _decode(row["source_snapshot_json"], {}),
        "status": row["status"],
        "revision": row["revision"],
        "transcript_version": row["transcript_version"],
        "edit_decision_version": row["edit_decision_version"],
        "duration_ms": row["duration_ms"],
        "transcript_hash": row["transcript_hash"],
        "words": _decode(row["words_json"], []),
        "candidates": _decode(row["candidates_json"], []),
        "decisions": _decode(row["decisions_json"], {}),
        "edl": _decode(row["edl_json"], None),
        "template_id": row["template_id"],
        "template_version": row["template_version"],
        "render_input": _decode(row["render_input_json"], None),
        "clean_master_file": row["clean_master_file"],
        "output_file": row["output_file"],
        "output_asset_id": row["output_asset_id"],
        "quality": _decode(row["quality_json"], None),
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _event(connection, project_id, username, event_type, revision, payload):
    connection.execute(
        """INSERT INTO video_compose_events
           (project_id,username,event_type,revision,payload_json,created_at)
           VALUES(?,?,?,?,?,?)""",
        (project_id, username, event_type, revision, _json(payload or {}), int(time.time())),
    )


def create_project(username, source_asset_id, source_revision, source_snapshot):
    username = str(username or "").strip()
    if not username:
        raise ValueError("项目缺少用户归属")
    project_id = "compose_" + uuid.uuid4().hex
    now = int(time.time())
    with closing(db()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO video_compose_projects
               (id,username,source_asset_id,source_revision,source_snapshot_json,
                status,revision,created_at,updated_at)
               VALUES(?,?,?,?,?,'created',1,?,?)""",
            (project_id, username, int(source_asset_id), str(source_revision),
             _json(source_snapshot), now, now),
        )
        _event(connection, project_id, username, "project_created", 1, {
            "source_asset_id": int(source_asset_id), "source_revision": source_revision,
        })
        connection.commit()
        row = connection.execute(
            "SELECT * FROM video_compose_projects WHERE id=?", (project_id,)
        ).fetchone()
    return _public(row)


def get_project(username, project_id):
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT * FROM video_compose_projects WHERE id=? AND username=?",
            (str(project_id), str(username)),
        ).fetchone()
    if not row:
        raise ProjectNotFound("一键成片项目不存在")
    return _public(row)


def list_projects(username, limit=30):
    limit = max(1, min(100, int(limit or 30)))
    with closing(db()) as connection:
        rows = connection.execute(
            """SELECT * FROM video_compose_projects WHERE username=?
               ORDER BY updated_at DESC LIMIT ?""",
            (str(username), limit),
        ).fetchall()
    return [_public(row) for row in rows]


def save_analysis(username, project_id, expected_revision, analysis, transcript_hash):
    now = int(time.time())
    with closing(db()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status,revision,transcript_version FROM video_compose_projects WHERE id=? AND username=?",
            (str(project_id), str(username)),
        ).fetchone()
        if not row:
            raise ProjectNotFound("一键成片项目不存在")
        if int(row["revision"]) != int(expected_revision):
            raise RevisionConflict("项目已更新，请刷新后重试")
        if row["status"] not in {"created", "transcribing", "analyzing_speech", "review_required"}:
            raise ValueError("当前项目状态不能更新转录分析")
        revision = int(row["revision"]) + 1
        transcript_version = int(row["transcript_version"]) + 1
        changed = connection.execute(
            """UPDATE video_compose_projects SET status='review_required',revision=?,
               transcript_version=?,edit_decision_version=0,duration_ms=?,transcript_hash=?,
               words_json=?,candidates_json=?,decisions_json=NULL,edl_json=NULL,error=NULL,updated_at=?
               WHERE id=? AND username=? AND revision=?""",
            (revision, transcript_version, int(analysis["duration_ms"]), transcript_hash,
             _json(analysis["words"]), _json(analysis["candidates"]), now,
             str(project_id), str(username), int(expected_revision)),
        ).rowcount
        if changed != 1:
            raise RevisionConflict("项目已更新，请刷新后重试")
        _event(connection, project_id, username, "analysis_ready", revision, {
            "transcript_version": transcript_version,
            "candidate_count": len(analysis["candidates"]),
            "transcript_hash": transcript_hash,
        })
        connection.commit()
    return get_project(username, project_id)


def save_render_result(username, project_id, expected_revision, render_input,
                       clean_master_file, output_file, output_asset_id, quality):
    now = int(time.time())
    with closing(db()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status,revision FROM video_compose_projects WHERE id=? AND username=?",
            (str(project_id), str(username)),
        ).fetchone()
        if not row:
            raise ProjectNotFound("一键成片项目不存在")
        if int(row["revision"]) != int(expected_revision):
            raise RevisionConflict("项目已更新，请刷新后重试")
        if row["status"] != "review_confirmed":
            raise ValueError("当前项目状态不能渲染")
        revision = int(row["revision"]) + 1
        connection.execute(
            """UPDATE video_compose_projects SET status='completed',revision=?,template_id=?,
               template_version=?,render_input_json=?,clean_master_file=?,output_file=?,
               output_asset_id=?,quality_json=?,error=NULL,updated_at=? WHERE id=? AND username=? AND revision=?""",
            (revision, str(render_input.get("template_id") or ""),
             str(render_input.get("template_version") or ""), _json(render_input),
             str(clean_master_file), str(output_file), int(output_asset_id), _json(quality), now,
             str(project_id), str(username), int(expected_revision)),
        )
        _event(connection, project_id, username, "render_completed", revision, {
            "template_id": render_input.get("template_id"),
            "template_version": render_input.get("template_version"),
            "output_file": output_file,
        })
        connection.commit()
    return get_project(username, project_id)


def confirm_edit_decisions(username, project_id, expected_revision, decisions, edl):
    now = int(time.time())
    with closing(db()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT status,revision,edit_decision_version FROM video_compose_projects
               WHERE id=? AND username=?""",
            (str(project_id), str(username)),
        ).fetchone()
        if not row:
            raise ProjectNotFound("一键成片项目不存在")
        if int(row["revision"]) != int(expected_revision):
            raise RevisionConflict("项目已更新，请刷新后重试")
        if row["status"] not in {"review_required", "review_confirmed"}:
            raise ValueError("当前项目状态不能确认粗剪")
        revision = int(row["revision"]) + 1
        decision_version = int(row["edit_decision_version"]) + 1
        changed = connection.execute(
            """UPDATE video_compose_projects SET status='review_confirmed',revision=?,
               edit_decision_version=?,decisions_json=?,edl_json=?,error=NULL,updated_at=?
               WHERE id=? AND username=? AND revision=?""",
            (revision, decision_version, _json(decisions), _json(edl), now,
             str(project_id), str(username), int(expected_revision)),
        ).rowcount
        if changed != 1:
            raise RevisionConflict("项目已更新，请刷新后重试")
        _event(connection, project_id, username, "edit_decisions_confirmed", revision, {
            "edit_decision_version": decision_version,
            "removed_candidate_ids": edl.get("removed_candidate_ids") or [],
            "output_duration_ms": edl.get("output_duration_ms"),
        })
        connection.commit()
    return get_project(username, project_id)
