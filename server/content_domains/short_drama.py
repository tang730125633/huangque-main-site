"""Persistence and optimistic-concurrency helpers for short-drama projects."""

import json
import time
import uuid


STAGES = ("draft", "characters_review", "script_review", "storyboard_review", "stills_review")
NEXT_STAGE = {
    "characters_review": "script_review",
    "script_review": "storyboard_review",
    "storyboard_review": "stills_review",
}
RATIOS = {"9:16", "16:9"}
DURATIONS = {30, 45, 60}
SHOT_COUNTS = set(range(6, 11))


class RevisionConflict(RuntimeError):
    pass


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
    if "ratio" in cleaned and cleaned["ratio"] not in RATIOS:
        raise ValueError("短剧比例仅支持 9:16、16:9")
    if "target_duration" in cleaned:
        cleaned["target_duration"] = int(cleaned["target_duration"])
        if cleaned["target_duration"] not in DURATIONS:
            raise ValueError("短剧时长仅支持 30、45、60 秒")
    if "shot_count" in cleaned:
        cleaned["shot_count"] = int(cleaned["shot_count"])
        if cleaned["shot_count"] not in SHOT_COUNTS:
            raise ValueError("分镜数量必须为 6–10 个")
    cleaned["visual_style"] = str(cleaned.get("visual_style") or "电影写实").strip()[:80]
    if "point_budget" in cleaned:
        cleaned["point_budget"] = max(0, int(cleaned["point_budget"] or 0))
    return cleaned


_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_projects (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
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


def _dict_rows(conn, query, params):
    cursor = conn.execute(query, params)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _project_detail(conn, username, project_id):
    projects = _dict_rows(conn,
        "SELECT * FROM short_drama_projects WHERE id=? AND username=? AND deleted=0",
        (project_id, username),
    )
    if not projects:
        raise LookupError("短剧项目不存在")
    detail = projects[0]
    detail["revision"] = int(detail["revision"])
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
        conn.commit()
    finally:
        conn.close()


def create_project(db_factory, username, payload):
    data = validate_project_payload(payload)
    now = int(time.time())
    project_id = str(uuid.uuid4())
    conn = _connection(db_factory)
    try:
        conn.execute(
            "INSERT INTO short_drama_projects "
            "(id, username, title, synopsis, ratio, target_duration, shot_count, visual_style, "
            "target_platform, point_budget, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, username, data["title"], data["synopsis"], data["ratio"],
             data["target_duration"], data["shot_count"], data["visual_style"],
             _text(data.get("target_platform") or "抖音", 80), data.get("point_budget", 0), now, now),
        )
        conn.commit()
        return _project_detail(conn, username, project_id)
    finally:
        conn.close()


def list_projects(db_factory, username):
    conn = _connection(db_factory)
    try:
        ids = conn.execute(
            "SELECT id FROM short_drama_projects WHERE username=? AND deleted=0 ORDER BY updated_at DESC, id DESC",
            (username,),
        ).fetchall()
        return [_project_detail(conn, username, project_id) for (project_id,) in ids]
    finally:
        conn.close()


def get_project(db_factory, username, project_id):
    conn = _connection(db_factory)
    try:
        return _project_detail(conn, username, project_id)
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


def update_project(db_factory, username, project_id, revision, patch):
    original_patch = dict(patch or {})
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
            "SELECT title FROM short_drama_projects WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not current:
            raise LookupError("短剧项目不存在")
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


