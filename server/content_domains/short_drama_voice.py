"""Voice-line snapshots and read models for short-drama production."""

import hashlib
import json
import sqlite3
import time
import uuid


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
DROP TRIGGER IF EXISTS short_drama_voice_jobs_project_guard;
DROP TRIGGER IF EXISTS short_drama_voice_jobs_project_update_guard;
DROP TRIGGER IF EXISTS short_drama_voice_quotes_project_guard;
DROP TRIGGER IF EXISTS short_drama_voice_quotes_project_update_guard;
DROP TRIGGER IF EXISTS short_drama_voice_charge_attempts_project_guard;
DROP TRIGGER IF EXISTS short_drama_voice_charge_attempts_project_update_guard;
CREATE TRIGGER short_drama_voice_jobs_project_guard
BEFORE INSERT ON short_drama_voice_jobs
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_projects AS project
  JOIN short_drama_shots AS shot
    ON shot.id=NEW.shot_id AND shot.project_id=NEW.project_id
  JOIN short_drama_voice_lines AS line
    ON line.id=NEW.voice_line_id AND line.project_id=NEW.project_id
    AND line.shot_id=NEW.shot_id
  WHERE project.id=NEW.project_id
    AND NOT EXISTS (
      SELECT 1 FROM short_drama_voice_quotes AS quote
      WHERE quote.consumed_job_id=NEW.job_id
        AND (quote.username<>NEW.username OR quote.project_id<>NEW.project_id
          OR quote.voice_line_id<>NEW.voice_line_id)
    )
    AND NOT EXISTS (
      SELECT 1 FROM short_drama_voice_charge_attempts AS attempt
      WHERE attempt.job_id=NEW.job_id
        AND (attempt.username<>NEW.username OR attempt.project_id<>NEW.project_id
          OR attempt.shot_id<>NEW.shot_id OR attempt.voice_line_id<>NEW.voice_line_id)
    )
)
BEGIN
  SELECT RAISE(ABORT, 'voice job references must share one project and actor');
END;
CREATE TRIGGER short_drama_voice_jobs_project_update_guard
BEFORE UPDATE OF username, project_id, shot_id, voice_line_id, job_id
ON short_drama_voice_jobs
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_projects AS project
  JOIN short_drama_shots AS shot
    ON shot.id=NEW.shot_id AND shot.project_id=NEW.project_id
  JOIN short_drama_voice_lines AS line
    ON line.id=NEW.voice_line_id AND line.project_id=NEW.project_id
    AND line.shot_id=NEW.shot_id
  WHERE project.id=NEW.project_id
    AND NOT EXISTS (
      SELECT 1 FROM short_drama_voice_quotes AS quote
      WHERE quote.consumed_job_id=NEW.job_id
        AND (quote.username<>NEW.username OR quote.project_id<>NEW.project_id
          OR quote.voice_line_id<>NEW.voice_line_id)
    )
    AND NOT EXISTS (
      SELECT 1 FROM short_drama_voice_charge_attempts AS attempt
      WHERE attempt.job_id=NEW.job_id
        AND (attempt.username<>NEW.username OR attempt.project_id<>NEW.project_id
          OR attempt.shot_id<>NEW.shot_id OR attempt.voice_line_id<>NEW.voice_line_id)
    )
)
BEGIN
  SELECT RAISE(ABORT, 'voice job references must share one project and actor');
END;
CREATE TRIGGER short_drama_voice_quotes_project_guard
BEFORE INSERT ON short_drama_voice_quotes
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_projects AS project
  JOIN short_drama_voice_lines AS line
    ON line.id=NEW.voice_line_id AND line.project_id=NEW.project_id
  WHERE project.id=NEW.project_id
    AND (NEW.consumed_job_id IS NULL OR EXISTS (
      SELECT 1 FROM short_drama_voice_jobs AS job
      WHERE job.job_id=NEW.consumed_job_id AND job.username=NEW.username
        AND job.project_id=NEW.project_id AND job.voice_line_id=NEW.voice_line_id
    ))
)
BEGIN
  SELECT RAISE(ABORT, 'voice quote references must share one project and actor');
