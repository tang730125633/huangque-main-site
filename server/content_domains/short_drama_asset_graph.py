"""Versioned asset graph and immutable per-shot generation packages."""

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing


ASSET_TYPES = {
    "character", "scene", "costume", "makeup", "prop", "vehicle", "clue",
}
RELATION_TYPES = {
    "appears_in", "located_in", "wears", "uses", "drives", "reveals", "related",
}


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


def _seed_entity(conn, project_id, key, asset_type, name, description, actor, now):
    row = conn.execute(
        "SELECT id FROM short_drama_graph_entities WHERE project_id=? AND asset_key=?",
        (project_id, key),
    ).fetchone()
    if row:
        return str(row[0]), False
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
    return entity_id, True


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
        relation_changed = False
        character_ids = {}
        for row in conn.execute(
            "SELECT character_key,name,identity_text,appearance_prompt,wardrobe_prompt "
            "FROM short_drama_characters WHERE project_id=? ORDER BY sort_order,id",
            (project_id,),
        ):
            description = "；".join(filter(None, [
                _text(row["identity_text"]), _text(row["appearance_prompt"]),
            ]))
            entity_id, added = _seed_entity(
                conn, project_id, "character:" + row["character_key"], "character",
                row["name"], description, actor, now,
            )
            character_ids[row["character_key"]] = entity_id
            if added:
                created.append(entity_id)
            wardrobe = _text(row["wardrobe_prompt"])
            if wardrobe:
                costume_id, costume_added = _seed_entity(
                    conn, project_id, "costume:%s:base" % row["character_key"],
                    "costume", "%s·基础服装" % row["name"], wardrobe, actor, now,
                )
                if costume_added:
                    created.append(costume_id)
                relation_changed = _upsert_relation(
                    conn, project_id, "asset", entity_id, "wears",
                    costume_id, None, {}, actor, now,
                ) or relation_changed
        for shot in conn.execute(
            "SELECT id,shot_key,scene_description,character_keys_json "
            "FROM short_drama_shots WHERE project_id=? ORDER BY sort_order,id",
            (project_id,),
        ):
            scene_id, added = _seed_entity(
                conn, project_id, "scene:" + shot["id"], "scene",
                "%s·场景" % shot["shot_key"], _text(shot["scene_description"]), actor, now,
            )
            if added:
                created.append(scene_id)
            relation_changed = _upsert_relation(
                conn, project_id, "shot", shot["id"], "located_in",
                scene_id, None, {}, actor, now,
            ) or relation_changed
            for character_key in _json(shot["character_keys_json"], []):
                entity_id = character_ids.get(str(character_key))
                if entity_id:
                    relation_changed = _upsert_relation(
                        conn, project_id, "shot", shot["id"], "appears_in",
                        entity_id, None, {}, actor, now,
                    ) or relation_changed
        if created or relation_changed:
            revision = _bump(conn, project_id, revision, now)
            _audit(conn, project_id, actor, "sync_foundation", "", {"created": created}, now)
        conn.commit()
        return {"ok": True, "graph_revision": revision, "created": len(created)}


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
                "ORDER BY source_scope,source_id,relation_type", (project_id,),
            )
        ]
        return {"project_id": project_id, "graph_revision": revision,
                "entities": entities, "relations": relations,
                "asset_types": sorted(ASSET_TYPES)}


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
        entity_id, added = _seed_entity(
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
            "SELECT id,current_version_id FROM short_drama_graph_entities "
            "WHERE id=? AND project_id=? AND status='active'",
            (body["entity_id"], body["project_id"]),
        ).fetchone()
        if not entity:
            raise AssetGraphError("asset_not_found", "资产不存在", 404)
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
            "SELECT version.id,version.entity_id,version.status "
            "FROM short_drama_graph_versions version JOIN short_drama_graph_entities entity "
            "ON entity.id=version.entity_id WHERE version.id=? AND entity.project_id=?",
            (body["version_id"], body["project_id"]),
        ).fetchone()
        if not row:
            raise AssetGraphError("asset_version_not_found", "资产版本不存在", 404)
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
        if not conn.execute(
            "SELECT 1 FROM short_drama_shots WHERE id=? AND project_id=?",
            (body["shot_id"], body["project_id"]),
        ).fetchone():
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
            conn, body["project_id"], "shot", body["shot_id"],
            body["relation_type"], body["entity_id"], version_id,
            body.get("metadata") or {}, actor, now,
        )
        revision = _bump(conn, body["project_id"], body["graph_revision"], now)
        _audit(conn, body["project_id"], actor, "bind_asset", body["entity_id"],
               {"shot_id": body["shot_id"], "relation_type": body["relation_type"]}, now)
        conn.commit()
        return {"ok": True, "graph_revision": revision}


def _package(conn, project_id, shot_id):
    shot = conn.execute(
        "SELECT id,shot_key,scene_description,camera_description,image_prompt,"
        "video_prompt,character_keys_json FROM short_drama_shots "
        "WHERE id=? AND project_id=?", (shot_id, project_id),
    ).fetchone()
    if not shot:
        raise AssetGraphError("shot_not_found", "镜头不存在", 404)
    assets, blockers = [], []
    relations = conn.execute(
        "SELECT relation.*,entity.asset_key,entity.asset_type,entity.name,"
        "entity.current_version_id FROM short_drama_graph_relations relation "
        "JOIN short_drama_graph_entities entity ON entity.id=relation.entity_id "
        "WHERE relation.project_id=? AND relation.source_scope='shot' "
        "AND relation.source_id=? AND entity.status='active' "
        "ORDER BY relation.relation_type,entity.asset_type,entity.name",
        (project_id, shot_id),
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
        number = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_graph_shot_snapshots "
            "WHERE project_id=? AND shot_id=?", (body["project_id"], body["shot_id"]),
        ).fetchone()[0])
        snapshot_id = str(uuid.uuid4())
        status = "blocked" if blockers else "ready"
        conn.execute(
            "INSERT INTO short_drama_graph_shot_snapshots"
            "(id,project_id,shot_id,version,graph_revision,status,package_json,package_hash,"
            "blockers_json,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, body["project_id"], body["shot_id"], number, revision,
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
