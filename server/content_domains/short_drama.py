"""Persistence and optimistic-concurrency helpers for short-drama projects."""

import base64
import copy
import json
import hashlib
import os
import re
import sqlite3
import time
import urllib.parse
import uuid
from contextlib import closing

from . import (
    short_drama_alignment,
    short_drama_advisor,
    short_drama_assembly,
    short_drama_completion,
    short_drama_conversation,
    short_drama_duration,
    short_drama_character_studio,
    short_drama_asset_graph,
    short_drama_autodraft,
    short_drama_preflight,
    short_drama_refinement,
    short_drama_lipsync,
    short_drama_lipsync_faces,
    short_drama_lipsync_observability,
    short_drama_lipsync_rollout,
    short_drama_playback,
    short_drama_production,
    short_drama_sound_design,
    short_drama_timeline,
    short_drama_video,
    short_drama_voice,
)


STAGES = (
    "draft", "characters_review", "script_review", "storyboard_review",
    "stills_review", "voice_review", "video_review", "assembly_review", "completed",
)
NEXT_STAGE = {
    "characters_review": "script_review",
    "script_review": "storyboard_review",
    "storyboard_review": "stills_review",
    "stills_review": "voice_review",
    "voice_review": "video_review",
    "video_review": "assembly_review",
    "assembly_review": "completed",
}
RATIOS = {"9:16", "16:9"}
DURATIONS = {30, 45, 60}
SHOT_COUNTS = set(range(6, 11))
DEFAULT_MAX_PROJECTS_PER_USER = 50
DEFAULT_PROJECT_PAGE_SIZE = 20
MAX_PROJECT_PAGE_SIZE = 50
MAX_CHARACTERS_PER_PROJECT = 20
MAX_DIALOGUE_LINES_PER_SCRIPT = 120
MAX_SCRIPT_VERSIONS_PER_PROJECT = 20
CONTENT_KEYS = {"characters", "script", "shots"}
PLANNING_SPEC_FIELDS = {
    "synopsis", "genre", "ratio", "target_duration", "shot_count", "visual_style", "target_platform",
}


class RevisionConflict(RuntimeError):
    pass


class AppliedJobConflict(RuntimeError):
    pass


class PointBudgetExceeded(ValueError):
    pass


class CharacterReferenceInProgress(ValueError):
    pass


class CharacterReferenceIdempotencyConflict(ValueError):
    pass


class CharacterReferenceProtected(ValueError):
    pass


class ProjectLimitExceeded(ValueError):
    def __init__(self, max_projects):
        super().__init__("短剧项目数量已达上限")
        self.max_projects = max_projects


class ScriptImportError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


class ProjectCreationError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


class ProjectHasUnappliedJobs(RuntimeError):
    pass


class AvatarBindingError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def _max_projects_per_user():
    try:
        value = int(os.getenv(
            "HQ_SHORT_DRAMA_MAX_PROJECTS_PER_USER",
            str(DEFAULT_MAX_PROJECTS_PER_USER),
        ))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PROJECTS_PER_USER
    return value if value > 0 else DEFAULT_MAX_PROJECTS_PER_USER


def _validate_page(value, default, maximum=None):
    if value is None:
        return default
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        raise ValueError("分页参数无效")
    return value


def validate_project_payload(payload, partial=False):
    cleaned = dict(payload or {})
    if not partial or "title" in cleaned:
        cleaned["title"] = str(cleaned.get("title") or "").strip()[:80]
        if not cleaned["title"]:
            raise ValueError("请输入短剧名称")
    if not partial or "synopsis" in cleaned:
        cleaned["synopsis"] = str(cleaned.get("synopsis") or "").strip()[:4000]
        if len(cleaned["synopsis"]) < 8:
            raise ValueError("故事梗概至少需要 8 个字")
    if not partial and "ratio" not in cleaned:
        raise ValueError("缺少短剧比例")
    if "ratio" in cleaned:
        if not isinstance(cleaned["ratio"], str) or cleaned["ratio"] not in RATIOS:
            raise ValueError("短剧比例仅支持 9:16、16:9")
    if not partial and "target_duration" not in cleaned:
        raise ValueError("缺少短剧目标时长")
    if "target_duration" in cleaned:
        if type(cleaned["target_duration"]) is not int or cleaned["target_duration"] not in DURATIONS:
            raise ValueError("短剧时长仅支持 30、45、60 秒")
    if not partial and "shot_count" not in cleaned:
        raise ValueError("缺少短剧分镜数量")
    if "shot_count" in cleaned:
        if type(cleaned["shot_count"]) is not int or cleaned["shot_count"] not in SHOT_COUNTS:
            raise ValueError("分镜数量必须为 6–10 个")
    if not partial:
        _validate_planning_limits(cleaned["target_duration"], cleaned["shot_count"])
    if "genre" in cleaned:
        cleaned["genre"] = str(cleaned.get("genre") or "").strip()
        if len(cleaned["genre"]) > 40:
            raise ValueError("题材类型不能超过 40 个字")
    elif not partial:
        cleaned["genre"] = ""
    cleaned["visual_style"] = str(cleaned.get("visual_style") or "电影写实").strip()[:80]
    if "point_budget" in cleaned:
        if type(cleaned["point_budget"]) is not int:
            raise ValueError("点数预算必须为整数")
        if cleaned["point_budget"] < 0:
            raise ValueError("点数预算不能为负数")
    if "board_id" in cleaned:
        if cleaned["board_id"] is not None and not isinstance(cleaned["board_id"], str):
            raise ValueError("画布 ID 无效")
        cleaned["board_id"] = _text(cleaned.get("board_id"), 128) or None
    return cleaned