END;
CREATE TRIGGER short_drama_voice_quotes_project_update_guard
BEFORE UPDATE OF username, project_id, voice_line_id, consumed_job_id
ON short_drama_voice_quotes
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_projects AS project
  JOIN short_drama_voice_lines AS line
    ON line.id=NEW.voice_line_id AND line.project_id=NEW.project_id
  WHERE project.id=NEW.project_id
    AND (NEW.consumed_job_id IS NULL OR EXISTS (
      SELECT 1 FROM short_drama_voice_jobs AS job
      WHERE job.job_id=NEW.consumed_job_id AND job.username=NEW.username
        AND job.project_id=NEW.project_id AND job.voice_line_id=NEW.voice_line_id
    ))
)
BEGIN
  SELECT RAISE(ABORT, 'voice quote references must share one project and actor');
END;
CREATE TRIGGER short_drama_voice_charge_attempts_project_guard
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
  WHERE project.id=NEW.project_id
    AND (NEW.job_id IS NULL OR EXISTS (
      SELECT 1 FROM short_drama_voice_jobs AS job
      WHERE job.job_id=NEW.job_id AND job.username=NEW.username
        AND job.project_id=NEW.project_id AND job.shot_id=NEW.shot_id
        AND job.voice_line_id=NEW.voice_line_id
    ))
)
BEGIN
  SELECT RAISE(ABORT, 'voice charge references must share one project and actor');
END;
CREATE TRIGGER short_drama_voice_charge_attempts_project_update_guard
BEFORE UPDATE OF username, project_id, shot_id, voice_line_id, quote_token, job_id
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
  WHERE project.id=NEW.project_id
    AND (NEW.job_id IS NULL OR EXISTS (
      SELECT 1 FROM short_drama_voice_jobs AS job
      WHERE job.job_id=NEW.job_id AND job.username=NEW.username
        AND job.project_id=NEW.project_id AND job.shot_id=NEW.shot_id
        AND job.voice_line_id=NEW.voice_line_id
    ))
)
BEGIN
  SELECT RAISE(ABORT, 'voice charge references must share one project and actor');
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


def _json_value(raw, fallback):
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value


def _number(value, default, minimum, maximum, integer=False):
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    result = max(minimum, min(maximum, result))
    return int(round(result)) if integer else round(result, 1)


def normalized_voice_settings(raw):
    value = raw if isinstance(raw, dict) else {}
    return {
        "speed": _number(value.get("speed"), 1.0, 0.5, 2.0),
        "pitch": _number(value.get("pitch"), 0, -12, 12, integer=True),
        "volume": _number(value.get("volume"), 0, -50, 100, integer=True),
    }


