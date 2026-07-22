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
STILL_REQUEST_FIELDS = {
    "project_id", "revision", "shot_id", "prompt", "mode", "count",
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


def normalize_still_request(body):
    if not isinstance(body, dict) or set(body) != STILL_REQUEST_FIELDS:
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
    return request, descriptor


def prepare_still_submission(db_factory, username, body):
    body, _descriptor = normalize_still_request(body)

    conn = db_factory()
    conn.row_factory = sqlite3.Row
    try:
        project = conn.execute(
            "SELECT * FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (body["project_id"], username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        project = dict(project)
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
    })
    return {"project": project, "shot": shot, "image_payload": image_payload}


def check_production_budget(db_factory, username, project_id, quoted_cost):
    if type(quoted_cost) is not int or quoted_cost < 0:
        raise ValueError("关键帧报价无效")
    conn = db_factory()
    try:
        project = conn.execute(
            "SELECT point_budget, spent_points, stage FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        point_budget, spent_points, stage = project
        if stage != "stills_review":
            raise ValueError("当前短剧阶段不能生成关键帧")
        point_budget = int(point_budget)
        if point_budget == 0:
            return
        reserved = conn.execute(
            "SELECT COALESCE(SUM(p.quoted_cost), 0) "
            "FROM short_drama_production_jobs p "
            "JOIN jobs j ON j.id=p.job_id AND j.username=p.username AND j.kind='image' "
            "WHERE p.username=? AND p.project_id=? "
            "AND p.status IN ('pending','running') "
            "AND j.status IN ('pending','running','done')",
            (username, project_id),
        ).fetchone()[0]
        reserved = int(reserved or 0)
        spent_points = int(spent_points)
        if spent_points + reserved + quoted_cost > point_budget:
            from .short_drama import PointBudgetExceeded
            raise PointBudgetExceeded(
                "短剧点数预算不足：已用 %d 点、已预留 %d 点、本次 %d 点、预算 %d 点" %
                (spent_points, reserved, quoted_cost, point_budget)
            )
    finally:
        conn.close()


def prepare_still_quote(db_factory, username, body, cost_of):
    prepared = prepare_still_submission(db_factory, username, body)
    cost = int(cost_of("image", prepared["image_payload"]))
    if cost < 0:
        raise ValueError("关键帧报价无效")
    check_production_budget(db_factory, username, prepared["project"]["id"], cost)
    return {"cost": cost, "count": 2, "kind": "still"}


def record_submitted_job(db_factory, *, username, project_id, shot_id, job_id,
                         idempotency_key, quoted_cost, connection=None):
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
        project = conn.execute(
            "SELECT stage FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
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
        "SELECT p.id, p.shot_id, p.job_id, p.status, p.quoted_cost, "
        "j.status, j.cost, j.payload, j.result "
        "FROM short_drama_production_jobs p "
        "JOIN jobs j ON j.id=p.job_id AND j.username=p.username AND j.kind='image' "
        "WHERE p.username=? AND p.project_id=? ORDER BY p.created_at, p.id",
        (username, project_id),
    ).fetchall()
    now = int(time.time())
    for (link_id, shot_id, job_id, link_status, quoted_cost, job_status, cost,
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
                or any(not isinstance(url, str) or not url for url in urls)
                or len(set(urls)) != 2):
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
        archived = conn.execute(
            "SELECT COUNT(*), MIN(version) FROM short_drama_asset_versions "
            "WHERE asset_id=? AND job_id=?",
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
            "UPDATE short_drama_assets "
            "SET current_version=COALESCE(current_version, ?), updated_at=? WHERE id=?",
            (int(archived[1]), now, asset_id),
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
        "JOIN jobs j ON j.id=p.job_id AND j.username=p.username AND j.kind='image' "
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


def select_asset(db_factory, username, body):
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
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT p.revision, p.stage "
            "FROM short_drama_assets a "
            "JOIN short_drama_projects p ON p.id=a.project_id "
            "JOIN short_drama_shots s ON s.id=a.shot_id AND s.project_id=a.project_id "
            "JOIN short_drama_asset_versions v "
            "ON v.asset_id=a.id AND v.version=? "
            "WHERE a.id=? AND a.project_id=? AND a.type='still' "
            "AND p.username=? AND p.deleted=0 AND v.status='done' AND v.ratio=p.ratio",
            (body["version"], asset_id, project_id, username),
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
            "WHERE id=? AND username=? AND revision=? "
            "AND stage='stills_review' AND deleted=0",
            (now, project_id, username, body["revision"]),
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
    return get_production(db_factory, username, project_id)


def confirm_stage(db_factory, username, body):
    if not isinstance(body, dict) or set(body) != {"project_id", "revision", "stage"}:
        raise ValueError("production stage confirmation fields are invalid")
    if type(body["project_id"]) is not str or not body["project_id"].strip():
        raise ValueError("project identifier is invalid")
    if type(body["revision"]) is not int or body["revision"] < 1:
        raise ValueError("project revision is invalid")
    if type(body["stage"]) is not str or body["stage"] != "stills_review":
        raise ValueError("only the stills review stage can be confirmed")

    project_id = body["project_id"].strip()
    conn = db_factory()
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT revision, stage, ratio FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("short drama project does not exist")
        if int(project[0]) != body["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        if project[1] != "stills_review":
            raise ValueError("short drama stages cannot be skipped")

        shot_ids = [row[0] for row in conn.execute(
            "SELECT id FROM short_drama_shots WHERE project_id=?",
            (project_id,),
        ).fetchall()]
        valid_rows = conn.execute(
            "SELECT s.id FROM short_drama_shots s "
            "JOIN short_drama_assets a "
            "ON a.project_id=s.project_id AND a.shot_id=s.id AND a.type='still' "
            "JOIN short_drama_asset_versions v "
            "ON v.asset_id=a.id AND v.version=a.current_version "
            "WHERE s.project_id=? AND a.locked=1 AND v.status='done' AND v.ratio=?",
            (project_id, project[2]),
        ).fetchall()
        valid_shot_ids = [row[0] for row in valid_rows]
        if (not shot_ids or len(valid_shot_ids) != len(shot_ids)
                or set(valid_shot_ids) != set(shot_ids)):
            raise ValueError("lock one completed current still for every shot first")

        cur = conn.execute(
            "UPDATE short_drama_projects "
            "SET stage='voice_review', revision=revision+1, updated_at=? "
            "WHERE id=? AND username=? AND revision=? "
            "AND stage='stills_review' AND deleted=0",
            (int(time.time()), project_id, username, body["revision"]),
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
    return get_production(db_factory, username, project_id)


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
        snapshot = build_production_snapshot(conn, dict(project), username)
        conn.commit()
        return snapshot
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