_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_projects (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  board_id TEXT,
  title TEXT NOT NULL,
  synopsis TEXT NOT NULL,
  ratio TEXT NOT NULL CHECK (ratio IN ('9:16','16:9')),
  target_duration INTEGER NOT NULL CHECK (target_duration IN (30,45,60)),
  shot_count INTEGER NOT NULL CHECK (shot_count BETWEEN 6 AND 10),
  genre TEXT NOT NULL DEFAULT '',
  visual_style TEXT NOT NULL DEFAULT '电影写实',
  target_platform TEXT NOT NULL DEFAULT '抖音',
  point_budget INTEGER NOT NULL DEFAULT 0,
  spent_points INTEGER NOT NULL DEFAULT 0,
  creation_status TEXT NOT NULL DEFAULT 'formal' CHECK (creation_status IN ('draft','formal')),
  stage TEXT NOT NULL DEFAULT 'draft',
  revision INTEGER NOT NULL DEFAULT 1,
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_projects_owner
  ON short_drama_projects(username, deleted, updated_at DESC);

CREATE TABLE IF NOT EXISTS short_drama_job_project_links (
  job_id INTEGER PRIMARY KEY,
  project_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_job_project_links_project
  ON short_drama_job_project_links(project_id, job_id);

CREATE TABLE IF NOT EXISTS short_drama_project_requests (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  operation TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  project_id TEXT NOT NULL UNIQUE
    REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username,operation,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_project_requests_project
  ON short_drama_project_requests(project_id);

CREATE TABLE IF NOT EXISTS short_drama_script_imports (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  project_id TEXT NOT NULL UNIQUE
    REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  source_text TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  filename TEXT NOT NULL DEFAULT '',
  content_type TEXT NOT NULL DEFAULT 'live_action',
  character_contract_json TEXT NOT NULL DEFAULT '[]',
  character_contract_migration_json TEXT NOT NULL DEFAULT '{}',
  roles_saved_at INTEGER,
  core_story_json TEXT NOT NULL DEFAULT '{}',
  core_story_confirmed_at INTEGER,
  import_mode TEXT NOT NULL CHECK(import_mode IN ('faithful','optimize')),
  status TEXT NOT NULL CHECK(status IN ('completed')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_script_import_project
  ON short_drama_script_imports(project_id,status);

CREATE TABLE IF NOT EXISTS short_drama_characters (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  character_key TEXT NOT NULL,
  name TEXT NOT NULL,
  identity_text TEXT NOT NULL DEFAULT '',
  personality TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL CHECK (source_type IN ('cinematic_avatar','ai_character')),
  avatar_id TEXT,
  appearance_prompt TEXT NOT NULL DEFAULT '',
  wardrobe_prompt TEXT NOT NULL DEFAULT '',
  reference_job_id INTEGER,
  reference_file TEXT NOT NULL DEFAULT '',
  reference_url TEXT NOT NULL DEFAULT '',
  reference_version INTEGER NOT NULL DEFAULT 0,
  reference_locked INTEGER NOT NULL DEFAULT 0 CHECK (reference_locked IN (0,1)),
  reference_source TEXT NOT NULL DEFAULT '',
  reference_asset_id TEXT NOT NULL DEFAULT '',
  reference_name TEXT NOT NULL DEFAULT '',
  reference_profile_stale INTEGER NOT NULL DEFAULT 0 CHECK (reference_profile_stale IN (0,1)),
  pending_reference_job_id INTEGER,
  pending_reference_file TEXT NOT NULL DEFAULT '',
  pending_reference_url TEXT NOT NULL DEFAULT '',
  pending_reference_version INTEGER NOT NULL DEFAULT 0,
  pending_reference_source TEXT NOT NULL DEFAULT '',
  pending_reference_asset_id TEXT NOT NULL DEFAULT '',
  pending_reference_name TEXT NOT NULL DEFAULT '',
  voice_key TEXT,
  voice_settings_json TEXT NOT NULL DEFAULT '{}',
  sort_order INTEGER NOT NULL DEFAULT 0,
  UNIQUE(project_id, character_key)
);

CREATE TABLE IF NOT EXISTS short_drama_character_reference_jobs (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  character_key TEXT NOT NULL,
  project_revision INTEGER NOT NULL,
  character_snapshot_hash TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  job_id INTEGER NOT NULL UNIQUE,
  cost INTEGER NOT NULL CHECK (cost >= 0),
  status TEXT NOT NULL CHECK (status IN ('linked','ready','done','failed')),
  error TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_character_reference_project
  ON short_drama_character_reference_jobs(project_id, character_key, status, created_at DESC);

CREATE TABLE IF NOT EXISTS short_drama_character_reference_attempts (
  charge_key TEXT PRIMARY KEY,
  refund_key TEXT NOT NULL UNIQUE,
  username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  character_key TEXT NOT NULL,
  project_revision INTEGER NOT NULL,
  character_snapshot_hash TEXT NOT NULL,
  cost INTEGER NOT NULL CHECK (cost >= 0),
  image_payload_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN
    ('accepted','charged','linked','refund_pending','refunded','failed')),
  points_left INTEGER,
  job_id INTEGER,
  terminal_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_character_reference_attempt_operation
  ON short_drama_character_reference_attempts(project_id, character_key, state, updated_at);

CREATE TABLE IF NOT EXISTS short_drama_scripts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  title TEXT NOT NULL,
  logline TEXT NOT NULL DEFAULT '',
  hook TEXT NOT NULL DEFAULT '',
  conflict_text TEXT NOT NULL DEFAULT '',
  turn_text TEXT NOT NULL DEFAULT '',
  ending TEXT NOT NULL DEFAULT '',
  dialogue_lines_json TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL,
  UNIQUE(project_id, version)
);

CREATE TABLE IF NOT EXISTS short_drama_dialogue_tokens (
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  client_token TEXT NOT NULL,
  line_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('active','retired')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(project_id, client_token),
  UNIQUE(project_id, line_id)
);

CREATE TABLE IF NOT EXISTS short_drama_shots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  script_version INTEGER NOT NULL,
  shot_key TEXT NOT NULL,
  sort_order INTEGER NOT NULL,
  duration INTEGER NOT NULL CHECK (duration IN (5,10)),
  scene_description TEXT NOT NULL,
  camera_description TEXT NOT NULL,
  character_keys_json TEXT NOT NULL DEFAULT '[]',
  dialogue_line_ids_json TEXT NOT NULL DEFAULT '[]',
  image_prompt TEXT NOT NULL,
  video_prompt TEXT NOT NULL,
  UNIQUE(project_id, script_version, shot_key)
);

CREATE TABLE IF NOT EXISTS short_drama_applied_jobs (
  job_id INTEGER PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  username TEXT NOT NULL,
  cost INTEGER NOT NULL,
  applied_at INTEGER NOT NULL
);
"""


def _connection(db_factory):
    conn = db_factory()
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _json(value, default):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_text(value, default):
    return json.dumps(default if value is None else value, ensure_ascii=False, separators=(",", ":"))


_REQUIRED_CHARACTER_REFERENCE_VIEWS = (
    "front_full", "side_full", "back_full",
)
_LEGACY_CHARACTER_REFERENCE_VIEWS = (
    "front_full", "side_full", "front_half",
)


def _reference_version(value, default=0):
    try:
        parsed = int(value)
        if parsed < 0:
            raise ValueError("negative reference version")
        return parsed
    except (TypeError, ValueError):
        try:
            return max(0, int(default or 0))
        except (TypeError, ValueError):
            return 0


def _legacy_character_contract_migration(value, reference_versions=None):
    """Describe legacy three-view contracts without inventing a back image."""
    reference_versions = reference_versions if isinstance(reference_versions, dict) else {}
    character_keys = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        views = tuple(item.get("reference_views") or ())
        key = str(item.get("character_key") or "").strip()
        if views == _LEGACY_CHARACTER_REFERENCE_VIEWS and key:
            character_keys.append(key)
    if not character_keys:
        return {}
    return {
        "required": True,
        "code": "back_full_confirmation_required",
        "character_keys": character_keys,
        "reference_version_baselines": {
            key: _reference_version(reference_versions.get(key))
            for key in character_keys
        },
        "missing_reference_views": ["back_full"],
        "legacy_reference_views": list(_LEGACY_CHARACTER_REFERENCE_VIEWS),
        "message": "旧草稿缺少背面全身图，请生成并确认可信 AI 三视图标准图；普通上传或旧版半身参考图不能作为迁移证据。",
    }


def _trusted_character_contract_migration_evidence(
        conn, project_id, character_key, expected=None):
    """Return evidence for the exact active or pending trusted AI binding."""
    jobs_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if not jobs_table:
        return None
    row = conn.execute(
        "SELECT c.reference_job_id,c.reference_version,"
        "c.pending_reference_job_id,c.pending_reference_version "
        "FROM short_drama_characters c "
        "JOIN short_drama_projects p ON p.id=c.project_id "
        "JOIN short_drama_character_reference_jobs r "
        "ON r.job_id=COALESCE(c.pending_reference_job_id,c.reference_job_id) "
        "AND r.project_id=c.project_id "
        "AND r.character_key=c.character_key "
        "JOIN jobs j ON j.id=r.job_id AND j.username=r.username "
        "WHERE c.project_id=? AND c.character_key=? AND p.deleted=0 "
        "AND r.owner_username=p.username AND r.status='done' "
        "AND j.kind='image' AND j.status='done'",
        (project_id, character_key),
    ).fetchone()
    if not row:
        return None
    bound_job_id = row[2] if row[2] is not None else row[0]
    bound_version = row[3] if row[2] is not None else row[1]
    evidence = {
        "source": "trusted_ai_three_view",
        "reference_version": _reference_version(bound_version),
        "job_id": int(bound_job_id),
    }
    if expected is None:
        return evidence
    if not isinstance(expected, dict):
        return None
    try:
        expected_job_id = int(expected.get("job_id"))
        expected_version = int(expected.get("reference_version"))
    except (TypeError, ValueError):
        return None
    if (expected.get("source") != evidence["source"]
            or expected_job_id != evidence["job_id"]
            or expected_version != evidence["reference_version"]):
        return None
    return evidence


def _record_character_contract_migration_evidence(
        conn, project_id, character_key, reference_version, job_id=None):
    """Bind trusted three-view evidence to one concrete preview version."""
    row = conn.execute(
        "SELECT character_contract_migration_json "
        "FROM short_drama_script_imports WHERE project_id=?",
        (project_id,),
    ).fetchone()
    migration = _json(row[0], {}) if row else {}
    pending = [str(value) for value in migration.get("character_keys") or []]
    if not migration.get("required") or character_key not in pending:
        return
    evidence = migration.get("three_view_evidence")
    evidence = dict(evidence) if isinstance(evidence, dict) else {}
    if job_id is None:
        evidence.pop(character_key, None)
    else:
        evidence[character_key] = {
            "source": "trusted_ai_three_view",
            "reference_version": _reference_version(reference_version),
            "job_id": int(job_id),
        }
    if evidence:
        migration["three_view_evidence"] = evidence
    else:
        migration.pop("three_view_evidence", None)
    conn.execute(
        "UPDATE short_drama_script_imports "
        "SET character_contract_migration_json=? WHERE project_id=?",
        (_json_text(migration, {}), project_id),
    )


def _sync_character_contract_migration_roles(conn, project_id, character_keys):
    """Remove legally deleted roles from pending migration state atomically."""
    row = conn.execute(
        "SELECT character_contract_migration_json "
        "FROM short_drama_script_imports WHERE project_id=?",
        (project_id,),
    ).fetchone()
    migration = _json(row[0], {}) if row else {}
    pending = [str(value) for value in migration.get("character_keys") or []]
    if not migration.get("required") or not pending:
        return
    retained = [key for key in pending if key in character_keys]
    if retained == pending:
        return
    if not retained:
        migration = {}
    else:
        baselines = migration.get("reference_version_baselines")
        baselines = baselines if isinstance(baselines, dict) else {}
        evidence = migration.get("three_view_evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        migration["character_keys"] = retained
        migration["reference_version_baselines"] = {
            key: baselines[key] for key in retained if key in baselines
        }
        retained_evidence = {
            key: evidence[key] for key in retained if key in evidence
        }
        if retained_evidence:
            migration["three_view_evidence"] = retained_evidence
        else:
            migration.pop("three_view_evidence", None)
    conn.execute(
        "UPDATE short_drama_script_imports "
        "SET character_contract_migration_json=? WHERE project_id=?",
        (_json_text(migration, {}), project_id),
    )


def _confirm_character_contract_migration(conn, project_id, character_key,
                                            reference_version,
                                            reference_job_id):
    """Clear one legacy-view marker only after a new preview is locked."""
    row = conn.execute(
        "SELECT character_contract_migration_json,character_contract_json "
        "FROM short_drama_script_imports WHERE project_id=?",
        (project_id,),
    ).fetchone()
    migration = _json(row[0], {}) if row else {}
    current = [str(value) for value in migration.get("character_keys") or []]
    baselines = migration.get("reference_version_baselines")
    baselines = baselines if isinstance(baselines, dict) else {}
    baseline = _reference_version(
        baselines.get(character_key, reference_version), reference_version
    )
    if (not migration.get("required") or character_key not in current
            or reference_version <= baseline):
        return
    evidence_map = migration.get("three_view_evidence")
    evidence_map = evidence_map if isinstance(evidence_map, dict) else {}
    evidence = _trusted_character_contract_migration_evidence(
        conn, project_id, character_key, evidence_map.get(character_key)
    )
    if (not evidence or reference_job_id is None
            or evidence["reference_version"] != reference_version
            or evidence["job_id"] != int(reference_job_id)):
        return
    contract = _json(row[1], [])
    upgraded_contract = []
    contract_eligible = False
    for item in contract if isinstance(contract, list) else []:
        clean = dict(item) if isinstance(item, dict) else item
        if (isinstance(clean, dict) and str(
                clean.get("character_key") or "").strip() == character_key):
            views = tuple(clean.get("reference_views") or ())
            if views == _LEGACY_CHARACTER_REFERENCE_VIEWS:
                clean["reference_views"] = list(
                    _REQUIRED_CHARACTER_REFERENCE_VIEWS
                )
                contract_eligible = True
            elif views == _REQUIRED_CHARACTER_REFERENCE_VIEWS:
                contract_eligible = True
        upgraded_contract.append(clean)
    if not contract_eligible:
        return
    pending = [value for value in current if value != character_key]
    baselines.pop(character_key, None)
    evidence_map.pop(character_key, None)
    if pending:
        migration["character_keys"] = pending
        migration["reference_version_baselines"] = baselines
        if evidence_map:
            migration["three_view_evidence"] = evidence_map
        else:
            migration.pop("three_view_evidence", None)
    else:
        migration = {}
    conn.execute(
        "UPDATE short_drama_script_imports "
        "SET character_contract_json=?,character_contract_migration_json=? "
        "WHERE project_id=?",
        (
            _json_text(upgraded_contract, []),
            _json_text(migration, {}),
            project_id,
        ),
    )


def _text(value, limit=None):
    text = str(value or "").strip()
    return text[:limit] if limit else text


def validate_planning_payload(payload):
    data = dict(payload or {})
    prompt = _text(data.get("prompt"), 4000)
    if not prompt:
        raise ValueError("请输入短剧需求")
    duration_value = data.get("dur", data.get("target_duration"))
    if type(duration_value) is int:
        target_duration = duration_value
    elif isinstance(duration_value, str) and duration_value.strip().lower() in {"30s", "45s", "60s"}:
        target_duration = {"30s": 30, "45s": 45, "60s": 60}[duration_value.strip().lower()]
    else:
        target_duration = 0
    ratio = _text(data.get("ratio") or "9:16")
    if ratio not in RATIOS:
        raise ValueError("短剧比例仅支持 9:16、16:9")
    shot_count = data.get("shot_count", 6)
    if type(shot_count) is not int:
        raise ValueError("分镜数量必须为整数")
    _validate_planning_limits(target_duration, shot_count)
    settings = {
        "prompt": prompt,
        "target_duration": target_duration,
        "ratio": ratio,
        "shot_count": shot_count,
        "style": _text(data.get("style") or "电影写实", 80),
        "platform": _text(data.get("platform") or "抖音", 80),
    }
    if "project_id" in data:
        if not isinstance(data["project_id"], str) or not data["project_id"].strip():
            raise ValueError("短剧项目 ID 无效")
        settings["project_id"] = data["project_id"].strip()
    if "project_revision" in data:
        if type(data["project_revision"]) is not int or data["project_revision"] < 1:
            raise ValueError("短剧项目版本无效")
        settings["project_revision"] = data["project_revision"]
    return settings


def validate_planning_submission(db_factory, username, payload, access=None):
    if not isinstance(payload, dict):
        raise ValueError("短剧策划请求必须是对象")
    allowed = {
        "format", "project_id", "project_revision", "prompt", "dur", "ratio", "shot_count",
        "style", "platform",
    }
    required = {"format", "project_id", "project_revision", "prompt", "dur", "ratio", "shot_count"}
    if set(payload) - allowed or not required.issubset(payload):
        raise ValueError("短剧策划请求字段不正确")
    if payload.get("format") != "short_drama":
        raise ValueError("短剧策划格式无效")
    settings = validate_planning_payload(payload)
    owner = _project_username_for_access(
        db_factory, username, settings["project_id"], access, write=True)
    conn = _connection(db_factory)
    try:
        row = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? AND deleted=0",
            (settings["project_id"], owner),
        ).fetchone()
        if not row:
            raise LookupError("短剧项目不存在")
        project = dict(row)
    finally:
        conn.close()
    if project["stage"] != "draft":
        raise ValueError("当前短剧阶段不能重新生成策划")
    if project["revision"] != settings["project_revision"]:
        raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
    expected = {
        "prompt": project["synopsis"],
        "target_duration": project["target_duration"],
        "ratio": project["ratio"],
        "shot_count": project["shot_count"],
        "style": project["visual_style"],
        "platform": project["target_platform"],
    }
    if any(settings[key] != value for key, value in expected.items()):
        raise ValueError("短剧策划设置与项目不一致")
    return {
        "format": "short_drama",
        "project_id": settings["project_id"],
        "project_revision": settings["project_revision"],
        "prompt": settings["prompt"],
        "dur": "%ss" % settings["target_duration"],
        "ratio": settings["ratio"],
        "shot_count": settings["shot_count"],
        "style": settings["style"],
        "platform": settings["platform"],
    }


def _validate_planning_limits(target_duration, shot_count):
    if target_duration not in DURATIONS:
        raise ValueError("短剧时长仅支持 15-30、30-60、60-90 秒")
    if shot_count not in SHOT_COUNTS:
        raise ValueError("分镜数量必须为 6-10 个")
    if not short_drama_duration.is_reachable(target_duration, shot_count):
        raise ValueError("短剧时长与分镜数量不匹配，无法组成 5/10 秒分镜")


def build_plan_prompt(settings):
    return (
        "为以下短剧需求生成可拍摄的完整规划。只输出一个 JSON 对象，不要解释，不要 markdown 代码块。\n"
        "需求：%s\n平台：%s；画幅：%s；成片时长区间：%s；分镜数：%s；视觉风格：%s。\n"
        "JSON 顶层必须且只能包含 title、logline、characters、script、shots。\n"
        "characters 是角色数组；每个角色必须包含 key、name、identity、personality、appearance_prompt、wardrobe_prompt，"
        "可选 voice_key、voice_settings。\n"
        "script 必须包含 hook、conflict、turn、ending、dialogue_lines；每条 dialogue_lines 必须包含 id、character_key、text。"
        "id 必须是唯一字符串，严格按台词顺序使用 line_001、line_002、line_003 这类格式；"
        "例如 {\"id\":\"line_001\",\"character_key\":\"boy\",\"text\":\"你怎么又来了？\"}。\n"
        "shots 是 6-10 条分镜数组；每条必须包含 key、duration、scene_description、camera_description、"
        "character_keys、dialogue_line_ids、image_prompt、video_prompt。duration 只能为 5 或 10 秒，"
        "所有 duration 之和必须位于 %s；优先保证对白和最后一镜完整，不要为凑时长截断镜头；"
        "character_keys 和 dialogue_line_ids 只能引用前述已定义的键。"
        "dialogue_line_ids 中的值必须与 dialogue_lines.id 完全一致。"
    ) % (
        settings["prompt"], settings["platform"], settings["ratio"],
        short_drama_duration.label(settings["target_duration"]),
        settings["shot_count"], settings["style"],
        short_drama_duration.label(settings["target_duration"]),
    )


def build_plan_retry_prompt(settings, raw, error):
    """Ask the same provider to correct one invalid result without a new paid job."""
    previous = str(raw or "")
    if len(previous) > 60000:
        previous = previous[:60000]
    return (
        "%s\n\n"
        "上一次输出没有通过格式校验，错误是：%s。\n"
        "请修正后重新输出完整 JSON 对象，不要只输出修改片段。"
        "不得删除剧情、角色、台词或分镜；所有台词 id 必须唯一，分镜引用必须同步。\n"
        "上一次输出：\n%s"
    ) % (build_plan_prompt(settings), str(error)[:300], previous)


def _required_text(item, key, limit):
    if key not in item or not isinstance(item[key], str):
        raise ValueError("短剧规划缺少字段: " + key)
    value = _text(item[key], limit)
    if not value:
        raise ValueError("短剧规划字段无效: " + key)
    return value


def _key_list(value, field):
    if not isinstance(value, list) or any(not isinstance(key, str) or not key.strip() for key in value):
        raise ValueError("短剧规划字段无效: " + field)
    values = [_text(key, 80) for key in value]
    if len(set(values)) != len(values):
        raise ValueError("短剧规划字段不能重复: " + field)
    return values


def _next_dialogue_id(position, used):
    candidate_number = max(1, int(position))
    while True:
        candidate = "line_%03d" % candidate_number
        if candidate not in used:
            return candidate
        candidate_number += 1


def repair_plan_dialogue_ids(raw):
    """Repair only deterministic dialogue-id shape errors before strict validation.

    Existing valid unique IDs are preserved. Missing, empty, or unique
    non-string IDs receive stable position-based IDs. Duplicate non-empty
    source IDs are ambiguous and must be rejected so the provider retry can
    regenerate both dialogue IDs and shot references together. Shot references
    are rewritten only through unambiguous aliases; semantic content is
    untouched.
    """
    if not isinstance(raw, dict):
        return raw
    script = raw.get("script")
    shots = raw.get("shots")
    if not isinstance(script, dict) or not isinstance(script.get("dialogue_lines"), list):
        return raw
    if not isinstance(shots, list):
        return raw

    repaired = copy.deepcopy(raw)
    lines = repaired["script"]["dialogue_lines"]
    seen_original_ids = set()
    for line in lines:
        if not isinstance(line, dict):
            continue
        original = line.get("id")
        original_text = (
            original.strip()
            if isinstance(original, str)
            else str(original).strip() if original is not None else ""
        )
        if not original_text:
            continue
        if original_text in seen_original_ids:
            raise ValueError("台词标识不能重复")
        seen_original_ids.add(original_text)

    used = set()
    reserved = {
        line["id"].strip()
        for line in lines
        if isinstance(line, dict)
        and isinstance(line.get("id"), str)
        and line["id"].strip()
    }
    aliases = {}

    for position, line in enumerate(lines, 1):
        if not isinstance(line, dict):
            continue
        original = line.get("id")
        original_text = original.strip() if isinstance(original, str) else str(original).strip() if original is not None else ""
        if original_text and isinstance(original, str) and original_text not in used:
            line_id = original_text
        else:
            line_id = _next_dialogue_id(position, used | reserved)
        line["id"] = line_id
        used.add(line_id)

        if original_text:
            aliases.setdefault(original_text, line_id)
        # Common model aliases are safe only when they point to the same
        # position and have not already been claimed by another line.
        for alias in (
                "line_%03d" % position,
                "line_%d" % position,
                "line-%d" % position,
                str(position),
        ):
            if alias not in reserved or alias == line_id:
                aliases.setdefault(alias, line_id)

    for shot in repaired["shots"]:
        if not isinstance(shot, dict) or not isinstance(shot.get("dialogue_line_ids"), list):
            continue
        repaired_references = []
        for reference in shot["dialogue_line_ids"]:
            reference_text = reference.strip() if isinstance(reference, str) else str(reference).strip()
            repaired_references.append(aliases.get(reference_text, reference_text))
        shot["dialogue_line_ids"] = repaired_references
    return repaired


def normalize_plan(raw, settings):
    if not isinstance(raw, dict):
        raise ValueError("短剧规划必须是 JSON 对象")
    try:
        target_duration = settings["target_duration"]
        shot_count = settings["shot_count"]
    except (KeyError, TypeError):
        raise ValueError("短剧规划设置无效")
    if type(target_duration) is not int or type(shot_count) is not int:
        raise ValueError("短剧规划设置无效")
    if not isinstance(settings.get("ratio"), str) or settings["ratio"] not in RATIOS:
        raise ValueError("短剧规划设置无效")
    _validate_planning_limits(target_duration, shot_count)
    required_top_level = {"title", "logline", "characters", "script", "shots"}
    if set(raw) != required_top_level:
        raise ValueError("短剧规划 JSON 字段不正确")
    title = _required_text(raw, "title", 80)
    logline = _required_text(raw, "logline", 4000)
    characters = raw["characters"]
    script = raw["script"]
    shots = raw["shots"]
    if not isinstance(characters, list) or not isinstance(script, dict) or not isinstance(shots, list):
        raise ValueError("短剧规划数据无效")
    if len(characters) > MAX_CHARACTERS_PER_PROJECT:
        raise ValueError("短剧角色数量不能超过 %d 个" % MAX_CHARACTERS_PER_PROJECT)

    normalized_characters = []
    for character in characters:
        if not isinstance(character, dict):
            raise ValueError("角色数据无效")
        source_type = character.get("source_type", "ai_character")
        if not isinstance(source_type, str) or source_type not in {"cinematic_avatar", "ai_character"}:
            raise ValueError("角色数据无效")
        voice_key = character.get("voice_key")
        if voice_key is not None and not isinstance(voice_key, str):
            raise ValueError("角色数据无效")
        voice_settings = character.get("voice_settings", {})
        if not isinstance(voice_settings, dict):
            raise ValueError("角色数据无效")
        identity = _required_text(character, "identity", 2000)
        normalized_characters.append({
            "key": _required_text(character, "key", 80),
            "name": _required_text(character, "name", 80),
            "identity": identity,
            "identity_text": identity,
            "personality": _required_text(character, "personality", 2000),
            "appearance_prompt": _required_text(character, "appearance_prompt", 4000),
            "wardrobe_prompt": _required_text(character, "wardrobe_prompt", 4000),
            "source_type": source_type,
            "voice_key": _text(voice_key, 80) or None,
            "voice_settings": voice_settings,
        })
    character_keys = [character["key"] for character in normalized_characters]
    if len(set(character_keys)) != len(character_keys):
        raise ValueError("角色标识不能重复")

    required_script = {"hook", "conflict", "turn", "ending", "dialogue_lines"}
    if set(script) != required_script or not isinstance(script["dialogue_lines"], list):
        raise ValueError("剧本数据无效")
    if len(script["dialogue_lines"]) > MAX_DIALOGUE_LINES_PER_SCRIPT:
        raise ValueError("剧本台词数量不能超过 %d 条" % MAX_DIALOGUE_LINES_PER_SCRIPT)
    dialogue_lines = []
    for line in script["dialogue_lines"]:
        if not isinstance(line, dict):
            raise ValueError("台词数据无效")
        line_id = _required_text(line, "id", 80)
        character_key = _required_text(line, "character_key", 80)
        if character_key not in character_keys:
            raise ValueError("台词引用了不存在的角色")
        dialogue_lines.append({
            "id": line_id,
            "character_key": character_key,
            "text": _required_text(line, "text", 4000),
        })
    dialogue_ids = [line["id"] for line in dialogue_lines]
    if len(set(dialogue_ids)) != len(dialogue_ids):
        raise ValueError("台词标识不能重复")
    normalized_script = {
        "title": title,
        "logline": logline,
        "hook": _required_text(script, "hook", 4000),
        "conflict": _required_text(script, "conflict", 4000),
        "turn": _required_text(script, "turn", 4000),
        "ending": _required_text(script, "ending", 4000),
        "dialogue_lines": dialogue_lines,
    }
    normalized_script["conflict_text"] = normalized_script["conflict"]
    normalized_script["turn_text"] = normalized_script["turn"]

    if len(shots) not in SHOT_COUNTS or len(shots) != shot_count:
        raise ValueError("分镜数量必须等于设定数量且为 6-10 个")
    normalized_shots = []
    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError("分镜数据无效")
        duration = shot.get("duration")
        if type(duration) is not int or duration not in (5, 10):
            raise ValueError("分镜时长必须为 5 或 10 秒")
        shot_character_keys = _key_list(shot.get("character_keys"), "character_keys")
        unknown_characters = set(shot_character_keys) - set(character_keys)
        if unknown_characters:
            raise ValueError("分镜引用了不存在的角色")
        dialogue_line_ids = _key_list(shot.get("dialogue_line_ids"), "dialogue_line_ids")
        unknown_dialogue_ids = set(dialogue_line_ids) - set(dialogue_ids)
        if unknown_dialogue_ids:
            raise ValueError("分镜引用了不存在的台词")
        normalized_shots.append({
            "key": _required_text(shot, "key", 80),
            "duration": duration,
            "scene_description": _required_text(shot, "scene_description", 4000),
            "camera_description": _required_text(shot, "camera_description", 4000),
            "character_keys": shot_character_keys,
            "dialogue_line_ids": dialogue_line_ids,
            "image_prompt": _required_text(shot, "image_prompt", 8000),
            "video_prompt": _required_text(shot, "video_prompt", 8000),
        })
    shot_keys = [shot["key"] for shot in normalized_shots]
    if len(set(shot_keys)) != len(shot_keys):
        raise ValueError("分镜标识不能重复")
    if not short_drama_duration.contains(
            target_duration, sum(shot["duration"] for shot in normalized_shots)):
        raise ValueError("分镜总时长必须位于所选成片时长区间")

    return {
        "title": title,
        "logline": logline,
        "characters": normalized_characters,
        "script": normalized_script,
        "shots": normalized_shots,
    }


def parse_and_normalize_plan(raw, settings):
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("短剧规划必须是 JSON")
    return normalize_plan(repair_plan_dialogue_ids(parsed), settings)


def _dict_rows(conn, query, params):
    cursor = conn.execute(query, params)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _table_columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def _ensure_job_project_links(conn):
    if not _table_columns(conn, "jobs"):
        return
    migration = "short_drama_job_project_links_backfill_v1"
    migrated = conn.execute(
        "SELECT 1 FROM short_drama_schema_migrations WHERE name=?", (migration,)
    ).fetchone()
    project_expr = (
        "CASE WHEN NEW.kind='copy' "
        "AND json_extract(NEW.payload,'$.format')='short_drama' "
        "THEN json_extract(NEW.payload,'$.project_id') "
        "WHEN NEW.kind='image' THEN "
        "json_extract(NEW.payload,'$.short_drama_scene_binding.project_id') END"
    )
    if not migrated:
        conn.execute(
            "INSERT OR IGNORE INTO short_drama_job_project_links "
            "(job_id,project_id,kind,created_at) "
            "SELECT id,CASE WHEN kind='copy' "
            "AND json_extract(payload,'$.format')='short_drama' "
            "THEN json_extract(payload,'$.project_id') "
            "WHEN kind='image' THEN "
            "json_extract(payload,'$.short_drama_scene_binding.project_id') END,"
            "kind,CAST(strftime('%s','now') AS INTEGER) FROM jobs "
            "WHERE json_valid(payload) AND ((kind='copy' "
            "AND json_extract(payload,'$.format')='short_drama' "
            "AND COALESCE(json_extract(payload,'$.project_id'),'')<>'') "
            "OR (kind='image' AND COALESCE(json_extract(payload,"
            "'$.short_drama_scene_binding.project_id'),'')<>''))"
        )
        conn.execute(
            "INSERT OR IGNORE INTO short_drama_schema_migrations(name,completed_at) "
            "VALUES (?,?)", (migration, int(time.time())),
        )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_short_drama_job_project_link "
        "AFTER INSERT ON jobs WHEN json_valid(NEW.payload) "
        "AND COALESCE(" + project_expr + ",'')<>'' BEGIN "
        "INSERT OR IGNORE INTO short_drama_job_project_links "
        "(job_id,project_id,kind,created_at) VALUES "
        "(NEW.id," + project_expr + ",NEW.kind,CAST(strftime('%s','now') AS INTEGER)); "
        "END"
    )


def _page_job_point_usage(conn, project_ids):
    project_ids = set(project_ids or ())
    usage = {
        project_id: {"actual": 0, "has_activity": False}
        for project_id in project_ids
    }
    job_columns = _table_columns(conn, "jobs")
    if not project_ids or not job_columns:
        return usage
    _ensure_job_project_links(conn)
    refunded_expr = "COALESCE(j.refunded,0)" if "refunded" in job_columns else "0"
    placeholders = ",".join("?" for _project_id in project_ids)
    rows = conn.execute(
        "SELECT l.project_id,SUM(CASE WHEN " + refunded_expr + "=1 THEN 0 "
        "WHEN COALESCE(j.cost,0)>0 THEN j.cost ELSE 0 END),COUNT(*) "
        "FROM short_drama_job_project_links l JOIN jobs j ON j.id=l.job_id "
        "WHERE l.project_id IN (" + placeholders + ") GROUP BY l.project_id",
        tuple(project_ids),
    ).fetchall()
    for project_id, actual, count in rows:
        usage[project_id]["has_activity"] = int(count or 0) > 0
        usage[project_id]["actual"] = max(0, int(actual or 0))
    return usage


def _page_project_point_usage(conn, projects):
    """Aggregate every point ledger for one page with a fixed query count."""
    projects = list(projects or ())
    project_ids = [row["id"] for row in projects]
    usage = {
        row["id"]: {
            "legacy": int(row.get("spent_points") or 0),
            "actual": 0, "reserved": 0, "has_activity": False,
        }
        for row in projects
    }
    if not project_ids:
        return {}
    placeholders = ",".join("?" for _project_id in project_ids)
    params = tuple(project_ids)
    job_columns = _table_columns(conn, "jobs")
    refunded_expr = "COALESCE(j.refunded,0)" if "refunded" in job_columns else "0"
    for project_id, values in _page_job_point_usage(conn, project_ids).items():
        usage[project_id]["actual"] += int(values["actual"])
        usage[project_id]["has_activity"] |= bool(values["has_activity"])

    linked_production_jobs = {project_id: set() for project_id in project_ids}
    production_columns = _table_columns(conn, "short_drama_production_jobs")
    if production_columns:
        production_refunded = (
            "COALESCE(p.refunded,0)" if "refunded" in production_columns else "0"
        )
        if job_columns:
            rows = conn.execute(
                "SELECT p.project_id,p.job_id,p.quoted_cost," + production_refunded
                + ",j.id,j.cost," + refunded_expr + " "
                "FROM short_drama_production_jobs p LEFT JOIN jobs j ON j.id=p.job_id "
                "WHERE p.project_id IN (" + placeholders + ")", params,
            ).fetchall()
        else:
            rows = [tuple(row) + (None, None, 0) for row in conn.execute(
                "SELECT p.project_id,p.job_id,p.quoted_cost," + production_refunded
                + " FROM short_drama_production_jobs p WHERE p.project_id IN ("
                + placeholders + ")", params,
            ).fetchall()]
        for project_id, job_id, quoted, link_refunded, found_id, cost, refunded in rows:
            item = usage[project_id]
            item["has_activity"] = True
            if job_id is not None:
                linked_production_jobs[project_id].add(int(job_id))
            if found_id is not None:
                if int(refunded or 0) != 1:
                    item["actual"] += max(0, int(cost or 0))
            elif int(link_refunded or 0) != 1:
                item["actual"] += max(0, int(quoted or 0))

    def simple_attempts(table, actual_states):
        if not _table_columns(conn, table):
            return
        for project_id, state, cost in conn.execute(
            "SELECT project_id,state,cost FROM " + table
            + " WHERE project_id IN (" + placeholders + ")", params,
        ).fetchall():
            item = usage[project_id]
            item["has_activity"] = True
            if state == "accepted":
                item["reserved"] += max(0, int(cost or 0))
            elif state in actual_states:
                item["actual"] += max(0, int(cost or 0))

    if _table_columns(conn, "short_drama_charge_attempts"):
        for project_id, state, cost, job_id in conn.execute(
            "SELECT project_id,state,cost,job_id FROM short_drama_charge_attempts "
            "WHERE project_id IN (" + placeholders + ")", params,
        ).fetchall():
            item = usage[project_id]
            item["has_activity"] = True
            if job_id is not None and int(job_id) in linked_production_jobs[project_id]:
                continue
            if state == "accepted":
                item["reserved"] += max(0, int(cost or 0))
            elif state in {"charged", "linked", "refund_pending"}:
                item["actual"] += max(0, int(cost or 0))

    def linked_attempts(table, actual_states):
        if not _table_columns(conn, table):
            return
        if job_columns:
            rows = conn.execute(
                "SELECT a.project_id,a.state,a.cost,a.job_id,j.id,j.cost,"
                + refunded_expr + " FROM " + table
                + " a LEFT JOIN jobs j ON j.id=a.job_id WHERE a.project_id IN ("
                + placeholders + ")", params,
            ).fetchall()
        else:
            rows = [tuple(row) + (None, None, 0) for row in conn.execute(
                "SELECT project_id,state,cost,job_id FROM " + table
                + " WHERE project_id IN (" + placeholders + ")", params,
            ).fetchall()]
        for project_id, state, cost, _job_id, found_id, job_cost, refunded in rows:
            item = usage[project_id]
            item["has_activity"] = True
            if state == "accepted":
                item["reserved"] += max(0, int(cost or 0))
            elif state in actual_states:
                if found_id is not None:
                    if int(refunded or 0) != 1:
                        item["actual"] += max(0, int(job_cost or 0))
                else:
                    item["actual"] += max(0, int(cost or 0))

    linked_attempts("short_drama_voice_charge_attempts", {"charged", "linked", "done", "refund_pending"})
    linked_attempts("short_drama_video_charge_attempts", {"charged", "linked", "done", "refund_pending"})
    linked_attempts("short_drama_final_attempts", {"charged", "done", "archived", "refund_pending"})
    simple_attempts("short_drama_delivery_attempts", {"charged", "linked", "refund_pending"})
    simple_attempts("short_drama_autodraft_attempts", {"charged", "linked", "refund_pending"})
    simple_attempts("short_drama_provider_shot_attempts", {"charged", "linked", "done", "refund_pending"})

    if _table_columns(conn, "short_drama_character_reference_jobs") and job_columns:
        rows = conn.execute(
            "SELECT r.project_id,r.cost,r.status,j.id,j.cost," + refunded_expr + " "
            "FROM short_drama_character_reference_jobs r "
            "LEFT JOIN jobs j ON j.id=r.job_id WHERE r.project_id IN ("
            + placeholders + ")", params,
        ).fetchall()
        for project_id, linked_cost, state, found_id, job_cost, refunded in rows:
            item = usage[project_id]
            item["has_activity"] = True
            if found_id is not None:
                if int(refunded or 0) != 1:
                    item["actual"] += max(0, int(job_cost or 0))
            elif state != "failed":
                item["actual"] += max(0, int(linked_cost or 0))

    if _table_columns(conn, "short_drama_character_reference_attempts"):
        for project_id, state, cost, job_id in conn.execute(
            "SELECT project_id,state,cost,job_id FROM "
            "short_drama_character_reference_attempts WHERE project_id IN ("
            + placeholders + ")", params,
        ).fetchall():
            item = usage[project_id]
            if state == "accepted":
                item["reserved"] += max(0, int(cost or 0))
            elif state in {"charged", "refund_pending"} and job_id is None:
                item["has_activity"] = True
                item["actual"] += max(0, int(cost or 0))

    if _table_columns(conn, "short_drama_sound_jobs") and job_columns:
        rows = conn.execute(
            "SELECT s.project_id,s.cost,s.status,j.id,j.cost," + refunded_expr + " "
            "FROM short_drama_sound_jobs s LEFT JOIN jobs j ON j.id=s.job_id "
            "WHERE s.project_id IN (" + placeholders + ")", params,
        ).fetchall()
        for project_id, linked_cost, state, found_id, job_cost, refunded in rows:
            item = usage[project_id]
            item["has_activity"] = True
            if found_id is not None:
                if int(refunded or 0) != 1:
                    item["actual"] += max(0, int(job_cost or 0))
            elif state != "failed":
                item["actual"] += max(0, int(linked_cost or 0))

    return {
        project_id: {
            "spent_points": item["actual"] if item["has_activity"] else item["legacy"],
            "reserved_points": item["reserved"],
        }
        for project_id, item in usage.items()
    }


def _project_point_usage(conn, project_id, page_job_usage=None):
    """Return one project-scoped ledger across planning and production charges."""
    project = conn.execute(
        "SELECT spent_points FROM short_drama_projects WHERE id=?", (project_id,)
    ).fetchone()
    legacy_spent = int(project[0] or 0) if project else 0
    page_usage = (page_job_usage or {}).get(project_id, {})
    actual = int(page_usage.get("actual") or 0)
    reserved = 0
    has_activity = bool(page_usage.get("has_activity"))

    job_columns = _table_columns(conn, "jobs")
    if job_columns and page_job_usage is None:
        _ensure_job_project_links(conn)
        refunded_expr = "COALESCE(j.refunded,0)" if "refunded" in job_columns else "0"
        for row in conn.execute(
                "SELECT j.cost," + refunded_expr + " "
                "FROM short_drama_job_project_links l "
                "JOIN jobs j ON j.id=l.job_id WHERE l.project_id=?",
                (project_id,),
        ).fetchall():
            has_activity = True
            if int(row[1] or 0) != 1:
                actual += max(0, int(row[0] or 0))

    linked_job_ids = set()
    production_columns = _table_columns(conn, "short_drama_production_jobs")
    if production_columns:
        production_refunded = (
            "COALESCE(p.refunded,0)" if "refunded" in production_columns else "0"
        )
        if job_columns:
            job_refunded = "COALESCE(j.refunded,0)" if "refunded" in job_columns else "0"
            rows = conn.execute(
                "SELECT p.job_id,p.quoted_cost," + production_refunded + ","
                "j.id,j.cost," + job_refunded + " "
                "FROM short_drama_production_jobs p "
                "LEFT JOIN jobs j ON j.id=p.job_id WHERE p.project_id=?",
                (project_id,),
            ).fetchall()
        else:
            rows = [tuple(row) + (None, None, 0) for row in conn.execute(
                "SELECT p.job_id,p.quoted_cost," + production_refunded + " "
                "FROM short_drama_production_jobs p WHERE p.project_id=?",
                (project_id,),
            ).fetchall()]
        for job_id, quoted_cost, link_refunded, found_job_id, cost, job_refunded in rows:
            has_activity = True
            if job_id is not None:
                linked_job_ids.add(int(job_id))
            if found_job_id is not None:
                if int(job_refunded or 0) != 1:
                    actual += max(0, int(cost or 0))
            elif int(link_refunded or 0) != 1:
                # Older ledgers can retain the project link after the global job is gone.
                actual += max(0, int(quoted_cost or 0))

    attempt_columns = _table_columns(conn, "short_drama_charge_attempts")
    if attempt_columns:
        for state, cost, job_id in conn.execute(
                "SELECT state,cost,job_id FROM short_drama_charge_attempts WHERE project_id=?",
                (project_id,),
        ).fetchall():
            has_activity = True
            if job_id is not None and int(job_id) in linked_job_ids:
                continue
            if state == "accepted":
                reserved += max(0, int(cost or 0))
            elif state in {"charged", "linked", "refund_pending"}:
                actual += max(0, int(cost or 0))

    voice_attempt_columns = _table_columns(conn, "short_drama_voice_charge_attempts")
    if voice_attempt_columns:
        for state, cost, job_id in conn.execute(
                "SELECT state,cost,job_id FROM short_drama_voice_charge_attempts "
                "WHERE project_id=?",
                (project_id,),
        ).fetchall():
            has_activity = True
            if state == "accepted":
                reserved += max(0, int(cost or 0))
                continue
            if state in {"charged", "linked", "done", "refund_pending"}:
                if job_id is not None and job_columns:
                    job = conn.execute(
                        "SELECT cost,COALESCE(refunded,0) FROM jobs WHERE id=?",
                        (job_id,),
                    ).fetchone()
                    if job:
                        if int(job[1] or 0) != 1:
                            actual += max(0, int(job[0] or 0))
                        continue
                if state not in {"refunded"}:
                    actual += max(0, int(cost or 0))

    video_attempt_columns = _table_columns(conn, "short_drama_video_charge_attempts")
    if video_attempt_columns:
        for state, cost, job_id in conn.execute(
                "SELECT state,cost,job_id FROM short_drama_video_charge_attempts "
                "WHERE project_id=?",
                (project_id,),
        ).fetchall():
            has_activity = True
            if state == "accepted":
                reserved += max(0, int(cost or 0))
                continue
            if state in {"charged", "linked", "done", "refund_pending"}:
                if job_id is not None and job_columns:
                    job = conn.execute(
                        "SELECT cost,COALESCE(refunded,0) FROM jobs WHERE id=?",
                        (job_id,),
                    ).fetchone()
                    if job:
                        if int(job[1] or 0) != 1:
                            actual += max(0, int(job[0] or 0))
                        continue
                actual += max(0, int(cost or 0))

    final_attempt_columns = _table_columns(conn, "short_drama_final_attempts")
    if final_attempt_columns:
        refunded_expr = (
            "COALESCE(refunded,0)" if "refunded" in job_columns else "0"
        )
        for state, cost, job_id in conn.execute(
                "SELECT state,cost,job_id FROM short_drama_final_attempts "
                "WHERE project_id=?",
                (project_id,),
        ).fetchall():
            has_activity = True
            if state == "accepted":
                reserved += max(0, int(cost or 0))
                continue
            if state not in {
                "charged", "done", "archived", "refund_pending"
            }:
                continue
            if job_id is not None and job_columns:
                job = conn.execute(
                    "SELECT cost," + refunded_expr + " FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if job:
                    if int(job[1] or 0) != 1:
                        actual += max(0, int(job[0] or 0))
                    continue
            actual += max(0, int(cost or 0))

    delivery_attempt_columns = _table_columns(
        conn, "short_drama_delivery_attempts"
    )
    if delivery_attempt_columns:
        for state, cost in conn.execute(
                "SELECT state,cost FROM short_drama_delivery_attempts "
                "WHERE project_id=?",
                (project_id,),
        ).fetchall():
            has_activity = True
            if state == "accepted":
                reserved += max(0, int(cost or 0))
            elif state in {"charged", "linked", "refund_pending"}:
                actual += max(0, int(cost or 0))

    autodraft_attempt_columns = _table_columns(
        conn, "short_drama_autodraft_attempts"
    )
    if autodraft_attempt_columns:
        for state, cost in conn.execute(
                "SELECT state,cost FROM short_drama_autodraft_attempts "
                "WHERE project_id=?",
                (project_id,),
        ).fetchall():
            has_activity = True
            if state == "accepted":
                reserved += max(0, int(cost or 0))
            elif state in {"charged", "linked", "refund_pending"}:
                actual += max(0, int(cost or 0))

    provider_shot_attempt_columns = _table_columns(
        conn, "short_drama_provider_shot_attempts"
    )
    if provider_shot_attempt_columns:
        for state, cost in conn.execute(
                "SELECT state,cost FROM short_drama_provider_shot_attempts "
                "WHERE project_id=?",
                (project_id,),
        ).fetchall():
            has_activity = True
            if state == "accepted":
                reserved += max(0, int(cost or 0))
            elif state in {"charged", "linked", "done", "refund_pending"}:
                actual += max(0, int(cost or 0))

    character_reference_columns = _table_columns(
        conn, "short_drama_character_reference_jobs"
    )
    job_columns = _table_columns(conn, "jobs")
    if character_reference_columns and job_columns:
        refunded_select = (
            "COALESCE(j.refunded,0)" if "refunded" in job_columns else "0"
        )
        rows = conn.execute(
            "SELECT r.job_id,r.cost,r.status,j.id,j.cost,"
            + refunded_select + " "
            "FROM short_drama_character_reference_jobs r "
            "LEFT JOIN jobs j ON j.id=r.job_id WHERE r.project_id=?",
            (project_id,),
        ).fetchall()
        for _job_id, linked_cost, state, found_job_id, job_cost, refunded in rows:
            has_activity = True
            if found_job_id is not None:
                if int(refunded or 0) != 1:
                    actual += max(0, int(job_cost or 0))
            elif state not in {"failed"}:
                actual += max(0, int(linked_cost or 0))

    character_attempt_columns = _table_columns(
        conn, "short_drama_character_reference_attempts"
    )
    if character_attempt_columns:
        for state, cost, job_id in conn.execute(
                "SELECT state,cost,job_id FROM "
                "short_drama_character_reference_attempts WHERE project_id=?",
                (project_id,),
        ).fetchall():
            if state == "accepted":
                reserved += max(0, int(cost or 0))
            elif state in {"charged", "refund_pending"} and job_id is None:
                has_activity = True
                actual += max(0, int(cost or 0))

    sound_job_columns = _table_columns(conn, "short_drama_sound_jobs")
    if sound_job_columns and job_columns:
        refunded_select = (
            "COALESCE(j.refunded,0)" if "refunded" in job_columns else "0"
        )
        rows = conn.execute(
            "SELECT s.cost,s.status,j.id,j.cost," + refunded_select + " "
            "FROM short_drama_sound_jobs s "
            "LEFT JOIN jobs j ON j.id=s.job_id WHERE s.project_id=?",
            (project_id,),
        ).fetchall()
        for linked_cost, state, found_job_id, job_cost, refunded in rows:
            has_activity = True
            if found_job_id is not None:
                if int(refunded or 0) != 1:
                    actual += max(0, int(job_cost or 0))
            elif state not in {"failed"}:
                actual += max(0, int(linked_cost or 0))

    if not has_activity:
        actual = legacy_spent
    return {"spent_points": actual, "reserved_points": reserved}


def _has_unapplied_charged_job(conn, username, project_id):
    if _table_columns(conn, "short_drama_character_reference_attempts") and conn.execute(
            "SELECT 1 FROM short_drama_character_reference_attempts a "
            "JOIN short_drama_projects project "
            "ON project.id=a.project_id AND project.username=? AND project.deleted=0 "
            "WHERE a.project_id=? "
            "AND a.state IN ('accepted','charged','refund_pending') LIMIT 1",
            (username, project_id),
    ).fetchone():
        return True
    character_reference_columns = _table_columns(
        conn, "short_drama_character_reference_jobs"
    )
    job_columns = _table_columns(conn, "jobs")
    if character_reference_columns and job_columns:
        not_refunded = (
            "AND COALESCE(j.refunded,0)<>1 " if "refunded" in job_columns else ""
        )
        if conn.execute(
                "SELECT 1 FROM short_drama_character_reference_jobs r "
                "JOIN short_drama_projects project "
                "ON project.id=r.project_id "
                "AND project.username=? AND project.deleted=0 "
                "JOIN jobs j ON j.id=r.job_id "
                "WHERE r.project_id=? AND r.status='linked' "
                + not_refunded + "LIMIT 1",
                (username, project_id),
        ).fetchone():
            return True
    if conn.execute(
            "SELECT 1 FROM short_drama_production_jobs p "
            "JOIN short_drama_projects project "
            "ON project.id=p.project_id AND project.username=? AND project.deleted=0 "
            "WHERE p.project_id=? AND p.status IN ('pending','running') LIMIT 1",
            (username, project_id),
    ).fetchone():
        return True
    if conn.execute(
            "SELECT 1 FROM short_drama_charge_attempts a "
            "JOIN short_drama_projects project "
            "ON project.id=a.project_id AND project.username=? AND project.deleted=0 "
            "WHERE a.project_id=? "
            "AND a.state IN ('accepted','charged','refund_pending') LIMIT 1",
            (username, project_id),
    ).fetchone():
        return True
    if _table_columns(conn, "short_drama_voice_charge_attempts") and conn.execute(
            "SELECT 1 FROM short_drama_voice_charge_attempts a "
            "JOIN short_drama_projects project "
            "ON project.id=a.project_id AND project.username=? AND project.deleted=0 "
            "WHERE a.project_id=? "
            "AND a.state IN ('accepted','charged','linked','refund_pending') LIMIT 1",
            (username, project_id),
    ).fetchone():
        return True
    if _table_columns(conn, "short_drama_video_charge_attempts") and conn.execute(
            "SELECT 1 FROM short_drama_video_charge_attempts a "
            "JOIN short_drama_projects project "
            "ON project.id=a.project_id AND project.username=? AND project.deleted=0 "
            "WHERE a.project_id=? "
            "AND a.state IN ('accepted','charged','linked','refund_pending') LIMIT 1",
            (username, project_id),
    ).fetchone():
        return True
    if _table_columns(conn, "short_drama_provider_shot_attempts") and conn.execute(
            "SELECT 1 FROM short_drama_provider_shot_attempts a "
            "JOIN short_drama_projects project "
            "ON project.id=a.project_id AND project.username=? AND project.deleted=0 "
            "WHERE a.project_id=? "
            "AND a.state IN ('accepted','charged','linked','refund_pending') LIMIT 1",
            (username, project_id),
    ).fetchone():
        return True
    applied_ids = {
        int(row[0]) for row in conn.execute(
            "SELECT job_id FROM short_drama_applied_jobs WHERE project_id=?",
            (project_id,),
        ).fetchall()
    }
    rows = conn.execute(
        "SELECT id, payload FROM jobs WHERE kind='copy' "
        "AND COALESCE(cost,0)>0 AND COALESCE(refunded,0)<>1",
    ).fetchall()
    for job_id, raw_payload in rows:
        if int(job_id) in applied_ids:
            continue
        payload = _json(raw_payload, {})
        if (isinstance(payload, dict) and payload.get("format") == "short_drama" and
                payload.get("project_id") == project_id):
            return True
    return False


def _project_detail(conn, username, project_id):
    projects = _dict_rows(conn,
        "SELECT * FROM short_drama_projects WHERE id=? AND username=? AND deleted=0",
        (project_id, username),
    )
    if not projects:
        raise LookupError("短剧项目不存在")
    detail = projects[0]
    detail["revision"] = int(detail["revision"])
    detail["spent_points"] = _project_point_usage(conn, project_id)["spent_points"]
    detail["characters"] = []
    for item in _dict_rows(conn,
        "SELECT * FROM short_drama_characters WHERE project_id=? ORDER BY sort_order, id",
        (project_id,),
    ):
        item["voice_settings"] = _json(item.pop("voice_settings_json"), {})
        detail["characters"].append(item)
    detail["script_versions"] = []
    for item in _dict_rows(conn,
        "SELECT * FROM short_drama_scripts WHERE project_id=? ORDER BY version",
        (project_id,),
    ):
        item["dialogue_lines"] = _json(item.pop("dialogue_lines_json"), [])
        detail["script_versions"].append(item)
    detail["dialogue_token_receipts"] = {
        row[0]: row[1] for row in conn.execute(
            "SELECT client_token,line_id FROM short_drama_dialogue_tokens "
            "WHERE project_id=? AND state='active' ORDER BY client_token",
            (project_id,),
        ).fetchall()
    }
    detail["shots"] = []
    for item in _dict_rows(conn,
        "SELECT * FROM short_drama_shots WHERE project_id=? ORDER BY script_version, sort_order, id",
        (project_id,),
    ):
        item["character_keys"] = _json(item.pop("character_keys_json"), [])
        item["dialogue_line_ids"] = _json(item.pop("dialogue_line_ids_json"), [])
        detail["shots"].append(item)
    imported = conn.execute(
        "SELECT source_text,filename,content_type,character_contract_json,"
        "roles_saved_at,import_mode,updated_at,core_story_json,"
        "character_contract_migration_json,"
        "core_story_confirmed_at FROM short_drama_script_imports "
        "WHERE project_id=? AND status='completed' LIMIT 1",
        (project_id,),
    ).fetchone()
    if imported:
        character_contract = _json(imported[3], [])
        detail["script_import"] = {
            "source_text": imported[0], "filename": imported[1],
            "content_type": imported[2],
            "character_contract": character_contract,
            "roles_saved_at": imported[4], "import_mode": imported[5],
            "updated_at": imported[6],
            "core_story": _json(imported[7], {}),
            "character_contract_migration": _json(imported[8], {}),
            "core_story_confirmed_at": imported[9],
        }
        confirmed_keys = {
            str(item.get("character_key") or "")
            for item in character_contract if isinstance(item, dict)
        }
        if imported[2] == "live_action" and confirmed_keys:
            detail["characters"] = [
                item for item in detail["characters"]
                if item.get("character_key") in confirmed_keys
            ]
    return detail


def init_db(db_factory):
    conn = _connection(db_factory)
    try:
        conn.executescript(_SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(short_drama_projects)")}
        if "board_id" not in columns:
            conn.execute("ALTER TABLE short_drama_projects ADD COLUMN board_id TEXT")
        if "genre" not in columns:
            conn.execute(
                "ALTER TABLE short_drama_projects ADD COLUMN genre "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "creation_status" not in columns:
            conn.execute(
                "ALTER TABLE short_drama_projects ADD COLUMN creation_status "
                "TEXT NOT NULL DEFAULT 'formal'"
            )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS short_drama_schema_migrations ("
            "name TEXT PRIMARY KEY,completed_at INTEGER NOT NULL)"
        )
        _ensure_job_project_links(conn)
        character_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(short_drama_characters)")
        }
        for name, declaration in {
            "reference_job_id": "INTEGER",
            "reference_file": "TEXT NOT NULL DEFAULT ''",
            "reference_url": "TEXT NOT NULL DEFAULT ''",
            "reference_version": "INTEGER NOT NULL DEFAULT 0",
            "reference_locked": "INTEGER NOT NULL DEFAULT 0",
            "reference_source": "TEXT NOT NULL DEFAULT ''",
            "reference_asset_id": "TEXT NOT NULL DEFAULT ''",
            "reference_name": "TEXT NOT NULL DEFAULT ''",
            "reference_profile_stale": "INTEGER NOT NULL DEFAULT 0",
            "pending_reference_job_id": "INTEGER",
            "pending_reference_file": "TEXT NOT NULL DEFAULT ''",
            "pending_reference_url": "TEXT NOT NULL DEFAULT ''",
            "pending_reference_version": "INTEGER NOT NULL DEFAULT 0",
            "pending_reference_source": "TEXT NOT NULL DEFAULT ''",
            "pending_reference_asset_id": "TEXT NOT NULL DEFAULT ''",
            "pending_reference_name": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in character_columns:
                conn.execute(
                    "ALTER TABLE short_drama_characters ADD COLUMN %s %s"
                    % (name, declaration)
                )
        import_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(short_drama_script_imports)")
        }
        for name, declaration in {
            "content_type": "TEXT NOT NULL DEFAULT 'live_action'",
            "character_contract_json": "TEXT NOT NULL DEFAULT '[]'",
            "character_contract_migration_json": "TEXT NOT NULL DEFAULT '{}'",
            "roles_saved_at": "INTEGER",
            "core_story_json": "TEXT NOT NULL DEFAULT '{}'",
            "core_story_confirmed_at": "INTEGER",
        }.items():
            if name not in import_columns:
                conn.execute(
                    "ALTER TABLE short_drama_script_imports ADD COLUMN %s %s"
                    % (name, declaration)
                )
        for import_id, project_id, contract_json, migration_json in conn.execute(
                "SELECT id,project_id,character_contract_json,"
                "character_contract_migration_json "
                "FROM short_drama_script_imports WHERE content_type='live_action'"):
            existing_migration = _json(migration_json, {})
            contract = _json(contract_json, [])
            legacy = _legacy_character_contract_migration(contract)
            character_keys = legacy.get("character_keys") or []
            current_versions = {}
            if character_keys:
                current_versions = {
                    str(row[0]): _reference_version(row[1])
                    for row in conn.execute(
                        "SELECT character_key,reference_version "
                        "FROM short_drama_characters WHERE project_id=?",
                        (project_id,),
                    ).fetchall()
                    if str(row[0]) in character_keys
                }
            existing_baselines = existing_migration.get(
                "reference_version_baselines"
            )
            if isinstance(existing_baselines, dict):
                for key in character_keys:
                    if key in existing_baselines:
                        current_versions[key] = _reference_version(
                            existing_baselines.get(key), current_versions.get(key, 0)
                        )
            migration = _legacy_character_contract_migration(
                contract, current_versions
            )
            if migration.get("required"):
                baselines = migration.get("reference_version_baselines") or {}
                evidence_field_present = (
                    "three_view_evidence" in existing_migration
                )
                existing_evidence = existing_migration.get(
                    "three_view_evidence"
                )
                existing_evidence = (
                    existing_evidence
                    if isinstance(existing_evidence, dict) else {}
                )
                evidence = {}
                for key in migration.get("character_keys") or []:
                    expected = None
                    if key in existing_evidence:
                        expected = existing_evidence[key]
                        if not isinstance(expected, dict):
                            expected = {}
                    elif evidence_field_present:
                        continue
                    trusted = _trusted_character_contract_migration_evidence(
                        conn, project_id, key, expected,
                    )
                    if (trusted and trusted["reference_version"]
                            > _reference_version(baselines.get(key))):
                        evidence[key] = trusted
                if evidence:
                    migration["three_view_evidence"] = evidence
                elif evidence_field_present:
                    migration["three_view_evidence"] = {}
            if migration.get("required") and migration != existing_migration:
                conn.execute(
                    "UPDATE short_drama_script_imports "
                    "SET character_contract_migration_json=? WHERE id=?",
                    (_json_text(migration, {}), import_id),
                )
        creation_status_migration = "creation_status_live_action_backfill_v1"
        if not conn.execute(
            "SELECT 1 FROM short_drama_schema_migrations WHERE name=?",
            (creation_status_migration,),
        ).fetchone():
            conn.execute(
                "UPDATE short_drama_projects SET creation_status='draft' "
                "WHERE creation_status='formal' AND stage='draft' AND id IN ("
                "SELECT project_id FROM short_drama_script_imports "
                "WHERE content_type='live_action' AND status='completed')"
            )
            conn.execute(
                "INSERT INTO short_drama_schema_migrations(name,completed_at) "
                "VALUES(?,?)", (creation_status_migration, int(time.time())),
            )
        conn.commit()
    finally:
        conn.close()
    short_drama_advisor.init_db(db_factory)
    short_drama_production.init_db(db_factory)
    short_drama_voice.init_db(db_factory)
    short_drama_alignment.init_db(db_factory)
    short_drama_timeline.init_db(db_factory)
    short_drama_video.init_db(db_factory)
    short_drama_assembly.init_db(db_factory)
    short_drama_sound_design.init_db(db_factory)
    short_drama_playback.init_db(db_factory)
    short_drama_completion.init_db(db_factory)
    short_drama_lipsync.init_db(db_factory)
    short_drama_lipsync_faces.init_db(db_factory)
    short_drama_lipsync_rollout.init_db(db_factory)
    short_drama_lipsync_observability.init_db(db_factory)
    short_drama_conversation.init_db(db_factory)
    short_drama_preflight.init_db(db_factory)
    short_drama_autodraft.init_db(db_factory)
    short_drama_refinement.init_db(db_factory)
    short_drama_asset_graph.init_db(db_factory)


def _project_username_for_access(db_factory, username, project_id, access=None, write=False):
    conn = _connection(db_factory)
    try:
        row = conn.execute(
            "SELECT username,board_id,stage,completion_id "
            "FROM short_drama_projects WHERE id=? AND deleted=0",
            (project_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise LookupError("short drama project does not exist")
    owner, board_id = row[0], row[1]
    if write and (row[2] == "completed" or row[3]):
        raise short_drama_completion.ProjectCompleted()
    if not board_id:
        if owner != username:
            raise LookupError("short drama project does not exist")
        return owner
    access = access if isinstance(access, dict) else {}
    role = str(access.get("role") or "").lower()
    if str(access.get("board_id") or "") != board_id or role not in {"owner", "editor", "viewer"}:
        raise LookupError("short drama project does not exist")
    if write and role not in {"owner", "editor"}:
        raise PermissionError("current board role is read-only")
    return owner


def validate_avatar_binding_submission(
    db_factory, username, binding, require_revision=True,
):
    if not binding:
        return
    project_id = str(binding.get("project_id") or "").strip()
    character_key = str(binding.get("character_key") or "").strip()
    revision = binding.get("project_revision")
    conn = _connection(db_factory)
    try:
        project = conn.execute(
            "SELECT revision FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("short drama project does not exist")
        if require_revision and int(project[0]) != revision:
            raise RevisionConflict("项目已更新，请刷新后重试")
        character = conn.execute(
            "SELECT 1 FROM short_drama_characters "
            "WHERE project_id=? AND character_key=?",
            (project_id, character_key),
        ).fetchone()
        if not character:
            raise LookupError("short drama character does not exist")
    finally:
        conn.close()


def _project_create_request_data(data):
    return {
        "board_id": data.get("board_id"),
        "title": data["title"],
        "synopsis": data["synopsis"],
        "ratio": data["ratio"],
        "target_duration": data["target_duration"],
        "shot_count": data["shot_count"],
        "genre": data.get("genre", ""),
        "visual_style": data["visual_style"],
        "target_platform": _text(data.get("target_platform") or "抖音", 80),
        "point_budget": data.get("point_budget", 0),
    }


def _require_project_create_access(data, access):
    board_id = data.get("board_id")
    if not board_id:
        return
    access = access if isinstance(access, dict) else {}
    if (str(access.get("board_id") or "") != board_id
            or str(access.get("role") or "").lower() not in {"owner", "editor"}):
        raise PermissionError("current board role cannot create this project")


def _insert_project_row(conn, username, data, request_data, project_id, now):
    max_projects = _max_projects_per_user()
    active_projects = conn.execute(
        "SELECT COUNT(*) FROM short_drama_projects WHERE username=? AND deleted=0",
        (username,),
    ).fetchone()[0]
    if active_projects >= max_projects:
        raise ProjectLimitExceeded(max_projects)
    conn.execute(
        "INSERT INTO short_drama_projects "
        "(id, username, board_id, title, synopsis, ratio, target_duration, shot_count, genre, visual_style, "
        "target_platform, point_budget, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, username, data.get("board_id"), data["title"], data["synopsis"], data["ratio"],
         data["target_duration"], data["shot_count"], data.get("genre", ""), data["visual_style"],
         request_data["target_platform"], request_data["point_budget"], now, now),
    )


def create_project(
    db_factory, username, payload, access=None, idempotency_key=None,
):
    data = validate_project_payload(payload)
    _require_project_create_access(data, access)
    key = str(idempotency_key or "").strip()
    if key and len(key) > 160:
        raise ProjectCreationError(
            "idempotency_key_invalid", "Idempotency-Key 长度不能超过 160 个字符"
        )
    operation = "project_create"
    request_data = _project_create_request_data(data)
    request_hash = hashlib.sha256(
        json.dumps(request_data, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    now = int(time.time())
    project_id = str(uuid.uuid4())
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if key:
            existing = conn.execute(
                "SELECT project_id,request_hash FROM short_drama_project_requests "
                "WHERE username=? AND operation=? AND idempotency_key=?",
                (username, operation, key),
            ).fetchone()
            if existing:
                if str(existing[1]) != request_hash:
                    raise ProjectCreationError(
                        "idempotency_conflict",
                        "同一 Idempotency-Key 不能用于不同的短剧项目",
                        409,
                    )
                result = _project_detail(conn, username, existing[0])
                conn.rollback()
                return result
        _insert_project_row(conn, username, data, request_data, project_id, now)
        if key:
            conn.execute(
                "INSERT INTO short_drama_project_requests "
                "(id,username,operation,idempotency_key,request_hash,project_id,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), username, operation, key, request_hash,
                    project_id, now, now,
                ),
            )
        conn.commit()
        return _project_detail(conn, username, project_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def promote_planner_project(
    db_factory, username, payload, idempotency_key, access=None,
):
    if not isinstance(payload, dict) or set(payload) != {
        "project", "planning_messages", "confirmed_contract",
    }:
        raise ProjectCreationError(
            "invalid_request", "确认剧本建项请求字段无效"
        )
    data = validate_project_payload(payload["project"])
    _require_project_create_access(data, access)
    messages = payload.get("planning_messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 10:
        raise ProjectCreationError(
            "invalid_request", "确认剧本策划消息数量无效"
        )
    messages = [short_drama_conversation._message(item) for item in messages]
    contract = payload.get("confirmed_contract")
    if not isinstance(contract, dict):
        raise ProjectCreationError(
            "invalid_request", "确认剧本合同格式无效"
        )
    key = str(idempotency_key or "").strip()
    if not key:
        raise ProjectCreationError(
            "idempotency_key_required", "缺少有效的 Idempotency-Key"
        )
    if len(key) > 160:
        raise ProjectCreationError(
            "idempotency_key_invalid", "Idempotency-Key 长度不能超过 160 个字符"
        )
    operation = "planner_promote"
    request_data = _project_create_request_data(data)
    request_hash = hashlib.sha256(json.dumps(
        {
            "project": request_data,
            "planning_messages": messages,
            "confirmed_contract": contract,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    now = int(time.time())
    project_id = str(uuid.uuid4())
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT project_id,request_hash FROM short_drama_project_requests "
            "WHERE username=? AND operation=? AND idempotency_key=?",
            (username, operation, key),
        ).fetchone()
        if existing:
            if str(existing[1]) != request_hash:
                raise ProjectCreationError(
                    "idempotency_conflict",
                    "同一 Idempotency-Key 不能用于不同的确认剧本建项请求",
                    409,
                )
            promoted_project = _project_detail(conn, username, existing[0])
            conversation_project = short_drama_conversation._project(
                conn, username, existing[0]
            )
            result = {
                "project": promoted_project,
                "workspace": short_drama_conversation._workspace(
                    conn, conversation_project, username
                ),
                "replayed": True,
            }
            conn.rollback()
            return result

        _insert_project_row(conn, username, data, request_data, project_id, now)
        project = short_drama_conversation._project(conn, username, project_id)
        short_drama_conversation._ensure_conversation(conn, project_id)
        for index, content in enumerate(messages):
            current = short_drama_conversation._conversation(conn, project_id)
            message_now = int(time.time() * 1000) + index * 2
            conn.execute(
                "INSERT INTO short_drama_conversation_messages "
                "(id,project_id,role,content,metadata_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), project_id, "user", content,
                    json.dumps({"kind": "preproject_promotion"}), message_now,
                ),
            )
            understanding = short_drama_conversation._understanding(
                project, short_drama_conversation._messages(conn, project_id)
            )
            reply, reply_metadata = short_drama_conversation._assistant_reply(
                project, understanding
            )
            conn.execute(
                "INSERT INTO short_drama_conversation_messages "
                "(id,project_id,role,content,metadata_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), project_id, "assistant", reply,
                    json.dumps(reply_metadata, ensure_ascii=False, sort_keys=True),
                    message_now + 1,
                ),
            )
            conn.execute(
                "UPDATE short_drama_conversations SET state='direction_review',"
                "understanding_json=?,revision=revision+1,updated_at=? "
                "WHERE project_id=? AND revision=?",
                (
                    json.dumps(understanding, ensure_ascii=False, sort_keys=True),
                    int(time.time()), project_id, int(current["revision"]),
                ),
            )

        current = short_drama_conversation._conversation(conn, project_id)
        version_id = short_drama_conversation._create_version(
            conn,
            project,
            username,
            current,
            "持久化用户已确认的逐镜合同",
            current["current_version_id"],
            confirmed_contract=contract,
        )
        current = short_drama_conversation._conversation(conn, project_id)
        locked_at = int(time.time())
        conn.execute(
            "UPDATE short_drama_script_snapshots SET status='locked',"
            "locked_by=?,locked_at=? WHERE project_id=? AND id=?",
            (username, locked_at, project_id, version_id),
        )
        conn.execute(
            "UPDATE short_drama_conversations SET state='script_locked',"
            "locked_version_id=?,revision=revision+1,updated_at=? "
            "WHERE project_id=? AND revision=?",
            (
                version_id, locked_at, project_id, int(current["revision"]),
            ),
        )
        conn.execute(
            "INSERT INTO short_drama_project_requests "
            "(id,username,operation,idempotency_key,request_hash,project_id,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), username, operation, key, request_hash,
                project_id, now, locked_at,
            ),
        )
        promoted_project = _project_detail(conn, username, project_id)
        response = {
            "project": promoted_project,
            "workspace": short_drama_conversation._workspace(
                conn, project, username
            ),
            "replayed": False,
        }
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _validate_import_character_contract(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ScriptImportError("invalid_character_contract", "角色确认数据无效")
    allowed = {
        "character_key", "name", "role_type", "gender", "identity_text", "relationships",
        "personality", "age", "face_shape", "hairstyle", "hair_color",
        "height_body", "fixed_clothing", "fixed_colors", "accessories",
        "appearance_prompt", "wardrobe_prompt", "reference_views",
    }
    text_fields = allowed - {"reference_views"}
    normalized = []
    keys = set()
    names = set()
    for item in value:
        if not isinstance(item, dict) or not set(item).issubset(allowed):
            raise ScriptImportError("invalid_character_contract", "角色确认字段无效")
        clean = {field: str(item.get(field) or "").strip() for field in text_fields}
        key = clean["character_key"]
        name = clean["name"]
        if not re.match(r"^[a-z][a-z0-9_]{0,63}$", key):
            raise ScriptImportError("invalid_character_key", "角色标识格式无效")
        if not name or len(name) > 80:
            raise ScriptImportError("invalid_character_name", "角色名称无效")
        if key in keys or name.casefold() in names:
            raise ScriptImportError("duplicate_character", "角色标识或名称重复")
        if clean["role_type"] not in {"main", "support", "crowd"}:
            raise ScriptImportError("invalid_role_type", "角色类型无效")
        if any(len(clean[field]) > 500 for field in text_fields - {"character_key", "name", "role_type"}):
            raise ScriptImportError("character_field_too_long", "角色信息过长")
        views = item.get("reference_views")
        if (not isinstance(views, list)
                or tuple(views) != _REQUIRED_CHARACTER_REFERENCE_VIEWS):
            raise ScriptImportError("invalid_reference_views", "角色卡必须包含正面、侧面和背面全身图")
        clean["reference_views"] = list(_REQUIRED_CHARACTER_REFERENCE_VIEWS)
        keys.add(key)
        names.add(name.casefold())
        normalized.append(clean)
    return normalized


def _characters_from_import_contract(contract):
    """Materialize the user-confirmed live-action roles as editable project characters."""
    characters = []
    for index, item in enumerate(contract or []):
        identity_parts = [
            item.get("role_type"), item.get("gender"), item.get("age"),
            item.get("identity_text"), item.get("relationships"),
        ]
        appearance_parts = [
            item.get("appearance_prompt"), item.get("gender"), item.get("age"),
            item.get("face_shape"), item.get("hairstyle"),
            item.get("hair_color"), item.get("height_body"),
        ]
        wardrobe_parts = [
            item.get("wardrobe_prompt"), item.get("fixed_clothing"),
            item.get("fixed_colors"), item.get("accessories"),
        ]
        identity_text = "；".join(
            str(value).strip() for value in identity_parts if value
        )
        if len(identity_text) > 2000:
            raise ScriptImportError(
                "character_identity_too_long",
                "角色类型、性别、年龄、身份和人物关系合并后不能超过 2,000 个字符",
            )
        characters.append({
            "character_key": item["character_key"],
            "name": item["name"],
            "identity_text": identity_text,
            "personality": item.get("personality") or "以用户确认的动作、表情、台词和连续性为准",
            "source_type": "ai_character",
            "appearance_prompt": "；".join(
                str(value).strip() for value in appearance_parts if value
            ) or "电影写实人物，单人，清晰正面五官",
            "wardrobe_prompt": "；".join(
                str(value).strip() for value in wardrobe_parts if value
            ),
            "reference_job_id": None,
            "reference_file": "",
            "reference_url": "",
            "reference_version": 0,
            "reference_locked": False,
            "reference_source": "",
            "reference_asset_id": "",
            "reference_name": "",
            "reference_profile_stale": False,
            "pending_reference_job_id": None,
            "pending_reference_file": "",
            "pending_reference_url": "",
            "pending_reference_version": 0,
            "pending_reference_source": "",
            "pending_reference_asset_id": "",
            "pending_reference_name": "",
            "voice_settings": {},
            "sort_order": index,
        })
    return _normalize_characters(characters, require_complete=False)


def import_script_project(db_factory, username, payload, idempotency_key):
    required = {
        "title", "synopsis", "ratio", "target_duration", "shot_count",
        "visual_style", "source_text", "filename", "import_mode",
    }
    optional = {
        "content_type", "character_contract", "genre", "source_requirement",
    }
    if (not isinstance(payload, dict) or not required.issubset(payload)
            or not set(payload).issubset(required | optional)):
        raise ScriptImportError("invalid_request", "剧本导入请求字段无效")
    project_data = validate_project_payload({
        key: payload[key] for key in (
            "title", "synopsis", "ratio", "target_duration", "shot_count",
            "visual_style",
        )
    } | {"genre": payload.get("genre", "")})
    source = str(payload.get("source_text") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    source_requirement = str(payload.get("source_requirement") or "").strip()
    if source_requirement not in {"", "complete_story"}:
        raise ScriptImportError("invalid_source_requirement", "故事原稿要求无效")
    if source_requirement == "complete_story" and len(source) < 100:
        raise ScriptImportError(
            "complete_story_too_short",
            "完整故事至少需要 100 个字，请补充人物、起因、发展、冲突、高潮和结局",
        )
    if len(source) < 8:
        raise ScriptImportError("script_too_short", "导入剧本至少需要 8 个字符")
    if len(source) > 50000:
        raise ScriptImportError("script_too_long", "单次最多导入 50,000 个字符", 413)
    mode = str(payload.get("import_mode") or "").strip()
    if mode not in {"faithful", "optimize"}:
        raise ScriptImportError("invalid_import_mode", "剧本导入模式无效")
    filename = str(payload.get("filename") or "").strip()
    if len(filename) > 255:
        raise ScriptImportError("invalid_filename", "导入文件名最多 255 个字符")
    content_type = str(payload.get("content_type") or "live_action").strip()
    if content_type != "live_action":
        raise ScriptImportError("unsupported_content_type", "该短剧类型尚未开放")
    character_contract = _validate_import_character_contract(
        payload.get("character_contract")
    )
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 160:
        raise ScriptImportError("idempotency_key_required", "缺少有效的幂等键")
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    request_data = {
        "project": project_data,
        "source_hash": source_hash,
        "filename": filename,
        "import_mode": mode,
        "content_type": content_type,
        "character_contract": character_contract,
        "source_requirement": source_requirement,
    }
    request_hash = hashlib.sha256(
        json.dumps(request_data, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    now = int(time.time())
    project_id = str(uuid.uuid4())
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT project_id,request_hash FROM short_drama_script_imports "
            "WHERE username=? AND idempotency_key=?",
            (username, key),
        ).fetchone()
        if existing:
            if str(existing[1]) != request_hash:
                raise ScriptImportError(
                    "idempotency_conflict",
                    "同一 Idempotency-Key 不能用于不同的剧本导入",
                    409,
                )
            result = _project_detail(conn, username, existing[0])
            result["script_import"] = {
                "status": "completed", "source_hash": source_hash,
                "import_mode": mode, "content_type": content_type,
                "character_count": len(source),
                "role_count": len(character_contract),
                "character_contract": character_contract,
                "replayed": True,
            }
            conn.rollback()
            return result
        active_projects = conn.execute(
            "SELECT COUNT(*) FROM short_drama_projects "
            "WHERE username=? AND deleted=0", (username,),
        ).fetchone()[0]
        max_projects = _max_projects_per_user()
        if active_projects >= max_projects:
            raise ProjectLimitExceeded(max_projects)
        conn.execute(
            "INSERT INTO short_drama_projects "
            "(id,username,title,synopsis,ratio,target_duration,shot_count,"
            "visual_style,target_platform,point_budget,creation_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?, ?,?)",
            (
                project_id, username, project_data["title"],
                project_data["synopsis"], project_data["ratio"],
                project_data["target_duration"], project_data["shot_count"],
                project_data["visual_style"], "抖音",
                project_data.get("point_budget", 0), "draft", now, now,
            ),
        )
        conn.execute(
            "INSERT INTO short_drama_script_imports "
            "(id,username,project_id,idempotency_key,request_hash,source_text,"
            "source_hash,filename,content_type,character_contract_json,import_mode,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'completed',?,?)",
            (
                str(uuid.uuid4()), username, project_id, key, request_hash,
                source, source_hash, filename, content_type,
                json.dumps(character_contract, ensure_ascii=False, separators=(",", ":")),
                mode, now, now,
            ),
        )
        if character_contract:
            _insert_characters(
                conn, project_id, _characters_from_import_contract(character_contract)
            )
        short_drama_conversation.seed_import_conversation(
            conn, username, project_id,
        )
        result = _project_detail(conn, username, project_id)
        result["script_import"] = {
            "status": "completed", "source_hash": source_hash,
            "import_mode": mode, "content_type": content_type,
            "character_count": len(source),
            "role_count": len(character_contract),
            "character_contract": character_contract,
            "replayed": False,
        }
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _page_project_progress(conn, project_ids):
    project_ids = list(project_ids or ())
    result = {project_id: {} for project_id in project_ids}
    if not project_ids:
        return result
    placeholders = ",".join("?" for _project_id in project_ids)
    params = tuple(project_ids)

    def grouped(table, expressions):
        if not _table_columns(conn, table):
            return
        rows = conn.execute(
            "SELECT project_id," + ",".join(expressions.values())
            + " FROM " + table + " WHERE project_id IN (" + placeholders
            + ") GROUP BY project_id", params,
        ).fetchall()
        keys = list(expressions)
        for row in rows:
            result[row[0]].update({key: int(value or 0) for key, value in zip(keys, row[1:])})

    grouped("short_drama_script_imports", {
        "story_ready": "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END)",
    })
    grouped("short_drama_characters", {
        "character_total": "COUNT(*)",
        "locked_characters": "SUM(CASE WHEN COALESCE(reference_locked,0)=1 THEN 1 ELSE 0 END)",
    })
    grouped("short_drama_script_snapshots", {
        "script_locked": "SUM(CASE WHEN status='locked' THEN 1 ELSE 0 END)",
    })
    grouped("short_drama_provider_shot_versions", {
        "ready_shots": "COUNT(DISTINCT CASE WHEN status='ready' THEN shot_key END)",
    })
    grouped("short_drama_autodraft_versions", {
        "preview_ready": "SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END)",
    })
    grouped("short_drama_refinement_versions", {
        "acceptance_ready": "SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END)",
    })
    grouped("short_drama_delivery_versions", {
        "delivery_ready": "SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END)",
    })
    grouped("short_drama_autodraft_jobs", {
        "active_autodraft": "SUM(CASE WHEN status IN ('queued','running') THEN 1 ELSE 0 END)",
    })
    grouped("short_drama_delivery_jobs", {
        "active_delivery": "SUM(CASE WHEN status IN ('queued','running') THEN 1 ELSE 0 END)",
    })
    return result


def _project_progress_summary(conn, project, page_progress=None):
    """Summarize the deepest durable production milestone for project cards."""
    project_id = project["id"]
    target_shots = max(1, int(project.get("shot_count") or 1))
    percent = 10
    progress_stage = "setup"
    label = "等待确认核心故事"
    detail = "基本信息已保存"

    metrics = (page_progress or {}).get(project_id)

    def count(table, where="1=1", params=(), metric=None):
        if metrics is not None and metric:
            return int(metrics.get(metric) or 0)
        if not _table_columns(conn, table):
            return 0
        return int(conn.execute(
            "SELECT COUNT(*) FROM %s WHERE project_id=? AND %s" % (table, where),
            (project_id,) + tuple(params),
        ).fetchone()[0])

    story_ready = count(
        "short_drama_script_imports", "status='completed'", metric="story_ready"
    ) > 0
    if story_ready:
        percent = 20
        progress_stage = "character_review"
        label = "等待完成角色形象"
        detail = "核心故事已确认"

    character_total = count("short_drama_characters", metric="character_total")
    locked_characters = count(
        "short_drama_characters", "COALESCE(reference_locked,0)=1", metric="locked_characters"
    )
    if character_total:
        ratio = min(1.0, locked_characters / character_total)
        percent = max(percent, 20 + round(15 * ratio))
        label = "角色形象 %s/%s" % (locked_characters, character_total)
        detail = "正在确认角色和标准图"
        if locked_characters >= character_total:
            percent = max(percent, 35)
            progress_stage = "script_review"
            label = "等待生成并锁定剧本"
            detail = "角色形象已完成"

    script_locked = count(
        "short_drama_script_snapshots", "status='locked'", metric="script_locked"
    ) > 0
    if script_locked:
        percent = max(percent, 50)
        progress_stage = "video_review"
        label = "等待生成镜头"
        detail = "剧本已锁定"

    ready_shots = 0
    if metrics is not None:
        ready_shots = int(metrics.get("ready_shots") or 0)
    elif _table_columns(conn, "short_drama_provider_shot_versions"):
        ready_shots = int(conn.execute(
            "SELECT COUNT(DISTINCT shot_key) "
            "FROM short_drama_provider_shot_versions "
            "WHERE project_id=? AND status='ready'",
            (project_id,),
        ).fetchone()[0])
    ready_shots = min(target_shots, ready_shots)
    if script_locked and ready_shots:
        percent = max(percent, 50 + round(20 * ready_shots / target_shots))
        label = "镜头生成中 %s/%s" % (ready_shots, target_shots)
        detail = "还有 %s 个镜头未完成" % max(0, target_shots - ready_shots)
    if ready_shots >= target_shots:
        percent = max(percent, 70)
        progress_stage = "assembly_review"
        label = "等待合成 1080p 草稿"
        detail = "全部镜头已完成"

    preview_ready = count(
        "short_drama_autodraft_versions", "status='ready'", metric="preview_ready"
    ) > 0
    if preview_ready:
        percent = max(percent, 85)
        progress_stage = "assembly_review"
        label = "等待全片验收"
        detail = "1080p 全片草稿已生成"

    acceptance_ready = count(
        "short_drama_refinement_versions", "status='confirmed'", metric="acceptance_ready"
    ) > 0
    if acceptance_ready:
        percent = max(percent, 90)
        progress_stage = "assembly_review"
        label = "等待导出 2K 成片"
        detail = "全片验收已通过"

    delivery_ready = count(
        "short_drama_delivery_versions", "status='ready'", metric="delivery_ready"
    ) > 0
    legacy_completed = project.get("stage") == "completed"
    if delivery_ready or legacy_completed:
        percent = 100
        progress_stage = "completed"
        label = "已完成正式交付"
        detail = "2K 正式成片已生成"
        active_reassembly = count(
            "short_drama_autodraft_jobs", "status IN ('queued','running')", metric="active_autodraft"
        ) + count(
            "short_drama_delivery_jobs", "status IN ('queued','running')", metric="active_delivery"
        )
        if active_reassembly:
            label = "已交付 · 新版本处理中"
            detail = "原交付版本保持可用"

    return {
        "progress_percent": min(100, max(0, int(percent))),
        "progress_stage": progress_stage,
        "progress_label": label,
        "progress_detail": detail,
        "completed_shots": ready_shots,
        "total_shots": target_shots,
    }


def list_projects(db_factory, username, page=1, page_size=DEFAULT_PROJECT_PAGE_SIZE,
                  access=None):
    page = _validate_page(page, 1)
    page_size = _validate_page(page_size, DEFAULT_PROJECT_PAGE_SIZE, MAX_PROJECT_PAGE_SIZE)
    conn = _connection(db_factory)
    try:
        access = access if isinstance(access, dict) else {}
        board_id = str(access.get("board_id") or "")
        role = str(access.get("role") or "").lower()
        if board_id and role in {"owner", "editor", "viewer"}:
            where = "board_id=? AND deleted=0"
            params = (board_id,)
        else:
            where = "username=? AND board_id IS NULL AND deleted=0"
            params = (username,)
        total = int(conn.execute(
            "SELECT COUNT(*) FROM short_drama_projects WHERE " + where,
            params,
        ).fetchone()[0])
        rows = _dict_rows(
            conn,
            "SELECT * FROM short_drama_projects WHERE " + where +
            " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            params + (page_size, (page - 1) * page_size),
        )
        page_usage = _page_project_point_usage(conn, rows)
        page_progress = _page_project_progress(conn, [row["id"] for row in rows])
        for row in rows:
            row["revision"] = int(row["revision"])
            row["spent_points"] = page_usage[row["id"]]["spent_points"]
            row.update(_project_progress_summary(conn, row, page_progress))
        return {
            "items": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        }
    finally:
        conn.close()


def get_project(db_factory, username, project_id, access=None):
    owner = _project_username_for_access(db_factory, username, project_id, access)
    reconcile_project_character_references(db_factory, owner, project_id)
    conn = _connection(db_factory)
    try:
        return _project_detail(conn, owner, project_id)
    finally:
        conn.close()


def delete_project(db_factory, username, project_id, revision):
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("短剧项目 ID 无效")
    if type(revision) is not int:
        raise ValueError("项目版本无效")
    now = int(time.time())
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _has_unapplied_charged_job(conn, username, project_id.strip()):
            raise ProjectHasUnappliedJobs("项目存在尚未结束或退款的付费任务")
        cur = conn.execute(
            "UPDATE short_drama_projects SET deleted=1, revision=revision+1, updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND deleted=0",
            (now, project_id.strip(), username, revision),
        )
        if cur.rowcount != 1:
            _raise_cas_error(conn, username, project_id.strip())
        conn.commit()
        return {"id": project_id.strip(), "revision": revision + 1, "deleted": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finalize_live_action_project(db_factory, username, payload):
    """Move a fully confirmed live-action creation draft into the project list."""
    if not isinstance(payload, dict):
        raise ValueError("真人短剧确认创建请求无效")
    _validate_project_request(payload, {"project_id", "revision"})
    project_id = payload["project_id"].strip()
    revision = payload["revision"]
    now = int(time.time())
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT p.revision,p.stage,p.creation_status,i.roles_saved_at,"
            "i.source_text,i.character_contract_json,i.core_story_confirmed_at,"
            "i.character_contract_migration_json "
            "FROM short_drama_projects p "
            "JOIN short_drama_script_imports i ON i.project_id=p.id "
            "WHERE p.id=? AND p.username=? AND p.deleted=0 "
            "AND i.content_type='live_action' AND i.status='completed'",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("真人短剧草稿不存在")
        if int(project[0]) != revision:
            raise RevisionConflict("项目版本已变化，请刷新后重试")
        if project[2] == "formal":
            conn.rollback()
            return _project_detail(conn, username, project_id)
        if project[1] != "draft" or project[3] is None:
            raise ValueError("请先保存全部角色资料")
        if project[6] is None:
            raise ValueError("请先确认核心故事")
        migration = _json(project[7], {})
        if migration.get("required") or migration.get("character_keys"):
            raise ValueError("旧草稿角色标准图尚未完成背面全身图补充与重新确认")
        characters = conn.execute(
            "SELECT character_key,name,reference_locked FROM short_drama_characters "
            "WHERE project_id=? ORDER BY sort_order,id",
            (project_id,),
        ).fetchall()
        if not characters:
            raise ValueError("请先建立并保存角色资料")
        contract = _json(project[5], [])
        required_keys = _character_reference_required_keys(project[4], contract)
        if not contract:
            # Preserve the old safety rule for legacy drafts that have no role
            # contract from which importance can be determined.
            required_keys = {str(row[0]) for row in characters}
        unlocked = [
            str(row[1]) for row in characters
            if str(row[0]) in required_keys and not bool(row[2])
        ]
        if unlocked:
            raise ValueError("请先确认并锁定角色标准图：" + "、".join(unlocked))
        cur = conn.execute(
            "UPDATE short_drama_projects SET creation_status='formal',"
            "revision=revision+1,updated_at=? "
            "WHERE id=? AND username=? AND revision=? "
            "AND creation_status='draft' AND deleted=0",
            (now, project_id, username, revision),
        )
        if cur.rowcount != 1:
            _raise_cas_error(conn, username, project_id)
        conn.commit()
        return _project_detail(conn, username, project_id)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _character_reference_required_keys(source_text, character_contract):
    """Return roles whose identity must be locked before production begins."""
    source = str(source_text or "")
    required = set()
    for item in character_contract or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("character_key") or "").strip()
        name = str(item.get("name") or "").strip()
        role_type = str(item.get("role_type") or "support").strip().lower()
        if not key:
            continue
        if role_type == "main":
            required.add(key)
            continue
        if role_type == "crowd":
            continue
        occurrence_count = source.count(name) if name else 0
        has_dialogue = bool(name and re.search(
            r"%s\s*[：:]" % re.escape(name), source
        ))
        if occurrence_count >= 2 or has_dialogue:
            required.add(key)
    return required


def validate_scene_image_binding(
        db_factory, username, binding, quoted_cost=None, access=None):
    if not isinstance(binding, dict) or set(binding) != {"project_id", "scene_key"}:
        raise ValueError("场景图生成必须绑定 project_id 与 scene_key")
    project_id = str(binding.get("project_id") or "").strip()
    scene_key = str(binding.get("scene_key") or "").strip()
    if not project_id or not scene_key:
        raise ValueError("场景图生成缺少项目或场景标识")
    owner_username = _project_username_for_access(
        db_factory, username, project_id, access, write=True
    )
    conn = db_factory()
    try:
        project = conn.execute(
            "SELECT point_budget FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, owner_username),
        ).fetchone()
        from . import short_drama_asset_graph
        scene = next((item for item in short_drama_asset_graph.scene_workspace(
            db_factory, owner_username, project_id
        )["scenes"] if item.get("scene_key") == scene_key), None)
        if not project or not scene:
            raise LookupError("短剧场景不存在或不属于当前账号")
        if quoted_cost is not None and int(project[0] or 0) > 0:
            usage = _project_point_usage(conn, project_id)
            if (
                int(usage.get("spent_points") or 0)
                + int(usage.get("reserved_points") or 0)
                + int(quoted_cost) > int(project[0])
            ):
                raise PointBudgetExceeded("短剧项目点数预算不足，无法生成场景图")
    finally:
        conn.close()
    return {"project_id": project_id, "scene_key": scene_key}


_CORE_STORY_FIELDS = (
    "title", "logline", "setup", "development", "turning_point",
    "climax", "ending", "central_conflict", "theme", "preservation_notes",
)


def _normalize_live_action_core_story(value):
    if not isinstance(value, dict) or set(value) != set(_CORE_STORY_FIELDS):
        raise ValueError("核心故事字段不完整")
    normalized = {}
    for field in _CORE_STORY_FIELDS:
        text_value = str(value.get(field) or "").strip()
        limit = 120 if field == "title" else 1200
        if field != "preservation_notes" and not text_value:
            raise ValueError("请补全核心故事：" + field)
        if len(text_value) > limit:
            raise ValueError("核心故事内容过长：" + field)
        normalized[field] = text_value
    return normalized


def confirm_live_action_core_story(db_factory, username, payload):
    """Persist the user's editable core-story checkpoint before visual work."""
    if not isinstance(payload, dict) or set(payload) != {
            "project_id", "revision", "core_story"}:
        raise ValueError("核心故事确认请求无效")
    project_id = str(payload.get("project_id") or "").strip()
    revision = payload.get("revision")
    if not project_id or type(revision) is not int or revision < 1:
        raise ValueError("核心故事项目或版本无效")
    core_story = _normalize_live_action_core_story(payload.get("core_story"))
    now = int(time.time())
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT p.revision,p.stage,p.creation_status,i.roles_saved_at,"
            "i.character_contract_json FROM short_drama_projects p "
            "JOIN short_drama_script_imports i ON i.project_id=p.id "
            "WHERE p.id=? AND p.username=? AND p.deleted=0 "
            "AND i.content_type='live_action' AND i.status='completed'",
            (project_id, username),
        ).fetchone()
        if not row:
            raise LookupError("真人短剧草稿不存在")
        if int(row[0]) != revision:
            raise RevisionConflict("项目版本已变化，请刷新后重试")
        if row[2] != "draft" or row[1] != "draft":
            raise ValueError("当前项目不能修改核心故事")
        contract = _json(row[4], [])
        if not contract:
            raise ValueError("请先完成剧本人物识别")
        conn.execute(
            "UPDATE short_drama_script_imports SET core_story_json=?,"
            "core_story_confirmed_at=?,updated_at=? WHERE project_id=?",
            (
                json.dumps(core_story, ensure_ascii=False, separators=(",", ":")),
                now, now, project_id,
            ),
        )
        updated = conn.execute(
            "UPDATE short_drama_projects SET synopsis=?,revision=revision+1,"
            "updated_at=? WHERE id=? AND username=? AND revision=? "
            "AND creation_status='draft' AND deleted=0",
            (core_story["logline"], now, project_id, username, revision),
        )
        if updated.rowcount != 1:
            _raise_cas_error(conn, username, project_id)
        conn.commit()
        return _project_detail(conn, username, project_id)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def abandon_live_action_project(db_factory, username, payload, idempotency_key):
    """Idempotently abandon an unpaid, unreferenced live-action import draft."""
    if not isinstance(payload, dict):
        raise ValueError("真人短剧临时项目放弃请求无效")
    _validate_project_request(payload, {"project_id", "revision"})
    project_id = payload["project_id"].strip()
    revision = payload["revision"]
    if revision < 1:
        raise ValueError("项目版本无效")
    key = str(idempotency_key or "").strip()
    if not key:
        raise ScriptImportError(
            "idempotency_key_required", "缺少有效的 Idempotency-Key",
        )
    if len(key) > 160:
        raise ScriptImportError(
            "idempotency_key_invalid", "Idempotency-Key 长度不能超过 160 个字符",
        )
    request_hash = hashlib.sha256(json.dumps(
        {"project_id": project_id, "revision": revision},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    now = int(time.time())
    operation = "live_action_abandon"
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT project_id,request_hash FROM short_drama_project_requests "
            "WHERE username=? AND operation=? AND idempotency_key=?",
            (username, operation, key),
        ).fetchone()
        if existing:
            if str(existing[1]) != request_hash:
                raise ScriptImportError(
                    "idempotency_conflict",
                    "同一 Idempotency-Key 不能用于不同的真人短剧放弃请求",
                    409,
                )
            deleted = conn.execute(
                "SELECT revision,deleted FROM short_drama_projects "
                "WHERE id=? AND username=?",
                (existing[0], username),
            ).fetchone()
            if not deleted or int(deleted[1] or 0) != 1:
                raise ScriptImportError(
                    "idempotency_state_invalid", "真人短剧放弃结果不完整", 409,
                )
            conn.rollback()
            return {
                "id": str(existing[0]), "revision": int(deleted[0]),
                "deleted": True, "replayed": True,
            }

        project = conn.execute(
            "SELECT p.revision,p.stage,p.deleted,p.board_id,p.completion_id "
            "FROM short_drama_projects p "
            "JOIN short_drama_script_imports i ON i.project_id=p.id "
            "WHERE p.id=? AND p.username=? AND i.username=? "
            "AND i.content_type='live_action' AND i.status='completed'",
            (project_id, username, username),
        ).fetchone()
        if not project or int(project[2] or 0) != 0:
            raise LookupError("真人短剧临时项目不存在")
        if int(project[0]) != revision:
            raise RevisionConflict("项目版本已变化，请刷新后重试")
        if project[1] != "draft" or project[3] or project[4]:
            raise ScriptImportError(
                "live_action_abandon_blocked", "仅可放弃未进入制作的真人短剧临时项目", 409,
            )
        if _has_unapplied_charged_job(conn, username, project_id):
            raise ProjectHasUnappliedJobs("项目存在尚未结束或退款的付费任务")
        if _project_point_usage(conn, project_id)["spent_points"] > 0:
            raise ScriptImportError(
                "live_action_abandon_blocked", "真人短剧临时项目已有点数活动，不能放弃", 409,
            )
        reference = conn.execute(
            "SELECT 1 FROM short_drama_characters WHERE project_id=? AND ("
            "reference_job_id IS NOT NULL OR reference_file<>'' OR reference_url<>'' "
            "OR reference_version>0 OR reference_locked=1) LIMIT 1",
            (project_id,),
        ).fetchone()
        if reference:
            raise ScriptImportError(
                "live_action_abandon_blocked", "真人短剧临时项目已有角色标准图活动，不能放弃", 409,
            )
        conn.execute(
            "INSERT INTO short_drama_project_requests "
            "(id,username,operation,idempotency_key,request_hash,project_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), username, operation, key, request_hash,
             project_id, now, now),
        )
        updated = conn.execute(
            "UPDATE short_drama_projects SET deleted=1,revision=revision+1,updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND deleted=0",
            (now, project_id, username, revision),
        )
        if updated.rowcount != 1:
            _raise_cas_error(conn, username, project_id)
        conn.commit()
        return {
            "id": project_id, "revision": revision + 1,
            "deleted": True, "replayed": False,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _planning_metadata(payload, result=None):
    if not isinstance(payload, dict) or payload.get("format") != "short_drama":
        raise ValueError("规划任务缺少项目绑定")
    settings = validate_planning_payload(payload)
    if "project_id" not in settings or "project_revision" not in settings:
        raise ValueError("规划任务缺少项目绑定")
    metadata = {
        "project_id": settings["project_id"],
        "project_revision": settings["project_revision"],
        "prompt": settings["prompt"],
        "ratio": settings["ratio"],
        "target_duration": settings["target_duration"],
        "shot_count": settings["shot_count"],
        "style": settings["style"],
        "platform": settings["platform"],
    }
    if result is None:
        return metadata
    if not isinstance(result, dict) or result.get("mode") != "short_drama":
        raise ValueError("规划任务结果不是短剧规划")
    result_settings = result.get("settings")
    expected_snapshot = {
        "ratio": metadata["ratio"],
        "target_duration": metadata["target_duration"],
        "shot_count": metadata["shot_count"],
    }
    if result.get("project_id") != metadata["project_id"]:
        raise ValueError("规划任务项目绑定不一致")
    if result.get("project_revision") != metadata["project_revision"]:
        raise ValueError("规划任务项目版本不一致")
    if result_settings != expected_snapshot:
        raise ValueError("规划任务设置快照不一致")
    if (result.get("prompt") != metadata["prompt"] or
            result.get("dur") != "%ss" % metadata["target_duration"] or
            result.get("ratio") != metadata["ratio"] or
            result.get("shot_count") != metadata["shot_count"]):
        raise ValueError("规划任务结果元数据不一致")
    return metadata


def _job_payload(row):
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        raise ValueError("规划任务请求无效")
    if not isinstance(payload, dict):
        raise ValueError("规划任务请求无效")
    return payload


def check_planning_budget(db_factory, username, project_id, quoted_cost, access=None):
    if type(quoted_cost) is not int or quoted_cost < 0:
        raise ValueError("短剧策划报价无效")
    owner = _project_username_for_access(
        db_factory, username, project_id, access, write=True)
    conn = _connection(db_factory)
    try:
        project = conn.execute(
            "SELECT point_budget, stage FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, owner),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        point_budget, stage = project
        if stage != "draft":
            raise ValueError("当前短剧阶段不能重新生成策划")
        point_budget = int(point_budget)
        if point_budget == 0:
            return
        usage = _project_point_usage(conn, project_id)
        spent_points = usage["spent_points"]
        reserved = usage["reserved_points"]
        if spent_points + reserved + quoted_cost > point_budget:
            raise PointBudgetExceeded(
                "短剧点数预算不足：已用 %d 点、已预留 %d 点、本次 %d 点、预算 %d 点" %
                (spent_points, reserved, quoted_cost, point_budget)
            )
    finally:
        conn.close()


def prepare_paid_planning_submission(db_factory, username, payload, cost_of, access=None):
    """Revalidate the bound request and its budget while core holds its submission lock."""
    cleaned = validate_planning_submission(db_factory, username, payload, access)
    recovered = find_recoverable_planning_job(
        db_factory, username, cleaned["project_id"], planning_payload=cleaned, access=access
    )
    if recovered:
        return cleaned, None, recovered
    cost = cost_of("copy", cleaned)
    check_planning_budget(db_factory, username, cleaned["project_id"], cost, access)
    return cleaned, cost, None


def find_recoverable_planning_job(db_factory, username, project_id, planning_payload=None,
                                  access=None):
    project = get_project(db_factory, username, project_id, access)
    if project["stage"] != "draft":
        return None
    requested = _planning_metadata(planning_payload) if planning_payload is not None else {
        "prompt": project["synopsis"], "ratio": project["ratio"],
        "target_duration": project["target_duration"], "shot_count": project["shot_count"],
        "style": project["visual_style"], "platform": project["target_platform"],
    }
    conn = _connection(db_factory)
    try:
        applied_ids = {
            int(row[0]) for row in conn.execute(
                "SELECT job_id FROM short_drama_applied_jobs WHERE project_id=? AND username=?",
                (project_id, username),
            ).fetchall()
        }
        rows = conn.execute(
            "SELECT id, cost, status, payload, result FROM jobs "
            "WHERE username=? AND kind='copy' AND status IN ('pending','running','done') "
            "ORDER BY id DESC",
            (username,),
        ).fetchall()
        for row in rows:
            if int(row["id"]) in applied_ids:
                continue
            try:
                payload = json.loads(row["payload"] or "{}")
                result = json.loads(row["result"] or "{}") if row["status"] == "done" else None
                metadata = _planning_metadata(payload, result)
            except (TypeError, ValueError):
                continue
            if metadata["project_id"] != project_id:
                continue
            if any(
                    metadata[key] != requested[key]
                    for key in ("prompt", "ratio", "target_duration", "shot_count", "style", "platform")):
                continue
            return {
                "job_id": int(row["id"]), "cost": int(row["cost"] or 0),
                "status": row["status"], "project_revision": metadata["project_revision"],
            }
        return None
    finally:
        conn.close()


def _raise_cas_error(conn, username, project_id):
    exists = conn.execute(
        "SELECT 1 FROM short_drama_projects WHERE id=? AND username=? AND deleted=0",
        (project_id, username),
    ).fetchone()
    if not exists:
        raise LookupError("短剧项目不存在")
    raise RevisionConflict("项目已在其他页面更新，请刷新后重试")


def update_project(db_factory, username, project_id, revision, patch, avatar_lookup=None):
    if not isinstance(patch, dict):
        raise ValueError("短剧更新内容必须是对象")
    original_patch = dict(patch)
    content_keys = set(original_patch) & CONTENT_KEYS
    if content_keys:
        character_contract = original_patch.get("character_contract")
        allowed_character_update = (
            content_keys == {"characters"}
            and set(original_patch).issubset({"characters", "character_contract"})
        )
        if len(content_keys) != 1 or (
                len(original_patch) != 1 and not allowed_character_update):
            raise ValueError("每次只能更新一个短剧内容分区")
        key = next(iter(content_keys))
        if key == "characters":
            return update_characters(
                db_factory, username, project_id, revision, original_patch[key],
                avatar_lookup, character_contract=character_contract,
            )
        if key == "script":
            return update_script(db_factory, username, project_id, revision, original_patch[key])
        return update_shots(db_factory, username, project_id, revision, original_patch[key])
    allowed = {"title", "synopsis", "genre", "ratio", "target_duration", "shot_count", "visual_style", "target_platform", "point_budget"}
    unknown = set(original_patch) - allowed
    if unknown:
        raise ValueError("不支持的短剧字段")
    data = validate_project_payload(original_patch, partial=True)
    changes = {key: data[key] for key in original_patch if key in data}
    if "target_platform" in changes:
        changes["target_platform"] = _text(changes["target_platform"], 80)
    if not changes:
        raise ValueError("请提供需要更新的字段")
    now = int(time.time())
    conn = _connection(db_factory)
    try:
        current = conn.execute(
            "SELECT title, stage, target_duration, shot_count FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not current:
            raise LookupError("短剧项目不存在")
        if current[1] != "draft" and set(changes) & PLANNING_SPEC_FIELDS:
            raise ValueError("策划生成后不能修改会使下游失效的项目设置")
        if set(changes) & {"target_duration", "shot_count"}:
            _validate_planning_limits(
                changes.get("target_duration", current[2]),
                changes.get("shot_count", current[3]),
            )
        title = changes.get("title", current[0])
        assignments = ["title=?"]
        values = [title]
        for key, value in changes.items():
            if key != "title":
                assignments.append(key + "=?")
                values.append(value)
        assignments.extend(["revision=revision+1", "updated_at=?"])
        values.extend([now, project_id, username, revision])
        cur = conn.execute(
            "UPDATE short_drama_projects SET " + ", ".join(assignments) +
            " WHERE id=? AND username=? AND revision=? AND deleted=0",
            values,
        )
        if cur.rowcount != 1:
            _raise_cas_error(conn, username, project_id)
        conn.commit()
        return _project_detail(conn, username, project_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_MISSING = object()


def _strict_text(item, names, limit, *, required=False, default=""):
    value = _MISSING
    for name in names:
        if name in item:
            value = item[name]
            break
    if value is _MISSING:
        if required:
            raise ValueError("短剧内容缺少字段: " + names[0])
        return default
    if not isinstance(value, str):
        raise ValueError("短剧内容字段无效: " + names[0])
    value = value.strip()[:limit]
    if required and not value:
        raise ValueError("短剧内容字段无效: " + names[0])
    return value


def _optional_key(value, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("短剧内容字段无效: " + field)
    return value.strip()[:80] or None


def _normalize_characters(characters, *, require_complete=False,
                          allow_empty_wardrobe=False):
    if not isinstance(characters, list):
        raise ValueError("角色数据必须是数组")
    if len(characters) > MAX_CHARACTERS_PER_PROJECT:
        raise ValueError("短剧角色数量不能超过 %d 个" % MAX_CHARACTERS_PER_PROJECT)
    normalized = []
    for index, character in enumerate(characters):
        if not isinstance(character, dict):
            raise ValueError("角色数据无效")
        if require_complete and "source_type" not in character:
            raise ValueError("短剧内容缺少字段: source_type")
        source_type = character.get("source_type", "ai_character")
        if not isinstance(source_type, str) or source_type not in {"cinematic_avatar", "ai_character"}:
            raise ValueError("角色数据无效")
        if require_complete and "voice_settings" not in character:
            raise ValueError("短剧内容缺少字段: voice_settings")
        voice_settings = character.get("voice_settings", {})
        if not isinstance(voice_settings, dict):
            raise ValueError("角色语音设置必须是对象")
        avatar_id = _optional_key(character.get("avatar_id"), "avatar_id")
        if source_type == "ai_character":
            avatar_id = None
        if require_complete and source_type == "cinematic_avatar" and not avatar_id:
            raise AvatarBindingError(
                "avatar_required", "请选择电影化形象"
            )
        normalized.append({
            "character_key": _strict_text(
                character, ("character_key", "key"), 80, required=True
            ),
            "name": _strict_text(character, ("name",), 80, required=True),
            "identity_text": _strict_text(
                character, ("identity_text", "identity"), 2000, required=require_complete
            ),
            "personality": _strict_text(
                character, ("personality",), 2000, required=require_complete
            ),
            "source_type": source_type,
            "avatar_id": avatar_id,
            "appearance_prompt": _strict_text(
                character, ("appearance_prompt",), 4000, required=require_complete
            ),
            "wardrobe_prompt": _strict_text(
                character, ("wardrobe_prompt",), 4000,
                required=require_complete and not allow_empty_wardrobe
            ),
            "reference_job_id": (
                int(character["reference_job_id"])
                if character.get("reference_job_id") not in (None, "") else None
            ),
            "reference_file": _text(character.get("reference_file"), 1000),
            "reference_url": _text(character.get("reference_url"), 2000),
            "reference_version": max(0, int(character.get("reference_version") or 0)),
            "reference_locked": bool(character.get("reference_locked")),
            "reference_source": _text(character.get("reference_source"), 40),
            "reference_asset_id": _text(character.get("reference_asset_id"), 160),
            "reference_name": _text(character.get("reference_name"), 240),
            "reference_profile_stale": bool(character.get("reference_profile_stale")),
            "pending_reference_job_id": (
                int(character["pending_reference_job_id"])
                if character.get("pending_reference_job_id") not in (None, "") else None
            ),
            "pending_reference_file": _text(character.get("pending_reference_file"), 1000),
            "pending_reference_url": _text(character.get("pending_reference_url"), 2000),
            "pending_reference_version": max(0, int(character.get("pending_reference_version") or 0)),
            "pending_reference_source": _text(character.get("pending_reference_source"), 40),
            "pending_reference_asset_id": _text(character.get("pending_reference_asset_id"), 160),
            "pending_reference_name": _text(character.get("pending_reference_name"), 240),
            "voice_key": _optional_key(character.get("voice_key"), "voice_key"),
            "voice_settings": voice_settings,
            "sort_order": index,
        })
    keys = [item["character_key"] for item in normalized]
    if len(set(keys)) != len(keys):
        raise ValueError("角色标识不能重复")
    return normalized


def _dialogue_line_id(line):
    """Return a persisted id; update_script owns all new-id allocation."""
    existing = str(line.get("id") or "").strip()
    if not existing or existing.startswith("tmp_"):
        raise ValueError("新增台词必须由服务端分配标识")
    if len(existing) > 80:
        raise ValueError("台词标识过长")
    return existing


def _normalize_script(script, character_keys, *, character_names=None, default_title=None,
                      require_complete=True):
    if not isinstance(script, dict):
        raise ValueError("剧本数据必须是对象")
    title = _strict_text(script, ("title",), 80, required=require_complete)
    if not title:
        if default_title is None or not isinstance(default_title, str):
            raise ValueError("剧本标题无效")
        title = default_title.strip()[:80] or "未命名剧本"
    dialogue_lines = script.get("dialogue_lines", _MISSING)
    if dialogue_lines is _MISSING or not isinstance(dialogue_lines, list):
        raise ValueError("剧本台词数据无效")
    if len(dialogue_lines) > MAX_DIALOGUE_LINES_PER_SCRIPT:
        raise ValueError("剧本台词数量不能超过 %d 条" % MAX_DIALOGUE_LINES_PER_SCRIPT)
    normalized_lines = []
    for line in dialogue_lines:
        if not isinstance(line, dict):
            raise ValueError("台词数据无效")
        character_key = _strict_text(line, ("character_key",), 80, required=True)
        if character_key not in character_keys and character_key != "narrator":
            raise ValueError("台词引用了不存在的角色")
        voice_overrides = line.get("voice_overrides", {})
        if not isinstance(voice_overrides, dict):
            raise ValueError("台词语音覆盖设置必须是 JSON 对象")
        speaker_name = str(
            (character_names or {}).get(character_key)
            or line.get("speaker_name_snapshot")
            or ("旁白" if character_key == "narrator" else character_key)
        ).strip()
        normalized_lines.append({
            "id": _dialogue_line_id(line),
            "character_key": character_key,
            "speaker_name_snapshot": speaker_name[:80],
            "text": _strict_text(line, ("text",), 4000, required=True),
            "subtitle_enabled": line.get("subtitle_enabled") is not False,
            "voice_overrides": voice_overrides,
        })
    ids = [line["id"] for line in normalized_lines]
    if len(set(ids)) != len(ids):
        raise ValueError("台词标识不能重复")
    required = require_complete
    return {
        "title": title,
        "logline": _strict_text(script, ("logline",), 4000, required=required),
        "hook": _strict_text(script, ("hook",), 4000, required=required),
        "conflict_text": _strict_text(
            script, ("conflict_text", "conflict"), 4000, required=required
        ),
        "turn_text": _strict_text(script, ("turn_text", "turn"), 4000, required=required),
        "ending": _strict_text(script, ("ending",), 4000, required=required),
        "dialogue_lines": normalized_lines,
    }


def _dialogue_retry_signature(line):
    character_key = line.get("character_key")
    text = line.get("text")
    voice_overrides = line.get("voice_overrides", {})
    if not isinstance(character_key, str) or not isinstance(text, str):
        raise ValueError("台词客户端请求标识对应内容无效")
    if not isinstance(voice_overrides, dict):
        raise ValueError("台词语音覆盖设置必须是 JSON 对象")
    return {
        "character_key": character_key.strip()[:80],
        "text": text.strip()[:4000],
        "subtitle_enabled": line.get("subtitle_enabled") is not False,
        "voice_overrides": voice_overrides,
    }


def _prepare_dialogue_line_ids(conn, project_id, current_script, submitted_script):
    """Validate immutable ids and allocate project-scoped ids for new lines."""
    if not isinstance(submitted_script, dict):
        return submitted_script
    submitted_lines = submitted_script.get("dialogue_lines")
    if not isinstance(submitted_lines, list):
        return submitted_script

    current_ids = {
        str(line.get("id") or "").strip()
        for line in (current_script.get("dialogue_lines") or [])
        if isinstance(line, dict) and str(line.get("id") or "").strip()
    }
    prepared = copy.deepcopy(submitted_script)
    now = int(time.time())
    token_indexes = set()

    for index, line in enumerate(prepared["dialogue_lines"]):
        if not isinstance(line, dict):
            continue
        line_id = str(line.get("id") or "").strip()
        token_value = line.get("client_token", _MISSING)
        client_token = token_value.strip() if isinstance(token_value, str) else ""
        if line_id:
            if line_id.startswith("tmp_"):
                raise ValueError("新增台词不能提交临时标识")
            if line_id not in current_ids:
                raise ValueError("台词标识不可修改")
            if client_token:
                raise ValueError("已有台词不能提交 client_token")
            line["id"] = line_id
            line.pop("client_token", None)
            continue

        if not client_token or len(client_token) > 120:
            raise ValueError("新增台词缺少有效的客户端请求标识")
        token_row = conn.execute(
            "SELECT line_id,state FROM short_drama_dialogue_tokens "
            "WHERE project_id=? AND client_token=?",
            (project_id, client_token),
        ).fetchone()
        if token_row:
            token_line_id, token_state = token_row
            if token_state != "active" or token_line_id not in current_ids:
                raise ValueError("该台词客户端请求标识已被使用")
            line_id = token_line_id
        else:
            line_id = "line_" + uuid.uuid5(
                uuid.NAMESPACE_URL,
                "short-drama-dialogue:%s:%s" % (project_id, client_token),
            ).hex
            conn.execute(
                "INSERT INTO short_drama_dialogue_tokens "
                "(project_id,client_token,line_id,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (project_id, client_token, line_id, "active", now, now),
            )
        line["id"] = line_id
        line.pop("client_token", None)
        token_indexes.add(index)

    official_by_id = {}
    for index, line in enumerate(prepared["dialogue_lines"]):
        if index in token_indexes or not isinstance(line, dict):
            continue
        line_id = str(line.get("id") or "").strip()
        if line_id:
            official_by_id[line_id] = line
    replay_indexes = set()
    for index in token_indexes:
        line = prepared["dialogue_lines"][index]
        official = official_by_id.get(str(line.get("id") or "").strip())
        if not official:
            continue
        if _dialogue_retry_signature(line) != _dialogue_retry_signature(official):
            raise ValueError("台词客户端请求标识与已保存内容冲突")
        replay_indexes.add(index)
    if replay_indexes:
        prepared["dialogue_lines"] = [
            line for index, line in enumerate(prepared["dialogue_lines"])
            if index not in replay_indexes
        ]
    return prepared


def _retire_removed_dialogue_tokens(conn, project_id, dialogue_ids):
    params = [int(time.time()), project_id]
    query = (
        "UPDATE short_drama_dialogue_tokens SET state='retired',updated_at=? "
        "WHERE project_id=? AND state='active'"
    )
    if dialogue_ids:
        query += " AND line_id NOT IN (%s)" % ",".join("?" for _ in dialogue_ids)
        params.extend(sorted(dialogue_ids))
    conn.execute(query, tuple(params))


def _normalize_shots(shots, character_keys, dialogue_ids, *, expected_count=None,
                     target_duration=None):
    if not isinstance(shots, list):
        raise ValueError("分镜数据必须是数组")
    if len(shots) not in SHOT_COUNTS or (expected_count is not None and len(shots) != expected_count):
        raise ValueError("分镜数量必须等于设定数量且为 6–10 个")
    normalized = []
    for index, shot in enumerate(shots):
        if not isinstance(shot, dict):
            raise ValueError("分镜数据无效")
        duration = shot.get("duration")
        if type(duration) is not int or duration not in (5, 10):
            raise ValueError("分镜时长必须为 5 或 10 秒")
        shot_character_keys = _key_list(shot.get("character_keys"), "character_keys")
        if set(shot_character_keys) - set(character_keys):
            raise ValueError("分镜引用了不存在的角色")
        shot_dialogue_ids = _key_list(shot.get("dialogue_line_ids"), "dialogue_line_ids")
        if set(shot_dialogue_ids) - set(dialogue_ids):
            raise ValueError("分镜引用了不存在的台词")
        normalized.append({
            "shot_key": _strict_text(shot, ("shot_key", "key"), 80, required=True),
            "sort_order": index,
            "duration": duration,
            "scene_description": _strict_text(
                shot, ("scene_description",), 4000, required=True
            ),
            "camera_description": _strict_text(
                shot, ("camera_description",), 4000, required=True
            ),
            "character_keys": shot_character_keys,
            "dialogue_line_ids": shot_dialogue_ids,
            "image_prompt": _strict_text(shot, ("image_prompt",), 8000, required=True),
            "video_prompt": _strict_text(shot, ("video_prompt",), 8000, required=True),
        })
    keys = [shot["shot_key"] for shot in normalized]
    if len(set(keys)) != len(keys):
        raise ValueError("分镜标识不能重复")
    if target_duration is not None and not short_drama_duration.contains(
            target_duration, sum(shot["duration"] for shot in normalized)):
        raise ValueError("分镜总时长必须位于所选成片时长区间")
    return normalized


def _validate_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError("短剧规划无效")
    characters = _normalize_characters(plan.get("characters", []))
    character_keys = {character["character_key"] for character in characters}
    character_names = {
        character["character_key"]: character["name"] for character in characters
    }
    script = _normalize_script(
        plan.get("script", plan.get("script_version", {})), character_keys,
        character_names=character_names,
        default_title=plan.get("title") or "未命名剧本", require_complete=False,
    )
    dialogue_ids = {line["id"] for line in script["dialogue_lines"]}
    shots = _normalize_shots(plan.get("shots", []), character_keys, dialogue_ids)
    return characters, script, shots


def _begin_content_update(conn, username, project_id, revision, required_stage):
    if type(revision) is not int:
        raise ValueError("revision 必须是整数")
    conn.execute("BEGIN IMMEDIATE")
    cursor = conn.execute(
        "SELECT * FROM short_drama_projects "
        "WHERE id=? AND username=? AND deleted=0",
        (project_id, username),
    )
    values = cursor.fetchone()
    if not values:
        raise LookupError("短剧项目不存在")
    row = dict(zip((column[0] for column in cursor.description), values))
    if row["revision"] != revision:
        raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
    if row["stage"] != required_stage:
        raise ValueError("当前阶段不能修改该内容")
    return row


def _insert_characters(conn, project_id, characters):
    for character in characters:
        conn.execute(
            "INSERT INTO short_drama_characters "
            "(id, project_id, character_key, name, identity_text, personality, source_type, avatar_id, "
            "appearance_prompt, wardrobe_prompt, reference_job_id, reference_file, reference_url, "
            "reference_version, reference_locked, reference_source, reference_asset_id, "
            "reference_name, reference_profile_stale,pending_reference_job_id,"
            "pending_reference_file,pending_reference_url,pending_reference_version,"
            "pending_reference_source,pending_reference_asset_id,pending_reference_name,"
            "voice_key, voice_settings_json, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), project_id, character["character_key"], character["name"],
             character["identity_text"], character["personality"], character["source_type"],
             character["avatar_id"], character["appearance_prompt"], character["wardrobe_prompt"],
             character["reference_job_id"], character["reference_file"], character["reference_url"],
             character["reference_version"], int(character["reference_locked"]),
             character.get("reference_source") or "", character.get("reference_asset_id") or "",
             character.get("reference_name") or "",
             int(character.get("reference_profile_stale") or 0),
             character.get("pending_reference_job_id"),
             character.get("pending_reference_file") or "",
             character.get("pending_reference_url") or "",
             int(character.get("pending_reference_version") or 0),
             character.get("pending_reference_source") or "",
             character.get("pending_reference_asset_id") or "",
             character.get("pending_reference_name") or "",
             character["voice_key"], _json_text(character["voice_settings"], {}),
             character["sort_order"]),
        )


def _append_script(conn, project_id, script, now):
    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM short_drama_scripts WHERE project_id=?",
        (project_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO short_drama_scripts "
        "(id, project_id, version, title, logline, hook, conflict_text, turn_text, ending, "
        "dialogue_lines_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), project_id, version, script["title"], script["logline"],
         script["hook"], script["conflict_text"], script["turn_text"], script["ending"],
         _json_text(script["dialogue_lines"], []), now),
    )
    return version


def _insert_shots(conn, project_id, script_version, shots):
    for shot in shots:
        conn.execute(
            "INSERT INTO short_drama_shots "
            "(id, project_id, script_version, shot_key, sort_order, duration, scene_description, "
            "camera_description, character_keys_json, dialogue_line_ids_json, image_prompt, "
            "video_prompt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), project_id, script_version, shot["shot_key"], shot["sort_order"],
             shot["duration"], shot["scene_description"], shot["camera_description"],
             _json_text(shot["character_keys"], []), _json_text(shot["dialogue_line_ids"], []),
             shot["image_prompt"], shot["video_prompt"]),
        )


def _cas_content_update(conn, username, project_id, revision, required_stage):
    cur = conn.execute(
        "UPDATE short_drama_projects SET revision=revision+1, updated_at=? "
        "WHERE id=? AND username=? AND revision=? AND stage=? AND deleted=0",
        (int(time.time()), project_id, username, revision, required_stage),
    )
    if cur.rowcount != 1:
        _raise_cas_error(conn, username, project_id)


def _current_content_bundle(project, *, characters=_MISSING, script=_MISSING, shots=_MISSING,
                            prune_character_refs=False, prune_dialogue_refs=False):
    normalized_characters = _normalize_characters(
        project["characters"] if characters is _MISSING else characters, require_complete=True
    )
    character_keys = {character["character_key"] for character in normalized_characters}
    character_names = {
        character["character_key"]: character["name"] for character in normalized_characters
    }
    current_scripts = project["script_versions"]
    if not current_scripts:
        raise ValueError("短剧项目缺少剧本")
    normalized_script = _normalize_script(
        current_scripts[-1] if script is _MISSING else script, character_keys,
        character_names=character_names,
        require_complete=True,
    )
    dialogue_ids = {line["id"] for line in normalized_script["dialogue_lines"]}
    candidate_shots = project["shots"] if shots is _MISSING else shots
    if prune_character_refs or prune_dialogue_refs:
        candidate_shots = [dict(shot) for shot in candidate_shots]
        if prune_character_refs:
            for shot in candidate_shots:
                keys = shot.get("character_keys")
                if not isinstance(keys, list):
                    raise ValueError("分镜关联数据无效")
                shot["character_keys"] = [key for key in keys if key in character_keys]
        if prune_dialogue_refs:
            for shot in candidate_shots:
                ids = shot.get("dialogue_line_ids")
                if not isinstance(ids, list):
                    raise ValueError("分镜关联数据无效")
                shot["dialogue_line_ids"] = [line_id for line_id in ids if line_id in dialogue_ids]
    normalized_shots = _normalize_shots(
        candidate_shots, character_keys, dialogue_ids,
        expected_count=project["shot_count"], target_duration=project["target_duration"],
    )
    return normalized_characters, normalized_script, normalized_shots


def _validate_owned_avatars(username, characters, avatar_lookup):
    for character in characters:
        if character["source_type"] != "cinematic_avatar":
            continue
        if not character.get("avatar_id") or not callable(avatar_lookup):
            raise AvatarBindingError(
                "avatar_required", "请选择电影化形象"
            )
        try:
            avatar = avatar_lookup(username, character["avatar_id"])
        except Exception:
            raise AvatarBindingError(
                "avatar_not_found",
                "该电影化形象不存在，请刷新后重新选择",
            )
        if not isinstance(avatar, dict):
            raise AvatarBindingError(
                "avatar_not_found",
                "该电影化形象不存在，请刷新后重新选择",
            )
        if avatar.get("username") != username:
            raise AvatarBindingError(
                "avatar_forbidden",
                "当前账号无权使用该电影化形象",
                status=403,
            )
        if (
            str(avatar.get("status") or "") != "ready"
            or not str(avatar.get("provider_avatar_id") or "").strip()
        ):
            raise AvatarBindingError(
                "avatar_not_ready",
                "该电影化形象仍在处理中，请稍后刷新",
            )
        if avatar.get("image_file"):
            character["reference_file"] = str(avatar["image_file"])
            character["reference_url"] = str(avatar.get("image_url") or "")
            character["reference_version"] = max(1, int(character.get("reference_version") or 0))
            character["reference_locked"] = True


def _safe_avatar_candidates(avatar_list, owner_username, limit=120):
    if not callable(avatar_list):
        raise ValueError("电影化形象库暂不可用")
    items = []
    for avatar in avatar_list(owner_username, limit):
        if (
            not isinstance(avatar, dict)
            or str(avatar.get("status") or "") != "ready"
            or not str(avatar.get("provider_avatar_id") or "").strip()
        ):
            continue
        items.append({
            key: avatar.get(key)
            for key in (
                "id", "name", "image_url", "status",
                "created_at", "updated_at",
            )
        })
    return items


def _resolve_ai_character_references(conn, username, characters):
    from . import image as image_domain
    for character in characters:
        # Existing references were already validated when they were selected or
        # generated. Profile-only edits copy those server-owned fields below;
        # do not re-treat them as untrusted client submissions.
        if character.pop("_preserve_reference", False):
            continue
        if character["source_type"] != "ai_character":
            continue
        if character.get("reference_source") in {"asset", "upload"}:
            trusted_file = (
                image_domain._trusted_short_drama_file(character.get("reference_file"))
                or image_domain._trusted_short_drama_file(
                    character.get("reference_url"), file_url=True
                )
            )
            if not trusted_file or not character.get("reference_url"):
                raise ValueError("角色标准图资产已失效")
            character["reference_file"] = trusted_file
            continue
        job_id = character.get("reference_job_id")
        if not job_id:
            character.update({
                "reference_file": "", "reference_url": "",
                "reference_version": 0, "reference_locked": False,
                "reference_source": "", "reference_asset_id": "",
                "reference_name": "",
            })
            continue
        row = conn.execute(
            "SELECT status,payload,result FROM jobs "
            "WHERE id=? AND username=? AND kind='image'",
            (job_id, username),
        ).fetchone()
        if not row or row[0] != "done":
            raise ValueError("AI 角色标准图任务不存在、未完成或不属于当前用户")
        payload = _json(row[1], {})
        result = _json(row[2], {})
        if (str(payload.get("provider") or "").lower() != "banana"
                or str(payload.get("model") or "nb2").lower() != "nb2"):
            raise ValueError("AI 角色标准图必须由 Nano Banana 2 生成")
        files = result.get("files") or []
        urls = result.get("urls") or []
        file_value = result.get("file") or (files[0] if files else "")
        url_value = result.get("url") or (urls[0] if urls else "")
        trusted_file = image_domain._trusted_short_drama_file(file_value)
        if not trusted_file:
            trusted_file = image_domain._trusted_short_drama_file(url_value, file_url=True)
        if not trusted_file or not isinstance(url_value, str) or not url_value:
            raise ValueError("AI 角色标准图结果无效")
        changed = trusted_file != character.get("reference_file")
        character["reference_file"] = trusted_file
        character["reference_url"] = url_value
        character["reference_version"] = max(
            1, int(character.get("reference_version") or 0) + (1 if changed else 0)
        )
        # A completed job only proves that the preview is valid. Locking is a
        # separate, explicit user action handled by confirm_character_reference.
        character["reference_locked"] = bool(character.get("reference_locked"))
        character["reference_source"] = "ai_generation"
        character["reference_asset_id"] = ""
        character["reference_name"] = ""


_CHARACTER_REFERENCE_STAGES = {
    "characters_review", "script_review", "storyboard_review", "stills_review",
}
_CHARACTER_SNAPSHOT_FIELDS = (
    "character_key", "name", "identity_text", "personality",
    "source_type", "avatar_id", "appearance_prompt", "wardrobe_prompt",
)


def _character_snapshot_hash(character):
    snapshot = {
        # Old accepted attempts may predate newly-added optional character
        # fields. Missing keys must hash as empty values so recovery remains
        # backward compatible instead of crashing.
        field: str(character.get(field) or "").strip()
        for field in _CHARACTER_SNAPSHOT_FIELDS
    }
    canonical = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _live_action_role_setup(conn, project_id):
    return conn.execute(
        "SELECT roles_saved_at,core_story_confirmed_at "
        "FROM short_drama_script_imports "
        "WHERE project_id=? AND content_type='live_action' "
        "AND status='completed' LIMIT 1",
        (project_id,),
    ).fetchone()


def _draft_character_profile_stage_allowed(conn, project_id):
    if _live_action_role_setup(conn, project_id):
        return True
    conversation = conn.execute(
        "SELECT state FROM short_drama_conversations WHERE project_id=?",
        (project_id,),
    ).fetchone()
    if conversation and conversation[0] == "direction_review":
        return True
    return bool(conn.execute(
        "SELECT 1 FROM short_drama_conversations conversation "
        "JOIN short_drama_script_snapshots snapshot "
        "ON snapshot.id=conversation.locked_version_id "
        "WHERE conversation.project_id=? AND conversation.state='script_locked' "
        "AND snapshot.project_id=? AND snapshot.status='locked'",
        (project_id, project_id),
    ).fetchone())


def _character_reference_stage_allowed(conn, project_id, stage):
    if stage in _CHARACTER_REFERENCE_STAGES:
        return True
    if stage != "draft":
        return False
    live_action_setup = _live_action_role_setup(conn, project_id)
    if live_action_setup:
        # The imported contract only seeds the form. Reference generation is
        # enabled after role profiles and the core story are both confirmed.
        return live_action_setup[0] is not None and live_action_setup[1] is not None
    return _draft_character_profile_stage_allowed(conn, project_id)


def _character_reference_prompt(character):
    lines = [
        "生成一张电影写实短剧角色标准图（三视图角色设定板）。",
        "同一个人物横向排列为：正面全身、侧面全身、背面全身。",
        "三幅视图均须从头顶到脚底完整入镜，人物等比例、同尺寸、自然站立，不要半身、不要裁切。",
        "三幅视图必须保持同一张脸、同一年龄、同一发型、同一体型、同一套服装、颜色和配饰。",
        "干净中性浅色背景，均匀棚拍光线，自然站姿，清晰五官，完整身体比例。",
        "角色名称：" + str(character["name"]),
    ]
    optional = (
        ("身份", character.get("identity_text")),
        ("性格与气质", character.get("personality")),
        ("性别、年龄、脸型、发型、发色、身高体型及外貌", character.get("appearance_prompt")),
        ("固定服装、固定颜色与配饰", character.get("wardrobe_prompt")),
    )
    lines.extend(label + "：" + str(value).strip() for label, value in optional if str(value or "").strip())
    lines.append("禁止文字、标签、水印、额外人物、服装变化、脸部变化、遮挡脸部或夸张动作。")
    return "\n".join(lines)


def _normalize_character_reference_request(body):
    expected = {"project_id", "revision", "character_key"}
    if not isinstance(body, dict) or set(body) != expected:
        raise ValueError("角色标准图请求字段不正确")
    project_id = str(body.get("project_id") or "").strip()
    character_key = str(body.get("character_key") or "").strip()
    revision = body.get("revision")
    if not project_id or not character_key or type(revision) is not int or revision < 1:
        raise ValueError("角色标准图项目、角色或版本无效")
    return {
        "project_id": project_id,
        "revision": revision,
        "character_key": character_key,
    }


def _character_reference_row(conn, username, idempotency_key):
    row = conn.execute(
        "SELECT r.*,j.status AS job_status,j.refunded AS job_refunded "
        "FROM short_drama_character_reference_jobs r "
        "LEFT JOIN jobs j ON j.id=r.job_id "
        "WHERE r.username=? AND r.idempotency_key=?",
        (username, idempotency_key),
    ).fetchone()
    return dict(row) if row else None


def _character_reference_attempt_dict(row):
    if not row:
        return None
    item = dict(row)
    try:
        item["payload"] = json.loads(item.pop("image_payload_json"))
    except (TypeError, ValueError):
        raise RuntimeError("stored character reference payload is invalid")
    raw_terminal = item.pop("terminal_json", None)
    item["terminal_response"] = json.loads(raw_terminal) if raw_terminal else None
    item["cost"] = int(item["cost"])
    if item.get("points_left") is not None:
        item["points_left"] = int(item["points_left"])
    if item.get("job_id") is not None:
        item["job_id"] = int(item["job_id"])
    return item


def get_character_reference_attempt(db_factory, username, idempotency_key):
    conn = _connection(db_factory)
    conn.row_factory = sqlite3.Row
    try:
        return _character_reference_attempt_dict(conn.execute(
            "SELECT * FROM short_drama_character_reference_attempts "
            "WHERE username=? AND idempotency_key=?",
            (username, idempotency_key),
        ).fetchone())
    finally:
        conn.close()


def validate_character_reference_attempt_request(attempt, request):
    normalized = _normalize_character_reference_request(request)
    if attempt and (
            attempt["project_id"] != normalized["project_id"]
            or attempt["character_key"] != normalized["character_key"]
            or int(attempt["project_revision"]) != normalized["revision"]):
        raise CharacterReferenceIdempotencyConflict(
            "同一个 Idempotency-Key 不能用于不同角色标准图请求"
        )
    return normalized


def _character_reference_transaction_keys(username, idempotency_key):
    digest = hashlib.sha256(
        ("%s\0%s" % (username, idempotency_key)).encode("utf-8")
    ).hexdigest()
    return (
        "short-drama-character-charge:" + digest,
        "short-drama-character-refund:" + digest,
    )


def accept_character_reference_attempt(db_factory, prepared, username):
    """Persist consent before Auth and globally serialize one active project character."""
    request = prepared["request"]
    charge_key, refund_key = _character_reference_transaction_keys(
        username, prepared["idempotency_key"]
    )
    now = int(time.time())
    conn = _connection(db_factory)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM short_drama_character_reference_attempts "
            "WHERE username=? AND idempotency_key=?",
            (username, prepared["idempotency_key"]),
        ).fetchone()
        if existing:
            conn.rollback()
            return _character_reference_attempt_dict(existing)
        project = conn.execute(
            "SELECT revision,stage,point_budget FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (request["project_id"], prepared["owner_username"]),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if int(project["revision"]) != int(request["revision"]):
            raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
        if not _character_reference_stage_allowed(
                conn, request["project_id"], project["stage"]):
            raise ValueError("当前阶段不能生成角色标准图")
        character = conn.execute(
            "SELECT character_key,name,identity_text,personality,"
            "source_type,avatar_id,appearance_prompt,wardrobe_prompt "
            "FROM short_drama_characters "
            "WHERE project_id=? AND character_key=?",
            (request["project_id"], request["character_key"]),
        ).fetchone()
        snapshot = dict(zip(_CHARACTER_SNAPSHOT_FIELDS, character or ()))
        if (not character
                or _character_snapshot_hash(snapshot) != prepared["snapshot_hash"]):
            raise RevisionConflict("角色资料已更新，请刷新后重新生成")
        unresolved = conn.execute(
            "SELECT username FROM short_drama_character_reference_attempts "
            "WHERE project_id=? AND character_key=? "
            "AND (state IN ('accepted','charged','refund_pending') "
            "OR (state='linked' AND EXISTS ("
            "SELECT 1 FROM jobs WHERE jobs.id=short_drama_character_reference_attempts.job_id "
            "AND jobs.status IN ('pending','running')))) LIMIT 1",
            (request["project_id"], request["character_key"]),
        ).fetchone()
        if unresolved:
            raise CharacterReferenceInProgress(
                "This character already has a generation task in progress"
            )
        usage = _project_point_usage(conn, request["project_id"])
        budget = int(project["point_budget"] or 0)
        if (budget and usage["spent_points"] + usage["reserved_points"]
                + int(prepared["cost"]) > budget):
            raise PointBudgetExceeded(
                "短剧点数预算不足：已用 %d 点、已预留 %d 点、本次 %d 点、预算 %d 点"
                % (
                    usage["spent_points"], usage["reserved_points"],
                    int(prepared["cost"]), budget,
                )
            )
        conn.execute(
            "INSERT INTO short_drama_character_reference_attempts "
            "(charge_key,refund_key,username,owner_username,idempotency_key,"
            "project_id,character_key,project_revision,character_snapshot_hash,cost,"
            "image_payload_json,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'accepted',?,?)",
            (
                charge_key, refund_key, username, prepared["owner_username"],
                prepared["idempotency_key"], request["project_id"],
                request["character_key"], request["revision"],
                prepared["snapshot_hash"], int(prepared["cost"]),
                json.dumps(prepared["payload"], ensure_ascii=False), now, now,
            ),
        )
        conn.commit()
        return get_character_reference_attempt(
            db_factory, username, prepared["idempotency_key"]
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def mark_character_reference_attempt_charged(
        db_factory, username, idempotency_key, points_left):
    conn = _connection(db_factory)
    try:
        conn.execute(
            "UPDATE short_drama_character_reference_attempts "
            "SET state='charged',points_left=?,updated_at=? "
            "WHERE username=? AND idempotency_key=? AND state IN ('accepted','charged')",
            (int(points_left), int(time.time()), username, idempotency_key),
        )
        conn.commit()
    finally:
        conn.close()
    return get_character_reference_attempt(db_factory, username, idempotency_key)


def fail_character_reference_attempt(
        db_factory, username, idempotency_key, response, job_id=None):
    """Persist refund ownership before any compensation call."""
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        if job_id:
            conn.execute(
                "UPDATE jobs SET status='error',error=?,"
                "refunded=CASE WHEN cost>0 THEN 2 ELSE refunded END,updated_at=? "
                "WHERE id=? AND status IN ('pending','running')",
                (str(response.get("detail") or "")[:300], now, int(job_id)),
            )
            conn.execute(
                "UPDATE short_drama_character_reference_jobs "
                "SET status='failed',error=?,updated_at=? WHERE job_id=?",
                (str(response.get("detail") or "")[:300], now, int(job_id)),
            )
        conn.execute(
            "UPDATE short_drama_character_reference_attempts "
            "SET state='refund_pending',job_id=COALESCE(?,job_id),terminal_json=?,updated_at=? "
            "WHERE username=? AND idempotency_key=? "
            "AND state IN ('charged','linked','refund_pending')",
            (
                job_id, json.dumps(response, ensure_ascii=False), now,
                username, idempotency_key,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_character_reference_attempt(db_factory, username, idempotency_key)


def reconcile_character_reference_refund(db_factory, points_domain, attempt):
    if not attempt or attempt.get("state") not in {"refund_pending", "refunded"}:
        return attempt
    if attempt["state"] == "refund_pending":
        try:
            points_domain.refund_points(
                attempt["username"], attempt["cost"],
                "short-drama character reference compensation",
                transaction_key=attempt["refund_key"],
            )
        except Exception:
            return attempt
        conn = _connection(db_factory)
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            conn.execute(
                "UPDATE short_drama_character_reference_attempts "
                "SET state='refunded',updated_at=? "
                "WHERE username=? AND idempotency_key=? "
                "AND state IN ('refund_pending','refunded')",
                (now, attempt["username"], attempt["idempotency_key"]),
            )
            if attempt.get("job_id"):
                conn.execute(
                    "UPDATE jobs SET refunded=1,updated_at=? "
                    "WHERE id=? AND refunded=2",
                    (now, int(attempt["job_id"])),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        attempt = get_character_reference_attempt(
            db_factory, attempt["username"], attempt["idempotency_key"]
        )
    return attempt


def retry_character_reference_refunds(db_factory, points_domain, limit=100):
    conn = _connection(db_factory)
    conn.row_factory = sqlite3.Row
    try:
        attempts = [
            _character_reference_attempt_dict(row)
            for row in conn.execute(
                "SELECT * FROM short_drama_character_reference_attempts "
                "WHERE state='refund_pending' ORDER BY updated_at,charge_key LIMIT ?",
                (max(1, int(limit or 100)),),
            ).fetchall()
        ]
    finally:
        conn.close()
    return sum(
        reconcile_character_reference_refund(
            db_factory, points_domain, attempt
        )["state"] == "refunded"
        for attempt in attempts
    )


def fail_linked_character_reference_job(
        db_factory, job_id, error, from_states=("pending", "running")):
    """Atomically hand a linked job's compensation to its persisted attempt."""
    conn = _connection(db_factory)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='short_drama_character_reference_attempts'"
        ).fetchone():
            conn.rollback()
            return None
        attempt = conn.execute(
            "SELECT username,idempotency_key FROM "
            "short_drama_character_reference_attempts "
            "WHERE job_id=? AND state IN ('linked','refund_pending')",
            (int(job_id),),
        ).fetchone()
        if not attempt:
            conn.rollback()
            return None
        placeholders = ",".join("?" for _ in from_states)
        now = int(time.time())
        message = str(error or "character reference generation failed")[:300]
        claimed = conn.execute(
            "UPDATE jobs SET status='error',error=?,"
            "refunded=CASE WHEN COALESCE(cost,0)>0 THEN 2 ELSE refunded END,"
            "updated_at=? WHERE id=? AND status IN (%s)" % placeholders,
            (message, now, int(job_id), *tuple(from_states)),
        ).rowcount == 1
        if claimed:
            terminal = {
                "detail": message,
                "code": "character_reference_generation_failed",
                "operation_terminal": True,
            }
            conn.execute(
                "UPDATE short_drama_character_reference_attempts "
                "SET state='refund_pending',terminal_json=?,updated_at=? "
                "WHERE job_id=? AND state IN ('linked','refund_pending')",
                (json.dumps(terminal, ensure_ascii=False), now, int(job_id)),
            )
            conn.execute(
                "UPDATE short_drama_character_reference_jobs "
                "SET status='failed',error=?,updated_at=? WHERE job_id=?",
                (message, now, int(job_id)),
            )
        conn.commit()
        return {"claimed": claimed, "attempt_owned": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_character_reference_attempt_failed(
        db_factory, username, idempotency_key, response):
    conn = _connection(db_factory)
    try:
        conn.execute(
            "UPDATE short_drama_character_reference_attempts "
            "SET state='failed',terminal_json=?,updated_at=? "
            "WHERE username=? AND idempotency_key=? AND state='accepted'",
            (
                json.dumps(response, ensure_ascii=False), int(time.time()),
                username, idempotency_key,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_character_reference_attempt(db_factory, username, idempotency_key)


def find_recoverable_character_reference(
        db_factory, username, owner_username, request, idempotency_key):
    normalized = _normalize_character_reference_request(request)
    conn = _connection(db_factory)
    conn.row_factory = sqlite3.Row
    try:
        row = _character_reference_row(conn, username, idempotency_key)
        if row:
            if (row["project_id"] != normalized["project_id"]
                    or row["character_key"] != normalized["character_key"]):
                raise ValueError("同一个 Idempotency-Key 不能用于不同角色标准图请求")
            return row
        row = conn.execute(
            "SELECT r.*,j.status AS job_status,j.refunded AS job_refunded "
            "FROM short_drama_character_reference_jobs r "
            "JOIN jobs j ON j.id=r.job_id "
            "WHERE r.owner_username=? AND r.project_id=? "
            "AND r.character_key=? AND r.status='linked' "
            "AND COALESCE(j.refunded,0)<>1 AND j.status IN ('pending','running','done') "
            "ORDER BY r.created_at DESC,r.id DESC LIMIT 1",
            (
                owner_username, normalized["project_id"],
                normalized["character_key"],
            ),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def prepare_character_reference_submission(
        db_factory, username, owner_username, request, idempotency_key, cost_of):
    normalized = _normalize_character_reference_request(request)
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("角色标准图生成必须提供 Idempotency-Key")
    conn = _connection(db_factory)
    conn.row_factory = sqlite3.Row
    try:
        project = conn.execute(
            "SELECT id,username,revision,stage,point_budget FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (normalized["project_id"], owner_username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if int(project["revision"]) != normalized["revision"]:
            raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
        if not _character_reference_stage_allowed(
                conn, normalized["project_id"], project["stage"]):
            raise ValueError("当前阶段不能生成角色标准图")
        character = conn.execute(
            "SELECT * FROM short_drama_characters "
            "WHERE project_id=? AND character_key=?",
            (normalized["project_id"], normalized["character_key"]),
        ).fetchone()
        if not character or character["source_type"] != "ai_character":
            raise ValueError("AI 角色不存在或不支持生成标准图")
        character = dict(character)
        for field in ("name", "appearance_prompt"):
            if not str(character.get(field) or "").strip():
                raise ValueError("请先完整填写角色名称和性别")
        live_action_import = conn.execute(
            "SELECT character_contract_json FROM short_drama_script_imports "
            "WHERE project_id=? AND content_type='live_action' "
            "AND status='completed' LIMIT 1",
            (normalized["project_id"],),
        ).fetchone()
        if live_action_import:
            contract = _json(live_action_import[0], [])
            role_contract = next(
                (item for item in contract if isinstance(item, dict)
                 and item.get("character_key") == normalized["character_key"]),
                {},
            )
            if not str(role_contract.get("fixed_clothing") or "").strip():
                raise ValueError(
                    "使用 AI 生成标准图前，请先填写固定服装提示词"
                )
        elif not str(character.get("wardrobe_prompt") or "").strip():
            raise ValueError("使用 AI 生成标准图前，请先填写固定服装提示词")
        from . import image as image_domain
        payload = image_domain.validate_image_payload({
            "provider": "banana",
            "model": "nb2",
            "quality": "hd",
            "ratio": "3:4",
            "count": 1,
            "prompt": _character_reference_prompt(character),
        })
        cost = int(cost_of("image", payload))
        usage = _project_point_usage(conn, normalized["project_id"])
        budget = int(project["point_budget"] or 0)
        if (budget and usage["spent_points"] + usage["reserved_points"] + cost > budget):
            raise PointBudgetExceeded(
                "短剧点数预算不足：已用 %d 点、已预留 %d 点、本次 %d 点、预算 %d 点"
                % (usage["spent_points"], usage["reserved_points"], cost, budget)
            )
        return {
            "request": normalized,
            "owner_username": owner_username,
            "payload": payload,
            "cost": cost,
            "snapshot_hash": _character_snapshot_hash(character),
            "idempotency_key": idempotency_key.strip(),
        }
    finally:
        conn.close()


def record_character_reference_job(connection, prepared, username, job_id):
    request = prepared["request"]
    attempt = connection.execute(
        "SELECT state,cost,points_left FROM short_drama_character_reference_attempts "
        "WHERE username=? AND idempotency_key=?",
        (username, prepared["idempotency_key"]),
    ).fetchone()
    if (not attempt or attempt[0] not in {"charged", "linked"}
            or int(attempt[1]) != int(prepared["cost"])
            or attempt[2] is None):
        raise ValueError("character reference charge attempt is not ready for a job")
    project = connection.execute(
        "SELECT revision,stage,point_budget FROM short_drama_projects "
        "WHERE id=? AND username=? AND deleted=0",
        (request["project_id"], prepared["owner_username"]),
    ).fetchone()
    if not project:
        raise LookupError("短剧项目不存在")
    if int(project[0]) != request["revision"]:
        raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
    if not _character_reference_stage_allowed(
            connection, request["project_id"], project[1]):
        raise ValueError("当前阶段不能生成角色标准图")
    character = connection.execute(
        "SELECT character_key,name,identity_text,personality,"
        "source_type,avatar_id,appearance_prompt,wardrobe_prompt "
        "FROM short_drama_characters WHERE project_id=? AND character_key=?",
        (request["project_id"], request["character_key"]),
    ).fetchone()
    character_snapshot = dict(zip(_CHARACTER_SNAPSHOT_FIELDS, character or ()))
    if (not character
            or _character_snapshot_hash(character_snapshot) != prepared["snapshot_hash"]):
        raise RevisionConflict("角色资料已更新，请刷新后重新生成")
    usage = _project_point_usage(connection, request["project_id"])
    budget = int(project[2] or 0)
    if (budget and usage["spent_points"] + usage["reserved_points"]
            + int(prepared["cost"]) > budget):
        raise PointBudgetExceeded(
            "短剧点数预算不足：已用 %d 点、已预留 %d 点、本次 %d 点、预算 %d 点"
            % (
                usage["spent_points"], usage["reserved_points"],
                int(prepared["cost"]), budget,
            )
        )
    now = int(time.time())
    connection.execute(
        "INSERT INTO short_drama_character_reference_jobs "
        "(id,username,owner_username,project_id,character_key,project_revision,"
        "character_snapshot_hash,idempotency_key,job_id,cost,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?, 'linked',?,?)",
        (
            str(uuid.uuid4()), username, prepared["owner_username"],
            request["project_id"], request["character_key"], request["revision"],
            prepared["snapshot_hash"], prepared["idempotency_key"], int(job_id),
            int(prepared["cost"]), now, now,
        ),
    )
    response = {
        "project_id": request["project_id"],
        "character_key": request["character_key"],
        "job_id": int(job_id),
        "cost": int(prepared["cost"]),
        "points_left": int(attempt[2]),
        "replayed": False,
        "association_status": "linked",
    }
    updated = connection.execute(
        "UPDATE short_drama_character_reference_attempts "
        "SET state='linked',job_id=?,terminal_json=?,updated_at=? "
        "WHERE username=? AND idempotency_key=? AND state='charged'",
        (
            int(job_id), json.dumps(response, ensure_ascii=False), now,
            username, prepared["idempotency_key"],
        ),
    )
    if updated.rowcount != 1:
        raise ValueError("character reference charge attempt could not be linked")


def reconcile_character_reference_job(db_factory, job_id, username=None, result=None):
    from . import image as image_domain
    conn = _connection(db_factory)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        params = [int(job_id)]
        username_clause = ""
        if username:
            username_clause = " AND r.username=?"
            params.append(username)
        row = conn.execute(
            "SELECT r.*,j.status AS job_status,j.refunded AS job_refunded,"
            "j.payload AS job_payload,j.result AS job_result "
            "FROM short_drama_character_reference_jobs r "
            "JOIN jobs j ON j.id=r.job_id WHERE r.job_id=?" + username_clause,
            tuple(params),
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        if row["status"] == "done":
            conn.commit()
            return dict(row)
        if int(row["job_refunded"] or 0) == 1 or row["job_status"] in {"error", "failed"}:
            conn.execute(
                "UPDATE short_drama_character_reference_jobs "
                "SET status='failed',error=?,updated_at=? WHERE job_id=?",
                ("角色标准图生成失败或已退款", int(time.time()), int(job_id)),
            )
            conn.commit()
            return _character_reference_row(conn, row["username"], row["idempotency_key"])
        if row["job_status"] != "done":
            conn.commit()
            return dict(row)
        payload = _json(row["job_payload"], {})
        generated = result if isinstance(result, dict) else _json(row["job_result"], {})
        if (str(payload.get("provider") or "").lower() != "banana"
                or str(payload.get("model") or "nb2").lower() != "nb2"):
            raise ValueError("角色标准图任务模型无效")
        files = generated.get("files") or []
        urls = generated.get("urls") or []
        file_value = generated.get("file") or (files[0] if files else "")
        url_value = generated.get("url") or (urls[0] if urls else "")
        trusted_file = (
            image_domain._trusted_short_drama_file(file_value)
            or image_domain._trusted_short_drama_file(url_value, file_url=True)
        )
        if not trusted_file or not isinstance(url_value, str) or not url_value:
            raise ValueError("角色标准图任务结果无效")
        character = conn.execute(
            "SELECT * FROM short_drama_characters "
            "WHERE project_id=? AND character_key=?",
            (row["project_id"], row["character_key"]),
        ).fetchone()
        if not character:
            raise LookupError("角色标准图关联的角色不存在")
        if _character_snapshot_hash(dict(character)) != row["character_snapshot_hash"]:
            conn.execute(
                "UPDATE short_drama_character_reference_jobs "
                "SET status='ready',error=?,updated_at=? WHERE job_id=?",
                ("角色资料已更新，生成结果已保留但未自动覆盖", int(time.time()), int(job_id)),
            )
            conn.commit()
            return _character_reference_row(conn, row["username"], row["idempotency_key"])
        next_version = max(1, int(character["reference_version"] or 0) + 1)
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_characters SET pending_reference_job_id=?,"
            "pending_reference_file=?,pending_reference_url=?,pending_reference_version=?,"
            "pending_reference_source='ai_generation',pending_reference_asset_id='',"
            "pending_reference_name='',reference_profile_stale=0 "
            "WHERE project_id=? AND character_key=?",
            (
                int(job_id), trusted_file, url_value, next_version,
                row["project_id"], row["character_key"],
            ),
        )
        conn.execute(
            "UPDATE short_drama_character_reference_jobs "
            "SET status='done',error='',updated_at=? WHERE job_id=?",
            (now, int(job_id)),
        )
        _record_character_contract_migration_evidence(
            conn, row["project_id"], row["character_key"],
            next_version, int(job_id),
        )
        conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1,updated_at=? "
            "WHERE id=? AND username=? AND deleted=0",
            (now, row["project_id"], row["owner_username"]),
        )
        conn.commit()
        return _character_reference_row(conn, row["username"], row["idempotency_key"])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reconcile_project_character_references(db_factory, owner_username, project_id):
    conn = _connection(db_factory)
    try:
        job_ids = [
            int(row[0]) for row in conn.execute(
                "SELECT r.job_id FROM short_drama_character_reference_jobs r "
                "JOIN short_drama_projects p ON p.id=r.project_id "
                "WHERE r.project_id=? AND p.username=? "
                "AND r.status IN ('linked','ready')",
                (project_id, owner_username),
            ).fetchall()
        ]
    finally:
        conn.close()
    for linked_job_id in job_ids:
        try:
            reconcile_character_reference_job(db_factory, linked_job_id)
        except Exception:
            pass


def select_character_reference(db_factory, owner_username, actor_username, body):
    """Attach an owned image asset or a validated local upload as an unlocked preview."""
    from . import (
        cli_uploads,
        image as image_domain,
        short_drama_reference_validation,
    )

    if not isinstance(body, dict):
        raise ValueError("角色标准图请求无效")
    project_id = str(body.get("project_id") or "").strip()
    character_key = str(body.get("character_key") or "").strip()
    source = str(body.get("source") or "").strip().lower()
    revision = body.get("revision")
    if (not project_id or not character_key or type(revision) is not int
            or revision < 1 or source not in {"asset", "upload"}):
        raise ValueError("角色标准图参数无效")

    reference_job_id = None
    trusted_generated_reference = False
    trusted_three_view_job_id = None
    reference_file = reference_url = reference_asset_id = reference_name = ""
    created_path = None
    raw = b""
    mime = ""
    conn = _connection(db_factory)
    try:
        project = conn.execute(
            "SELECT revision,stage FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, owner_username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if int(project[0]) != revision:
            raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
        if not _character_reference_stage_allowed(conn, project_id, project[1]):
            raise ValueError("当前阶段不能设置角色标准图")
        character = conn.execute(
            "SELECT reference_version FROM short_drama_characters "
            "WHERE project_id=? AND character_key=?",
            (project_id, character_key),
        ).fetchone()
        if not character:
            raise LookupError("角色不存在")

        if source == "asset":
            try:
                reference_job_id = int(body.get("asset_job_id"))
            except (TypeError, ValueError):
                raise ValueError("请选择有效的图片资产")
            job = conn.execute(
                "SELECT result FROM jobs WHERE id=? AND username=? "
                "AND kind='image' AND status='done'",
                (reference_job_id, actor_username),
            ).fetchone()
            if not job:
                raise ValueError("图片资产不存在或不属于当前用户")
            trusted_generated_reference = bool(conn.execute(
                "SELECT 1 FROM short_drama_character_reference_jobs "
                "WHERE job_id=? AND username=? AND status IN ('ready','done')",
                (reference_job_id, actor_username),
            ).fetchone())
            trusted_three_view = conn.execute(
                "SELECT 1 FROM short_drama_character_reference_jobs "
                "WHERE job_id=? AND username=? AND owner_username=? "
                "AND project_id=? AND character_key=? AND status='done'",
                (
                    reference_job_id, actor_username, owner_username,
                    project_id, character_key,
                ),
            ).fetchone()
            if trusted_three_view:
                trusted_three_view_job_id = reference_job_id
            result = _json(job[0], {})
            urls = result.get("urls") if isinstance(result.get("urls"), list) else []
            files = result.get("files") if isinstance(result.get("files"), list) else []
            if not urls and result.get("url"):
                urls = [result.get("url")]
            if not files and result.get("file"):
                files = [result.get("file")]
            requested_url = str(body.get("asset_url") or "").strip()
            if requested_url:
                if requested_url not in urls:
                    raise ValueError("选择的图片与资产记录不匹配")
                selected_index = urls.index(requested_url)
            else:
                selected_index = 0
            reference_url = str(urls[selected_index] if selected_index < len(urls) else "")
            file_value = files[selected_index] if selected_index < len(files) else (files[0] if files else "")
            reference_file = (
                image_domain._trusted_short_drama_file(file_value)
                or image_domain._trusted_short_drama_file(reference_url, file_url=True)
            )
            if not reference_file or not reference_url:
                raise ValueError("该图片资产无法用作角色标准图")
            asset_path = (image_domain.OUT_DIR / reference_file).resolve()
            try:
                raw = asset_path.read_bytes()
            except OSError:
                raise ValueError("该图片资产无法用作角色标准图")
            if not raw or len(raw) > cli_uploads.MAX_BYTES:
                raise ValueError("该图片资产无法用作角色标准图")
            mime = cli_uploads.detect_mime(raw[:16])
            if mime not in cli_uploads.MIME_EXTENSIONS:
                raise ValueError("该图片资产无法用作角色标准图")
            reference_asset_id = str(reference_job_id)
            reference_name = _text(body.get("filename"), 240) or "我的资产 #" + reference_asset_id
        else:
            data_url = str(body.get("image_data") or "")
            match = re.fullmatch(
                r"data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=\r\n]+)",
                data_url,
            )
            if not match:
                raise ValueError("请上传 JPG、PNG 或 WebP 图片")
            try:
                raw = base64.b64decode(match.group(2), validate=True)
            except Exception:
                raise ValueError("上传图片内容无效")
            mime = match.group(1)
            if not raw or len(raw) > cli_uploads.MAX_BYTES:
                raise ValueError("图片大小必须在 10MB 以内")
            detected_mime = cli_uploads.detect_mime(raw[:16])
            if detected_mime not in cli_uploads.MIME_EXTENSIONS:
                raise ValueError("图片内容无效或格式不支持")
            # Browsers derive File.type from the filename on some platforms.
            # Trust the supported image signature and normalize the stored file
            # instead of rejecting a JPEG that happens to be named .png, etc.
            mime = detected_mime
            reference_name = _text(body.get("filename"), 240) or "本地上传图片"
    finally:
        conn.close()

    # A completed short-drama character-reference job has already been created
    # by this workflow and is trusted through its durable job association.  Do
    # not send the same generated board through the upload-oriented vision gate
    # again: that gate can mistake a photorealistic three-view board for a
    # synthetic/non-person image.  Other library assets and local uploads still
    # receive the full content check.
    if not trusted_generated_reference:
        short_drama_reference_validation.validate_character_reference(raw, mime)

    if source == "upload":
        extension = cli_uploads.MIME_EXTENSIONS[mime]
        relative = "short_drama_role_uploads/role_%s%s" % (uuid.uuid4().hex, extension)
        created_path = (image_domain.OUT_DIR / relative).resolve()
        created_path.parent.mkdir(parents=True, exist_ok=True)
        created_path.write_bytes(raw)
        reference_file = relative
        reference_url = "/api/gen/file/" + relative

    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT revision,stage FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, owner_username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if int(project[0]) != revision:
            raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
        if not _character_reference_stage_allowed(conn, project_id, project[1]):
            raise ValueError("当前阶段不能设置角色标准图")
        character = conn.execute(
            "SELECT reference_version FROM short_drama_characters "
            "WHERE project_id=? AND character_key=?",
            (project_id, character_key),
        ).fetchone()
        if not character:
            raise LookupError("角色不存在")
        next_version = max(1, int(character[0] or 0) + 1)
        conn.execute(
            "UPDATE short_drama_characters SET pending_reference_job_id=?,"
            "pending_reference_file=?,pending_reference_url=?,pending_reference_version=?,"
            "pending_reference_source=?,pending_reference_asset_id=?,pending_reference_name=?,"
            "reference_profile_stale=0 "
            "WHERE project_id=? AND character_key=?",
            (
                reference_job_id, reference_file, reference_url, next_version, source,
                reference_asset_id, reference_name, project_id, character_key,
            ),
        )
        _record_character_contract_migration_evidence(
            conn, project_id, character_key, next_version,
            trusted_three_view_job_id,
        )
        _cas_content_update(conn, owner_username, project_id, revision, project[1])
        conn.commit()
        return _project_detail(conn, owner_username, project_id)
    except Exception:
        conn.rollback()
        if created_path is not None:
            try:
                created_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        conn.close()


def confirm_character_reference(db_factory, username, project_id, revision,
                                character_key, reference_version):
    """Explicitly lock a generated character preview after user review."""
    if type(revision) is not int or type(reference_version) is not int:
        raise ValueError("角色标准图确认版本无效")
    project_id = str(project_id or "").strip()
    character_key = str(character_key or "").strip()
    if not project_id or not character_key or revision < 1 or reference_version < 1:
        raise ValueError("角色标准图确认参数无效")
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT revision,stage FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if int(project[0]) != revision:
            raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
        if not _character_reference_stage_allowed(conn, project_id, project[1]):
            raise ValueError("当前阶段不能确认角色标准图")
        character = conn.execute(
            "SELECT reference_job_id,reference_file,reference_url,reference_version,reference_source,"
            "pending_reference_job_id,pending_reference_file,pending_reference_url,"
            "pending_reference_version,pending_reference_source "
            "FROM short_drama_characters WHERE project_id=? AND character_key=?",
            (project_id, character_key),
        ).fetchone()
        if not character:
            raise LookupError("角色不存在")
        candidate_job_id = character[5] if character[6] and character[7] else character[0]
        candidate_file = character[6] or character[1]
        candidate_url = character[7] or character[2]
        candidate_version = int(character[8] or character[3] or 0)
        candidate_source = character[9] or character[4]
        if (not candidate_file or not candidate_url or candidate_version != reference_version):
            raise ValueError("角色标准图尚未生成、已失效或版本不匹配")
        # The job may have been submitted by an editor on a shared canvas while
        # the project is still owned by ``username``.  Project/character
        # ownership above is the authority boundary; do not incorrectly reject
        # a valid collaborator-generated preview here.
        if candidate_job_id:
            job = conn.execute(
                "SELECT status FROM jobs WHERE id=? AND kind='image'",
                (int(candidate_job_id),),
            ).fetchone()
            if not job or job[0] != "done":
                raise ValueError("角色标准图任务尚未完成")
        elif candidate_source != "upload":
            raise ValueError("角色标准图来源无效")
        conn.execute(
            "UPDATE short_drama_characters SET reference_job_id=?,reference_file=?,"
            "reference_url=?,reference_version=?,reference_locked=1,reference_source=?,"
            "reference_asset_id=pending_reference_asset_id,reference_name=pending_reference_name,"
            "pending_reference_job_id=NULL,pending_reference_file='',pending_reference_url='',"
            "pending_reference_version=0,pending_reference_source='',pending_reference_asset_id='',"
            "pending_reference_name='',reference_profile_stale=0 "
            "WHERE project_id=? AND character_key=?",
            (candidate_job_id, candidate_file, candidate_url, candidate_version,
             candidate_source, project_id, character_key),
        )
        _confirm_character_contract_migration(
            conn, project_id, character_key, candidate_version, candidate_job_id
        )
        _cas_content_update(conn, username, project_id, revision, project[1])
        conn.commit()
        return _project_detail(conn, username, project_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _live_action_character_contract(conn, project_id, characters, provided=None):
    """Return the persisted live-action role contract for this exact role list."""
    if provided is not None:
        contract = _validate_import_character_contract(provided)
    else:
        row = conn.execute(
            "SELECT character_contract_json FROM short_drama_script_imports "
            "WHERE project_id=? AND content_type='live_action' AND status='completed'",
            (project_id,),
        ).fetchone()
        stored = _json(row[0], []) if row else []
        stored_by_key = {
            str(item.get("character_key") or ""): dict(item)
            for item in stored if isinstance(item, dict)
        }
        contract = []
        for index, character in enumerate(characters):
            key = character["character_key"]
            item = stored_by_key.get(key, {
                "character_key": key,
                "role_type": "main" if index == 0 else "support",
                "gender": "", "identity_text": "", "relationships": "",
                "age": "", "face_shape": "", "hairstyle": "",
                "hair_color": "", "height_body": "", "fixed_clothing": "",
                "fixed_colors": "", "accessories": "",
                "reference_views": ["front_full", "side_full", "back_full"],
            })
            item.update({
                "character_key": key,
                "name": character["name"],
                "personality": character["personality"],
                "appearance_prompt": character["appearance_prompt"],
                "wardrobe_prompt": character["wardrobe_prompt"],
            })
            contract.append(item)
        contract = _validate_import_character_contract(contract)
    expected = [
        (character["character_key"], character["name"]) for character in characters
    ]
    actual = [(item["character_key"], item["name"]) for item in contract]
    if actual != expected:
        raise ValueError("character_contract must match the persisted character list")
    return contract


_LIVE_ACTION_CONTRACT_CHARACTER_FIELDS = (
    "character_key", "name", "identity_text", "personality", "source_type",
    "avatar_id", "appearance_prompt", "wardrobe_prompt", "voice_key",
    "voice_settings", "sort_order",
)


def _live_action_contract_character_payload(characters):
    return [
        {field: character.get(field) for field in _LIVE_ACTION_CONTRACT_CHARACTER_FIELDS}
        for character in characters
    ]


def _character_has_reference_asset(character):
    return bool(
        character.get("reference_job_id")
        or character.get("reference_file")
        or character.get("reference_url")
        or int(character.get("reference_version") or 0) > 0
        or character.get("reference_locked")
    )


def _character_reference_activity_keys(conn, project_id):
    protected = set()
    if _table_columns(conn, "short_drama_character_reference_attempts"):
        job_columns = _table_columns(conn, "jobs")
        linked_activity_clause = (
            "OR (state='linked' AND EXISTS ("
            "SELECT 1 FROM jobs WHERE jobs.id="
            "short_drama_character_reference_attempts.job_id "
            "AND jobs.status IN ('pending','running')))"
            if job_columns else ""
        )
        protected.update(
            row[0] for row in conn.execute(
                "SELECT DISTINCT character_key FROM "
                "short_drama_character_reference_attempts WHERE project_id=? "
                "AND (state IN ('accepted','charged','refund_pending') "
                + linked_activity_clause + ")",
                (project_id,),
            ).fetchall()
        )
    if _table_columns(conn, "short_drama_character_reference_jobs"):
        job_columns = _table_columns(conn, "jobs")
        if job_columns:
            refund_clause = (
                "AND (j.id IS NULL OR COALESCE(j.refunded,0)<>1) "
                if "refunded" in job_columns else ""
            )
            protected.update(
                row[0] for row in conn.execute(
                    "SELECT DISTINCT r.character_key FROM "
                    "short_drama_character_reference_jobs r "
                    "LEFT JOIN jobs j ON j.id=r.job_id WHERE r.project_id=? "
                    "AND r.status='linked' "
                    "AND (j.id IS NULL OR j.status IN ('pending','running')) "
                    + refund_clause,
                    (project_id,),
                ).fetchall()
            )
    return protected


def update_characters(db_factory, username, project_id, revision, characters, avatar_lookup=None,
                      *, character_contract=None):
    required_stage = "characters_review"
    if type(revision) is not int:
        raise ValueError("revision 必须是整数")
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT revision,stage FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not row:
            raise LookupError("短剧项目不存在")
        if int(row[0]) != revision:
            raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
        current_stage = row[1]
        editable_draft = (
            current_stage == "draft"
            and _draft_character_profile_stage_allowed(conn, project_id)
        )
        reference_only = current_stage != required_stage and not editable_draft
        if reference_only and current_stage not in {
                "script_review", "storyboard_review", "stills_review"}:
            raise ValueError("当前阶段不能修改角色标准图")
        project = _project_detail(conn, username, project_id)
        live_action_setup = (
            _live_action_role_setup(conn, project_id) if editable_draft else None
        )
        role_setup_only = bool(
            live_action_setup
            and not project["script_versions"]
            and not project["shots"]
        )
        validated_character_contract = None
        if live_action_setup and not reference_only:
            if character_contract is None:
                raise ValueError(
                    "live-action character updates require character_contract"
                )
            validated_character_contract = _validate_import_character_contract(
                character_contract
            )
            canonical_characters = _characters_from_import_contract(
                validated_character_contract
            )
            submitted_characters = _normalize_characters(
                characters, require_complete=True, allow_empty_wardrobe=True
            )
            if (
                _live_action_contract_character_payload(submitted_characters)
                != _live_action_contract_character_payload(canonical_characters)
            ):
                raise ValueError(
                    "characters must be derived from the submitted character_contract"
                )
            characters = canonical_characters
        if role_setup_only:
            # A live-action import project is intentionally created before its
            # script and storyboard are materialized.  Saving the role form is
            # therefore a standalone content operation at this point; later
            # stages continue to validate the complete character/script/shot
            # bundle atomically.
            normalized_characters = _normalize_characters(
                characters, require_complete=True, allow_empty_wardrobe=True
            )
            normalized_shots = []
        else:
            normalized_characters, _script, normalized_shots = _current_content_bundle(
                project, characters=characters, prune_character_refs=True
            )
        if reference_only:
            stable_fields = (
                "character_key", "name", "identity_text", "personality",
                "source_type", "avatar_id", "appearance_prompt", "wardrobe_prompt",
                "voice_key", "voice_settings", "sort_order",
            )
            existing = project["characters"]
            if len(normalized_characters) != len(existing) or any(
                    any(candidate.get(field) != original.get(field)
                        for field in stable_fields)
                    for candidate, original in zip(normalized_characters, existing)):
                raise ValueError("角色确认后只能补充或重新生成角色标准图")
        existing_by_key = {
            character["character_key"]: character for character in project["characters"]
        }
        reference_activity_keys = _character_reference_activity_keys(
            conn, project_id
        )
        submitted_keys = {
            character["character_key"] for character in normalized_characters
        }
        for character_key, existing in existing_by_key.items():
            if ((_character_has_reference_asset(existing)
                 or character_key in reference_activity_keys)
                    and character_key not in submitted_keys):
                raise CharacterReferenceProtected(
                    "该角色已有付费或锁定的角色标准图，不能删除"
                )
        reference_fields = (
            "name", "identity_text", "personality", "source_type", "avatar_id",
            "appearance_prompt", "wardrobe_prompt",
        )
        for character in normalized_characters:
            existing = existing_by_key.get(character["character_key"]) or {}
            character["reference_version"] = int(existing.get("reference_version") or 0)
            reference_changed = bool(existing) and any(
                character.get(field) != existing.get(field) for field in reference_fields
            )
            if (reference_changed
                    and character["character_key"] in reference_activity_keys):
                raise CharacterReferenceProtected(
                    "该角色已有付费或锁定的角色标准图，生成任务进行中，不能修改当前角色资料"
                )
            if existing:
                character["reference_job_id"] = existing.get("reference_job_id")
                character["reference_file"] = existing.get("reference_file") or ""
                character["reference_url"] = existing.get("reference_url") or ""
                character["reference_locked"] = bool(existing.get("reference_locked"))
                character["reference_source"] = existing.get("reference_source") or ""
                character["reference_asset_id"] = existing.get("reference_asset_id") or ""
                character["reference_name"] = existing.get("reference_name") or ""
                character["reference_profile_stale"] = bool(
                    existing.get("reference_profile_stale") or (
                        reference_changed and (
                            _character_has_reference_asset(existing)
                        )
                    )
                )
                character["_preserve_reference"] = True
                character["pending_reference_job_id"] = existing.get("pending_reference_job_id")
                character["pending_reference_file"] = existing.get("pending_reference_file") or ""
                character["pending_reference_url"] = existing.get("pending_reference_url") or ""
                character["pending_reference_version"] = int(existing.get("pending_reference_version") or 0)
                character["pending_reference_source"] = existing.get("pending_reference_source") or ""
                character["pending_reference_asset_id"] = existing.get("pending_reference_asset_id") or ""
                character["pending_reference_name"] = existing.get("pending_reference_name") or ""
        _validate_owned_avatars(username, normalized_characters, avatar_lookup)
        _resolve_ai_character_references(conn, username, normalized_characters)
        synced_character_contract = None
        if live_action_setup and not reference_only:
            synced_character_contract = _live_action_character_contract(
                conn, project_id, normalized_characters,
                validated_character_contract,
            )
        if reference_only:
            for character in normalized_characters:
                conn.execute(
                    "UPDATE short_drama_characters SET reference_job_id=?,reference_file=?,"
                    "reference_url=?,reference_version=?,reference_locked=?,reference_source=?,"
                    "reference_asset_id=?,reference_name=? "
                    "WHERE project_id=? AND character_key=?",
                    (
                        character["reference_job_id"], character["reference_file"],
                        character["reference_url"], character["reference_version"],
                        int(character["reference_locked"]), character.get("reference_source") or "",
                        character.get("reference_asset_id") or "", character.get("reference_name") or "",
                        project_id,
                        character["character_key"],
                    ),
                )
            _cas_content_update(conn, username, project_id, revision, current_stage)
        else:
            conn.execute("DELETE FROM short_drama_characters WHERE project_id=?", (project_id,))
            _insert_characters(conn, project_id, normalized_characters)
            for original, shot in zip(project["shots"], normalized_shots):
                conn.execute(
                    "UPDATE short_drama_shots SET character_keys_json=? WHERE id=? AND project_id=?",
                    (_json_text(shot["character_keys"], []), original["id"], project_id),
                )
            _cas_content_update(
                conn, username, project_id, revision,
                current_stage if editable_draft else required_stage,
            )
            if live_action_setup:
                saved_at = int(time.time())
                _sync_character_contract_migration_roles(
                    conn, project_id, submitted_keys
                )
                conn.execute(
                    "UPDATE short_drama_script_imports "
                    "SET character_contract_json=?,roles_saved_at=?,updated_at=? "
                    "WHERE project_id=?",
                    (
                        json.dumps(synced_character_contract, ensure_ascii=False,
                                   separators=(",", ":")),
                        saved_at, saved_at, project_id,
                    ),
                )
        conn.commit()
        return _project_detail(conn, username, project_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_script(db_factory, username, project_id, revision, script):
    required_stage = "script_review"
    conn = _connection(db_factory)
    try:
        _begin_content_update(conn, username, project_id, revision, required_stage)
        project = _project_detail(conn, username, project_id)
        if len(project["script_versions"]) >= MAX_SCRIPT_VERSIONS_PER_PROJECT:
            raise ValueError("剧本版本数量已达上限，请确认当前版本后继续")
        prepared_script = _prepare_dialogue_line_ids(
            conn, project_id, project["script_versions"][-1], script
        )
        _characters, normalized_script, normalized_shots = _current_content_bundle(
            project, script=prepared_script, prune_dialogue_refs=True
        )
        _retire_removed_dialogue_tokens(
            conn, project_id, {line["id"] for line in normalized_script["dialogue_lines"]}
        )
        now = int(time.time())
        version = _append_script(conn, project_id, normalized_script, now)
        for original, shot in zip(project["shots"], normalized_shots):
            conn.execute(
                "UPDATE short_drama_shots SET script_version=?, dialogue_line_ids_json=? "
                "WHERE id=? AND project_id=?",
                (version, _json_text(shot["dialogue_line_ids"], []), original["id"], project_id),
            )
        _cas_content_update(conn, username, project_id, revision, required_stage)
        conn.commit()
        return _project_detail(conn, username, project_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_shots(db_factory, username, project_id, revision, shots):
    required_stage = "storyboard_review"
    conn = _connection(db_factory)
    try:
        _begin_content_update(conn, username, project_id, revision, required_stage)
        project = _project_detail(conn, username, project_id)
        _characters, _script, normalized_shots = _current_content_bundle(
            project, shots=shots
        )
        script_version = project["script_versions"][-1]["version"]
        existing_cursor = conn.execute(
            "SELECT * FROM short_drama_shots WHERE project_id=?",
            (project_id,),
        )
        columns = [column[0] for column in existing_cursor.description]
        existing_rows = [
            dict(zip(columns, row)) for row in existing_cursor.fetchall()
        ]
        existing = {row["shot_key"]: row for row in existing_rows}
        incoming_keys = {shot["shot_key"] for shot in normalized_shots}
        removed_ids = [
            row["id"] for key, row in existing.items() if key not in incoming_keys
        ]
        changed_ids = []
        prepared = []
        for shot in normalized_shots:
            row = existing.get(shot["shot_key"])
            shot_id = row["id"] if row else str(uuid.uuid4())
            character_json = _json_text(shot["character_keys"], [])
            dialogue_json = _json_text(shot["dialogue_line_ids"], [])
            values = (
                script_version, shot["sort_order"], shot["duration"],
                shot["scene_description"], shot["camera_description"],
                character_json, dialogue_json, shot["image_prompt"],
                shot["video_prompt"],
            )
            if not row or values != (
                int(row["script_version"]), int(row["sort_order"]),
                int(row["duration"]), row["scene_description"],
                row["camera_description"], row["character_keys_json"],
                row["dialogue_line_ids_json"], row["image_prompt"],
                row["video_prompt"],
            ):
                changed_ids.append(shot_id)
            prepared.append((shot_id, shot, character_json, dialogue_json, row is not None))
        short_drama_asset_graph.invalidate_shot_content(
            conn, project_id, username, changed_ids, removed_ids,
        )
        if removed_ids:
            placeholders = ",".join("?" for _ in removed_ids)
            conn.execute(
                "DELETE FROM short_drama_shots WHERE project_id=? AND id IN (%s)"
                % placeholders,
                (project_id, *removed_ids),
            )
        for shot_id, shot, character_json, dialogue_json, existed in prepared:
            if existed:
                conn.execute(
                    "UPDATE short_drama_shots SET script_version=?,sort_order=?,duration=?,"
                    "scene_description=?,camera_description=?,character_keys_json=?,"
                    "dialogue_line_ids_json=?,image_prompt=?,video_prompt=? "
                    "WHERE id=? AND project_id=?",
                    (
                        script_version, shot["sort_order"], shot["duration"],
                        shot["scene_description"], shot["camera_description"],
                        character_json, dialogue_json, shot["image_prompt"],
                        shot["video_prompt"], shot_id, project_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO short_drama_shots "
                    "(id,project_id,script_version,shot_key,sort_order,duration,"
                    "scene_description,camera_description,character_keys_json,"
                    "dialogue_line_ids_json,image_prompt,video_prompt) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        shot_id, project_id, script_version, shot["shot_key"],
                        shot["sort_order"], shot["duration"],
                        shot["scene_description"], shot["camera_description"],
                        character_json, dialogue_json, shot["image_prompt"],
                        shot["video_prompt"],
                    ),
                )
        _cas_content_update(conn, username, project_id, revision, required_stage)
        conn.commit()
        return _project_detail(conn, username, project_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_plan(db_factory, username, project_id, revision, plan, planning_cost, planning_job_id,
               planning_metadata=None, avatar_lookup=None):
    characters, script, shots = _validate_plan(plan)
    if not isinstance(script["dialogue_lines"], list):
        raise ValueError("剧本台词数据无效")
    try:
        cost = max(0, int(planning_cost))
        job_id = int(planning_job_id)
    except (TypeError, ValueError):
        raise ValueError("规划任务无效")
    now = int(time.time())
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT title, synopsis, ratio, target_duration, shot_count, visual_style, "
            "target_platform, revision, stage FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        applied = conn.execute(
            "SELECT project_id, username FROM short_drama_applied_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if applied:
            raise AppliedJobConflict("规划任务已经应用过")
        if project[7] != revision:
            raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
        if project[8] != "draft":
            raise ValueError("当前短剧阶段不能应用策划")
        if planning_metadata is not None:
            if planning_metadata.get("project_id") != project_id:
                raise ValueError("规划任务不属于当前短剧项目")
            if (planning_metadata.get("ratio"), planning_metadata.get("target_duration"),
                    planning_metadata.get("shot_count")) != (project[2], project[3], project[4]):
                raise ValueError("规划任务设置与当前项目不一致")
            if (planning_metadata.get("prompt"), planning_metadata.get("style"),
                    planning_metadata.get("platform")) != (project[1], project[5], project[6]):
                raise ValueError("规划任务需求与当前项目不一致")
        if not short_drama_duration.contains(
                project[3], sum(shot["duration"] for shot in shots)):
            raise ValueError("分镜总时长必须位于所选成片时长区间")
        _validate_owned_avatars(username, characters, avatar_lookup)
        try:
            conn.execute(
                "INSERT INTO short_drama_applied_jobs (job_id, project_id, username, cost, applied_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, project_id, username, cost, now),
            )
        except sqlite3.IntegrityError:
            applied = conn.execute(
                "SELECT 1 FROM short_drama_applied_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if applied:
                raise AppliedJobConflict("规划任务已经应用过")
            raise
        next_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM short_drama_scripts WHERE project_id=?", (project_id,)
        ).fetchone()[0]
        conn.execute("DELETE FROM short_drama_characters WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM short_drama_shots WHERE project_id=?", (project_id,))
        for character in characters:
            conn.execute(
                "INSERT INTO short_drama_characters "
                "(id, project_id, character_key, name, identity_text, personality, source_type, avatar_id, "
                "appearance_prompt, wardrobe_prompt, voice_key, voice_settings_json, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), project_id, character["character_key"], character["name"],
                 character["identity_text"], character["personality"], character["source_type"],
                 character["avatar_id"], character["appearance_prompt"], character["wardrobe_prompt"],
                 character["voice_key"], _json_text(character["voice_settings"], {}), character["sort_order"]),
            )
        conn.execute(
            "INSERT INTO short_drama_scripts "
            "(id, project_id, version, title, logline, hook, conflict_text, turn_text, ending, dialogue_lines_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), project_id, next_version, script["title"], script["logline"], script["hook"],
             script["conflict_text"], script["turn_text"], script["ending"],
             _json_text(script["dialogue_lines"], []), now),
        )
        for shot in shots:
            conn.execute(
                "INSERT INTO short_drama_shots "
                "(id, project_id, script_version, shot_key, sort_order, duration, scene_description, "
                "camera_description, character_keys_json, dialogue_line_ids_json, image_prompt, video_prompt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), project_id, next_version, shot["shot_key"], shot["sort_order"],
                 shot["duration"], shot["scene_description"], shot["camera_description"],
                 _json_text(shot["character_keys"], []), _json_text(shot["dialogue_line_ids"], []),
                 shot["image_prompt"], shot["video_prompt"]),
            )
        cur = conn.execute(
            "UPDATE short_drama_projects SET stage='characters_review', "
            "revision=revision+1, updated_at=? WHERE id=? AND username=? AND revision=? AND deleted=0",
            (now, project_id, username, revision),
        )
        if cur.rowcount != 1:
            _raise_cas_error(conn, username, project_id)
        conn.commit()
        return _project_detail(conn, username, project_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def confirm_stage(db_factory, username, project_id, revision, current_stage,
                  avatar_lookup=None):
    if current_stage == "assembly_review":
        short_drama_completion.reject_legacy_completion()
    if current_stage == "stills_review":
        return short_drama_production.confirm_stage(db_factory, username, {
            "project_id": project_id,
            "revision": revision,
            "stage": current_stage,
        })
    if current_stage == "voice_review":
        return short_drama_voice.confirm_voice_stage(db_factory, username, {
            "project_id": project_id,
            "revision": revision,
            "stage": current_stage,
        })
    if current_stage == "video_review":
        return short_drama_video.confirm_video_stage(db_factory, username, {
            "project_id": project_id,
            "revision": revision,
            "stage": current_stage,
        }, avatar_lookup)
    if (
        current_stage in short_drama_production.PRODUCTION_STAGES
        and current_stage != "assembly_review"
    ):
        raise ValueError("当前批次只允许确认关键帧阶段")
    if current_stage not in NEXT_STAGE:
        raise ValueError("当前阶段不可确认")
    now = int(time.time())
    conn = _connection(db_factory)
    try:
        project = conn.execute(
            "SELECT revision, stage FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if project[0] != revision:
            raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
        if project[1] != current_stage:
            raise ValueError("不能跳过短剧阶段")
        cur = conn.execute(
            "UPDATE short_drama_projects SET stage=?, revision=revision+1, updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND stage=? AND deleted=0",
            (NEXT_STAGE[current_stage], now, project_id, username, revision, current_stage),
        )
        if cur.rowcount != 1:
            _raise_cas_error(conn, username, project_id)
        conn.commit()
        return _project_detail(conn, username, project_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_HTTP_ROUTES = {
    "GET": {
        "/api/gen/short-drama/projects",
        "/api/gen/short-drama/project",
        "/api/gen/short-drama/production",
        "/api/gen/short-drama/voice",
        "/api/gen/short-drama/subtitle-alignment/workspace",
        "/api/gen/short-drama/master-timeline",
        "/api/gen/short-drama/master-timeline/versions",
        "/api/gen/short-drama/lipsync/snapshot",
        "/api/gen/short-drama/lipsync/jobs/{job_id}",
        "/api/gen/short-drama/lipsync/faces/analyses/{analysis_id}",
        "/api/gen/short-drama/lipsync/faces/current",
        "/api/gen/short-drama/video",
        "/api/gen/short-drama/avatar-candidates",
        "/api/gen/short-drama/video-cast/avatars",
        "/api/gen/short-drama/assembly",
        "/api/gen/short-drama/sound-design",
        "/api/gen/short-drama/sound-design/jobs",
        "/api/gen/short-drama/assembly/audio-assets",
        "/api/gen/short-drama/playback",
        "/api/gen/short-drama/playback/jobs/{job_id}",
        "/api/gen/short-drama/completion/readiness",
        "/api/gen/short-drama/completion",
        "/api/gen/short-drama/final-assets/{asset_id}",
        "/api/gen/short-drama/planning-job",
        "/api/gen/short-drama/planning-quote",
        "/api/gen/short-drama/conversation",
        "/api/gen/short-drama/character-studio",
        "/api/gen/short-drama/asset-graph",
        "/api/gen/short-drama/asset-graph/scenes",
        "/api/gen/short-drama/asset-graph/shot-package",
        "/api/gen/short-drama/conversation/jobs/{job_id}",
        "/api/gen/short-drama/preflight",
        "/api/gen/short-drama/autodraft",
        "/api/gen/short-drama/autodraft/jobs/{job_id}",
        "/api/gen/short-drama/autodraft/provider-jobs/{job_id}",
        "/api/gen/short-drama/refinement",
        "/api/gen/short-drama/refinement/jobs/{job_id}",
        "/api/gen/short-drama/delivery/jobs/{job_id}",
    },
    "POST": {
        "/api/gen/short-drama/advisor",
        "/api/gen/short-drama/projects",
        "/api/gen/short-drama/projects/promote",
        "/api/gen/short-drama/projects/import",
        "/api/gen/short-drama/projects/live-action/core-story",
        "/api/gen/short-drama/projects/live-action/finalize",
        "/api/gen/short-drama/projects/live-action/abandon",
        "/api/gen/short-drama/project/delete",
        "/api/gen/short-drama/apply-plan",
        "/api/gen/short-drama/confirm",
        "/api/gen/short-drama/asset-quote",
        "/api/gen/short-drama/voice-quote",
        "/api/gen/short-drama/video-quote",
        "/api/gen/short-drama/video-cast",
        "/api/gen/short-drama/save-voice-timeline",
        "/api/gen/short-drama/set-voice-shot-lock",
        "/api/gen/short-drama/set-video-shot-lock",
        "/api/gen/short-drama/subtitle-alignment/jobs",
        "/api/gen/short-drama/subtitle-alignment/cancel",
        "/api/gen/short-drama/subtitle-alignment/timeline",
        "/api/gen/short-drama/subtitle-alignment/lock",
        "/api/gen/short-drama/master-timeline/rebuild",
        "/api/gen/short-drama/master-timeline/confirm",
        "/api/gen/short-drama/lipsync/quote",
        "/api/gen/short-drama/lipsync/jobs",
        "/api/gen/short-drama/lipsync/jobs/{job_id}/retry",
        "/api/gen/short-drama/lipsync/jobs/{job_id}/cancel",
        "/api/gen/short-drama/lipsync/faces/analyze",
        "/api/gen/short-drama/lipsync/faces/confirm",
        "/api/gen/short-drama/lipsync/versions/{version_id}/lock",
        "/api/gen/short-drama/select-asset",
        "/api/gen/short-drama/select-voice-version",
        "/api/gen/short-drama/select-video-version",
        "/api/gen/short-drama/confirm-production-stage",
        "/api/gen/short-drama/assembly/preview",
        "/api/gen/short-drama/assembly/final-quote",
        "/api/gen/short-drama/assembly/export",
        "/api/gen/short-drama/assembly/confirm",
        "/api/gen/short-drama/sound-design/analyze",
        "/api/gen/short-drama/sound-design/quote",
        "/api/gen/short-drama/sound-design/jobs",
        "/api/gen/short-drama/sound-design/apply",
        "/api/gen/short-drama/playback/remux",
        "/api/gen/short-drama/playback/refresh",
        "/api/gen/short-drama/completion/confirm",
        "/api/gen/short-drama/conversation/messages",
        "/api/gen/short-drama/conversation/script/generate",
        "/api/gen/short-drama/conversation/script/shot/update",
        "/api/gen/short-drama/conversation/script/shot/regenerate",
        "/api/gen/short-drama/conversation/script/shot/lock",
        "/api/gen/short-drama/conversation/script/shot/structure",
        "/api/gen/short-drama/conversation/script/restore",
        "/api/gen/short-drama/conversation/script/lock",
        "/api/gen/short-drama/character-studio/profile",
        "/api/gen/short-drama/character-studio/bind-avatar",
        "/api/gen/short-drama/asset-graph/sync",
        "/api/gen/short-drama/asset-graph/assets",
        "/api/gen/short-drama/asset-graph/versions",
        "/api/gen/short-drama/asset-graph/versions/lock",
        "/api/gen/short-drama/asset-graph/bindings",
        "/api/gen/short-drama/asset-graph/snapshots",
        "/api/gen/short-drama/asset-graph/scenes/reference",
        "/api/gen/short-drama/asset-graph/scenes/lock",
        "/api/gen/short-drama/asset-graph/scenes",
        "/api/gen/short-drama/asset-graph/scenes/update",
        "/api/gen/short-drama/asset-graph/scenes/bind-shot",
        "/api/gen/short-drama/asset-graph/scenes/delete",
        "/api/gen/short-drama/asset-graph/scenes/restore",
        "/api/gen/short-drama/preflight/generate",
        "/api/gen/short-drama/preflight/confirm",
        "/api/gen/short-drama/autodraft/provider-preflight",
        "/api/gen/short-drama/autodraft/provider-quote",
        "/api/gen/short-drama/autodraft/provider-version/select",
        "/api/gen/short-drama/autodraft/provider-jobs",
        "/api/gen/short-drama/autodraft/provider-jobs/{job_id}/reconcile",
        "/api/gen/short-drama/autodraft/jobs",
        "/api/gen/short-drama/autodraft/jobs/{job_id}/retry",
        "/api/gen/short-drama/autodraft/jobs/{job_id}/cancel",
        "/api/gen/short-drama/refinement/changes/preview",
        "/api/gen/short-drama/refinement/candidates/adopt",
        "/api/gen/short-drama/refinement/candidates/reassemble",
        "/api/gen/short-drama/refinement/jobs",
        "/api/gen/short-drama/refinement/issues",
        "/api/gen/short-drama/refinement/issues/keep-original",
        "/api/gen/short-drama/refinement/media-preference",
        "/api/gen/short-drama/refinement/reassemble",
        "/api/gen/short-drama/refinement/confirm",
        "/api/gen/short-drama/refinement/restore",
        "/api/gen/short-drama/delivery/quote",
        "/api/gen/short-drama/delivery/jobs",
    },
    "PUT": {
        "/api/gen/short-drama/project",
        "/api/gen/short-drama/master-timeline",
        "/api/gen/short-drama/lipsync/speakers",
        "/api/gen/short-drama/lipsync/versions/{version_id}/select",
        "/api/gen/short-drama/assembly/config",
        "/api/gen/short-drama/sound-design/suggestions",
        "/api/gen/short-drama/playback/versions/{version_id}/select",
    },
}


def _http_error(handler, error, *, operation_terminal=False):
    terminal = {"operation_terminal": True} if operation_terminal else {}
    if isinstance(error, short_drama_advisor.AdvisorError):
        handler._send(error.status, {
            "detail": str(error)[:220],
            "code": error.code,
            **terminal,
        })
    elif isinstance(error, short_drama_lipsync_rollout.RolloutError):
        handler._send(error.status, {
            "detail": str(error)[:220],
            "code": error.code,
            "rollout": error.decision,
            **terminal,
        })
    elif isinstance(error, short_drama_conversation.ConversationError):
        handler._send(error.status, {
            "detail": str(error)[:220],
            "code": error.code,
            **terminal,
        })
    elif isinstance(error, short_drama_character_studio.CharacterStudioError):
        handler._send(error.status, {
            "detail": str(error)[:220],
            "code": error.code,
            **terminal,
        })
    elif isinstance(error, short_drama_asset_graph.AssetGraphError):
        payload = {
            "detail": str(error)[:220], "code": error.code, **terminal,
        }
        if error.blockers:
            payload["blockers"] = error.blockers
        handler._send(error.status, payload)
    elif isinstance(error, short_drama_preflight.PreflightError):
        handler._send(error.status, {
            "detail": str(error)[:220],
            "code": error.code,
            **terminal,
        })
    elif isinstance(error, short_drama_autodraft.AutodraftError):
        handler._send(error.status, {
            "detail": str(error)[:220],
            "code": error.code,
            **terminal,
        })
    elif isinstance(error, short_drama_refinement.RefinementError):
        handler._send(error.status, {
            "detail": str(error)[:220],
            "code": error.code,
            **terminal,
        })
    elif isinstance(error, ProjectLimitExceeded):
        handler._send(429, {
            "detail": str(error)[:220],
            "code": "short_drama_project_cap",
            "max_projects": error.max_projects,
            **terminal,
        })
    elif isinstance(error, ScriptImportError):
        handler._send(error.status, {
            "detail": str(error)[:220], "code": error.code, **terminal,
        })
    elif isinstance(error, CharacterReferenceProtected):
        handler._send(409, {
            "detail": str(error)[:220],
            "code": "character_reference_protected",
            **terminal,
        })
    elif isinstance(error, ProjectCreationError):
        handler._send(error.status, {
            "detail": str(error)[:220], "code": error.code, **terminal,
        })
    elif isinstance(error, ProjectHasUnappliedJobs):
        handler._send(409, {
            "detail": str(error)[:220],
            "code": "short_drama_unapplied_paid_job",
            **terminal,
        })
    elif isinstance(error, LookupError):
        handler._send(404, {"detail": str(error)[:220], **terminal})
    elif isinstance(error, RevisionConflict):
        handler._send(409, {"detail": str(error)[:220], "code": "revision_conflict", **terminal})
    elif isinstance(error, AvatarBindingError):
        handler._send(error.status, {
            "detail": str(error)[:220], "code": error.code, **terminal,
        })
    elif isinstance(error, AppliedJobConflict):
        handler._send(409, {"detail": str(error)[:220], "code": "job_already_applied", **terminal})
    elif isinstance(error, CharacterReferenceIdempotencyConflict):
        handler._send(409, {
            "detail": str(error)[:220], "code": "idempotency_conflict", **terminal,
        })
    elif isinstance(error, short_drama_assembly.PreviewIdempotencyConflict):
        handler._send(409, {
            "detail": str(error)[:220], "code": "idempotency_conflict",
            **terminal,
        })
    elif isinstance(error, short_drama_assembly.ActiveCompositionJob):
        handler._send(409, {
            "detail": str(error)[:220], "code": "active_composition_job",
            "retry_after_ms": 2000, **terminal,
        })
    elif isinstance(error, short_drama_assembly.PreviewBlocked):
        status = {
            "export_unavailable": 503,
            "subtitle_font_unavailable": 503,
            "revision_conflict": 409,
            "preview_stale": 409,
            "preview_invalid": 409,
            "export_stage_invalid": 409,
            "final_missing": 409,
        }.get(error.code, 400)
        handler._send(
            status,
            {"detail": str(error)[:220], "code": error.code, **terminal},
        )
    elif isinstance(error, short_drama_completion.CompletionError):
        payload = {
            "detail": str(error)[:220],
            "code": error.code,
            **terminal,
        }
        if error.blockers:
            payload["blockers"] = error.blockers
        handler._send(error.status, payload)
    elif isinstance(error, short_drama_playback.PlaybackError):
        handler._send(error.status, {
            "detail": str(error)[:220],
            "code": error.code,
            **terminal,
        })
    elif isinstance(error, short_drama_voice.VoiceQuoteConsumed):
        handler._send(409, {
            "detail": str(error)[:220], "code": "idempotency_conflict", **terminal,
        })
    elif isinstance(error, short_drama_voice.VoiceChargeInProgress):
        handler._send(409, {
            "detail": str(error)[:220], "code": "charge_attempt_in_progress", **terminal,
        })
    elif isinstance(error, short_drama_voice.VoiceTimelineValidationError):
        blocker = dict(error.blocker)
        blocker["detail"] = str(error)[:220]
        blocker.update(terminal)
        handler._send(422, blocker)
    elif isinstance(error, short_drama_alignment.AlignmentError):
        handler._send(error.status, {
            "detail": str(error)[:220],
            "code": error.code,
            "blockers": error.blockers,
            **terminal,
        })
    elif isinstance(error, short_drama_timeline.TimelineError):
        handler._send(error.status, {
            "detail": str(error)[:220],
            "code": error.code,
            "blockers": error.blockers,
            **terminal,
        })
    elif isinstance(error, short_drama_lipsync.LipsyncQuoteError):
        payload = {
            "detail": str(error)[:220],
            "code": error.code,
            **terminal,
        }
        if error.blockers:
            payload["blockers"] = error.blockers
        handler._send(error.status, payload)
    elif isinstance(error, short_drama_lipsync.LipsyncJobError):
        payload = {
            "detail": str(error)[:220],
            "code": error.code,
            **terminal,
        }
        if error.retry_after_ms:
            payload["retry_after_ms"] = error.retry_after_ms
        handler._send(error.status, payload)
    elif isinstance(error, short_drama_lipsync.LipsyncVersionError):
        payload = {
            "detail": str(error)[:220],
            "code": error.code,
            **terminal,
        }
        if error.blockers:
            payload["blockers"] = error.blockers
        handler._send(error.status, payload)
    elif isinstance(error, short_drama_lipsync_faces.FaceAnalysisError):
        handler._send(error.status, {
            "detail": str(error)[:220],
            "code": error.code,
            "blockers": error.blockers,
            **terminal,
        })
    elif isinstance(error, short_drama_video.VideoQuoteConsumed):
        handler._send(409, {
            "detail": str(error)[:220], "code": "idempotency_conflict", **terminal,
        })
    elif isinstance(error, short_drama_video.VideoChargeInProgress):
        handler._send(409, {
            "detail": str(error)[:220], "code": "charge_attempt_in_progress", **terminal,
        })
    elif isinstance(error, short_drama_video.VideoCastConflict):
        handler._send(409, {
            "detail": str(error)[:220], "code": error.code, **terminal,
        })
    elif isinstance(error, short_drama_video.VideoBlocked):
        handler._send(400, {
            "detail": str(error)[:220], "code": error.code, **terminal,
        })
    elif isinstance(error, short_drama_sound_design.SoundDesignError):
        handler._send(error.status, {
            "detail": str(error)[:220], "code": error.code, **terminal,
        })
    elif isinstance(error, PointBudgetExceeded):
        handler._send(400, {"detail": str(error)[:220], "code": "point_budget_exceeded", **terminal})
    elif isinstance(error, PermissionError):
        handler._send(403, {"detail": str(error)[:220], "code": "forbidden", **terminal})
    elif error.__class__.__name__ == "AuthPointsError":
        status = int(getattr(error, "status", 502) or 502)
        payload = {
            "detail": str(getattr(error, "detail", error))[:220],
            "code": "charge_rejected" if status == 402 else "charge_unavailable",
            **terminal,
        }
        error_data = getattr(error, "data", None)
        if status == 402 and isinstance(error_data, dict) and error_data.get("need") is not None:
            payload["need"] = error_data["need"]
        handler._send(
            status if status in {400, 402, 403, 502, 503} else 502,
            payload,
        )
    else:
        handler._send(400, {"detail": str(error)[:220], **terminal})


def _request_object(handler):
    body = handler._json_body_strict()
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return body


def _project_id_from_query(handler):
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
    project_id = (query.get("id") or [""])[0].strip()
    if not project_id:
        raise ValueError("缺少短剧项目 ID")
    return project_id


def _planning_project_id_from_query(handler):
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
    project_id = (query.get("project_id") or [""])[0].strip()
    if not project_id:
        raise ValueError("缺少短剧项目 ID")
    return project_id


def _project_pagination_from_query(handler):
    query = urllib.parse.parse_qs(
        urllib.parse.urlparse(handler.path).query, keep_blank_values=True
    )

    def parse(name, default, maximum=None):
        values = query.get(name)
        if not values:
            return default
        raw = values[0]
        if not raw or not raw.isdigit():
            raise ValueError("分页参数无效")
        return _validate_page(int(raw), default, maximum)

    return (
        parse("page", 1),
        parse("page_size", DEFAULT_PROJECT_PAGE_SIZE, MAX_PROJECT_PAGE_SIZE),
    )


def _validate_project_request(body, expected_fields):
    if set(body) != expected_fields:
        raise ValueError("请求字段不正确")
    if not isinstance(body.get("project_id"), str) or not body["project_id"].strip():
        raise ValueError("短剧项目 ID 无效")
    if type(body.get("revision")) is not int:
        raise ValueError("项目版本无效")


def _planning_job(db_factory, username, job_id, project_id):
    if type(job_id) is not int:
        raise ValueError("规划任务 ID 无效")
    with closing(db_factory()) as conn:
        job = conn.execute(
            "SELECT id, kind, username, cost, status, payload, result FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    if not job or job["username"] != username:
        raise LookupError("规划任务不存在")
    if job["kind"] != "copy" or job["status"] != "done":
        raise ValueError("规划任务尚未完成")
    try:
        payload = json.loads(job["payload"] or "{}")
        result = json.loads(job["result"] or "{}")
    except (TypeError, ValueError):
        raise ValueError("规划任务结果无效")
    if not isinstance(result, dict) or result.get("mode") != "short_drama" or not isinstance(result.get("plan"), dict):
        raise ValueError("规划任务结果不是短剧规划")
    metadata = _planning_metadata(payload, result)
    if metadata["project_id"] != project_id:
        raise ValueError("规划任务不属于当前短剧项目")
    return job, result["plan"], metadata


def dispatch_http(handler, method, db_factory, verify_token, cost_of=None, avatar_lookup=None,
                  mutation_lock=None, canvas_access_resolver=None, voice_validator=None,
                  points_getter=None, audio_asset_lookup=None, audio_asset_list=None,
                  enqueue_job=None,
                  shared_video_submission_limit=None,
                  deduct_points=None, refund_points=None, charge_lookup=None,
                  avatar_list=None, audio_asset_job_lookup=None,
                  audio_asset_recorder=None, lipsync_provider_ready=None,
                  lipsync_wake=None):
    """Handle the domain's synchronous routes inside core.H; return whether matched."""
    path = handler.path.split("?", 1)[0]
    route_path = path
    if path.startswith("/api/gen/short-drama/playback/jobs/"):
        route_path = "/api/gen/short-drama/playback/jobs/{job_id}"
    elif path.startswith("/api/gen/short-drama/conversation/jobs/"):
        route_path = "/api/gen/short-drama/conversation/jobs/{job_id}"
    elif path.startswith("/api/gen/short-drama/autodraft/jobs/"):
        if path.endswith("/retry"):
            route_path = "/api/gen/short-drama/autodraft/jobs/{job_id}/retry"
        elif path.endswith("/cancel"):
            route_path = "/api/gen/short-drama/autodraft/jobs/{job_id}/cancel"
        else:
            route_path = "/api/gen/short-drama/autodraft/jobs/{job_id}"
    elif path.startswith("/api/gen/short-drama/autodraft/provider-jobs/"):
        route_path = (
            "/api/gen/short-drama/autodraft/provider-jobs/{job_id}/reconcile"
            if path.endswith("/reconcile")
            else "/api/gen/short-drama/autodraft/provider-jobs/{job_id}"
        )
    elif path.startswith("/api/gen/short-drama/refinement/jobs/"):
        route_path = "/api/gen/short-drama/refinement/jobs/{job_id}"
    elif path.startswith("/api/gen/short-drama/delivery/jobs/"):
        route_path = "/api/gen/short-drama/delivery/jobs/{job_id}"
    elif path.startswith("/api/gen/short-drama/lipsync/jobs/"):
        if path.endswith("/retry"):
            route_path = "/api/gen/short-drama/lipsync/jobs/{job_id}/retry"
        elif path.endswith("/cancel"):
            route_path = "/api/gen/short-drama/lipsync/jobs/{job_id}/cancel"
        else:
            route_path = "/api/gen/short-drama/lipsync/jobs/{job_id}"
    elif path.startswith("/api/gen/short-drama/lipsync/faces/analyses/"):
        route_path = (
            "/api/gen/short-drama/lipsync/faces/analyses/{analysis_id}"
        )
    elif path.startswith("/api/gen/short-drama/lipsync/versions/"):
        if path.endswith("/select"):
            route_path = (
                "/api/gen/short-drama/lipsync/versions/{version_id}/select"
            )
        elif path.endswith("/lock"):
            route_path = (
                "/api/gen/short-drama/lipsync/versions/{version_id}/lock"
            )
    elif (
        path.startswith("/api/gen/short-drama/playback/versions/")
        and path.endswith("/select")
    ):
        route_path = (
            "/api/gen/short-drama/playback/versions/{version_id}/select"
        )
    elif path.startswith("/api/gen/short-drama/final-assets/"):
        route_path = "/api/gen/short-drama/final-assets/{asset_id}"
    if route_path not in _HTTP_ROUTES.get(method, ()):
        return False
    user = verify_token(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录"})
        return True
    if user.get("must_change"):
        handler._send(403, {"detail": "请先修改初始密码后再使用"})
        return True
    username = user["username"]
    access = canvas_access_resolver(handler) if callable(canvas_access_resolver) else None
    if avatar_lookup is None:
        from . import video
        avatar_lookup = video.get_video_avatar
    if avatar_list is None:
        from . import video
        avatar_list = video.list_video_avatars
    try:
        if method == "GET" and path == "/api/gen/short-drama/asset-graph":
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False,
            )
            handler._send(200, short_drama_asset_graph.workspace(
                db_factory, owner, project_id,
            ))
        elif method == "GET" and path == "/api/gen/short-drama/asset-graph/scenes":
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False,
            )
            handler._send(200, short_drama_asset_graph.scene_workspace(
                db_factory, owner, project_id,
            ))
        elif method == "GET" and path == "/api/gen/short-drama/asset-graph/shot-package":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
            project_id = (query.get("project_id") or [""])[0].strip()
            shot_id = (query.get("shot_id") or [""])[0].strip()
            if not project_id or not shot_id:
                raise ValueError("缺少短剧项目或镜头 ID")
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False,
            )
            handler._send(200, short_drama_asset_graph.current_package(
                db_factory, owner, project_id, shot_id,
            ))
        elif method == "POST" and path.startswith(
            "/api/gen/short-drama/asset-graph/"
        ):
            body = _request_object(handler)
            project_id = _text(body.get("project_id"), 160)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=True,
            )
            actions = {
                "/api/gen/short-drama/asset-graph/sync": lambda: (
                    short_drama_asset_graph.sync_foundation(
                        db_factory, owner, username, project_id,
                        body.get("graph_revision"),
                    )
                ),
                "/api/gen/short-drama/asset-graph/assets": lambda: (
                    short_drama_asset_graph.create_asset(
                        db_factory, owner, username, body,
                    )
                ),
                "/api/gen/short-drama/asset-graph/versions": lambda: (
                    short_drama_asset_graph.create_version(
                        db_factory, owner, username, body,
                    )
                ),
                "/api/gen/short-drama/asset-graph/versions/lock": lambda: (
                    short_drama_asset_graph.lock_version(
                        db_factory, owner, username, body,
                    )
                ),
                "/api/gen/short-drama/asset-graph/bindings": lambda: (
                    short_drama_asset_graph.bind_asset(
                        db_factory, owner, username, body,
                    )
                ),
                "/api/gen/short-drama/asset-graph/snapshots": lambda: (
                    short_drama_asset_graph.build_snapshot(
                        db_factory, owner, username, body,
                    )
                ),
                "/api/gen/short-drama/asset-graph/scenes/reference": lambda: (
                    short_drama_asset_graph.set_scene_reference(
                        db_factory, owner, username, body,
                    )
                ),
                "/api/gen/short-drama/asset-graph/scenes/lock": lambda: (
                    short_drama_asset_graph.lock_scene_reference(
                        db_factory, owner, username, body,
                    )
                ),
                "/api/gen/short-drama/asset-graph/scenes": lambda: (
                    short_drama_asset_graph.create_scene(
                        db_factory, owner, username, body,
                    )
                ),
                "/api/gen/short-drama/asset-graph/scenes/update": lambda: (
                    short_drama_asset_graph.update_scene(
                        db_factory, owner, username, body,
                    )
                ),
                "/api/gen/short-drama/asset-graph/scenes/bind-shot": lambda: (
                    short_drama_asset_graph.bind_scene_to_shot(
                        db_factory, owner, username, body,
                    )
                ),
                "/api/gen/short-drama/asset-graph/scenes/delete": lambda: (
                    short_drama_asset_graph.delete_scene(
                        db_factory, owner, username, body,
                    )
                ),
                "/api/gen/short-drama/asset-graph/scenes/restore": lambda: (
                    short_drama_asset_graph.restore_scene(
                        db_factory, owner, username, body,
                    )
                ),
            }
            handler._send(200, actions[path]())
        elif method == "POST" and path == "/api/gen/short-drama/advisor":
            handler._send(200, short_drama_advisor.advise(
                _request_object(handler), username=username, db_factory=db_factory,
            ))
        elif (
            method == "GET"
            and path.startswith("/api/gen/short-drama/conversation/jobs/")
        ):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False,
            )
            handler._send(200, short_drama_conversation.get_job(
                db_factory, owner, project_id, path.rsplit("/", 1)[-1],
            ))
        elif (
            method == "GET"
            and path.startswith("/api/gen/short-drama/autodraft/provider-jobs/")
        ):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False,
            )
            handler._send(200, short_drama_autodraft.reconcile_provider_job(
                db_factory, owner, project_id, path.rsplit("/", 1)[-1],
                refund_points=refund_points, charge_lookup=charge_lookup,
            ))
        elif (
            method == "GET"
            and path.startswith("/api/gen/short-drama/autodraft/jobs/")
        ):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False,
            )
            handler._send(200, short_drama_autodraft.get_job(
                db_factory, owner, project_id, path.rsplit("/", 1)[-1],
            ))
        elif (
            method == "GET"
            and path.startswith("/api/gen/short-drama/refinement/jobs/")
        ):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False,
            )
            handler._send(200, short_drama_refinement.get_refinement_job(
                db_factory, owner, project_id, path.rsplit("/", 1)[-1],
            ))
        elif (
            method == "GET"
            and path.startswith("/api/gen/short-drama/delivery/jobs/")
        ):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False,
            )
            handler._send(200, short_drama_refinement.get_delivery_job(
                db_factory, owner, project_id, path.rsplit("/", 1)[-1],
                refund_points=refund_points,
            ))
        elif method == "GET" and path.endswith("/conversation"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False,
            )
            role = str((access or {}).get("role") or "").lower()
            handler._send(200, short_drama_conversation.workspace(
                db_factory, owner, username, project_id,
                can_edit=(not role or role in {"owner", "editor"}),
            ))
        elif method == "GET" and path.endswith("/character-studio"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False,
            )
            role = str((access or {}).get("role") or "").lower()
            reconcile_project_character_references(
                db_factory, owner, project_id
            )
            handler._send(200, short_drama_character_studio.workspace(
                db_factory, owner, username, project_id,
                can_edit=(not role or role in {"owner", "editor"}),
                avatar_list=avatar_list,
            ))
        elif method == "GET" and path.endswith("/preflight"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False,
            )
            role = str((access or {}).get("role") or "").lower()
            handler._send(200, short_drama_preflight.workspace(
                db_factory, owner, username, project_id,
                can_edit=(not role or role in {"owner", "editor"}),
            ))
        elif method == "GET" and path.endswith("/autodraft"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False,
            )
            role = str((access or {}).get("role") or "").lower()
            handler._send(200, short_drama_autodraft.workspace(
                db_factory, owner, username, project_id,
                can_edit=(not role or role in {"owner", "editor"}),
                avatar_list=avatar_list,
                refund_points=refund_points,
            ))
        elif method == "GET" and path.endswith("/refinement"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False,
            )
            role = str((access or {}).get("role") or "").lower()
            handler._send(200, short_drama_refinement.workspace(
                db_factory, owner, username, project_id,
                can_edit=(not role or role in {"owner", "editor"}),
            ))
        elif (
            method == "POST"
            and path.startswith("/api/gen/short-drama/conversation/")
        ):
            body = _request_object(handler)
            project_id = str(body.get("project_id") or "")
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=True,
            )
            operation = {
                "/api/gen/short-drama/conversation/messages":
                    short_drama_conversation.send_message,
                "/api/gen/short-drama/conversation/script/generate":
                    short_drama_conversation.generate_script,
                "/api/gen/short-drama/conversation/script/shot/update":
                    short_drama_conversation.update_shot,
                "/api/gen/short-drama/conversation/script/shot/regenerate":
                    short_drama_conversation.regenerate_shot,
                "/api/gen/short-drama/conversation/script/shot/lock":
                    short_drama_conversation.set_shot_lock,
                "/api/gen/short-drama/conversation/script/shot/structure":
                    short_drama_conversation.change_shot_structure,
                "/api/gen/short-drama/conversation/script/restore":
                    short_drama_conversation.restore_version,
                "/api/gen/short-drama/conversation/script/lock":
                    short_drama_conversation.lock_script,
            }[path]
            args = (
                db_factory, owner, username, body,
                str(handler.headers.get("Idempotency-Key") or ""),
            )
            if mutation_lock is not None:
                with mutation_lock:
                    result = operation(*args)
            else:
                result = operation(*args)
            handler._send(200, result)
        elif (
            method == "POST"
            and path.startswith("/api/gen/short-drama/character-studio/")
        ):
            body = _request_object(handler)
            project_id = str(body.get("project_id") or "")
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=True,
            )
            if path.endswith("/profile"):
                operation = short_drama_character_studio.save_profile
                args = (db_factory, owner, body)
                kwargs = {}
            else:
                operation = short_drama_character_studio.bind_avatar
                args = (db_factory, owner, body)
                kwargs = {"avatar_lookup": avatar_lookup}
            if mutation_lock is not None:
                with mutation_lock:
                    result = operation(*args, **kwargs)
            else:
                result = operation(*args, **kwargs)
            handler._send(200, result)
        elif (
            method == "POST"
            and path.startswith("/api/gen/short-drama/preflight/")
        ):
            body = _request_object(handler)
            project_id = str(body.get("project_id") or "")
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=True,
            )
            operation = {
                "/api/gen/short-drama/preflight/generate":
                    short_drama_preflight.generate_plan,
                "/api/gen/short-drama/preflight/confirm":
                    short_drama_preflight.confirm_plan,
            }[path]
            args = (
                db_factory, owner, username, body,
                str(handler.headers.get("Idempotency-Key") or ""),
            )
            if mutation_lock is not None:
                with mutation_lock:
                    result = operation(*args)
            else:
                result = operation(*args)
            handler._send(200, result)
        elif (
            method == "POST"
            and path.startswith("/api/gen/short-drama/autodraft/")
        ):
            body = _request_object(handler)
            project_id = str(body.get("project_id") or "")
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=True,
            )
            if path == "/api/gen/short-drama/autodraft/provider-preflight":
                result = short_drama_autodraft.preview_provider_request(
                    db_factory, owner, username, body,
                    avatar_lookup=avatar_lookup,
                )
            elif path == "/api/gen/short-drama/autodraft/provider-quote":
                result = short_drama_autodraft.create_provider_quote(
                    db_factory, owner, username, body,
                    avatar_lookup=avatar_lookup,
                )
            elif path == "/api/gen/short-drama/autodraft/provider-version/select":
                result = short_drama_autodraft.select_provider_version(
                    db_factory, owner, body,
                )
            elif path.endswith("/reconcile"):
                result = short_drama_autodraft.reconcile_unknown_provider_submission(
                    db_factory, owner, username, user.get("role"),
                    path.split("/")[-2], body,
                    refund_points=refund_points,
                )
            elif path == "/api/gen/short-drama/autodraft/provider-jobs":
                args = (
                    db_factory, owner, username, body,
                    str(handler.headers.get("Idempotency-Key") or ""),
                )
                kwargs = {
                    "avatar_lookup": avatar_lookup,
                    "deduct_points": deduct_points,
                    "refund_points": refund_points,
                    "charge_lookup": charge_lookup,
                    "project_usage": _project_point_usage,
                    "enqueue_job": enqueue_job,
                    "shared_video_submission_limit": (
                        shared_video_submission_limit
                    ),
                }
                if mutation_lock is not None:
                    with mutation_lock:
                        result = short_drama_autodraft.start_provider_job(
                            *args, **kwargs
                        )
                else:
                    result = short_drama_autodraft.start_provider_job(
                        *args, **kwargs
                    )
            elif path == "/api/gen/short-drama/autodraft/jobs":
                args = (
                    db_factory, owner, username, body,
                    str(handler.headers.get("Idempotency-Key") or ""),
                )
                kwargs = {
                    "deduct_points": deduct_points,
                    "refund_points": refund_points,
                    "charge_lookup": charge_lookup,
                    "project_usage": _project_point_usage,
                }
                if mutation_lock is not None:
                    with mutation_lock:
                        result = short_drama_autodraft.start_job(*args, **kwargs)
                else:
                    result = short_drama_autodraft.start_job(*args, **kwargs)
            elif path.endswith("/retry"):
                result = short_drama_autodraft.retry_job(
                    db_factory, owner, username,
                    dict(body, job_id=path.split("/")[-2]),
                )
            else:
                result = short_drama_autodraft.cancel_job(
                    db_factory, owner,
                    dict(body, job_id=path.split("/")[-2]),
                )
            handler._send(200, result)
        elif (
            method == "POST"
            and (
                path.startswith("/api/gen/short-drama/refinement/")
                or path.startswith("/api/gen/short-drama/delivery/")
            )
        ):
            body = _request_object(handler)
            project_id = str(body.get("project_id") or "")
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=True,
            )
            if path == "/api/gen/short-drama/refinement/changes/preview":
                result = short_drama_refinement.preview_change(
                    db_factory, owner, username, body,
                )
            elif path == "/api/gen/short-drama/refinement/candidates/adopt":
                result = short_drama_refinement.adopt_refinement_candidate(
                    db_factory, owner, username, body,
                    str(handler.headers.get("Idempotency-Key") or ""),
                )
            elif path == "/api/gen/short-drama/refinement/candidates/reassemble":
                result = short_drama_refinement.reassemble_refinement_candidates(
                    db_factory, owner, username, body,
                    str(handler.headers.get("Idempotency-Key") or ""),
                )
            elif path == "/api/gen/short-drama/refinement/jobs":
                result = short_drama_refinement.start_refinement_job(
                    db_factory, owner, username, body,
                    str(handler.headers.get("Idempotency-Key") or ""),
                )
            elif path == "/api/gen/short-drama/refinement/issues":
                result = short_drama_refinement.mark_issue(
                    db_factory, owner, username, body,
                )
            elif path == "/api/gen/short-drama/refinement/issues/keep-original":
                result = short_drama_refinement.keep_original_shot(
                    db_factory, owner, username, body,
                )
            elif path == "/api/gen/short-drama/refinement/media-preference":
                result = short_drama_refinement.set_media_preference(
                    db_factory, owner, username, body,
                )
            elif path == "/api/gen/short-drama/refinement/reassemble":
                result = short_drama_refinement.reassemble_refinement(
                    db_factory, owner, username, body,
                    str(handler.headers.get("Idempotency-Key") or ""),
                )
            elif path == "/api/gen/short-drama/refinement/confirm":
                result = short_drama_refinement.confirm_refinement(
                    db_factory, owner, username, body,
                )
            elif path == "/api/gen/short-drama/refinement/restore":
                result = short_drama_refinement.restore_refinement(
                    db_factory, owner, username, body,
                )
            elif path == "/api/gen/short-drama/delivery/quote":
                result = short_drama_refinement.create_delivery_quote(
                    db_factory, owner, body,
                )
            else:
                args = (
                    db_factory, owner, username, body,
                    str(handler.headers.get("Idempotency-Key") or ""),
                )
                kwargs = {
                    "deduct_points": deduct_points,
                    "refund_points": refund_points,
                    "charge_lookup": charge_lookup,
                    "project_usage": _project_point_usage,
                }
                if mutation_lock is not None:
                    with mutation_lock:
                        result = short_drama_refinement.start_delivery_job(
                            *args, **kwargs
                        )
                else:
                    result = short_drama_refinement.start_delivery_job(
                        *args, **kwargs
                    )
            handler._send(200, result)
        elif method == "GET" and path.endswith("/planning-quote"):
            if not callable(cost_of):
                raise ValueError("短剧策划报价暂不可用")
            cost = int(cost_of("copy", {"format": "short_drama"}))
            if cost < 0:
                raise ValueError("短剧策划报价无效")
            handler._send(200, {"cost": cost})
        elif method == "POST" and path.endswith("/asset-quote"):
            if not callable(cost_of):
                raise ValueError("关键帧报价暂不可用")
            body = _request_object(handler)
            _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(200, short_drama_production.prepare_still_quote(
                db_factory, username, body, cost_of, access
            ))
        elif method == "POST" and path.endswith("/sound-design/analyze"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(
                200, short_drama_sound_design.analyze(db_factory, owner, body)
            )
        elif method == "POST" and path.endswith("/sound-design/quote"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            quote = short_drama_sound_design.prepare_quote(
                db_factory, username, owner, body,
                point_usage=_project_point_usage,
            )
            if callable(points_getter):
                quote["points_left"] = max(0, int(points_getter(username)))
                quote["can_submit"] = (
                    quote["points_left"] >= quote["total_cost"]
                )
            handler._send(200, quote)
        elif method == "POST" and path.endswith("/sound-design/jobs"):
            if not all(callable(item) for item in (
                deduct_points, refund_points, enqueue_job,
            )):
                raise short_drama_sound_design.SoundDesignError(
                    "sound_design_submit_unavailable",
                    "AI 音效任务服务暂不可用", 503,
                )
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            submit_sound = lambda: short_drama_sound_design.submit(
                db_factory, username, owner, body,
                str(handler.headers.get("Idempotency-Key") or ""),
                deduct_points=deduct_points,
                refund_points=refund_points,
                enqueue=enqueue_job,
                point_usage=_project_point_usage,
            )
            if mutation_lock is not None:
                with mutation_lock:
                    result = submit_sound()
            else:
                result = submit_sound()
            handler._send(200 if result["replayed"] else 202, result)
        elif method == "POST" and path.endswith("/sound-design/apply"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(200, short_drama_sound_design.apply_generated(
                db_factory, owner, body,
                assembly_module=short_drama_assembly,
                audio_asset_lookup=audio_asset_lookup,
            ))
        elif method == "POST" and path.endswith("/voice-quote"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            quote = short_drama_voice.prepare_voice_quote(
                db_factory, username, owner, body, cost_of, voice_validator
            )
            if callable(points_getter):
                quote["points_left"] = max(0, int(points_getter(username)))
                quote["can_submit"] = (
                    quote["can_submit"] and
                    quote["points_left"] >= quote["total_cost"]
                )
            handler._send(200, quote)
        elif method == "POST" and path.endswith("/subtitle-alignment/jobs"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            result = short_drama_alignment.create_job(
                db_factory, owner, body,
                str(handler.headers.get("Idempotency-Key") or ""),
            )
            handler._send(
                200 if result["replayed"] or result["reused"] else 202,
                result,
            )
        elif method == "POST" and path.endswith("/subtitle-alignment/timeline"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(
                200, short_drama_alignment.save_timeline(
                    db_factory, owner, body, actor_username=username
                )
            )
        elif method == "POST" and path.endswith("/subtitle-alignment/lock"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(
                200, short_drama_alignment.lock_version(db_factory, owner, body)
            )
        elif method == "POST" and path.endswith("/subtitle-alignment/cancel"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(
                200, short_drama_alignment.cancel_job(db_factory, owner, body)
            )
        elif method == "POST" and path.endswith("/master-timeline/rebuild"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(200, short_drama_timeline.rebuild(
                db_factory, owner, username, body,
                str(handler.headers.get("Idempotency-Key") or ""),
            ))
        elif method == "POST" and path.endswith("/master-timeline/confirm"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(200, short_drama_timeline.confirm(
                db_factory, owner, username, body,
                str(handler.headers.get("Idempotency-Key") or ""),
            ))
        elif method == "POST" and path.endswith("/lipsync/faces/analyze"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(
                200,
                short_drama_lipsync_faces.analyze(
                    db_factory, owner, username, body
                ),
            )
        elif method == "POST" and path.endswith("/lipsync/faces/confirm"):
            body = _request_object(handler)
            analysis_id = str(body.get("analysis_id") or "")
            project_id = short_drama_lipsync_faces.analysis_project_id(
                db_factory, analysis_id
            )
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=True,
            )
            handler._send(
                200,
                short_drama_lipsync_faces.confirm(
                    db_factory, owner, username, body
                ),
            )
        elif method == "POST" and path.endswith("/lipsync/quote"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            short_drama_lipsync_rollout.require(
                db_factory, username, str(body.get("project_id") or ""),
                operation="quote", provider=str(body.get("provider") or ""),
            )
            handler._send(200, short_drama_lipsync.create_quote(
                db_factory, username, owner, body
            ))
        elif method == "POST" and path.endswith("/lipsync/jobs"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            short_drama_lipsync_rollout.require(
                db_factory, username, str(body.get("project_id") or ""),
                operation="create",
                provider=short_drama_lipsync_rollout.quote_provider(
                    db_factory, body.get("quote_id")
                ),
            )
            handler._send(202, short_drama_lipsync.create_job(
                db_factory, username, owner, body,
                str(handler.headers.get("Idempotency-Key") or ""),
                deduct_points=deduct_points, refund_points=refund_points,
                charge_lookup=charge_lookup,
                provider_ready=lipsync_provider_ready,
                enqueue=lipsync_wake,
            ))
        elif method == "POST" and (
            "/lipsync/jobs/" in path
            and (path.endswith("/retry") or path.endswith("/cancel"))
        ):
            parts = path.rstrip("/").split("/")
            job_id = parts[-2]
            project_id = short_drama_lipsync.job_project_id(db_factory, job_id)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=True
            )
            if path.endswith("/retry"):
                short_drama_lipsync_rollout.require(
                    db_factory, username, project_id, operation="retry",
                    provider=short_drama_lipsync_rollout.job_provider(
                        db_factory, job_id
                    ),
                )
            operation = (
                short_drama_lipsync.retry_job
                if path.endswith("/retry")
                else short_drama_lipsync.cancel_job
            )
            result = operation(db_factory, owner, job_id)
            if callable(lipsync_wake):
                lipsync_wake()
            handler._send(200, result)
        elif (
            method == "POST"
            and path.startswith("/api/gen/short-drama/lipsync/versions/")
            and path.endswith("/lock")
        ):
            version_id = path.split("/")[-2]
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            short_drama_lipsync_rollout.require(
                db_factory, username, str(body.get("project_id") or ""),
                operation="lock",
            )
            handler._send(200, short_drama_lipsync.lock_version(
                db_factory, owner, username, body, version_id
            ))
        elif method == "POST" and path.endswith("/video-quote"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            quote = short_drama_video.prepare_video_quote(
                db_factory, username, owner, body, cost_of, avatar_lookup
            )
            if callable(points_getter):
                quote["points_left"] = max(0, int(points_getter(username)))
                quote["can_submit"] = (
                    quote["can_submit"]
                    and
                    quote["points_left"] >= quote["total_cost"]
                )
            handler._send(200, quote)
        elif method == "POST" and path.endswith("/video-cast"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            if mutation_lock is not None:
                with mutation_lock:
                    saved = short_drama_video.save_video_cast(
                        db_factory, owner, body, avatar_lookup
                    )
            else:
                saved = short_drama_video.save_video_cast(
                    db_factory, owner, body, avatar_lookup
                )
            handler._send(200, saved)
        elif method == "PUT" and path.endswith("/assembly/config"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            if mutation_lock is not None:
                with mutation_lock:
                    saved = short_drama_assembly.save_assembly_config(
                        db_factory, owner, body, audio_asset_lookup
                    )
            else:
                saved = short_drama_assembly.save_assembly_config(
                    db_factory, owner, body, audio_asset_lookup
                )
            handler._send(200, saved)
        elif method == "POST" and path.endswith((
            "/playback/remux", "/playback/refresh"
        )):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            remux_args = (
                db_factory, username, owner, body,
                str(handler.headers.get("Idempotency-Key") or ""),
            )
            if mutation_lock is not None:
                with mutation_lock:
                    result = short_drama_playback.create_remux_job(
                        *remux_args, enqueue=enqueue_job
                    )
            else:
                result = short_drama_playback.create_remux_job(
                    *remux_args, enqueue=enqueue_job
                )
            handler._send(
                200 if result.get("replayed") else 202, result
            )
        elif method == "POST" and path.endswith("/assembly/preview"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            short_drama_lipsync_rollout.require_project_operation(
                db_factory, username, str(body.get("project_id") or ""),
                operation="preview",
            )
            preview_args = (
                db_factory, username, owner, body,
                str(handler.headers.get("Idempotency-Key") or ""),
            )
            if mutation_lock is not None:
                with mutation_lock:
                    result = short_drama_assembly.create_preview_job(
                        *preview_args, enqueue=enqueue_job,
                        bgm_lookup=audio_asset_lookup,
                    )
            else:
                result = short_drama_assembly.create_preview_job(
                    *preview_args, enqueue=enqueue_job,
                    bgm_lookup=audio_asset_lookup,
                )
            handler._send(200 if result["replayed"] else 202, result)
        elif method == "POST" and path.endswith("/assembly/final-quote"):
            from . import cos
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            short_drama_lipsync_rollout.require_project_operation(
                db_factory, username, str(body.get("project_id") or ""),
                operation="final_quote",
            )
            final_cost = max(0, int(os.environ.get(
                "SHORT_DRAMA_FINAL_COST", "0"
            ) or 0))
            with closing(db_factory()) as budget_conn:
                budget_project = budget_conn.execute(
                    "SELECT point_budget FROM short_drama_projects "
                    "WHERE id=? AND username=? AND deleted=0",
                    (str(body.get("project_id") or ""), owner),
                ).fetchone()
                usage = _project_point_usage(
                    budget_conn, str(body.get("project_id") or "")
                )
            budget = int(budget_project[0] or 0) if budget_project else 0
            if (
                budget > 0
                and usage["spent_points"] + usage["reserved_points"]
                + final_cost > budget
            ):
                raise PointBudgetExceeded("正式导出超出项目点数预算")
            result = short_drama_assembly.create_final_quote(
                db_factory, username, owner, body, final_cost,
                storage_available=cos.enabled(),
                bgm_lookup=audio_asset_lookup,
            )
            if callable(points_getter):
                result["points_left"] = max(0, int(points_getter(username)))
                result["can_submit"] = (
                    result["points_left"] >= result["total_cost"]
                )
            handler._send(200, result)
        elif method == "POST" and path.endswith("/assembly/export"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            short_drama_lipsync_rollout.require_project_operation(
                db_factory, username, str(body.get("project_id") or ""),
                operation="export",
            )
            export_args = (
                db_factory, username, owner, body,
                str(handler.headers.get("Idempotency-Key") or ""),
            )
            if mutation_lock is not None:
                with mutation_lock:
                    result = short_drama_assembly.create_final_job(
                        *export_args, deduct_points=deduct_points,
                        refund_points=refund_points, enqueue=enqueue_job,
                        bgm_lookup=audio_asset_lookup,
                        point_usage=_project_point_usage,
                        charge_lookup=charge_lookup,
                    )
            else:
                result = short_drama_assembly.create_final_job(
                    *export_args, deduct_points=deduct_points,
                    refund_points=refund_points, enqueue=enqueue_job,
                    bgm_lookup=audio_asset_lookup,
                    point_usage=_project_point_usage,
                    charge_lookup=charge_lookup,
                )
            handler._send(200 if result["replayed"] else 202, result)
        elif method == "POST" and path.endswith("/completion/confirm"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=False,
            )
            short_drama_lipsync_rollout.require_project_operation(
                db_factory, username, str(body.get("project_id") or ""),
                operation="completion",
            )
            confirm_args = (
                db_factory, username, owner,
                str((access or {}).get("board_id") or ""),
                body, str(handler.headers.get("Idempotency-Key") or ""),
            )
            if mutation_lock is not None:
                with mutation_lock:
                    result = short_drama_completion.confirm(
                        *confirm_args, point_usage=_project_point_usage
                    )
            else:
                result = short_drama_completion.confirm(
                    *confirm_args, point_usage=_project_point_usage
                )
            handler._send(200, result)
        elif method == "POST" and path.endswith("/assembly/confirm"):
            short_drama_completion.reject_legacy_completion()
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(
                200,
                short_drama_assembly.confirm_final(
                    db_factory, owner, body,
                ),
            )
        elif method == "POST" and path.endswith("/select-asset"):
            body = _request_object(handler)
            _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            if mutation_lock is not None:
                with mutation_lock:
                    selected = short_drama_production.select_asset(
                        db_factory, username, body, access
                    )
            else:
                selected = short_drama_production.select_asset(db_factory, username, body, access)
            handler._send(200, selected)
        elif method == "POST" and path.endswith("/select-voice-version"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            if mutation_lock is not None:
                with mutation_lock:
                    selected = short_drama_voice.select_voice_version(
                        db_factory, owner, body
                    )
            else:
                selected = short_drama_voice.select_voice_version(
                    db_factory, owner, body
                )
            handler._send(200, selected)
        elif method == "POST" and path.endswith("/select-video-version"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            if mutation_lock is not None:
                with mutation_lock:
                    selected = short_drama_video.select_video_version(
                        db_factory, owner, body, avatar_lookup
                    )
            else:
                selected = short_drama_video.select_video_version(
                    db_factory, owner, body, avatar_lookup
                )
            handler._send(200, selected)
        elif method == "POST" and path.endswith("/save-voice-timeline"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            if mutation_lock is not None:
                with mutation_lock:
                    saved = short_drama_voice.save_voice_timeline(
                        db_factory, owner, body
                    )
            else:
                saved = short_drama_voice.save_voice_timeline(
                    db_factory, owner, body
                )
            handler._send(200, saved)
        elif method == "POST" and path.endswith("/set-voice-shot-lock"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            if mutation_lock is not None:
                with mutation_lock:
                    locked = short_drama_voice.set_voice_shot_lock(
                        db_factory, owner, body
                    )
            else:
                locked = short_drama_voice.set_voice_shot_lock(
                    db_factory, owner, body
                )
            handler._send(200, locked)
        elif method == "POST" and path.endswith("/set-video-shot-lock"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            if mutation_lock is not None:
                with mutation_lock:
                    locked = short_drama_video.set_video_shot_lock(
                        db_factory, owner, body, avatar_lookup
                    )
            else:
                locked = short_drama_video.set_video_shot_lock(
                    db_factory, owner, body, avatar_lookup
                )
            handler._send(200, locked)
        elif method == "POST" and path.endswith("/confirm-production-stage"):
            body = _request_object(handler)
            if body.get("stage") == "video_review":
                owner = _project_username_for_access(
                    db_factory, username, str(body.get("project_id") or ""),
                    access, write=True,
                )
                if mutation_lock is not None:
                    with mutation_lock:
                        confirmed = short_drama_video.confirm_video_stage(
                            db_factory, owner, body, avatar_lookup
                        )
                else:
                    confirmed = short_drama_video.confirm_video_stage(
                        db_factory, owner, body, avatar_lookup
                    )
            else:
                if mutation_lock is not None:
                    with mutation_lock:
                        confirmed = short_drama_production.confirm_stage(
                            db_factory, username, body, access
                        )
                else:
                    confirmed = short_drama_production.confirm_stage(
                        db_factory, username, body, access
                    )
            handler._send(200, confirmed)
        elif method == "GET" and path.endswith("/projects"):
            page, page_size = _project_pagination_from_query(handler)
            handler._send(200, list_projects(
                db_factory, username, page, page_size, access))
        elif method == "GET" and path.endswith("/planning-job"):
            planning_project_id = _planning_project_id_from_query(handler)
            recovered = find_recoverable_planning_job(
                db_factory, username, planning_project_id, access=access
            )
            handler._send(200, recovered or {"job_id": None})
        elif method == "GET" and path.endswith("/production"):
            handler._send(200, short_drama_production.get_production(
                db_factory, username, _planning_project_id_from_query(handler), access
            ))
        elif method == "GET" and path.endswith("/voice"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            voice_workspace = short_drama_voice.get_voice_workspace(
                db_factory, owner, project_id
            )
            voice_workspace["alignment"] = short_drama_alignment.get_workspace(
                db_factory, owner, project_id
            )
            voice_workspace["master_timeline"] = (
                short_drama_timeline.get_snapshot(
                    db_factory, owner, project_id
                )
            )
            handler._send(200, voice_workspace)
        elif method == "GET" and path.endswith("/subtitle-alignment/workspace"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(
                200,
                short_drama_alignment.get_workspace(
                    db_factory, owner, project_id
                ),
            )
        elif method == "GET" and path.endswith("/master-timeline/versions"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(
                200,
                short_drama_timeline.get_versions(
                    db_factory, owner, project_id
                ),
            )
        elif method == "GET" and path.endswith("/master-timeline"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(
                200,
                short_drama_timeline.get_snapshot(
                    db_factory, owner, project_id
                ),
            )
        elif method == "GET" and path.endswith("/lipsync/snapshot"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            access_role = str((access or {}).get("role") or "").lower()
            can_write = (
                owner == username
                if not str((access or {}).get("board_id") or "")
                else access_role in {"owner", "editor"}
            )
            snapshot = short_drama_lipsync.get_snapshot(
                db_factory, owner, project_id, can_write=can_write
            )
            snapshot["rollout"] = short_drama_lipsync_rollout.evaluate(
                db_factory, username, project_id, operation="read"
            )
            handler._send(200, snapshot)
        elif method == "GET" and path.startswith(
            "/api/gen/short-drama/lipsync/faces/analyses/"
        ):
            analysis_id = path.rstrip("/").split("/")[-1]
            project_id = short_drama_lipsync_faces.analysis_project_id(
                db_factory, analysis_id
            )
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(
                200,
                short_drama_lipsync_faces.get_analysis(
                    db_factory, owner, analysis_id
                ),
            )
        elif method == "GET" and path.endswith("/lipsync/faces/current"):
            project_id = _planning_project_id_from_query(handler)
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(handler.path).query
            )
            shot_id = str((query.get("shot_id") or [""])[0]).strip()
            if not shot_id:
                raise ValueError("shot_id is required")
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(200, {
                "current": short_drama_lipsync_faces.get_current(
                    db_factory, owner, project_id, shot_id
                )
            })
        elif method == "GET" and "/lipsync/jobs/" in path:
            job_id = path.rstrip("/").split("/")[-1]
            project_id = short_drama_lipsync.job_project_id(db_factory, job_id)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(
                200, short_drama_lipsync.get_job(db_factory, owner, job_id)
            )
        elif method == "GET" and path.endswith((
            "/avatar-candidates", "/video-cast/avatars"
        )):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=True
            )
            handler._send(200, {
                "items": _safe_avatar_candidates(avatar_list, owner, 120),
                "can_create_avatar": owner == username,
            })
        elif method == "GET" and path.endswith("/video"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(
                200,
                short_drama_video.get_video_workspace(
                    db_factory, owner, project_id, avatar_lookup
                ),
            )
        elif method == "GET" and path.endswith("/completion/readiness"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(200, short_drama_completion.readiness(
                db_factory, username, owner, project_id,
                point_usage=_project_point_usage,
            ))
        elif method == "GET" and path.endswith("/completion"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(200, short_drama_completion.get_completion(
                db_factory, owner, project_id
            ))
        elif method == "GET" and path.startswith(
            "/api/gen/short-drama/playback/jobs/"
        ):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            try:
                job_id = int(path.rsplit("/", 1)[-1])
            except (TypeError, ValueError):
                raise ValueError("重封装任务 ID 无效")
            handler._send(200, short_drama_playback.get_job(
                db_factory, owner, project_id, job_id
            ))
        elif method == "GET" and path.endswith("/playback"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(200, short_drama_playback.get_snapshot(
                db_factory, owner, project_id
            ))
        elif method == "GET" and path.endswith("/assembly/audio-assets"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            if not callable(audio_asset_list):
                raise ValueError("音频资产列表暂不可用")
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(handler.path).query
            )
            try:
                limit = int((query.get("limit") or ["120"])[0])
            except (TypeError, ValueError):
                limit = 120
            handler._send(200, {
                "items": audio_asset_list(owner, limit),
            })
        elif method == "GET" and path.startswith(
            "/api/gen/short-drama/final-assets/"
        ):
            asset_id = urllib.parse.unquote(
                path.rsplit("/", 1)[-1]
            ).strip()
            project_id = short_drama_assembly.final_asset_project_id(
                db_factory, asset_id
            )
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(
                200,
                short_drama_assembly.get_final_asset(
                    db_factory, owner, asset_id
                ),
            )
        elif method == "GET" and path.endswith("/assembly"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            workspace = short_drama_assembly.get_assembly_workspace(
                db_factory, owner, project_id,
                bgm_lookup=audio_asset_lookup,
            )
            workspace["completion"] = short_drama_completion.readiness(
                db_factory, username, owner, project_id,
                point_usage=_project_point_usage,
            )
            if workspace["stage"] == "completed":
                workspace["actions"] = {
                    key: False for key in workspace.get("actions", {})
                }
            handler._send(200, workspace)
        elif method == "GET" and path.endswith("/sound-design/jobs"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(
                200, short_drama_sound_design.jobs(
                    db_factory, owner, project_id,
                    audio_asset_by_job=audio_asset_job_lookup,
                    record_audio_asset=audio_asset_recorder,
                )
            )
        elif method == "GET" and path.endswith("/sound-design"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(
                200, short_drama_sound_design.workspace(
                    db_factory, owner, project_id
                )
            )
        elif method == "GET":
            project_id = _project_id_from_query(handler)
            handler._send(200, get_project(db_factory, username, project_id, access))
        elif (
            method == "PUT"
            and path.startswith("/api/gen/short-drama/lipsync/versions/")
            and path.endswith("/select")
        ):
            version_id = path.split("/")[-2]
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            short_drama_lipsync_rollout.require(
                db_factory, username, str(body.get("project_id") or ""),
                operation="select",
            )
            handler._send(200, short_drama_lipsync.select_version(
                db_factory, owner, body, version_id
            ))
        elif method == "PUT" and path.endswith("/lipsync/speakers"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(200, short_drama_lipsync.update_speakers(
                db_factory, owner, username, body,
                str(handler.headers.get("Idempotency-Key") or ""),
            ))
        elif (
            method == "PUT"
            and path.startswith("/api/gen/short-drama/playback/versions/")
            and path.endswith("/select")
        ):
            version_id = path.split("/")[-2]
            body = _request_object(handler)
            if "version_id" in body and body["version_id"] != version_id:
                raise ValueError("播放版本 ID 不一致")
            body["version_id"] = version_id
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(200, short_drama_playback.select_version(
                db_factory, username, owner, body
            ))
        elif method == "PUT" and path.endswith("/master-timeline"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(200, short_drama_timeline.save_changes(
                db_factory, owner, username, body,
                str(handler.headers.get("Idempotency-Key") or ""),
            ))
        elif method == "PUT" and path.endswith("/sound-design/suggestions"):
            body = _request_object(handler)
            owner = _project_username_for_access(
                db_factory, username, str(body.get("project_id") or ""),
                access, write=True,
            )
            handler._send(200, short_drama_sound_design.update_suggestions(
                db_factory, owner, body
            ))
        elif method == "PUT":
            project_id = _project_id_from_query(handler)
            owner = _project_username_for_access(db_factory, username, project_id, access, write=True)
            body = _request_object(handler)
            if "revision" not in body:
                raise ValueError("缺少项目版本")
            revision = body.pop("revision")
            if type(revision) is not int:
                raise ValueError("项目版本无效")
            if mutation_lock is not None:
                with mutation_lock:
                    updated = update_project(
                        db_factory, owner, project_id, revision, body, avatar_lookup=avatar_lookup
                    )
            else:
                updated = update_project(
                    db_factory, owner, project_id, revision, body, avatar_lookup=avatar_lookup
                )
            handler._send(200, updated)
        elif path.endswith("/projects/live-action/core-story"):
            body = _request_object(handler)
            if mutation_lock is not None:
                with mutation_lock:
                    confirmed_story = confirm_live_action_core_story(
                        db_factory, username, body
                    )
            else:
                confirmed_story = confirm_live_action_core_story(
                    db_factory, username, body
                )
            handler._send(200, confirmed_story)
        elif path.endswith("/projects/live-action/finalize"):
            body = _request_object(handler)
            if mutation_lock is not None:
                with mutation_lock:
                    finalized = finalize_live_action_project(
                        db_factory, username, body
                    )
            else:
                finalized = finalize_live_action_project(
                    db_factory, username, body
                )
            handler._send(200, finalized)
        elif path.endswith("/projects/live-action/abandon"):
            body = _request_object(handler)
            if mutation_lock is not None:
                with mutation_lock:
                    deleted = abandon_live_action_project(
                        db_factory, username, body,
                        str(handler.headers.get("Idempotency-Key") or ""),
                    )
            else:
                deleted = abandon_live_action_project(
                    db_factory, username, body,
                    str(handler.headers.get("Idempotency-Key") or ""),
                )
            handler._send(200, deleted)
        elif path.endswith("/project/delete"):
            body = _request_object(handler)
            _validate_project_request(body, {"project_id", "revision"})
            owner = _project_username_for_access(
                db_factory, username, body["project_id"], access, write=True)
            if mutation_lock is not None:
                with mutation_lock:
                    deleted = delete_project(
                        db_factory, owner, body["project_id"], body["revision"]
                    )
            else:
                deleted = delete_project(
                    db_factory, owner, body["project_id"], body["revision"]
                )
            handler._send(200, deleted)
        elif path.endswith("/projects/promote"):
            handler._send(200, promote_planner_project(
                db_factory,
                username,
                _request_object(handler),
                str(handler.headers.get("Idempotency-Key") or ""),
                access,
            ))
        elif path.endswith("/projects/import"):
            handler._send(200, import_script_project(
                db_factory, username, _request_object(handler),
                str(handler.headers.get("Idempotency-Key") or ""),
            ))
        elif path.endswith("/projects"):
            handler._send(200, create_project(
                db_factory, username, _request_object(handler), access,
                str(handler.headers.get("Idempotency-Key") or ""),
            ))
        elif path.endswith("/apply-plan"):
            body = _request_object(handler)
            _validate_project_request(body, {"project_id", "revision", "job_id"})
            owner = _project_username_for_access(
                db_factory, username, body["project_id"], access, write=True)
            job, plan, metadata = _planning_job(
                db_factory, username, body["job_id"], body["project_id"]
            )
            handler._send(200, apply_plan(
                db_factory, owner, body["project_id"], body["revision"], plan,
                planning_cost=job["cost"], planning_job_id=job["id"],
                planning_metadata=metadata, avatar_lookup=avatar_lookup,
            ))
        else:
            body = _request_object(handler)
            _validate_project_request(body, {"project_id", "revision", "stage"})
            owner = _project_username_for_access(
                db_factory, username, body["project_id"], access, write=True)
            if not isinstance(body["stage"], str):
                raise ValueError("阶段确认请求无效")
            if mutation_lock is not None:
                with mutation_lock:
                    confirmed = confirm_stage(
                        db_factory, owner, body["project_id"], body["revision"],
                        body["stage"], avatar_lookup
                    )
            else:
                confirmed = confirm_stage(
                    db_factory, owner, body["project_id"], body["revision"],
                    body["stage"], avatar_lookup
                )
            handler._send(200, confirmed)
    except (LookupError, RevisionConflict, AppliedJobConflict, ProjectHasUnappliedJobs,
            PermissionError, ValueError, short_drama_voice.VoiceQuoteConsumed,
            short_drama_voice.VoiceChargeInProgress,
            short_drama_alignment.AlignmentError,
            short_drama_timeline.TimelineError,
            short_drama_lipsync_rollout.RolloutError,
            short_drama_lipsync.LipsyncQuoteError,
            short_drama_lipsync.LipsyncJobError,
            short_drama_lipsync.LipsyncVersionError,
            short_drama_lipsync_faces.FaceAnalysisError,
            short_drama_video.VideoQuoteConsumed,
            short_drama_video.VideoChargeInProgress,
            short_drama_video.VideoBlocked,
            short_drama_sound_design.SoundDesignError,
            short_drama_assembly.PreviewIdempotencyConflict,
            short_drama_assembly.ActiveCompositionJob,
            short_drama_assembly.PreviewBlocked,
            short_drama_playback.PlaybackError,
            short_drama_completion.CompletionError) as error:
        _http_error(handler, error)
    except Exception as error:
        if error.__class__.__name__ != "AuthPointsError":
            raise
        _http_error(handler, error)
    return True
