"""Standalone short-drama refinement and delivery preparation (PR-5).

The local deterministic adapter is an explicit, zero-cost development preview.
It must never be presented or billed as a real 1080p formal delivery.  Production
delivery stays closed until a real renderer can create and validate a new asset.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

from . import (
    short_drama_assembly_plan as media_plan,
    short_drama_duration,
    submission_idempotency,
)


ACTIVE = {"queued", "running"}
TERMINAL = {"succeeded", "failed", "canceled"}
ACCEPTANCE_CHECKS = (
    "story_continuity",
    "character_consistency",
    "audio_video_sync",
    "subtitle_timing",
    "visual_integrity",
    "transition_quality",
)
_REASSEMBLY_ENDPOINT = "/api/gen/short-drama/refinement/reassemble"
_REASSEMBLY_LEASE_SECONDS = 30


class RefinementError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_refinement_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  source_draft_version_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','confirmed','superseded')),
  url TEXT NOT NULL,
  shots_json TEXT NOT NULL,
  issues_json TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  preview_file_hash TEXT NOT NULL DEFAULT '',
  media_json TEXT NOT NULL DEFAULT '{}',
  change_summary TEXT NOT NULL,
  created_by TEXT NOT NULL,
  confirmed_at INTEGER,
  created_at INTEGER NOT NULL,
  UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS short_drama_refinement_jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  source_version_id TEXT NOT NULL,
  shot_key TEXT NOT NULL,
  actor_username TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  replacement_provider_version_id TEXT,
  status TEXT NOT NULL CHECK(status IN
    ('queued','running','succeeded','failed','canceled')),
  progress INTEGER NOT NULL DEFAULT 0,
  poll_count INTEGER NOT NULL DEFAULT 0,
  result_json TEXT,
  error_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(actor_username, idempotency_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_short_drama_refinement_active
  ON short_drama_refinement_jobs(project_id) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS short_drama_delivery_quotes (
  token TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  refinement_version_id TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  cost INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER NOT NULL,
  consumed_job_id TEXT,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS short_drama_delivery_attempts (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  quote_token TEXT NOT NULL,
  cost INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK(state IN
    ('accepted','charged','linked','refund_pending','refunded','failed')),
  job_id TEXT,
  error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(actor_username, idempotency_key)
);
CREATE TABLE IF NOT EXISTS short_drama_delivery_jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  refinement_version_id TEXT NOT NULL,
  actor_username TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('queued','running','succeeded','failed','canceled')),
  phase TEXT NOT NULL,
  progress INTEGER NOT NULL DEFAULT 0,
  poll_count INTEGER NOT NULL DEFAULT 0,
  input_hash TEXT NOT NULL,
  cost INTEGER NOT NULL DEFAULT 0,
  result_json TEXT,
  error_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_short_drama_delivery_active
  ON short_drama_delivery_jobs(project_id) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS short_drama_delivery_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL UNIQUE,
  refinement_version_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status='ready'),
  url TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS short_drama_refinement_acceptances (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  refinement_version_id TEXT NOT NULL
    REFERENCES short_drama_refinement_versions(id) ON DELETE CASCADE,
  checklist_json TEXT NOT NULL,
  source_hashes_json TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  accepted_by TEXT NOT NULL,
  accepted_at INTEGER NOT NULL,
  invalidated_at INTEGER,
  invalidation_reason TEXT NOT NULL DEFAULT '',
  UNIQUE(refinement_version_id)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_refinement_acceptance_project
  ON short_drama_refinement_acceptances(project_id,accepted_at DESC);
CREATE TABLE IF NOT EXISTS short_drama_refinement_media_preferences (
  project_id TEXT PRIMARY KEY REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  mode TEXT NOT NULL CHECK(mode IN ('voice_timeline','provider_audio','silent')),
  confirmed_by TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS short_drama_reassembly_operations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  source_version_id TEXT NOT NULL
    REFERENCES short_drama_refinement_versions(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN ('processing','succeeded')),
  lease_token TEXT,
  lease_owner TEXT,
  lease_expires_at INTEGER,
  heartbeat_at INTEGER,
  render_id TEXT NOT NULL DEFAULT '',
  refinement_version_id TEXT
    REFERENCES short_drama_refinement_versions(id) ON DELETE SET NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(project_id, source_version_id)
);
"""


def _connection(db_factory):
    conn = db_factory()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_factory):
    conn = _connection(db_factory)
    try:
        conn.executescript(_SCHEMA)
        columns = {
            str(row[1]) for row in conn.execute(
                "PRAGMA table_info(short_drama_refinement_jobs)"
            ).fetchall()
        }
        if "replacement_provider_version_id" not in columns:
            conn.execute(
                "ALTER TABLE short_drama_refinement_jobs ADD COLUMN "
                "replacement_provider_version_id TEXT"
            )
        version_columns = {
            str(row[1]) for row in conn.execute(
                "PRAGMA table_info(short_drama_refinement_versions)"
            ).fetchall()
        }
        if "preview_file_hash" not in version_columns:
            conn.execute(
                "ALTER TABLE short_drama_refinement_versions ADD COLUMN "
                "preview_file_hash TEXT NOT NULL DEFAULT ''"
            )
        if "media_json" not in version_columns:
            conn.execute(
                "ALTER TABLE short_drama_refinement_versions ADD COLUMN "
                "media_json TEXT NOT NULL DEFAULT '{}'"
            )
        preference_sql = str(conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND "
            "name='short_drama_refinement_media_preferences'"
        ).fetchone()[0] or "")
        if "provider_audio" not in preference_sql:
            conn.execute(
                "ALTER TABLE short_drama_refinement_media_preferences "
                "RENAME TO short_drama_refinement_media_preferences_legacy"
            )
            conn.execute(
                "CREATE TABLE short_drama_refinement_media_preferences ("
                "project_id TEXT PRIMARY KEY REFERENCES short_drama_projects(id) "
                "ON DELETE CASCADE, mode TEXT NOT NULL CHECK(mode IN "
                "('voice_timeline','provider_audio','silent')), "
                "confirmed_by TEXT NOT NULL, updated_at INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO short_drama_refinement_media_preferences "
                "SELECT project_id,mode,confirmed_by,updated_at FROM "
                "short_drama_refinement_media_preferences_legacy"
            )
            conn.execute(
                "DROP TABLE short_drama_refinement_media_preferences_legacy"
            )
        conn.commit()
    finally:
        conn.close()


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json(value, fallback):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _hash(value):
    return hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _idempotency_db_factory(db_factory):
    def connect():
        conn = db_factory()
        conn.row_factory = sqlite3.Row
        return conn
    return connect


def _reassembly_for_source(conn, project_id, source_version_id):
    rows = conn.execute(
        "SELECT * FROM short_drama_refinement_versions WHERE project_id=? "
        "ORDER BY version DESC", (project_id,),
    ).fetchall()
    for row in rows:
        item = _refinement(row)
        operation = (item.get("media") or {}).get("reassembly") or {}
        if str(operation.get("source_version_id") or "") == source_version_id:
            return item
    return None


def _cleanup_reassembly_output(short_drama_autodraft, project_id, render_id):
    root = short_drama_autodraft._content_root()
    target = root / "short_drama_autodraft" / project_id / render_id
    temporary = target.with_name(".%s.tmp" % target.name)
    for path in (temporary, target):
        try:
            if path.exists():
                shutil.rmtree(path)
        except OSError:
            pass


