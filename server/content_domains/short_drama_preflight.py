"""Production-readiness planning for the standalone short-drama studio.

PR-3 turns the immutable PR-2 script into an immutable production plan.  It
does not charge points, enqueue media jobs, or mutate the legacy nine-stage
project.  A confirmed plan is the hand-off contract for automatic draft
production in the next PR.
"""

import hashlib
import json
import math
import sqlite3
import time
import uuid


QUALITY_ROUTES = {"quick_draft", "formal"}
PLAN_STATUSES = {"draft", "confirmed", "superseded"}
MAX_PLAN_VERSIONS = 20


class PreflightError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_production_plans (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  source_script_version_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','confirmed','superseded')),
  quality_route TEXT NOT NULL CHECK(quality_route IN ('quick_draft','formal')),
  plan_json TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  confirmed_by TEXT,
  confirmed_at INTEGER,
  UNIQUE(project_id, version),
  UNIQUE(project_id, input_hash)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_production_plans_project
  ON short_drama_production_plans(project_id, version DESC);

CREATE TABLE IF NOT EXISTS short_drama_preflight_requests (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  operation TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(actor_username, project_id, operation, idempotency_key)
);
"""


def _connection(db_factory):
    conn = db_factory()
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json(value, fallback):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed


def _hash(value):
    return hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()


def _idempotency_key(value):
    value = str(value or "").strip()
    if not value or len(value) > 160:
        raise PreflightError("idempotency_key_required", "缺少有效的幂等键")
    return value


def init_db(db_factory):
    conn = _connection(db_factory)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _project(conn, owner_username, project_id):
    cursor = conn.execute(
        "SELECT id,title,synopsis,ratio,target_duration,shot_count,visual_style,"
        "target_platform,point_budget,spent_points,stage,revision "
        "FROM short_drama_projects WHERE id=? AND username=? AND deleted=0",
        (project_id, owner_username),
    )
    values = cursor.fetchone()
    if not values:
        raise LookupError("short drama project does not exist")
    project = dict(zip((column[0] for column in cursor.description), values))
    character_cursor = conn.execute(
        "SELECT character_key,name,identity_text,personality,source_type,"
        "avatar_id,appearance_prompt,wardrobe_prompt,reference_version,"
        "reference_locked FROM short_drama_characters WHERE project_id=? "
        "ORDER BY sort_order,id",
        (project_id,),
    )
    character_columns = [column[0] for column in character_cursor.description]
    character_rows = [
        dict(zip(character_columns, row))
        for row in character_cursor.fetchall()
    ]
    project["character_snapshot_hash"] = _hash(character_rows)
    return project


def _locked_script(conn, project_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT snapshot.* FROM short_drama_conversations conversation "
        "JOIN short_drama_script_snapshots snapshot "
        "ON snapshot.id=conversation.locked_version_id "
        "WHERE conversation.project_id=? AND conversation.state='script_locked' "
        "AND snapshot.project_id=? AND snapshot.status='locked'",
        (project_id, project_id),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["script"] = _json(item.pop("script_json"), {})
    item["version"] = int(item["version"])
    return item


def _conversation_revision(conn, project_id):
    row = conn.execute(
        "SELECT revision FROM short_drama_conversations WHERE project_id=?",
        (project_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def _plan(row):
    if not row:
        return None
    item = dict(row)
    item["plan"] = _json(item.pop("plan_json"), {})
    item["version"] = int(item["version"])
    return item


def _plans(conn, project_id):
    conn.row_factory = sqlite3.Row
    return [
        _plan(row)
        for row in conn.execute(
            "SELECT * FROM short_drama_production_plans WHERE project_id=? "
            "ORDER BY version DESC",
            (project_id,),
        ).fetchall()
    ]


def _current_inputs(project, script, quality_route):
    return {
        "project": {
            "ratio": project["ratio"],
            "target_duration": int(project["target_duration"]),
            "shot_count": int(project["shot_count"]),
            "visual_style": project["visual_style"],
            "target_platform": project["target_platform"],
            "point_budget": int(project["point_budget"] or 0),
            "spent_points": int(project["spent_points"] or 0),
        },
        "source_script_version_id": script["id"],
        "source_script_hash": script["input_hash"],
        "character_snapshot_hash": project.get("character_snapshot_hash") or "",
        "quality_route": quality_route,
        "contract_version": "standalone-preflight-v3",
    }


def _dialogue_by_id(script):
    return {
        str(item.get("id") or ""): item
        for item in script.get("dialogue_lines", [])
        if isinstance(item, dict)
    }


def _duration_plan(script, target_seconds):
    shots = [item for item in script.get("shots", []) if isinstance(item, dict)]
    dialogue = _dialogue_by_id(script)
    if not shots:
        raise PreflightError("script_shots_missing", "锁定剧本没有可制作镜头", 422)
    weights = []
    speech_total_ms = 0
    for shot in shots:
        texts = [
            str(dialogue.get(str(line_id), {}).get("text") or "").strip()
            for line_id in shot.get("dialogue_line_ids", [])
        ]
        characters = sum(len(value) for value in texts)
        speech_ms = int(math.ceil(characters / 5.2 * 1000)) if characters else 0
        speech_total_ms += speech_ms
        visual_ms = max(1800, int(float(shot.get("duration_seconds") or 3) * 1000))
        weights.append(max(visual_ms, speech_ms + (500 if speech_ms else 0)))
    target_ms = int(target_seconds) * 1000
    total_weight = max(1, sum(weights))
    allocations = [max(1000, int(target_ms * weight / total_weight)) for weight in weights]
    difference = target_ms - sum(allocations)
    allocations[-1] += difference
    if allocations[-1] < 1000:
        shortage = 1000 - allocations[-1]
        allocations[-1] = 1000
        for index in range(len(allocations) - 2, -1, -1):
            available = max(0, allocations[index] - 1000)
            moved = min(available, shortage)
            allocations[index] -= moved
            shortage -= moved
            if not shortage:
                break
        if shortage:
            raise PreflightError("target_duration_too_short", "目标总时长不足以容纳全部镜头", 422)
    cursor = 0
    result = []
    for index, shot in enumerate(shots):
        duration_ms = allocations[index]
        result.append({
            "shot_key": str(shot.get("shot_key") or "shot_%02d" % (index + 1)),
            "sort_order": int(shot.get("sort_order") or index + 1),
            "start_ms": cursor,
            "end_ms": cursor + duration_ms,
            "duration_ms": duration_ms,
            "source_duration_ms": int(float(shot.get("duration_seconds") or 0) * 1000),
            "reason": (
                "对白驱动" if any(shot.get("dialogue_line_ids") or []) else "动作与节奏驱动"
            ),
        })
        cursor += duration_ms
    return result, speech_total_ms


def _asset_plan(script):
    characters = []
    for item in script.get("characters", []):
        if not isinstance(item, dict):
            continue
        characters.append({
            "scope": "character",
            "key": str(item.get("character_key") or ""),
            "label": str(item.get("name") or "未命名角色"),
            "status": "recommended",
            "source": "system_recommendation",
            "required": True,
            "note": "尚未绑定人物参考，将采用系统推荐形象进入快速草稿。",
        })
    scenes = []
    for index, item in enumerate(script.get("scenes", [])):
        if not isinstance(item, dict):
            continue
        scenes.append({
            "scope": "scene",
            "key": "scene_%02d" % (index + 1),
            "label": str(item.get("location") or "未命名场景"),
            "status": "recommended",
            "source": "system_recommendation",
            "required": True,
            "note": "尚未绑定场景参考，将根据锁定剧本自动生成。",
        })
    return characters + scenes


def _material_plan(script, duration):
    """Preserve the locked script as executable, shot-level production inputs."""
    dialogue = _dialogue_by_id(script)
    characters = {
        str(item.get("character_key") or ""): str(item.get("name") or "")
        for item in script.get("characters", [])
        if isinstance(item, dict)
    }
    timing = {
        str(item.get("shot_key") or ""): item
        for item in duration
        if isinstance(item, dict)
    }
    result = []
    for index, shot in enumerate(
        item for item in script.get("shots", []) if isinstance(item, dict)
    ):
        shot_key = str(shot.get("shot_key") or "shot_%02d" % (index + 1))
        slot = timing.get(shot_key) or {}
        dialogue_items = []
        for line_id in shot.get("dialogue_line_ids", []):
            line = dialogue.get(str(line_id))
            if not line:
                continue
            character_key = str(line.get("character_key") or "")
            dialogue_items.append({
                "line_id": str(line.get("id") or line_id),
                "character_key": character_key,
                "speaker": str(
                    line.get("speaker")
                    or characters.get(character_key)
                    or character_key
                ),
                "text": str(line.get("text") or "").strip(),
            })
        character_keys = [
            str(value) for value in shot.get("character_keys", []) if str(value)
        ]
        item = {
            "shot_key": shot_key,
            "sort_order": int(shot.get("sort_order") or index + 1),
            "start_ms": int(slot.get("start_ms") or 0),
            "end_ms": int(slot.get("end_ms") or 0),
            "duration_ms": int(slot.get("duration_ms") or 0),
            "scene": str(shot.get("scene") or "").strip(),
            "beat": str(shot.get("beat") or "").strip(),
            "visual_prompt": str(shot.get("visual") or "").strip(),
            "provider_prompt": str(
                shot.get("provider_prompt") or shot.get("visual") or ""
            ).strip(),
            "negative_prompt": str(shot.get("negative_prompt") or "").strip(),
            "camera": str(shot.get("camera") or "").strip(),
            "character_keys": character_keys,
            "character_names": [
                characters.get(key) or key for key in character_keys
            ],
            "dialogue": dialogue_items,
            "dialogue_text": "\n".join(
                ("%s：%s" % (line["speaker"], line["text"])).strip("：")
                for line in dialogue_items
                if line["text"]
            ),
        }
        item["input_hash"] = _hash(item)
        result.append(item)
    return result


def _route_options(shot_count):
    shot_count = int(shot_count)
    return [
        {
            "key": "quick_draft",
            "name": "快速草稿",
            "resolution": "720p",
            "estimated_points": shot_count * 8 + 7,
            "estimated_minutes": max(3, shot_count * 2),
            "description": "优先尽快得到完整可播放草稿，允许系统推荐素材和安全降级。",
        },
        {
            "key": "formal",
            "name": "正式制作",
            "resolution": "1080p",
            "estimated_points": shot_count * 24 + 18,
            "estimated_minutes": max(12, shot_count * 6),
            "description": "优先一致性和交付质量，后续执行完整素材与质量门禁。",
        },
    ]


def _build_plan(project, script_snapshot, quality_route):
    script = script_snapshot["script"]
    duration, speech_total_ms = _duration_plan(
        script, int(project["target_duration"])
    )
    assets = _asset_plan(script)
    material_plan = _material_plan(script, duration)
    routes = _route_options(len(duration))
    selected = next(item for item in routes if item["key"] == quality_route)
    required_acceptance = []
    checks = []
    target_ms = int(project["target_duration"]) * 1000
    if speech_total_ms > target_ms:
        required_acceptance.append("duration_compression")
        checks.append({
            "key": "duration",
            "label": "时长",
            "status": "warning",
            "summary": "对白自然语速预计超过目标时长，需要在配音阶段压缩停顿或精简个别台词。",
            "suggestion": "接受系统的对白节奏调整；若试听仍拥挤，再返回剧本修改台词。",
        })
    else:
        checks.append({
            "key": "duration",
            "label": "时长",
            "status": "pass",
            "summary": "对白与镜头节奏可放入目标总时长。",
            "suggestion": "",
        })
    if assets:
        required_acceptance.append("recommended_assets")
        checks.append({
            "key": "assets",
            "label": "参考素材",
            "status": "warning",
            "summary": "有 %d 项人物或场景素材将采用系统推荐方案。" % len(assets),
            "suggestion": "可以先生成快速草稿，之后只替换真正影响效果的参考素材。",
        })
    characters = [item for item in script.get("characters", []) if isinstance(item, dict)]
    scenes = [item for item in script.get("scenes", []) if isinstance(item, dict)]
    complexity_status = "warning" if len(characters) > 4 or len(scenes) > 5 else "pass"
    checks.append({
        "key": "complexity",
        "label": "制作复杂度",
        "status": complexity_status,
        "summary": "%d 个角色、%d 个场景、%d 个镜头。" % (
            len(characters), len(scenes), len(duration)
        ),
        "suggestion": "角色或场景过多时建议优先走快速草稿。" if complexity_status == "warning" else "",
    })
    known_characters = {
        str(item.get("character_key") or "") for item in characters
    }
    unknown = sorted({
        str(key)
        for shot in script.get("shots", [])
        if isinstance(shot, dict)
        for key in shot.get("character_keys", [])
        if str(key) not in known_characters
    })
    if unknown:
        checks.append({
            "key": "consistency",
            "label": "一致性",
            "status": "blocker",
            "summary": "镜头引用了不存在的角色：" + "、".join(unknown[:5]),
            "suggestion": "返回剧本阶段修正角色引用后重新锁定。",
        })
    else:
        checks.append({
            "key": "consistency",
            "label": "一致性",
            "status": "pass",
            "summary": "人物引用、场景顺序和镜头编号一致。",
            "suggestion": "",
        })
    budget = int(project["point_budget"] or 0)
    spent = int(project["spent_points"] or 0)
    available = None if budget == 0 else max(0, budget - spent)
    over_budget = available is not None and selected["estimated_points"] > available
    checks.append({
        "key": "budget",
        "label": "预算",
        "status": "blocker" if over_budget else "pass",
        "summary": (
            "预计 %d 点，超过项目剩余预算 %d 点。"
            % (selected["estimated_points"], available)
            if over_budget
            else "预计 %d 点；当前仅为计划估算，本阶段不扣点。"
            % selected["estimated_points"]
        ),
        "suggestion": "提高项目预算或切换快速草稿路线。" if over_budget else "",
    })
    blockers = [item["key"] for item in checks if item["status"] == "blocker"]
    return {
        "contract_version": "standalone-preflight-v2",
        "source_script": {
            "id": script_snapshot["id"],
            "version": script_snapshot["version"],
            "input_hash": script_snapshot["input_hash"],
        },
        "quality_route": quality_route,
        "route_options": routes,
        "estimate": {
            "points": selected["estimated_points"],
            "minutes": selected["estimated_minutes"],
            "resolution": selected["resolution"],
            "billing": "estimate_only",
        },
        "duration": {
            "target_ms": target_ms,
            "speech_estimate_ms": speech_total_ms,
            "shots": duration,
        },
        "material_plan": material_plan,
        "assets": assets,
        "checks": checks,
        "blockers": blockers,
        "required_acceptance": sorted(set(required_acceptance)),
        "ready": not blockers,
        "next_phase": "automatic_draft",
    }


def _existing_request(conn, actor, project_id, operation, key, request_hash):
    row = conn.execute(
        "SELECT request_hash,response_json FROM short_drama_preflight_requests "
        "WHERE actor_username=? AND project_id=? AND operation=? AND idempotency_key=?",
        (actor, project_id, operation, key),
    ).fetchone()
    if not row:
        return None
    if row[0] != request_hash:
        raise PreflightError("idempotency_conflict", "该幂等键已用于不同请求", 409)
    return _json(row[1], {})


def _store_request(conn, actor, project_id, operation, key, request_hash, response):
    conn.execute(
        "INSERT INTO short_drama_preflight_requests "
        "(id,actor_username,project_id,operation,idempotency_key,request_hash,"
        "response_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()),
            actor,
            project_id,
            operation,
            key,
            request_hash,
            _json_text(response),
            int(time.time()),
        ),
    )


def _workspace(conn, project, actor_username, can_edit):
    script = _locked_script(conn, project["id"])
    plans = _plans(conn, project["id"])
    current = next(
        (plan for plan in plans if plan["status"] in {"draft", "confirmed"}),
        None,
    )
    stale = False
    if current and script:
        expected = _hash(_current_inputs(project, script, current["quality_route"]))
        stale = current["input_hash"] != expected
    if not script:
        state = "script_required"
    elif current and current["status"] == "confirmed" and not stale:
        state = "confirmed"
    elif current and not stale:
        state = "ready_for_confirmation"
    else:
        state = "awaiting_preflight"
    return {
        "project": {
            "id": project["id"],
            "title": project["title"],
            "point_budget": int(project["point_budget"] or 0),
            "spent_points": int(project["spent_points"] or 0),
        },
        "state": state,
        "conversation_revision": _conversation_revision(conn, project["id"]),
        "source_script": (
            {
                "id": script["id"],
                "version": script["version"],
                "input_hash": script["input_hash"],
            }
            if script else None
        ),
        "current_plan": current,
        "versions": plans,
        "stale": stale,
        "permissions": {"can_edit": bool(can_edit), "actor": actor_username},
        "billing": {"charged": False, "cost": 0, "mode": "estimate_only"},
    }


def workspace(db_factory, owner_username, actor_username, project_id, can_edit=True):
    conn = _connection(db_factory)
    try:
        project = _project(conn, owner_username, project_id)
        return _workspace(conn, project, actor_username, can_edit)
    finally:
        conn.close()


def generate_plan(db_factory, owner_username, actor_username, body, idempotency_key):
    project_id = str(body.get("project_id") or "").strip()
    revision = body.get("conversation_revision")
    if type(revision) is not int or revision < 1:
        raise PreflightError("conversation_revision_invalid", "对话版本无效")
    quality_route = str(body.get("quality_route") or "quick_draft").strip()
    if quality_route not in QUALITY_ROUTES:
        raise PreflightError("quality_route_invalid", "制作路线无效")
    key = _idempotency_key(idempotency_key)
    request_hash = _hash({"revision": revision, "quality_route": quality_route})
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, project_id)
        replay = _existing_request(
            conn, actor_username, project_id, "generate", key, request_hash
        )
        if replay is not None:
            conn.rollback()
            replay["replayed"] = True
            return replay
        if _conversation_revision(conn, project_id) != revision:
            raise PreflightError(
                "conversation_revision_conflict", "剧本状态已更新，请刷新后重试", 409
            )
        script = _locked_script(conn, project_id)
        if not script:
            raise PreflightError("locked_script_required", "请先锁定当前剧本", 409)
        input_hash = _hash(_current_inputs(project, script, quality_route))
        existing_row = conn.execute(
            "SELECT * FROM short_drama_production_plans "
            "WHERE project_id=? AND input_hash=?",
            (project_id, input_hash),
        ).fetchone()
        if existing_row:
            existing_id = existing_row[0]
            existing_status = existing_row[4]
            conn.execute(
                "UPDATE short_drama_production_plans SET status='superseded' "
                "WHERE project_id=? AND id<>? AND status='draft'",
                (project_id, existing_id),
            )
            if existing_status != "confirmed":
                conn.execute(
                    "UPDATE short_drama_production_plans SET status='draft' WHERE id=?",
                    (existing_id,),
                )
            response = _workspace(conn, project, actor_username, True)
            response["replayed"] = False
            response["reused_plan_id"] = existing_id
        else:
            version = int(conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_production_plans "
                "WHERE project_id=?",
                (project_id,),
            ).fetchone()[0])
            if version > MAX_PLAN_VERSIONS:
                raise PreflightError("production_plan_limit", "制作方案版本数量已达上限")
            plan_id = str(uuid.uuid4())
            now = int(time.time())
            plan = _build_plan(project, script, quality_route)
            conn.execute(
                "UPDATE short_drama_production_plans SET status='superseded' "
                "WHERE project_id=? AND status='draft'",
                (project_id,),
            )
            conn.execute(
                "INSERT INTO short_drama_production_plans "
                "(id,project_id,version,source_script_version_id,status,quality_route,"
                "plan_json,input_hash,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id, project_id, version, script["id"], "draft",
                    quality_route, _json_text(plan), input_hash, actor_username, now,
                ),
            )
            response = _workspace(conn, project, actor_username, True)
            response["replayed"] = False
        _store_request(
            conn, actor_username, project_id, "generate", key, request_hash, response
        )
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def confirm_plan(db_factory, owner_username, actor_username, body, idempotency_key):
    project_id = str(body.get("project_id") or "").strip()
    plan_id = str(body.get("plan_id") or "").strip()
    plan_version = body.get("plan_version")
    if type(plan_version) is not int or plan_version < 1:
        raise PreflightError("plan_version_invalid", "制作方案版本无效")
    accepted = sorted({
        str(item) for item in (body.get("accepted_issue_keys") or [])
        if str(item).strip()
    })
    key = _idempotency_key(idempotency_key)
    request_hash = _hash({
        "plan_id": plan_id,
        "plan_version": plan_version,
        "accepted_issue_keys": accepted,
    })
    conn = _connection(db_factory)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, project_id)
        replay = _existing_request(
            conn, actor_username, project_id, "confirm", key, request_hash
        )
        if replay is not None:
            conn.rollback()
            replay["replayed"] = True
            return replay
        row = conn.execute(
            "SELECT * FROM short_drama_production_plans WHERE id=? AND project_id=?",
            (plan_id, project_id),
        ).fetchone()
        current = _plan(row)
        if not current:
            raise LookupError("production plan does not exist")
        latest = conn.execute(
            "SELECT id FROM short_drama_production_plans WHERE project_id=? "
            "AND status IN ('draft','confirmed') ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if int(current["version"]) != plan_version or latest["id"] != plan_id:
            raise PreflightError("stale_production_plan", "只能确认当前制作方案", 409)
        if current["status"] == "confirmed":
            response = _workspace(conn, project, actor_username, True)
            response["replayed"] = True
            conn.rollback()
            return response
        script = _locked_script(conn, project_id)
        if not script:
            raise PreflightError("locked_script_required", "锁定剧本已失效", 409)
        expected = _hash(_current_inputs(project, script, current["quality_route"]))
        if current["input_hash"] != expected:
            raise PreflightError("stale_production_plan", "制作输入已变化，请重新体检", 409)
        plan = current["plan"]
        if not plan.get("ready") or plan.get("blockers"):
            raise PreflightError("preflight_blocked", "制作方案仍有阻塞问题", 422)
        missing = sorted(set(plan.get("required_acceptance") or []) - set(accepted))
        if missing:
            raise PreflightError(
                "adjustments_not_accepted",
                "请先确认系统建议的时长或推荐素材调整",
                422,
            )
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_production_plans SET status='superseded' "
            "WHERE project_id=? AND id<>? AND status IN ('draft','confirmed')",
            (project_id, plan_id),
        )
        cursor = conn.execute(
            "UPDATE short_drama_production_plans SET status='confirmed',"
            "confirmed_by=?,confirmed_at=? WHERE id=? AND status='draft'",
            (actor_username, now, plan_id),
        )
        if cursor.rowcount != 1:
            raise PreflightError("production_plan_conflict", "制作方案已被更新", 409)
        response = _workspace(conn, project, actor_username, True)
        response["replayed"] = False
        _store_request(
            conn, actor_username, project_id, "confirm", key, request_hash, response
        )
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
