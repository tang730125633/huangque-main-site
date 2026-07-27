"""Persistence and optimistic-concurrency helpers for short-drama projects."""

import json
import os
import sqlite3
import time
import urllib.parse
import uuid
from contextlib import closing

from . import (
    short_drama_alignment,
    short_drama_assembly,
    short_drama_production,
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
    "synopsis", "ratio", "target_duration", "shot_count", "visual_style", "target_platform",
}


class RevisionConflict(RuntimeError):
    pass


class AppliedJobConflict(RuntimeError):
    pass


class PointBudgetExceeded(ValueError):
    pass


class ProjectLimitExceeded(ValueError):
    def __init__(self, max_projects):
        super().__init__("短剧项目数量已达上限")
        self.max_projects = max_projects


class ProjectHasUnappliedJobs(RuntimeError):
    pass


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
  visual_style TEXT NOT NULL DEFAULT '电影写实',
  target_platform TEXT NOT NULL DEFAULT '抖音',
  point_budget INTEGER NOT NULL DEFAULT 0,
  spent_points INTEGER NOT NULL DEFAULT 0,
  stage TEXT NOT NULL DEFAULT 'draft',
  revision INTEGER NOT NULL DEFAULT 1,
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_projects_owner
  ON short_drama_projects(username, deleted, updated_at DESC);

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
  voice_key TEXT,
  voice_settings_json TEXT NOT NULL DEFAULT '{}',
  sort_order INTEGER NOT NULL DEFAULT 0,
  UNIQUE(project_id, character_key)
);

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
        raise ValueError("短剧时长仅支持 30、45、60 秒")
    if shot_count not in SHOT_COUNTS:
        raise ValueError("分镜数量必须为 6-10 个")
    if target_duration % 5 or not 5 * shot_count <= target_duration <= 10 * shot_count:
        raise ValueError("短剧时长与分镜数量不匹配，无法组成 5/10 秒分镜")


def build_plan_prompt(settings):
    return (
        "为以下短剧需求生成可拍摄的完整规划。只输出一个 JSON 对象，不要解释，不要 markdown 代码块。\n"
        "需求：%s\n平台：%s；画幅：%s；总时长：%s 秒；分镜数：%s；视觉风格：%s。\n"
        "JSON 顶层必须且只能包含 title、logline、characters、script、shots。\n"
        "characters 是角色数组；每个角色必须包含 key、name、identity、personality、appearance_prompt、wardrobe_prompt，"
        "可选 voice_key、voice_settings。\n"
        "script 必须包含 hook、conflict、turn、ending、dialogue_lines；每条 dialogue_lines 必须包含 id、character_key、text。\n"
        "shots 是 6-10 条分镜数组；每条必须包含 key、duration、scene_description、camera_description、"
        "character_keys、dialogue_line_ids、image_prompt、video_prompt。duration 只能是 5 或 10，"
        "所有 duration 之和必须恰好为 %s；character_keys 和 dialogue_line_ids 只能引用前述已定义的键。"
    ) % (
        settings["prompt"], settings["platform"], settings["ratio"], settings["target_duration"],
        settings["shot_count"], settings["style"], settings["target_duration"],
    )


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
        if type(duration) is not int or duration not in {5, 10}:
            raise ValueError("分镜时长只能是 5 或 10 秒")
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
    if sum(shot["duration"] for shot in normalized_shots) != target_duration:
        raise ValueError("分镜总时长必须等于短剧目标时长")

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
    return normalize_plan(parsed, settings)


def _dict_rows(conn, query, params):
    cursor = conn.execute(query, params)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _table_columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def _charged_planning_points_by_project(conn, username, project_ids=None):
    wanted = set(project_ids) if project_ids is not None else None
    totals = {}
    job_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if not job_columns:
        return totals
    refund_filter = " AND COALESCE(refunded,0)<>1" if "refunded" in job_columns else ""
    rows = _dict_rows(
        conn,
        "SELECT cost, payload FROM jobs WHERE username=? AND kind='copy' "
        "AND COALESCE(cost,0)>0" + refund_filter,
        (username,),
    )
    for row in rows:
        payload = _json(row["payload"], {})
        project_id = payload.get("project_id") if isinstance(payload, dict) else None
        if (isinstance(payload, dict) and payload.get("format") == "short_drama" and project_id and
                (wanted is None or project_id in wanted)):
            totals[project_id] = totals.get(project_id, 0) + int(row["cost"] or 0)
    return totals