def _claim_reassembly_operation(
        db_factory, project_id, source_version_id, worker, render_id, now=None):
    now = int(time.time()) if now is None else int(now)
    token = uuid.uuid4().hex
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM short_drama_reassembly_operations "
            "WHERE project_id=? AND source_version_id=?",
            (project_id, source_version_id),
        ).fetchone()
        if row and row["status"] == "succeeded":
            conn.commit()
            return "succeeded", dict(row)
        if row and int(row["lease_expires_at"] or 0) > now:
            conn.commit()
            return "processing", dict(row)
        previous_render_id = str(row["render_id"] or "") if row else ""
        if row:
            conn.execute(
                "UPDATE short_drama_reassembly_operations SET status='processing',"
                "lease_token=?,lease_owner=?,lease_expires_at=?,heartbeat_at=?,"
                "render_id=?,refinement_version_id=NULL,updated_at=? WHERE id=?",
                (
                    token, worker, now + _REASSEMBLY_LEASE_SECONDS, now,
                    render_id, now, row["id"],
                ),
            )
            operation_id = row["id"]
        else:
            operation_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO short_drama_reassembly_operations "
                "(id,project_id,source_version_id,status,lease_token,lease_owner,"
                "lease_expires_at,heartbeat_at,render_id,created_at,updated_at) "
                "VALUES (?,?,?,'processing',?,?,?,?,?,?,?)",
                (
                    operation_id, project_id, source_version_id, token, worker,
                    now + _REASSEMBLY_LEASE_SECONDS, now, render_id, now, now,
                ),
            )
        conn.commit()
        return "acquired", {
            "id": operation_id, "lease_token": token,
            "render_id": render_id, "previous_render_id": previous_render_id,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _heartbeat_reassembly_operation(db_factory, operation_id, token, now=None):
    now = int(time.time()) if now is None else int(now)
    conn = _connection(db_factory)
    try:
        changed = conn.execute(
            "UPDATE short_drama_reassembly_operations SET heartbeat_at=?,"
            "lease_expires_at=?,updated_at=? WHERE id=? AND lease_token=? "
            "AND status='processing'",
            (
                now, now + _REASSEMBLY_LEASE_SECONDS, now,
                operation_id, token,
            ),
        ).rowcount
        conn.commit()
        return bool(changed)
    finally:
        conn.close()


def _abort_reassembly_operation(db_factory, operation_id, token):
    conn = _connection(db_factory)
    try:
        changed = conn.execute(
            "DELETE FROM short_drama_reassembly_operations "
            "WHERE id=? AND lease_token=? AND status='processing'",
            (operation_id, token),
        ).rowcount
        conn.commit()
        return bool(changed)
    finally:
        conn.close()


class _ReassemblyLeaseKeeper:
    """Renew one claimed operation throughout preprocessing and rendering."""

    def __init__(self, db_factory, operation_id, token):
        self.db_factory = db_factory
        self.operation_id = operation_id
        self.token = token
        self.stopped = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(
            target=self._keep_alive,
            name="short-drama-reassembly-heartbeat",
            daemon=True,
        )

    def _renew(self):
        try:
            active = _heartbeat_reassembly_operation(
                self.db_factory, self.operation_id, self.token,
            )
        except Exception:
            active = False
        if not active:
            self.lost.set()
        return active

    def _keep_alive(self):
        while not self.stopped.wait(max(1, _REASSEMBLY_LEASE_SECONDS // 3)):
            if not self._renew():
                return

    def start(self):
        # Renew synchronously before any media I/O so a very short test lease
        # cannot expire between the claim transaction and thread scheduling.
        if not self._renew():
            self.ensure_active()
        self.thread.start()
        return self

    def ensure_active(self):
        if self.lost.is_set():
            raise RefinementError(
                "refinement_reassembly_lease_lost",
                "重新装配租约已失效，当前结果不会保存", 409,
            )

    def close(self):
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1)


def _provider_asset(conn, project_id, shot_key, version_id=None):
    values = [project_id, shot_key]
    where = "v.project_id=? AND v.shot_key=? AND v.status='ready'"
    if version_id:
        where += " AND v.id=?"
        values.append(version_id)
    row = conn.execute(
        "SELECT v.*,j.status AS job_status,j.id AS internal_provider_job_id,"
        "j.created_at AS provider_job_created_at "
        "FROM short_drama_provider_shot_versions v "
        "JOIN short_drama_provider_shot_jobs j ON j.id=v.job_id WHERE " + where +
        " ORDER BY v.version DESC,v.created_at DESC LIMIT 1",
        tuple(values),
    ).fetchone()
    item = dict(row) if row else None
    if not item or item.get("job_status") != "succeeded":
        raise RefinementError(
            "refinement_provider_asset_required",
            "请先通过真实画面 Provider 为该镜头生成一个成功的新版本",
            409,
        )
    return item


def _replacement_provider_asset(conn, project_id, shot, requested_id=""):
    shot_key = str(shot.get("shot_key") or "")
    previous_id = str(shot.get("provider_version_id") or "")
    issue = shot.get("issue") if isinstance(shot.get("issue"), dict) else None
    if not issue:
        raise RefinementError(
            "refinement_issue_required",
            "当前镜头没有待处理问题，不能创建重做任务",
            409,
        )
    if requested_id:
        replacement = _provider_asset(
            conn, project_id, shot_key, str(requested_id)
        )
    else:
        replacement = _provider_asset(conn, project_id, shot_key)
    version_floor = int(
        issue.get("provider_version_floor")
        if issue.get("provider_version_floor") is not None
        else shot.get("provider_version") or 0
    )
    if (
        str(replacement["id"]) == previous_id
        or int(replacement.get("version") or 0) <= version_floor
    ):
        raise RefinementError(
            "refinement_new_provider_asset_required",
            "当前最新 Provider 镜头仍是旧版本，请先真实重生成该镜头",
            409,
        )
    reported_at = int(issue.get("reported_at") or 0)
    replacement_created_at = int(
        replacement.get("provider_job_created_at") or 0
    )
    if reported_at and replacement_created_at < reported_at:
        raise RefinementError(
            "refinement_provider_asset_predates_issue",
            "替换素材的 Provider 任务早于当前问题记录，请重新生成该镜头",
            409,
        )
    source_job_id = str(issue.get("source_provider_job_id") or "")
    replacement_job_id = str(replacement.get("provider_job_id") or "")
    if source_job_id and replacement_job_id == source_job_id:
        raise RefinementError(
            "refinement_new_provider_job_required",
            "替换素材必须来自新的 Provider 任务",
            409,
        )
    return replacement


def _issue_revision(source, shot):
    issue = shot.get("issue") if isinstance(shot.get("issue"), dict) else {}
    return str(issue.get("issue_id") or _hash({
        "source_version_id": source["id"],
        "source_hash": source["input_hash"],
        "shot_key": shot.get("shot_key"),
        "issue": issue,
    }))


def _refinement_request_hash(project_id, source, shot, replacement):
    issue = shot.get("issue") if isinstance(shot.get("issue"), dict) else {}
    version_floor = int(
        issue.get("provider_version_floor")
        if issue.get("provider_version_floor") is not None
        else shot.get("provider_version") or 0
    )
    return _hash({
        "project_id": project_id,
        "source_version_id": source["id"],
        "shot_key": str(shot.get("shot_key") or ""),
        "issue_revision": _issue_revision(source, shot),
        "source_provider_version_id": str(
            shot.get("provider_version_id") or ""
        ),
        "source_provider_version": int(shot.get("provider_version") or 0),
        "provider_version_floor": version_floor,
        "replacement_provider_version_id": str(replacement["id"]),
        "replacement_provider_job_id": str(
            replacement["internal_provider_job_id"]
        ),
    })


def _key(value):
    value = str(value or "").strip()
    if not value or len(value) > 160:
        raise RefinementError("idempotency_key_required", "缺少有效的幂等键")
    return value


def _project(conn, owner_username, project_id):
    row = conn.execute(
        "SELECT id,title,ratio,target_duration,shot_count,point_budget,spent_points "
        "FROM short_drama_projects WHERE id=? AND username=? AND deleted=0",
        (project_id, owner_username),
    ).fetchone()
    if not row:
        raise LookupError("short drama project does not exist")
    return dict(row)


def _row_json(row, fields):
    if not row:
        return None
    item = dict(row)
    for field, fallback in fields.items():
        item[field] = _json(item.pop(field + "_json"), fallback)
    return item


def _refinement(row):
    item = _row_json(row, {"shots": [], "issues": [], "media": {}})
    if item:
        item["version"] = int(item["version"])
    return item


def _job(row):
    item = _row_json(row, {"result": None, "error": None})
    if item:
        item["progress"] = int(item["progress"])
        item["poll_count"] = int(item["poll_count"])
        if "cost" in item:
            item["cost"] = int(item["cost"])
    return item


def _delivery(row):
    item = _row_json(row, {"snapshot": {}})
    if item:
        item["version"] = int(item["version"])
        snapshot = item["snapshot"]
        if snapshot.get("adapter") == "local_deterministic":
            snapshot["deliverable"] = False
            snapshot["output_kind"] = "demo_preview"
    return item


def _current_draft(conn, project_id):
    row = conn.execute(
        "SELECT * FROM short_drama_autodraft_versions WHERE project_id=? "
        "ORDER BY version DESC LIMIT 1", (project_id,),
    ).fetchone()
    if not row:
        raise RefinementError(
            "playable_draft_required", "请先完成 720p 可播放草稿", 409
        )
    item = dict(row)
    item["manifest"] = _json(item.pop("manifest_json"), {})
    return item


def _acceptance_evidence(conn, project, refinement):
    draft = _current_draft(conn, project["id"])
    if draft["id"] != refinement["source_draft_version_id"]:
        raise RefinementError(
            "refinement_source_stale", "精修版本对应的自动草稿已经过期", 409
        )
    manifest = draft["manifest"]
    refinement_media = refinement.get("media") or {}
    draft_media = manifest.get("media_contract") or {}
    media = (
        refinement_media.get("media_contract")
        or draft_media or {}
    )
    media_current = True
    live_media = media
    if refinement_media.get("media_contract"):
        live_media = draft_media or media
        if not all(
            str(live_media.get(key) or "") == str(media.get(key) or "")
            for key in ("audio_hash", "subtitle_hash", "timeline_hash")
        ):
            media_current = False
    if media.get("evidence_source") in {
        "locked_voice_tables", "explicit_provider_audio_confirmation",
        "explicit_silent_confirmation",
    }:
        from . import short_drama_autodraft
        live_media = short_drama_autodraft._locked_media_contract(conn, project)
        media_current = all(
            str(live_media.get(key) or "") == str(media.get(key) or "")
            for key in ("audio_hash", "subtitle_hash", "timeline_hash")
        ) and (
            live_media.get("delivery_eligible") is True
            and live_media.get("evidence_source") == media.get("evidence_source")
        )
    visual = [{
        "shot_key": str(item.get("shot_key") or ""),
        "version_id": str(item.get("provider_version_id") or ""),
        "provider_job_id": str(item.get("provider_job_id") or ""),
        "input_hash": str(item.get("input_hash") or ""),
        "file_hash": str(item.get("file_hash") or ""),
    } for item in refinement.get("shots") or []]
    material_hash = str(media.get("material_hash") or "")
    preview_hash = str(refinement.get("preview_file_hash") or "")
    preview_file = str(refinement_media.get("preview_file") or "")
    if preview_file:
        root = Path(os.environ.get(
            "CONTENT_OUT", str(Path(__file__).resolve().parents[1] / "content_out")
        )).resolve()
        candidate = (root / preview_file).resolve()
        try:
            candidate.relative_to(root)
            live_preview_hash = _file_hash(candidate) if candidate.is_file() else "missing"
        except (ValueError, OSError):
            live_preview_hash = "invalid"
        if not preview_hash or live_preview_hash != preview_hash:
            media_current = False
        preview_hash = live_preview_hash
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='short_drama_provider_shot_versions'"
    ).fetchone():
        selected = []
        from . import short_drama_autodraft
        for shot in refinement.get("shots") or []:
            version_id = str(shot.get("provider_version_id") or "")
            if not version_id:
                continue
            row = conn.execute(
                "SELECT id,shot_key,input_hash,file FROM "
                "short_drama_provider_shot_versions WHERE id=? AND project_id=? "
                "AND status='ready'",
                (version_id, project["id"]),
            ).fetchone()
            if not row:
                media_current = False
                continue
            item = dict(row)
            try:
                item["file_hash"] = _file_hash(
                    short_drama_autodraft._controlled_provider_file(item["file"])
                )
            except (OSError, short_drama_autodraft.AutodraftError):
                item["file_hash"] = "missing"
                media_current = False
            if (
                shot.get("file_hash")
                and str(shot.get("file_hash")) != item["file_hash"]
            ):
                media_current = False
            selected.append(item)
        if selected:
            include_physical_hash = bool(
                refinement.get("preview_file_hash")
                or refinement_media.get("preview_file_hash")
            )
            material_hash = _hash([dict({
                "id": item["id"], "shot_key": item["shot_key"],
                "input_hash": item["input_hash"],
            }, **({"file_hash": item["file_hash"]} if include_physical_hash else {}))
                for item in selected])
    if media.get("evidence_source") in {
        "locked_voice_tables", "explicit_provider_audio_confirmation",
        "explicit_silent_confirmation",
    }:
        media_current = (
            media_current
            and bool(material_hash)
            and material_hash == str(media.get("material_hash") or "")
        )
    source_hashes = {
        "project": _hash({
            "ratio": project["ratio"],
            "target_duration": int(project["target_duration"]),
        }),
        "draft": str(draft["input_hash"]),
        "refinement": str(refinement["input_hash"]),
        "visual": _hash(visual),
        "preview": preview_hash or str(refinement["url"]),
        "audio": str(live_media.get("audio_hash") or ""),
        "subtitle": str(live_media.get("subtitle_hash") or ""),
        "timeline": str(live_media.get("timeline_hash") or ""),
        "material": material_hash or str(
            (manifest.get("media_contract") or {}).get("material_hash")
            or _hash(visual)
        ),
    }
    snapshot = {
        "contract_version": "short-drama-refinement-acceptance-v1",
        "project_id": project["id"],
        "refinement_version_id": refinement["id"],
        "ratio": project["ratio"],
        "duration_ms": _refinement_duration_ms(project, refinement),
        "source_hashes": source_hashes,
        "media_contract": media,
        "media_validation": refinement_media.get("media_validation") or {},
        "media_current": media_current,
    }
    return source_hashes, snapshot


def _acceptance(row):
    if not row:
        return None
    item = dict(row)
    item["checklist"] = _json(item.pop("checklist_json"), {})
    item["source_hashes"] = _json(item.pop("source_hashes_json"), {})
    item["snapshot"] = _json(item.pop("snapshot_json"), {})
    item["valid"] = item.get("invalidated_at") is None
    return item


def _valid_acceptance(conn, project, refinement, *, require=False):
    item = _acceptance(conn.execute(
        "SELECT * FROM short_drama_refinement_acceptances "
        "WHERE refinement_version_id=?", (refinement["id"],),
    ).fetchone())
    if not item or not item["valid"]:
        if require:
            raise RefinementError(
                "refinement_acceptance_required", "当前精修版本尚未完成有效验收", 409
            )
        return item
    source_hashes, snapshot = _acceptance_evidence(conn, project, refinement)
    if (
        item["source_hashes"] != source_hashes
        or item["snapshot_hash"] != _hash(snapshot)
    ):
        conn.execute(
            "UPDATE short_drama_refinement_acceptances SET invalidated_at=?,"
            "invalidation_reason='source_changed' WHERE id=? AND invalidated_at IS NULL",
            (int(time.time()), item["id"]),
        )
        if require:
            raise RefinementError(
                "refinement_acceptance_stale", "验收后的画面、音轨、字幕或素材已变化", 409
            )
        item["valid"] = False
        item["invalidation_reason"] = "source_changed"
    return item


def _refinement_duration_ms(project, refinement):
    """Use the immutable preview evidence, never the public band sentinel."""
    media = (refinement or {}).get("media") or {}
    validation = media.get("media_validation") or {}
    value = int(validation.get("duration_ms") or 0)
    if value > 0:
        return value
    value = max(
        [int(item.get("end_ms") or 0) for item in (refinement or {}).get("shots") or []]
        or [0]
    )
    if value > 0:
        return value
    shot_count = int(project.get("shot_count") or 0)
    if shot_count:
        return short_drama_duration.choose(
            project["target_duration"], shot_count,
        ) * 1000
    # Only legacy rows without preview evidence can reach this fallback.
    return int(project["target_duration"]) * 1000


def _latest_refinement(conn, project_id):
    return _refinement(conn.execute(
        "SELECT * FROM short_drama_refinement_versions WHERE project_id=? "
        "ORDER BY version DESC LIMIT 1", (project_id,),
    ).fetchone())


def _refinement_assembly_status(conn, project, refinement):
    """Compare the preview duration with all physical shot media."""
    from . import short_drama_autodraft

    source_duration_ms = 0
    shot_durations = []
    try:
        for shot in refinement.get("shots") or []:
            shot_key = str(shot.get("shot_key") or "")
            version_id = str(shot.get("provider_version_id") or "")
            asset = _provider_asset(conn, project["id"], shot_key, version_id or None)
            path = short_drama_autodraft._controlled_provider_file(asset["file"])
            probe = media_plan.probe_media(path)
            duration_ms = int(probe.get("duration_ms") or 0)
            if not probe.get("video") or duration_ms <= 0:
                raise ValueError("invalid provider media")
            source_duration_ms += duration_ms
            shot_durations.append({"shot_key": shot_key, "duration_ms": duration_ms})
    except (OSError, ValueError, RefinementError, media_plan.MediaPlanError,
            short_drama_autodraft.AutodraftError):
        return {"available": False, "reassembly_required": False,
                "message": "暂时无法核对完整镜头时长"}

    validation = (refinement.get("media") or {}).get("media_validation") or {}
    preview_duration_ms = int(validation.get("duration_ms") or 0)
    planned_duration_ms = max(
        [int(item.get("end_ms") or 0) for item in refinement.get("shots") or []]
        or [_refinement_duration_ms(project, refinement)]
    )
    if preview_duration_ms <= 0:
        preview_duration_ms = planned_duration_ms
    missing_ms = max(0, source_duration_ms - preview_duration_ms)
    required = missing_ms > 500
    return {
        "available": True, "reassembly_required": required,
        "source_duration_ms": source_duration_ms,
        "preview_duration_ms": preview_duration_ms,
        "missing_duration_ms": missing_ms,
        "shot_durations": shot_durations,
        "message": ("现有预览没有包含全部镜头时长，请免费重新装配完整预览"
                    if required else "当前预览已包含全部镜头"),
    }


def _seed_refinement(conn, project_id, actor_username):
    draft = _current_draft(conn, project_id)
    current = _latest_refinement(conn, project_id)
    if current and current["source_draft_version_id"] == draft["id"]:
        return current
    manifest = draft["manifest"]
    now = int(time.time())
    version = int(conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_refinement_versions "
        "WHERE project_id=?", (project_id,),
    ).fetchone()[0])
    item = {
        "id": uuid.uuid4().hex,
        "project_id": project_id,
        "source_draft_version_id": draft["id"],
        "version": version,
        "status": "draft",
        "url": draft["url"],
        "shots": list(manifest.get("shots") or []),
        "issues": list(manifest.get("issues") or []),
        "input_hash": draft["input_hash"],
        "change_summary": "从 720p 自动草稿创建精修工作副本",
        "created_by": actor_username,
        "confirmed_at": None,
        "created_at": now,
    }
    conn.execute(
        "INSERT INTO short_drama_refinement_versions "
        "(id,project_id,source_draft_version_id,version,status,url,shots_json,"
        "issues_json,input_hash,change_summary,created_by,confirmed_at,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            item["id"], project_id, draft["id"], version, "draft", draft["url"],
            _json_text(item["shots"]), _json_text(item["issues"]),
            item["input_hash"], item["change_summary"], actor_username, None, now,
        ),
    )
    return item


