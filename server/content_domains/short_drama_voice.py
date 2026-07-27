"""Voice-line snapshots and read models for short-drama production."""

import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import closing


VOICE_STAGES = {
    "voice_review", "video_review", "assembly_review", "completed",
}
VOICE_WRITE_STAGE = "voice_review"
VOICE_QUOTE_TTL_SECONDS = 300
VOICE_QUOTE_MAX_LINES = 50
VOICE_ENDPOINT = "/api/gen/short-drama/generate-voice"
VOICE_SUBTITLE_MAX_LENGTH = 2000
VOICE_TIMELINE_GAP_MS = 150


class VoiceQuoteConsumed(RuntimeError):
    pass


class VoiceChargeInProgress(RuntimeError):
    pass


class VoiceTimelineValidationError(ValueError):
    def __init__(self, blocker):
        self.blocker = dict(blocker or {})
        super().__init__(
            self.blocker.get("message") or "字幕时间轴校验失败"
        )


_BLOCKER_MESSAGES = {
    "missing_current_version": "存在尚未生成成功配音的台词",
    "stale_current_version": "当前配音版本与台词或音色参数不一致",
    "metadata_pending": "音频时长仍在解析，请稍后刷新",
    "timeline_missing": "字幕时间轴尚未保存",
    "timeline_invalid": "字幕时间值不完整或顺序无效",
    "subtitle_overlap": "可见字幕时间区间发生重叠",
    "audio_overlap": "配音播放时间发生重叠",
    "duration_overflow": "配音或字幕超过镜头时长",
    "active_job": "当前镜头仍有配音任务处理中",
    "refund_pending": "当前镜头仍有退款处理中",
    "charge_attempt_pending": "当前镜头仍有未结算的扣点尝试",
    "missing_locked_voice_shot": "仍有镜头尚未锁定",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_voice_shots (
  shot_id TEXT PRIMARY KEY REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0,1)),
  audio_mode TEXT NOT NULL DEFAULT 'voiceover'
    CHECK (audio_mode IN ('voiceover','native')),
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
"""

_TRIGGER_SCHEMA = """
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
FOR EACH ROW WHEN NEW.shot_id IS NOT OLD.shot_id
  OR NEW.project_id IS NOT OLD.project_id
BEGIN
  SELECT RAISE(ABORT, 'voice shot source identity is immutable');
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
FOR EACH ROW WHEN NEW.project_id IS NOT OLD.project_id
  OR NEW.shot_id IS NOT OLD.shot_id
BEGIN
  SELECT RAISE(ABORT, 'voice line source identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_voice_lines_source_text_immutable
BEFORE UPDATE OF dialogue_line_id, line_type, sort_order, character_key, source_text
ON short_drama_voice_lines
FOR EACH ROW WHEN NEW.dialogue_line_id IS NOT OLD.dialogue_line_id
  OR NEW.line_type IS NOT OLD.line_type
  OR NEW.sort_order IS NOT OLD.sort_order
  OR NEW.character_key IS NOT OLD.character_key
  OR NEW.source_text IS NOT OLD.source_text
BEGIN
  SELECT RAISE(ABORT, 'voice line source identity is immutable');
END;
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
FOR EACH ROW
BEGIN
  SELECT CASE WHEN NOT EXISTS (
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
  ) THEN RAISE(ABORT, 'voice job references must share one project and actor') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM short_drama_voice_quotes AS quote
    WHERE quote.consumed_job_id=OLD.job_id
      AND (NEW.job_id IS NOT OLD.job_id OR quote.username<>NEW.username
        OR quote.project_id<>NEW.project_id OR quote.voice_line_id<>NEW.voice_line_id)
  ) OR EXISTS (
    SELECT 1 FROM short_drama_voice_charge_attempts AS attempt
    WHERE attempt.job_id=OLD.job_id
      AND (NEW.job_id IS NOT OLD.job_id OR attempt.username<>NEW.username
        OR attempt.project_id<>NEW.project_id OR attempt.shot_id<>NEW.shot_id
        OR attempt.voice_line_id<>NEW.voice_line_id)
  ) OR EXISTS (
    SELECT 1 FROM short_drama_voice_versions AS version
    WHERE version.job_id=OLD.job_id
      AND (NEW.job_id IS NOT OLD.job_id OR version.voice_line_id<>NEW.voice_line_id)
  ) THEN RAISE(ABORT, 'voice job identity is referenced') END;
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
BEFORE UPDATE OF token, username, project_id, voice_line_id, consumed_job_id
ON short_drama_voice_quotes
FOR EACH ROW
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM short_drama_projects AS project
    JOIN short_drama_voice_lines AS line
      ON line.id=NEW.voice_line_id AND line.project_id=NEW.project_id
    WHERE project.id=NEW.project_id
      AND (NEW.consumed_job_id IS NULL OR EXISTS (
        SELECT 1 FROM short_drama_voice_jobs AS job
        WHERE job.job_id=NEW.consumed_job_id AND job.username=NEW.username
          AND job.project_id=NEW.project_id AND job.voice_line_id=NEW.voice_line_id
      ))
  ) THEN RAISE(ABORT, 'voice quote references must share one project and actor') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM short_drama_voice_charge_attempts AS attempt
    WHERE attempt.quote_token=OLD.token
      AND (NEW.token IS NOT OLD.token OR attempt.username<>NEW.username
        OR attempt.project_id<>NEW.project_id
        OR attempt.voice_line_id<>NEW.voice_line_id
        OR attempt.job_id IS NOT NEW.consumed_job_id)
  ) THEN RAISE(ABORT, 'voice quote identity is referenced') END;
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
    AND (NEW.job_id IS NULL OR quote.consumed_job_id IS NULL
      OR quote.consumed_job_id IS NEW.job_id)
  WHERE project.id=NEW.project_id
    AND NOT EXISTS (
      SELECT 1 FROM short_drama_voice_charge_attempts AS existing
      WHERE (existing.charge_key=NEW.charge_key
          OR existing.refund_key=NEW.refund_key
          OR (existing.username=NEW.username AND existing.endpoint=NEW.endpoint
            AND existing.idempotency_key=NEW.idempotency_key))
        AND existing.job_id IS NOT NULL
        AND NEW.job_id IS NOT existing.job_id
    )
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
    AND (NEW.job_id IS NULL OR quote.consumed_job_id IS NULL
      OR quote.consumed_job_id IS NEW.job_id)
  WHERE project.id=NEW.project_id
    AND (OLD.job_id IS NULL OR NEW.job_id IS OLD.job_id)
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
CREATE TRIGGER short_drama_voice_versions_line_job_guard
BEFORE INSERT ON short_drama_voice_versions
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_voice_jobs AS job
  WHERE job.job_id=NEW.job_id AND job.voice_line_id=NEW.voice_line_id
)
BEGIN
  SELECT RAISE(ABORT, 'voice version job does not belong to line');
END;
CREATE TRIGGER short_drama_voice_versions_line_job_update_guard
BEFORE UPDATE OF voice_line_id, job_id ON short_drama_voice_versions
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_voice_jobs AS job
  WHERE job.job_id=NEW.job_id AND job.voice_line_id=NEW.voice_line_id
)
BEGIN
  SELECT RAISE(ABORT, 'voice version job does not belong to line');
