"""Persistence helpers for short-drama production assets and jobs."""

import time
import uuid


ASSET_TYPES = {"still"}
JOB_KINDS = {"still"}


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
        "SELECT id FROM short_drama_shots WHERE project_id=?", (project_id,)
    ).fetchall()
    for (shot_id,) in shot_ids:
        conn.execute(
            "INSERT OR IGNORE INTO short_drama_assets "
            "(id, project_id, shot_id, type, created_at, updated_at) VALUES (?, ?, ?, 'still', ?, ?)",
            (str(uuid.uuid4()), project_id, shot_id, now, now),
        )