def _refinement_versions(conn, project_id):
    return [
        _refinement(row) for row in conn.execute(
            "SELECT * FROM short_drama_refinement_versions WHERE project_id=? "
            "ORDER BY version DESC", (project_id,),
        ).fetchall()
    ]


def _delivery_versions(conn, project_id):
    return [
        _delivery(row) for row in conn.execute(
            "SELECT * FROM short_drama_delivery_versions WHERE project_id=? "
            "ORDER BY version DESC", (project_id,),
        ).fetchall()
    ]


def _delivery_capability():
    mode = os.getenv("HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE", "").strip().lower()
    if mode == "local_ffmpeg":
        ffmpeg = os.environ.get("FFMPEG_BIN", "ffmpeg")
        ffprobe = os.environ.get("FFPROBE_BIN", "ffprobe")
        checks = {}
        for name, command in (
            ("ffmpeg", [ffmpeg, "-version"]),
            ("ffprobe", [ffprobe, "-version"]),
        ):
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=10,
                )
                checks[name] = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                checks[name] = False
        encoders = ""
        if checks["ffmpeg"]:
            try:
                result = subprocess.run(
                    [ffmpeg, "-hide_banner", "-encoders"],
                    capture_output=True, text=True, timeout=15,
                )
                encoders = (result.stdout or "") if result.returncode == 0 else ""
            except (OSError, subprocess.TimeoutExpired):
                pass
        checks["libx264"] = "libx264" in encoders
        checks["aac"] = " aac " in (" " + encoders.replace("\n", " ") + " ")
        root = Path(os.environ.get(
            "CONTENT_OUT", str(Path(__file__).resolve().parents[1] / "content_out")
        )).resolve()
        probe_dir = root if root.exists() else root.parent
        try:
            probe_dir.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                prefix=".delivery-capability-", dir=str(probe_dir), delete=True
            )
            handle.close()
            checks["output_writable"] = True
        except OSError:
            checks["output_writable"] = False
        unavailable = [name for name, ready in checks.items() if not ready]
        if unavailable:
            return {
                "delivery_enabled": False,
                "deliverable": False,
                "mode": "unavailable",
                "adapter": "local_ffmpeg",
                "formal_cost": 0,
                "reason": "missing_" + unavailable[0],
                "checks": checks,
            }
        return {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "local_ffmpeg",
            "adapter": "local_ffmpeg",
            "formal_cost": 0,
            "reason": "local_1080p_renderer",
            "checks": checks,
        }
    demo_enabled = (
        mode == "demo"
        and os.getenv("HQ_SHORT_DRAMA_AUTODRAFT_DEV_FREE") == "1"
    )
    if demo_enabled:
        return {
            "delivery_enabled": True,
            "deliverable": False,
            "mode": "development_free",
            "adapter": "local_deterministic",
            "formal_cost": 0,
            "reason": "demo_preview_only",
        }
    return {
        "delivery_enabled": False,
        "deliverable": False,
        "mode": "disabled",
        "adapter": "disabled",
        "formal_cost": 0,
        "reason": "formal_executor_unavailable",
    }


def _require_delivery_available():
    capability = _delivery_capability()
    if not capability["delivery_enabled"]:
        raise RefinementError(
            "formal_delivery_unavailable",
            "真实 1080p 正式交付执行器尚未启用，本次不会询价或扣点",
            503,
        )
    return capability


def _formal_cost(capability=None):
    capability = capability or _delivery_capability()
    return max(0, int(capability.get("formal_cost") or 0))


def _render_refinement_preview(conn, job, source):
    from . import short_drama_autodraft

    latest = _latest_refinement(conn, job["project_id"])
    if not latest or latest["id"] != source["id"]:
        raise RefinementError(
            "refinement_source_stale",
            "问题镜头来源版本已经变化，请基于当前版本重新生成",
            409,
        )
    source_shot = next(
        (
            item for item in source["shots"]
            if str(item.get("shot_key") or "") == job["shot_key"]
        ),
        None,
    )
    if not source_shot:
        raise RefinementError("shot_not_found", "目标镜头不存在", 404)
    replacement = _replacement_provider_asset(
        conn,
        job["project_id"],
        source_shot,
        str(job.get("replacement_provider_version_id") or ""),
    )
    project_row = conn.execute(
        "SELECT id,ratio,target_duration FROM short_drama_projects WHERE id=?",
        (job["project_id"],),
    ).fetchone()
    project = dict(project_row) if project_row else None
    if not project:
        raise RefinementError(
            "refinement_project_missing", "短剧项目不存在", 404
        )
    draft = _current_draft(conn, job["project_id"])
    duration_ms = int(
        draft["manifest"].get("duration_ms")
        or _refinement_duration_ms(project, source)
    )
    media = _refinement_preview_media_contract(
        conn, project, source, short_drama_autodraft
    )
    assets = []
    shots = []
    for original in source["shots"]:
        shot = dict(original)
        shot_key = str(shot.get("shot_key") or "")
        version_id = (
            str(job.get("replacement_provider_version_id") or "")
            if shot_key == job["shot_key"]
            else str(shot.get("provider_version_id") or "")
        )
        asset = (
            replacement
            if shot_key == job["shot_key"]
            else _provider_asset(
                conn, job["project_id"], shot_key, version_id or None
            )
        )
        try:
            path = short_drama_autodraft._controlled_provider_file(asset["file"])
            probe = media_plan.probe_media(path)
        except short_drama_autodraft.AutodraftError as error:
            raise RefinementError(error.code, str(error), error.status) from error
        except media_plan.MediaPlanError as error:
            raise RefinementError(error.code, str(error), 409) from error
        if not probe.get("video") or int(probe.get("duration_ms") or 0) <= 0:
            raise RefinementError(
                "refinement_provider_media_invalid",
                "Provider 重做镜头缺少有效视频流或时长",
                409,
            )
        expected_ms = max(
            0, int(shot.get("end_ms") or 0) - int(shot.get("start_ms") or 0)
        )
        if expected_ms and abs(int(probe["duration_ms"]) - expected_ms) > max(
            1500, int(expected_ms * 0.35)
        ):
            raise RefinementError(
                "refinement_provider_duration_invalid",
                "Provider 重做镜头时长与锁定镜头时间线不一致",
                409,
            )
        file_hash = _file_hash(path)
        asset = dict(asset)
        asset.pop("job_status", None)
        asset["file_hash"] = file_hash
        assets.append(asset)
        shot.update({
            "status": "ready",
            "visual_source": (
                "provider_regeneration"
                if shot_key == job["shot_key"] else "provider"
            ),
            "provider": str(asset.get("provider") or ""),
            "provider_version_id": str(asset["id"]),
            "provider_version": int(asset["version"]),
            "provider_job_id": str(asset.get("provider_job_id") or ""),
            "file": str(asset["file"]),
            "url": str(asset["url"]),
            "file_hash": file_hash,
            "input_hash": str(asset["input_hash"]),
            "media_validation": {
                "duration_ms": int(probe["duration_ms"]),
                "video": bool(probe.get("video")),
                "audio": bool(probe.get("audio")),
            },
            "issue": None if shot_key == job["shot_key"] else shot.get("issue"),
        })
        shots.append(shot)
    media["material_hash"] = _hash([{
        "id": item["id"], "shot_key": item["shot_key"],
        "input_hash": item["input_hash"], "file_hash": item["file_hash"],
    } for item in assets])
    try:
        rendered = short_drama_autodraft._render_provider_preview(
            job["project_id"], "refinement-" + job["id"], {
                "all_ready": True,
                "shots": assets,
                "ratio": project["ratio"],
                "duration_ms": duration_ms,
                "media_contract": media,
            },
        )
        output = short_drama_autodraft._controlled_provider_file(rendered["file"])
    except short_drama_autodraft.AutodraftError as error:
        raise RefinementError(error.code, str(error), error.status) from error
    preview_hash = _file_hash(output)
    return {
        "url": rendered["url"],
        "file": rendered["file"],
        "file_hash": preview_hash,
        "probe": rendered["probe"],
        "media_contract": media,
        "shots": shots,
    }