def voice_input_hash(speech_text, voice_key, speed, pitch, volume):
    descriptor = {
        "speech_text": str(speech_text),
        "voice_key": str(voice_key),
        "speed": float(speed),
        "pitch": int(pitch),
        "volume": int(volume),
    }
    encoded = json.dumps(
        descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_voice_workspace(conn, project_id, allowed_stages=None):
    conn.row_factory = sqlite3.Row
    project = conn.execute(
        "SELECT * FROM short_drama_projects WHERE id=? AND deleted=0",
        (project_id,),
    ).fetchone()
    if not project:
        raise LookupError("短剧项目不存在")
    allowed = set(allowed_stages or VOICE_STAGES)
    if project["stage"] not in allowed:
        raise ValueError("短剧项目尚未进入配音阶段")
    existing = conn.execute(
        "SELECT 1 FROM short_drama_voice_shots WHERE project_id=? LIMIT 1",
        (project_id,),
    ).fetchone()
    if existing:
        return
    script = conn.execute(
        "SELECT dialogue_lines_json FROM short_drama_scripts "
        "WHERE project_id=? ORDER BY version DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if not script:
        raise ValueError("短剧项目缺少已确认剧本")
    dialogue_items = _json_value(script["dialogue_lines_json"], [])
    dialogue = {
        item.get("id"): item for item in dialogue_items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    characters = {
        row["character_key"]: row for row in conn.execute(
            "SELECT * FROM short_drama_characters WHERE project_id=?",
            (project_id,),
        )
    }
    shots = conn.execute(
        "SELECT * FROM short_drama_shots WHERE project_id=? "
        "ORDER BY sort_order,id",
        (project_id,),
    ).fetchall()
    if not shots:
        raise ValueError("短剧项目缺少已确认分镜")
    now = int(time.time())
    for shot in shots:
        conn.execute(
            "INSERT INTO short_drama_voice_shots "
            "(shot_id,project_id,locked,timeline_revision,created_at,updated_at) "
            "VALUES (?,?,0,1,?,?)",
            (shot["id"], project_id, now, now),
        )
        line_ids = _json_value(shot["dialogue_line_ids_json"], [])
        for sort_order, dialogue_line_id in enumerate(line_ids):
            source = dialogue.get(dialogue_line_id)
            if not source:
                raise ValueError("分镜引用了不存在的台词")
            character_key = str(source.get("character_key") or "")
            character = characters.get(character_key)
            if not character:
                raise ValueError("台词引用了不存在的角色")
            settings = normalized_voice_settings(
                _json_value(character["voice_settings_json"], {})
            )
            speech_text = str(source.get("text") or "").strip()
            if not speech_text:
                raise ValueError("配音台词不能为空")
            voice_key = str(character["voice_key"] or "").strip()
            input_hash = voice_input_hash(
                speech_text, voice_key, settings["speed"],
                settings["pitch"], settings["volume"],
            )
            conn.execute(
                "INSERT INTO short_drama_voice_lines "
                "(id,project_id,shot_id,dialogue_line_id,line_type,sort_order,"
                "character_key,source_text,speech_text,subtitle_text,"
                "subtitle_visible,voice_key,speed,pitch,volume,current_version,"
                "start_ms,end_ms,input_hash,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,NULL,NULL,NULL,?,?,?)",
                (
                    str(uuid.uuid4()), project_id, shot["id"], dialogue_line_id,
                    "narration" if character_key == "narrator" else "dialogue",
                    sort_order, character_key, speech_text, speech_text, speech_text,
                    voice_key, settings["speed"], settings["pitch"],
                    settings["volume"], input_hash, now, now,
                ),
            )


def _line_snapshot(row, character_name):
    return {
        "id": row["id"],
        "dialogue_line_id": row["dialogue_line_id"],
        "line_type": row["line_type"],
        "sort_order": row["sort_order"],
        "character_key": row["character_key"],
        "character_name": character_name,
        "source_text": row["source_text"],
        "speech_text": row["speech_text"],
        "subtitle_text": row["subtitle_text"],
        "subtitle_visible": bool(row["subtitle_visible"]),
        "voice_key": row["voice_key"],
        "speed": row["speed"],
        "pitch": row["pitch"],
        "volume": row["volume"],
        "current_version": row["current_version"],
        "start_ms": row["start_ms"],
        "end_ms": row["end_ms"],
        "input_hash": row["input_hash"],
        "versions": [],
        "job": None,
    }


def build_voice_snapshot(conn, project):
    conn.row_factory = sqlite3.Row
    characters = {
        row["character_key"]: row["name"] for row in conn.execute(
            "SELECT character_key,name FROM short_drama_characters WHERE project_id=?",
            (project["id"],),
        )
    }
    voice_shots = {
        row["shot_id"]: row for row in conn.execute(
            "SELECT * FROM short_drama_voice_shots WHERE project_id=?",
            (project["id"],),
        )
    }
    lines = {}
    for row in conn.execute(
        "SELECT * FROM short_drama_voice_lines WHERE project_id=? "
        "ORDER BY shot_id,sort_order",
        (project["id"],),
    ):
        lines.setdefault(row["shot_id"], []).append(
            _line_snapshot(row, characters.get(row["character_key"], row["character_key"]))
        )
    shots = []
    for shot in conn.execute(
        "SELECT id,shot_key,sort_order,duration FROM short_drama_shots "
        "WHERE project_id=? ORDER BY sort_order,id",
        (project["id"],),
    ):
        shot_lines = lines.get(shot["id"], [])
        state = voice_shots[shot["id"]]
        shots.append({
            "id": shot["id"],
            "shot_key": shot["shot_key"],
            "sort_order": shot["sort_order"],
            "duration": shot["duration"],
            "locked": bool(state["locked"]),
            "timeline_revision": state["timeline_revision"],
            "status": "silent" if not shot_lines else "pending",
            "lines": shot_lines,
        })
    return {
        "project_id": project["id"],
        "revision": project["revision"],
        "stage": project["stage"],
        "ratio": project["ratio"],
        "target_duration": project["target_duration"],
        "point_budget": project["point_budget"],
        "spent_points": project["spent_points"],
        "reserved_points": 0,
        "shots": shots,
    }


def get_voice_workspace(db_factory, username, project_id):
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT * FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        ensure_voice_workspace(conn, project_id)
        snapshot = build_voice_snapshot(conn, project)
        conn.commit()
        return snapshot
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
