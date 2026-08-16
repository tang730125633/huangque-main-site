"""Character preparation for the standalone short-drama workspace.

The conversation script remains immutable.  This module materializes its
characters into the existing production character table and owns only the
mutable production profile and avatar binding.
"""

import hashlib
import json
import sqlite3
import time
import uuid


class CharacterStudioError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


def _connection(db_factory):
    conn = db_factory()
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _json(value, fallback):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed


def _text(value, limit=4000):
    return str(value or "").strip()[:limit]


def _locked_script(conn, project_id):
    row = conn.execute(
        "SELECT snapshot.script_json,snapshot.id,snapshot.version "
        "FROM short_drama_conversations conversation "
        "JOIN short_drama_script_snapshots snapshot "
        "ON snapshot.id=conversation.locked_version_id "
        "WHERE conversation.project_id=? AND conversation.state='script_locked' "
        "AND snapshot.project_id=? AND snapshot.status='locked'",
        (project_id, project_id),
    ).fetchone()
    if not row:
        raise CharacterStudioError(
            "script_not_locked", "请先锁定剧本，再准备角色形象", 409
        )
    return {
        "id": row["id"],
        "version": int(row["version"]),
        "script": _json(row["script_json"], {}),
    }


def _stable_character_key(name, index):
    digest = hashlib.sha256(
        ("%s\0%d" % (_text(name, 200), int(index))).encode("utf-8")
    ).hexdigest()[:12]
    return "role_" + digest


def _script_characters(script):
    """Return the union of declared, speaking and visible characters."""
    declared = []
    by_key = {}
    for index, item in enumerate(script.get("characters") or []):
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), 200) or "未命名角色"
        key = _text(item.get("character_key"), 160) or _stable_character_key(
            name, index
        )
        if key in by_key:
            continue
        character = {
            "character_key": key,
            "name": name,
            "identity_text": _text(
                item.get("identity_text") or item.get("identity"), 2000
            ) or ("故事中的" + name),
            "personality": _text(item.get("personality"), 4000),
            "sort_order": len(declared),
        }
        by_key[key] = character
        declared.append(character)

    speaker_names = {}
    for line in script.get("dialogue_lines") or []:
        if not isinstance(line, dict):
            continue
        key = _text(line.get("character_key"), 160)
        name = _text(line.get("speaker") or line.get("speaker_name"), 200)
        if key and name:
            speaker_names[key] = name
        if key and key not in by_key and key != "narrator":
            character = {
                "character_key": key,
                "name": name or key,
                "identity_text": "剧本台词中的角色",
                "personality": "",
                "sort_order": len(declared),
            }
            by_key[key] = character
            declared.append(character)

    for shot in script.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        for key_value in shot.get("character_keys") or []:
            key = _text(key_value, 160)
            if not key or key == "narrator" or key in by_key:
                continue
            character = {
                "character_key": key,
                "name": speaker_names.get(key) or key,
                "identity_text": "剧本镜头中的角色",
                "personality": "",
                "sort_order": len(declared),
            }
            by_key[key] = character
            declared.append(character)
    return declared