def _refinement_preview_media_contract(conn, project, source, autodraft=None):
    """Keep a visual redo independent from the final voice/subtitle gate.

    A replacement shot is already paid for and generated at this point.  The
    720p refinement preview must therefore be able to adopt it even when the
    user has not chosen the final voice workflow yet.  Prefer the current live
    locked contract, then the contract used by the source preview, and finally
    preserve Provider audio as a temporary preview-only fallback.
    """
    if autodraft is None:
        from . import short_drama_autodraft as autodraft

    live = dict(autodraft._locked_media_contract(conn, project) or {})
    if live.get("delivery_eligible") is True:
        return live

    source_media = source.get("media") or {}
    source_contract = dict(source_media.get("media_contract") or {})
    if not source_contract:
        row = conn.execute(
            "SELECT manifest_json FROM short_drama_autodraft_versions "
            "WHERE id=? AND project_id=?",
            (source.get("source_draft_version_id"), project["id"]),
        ).fetchone()
        manifest = _json(row["manifest_json"], {}) if row else {}
        source_contract = dict(manifest.get("media_contract") or {})
    if source_contract.get("delivery_eligible") is True:
        return source_contract

    mode = str(source_contract.get("media_mode") or "provider_audio")
    if mode not in {"provider_audio", "silent"}:
        mode = "provider_audio"
    duration_ms = _refinement_duration_ms(project, source)
    return {
        "contract_version": "short-drama-locked-media-v1",
        "evidence_source": "refinement_preview_audio_fallback",
        "delivery_eligible": True,
        "reason": "",
        "media_mode": mode,
        "silent_confirmed": mode == "silent",
        "audio_tracks": [],
        "subtitles": [],
        "audio_hash": _hash({
            "mode": mode,
            "source_refinement_id": str(source.get("id") or ""),
        }),
        "subtitle_hash": _hash([]),
        "timeline_hash": _hash({
            "mode": mode,
            "duration_ms": duration_ms,
            "source_refinement_id": str(source.get("id") or ""),
        }),
        "subtitle_required": False,
        "preview_only": True,
    }


def _complete_refinement(conn, row, rendered=None):
    job = _job(row)
    source = _refinement(conn.execute(
        "SELECT * FROM short_drama_refinement_versions WHERE id=? AND project_id=?",
        (job["source_version_id"], job["project_id"]),
    ).fetchone())
    if not source:
        raise RefinementError("refinement_source_missing", "精修来源版本不存在", 409)
    latest = _latest_refinement(conn, job["project_id"])
    if not latest or latest["id"] != source["id"]:
        raise RefinementError(
            "refinement_source_stale",
            "问题镜头来源版本已经变化，请重新提交重做任务",
            409,
        )
    source_shot = next(
        (
            item for item in source["shots"]
            if str(item.get("shot_key") or "") == job["shot_key"]
        ),
        None,
    )
    if not source_shot:
        raise RefinementError("shot_not_found", "目标镜头不存在", 404)
    replacement = _replacement_provider_asset(
        conn,
        job["project_id"],
        source_shot,
        str(job.get("replacement_provider_version_id") or ""),
    )
    if job["request_hash"] != _refinement_request_hash(
        job["project_id"], source, source_shot, replacement
    ):
        raise RefinementError(
            "refinement_request_stale",
            "问题或 Provider 替换版本已经变化，请重新提交重做任务",
            409,
        )
    rendered = rendered or _render_refinement_preview(conn, job, source)
    shots = rendered["shots"]
    issues = [
        item for item in source["issues"]
        if str(item.get("shot_key")) != job["shot_key"]
    ]
    version = int(conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_refinement_versions "
        "WHERE project_id=?", (job["project_id"],),
    ).fetchone()[0])
    now = int(time.time())
    version_id = uuid.uuid4().hex
    input_hash = _hash({
        "source": source["input_hash"], "shot_key": job["shot_key"],
        "operation": "provider_regeneration", "version": version,
        "request_hash": job["request_hash"],
        "issue_revision": _issue_revision(source, source_shot),
        "source_provider_version_id": str(
            source_shot.get("provider_version_id") or ""
        ),
        "replacement_provider_version_id": job["replacement_provider_version_id"],
        "replacement_provider_job_id": replacement["internal_provider_job_id"],
        "preview_file_hash": rendered["file_hash"],
        "material_hash": rendered["media_contract"]["material_hash"],
    })
    conn.execute(
        "UPDATE short_drama_refinement_acceptances SET invalidated_at=?,"
        "invalidation_reason='refinement_redone' "
        "WHERE project_id=? AND invalidated_at IS NULL", (now, job["project_id"]),
    )
    conn.execute(
        "UPDATE short_drama_refinement_versions SET status='superseded' "
        "WHERE project_id=? AND status='confirmed'", (job["project_id"],),
    )
    conn.execute(
        "INSERT INTO short_drama_refinement_versions "
        "(id,project_id,source_draft_version_id,version,status,url,shots_json,"
        "issues_json,input_hash,preview_file_hash,media_json,change_summary,"
        "created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            version_id, job["project_id"], source["source_draft_version_id"],
            version, "draft", rendered["url"], _json_text(shots),
            _json_text(issues), input_hash, rendered["file_hash"],
            _json_text({
                "preview_file": rendered["file"],
                "preview_file_hash": rendered["file_hash"],
                "media_validation": rendered["probe"],
                "media_contract": rendered["media_contract"],
            }), "重做并重新装配镜头 " + job["shot_key"],
            job["actor_username"], now,
        ),
    )
    result = {
        "version_id": version_id, "version": version,
        "shot_key": job["shot_key"], "remaining_issues": len(issues),
        "url": rendered["url"], "preview_file_hash": rendered["file_hash"],
        "issue_revision": _issue_revision(source, source_shot),
        "source_provider_version_id": str(
            source_shot.get("provider_version_id") or ""
        ),
        "replacement_provider_version_id": job["replacement_provider_version_id"],
        "replacement_provider_job_id": replacement["internal_provider_job_id"],
    }
    conn.execute(
        "UPDATE short_drama_refinement_jobs SET status='succeeded',progress=100,"
        "result_json=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
        (_json_text(result), now, job["id"]),
    )
    return _job(conn.execute(
        "SELECT * FROM short_drama_refinement_jobs WHERE id=?", (job["id"],)
    ).fetchone())


def _advance_refinement(conn, row):
    job = _job(row)
    if not job or job["status"] not in ACTIVE:
        return job
    poll = job["poll_count"] + 1
    if poll >= 3:
        savepoint_active = False
        try:
            source = _refinement(conn.execute(
                "SELECT * FROM short_drama_refinement_versions "
                "WHERE id=? AND project_id=?",
                (job["source_version_id"], job["project_id"]),
            ).fetchone())
            if not source:
                raise RefinementError(
                    "refinement_source_missing", "精修来源版本不存在", 409
                )
            # Rendering and media probing happen before the write savepoint so a
            # slow external process never holds a SQLite write transaction.
            rendered = _render_refinement_preview(conn, job, source)
            conn.execute("SAVEPOINT refinement_complete")
            savepoint_active = True
            completed = _complete_refinement(conn, row, rendered=rendered)
            conn.execute("RELEASE SAVEPOINT refinement_complete")
            savepoint_active = False
            return completed
        except Exception as error:
            if savepoint_active:
                conn.execute("ROLLBACK TO SAVEPOINT refinement_complete")
                conn.execute("RELEASE SAVEPOINT refinement_complete")
            root = Path(os.environ.get(
                "CONTENT_OUT",
                str(Path(__file__).resolve().parents[1] / "content_out"),
            )).resolve()
            target = (
                root / "short_drama_autodraft" / job["project_id"] /
                ("refinement-%s" % job["id"])
            )
            cleanup_targets = [target, target.with_name(".%s.tmp" % target.name)]
            cleanup_error = ""
            try:
                for cleanup_target in cleanup_targets:
                    if cleanup_target.exists():
                        shutil.rmtree(cleanup_target)
            except OSError as cleanup:
                cleanup_error = str(cleanup)[:300]
            failure = {
                "code": getattr(error, "code", "refinement_render_failed"),
                "detail": str(error)[:500],
                "retryable": True,
                "stage": "validating_and_assembling",
                "issue_preserved": True,
                "temporary_output_cleaned": not any(
                    path.exists() for path in cleanup_targets
                ),
                "compensation": "provider_asset_retained_no_automatic_refund",
            }
            if cleanup_error:
                failure["cleanup_error"] = cleanup_error
            conn.execute(
                "UPDATE short_drama_refinement_jobs SET status='failed',"
                "progress=100,poll_count=?,result_json=NULL,error_json=?,"
                "updated_at=? WHERE id=? AND status IN ('queued','running')",
                (poll, _json_text(failure), int(time.time()), job["id"]),
            )
            return _job(conn.execute(
                "SELECT * FROM short_drama_refinement_jobs WHERE id=?",
                (job["id"],),
            ).fetchone())
    conn.execute(
        "UPDATE short_drama_refinement_jobs SET status='running',progress=?,"
        "poll_count=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
        (35 if poll == 1 else 75, poll, int(time.time()), job["id"]),
    )
    return _job(conn.execute(
        "SELECT * FROM short_drama_refinement_jobs WHERE id=?", (job["id"],)
    ).fetchone())


def _dimensions(ratio, resolution):
    profiles = {
        ("16:9", "720p"): (1280, 720),
        ("9:16", "720p"): (720, 1280),
        ("16:9", "1080p"): (1920, 1080),
        ("9:16", "1080p"): (1080, 1920),
    }
    try:
        return profiles[(ratio, resolution)]
    except KeyError as error:
        raise RefinementError("delivery_ratio_invalid", "项目画幅不受支持", 409) from error


