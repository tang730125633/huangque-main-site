"""Voice-line snapshots and read models for short-drama production."""

import sqlite3


VOICE_STAGES = {
    "voice_review", "video_review", "assembly_review", "completed",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_voice_shots (
  shot_id TEXT PRIMARY KEY REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0,1)),
  timeline_revision INTEGER NOT NULL DEFAULT 1 CHECK (timeline_revision >= 1),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS short_drama_voice_lines (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  dialogue_line_id TEXT,
  line_type TEXT NOT NULL CHECK (line_type IN ('dialogue','narration')),
  sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
  character_key TEXT NOT NULL DEFAULT '',
  source_text TEXT NOT NULL,
  speech_text TEXT NOT NULL,
  subtitle_text TEXT NOT NULL,
  subtitle_visible INTEGER NOT NULL DEFAULT 1 CHECK (subtitle_visible IN (0,1)),
  voice_key TEXT NOT NULL DEFAULT '',
  speed REAL NOT NULL DEFAULT 1.0 CHECK (speed >= 0.5 AND speed <= 2.0),
  pitch INTEGER NOT NULL DEFAULT 0 CHECK (pitch >= -12 AND pitch <= 12),
  volume INTEGER NOT NULL DEFAULT 0 CHECK (volume >= -50 AND volume <= 100),
  current_version INTEGER,
  start_ms INTEGER CHECK (
    start_ms IS NULL OR (typeof(start_ms)='integer' AND start_ms >= 0)
  ),
  end_ms INTEGER CHECK (
    end_ms IS NULL OR (typeof(end_ms)='integer' AND end_ms > 0)
  ),
  input_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(project_id, shot_id, sort_order)
);
CREATE TABLE IF NOT EXISTS short_drama_voice_versions (
  id TEXT PRIMARY KEY,
  voice_line_id TEXT NOT NULL REFERENCES short_drama_voice_lines(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version >= 1),
  job_id INTEGER NOT NULL UNIQUE,
  audio_file TEXT NOT NULL DEFAULT '',
  audio_url TEXT NOT NULL DEFAULT '',
  duration_ms INTEGER CHECK (
    duration_ms IS NULL OR (typeof(duration_ms)='integer' AND duration_ms > 0)
  ),
  speech_text TEXT NOT NULL,
  voice_key TEXT NOT NULL,
  settings_json TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  cost INTEGER NOT NULL DEFAULT 0 CHECK (cost >= 0),
  status TEXT NOT NULL CHECK (status IN ('metadata_pending','done','failed')),
  error TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  UNIQUE(voice_line_id, version)
);
CREATE TABLE IF NOT EXISTS short_drama_voice_jobs (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  voice_line_id TEXT NOT NULL REFERENCES short_drama_voice_lines(id) ON DELETE CASCADE,
  job_id INTEGER NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL,
  quoted_cost INTEGER NOT NULL CHECK (quoted_cost >= 0),
  status TEXT NOT NULL CHECK (status IN ('pending','running','metadata_pending','done','failed')),
  error TEXT NOT NULL DEFAULT '',
  refunded INTEGER NOT NULL DEFAULT 0 CHECK (refunded IN (0,1,2)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, idempotency_key)
);
CREATE TABLE IF NOT EXISTS short_drama_voice_quotes (
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  voice_line_id TEXT NOT NULL REFERENCES short_drama_voice_lines(id) ON DELETE CASCADE,
  request_hash TEXT NOT NULL,
  cost INTEGER NOT NULL CHECK (cost >= 0),
  expires_at INTEGER NOT NULL,
  consumed_idempotency_key TEXT,
  consumed_job_id INTEGER,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS short_drama_voice_charge_attempts (
  charge_key TEXT PRIMARY KEY,
  refund_key TEXT NOT NULL UNIQUE,
  username TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  voice_line_id TEXT NOT NULL REFERENCES short_drama_voice_lines(id) ON DELETE CASCADE,
  quote_token TEXT NOT NULL REFERENCES short_drama_voice_quotes(token),
  cost INTEGER NOT NULL CHECK (cost >= 0),
  audio_payload_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (
    state IN ('accepted','charged','linked','done','refund_pending','refunded','failed')
  ),
  points_left INTEGER,
  job_id INTEGER,
  terminal_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, endpoint, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_voice_lines_project
  ON short_drama_voice_lines(project_id, shot_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_short_drama_voice_jobs_project
  ON short_drama_voice_jobs(username, project_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_short_drama_voice_quotes_lookup
  ON short_drama_voice_quotes(username, project_id, voice_line_id, expires_at);
CREATE TRIGGER IF NOT EXISTS short_drama_voice_shots_project_guard
BEFORE INSERT ON short_drama_voice_shots
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'voice shot must belong to project');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_voice_shots_project_update_guard
BEFORE UPDATE OF shot_id, project_id ON short_drama_voice_shots
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'voice shot must belong to project');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_voice_lines_project_guard
BEFORE INSERT ON short_drama_voice_lines
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'voice line shot must belong to project');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_voice_lines_project_update_guard
BEFORE UPDATE OF project_id, shot_id ON short_drama_voice_lines
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'voice line shot must belong to project');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_voice_lines_source_text_immutable
BEFORE UPDATE OF source_text ON short_drama_voice_lines
FOR EACH ROW WHEN NEW.source_text IS NOT OLD.source_text
BEGIN
  SELECT RAISE(ABORT, 'voice line source text is immutable');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_voice_jobs_project_guard