def _confirmed_character_contract(conn, project_id):
    row = conn.execute(
        "SELECT character_contract_json FROM short_drama_script_imports "
        "WHERE project_id=? AND content_type='live_action' AND status='completed' "
        "ORDER BY updated_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    contract = _json(row[0], []) if row else []
    return [item for item in contract if isinstance(item, dict) and _text(
        item.get("character_key"), 160
    )]


def _sync_locked_characters(conn, project_id, locked, confirmed_contract=None):
    """Idempotently add missing roles without overwriting user preparation."""
    characters = _script_characters(locked["script"])
    confirmed_keys = {
        _text(item.get("character_key"), 160)
        for item in confirmed_contract or []
    }
    if confirmed_keys:
        characters = [
            item for item in characters
            if item["character_key"] in confirmed_keys
        ]
    now = int(time.time())
    for item in characters:
        existing = conn.execute(
            "SELECT id,name,identity_text,personality FROM short_drama_characters "
            "WHERE project_id=? AND character_key=?",
            (project_id, item["character_key"]),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE short_drama_characters SET "
                "name=CASE WHEN name='' THEN ? ELSE name END,"
                "identity_text=CASE WHEN identity_text='' THEN ? ELSE identity_text END,"
                "personality=CASE WHEN personality='' THEN ? ELSE personality END,"
                "sort_order=? WHERE project_id=? AND character_key=?",
                (
                    item["name"], item["identity_text"], item["personality"],
                    item["sort_order"], project_id, item["character_key"],
                ),
            )
            continue
        conn.execute(
            "INSERT INTO short_drama_characters "
            "(id,project_id,character_key,name,identity_text,personality,"
            "source_type,avatar_id,appearance_prompt,wardrobe_prompt,"
            "voice_key,voice_settings_json,sort_order) "
            "VALUES (?,?,?,?,?,?,'ai_character',NULL,'','',NULL,'{}',?)",
            (
                str(uuid.uuid4()), project_id, item["character_key"], item["name"],
                item["identity_text"], item["personality"], item["sort_order"],
            ),
        )
    return characters


def _affected_shots(script, character_key):
    dialogue = {
        _text(line.get("id"), 160): _text(line.get("character_key"), 160)
        for line in script.get("dialogue_lines") or []
        if isinstance(line, dict)
    }
    result = []
    for index, shot in enumerate(script.get("shots") or []):
        if not isinstance(shot, dict):
            continue
        visible = {
            _text(value, 160) for value in shot.get("character_keys") or []
        }
        speaking = {
            dialogue.get(_text(value, 160), "")
            for value in shot.get("dialogue_line_ids") or []
        }
        if character_key in visible or character_key in speaking:
            result.append({
                "shot_key": _text(shot.get("shot_key"), 160)
                or "shot_%02d" % (index + 1),
                "sort_order": int(shot.get("sort_order") or index + 1),
            })
    return result


def _avatar_items(owner_username, avatar_list):
    if not callable(avatar_list):
        return []
    try:
        candidates = avatar_list(owner_username, 120)
    except Exception:
        candidates = []
    result = []
    for avatar in candidates or []:
        if (
            not isinstance(avatar, dict)
            or str(avatar.get("status") or "") != "ready"
            or not str(avatar.get("provider_avatar_id") or "").strip()
        ):
            continue
        result.append({
            "id": str(avatar.get("id") or ""),
            "name": _text(avatar.get("name"), 200) or "未命名形象",
            "image_url": _text(avatar.get("image_url"), 4000),
            "status": "ready",
            "provider_bound": True,
        })
    return result


def _write_job_binding(conn, job_id, result, binding, status, message,
                       project_revision=None):
    outcome = dict(binding)
    outcome.update({"status": status, "message": message})
    if project_revision is not None:
        outcome["project_revision"] = int(project_revision)
    result = dict(result)
    result["short_drama_binding"] = outcome
    conn.execute(
        "UPDATE jobs SET result=?,updated_at=? WHERE id=? AND kind='avatar' "
        "AND status='done'",
        (json.dumps(result, ensure_ascii=False), int(time.time()), int(job_id)),
    )
    return outcome


def reconcile_avatar_job(db_factory, job_id, owner_username=None):
    """Idempotently bind a completed Avatar job to its persisted role target."""
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT id,username,kind,status,payload,result FROM jobs WHERE id=?",
            (int(job_id),),
        ).fetchone()
        if (
            not job or job["kind"] != "avatar" or job["status"] != "done"
            or (owner_username and job["username"] != owner_username)
        ):
            conn.rollback()
            return None
        payload = _json(job["payload"], {})
        result = _json(job["result"], {})
        binding = payload.get("short_drama_binding") or {}
        if not isinstance(binding, dict) or not binding:
            conn.rollback()
            return None
        previous = result.get("short_drama_binding") or {}
        if previous.get("status") in {"bound", "conflict", "failed"}:
            conn.rollback()
            return previous
        project_id = _text(binding.get("project_id"), 160)
        character_key = _text(binding.get("character_key"), 160)
        expected_revision = binding.get("project_revision")
        avatar_id = result.get("avatar_id")
        if (
            not project_id or not character_key
            or type(expected_revision) is not int or expected_revision < 1
            or not str(avatar_id or "").isdigit()
        ):
            outcome = _write_job_binding(
                conn, job_id, result, binding, "failed",
                "自动绑定参数不完整，请手动选择电影化身",
            )
            conn.commit()
            return outcome
        project = conn.execute(
            "SELECT revision FROM short_drama_projects WHERE id=? AND username=? "
            "AND deleted=0",
            (project_id, job["username"]),
        ).fetchone()
        character = conn.execute(
            "SELECT avatar_id FROM short_drama_characters WHERE project_id=? "
            "AND character_key=?",
            (project_id, character_key),
        ).fetchone()
        avatar = conn.execute(
            "SELECT id,username,status,provider_avatar_id FROM avatars WHERE id=?",
            (int(avatar_id),),
        ).fetchone()
        if not project or not character:
            outcome = _write_job_binding(
                conn, job_id, result, binding, "failed",
                "项目或角色已不存在，未执行自动绑定",
            )
            conn.commit()
            return outcome
        if (
            not avatar or avatar["username"] != job["username"]
            or avatar["status"] != "ready"
            or not _text(avatar["provider_avatar_id"], 400)
        ):
            outcome = _write_job_binding(
                conn, job_id, result, binding, "failed",
                "电影化身不可用或归属不匹配，未执行自动绑定",
                project["revision"],
            )
            conn.commit()
            return outcome
        if character["avatar_id"] == int(avatar_id):
            outcome = _write_job_binding(
                conn, job_id, result, binding, "bound", "电影化身已绑定",
                project["revision"],
            )
            conn.commit()
            return outcome
        if int(project["revision"]) != expected_revision:
            outcome = _write_job_binding(
                conn, job_id, result, binding, "conflict",
                "项目已更新，请刷新后手动确认电影化身绑定",
                project["revision"],
            )
            conn.commit()
            return outcome
        if conn.execute(
            "SELECT 1 FROM short_drama_characters WHERE project_id=? "
            "AND character_key<>? AND avatar_id=? LIMIT 1",
            (project_id, character_key, int(avatar_id)),
        ).fetchone():
            outcome = _write_job_binding(
                conn, job_id, result, binding, "conflict",
                "该电影化身已绑定到其他角色",
                project["revision"],
            )
            conn.commit()
            return outcome
        changed = conn.execute(
            "UPDATE short_drama_characters SET source_type='cinematic_avatar',"
            "avatar_id=? WHERE project_id=? AND character_key=?",
            (int(avatar_id), project_id, character_key),
        )
        project_changed = conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1,updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND deleted=0",
            (int(time.time()), project_id, job["username"], expected_revision),
        )
        if changed.rowcount != 1 or project_changed.rowcount != 1:
            raise CharacterStudioError(
                "project_revision_conflict", "项目已更新，请刷新后重试", 409
            )
        outcome = _write_job_binding(
            conn, job_id, result, binding, "bound", "电影化身已自动绑定",
            expected_revision + 1,
        )
        conn.commit()
        return outcome
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _reconcile_completed_avatar_jobs(db_factory, owner_username, project_id):
    conn = _connection(db_factory)
    try:
        rows = conn.execute(
            "SELECT id,payload FROM jobs WHERE username=? AND kind='avatar' "
            "AND status='done' ORDER BY id DESC LIMIT 200",
            (owner_username,),
        ).fetchall()
    except sqlite3.OperationalError:
        return
    finally:
        conn.close()
    for row in rows:
        binding = _json(row["payload"], {}).get("short_drama_binding") or {}
        if str(binding.get("project_id") or "") != str(project_id):
            continue
        try:
            reconcile_avatar_job(db_factory, row["id"], owner_username)
        except (sqlite3.Error, CharacterStudioError, TypeError, ValueError):
            # Job completion already succeeded; a transient reconciliation
            # failure must not make the whole character workspace unavailable.
            continue