def _validate_delivery_media(path, ratio, duration_ms, *, subtitle_required):
    try:
        probe = media_plan.probe_media(path)
    except media_plan.MediaPlanError as error:
        raise RefinementError(error.code, str(error), 409) from error
    width, height = media_plan.dimensions_for_ratio(probe)
    expected = _dimensions(ratio, "1080p")
    if (width, height) != expected:
        raise RefinementError(
            "delivery_dimensions_invalid", "正式成片尺寸与项目画幅不一致", 409
        )
    if not probe.get("video") or not probe.get("audio"):
        raise RefinementError(
            "delivery_streams_missing", "正式成片必须同时包含视频和音频", 409
        )
    if abs(int(probe["duration_ms"]) - int(duration_ms)) > 1500:
        raise RefinementError(
            "delivery_duration_invalid", "正式成片时长与锁定时间线不一致", 409
        )
    subtitle_count = 0
    try:
        result = subprocess.run(
            [
                os.environ.get("FFPROBE_BIN", "ffprobe"), "-v", "error",
                "-select_streams", "s", "-show_entries", "stream=index",
                "-of", "csv=p=0", str(path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            subtitle_count = len([line for line in result.stdout.splitlines() if line.strip()])
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RefinementError("media_probe_failed", "字幕流验证失败", 409) from error
    if subtitle_required and subtitle_count < 1:
        raise RefinementError(
            "delivery_subtitle_missing", "正式成片缺少锁定字幕流", 409
        )
    return {"probe": probe, "subtitle_streams": subtitle_count}


def _complete_delivery(conn, row):
    job = _job(row)
    capability = _require_delivery_available()
    supported = (
        (capability["mode"], capability["adapter"])
        in {
            ("development_free", "local_deterministic"),
            ("local_ffmpeg", "local_ffmpeg"),
        }
        and int(job.get("cost") or 0) == 0
    )
    if not supported:
        raise RefinementError(
            "formal_executor_unavailable",
            "真实 1080p 正式交付执行器尚未接入，禁止使用本地适配器交付",
            503,
        )
    source = _refinement(conn.execute(
        "SELECT * FROM short_drama_refinement_versions WHERE id=? AND project_id=?",
        (job["refinement_version_id"], job["project_id"]),
    ).fetchone())
    if not source or source["status"] != "confirmed":
        raise RefinementError("confirmed_refinement_required", "精修版本尚未确认", 409)
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        raise RefinementError(
            "delivery_source_missing", "演示预览来源不存在", 409
        )
    project_row = conn.execute(
        "SELECT id,ratio,target_duration FROM short_drama_projects WHERE id=?",
        (job["project_id"],),
    ).fetchone()
    project = dict(project_row) if project_row else None
    if not project:
        raise RefinementError("delivery_project_missing", "短剧项目不存在", 404)
    acceptance = _valid_acceptance(conn, project, source, require=True)
    media_contract = acceptance["snapshot"].get("media_contract") or {}
    deliverable = capability["mode"] == "local_ffmpeg"
    output_url = source_url
    output_file = ""
    output_hash = ""
    if deliverable:
        prefix = "/api/gen/file/"
        if not source_url.startswith(prefix):
            raise RefinementError(
                "delivery_source_uncontrolled",
                "1080p 导出只允许使用已归档的项目预览文件", 409,
            )
        server_dir = Path(__file__).resolve().parents[1]
        root = Path(os.environ.get(
            "CONTENT_OUT", str(server_dir / "content_out")
        )).resolve()
        source_file = (root / unquote(source_url[len(prefix):])).resolve()
        try:
            source_file.relative_to(root)
        except ValueError as error:
            raise RefinementError(
                "delivery_source_uncontrolled", "正式导出来源路径不安全", 409
            ) from error
        if not source_file.is_file():
            raise RefinementError(
                "delivery_source_missing", "720p 合成预览文件不存在", 409
            )
        target = root / "short_drama_delivery" / job["project_id"] / job["id"]
        temp = target.with_name(".%s.tmp" % target.name)
        if temp.exists():
            shutil.rmtree(temp)
        temp.mkdir(parents=True, exist_ok=True)
        rendered = temp / "final-1080p.mp4"
        width, height = _dimensions(project["ratio"], "1080p")
        command = [
            os.environ.get("FFMPEG_BIN", "ffmpeg"), "-y", "-hide_banner",
            "-loglevel", "error", "-i", str(source_file),
            "-map", "0:v:0", "-map", "0:a:0", "-map", "0:s?",
            "-vf", "scale=%d:%d:force_original_aspect_ratio=decrease,"
            "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black,fps=25,setsar=1"
            % (width, height, width, height),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-c:s", "mov_text",
            "-movflags", "+faststart", str(rendered),
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=1800,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RefinementError(
                "formal_renderer_unavailable", "1080p 导出执行器不可用", 503
            ) from error
        if result.returncode != 0 or not rendered.is_file():
            raise RefinementError(
                "formal_render_failed",
                str(result.stderr or "1080p 正式导出失败").strip()[-500:], 409,
            )
        validation = _validate_delivery_media(
            rendered, project["ratio"], _refinement_duration_ms(project, source),
            subtitle_required=bool(media_contract.get("subtitle_required")),
        )
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.rename(target)
        output_file = (
            Path("short_drama_delivery") / job["project_id"] / job["id"] /
            "final-1080p.mp4"
        ).as_posix()
        output_url = "/api/gen/file/" + output_file
        digest = hashlib.sha256()
        with (root / output_file).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        output_hash = digest.hexdigest()
    version = int(conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_delivery_versions "
        "WHERE project_id=?", (job["project_id"],),
    ).fetchone()[0])
    now = int(time.time())
    version_id = uuid.uuid4().hex
    snapshot = {
        "contract_version": "standalone-delivery-v1",
        "resolution": "1080p" if deliverable else "source",
        "refinement_version_id": source["id"],
        "refinement_version": source["version"],
        "source_hash": source["input_hash"],
        "shots": source["shots"],
        "issues": [],
        "playback_url": output_url,
        "output_file": output_file,
        "output_hash": output_hash,
        "media_validation": validation if deliverable else {},
        "adapter": capability["adapter"],
        "output_kind": "formal_1080p" if deliverable else "demo_preview",
        "deliverable": deliverable,
        "immutable": True,
    }
    input_hash = _hash(snapshot)
    conn.execute(
        "INSERT INTO short_drama_delivery_versions "
        "(id,project_id,job_id,refinement_version_id,version,status,url,"
        "snapshot_json,input_hash,created_at) VALUES (?,?,?,?,?,'ready',?,?,?,?)",
        (
            version_id, job["project_id"], job["id"], source["id"], version,
            output_url, _json_text(snapshot), input_hash, now,
        ),
    )
    result = {
        "version_id": version_id, "version": version,
        "resolution": "1080p" if deliverable else "source",
        "url": output_url, "snapshot_hash": input_hash,
        "output_kind": "formal_1080p" if deliverable else "demo_preview",
        "deliverable": deliverable,
    }
    conn.execute(
        "UPDATE short_drama_delivery_jobs SET status='succeeded',phase='completed',"
        "progress=100,result_json=?,updated_at=? WHERE id=? AND status IN "
        "('queued','running')",
        (_json_text(result), now, job["id"]),
    )
    return _job(conn.execute(
        "SELECT * FROM short_drama_delivery_jobs WHERE id=?", (job["id"],)
    ).fetchone())


def _advance_delivery(conn, row):
    job = _job(row)
    if not job or job["status"] not in ACTIVE:
        return job
    poll = job["poll_count"] + 1
    phases = ((25, "planning"), (60, "compositing"), (90, "packaging"))
    if poll > len(phases):
        try:
            return _complete_delivery(conn, row)
        except Exception as error:
            root = Path(os.environ.get(
                "CONTENT_OUT",
                str(Path(__file__).resolve().parents[1] / "content_out"),
            )).resolve()
            temp = root / "short_drama_delivery" / job["project_id"] / (
                ".%s.tmp" % job["id"]
            )
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)
            code = getattr(error, "code", "formal_render_failed")
            retryable = code in {
                "formal_renderer_unavailable", "formal_delivery_unavailable",
                "media_probe_failed", "ffprobe_unavailable",
            }
            conn.execute(
                "UPDATE short_drama_delivery_jobs SET status='failed',phase='failed',"
                "progress=100,poll_count=?,error_json=?,updated_at=? "
                "WHERE id=? AND status IN ('queued','running')",
                (
                    poll, _json_text({
                        "code": code, "detail": str(error)[:500],
                        "retryable": retryable,
                    }), int(time.time()), job["id"],
                ),
            )
            return _job(conn.execute(
                "SELECT * FROM short_drama_delivery_jobs WHERE id=?", (job["id"],),
            ).fetchone())
    progress, phase = phases[poll - 1]
    conn.execute(
        "UPDATE short_drama_delivery_jobs SET status='running',phase=?,"
        "progress=?,poll_count=?,updated_at=? WHERE id=? AND status IN "
        "('queued','running')",
        (phase, progress, poll, int(time.time()), job["id"]),
    )
    return _job(conn.execute(
        "SELECT * FROM short_drama_delivery_jobs WHERE id=?", (job["id"],)
    ).fetchone())


def workspace(db_factory, owner_username, actor_username, project_id, can_edit=True):
    conn = _connection(db_factory)
    try:
        project = _project(conn, owner_username, project_id)
        current = _seed_refinement(conn, project_id, actor_username)
        refinement_job_row = conn.execute(
            "SELECT * FROM short_drama_refinement_jobs WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT 1", (project_id,),
        ).fetchone()
        refinement_job = (
            _advance_refinement(conn, refinement_job_row)
            if refinement_job_row else None
        )
        delivery_job_row = conn.execute(
            "SELECT * FROM short_drama_delivery_jobs WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT 1", (project_id,),
        ).fetchone()
        delivery_job = (
            _advance_delivery(conn, delivery_job_row) if delivery_job_row else None
        )
        refinements = _refinement_versions(conn, project_id)
        deliveries = _delivery_versions(conn, project_id)
        current = refinements[0] if refinements else current
        current["assembly_status"] = _refinement_assembly_status(
            conn, project, current
        )
        if (
            refinement_job
            and str(refinement_job.get("source_version_id") or "")
            != str(current.get("id") or "")
        ):
            # A newer 720p/refinement version already superseded this job's
            # source.  Do not keep showing an old failure beside the new film.
            refinement_job = None
        active_delivery = (
            deliveries[0] if deliveries
            and deliveries[0]["refinement_version_id"] == current["id"] else None
        )
        source_hashes, acceptance_snapshot = _acceptance_evidence(
            conn, project, current
        )
        media_contract = acceptance_snapshot.get("media_contract") or {}
        preference_row = conn.execute(
            "SELECT mode,confirmed_by,updated_at FROM "
            "short_drama_refinement_media_preferences WHERE project_id=?",
            (project_id,),
        ).fetchone()
        acceptance = _valid_acceptance(conn, project, current)
        capability = _delivery_capability()
        conn.commit()
        return {
            "project": project,
            "state": (
                (
                    "delivered"
                    if active_delivery["snapshot"].get("deliverable") is True
                    else "demo_ready"
                ) if active_delivery
                else "delivering" if delivery_job and delivery_job["status"] in ACTIVE
                else "ready_for_delivery" if current["status"] == "confirmed"
                else "refining"
            ),
            "current_refinement": current,
            "acceptance": acceptance,
            "acceptance_requirements": {
                "contract_version": "short-drama-refinement-acceptance-v1",
                "checklist_keys": list(ACCEPTANCE_CHECKS),
                "source_hashes": source_hashes,
                "snapshot_hash": _hash(acceptance_snapshot),
                "media": {
                    "ready": (
                        media_contract.get("delivery_eligible") is True
                        and acceptance_snapshot.get("media_current") is True
                    ),
                    "reason": str(media_contract.get("reason") or ""),
                    "mode": str(media_contract.get("media_mode") or "voice_timeline"),
                    "evidence_source": str(
                        media_contract.get("evidence_source") or ""
                    ),
                },
            },
            "media_preference": dict(preference_row) if preference_row else {
                "mode": "voice_timeline", "confirmed_by": "", "updated_at": 0,
            },
            "refinement_versions": refinements,
            "current_refinement_job": refinement_job,
            "current_delivery_job": delivery_job,
            "current_delivery": active_delivery,
            "delivery_versions": deliveries,
            "billing": capability,
            "permissions": {"can_edit": bool(can_edit), "actor": actor_username},
        }
    finally:
        conn.close()


def reassemble_refinement(
        db_factory, owner_username, actor_username, body, idempotency_key):
    """Idempotently create one local reassembly per source version."""
    key = submission_idempotency.clean_key(idempotency_key)
    if not key:
        raise RefinementError(
            "refinement_reassembly_idempotency_required",
            "重新装配必须提供 Idempotency-Key", 422,
        )
    request = {
        "project_id": str(body.get("project_id") or "").strip(),
        "version_id": str(body.get("version_id") or "").strip(),
    }
    idem_db = _idempotency_db_factory(db_factory)
    state, response = submission_idempotency.begin(
        idem_db, actor_username, _REASSEMBLY_ENDPOINT, key, request,
    )
    if state == "conflict":
        raise RefinementError(
            "refinement_reassembly_idempotency_conflict",
            "同一 Idempotency-Key 不能用于不同重新装配请求", 409,
        )
    if state == "processing":
        raise RefinementError(
            "refinement_reassembly_in_progress",
            "相同重新装配请求正在处理中", 409,
        )
    if state == "replay":
        return response
    try:
        result = _perform_reassemble_refinement(
            db_factory, owner_username, actor_username, request, key,
        )
        submission_idempotency.complete(
            idem_db, actor_username, _REASSEMBLY_ENDPOINT, key, result,
        )
        return result
    except Exception:
        submission_idempotency.abort(
            idem_db, actor_username, _REASSEMBLY_ENDPOINT, key,
        )
        raise


def _perform_reassemble_refinement(
        db_factory, owner_username, actor_username, body, idempotency_key):
    """Create a complete preview from existing paid shot files only."""
    from . import short_drama_autodraft

    project_id = str(body.get("project_id") or "").strip()
    source_version_id = str(body.get("version_id") or "").strip()
    if not project_id or not source_version_id:
        raise RefinementError(
            "refinement_reassembly_invalid", "缺少要重新装配的精修版本", 422
        )
    conn = _connection(db_factory)
    render_id = ""
    output_persisted = False
    operation_id = ""
    lease_token = ""
    lease_keeper = None
    try:
        project = _project(conn, owner_username, project_id)
        completed = _reassembly_for_source(conn, project_id, source_version_id)
        if completed:
            completed["assembly_status"] = _refinement_assembly_status(
                conn, project, completed
            )
            completed["points_charged"] = 0
            completed["provider_called"] = False
            completed["idempotent_replay"] = True
            return completed
        source = _latest_refinement(conn, project_id)
        if not source or source["id"] != source_version_id:
            raise RefinementError(
                "refinement_version_stale", "预览版本已变化，请刷新后重试", 409
            )
        if conn.execute(
            "SELECT 1 FROM short_drama_refinement_jobs WHERE project_id=? "
            "AND status IN ('queued','running')", (project_id,),
        ).fetchone():
            raise RefinementError(
                "active_refinement_job", "已有镜头精修任务处理中", 409
            )

        render_id = "reassembly-" + uuid.uuid4().hex
        claim_state, operation = _claim_reassembly_operation(
            db_factory, project_id, source_version_id,
            "%s:%s" % (actor_username, idempotency_key), render_id,
        )
        if claim_state == "succeeded":
            completed = _reassembly_for_source(
                conn, project_id, source_version_id
            )
            if not completed:
                raise RefinementError(
                    "refinement_reassembly_result_missing",
                    "重新装配记录缺少成功版本，请稍后重试", 409,
                )
            completed["assembly_status"] = _refinement_assembly_status(
                conn, project, completed
            )
            completed["points_charged"] = 0
            completed["provider_called"] = False
            completed["idempotent_replay"] = True
            return completed
        if claim_state == "processing":
            raise RefinementError(
                "refinement_reassembly_in_progress",
                "该精修版本正在重新装配，请稍后重试", 409,
            )
        operation_id = operation["id"]
        lease_token = operation["lease_token"]
        lease_keeper = _ReassemblyLeaseKeeper(
            db_factory, operation_id, lease_token,
        ).start()
        previous_render_id = operation.get("previous_render_id") or ""
        if previous_render_id and previous_render_id != render_id:
            _cleanup_reassembly_output(
                short_drama_autodraft, project_id, previous_render_id
            )

        media = _refinement_preview_media_contract(
            conn, project, source, short_drama_autodraft
        )
        assets, shots = [], []
        cursor_ms = 0
        for original in source.get("shots") or []:
            shot = dict(original)
            shot_key = str(shot.get("shot_key") or "")
            provider_version_id = str(shot.get("provider_version_id") or "")
            asset = _provider_asset(
                conn, project_id, shot_key, provider_version_id or None
            )
            try:
                path = short_drama_autodraft._controlled_provider_file(asset["file"])
                probe = media_plan.probe_media(path)
            except short_drama_autodraft.AutodraftError as error:
                raise RefinementError(error.code, str(error), error.status) from error
            except media_plan.MediaPlanError as error:
                raise RefinementError(error.code, str(error), 409) from error
            duration_ms = int(probe.get("duration_ms") or 0)
            if not probe.get("video") or duration_ms <= 0:
                raise RefinementError(
                    "refinement_provider_media_invalid",
                    "已有镜头缺少有效视频流或时长", 409,
                )
            file_hash = _file_hash(path)
            clean_asset = dict(asset)
            clean_asset.pop("job_status", None)
            clean_asset["file_hash"] = file_hash
            assets.append(clean_asset)
            shot.update({
                "status": "ready", "start_ms": cursor_ms,
                "end_ms": cursor_ms + duration_ms,
                "provider": str(asset.get("provider") or ""),
                "provider_version_id": str(asset["id"]),
                "provider_version": int(asset["version"]),
                "provider_job_id": str(asset.get("provider_job_id") or ""),
                "file": str(asset["file"]), "url": str(asset["url"]),
                "file_hash": file_hash, "input_hash": str(asset["input_hash"]),
                "media_validation": {"duration_ms": duration_ms, "video": True,
                                     "audio": bool(probe.get("audio"))},
            })
            shots.append(shot)
            cursor_ms += duration_ms

        media["material_hash"] = _hash([{
            "id": item["id"], "shot_key": item["shot_key"],
            "input_hash": item["input_hash"], "file_hash": item["file_hash"],
        } for item in assets])
        lease_keeper.ensure_active()
        try:
            rendered = short_drama_autodraft._render_provider_preview(
                project_id, render_id, {
                    "all_ready": True, "shots": assets,
                    "ratio": project["ratio"],
                    "duration_ms": cursor_ms, "media_contract": media,
                    "_cancel_event": lease_keeper.lost,
                },
            )
            output = short_drama_autodraft._controlled_provider_file(rendered["file"])
        except short_drama_autodraft.AutodraftError as error:
            lease_keeper.ensure_active()
            raise RefinementError(error.code, str(error), error.status) from error

        lease_keeper.ensure_active()
        preview_hash = _file_hash(output)
        lease_keeper.ensure_active()
        conn.execute("BEGIN IMMEDIATE")
        lease = conn.execute(
            "SELECT lease_token,lease_expires_at,status FROM "
            "short_drama_reassembly_operations WHERE id=?",
            (operation_id,),
        ).fetchone()
        if (
            not lease or lease["status"] != "processing"
            or lease["lease_token"] != lease_token
            or int(lease["lease_expires_at"] or 0) <= int(time.time())
        ):
            raise RefinementError(
                "refinement_reassembly_lease_lost",
                "重新装配租约已失效，结果未保存", 409,
            )
        lease_keeper.ensure_active()
        completed = _reassembly_for_source(conn, project_id, source_version_id)
        if completed:
            now = int(time.time())
            conn.execute(
                "UPDATE short_drama_reassembly_operations SET "
                "status='succeeded',refinement_version_id=?,lease_token=NULL,"
                "lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=?,updated_at=? "
                "WHERE id=? AND lease_token=? AND status='processing'",
                (completed["id"], now, now, operation_id, lease_token),
            )
            conn.commit()
            _cleanup_reassembly_output(
                short_drama_autodraft, project_id, render_id
            )
            render_id = ""
            completed["assembly_status"] = _refinement_assembly_status(
                conn, project, completed
            )
            completed["points_charged"] = 0
            completed["provider_called"] = False
            completed["idempotent_replay"] = True
            return completed
        latest = _latest_refinement(conn, project_id)
        if not latest or latest["id"] != source_version_id:
            raise RefinementError(
                "refinement_version_stale", "预览版本已变化，请刷新后重试", 409
            )
        version = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_refinement_versions "
            "WHERE project_id=?", (project_id,),
        ).fetchone()[0])
        now = int(time.time())
        new_id = uuid.uuid4().hex
        actual_duration_ms = int(rendered.get("duration_ms") or cursor_ms)
        input_hash = _hash({
            "source": source["input_hash"], "operation": "full_reassembly",
            "version": version, "preview_file_hash": preview_hash,
            "duration_ms": actual_duration_ms,
        })
        conn.execute(
            "UPDATE short_drama_refinement_acceptances SET invalidated_at=?,"
            "invalidation_reason='preview_reassembled' WHERE project_id=? "
            "AND invalidated_at IS NULL", (now, project_id),
        )
        conn.execute(
            "UPDATE short_drama_refinement_versions SET status='superseded' "
            "WHERE project_id=? AND status='confirmed'", (project_id,),
        )
        conn.execute(
            "INSERT INTO short_drama_refinement_versions "
            "(id,project_id,source_draft_version_id,version,status,url,shots_json,"
            "issues_json,input_hash,preview_file_hash,media_json,change_summary,"
            "created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_id, project_id, source["source_draft_version_id"], version,
                "draft", rendered["url"], _json_text(shots),
                _json_text(source.get("issues") or []), input_hash, preview_hash,
                _json_text({"preview_file": rendered["file"],
                            "preview_file_hash": preview_hash,
                            "media_validation": rendered["probe"],
                            "media_contract": media,
                            "reassembly": {
                                "source_version_id": source_version_id,
                                "idempotency_key": idempotency_key,
                            }}),
                "按镜头实际时长重新装配完整预览", actor_username, now,
            ),
        )
        changed = conn.execute(
            "UPDATE short_drama_reassembly_operations SET status='succeeded',"
            "refinement_version_id=?,lease_token=NULL,lease_owner=NULL,"
            "lease_expires_at=NULL,heartbeat_at=?,updated_at=? "
            "WHERE id=? AND lease_token=? AND status='processing'",
            (new_id, now, now, operation_id, lease_token),
        ).rowcount
        if not changed:
            raise RefinementError(
                "refinement_reassembly_lease_lost",
                "重新装配租约已失效，结果未保存", 409,
            )
        conn.commit()
        output_persisted = True
        result = _latest_refinement(conn, project_id)
        result["assembly_status"] = _refinement_assembly_status(
            conn, project, result
        )
        result["points_charged"] = 0
        result["provider_called"] = False
        return result
    except Exception:
        conn.rollback()
        if operation_id and lease_token:
            _abort_reassembly_operation(db_factory, operation_id, lease_token)
        if render_id and not output_persisted:
            _cleanup_reassembly_output(
                short_drama_autodraft, project_id, render_id
            )
        raise
    finally:
        if lease_keeper is not None:
            lease_keeper.close()
        conn.close()


