"""Idempotent SQLite schema for short-drama master timeline versions."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_timeline_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version >= 1),
  parent_id TEXT REFERENCES short_drama_timeline_versions(id),
  status TEXT NOT NULL CHECK (status IN ('draft','blocked','ready')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  contract_version TEXT NOT NULL,
  duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
  source_hashes_json TEXT NOT NULL,
  timeline_hash TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  subtitle_cues_json TEXT NOT NULL,
  blockers_json TEXT NOT NULL DEFAULT '[]',
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(project_id, version)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_timeline_versions_project
  ON short_drama_timeline_versions(project_id, version DESC);

CREATE TABLE IF NOT EXISTS short_drama_timeline_segments (
  id TEXT NOT NULL,
  version_id TEXT NOT NULL REFERENCES short_drama_timeline_versions(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  line_id TEXT NOT NULL,
  character_key TEXT NOT NULL DEFAULT '',
  voice_asset_id TEXT NOT NULL DEFAULT '',
  start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
  end_ms INTEGER NOT NULL CHECK (end_ms > start_ms),
  speaking_mode TEXT NOT NULL CHECK (
    speaking_mode IN ('visible','offscreen','narration')
  ),
  face_target_json TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(version_id, id)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_timeline_segments_version
  ON short_drama_timeline_segments(version_id, sort_order, start_ms);

CREATE TABLE IF NOT EXISTS short_drama_timeline_current (
  project_id TEXT PRIMARY KEY REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  version_id TEXT NOT NULL REFERENCES short_drama_timeline_versions(id),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS short_drama_timeline_audit (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  version_id TEXT REFERENCES short_drama_timeline_versions(id),
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  before_hash TEXT NOT NULL DEFAULT '',
  after_hash TEXT NOT NULL DEFAULT '',
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_timeline_audit_project
  ON short_drama_timeline_audit(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS short_drama_timeline_requests (
  actor TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  action TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY(actor, idempotency_key)
);

CREATE TRIGGER IF NOT EXISTS short_drama_timeline_segment_project_guard
BEFORE INSERT ON short_drama_timeline_segments
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'timeline segment shot must belong to project');
END;
"""


def init_db(db_factory):
    conn = db_factory()
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
