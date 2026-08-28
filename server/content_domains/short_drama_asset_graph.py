"""Versioned asset graph and immutable per-shot generation packages."""

import base64
import hashlib
import json
import re
import sqlite3
import time
import urllib.parse
import uuid
from contextlib import closing


ASSET_TYPES = {
    "character", "scene", "costume", "makeup", "prop", "vehicle", "clue",
}
RELATION_TYPES = {
    "appears_in", "located_in", "wears", "uses", "drives", "reveals", "related",
}
_SCENE_BINDING_DISABLED_RELATION = "scene_binding_disabled"


class AssetGraphError(ValueError):
    def __init__(self, code, message, status=400, blockers=None):
        super().__init__(message)
        self.code = code
        self.status = int(status)
        self.blockers = list(blockers or [])


SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_graph_state (
  project_id TEXT PRIMARY KEY REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS short_drama_graph_entities (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  asset_key TEXT NOT NULL,
  asset_type TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
  current_version_id TEXT,
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(project_id, asset_key)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_graph_entities_project
  ON short_drama_graph_entities(project_id, asset_type, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS short_drama_graph_versions (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES short_drama_graph_entities(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version >= 1),
  parent_id TEXT REFERENCES short_drama_graph_versions(id),
  status TEXT NOT NULL CHECK (status IN ('draft','locked','retired')),
  prompt TEXT NOT NULL DEFAULT '',
  negative_prompt TEXT NOT NULL DEFAULT '',
  references_json TEXT NOT NULL DEFAULT '[]',
  attributes_json TEXT NOT NULL DEFAULT '{}',
  valid_from TEXT NOT NULL DEFAULT '',
  valid_to TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  locked_at INTEGER,
  UNIQUE(entity_id, version)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_graph_versions_entity
  ON short_drama_graph_versions(entity_id, version DESC);
CREATE TABLE IF NOT EXISTS short_drama_graph_relations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  source_scope TEXT NOT NULL CHECK (source_scope IN ('project','shot','asset')),
  source_id TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  entity_id TEXT NOT NULL REFERENCES short_drama_graph_entities(id) ON DELETE CASCADE,
  version_id TEXT REFERENCES short_drama_graph_versions(id),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(project_id, source_scope, source_id, relation_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_graph_relations_source
  ON short_drama_graph_relations(project_id, source_scope, source_id);
CREATE TABLE IF NOT EXISTS short_drama_graph_shot_snapshots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version >= 1),
  graph_revision INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ready','blocked')),
  package_json TEXT NOT NULL,
  package_hash TEXT NOT NULL,
  blockers_json TEXT NOT NULL DEFAULT '[]',
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(project_id, shot_id, version)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_graph_shot_snapshots_shot
  ON short_drama_graph_shot_snapshots(project_id, shot_id, version DESC);
CREATE TABLE IF NOT EXISTS short_drama_graph_audit (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target_id TEXT NOT NULL DEFAULT '',
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL
);
"""


def _json(value, fallback):
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value, limit=4000):
    return str(value or "").strip()[:limit]


def _connection(db_factory):
    conn = db_factory()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_factory):
    with closing(_connection(db_factory)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _project(conn, owner, project_id):
    row = conn.execute(
        "SELECT id,revision FROM short_drama_projects "
        "WHERE id=? AND username=? AND deleted=0", (project_id, owner),
    ).fetchone()
    if not row:
        raise LookupError("short drama project does not exist")
    return row


def _ensure_state(conn, project_id, now):
    conn.execute(
        "INSERT OR IGNORE INTO short_drama_graph_state"
        "(project_id,revision,updated_at) VALUES (?,1,?)", (project_id, now),
    )
    return conn.execute(
        "SELECT revision FROM short_drama_graph_state WHERE project_id=?",
        (project_id,),
    ).fetchone()[0]


def _bump(conn, project_id, expected_revision, now):
    changed = conn.execute(
        "UPDATE short_drama_graph_state SET revision=revision+1,updated_at=? "
        "WHERE project_id=? AND revision=?", (now, project_id, expected_revision),
    ).rowcount
    if changed != 1:
        raise AssetGraphError("graph_revision_conflict", "资产图谱已更新，请刷新后重试", 409)
    return expected_revision + 1


def _audit(conn, project_id, actor, action, target_id, details, now):
    conn.execute(
        "INSERT INTO short_drama_graph_audit"
        "(id,project_id,actor,action,target_id,details_json,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), project_id, actor, action, target_id,
         _canonical(details or {}), now),
    )


def invalidate_shot_content(conn, project_id, actor, changed_shot_ids, removed_shot_ids):
    """Invalidate graph snapshots after a transactional storyboard save.

    The graph state row is the project-level opt-in marker.  Legacy projects
    without that row keep their old generation path, while graph-enabled
    projects advance once per save whenever provider-relevant shot content or
    membership changes.
    """
    changed = sorted({str(value) for value in changed_shot_ids or [] if value})
    removed = sorted({str(value) for value in removed_shot_ids or [] if value})
    if removed:
        placeholders = ",".join("?" for _ in removed)
        conn.execute(
            "DELETE FROM short_drama_graph_relations WHERE project_id=? "
            "AND source_scope='shot' AND source_id IN (%s)" % placeholders,
            (project_id, *removed),
        )
    state = conn.execute(
        "SELECT revision FROM short_drama_graph_state WHERE project_id=?",
        (project_id,),
    ).fetchone()
    if not state or not (changed or removed):
        return int(state[0]) if state else None
    now = int(time.time())
    revision = int(state[0]) + 1
    conn.execute(
        "UPDATE short_drama_graph_state SET revision=?,updated_at=? WHERE project_id=?",
        (revision, now, project_id),
    )
    _audit(
        conn, project_id, actor, "storyboard_changed", project_id,
        {"changed_shot_ids": changed, "removed_shot_ids": removed,
         "graph_revision": revision}, now,
    )
    return revision


def _script_shot_map(script):
    result = {}
    for index, item in enumerate((script or {}).get("shots") or []):
        if not isinstance(item, dict):
            continue
        shot_key = _text(item.get("shot_key"), 160) or "shot_%02d" % (index + 1)
        result[shot_key] = {
            "scene": _text(
                item.get("scene_description") or item.get("scene") or item.get("visual"),
                4000,
            ),
            "provider": {
                "sort_order": int(item.get("sort_order") or index + 1),
                "duration": int(item.get("duration_seconds") or item.get("duration") or 5),
                "camera": _text(item.get("camera_description") or item.get("camera"), 4000),
                "visual": _text(item.get("visual"), 4000),
                "image_prompt": _text(item.get("image_prompt"), 8000),
                "video_prompt": _text(item.get("video_prompt"), 8000),
                "provider_prompt": _text(item.get("provider_prompt"), 8000),
                "character_keys": item.get("character_keys") or [],
                "dialogue_line_ids": item.get("dialogue_line_ids") or [],
            },
        }
    return result


def invalidate_script_mutation(conn, project_id, actor, before_script, after_script):
    """Atomically retire stale scene references and snapshots after script edits."""
    conn.row_factory = sqlite3.Row
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    before = _script_shot_map(before_script)
    after = _script_shot_map(after_script)
    changed = {
        key for key in before.keys() & after.keys()
        if before[key] != after[key]
    }
    removed = set(before) - set(after)
    added = set(after) - set(before)
    if not (changed or removed or added):
        if "short_drama_graph_state" not in tables:
            return None
        state = conn.execute(
            "SELECT revision FROM short_drama_graph_state WHERE project_id=?", (project_id,),
        ).fetchone()
        return int(state[0]) if state else None
    now = int(time.time())
    invalidated_still_shot_ids = []
    if "short_drama_assets" in tables:
        legacy_by_key = _legacy_shots_by_key(conn, project_id)
        invalidated_still_shot_ids = sorted({
            str(legacy_by_key[key]["id"])
            for key in changed | removed if key in legacy_by_key
        })
        if invalidated_still_shot_ids:
            placeholders = ",".join("?" for _ in invalidated_still_shot_ids)
            conn.execute(
                "UPDATE short_drama_assets SET locked=0,updated_at=? "
                "WHERE project_id=? AND shot_id IN (%s)" % placeholders,
                (now, project_id, *invalidated_still_shot_ids),
            )
    if "short_drama_graph_state" not in tables:
        return None
    state = conn.execute(
        "SELECT revision FROM short_drama_graph_state WHERE project_id=?", (project_id,),
    ).fetchone()
    if not state:
        return None
    rows = conn.execute(
        "SELECT relation.id,relation.source_id,relation.entity_id,relation.relation_type,"
        "relation.metadata_json,shot.shot_key,entity.asset_key,entity.description "
        "FROM short_drama_graph_relations relation "
        "JOIN short_drama_graph_entities entity ON entity.id=relation.entity_id "
        "LEFT JOIN short_drama_shots shot ON shot.id=relation.source_id "
        "AND shot.project_id=relation.project_id WHERE relation.project_id=? "
        "AND relation.source_scope='shot'", (project_id,),
    ).fetchall()
    relations_by_key = {}
    for row in rows:
        metadata = _json(row["metadata_json"], {})
        shot_key = _text(metadata.get("shot_key"), 160) or _text(row["shot_key"], 160)
        if shot_key:
            relations_by_key.setdefault(shot_key, []).append(row)
    invalidated_entities = set()
    for shot_key in sorted(changed):
        if before[shot_key]["scene"] == after[shot_key]["scene"]:
            continue
        for relation in relations_by_key.get(shot_key, []):
            if relation["relation_type"] != "located_in":
                continue
            entity_id = str(relation["entity_id"])
            if entity_id in invalidated_entities:
                continue
            custom = _text(relation["asset_key"], 240).startswith("scene-custom:")
            description = (
                _text(relation["description"], 4000)
                if custom else after[shot_key]["scene"]
            )
            if not custom:
                conn.execute(
                    "UPDATE short_drama_graph_entities SET description=?,updated_at=? "
                    "WHERE id=?", (description, now, entity_id),
                )
            _invalidate_scene_reference_versions(conn, entity_id, description, actor, now)
            invalidated_entities.add(entity_id)
    removed_scene_entities = {
        str(row["entity_id"])
        for key in removed for row in relations_by_key.get(key, [])
        if (
            row["relation_type"] == "located_in"
            and not _text(row["asset_key"], 240).startswith("scene-custom:")
        )
    }
    removed_relation_ids = [
        str(row["id"]) for key in removed for row in relations_by_key.get(key, [])
    ]
    if removed_relation_ids:
        conn.executemany(
            "DELETE FROM short_drama_graph_relations WHERE id=?",
            [(relation_id,) for relation_id in removed_relation_ids],
        )
    retired_entities = set()
    for entity_id in sorted(removed_scene_entities):
        if conn.execute(
            "SELECT 1 FROM short_drama_graph_relations WHERE project_id=? "
            "AND entity_id=? AND source_scope='shot' AND relation_type='located_in' LIMIT 1",
            (project_id, entity_id),
        ).fetchone():
            continue
        conn.execute(
            "UPDATE short_drama_graph_versions SET status='retired' "
            "WHERE entity_id=? AND status<>'retired'", (entity_id,),
        )
        conn.execute(
            "UPDATE short_drama_graph_entities SET status='retired',"
            "current_version_id=NULL,updated_at=? WHERE id=?",
            (now, entity_id),
        )
        retired_entities.add(entity_id)

    revision = int(state[0]) + 1
    conn.execute(
        "UPDATE short_drama_graph_state SET revision=?,updated_at=? WHERE project_id=?",
        (revision, now, project_id),
    )
    _audit(conn, project_id, actor, "conversation_script_changed", project_id, {
        "changed_shot_keys": sorted(changed),
        "removed_shot_keys": sorted(removed),
        "added_shot_keys": sorted(added),
        "invalidated_scene_entity_ids": sorted(invalidated_entities),
        "retired_scene_entity_ids": sorted(retired_entities),
        "invalidated_still_shot_ids": invalidated_still_shot_ids,
        "removed_relation_ids": sorted(removed_relation_ids),
        "graph_revision": revision,
    }, now)
    return revision


def _invalidate_scene_reference_versions(conn, entity_id, description, actor, now):
    latest = conn.execute(
        "SELECT id,version FROM short_drama_graph_versions WHERE entity_id=? "
        "ORDER BY version DESC LIMIT 1", (entity_id,),
    ).fetchone()
    conn.execute(
        "UPDATE short_drama_graph_versions SET status='retired' "
        "WHERE entity_id=? AND status<>'retired'", (entity_id,),
    )
    content = {
        "prompt": description, "negative_prompt": "", "references": [],
        "attributes": {"seeded": True, "semantic_changed": True},
        "valid_from": "", "valid_to": "",
    }
    conn.execute(
        "INSERT INTO short_drama_graph_versions"
        "(id,entity_id,version,parent_id,status,prompt,negative_prompt,references_json,"
        "attributes_json,valid_from,valid_to,content_hash,created_by,created_at) "
        "VALUES (?,?,?,?, 'draft',?,?,?,?,?,?,?, ?,?)",
        (
            str(uuid.uuid4()), entity_id, int(latest["version"] if latest else 0) + 1,
            latest["id"] if latest else None, description, "", _canonical([]),
            _canonical(content["attributes"]), "", "", _hash(content), actor, now,
        ),
    )
    conn.execute(
        "UPDATE short_drama_graph_entities SET current_version_id=NULL,updated_at=? "
        "WHERE id=?", (now, entity_id),
    )


def _seed_entity(conn, project_id, key, asset_type, name, description, actor, now):
    row = conn.execute(
        "SELECT id,status,name,description FROM short_drama_graph_entities "
        "WHERE project_id=? AND asset_key=?",
        (project_id, key),
    ).fetchone()
    if row:
        entity_id = str(row["id"])
        if row["status"] != "retired":
            if (
                asset_type == "scene"
                and (_text(row["name"]) != _text(name)
                     or _text(row["description"]) != _text(description))
            ):
                conn.execute(
                    "UPDATE short_drama_graph_entities SET name=?,description=?,updated_at=? "
                    "WHERE id=?", (name, description, now, entity_id),
                )
                _invalidate_scene_reference_versions(
                    conn, entity_id, description, actor, now,
                )
            return entity_id, False, False
        latest = conn.execute(
            "SELECT id,version FROM short_drama_graph_versions WHERE entity_id=? "
            "ORDER BY version DESC LIMIT 1", (entity_id,),
        ).fetchone()
        conn.execute(
            "UPDATE short_drama_graph_versions SET status='retired' "
            "WHERE entity_id=? AND status<>'retired'", (entity_id,),
        )
        conn.execute(
            "UPDATE short_drama_graph_entities SET asset_type=?,name=?,description=?,"
            "status='active',current_version_id=NULL,updated_at=? WHERE id=?",
            (asset_type, name, description, now, entity_id),
        )
        content = {"prompt": description, "negative_prompt": "", "references": [],
                   "attributes": {"seeded": True, "reactivated": True},
                   "valid_from": "", "valid_to": ""}
        conn.execute(
            "INSERT INTO short_drama_graph_versions"
            "(id,entity_id,version,parent_id,status,prompt,negative_prompt,references_json,"
            "attributes_json,valid_from,valid_to,content_hash,created_by,created_at) "
            "VALUES (?,?,?,?, 'draft',?,?,?,?,?,?,?, ?,?)",
            (str(uuid.uuid4()), entity_id, int(latest["version"] if latest else 0) + 1,
             latest["id"] if latest else None, content["prompt"], content["negative_prompt"],
             _canonical(content["references"]), _canonical(content["attributes"]),
             "", "", _hash(content), actor, now),
        )
        return entity_id, False, True
    entity_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO short_drama_graph_entities"
        "(id,project_id,asset_key,asset_type,name,description,created_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (entity_id, project_id, key, asset_type, name, description, actor, now, now),
    )
    content = {"prompt": description, "negative_prompt": "", "references": [],
               "attributes": {"seeded": True}, "valid_from": "", "valid_to": ""}
    version_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO short_drama_graph_versions"
        "(id,entity_id,version,parent_id,status,prompt,negative_prompt,references_json,"
        "attributes_json,valid_from,valid_to,content_hash,created_by,created_at) "
        "VALUES (?,?,1,NULL,'draft',?,?,?,?,?,?,?, ?,?)",
        (version_id, entity_id, content["prompt"], content["negative_prompt"],
         _canonical(content["references"]), _canonical(content["attributes"]),
         "", "", _hash(content), actor, now),
    )
    return entity_id, True, False


def _current_script(conn, project_id):
    """Read the editable conversation script without legacy production tables."""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not {"short_drama_conversations", "short_drama_script_snapshots"}.issubset(tables):
        return None
    snapshot = conn.execute(
        "SELECT snapshot.script_json FROM short_drama_conversations conversation "
        "JOIN short_drama_script_snapshots snapshot "
        "ON snapshot.id=conversation.current_version_id "
        "WHERE conversation.project_id=?", (project_id,),
    ).fetchone()
    if not snapshot:
        return None
    return _json(snapshot[0], {})


def current_project_dialogue_lines(conn, project_id):
    """Return current conversation dialogue, or None for a legacy-only project."""
    script = _current_script(conn, project_id)
    if script is None:
        return None
    return [
        dict(item) for item in script.get("dialogue_lines") or []
        if isinstance(item, dict)
    ]


def _current_script_shots(conn, project_id):
    """Read the editable script shots without depending on the legacy shot table."""
    script = _current_script(conn, project_id)
    if script is None:
        return None
    shots = []
    for index, item in enumerate(script.get("shots") or []):
        if not isinstance(item, dict):
            continue
        shot_key = _text(item.get("shot_key"), 160) or "shot_%02d" % (index + 1)
        shots.append({
            "id": "script:" + shot_key,
            "shot_key": shot_key,
            "sort_order": int(item.get("sort_order") or index + 1),
            "duration": int(item.get("duration_seconds") or item.get("duration") or 5),
            "scene_description": _text(
                item.get("scene_description") or item.get("scene") or item.get("visual"),
                4000,
            ),
            "camera_description": _text(
                item.get("camera_description") or item.get("camera"), 4000,
            ),
            "image_prompt": _text(
                item.get("image_prompt") or item.get("provider_prompt")
                or item.get("visual"), 8000,
            ),
            "video_prompt": _text(
                item.get("video_prompt") or item.get("provider_prompt")
                or item.get("visual"), 8000,
            ),
            "character_keys_json": _canonical(item.get("character_keys") or []),
            "dialogue_line_ids_json": _canonical(item.get("dialogue_line_ids") or []),
        })
    return shots


def _compatibility_shot_id(project_id, shot_key):
    digest = hashlib.sha256(
        (str(project_id) + "\0" + str(shot_key)).encode("utf-8")
    ).hexdigest()[:32]
    return "conversation-shot:" + digest


def _legacy_shots_by_key(conn, project_id):
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(short_drama_shots)")
    }
    order = "script_version DESC,rowid DESC" if "script_version" in columns else "rowid DESC"
    result = {}
    for row in conn.execute(
        "SELECT * FROM short_drama_shots WHERE project_id=? ORDER BY " + order,
        (project_id,),
    ):
        result.setdefault(str(row["shot_key"]), dict(row))
    return result


def _materialize_script_shots(conn, project_id, script_shots):
    """Create stable FK anchors while keeping the conversation snapshot authoritative."""
    if script_shots is None:
        return None
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(short_drama_shots)")
    }
    project_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(short_drama_projects)")
    }
    script_version = 1
    if "script_version" in project_columns:
        row = conn.execute(
            "SELECT script_version FROM short_drama_projects WHERE id=?", (project_id,),
        ).fetchone()
        script_version = int(row[0] or 1) if row else 1
    projected = []
    legacy_by_key = _legacy_shots_by_key(conn, project_id)
    for item in script_shots:
        shot = dict(item)
        existing = legacy_by_key.get(shot["shot_key"])
        shot_id = str(existing["id"]) if existing else _compatibility_shot_id(
            project_id, shot["shot_key"],
        )
        values = {
            "id": shot_id,
            "project_id": project_id,
            "script_version": script_version,
            "shot_key": shot["shot_key"],
            "sort_order": int(shot.get("sort_order") or 0),
            "duration": 5 if int(shot.get("duration") or 5) <= 7 else 10,
            "scene_description": _text(shot.get("scene_description"), 4000),
            "camera_description": _text(shot.get("camera_description"), 4000),
            "character_keys_json": shot.get("character_keys_json") or "[]",
            "dialogue_line_ids_json": shot.get("dialogue_line_ids_json") or "[]",
            "image_prompt": _text(shot.get("image_prompt"), 8000),
            "video_prompt": _text(shot.get("video_prompt"), 8000),
        }
        writable = [name for name in values if name in columns]
        if existing and shot_id.startswith("conversation-shot:"):
            mutable = [
                name for name in writable
                if name not in {"id", "project_id", "script_version"}
            ]
            conn.execute(
                "UPDATE short_drama_shots SET %s WHERE id=? AND project_id=?" %
                ",".join("%s=?" % name for name in mutable),
                tuple(values[name] for name in mutable) + (shot_id, project_id),
            )
        elif not existing:
            conn.execute(
                "INSERT INTO short_drama_shots(%s) VALUES (%s)" % (
                    ",".join(writable), ",".join("?" for _ in writable),
                ),
                tuple(values[name] for name in writable),
            )
        shot["id"] = shot_id
        projected.append(shot)
    return projected


def current_project_shots(conn, project_id, *, materialize=False):
    """Return only current shots, overlaying authoritative conversation fields."""
    conn.row_factory = sqlite3.Row
    current = _current_script_shots(conn, project_id)
    if current is None:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM short_drama_shots WHERE project_id=? ORDER BY sort_order,id",
            (project_id,),
        )]
    projected = _materialize_script_shots(conn, project_id, current) if materialize else current
    legacy_by_key = _legacy_shots_by_key(conn, project_id)
    result = []
    for item in projected:
        legacy = legacy_by_key.get(item["shot_key"])
        stable_id = str(item["id"] if materialize else (
            legacy["id"] if legacy else _compatibility_shot_id(project_id, item["shot_key"])
        ))
        resolved = dict(legacy or {})
        resolved.update(item)
        resolved["id"] = stable_id
        result.append(resolved)
    return sorted(
        result,
        key=lambda row: (int(row.get("sort_order") or 0), str(row.get("id") or "")),
    )


def resolve_current_shot(conn, project_id, shot_id, *, materialize=False):
    """Resolve a production shot against the current conversation snapshot."""
    requested = str(shot_id)
    for item in current_project_shots(conn, project_id, materialize=materialize):
        if requested not in {str(item["id"]), "script:" + item["shot_key"], item["shot_key"]}:
            continue
        return item
    return None


_CURRENT_SCRIPT_UNSET = object()


def _foundation_shots(conn, project_id, current_script_shots=_CURRENT_SCRIPT_UNSET):
    """Use the current script as authoritative, with legacy rows only as an ID bridge."""
    legacy_shots = [dict(row) for row in conn.execute(
        "SELECT id,shot_key,sort_order,scene_description,character_keys_json "
        "FROM short_drama_shots WHERE project_id=? ORDER BY sort_order,id",
        (project_id,),
    )]
    if current_script_shots is _CURRENT_SCRIPT_UNSET:
        current_script_shots = _current_script_shots(conn, project_id)
    if current_script_shots is None:
        return legacy_shots
    legacy_by_key = {row["shot_key"]: row for row in legacy_shots}
    shots = []
    for script_shot in current_script_shots:
        merged = dict(script_shot)
        existing = legacy_by_key.get(script_shot["shot_key"])
        if existing and str(script_shot.get("id") or "").startswith("script:"):
            merged["id"] = existing["id"]
        shots.append(merged)
    return sorted(shots, key=lambda row: (int(row.get("sort_order") or 0), row["shot_key"]))


def _cleanup_removed_shot_graph(conn, project_id, foundation_shots, now):
    """Remove stale shot bindings and retire their generated foundation scenes."""
    active_ids = {str(row["id"]) for row in foundation_shots}
    active_by_key = {str(row["shot_key"]): str(row["id"]) for row in foundation_shots}
    stale_relation_ids = []
    removed_shots = set()
    for row in conn.execute(
        "SELECT id,source_id,metadata_json FROM short_drama_graph_relations "
        "WHERE project_id=? AND source_scope='shot'", (project_id,),
    ).fetchall():
        metadata = _json(row["metadata_json"], {})
        shot_key = _text(metadata.get("shot_key"), 160)
        source_id = str(row["source_id"])
        expected_id = active_by_key.get(shot_key) if shot_key else None
        if source_id in active_ids and (not expected_id or source_id == expected_id):
            continue
        stale_relation_ids.append(str(row["id"]))
        removed_shots.add(shot_key or source_id)
    if stale_relation_ids:
        conn.executemany(
            "DELETE FROM short_drama_graph_relations WHERE id=?",
            [(relation_id,) for relation_id in stale_relation_ids],
        )

    retired_entities = []
    for row in conn.execute(
        "SELECT id,asset_key FROM short_drama_graph_entities WHERE project_id=? "
        "AND asset_type='scene' AND status='active'", (project_id,),
    ).fetchall():
        asset_key = _text(row["asset_key"], 240)
        if not asset_key.startswith("scene:") or asset_key.startswith("scene-custom:"):
            continue
        if asset_key[len("scene:"):] in active_ids:
            continue
        entity_id = str(row["id"])
        conn.execute(
            "DELETE FROM short_drama_graph_relations WHERE project_id=? AND entity_id=?",
            (project_id, entity_id),
        )
        conn.execute(
            "UPDATE short_drama_graph_versions SET status='retired' "
            "WHERE entity_id=? AND status<>'retired'", (entity_id,),
        )
        conn.execute(
            "UPDATE short_drama_graph_entities SET status='retired',"
            "current_version_id=NULL,updated_at=? WHERE id=?",
            (now, entity_id),
        )
        retired_entities.append(entity_id)
    return {
        "removed_shots": sorted(removed_shots),
        "removed_relation_ids": sorted(stale_relation_ids),
        "retired_entity_ids": sorted(retired_entities),
    }


def sync_foundation(db_factory, owner, actor, project_id, expected_revision=None):
    """Idempotently seed characters, base costumes and one scene per shot."""
    now = int(time.time())
    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner, project_id)
        revision = int(_ensure_state(conn, project_id, now))
        if expected_revision is not None and expected_revision != revision:
            raise AssetGraphError("graph_revision_conflict", "资产图谱已更新，请刷新后重试", 409)
        created = []
        reactivated = []
        relation_changed = False
        current_script_shots = _materialize_script_shots(
            conn, project_id, _current_script_shots(conn, project_id),
        )
        foundation_shots = _foundation_shots(conn, project_id, current_script_shots)
        cleanup = {
            "removed_shots": [], "removed_relation_ids": [], "retired_entity_ids": [],
        }
        if current_script_shots is not None:
            cleanup = _cleanup_removed_shot_graph(conn, project_id, foundation_shots, now)
        character_ids = {}
        for row in conn.execute(
            "SELECT character_key,name,identity_text,appearance_prompt,wardrobe_prompt "
            "FROM short_drama_characters WHERE project_id=? ORDER BY sort_order,id",
            (project_id,),
        ):
            description = "；".join(filter(None, [
                _text(row["identity_text"]), _text(row["appearance_prompt"]),
            ]))
            entity_id, added, was_reactivated = _seed_entity(
                conn, project_id, "character:" + row["character_key"], "character",
                row["name"], description, actor, now,
            )
            character_ids[row["character_key"]] = entity_id
            if added:
                created.append(entity_id)
            if was_reactivated:
                reactivated.append(entity_id)
            wardrobe = _text(row["wardrobe_prompt"])
            if wardrobe:
                costume_id, costume_added, costume_reactivated = _seed_entity(
                    conn, project_id, "costume:%s:base" % row["character_key"],
                    "costume", "%s·基础服装" % row["name"], wardrobe, actor, now,
                )
                if costume_added:
                    created.append(costume_id)
                if costume_reactivated:
                    reactivated.append(costume_id)
                relation_changed = _upsert_relation(
                    conn, project_id, "asset", entity_id, "wears",
                    costume_id, None, {}, actor, now,
                ) or relation_changed
        for shot in foundation_shots:
            scene_id, added, was_reactivated = _seed_entity(
                conn, project_id, "scene:" + shot["id"], "scene",
                "%s·场景" % shot["shot_key"], _text(shot["scene_description"]), actor, now,
            )
            if added:
                created.append(scene_id)
            if was_reactivated:
                reactivated.append(scene_id)
            existing_scene = conn.execute(
                "SELECT entity_id,version_id FROM short_drama_graph_relations "
                "WHERE project_id=? AND source_scope='shot' AND source_id=? "
                "AND relation_type='located_in' LIMIT 1",
                (project_id, shot["id"]),
            ).fetchone()
            binding_disabled = conn.execute(
                "SELECT 1 FROM short_drama_graph_relations "
                "WHERE project_id=? AND source_scope='shot' AND source_id=? "
                "AND relation_type=? LIMIT 1",
                (project_id, shot["id"], _SCENE_BINDING_DISABLED_RELATION),
            ).fetchone()
            if existing_scene or not binding_disabled:
                relation_changed = _upsert_relation(
                    conn, project_id, "shot", shot["id"], "located_in",
                    str(existing_scene["entity_id"]) if existing_scene else scene_id,
                    existing_scene["version_id"] if existing_scene else None, {
                        "shot_key": shot["shot_key"],
                        "sort_order": int(shot.get("sort_order") or 0),
                        "scene_description": _text(shot["scene_description"]),
                    }, actor, now,
                ) or relation_changed
            for character_key in _json(shot["character_keys_json"], []):
                entity_id = character_ids.get(str(character_key))
                if entity_id:
                    relation_changed = _upsert_relation(
                        conn, project_id, "shot", shot["id"], "appears_in",
                        entity_id, None, {}, actor, now,
                    ) or relation_changed
        cleanup_changed = bool(
            cleanup["removed_relation_ids"] or cleanup["retired_entity_ids"]
        )
        if created or reactivated or relation_changed or cleanup_changed:
            revision = _bump(conn, project_id, revision, now)
            _audit(conn, project_id, actor, "sync_foundation", "", {
                "created": created,
                "reactivated": reactivated,
                **cleanup,
            }, now)
        conn.commit()
        return {
            "ok": True,
            "graph_revision": revision,
            "created": len(created),
            "reactivated": len(reactivated),
            "removed_relations": len(cleanup["removed_relation_ids"]),
            "retired_entities": len(cleanup["retired_entity_ids"]),
        }


def _upsert_relation(conn, project_id, scope, source_id, relation_type, entity_id,
                     version_id, metadata, actor, now):
    metadata_json = _canonical(metadata or {})
    existing = conn.execute(
        "SELECT version_id,metadata_json FROM short_drama_graph_relations "
        "WHERE project_id=? AND source_scope=? AND source_id=? "
        "AND relation_type=? AND entity_id=?",
        (project_id, scope, source_id, relation_type, entity_id),
    ).fetchone()
    if existing and existing["version_id"] == version_id and existing["metadata_json"] == metadata_json:
        return False
    relation_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO short_drama_graph_relations"
        "(id,project_id,source_scope,source_id,relation_type,entity_id,version_id,"
        "metadata_json,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(project_id,source_scope,source_id,relation_type,entity_id) "
        "DO UPDATE SET version_id=excluded.version_id,metadata_json=excluded.metadata_json,"
        "updated_at=excluded.updated_at",
        (relation_id, project_id, scope, source_id, relation_type, entity_id,
         version_id, metadata_json, actor, now, now),
    )
    return True


def workspace(db_factory, owner, project_id):
    with closing(_connection(db_factory)) as conn:
        _project(conn, owner, project_id)
        state = conn.execute(
            "SELECT revision FROM short_drama_graph_state WHERE project_id=?",
            (project_id,),
        ).fetchone()
        revision = int(state[0]) if state else 1
        entities = []
        for entity in conn.execute(
            "SELECT * FROM short_drama_graph_entities WHERE project_id=? "
            "ORDER BY asset_type,name,id", (project_id,),
        ):
            item = dict(entity)
            item["versions"] = [
                {**dict(row), "references": _json(row["references_json"], []),
                 "attributes": _json(row["attributes_json"], {})}
                for row in conn.execute(
                    "SELECT * FROM short_drama_graph_versions WHERE entity_id=? "
                    "ORDER BY version DESC", (entity["id"],),
                )
            ]
            entities.append(item)
        relations = [
            {**dict(row), "metadata": _json(row["metadata_json"], {})}
            for row in conn.execute(
                "SELECT * FROM short_drama_graph_relations WHERE project_id=? "
                "AND relation_type<>? ORDER BY source_scope,source_id,relation_type",
                (project_id, _SCENE_BINDING_DISABLED_RELATION),
            )
        ]
        return {"project_id": project_id, "graph_revision": revision,
                "entities": entities, "relations": relations,
                "asset_types": sorted(ASSET_TYPES)}


def _scene_group_key(value):
    normalized = re.sub(r"\s+", " ", _text(value, 4000)).strip().lower()
    return "scene-group:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _scene_key(row):
    asset_key = _text(row.get("asset_key"), 80)
    if asset_key.startswith("scene-custom:"):
        return asset_key
    return _scene_group_key(row.get("scene_description"))


def _scene_rows(conn, project_id):
    rows = conn.execute(
        "SELECT entity.*,relation.source_id AS relation_source_id,relation.metadata_json,"
        "shot.id AS shot_id,shot.shot_key,shot.sort_order,shot.scene_description "
        "FROM short_drama_graph_entities entity "
        "LEFT JOIN short_drama_graph_relations relation ON relation.entity_id=entity.id "
        "AND relation.project_id=entity.project_id AND relation.source_scope='shot' "
        "AND relation.relation_type='located_in' "
        "LEFT JOIN short_drama_shots shot ON shot.id=relation.source_id "
        "AND shot.project_id=entity.project_id WHERE entity.project_id=? "
        "AND entity.asset_type='scene' AND entity.status='active' "
        "AND (relation.id IS NOT NULL OR entity.asset_key LIKE 'scene-custom:%') "
        "ORDER BY COALESCE(shot.sort_order,999999),relation.source_id",
        (project_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        metadata = _json(item.pop("metadata_json", "{}"), {})
        item["shot_id"] = item.get("shot_id") or item.get("relation_source_id")
        item["shot_key"] = _text(metadata.get("shot_key"), 160) or item.get("shot_key")
        item["sort_order"] = int(metadata.get("sort_order") or item.get("sort_order") or 0)
        custom_scene = _text(item.get("asset_key"), 80).startswith("scene-custom:")
        item["custom_scene"] = custom_scene
        item["scene_description"] = (
            (item.get("description") or item.get("name")) if custom_scene else
            (_text(metadata.get("scene_description"), 4000) or item.get("scene_description")
             or item.get("description") or item.get("name"))
        )
        result.append(item)
    return result


def _scene_version(conn, entity_id):
    return conn.execute(
        "SELECT * FROM short_drama_graph_versions WHERE entity_id=? "
        "ORDER BY version DESC LIMIT 1", (entity_id,),
    ).fetchone()


def _scene_reference_identity(version):
    """Return the immutable identity shared by one grouped-scene update."""
    if not version:
        return ""
    attributes = _json(version["attributes_json"], {})
    return (
        _text(attributes.get("scene_operation_id"), 160)
        or _text(version["content_hash"], 160)
    )


def _scene_upload_prefix(owner, project_id):
    owner_key = hashlib.sha256(_text(owner, 160).encode("utf-8")).hexdigest()[:16]
    project_key = hashlib.sha256(_text(project_id, 160).encode("utf-8")).hexdigest()[:16]
    return "short_drama_scene_uploads/%s/%s/" % (owner_key, project_key)


def _scene_result_file(value):
    try:
        parsed = urllib.parse.urlsplit(_text(value, 1000))
    except ValueError:
        return ""
    if (
        parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
        or parsed.path.startswith("/")
    ):
        return ""
    relative = urllib.parse.unquote(parsed.path)
    if (
        not relative or "\\" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        return ""
    return relative


def _scene_result_url_file(value):
    try:
        parsed = urllib.parse.urlsplit(_text(value, 2000))
    except ValueError:
        return ""
    prefix = "/api/gen/file/"
    if (
        parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
        or not parsed.path.startswith(prefix)
    ):
        return ""
    return _scene_result_file(parsed.path[len(prefix):])


def _scene_result_url_identity(value):
    """Return one canonical, validation-safe identity for a result URL."""
    value = _text(value, 2000)
    local_file = _scene_result_url_file(value)
    if local_file:
        return "local:" + local_file
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc or not hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.fragment
    ):
        return ""
    return "remote:" + urllib.parse.urlunsplit((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        urllib.parse.unquote(parsed.path),
        parsed.query,
        "",
    ))


def _scene_asset_job_matches(conn, actor, reference):
    try:
        job_id = int(reference.get("asset_job_id"))
    except (TypeError, ValueError):
        return False
    job = conn.execute(
        "SELECT result FROM jobs WHERE id=? AND username=? "
        "AND kind='image' AND status='done'",
        (job_id, actor),
    ).fetchone()
    if not job:
        return False
    result = _json(job[0], {})
    urls = result.get("urls") if isinstance(result.get("urls"), list) else []
    files = result.get("files") if isinstance(result.get("files"), list) else []
    if not urls and result.get("url"):
        urls = [result.get("url")]
    if not files and result.get("file"):
        files = [result.get("file")]
    requested_file = _text(reference.get("file"), 1000)
    requested_url = _text(reference.get("url"), 2000)
    normalized_urls = [_text(value, 2000) for value in urls]
    url_identities = [_scene_result_url_identity(value) for value in normalized_urls]
    if (
        any(not value for value in url_identities)
        or len(set(url_identities)) != len(url_identities)
        or normalized_urls.count(requested_url) != 1
    ):
        return False
    index = normalized_urls.index(requested_url)
    if files:
        indexed_file = _scene_result_file(files[index]) if index < len(files) else ""
        requested_file = _scene_result_file(requested_file)
        local_url_file = _scene_result_url_file(requested_url)
        return (
            bool(indexed_file)
            and requested_file == indexed_file
            and (
                not requested_url.startswith("/api/gen/file/")
                or local_url_file == indexed_file
            )
        )
    local_url_file = _scene_result_url_file(requested_url)
    return bool(local_url_file) and local_url_file == _scene_result_file(requested_file)


def _trusted_scene_reference(conn, owner, project_id, scene_key, version, reference):
    if not version or not isinstance(reference, dict):
        return False
    attributes = _json(version["attributes_json"], {})
    operation_id = _text(attributes.get("scene_operation_id"), 160)
    source = _text(attributes.get("source"), 40).lower()
    reference_owner = _text(attributes.get("scene_reference_owner"), 160)
    reference_actor = _text(attributes.get("scene_reference_actor"), 160)
    reference_project = _text(attributes.get("scene_reference_project_id"), 160)
    if (
        not operation_id
        or source not in {"upload", "asset"}
        or reference_owner != _text(owner, 160)
        or reference_project != _text(project_id, 160)
        or not reference_actor
    ):
        return False
    audits = conn.execute(
        "SELECT actor,details_json FROM short_drama_graph_audit "
        "WHERE project_id=? AND action='set_scene_reference' AND target_id=? "
        "ORDER BY created_at DESC,id DESC",
        (project_id, scene_key),
    ).fetchall()
    trusted_audit = next((
        row for row in audits
        if _text(row["actor"], 160) == reference_actor
        and _text(_json(row["details_json"], {}).get("operation_id"), 160)
        == operation_id
        and _text(_json(row["details_json"], {}).get("source"), 40).lower()
        == source
    ), None)
    if not trusted_audit:
        return False
    relative = _text(reference.get("file"), 1000).replace("\\", "/").lstrip("/")
    if not relative or ".." in relative.split("/"):
        return False
    if source == "upload":
        return (
            relative.startswith(_scene_upload_prefix(owner, project_id))
            and _text(reference.get("url"), 2000)
            == "/api/gen/file/" + relative
        )
    return _scene_asset_job_matches(conn, reference_actor, reference)


def scene_upload_file_access(conn, username, relative, access=None):
    """Authorize one controlled scene upload through its trusted graph evidence."""
    conn.row_factory = sqlite3.Row
    relative = _text(relative, 1000).replace("\\", "/").lstrip("/")
    access = access if isinstance(access, dict) else {}
    if not relative or ".." in relative.split("/"):
        return False
    candidates = conn.execute(
        "SELECT version.*,entity.project_id AS access_project_id,"
        "project.username AS access_owner,project.board_id AS access_board_id "
        "FROM short_drama_graph_versions version "
        "JOIN short_drama_graph_entities entity ON entity.id=version.entity_id "
        "JOIN short_drama_projects project ON project.id=entity.project_id "
        "WHERE project.deleted=0 AND entity.asset_type='scene' AND entity.status='active' "
        "AND version.references_json LIKE ?",
        ("%" + relative + "%",),
    ).fetchall()
    for version in candidates:
        references = _json(version["references_json"], [])
        reference = next((
            item for item in references
            if isinstance(item, dict)
            and _text(item.get("file"), 1000).replace("\\", "/").lstrip("/")
            == relative
            and _text(item.get("url"), 2000) == "/api/gen/file/" + relative
        ), None) if isinstance(references, list) else None
        if not reference:
            continue
        project_id = _text(version["access_project_id"], 160)
        owner = _text(version["access_owner"], 160)
        targets = conn.execute(
            "SELECT DISTINCT target_id FROM short_drama_graph_audit "
            "WHERE project_id=? AND action='set_scene_reference'",
            (project_id,),
        ).fetchall()
        if not any(
            _trusted_scene_reference(
                conn, owner, project_id, _text(target[0], 160), version, reference,
            )
            for target in targets
        ):
            continue
        board_id = _text(version["access_board_id"], 160)
        if owner == _text(username, 160) and not board_id:
            return True
        if (
            board_id
            and _text(access.get("board_id"), 160) == board_id
            and _text(access.get("role"), 40).lower()
            in {"owner", "editor", "viewer"}
        ):
            return True
    return False


def scene_workspace(db_factory, owner, project_id):
    """Return user-facing scene groups without exposing graph internals."""
    with closing(_connection(db_factory)) as conn:
        _project(conn, owner, project_id)
        state = conn.execute(
            "SELECT revision FROM short_drama_graph_state WHERE project_id=?",
            (project_id,),
        ).fetchone()
        groups = {}
        for row in _scene_rows(conn, project_id):
            group_key = _scene_key(row)
            group = groups.setdefault(group_key, {
                "scene_key": group_key,
                "name": _text(row["scene_description"], 200) or "未命名场景",
                "description": _text(row["scene_description"]),
                "shots": [], "entity_ids": [], "locked": True,
                "preview": None, "custom": bool(row.get("custom_scene")),
            })
            if row.get("custom_scene"):
                group["name"] = _text(row.get("name"), 200) or "未命名场景"
            if row.get("shot_id") or row.get("shot_key"):
                group["shots"].append({
                    "id": row["shot_id"], "shot_key": row["shot_key"],
                    "sort_order": int(row["sort_order"] or 0),
                })
            if row["id"] in group["entity_ids"]:
                continue
            group["entity_ids"].append(row["id"])
            version = _scene_version(conn, row["id"])
            if not version:
                group["locked"] = False
                continue
            references = _json(version["references_json"], [])
            reference = references[0] if references and isinstance(references[0], dict) else {}
            if reference and not _trusted_scene_reference(
                conn, owner, project_id, group_key, version, reference,
            ):
                group["locked"] = False
                continue
            attributes = _json(version["attributes_json"], {})
            candidate = {
                "version_id": version["id"], "version": int(version["version"]),
                "reference_identity": _scene_reference_identity(version),
                "status": version["status"], "prompt": version["prompt"],
                "source": _text(attributes.get("source"), 40),
                "reference_source": (
                    _text(attributes.get("reference_source"), 40)
                    or _text(attributes.get("source"), 40)
                ),
                "file": _text(reference.get("file"), 1000),
                "url": _text(reference.get("url"), 2000),
                "name": _text(reference.get("name"), 240),
            }
            if group["preview"] is None or candidate["version"] > group["preview"]["version"]:
                group["preview"] = candidate
            if version["status"] != "locked" or not (candidate["file"] or candidate["url"]):
                group["locked"] = False
        items = list(groups.values())
        for item in items:
            item["locked"] = bool(item["locked"] and item["preview"])
            if item["preview"] and item["preview"]["status"] != "locked":
                item["locked"] = False
        deleted_scenes = [dict(row) for row in conn.execute(
            "SELECT asset_key AS scene_key,name,description,updated_at AS deleted_at "
            "FROM short_drama_graph_entities WHERE project_id=? AND asset_type='scene' "
            "AND status='retired' AND asset_key LIKE 'scene-custom:%' "
            "ORDER BY updated_at DESC,id DESC LIMIT 20",
            (project_id,),
        ).fetchall()]
        return {
            "project_id": project_id,
            "graph_revision": int(state[0]) if state else 1,
            "scenes": items,
            "deleted_scenes": deleted_scenes,
        }


def _replace_scene_shot_bindings(conn, project_id, entity_id, shot_keys, actor, now):
    requested = []
    for value in shot_keys or []:
        key = _text(value, 160)
        if key and key not in requested:
            requested.append(key)
    shots = _foundation_shots(conn, project_id)
    by_key = {row["shot_key"]: row for row in shots}
    if any(key not in by_key for key in requested):
        raise AssetGraphError("scene_shots_invalid", "部分镜头不存在，请刷新后重试", 422)
    requested_ids = {by_key[key]["id"] for key in requested}
    for row in conn.execute(
        "SELECT source_id FROM short_drama_graph_relations WHERE project_id=? "
        "AND source_scope='shot' AND relation_type='located_in' AND entity_id=?",
        (project_id, entity_id),
    ).fetchall():
        if row[0] not in requested_ids:
            conn.execute(
                "DELETE FROM short_drama_graph_relations WHERE project_id=? "
                "AND source_scope='shot' AND source_id=? AND relation_type='located_in' "
                "AND entity_id=?", (project_id, row[0], entity_id),
            )
    for key in requested:
        shot = by_key[key]
        conn.execute(
            "DELETE FROM short_drama_graph_relations WHERE project_id=? "
            "AND source_scope='shot' AND source_id=? AND relation_type=?",
            (project_id, shot["id"], _SCENE_BINDING_DISABLED_RELATION),
        )
        conn.execute(
            "DELETE FROM short_drama_graph_relations WHERE project_id=? "
            "AND source_scope='shot' AND source_id=? AND relation_type='located_in' "
            "AND entity_id<>?", (project_id, shot["id"], entity_id),
        )
        _upsert_relation(
            conn, project_id, "shot", shot["id"], "located_in", entity_id, None,
            {"shot_key": key, "sort_order": int(shot["sort_order"] or 0),
             "scene_description": _text(shot["scene_description"], 4000)},
            actor, now,
        )


def create_scene(db_factory, owner, actor, body):
    required = {"project_id", "graph_revision", "name", "description", "shot_keys"}
    if (not isinstance(body, dict) or not required.issubset(body)
            or type(body.get("graph_revision")) is not int
            or not isinstance(body.get("shot_keys"), list)):
        raise AssetGraphError("scene_create_invalid", "新增场景参数不完整", 422)
    project_id = _text(body["project_id"], 160)
    name = _text(body["name"], 120).strip()
    description = _text(body["description"], 4000).strip()
    if not name or len(description) < 2:
        raise AssetGraphError("scene_create_invalid", "请填写场景名称和具体描述", 422)
    now = int(time.time())
    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner, project_id)
        revision = int(_ensure_state(conn, project_id, now))
        if revision != body["graph_revision"]:
            raise AssetGraphError("graph_revision_conflict", "场景已更新，请刷新后重试", 409)
        entity_id, _, _ = _seed_entity(
            conn, project_id, "scene-custom:" + uuid.uuid4().hex,
            "scene", name, description, actor, now,
        )
        _replace_scene_shot_bindings(conn, project_id, entity_id,
                                     body.get("shot_keys"), actor, now)
        revision = _bump(conn, project_id, revision, now)
        _audit(conn, project_id, actor, "create_scene", entity_id,
               {"name": name, "shot_keys": body.get("shot_keys")}, now)
        conn.commit()
    return scene_workspace(db_factory, owner, project_id)


def update_scene(db_factory, owner, actor, body):
    required = {"project_id", "graph_revision", "scene_key", "name", "description", "shot_keys"}
    if (not isinstance(body, dict) or not required.issubset(body)
            or type(body.get("graph_revision")) is not int
            or not isinstance(body.get("shot_keys"), list)):
        raise AssetGraphError("scene_update_invalid", "场景修改参数不完整", 422)
    project_id = _text(body["project_id"], 160)
    scene_key = _text(body["scene_key"], 80)
    name = _text(body["name"], 120).strip()
    description = _text(body["description"], 4000).strip()
    if not name or len(description) < 2:
        raise AssetGraphError("scene_update_invalid", "请填写场景名称和具体描述", 422)
    now = int(time.time())
    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner, project_id)
        revision = int(_ensure_state(conn, project_id, now))
        if revision != body["graph_revision"]:
            raise AssetGraphError("graph_revision_conflict", "场景已更新，请刷新后重试", 409)
        row = conn.execute(
            "SELECT id,asset_key,name,description FROM short_drama_graph_entities WHERE project_id=? "
            "AND asset_key=? AND asset_type='scene' AND status='active'",
            (project_id, scene_key),
        ).fetchone()
        if not row or not _text(row["asset_key"]).startswith("scene-custom:"):
            raise AssetGraphError("scene_not_editable", "系统整理的主场景不可改名，请新增场景", 422)
        semantic_changed = (
            _text(row["name"]) != name or _text(row["description"]) != description
        )
        conn.execute(
            "UPDATE short_drama_graph_entities SET name=?,description=?,updated_at=? WHERE id=?",
            (name, description, now, row["id"]),
        )
        if semantic_changed:
            _invalidate_scene_reference_versions(
                conn, row["id"], description, actor, now,
            )
        _replace_scene_shot_bindings(conn, project_id, row["id"],
                                     body.get("shot_keys"), actor, now)
        revision = _bump(conn, project_id, revision, now)
        _audit(conn, project_id, actor, "update_scene", row["id"],
               {"name": name, "shot_keys": body.get("shot_keys"),
                "reference_invalidated": semantic_changed}, now)
        conn.commit()
    return scene_workspace(db_factory, owner, project_id)


def bind_scene_to_shot(db_factory, owner, actor, body):
    """Bind one storyboard shot to an existing scene, or explicitly unbind it."""
    required = {"project_id", "graph_revision", "shot_key", "scene_key"}
    if (not isinstance(body, dict) or set(body) != required
            or type(body.get("graph_revision")) is not int):
        raise AssetGraphError("scene_binding_invalid", "镜头场景绑定参数不完整", 422)
    project_id = _text(body["project_id"], 160)
    shot_key = _text(body["shot_key"], 160)
    scene_key = _text(body["scene_key"], 160)
    now = int(time.time())
    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner, project_id)
        revision = int(_ensure_state(conn, project_id, now))
        if revision != body["graph_revision"]:
            raise AssetGraphError("graph_revision_conflict", "场景已更新，请刷新后重试", 409)
        shot = next(
            (item for item in _foundation_shots(conn, project_id)
             if item["shot_key"] == shot_key),
            None,
        )
        if not shot:
            raise AssetGraphError("shot_not_found", "镜头不存在，请刷新后重试", 404)
        scene = None
        if scene_key:
            scene = next(
                (row for row in _scene_rows(conn, project_id)
                 if _scene_key(row) == scene_key),
                None,
            )
            if not scene:
                raise AssetGraphError("scene_not_found", "所选场景不存在，请刷新后重试", 404)
        conn.execute(
            "DELETE FROM short_drama_graph_relations WHERE project_id=? "
            "AND source_scope='shot' AND source_id=? "
            "AND relation_type IN ('located_in',?)",
            (project_id, shot["id"], _SCENE_BINDING_DISABLED_RELATION),
        )
        if scene_key:
            _upsert_relation(
                conn, project_id, "shot", shot["id"], "located_in", scene["id"],
                None, {"shot_key": shot_key,
                       "sort_order": int(shot["sort_order"] or 0),
                       "scene_description": _text(shot["scene_description"], 4000)},
                actor, now,
            )
        else:
            foundation = conn.execute(
                "SELECT id FROM short_drama_graph_entities WHERE project_id=? "
                "AND asset_key=? AND asset_type='scene' AND status='active'",
                (project_id, "scene:" + str(shot["id"])),
            ).fetchone()
            if not foundation:
                raise AssetGraphError(
                    "scene_foundation_missing", "镜头基础场景尚未同步，请刷新后重试", 409,
                )
            _upsert_relation(
                conn, project_id, "shot", shot["id"],
                _SCENE_BINDING_DISABLED_RELATION, str(foundation["id"]), None,
                {"shot_key": shot_key}, actor, now,
            )
        revision = _bump(conn, project_id, revision, now)
        _audit(conn, project_id, actor, "bind_scene_to_shot", shot["id"],
               {"shot_key": shot_key, "scene_key": scene_key}, now)
        conn.commit()
    return scene_workspace(db_factory, owner, project_id)


def delete_scene(db_factory, owner, actor, body):
    required = {"project_id", "graph_revision", "scene_key"}
    if (not isinstance(body, dict) or set(body) != required
            or type(body.get("graph_revision")) is not int):
        raise AssetGraphError("scene_delete_invalid", "删除场景参数不完整", 422)
    project_id = _text(body["project_id"], 160)
    scene_key = _text(body["scene_key"], 80)
    now = int(time.time())
    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner, project_id)
        revision = int(_ensure_state(conn, project_id, now))
        if revision != body["graph_revision"]:
            raise AssetGraphError("graph_revision_conflict", "场景已更新，请刷新后重试", 409)
        row = conn.execute(
            "SELECT id,asset_key FROM short_drama_graph_entities WHERE project_id=? "
            "AND asset_key=? AND asset_type='scene' AND status='active'",
            (project_id, scene_key),
        ).fetchone()
        if not row or not _text(row["asset_key"]).startswith("scene-custom:"):
            raise AssetGraphError("scene_not_deletable", "系统整理的主场景不能删除", 422)
        shot_count = int(conn.execute(
            "SELECT COUNT(*) FROM short_drama_graph_relations WHERE project_id=? "
            "AND entity_id=? AND source_scope='shot' AND relation_type='located_in'",
            (project_id, row["id"]),
        ).fetchone()[0])
        if shot_count:
            raise AssetGraphError("scene_in_use", "该场景仍绑定镜头，请先取消镜头关联", 409)
        conn.execute(
            "UPDATE short_drama_graph_entities SET status='retired',updated_at=? WHERE id=?",
            (now, row["id"]),
        )
        revision = _bump(conn, project_id, revision, now)
        _audit(conn, project_id, actor, "delete_scene", row["id"], {}, now)
        conn.commit()
    return scene_workspace(db_factory, owner, project_id)


def restore_scene(db_factory, owner, actor, body):
    required = {"project_id", "graph_revision", "scene_key"}
    if (not isinstance(body, dict) or set(body) != required
            or type(body.get("graph_revision")) is not int):
        raise AssetGraphError("scene_restore_invalid", "恢复场景参数不完整", 422)
    project_id = _text(body["project_id"], 160)
    scene_key = _text(body["scene_key"], 80)
    now = int(time.time())
    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner, project_id)
        revision = int(_ensure_state(conn, project_id, now))
        if revision != body["graph_revision"]:
            raise AssetGraphError("graph_revision_conflict", "场景已更新，请刷新后重试", 409)
        row = conn.execute(
            "SELECT id,asset_key FROM short_drama_graph_entities WHERE project_id=? "
            "AND asset_key=? AND asset_type='scene' AND status='retired'",
            (project_id, scene_key),
        ).fetchone()
        if not row or not _text(row["asset_key"]).startswith("scene-custom:"):
            raise AssetGraphError("scene_not_restorable", "没有找到可恢复的场景", 404)
        conn.execute(
            "UPDATE short_drama_graph_entities SET status='active',updated_at=? WHERE id=?",
            (now, row["id"]),
        )
        revision = _bump(conn, project_id, revision, now)
        _audit(conn, project_id, actor, "restore_scene", row["id"], {}, now)
        conn.commit()
    return scene_workspace(db_factory, owner, project_id)


def _resolve_scene_reference(conn, owner, actor, project_id, body):
    from . import cli_uploads, image as image_domain

    source = _text(body.get("source"), 40).lower()
    created_path = None
    if source == "upload":
        match = re.fullmatch(
            r"data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=\r\n]+)",
            str(body.get("image_data") or ""),
        )
        if not match:
            raise AssetGraphError("scene_image_invalid", "请上传 JPG、PNG 或 WebP 场景图", 422)
        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except Exception as error:
            raise AssetGraphError("scene_image_invalid", "上传的场景图内容无效", 422) from error
        if not raw or len(raw) > cli_uploads.MAX_BYTES:
            raise AssetGraphError("scene_image_invalid", "场景图大小必须在 10MB 以内", 422)
        mime = cli_uploads.detect_mime(raw[:16])
        if mime not in cli_uploads.MIME_EXTENSIONS:
            raise AssetGraphError("scene_image_invalid", "场景图不是有效的 JPG、PNG 或 WebP 图片", 422)
        relative = _scene_upload_prefix(owner, project_id) + "scene_%s%s" % (
            uuid.uuid4().hex, cli_uploads.MIME_EXTENSIONS[mime],
        )
        created_path = (image_domain.OUT_DIR / relative).resolve()
        created_path.parent.mkdir(parents=True, exist_ok=True)
        created_path.write_bytes(raw)
        return ({"file": relative, "url": "/api/gen/file/" + relative,
                 "name": _text(body.get("filename"), 240) or "本地场景图"}, created_path)
    if source == "asset":
        try:
            job_id = int(body.get("asset_job_id"))
        except (TypeError, ValueError) as error:
            raise AssetGraphError("scene_asset_invalid", "请选择有效的场景图片资产", 422) from error
        job = conn.execute(
            "SELECT result FROM jobs WHERE id=? AND username=? AND kind='image' AND status='done'",
            (job_id, actor),
        ).fetchone()
        if not job:
            raise AssetGraphError("scene_asset_invalid", "场景图片资产不存在或不属于当前用户", 422)
        result = _json(job[0], {})
        urls = result.get("urls") if isinstance(result.get("urls"), list) else []
        files = result.get("files") if isinstance(result.get("files"), list) else []
        if not urls and result.get("url"):
            urls = [result.get("url")]
        if not files and result.get("file"):
            files = [result.get("file")]
        urls = [_text(value, 2000) for value in urls]
        url_identities = [_scene_result_url_identity(value) for value in urls]
        if (
            not urls
            or any(not value for value in url_identities)
            or len(set(url_identities)) != len(url_identities)
        ):
            raise AssetGraphError(
                "scene_asset_invalid",
                "场景图片资产结果无法唯一匹配",
                422,
            )
        requested_url = _text(body.get("asset_url"), 2000)
        if requested_url:
            if requested_url not in urls:
                raise AssetGraphError(
                    "scene_asset_invalid",
                    "选择的图片与场景资产记录不匹配",
                    422,
                )
            index = urls.index(requested_url)
        else:
            index = 0
        url = _text(urls[index] if index < len(urls) else "", 2000)
        if files and index >= len(files):
            raise AssetGraphError(
                "scene_asset_invalid",
                "选择的图片缺少同一结果位置的本地文件",
                422,
            )
        file_value = files[index] if files else ""
        normalized_file = _scene_result_file(file_value)
        local_url_file = _scene_result_url_file(url)
        if files and (
            not normalized_file
            or (
                url.startswith("/api/gen/file/")
                and local_url_file != normalized_file
            )
        ):
            raise AssetGraphError(
                "scene_asset_invalid",
                "选择的图片 URL 与本地文件不属于同一结果",
                422,
            )
        file_name = image_domain._trusted_short_drama_file(normalized_file)
        if not files:
            file_name = image_domain._trusted_short_drama_file(url, file_url=True)
        if not file_name or not url:
            raise AssetGraphError("scene_asset_invalid", "该图片资产无法用作场景图", 422)
        return ({"file": file_name, "url": url,
                 "name": _text(body.get("filename"), 240) or "生成的场景图",
                 "asset_job_id": job_id}, None)
    raise AssetGraphError("scene_source_invalid", "场景图来源无效", 422)


def set_scene_reference(db_factory, owner, actor, body):
    required = {"project_id", "graph_revision", "scene_key", "source"}
    if not isinstance(body, dict) or not required.issubset(body) or type(body["graph_revision"]) is not int:
        raise AssetGraphError("scene_reference_invalid", "场景图参数不完整", 422)
    project_id = _text(body["project_id"], 160)
    scene_key = _text(body["scene_key"], 80)
    created_path = None
    committed = False
    try:
        with closing(_connection(db_factory)) as lookup:
            _project(lookup, owner, project_id)
            reference, created_path = _resolve_scene_reference(
                lookup, owner, actor, project_id, body,
            )
        now = int(time.time())
        with closing(_connection(db_factory)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _project(conn, owner, project_id)
            revision = int(_ensure_state(conn, project_id, now))
            if revision != body["graph_revision"]:
                raise AssetGraphError("graph_revision_conflict", "场景已在其他页面更新，请刷新后重试", 409)
            rows = [row for row in _scene_rows(conn, project_id)
                    if _scene_key(row) == scene_key]
            if not rows:
                raise AssetGraphError("scene_not_found", "场景不存在", 404)
            operation_id = str(uuid.uuid4())
            source = _text(body.get("source"), 40).lower()
            reference_source = (
                _text(body.get("reference_source"), 40).lower() or source
            )
            prompt = _text(body.get("prompt") or rows[0]["scene_description"], 8000)
            entity_rows = {row["id"]: row for row in rows}.values()
            for row in entity_rows:
                number = int(conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_graph_versions WHERE entity_id=?",
                    (row["id"],),
                ).fetchone()[0])
                current = conn.execute(
                    "SELECT current_version_id FROM short_drama_graph_entities WHERE id=?",
                    (row["id"],),
                ).fetchone()[0]
                content = {"prompt": prompt, "negative_prompt": "人物、文字、Logo、水印",
                           "references": [reference],
                           "attributes": {
                               "source": source,
                               "reference_source": reference_source,
                               "scene_operation_id": operation_id,
                               "scene_reference_owner": owner,
                               "scene_reference_actor": actor,
                               "scene_reference_project_id": project_id,
                           },
                           "valid_from": "", "valid_to": ""}
                conn.execute(
                    "INSERT INTO short_drama_graph_versions"
                    "(id,entity_id,version,parent_id,status,prompt,negative_prompt,references_json,"
                    "attributes_json,valid_from,valid_to,content_hash,created_by,created_at) "
                    "VALUES (?,?,?,?, 'draft',?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), row["id"], number, current, prompt,
                     content["negative_prompt"], _canonical([reference]),
                     _canonical(content["attributes"]), "", "", _hash(content), actor, now),
                )
            revision = _bump(conn, project_id, revision, now)
            _audit(conn, project_id, actor, "set_scene_reference", scene_key,
                   {"source": source, "shot_count": len(rows),
                    "reference_source": reference_source,
                    "operation_id": operation_id}, now)
            conn.commit()
            committed = True
        return scene_workspace(db_factory, owner, project_id)
    except Exception:
        if created_path is not None and not committed:
            try:
                created_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def lock_scene_reference(db_factory, owner, actor, body):
    required = {"project_id", "graph_revision", "scene_key"}
    if not isinstance(body, dict) or set(body) != required or type(body["graph_revision"]) is not int:
        raise AssetGraphError("scene_lock_invalid", "场景锁定参数不完整", 422)
    project_id = _text(body["project_id"], 160)
    scene_key = _text(body["scene_key"], 80)
    now = int(time.time())
    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner, project_id)
        revision = int(_ensure_state(conn, project_id, now))
        if revision != body["graph_revision"]:
            raise AssetGraphError("graph_revision_conflict", "场景已在其他页面更新，请刷新后重试", 409)
        rows = [row for row in _scene_rows(conn, project_id)
                if _scene_key(row) == scene_key]
        if not rows:
            raise AssetGraphError("scene_not_found", "场景不存在", 404)
        locked_ids = []
        entity_rows = {row["id"]: row for row in rows}.values()
        for row in entity_rows:
            version = conn.execute(
                "SELECT * FROM short_drama_graph_versions WHERE entity_id=? AND status='draft' "
                "ORDER BY version DESC LIMIT 1", (row["id"],),
            ).fetchone()
            references = _json(version["references_json"], []) if version else []
            if not version or not references:
                raise AssetGraphError("scene_reference_missing", "请先上传或生成场景图", 422)
            reference = references[0] if isinstance(references[0], dict) else {}
            if not _trusted_scene_reference(
                conn, owner, project_id, scene_key, version, reference,
            ):
                raise AssetGraphError(
                    "scene_reference_untrusted",
                    "场景参考图缺少可信来源或不属于当前项目",
                    422,
                )
            conn.execute(
                "UPDATE short_drama_graph_versions SET status='retired' "
                "WHERE entity_id=? AND status='locked'", (row["id"],),
            )
            conn.execute(
                "UPDATE short_drama_graph_versions SET status='locked',locked_at=? WHERE id=?",
                (now, version["id"]),
            )
            conn.execute(
                "UPDATE short_drama_graph_entities SET current_version_id=?,updated_at=? WHERE id=?",
                (version["id"], now, row["id"]),
            )
            locked_ids.append(version["id"])
        revision = _bump(conn, project_id, revision, now)
        _audit(conn, project_id, actor, "lock_scene_reference", scene_key,
               {"versions": locked_ids, "shot_count": len(rows)}, now)
        conn.commit()
    return scene_workspace(db_factory, owner, project_id)


def locked_scene_reference(conn, project_id, shot_key, scene_key=None):
    """Return the locked scene image bound to a storyboard shot, if configured."""
    conn.row_factory = sqlite3.Row
    rows = _scene_rows(conn, project_id)
    requested_scene_key = _text(scene_key, 160)
    if requested_scene_key:
        scene = next((row for row in rows if _scene_key(row) == requested_scene_key), None)
    else:
        scene = next((row for row in rows if row.get("shot_key") == shot_key), None)
    if not scene or not scene.get("current_version_id"):
        return None
    version = conn.execute(
        "SELECT id,version,prompt,references_json,attributes_json,content_hash,status "
        "FROM short_drama_graph_versions WHERE id=?",
        (scene["current_version_id"],),
    ).fetchone()
    if not version or version["status"] != "locked":
        return None
    references = _json(version["references_json"], [])
    reference = references[0] if references and isinstance(references[0], dict) else {}
    if not (_text(reference.get("file"), 1000) or _text(reference.get("url"), 2000)):
        return None
    project = conn.execute(
        "SELECT username FROM short_drama_projects WHERE id=? AND deleted=0",
        (project_id,),
    ).fetchone()
    if not project or not _trusted_scene_reference(
        conn, project[0], project_id, _scene_key(scene), version, reference,
    ):
        return None
    return {
        "scene_key": _scene_key(scene),
        "version_id": _text(version["id"], 160),
        "reference_identity": _scene_reference_identity(version),
        "version": int(version["version"] or 0),
        "name": _text(scene["scene_description"], 200) or "锁定场景",
        "prompt": _text(version["prompt"], 8000),
        "file": _text(reference.get("file"), 1000),
        "url": _text(reference.get("url"), 2000),
    }


def require_locked_scene_reference(
    conn, owner, project_id, shot_key, expected_reference,
):
    """Revalidate one persisted provider scene binding without trusting its paths."""
    if not isinstance(expected_reference, dict):
        raise AssetGraphError(
            "scene_reference_untrusted",
            "场景参考图缺少可信来源或不属于当前项目",
            422,
        )
    project = conn.execute(
        "SELECT username FROM short_drama_projects WHERE id=? AND deleted=0",
        (project_id,),
    ).fetchone()
    if not project or _text(project[0], 160) != _text(owner, 160):
        raise AssetGraphError(
            "scene_reference_untrusted",
            "场景参考图缺少可信来源或不属于当前项目",
            422,
        )
    current = locked_scene_reference(
        conn,
        project_id,
        shot_key,
        _text(expected_reference.get("scene_key"), 160),
    )
    expected = {
        "version_id": _text(expected_reference.get("scene_version_id"), 160),
        "reference_identity": _text(
            expected_reference.get("scene_reference_identity"), 160,
        ),
        "file": _text(expected_reference.get("file"), 1000),
        "url": _text(expected_reference.get("url"), 2000),
    }
    if not current or any(
        not value or _text(current.get(key), 2000) != value
        for key, value in expected.items()
    ):
        raise AssetGraphError(
            "scene_reference_untrusted",
            "场景参考图缺少可信来源或不属于当前项目",
            422,
        )
    return current


def bound_scene_key(conn, project_id, shot_key, scene_key=None):
    """Return the scene bound to a shot even when its reference is not ready."""
    conn.row_factory = sqlite3.Row
    rows = _scene_rows(conn, project_id)
    requested_scene_key = _text(scene_key, 160)
    if requested_scene_key:
        scene = next(
            (row for row in rows if _scene_key(row) == requested_scene_key), None,
        )
    else:
        scene = next(
            (row for row in rows if row.get("shot_key") == shot_key), None,
        )
    return _scene_key(scene) if scene else ""


def create_asset(db_factory, owner, actor, body):
    required = {"project_id", "graph_revision", "asset_key", "asset_type", "name"}
    if not isinstance(body, dict) or not required.issubset(body):
        raise AssetGraphError("asset_invalid", "资产字段不完整", 422)
    project_id = _text(body["project_id"], 160)
    key = _text(body["asset_key"], 160)
    asset_type = _text(body["asset_type"], 40)
    name = _text(body["name"], 200)
    if not key or not name or asset_type not in ASSET_TYPES or type(body["graph_revision"]) is not int:
        raise AssetGraphError("asset_invalid", "资产名称、类型或版本无效", 422)
    now = int(time.time())
    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner, project_id)
        revision = int(_ensure_state(conn, project_id, now))
        entity_id, added, _ = _seed_entity(
            conn, project_id, key, asset_type, name,
            _text(body.get("description"), 4000), actor, now,
        )
        if not added:
            raise AssetGraphError("asset_key_conflict", "资产标识已存在", 409)
        revision = _bump(conn, project_id, body["graph_revision"], now)
        _audit(conn, project_id, actor, "create_asset", entity_id, {}, now)
        conn.commit()
        return {"id": entity_id, "graph_revision": revision}


def create_version(db_factory, owner, actor, body):
    required = {"project_id", "graph_revision", "entity_id", "prompt"}
    if not isinstance(body, dict) or not required.issubset(body):
        raise AssetGraphError("asset_version_invalid", "资产版本字段不完整", 422)
    references = body.get("references") or []
    attributes = body.get("attributes") or {}
    if not isinstance(references, list) or len(references) > 8 or not isinstance(attributes, dict):
        raise AssetGraphError("asset_version_invalid", "参考图或属性格式无效", 422)
    now = int(time.time())
    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner, body["project_id"])
        entity = conn.execute(
            "SELECT id,current_version_id,asset_type FROM short_drama_graph_entities "
            "WHERE id=? AND project_id=? AND status='active'",
            (body["entity_id"], body["project_id"]),
        ).fetchone()
        if not entity:
            raise AssetGraphError("asset_not_found", "资产不存在", 404)
        if entity["asset_type"] == "scene" and references:
            raise AssetGraphError(
                "scene_reference_source_required",
                "场景参考图只能通过受控上传或本人图片资产设置",
                422,
            )
        number = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_graph_versions WHERE entity_id=?",
            (entity["id"],),
        ).fetchone()[0])
        content = {
            "prompt": _text(body["prompt"], 8000),
            "negative_prompt": _text(body.get("negative_prompt"), 4000),
            "references": references,
            "attributes": attributes,
            "valid_from": _text(body.get("valid_from"), 80),
            "valid_to": _text(body.get("valid_to"), 80),
        }
        version_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO short_drama_graph_versions"
            "(id,entity_id,version,parent_id,status,prompt,negative_prompt,references_json,"
            "attributes_json,valid_from,valid_to,content_hash,created_by,created_at) "
            "VALUES (?,?,?,?, 'draft',?,?,?,?,?,?,?,?,?)",
            (version_id, entity["id"], number, entity["current_version_id"],
             content["prompt"], content["negative_prompt"],
             _canonical(references), _canonical(attributes), content["valid_from"],
             content["valid_to"], _hash(content), actor, now),
        )
        revision = _bump(conn, body["project_id"], body["graph_revision"], now)
        _audit(conn, body["project_id"], actor, "create_version", version_id,
               {"entity_id": entity["id"], "version": number}, now)
        conn.commit()
        return {"id": version_id, "version": number, "status": "draft",
                "graph_revision": revision}


def lock_version(db_factory, owner, actor, body):
    required = {"project_id", "graph_revision", "version_id"}
    if not isinstance(body, dict) or set(body) != required:
        raise AssetGraphError("asset_lock_invalid", "锁定请求字段不完整", 422)
    now = int(time.time())
    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner, body["project_id"])
        revision = int(_ensure_state(conn, body["project_id"], now))
        if body["graph_revision"] != revision:
            raise AssetGraphError(
                "graph_revision_conflict", "资产图谱已更新，请刷新后重试", 409,
            )
        row = conn.execute(
            "SELECT version.id,version.entity_id,version.status,"
            "version.references_json,version.attributes_json,"
            "entity.asset_type,entity.asset_key "
            "FROM short_drama_graph_versions version JOIN short_drama_graph_entities entity "
            "ON entity.id=version.entity_id WHERE version.id=? AND entity.project_id=?",
            (body["version_id"], body["project_id"]),
        ).fetchone()
        if not row:
            raise AssetGraphError("asset_version_not_found", "资产版本不存在", 404)
        references = _json(row["references_json"], [])
        if row["asset_type"] == "scene" and references:
            reference = references[0] if isinstance(references[0], dict) else {}
            scene_row = next((
                item for item in _scene_rows(conn, body["project_id"])
                if item["id"] == row["entity_id"]
            ), None)
            scene_key = _scene_key(scene_row) if scene_row else row["asset_key"]
            if not _trusted_scene_reference(
                conn, owner, body["project_id"], scene_key, row, reference,
            ):
                raise AssetGraphError(
                    "scene_reference_untrusted",
                    "场景参考图缺少可信来源或不属于当前项目",
                    422,
                )
        if row["status"] != "locked":
            conn.execute(
                "UPDATE short_drama_graph_versions SET status='retired' "
                "WHERE entity_id=? AND status='locked'", (row["entity_id"],),
            )
            conn.execute(
                "UPDATE short_drama_graph_versions SET status='locked',locked_at=? WHERE id=?",
                (now, row["id"]),
            )
            conn.execute(
                "UPDATE short_drama_graph_entities SET current_version_id=?,updated_at=? WHERE id=?",
                (row["id"], now, row["entity_id"]),
            )
            revision = _bump(conn, body["project_id"], revision, now)
            _audit(conn, body["project_id"], actor, "lock_version", row["id"], {}, now)
        conn.commit()
        return {"ok": True, "version_id": row["id"], "graph_revision": revision}


def bind_asset(db_factory, owner, actor, body):
    required = {"project_id", "graph_revision", "shot_id", "relation_type", "entity_id"}
    if not isinstance(body, dict) or not required.issubset(body):
        raise AssetGraphError("asset_binding_invalid", "镜头资产绑定字段不完整", 422)
    if body["relation_type"] not in RELATION_TYPES:
        raise AssetGraphError("asset_binding_invalid", "资产关系类型无效", 422)
    now = int(time.time())
    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner, body["project_id"])
        shot = resolve_current_shot(
            conn, body["project_id"], body["shot_id"], materialize=True,
        )
        if not shot:
            raise AssetGraphError("shot_not_found", "镜头不存在", 404)
        entity = conn.execute(
            "SELECT current_version_id FROM short_drama_graph_entities "
            "WHERE id=? AND project_id=? AND status='active'",
            (body["entity_id"], body["project_id"]),
        ).fetchone()
        if not entity:
            raise AssetGraphError("asset_not_found", "资产不存在", 404)
        version_id = _text(body.get("version_id"), 160) or entity[0]
        if version_id and not conn.execute(
            "SELECT 1 FROM short_drama_graph_versions WHERE id=? AND entity_id=?",
            (version_id, body["entity_id"]),
        ).fetchone():
            raise AssetGraphError("asset_version_not_found", "资产版本不属于该资产", 422)
        _upsert_relation(
            conn, body["project_id"], "shot", shot["id"],
            body["relation_type"], body["entity_id"], version_id,
            body.get("metadata") or {}, actor, now,
        )
        revision = _bump(conn, body["project_id"], body["graph_revision"], now)
        _audit(conn, body["project_id"], actor, "bind_asset", body["entity_id"],
               {"shot_id": shot["id"], "relation_type": body["relation_type"]}, now)
        conn.commit()
        return {"ok": True, "graph_revision": revision}


def _package(conn, project_id, shot_id):
    shot = resolve_current_shot(conn, project_id, shot_id, materialize=True)
    if not shot:
        raise AssetGraphError("shot_not_found", "镜头不存在", 404)
    shot_id = shot["id"]
    assets, blockers = [], []
    relations = conn.execute(
        "SELECT relation.*,entity.asset_key,entity.asset_type,entity.name,"
        "entity.current_version_id FROM short_drama_graph_relations relation "
        "JOIN short_drama_graph_entities entity ON entity.id=relation.entity_id "
        "WHERE relation.project_id=? AND relation.source_scope='shot' "
        "AND relation.source_id=? AND relation.relation_type<>? "
        "AND entity.status='active' "
        "ORDER BY relation.relation_type,entity.asset_type,entity.name",
        (project_id, shot_id, _SCENE_BINDING_DISABLED_RELATION),
    ).fetchall()
    for relation in relations:
        version_id = relation["version_id"] or relation["current_version_id"]
        version = conn.execute(
            "SELECT * FROM short_drama_graph_versions WHERE id=? AND entity_id=?",
            (version_id, relation["entity_id"]),
        ).fetchone() if version_id else None
        if not version or version["status"] != "locked":
            blockers.append({"code": "asset_version_unlocked", "entity_id": relation["entity_id"],
                             "asset_name": relation["name"]})
            continue
        assets.append({
            "entity_id": relation["entity_id"], "asset_key": relation["asset_key"],
            "asset_type": relation["asset_type"], "name": relation["name"],
            "relation_type": relation["relation_type"], "version_id": version["id"],
            "version": int(version["version"]), "content_hash": version["content_hash"],
            "prompt": version["prompt"], "negative_prompt": version["negative_prompt"],
            "references": _json(version["references_json"], []),
            "attributes": _json(version["attributes_json"], {}),
            "valid_from": version["valid_from"], "valid_to": version["valid_to"],
        })
    expected_characters = set(_json(shot["character_keys_json"], []))
    bound_characters = {
        item["asset_key"].split(":", 1)[1] for item in assets
        if item["asset_type"] == "character" and ":" in item["asset_key"]
    }
    for missing in sorted(expected_characters - bound_characters):
        blockers.append({"code": "character_asset_missing", "character_key": missing})
    if not any(item["asset_type"] == "scene" for item in assets):
        blockers.append({"code": "scene_asset_missing"})
    package = {
        "contract_version": "short-drama-asset-package-v1",
        "project_id": project_id, "shot_id": shot_id, "shot_key": shot["shot_key"],
        "shot": {key: shot[key] for key in (
            "scene_description", "camera_description", "image_prompt", "video_prompt",
        )},
        "assets": assets,
    }
    package["package_hash"] = _hash(package)
    return package, blockers


def build_snapshot(db_factory, owner, actor, body):
    required = {"project_id", "graph_revision", "shot_id"}
    if not isinstance(body, dict) or set(body) != required:
        raise AssetGraphError("asset_snapshot_invalid", "镜头资产快照字段不完整", 422)
    now = int(time.time())
    with closing(_connection(db_factory)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner, body["project_id"])
        revision = int(_ensure_state(conn, body["project_id"], now))
        if revision != body["graph_revision"]:
            raise AssetGraphError("graph_revision_conflict", "资产图谱已更新，请刷新后重试", 409)
        package, blockers = _package(conn, body["project_id"], body["shot_id"])
        resolved_shot_id = package["shot_id"]
        number = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_graph_shot_snapshots "
            "WHERE project_id=? AND shot_id=?", (body["project_id"], resolved_shot_id),
        ).fetchone()[0])
        snapshot_id = str(uuid.uuid4())
        status = "blocked" if blockers else "ready"
        conn.execute(
            "INSERT INTO short_drama_graph_shot_snapshots"
            "(id,project_id,shot_id,version,graph_revision,status,package_json,package_hash,"
            "blockers_json,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, body["project_id"], resolved_shot_id, number, revision,
             status, _canonical(package), package["package_hash"], _canonical(blockers),
             actor, now),
        )
        _audit(conn, body["project_id"], actor, "build_snapshot", snapshot_id,
               {"status": status, "blockers": blockers}, now)
        conn.commit()
        return {"id": snapshot_id, "version": number, "status": status,
                "graph_revision": revision, "package": package, "blockers": blockers}


def current_package(db_factory, owner, project_id, shot_id):
    with closing(_connection(db_factory)) as conn:
        _project(conn, owner, project_id)
        shot = resolve_current_shot(conn, project_id, shot_id)
        if not shot:
            raise AssetGraphError("shot_not_found", "镜头不存在", 404)
        shot_id = shot["id"]
        row = conn.execute(
            "SELECT * FROM short_drama_graph_shot_snapshots WHERE project_id=? "
            "AND shot_id=? ORDER BY version DESC LIMIT 1", (project_id, shot_id),
        ).fetchone()
        if not row:
            raise AssetGraphError("asset_snapshot_missing", "请先生成镜头资产快照", 409)
        return {"id": row["id"], "version": int(row["version"]),
                "status": row["status"], "graph_revision": int(row["graph_revision"]),
                "package": _json(row["package_json"], {}),
                "blockers": _json(row["blockers_json"], [])}


def generation_contract(conn, project_id, shot_id):
    """Return the current immutable snapshot contract for a graph project."""
    conn.row_factory = sqlite3.Row
    shot = resolve_current_shot(conn, project_id, shot_id)
    if not shot:
        raise AssetGraphError("shot_not_found", "镜头不存在", 404)
    shot_id = shot["id"]
    current_revision = conn.execute(
        "SELECT revision FROM short_drama_graph_state WHERE project_id=?",
        (project_id,),
    ).fetchone()
    if not current_revision:
        return None
    row = conn.execute(
        "SELECT * FROM short_drama_graph_shot_snapshots WHERE project_id=? "
        "AND shot_id=? ORDER BY version DESC LIMIT 1", (project_id, shot_id),
    ).fetchone()
    if not row:
        raise AssetGraphError(
            "asset_snapshot_missing", "请先生成当前镜头的资产快照", 409,
        )
    blockers = _json(row["blockers_json"], [])
    if row["status"] != "ready":
        raise AssetGraphError(
            "asset_snapshot_blocked", "镜头资产仍有未锁定或缺失项", 409, blockers,
        )
    if int(current_revision[0]) != int(row["graph_revision"]):
        raise AssetGraphError(
            "asset_snapshot_stale", "资产图谱已更新，请重新生成镜头资产快照", 409,
        )
    package = _json(row["package_json"], {})
    if package.get("package_hash") != row["package_hash"]:
        raise AssetGraphError(
            "asset_snapshot_invalid", "镜头资产快照校验失败，请重新生成", 409,
        )
    return {
        "snapshot_id": row["id"],
        "package_hash": row["package_hash"],
        "graph_revision": int(row["graph_revision"]),
        "package": package,
    }


def quoted_generation_contract(
        conn, project_id, shot_id, snapshot_id, package_hash, graph_revision,
        *, require_current=True):
    """Load and validate the exact immutable snapshot bound to a quote."""
    conn.row_factory = sqlite3.Row
    shot = resolve_current_shot(conn, project_id, shot_id)
    if not shot:
        raise AssetGraphError("shot_not_found", "镜头不存在", 404)
    shot_id = shot["id"]
    values = (snapshot_id, package_hash, graph_revision)
    if all(value is None for value in values):
        if conn.execute(
            "SELECT 1 FROM short_drama_graph_state WHERE project_id=?",
            (project_id,),
        ).fetchone():
            raise AssetGraphError(
                "asset_quote_stale", "报价未绑定当前资产快照，请重新报价", 409,
            )
        return None
    if any(value is None for value in values):
        raise AssetGraphError(
            "asset_quote_stale", "报价资产契约不完整，请重新报价", 409,
        )
    row = conn.execute(
        "SELECT * FROM short_drama_graph_shot_snapshots WHERE id=? "
        "AND project_id=? AND shot_id=?",
        (snapshot_id, project_id, shot_id),
    ).fetchone()
    if (not row or row["status"] != "ready"
            or row["package_hash"] != package_hash
            or int(row["graph_revision"]) != int(graph_revision)):
        raise AssetGraphError(
            "asset_quote_stale", "报价绑定的资产快照无效，请重新报价", 409,
        )
    if require_current:
        current = conn.execute(
            "SELECT revision FROM short_drama_graph_state WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if not current or int(current[0]) != int(graph_revision):
            raise AssetGraphError(
                "asset_quote_stale", "资产图谱已更新，请重新报价", 409,
            )
    package = _json(row["package_json"], {})
    if package.get("package_hash") != package_hash:
        raise AssetGraphError(
            "asset_quote_stale", "报价绑定的资产包校验失败，请重新报价", 409,
        )
    return {
        "snapshot_id": row["id"], "package_hash": row["package_hash"],
        "graph_revision": int(row["graph_revision"]), "package": package,
    }


def generation_package(conn, project_id, shot_id):
    """Return the current package, or None only for true legacy projects."""
    contract = generation_contract(conn, project_id, shot_id)
    return contract["package"] if contract else None


def prompt_context(package):
    if not package:
        return ""
    lines = []
    for asset in package.get("assets") or []:
        name = _text(asset.get("name"), 200)
        prompt = _text(asset.get("prompt"), 2000)
        if name or prompt:
            lines.append("- %s：%s" % (name or asset.get("asset_type"), prompt))
    return "\nLocked asset package:\n" + "\n".join(lines) if lines else ""