def set_media_preference(
        db_factory, owner_username, actor_username, body):
    project_id = str(body.get("project_id") or "").strip()
    mode = str(body.get("mode") or "").strip()
    if mode not in {"voice_timeline", "provider_audio", "silent"}:
        raise RefinementError(
            "media_preference_invalid", "请选择配音字幕或无配音字幕模式"
        )
    conn = _connection(db_factory)
    try:
        _project(conn, owner_username, project_id)
        now = int(time.time())
        conn.execute(
            "INSERT INTO short_drama_refinement_media_preferences "
            "(project_id,mode,confirmed_by,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET mode=excluded.mode,"
            "confirmed_by=excluded.confirmed_by,updated_at=excluded.updated_at",
            (project_id, mode, actor_username, now),
        )
        conn.execute(
            "UPDATE short_drama_refinement_acceptances SET invalidated_at=?,"
            "invalidation_reason='media_preference_changed' "
            "WHERE project_id=? AND invalidated_at IS NULL",
            (now, project_id),
        )
        conn.commit()
        return {
            "project_id": project_id, "mode": mode,
            "confirmed_by": actor_username, "updated_at": now,
            "requires_preview_regeneration": True,
        }
    finally:
        conn.close()


def preview_change(db_factory, owner_username, actor_username, body):
    project_id = str(body.get("project_id") or "").strip()
    shot_key = str(body.get("shot_key") or "").strip()
    conn = _connection(db_factory)
    try:
        _project(conn, owner_username, project_id)
        current = _seed_refinement(conn, project_id, actor_username)
        shot = next(
            (item for item in current["shots"]
             if str(item.get("shot_key")) == shot_key),
            None,
        )
        if not shot:
            raise RefinementError("shot_not_found", "目标镜头不存在", 404)
        replacement = None
        replacement_error = None
        try:
            replacement = _replacement_provider_asset(
                conn, project_id, shot,
                str(body.get("replacement_provider_version_id") or ""),
            )
        except RefinementError as error:
            replacement_error = {"code": error.code, "message": str(error)}
        return {
            "project_id": project_id, "source_version_id": current["id"],
            "shot_key": shot_key, "affected_shots": [shot_key],
            "invalidated_assets": ["visual:" + shot_key],
            "estimated_points": 0,
            "estimated_seconds": 30,
            "before": shot,
            "after": dict(
                shot, status="ready", visual_source="provider_regeneration",
                provider_version_id=(replacement or {}).get("id", ""),
            ),
            "replacement_ready": bool(replacement),
            "replacement_provider_version_id": (
                str(replacement["id"]) if replacement else ""
            ),
            "issue_revision": _issue_revision(current, shot),
            "source_provider_version_id": str(
                shot.get("provider_version_id") or ""
            ),
            "replacement_error": replacement_error,
            "recovery_point": current["id"],
        }
    finally:
        conn.close()