def workspace(
    db_factory, owner_username, actor_username, project_id, can_edit=True,
    avatar_list=None,
):
    _reconcile_completed_avatar_jobs(db_factory, owner_username, project_id)
    conn = _connection(db_factory)
    try:
        project = conn.execute(
            "SELECT id,revision,stage FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, owner_username),
        ).fetchone()
        if not project:
            raise LookupError("short drama project does not exist")
        locked = _locked_script(conn, project_id)
        confirmed_contract = _confirmed_character_contract(conn, project_id)
        _sync_locked_characters(
            conn, project_id, locked, confirmed_contract=confirmed_contract
        )
        conn.commit()
        avatars = _avatar_items(owner_username, avatar_list)
        avatars_by_id = {str(item["id"]): item for item in avatars}
        rows = conn.execute(
            "SELECT * FROM short_drama_characters WHERE project_id=? "
            "ORDER BY sort_order,id",
            (project_id,),
        ).fetchall()
        confirmed_keys = {
            _text(item.get("character_key"), 160)
            for item in confirmed_contract
        }
        if confirmed_keys:
            rows = [row for row in rows if row["character_key"] in confirmed_keys]
        active_reference_jobs = {}
        for reference_row in conn.execute(
            "SELECT character_key,job_id,status "
            "FROM short_drama_character_reference_jobs "
            "WHERE project_id=? AND owner_username=? "
            "AND status IN ('linked','ready') ORDER BY updated_at DESC",
            (project_id, owner_username),
        ).fetchall():
            active_reference_jobs.setdefault(
                reference_row["character_key"], reference_row
            )
        characters = []
        for row in rows:
            item = dict(row)
            active_reference = active_reference_jobs.get(item["character_key"])
            if active_reference:
                item["reference_job_id"] = int(active_reference["job_id"])
                item["reference_job_status"] = active_reference["status"]
            elif item.get("reference_job_id"):
                item["reference_job_status"] = "done"
            avatar = avatars_by_id.get(str(item.get("avatar_id") or ""))
            image_url = (
                avatar.get("image_url", "") if avatar
                else _text(item.get("reference_url"), 4000)
            )
            profile_ready = all(_text(item.get(field)) for field in (
                "identity_text", "personality", "appearance_prompt",
                "wardrobe_prompt",
            ))
            binding_ready = bool(
                avatar and avatar.get("provider_bound")
            )
            item.update({
                "image_url": image_url,
                "reference_image_url": _text(
                    item.get("reference_url"), 4000
                ),
                "avatar": avatar,
                "profile_ready": profile_ready,
                "binding_ready": binding_ready,
                "affected_shots": _affected_shots(
                    locked["script"], item["character_key"]
                ),
            })
            characters.append(item)
        return {
            "project_id": project_id,
            "project_revision": int(project["revision"]),
            "script_version_id": locked["id"],
            "script_version": locked["version"],
            "characters": characters,
            "avatars": avatars,
            "summary": {
                "total": len(characters),
                "profile_ready": sum(
                    bool(item["profile_ready"]) for item in characters
                ),
                "image_ready": sum(
                    bool(item["image_url"]) for item in characters
                ),
                "binding_ready": sum(
                    bool(item["binding_ready"]) for item in characters
                ),
            },
            "permissions": {
                "can_edit": bool(can_edit),
                "actor": actor_username,
                "can_create_avatar": owner_username == actor_username,
            },
        }
    finally:
        conn.close()


