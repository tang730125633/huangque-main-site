"""Read-only immutable lipsync version projections for PR-E."""

import json
import time
import uuid


class LipsyncVersionError(ValueError):
    def __init__(self, code, message, *, status=400, blockers=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.blockers = list(blockers or [])


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _json(value, fallback):
    try:
        result = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return result if isinstance(result, type(fallback)) else fallback


def list_versions(conn, project_id, input_hash):
    rows = conn.execute(
        "SELECT * FROM short_drama_lipsync_versions "
        "WHERE project_id=? ORDER BY shot_id,version DESC",
        (project_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["dependency_hashes"] = _json(
            item.pop("dependency_hashes_json"), {}
        )
        item["media_spec"] = _json(item.pop("media_spec_json"), {})
        item["cost"] = _json(item.pop("cost_json"), {})
        item["stale"] = item["input_hash"] != input_hash
        item["media_url"] = (
            "/api/gen/file/" + str(item["file"]).replace("\\", "/").lstrip("/")
        )
        item["subtitle_url"] = ""
        result.append(item)
    return result


def current_versions(conn, project_id, versions):
    by_id = {item["id"]: item for item in versions}
    result = []
    for row in conn.execute(
        "SELECT * FROM short_drama_lipsync_current "
        "WHERE project_id=? ORDER BY shot_id",
        (project_id,),
    ):
        item = dict(row)
        item["version"] = by_id.get(item["version_id"])
        result.append(item)
    return result


def _version(conn, project_id, version_id):
    conn.row_factory = __import__("sqlite3").Row
    row = conn.execute(
        "SELECT * FROM short_drama_lipsync_versions "
        "WHERE id=? AND project_id=?",
        (version_id, project_id),
    ).fetchone()
    if not row:
        raise LipsyncVersionError(
            "version_not_found", "口型版本不存在", status=404
        )
    return dict(row)


def select(conn, *, project_id, version_id, expected_input_hash,
           expected_revision=None, now=None):
    """Move the mutable current pointer without changing immutable media."""
    now = int(time.time()) if now is None else int(now)
    version = _version(conn, project_id, version_id)
    if version["input_hash"] != str(expected_input_hash or ""):
        raise LipsyncVersionError(
            "stale_version", "口型版本依赖已经变化，请重新生成", status=409
        )
    current = conn.execute(
        "SELECT * FROM short_drama_lipsync_current "
        "WHERE project_id=? AND shot_id=?",
        (project_id, version["shot_id"]),
    ).fetchone()
    if current and current["locked_at"] is not None:
        raise LipsyncVersionError(
            "version_locked", "当前口型版本已经锁定", status=409
        )
    if (
        current and expected_revision is not None
        and int(current["revision"]) != int(expected_revision)
    ):
        raise LipsyncVersionError(
            "version_revision_changed",
            "口型版本已被其他页面更新，请刷新后重试",
            status=409,
        )
    revision = int(current["revision"]) + 1 if current else 1
    conn.execute(
        "INSERT INTO short_drama_lipsync_current "
        "(project_id,shot_id,version_id,revision,locked_at,locked_by,updated_at) "
        "VALUES (?,?,?,?,NULL,NULL,?) "
        "ON CONFLICT(project_id,shot_id) DO UPDATE SET "
        "version_id=excluded.version_id,revision=excluded.revision,"
        "locked_at=NULL,locked_by=NULL,updated_at=excluded.updated_at",
        (
            project_id, version["shot_id"], version_id, revision, now,
        ),
    )
    return {
        "project_id": project_id,
        "shot_id": version["shot_id"],
        "version_id": version_id,
        "revision": revision,
        "locked": False,
        "updated_at": now,
    }


def lock(conn, *, actor, project_id, version_id, expected_input_hash,
         expected_revision=None, now=None):
    """Lock the already-selected immutable version after readiness checks."""
    now = int(time.time()) if now is None else int(now)
    version = _version(conn, project_id, version_id)
    if version["input_hash"] != str(expected_input_hash or ""):
        raise LipsyncVersionError(
            "stale_version", "口型版本依赖已经变化，不能锁定", status=409
        )
    current = conn.execute(
        "SELECT * FROM short_drama_lipsync_current "
        "WHERE project_id=? AND shot_id=?",
        (project_id, version["shot_id"]),
    ).fetchone()
    if not current or current["version_id"] != version_id:
        raise LipsyncVersionError(
            "version_not_selected", "请先选择该口型版本", status=409
        )
    if (
        expected_revision is not None
        and int(current["revision"]) != int(expected_revision)
    ):
        raise LipsyncVersionError(
            "version_revision_changed",
            "口型版本已被其他页面更新，请刷新后重试",
            status=409,
        )
    if current["locked_at"] is not None:
        return {
            "project_id": project_id,
            "shot_id": version["shot_id"],
            "version_id": version_id,
            "revision": int(current["revision"]),
            "locked": True,
            "locked_at": int(current["locked_at"]),
            "locked_by": current["locked_by"],
            "replayed": True,
        }
    revision = int(current["revision"]) + 1
    changed = conn.execute(
        "UPDATE short_drama_lipsync_current SET revision=?,locked_at=?,"
        "locked_by=?,updated_at=? WHERE project_id=? AND shot_id=? "
        "AND revision=? AND locked_at IS NULL",
        (
            revision, now, actor, now, project_id, version["shot_id"],
            int(current["revision"]),
        ),
    ).rowcount
    if changed != 1:
        raise LipsyncVersionError(
            "lock_conflict", "口型版本已被其他用户处理", status=409
        )
    return {
        "project_id": project_id,
        "shot_id": version["shot_id"],
        "version_id": version_id,
        "revision": revision,
        "locked": True,
        "locked_at": now,
        "locked_by": actor,
        "replayed": False,
    }


def publish(conn, *, job_id, artifact, dependency_hashes, cost, model_version,
            now=None):
    """Atomically create one immutable version after media acceptance."""
    now = int(time.time()) if now is None else int(now)
    conn.row_factory = __import__("sqlite3").Row
    job = conn.execute(
        "SELECT * FROM short_drama_lipsync_jobs WHERE id=?", (job_id,)
    ).fetchone()
    if not job or job["state"] != "succeeded":
        raise ValueError("lipsync job is not ready for publication")
    existing = conn.execute(
        "SELECT * FROM short_drama_lipsync_versions WHERE job_id=?", (job_id,)
    ).fetchone()
    if existing:
        return dict(existing)
    version = int(conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_lipsync_versions "
        "WHERE project_id=? AND shot_id=?",
        (job["project_id"], job["shot_id"]),
    ).fetchone()[0])
    version_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO short_drama_lipsync_versions "
        "(id,project_id,shot_id,version,job_id,input_hash,provider,"
        "model_version,dependency_hashes_json,media_spec_json,file,file_hash,"
        "cost_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            version_id, job["project_id"], job["shot_id"], version, job_id,
            job["input_hash"], job["provider"], str(model_version),
            canonical_json(dependency_hashes), canonical_json(artifact["media_spec"]),
            artifact["file"], artifact["file_hash"], canonical_json(cost), now,
        ),
    )
    return dict(conn.execute(
        "SELECT * FROM short_drama_lipsync_versions WHERE id=?", (version_id,)
    ).fetchone())