def mark_issue(db_factory, owner_username, actor_username, body):
    project_id = str(body.get("project_id") or "").strip()
    version_id = str(body.get("version_id") or "").strip()
    shot_key = str(body.get("shot_key") or "").strip()
    issue_code = str(body.get("issue_code") or "user_reported_issue").strip()
    message = str(body.get("message") or "用户验收时标记该镜头需要重做").strip()
    if not shot_key or not issue_code or len(issue_code) > 80 or len(message) > 500:
        raise RefinementError("refinement_issue_invalid", "问题镜头信息无效", 422)
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner_username, project_id)
        current = _latest_refinement(conn, project_id)
        if not current or current["id"] != version_id:
            raise RefinementError(
                "refinement_version_stale", "只能标记当前精修版本的问题镜头", 409
            )
        shots = [dict(item) for item in current["shots"]]
        shot = next(
            (item for item in shots if str(item.get("shot_key")) == shot_key), None
        )
        if not shot:
            raise RefinementError("shot_not_found", "目标镜头不存在", 404)
        now = int(time.time())
        provider_version_floor = int(conn.execute(
            "SELECT COALESCE(MAX(version),0) FROM "
            "short_drama_provider_shot_versions WHERE project_id=? AND shot_key=?",
            (project_id, shot_key),
        ).fetchone()[0])
        issue = {
            "issue_id": uuid.uuid4().hex,
            "code": issue_code,
            "severity": "error",
            "shot_key": shot_key,
            "message": message,
            "reported_by": actor_username,
            "reported_at": now,
            "source_refinement_version_id": current["id"],
            "source_provider_version_id": str(
                shot.get("provider_version_id") or ""
            ),
            "source_provider_version": int(shot.get("provider_version") or 0),
            "source_provider_job_id": str(shot.get("provider_job_id") or ""),
            "source_provider_file_hash": str(shot.get("file_hash") or ""),
            "provider_version_floor": provider_version_floor,
        }
        issues = [
            dict(item) for item in current["issues"]
            if str(item.get("shot_key")) != shot_key
        ] + [issue]
        shot["status"] = "degraded"
        shot["issue"] = issue
        version = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_refinement_versions "
            "WHERE project_id=?", (project_id,),
        ).fetchone()[0])
        new_id = uuid.uuid4().hex
        input_hash = _hash({
            "source": current["input_hash"], "shots": shots, "issues": issues,
            "reported_by": actor_username,
        })
        conn.execute(
            "UPDATE short_drama_refinement_versions SET status='superseded' "
            "WHERE project_id=? AND status='confirmed'", (project_id,),
        )
        conn.execute(
            "UPDATE short_drama_refinement_acceptances SET invalidated_at=?,"
            "invalidation_reason='issue_reported' "
            "WHERE project_id=? AND invalidated_at IS NULL", (now, project_id),
        )
        conn.execute(
            "INSERT INTO short_drama_refinement_versions "
            "(id,project_id,source_draft_version_id,version,status,url,shots_json,"
            "issues_json,input_hash,preview_file_hash,media_json,change_summary,"
            "created_by,created_at) VALUES (?,?,?,?, 'draft',?,?,?,?,?,?,?,?,?)",
            (
                new_id, project_id, current["source_draft_version_id"], version,
                current["url"], _json_text(shots), _json_text(issues), input_hash,
                str(current.get("preview_file_hash") or ""),
                _json_text(current.get("media") or {}),
                "验收标记问题镜头 %s" % shot_key, actor_username, now,
            ),
        )
        conn.commit()
        return _refinement(conn.execute(
            "SELECT * FROM short_drama_refinement_versions WHERE id=?", (new_id,),
        ).fetchone())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def start_refinement_job(
    db_factory, owner_username, actor_username, body, idempotency_key
):
    project_id = str(body.get("project_id") or "").strip()
    shot_key = str(body.get("shot_key") or "").strip()
    key = _key(idempotency_key)
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner_username, project_id)
        existing = conn.execute(
            "SELECT * FROM short_drama_refinement_jobs WHERE actor_username=? "
            "AND idempotency_key=?", (actor_username, key),
        ).fetchone()
        if existing:
            item = _job(existing)
            requested_replacement = str(
                body.get("replacement_provider_version_id") or ""
            )
            if (
                item["project_id"] != project_id
                or item["shot_key"] != shot_key
                or (
                    requested_replacement
                    and requested_replacement
                    != str(item.get("replacement_provider_version_id") or "")
                )
            ):
                raise RefinementError(
                    "idempotency_conflict",
                    "幂等键已用于其他精修请求",
                    409,
                )
            item["replayed"] = True
            conn.commit()
            return item
        source = _seed_refinement(conn, project_id, actor_username)
        requested_source = str(body.get("source_version_id") or "")
        if requested_source and requested_source != source["id"]:
            raise RefinementError(
                "refinement_source_stale",
                "精修来源版本已变化，请重新预览后提交",
                409,
            )
        if source["status"] == "confirmed":
            raise RefinementError("refinement_locked", "已确认精修版本不可修改", 409)
        if not any(str(item.get("shot_key")) == shot_key for item in source["shots"]):
            raise RefinementError("shot_not_found", "目标镜头不存在", 404)
        source_shot = next(
            item for item in source["shots"]
            if str(item.get("shot_key")) == shot_key
        )
        replacement = _replacement_provider_asset(
            conn, project_id, source_shot,
            str(body.get("replacement_provider_version_id") or ""),
        )
        request_hash = _refinement_request_hash(
            project_id, source, source_shot, replacement
        )
        if conn.execute(
            "SELECT 1 FROM short_drama_refinement_jobs WHERE project_id=? "
            "AND status IN ('queued','running')", (project_id,),
        ).fetchone():
            raise RefinementError("active_refinement_job", "已有镜头精修任务处理中", 409)
        now = int(time.time())
        job_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO short_drama_refinement_jobs "
            "(id,project_id,source_version_id,shot_key,actor_username,"
            "idempotency_key,request_hash,replacement_provider_version_id,status,"
            "progress,poll_count,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,'queued',5,0,?,?)",
            (
                job_id, project_id, source["id"], shot_key, actor_username,
                key, request_hash, replacement["id"], now, now,
            ),
        )
        conn.commit()
        item = _job(conn.execute(
            "SELECT * FROM short_drama_refinement_jobs WHERE id=?", (job_id,)
        ).fetchone())
        item["replayed"] = False
        return item
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_refinement_job(db_factory, owner_username, project_id, job_id):
    conn = _connection(db_factory)
    try:
        _project(conn, owner_username, project_id)
        row = conn.execute(
            "SELECT * FROM short_drama_refinement_jobs WHERE id=? AND project_id=?",
            (job_id, project_id),
        ).fetchone()
        if not row:
            raise LookupError("refinement job does not exist")
        item = _advance_refinement(conn, row)
        conn.commit()
        return item
    finally:
        conn.close()


def confirm_refinement(db_factory, owner_username, actor_username, body):
    project_id = str(body.get("project_id") or "").strip()
    version_id = str(body.get("version_id") or "").strip()
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, project_id)
        current = _refinement(conn.execute(
            "SELECT * FROM short_drama_refinement_versions WHERE id=? "
            "AND project_id=?", (version_id, project_id),
        ).fetchone())
        if not current:
            raise RefinementError("refinement_version_missing", "精修版本不存在", 404)
        latest = _latest_refinement(conn, project_id)
        if not latest or latest["id"] != current["id"]:
            raise RefinementError(
                "refinement_version_stale", "只能验收当前最新精修版本", 409
            )
        if current["issues"]:
            raise RefinementError(
                "refinement_issues_remaining", "仍有待处理镜头，不能确认正式交付", 409
            )
        checklist = body.get("checklist")
        if (
            not isinstance(checklist, dict)
            or set(checklist) != set(ACCEPTANCE_CHECKS)
            or any(checklist[key] is not True for key in ACCEPTANCE_CHECKS)
        ):
            raise RefinementError(
                "refinement_acceptance_incomplete", "必须完整通过六项全片验收", 409
            )
        source_hashes, snapshot = _acceptance_evidence(conn, project, current)
        if snapshot.get("media_current") is not True:
            raise RefinementError(
                "refinement_source_stale",
                "锁定音轨或字幕已变化，请重新生成并播放 720p 预览", 409,
            )
        supplied_hashes = body.get("source_hashes")
        if not isinstance(supplied_hashes, dict) or supplied_hashes != source_hashes:
            raise RefinementError(
                "refinement_acceptance_stale", "验收输入已变化，请刷新后重新验收", 409
            )
        assembly_status = _refinement_assembly_status(conn, project, current)
        if not assembly_status.get("available"):
            raise RefinementError(
                "refinement_assembly_unavailable",
                "暂时无法核对完整镜头时长，请稍后重试",
                409,
            )
        if assembly_status.get("reassembly_required"):
            raise RefinementError(
                "refinement_reassembly_required",
                "当前预览未包含全部镜头时长，请先免费重新装配",
                409,
            )
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_refinement_versions SET status='superseded' "
            "WHERE project_id=? AND status='confirmed' AND id<>?",
            (project_id, version_id),
        )
        conn.execute(
            "UPDATE short_drama_refinement_versions SET status='confirmed',"
            "confirmed_at=? WHERE id=?", (now, version_id),
        )
        conn.execute(
            "INSERT INTO short_drama_refinement_acceptances "
            "(id,project_id,refinement_version_id,checklist_json,source_hashes_json,"
            "snapshot_json,snapshot_hash,accepted_by,accepted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(refinement_version_id) DO UPDATE SET "
            "checklist_json=excluded.checklist_json,"
            "source_hashes_json=excluded.source_hashes_json,"
            "snapshot_json=excluded.snapshot_json,snapshot_hash=excluded.snapshot_hash,"
            "accepted_by=excluded.accepted_by,accepted_at=excluded.accepted_at,"
            "invalidated_at=NULL,invalidation_reason=''",
            (
                uuid.uuid4().hex, project_id, version_id, _json_text(checklist),
                _json_text(source_hashes), _json_text(snapshot), _hash(snapshot),
                actor_username, now,
            ),
        )
        conn.commit()
        result = _refinement(conn.execute(
            "SELECT * FROM short_drama_refinement_versions WHERE id=?",
            (version_id,),
        ).fetchone())
        result["acceptance"] = _acceptance(conn.execute(
            "SELECT * FROM short_drama_refinement_acceptances "
            "WHERE refinement_version_id=?", (version_id,),
        ).fetchone())
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def restore_refinement(db_factory, owner_username, actor_username, body):
    project_id = str(body.get("project_id") or "").strip()
    version_id = str(body.get("version_id") or "").strip()
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner_username, project_id)
        source = _refinement(conn.execute(
            "SELECT * FROM short_drama_refinement_versions WHERE id=? "
            "AND project_id=?", (version_id, project_id),
        ).fetchone())
        if not source:
            raise RefinementError("refinement_version_missing", "精修版本不存在", 404)
        if conn.execute(
            "SELECT 1 FROM short_drama_refinement_jobs WHERE project_id=? "
            "AND status IN ('queued','running')", (project_id,),
        ).fetchone():
            raise RefinementError("active_refinement_job", "精修任务运行中，不能恢复版本", 409)
        version = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_refinement_versions "
            "WHERE project_id=?", (project_id,),
        ).fetchone()[0])
        now = int(time.time())
        new_id = uuid.uuid4().hex
        conn.execute(
            "UPDATE short_drama_refinement_acceptances SET invalidated_at=?,"
            "invalidation_reason='version_restored' "
            "WHERE project_id=? AND invalidated_at IS NULL", (now, project_id),
        )
        conn.execute(
            "UPDATE short_drama_refinement_versions SET status='superseded' "
            "WHERE project_id=? AND status='confirmed'", (project_id,),
        )
        conn.execute(
            "INSERT INTO short_drama_refinement_versions "
            "(id,project_id,source_draft_version_id,version,status,url,shots_json,"
            "issues_json,input_hash,preview_file_hash,media_json,change_summary,"
            "created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_id, project_id, source["source_draft_version_id"], version,
                "draft", source["url"], _json_text(source["shots"]),
                _json_text(source["issues"]), source["input_hash"],
                str(source.get("preview_file_hash") or ""),
                _json_text(source.get("media") or {}),
                "从精修 v%d 恢复" % source["version"], actor_username, now,
            ),
        )
        conn.commit()
        return _refinement(conn.execute(
            "SELECT * FROM short_drama_refinement_versions WHERE id=?", (new_id,)
        ).fetchone())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_delivery_quote(db_factory, owner_username, body):
    project_id = str(body.get("project_id") or "").strip()
    version_id = str(body.get("version_id") or "").strip()
    capability = _require_delivery_available()
    conn = _connection(db_factory)
    try:
        project = _project(conn, owner_username, project_id)
        source = _refinement(conn.execute(
            "SELECT * FROM short_drama_refinement_versions WHERE id=? "
            "AND project_id=? AND status='confirmed'", (version_id, project_id),
        ).fetchone())
        if not source:
            raise RefinementError("confirmed_refinement_required", "请先确认精修版本", 409)
        latest = _latest_refinement(conn, project_id)
        if not latest or latest["id"] != source["id"]:
            raise RefinementError(
                "refinement_version_stale", "只能导出当前已验收的精修版本", 409
            )
        acceptance = _valid_acceptance(conn, project, source, require=True)
        media = acceptance["snapshot"].get("media_contract") or {}
        if capability.get("deliverable") and media.get("delivery_eligible") is not True:
            raise RefinementError(
                "delivery_media_incomplete", "正式交付缺少已锁定音轨或字幕时间线", 409
            )
        now = int(time.time())
        token = uuid.uuid4().hex
        cost = _formal_cost(capability)
        input_hash = _hash({
            "project_id": project_id, "version_id": version_id,
            "source_hash": source["input_hash"],
            "resolution": (
                "source"
                if capability["mode"] == "development_free"
                else "1080p"
            ),
            "mode": capability["mode"],
            "adapter": capability["adapter"],
        })
        conn.execute(
            "INSERT INTO short_drama_delivery_quotes "
            "(token,project_id,refinement_version_id,input_hash,cost,expires_at,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (token, project_id, version_id, input_hash, cost, now + 300, now),
        )
        conn.commit()
        return {
            "quote_token": token, "project_id": project_id,
            "version_id": version_id,
            "resolution": (
                "source"
                if capability["mode"] == "development_free"
                else "1080p"
            ),
            "cost": cost, "expires_at": now + 300, "input_hash": input_hash,
            "mode": capability["mode"],
            "deliverable": capability["deliverable"],
        }
    except RefinementError:
        if conn.in_transaction:
            conn.commit()
        raise
    finally:
        conn.close()