def save_profile(db_factory, owner_username, body):
    required = {
        "project_id", "project_revision", "character_key", "identity_text",
        "personality", "appearance_prompt", "wardrobe_prompt",
    }
    allowed = required | {"name"}
    if (
        not isinstance(body, dict)
        or not required.issubset(body)
        or set(body) - allowed
    ):
        raise CharacterStudioError(
            "character_profile_invalid", "角色档案字段不完整", 422
        )
    project_id = _text(body.get("project_id"), 160)
    character_key = _text(body.get("character_key"), 160)
    supplied_name = "name" in body
    raw_name = str(body.get("name") or "").strip() if supplied_name else ""
    name = _text(raw_name, 20) if supplied_name else None
    revision = body.get("project_revision")
    values = {
        field: _text(body.get(field), 4000)
        for field in (
            "identity_text", "personality", "appearance_prompt",
            "wardrobe_prompt",
        )
    }
    if (
        not project_id or not character_key or type(revision) is not int
        or not all(values.values())
    ):
        raise CharacterStudioError(
            "character_profile_incomplete",
            "请完整填写身份、性格、外貌和穿着信息",
            422,
        )
    if supplied_name and (not name or len(raw_name) > 20):
        raise CharacterStudioError(
            "character_name_invalid", "角色名称需为 1 至 20 个字符", 422
        )
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT revision FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, owner_username),
        ).fetchone()
        if not project:
            raise LookupError("short drama project does not exist")
        _locked_script(conn, project_id)
        if int(project["revision"]) != revision:
            raise CharacterStudioError(
                "project_revision_conflict", "项目已更新，请刷新后重试", 409
            )
        current = conn.execute(
            "SELECT name,identity_text,personality,appearance_prompt,wardrobe_prompt "
            "FROM short_drama_characters WHERE project_id=? AND character_key=?",
            (project_id, character_key),
        ).fetchone()
        if not current:
            raise CharacterStudioError(
                "character_not_found", "剧本中不存在该角色", 404
            )
        if supplied_name:
            duplicate = conn.execute(
                "SELECT 1 FROM short_drama_characters "
                "WHERE project_id=? AND character_key<>? "
                "AND name=? COLLATE NOCASE LIMIT 1",
                (project_id, character_key, name),
            ).fetchone()
            if duplicate:
                raise CharacterStudioError(
                    "character_name_duplicate", "角色名称已被其他角色使用", 409
                )
        prompt_changed = (
            current["appearance_prompt"] != values["appearance_prompt"]
            or current["wardrobe_prompt"] != values["wardrobe_prompt"]
        )
        profile_changed = (
            (supplied_name and current["name"] != name)
            or current["identity_text"] != values["identity_text"]
            or current["personality"] != values["personality"]
            or prompt_changed
        )
        if not profile_changed:
            conn.commit()
            return {
                "ok": True,
                "project_revision": revision,
                "name": current["name"],
            }
        conn.execute(
            "UPDATE short_drama_characters SET "
            "name=CASE WHEN ? IS NULL THEN name ELSE ? END,"
            "identity_text=?,personality=?,"
            "appearance_prompt=?,wardrobe_prompt=?,"
            "reference_job_id=CASE WHEN ? THEN NULL ELSE reference_job_id END,"
            "reference_file=CASE WHEN ? THEN '' ELSE reference_file END,"
            "reference_url=CASE WHEN ? THEN '' ELSE reference_url END,"
            "reference_locked=CASE WHEN ? THEN 0 ELSE reference_locked END "
            "WHERE project_id=? AND character_key=?",
            (
                name, name,
                values["identity_text"], values["personality"],
                values["appearance_prompt"], values["wardrobe_prompt"],
                int(prompt_changed), int(prompt_changed), int(prompt_changed),
                int(prompt_changed), project_id, character_key,
            ),
        )
        changed = conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1,updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND deleted=0",
            (int(time.time()), project_id, owner_username, revision),
        )
        if changed.rowcount != 1:
            raise CharacterStudioError(
                "project_revision_conflict", "项目已更新，请刷新后重试", 409
            )
        conn.commit()
        return {
            "ok": True,
            "project_revision": revision + 1,
            "name": name if supplied_name else current["name"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bind_avatar(db_factory, owner_username, body, avatar_lookup=None):
    expected = {
        "project_id", "project_revision", "character_key", "avatar_id",
    }
    if not isinstance(body, dict) or set(body) != expected:
        raise CharacterStudioError(
            "character_binding_invalid", "角色形象绑定字段不完整", 422
        )
    project_id = _text(body.get("project_id"), 160)
    character_key = _text(body.get("character_key"), 160)
    avatar_id = _text(body.get("avatar_id"), 160)
    revision = body.get("project_revision")
    if (
        not project_id or not character_key or type(revision) is not int
        or (avatar_id and not callable(avatar_lookup))
    ):
        raise CharacterStudioError(
            "character_binding_invalid", "角色或电影化身参数无效", 422
        )
    avatar = None
    if avatar_id:
        try:
            avatar = avatar_lookup(owner_username, avatar_id)
        except Exception as error:
            raise CharacterStudioError(
                "avatar_not_found", "所选电影化身不存在或无权使用", 404
            ) from error
        if (
            not isinstance(avatar, dict)
            or str(avatar.get("username") or "") != owner_username
            or str(avatar.get("status") or "") != "ready"
            or not str(avatar.get("provider_avatar_id") or "").strip()
        ):
            raise CharacterStudioError(
                "avatar_not_ready", "所选电影化身尚未完成 Provider 绑定", 422
            )
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT revision FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, owner_username),
        ).fetchone()
        if not project:
            raise LookupError("short drama project does not exist")
        _locked_script(conn, project_id)
        if int(project["revision"]) != revision:
            raise CharacterStudioError(
                "project_revision_conflict", "项目已更新，请刷新后重试", 409
            )
        if avatar_id and conn.execute(
            "SELECT 1 FROM short_drama_characters WHERE project_id=? "
            "AND character_key<>? AND avatar_id=? LIMIT 1",
            (project_id, character_key, int(avatar["id"])),
        ).fetchone():
            raise CharacterStudioError(
                "duplicate_avatar_binding",
                "同一个电影化身不能同时代表两个剧本角色",
                409,
            )
        changed = conn.execute(
            "UPDATE short_drama_characters SET source_type=?,avatar_id=? "
            "WHERE project_id=? AND character_key=?",
            (
                "cinematic_avatar" if avatar_id else "ai_character",
                int(avatar["id"]) if avatar_id else None,
                project_id, character_key,
            ),
        )
        if changed.rowcount != 1:
            raise CharacterStudioError(
                "character_not_found", "剧本中不存在该角色", 404
            )
        project_changed = conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1,updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND deleted=0",
            (int(time.time()), project_id, owner_username, revision),
        )
        if project_changed.rowcount != 1:
            raise CharacterStudioError(
                "project_revision_conflict", "项目已更新，请刷新后重试", 409
            )
        conn.commit()
        return {"ok": True, "project_revision": revision + 1}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