def _validate_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError("短剧规划无效")
    characters = plan.get("characters", [])
    shots = plan.get("shots", [])
    script = plan.get("script", plan.get("script_version", {}))
    if not isinstance(characters, list) or not isinstance(shots, list) or not isinstance(script, dict):
        raise ValueError("短剧规划无效")
    normalized_characters = []
    for index, character in enumerate(characters):
        if not isinstance(character, dict):
            raise ValueError("角色数据无效")
        character_key = _text(character.get("character_key") or character.get("key"), 80)
        name = _text(character.get("name"), 80)
        source_type = character.get("source_type", "ai_character")
        if not character_key or not name or source_type not in {"cinematic_avatar", "ai_character"}:
            raise ValueError("角色数据无效")
        normalized_characters.append({
            "character_key": character_key, "name": name,
            "identity_text": _text(character.get("identity_text"), 2000),
            "personality": _text(character.get("personality"), 2000),
            "source_type": source_type, "avatar_id": character.get("avatar_id") or None,
            "appearance_prompt": _text(character.get("appearance_prompt"), 4000),
            "wardrobe_prompt": _text(character.get("wardrobe_prompt"), 4000),
            "voice_key": character.get("voice_key") or None,
            "voice_settings": character.get("voice_settings", {}), "sort_order": index,
        })
    if len({item["character_key"] for item in normalized_characters}) != len(normalized_characters):
        raise ValueError("角色标识不能重复")
    normalized_shots = []
    for index, shot in enumerate(shots):
        if not isinstance(shot, dict):
            raise ValueError("分镜数据无效")
        shot_key = _text(shot.get("shot_key") or shot.get("key"), 80)
        try:
            duration = int(shot.get("duration"))
        except (TypeError, ValueError):
            duration = 0
        if not shot_key or duration not in {5, 10}:
            raise ValueError("分镜数据无效")
        normalized_shots.append({
            "shot_key": shot_key, "sort_order": index, "duration": duration,
            "scene_description": _text(shot.get("scene_description"), 4000),
            "camera_description": _text(shot.get("camera_description"), 4000),
            "character_keys": shot.get("character_keys", []),
            "dialogue_line_ids": shot.get("dialogue_line_ids", []),
            "image_prompt": _text(shot.get("image_prompt"), 8000),
            "video_prompt": _text(shot.get("video_prompt"), 8000),
        })
    if len({item["shot_key"] for item in normalized_shots}) != len(normalized_shots):
        raise ValueError("分镜标识不能重复")
    if len(normalized_shots) not in SHOT_COUNTS:
        raise ValueError("分镜数量必须为 6–10 个")
    if not all(isinstance(item["character_keys"], list) and isinstance(item["dialogue_line_ids"], list)
               for item in normalized_shots):
        raise ValueError("分镜关联数据无效")
    return normalized_characters, {
        "title": _text(script.get("title") or plan.get("title") or "未命名剧本", 80),
        "logline": _text(script.get("logline"), 4000), "hook": _text(script.get("hook"), 4000),
        "conflict_text": _text(script.get("conflict_text"), 4000),
        "turn_text": _text(script.get("turn_text"), 4000), "ending": _text(script.get("ending"), 4000),
        "dialogue_lines": script.get("dialogue_lines", []),
    }, normalized_shots


def apply_plan(db_factory, username, project_id, revision, plan, planning_cost, planning_job_id):
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
        conn.execute("BEGIN")
        applied = conn.execute(
            "SELECT project_id, username FROM short_drama_applied_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if applied:
            if applied == (project_id, username):
                conn.rollback()
                return _project_detail(conn, username, project_id)
            raise ValueError("规划任务已属于其他项目")
        project = conn.execute(
            "SELECT title, target_duration FROM short_drama_projects "
            "WHERE id=? AND username=? AND revision=? AND deleted=0",
            (project_id, username, revision),
        ).fetchone()
        if not project:
            _raise_cas_error(conn, username, project_id)
        if sum(shot["duration"] for shot in shots) != project[1]:
            raise ValueError("分镜总时长必须等于短剧目标时长")
        conn.execute(
            "INSERT INTO short_drama_applied_jobs (job_id, project_id, username, cost, applied_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, project_id, username, cost, now),
        )
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
            "UPDATE short_drama_projects SET stage='characters_review', spent_points=spent_points+?, "
            "revision=revision+1, updated_at=? WHERE id=? AND username=? AND revision=? AND deleted=0",
            (cost, now, project_id, username, revision),
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
    if current_stage not in NEXT_STAGE:
        raise ValueError("当前阶段不可确认")
    now = int(time.time())
    conn = _connection(db_factory)
    try:
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