def _ledger_matches(actor_username, cost, ledger):
    if not isinstance(ledger, dict):
        return False
    try:
        return (
            str(ledger.get("username") or "") == actor_username
            and int(ledger.get("delta") or 0) == -int(cost)
        )
    except (TypeError, ValueError):
        return False


def start_delivery_job(
    db_factory, owner_username, actor_username, body, idempotency_key,
    deduct_points=None, refund_points=None, charge_lookup=None,
    project_usage=None,
):
    capability = _require_delivery_available()
    project_id = str(body.get("project_id") or "").strip()
    quote_token = str(body.get("quote_token") or "").strip()
    key = _key(idempotency_key)
    now = int(time.time())
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, project_id)
        quote = conn.execute(
            "SELECT * FROM short_drama_delivery_quotes WHERE token=? "
            "AND project_id=?", (quote_token, project_id),
        ).fetchone()
        if not quote:
            raise RefinementError("delivery_quote_missing", "正式导出报价不存在", 404)
        request_hash = _hash({
            "project_id": project_id, "quote_token": quote_token,
            "input_hash": quote["input_hash"],
        })
        existing = conn.execute(
            "SELECT * FROM short_drama_delivery_attempts WHERE actor_username=? "
            "AND idempotency_key=?", (actor_username, key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise RefinementError("idempotency_conflict", "幂等键已用于其他正式导出", 409)
            if existing["job_id"]:
                result = _job(conn.execute(
                    "SELECT * FROM short_drama_delivery_jobs WHERE id=?",
                    (existing["job_id"],),
                ).fetchone())
                conn.commit()
                result["replayed"] = True
                return result
            raise RefinementError("delivery_recovery_pending", "扣点状态正在恢复", 409)
        if int(quote["expires_at"]) < now:
            raise RefinementError("delivery_quote_expired", "正式导出报价已过期", 409)
        if quote["consumed_job_id"]:
            raise RefinementError("delivery_quote_consumed", "正式导出报价已使用", 409)
        if conn.execute(
            "SELECT 1 FROM short_drama_delivery_jobs WHERE project_id=? "
            "AND status IN ('queued','running')", (project_id,),
        ).fetchone():
            raise RefinementError("active_delivery_job", "已有正式导出任务处理中", 409)
        source = _refinement(conn.execute(
            "SELECT * FROM short_drama_refinement_versions WHERE id=? "
            "AND project_id=? AND status='confirmed'",
            (quote["refinement_version_id"], project_id),
        ).fetchone())
        latest = _latest_refinement(conn, project_id)
        if not source or not latest or latest["id"] != source["id"]:
            raise RefinementError(
                "delivery_source_changed", "精修版本已变化，请重新询价", 409
            )
        acceptance = _valid_acceptance(conn, project, source, require=True)
        media = acceptance["snapshot"].get("media_contract") or {}
        if capability.get("deliverable") and media.get("delivery_eligible") is not True:
            raise RefinementError(
                "delivery_media_incomplete", "正式交付缺少已锁定音轨或字幕时间线", 409
            )
        expected = _hash({
            "project_id": project_id, "version_id": quote["refinement_version_id"],
            "source_hash": source["input_hash"] if source else "",
            "resolution": (
                "source"
                if capability["mode"] == "development_free"
                else "1080p"
            ),
            "mode": capability["mode"],
            "adapter": capability["adapter"],
        })
        if not source or expected != quote["input_hash"]:
            raise RefinementError("delivery_source_changed", "精修版本已变化，请重新询价", 409)
        cost = int(quote["cost"])
        if capability["mode"] == "development_free" and cost != 0:
            raise RefinementError(
                "demo_delivery_must_be_free",
                "本地演示预览费用必须为 0，本次不会扣点",
                409,
            )
        if callable(project_usage):
            usage = project_usage(conn, project_id)
        else:
            usage = {
                "spent_points": int(project.get("spent_points") or 0),
                "reserved_points": sum(
                    max(0, int(row[0] or 0))
                    for row in conn.execute(
                        "SELECT cost FROM short_drama_delivery_attempts "
                        "WHERE project_id=? AND state='accepted'",
                        (project_id,),
                    ).fetchall()
                ),
            }
        budget = int(project.get("point_budget") or 0)
        if (
            budget
            and int(usage.get("spent_points") or 0)
            + int(usage.get("reserved_points") or 0)
            + cost
            > budget
        ):
            raise RefinementError(
                "point_budget_exceeded",
                "项目点数预算不足：已用 %d 点，预留 %d 点，本次 %d 点，预算 %d 点"
                % (
                    int(usage.get("spent_points") or 0),
                    int(usage.get("reserved_points") or 0),
                    cost,
                    budget,
                ),
                409,
            )
        attempt_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO short_drama_delivery_attempts "
            "(id,actor_username,project_id,idempotency_key,request_hash,"
            "quote_token,cost,state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,'accepted',?,?)",
            (
                attempt_id, actor_username, project_id, key, request_hash,
                quote_token, cost, now, now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    charged = False
    job_conn = None
    try:
        charge_key = "short-drama-delivery-charge:" + attempt_id
        if cost:
            if not callable(deduct_points):
                raise RefinementError("billing_unavailable", "正式导出扣点服务不可用", 503)
            try:
                deduct_points(actor_username, cost, "短剧 1080p 正式导出", charge_key)
            except Exception:
                ledger = charge_lookup(charge_key) if callable(charge_lookup) else None
                if not _ledger_matches(actor_username, cost, ledger):
                    raise
            charged = True
        job_conn = _connection(db_factory)
        job_conn.execute("BEGIN IMMEDIATE")
        quote = job_conn.execute(
            "SELECT * FROM short_drama_delivery_quotes WHERE token=?",
            (quote_token,),
        ).fetchone()
        if not quote or quote["consumed_job_id"]:
            raise RefinementError("delivery_quote_consumed", "正式导出报价已使用", 409)
        job_id = uuid.uuid4().hex
        job_conn.execute(
            "UPDATE short_drama_delivery_attempts SET state='charged',updated_at=? "
            "WHERE id=? AND state='accepted'", (int(time.time()), attempt_id),
        )
        job_conn.execute(
            "INSERT INTO short_drama_delivery_jobs "
            "(id,project_id,refinement_version_id,actor_username,status,phase,"
            "progress,poll_count,input_hash,cost,created_at,updated_at) "
            "VALUES (?,?,?,?,'queued','queued',5,0,?,?,?,?)",
            (
                job_id, project_id, quote["refinement_version_id"],
                actor_username, quote["input_hash"], int(quote["cost"]), now, now,
            ),
        )
        job_conn.execute(
            "UPDATE short_drama_delivery_quotes SET consumed_job_id=? "
            "WHERE token=? AND consumed_job_id IS NULL", (job_id, quote_token),
        )
        job_conn.execute(
            "UPDATE short_drama_delivery_attempts SET state='linked',job_id=?,"
            "updated_at=? WHERE id=? AND state='charged' AND job_id IS NULL",
            (job_id, int(time.time()), attempt_id),
        )
        job_conn.commit()
        result = _job(job_conn.execute(
            "SELECT * FROM short_drama_delivery_jobs WHERE id=?", (job_id,)
        ).fetchone())
        result["replayed"] = False
        return result
    except Exception as error:
        if job_conn is not None and job_conn.in_transaction:
            job_conn.rollback()
        state = "failed"
        if charged and callable(refund_points):
            try:
                refund_points(
                    actor_username, cost, "短剧正式导出建单失败补偿",
                    "short-drama-delivery-refund:" + attempt_id,
                )
                state = "refunded"
            except Exception:
                state = "refund_pending"
        recovery = _connection(db_factory)
        try:
            recovery.execute(
                "UPDATE short_drama_delivery_attempts SET state=?,error=?,"
                "updated_at=? WHERE id=? AND job_id IS NULL",
                (state, str(error)[:300], int(time.time()), attempt_id),
            )
            recovery.commit()
        finally:
            recovery.close()
        raise
    finally:
        if job_conn is not None:
            job_conn.close()


def get_delivery_job(db_factory, owner_username, project_id, job_id):
    conn = _connection(db_factory)
    try:
        _project(conn, owner_username, project_id)
        row = conn.execute(
            "SELECT * FROM short_drama_delivery_jobs WHERE id=? AND project_id=?",
            (job_id, project_id),
        ).fetchone()
        if not row:
            raise LookupError("delivery job does not exist")
        item = _advance_delivery(conn, row)
        conn.commit()
        return item
    finally:
        conn.close()


def reconcile_delivery_refund(db_factory, points_domain, attempt):
    """Retry one persisted delivery refund with an idempotent ledger key."""
    if not attempt or attempt.get("state") not in {"refund_pending", "refunded"}:
        return attempt
    if attempt["state"] == "refund_pending":
        refund = getattr(points_domain, "refund_points", None)
        if not callable(refund):
            return attempt
        try:
            refund(
                attempt["actor_username"],
                int(attempt["cost"] or 0),
                "short-drama formal delivery compensation",
                transaction_key="short-drama-delivery-refund:" + attempt["id"],
            )
        except Exception:
            return attempt
        conn = _connection(db_factory)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE short_drama_delivery_attempts "
                "SET state='refunded',updated_at=? "
                "WHERE id=? AND state IN ('refund_pending','refunded') "
                "AND job_id IS NULL",
                (int(time.time()), attempt["id"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    conn = _connection(db_factory)
    try:
        row = conn.execute(
            "SELECT * FROM short_drama_delivery_attempts WHERE id=?",
            (attempt["id"],),
        ).fetchone()
        return dict(row) if row else attempt
    finally:
        conn.close()


def retry_delivery_attempt_refunds(db_factory, points_domain, limit=100):
    """Recover charged delivery attempts whose first refund call failed."""
    conn = _connection(db_factory)
    try:
        attempts = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM short_drama_delivery_attempts "
                "WHERE state='refund_pending' AND job_id IS NULL "
                "ORDER BY updated_at,id LIMIT ?",
                (max(1, int(limit or 100)),),
            ).fetchall()
        ]
    finally:
        conn.close()
    return sum(
        reconcile_delivery_refund(db_factory, points_domain, attempt)["state"]
        == "refunded"
        for attempt in attempts
    )
