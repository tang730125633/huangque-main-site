"""C-3 short-drama cinematic video workspace and paid-job ledger.

The browser is deliberately not authoritative here.  Quotes, idempotency,
project/shot revisions, active jobs, versions and locks all live in SQLite so
refreshing a tab never creates a second paid provider request.
"""

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import uuid

from . import short_drama_prompt_compiler


VIDEO_WRITE_STAGE = "video_review"
VIDEO_READ_STAGES = {"video_review", "assembly_review", "completed"}
QUOTE_TTL_SECONDS = 300
VIDEO_DURATION_TOLERANCE_MS = 200
VIDEO_CHARGE_RECOVERY_SECONDS = 120
VIDEO_CHARGE_LEDGER_UNCONFIRMED = "video_charge_ledger_unconfirmed"
VIDEO_REQUEST_FIELDS = {
    "project_id", "revision", "shot_id", "video_revision",
    "prompt", "enhance_prompt",
}
VIDEO_SUBMISSION_FIELDS = {
    "project_id", "revision", "shot_id", "video_revision", "quote_token",
}
VIDEO_CAST_FIELDS = {"project_id", "revision", "bindings"}
ACTIVE_JOB_STATES = {"pending", "running", "uploading", "submitted", "downloading",
                     "metadata_pending"}


class VideoBlocked(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class VideoQuoteConsumed(RuntimeError):
    pass


class VideoChargeInProgress(RuntimeError):
    pass


class VideoCastConflict(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_video_shots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  current_version INTEGER,
  locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0,1)),
  video_revision INTEGER NOT NULL DEFAULT 1 CHECK (video_revision >= 1),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(project_id, shot_id)
);
CREATE TABLE IF NOT EXISTS short_drama_video_versions (
  id TEXT PRIMARY KEY,
  video_shot_id TEXT NOT NULL REFERENCES short_drama_video_shots(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  job_id INTEGER NOT NULL UNIQUE,
  url TEXT NOT NULL DEFAULT '',
  file TEXT NOT NULL DEFAULT '',
  cover_url TEXT NOT NULL DEFAULT '',
  cover_file TEXT NOT NULL DEFAULT '',
  duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
  ratio TEXT NOT NULL CHECK (ratio IN ('9:16','16:9')),
  prompt TEXT NOT NULL,
  enhance_prompt INTEGER NOT NULL DEFAULT 0 CHECK (enhance_prompt IN (0,1)),
  input_hash TEXT NOT NULL,
  cost INTEGER NOT NULL DEFAULT 0 CHECK (cost >= 0),
  provider TEXT NOT NULL DEFAULT '',
  provider_video_id TEXT NOT NULL DEFAULT '',
  prompt_template_version TEXT NOT NULL DEFAULT '',
  compiled_prompt_hash TEXT NOT NULL DEFAULT '',
  visual_spec_hash TEXT NOT NULL DEFAULT '',
  semantic_status TEXT NOT NULL DEFAULT 'legacy',
  semantic_report_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK (status IN ('done','failed')),
  created_at INTEGER NOT NULL,
  UNIQUE(video_shot_id, version)
);
CREATE TABLE IF NOT EXISTS short_drama_video_jobs (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  job_id INTEGER NOT NULL UNIQUE,
  provider_video_id TEXT NOT NULL DEFAULT '',
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT '',
  refunded INTEGER NOT NULL DEFAULT 0 CHECK (refunded IN (0,1,2)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, idempotency_key)
);
CREATE TABLE IF NOT EXISTS short_drama_video_quotes (
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  video_revision INTEGER NOT NULL,
  request_hash TEXT NOT NULL,
  prompt TEXT NOT NULL,
  enhance_prompt INTEGER NOT NULL DEFAULT 0 CHECK (enhance_prompt IN (0,1)),
  duration INTEGER NOT NULL,
  ratio TEXT NOT NULL CHECK (ratio IN ('9:16','16:9')),
  cost INTEGER NOT NULL CHECK (cost >= 0),
  input_json TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  consumed_idempotency_key TEXT,
  consumed_job_id INTEGER,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS short_drama_video_charge_attempts (
  charge_key TEXT PRIMARY KEY,
  refund_key TEXT NOT NULL UNIQUE,
  username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  quote_token TEXT NOT NULL REFERENCES short_drama_video_quotes(token),
  cost INTEGER NOT NULL CHECK (cost >= 0),
  video_payload_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN
    ('accepted','charged','linked','done','refund_pending','refunded','failed')),
  points_left INTEGER,
  job_id INTEGER,
  terminal_json TEXT,
  recovery_token TEXT,
  recovery_started_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, endpoint, idempotency_key)
);
CREATE TABLE IF NOT EXISTS short_drama_video_cast (
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  character_key TEXT NOT NULL,
  avatar_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(project_id, character_key),
  UNIQUE(project_id, avatar_id),
  FOREIGN KEY(project_id, character_key)
    REFERENCES short_drama_characters(project_id, character_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_short_drama_video_jobs_project
  ON short_drama_video_jobs(project_id, shot_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_short_drama_video_quotes_lookup
  ON short_drama_video_quotes(username, project_id, shot_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_short_drama_video_attempts_project
  ON short_drama_video_charge_attempts(project_id, shot_id, state, updated_at);
CREATE TRIGGER IF NOT EXISTS short_drama_video_shot_project_guard
BEFORE INSERT ON short_drama_video_shots
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'short drama video shot must belong to project');
END;
CREATE TRIGGER IF NOT EXISTS short_drama_video_job_project_guard
BEFORE INSERT ON short_drama_video_jobs
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_shots
  WHERE id=NEW.shot_id AND project_id=NEW.project_id
)
BEGIN
  SELECT RAISE(ABORT, 'short drama video job shot must belong to project');
END;
"""

_TRIGGER_SCHEMA = """
DROP TRIGGER IF EXISTS short_drama_video_shot_identity_guard;
DROP TRIGGER IF EXISTS short_drama_video_job_identity_guard;
DROP TRIGGER IF EXISTS short_drama_video_job_update_guard;
DROP TRIGGER IF EXISTS short_drama_video_quote_identity_guard;
DROP TRIGGER IF EXISTS short_drama_video_quote_update_guard;
DROP TRIGGER IF EXISTS short_drama_video_attempt_identity_guard;
DROP TRIGGER IF EXISTS short_drama_video_attempt_update_guard;
DROP TRIGGER IF EXISTS short_drama_video_version_job_guard;
CREATE TRIGGER short_drama_video_shot_identity_guard
BEFORE UPDATE OF project_id,shot_id ON short_drama_video_shots
FOR EACH ROW WHEN NEW.project_id IS NOT OLD.project_id OR NEW.shot_id IS NOT OLD.shot_id
BEGIN SELECT RAISE(ABORT,'video shot identity is immutable'); END;
CREATE TRIGGER short_drama_video_job_identity_guard
BEFORE INSERT ON short_drama_video_jobs
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_projects project
  JOIN short_drama_shots shot ON shot.id=NEW.shot_id AND shot.project_id=NEW.project_id
  WHERE project.id=NEW.project_id AND project.username=NEW.owner_username
)
BEGIN SELECT RAISE(ABORT,'video job references must share one project and owner'); END;
CREATE TRIGGER short_drama_video_job_update_guard
BEFORE UPDATE OF username,owner_username,project_id,shot_id,job_id,idempotency_key
ON short_drama_video_jobs
FOR EACH ROW WHEN NEW.username IS NOT OLD.username
 OR NEW.owner_username IS NOT OLD.owner_username
 OR NEW.project_id IS NOT OLD.project_id OR NEW.shot_id IS NOT OLD.shot_id
 OR NEW.job_id IS NOT OLD.job_id OR NEW.idempotency_key IS NOT OLD.idempotency_key
BEGIN SELECT RAISE(ABORT,'video job identity is immutable'); END;
CREATE TRIGGER short_drama_video_quote_identity_guard
BEFORE INSERT ON short_drama_video_quotes
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_projects project
  JOIN short_drama_shots shot ON shot.id=NEW.shot_id AND shot.project_id=NEW.project_id
  WHERE project.id=NEW.project_id AND project.username=NEW.owner_username
)
BEGIN SELECT RAISE(ABORT,'video quote references must share one project and owner'); END;
CREATE TRIGGER short_drama_video_quote_update_guard
BEFORE UPDATE OF token,username,owner_username,project_id,shot_id,
  consumed_idempotency_key,consumed_job_id ON short_drama_video_quotes
FOR EACH ROW WHEN NEW.token IS NOT OLD.token OR NEW.username IS NOT OLD.username
 OR NEW.owner_username IS NOT OLD.owner_username
 OR NEW.project_id IS NOT OLD.project_id OR NEW.shot_id IS NOT OLD.shot_id
 OR (OLD.consumed_idempotency_key IS NOT NULL
   AND NEW.consumed_idempotency_key IS NOT OLD.consumed_idempotency_key)
 OR (OLD.consumed_job_id IS NOT NULL AND NEW.consumed_job_id IS NOT OLD.consumed_job_id)
 OR (NEW.consumed_job_id IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM short_drama_video_jobs job
   WHERE job.job_id=NEW.consumed_job_id AND job.username=NEW.username
    AND job.owner_username=NEW.owner_username AND job.project_id=NEW.project_id
    AND job.shot_id=NEW.shot_id
 ))
BEGIN SELECT RAISE(ABORT,'video quote identity is invalid or already bound'); END;
CREATE TRIGGER short_drama_video_attempt_identity_guard
BEFORE INSERT ON short_drama_video_charge_attempts
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_video_quotes quote
  WHERE quote.token=NEW.quote_token AND quote.username=NEW.username
   AND quote.owner_username=NEW.owner_username AND quote.project_id=NEW.project_id
   AND quote.shot_id=NEW.shot_id AND NEW.job_id IS NULL
)
BEGIN SELECT RAISE(ABORT,'video charge references must match the quote'); END;
CREATE TRIGGER short_drama_video_attempt_update_guard
BEFORE UPDATE OF username,owner_username,endpoint,idempotency_key,project_id,
  shot_id,quote_token,job_id ON short_drama_video_charge_attempts