BEFORE INSERT ON short_drama_voice_jobs
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_projects AS project
  JOIN short_drama_shots AS shot
    ON shot.id=NEW.shot_id AND shot.project_id=NEW.project_id
  JOIN short_drama_voice_lines AS line
    ON line.id=NEW.voice_line_id AND line.project_id=NEW.project_id
    AND line.shot_id=NEW.shot_id
  WHERE project.id=NEW.project_id AND project.username=NEW.username
)
BEGIN
  SELECT RAISE(ABORT, 'voice job references must belong to project owner');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_voice_jobs_project_update_guard
BEFORE UPDATE OF username, project_id, shot_id, voice_line_id ON short_drama_voice_jobs
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_projects AS project
  JOIN short_drama_shots AS shot
    ON shot.id=NEW.shot_id AND shot.project_id=NEW.project_id
  JOIN short_drama_voice_lines AS line
    ON line.id=NEW.voice_line_id AND line.project_id=NEW.project_id
    AND line.shot_id=NEW.shot_id
  WHERE project.id=NEW.project_id AND project.username=NEW.username
)
BEGIN
  SELECT RAISE(ABORT, 'voice job references must belong to project owner');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_voice_quotes_project_guard
BEFORE INSERT ON short_drama_voice_quotes
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_projects AS project
  JOIN short_drama_voice_lines AS line
    ON line.id=NEW.voice_line_id AND line.project_id=NEW.project_id
  WHERE project.id=NEW.project_id AND project.username=NEW.username
)
BEGIN
  SELECT RAISE(ABORT, 'voice quote line must belong to project owner');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_voice_quotes_project_update_guard
BEFORE UPDATE OF username, project_id, voice_line_id ON short_drama_voice_quotes
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_projects AS project
  JOIN short_drama_voice_lines AS line
    ON line.id=NEW.voice_line_id AND line.project_id=NEW.project_id
  WHERE project.id=NEW.project_id AND project.username=NEW.username
)
BEGIN
  SELECT RAISE(ABORT, 'voice quote line must belong to project owner');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_voice_charge_attempts_project_guard
BEFORE INSERT ON short_drama_voice_charge_attempts
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_projects AS project
  JOIN short_drama_shots AS shot
    ON shot.id=NEW.shot_id AND shot.project_id=NEW.project_id
  JOIN short_drama_voice_lines AS line
    ON line.id=NEW.voice_line_id AND line.project_id=NEW.project_id
    AND line.shot_id=NEW.shot_id
  JOIN short_drama_voice_quotes AS quote
    ON quote.token=NEW.quote_token AND quote.username=NEW.username
    AND quote.project_id=NEW.project_id AND quote.voice_line_id=NEW.voice_line_id
  WHERE project.id=NEW.project_id AND project.username=NEW.username
)
BEGIN
  SELECT RAISE(ABORT, 'voice charge references must belong to project owner');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_voice_charge_attempts_project_update_guard
BEFORE UPDATE OF username, project_id, shot_id, voice_line_id, quote_token
ON short_drama_voice_charge_attempts
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_projects AS project
  JOIN short_drama_shots AS shot
    ON shot.id=NEW.shot_id AND shot.project_id=NEW.project_id
  JOIN short_drama_voice_lines AS line
    ON line.id=NEW.voice_line_id AND line.project_id=NEW.project_id
    AND line.shot_id=NEW.shot_id
  JOIN short_drama_voice_quotes AS quote
    ON quote.token=NEW.quote_token AND quote.username=NEW.username
    AND quote.project_id=NEW.project_id AND quote.voice_line_id=NEW.voice_line_id
  WHERE project.id=NEW.project_id AND project.username=NEW.username
)
BEGIN
  SELECT RAISE(ABORT, 'voice charge references must belong to project owner');
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