def _charged_planning_points(conn, username, project_id):
    return _charged_planning_points_by_project(conn, username, {project_id}).get(project_id, 0)


def _project_point_usage(conn, project_id):
    """Return one project-scoped ledger across planning and production charges."""
    project = conn.execute(
        "SELECT spent_points FROM short_drama_projects WHERE id=?", (project_id,)
    ).fetchone()
    legacy_spent = int(project[0] or 0) if project else 0
    actual = 0
    reserved = 0
    has_activity = False

    job_columns = _table_columns(conn, "jobs")
    if job_columns:
        refunded_expr = "COALESCE(refunded,0)" if "refunded" in job_columns else "0"
        for row in conn.execute(
                "SELECT cost,payload," + refunded_expr + " FROM jobs WHERE kind='copy'"
        ).fetchall():
            payload = _json(row[1], {})
            if (not isinstance(payload, dict) or payload.get("format") != "short_drama"
                    or payload.get("project_id") != project_id):
                continue
            has_activity = True
            if int(row[2] or 0) != 1:
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

    if not has_activity:
        actual = legacy_spent
    return {"spent_points": actual, "reserved_points": reserved}


def _has_unapplied_charged_job(conn, username, project_id):
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
    applied_ids = {
        int(row[0]) for row in conn.execute(
            "SELECT job_id FROM short_drama_applied_jobs WHERE project_id=? AND username=?",
            (project_id, username),
        ).fetchall()
    }
    rows = conn.execute(
        "SELECT id, payload FROM jobs WHERE username=? AND kind='copy' "
        "AND COALESCE(cost,0)>0 AND COALESCE(refunded,0)<>1",
        (username,),
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
    detail["spent_points"] = _charged_planning_points(conn, username, project_id)
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
    detail["shots"] = []
    for item in _dict_rows(conn,
        "SELECT * FROM short_drama_shots WHERE project_id=? ORDER BY script_version, sort_order, id",
        (project_id,),
    ):
        item["character_keys"] = _json(item.pop("character_keys_json"), [])
        item["dialogue_line_ids"] = _json(item.pop("dialogue_line_ids_json"), [])
        detail["shots"].append(item)
    return detail


def init_db(db_factory):
    conn = _connection(db_factory)
    try:
        conn.executescript(_SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(short_drama_projects)")}
        if "board_id" not in columns:
            conn.execute("ALTER TABLE short_drama_projects ADD COLUMN board_id TEXT")
        character_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(short_drama_characters)"
            )
        }
        for name, declaration in {
            "reference_job_id": "INTEGER",
            "reference_file": "TEXT NOT NULL DEFAULT ''",
            "reference_url": "TEXT NOT NULL DEFAULT ''",
            "reference_version": "INTEGER NOT NULL DEFAULT 0",
            "reference_locked": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in character_columns:
                conn.execute(
                    "ALTER TABLE short_drama_characters ADD COLUMN %s %s"
                    % (name, declaration)
                )
        conn.commit()
    finally:
        conn.close()
    short_drama_production.init_db(db_factory)
    short_drama_voice.init_db(db_factory)
    short_drama_alignment.init_db(db_factory)
    short_drama_video.init_db(db_factory)
    short_drama_assembly.init_db(db_factory)


def _project_username_for_access(db_factory, username, project_id, access=None, write=False):
    conn = _connection(db_factory)
    try:
        row = conn.execute(
            "SELECT username, board_id FROM short_drama_projects WHERE id=? AND deleted=0",
            (project_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise LookupError("short drama project does not exist")
    owner, board_id = row[0], row[1]
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


def create_project(db_factory, username, payload, access=None):
    data = validate_project_payload(payload)
    board_id = data.get("board_id")
    if board_id:
        access = access if isinstance(access, dict) else {}
        if (str(access.get("board_id") or "") != board_id
                or str(access.get("role") or "").lower() not in {"owner", "editor"}):
            raise PermissionError("current board role cannot create this project")
    now = int(time.time())
    project_id = str(uuid.uuid4())
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        max_projects = _max_projects_per_user()
        active_projects = conn.execute(
            "SELECT COUNT(*) FROM short_drama_projects WHERE username=? AND deleted=0",
            (username,),
        ).fetchone()[0]
        if active_projects >= max_projects:
            raise ProjectLimitExceeded(max_projects)
        conn.execute(
            "INSERT INTO short_drama_projects "
            "(id, username, board_id, title, synopsis, ratio, target_duration, shot_count, visual_style, "
            "target_platform, point_budget, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, username, data.get("board_id"), data["title"], data["synopsis"], data["ratio"],
             data["target_duration"], data["shot_count"], data["visual_style"],
             _text(data.get("target_platform") or "抖音", 80), data.get("point_budget", 0), now, now),
        )
        conn.commit()
        return _project_detail(conn, username, project_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
        for row in rows:
            row["revision"] = int(row["revision"])
            row["spent_points"] = _charged_planning_points(
                conn, row["username"], row["id"])
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
        spent_points = _charged_planning_points(conn, username, project_id)
        applied_ids = {
            int(row[0]) for row in conn.execute(
                "SELECT job_id FROM short_drama_applied_jobs WHERE project_id=?",
                (project_id,),
            ).fetchall()
        }
        outstanding = 0
        for row in conn.execute(
            "SELECT id, cost, payload FROM jobs WHERE username=? AND kind='copy' "
            "AND status IN ('pending','running','done')",
            (username,),
        ).fetchall():
            if int(row[0]) in applied_ids:
                continue
            try:
                payload = json.loads(row[2] or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("format") == "short_drama" and payload.get("project_id") == project_id:
                outstanding += max(0, int(row[1] or 0))
        if spent_points + outstanding + quoted_cost > point_budget:
            raise PointBudgetExceeded(
                "短剧点数预算不足：已扣 %d 点、本次 %d 点、预算 %d 点" %
                (spent_points, quoted_cost, point_budget)
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
        if len(content_keys) != 1 or len(original_patch) != 1:
            raise ValueError("每次只能更新一个短剧内容分区")
        key = next(iter(content_keys))
        if key == "characters":
            return update_characters(
                db_factory, username, project_id, revision, original_patch[key], avatar_lookup
            )
        if key == "script":
            return update_script(db_factory, username, project_id, revision, original_patch[key])
        return update_shots(db_factory, username, project_id, revision, original_patch[key])
    allowed = {"title", "synopsis", "ratio", "target_duration", "shot_count", "visual_style", "target_platform", "point_budget"}
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


def _normalize_characters(characters, *, require_complete=False):
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
        if require_complete and source_type == "cinematic_avatar" and not avatar_id:
            raise ValueError("电影化身角色必须提供 avatar_id")
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
                character, ("wardrobe_prompt",), 4000, required=require_complete
            ),
            "voice_key": _optional_key(character.get("voice_key"), "voice_key"),
            "voice_settings": voice_settings,
            "sort_order": index,
        })
    keys = [item["character_key"] for item in normalized]
    if len(set(keys)) != len(keys):
        raise ValueError("角色标识不能重复")
    return normalized


def _normalize_script(script, character_keys, *, default_title=None, require_complete=True):
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
        if character_key not in character_keys:
            raise ValueError("台词引用了不存在的角色")
        normalized_lines.append({
            "id": _strict_text(line, ("id",), 80, required=True),
            "character_key": character_key,
            "text": _strict_text(line, ("text",), 4000, required=True),
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
        if type(duration) is not int or duration not in {5, 10}:
            raise ValueError("分镜时长只能是 5 或 10 秒")
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
    if target_duration is not None and sum(shot["duration"] for shot in normalized) != target_duration:
        raise ValueError("分镜总时长必须等于短剧目标时长")
    return normalized


def _validate_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError("短剧规划无效")
    characters = _normalize_characters(plan.get("characters", []))
    character_keys = {character["character_key"] for character in characters}
    script = _normalize_script(
        plan.get("script", plan.get("script_version", {})), character_keys,
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
            "appearance_prompt, wardrobe_prompt, voice_key, voice_settings_json, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), project_id, character["character_key"], character["name"],
             character["identity_text"], character["personality"], character["source_type"],
             character["avatar_id"], character["appearance_prompt"], character["wardrobe_prompt"],
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
    current_scripts = project["script_versions"]
    if not current_scripts:
        raise ValueError("短剧项目缺少剧本")
    normalized_script = _normalize_script(
        current_scripts[-1] if script is _MISSING else script, character_keys,
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
            raise ValueError("电影化身不存在或不属于当前用户")
        try:
            avatar = avatar_lookup(username, character["avatar_id"])
        except Exception:
            raise ValueError("电影化身不存在或不属于当前用户")
        if (not isinstance(avatar, dict) or avatar.get("username") != username or
                avatar.get("status") == "deleted"):
            raise ValueError("电影化身不存在或不属于当前用户")


def update_characters(db_factory, username, project_id, revision, characters, avatar_lookup=None):
    required_stage = "characters_review"
    conn = _connection(db_factory)
    try:
        _begin_content_update(conn, username, project_id, revision, required_stage)
        project = _project_detail(conn, username, project_id)
        normalized_characters, _script, normalized_shots = _current_content_bundle(
            project, characters=characters, prune_character_refs=True
        )
        _validate_owned_avatars(username, normalized_characters, avatar_lookup)
        conn.execute("DELETE FROM short_drama_characters WHERE project_id=?", (project_id,))
        _insert_characters(conn, project_id, normalized_characters)
        for original, shot in zip(project["shots"], normalized_shots):
            conn.execute(
                "UPDATE short_drama_shots SET character_keys_json=? WHERE id=? AND project_id=?",
                (_json_text(shot["character_keys"], []), original["id"], project_id),
            )
        _cas_content_update(conn, username, project_id, revision, required_stage)
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
        _characters, normalized_script, normalized_shots = _current_content_bundle(
            project, script=script, prune_dialogue_refs=True
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
        conn.execute("DELETE FROM short_drama_shots WHERE project_id=?", (project_id,))
        _insert_shots(conn, project_id, script_version, normalized_shots)
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
        if sum(shot["duration"] for shot in shots) != project[3]:
            raise ValueError("分镜总时长必须等于短剧目标时长")
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


def confirm_stage(db_factory, username, project_id, revision, current_stage):
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
    if current_stage in short_drama_production.PRODUCTION_STAGES:
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
        "/api/gen/short-drama/video",
        "/api/gen/short-drama/video-cast/avatars",
        "/api/gen/short-drama/assembly",
        "/api/gen/short-drama/planning-job",
        "/api/gen/short-drama/planning-quote",
    },
    "POST": {
        "/api/gen/short-drama/projects",
        "/api/gen/short-drama/project/delete",
        "/api/gen/short-drama/apply-plan",
        "/api/gen/short-drama/confirm",
        "/api/gen/short-drama/asset-quote",
        "/api/gen/short-drama/voice-quote",
        "/api/gen/short-drama/generate-voice",
        "/api/gen/short-drama/generate-video",
        "/api/gen/short-drama/save-voice-timeline",
        "/api/gen/short-drama/set-voice-shot-lock",
        "/api/gen/short-drama/set-video-shot-lock",
        "/api/gen/short-drama/subtitle-alignment/jobs",
        "/api/gen/short-drama/subtitle-alignment/cancel",
        "/api/gen/short-drama/subtitle-alignment/timeline",
        "/api/gen/short-drama/subtitle-alignment/lock",
        "/api/gen/short-drama/select-asset",
        "/api/gen/short-drama/select-voice-version",
        "/api/gen/short-drama/video-quote",
        "/api/gen/short-drama/video-cast",
        "/api/gen/short-drama/select-asset",
        "/api/gen/short-drama/select-video",
        "/api/gen/short-drama/confirm-production-stage",
        "/api/gen/short-drama/use-native-audio",
        "/api/gen/short-drama/confirm-video-stage",
        "/api/gen/short-drama/render-final",
        "/api/gen/short-drama/confirm-assembly",
    },
    "PUT": {"/api/gen/short-drama/project"},
}


def _http_error(handler, error, *, operation_terminal=False):
    terminal = {"operation_terminal": True} if operation_terminal else {}
    if isinstance(error, ProjectLimitExceeded):
        handler._send(429, {
            "detail": str(error)[:220],
            "code": "short_drama_project_cap",
            "max_projects": error.max_projects,
            **terminal,
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
    elif isinstance(error, AppliedJobConflict):
        handler._send(409, {"detail": str(error)[:220], "code": "job_already_applied", **terminal})
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
    elif isinstance(error, PointBudgetExceeded):
        handler._send(400, {"detail": str(error)[:220], "code": "point_budget_exceeded", **terminal})
    elif isinstance(error, PermissionError):
        handler._send(403, {"detail": str(error)[:220], "code": "forbidden", **terminal})
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


def _handle_generate_voice(handler, db_factory, verify_token, canvas_access_resolver,
                           audio_domain, points_domain, core_dependencies):
    """Submit one paid voice job while keeping cross-domain plumbing out of core."""
    jdb = db_factory
    verify = verify_token
    feature_flags = core_dependencies["feature_flags"]
    miniprogram_security = core_dependencies["miniprogram_security"]
    _must_change_password = core_dependencies["_must_change_password"]
    _idempotency_key = core_dependencies["_idempotency_key"]
    _submission_lock = core_dependencies["_submission_lock"]
    _user_active_job_count = core_dependencies["_user_active_job_count"]
    MAX_USER_ACTIVE_JOBS = core_dependencies["MAX_USER_ACTIVE_JOBS"]
    is_shutting_down = core_dependencies["is_shutting_down"]
    jobs_store = core_dependencies["jobs_store"]
    SERVICE_OWNER = core_dependencies["SERVICE_OWNER"]
    enqueue_job = core_dependencies["enqueue_job"]
    _reject_pending_job = core_dependencies["_reject_pending_job"]
    user = verify(handler._token())
    if not user:
        return handler._send(401, {"detail": "未登录或登录已过期"})
    if _must_change_password(user):
        return handler._send(403, {"detail": "请先修改初始密码"})
    try:
        request_body = handler._json_body_strict()
        normalized = short_drama_voice.normalize_generate_request(
            request_body
        )
        idem_key = _idempotency_key(handler.headers.get("Idempotency-Key"))
        if not idem_key:
            raise ValueError("配音生成必须提供 Idempotency-Key")
        known_attempt = (
            short_drama_voice.recover_voice_submission(
                jdb, user["username"], request_body, idem_key
            )
        )
        owner = known_attempt.get("owner_username") if known_attempt else None
    except (short_drama_voice.VoiceQuoteConsumed,
            short_drama_voice.VoiceChargeInProgress,
            LookupError, PermissionError, ValueError,
            RevisionConflict) as error:
        _http_error(handler, error, operation_terminal=True)
        return
    if not known_attempt:
        try:
            feature_flags.require_enabled("audio")
            miniprogram_security.check_payload(request_body)
            access = canvas_access_resolver(handler)
            owner = _project_username_for_access(
                jdb, user["username"], normalized["project_id"], access, write=True
            )
            audio_domain.resolve_audio_provider_voice(
                user["username"], normalized["voice_key"]
            )
        except feature_flags.FeatureDisabled as error:
            return handler._send(503, {"detail": str(error)})
        except miniprogram_security.ContentRejected as error:
            return handler._send(400, {
                "detail": str(error), "code": "content_rejected",
                "operation_terminal": True,
            })
        except miniprogram_security.SecurityUnavailable as error:
            return handler._send(503, {
                "detail": str(error), "code": "content_security_unavailable",
                "retry_after_ms": 5000,
            })
        except (LookupError, PermissionError, ValueError,
                RevisionConflict) as error:
            _http_error(
                handler, error, operation_terminal=True
            )
            return
    with _submission_lock:
        try:
            attempt = (
                short_drama_voice
                .recover_voice_submission(
                    jdb, user["username"], request_body, idem_key
                )
            )
            replay = attempt is not None
            if replay:
                owner = attempt.get("owner_username")
            else:
                active_jobs = _user_active_job_count(user["username"])
                if active_jobs >= MAX_USER_ACTIVE_JOBS:
                    return handler._send(429, {
                        "detail": "您有 %d 个任务正在排队/生成，完成后再提交" %
                                  active_jobs,
                        "code": "active_job_cap",
                        "active_jobs": active_jobs,
                        "max_active_jobs": MAX_USER_ACTIVE_JOBS,
                        "retry_after_ms": 4000,
                    })
                attempt, replay = (
                    short_drama_voice
                    .prepare_voice_submission(
                        jdb, user["username"], owner, request_body, idem_key
                    )
                )
        except (short_drama_voice.VoiceQuoteConsumed,
                short_drama_voice.VoiceChargeInProgress,
                LookupError, PermissionError, ValueError,
                RevisionConflict) as error:
            _http_error(
                handler, error, operation_terminal=True
            )
            return
        if replay and attempt.get("job_id"):
            try:
                short_drama_voice.get_voice_workspace(
                    jdb, owner, attempt["project_id"]
                )
                attempt = (
                    short_drama_voice.get_voice_attempt(
                        jdb, user["username"], idem_key
                    )
                )
            except Exception:
                pass
        if replay and attempt["state"] in {"linked", "done"}:
            return handler._send(200, {
                "project_id": attempt["project_id"],
                "line_id": attempt["voice_line_id"],
                "job_id": int(attempt["job_id"]),
                "cost": int(attempt["cost"]),
                "points_left": attempt["points_left"],
                "replayed": True,
            })
        if replay and attempt["state"] in {"refund_pending", "refunded", "failed"}:
            terminal = dict(attempt.get("terminal_response") or {
                "detail": "本次配音生成未受理，请重新询价",
            })
            status = 503 if attempt["state"] == "refund_pending" else 409
            terminal.setdefault("code", "voice_refund_pending"
                                if status == 503 else "voice_operation_terminal")
            terminal["operation_terminal"] = True
            return handler._send(status, terminal)
        if is_shutting_down():
            return handler._send(503, {
                "detail": "服务正在更新，请稍等几秒后重试（未重复扣点）",
                "code": "shutting_down", "retry_after_ms": 5000,
            })
        try:
            if attempt["state"] == "accepted":
                points_left = points_domain.deduct_points(
                    user["username"], int(attempt["cost"]),
                    "short-drama voice",
                    transaction_key=attempt["charge_key"],
                )
                attempt = (
                    short_drama_voice
                    .mark_voice_attempt_charged(
                        jdb, user["username"], idem_key, points_left
                    )
                )
            else:
                points_left = int(attempt["points_left"])
            jid = jobs_store.create_job_after_charge(
                jdb, "audio", user["username"], int(attempt["cost"]),
                attempt["audio_payload"], SERVICE_OWNER,
                before_commit=lambda connection, job_id:
                    short_drama_voice.bind_voice_job(
                        jdb, user["username"], idem_key, connection, job_id
                    ),
            )
            attempt = short_drama_voice.get_voice_attempt(
                jdb, user["username"], idem_key
            )
        except points_domain.AuthPointsError as error:
            if error.status == 402:
                short_drama_voice.mark_voice_attempt_failed(
                    jdb, user["username"], idem_key,
                    {"detail": error.detail, "code": "charge_rejected"},
                )
            return handler._send(
                402 if error.status == 402 else 502,
                {"detail": error.detail, "need": int(attempt["cost"])},
            )
        except Exception:
            terminal = {
                "detail": "配音任务创建失败，退款正在自动处理",
                "code": "voice_job_create_failed",
                "operation_terminal": True,
            }
            attempt = (
                short_drama_voice
                .mark_voice_attempt_refund_pending(
                    jdb, user["username"], idem_key, terminal
                )
            )
            try:
                points_domain.refund_points(
                    user["username"], int(attempt["cost"]),
                    "short-drama voice:create-failed",
                    transaction_key=attempt["refund_key"],
                )
                short_drama_voice.mark_voice_attempt_refunded(
                    jdb, user["username"], idem_key
                )
                return handler._send(500, terminal)
            except Exception:
                return handler._send(503, {
                    "detail": "配音任务创建失败，退款正在自动重试",
                    "code": "voice_refund_pending",
                    "retry_after_ms": 5000,
                })
        if not enqueue_job(jid, "audio"):
            _reject_pending_job(
                jid, user["username"], int(attempt["cost"]),
                "任务队列已满，请稍后再试",
            )
            return handler._send(429, {
                "detail": "任务队列已满，请重新询价后重试",
                "code": "queue_full", "operation_terminal": True,
                "retry_after_ms": 4000,
            })
        return handler._send(200, {
            "project_id": attempt["project_id"],
            "line_id": attempt["voice_line_id"],
            "job_id": jid,
            "cost": int(attempt["cost"]),
            "points_left": points_left,
            "replayed": False,
        })


def dispatch_http(handler, method, db_factory, verify_token, cost_of=None, avatar_lookup=None,
                  mutation_lock=None, canvas_access_resolver=None, voice_validator=None,
                  points_getter=None, generation_dependencies=None,
                  avatar_list=None):
    """Handle the domain's synchronous routes inside core.H; return whether matched."""
    path = handler.path.split("?", 1)[0]
    if path not in _HTTP_ROUTES.get(method, ()):
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
        if method == "GET" and path.endswith("/planning-quote"):
            if not callable(cost_of):
                raise ValueError("短剧策划报价暂不可用")
            cost = int(cost_of("copy", {"format": "short_drama"}))
            if cost < 0:
                raise ValueError("短剧策划报价无效")
            handler._send(200, {"cost": cost})
        elif method == "POST" and path.endswith("/asset-quote"):
            if not callable(cost_of):
                raise ValueError("关键帧报价暂不可用")
            handler._send(200, short_drama_production.prepare_still_quote(
                db_factory, username, _request_object(handler), cost_of, access
            ))
        elif method == "POST" and path.endswith("/generate-voice"):
            if not generation_dependencies:
                raise ValueError("鐭墽閰嶉煶鐢熸垚鏆備笉鍙敤")
            _handle_generate_voice(
                handler, db_factory, verify_token, canvas_access_resolver,
                *generation_dependencies,
            )
        elif method == "POST" and path.endswith("/generate-video"):
            if not generation_dependencies:
                raise ValueError("短剧视频生成暂不可用")
            short_drama_video.handle_generate(
                handler, db_factory, username, access,
                generation_dependencies[1], generation_dependencies[2],
            )
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
        elif method == "POST" and path.endswith("/video-quote"):
            if not callable(cost_of):
                raise ValueError("短剧视频报价暂不可用")
            handler._send(200, short_drama_video.prepare_quote(
                db_factory, username, _request_object(handler), cost_of, access
            ))
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
        elif method == "POST" and path.endswith("/select-asset"):
            body = _request_object(handler)
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
        elif method == "POST" and path.endswith("/select-video"):
            body = _request_object(handler)
            if mutation_lock is not None:
                with mutation_lock:
                    selected = short_drama_video.select_version(
                        db_factory, username, body, access
                    )
            else:
                selected = short_drama_video.select_version(
                    db_factory, username, body, access
                )
            handler._send(200, selected)
        elif method == "POST" and path.endswith("/confirm-production-stage"):
            body = _request_object(handler)
            if mutation_lock is not None:
                with mutation_lock:
                    confirmed = short_drama_production.confirm_stage(
                        db_factory, username, body, access
                    )
            else:
                confirmed = short_drama_production.confirm_stage(db_factory, username, body, access)
            handler._send(200, confirmed)
        elif method == "POST" and path.endswith("/use-native-audio"):
            body = _request_object(handler)
            _validate_project_request(body, {"project_id", "revision"})
            owner = _project_username_for_access(
                db_factory, username, body["project_id"], access, write=True
            )
            if mutation_lock is not None:
                with mutation_lock:
                    confirmed = short_drama_voice.confirm_native_audio(
                        db_factory, owner, body["project_id"], body["revision"]
                    )
            else:
                confirmed = short_drama_voice.confirm_native_audio(
                    db_factory, owner, body["project_id"], body["revision"]
                )
            handler._send(200, confirmed)
        elif method == "POST" and path.endswith("/confirm-video-stage"):
            body = _request_object(handler)
            if mutation_lock is not None:
                with mutation_lock:
                    confirmed = short_drama_video.confirm_stage(
                        db_factory, username, body, access
                    )
            else:
                confirmed = short_drama_video.confirm_stage(
                    db_factory, username, body, access
                )
            handler._send(200, confirmed)
        elif method == "POST" and path.endswith("/render-final"):
            body = _request_object(handler)
            _validate_project_request(
                body, {"project_id", "revision", "idempotency_key"}
            )
            owner = _project_username_for_access(
                db_factory, username, body["project_id"], access, write=True
            )
            rendered = short_drama_assembly.start_final_render(
                db_factory, owner, body["project_id"], body["revision"],
                body["idempotency_key"],
            )
            handler._send(200, rendered)
        elif method == "POST" and path.endswith("/confirm-assembly"):
            body = _request_object(handler)
            _validate_project_request(body, {"project_id", "revision"})
            owner = _project_username_for_access(
                db_factory, username, body["project_id"], access, write=True
            )
            confirmed = short_drama_assembly.confirm_completed(
                db_factory, owner, body["project_id"], body["revision"]
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
        elif method == "GET" and path.endswith("/video-cast/avatars"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=True
            )
            items = []
            for avatar in avatar_list(owner, 120):
                if (not isinstance(avatar, dict)
                        or str(avatar.get("status") or "") != "ready"
                        or not str(avatar.get("provider_avatar_id") or "").strip()):
                    continue
                items.append({
                    key: avatar.get(key)
                    for key in (
                        "id", "name", "image_url", "status",
                        "created_at", "updated_at",
                    )
                })
            handler._send(200, {
                "items": items,
                "can_create_avatar": owner == username,
            })
        elif method == "GET" and path.endswith("/video"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(
                200,
                short_drama_video.get_workspace(
                    db_factory, owner, project_id
                ),
            )
        elif method == "GET" and path.endswith("/assembly"):
            project_id = _planning_project_id_from_query(handler)
            owner = _project_username_for_access(
                db_factory, username, project_id, access, write=False
            )
            handler._send(
                200,
                short_drama_assembly.get_assembly_workspace(
                    db_factory, owner, project_id
                ),
            )
        elif method == "GET":
            project_id = _project_id_from_query(handler)
            handler._send(200, get_project(db_factory, username, project_id, access))
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
        elif path.endswith("/project/delete"):
            body = _request_object(handler)
            _validate_project_request(body, {"project_id", "revision"})
            if mutation_lock is not None:
                with mutation_lock:
                    deleted = delete_project(
                        db_factory, username, body["project_id"], body["revision"]
                    )
            else:
                deleted = delete_project(
                    db_factory, username, body["project_id"], body["revision"]
                )
            handler._send(200, deleted)
        elif path.endswith("/projects"):
            handler._send(200, create_project(db_factory, username, _request_object(handler), access))
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
                        db_factory, owner, body["project_id"], body["revision"], body["stage"]
                    )
            else:
                confirmed = confirm_stage(
                    db_factory, owner, body["project_id"], body["revision"], body["stage"]
                )
            handler._send(200, confirmed)
    except (LookupError, RevisionConflict, AppliedJobConflict, ProjectHasUnappliedJobs,
            PermissionError, ValueError, short_drama_voice.VoiceQuoteConsumed,
            short_drama_voice.VoiceChargeInProgress,
            short_drama_alignment.AlignmentError,
            short_drama_video.VideoQuoteConsumed,
            short_drama_video.VideoChargeInProgress,
            short_drama_video.VideoBlocked,
            short_drama_assembly.PreviewIdempotencyConflict,
            short_drama_assembly.ActiveCompositionJob,
            short_drama_assembly.PreviewBlocked) as error:
        _http_error(handler, error)
    return True