FOR EACH ROW WHEN NEW.username IS NOT OLD.username
 OR NEW.owner_username IS NOT OLD.owner_username OR NEW.endpoint IS NOT OLD.endpoint
 OR NEW.idempotency_key IS NOT OLD.idempotency_key
 OR NEW.project_id IS NOT OLD.project_id OR NEW.shot_id IS NOT OLD.shot_id
 OR NEW.quote_token IS NOT OLD.quote_token
 OR (OLD.job_id IS NOT NULL AND NEW.job_id IS NOT OLD.job_id)
 OR (NEW.job_id IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM short_drama_video_jobs job
   WHERE job.job_id=NEW.job_id AND job.username=NEW.username
    AND job.owner_username=NEW.owner_username AND job.project_id=NEW.project_id
    AND job.shot_id=NEW.shot_id AND job.idempotency_key=NEW.idempotency_key
 ))
BEGIN SELECT RAISE(ABORT,'video charge identity is invalid or already bound'); END;
CREATE TRIGGER short_drama_video_version_job_guard
BEFORE INSERT ON short_drama_video_versions
FOR EACH ROW WHEN NOT EXISTS (
  SELECT 1 FROM short_drama_video_jobs job
  JOIN short_drama_video_shots shot
    ON shot.id=NEW.video_shot_id AND shot.shot_id=job.shot_id
   AND shot.project_id=job.project_id
  WHERE job.job_id=NEW.job_id
)
BEGIN SELECT RAISE(ABORT,'video version job does not belong to shot'); END;
"""


BLOCKER_MESSAGES = {
    "missing_locked_still": "请先锁定当前镜头的关键帧",
    "missing_locked_voice_shot": "请先完成并锁定当前镜头的配音字幕",
    "missing_cinematic_avatar": "当前镜头缺少可用的电影化身",
    "invalid_avatar_count": "电影化身数量必须为 1–3 个",
    "invalid_video_prompt": "视频提示词不能为空且不能超过 2000 字",
    "invalid_duration_or_ratio": "镜头只支持 5/10 秒及 9:16/16:9",
    "active_job": "当前镜头已有视频任务正在处理",
    "metadata_pending": "视频已生成，正在整理文件、封面和时长",
    "missing_current_version": "当前镜头还没有可确认的视频版本",
    "stale_current_version": "当前视频版本与已锁定输入不一致，请重新生成或选版",
    "duration_mismatch": "视频实际时长与镜头时长不一致",
    "refund_pending": "失败任务退款尚未完成",
    "charge_attempt_pending": "扣点尝试尚未收口",
    "ledger_inconsistent": "任务、扣点与视频版本账本不一致",
    "missing_locked_video_shot": "仍有镜头视频尚未锁定",
    "locked_video_shot": "受影响镜头已锁定，请先解除锁定后再修改角色绑定",
    "active_cast_job": "受影响镜头仍有视频任务正在处理，请等待任务完成后再修改角色绑定",
    "invalid_cast_avatar": "所选电影化身不可用，请刷新形象库后重新选择",
    "duplicate_cast_avatar": "同一项目内不同角色不能绑定同一个电影化身",
    "spoken_dialogue_requested": "画面提示词不能要求人物说话、唱歌或朗读",
    "generated_text_requested": "画面提示词不能要求模型生成字幕、标题或画面文字",
    "audio_generation_requested": "画面提示词不能要求模型生成音乐、音效或其他声音",
    "prompt_override_attempt": "画面提示词不能覆盖或绕过短剧视觉规则",
    "unknown_character_requested": "画面提示词引用了当前镜头之外的项目角色",
    "invalid_visual_spec": "镜头缺少可用于视频生成的锁定语义信息",
    "compiled_prompt_too_long": "结构化视频提示词超过供应商长度限制",
    "semantic_visual_rejected": "当前视频未通过画面语义检查，不能锁定",
}


def init_db(db_factory):
    conn = db_factory()
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        attempt_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(short_drama_video_charge_attempts)"
            )
        }
        if "recovery_token" not in attempt_columns:
            conn.execute(
                "ALTER TABLE short_drama_video_charge_attempts "
                "ADD COLUMN recovery_token TEXT"
            )
        if "recovery_started_at" not in attempt_columns:
            conn.execute(
                "ALTER TABLE short_drama_video_charge_attempts "
                "ADD COLUMN recovery_started_at INTEGER"
            )
        version_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(short_drama_video_versions)"
            )
        }
        for column, definition in {
            "prompt_template_version": "TEXT NOT NULL DEFAULT ''",
            "compiled_prompt_hash": "TEXT NOT NULL DEFAULT ''",
            "visual_spec_hash": "TEXT NOT NULL DEFAULT ''",
            "semantic_status": "TEXT NOT NULL DEFAULT 'legacy'",
            "semantic_report_json": "TEXT NOT NULL DEFAULT '{}'",
        }.items():
            if column not in version_columns:
                conn.execute(
                    "ALTER TABLE short_drama_video_versions ADD COLUMN %s %s"
                    % (column, definition)
                )
        conn.executescript(_TRIGGER_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _json(raw, fallback):
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value


def _stable_hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _normalized_character_reference(value):
    return unicodedata.normalize(
        "NFKC", str(value or "")
    ).strip().casefold()


def _prompt_character_references(prompt, project_characters):
    """Return explicitly referenced project character keys.

    Chinese names are matched only when the user uses an explicit marker such
    as ``@小雨`` or ``[角色:小雨]``.  Plain ASCII display names retain
    compatibility through case-insensitive whole-token matching.  Character
    keys are never treated as ordinary prose.
    """
    source = _normalized_character_reference(prompt)
    referenced = set()
    for character in project_characters:
        character_key = str(character["character_key"] or "").strip()
        if not character_key or character_key == "narrator":
            continue
        normalized_key = _normalized_character_reference(character_key)
        normalized_name = _normalized_character_reference(character["name"])
        tokens = {
            token for token in (normalized_key, normalized_name) if token
        }
        explicit = False
        for token in sorted(tokens, key=len, reverse=True):
            escaped = re.escape(token)
            if re.search(r"@\s*" + escaped + r"(?!\w)", source):
                explicit = True
                break
            if re.search(
                r"(?:\[|【)\s*(?:角色\s*[:：]\s*)?"
                + escaped + r"\s*(?:\]|】)",
                source,
            ):
                explicit = True
                break
        if explicit:
            referenced.add(character_key)
            continue
        if normalized_name and normalized_name.isascii():
            escaped_name = re.escape(normalized_name)
            if re.search(
                r"(?<![0-9a-z_])" + escaped_name + r"(?![0-9a-z_])",
                source,
            ):
                referenced.add(character_key)
    return referenced


def _table_exists(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def _blocker(code, shot_id=None, character_key=None, character_name=None):
    item = {"code": code, "message": BLOCKER_MESSAGES.get(code, code)}
    if shot_id:
        item["shot_id"] = shot_id
    if character_key:
        item["character_key"] = character_key
        item["character_name"] = character_name or character_key
        item["message"] = "%s：%s" % (item["character_name"], item["message"])
    return item


def _append_blocker(items, code, shot_id=None, character_key=None, character_name=None):
    if not any(item.get("code") == code and item.get("shot_id") == shot_id
               and item.get("character_key") == character_key
               for item in items):
        items.append(_blocker(
            code, shot_id, character_key=character_key,
            character_name=character_name,
        ))


def _project(conn, owner_username, project_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM short_drama_projects "
        "WHERE id=? AND username=? AND deleted=0",
        (project_id, owner_username),
    ).fetchone()
    if not row:
        raise LookupError("short drama project does not exist")
    return row


def ensure_video_workspace(conn, project_id, allowed_stages=None):
    conn.row_factory = sqlite3.Row
    project = conn.execute(
        "SELECT * FROM short_drama_projects WHERE id=? AND deleted=0", (project_id,)
    ).fetchone()
    if not project:
        raise LookupError("short drama project does not exist")
    if allowed_stages is not None and project["stage"] not in set(allowed_stages):
        raise ValueError("short drama project is not in the video stage")
    now = int(time.time())
    for shot in conn.execute(
        "SELECT id FROM short_drama_shots WHERE project_id=? ORDER BY sort_order,id",
        (project_id,),
    ):
        conn.execute(
            "INSERT OR IGNORE INTO short_drama_video_shots "
            "(id,project_id,shot_id,current_version,locked,video_revision,created_at,updated_at) "
            "VALUES (?,?,?,NULL,0,1,?,?)",
            (str(uuid.uuid4()), project_id, shot["id"], now, now),
        )


def normalize_quote_request(body):
    if not isinstance(body, dict) or set(body) != VIDEO_REQUEST_FIELDS:
        raise ValueError("video quote request fields are invalid")
    project_id = str(body.get("project_id") or "").strip()
    shot_id = str(body.get("shot_id") or "").strip()
    prompt = str(body.get("prompt") or "").strip()
    revision = body.get("revision")
    video_revision = body.get("video_revision")
    if (not project_id or not shot_id or type(revision) is not int or revision < 1
            or type(video_revision) is not int or video_revision < 1
            or type(body.get("enhance_prompt")) is not bool):
        raise ValueError("video quote request is invalid")
    if not prompt or len(prompt) > 2000:
        raise VideoBlocked("invalid_video_prompt", BLOCKER_MESSAGES["invalid_video_prompt"])
    return {
        "project_id": project_id, "revision": revision, "shot_id": shot_id,
        "video_revision": video_revision, "prompt": prompt,
        "enhance_prompt": body["enhance_prompt"],
    }


def normalize_generate_request(body):
    if not isinstance(body, dict) or set(body) != VIDEO_SUBMISSION_FIELDS:
        raise ValueError("video generation request fields are invalid")
    project_id = str(body.get("project_id") or "").strip()
    shot_id = str(body.get("shot_id") or "").strip()
    token = str(body.get("quote_token") or "").strip()
    if (not project_id or not shot_id or not token
            or type(body.get("revision")) is not int or body["revision"] < 1
            or type(body.get("video_revision")) is not int
            or body["video_revision"] < 1):
        raise ValueError("video generation request is invalid")
    return {
        "project_id": project_id, "revision": body["revision"],
        "shot_id": shot_id, "video_revision": body["video_revision"],
        "quote_token": token,
    }


def normalize_video_cast_request(body):
    if not isinstance(body, dict) or set(body) != VIDEO_CAST_FIELDS:
        raise ValueError("video cast request fields are invalid")
    project_id = str(body.get("project_id") or "").strip()
    revision = body.get("revision")
    bindings = body.get("bindings")
    if (not project_id or type(revision) is not int or revision < 1
            or not isinstance(bindings, list) or len(bindings) > 100):
        raise ValueError("video cast request is invalid")
    normalized = []
    character_keys = set()
    avatar_ids = set()
    for item in bindings:
        if not isinstance(item, dict) or set(item) != {"character_key", "avatar_id"}:
            raise ValueError("video cast binding fields are invalid")
        character_key = str(item.get("character_key") or "").strip()
        avatar_id = item.get("avatar_id")
        if (not character_key or len(character_key) > 120
                or type(avatar_id) is not int or avatar_id < 1):
            raise ValueError("video cast binding is invalid")
        if character_key in character_keys:
            raise ValueError("video cast contains duplicate characters")
        if avatar_id in avatar_ids:
            raise ValueError(BLOCKER_MESSAGES["duplicate_cast_avatar"])
        character_keys.add(character_key)
        avatar_ids.add(avatar_id)
        normalized.append({
            "character_key": character_key,
            "avatar_id": avatar_id,
        })
    return {
        "project_id": project_id,
        "revision": revision,
        "bindings": normalized,
    }


def _shot_character_keys(row):
    return {
        str(key) for key in _json(row["character_keys_json"], [])
        if str(key).strip() and str(key) != "narrator"
    }


def _project_cast_rows(conn, project_id):
    return {
        row["character_key"]: row for row in conn.execute(
            "SELECT project_id,character_key,avatar_id,created_at,updated_at "
            "FROM short_drama_video_cast WHERE project_id=?",
            (project_id,),
        )
    }


def _required_cast_characters(conn, project_id):
    referenced = {}
    for shot in conn.execute(
        "SELECT id,character_keys_json FROM short_drama_shots "
        "WHERE project_id=? ORDER BY sort_order,id",
        (project_id,),
    ):
        for character_key in _shot_character_keys(shot):
            referenced.setdefault(character_key, []).append(shot["id"])
    if not referenced:
        return [], {}
    placeholders = ",".join("?" for _ in referenced)
    rows = conn.execute(
        "SELECT character_key,name,source_type,avatar_id,reference_file,"
        "reference_url,reference_locked,sort_order "
        "FROM short_drama_characters WHERE project_id=? AND character_key IN ("
        + placeholders + ") ORDER BY sort_order,character_key",
        (project_id, *referenced.keys()),
    ).fetchall()
    return rows, referenced


def _usable_avatar(owner_username, avatar_id, avatar_lookup):
    if not callable(avatar_lookup):
        return None
    try:
        avatar = avatar_lookup(owner_username, avatar_id)
    except Exception:
        return None
    if (not isinstance(avatar, dict)
            or avatar.get("username") != owner_username
            or avatar.get("status") != "ready"
            or not str(avatar.get("provider_avatar_id") or "").strip()):
        return None
    return avatar


def _cast_character_items(conn, project, avatar_lookup=None):
    characters, referenced = _required_cast_characters(conn, project["id"])
    overrides = _project_cast_rows(conn, project["id"])
    items = []
    for character in characters:
        override = overrides.get(character["character_key"])
        source = "video_cast" if override else "character"
        avatar_id = (
            int(override["avatar_id"]) if override else
            character["avatar_id"] if character["source_type"] == "cinematic_avatar" else None
        )
        avatar = _usable_avatar(project["username"], avatar_id, avatar_lookup)
        valid = bool(avatar)
        items.append({
            "character_key": character["character_key"],
            "name": character["name"],
            "reference_file": character["reference_file"] or "",
            "reference_url": character["reference_url"] or "",
            "reference_locked": bool(character["reference_locked"]),
            "source_type": character["source_type"],
            "binding_source": source if avatar_id is not None else "missing",
            "avatar_id": int(avatar_id) if avatar_id is not None else None,
            "avatar_name": str((avatar or {}).get("name") or ""),
            "avatar_status": str((avatar or {}).get("status") or "unavailable"),
            "valid": valid,
            "shot_count": len(referenced.get(character["character_key"], [])),
            "blocker": None if valid else {
                "code": "missing_cinematic_avatar",
                "message": "%s：%s" % (
                    character["name"], BLOCKER_MESSAGES["missing_cinematic_avatar"]
                ),
            },
        })
    return items


def _shot_dependencies(conn, project, shot_id, prompt=None, avatar_lookup=None):
    conn.row_factory = sqlite3.Row
    shot = conn.execute(
        "SELECT * FROM short_drama_shots WHERE id=? AND project_id=?",
        (shot_id, project["id"]),
    ).fetchone()
    if not shot:
        raise LookupError("short drama shot does not exist")
    prompt = str(prompt if prompt is not None else shot["video_prompt"] or "").strip()
    blockers = []
    if not prompt or len(prompt) > 2000:
        _append_blocker(blockers, "invalid_video_prompt", shot_id)
    duration = int(shot["duration"])
    ratio = str(project["ratio"])
    if duration not in {5, 10} or ratio not in {"9:16", "16:9"}:
        _append_blocker(blockers, "invalid_duration_or_ratio", shot_id)

    still = conn.execute(
        "SELECT a.id AS asset_id,a.current_version,v.id AS version_id,"
        "v.file,v.url,v.ratio,v.status "
        "FROM short_drama_assets a "
        "JOIN short_drama_asset_versions v "
        "ON v.asset_id=a.id AND v.version=a.current_version "
        "WHERE a.project_id=? AND a.shot_id=? AND a.type='still' "
        "AND a.locked=1 AND v.status='done'",
        (project["id"], shot_id),
    ).fetchone()
    if not still or still["ratio"] != ratio or not str(still["file"] or "").strip():
        _append_blocker(blockers, "missing_locked_still", shot_id)

    voice_shot = conn.execute(
        "SELECT locked,timeline_revision FROM short_drama_voice_shots "
        "WHERE project_id=? AND shot_id=?",
        (project["id"], shot_id),
    ).fetchone()
    if not voice_shot or not bool(voice_shot["locked"]):
        _append_blocker(blockers, "missing_locked_voice_shot", shot_id)

    character_keys = _json(shot["character_keys_json"], [])
    placeholders = ",".join("?" for _ in character_keys)
    characters = []
    if character_keys:
        characters = conn.execute(
            "SELECT character_key,name,identity_text,personality,source_type,avatar_id,"
            "appearance_prompt,wardrobe_prompt,reference_version "
            "FROM short_drama_characters "
            "WHERE project_id=? AND character_key IN (" + placeholders + ")",
            (project["id"], *character_keys),
        ).fetchall()
    expected_character_keys = {
        str(key) for key in character_keys if str(key) != "narrator"
    }
    project_characters = conn.execute(
        "SELECT character_key,name FROM short_drama_characters WHERE project_id=?",
        (project["id"],),
    ).fetchall()
    for referenced_key in _prompt_character_references(
        prompt, project_characters
    ):
        if referenced_key not in expected_character_keys:
            _append_blocker(blockers, "unknown_character_requested", shot_id)
            break
    cast_rows = _project_cast_rows(conn, project["id"])
    avatar_ids = []
    found_character_keys = {
        str(character["character_key"]) for character in characters
        if str(character["character_key"]) != "narrator"
    }
    missing_avatar = found_character_keys != expected_character_keys
    if missing_avatar:
        _append_blocker(blockers, "missing_cinematic_avatar", shot_id)
    for character in characters:
        if character["character_key"] == "narrator":
            continue
        cast = cast_rows.get(character["character_key"])
        avatar_id = (
            cast["avatar_id"] if cast else
            character["avatar_id"] if character["source_type"] == "cinematic_avatar" else None
        )
        if not avatar_id:
            missing_avatar = True
            _append_blocker(
                blockers, "missing_cinematic_avatar", shot_id,
                character["character_key"], character["name"],
            )
            continue
        avatar_ids.append(str(avatar_id))
    avatar_ids = list(dict.fromkeys(avatar_ids))
    if not avatar_ids and not missing_avatar:
        _append_blocker(blockers, "missing_cinematic_avatar", shot_id)
    if len(avatar_ids) < 1 or len(avatar_ids) > 3:
        _append_blocker(blockers, "invalid_avatar_count", shot_id)
    if callable(avatar_lookup) and not missing_avatar and 1 <= len(avatar_ids) <= 3:
        for avatar_id in avatar_ids:
            try:
                avatar = avatar_lookup(project["username"], avatar_id)
            except Exception:
                avatar = None
            if (not isinstance(avatar, dict)
                    or avatar.get("username") != project["username"]
                    or avatar.get("status") != "ready"
                    or not avatar.get("provider_avatar_id")):
                _append_blocker(blockers, "missing_cinematic_avatar", shot_id)
                for character in characters:
                    cast = cast_rows.get(character["character_key"])
                    effective_id = (
                        cast["avatar_id"] if cast else
                        character["avatar_id"]
                        if character["source_type"] == "cinematic_avatar" else None
                    )
                    if str(effective_id or "") == avatar_id:
                        _append_blocker(
                            blockers, "missing_cinematic_avatar", shot_id,
                            character["character_key"], character["name"],
                        )
                        break
                break

    voice_items = []
    if voice_shot:
        for line in conn.execute(
            "SELECT line.id,line.start_ms,line.end_ms,line.subtitle_text,"
            "line.subtitle_visible,line.current_version,line.input_hash,"
            "version.audio_url,version.audio_file,version.duration_ms,"
            "version.input_hash AS version_input_hash "
            "FROM short_drama_voice_lines line "
            "LEFT JOIN short_drama_voice_versions version "
            "ON version.voice_line_id=line.id AND version.version=line.current_version "
            "WHERE line.project_id=? AND line.shot_id=? ORDER BY line.sort_order,line.id",
            (project["id"], shot_id),
        ):
            voice_items.append({
                "line_id": line["id"],
                "current_version": line["current_version"],
                "input_hash": line["input_hash"],
                "version_input_hash": line["version_input_hash"],
                "start_ms": line["start_ms"], "end_ms": line["end_ms"],
                "subtitle_text": line["subtitle_text"],
                "subtitle_visible": bool(line["subtitle_visible"]),
                "audio_url": line["audio_url"] or "",
                "audio_file": line["audio_file"] or "",
                "duration_ms": int(line["duration_ms"] or 0),
            })

    previous = conn.execute(
        "SELECT previous.shot_key FROM short_drama_shots current "
        "JOIN short_drama_shots previous ON previous.project_id=current.project_id "
        "AND previous.sort_order<current.sort_order "
        "JOIN short_drama_assets asset ON asset.project_id=previous.project_id "
        "AND asset.shot_id=previous.id AND asset.type='still' AND asset.locked=1 "
        "JOIN short_drama_asset_versions version ON version.asset_id=asset.id "
        "AND version.version=asset.current_version AND version.status='done' "
        "WHERE current.id=? AND current.project_id=? "
        "ORDER BY previous.sort_order DESC,previous.id DESC LIMIT 1",
        (shot_id, project["id"]),
    ).fetchone()
    visual_spec = {
        "project_id": project["id"],
        "shot_id": shot_id,
        "shot_key": shot["shot_key"],
        "ratio": ratio,
        "duration": duration,
        "visual_style": project["visual_style"],
        "target_platform": project["target_platform"],
        "scene": shot["scene_description"],
        "camera": shot["camera_description"],
        # The request prompt is the authoritative per-version action.  Do not
        # fall back to the planning-stage shot prompt here after a caller has
        # supplied an edited prompt, otherwise an old unsafe prompt can bypass
        # validation and still reach the provider.
        "action": prompt,
        "emotion": "",
        "continuity": (
            "Preserve identity, wardrobe, lighting, screen direction, and spatial "
            "continuity from the previous locked shot %s." % previous["shot_key"]
            if previous else ""
        ),
        "characters": [
            {
                "character_key": character["character_key"],
                "name": character["name"],
                "identity": character["identity_text"],
                "personality": character["personality"],
                "appearance": character["appearance_prompt"],
                "wardrobe": character["wardrobe_prompt"],
                "reference_version": int(character["reference_version"] or 0),
            }
            for character in characters
            if str(character["character_key"]) != "narrator"
        ],
    }
    compiled = None
    try:
        compiled = short_drama_prompt_compiler.compile_visual_only_prompt(
            visual_spec, prompt
        )
    except short_drama_prompt_compiler.PromptSemanticError as error:
        _append_blocker(blockers, error.code, shot_id)
    descriptor = {
        "project_id": project["id"], "shot_id": shot_id,
        "ratio": ratio, "duration": duration, "prompt": prompt,
        "prompt_template_version": (
            compiled["template_version"] if compiled else
            short_drama_prompt_compiler.PROMPT_TEMPLATE_VERSION
        ),
        "visual_spec": visual_spec,
        "visual_spec_hash": compiled["spec_hash"] if compiled else "",
        "compiled_prompt_hash": (
            compiled["compiled_prompt_hash"] if compiled else ""
        ),
        "avatar_ids": sorted(avatar_ids),
        "still": {
            "asset_id": still["asset_id"] if still else None,
            "version_id": still["version_id"] if still else None,
            "version": still["current_version"] if still else None,
            "file": still["file"] if still else None,
        },
        "voice": {
            "timeline_revision": (
                int(voice_shot["timeline_revision"]) if voice_shot else None
            ),
            "items": voice_items,
        },
    }
    return {
        "shot": shot, "prompt": prompt, "duration": duration, "ratio": ratio,
        "still": dict(still) if still else None, "avatar_ids": avatar_ids,
        "voice_items": voice_items, "descriptor": descriptor,
        "visual_spec": visual_spec, "compiled": compiled,
        "input_hash": _stable_hash(descriptor), "blockers": blockers,
    }


def _raise_first_blocker(dependencies, allowed=()):
    for blocker in dependencies["blockers"]:
        if blocker["code"] not in set(allowed):
            raise VideoBlocked(blocker["code"], blocker["message"])


def prepare_video_quote(db_factory, actor_username, owner_username, body, cost_of,
                        avatar_lookup=None):
    request = normalize_quote_request(body)
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, request["project_id"])
        if project["stage"] != VIDEO_WRITE_STAGE:
            raise ValueError("videos cannot be generated in the current stage")
        if int(project["revision"]) != request["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        ensure_video_workspace(conn, project["id"], {VIDEO_WRITE_STAGE})
        reconcile_video_jobs(conn, project["id"])
        slot = conn.execute(
            "SELECT * FROM short_drama_video_shots "
            "WHERE project_id=? AND shot_id=?",
            (project["id"], request["shot_id"]),
        ).fetchone()
        if not slot:
            raise LookupError("short drama shot does not exist")
        if int(slot["video_revision"]) != request["video_revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("video shot was updated; refresh and retry")
        if bool(slot["locked"]):
            raise ValueError("locked video shot cannot be regenerated")
        dependencies = _shot_dependencies(
            conn, project, request["shot_id"], request["prompt"], avatar_lookup
        )
        _raise_first_blocker(dependencies)
        if conn.execute(
            "SELECT 1 FROM short_drama_video_jobs "
            "WHERE project_id=? AND shot_id=? AND status IN "
            "('pending','running','uploading','submitted','downloading','metadata_pending') "
            "LIMIT 1",
            (project["id"], request["shot_id"]),
        ).fetchone():
            raise VideoBlocked("active_job", BLOCKER_MESSAGES["active_job"])
        compiled = dependencies["compiled"]
        if not compiled:
            raise VideoBlocked(
                "invalid_visual_spec", BLOCKER_MESSAGES["invalid_visual_spec"]
            )
        payload = {
            "cine_mode": "open",
            "avatar_ids": dependencies["avatar_ids"],
            "prompt": compiled["prompt"],
            "resolution": "1080p",
            "ratio": dependencies["ratio"],
            "duration": dependencies["duration"],
            "reference_image_files": [dependencies["still"]["file"]],
            # Provider-side prompt enhancement is not authoritative and may
            # remove the immutable visual-only constraints.
            "enhance_prompt": False,
            "generate_audio": False,
            "_short_drama_video": {
                "project_id": project["id"], "shot_id": request["shot_id"],
                "input_hash": dependencies["input_hash"],
                "visual_only": True,
                "user_prompt": request["prompt"],
                "prompt_template_version": compiled["template_version"],
                "compiled_prompt_hash": compiled["compiled_prompt_hash"],
                "visual_spec_hash": compiled["spec_hash"],
                "visual_spec": compiled["spec"],
            },
        }
        if not callable(cost_of):
            raise ValueError("cinematic video quote is unavailable")
        cost = int(cost_of("cinematic", payload))
        if cost < 0:
            raise ValueError("cinematic video quote is invalid")
        request_hash = _stable_hash({
            "project_id": project["id"], "revision": request["revision"],
            "shot_id": request["shot_id"],
            "video_revision": request["video_revision"],
            "input_hash": dependencies["input_hash"],
            "enhance_prompt": False,
            "compiled_prompt_hash": compiled["compiled_prompt_hash"],
            "prompt_template_version": compiled["template_version"],
        })
        now = int(time.time())
        token = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO short_drama_video_quotes "
            "(token,username,owner_username,project_id,shot_id,video_revision,"
            "request_hash,prompt,enhance_prompt,duration,ratio,cost,input_json,"
            "expires_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                token, actor_username, owner_username, project["id"],
                request["shot_id"], request["video_revision"], request_hash,
                request["prompt"], 0,
                dependencies["duration"], dependencies["ratio"], cost,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                now + QUOTE_TTL_SECONDS, now,
            ),
        )
        from .short_drama import _project_point_usage
        usage = _project_point_usage(conn, project["id"])
        budget = int(project["point_budget"] or 0)
        budget_left = None if budget == 0 else max(
            0, budget - usage["spent_points"] - usage["reserved_points"]
        )
        conn.commit()
        return {
            "project_id": project["id"], "shot_id": request["shot_id"],
            "video_revision": request["video_revision"],
            "total_cost": cost, "unit_rate": cost // dependencies["duration"],
            "duration": dependencies["duration"], "ratio": dependencies["ratio"],
            "quote_token": token, "expires_at": now + QUOTE_TTL_SECONDS,
            "request_hash": request_hash, "input_hash": dependencies["input_hash"],
            "input_summary": {
                "avatar_count": len(dependencies["avatar_ids"]),
                "still_version": dependencies["still"]["current_version"],
                "prompt": request["prompt"],
            },
            "point_budget": budget,
            "budget_left": budget_left,
            "can_submit": budget_left is None or cost <= budget_left,
            "spent_points": usage["spent_points"],
            "reserved_points": usage["reserved_points"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _attempt_dict(row):
    if not row:
        return None
    item = dict(row)
    item["video_payload"] = _json(item.pop("video_payload_json"), {})
    item["terminal_response"] = _json(item.pop("terminal_json"), None)
    return item


def get_video_attempt(db_factory, actor_username, idempotency_key):
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        return _attempt_dict(conn.execute(
            "SELECT * FROM short_drama_video_charge_attempts "
            "WHERE username=? AND endpoint='generate-video' AND idempotency_key=?",
            (actor_username, idempotency_key),
        ).fetchone())
    finally:
        conn.close()


def recover_video_submission(db_factory, actor_username, body, idempotency_key):
    request = normalize_generate_request(body)
    operation_hash = _stable_hash(request)
    attempt = get_video_attempt(db_factory, actor_username, idempotency_key)
    if not attempt:
        return None
    if attempt["request_hash"] != operation_hash:
        raise VideoQuoteConsumed("Idempotency-Key is already bound to another request")
    return attempt


def prepare_video_submission(db_factory, actor_username, owner_username, body,
                             idempotency_key, avatar_lookup=None):
    request = normalize_generate_request(body)
    operation_hash = _stable_hash(request)
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM short_drama_video_charge_attempts "
            "WHERE username=? AND endpoint='generate-video' AND idempotency_key=?",
            (actor_username, idempotency_key),
        ).fetchone()
        if existing:
            item = _attempt_dict(existing)
            if item["request_hash"] != operation_hash:
                raise VideoQuoteConsumed(
                    "Idempotency-Key is already bound to another request"
                )
            conn.commit()
            return item, True
        project = _project(conn, owner_username, request["project_id"])
        if project["stage"] != VIDEO_WRITE_STAGE:
            raise ValueError("videos cannot be generated in the current stage")
        if int(project["revision"]) != request["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        ensure_video_workspace(conn, project["id"], {VIDEO_WRITE_STAGE})
        reconcile_video_jobs(conn, project["id"])
        slot = conn.execute(
            "SELECT * FROM short_drama_video_shots "
            "WHERE project_id=? AND shot_id=?",
            (project["id"], request["shot_id"]),
        ).fetchone()
        if not slot:
            raise LookupError("short drama shot does not exist")
        if int(slot["video_revision"]) != request["video_revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("video shot was updated; refresh and retry")
        if bool(slot["locked"]):
            raise ValueError("locked video shot cannot be regenerated")
        quote = conn.execute(
            "SELECT * FROM short_drama_video_quotes "
            "WHERE token=? AND username=? AND owner_username=?",
            (request["quote_token"], actor_username, owner_username),
        ).fetchone()
        if not quote or quote["project_id"] != project["id"] or quote["shot_id"] != request["shot_id"]:
            raise ValueError("video quote does not exist")
        if int(quote["video_revision"]) != request["video_revision"]:
            raise ValueError("video quote no longer matches this shot")
        if int(quote["expires_at"]) < int(time.time()):
            raise ValueError("video quote has expired; request a new quote")
        if quote["consumed_idempotency_key"]:
            if quote["consumed_idempotency_key"] != idempotency_key:
                raise VideoQuoteConsumed("video quote has already been consumed")
            raise VideoChargeInProgress("video submission is being recovered")
        from .short_drama import _project_point_usage, PointBudgetExceeded
        usage = _project_point_usage(conn, project["id"])
        budget = int(project["point_budget"] or 0)
        cost = int(quote["cost"])
        if budget and usage["spent_points"] + usage["reserved_points"] + cost > budget:
            raise PointBudgetExceeded(
                "short drama point budget is insufficient"
            )
        if conn.execute(
            "SELECT 1 FROM short_drama_video_jobs WHERE project_id=? AND shot_id=? "
            "AND status IN ('pending','running','uploading','submitted','downloading','metadata_pending') "
            "LIMIT 1",
            (project["id"], request["shot_id"]),
        ).fetchone():
            raise VideoBlocked("active_job", BLOCKER_MESSAGES["active_job"])
        payload = _json(quote["input_json"], {})
        metadata = payload.get("_short_drama_video") or {}
        dependencies = _shot_dependencies(
            conn, project, request["shot_id"], quote["prompt"], avatar_lookup
        )
        _raise_first_blocker(dependencies)
        if dependencies["input_hash"] != metadata.get("input_hash"):
            raise ValueError("video inputs changed; request a new quote")
        now = int(time.time())
        if str(dependencies["shot"]["video_prompt"] or "").strip() != quote["prompt"]:
            conn.execute(
                "UPDATE short_drama_shots SET video_prompt=? "
                "WHERE id=? AND project_id=?",
                (quote["prompt"], request["shot_id"], project["id"]),
            )
            updated = conn.execute(
                "UPDATE short_drama_projects SET revision=revision+1,updated_at=? "
                "WHERE id=? AND username=? AND revision=? AND stage='video_review'",
                (now, project["id"], owner_username, request["revision"]),
            )
            if updated.rowcount != 1:
                from .short_drama import RevisionConflict
                raise RevisionConflict("project was updated; refresh and retry")
        charge_key = "short-drama-video:%s:%s" % (actor_username, idempotency_key)
        refund_key = charge_key + ":refund"
        conn.execute(
            "INSERT INTO short_drama_video_charge_attempts "
            "(charge_key,refund_key,username,owner_username,endpoint,idempotency_key,"
            "request_hash,project_id,shot_id,quote_token,cost,video_payload_json,state,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                charge_key, refund_key, actor_username, owner_username,
                "generate-video", idempotency_key, operation_hash, project["id"],
                request["shot_id"], quote["token"], cost,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                "accepted", now, now,
            ),
        )
        conn.execute(
            "UPDATE short_drama_video_quotes SET consumed_idempotency_key=? "
            "WHERE token=? AND consumed_idempotency_key IS NULL",
            (idempotency_key, quote["token"]),
        )
        attempt = conn.execute(
            "SELECT * FROM short_drama_video_charge_attempts "
            "WHERE username=? AND endpoint='generate-video' AND idempotency_key=?",
            (actor_username, idempotency_key),
        ).fetchone()
        conn.commit()
        return _attempt_dict(attempt), False
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_video_attempt_charge(db_factory, username, idempotency_key):
    """Lease one accepted attempt before the remote points deduction starts."""
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        token = "submission:" + str(uuid.uuid4())
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_video_charge_attempts "
            "SET recovery_token=?,recovery_started_at=?,updated_at=? "
            "WHERE username=? AND endpoint='generate-video' AND idempotency_key=? "
            "AND state='accepted' AND job_id IS NULL AND recovery_token IS NULL",
            (token, now, now, username, idempotency_key),
        )
        conn.commit()
        return _attempt_dict(conn.execute(
            "SELECT * FROM short_drama_video_charge_attempts "
            "WHERE username=? AND endpoint='generate-video' AND idempotency_key=?",
            (username, idempotency_key),
        ).fetchone())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_video_attempt_charged(db_factory, username, idempotency_key, points_left,
                               claim_token):
    return _transition_attempt(
        db_factory, username, idempotency_key, {"accepted"}, "charged",
        points_left=points_left, claim_token=claim_token, clear_claim=True,
    )


def mark_video_attempt_refund_pending(db_factory, username, idempotency_key, terminal):
    return _transition_attempt(
        db_factory, username, idempotency_key, {"charged", "linked"},
        "refund_pending", terminal=terminal,
    )


def mark_video_attempt_refunded(db_factory, username, idempotency_key):
    return _transition_attempt(
        db_factory, username, idempotency_key, {"refund_pending"}, "refunded"
    )


def mark_video_attempt_failed(db_factory, username, idempotency_key, terminal,
                              claim_token=None):
    return _transition_attempt(
        db_factory, username, idempotency_key, {"accepted"}, "failed",
        terminal=terminal, claim_token=claim_token,
        require_unclaimed=claim_token is None, clear_claim=claim_token is not None,
    )


def _claim_stale_video_attempts(db_factory, limit):
    now = int(time.time())
    cutoff = now - VIDEO_CHARGE_RECOVERY_SECONDS
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        candidates = conn.execute(
            "SELECT charge_key FROM short_drama_video_charge_attempts "
            "WHERE state='accepted' AND job_id IS NULL AND updated_at<=? "
            "AND (recovery_token IS NULL OR recovery_started_at<=?) "
            "ORDER BY updated_at,charge_key LIMIT ?",
            (cutoff, cutoff, max(1, int(limit or 1))),
        ).fetchall()
        claimed = []
        for candidate in candidates:
            token = str(uuid.uuid4())
            updated = conn.execute(
                "UPDATE short_drama_video_charge_attempts "
                "SET recovery_token=?,recovery_started_at=? "
                "WHERE charge_key=? AND state='accepted' AND job_id IS NULL "
                "AND updated_at<=? "
                "AND (recovery_token IS NULL OR recovery_started_at<=?)",
                (token, now, candidate["charge_key"], cutoff, cutoff),
            )
            if updated.rowcount == 1:
                row = conn.execute(
                    "SELECT * FROM short_drama_video_charge_attempts "
                    "WHERE charge_key=? AND recovery_token=?",
                    (candidate["charge_key"], token),
                ).fetchone()
                if row:
                    claimed.append((dict(row), token))
        conn.commit()
        return claimed
    finally:
        conn.close()


def _finish_video_charge_recovery(db_factory, row, token, ledger):
    now = int(time.time())
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        if ledger is None:
            previous = _json(row.get("terminal_json"), {})
            if previous.get("code") != VIDEO_CHARGE_LEDGER_UNCONFIRMED:
                terminal = {
                    "detail": "video charge ledger is not visible yet; "
                              "a second authoritative query is required",
                    "code": VIDEO_CHARGE_LEDGER_UNCONFIRMED,
                    "operation_terminal": False,
                    "observed_at": now,
                }
                updated = conn.execute(
                    "UPDATE short_drama_video_charge_attempts "
                    "SET terminal_json=?,recovery_token=NULL,"
                    "recovery_started_at=NULL,updated_at=? "
                    "WHERE charge_key=? AND state='accepted' "
                    "AND recovery_token=?",
                    (
                        json.dumps(terminal, ensure_ascii=False),
                        now, row["charge_key"], token,
                    ),
                )
                conn.commit()
                return "observing" if updated.rowcount == 1 else "lost"
            terminal = {
                "detail": "video submission expired before charging",
                "code": "video_operation_terminal",
                "operation_terminal": True,
            }
            target = "failed"
            points_left = None
        else:
            try:
                ledger_matches = (
                    str(ledger.get("username") or "") == str(row["username"])
                    and int(ledger.get("delta") or 0) == -int(row["cost"])
                )
            except (AttributeError, TypeError, ValueError):
                ledger_matches = False
            if ledger_matches:
                terminal = {
                    "detail": "video charge was committed but no job was linked; "
                              "refund is being recovered",
                    "code": "video_refund_pending",
                    "operation_terminal": True,
                }
                target = "refund_pending"
                points_left = int(ledger.get("after_points") or 0)
            else:
                terminal = {
                    "detail": "video charge ledger does not match the submission",
                    "code": "video_ledger_inconsistent",
                    "operation_terminal": False,
                }
                conn.execute(
                    "UPDATE short_drama_video_charge_attempts "
                    "SET terminal_json=?,recovery_token=NULL,"
                    "recovery_started_at=NULL,updated_at=? "
                    "WHERE charge_key=? AND state='accepted' "
                    "AND recovery_token=?",
                    (
                        json.dumps(terminal, ensure_ascii=False),
                        now, row["charge_key"], token,
                    ),
                )
                conn.commit()
                return "inconsistent"
        fields = [
            "state=?", "terminal_json=?", "recovery_token=NULL",
            "recovery_started_at=NULL", "updated_at=?",
        ]
        params = [target, json.dumps(terminal, ensure_ascii=False), now]
        if points_left is not None:
            fields.append("points_left=?")
            params.append(points_left)
        params.extend([row["charge_key"], token])
        updated = conn.execute(
            "UPDATE short_drama_video_charge_attempts SET " + ",".join(fields) +
            " WHERE charge_key=? AND state='accepted' AND recovery_token=?",
            params,
        )
        conn.commit()
        return target if updated.rowcount == 1 else "lost"
    finally:
        conn.close()


def _release_video_charge_recovery(db_factory, charge_key, token):
    conn = db_factory()
    try:
        conn.execute(
            "UPDATE short_drama_video_charge_attempts "
            "SET recovery_token=NULL,recovery_started_at=NULL,updated_at=? "
            "WHERE charge_key=? AND state='accepted' AND recovery_token=?",
            (int(time.time()), charge_key, token),
        )
        conn.commit()
    finally:
        conn.close()


def retry_video_attempt_refunds(db_factory, points_domain, limit=64):
    for row, token in _claim_stale_video_attempts(db_factory, limit):
        try:
            ledger = points_domain.get_points_transaction(row["charge_key"])
        except Exception:
            _release_video_charge_recovery(
                db_factory, row["charge_key"], token
            )
            continue
        _finish_video_charge_recovery(db_factory, row, token, ledger)

    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        cutoff = int(time.time()) - VIDEO_CHARGE_RECOVERY_SECONDS
        conn.execute(
            "UPDATE short_drama_video_charge_attempts SET state='refund_pending',"
            "terminal_json=?,updated_at=? WHERE state='charged' AND job_id IS NULL "
            "AND updated_at<=?",
            (
                json.dumps({
                    "detail": "video job was not linked; refund is being recovered",
                    "code": "video_refund_pending",
                    "operation_terminal": True,
                }),
                int(time.time()), cutoff,
            ),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM short_drama_video_charge_attempts "
            "WHERE state='refund_pending' AND job_id IS NULL "
            "ORDER BY updated_at LIMIT ?",
            (max(1, int(limit or 1)),),
        ).fetchall()
    finally:
        conn.close()
    refunded = 0
    for row in rows:
        try:
            points_domain.refund_points(
                row["username"], int(row["cost"]),
                "short-drama video:recovery",
                transaction_key=row["refund_key"],
            )
            mark_video_attempt_refunded(
                db_factory, row["username"], row["idempotency_key"]
            )
            refunded += 1
        except Exception:
            continue
    return refunded


def _transition_attempt(db_factory, username, idempotency_key, from_states, target,
                        points_left=None, terminal=None, require_unclaimed=False,
                        claim_token=None, clear_claim=False):
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        now = int(time.time())
        placeholders = ",".join("?" for _ in from_states)
        fields = ["state=?", "updated_at=?"]
        params = [target, now]
        if points_left is not None:
            fields.append("points_left=?")
            params.append(int(points_left))
        if terminal is not None:
            fields.append("terminal_json=?")
            params.append(json.dumps(terminal, ensure_ascii=False))
        if clear_claim:
            fields.extend(["recovery_token=NULL", "recovery_started_at=NULL"])
        params.extend([username, idempotency_key, *sorted(from_states)])
        unclaimed = " AND recovery_token IS NULL" if require_unclaimed else ""
        claimed = " AND recovery_token=?" if claim_token is not None else ""
        if claim_token is not None:
            params.append(claim_token)
        conn.execute(
            "UPDATE short_drama_video_charge_attempts SET " + ",".join(fields) +
            " WHERE username=? AND endpoint='generate-video' AND idempotency_key=? "
            "AND state IN (" + placeholders + ")" + unclaimed + claimed,
            params,
        )
        conn.commit()
        return _attempt_dict(conn.execute(
            "SELECT * FROM short_drama_video_charge_attempts "
            "WHERE username=? AND endpoint='generate-video' AND idempotency_key=?",
            (username, idempotency_key),
        ).fetchone())
    finally:
        conn.close()


def bind_video_job(db_factory, username, idempotency_key, connection, job_id):
    connection.row_factory = sqlite3.Row
    attempt = connection.execute(
        "SELECT * FROM short_drama_video_charge_attempts "
        "WHERE username=? AND endpoint='generate-video' AND idempotency_key=?",
        (username, idempotency_key),
    ).fetchone()
    if not attempt or attempt["state"] != "charged" or attempt["job_id"] is not None:
        raise VideoChargeInProgress("video charge attempt cannot be rebound")
    now = int(time.time())
    connection.execute(
        "INSERT INTO short_drama_video_jobs "
        "(id,username,owner_username,project_id,shot_id,job_id,idempotency_key,"
        "request_hash,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()), username, attempt["owner_username"],
            attempt["project_id"], attempt["shot_id"], int(job_id),
            idempotency_key, attempt["request_hash"], "pending", now, now,
        ),
    )
    connection.execute(
        "UPDATE short_drama_video_charge_attempts "
        "SET state='linked',job_id=?,updated_at=? WHERE charge_key=?",
        (int(job_id), now, attempt["charge_key"]),
    )
    connection.execute(
        "UPDATE short_drama_video_quotes SET consumed_job_id=? "
        "WHERE token=? AND consumed_job_id IS NULL",
        (int(job_id), attempt["quote_token"]),
    )


def _duration_ms(result):
    raw = result.get("duration_ms")
    if raw is None:
        raw = result.get("duration")
        try:
            return max(0, int(round(float(raw) * 1000)))
        except (TypeError, ValueError):
            return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def reconcile_video_jobs(conn, project_id):
    if not _table_exists(conn, "jobs"):
        return
    conn.row_factory = sqlite3.Row
    job_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    refunded_expr = "COALESCE(job.refunded,0)" if "refunded" in job_columns else "0"
    error_expr = "COALESCE(job.error,'')" if "error" in job_columns else "''"
    rows = conn.execute(
        "SELECT link.*,job.status AS generic_status,job.cost,job.payload,job.result,"
        + refunded_expr + " AS generic_refunded," + error_expr + " AS generic_error "
        "FROM short_drama_video_jobs link JOIN jobs job ON job.id=link.job_id "
        "AND job.username=link.username AND job.kind='cinematic' "
        "WHERE link.project_id=? ORDER BY link.created_at,link.id",
        (project_id,),
    ).fetchall()
    now = int(time.time())
    for row in rows:
        generic = str(row["generic_status"] or "")
        status = (
            "failed" if generic in {"error", "failed", "cancelled"}
            else generic if generic in {"pending", "running", "done"} else "failed"
        )
        provider_video_id = row["provider_video_id"]
        if _table_exists(conn, "video_assets"):
            asset = conn.execute(
                "SELECT provider_video_id,phase FROM video_assets WHERE job_id=?",
                (row["job_id"],),
            ).fetchone()
            if asset:
                provider_video_id = str(asset[0] or provider_video_id or "")
                phase = str(asset[1] or "")
                if status in {"pending", "running"} and phase:
                    status = (
                        "uploading" if "upload" in phase
                        else "submitted" if provider_video_id and "poll" in phase
                        else "downloading" if "download" in phase
                        else status
                    )
        if generic == "done":
            result = _json(row["result"], {})
            payload = _json(row["payload"], {})
            metadata = payload.get("_short_drama_video") or {}
            video_file = str(result.get("video_file") or "")
            video_url = str(result.get("video_url") or "")
            duration_ms = _duration_ms(result)
            if not metadata.get("input_hash") or not (video_file or video_url) or duration_ms <= 0:
                status = "metadata_pending"
            else:
                slot = conn.execute(
                    "SELECT * FROM short_drama_video_shots "
                    "WHERE project_id=? AND shot_id=?",
                    (project_id, row["shot_id"]),
                ).fetchone()
                if not slot:
                    status = "failed"
                else:
                    existing = conn.execute(
                        "SELECT version FROM short_drama_video_versions WHERE job_id=?",
                        (row["job_id"],),
                    ).fetchone()
                    if existing:
                        version_number = int(existing[0])
                    else:
                        version_number = int(conn.execute(
                            "SELECT COALESCE(MAX(version),0)+1 "
                            "FROM short_drama_video_versions WHERE video_shot_id=?",
                            (slot["id"],),
                        ).fetchone()[0])
                        conn.execute(
                            "INSERT INTO short_drama_video_versions "
                            "(id,video_shot_id,version,job_id,url,file,cover_url,cover_file,"
                            "duration_ms,ratio,prompt,enhance_prompt,input_hash,cost,provider,"
                            "provider_video_id,prompt_template_version,"
                            "compiled_prompt_hash,visual_spec_hash,semantic_status,"
                            "semantic_report_json,status,created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                str(uuid.uuid4()), slot["id"], version_number,
                                int(row["job_id"]), video_url, video_file,
                                str(result.get("image_url") or result.get("thumbnail_url") or ""),
                                str(result.get("image_file") or ""), duration_ms,
                                str(result.get("ratio") or payload.get("ratio") or ""),
                                str(
                                    metadata.get("user_prompt")
                                    or result.get("prompt")
                                    or payload.get("prompt")
                                    or ""
                                ),
                                int(bool(payload.get("enhance_prompt"))),
                                str(metadata["input_hash"]), int(row["cost"] or 0),
                                str(result.get("provider") or ""),
                                str(result.get("video_id") or provider_video_id or ""),
                                str(
                                    result.get("prompt_template_version")
                                    or metadata.get("prompt_template_version") or ""
                                ),
                                str(
                                    result.get("compiled_prompt_hash")
                                    or metadata.get("compiled_prompt_hash") or ""
                                ),
                                str(
                                    result.get("visual_spec_hash")
                                    or metadata.get("visual_spec_hash") or ""
                                ),
                                str(
                                    (result.get("visual_gate_report") or {}).get(
                                        "decision"
                                    ) or "unavailable"
                                ),
                                json.dumps(
                                    result.get("visual_gate_report") or {},
                                    ensure_ascii=False, separators=(",", ":"),
                                ),
                                "done", now,
                            ),
                        )
                    conn.execute(
                        "UPDATE short_drama_video_shots "
                        "SET current_version=COALESCE(current_version,?),updated_at=? "
                        "WHERE id=?",
                        (version_number, now, slot["id"]),
                    )
                    conn.execute(
                        "UPDATE short_drama_video_charge_attempts "
                        "SET state='done',updated_at=? WHERE job_id=? AND state='linked'",
                        (now, row["job_id"]),
                    )
                    status = "done"
        if generic in {"error", "failed", "cancelled"}:
            refunded = int(row["generic_refunded"] or 0)
            attempt_state = "refunded" if refunded == 1 else "refund_pending"
            conn.execute(
                "UPDATE short_drama_video_charge_attempts SET state=?,updated_at=? "
                "WHERE job_id=? AND state IN ('charged','linked','refund_pending')",
                (attempt_state, now, row["job_id"]),
            )
        conn.execute(
            "UPDATE short_drama_video_jobs SET provider_video_id=?,status=?,error=?,"
            "refunded=?,updated_at=? WHERE id=?",
            (
                provider_video_id or "", status,
                str(row["generic_error"] or "")[:300],
                int(row["generic_refunded"] or 0), now, row["id"],
            ),
        )


def _version_item(row):
    item = dict(row)
    item["enhance_prompt"] = bool(item["enhance_prompt"])
    item["semantic_report"] = _json(
        item.pop("semantic_report_json", "{}"), {}
    )
    return item


def _semantic_blocker_code(version):
    """Derive authorization from the authoritative report policy.

    ``semantic_status`` is a display/index field and ``blocking`` is persisted
    diagnostic output.  Neither may override shadow mode or an unavailable
    report when deciding whether a version can be locked.
    """
    report = version.get("semantic_report") or {}
    if not isinstance(report, dict):
        return None
    mode = str(report.get("mode") or "").strip().lower()
    decision = str(report.get("decision") or "").strip().lower()
    if mode == "enforce" and decision == "rejected_visual":
        return "semantic_visual_rejected"
    return None


def save_video_cast(db_factory, owner_username, body, avatar_lookup=None):
    request = normalize_video_cast_request(body)
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, request["project_id"])
        if project["stage"] != VIDEO_WRITE_STAGE:
            raise VideoCastConflict(
                "video_stage_readonly",
                "仅可在电影化身视频阶段修改角色绑定",
            )
        if int(project["revision"]) != request["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        ensure_video_workspace(conn, project["id"], {VIDEO_WRITE_STAGE})
        reconcile_video_jobs(conn, project["id"])
        characters, _ = _required_cast_characters(conn, project["id"])
        required_keys = {row["character_key"] for row in characters}
        incoming = {
            item["character_key"]: item["avatar_id"]
            for item in request["bindings"]
        }
        unknown = set(incoming) - required_keys
        if unknown:
            raise ValueError("video cast contains a character outside this project")
        validated = {}
        for character_key, avatar_id in incoming.items():
            avatar = _usable_avatar(owner_username, avatar_id, avatar_lookup)
            if not avatar:
                raise VideoCastConflict(
                    "invalid_cast_avatar",
                    "%s：%s" % (
                        character_key, BLOCKER_MESSAGES["invalid_cast_avatar"]
                    ),
                )
            validated[character_key] = int(avatar["id"])
        native_avatar_ids = {
            character["character_key"]: int(character["avatar_id"])
            for character in characters
            if (character["source_type"] == "cinematic_avatar"
                and character["avatar_id"])
        }
        validated = {
            character_key: avatar_id
            for character_key, avatar_id in validated.items()
            if native_avatar_ids.get(character_key) != avatar_id
        }
        if len(set(validated.values())) != len(validated):
            raise ValueError(BLOCKER_MESSAGES["duplicate_cast_avatar"])
        effective_avatar_ids = []
        for character in characters:
            avatar_id = validated.get(character["character_key"])
            if (avatar_id is None
                    and character["source_type"] == "cinematic_avatar"
                    and character["avatar_id"]):
                avatar_id = int(character["avatar_id"])
            if avatar_id is not None:
                effective_avatar_ids.append(avatar_id)
        if len(set(effective_avatar_ids)) != len(effective_avatar_ids):
            raise ValueError(BLOCKER_MESSAGES["duplicate_cast_avatar"])

        existing = {
            key: int(row["avatar_id"])
            for key, row in _project_cast_rows(conn, project["id"]).items()
        }
        changed_keys = {
            key for key in set(existing) | set(validated)
            if existing.get(key) != validated.get(key)
        }
        if not changed_keys:
            snapshot = build_video_snapshot(conn, project, avatar_lookup)
            conn.commit()
            return snapshot

        affected_shots = [
            shot for shot in conn.execute(
                "SELECT id,character_keys_json FROM short_drama_shots "
                "WHERE project_id=?",
                (project["id"],),
            )
            if _shot_character_keys(shot) & changed_keys
        ]
        affected_ids = [shot["id"] for shot in affected_shots]
        if affected_ids:
            placeholders = ",".join("?" for _ in affected_ids)
            if conn.execute(
                "SELECT 1 FROM short_drama_video_shots "
                "WHERE project_id=? AND shot_id IN (" + placeholders + ") "
                "AND locked=1 LIMIT 1",
                (project["id"], *affected_ids),
            ).fetchone():
                raise VideoCastConflict(
                    "locked_video_shot", BLOCKER_MESSAGES["locked_video_shot"]
                )
            if conn.execute(
                "SELECT 1 FROM short_drama_video_jobs "
                "WHERE project_id=? AND shot_id IN (" + placeholders + ") "
                "AND status IN ('pending','running','uploading','submitted',"
                "'downloading','metadata_pending') LIMIT 1",
                (project["id"], *affected_ids),
            ).fetchone():
                raise VideoCastConflict(
                    "active_job", BLOCKER_MESSAGES["active_cast_job"]
                )

        now = int(time.time())
        for character_key in set(existing) - set(validated):
            conn.execute(
                "DELETE FROM short_drama_video_cast "
                "WHERE project_id=? AND character_key=?",
                (project["id"], character_key),
            )
        for character_key, avatar_id in validated.items():
            conn.execute(
                "INSERT INTO short_drama_video_cast "
                "(project_id,character_key,avatar_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(project_id,character_key) DO UPDATE SET "
                "avatar_id=excluded.avatar_id,updated_at=excluded.updated_at",
                (project["id"], character_key, avatar_id, now, now),
            )
        if affected_ids:
            placeholders = ",".join("?" for _ in affected_ids)
            conn.execute(
                "UPDATE short_drama_video_shots SET current_version=NULL,"
                "video_revision=video_revision+1,updated_at=? "
                "WHERE project_id=? AND shot_id IN (" + placeholders + ")",
                (now, project["id"], *affected_ids),
            )
        updated = conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1,updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND stage='video_review'",
            (now, project["id"], owner_username, request["revision"]),
        )
        if updated.rowcount != 1:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        project = _project(conn, owner_username, project["id"])
        snapshot = build_video_snapshot(conn, project, avatar_lookup)
        conn.commit()
        return snapshot
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def build_video_snapshot(conn, project, avatar_lookup=None):
    conn.row_factory = sqlite3.Row
    from .short_drama import _project_point_usage
    usage = _project_point_usage(conn, project["id"])
    slots = {
        row["shot_id"]: row for row in conn.execute(
            "SELECT * FROM short_drama_video_shots WHERE project_id=?",
            (project["id"],),
        )
    }
    versions = {}
    for row in conn.execute(
        "SELECT version.* FROM short_drama_video_versions version "
        "JOIN short_drama_video_shots shot ON shot.id=version.video_shot_id "
        "WHERE shot.project_id=? ORDER BY shot.shot_id,version.version DESC",
        (project["id"],),
    ):
        versions.setdefault(row["video_shot_id"], []).append(_version_item(row))
    jobs = {}
    for row in conn.execute(
        "SELECT * FROM short_drama_video_jobs WHERE project_id=? "
        "ORDER BY created_at DESC,job_id DESC",
        (project["id"],),
    ):
        jobs.setdefault(row["shot_id"], dict(row))

    shots = []
    for shot in conn.execute(
        "SELECT * FROM short_drama_shots WHERE project_id=? ORDER BY sort_order,id",
        (project["id"],),
    ):
        slot = slots[shot["id"]]
        shot_versions = versions.get(slot["id"], [])
        current = next(
            (item for item in shot_versions
             if int(item["version"]) == int(slot["current_version"] or 0)),
            None,
        )
        # A generated version owns the prompt that produced it.  Rebuild its
        # dependency hash from that same prompt instead of the mutable
        # planning-stage shot.video_prompt.
        effective_prompt = (
            current["prompt"] if current is not None else shot["video_prompt"]
        )
        dependencies = _shot_dependencies(
            conn, project, shot["id"], prompt=effective_prompt,
            avatar_lookup=avatar_lookup,
        )
        job = jobs.get(shot["id"])
        blockers = list(dependencies["blockers"])
        if job and job["status"] in ACTIVE_JOB_STATES:
            _append_blocker(
                blockers,
                "metadata_pending" if job["status"] == "metadata_pending" else "active_job",
                shot["id"],
            )
        attempt_states = {
            row[0] for row in conn.execute(
                "SELECT state FROM short_drama_video_charge_attempts "
                "WHERE project_id=? AND shot_id=?",
                (project["id"], shot["id"]),
            )
        }
        if "refund_pending" in attempt_states:
            _append_blocker(blockers, "refund_pending", shot["id"])
        if attempt_states & {"accepted", "charged"}:
            _append_blocker(blockers, "charge_attempt_pending", shot["id"])
        if not current:
            _append_blocker(blockers, "missing_current_version", shot["id"])
        else:
            if current["input_hash"] != dependencies["input_hash"]:
                _append_blocker(blockers, "stale_current_version", shot["id"])
            semantic_blocker = _semantic_blocker_code(current)
            if semantic_blocker:
                _append_blocker(blockers, semantic_blocker, shot["id"])
            expected_ms = int(shot["duration"]) * 1000
            if abs(int(current["duration_ms"]) - expected_ms) > VIDEO_DURATION_TOLERANCE_MS:
                _append_blocker(blockers, "duration_mismatch", shot["id"])
            if current["ratio"] != project["ratio"] or not (current["file"] or current["url"]):
                _append_blocker(blockers, "ledger_inconsistent", shot["id"])
        if bool(slot["locked"]) and blockers:
            conn.execute(
                "UPDATE short_drama_video_shots SET locked=0,video_revision=video_revision+1,"
                "updated_at=? WHERE id=? AND locked=1",
                (int(time.time()), slot["id"]),
            )
            slot = conn.execute(
                "SELECT * FROM short_drama_video_shots WHERE id=?", (slot["id"],)
            ).fetchone()
        status = "blocked"
        if bool(slot["locked"]):
            status = "locked"
        elif job and job["status"] in ACTIVE_JOB_STATES:
            status = job["status"]
        elif job and job["status"] == "failed":
            status = "failed"
        elif current:
            status = "done"
        elif not blockers or all(item["code"] == "missing_current_version" for item in blockers):
            status = "empty"
        shots.append({
            "id": shot["id"], "shot_key": shot["shot_key"],
            "sort_order": int(shot["sort_order"]),
            "duration": int(shot["duration"]), "video_prompt": effective_prompt,
            "character_keys": _json(shot["character_keys_json"], []),
            "video_shot_id": slot["id"],
            "video_revision": int(slot["video_revision"]),
            "current_version": slot["current_version"],
            "locked": bool(slot["locked"]), "status": status,
            "lockable": not blockers, "lock_blockers": blockers,
            "versions": shot_versions, "job": job,
            "still": dependencies["still"],
            "avatar_ids": dependencies["avatar_ids"],
            "voice_tracks": dependencies["voice_items"],
            "input_hash": dependencies["input_hash"],
        })
    handoff = []
    for shot in shots:
        if not shot["locked"]:
            _append_blocker(handoff, "missing_locked_video_shot", shot["id"])
        for blocker in shot["lock_blockers"]:
            _append_blocker(handoff, blocker["code"], shot["id"])
    return {
        "project_id": project["id"], "revision": int(project["revision"]),
        "stage": project["stage"], "ratio": project["ratio"],
        "target_duration": int(project["target_duration"]),
        "point_budget": int(project["point_budget"]),
        "spent_points": usage["spent_points"],
        "reserved_points": usage["reserved_points"],
        "cast_characters": _cast_character_items(conn, project, avatar_lookup),
        "shots": shots,
        "unlocked_shot_count": sum(not shot["locked"] for shot in shots),
        "handoff_blocked": bool(handoff), "handoff_blockers": handoff,
    }


def get_video_workspace(db_factory, owner_username, project_id, avatar_lookup=None):
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, project_id)
        if project["stage"] not in VIDEO_READ_STAGES:
            raise ValueError("short drama project has not entered video review")
        ensure_video_workspace(conn, project_id)
        reconcile_video_jobs(conn, project_id)
        project = _project(conn, owner_username, project_id)
        snapshot = build_video_snapshot(conn, project, avatar_lookup)
        conn.commit()
        return snapshot
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _write_request(body, expected, operation):
    if not isinstance(body, dict) or set(body) != expected:
        raise ValueError("%s request fields are invalid" % operation)
    project_id = str(body.get("project_id") or "").strip()
    shot_id = str(body.get("shot_id") or "").strip()
    if (not project_id or not shot_id or type(body.get("revision")) is not int
            or type(body.get("video_revision")) is not int):
        raise ValueError("%s request is invalid" % operation)
    return dict(body, project_id=project_id, shot_id=shot_id)


def select_video_version(db_factory, owner_username, body, avatar_lookup=None):
    request = _write_request(
        body, {"project_id", "revision", "shot_id", "video_revision", "version"},
        "video version selection",
    )
    if type(request.get("version")) is not int or request["version"] < 1:
        raise ValueError("video version is invalid")
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, request["project_id"])
        if project["stage"] != VIDEO_WRITE_STAGE:
            raise ValueError("video versions are read-only in the current stage")
        if int(project["revision"]) != request["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        slot = conn.execute(
            "SELECT * FROM short_drama_video_shots WHERE project_id=? AND shot_id=?",
            (project["id"], request["shot_id"]),
        ).fetchone()
        if (not slot or int(slot["video_revision"]) != request["video_revision"]):
            from .short_drama import RevisionConflict
            raise RevisionConflict("video shot was updated; refresh and retry")
        if bool(slot["locked"]):
            raise ValueError("locked video shot cannot change versions")
        version = conn.execute(
            "SELECT 1 FROM short_drama_video_versions "
            "WHERE video_shot_id=? AND version=? AND status='done'",
            (slot["id"], request["version"]),
        ).fetchone()
        if not version:
            raise LookupError("video version does not exist")
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_video_shots SET current_version=?,"
            "video_revision=video_revision+1,updated_at=? WHERE id=?",
            (request["version"], now, slot["id"]),
        )
        updated = conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1,updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND stage='video_review'",
            (now, project["id"], owner_username, request["revision"]),
        )
        if updated.rowcount != 1:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_video_workspace(
        db_factory, owner_username, request["project_id"], avatar_lookup
    )


def set_video_shot_lock(db_factory, owner_username, body, avatar_lookup=None):
    request = _write_request(
        body, {"project_id", "revision", "shot_id", "video_revision", "lock"},
        "video shot lock",
    )
    if type(request.get("lock")) is not bool:
        raise ValueError("video lock state is invalid")
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, request["project_id"])
        if project["stage"] != VIDEO_WRITE_STAGE:
            raise ValueError("videos are read-only in the current stage")
        if int(project["revision"]) != request["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        ensure_video_workspace(conn, project["id"], {VIDEO_WRITE_STAGE})
        reconcile_video_jobs(conn, project["id"])
        snapshot = build_video_snapshot(conn, project, avatar_lookup)
        shot = next(
            (item for item in snapshot["shots"] if item["id"] == request["shot_id"]),
            None,
        )
        if not shot:
            raise LookupError("short drama shot does not exist")
        if shot["video_revision"] != request["video_revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("video shot was updated; refresh and retry")
        if request["lock"] and shot["lock_blockers"]:
            first = shot["lock_blockers"][0]
            raise VideoBlocked(first["code"], first["message"])
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_video_shots SET locked=?,"
            "video_revision=video_revision+1,updated_at=? "
            "WHERE project_id=? AND shot_id=? AND video_revision=?",
            (
                int(request["lock"]), now, project["id"], request["shot_id"],
                request["video_revision"],
            ),
        )
        updated = conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1,updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND stage='video_review'",
            (now, project["id"], owner_username, request["revision"]),
        )
        if updated.rowcount != 1:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_video_workspace(
        db_factory, owner_username, request["project_id"], avatar_lookup
    )


def confirm_video_stage(db_factory, owner_username, body, avatar_lookup=None):
    if not isinstance(body, dict) or set(body) != {"project_id", "revision", "stage"}:
        raise ValueError("video stage confirmation fields are invalid")
    project_id = str(body.get("project_id") or "").strip()
    if (not project_id or type(body.get("revision")) is not int
            or body.get("stage") != VIDEO_WRITE_STAGE):
        raise ValueError("video stage confirmation is invalid")
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, project_id)
        if project["stage"] != VIDEO_WRITE_STAGE:
            raise ValueError("short drama stages cannot be skipped")
        if int(project["revision"]) != body["revision"]:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        ensure_video_workspace(conn, project_id, {VIDEO_WRITE_STAGE})
        reconcile_video_jobs(conn, project_id)
        snapshot = build_video_snapshot(conn, project, avatar_lookup)
        if snapshot["handoff_blocked"]:
            first = snapshot["handoff_blockers"][0]
            raise VideoBlocked(first["code"], first["message"])
        now = int(time.time())
        updated = conn.execute(
            "UPDATE short_drama_projects "
            "SET stage='assembly_review',revision=revision+1,updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND stage='video_review'",
            (now, project_id, owner_username, body["revision"]),
        )
        if updated.rowcount != 1:
            from .short_drama import RevisionConflict
            raise RevisionConflict("project was updated; refresh and retry")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_video_workspace(
        db_factory, owner_username, project_id, avatar_lookup
    )
