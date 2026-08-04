"""Persistent project settings for the canvas digital presenter domain."""

from contextlib import closing
import hashlib
import json
import re
import time
import uuid

from .canvas_access import PermissionDenied


class RevisionConflict(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_CREATE_OPERATION = "create_project"


EDITABLE_FIELDS = {
    "title",
    "script_text",
    "ratio",
    "resolution",
    "voice_key",
    "target_duration",
}

DEFAULTS = {
    "title": "未命名数字人口播",
    "script_text": "",
    "ratio": "9:16",
    "resolution": "1080p",
    "voice_key": "",
    "avatar_asset_id": None,
    "background_asset_id": None,
    "background_mode": "source",
    "target_duration": 30,
}


def init_db(db_factory):
    with closing(db_factory()) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS digital_presenter_projects(
                id TEXT PRIMARY KEY,
                owner_username TEXT NOT NULL,
                created_by TEXT NOT NULL,
                board_id TEXT NOT NULL,
                title TEXT NOT NULL,
                script_text TEXT NOT NULL DEFAULT '',
                ratio TEXT NOT NULL,
                resolution TEXT NOT NULL DEFAULT '1080p',
                voice_key TEXT NOT NULL DEFAULT '',
                avatar_asset_id TEXT,
                background_asset_id TEXT,
                background_mode TEXT NOT NULL DEFAULT 'source',
                target_duration INTEGER NOT NULL,
                stage TEXT NOT NULL DEFAULT 'draft',
                revision INTEGER NOT NULL DEFAULT 1,
                plan_revision INTEGER NOT NULL DEFAULT 0,
                confirmed_plan_revision INTEGER,
                timeline_revision INTEGER NOT NULL DEFAULT 0,
                spent_points INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_digital_presenter_projects_board "
            "ON digital_presenter_projects(board_id, updated_at DESC)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS digital_presenter_idempotency(
                actor_username TEXT NOT NULL,
                board_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                project_id TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(actor_username, board_id, operation, idempotency_key)
            )"""
        )
        connection.commit()


def _text(value, field, *, empty=True, maximum=20000):
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field)
    value = value.strip() if field != "script_text" else value
    if not empty and not value:
        raise ValueError("%s cannot be empty" % field)
    if len(value) > maximum or any(ord(char) < 9 for char in value):
        raise ValueError("%s is invalid" % field)
    return value


def _optional_id(value, field):
    if value is None:
        return None
    return _text(value, field, empty=False, maximum=200)


def _normalize_fields(payload, *, partial):
    if not isinstance(payload, dict):
        raise ValueError("project body must be a JSON object")
    unknown = set(payload) - EDITABLE_FIELDS
    if unknown:
        raise ValueError("invalid project fields: %s" % ", ".join(sorted(unknown)))
    if partial and not payload:
        raise ValueError("project update cannot be empty")
    values = {} if partial else dict(DEFAULTS)
    values.update(payload)
    if "title" in values:
        values["title"] = _text(values["title"], "title", empty=False, maximum=80)
    if "script_text" in values:
        values["script_text"] = _text(values["script_text"], "script_text")
    if "ratio" in values and values["ratio"] not in {"9:16", "16:9"}:
        raise ValueError("ratio must be 9:16 or 16:9")
    if "resolution" in values and values["resolution"] != "1080p":
        raise ValueError("resolution must be 1080p")
    if "target_duration" in values:
        duration = values["target_duration"]
        if type(duration) is not int or not 30 <= duration <= 180:
            raise ValueError("target_duration must be an integer from 30 to 180")
    if "voice_key" in values:
        values["voice_key"] = _text(values["voice_key"], "voice_key", maximum=200)
    for field in ("avatar_asset_id", "background_asset_id"):
        if field in values:
            values[field] = _optional_id(values[field], field)
    if "background_mode" in values and values["background_mode"] not in {
        "source", "separate", "none"
    }:
        raise ValueError("background_mode is invalid")
    return values


def _public(row):
    result = dict(row)
    result["deleted"] = bool(result["deleted"])
    return result


def _row(connection, project_id, *, include_deleted=False):
    sql = "SELECT * FROM digital_presenter_projects WHERE id=?"
    if not include_deleted:
        sql += " AND deleted=0"
    return connection.execute(sql, (project_id,)).fetchone()


def _visible_row(connection, access, project_id, *, include_deleted=False):
    row = _row(connection, project_id, include_deleted=include_deleted)
    if not row or row["board_id"] != access.board_id:
        raise LookupError("数字人口播项目不存在")
    return row


def _revision(value):
    if type(value) is not int or value < 1:
        raise ValueError("项目版本无效")
    return value


def _idempotency_key(value):
    key = str(value or "").strip()
    if not key:
        raise ValueError("missing Idempotency-Key")
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise ValueError("Idempotency-Key must be 8-128 letters, numbers, or . _ : -")
    return key


def _request_hash(fields):
    canonical = json.dumps(
        fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotency_row(connection, access, key):
    return connection.execute(
        "SELECT request_hash,response_json FROM digital_presenter_idempotency "
        "WHERE actor_username=? AND board_id=? AND operation=? AND idempotency_key=?",
        (access.actor_username, access.board_id, _CREATE_OPERATION, key),
    ).fetchone()


def create_project(db_factory, access, payload, idempotency_key):
    access.require_write()
    fields = _normalize_fields(payload, partial=False)
    idempotency_key = _idempotency_key(idempotency_key)
    request_hash = _request_hash(fields)
    now = int(time.time())
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = _idempotency_row(connection, access, idempotency_key)
            if existing:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict(
                        "the same Idempotency-Key cannot be used for a different request"
                    )
                response = json.loads(existing["response_json"])
                connection.commit()
                return response

            project_id = "dp_" + uuid.uuid4().hex
            connection.execute(
                """INSERT INTO digital_presenter_projects(
                    id, owner_username, created_by, board_id, title, script_text,
                    ratio, resolution, voice_key, avatar_asset_id, background_asset_id,
                    background_mode, target_duration, stage, revision, plan_revision,
                    confirmed_plan_revision, timeline_revision, spent_points, deleted,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',1,0,NULL,0,0,0,?,?)""",
                (
                    project_id, access.board_owner_username, access.actor_username,
                    access.board_id, fields["title"], fields["script_text"], fields["ratio"],
                    fields["resolution"], fields["voice_key"], fields["avatar_asset_id"],
                    fields["background_asset_id"], fields["background_mode"],
                    fields["target_duration"], now, now,
                ),
            )
            response = _public(_row(connection, project_id))
            connection.execute(
                """INSERT INTO digital_presenter_idempotency(
                    actor_username,board_id,operation,idempotency_key,request_hash,
                    project_id,response_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    access.actor_username, access.board_id, _CREATE_OPERATION,
                    idempotency_key, request_hash, project_id,
                    json.dumps(response, ensure_ascii=False, sort_keys=True), now, now,
                ),
            )
            connection.commit()
            return response
        except Exception:
            connection.rollback()
            raise


def get_project(db_factory, access, project_id):
    with closing(db_factory()) as connection:
        row = _visible_row(connection, access, project_id)
    access.require_read()
    return _public(row)


def update_project(db_factory, access, project_id, revision, patch):
    revision = _revision(revision)
    fields = _normalize_fields(patch, partial=True)
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _visible_row(connection, access, project_id)
            access.require_write()
            if row["revision"] != revision:
                raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
            now = int(time.time())
            assignments = ["%s=?" % field for field in fields]
            values = [fields[field] for field in fields]
            assignments.extend(["revision=revision+1", "updated_at=?"])
            values.extend([now, project_id, access.board_id, revision])
            cursor = connection.execute(
                "UPDATE digital_presenter_projects SET %s "
                "WHERE id=? AND board_id=? AND revision=? AND deleted=0"
                % ", ".join(assignments),
                values,
            )
            if cursor.rowcount != 1:
                raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
            updated = _row(connection, project_id)
            connection.commit()
            return _public(updated)
        except Exception:
            connection.rollback()
            raise


def delete_project(db_factory, access, project_id, revision):
    revision = _revision(revision)
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _visible_row(connection, access, project_id)
            access.require_delete()
            if row["revision"] != revision:
                raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
            cursor = connection.execute(
                "UPDATE digital_presenter_projects SET deleted=1, revision=revision+1, updated_at=? "
                "WHERE id=? AND board_id=? AND revision=? AND deleted=0",
                (int(time.time()), project_id, access.board_id, revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
            deleted = _row(connection, project_id, include_deleted=True)
            connection.commit()
            return _public(deleted)
        except Exception:
            connection.rollback()
            raise