END;
"""


_VOICE_TRIGGER_NAMES = (
    "short_drama_voice_shots_project_guard",
    "short_drama_voice_shots_project_update_guard",
    "short_drama_voice_lines_project_guard",
    "short_drama_voice_lines_project_update_guard",
    "short_drama_voice_lines_source_text_immutable",
    "short_drama_voice_jobs_project_guard",
    "short_drama_voice_jobs_project_update_guard",
    "short_drama_voice_quotes_project_guard",
    "short_drama_voice_quotes_project_update_guard",
    "short_drama_voice_charge_attempts_project_guard",
    "short_drama_voice_charge_attempts_project_update_guard",
    "short_drama_voice_versions_line_job_guard",
    "short_drama_voice_versions_line_job_update_guard",
)


def _replace_voice_triggers(conn):
    for name in _VOICE_TRIGGER_NAMES:
        conn.execute("DROP TRIGGER IF EXISTS %s" % name)
    conn.executescript(_TRIGGER_SCHEMA)


def init_db(db_factory):
    conn = db_factory()
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(short_drama_voice_shots)"
            )
        }
        if "audio_mode" not in columns:
            conn.execute(
                "ALTER TABLE short_drama_voice_shots ADD COLUMN audio_mode "
                "TEXT NOT NULL DEFAULT 'voiceover' "
                "CHECK (audio_mode IN ('voiceover','native'))"
            )
        _replace_voice_triggers(conn)
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


def _request_hash(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _voice_request_item(raw):
    if not isinstance(raw, dict):
        raise ValueError("配音参数无效")
    allowed = {"line_id", "voice_key", "speed", "pitch", "volume"}
    if set(raw) != allowed:
        raise ValueError("配音参数字段不正确")
    line_id = str(raw.get("line_id") or "").strip()
    voice_key = str(raw.get("voice_key") or "").strip()
    if not line_id:
        raise ValueError("缺少配音台词")
    if not voice_key or len(voice_key) > 160:
        raise ValueError("请选择有效音色")
    speed = raw.get("speed")
    pitch = raw.get("pitch")
    volume = raw.get("volume")
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.5 <= speed <= 2:
        raise ValueError("语速必须在 0.5-2.0 之间")
    if isinstance(pitch, bool) or type(pitch) is not int or not -12 <= pitch <= 12:
        raise ValueError("音调必须为 -12 到 12 的整数")
    if isinstance(volume, bool) or type(volume) is not int or not -50 <= volume <= 100:
        raise ValueError("音量必须为 -50 到 100 的整数")
    settings = {
        "speed": round(float(speed), 1),
        "pitch": pitch,
        "volume": volume,
    }
    return {
        "line_id": line_id,
        "voice_key": voice_key,
        **settings,
    }


def normalize_quote_request(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    if set(payload) != {"project_id", "revision", "items"}:
        raise ValueError("询价请求字段不正确")
    project_id = str(payload.get("project_id") or "").strip()
    revision = payload.get("revision")
    items = payload.get("items")
    if not project_id:
        raise ValueError("缺少短剧项目 ID")
    if type(revision) is not int:
        raise ValueError("项目版本无效")
    if not isinstance(items, list) or not 1 <= len(items) <= VOICE_QUOTE_MAX_LINES:
        raise ValueError("每次询价需包含 1-%d 条台词" % VOICE_QUOTE_MAX_LINES)
    normalized = [_voice_request_item(item) for item in items]
    line_ids = [item["line_id"] for item in normalized]
    if len(set(line_ids)) != len(line_ids):
        raise ValueError("同一台词不能重复询价")
    return {"project_id": project_id, "revision": revision, "items": normalized}


def normalize_generate_request(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    allowed = {
        "project_id", "revision", "line_id", "voice_key",
        "speed", "pitch", "volume", "quote_token",
    }
    if set(payload) != allowed:
        raise ValueError("配音生成请求字段不正确")
    item = _voice_request_item({
        "line_id": payload.get("line_id"),
        "voice_key": payload.get("voice_key"),
        "speed": payload.get("speed"),
        "pitch": payload.get("pitch"),
        "volume": payload.get("volume"),
    })
    project_id = str(payload.get("project_id") or "").strip()
    quote_token = str(payload.get("quote_token") or "").strip()
    if not project_id:
        raise ValueError("缺少短剧项目 ID")
    if type(payload.get("revision")) is not int:
        raise ValueError("项目版本无效")
    if not quote_token:
        raise ValueError("缺少配音询价凭证")
    return {
        "project_id": project_id,
        "revision": payload["revision"],
        **item,
        "quote_token": quote_token,
    }


def _quote_descriptor(project_id, revision, line, item):
    return {
        "project_id": project_id,
        "revision": revision,
        "line_id": line["id"],
        "speech_text": line["speech_text"],
        "voice_key": item["voice_key"],
        "speed": item["speed"],
        "pitch": item["pitch"],
        "volume": item["volume"],
    }


def prepare_voice_quote(db_factory, actor_username, owner_username, payload, cost_of,
                        voice_validator=None):
    request = normalize_quote_request(payload)
    if not callable(cost_of):
        raise ValueError("配音报价暂不可用")
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT * FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (request["project_id"], owner_username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if project["stage"] != VOICE_WRITE_STAGE:
            raise ValueError("当前短剧阶段不能生成配音")
        if int(project["revision"]) != request["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已被更新，请刷新后重试")
        ensure_voice_workspace(conn, project["id"], {VOICE_WRITE_STAGE})
        expires_at = int(time.time()) + VOICE_QUOTE_TTL_SECONDS
        results = []
        total = 0
        for item in request["items"]:
            line = conn.execute(
                "SELECT * FROM short_drama_voice_lines "
                "WHERE id=? AND project_id=?",
                (item["line_id"], project["id"]),
            ).fetchone()
            if not line:
                raise LookupError("配音台词不存在")
            if conn.execute(
                "SELECT locked FROM short_drama_voice_shots "
                "WHERE shot_id=? AND project_id=?",
                (line["shot_id"], project["id"]),
            ).fetchone()[0]:
                raise ValueError("已锁定镜头不能重新生成配音")
            if not str(line["speech_text"] or "").strip():
                raise ValueError("配音台词不能为空")
            if callable(voice_validator):
                voice_validator(actor_username, item["voice_key"])
            descriptor = _quote_descriptor(
                project["id"], request["revision"], line, item
            )
            audio_payload = {
                "text": line["speech_text"],
                "voice": item["voice_key"],
                "speed": item["speed"],
                "pitch": item["pitch"],
                "volume": item["volume"],
            }
            cost = int(cost_of("audio", audio_payload))
            if cost < 0:
                raise ValueError("配音报价无效")
            token = uuid.uuid4().hex
            request_hash = _request_hash(descriptor)
            conn.execute(
                "INSERT INTO short_drama_voice_quotes "
                "(token,username,project_id,voice_line_id,request_hash,cost,"
                "expires_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    token, actor_username, project["id"], line["id"],
                    request_hash, cost, expires_at, int(time.time()),
                ),
            )
            results.append({
                "line_id": line["id"],
                "shot_id": line["shot_id"],
                "quote_token": token,
                "cost": cost,
                "expires_at": expires_at,
                "input": descriptor,
            })
            total += cost
        from .short_drama import _project_point_usage
        usage = _project_point_usage(conn, project["id"])
        budget = int(project["point_budget"] or 0)
        budget_left = None if budget == 0 else max(
            0, budget - usage["spent_points"] - usage["reserved_points"]
        )
        conn.commit()
        return {
            "project_id": project["id"],
            "revision": int(project["revision"]),
            "items": results,
            "total_cost": total,
            "point_budget": budget,
            "budget_left": budget_left,
            "can_submit": budget_left is None or total <= budget_left,
            "spent_points": usage["spent_points"],
            "reserved_points": usage["reserved_points"],
            "expires_at": expires_at,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def prepare_voice_submission(db_factory, actor_username, owner_username, payload,
                             idempotency_key):
    request = normalize_generate_request(payload)
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("配音生成必须提供 Idempotency-Key")
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT * FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (request["project_id"], owner_username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if project["stage"] != VOICE_WRITE_STAGE:
            raise ValueError("当前短剧阶段不能生成配音")
        if int(project["revision"]) != request["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已被更新，请刷新后重试")
        line = conn.execute(
            "SELECT * FROM short_drama_voice_lines WHERE id=? AND project_id=?",
            (request["line_id"], project["id"]),
        ).fetchone()
        if not line:
            raise LookupError("配音台词不存在")
        if conn.execute(
            "SELECT locked FROM short_drama_voice_shots "
            "WHERE shot_id=? AND project_id=?",
            (line["shot_id"], project["id"]),
        ).fetchone()[0]:
            raise ValueError("已锁定镜头不能重新生成配音")
        descriptor = _quote_descriptor(project["id"], request["revision"], line, request)
        request_hash = _request_hash(descriptor)
        existing = conn.execute(
            "SELECT * FROM short_drama_voice_charge_attempts "
            "WHERE username=? AND endpoint=? AND idempotency_key=?",
            (actor_username, VOICE_ENDPOINT, key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise VoiceQuoteConsumed("同一个 Idempotency-Key 不能用于不同请求")
            conn.commit()
            return _attempt_dict(existing), True
        if conn.execute(
            "SELECT 1 FROM short_drama_voice_charge_attempts AS attempt "
            "LEFT JOIN short_drama_voice_jobs AS job ON job.job_id=attempt.job_id "
            "WHERE attempt.project_id=? AND attempt.voice_line_id=? "
            "AND (attempt.state IN ('accepted','charged','linked','refund_pending') "
            "OR job.status IN ('pending','running','metadata_pending')) LIMIT 1",
            (project["id"], line["id"]),
        ).fetchone():
            raise VoiceChargeInProgress("该台词已有配音任务正在处理")
        quote = conn.execute(
            "SELECT * FROM short_drama_voice_quotes "
            "WHERE token=? AND username=? AND project_id=? AND voice_line_id=?",
            (
                request["quote_token"], actor_username,
                project["id"], line["id"],
            ),
        ).fetchone()
        if not quote:
            raise LookupError("配音询价凭证不存在")
        if quote["request_hash"] != request_hash:
            raise ValueError("配音请求与询价内容不一致")
        if int(quote["expires_at"]) < int(time.time()):
            raise ValueError("配音询价已过期，请重新询价")
        if quote["consumed_idempotency_key"]:
            if quote["consumed_idempotency_key"] != key:
                raise VoiceQuoteConsumed("配音询价凭证已被使用")
        from .short_drama import _project_point_usage, PointBudgetExceeded
        usage = _project_point_usage(conn, project["id"])
        budget = int(project["point_budget"] or 0)
        cost = int(quote["cost"])
        if budget and usage["spent_points"] + usage["reserved_points"] + cost > budget:
            raise PointBudgetExceeded(
                "短剧点数预算不足：已用 %d 点、已预留 %d 点、本次 %d 点、预算 %d 点" %
                (
                    usage["spent_points"], usage["reserved_points"],
                    cost, budget,
                )
            )
        now = int(time.time())
        charge_key = "sdv-charge:" + uuid.uuid4().hex
        attempt = {
            "charge_key": charge_key,
            "refund_key": "sdv-refund:" + uuid.uuid4().hex,
            "username": actor_username,
            "endpoint": VOICE_ENDPOINT,
            "idempotency_key": key,
            "request_hash": request_hash,
            "project_id": project["id"],
            "shot_id": line["shot_id"],
            "voice_line_id": line["id"],
            "quote_token": quote["token"],
            "cost": cost,
            "audio_payload": {
                "text": line["speech_text"],
                "voice": request["voice_key"],
                "speed": request["speed"],
                "pitch": request["pitch"],
                "volume": request["volume"],
                "_short_drama_voice": {
                    "project_id": project["id"],
                    "shot_id": line["shot_id"],
                    "line_id": line["id"],
                    "input_hash": voice_input_hash(
                        line["speech_text"], request["voice_key"],
                        request["speed"], request["pitch"], request["volume"],
                    ),
                },
            },
            "state": "accepted",
            "points_left": None,
            "job_id": None,
            "terminal_response": None,
            "created_at": now,
            "updated_at": now,
        }
        input_hash = attempt["audio_payload"]["_short_drama_voice"]["input_hash"]
        conn.execute(
            "UPDATE short_drama_voice_lines "
            "SET voice_key=?,speed=?,pitch=?,volume=?,input_hash=?,updated_at=? "
            "WHERE id=?",
            (
                request["voice_key"], request["speed"], request["pitch"],
                request["volume"], input_hash, now, line["id"],
            ),
        )
        conn.execute(
            "INSERT INTO short_drama_voice_charge_attempts "
            "(charge_key,refund_key,username,endpoint,idempotency_key,request_hash,"
            "project_id,shot_id,voice_line_id,quote_token,cost,audio_payload_json,"
            "state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                attempt["charge_key"], attempt["refund_key"], actor_username,
                VOICE_ENDPOINT, key, request_hash, project["id"], line["shot_id"],
                line["id"], quote["token"], cost,
                json.dumps(attempt["audio_payload"], ensure_ascii=False),
                "accepted", now, now,
            ),
        )
        conn.execute(
            "UPDATE short_drama_voice_quotes "
            "SET consumed_idempotency_key=? WHERE token=?",
            (key, quote["token"]),
        )
        conn.commit()
        return attempt, False
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _attempt_dict(row):
    value = dict(row)
    value["audio_payload"] = _json_value(value.pop("audio_payload_json", ""), {})
    value["terminal_response"] = _json_value(value.pop("terminal_json", ""), None)
    return value


def recover_voice_submission(db_factory, actor_username, payload, idempotency_key):
    """Return an immutable persisted operation before consulting mutable project state."""
    request = normalize_generate_request(payload)
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("配音生成必须提供 Idempotency-Key")
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT attempts.*,projects.username AS owner_username "
            "FROM short_drama_voice_charge_attempts AS attempts "
            "LEFT JOIN short_drama_projects AS projects "
            "ON projects.id=attempts.project_id "
            "WHERE attempts.username=? AND attempts.endpoint=? "
            "AND attempts.idempotency_key=?",
            (actor_username, VOICE_ENDPOINT, key),
        ).fetchone()
        if not row:
            return None
        attempt = _attempt_dict(row)
        audio_payload = attempt.get("audio_payload") or {}
        descriptor = {
            "project_id": request["project_id"],
            "revision": request["revision"],
            "line_id": request["line_id"],
            "speech_text": str(audio_payload.get("text") or ""),
            "voice_key": request["voice_key"],
            "speed": request["speed"],
            "pitch": request["pitch"],
            "volume": request["volume"],
        }
        if (
            attempt["project_id"] != request["project_id"]
            or attempt["voice_line_id"] != request["line_id"]
            or attempt["quote_token"] != request["quote_token"]
            or attempt["request_hash"] != _request_hash(descriptor)
        ):
            raise VoiceQuoteConsumed(
                "同一个 Idempotency-Key 不能用于不同请求"
            )
        return attempt


def get_voice_attempt(db_factory, actor_username, idempotency_key):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM short_drama_voice_charge_attempts "
            "WHERE username=? AND endpoint=? AND idempotency_key=?",
            (actor_username, VOICE_ENDPOINT, idempotency_key),
        ).fetchone()
        return _attempt_dict(row) if row else None


def mark_voice_attempt_charged(db_factory, actor_username, idempotency_key,
                               points_left):
    with closing(db_factory()) as conn:
        conn.execute(
            "UPDATE short_drama_voice_charge_attempts "
            "SET state='charged',points_left=?,updated_at=? "
            "WHERE username=? AND endpoint=? AND idempotency_key=? "
            "AND state='accepted'",
            (
                int(points_left), int(time.time()), actor_username,
                VOICE_ENDPOINT, idempotency_key,
            ),
        )
        conn.commit()
    return get_voice_attempt(db_factory, actor_username, idempotency_key)


def mark_voice_attempt_failed(db_factory, actor_username, idempotency_key,
                              terminal_response):
    with closing(db_factory()) as conn:
        conn.execute(
            "UPDATE short_drama_voice_charge_attempts "
            "SET state='failed',terminal_json=?,updated_at=? "
            "WHERE username=? AND endpoint=? AND idempotency_key=? "
            "AND state='accepted'",
            (
                json.dumps(terminal_response or {}, ensure_ascii=False),
                int(time.time()), actor_username, VOICE_ENDPOINT, idempotency_key,
            ),
        )
        conn.commit()
    return get_voice_attempt(db_factory, actor_username, idempotency_key)


def bind_voice_job(db_factory, actor_username, idempotency_key, connection, job_id):
    connection.row_factory = sqlite3.Row
    attempt = connection.execute(
        "SELECT * FROM short_drama_voice_charge_attempts "
        "WHERE username=? AND endpoint=? AND idempotency_key=?",
        (actor_username, VOICE_ENDPOINT, idempotency_key),
    ).fetchone()
    if not attempt or attempt["state"] != "charged" or attempt["job_id"] is not None:
        raise VoiceChargeInProgress("配音扣点记录状态不允许绑定任务")
    now = int(time.time())
    connection.execute(
        "INSERT INTO short_drama_voice_jobs "
        "(id,username,project_id,shot_id,voice_line_id,job_id,idempotency_key,"
        "quoted_cost,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()), actor_username, attempt["project_id"],
            attempt["shot_id"], attempt["voice_line_id"], int(job_id),
            idempotency_key, int(attempt["cost"]), "pending", now, now,
        ),
    )
    connection.execute(
        "UPDATE short_drama_voice_charge_attempts "
        "SET state='linked',job_id=?,updated_at=? WHERE charge_key=?",
        (int(job_id), now, attempt["charge_key"]),
    )
    connection.execute(
        "UPDATE short_drama_voice_quotes SET consumed_job_id=? WHERE token=?",
        (int(job_id), attempt["quote_token"]),
    )


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


def mark_voice_attempt_refund_pending(db_factory, actor_username, idempotency_key,
                                      terminal_response):
    with closing(db_factory()) as conn:
        conn.execute(
            "UPDATE short_drama_voice_charge_attempts "
            "SET state='refund_pending',terminal_json=?,updated_at=? "
            "WHERE username=? AND endpoint=? AND idempotency_key=? "
            "AND state IN ('charged','linked')",
            (
                json.dumps(terminal_response or {}, ensure_ascii=False),
                int(time.time()), actor_username, VOICE_ENDPOINT, idempotency_key,
            ),
        )
        conn.commit()
    return get_voice_attempt(db_factory, actor_username, idempotency_key)


def mark_voice_attempt_refunded(db_factory, actor_username, idempotency_key):
    with closing(db_factory()) as conn:
        conn.execute(
            "UPDATE short_drama_voice_charge_attempts "
            "SET state='refunded',updated_at=? "
            "WHERE username=? AND endpoint=? AND idempotency_key=? "
            "AND state='refund_pending'",
            (int(time.time()), actor_username, VOICE_ENDPOINT, idempotency_key),
        )
        conn.commit()
    return get_voice_attempt(db_factory, actor_username, idempotency_key)


def retry_voice_attempt_refunds(db_factory, points_domain, limit=64):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM short_drama_voice_charge_attempts "
            "WHERE state='refund_pending' AND job_id IS NULL "
            "ORDER BY updated_at LIMIT ?",
            (max(1, int(limit or 1)),),
        ).fetchall()
    refunded = 0
    for row in rows:
        try:
            points_domain.refund_points(
                row["username"], int(row["cost"]),
                "short-drama voice:recovery", transaction_key=row["refund_key"],
            )
            mark_voice_attempt_refunded(
                db_factory, row["username"], row["idempotency_key"]
            )
            refunded += 1
        except Exception:
            continue
    return refunded


def reconcile_voice_jobs(conn, project_id):
    """Project generic audio jobs into the voice read model.

    The generic jobs table remains authoritative for execution/refund status.
    Calling this method repeatedly is safe and repairs a missed worker callback.
    """
    conn.row_factory = sqlite3.Row
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone():
        return
    job_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if not {"id", "status", "result", "error", "refunded"}.issubset(job_columns):
        return
    rows = conn.execute(
        "SELECT voice.*,job.status AS generic_status,job.result,job.error AS generic_error,"
        "COALESCE(job.refunded,0) AS generic_refunded "
        "FROM short_drama_voice_jobs voice "
        "LEFT JOIN jobs job ON job.id=voice.job_id "
        "WHERE voice.project_id=?",
        (project_id,),
    ).fetchall()
    now = int(time.time())
    for row in rows:
        generic_status = row["generic_status"]
        if generic_status in {"pending", "running"}:
            if row["status"] != generic_status:
                conn.execute(
                    "UPDATE short_drama_voice_jobs SET status=?,updated_at=? WHERE id=?",
                    (generic_status, now, row["id"]),
                )
            continue
        if generic_status == "done":
            result = _json_value(row["result"], {})
            if not isinstance(result, dict):
                result = {}
            duration_ms = result.get("duration_ms")
            if type(duration_ms) is not int or duration_ms <= 0:
                duration_ms = None
                if result.get("file"):
                    try:
                        from . import audio
                        duration_ms = audio._audio_duration_ms(result["file"])
                    except Exception:
                        duration_ms = None
            version = conn.execute(
                "SELECT id,voice_line_id,version,status,input_hash "
                "FROM short_drama_voice_versions WHERE job_id=?",
                (row["job_id"],),
            ).fetchone()
            if not version:
                line = conn.execute(
                    "SELECT * FROM short_drama_voice_lines WHERE id=?",
                    (row["voice_line_id"],),
                ).fetchone()
                if not line:
                    continue
                payload_row = conn.execute(
                    "SELECT audio_payload_json FROM short_drama_voice_charge_attempts "
                    "WHERE job_id=?",
                    (row["job_id"],),
                ).fetchone()
                payload = _json_value(payload_row[0] if payload_row else "", {})
                metadata = payload.get("_short_drama_voice") or {}
                settings = normalized_voice_settings(payload)
                input_hash = str(metadata.get("input_hash") or voice_input_hash(
                    payload.get("text") or line["speech_text"],
                    payload.get("voice") or line["voice_key"],
                    settings["speed"], settings["pitch"], settings["volume"],
                ))
                next_version = int(conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 "
                    "FROM short_drama_voice_versions WHERE voice_line_id=?",
                    (line["id"],),
                ).fetchone()[0])
                version_id = str(uuid.uuid4())
                status = "done" if duration_ms else "metadata_pending"
                conn.execute(
                    "INSERT INTO short_drama_voice_versions "
                    "(id,voice_line_id,version,job_id,audio_file,audio_url,duration_ms,"
                    "speech_text,voice_key,settings_json,input_hash,cost,status,error,"
                    "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        version_id, line["id"], next_version, row["job_id"],
                        str(result.get("file") or ""), str(result.get("url") or ""),
                        duration_ms, str(payload.get("text") or line["speech_text"]),
                        str(payload.get("voice") or line["voice_key"]),
                        json.dumps(settings, ensure_ascii=False), input_hash,
                        int(row["quoted_cost"]), status,
                        "" if duration_ms else "音频时长待解析", now,
                    ),
                )
                if duration_ms and input_hash == line["input_hash"]:
                    conn.execute(
                        "UPDATE short_drama_voice_lines "
                        "SET current_version=?,updated_at=? WHERE id=?",
                        (next_version, now, line["id"]),
                    )
            elif duration_ms and version["status"] == "metadata_pending":
                conn.execute(
                    "UPDATE short_drama_voice_versions "
                    "SET duration_ms=?,status='done',error='' WHERE id=?",
                    (duration_ms, version["id"]),
                )
                line = conn.execute(
                    "SELECT input_hash FROM short_drama_voice_lines WHERE id=?",
                    (version["voice_line_id"],),
                ).fetchone()
                if line and line["input_hash"] == version["input_hash"]:
                    conn.execute(
                        "UPDATE short_drama_voice_lines "
                        "SET current_version=?,updated_at=? WHERE id=?",
                        (
                            version["version"], now,
                            version["voice_line_id"],
                        ),
                    )
            target = "done" if duration_ms else "metadata_pending"
            conn.execute(
                "UPDATE short_drama_voice_jobs SET status=?,error=?,updated_at=? WHERE id=?",
                (
                    target, "" if duration_ms else "音频时长待解析",
                    now, row["id"],
                ),
            )
            conn.execute(
                "UPDATE short_drama_voice_charge_attempts "
                "SET state='done',updated_at=? WHERE job_id=? AND state='linked'",
                (now, row["job_id"]),
            )
            continue
        if generic_status == "error":
            error = str(row["generic_error"] or "配音生成失败")[:500]
            refunded = int(row["generic_refunded"] or 0)
            if not conn.execute(
                    "SELECT 1 FROM short_drama_voice_versions WHERE job_id=?",
                    (row["job_id"],),
            ).fetchone():
                line = conn.execute(
                    "SELECT * FROM short_drama_voice_lines WHERE id=?",
                    (row["voice_line_id"],),
                ).fetchone()
                payload_row = conn.execute(
                    "SELECT audio_payload_json FROM short_drama_voice_charge_attempts "
                    "WHERE job_id=?",
                    (row["job_id"],),
                ).fetchone()
                payload = _json_value(payload_row[0] if payload_row else "", {})
                metadata = payload.get("_short_drama_voice") or {}
                settings = normalized_voice_settings(payload)
                if line:
                    next_version = int(conn.execute(
                        "SELECT COALESCE(MAX(version),0)+1 "
                        "FROM short_drama_voice_versions WHERE voice_line_id=?",
                        (line["id"],),
                    ).fetchone()[0])
                    conn.execute(
                        "INSERT INTO short_drama_voice_versions "
                        "(id,voice_line_id,version,job_id,audio_file,audio_url,"
                        "duration_ms,speech_text,voice_key,settings_json,input_hash,"
                        "cost,status,error,created_at) "
                        "VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?,'failed',?,?)",
                        (
                            str(uuid.uuid4()), line["id"], next_version,
                            row["job_id"], "", "",
                            str(payload.get("text") or line["speech_text"]),
                            str(payload.get("voice") or line["voice_key"]),
                            json.dumps(settings, ensure_ascii=False),
                            str(metadata.get("input_hash") or line["input_hash"]),
                            int(row["quoted_cost"]), error, now,
                        ),
                    )
            conn.execute(
                "UPDATE short_drama_voice_jobs "
                "SET status='failed',error=?,refunded=?,updated_at=? WHERE id=?",
                (error, 1 if refunded == 1 else 2, now, row["id"]),
            )
            conn.execute(
                "UPDATE short_drama_voice_charge_attempts "
                "SET state=?,terminal_json=?,updated_at=? "
                "WHERE job_id=? AND state IN ('linked','refund_pending')",
                (
                    "refunded" if refunded == 1 else "refund_pending",
                    json.dumps({"detail": error}, ensure_ascii=False),
                    now, row["job_id"],
                ),
            )


def select_voice_version(db_factory, owner_username, payload):
    if not isinstance(payload, dict) or set(payload) != {
            "project_id", "revision", "line_id", "version"}:
        raise ValueError("版本选择请求字段不正确")
    project_id = str(payload.get("project_id") or "").strip()
    line_id = str(payload.get("line_id") or "").strip()
    revision = payload.get("revision")
    version_number = payload.get("version")
    if not project_id or not line_id or type(revision) is not int:
        raise ValueError("版本选择请求无效")
    if type(version_number) is not int or version_number < 1:
        raise ValueError("配音版本无效")
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT * FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, owner_username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if project["stage"] != VOICE_WRITE_STAGE:
            raise ValueError("当前短剧阶段不能选择配音版本")
        if int(project["revision"]) != revision:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已被更新，请刷新后重试")
        line = conn.execute(
            "SELECT * FROM short_drama_voice_lines WHERE id=? AND project_id=?",
            (line_id, project_id),
        ).fetchone()
        if line and conn.execute(
            "SELECT locked FROM short_drama_voice_shots "
            "WHERE shot_id=? AND project_id=?",
            (line["shot_id"], project_id),
        ).fetchone()[0]:
            raise ValueError("已锁定镜头不能选择配音版本")
        version = conn.execute(
            "SELECT * FROM short_drama_voice_versions "
            "WHERE voice_line_id=? AND version=?",
            (line_id, version_number),
        ).fetchone()
        if not line or not version:
            raise LookupError("配音版本不存在")
        if version["status"] != "done":
            raise ValueError("只能选择已完成的配音版本")
        if version["input_hash"] != line["input_hash"]:
            raise ValueError("该配音版本与当前台词或参数不一致")
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_voice_lines SET current_version=?,updated_at=? WHERE id=?",
            (version_number, now, line_id),
        )
        conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1,updated_at=? WHERE id=?",
            (now, project_id),
        )
        conn.commit()
        return {
            "project_id": project_id,
            "line_id": line_id,
            "current_version": version_number,
            "revision": revision + 1,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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


def _blocker(code, shot_id=None, line_id=None, **details):
    item = {"code": code, "message": _BLOCKER_MESSAGES[code]}
    if shot_id is not None:
        item["shot_id"] = shot_id
    if line_id is not None:
        item["line_id"] = line_id
    item.update({
        key: value for key, value in details.items() if value is not None
    })
    return item


def _current_version(line):
    current = line.get("current_version")
    return next(
        (item for item in line.get("versions", [])
         if item.get("version") == current),
        None,
    )


def _append_unique_blocker(
        blockers, code, shot_id=None, line_id=None, **details):
    identity = (code, shot_id, line_id)
    if any(
        (item["code"], item.get("shot_id"), item.get("line_id")) == identity
        for item in blockers
    ):
        return
    blockers.append(_blocker(code, shot_id, line_id, **details))


def _recommended_voice_speed(current_speed, duration_ms, available_ms):
    if (
        type(current_speed) not in (int, float)
        or not isinstance(duration_ms, int)
        or not isinstance(available_ms, int)
        or not math.isfinite(current_speed)
        or current_speed < 0.5 or current_speed > 2
        or duration_ms <= 0 or available_ms <= 0
    ):
        return None
    required = float(current_speed) * duration_ms / available_ms * 1.03
    recommended = math.ceil(required * 20) / 20
    return recommended if 0.5 <= recommended <= 2 else None


def _timeline_suggestions(lines, duration_limit=None):
    ordered = sorted(lines, key=lambda item: (item["sort_order"], item["id"]))
    durations = []
    for line in ordered:
        version = _current_version(line)
        duration = version.get("duration_ms") if version else None
        if (
            not version or version.get("status") != "done"
            or version.get("input_hash") != line.get("input_hash")
            or type(duration) is not int or duration <= 0
        ):
            durations.append(None)
        else:
            durations.append(duration)
    cursor = 0
    suggestions = {}
    for line, duration in zip(ordered, durations):
        if duration is None:
            suggestions[line["id"]] = (None, None)
            continue
        suggestions[line["id"]] = (cursor, cursor + duration)
        cursor += duration + VOICE_TIMELINE_GAP_MS
    return suggestions


def _timeline_blockers(shot):
    blockers = []
    shot_id = shot["id"]
    duration_limit = int(shot["duration"]) * 1000
    lines = sorted(
        shot.get("lines", []),
        key=lambda item: (item["sort_order"], item["id"]),
    )
    audio_intervals = []
    subtitle_intervals = []
    for line in lines:
        line_id = line["id"]
        version = _current_version(line)
        if not version or version.get("status") == "failed":
            _append_unique_blocker(
                blockers, "missing_current_version", shot_id, line_id
            )
            continue
        if version.get("status") == "metadata_pending":
            _append_unique_blocker(
                blockers, "metadata_pending", shot_id, line_id
            )
            continue
        if version.get("input_hash") != line.get("input_hash"):
            _append_unique_blocker(
                blockers, "stale_current_version", shot_id, line_id
            )
            continue
        duration = version.get("duration_ms")
        if type(duration) is not int or duration <= 0:
            _append_unique_blocker(
                blockers, "metadata_pending", shot_id, line_id
            )
            continue
        start_ms = line.get("start_ms")
        end_ms = line.get("end_ms")
        if start_ms is None or end_ms is None:
            _append_unique_blocker(blockers, "timeline_missing", shot_id)
            continue
        if (
            type(start_ms) is not int or type(end_ms) is not int
            or start_ms < 0 or end_ms <= start_ms
        ):
            _append_unique_blocker(
                blockers, "timeline_invalid", shot_id, line_id
            )
            continue
        audio_end_ms = start_ms + duration
        if end_ms > duration_limit or audio_end_ms > duration_limit:
            audio_overflow_ms = max(0, audio_end_ms - duration_limit)
            subtitle_overflow_ms = max(0, end_ms - duration_limit)
            overflow_ms = max(audio_overflow_ms, subtitle_overflow_ms)
            _append_unique_blocker(
                blockers, "duration_overflow", shot_id, line_id,
                shot_duration_ms=duration_limit,
                audio_duration_ms=duration,
                audio_start_ms=start_ms,
                audio_end_ms=audio_end_ms,
                subtitle_end_ms=end_ms,
                audio_overflow_ms=audio_overflow_ms,
                subtitle_overflow_ms=subtitle_overflow_ms,
                overflow_ms=overflow_ms,
                recommended_speed=(
                    _recommended_voice_speed(
                        line.get("speed"), duration, duration_limit - start_ms
                    )
                    if audio_overflow_ms > 0 else None
                ),
            )
        audio_intervals.append((start_ms, start_ms + duration, line_id))
        if line.get("subtitle_visible"):
            if not str(line.get("subtitle_text") or "").strip():
                _append_unique_blocker(
                    blockers, "timeline_invalid", shot_id, line_id
                )
            subtitle_intervals.append((start_ms, end_ms, line_id))
    audio_intervals.sort(key=lambda interval: (interval[0], interval[1], interval[2]))
    subtitle_intervals.sort(
        key=lambda interval: (interval[0], interval[1], interval[2])
    )
    for previous, current in zip(audio_intervals, audio_intervals[1:]):
        if current[0] < previous[1]:
            _append_unique_blocker(
                blockers, "audio_overlap", shot_id, current[2]
            )
    for previous, current in zip(subtitle_intervals, subtitle_intervals[1:]):
        if current[0] < previous[1]:
            _append_unique_blocker(
                blockers, "subtitle_overlap", shot_id, current[2]
            )
    return blockers


def _operational_blockers(conn, shot):
    blockers = []
    shot_id = shot["id"]
    active = conn.execute(
        "SELECT 1 FROM short_drama_voice_jobs "
        "WHERE project_id=? AND shot_id=? "
        "AND status IN ('pending','running','metadata_pending') LIMIT 1",
        (shot["project_id"], shot_id),
    ).fetchone()
    if active:
        _append_unique_blocker(blockers, "active_job", shot_id)
    states = {
        row[0] for row in conn.execute(
            "SELECT state FROM short_drama_voice_charge_attempts "
            "WHERE project_id=? AND shot_id=? "
            "AND state IN ('accepted','charged','refund_pending')",
            (shot["project_id"], shot_id),
        )
    }
    if "refund_pending" in states:
        _append_unique_blocker(blockers, "refund_pending", shot_id)
    if states & {"accepted", "charged"}:
        _append_unique_blocker(
            blockers, "charge_attempt_pending", shot_id
        )
    return blockers


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
    line_map = {
        line["id"]: line for shot_lines in lines.values() for line in shot_lines
    }
    for row in conn.execute(
        "SELECT version.* FROM short_drama_voice_versions version "
        "JOIN short_drama_voice_lines line ON line.id=version.voice_line_id "
        "WHERE line.project_id=? ORDER BY version.voice_line_id,version.version DESC",
        (project["id"],),
    ):
        line = line_map.get(row["voice_line_id"])
        if not line:
            continue
        item = dict(row)
        item["settings"] = _json_value(item.pop("settings_json"), {})
        line["versions"].append(item)
    for row in conn.execute(
        "SELECT * FROM short_drama_voice_jobs WHERE project_id=? "
        "ORDER BY created_at DESC,job_id DESC",
        (project["id"],),
    ):
        line = line_map.get(row["voice_line_id"])
        if line and line["job"] is None:
            line["job"] = dict(row)
    shots = []
    for shot in conn.execute(
        "SELECT id,shot_key,sort_order,duration FROM short_drama_shots "
        "WHERE project_id=? ORDER BY sort_order,id",
        (project["id"],),
    ):
        shot_lines = lines.get(shot["id"], [])
        state = voice_shots[shot["id"]]
        line_statuses = [
            (line["job"] or {}).get("status") or
            ("done" if line["current_version"] else "pending")
            for line in shot_lines
        ]
        if not shot_lines:
            shot_status = "silent"
        elif any(status == "failed" for status in line_statuses):
            shot_status = "failed"
        elif any(status in {"pending", "running", "metadata_pending"}
                 for status in line_statuses):
            shot_status = "pending"
        elif all(status == "done" for status in line_statuses):
            shot_status = "ready"
        else:
            shot_status = "pending"
        shots.append({
            "id": shot["id"],
            "project_id": project["id"],
            "shot_key": shot["shot_key"],
            "sort_order": shot["sort_order"],
            "duration": shot["duration"],
            "locked": bool(state["locked"]),
            "audio_mode": state["audio_mode"],
            "timeline_revision": state["timeline_revision"],
            "status": "native" if state["audio_mode"] == "native" else shot_status,
            "lines": shot_lines,
        })
        current_shot = shots[-1]
        suggestions = _timeline_suggestions(
            shot_lines, int(current_shot["duration"]) * 1000
        )
        for line in shot_lines:
            suggested = suggestions[line["id"]]
            line["suggested_start_ms"] = suggested[0]
            line["suggested_end_ms"] = suggested[1]
        blockers = []
        if current_shot["audio_mode"] != "native":
            blockers = [] if not shot_lines else _timeline_blockers(current_shot)
            blockers.extend(_operational_blockers(conn, current_shot))
        if current_shot["locked"] and blockers:
            conn.execute(
                "UPDATE short_drama_voice_shots SET locked=0,updated_at=? "
                "WHERE shot_id=? AND locked=1",
                (int(time.time()), shot["id"]),
            )
            current_shot["locked"] = False
        current_shot["lock_blockers"] = blockers
        current_shot["lockable"] = not blockers
        if current_shot["locked"] and current_shot["audio_mode"] != "native":
            current_shot["status"] = "done"
    handoff_blockers = []
    for shot in shots:
        if not shot["locked"]:
            handoff_blockers.append(
                _blocker("missing_locked_voice_shot", shot["id"])
            )
        for blocker in shot["lock_blockers"]:
            _append_unique_blocker(
                handoff_blockers, blocker["code"],
                blocker.get("shot_id"), blocker.get("line_id"),
            )
    for shot in shots:
        shot.pop("project_id", None)
    from .short_drama import _project_point_usage
    usage = _project_point_usage(conn, project["id"])
    return {
        "project_id": project["id"],
        "revision": project["revision"],
        "stage": project["stage"],
        "ratio": project["ratio"],
        "target_duration": project["target_duration"],
        "point_budget": project["point_budget"],
        "spent_points": usage["spent_points"],
        "reserved_points": usage["reserved_points"],
        "shots": shots,
        "unlocked_shot_count": sum(not shot["locked"] for shot in shots),
        "handoff_blocked": bool(handoff_blockers),
        "handoff_blockers": handoff_blockers,
    }


def _timeline_request(payload):
    if not isinstance(payload, dict) or set(payload) != {
        "project_id", "revision", "shot_id", "timeline_revision", "items",
    }:
        raise ValueError("字幕时间轴请求字段不正确")
    project_id = str(payload.get("project_id") or "").strip()
    shot_id = str(payload.get("shot_id") or "").strip()
    revision = payload.get("revision")
    timeline_revision = payload.get("timeline_revision")
    items = payload.get("items")
    if (
        not project_id or not shot_id
        or type(revision) is not int or revision < 1
        or type(timeline_revision) is not int or timeline_revision < 1
        or not isinstance(items, list)
    ):
        raise ValueError("字幕时间轴请求无效")
    normalized = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "line_id", "subtitle_text", "subtitle_visible",
            "start_ms", "end_ms",
        }:
            raise ValueError("字幕时间轴条目字段不正确")
        line_id = str(item.get("line_id") or "").strip()
        subtitle_text = item.get("subtitle_text")
        subtitle_visible = item.get("subtitle_visible")
        start_ms = item.get("start_ms")
        end_ms = item.get("end_ms")
        if (
            not line_id or not isinstance(subtitle_text, str)
            or len(subtitle_text) > VOICE_SUBTITLE_MAX_LENGTH
            or type(subtitle_visible) is not bool
            or type(start_ms) is not int or type(end_ms) is not int
            or start_ms < 0 or end_ms <= start_ms
        ):
            raise ValueError("字幕时间轴条目无效")
        subtitle_text = subtitle_text.strip()
        if subtitle_visible and not subtitle_text:
            raise ValueError("显示字幕时字幕文本不能为空")
        normalized.append({
            "line_id": line_id,
            "subtitle_text": subtitle_text,
            "subtitle_visible": subtitle_visible,
            "start_ms": start_ms,
            "end_ms": end_ms,
        })
    return {
        "project_id": project_id,
        "revision": revision,
        "shot_id": shot_id,
        "timeline_revision": timeline_revision,
        "items": normalized,
    }


def _voice_project_for_write(conn, owner_username, project_id, revision):
    conn.row_factory = sqlite3.Row
    project = conn.execute(
        "SELECT * FROM short_drama_projects "
        "WHERE id=? AND username=? AND deleted=0",
        (project_id, owner_username),
    ).fetchone()
    if not project:
        raise LookupError("短剧项目不存在")
    if project["stage"] != VOICE_WRITE_STAGE:
        raise ValueError("当前短剧阶段不能修改配音字幕")
    if int(project["revision"]) != revision:
        from .short_drama import RevisionConflict
        raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
    ensure_voice_workspace(conn, project_id, {VOICE_WRITE_STAGE})
    reconcile_voice_jobs(conn, project_id)
    return project


def _voice_shot(snapshot, shot_id):
    shot = next(
        (item for item in snapshot.get("shots", []) if item["id"] == shot_id),
        None,
    )
    if not shot:
        raise LookupError("短剧镜头不存在")
    return shot


def save_voice_timeline(db_factory, owner_username, payload):
    request = _timeline_request(payload)
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _voice_project_for_write(
            conn, owner_username, request["project_id"], request["revision"]
        )
        snapshot = build_voice_snapshot(conn, project)
        shot = _voice_shot(snapshot, request["shot_id"])
        if shot["locked"]:
            raise ValueError("已锁定镜头不能修改字幕时间轴")
        if int(shot["timeline_revision"]) != request["timeline_revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("镜头时间轴已更新，请刷新后重试")
        expected = {line["id"] for line in shot["lines"]}
        provided = [item["line_id"] for item in request["items"]]
        if len(provided) != len(set(provided)) or set(provided) != expected:
            raise ValueError("必须一次提交当前镜头的全部且不重复台词")
        updates = {item["line_id"]: item for item in request["items"]}
        for line in shot["lines"]:
            line.update(updates[line["id"]])
        blockers = _timeline_blockers(shot)
        if blockers:
            raise VoiceTimelineValidationError(blockers[0])
        now = int(time.time())
        for item in request["items"]:
            conn.execute(
                "UPDATE short_drama_voice_lines SET subtitle_text=?,"
                "subtitle_visible=?,start_ms=?,end_ms=?,updated_at=? "
                "WHERE id=? AND project_id=? AND shot_id=?",
                (
                    item["subtitle_text"], int(item["subtitle_visible"]),
                    item["start_ms"], item["end_ms"], now, item["line_id"],
                    request["project_id"], request["shot_id"],
                ),
            )
        shot_update = conn.execute(
            "UPDATE short_drama_voice_shots "
            "SET timeline_revision=timeline_revision+1,updated_at=? "
            "WHERE shot_id=? AND project_id=? AND timeline_revision=? AND locked=0",
            (
                now, request["shot_id"], request["project_id"],
                request["timeline_revision"],
            ),
        )
        project_update = conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1,updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND stage=? AND deleted=0",
            (
                now, request["project_id"], owner_username,
                request["revision"], VOICE_WRITE_STAGE,
            ),
        )
        if shot_update.rowcount != 1 or project_update.rowcount != 1:
            from .short_drama import RevisionConflict
            raise RevisionConflict("字幕时间轴已在其他页面更新，请刷新后重试")
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=?",
            (request["project_id"],),
        ).fetchone()
        result = build_voice_snapshot(conn, project)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _lock_request(payload):
    if not isinstance(payload, dict) or set(payload) != {
        "project_id", "revision", "shot_id", "timeline_revision", "lock",
    }:
        raise ValueError("镜头锁定请求字段不正确")
    project_id = str(payload.get("project_id") or "").strip()
    shot_id = str(payload.get("shot_id") or "").strip()
    if (
        not project_id or not shot_id
        or type(payload.get("revision")) is not int
        or type(payload.get("timeline_revision")) is not int
        or type(payload.get("lock")) is not bool
    ):
        raise ValueError("镜头锁定请求无效")
    return {
        "project_id": project_id,
        "revision": payload["revision"],
        "shot_id": shot_id,
        "timeline_revision": payload["timeline_revision"],
        "lock": payload["lock"],
    }


def set_voice_shot_lock(db_factory, owner_username, payload):
    request = _lock_request(payload)
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _voice_project_for_write(
            conn, owner_username, request["project_id"], request["revision"]
        )
        snapshot = build_voice_snapshot(conn, project)
        shot = _voice_shot(snapshot, request["shot_id"])
        if int(shot["timeline_revision"]) != request["timeline_revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("镜头时间轴已更新，请刷新后重试")
        if request["lock"] and shot["lock_blockers"]:
            raise ValueError(shot["lock_blockers"][0]["message"])
        now = int(time.time())
        shot_update = conn.execute(
            "UPDATE short_drama_voice_shots SET locked=?,updated_at=? "
            "WHERE shot_id=? AND project_id=? AND timeline_revision=?",
            (
                int(request["lock"]), now, request["shot_id"],
                request["project_id"], request["timeline_revision"],
            ),
        )
        project_update = conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1,updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND stage=? AND deleted=0",
            (
                now, request["project_id"], owner_username,
                request["revision"], VOICE_WRITE_STAGE,
            ),
        )
        if shot_update.rowcount != 1 or project_update.rowcount != 1:
            from .short_drama import RevisionConflict
            raise RevisionConflict("镜头或项目已更新，请刷新后重试")
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=?",
            (request["project_id"],),
        ).fetchone()
        result = build_voice_snapshot(conn, project)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def confirm_voice_stage(db_factory, owner_username, payload):
    if not isinstance(payload, dict) or set(payload) != {
        "project_id", "revision", "stage",
    }:
        raise ValueError("配音阶段确认请求字段不正确")
    project_id = str(payload.get("project_id") or "").strip()
    revision = payload.get("revision")
    if (
        not project_id or type(revision) is not int
        or payload.get("stage") != VOICE_WRITE_STAGE
    ):
        raise ValueError("配音阶段确认请求无效")
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _voice_project_for_write(
            conn, owner_username, project_id, revision
        )
        snapshot = build_voice_snapshot(conn, project)
        if snapshot["handoff_blocked"]:
            raise ValueError(snapshot["handoff_blockers"][0]["message"])
        # The alignment gate and stage CAS must share this write transaction.
        # A provider job commits its recovery identity before it materializes a
        # version, so checking only before this transaction leaves a race.
        from . import short_drama_alignment
        short_drama_alignment.require_locked_if_started_in_transaction(
            conn, project
        )
        now = int(time.time())
        updated = conn.execute(
            "UPDATE short_drama_projects "
            "SET stage='video_review',revision=revision+1,updated_at=? "
            "WHERE id=? AND username=? AND revision=? "
            "AND stage=? AND deleted=0",
            (now, project_id, owner_username, revision, VOICE_WRITE_STAGE),
        )
        if updated.rowcount != 1:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已更新，请刷新后重试")
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=?",
            (project_id,),
        ).fetchone()
        result = build_voice_snapshot(conn, project)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
        reconcile_voice_jobs(conn, project_id)
        snapshot = build_voice_snapshot(conn, project)
        conn.commit()
        return snapshot
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def confirm_native_audio(db_factory, username, project_id, revision):
    """Use each generated clip's own soundtrack for the first production slice."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("短剧项目不能为空")
    if type(revision) is not int or revision < 1:
        raise ValueError("短剧项目版本无效")
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT * FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id.strip(), username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if int(project["revision"]) != revision:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已更新，请刷新后重试")
        if project["stage"] != "voice_review":
            raise ValueError("短剧项目尚未进入配音阶段")
        ensure_voice_workspace(conn, project["id"], allowed_stages={"voice_review"})
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_voice_shots "
            "SET locked=1,audio_mode='native',updated_at=? WHERE project_id=?",
            (now, project["id"]),
        )
        updated = conn.execute(
            "UPDATE short_drama_projects "
            "SET stage='video_review',revision=revision+1,updated_at=? "
            "WHERE id=? AND revision=? AND stage='voice_review'",
            (now, project["id"], revision),
        )
        if updated.rowcount != 1:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已更新，请刷新后重试")
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=?", (project["id"],)
        ).fetchone()
        result = build_voice_snapshot(conn, project)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
