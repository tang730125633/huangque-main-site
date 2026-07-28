"""Persistent, idempotent intermediate-artifact cache for short-drama D-2."""

import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import PurePosixPath

from .short_drama_assembly_audio import ENGINE_VERSION
from .short_drama_assembly_plan import canonical_hash


ARTIFACT_KINDS = {
    "shot_voice", "dialogue", "bgm", "master_audio",
    "subtitles_ass", "manifest",
}
REQUIRED_ARTIFACT_KINDS = {
    "shot_voice", "dialogue", "master_audio", "subtitles_ass", "manifest",
}
BUILD_STATES = {"building", "ready", "failed", "stale"}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_assembly_builds (
  project_id TEXT NOT NULL
    REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  d1_input_hash TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  claim_token TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('building','ready','failed','stale')),
  manifest_json TEXT NOT NULL DEFAULT '{}',
  error_code TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(project_id,input_hash)
);
CREATE TABLE IF NOT EXISTS short_drama_assembly_artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (
    kind IN ('shot_voice','dialogue','bgm','master_audio',
             'subtitles_ass','manifest')
  ),
  shot_id TEXT NOT NULL DEFAULT '',
  file TEXT NOT NULL,
  file_hash TEXT NOT NULL,
  duration_ms INTEGER CHECK (
    duration_ms IS NULL OR
    (typeof(duration_ms)='integer' AND duration_ms > 0)
  ),
  sample_rate INTEGER CHECK (
    sample_rate IS NULL OR
    (typeof(sample_rate)='integer' AND sample_rate > 0)
  ),
  channels INTEGER CHECK (
    channels IS NULL OR
    (typeof(channels)='integer' AND channels > 0)
  ),
  status TEXT NOT NULL DEFAULT 'ready'
    CHECK (status IN ('ready','stale')),
  created_at INTEGER NOT NULL,
  FOREIGN KEY(project_id,input_hash)
    REFERENCES short_drama_assembly_builds(project_id,input_hash)
    ON DELETE CASCADE,
  UNIQUE(project_id,input_hash,kind,shot_id)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_assembly_builds_project
  ON short_drama_assembly_builds(project_id,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_short_drama_assembly_artifacts_project
  ON short_drama_assembly_artifacts(project_id,input_hash,status,kind);
DROP TRIGGER IF EXISTS short_drama_assembly_build_identity_guard;
CREATE TRIGGER short_drama_assembly_build_identity_guard
BEFORE UPDATE OF project_id,d1_input_hash,input_hash,engine_version
ON short_drama_assembly_builds
FOR EACH ROW WHEN NEW.project_id IS NOT OLD.project_id
 OR NEW.d1_input_hash IS NOT OLD.d1_input_hash
 OR NEW.input_hash IS NOT OLD.input_hash
 OR NEW.engine_version IS NOT OLD.engine_version
BEGIN SELECT RAISE(ABORT,'assembly build identity is immutable'); END;
DROP TRIGGER IF EXISTS short_drama_assembly_artifact_identity_guard;
CREATE TRIGGER short_drama_assembly_artifact_identity_guard
BEFORE UPDATE OF project_id,input_hash,kind,shot_id
ON short_drama_assembly_artifacts
FOR EACH ROW WHEN NEW.project_id IS NOT OLD.project_id
 OR NEW.input_hash IS NOT OLD.input_hash
 OR NEW.kind IS NOT OLD.kind OR NEW.shot_id IS NOT OLD.shot_id
BEGIN SELECT RAISE(ABORT,'assembly artifact identity is immutable'); END;
"""


def init_db(db_factory):
    with closing(db_factory()) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(short_drama_assembly_builds)"
            )
        }
        if "claim_token" not in columns:
            conn.execute(
                "ALTER TABLE short_drama_assembly_builds "
                "ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''"
            )
        conn.commit()


def compute_input_hash(d1_input_hash, config, voice_sources, bgm_source):
    return canonical_hash({
        "d1_input_hash": str(d1_input_hash or ""),
        "engine_version": ENGINE_VERSION,
        "config": config,
        "voice_sources": voice_sources,
        "bgm_source": bgm_source,
        "output": {
            "sample_rate": 48000,
            "channels": 2,
            "codec": "pcm_s16le",
            "loudness_lufs": -16.0,
            "true_peak_dbtp": -1.5,
        },
    })


def claim_build(
    db_factory,
    project_id,
    d1_input_hash,
    input_hash,
    now=None,
    stale_after_seconds=600,
    ready_validator=None,
):
    now = int(time.time()) if now is None else int(now)
    claim_token = uuid.uuid4().hex
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM short_drama_assembly_builds "
            "WHERE project_id=? AND input_hash=?",
            (project_id, input_hash),
        ).fetchone()
        if row:
            if row["d1_input_hash"] != d1_input_hash:
                conn.rollback()
                raise ValueError("D-2 输入哈希身份冲突")
            if row["status"] == "ready":
                if ready_validator is None:
                    conn.commit()
                    return {
                        "status": "validation_required",
                        "claim_token": None,
                    }
                try:
                    ready_is_valid = bool(
                        ready_validator(project_id, input_hash)
                    )
                except Exception:
                    ready_is_valid = False
                if ready_is_valid:
                    conn.commit()
                    return {"status": "ready", "claim_token": None}
                conn.execute(
                    "UPDATE short_drama_assembly_artifacts SET status='stale' "
                    "WHERE project_id=? AND input_hash=?",
                    (project_id, input_hash),
                )
            if (
                row["status"] == "building"
                and row["updated_at"] > now - stale_after_seconds
            ):
                conn.commit()
                return {"status": "in_progress", "claim_token": None}
            conn.execute(
                "UPDATE short_drama_assembly_builds "
                "SET status='building',manifest_json='{}',error_code='',"
                "claim_token=?,updated_at=? "
                "WHERE project_id=? AND input_hash=?",
                (claim_token, now, project_id, input_hash),
            )
            conn.commit()
            return {"status": "claimed", "claim_token": claim_token}
        conn.execute(
            "INSERT INTO short_drama_assembly_builds "
            "(project_id,d1_input_hash,input_hash,engine_version,claim_token,"
            "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                project_id, d1_input_hash, input_hash, ENGINE_VERSION,
                claim_token,
                "building", now, now,
            ),
        )
        conn.commit()
        return {"status": "claimed", "claim_token": claim_token}


def claim_is_current(db_factory, project_id, input_hash, claim_token):
    if not claim_token:
        return False
    with closing(db_factory()) as conn:
        row = conn.execute(
            "SELECT 1 FROM short_drama_assembly_builds "
            "WHERE project_id=? AND input_hash=? AND status='building' "
            "AND claim_token=?",
            (project_id, input_hash, claim_token),
        ).fetchone()
        return row is not None


def _artifact(value):
    item = dict(value)
    kind = str(item.get("kind") or "")
    shot_id = str(item.get("shot_id") or "")
    file_value = str(item.get("file") or "").replace("\\", "/")
    path = PurePosixPath(file_value)
    file_hash = str(item.get("file_hash") or "")
    if kind not in ARTIFACT_KINDS:
        raise ValueError("D-2 产物类型无效")
    if (
        not file_value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise ValueError("D-2 产物路径无效")
    if not SHA256_PATTERN.fullmatch(file_hash):
        raise ValueError("D-2 产物哈希无效")
    return {
        "kind": kind,
        "shot_id": shot_id,
        "file": file_value,
        "file_hash": file_hash,
        "duration_ms": item.get("duration_ms"),
        "sample_rate": item.get("sample_rate"),
        "channels": item.get("channels"),
    }


def record_ready(
    db_factory,
    project_id,
    d1_input_hash,
    input_hash,
    artifact_values,
    manifest,
    claim_token,
    now=None,
):
    normalized = [_artifact(item) for item in artifact_values]
    kinds = {item["kind"] for item in normalized}
    if not REQUIRED_ARTIFACT_KINDS.issubset(kinds):
        raise ValueError("D-2 产物包不完整")
    if any(
        item["kind"] == "shot_voice" and not item["shot_id"]
        for item in normalized
    ):
        raise ValueError("D-2 镜头配音产物缺少镜头标识")
    now = int(time.time()) if now is None else int(now)
    manifest_json = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM short_drama_assembly_builds "
            "WHERE project_id=? AND input_hash=?",
            (project_id, input_hash),
        ).fetchone()
        if (
            not row
            or row["d1_input_hash"] != d1_input_hash
            or row["status"] != "building"
            or row["claim_token"] != claim_token
        ):
            conn.rollback()
            raise ValueError("D-2 构建状态不允许登记产物")
        conn.execute(
            "DELETE FROM short_drama_assembly_artifacts "
            "WHERE project_id=? AND input_hash=? AND status='stale'",
            (project_id, input_hash),
        )
        for item in normalized:
            conn.execute(
                "INSERT INTO short_drama_assembly_artifacts "
                "(project_id,input_hash,kind,shot_id,file,file_hash,duration_ms,"
                "sample_rate,channels,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'ready',?)",
                (
                    project_id, input_hash, item["kind"], item["shot_id"],
                    item["file"], item["file_hash"], item["duration_ms"],
                    item["sample_rate"], item["channels"], now,
                ),
            )
        conn.execute(
            "UPDATE short_drama_assembly_builds "
            "SET status='ready',manifest_json=?,error_code='',updated_at=? "
            "WHERE project_id=? AND input_hash=?",
            (manifest_json, now, project_id, input_hash),
        )
        conn.commit()


def mark_failed(
    db_factory, project_id, input_hash, error_code, claim_token, now=None
):
    now = int(time.time()) if now is None else int(now)
    with closing(db_factory()) as conn:
        conn.execute(
            "UPDATE short_drama_assembly_builds "
            "SET status='failed',error_code=?,updated_at=? "
            "WHERE project_id=? AND input_hash=? AND status='building' "
            "AND claim_token=?",
            (
                str(error_code or "")[:80], now, project_id, input_hash,
                claim_token,
            ),
        )
        conn.commit()


def build_snapshot_from_conn(conn, project_id, d1_input_hash, input_hash):
    if not input_hash:
        return {
            "engine_version": ENGINE_VERSION,
            "input_hash": None,
            "status": "not_built",
            "error_code": "",
            "artifacts": [],
        }
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status,error_code,manifest_json FROM "
        "short_drama_assembly_builds WHERE project_id=? AND input_hash=? "
        "AND d1_input_hash=?",
        (project_id, input_hash, d1_input_hash),
    ).fetchone()
    if not row:
        return {
            "engine_version": ENGINE_VERSION,
            "input_hash": input_hash,
            "status": "not_built",
            "error_code": "",
            "artifacts": [],
        }
    items = [
        {
            "kind": item["kind"],
            "shot_id": item["shot_id"] or None,
            "file_hash": item["file_hash"],
            "duration_ms": item["duration_ms"],
            "sample_rate": item["sample_rate"],
            "channels": item["channels"],
            "status": item["status"],
        }
        for item in conn.execute(
            "SELECT kind,shot_id,file_hash,duration_ms,sample_rate,"
            "channels,status FROM short_drama_assembly_artifacts "
            "WHERE project_id=? AND input_hash=? ORDER BY kind,shot_id",
            (project_id, input_hash),
        )
    ]
    try:
        manifest = json.loads(row["manifest_json"])
    except (TypeError, ValueError):
        manifest = {}
    return {
        "engine_version": ENGINE_VERSION,
        "input_hash": input_hash,
        "status": row["status"],
        "error_code": row["error_code"],
        "artifacts": items,
        "manifest": manifest if isinstance(manifest, dict) else {},
    }


def build_snapshot(db_factory, project_id, d1_input_hash, input_hash):
    with closing(db_factory()) as conn:
        return build_snapshot_from_conn(
            conn, project_id, d1_input_hash, input_hash
        )


def ready_files(db_factory, project_id, input_hash):
    """Return trusted relative paths for one completed immutable D-2 bundle."""
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        build = conn.execute(
            "SELECT status FROM short_drama_assembly_builds "
            "WHERE project_id=? AND input_hash=?",
            (project_id, input_hash),
        ).fetchone()
        if not build or build["status"] != "ready":
            return {}
        return {
            (row["kind"], row["shot_id"] or ""): row["file"]
            for row in conn.execute(
                "SELECT kind,shot_id,file FROM short_drama_assembly_artifacts "
                "WHERE project_id=? AND input_hash=? AND status='ready'",
                (project_id, input_hash),
            )
        }


def reusable_audio_files(db_factory, project_id, master_audio_hash):
    """Find a ready audio bundle and preserve every expected file hash."""
    if not master_audio_hash:
        return {"source_input_hash": None, "files": {}}
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT input_hash,manifest_json FROM short_drama_assembly_builds "
            "WHERE project_id=? AND status='ready' ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()
        matched_hash = None
        for row in rows:
            try:
                manifest = json.loads(row["manifest_json"] or "{}")
            except (TypeError, ValueError):
                continue
            master = manifest.get("master_audio")
            if (
                isinstance(master, dict)
                and master.get("master_audio_hash") == master_audio_hash
            ):
                matched_hash = row["input_hash"]
                break
        if not matched_hash:
            return {"source_input_hash": None, "files": {}}
        files = {
            (row["kind"], row["shot_id"] or ""): {
                "file": row["file"],
                "file_hash": row["file_hash"],
                "duration_ms": row["duration_ms"],
                "sample_rate": row["sample_rate"],
                "channels": row["channels"],
            }
            for row in conn.execute(
                "SELECT kind,shot_id,file,file_hash,duration_ms,sample_rate,"
                "channels FROM short_drama_assembly_artifacts "
                "WHERE project_id=? AND input_hash=? AND status='ready' "
                "AND kind IN ('shot_voice','dialogue','bgm','master_audio')",
                (project_id, matched_hash),
            )
        }
        return {"source_input_hash": matched_hash, "files": files}


def mark_reusable_audio_stale(
    db_factory, project_id, source_input_hash, error_code
):
    """Atomically retire one corrupt source bundle without touching its consumer."""
    if not source_input_hash:
        return False
    with closing(db_factory()) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE short_drama_assembly_builds "
            "SET status='stale',error_code=?,updated_at=? "
            "WHERE project_id=? AND input_hash=? AND status='ready'",
            (
                str(error_code or "audio_cache_hash_mismatch")[:80],
                int(time.time()),
                project_id,
                source_input_hash,
            ),
        )
        if cursor.rowcount:
            conn.execute(
                "UPDATE short_drama_assembly_artifacts SET status='stale' "
                "WHERE project_id=? AND input_hash=? AND status='ready'",
                (project_id, source_input_hash),
            )
        conn.commit()
        return bool(cursor.rowcount)
