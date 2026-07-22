"""Persistence helpers for short-drama production assets and jobs."""

import json
import sqlite3
import time
import uuid


ASSET_TYPES = {"still"}
JOB_KINDS = {"still"}
PRODUCTION_STAGES = {
    "stills_review", "voice_review", "video_review", "assembly_review", "completed",
}


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
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, kind, idempotency_key)
);
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


def init_db(db_factory):
    conn = db_factory()
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
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


def reconcile_jobs(conn, username, project_id):
    rows = conn.execute(
        "SELECT p.id, p.shot_id, p.job_id, p.status, j.status, j.cost, j.payload, j.result "
        "FROM short_drama_production_jobs p "
        "JOIN jobs j ON j.id=p.job_id AND j.username=p.username "
        "WHERE p.username=? AND p.project_id=? ORDER BY p.created_at, p.id",
        (username, project_id),
    ).fetchall()
    now = int(time.time())
    for (link_id, shot_id, job_id, link_status, job_status, cost,
         payload_json, result_json) in rows:
        status = job_status if job_status in {"pending", "running", "done", "failed"} else "failed"
        conn.execute(
            "UPDATE short_drama_production_jobs SET status=?, updated_at=? WHERE id=?",
            (status, now, link_id),
        )
        if status != "done":
            continue
        payload = _json_object(payload_json, "关键帧任务参数无效")
        result = _json_object(result_json, "关键帧任务结果无效")
        project_ratio_row = conn.execute(
            "SELECT ratio FROM short_drama_projects WHERE id=?", (project_id,)
        ).fetchone()
        project_ratio = project_ratio_row[0]
        if result.get("ratio") != project_ratio or payload.get("ratio") != project_ratio:
            raise ValueError("关键帧任务比例与项目不一致")
        urls = result.get("urls") or ([result.get("url")] if result.get("url") else [])
        if (not isinstance(urls, list) or len(urls) != 2
                or any(not isinstance(url, str) or not url for url in urls)):
            raise ValueError("关键帧任务必须返回 2 张候选图")
        prompt = payload.get("prompt") or ""
        if not isinstance(prompt, str):
            raise ValueError("关键帧任务参数无效")
        asset_row = conn.execute(
            "SELECT id FROM short_drama_assets "
            "WHERE project_id=? AND shot_id=? AND type='still'",
            (project_id, shot_id),
        ).fetchone()
        asset_id = asset_row[0]
        next_version = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_asset_versions WHERE asset_id=?",
            (asset_id,),
        ).fetchone()[0])
        for offset, url in enumerate(urls):
            conn.execute(
                "INSERT OR IGNORE INTO short_drama_asset_versions "
                "(id, asset_id, version, job_id, url, prompt, ratio, cost, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'done', ?)",
                (str(uuid.uuid4()), asset_id, next_version + offset, job_id, url,
                 prompt, project_ratio, int(cost or 0), now),
            )
        conn.execute(
            "UPDATE short_drama_assets "
            "SET current_version=COALESCE(current_version, ?), updated_at=? WHERE id=?",
            (next_version, now, asset_id),
        )


def _query_dicts(conn, query, params=()):
    cursor = conn.execute(query, params)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def build_production_snapshot(conn, project, username):
    project_id = project["id"]
    shots = _query_dicts(
        conn,
        "SELECT id, shot_key, sort_order, duration, image_prompt "
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
    active_jobs = {}
    reserved_points = 0
    for job in _query_dicts(
        conn,
        "SELECT p.id, p.shot_id, p.job_id, p.kind, p.status, p.quoted_cost "
        "FROM short_drama_production_jobs p "
        "JOIN jobs j ON j.id=p.job_id AND j.username=p.username "
        "WHERE p.username=? AND p.project_id=? "
        "AND p.status IN ('pending','running') "
        "ORDER BY p.created_at DESC, p.id DESC",
        (username, project_id),
    ):
        reserved_points += int(job["quoted_cost"])
        shot_id = job.pop("shot_id")
        active_jobs.setdefault(shot_id, job)

    shot_items = []
    for shot in shots:
        asset = assets[shot["id"]]
        shot_items.append({
            "id": shot["id"],
            "shot_key": shot["shot_key"],
            "sort_order": int(shot["sort_order"]),
            "duration": int(shot["duration"]),
            "image_prompt": shot["image_prompt"],
            "still": {
                "asset_id": asset["id"],
                "current_version": (
                    None if asset["current_version"] is None else int(asset["current_version"])
                ),
                "locked": bool(asset["locked"]),
                "versions": versions_by_asset.get(asset["id"], []),
                "job": active_jobs.get(shot["id"]),
            },
        })
    return {
        "project_id": project_id,
        "revision": int(project["revision"]),
        "stage": project["stage"],
        "ratio": project["ratio"],
        "point_budget": int(project["point_budget"]),
        "spent_points": int(project["spent_points"]),
        "reserved_points": reserved_points,
        "shots": shot_items,
    }


def get_production(db_factory, username, project_id):
    conn = db_factory()
    conn.row_factory = sqlite3.Row
    try:
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if project["stage"] not in PRODUCTION_STAGES:
            raise ValueError("短剧项目尚未进入素材制作")
        ensure_asset_slots(conn, project_id)
        reconcile_jobs(conn, username, project_id)
        conn.commit()
        return build_production_snapshot(conn, dict(project), username)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
