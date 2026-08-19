"""Standalone short-drama automatic draft production (PR-4).

Consumes a confirmed PR-3 plan and records a durable paid attempt. A fixed
sample video may only be used when the explicit demo switch is enabled; it is
never represented as a real project result.
"""

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing, nullcontext
from pathlib import Path

from providers.short_drama_visual import capability_snapshot
from providers.short_drama_visual.heygen_cinematic import (
    HeyGenCinematicShotProvider,
)
from providers.short_drama_visual.runtime import load_by_name, load_from_environment
from providers.short_drama_visual.base import VisualProviderError

from . import jobs_store, points as points_domain
from . import short_drama_assembly_plan as media_plan
from . import short_drama_asset_graph, short_drama_duration, short_drama_native_audio


ACTIVE = {"queued", "running"}
PHASES = (
    ("queued", 5), ("assets", 20), ("visuals", 45),
    ("audio_video", 70), ("finishing", 90), ("completed", 100),
)
FALLBACK_URL = "/assets/meiye_video.mp4"
PROVIDER_QUOTE_TTL_SECONDS = 300
PROVIDER_ACTIVE = {"billing", "queued", "submitting", "running", "submit_unknown"}
PROVIDER_BILLING_OBSERVE_AFTER_SECONDS = 300
PROVIDER_BILLING_CONFIRM_SECONDS = 60


def _positive_env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return int(default)


PROVIDER_SHOT_DEADLINE_SECONDS = _positive_env_int(
    "HQ_SHORT_DRAMA_PROVIDER_SHOT_DEADLINE_SECONDS", 1800
)
PROVIDER_SHOT_MAX_POLLS = _positive_env_int(
    "HQ_SHORT_DRAMA_PROVIDER_SHOT_MAX_POLLS", 720
)


class AutodraftError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_autodraft_attempts (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  plan_id TEXT NOT NULL,
  cost INTEGER NOT NULL DEFAULT 0,
  charge_key TEXT NOT NULL,
  refund_key TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN
    ('accepted','charged','linked','refund_pending','refunded','failed')),
  job_id TEXT,
  error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(actor_username, idempotency_key)
);
CREATE TABLE IF NOT EXISTS short_drama_autodraft_jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  owner_username TEXT NOT NULL,
  actor_username TEXT NOT NULL,
  plan_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('queued','running','succeeded','degraded','failed','canceled')),
  phase TEXT NOT NULL,
  progress INTEGER NOT NULL DEFAULT 0,
  poll_count INTEGER NOT NULL DEFAULT 0,
  input_hash TEXT NOT NULL,
  request_json TEXT NOT NULL,
  result_json TEXT,
  error_json TEXT,
  cost INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_short_drama_autodraft_active
  ON short_drama_autodraft_jobs(project_id) WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS idx_short_drama_autodraft_jobs_project
  ON short_drama_autodraft_jobs(project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS short_drama_autodraft_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL UNIQUE,
  version INTEGER NOT NULL,
  plan_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ready','degraded')),
  url TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS short_drama_provider_shot_quotes (
  token TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  plan_id TEXT NOT NULL,
  shot_key TEXT NOT NULL,
  character_key TEXT NOT NULL,
  avatar_id TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  request_json TEXT NOT NULL,
  cost INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  consumed_job_id TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_provider_quotes_project
  ON short_drama_provider_shot_quotes(project_id, shot_key, created_at DESC);
CREATE TABLE IF NOT EXISTS short_drama_provider_shot_attempts (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  quote_token TEXT NOT NULL REFERENCES short_drama_provider_shot_quotes(token),
  cost INTEGER NOT NULL,
  charge_key TEXT NOT NULL,
  refund_key TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN
    ('accepted','charged','linked','done','refund_pending','refunded','failed')),
  job_id TEXT,
  error_json TEXT,
  refund_retry_count INTEGER NOT NULL DEFAULT 0,
  refund_retry_at INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(actor_username, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_provider_attempts_project
  ON short_drama_provider_shot_attempts(project_id, state, updated_at);
CREATE TABLE IF NOT EXISTS short_drama_provider_shot_jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  owner_username TEXT NOT NULL,
  actor_username TEXT NOT NULL,
  plan_id TEXT NOT NULL,
  shot_key TEXT NOT NULL,
  character_key TEXT NOT NULL,
  avatar_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_job_id TEXT,
  finalizing_token TEXT,
  finalizing_at INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK(status IN
    ('billing','queued','submitting','running','succeeded','failed',
     'canceled','submit_unknown')),
  progress INTEGER NOT NULL DEFAULT 0,
  poll_count INTEGER NOT NULL DEFAULT 0,
  input_hash TEXT NOT NULL,
  request_json TEXT NOT NULL,
  result_json TEXT,
  error_json TEXT,
  cost INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_short_drama_provider_shot_active
  ON short_drama_provider_shot_jobs(project_id, shot_key)
  WHERE status IN ('billing','queued','submitting','running','submit_unknown');
CREATE INDEX IF NOT EXISTS idx_short_drama_provider_shot_jobs_project
  ON short_drama_provider_shot_jobs(project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS short_drama_provider_shot_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL UNIQUE REFERENCES short_drama_provider_shot_jobs(id),
  shot_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  provider TEXT NOT NULL,
  provider_job_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ready')),
  file TEXT NOT NULL,
  url TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(project_id, shot_key, version)
);
CREATE TABLE IF NOT EXISTS short_drama_provider_shot_execution_overrides (
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_key TEXT NOT NULL,
  execution_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(project_id, shot_key)
);
CREATE TABLE IF NOT EXISTS short_drama_provider_shot_selections (
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_key TEXT NOT NULL,
  version_id TEXT NOT NULL REFERENCES short_drama_provider_shot_versions(id) ON DELETE CASCADE,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(project_id, shot_key)
);
"""


def _connection(db_factory):
    conn = db_factory()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _read_connection(db_factory, connection=None):
    if connection is not None:
        return nullcontext(connection)
    return closing(_connection(db_factory))


def init_db(db_factory):
    conn = _connection(db_factory)
    try:
        conn.executescript(_SCHEMA)
        columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(short_drama_provider_shot_attempts)"
            ).fetchall()
        }
        if "refund_retry_count" not in columns:
            conn.execute(
                "ALTER TABLE short_drama_provider_shot_attempts "
                "ADD COLUMN refund_retry_count INTEGER NOT NULL DEFAULT 0"
            )
        if "refund_retry_at" not in columns:
            conn.execute(
                "ALTER TABLE short_drama_provider_shot_attempts "
                "ADD COLUMN refund_retry_at INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_short_drama_provider_refunds_due "
            "ON short_drama_provider_shot_attempts"
            "(state,refund_retry_at,updated_at)"
        )
        job_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(short_drama_provider_shot_jobs)"
            ).fetchall()
        }
        if "finalizing_token" not in job_columns:
            conn.execute(
                "ALTER TABLE short_drama_provider_shot_jobs "
                "ADD COLUMN finalizing_token TEXT"
            )
        if "finalizing_at" not in job_columns:
            conn.execute(
                "ALTER TABLE short_drama_provider_shot_jobs "
                "ADD COLUMN finalizing_at INTEGER NOT NULL DEFAULT 0"
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


def _charge_ledger_matches(actor_username, cost, ledger):
    if not isinstance(ledger, dict):
        return False
    try:
        return (
            str(ledger.get("username") or "") == str(actor_username)
            and int(ledger.get("delta") or 0) == -int(cost)
        )
    except (TypeError, ValueError):
        return False


def _key(value):
    value = str(value or "").strip()
    if not value or len(value) > 160:
        raise AutodraftError("idempotency_key_required", "缺少有效的幂等键")
    return value


def _project(conn, owner_username, project_id):
    row = conn.execute(
        "SELECT id,title,ratio,visual_style,target_duration,shot_count,point_budget,spent_points "
        "FROM short_drama_projects WHERE id=? AND username=? AND deleted=0",
        (project_id, owner_username),
    ).fetchone()
    if not row:
        raise LookupError("short drama project does not exist")
    return dict(row)


def _confirmed_plan(conn, project_id, plan_id=None):
    if plan_id:
        row = conn.execute(
            "SELECT * FROM short_drama_production_plans "
            "WHERE id=? AND project_id=? AND status='confirmed'",
            (plan_id, project_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM short_drama_production_plans "
            "WHERE project_id=? AND status='confirmed' ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
    if not row:
        raise AutodraftError(
            "confirmed_plan_required", "请先确认制作方案，再生成自动草稿", 409
        )
    item = dict(row)
    item["plan"] = _json(item.pop("plan_json"), {})
    return item


def _cost(plan):
    if os.getenv("HQ_SHORT_DRAMA_AUTODRAFT_DEV_FREE") == "1":
        return 0
    return max(0, int((plan.get("estimate") or {}).get("points") or 0))


def _demo_fallback_enabled():
    return os.getenv("HQ_SHORT_DRAMA_AUTODRAFT_DEMO_FALLBACK") == "1"


def _production_capability():
    if _demo_fallback_enabled():
        return {
            "ready": True,
            "mode": "demo",
            "message": "当前仅启用显式演示素材，不会作为真实项目成片交付。",
        }
    provider = capability_snapshot()
    return {
        "ready": False,
        "mode": "provider_poc" if provider["configured"] else "unavailable",
        "provider_poc_ready": bool(provider["configured"]),
        "single_shot_executor_ready": bool(provider["configured"]),
        "provider": provider,
        "message": (
            "真实画面 Provider 已配置；可按镜头报价、确认扣点并异步生成。"
            if provider["configured"]
            else provider["message"]
        ),
    }


def _provider_assembly_snapshot(conn, project_id, plan, provider_name=""):
    """Return one immutable latest ready Provider asset for every planned shot."""
    required = [
        str(item.get("shot_key") or "shot_%02d" % (index + 1))
        for index, item in enumerate(plan.get("material_plan") or [])
        if isinstance(item, dict)
    ]
    latest = {}
    selected = {
        str(row["shot_key"]): str(row["version_id"])
        for row in conn.execute(
            "SELECT shot_key,version_id FROM short_drama_provider_shot_selections "
            "WHERE project_id=?", (project_id,),
        ).fetchall()
    }
    for row in conn.execute(
        "SELECT v.*,j.request_json,j.result_json "
        "FROM short_drama_provider_shot_versions v "
        "JOIN short_drama_provider_shot_jobs j ON j.id=v.job_id "
        "WHERE v.project_id=? AND v.status='ready' "
        "ORDER BY v.shot_key,v.version DESC,v.created_at DESC",
        (project_id,),
    ).fetchall():
        item = _provider_version(row)
        if provider_name and item.get("provider") != provider_name:
            continue
        key = str(item["shot_key"])
        if selected.get(key) == str(item["id"]):
            latest[key] = item
        else:
            latest.setdefault(key, item)
    shots = [latest[key] for key in required if key in latest]
    assets_ready = bool(required) and len(shots) == len(required)
    low_resolution_shot_keys = []
    if provider_name == "minimax_h3":
        low_resolution_shot_keys = [
            str(item["shot_key"])
            for item in shots
            if not item.get("native_media")
        ]
    quality_ready = not low_resolution_shot_keys
    continuity_ready = []
    continuity_missing = []
    if len(required) > 1:
        jobs = {
            str(row["id"]): _json(row["request_json"], {})
            for row in conn.execute(
                "SELECT id,request_json FROM short_drama_provider_shot_jobs "
                "WHERE project_id=?", (project_id,),
            ).fetchall()
        }
        for key in required[1:]:
            version = latest.get(key)
            request = jobs.get(str((version or {}).get("job_id") or ""), {})
            references = request.get("reference_images") or []
            inherited = any(
                isinstance(item, dict)
                and str(item.get("character_key") or "") == "__continuity_tail__"
                for item in references
            )
            (continuity_ready if inherited else continuity_missing).append(key)
    return {
        "required_shot_keys": required,
        "ready_shot_keys": [str(item["shot_key"]) for item in shots],
        "required_count": len(required),
        "ready_count": len(shots),
        "missing_shot_keys": [key for key in required if key not in latest],
        "assets_ready": assets_ready,
        "quality_ready": quality_ready,
        "low_resolution_shot_keys": low_resolution_shot_keys,
        "all_ready": assets_ready and quality_ready,
        "continuity_required_count": max(0, len(required) - 1),
        "continuity_ready_count": len(continuity_ready),
        "continuity_ready_shot_keys": continuity_ready,
        "continuity_missing_shot_keys": continuity_missing,
        "continuity_ready": (
            len(required) <= 1 or len(continuity_ready) == len(required) - 1
        ),
        "shots": shots,
        "duration_ms": sum(
            max(0, int((item.get("request_snapshot") or {}).get(
                "timeline_duration_seconds"
            ) or (item.get("request_snapshot") or {}).get("duration_seconds") or 0)) * 1000
            for item in shots
        ),
    }


def _sanitized_native_audio(value):
    if not isinstance(value, dict):
        return {}
    result = {
        "audible": value.get("audible") is True,
        "codec": str(value.get("codec") or "")[:32],
    }
    for key in ("sample_rate", "channels"):
        try:
            result[key] = max(0, int(value.get(key) or 0))
        except (TypeError, ValueError):
            result[key] = 0
    for key in ("mean_volume_dbfs", "max_volume_dbfs"):
        try:
            number = float(value.get(key))
        except (TypeError, ValueError):
            number = None
        result[key] = number if number is not None and math.isfinite(number) else None
    return result


def _sanitized_native_media(value):
    if not isinstance(value, dict):
        return {}

    def controlled_file(raw):
        text = str(raw or "").strip().replace("\\", "/")
        parts = Path(text).parts
        return text if len(parts) == 2 and parts[0] == "video" else ""

    def sha256(raw):
        text = str(raw or "").strip()
        return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""

    raw = value.get("raw") if isinstance(value.get("raw"), dict) else {}
    derived = (
        value.get("derived") if isinstance(value.get("derived"), dict) else {}
    )
    raw_file = controlled_file(raw.get("file"))
    derived_file = controlled_file(derived.get("file"))
    raw_hash = sha256(raw.get("sha256"))
    derived_hash = sha256(derived.get("sha256"))
    lineage_hash = sha256(derived.get("derived_from_sha256"))
    try:
        raw_size = int(raw.get("size_bytes") or 0)
        derived_size = int(derived.get("size_bytes") or 0)
        inspected_at = int(value.get("inspected_at") or 0)
        width = int((value.get("resolution") or {}).get("width") or 0)
        height = int((value.get("resolution") or {}).get("height") or 0)
    except (TypeError, ValueError, AttributeError):
        return {}
    audio = _sanitized_native_audio(value.get("audio"))
    if (
        not raw_file or not derived_file or not raw_hash or not derived_hash
        or lineage_hash != raw_hash or raw_size <= 0 or derived_size <= 0
        or inspected_at <= 0 or max(width, height) < 2500
        or min(width, height) < 1400 or audio.get("audible") is not True
    ):
        return {}
    return {
        "raw": {
            "file": raw_file, "sha256": raw_hash, "size_bytes": raw_size,
        },
        "derived": {
            "file": derived_file, "sha256": derived_hash,
            "size_bytes": derived_size, "derived_from_sha256": lineage_hash,
        },
        "resolution": {"width": width, "height": height},
        "audio": audio,
        "inspected_at": inspected_at,
    }


def _automatic_native_audio_contract(conn, project, project_shot_count):
    if project_shot_count <= 0:
        return None
    plan_row = conn.execute(
        "SELECT plan_json FROM short_drama_production_plans "
        "WHERE project_id=? AND status='confirmed' "
        "ORDER BY version DESC LIMIT 1",
        (project["id"],),
    ).fetchone()
    plan = _json(plan_row["plan_json"], {}) if plan_row else {}
    required_shot_keys = [
        str(item.get("shot_key") or "shot_%02d" % (index + 1))
        for index, item in enumerate(plan.get("material_plan") or [])
        if isinstance(item, dict)
    ]
    if len(required_shot_keys) != project_shot_count:
        return None
    rows = conn.execute(
        "SELECT v.*,j.result_json,"
        "CASE WHEN s.version_id=v.id THEN 1 ELSE 0 END selected "
        "FROM short_drama_provider_shot_versions v "
        "JOIN short_drama_provider_shot_jobs j ON j.id=v.job_id "
        "LEFT JOIN short_drama_provider_shot_selections s "
        "ON s.project_id=v.project_id AND s.shot_key=v.shot_key "
        "WHERE v.project_id=? AND v.status='ready' "
        "ORDER BY v.shot_key,selected DESC,v.version DESC,v.created_at DESC",
        (project["id"],),
    ).fetchall()
    effective = {}
    for row in rows:
        item = _provider_version(row)
        effective.setdefault(str(item["shot_key"]), item)
    if set(effective) != set(required_shot_keys):
        return None
    versions = list(effective.values())
    if (
        len(versions) != project_shot_count
        or any(item.get("provider") != "minimax_h3" for item in versions)
    ):
        return None
    invalid_shot_keys = [
        str(item["shot_key"]) for item in versions
        if (item.get("native_audio") or {}).get("audible") is not True
        or not item.get("native_media")
    ]
    if invalid_shot_keys:
        return {
            "contract_version": "short-drama-locked-media-v1",
            "delivery_eligible": False,
            "reason": "provider_native_audio_incomplete",
            "media_mode": "provider_audio",
            "invalid_shot_keys": sorted(invalid_shot_keys),
            "audio_tracks": [], "subtitles": [],
            "audio_hash": "", "subtitle_hash": "", "timeline_hash": "",
            "subtitle_required": False,
        }
    evidence = [
        {
            "id": item["id"], "shot_key": item["shot_key"],
            "input_hash": item["input_hash"],
            "native_audio": item["native_audio"],
            "raw_sha256": item["native_media"]["raw"]["sha256"],
            "derived_sha256": item["native_media"]["derived"]["sha256"],
        }
        for item in sorted(versions, key=lambda current: str(current["shot_key"]))
    ]
    timeline = {
        "mode": "provider_audio", "project_id": project["id"],
        "version_ids": [item["id"] for item in evidence],
        "duration_ms": short_drama_duration.choose(
            project["target_duration"], project_shot_count,
        ) * 1000,
    }
    return {
        "contract_version": "short-drama-locked-media-v1",
        "evidence_source": "validated_provider_native_audio",
        "delivery_eligible": True, "reason": "",
        "media_mode": "provider_audio", "silent_confirmed": False,
        "confirmed_by": "system", "audio_tracks": [], "subtitles": [],
        "audio_hash": _hash(evidence), "subtitle_hash": _hash([]),
        "timeline_hash": _hash(timeline), "subtitle_required": False,
    }


def _content_root():
    server_dir = Path(__file__).resolve().parents[1]
    return Path(os.environ.get(
        "CONTENT_OUT", str(server_dir / "content_out")
    )).resolve()


def _controlled_provider_file(relative):
    root = _content_root()
    target = (root / str(relative or "")).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise AutodraftError(
            "provider_asset_path_invalid", "Provider 镜头文件路径不安全", 409
        ) from error
    if not target.is_file():
        raise AutodraftError(
            "provider_asset_missing", "Provider 镜头文件不存在，请重新生成该镜头", 409
        )
    return target


def _verified_native_assembly_sources(assembly, snapshot_dir):
    snapshot_root = Path(snapshot_dir)
    snapshot_root.mkdir(parents=True, exist_ok=True)
    sources = []
    for index, item in enumerate(assembly.get("shots") or []):
        shot_key = str(item.get("shot_key") or "shot_%02d" % (index + 1))
        source = _controlled_provider_file(item.get("file"))
        snapshot = snapshot_root / ("source-%03d%s" % (index + 1, source.suffix))
        try:
            with source.open("rb") as reader, snapshot.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
        except OSError as error:
            raise AutodraftError(
                "provider_native_media_unavailable",
                "%s 的采用版本无法建立稳定媒体快照" % shot_key,
                409,
            ) from error
        if str(item.get("provider") or "") == "minimax_h3":
            locked = _sanitized_native_media(item.get("native_media"))
            if not locked:
                raise AutodraftError(
                    "provider_native_media_invalid",
                    "%s 缺少可信的原生媒体证据，请重新生成该镜头" % shot_key,
                    409,
                )
            selected_file = str(item.get("file") or "").replace("\\", "/")
            expected = next(
                (
                    locked[key] for key in ("raw", "derived")
                    if locked[key]["file"] == selected_file
                ),
                None,
            )
            if not expected:
                raise AutodraftError(
                    "provider_native_media_changed",
                    "%s 的采用文件与锁定媒体证据不一致" % shot_key,
                    409,
                )
            try:
                current = short_drama_native_audio.inspect_native_media(
                    snapshot, expected_resolution="2K"
                )
            except short_drama_native_audio.NativeAudioError as error:
                code = (
                    "provider_native_audio_invalid"
                    if "audio" in error.code
                    else "provider_native_media_invalid"
                )
                raise AutodraftError(
                    code, "%s：%s" % (shot_key, str(error)), 409,
                ) from error
            if (
                current["sha256"] != expected["sha256"]
                or int(current["size_bytes"]) != int(expected["size_bytes"])
            ):
                raise AutodraftError(
                    "provider_native_media_changed",
                    "%s 的采用文件已发生变化，请重新生成或重新采用版本" % shot_key,
                    409,
                )
        sources.append(snapshot)
    return sources


def _table_exists(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone())


def _locked_media_contract(conn, project):
    project_shot_count = int(
        project["shot_count"] if "shot_count" in project.keys() else 0
    )
    empty = {
        "contract_version": "short-drama-locked-media-v1",
        "delivery_eligible": False,
        "reason": "locked_voice_timeline_missing",
        "audio_tracks": [], "subtitles": [],
        "audio_hash": "", "subtitle_hash": "", "timeline_hash": "",
        "subtitle_required": False,
    }
    if _table_exists(conn, "short_drama_refinement_media_preferences"):
        preference = conn.execute(
            "SELECT mode,confirmed_by,updated_at FROM "
            "short_drama_refinement_media_preferences WHERE project_id=?",
            (project["id"],),
        ).fetchone()
        if preference and preference["mode"] in {"provider_audio", "silent"}:
            mode = str(preference["mode"])
            timeline = {
                "mode": mode,
                "project_id": project["id"],
                "duration_ms": short_drama_duration.choose(
                    project["target_duration"], project_shot_count,
                ) * 1000,
                "updated_at": int(preference["updated_at"]),
            }
            return {
                "contract_version": "short-drama-locked-media-v1",
                "evidence_source": (
                    "explicit_provider_audio_confirmation"
                    if mode == "provider_audio"
                    else "explicit_silent_confirmation"
                ),
                "delivery_eligible": True, "reason": "",
                "media_mode": mode,
                "silent_confirmed": mode == "silent",
                "confirmed_by": str(preference["confirmed_by"]),
                "audio_tracks": [], "subtitles": [],
                "audio_hash": _hash({"mode": mode}),
                "subtitle_hash": _hash([]),
                "timeline_hash": _hash(timeline),
                "subtitle_required": False,
            }
    native_audio_contract = _automatic_native_audio_contract(
        conn, project, project_shot_count,
    )
    if native_audio_contract:
        return native_audio_contract
    required = {
        "short_drama_shots", "short_drama_voice_shots",
        "short_drama_voice_lines", "short_drama_voice_versions",
    }
    if any(not _table_exists(conn, name) for name in required):
        return empty
    shot_rows = short_drama_asset_graph.current_project_shots(
        conn, project["id"],
    )
    voice_rows = {
        row["shot_id"]: row for row in conn.execute(
            "SELECT shot_id,locked,timeline_revision FROM short_drama_voice_shots "
            "WHERE project_id=?", (project["id"],),
        )
    }
    if not shot_rows or any(
            not voice_rows.get(row["id"])
            or not voice_rows[row["id"]]["locked"] for row in shot_rows):
        return empty
    cursor = 0
    tracks, subtitles, timeline = [], [], []
    for shot in shot_rows:
        voice = voice_rows[shot["id"]]
        shot_start = cursor
        cursor += int(shot["duration"]) * 1000
        lines = conn.execute(
            "SELECT line.*,version.id AS audio_version_id,version.audio_file,"
            "version.duration_ms AS audio_duration_ms,version.input_hash AS audio_hash,"
            "version.status AS audio_status FROM short_drama_voice_lines line "
            "LEFT JOIN short_drama_voice_versions version "
            "ON version.voice_line_id=line.id AND version.version=line.current_version "
            "WHERE line.project_id=? AND line.shot_id=? ORDER BY line.sort_order,line.id",
            (project["id"], shot["id"]),
        ).fetchall()
        for line in lines:
            start_ms = shot_start + int(line["start_ms"] or 0)
            end_ms = shot_start + int(line["end_ms"] or 0)
            if (
                not line["current_version"] or line["audio_status"] != "done"
                or not str(line["audio_file"] or "").strip()
                or not line["audio_duration_ms"]
            ):
                return dict(empty, reason="locked_audio_incomplete")
            tracks.append({
                "line_id": line["id"], "version_id": line["audio_version_id"],
                "file": line["audio_file"], "start_ms": start_ms,
                "duration_ms": int(line["audio_duration_ms"]),
                "input_hash": line["audio_hash"],
            })
            if line["subtitle_visible"]:
                text = str(line["subtitle_text"] or "").strip()
                if not text or end_ms <= start_ms:
                    return dict(empty, reason="locked_subtitle_incomplete")
                subtitles.append({
                    "line_id": line["id"], "start_ms": start_ms,
                    "end_ms": end_ms, "text": text,
                })
        timeline.append({
            "shot_id": shot["id"], "shot_key": shot["shot_key"],
            "timeline_revision": int(voice["timeline_revision"]),
            "start_ms": shot_start, "end_ms": cursor,
        })
    if not tracks:
        return dict(empty, reason="locked_audio_missing")
    return {
        "contract_version": "short-drama-locked-media-v1",
        "evidence_source": "locked_voice_tables",
        "delivery_eligible": True, "reason": "",
        "audio_tracks": tracks, "subtitles": subtitles,
        "audio_hash": _hash(tracks), "subtitle_hash": _hash(subtitles),
        "timeline_hash": _hash(timeline), "subtitle_required": bool(subtitles),
    }


def _srt_time(milliseconds):
    value = max(0, int(milliseconds))
    hours, value = divmod(value, 3600000)
    minutes, value = divmod(value, 60000)
    seconds, millis = divmod(value, 1000)
    return "%02d:%02d:%02d,%03d" % (hours, minutes, seconds, millis)


def _write_subtitles(path, subtitles):
    content = "\n\n".join(
        "%d\n%s --> %s\n%s" % (
            index, _srt_time(item["start_ms"]), _srt_time(item["end_ms"]),
            str(item["text"]).replace("\r", " ").replace("\n", " "),
        )
        for index, item in enumerate(subtitles, 1)
    )
    path.write_text(content + ("\n" if content else ""), encoding="utf-8")


def _stop_preview_process(process):
    """Stop and reap one renderer process, escalating only if necessary."""
    try:
        process.terminate()
    except OSError:
        pass
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        return process.communicate()


def _run_preview_process(command, cancel_event=None, timeout=900):
    """Run FFmpeg while allowing a lease owner to cancel it immediately."""
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as error:
        raise AutodraftError(
            "preview_renderer_unavailable", "1080p 预览合成器不可用", 503
        ) from error
    deadline = time.monotonic() + max(1, int(timeout))
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _stop_preview_process(process)
            raise AutodraftError(
                "preview_render_cancelled", "1080p 预览合成已取消", 409
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_preview_process(process)
            raise AutodraftError(
                "preview_renderer_unavailable", "1080p 预览合成超时", 503
            )
        try:
            stdout, stderr = process.communicate(timeout=min(0.25, remaining))
            return subprocess.CompletedProcess(
                command, process.returncode, stdout, stderr,
            )
        except subprocess.TimeoutExpired:
            continue


def _render_provider_preview(project_id, job_id, assembly, cancel_event=None):
    """Normalize paid shots with their locked audio/subtitle timeline."""
    cancel_event = cancel_event or assembly.get("_cancel_event")
    root = _content_root()
    target_dir = root / "short_drama_autodraft" / project_id / job_id
    temp_dir = target_dir.with_name(".%s.tmp" % target_dir.name)
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        sources = _verified_native_assembly_sources(
            assembly, temp_dir / "sources"
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    if not sources:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise AutodraftError("provider_shots_required", "没有可合成的镜头", 409)
    source_probes = []
    for source in sources:
        try:
            source_probes.append(media_plan.probe_media(source))
        except media_plan.MediaPlanError as error:
            raise AutodraftError(error.code, str(error), 409) from error
    # Generated providers commonly return 6 seconds for a planned 4-5 second
    # shot. Preserve every completed source, especially the final shot, rather
    # than truncating the concatenation to a legacy exact-duration preset.
    source_duration_ms = sum(
        max(1, int(item.get("duration_ms") or 0)) for item in source_probes
    )
    duration_ms = source_duration_ms or int(assembly.get("duration_ms") or 0)
    output = temp_dir / "preview-1080p.mp4"
    ffmpeg = os.environ.get("FFMPEG_BIN", "ffmpeg")
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for source in sources:
        command.extend(["-i", str(source)])
    media = assembly.get("media_contract") or {}
    audio_paths = []
    for track in media.get("audio_tracks") or []:
        audio_path = _controlled_provider_file(track["file"])
        audio_paths.append(audio_path)
        command.extend(["-i", str(audio_path)])
    subtitle_input = None
    if media.get("subtitles"):
        subtitle_input = temp_dir / "locked-subtitles.srt"
        _write_subtitles(subtitle_input, media["subtitles"])
        command.extend(["-f", "srt", "-i", str(subtitle_input)])
    ratio = str(assembly.get("ratio") or "16:9")
    width, height = ((1080, 1920) if ratio == "9:16" else (1920, 1080))
    filters = []
    labels = []
    for index in range(len(sources)):
        filters.append(
            "[%d:v:0]scale=%d:%d:force_original_aspect_ratio=increase:flags=lanczos,"
            "crop=%d:%d,fps=25,setsar=1,"
            "setpts=PTS-STARTPTS[v%d]" % (
                index, width, height, width, height, index,
            )
        )
        labels.append("[v%d]" % index)
    filters.append(
        "%sconcat=n=%d:v=1:a=0[outv]" % ("".join(labels), len(labels))
    )
    if audio_paths:
        audio_labels = []
        for offset, track in enumerate(media["audio_tracks"]):
            input_index = len(sources) + offset
            label = "voice%d" % offset
            delay = max(0, int(track["start_ms"]))
            filters.append(
                "[%d:a:0]aresample=48000,aformat=channel_layouts=stereo,"
                "adelay=%d|%d,asetpts=PTS-STARTPTS[%s]"
                % (input_index, delay, delay, label)
            )
            audio_labels.append("[%s]" % label)
        filters.append(
            "%samix=inputs=%d:duration=longest:dropout_transition=0,"
            "atrim=duration=%.3f,apad=whole_dur=%.3f[outa]" % (
                "".join(audio_labels), len(audio_labels),
                duration_ms / 1000.0, duration_ms / 1000.0,
            )
        )
    else:
        audio_labels = []
        silent_mode = media.get("media_mode") == "silent"
        for index, source in enumerate(sources):
            probe = source_probes[index]
            label = "a%d" % index
            if probe.get("audio") and not silent_mode:
                filters.append(
                    "[%d:a:0]aresample=48000,aformat=channel_layouts=stereo,"
                    "asetpts=PTS-STARTPTS[%s]" % (index, label)
                )
            else:
                filters.append(
                    "anullsrc=r=48000:cl=stereo,atrim=duration=%.3f[%s]"
                    % (probe["duration_ms"] / 1000.0, label)
                )
            audio_labels.append("[%s]" % label)
        filters.append(
            "%sconcat=n=%d:v=0:a=1[outa]" % (
                "".join(audio_labels), len(audio_labels),
            )
        )
    command.extend([
        "-filter_complex", ";".join(filters), "-map", "[outv]", "-map", "[outa]",
    ])
    if subtitle_input:
        command.extend(["-map", "%d:0" % (len(sources) + len(audio_paths))])
    command.extend([
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
    ])
    if subtitle_input:
        command.extend(["-c:s", "mov_text"])
    if duration_ms > 0:
        command.extend(["-t", "%.3f" % (duration_ms / 1000.0)])
    command.extend(["-movflags", "+faststart", str(output)])
    result = _run_preview_process(command, cancel_event=cancel_event, timeout=900)
    if result.returncode != 0 or not output.is_file():
        raise AutodraftError(
            "preview_render_failed",
            str(result.stderr or "1080p 预览合成失败").strip()[-500:], 409,
        )
    try:
        probe = media_plan.probe_media(output)
    except media_plan.MediaPlanError as error:
        raise AutodraftError(error.code, str(error), 409) from error
    actual_width, actual_height = media_plan.dimensions_for_ratio(probe)
    if (actual_width, actual_height) != (width, height) or not probe.get("audio"):
        raise AutodraftError(
            "preview_media_invalid", "1080p 预览的画幅或音频流验证失败", 409
        )
    if duration_ms and abs(int(probe["duration_ms"]) - duration_ms) > 1500:
        raise AutodraftError(
            "preview_duration_invalid", "1080p 预览时长与锁定时间线不一致", 409
        )
    if subtitle_input:
        try:
            subtitle_probe = subprocess.run(
                [
                    os.environ.get("FFPROBE_BIN", "ffprobe"), "-v", "error",
                    "-select_streams", "s", "-show_entries", "stream=index",
                    "-of", "csv=p=0", str(output),
                ], capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AutodraftError(
                "preview_probe_failed", "1080p 预览字幕流验证失败", 409
            ) from error
        if subtitle_probe.returncode != 0 or not subtitle_probe.stdout.strip():
            raise AutodraftError(
                "preview_subtitle_missing", "1080p 预览缺少锁定字幕流", 409
            )
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir.rename(target_dir)
    relative = output.relative_to(temp_dir)
    file_key = (Path("short_drama_autodraft") / project_id / job_id / relative).as_posix()
    return {
        "file": file_key, "url": "/api/gen/file/" + file_key,
        "probe": probe, "duration_ms": int(probe.get("duration_ms") or duration_ms),
        "source_duration_ms": source_duration_ms,
    }


def _locked_character_reference_ready(row, provider_name):
    if not row or not row["reference_locked"]:
        return False
    reference_file = str(row["reference_file"] or "").strip()
    if provider_name == "minimax_h3":
        # MiniMax references are converted from project-owned local bytes.
        # A historical public URL is display-only and cannot be submitted.
        return bool(reference_file)
    reference_url = str(row["reference_url"] or "").strip()
    return bool(
        provider_name == "grok"
        and (
            reference_file
            or reference_url.startswith(("http://", "https://"))
        )
    )


def _provider_poc_inputs(
    plan, owner_username, avatar_list=None, conn=None, project_id="",
):
    provider = load_from_environment() or HeyGenCinematicShotProvider()
    provider_name = provider.name
    shots = []
    for index, shot in enumerate(plan.get("material_plan") or []):
        if not isinstance(shot, dict):
            continue
        shots.append({
            "shot_key": str(shot.get("shot_key") or "shot_%02d" % (index + 1)),
            "sort_order": int(shot.get("sort_order") or index + 1),
            "duration_ms": int(shot.get("duration_ms") or 0),
            "scene": str(shot.get("scene") or ""),
            "character_names": [
                str(value) for value in shot.get("character_names") or []
            ],
        })
    avatars = []
    if callable(avatar_list):
        try:
            candidates = avatar_list(owner_username, 120)
        except Exception:
            candidates = []
        for avatar in candidates or []:
            if not isinstance(avatar, dict):
                continue
            if provider_name == "grok":
                provider_ready = bool(str(avatar.get("image_url") or "").strip())
            elif provider_name == "minimax_h3":
                provider_ready = False
            else:
                provider_ready = bool(
                    str(avatar.get("provider_avatar_id") or "").strip()
                )
            if (
                str(avatar.get("status") or "") != "ready"
                or not provider_ready
            ):
                continue
            avatars.append({
                "id": str(avatar.get("id") or ""),
                "name": str(avatar.get("name") or "未命名形象"),
                "image_url": str(avatar.get("image_url") or ""),
                "status": "ready",
                "provider_bound": True,
            })
    bindings = {}
    characters = []
    if conn is not None and project_id:
        required_character_keys = []
        for material in plan.get("material_plan") or []:
            if not isinstance(material, dict):
                continue
            for dialogue in material.get("dialogue") or []:
                key = str(
                    dialogue.get("character_key")
                    if isinstance(dialogue, dict) else ""
                ).strip()
                if key and key not in required_character_keys:
                    required_character_keys.append(key)
            for value in material.get("character_keys") or []:
                key = str(value or "").strip()
                if key and key not in required_character_keys:
                    required_character_keys.append(key)
        avatar_by_id = {str(item["id"]): item for item in avatars}
        for row in conn.execute(
            "SELECT character_key,name,avatar_id,reference_file,reference_url,"
            "reference_locked "
            "FROM short_drama_characters WHERE project_id=? ORDER BY sort_order,id",
            (project_id,),
        ).fetchall():
            avatar = avatar_by_id.get(str(row["avatar_id"] or ""))
            character_reference_ready = _locked_character_reference_ready(
                row, provider_name,
            )
            if provider_name == "minimax_h3":
                generation_identity_id = (
                    "character:" + str(row["character_key"])
                    if character_reference_ready else ""
                )
            elif avatar:
                generation_identity_id = str(avatar["id"])
            elif provider_name == "grok" and character_reference_ready:
                generation_identity_id = "character:" + str(row["character_key"])
            else:
                generation_identity_id = ""
            item = {
                "character_key": str(row["character_key"]),
                "name": str(row["name"]),
                "avatar_id": str(row["avatar_id"] or ""),
                "image_url": (
                    str(avatar.get("image_url") or "") if avatar
                    else str(row["reference_url"] or "")
                ),
                "binding_ready": bool(generation_identity_id),
                "generation_identity_id": generation_identity_id,
            }
            characters.append(item)
            if generation_identity_id:
                bindings[item["character_key"]] = generation_identity_id
        material_by_key = {
            str(item.get("shot_key") or ""): item
            for item in plan.get("material_plan") or []
            if isinstance(item, dict)
        }
        for shot in shots:
            source = material_by_key.get(shot["shot_key"]) or {}
            override = _execution_override(conn, project_id, shot["shot_key"])
            override_keys = (
                [str(value) for value in override.get("character_keys") or [] if str(value)]
                if override and "character_keys" in override else None
            )
            keys = override_keys if override_keys is not None else [
                str(value) for value in source.get("character_keys") or []
                if str(value)
            ]
            dialogue_keys = [] if override_keys is not None else [
                str(item.get("character_key") or "")
                for item in source.get("dialogue") or []
                if isinstance(item, dict) and str(item.get("character_key") or "")
            ]
            required = []
            for key in dialogue_keys + keys:
                if key not in required:
                    required.append(key)
            shot["character_keys"] = required
            shot["binding_ready"] = bool(
                required and all(bindings.get(key) for key in required)
            )
            shot["primary_character_key"] = required[0] if required else ""
            shot["primary_avatar_id"] = bindings.get(
                shot["primary_character_key"], ""
            )
        ready_shots = {
            str(row["shot_key"])
            for row in conn.execute(
                "SELECT DISTINCT shot_key FROM short_drama_provider_shot_versions "
                "WHERE project_id=? AND status='ready'",
                (project_id,),
            ).fetchall()
        }
        ordered = sorted(shots, key=lambda item: (item["sort_order"], item["shot_key"]))
        for index, shot in enumerate(ordered):
            previous_key = ordered[index - 1]["shot_key"] if index else ""
            shot["previous_shot_key"] = previous_key
            shot["sequence_ready"] = bool(not previous_key or previous_key in ready_shots)
            shot["continuity_mode"] = (
                "previous_shot_tail" if previous_key and previous_key in ready_shots
                else "scene_baseline" if not previous_key
                else "waiting_previous_shot"
            )
    return {
        "provider": provider_name,
        "shots": shots,
        "avatars": avatars,
        "characters": characters,
        "bindings": bindings,
        "all_roles_bound": bool(
            characters and all(item["binding_ready"] for item in characters)
        ),
        "billable": False,
        "external_submission": False,
    }


def _extract_tail_reference(video_file):
    """Create a durable final-frame image beside a completed shot video."""
    relative = str(video_file or "").strip().replace("\\", "/").lstrip("/")
    if not relative:
        return None
    try:
        from .core import _out_path

        source = _out_path(relative)
        if not source.is_file():
            return None
        tail = source.with_name(source.stem + "_continuity_tail.jpg")
        if not tail.is_file() or tail.stat().st_size <= 0:
            subprocess.run(
                [
                    os.environ.get("FFMPEG_BIN", "ffmpeg"), "-y", "-hide_banner",
                    "-loglevel", "error", "-sseof", "-0.08", "-i", str(source),
                    "-frames:v", "1", "-q:v", "2", str(tail),
                ],
                check=True, timeout=45, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        if not tail.is_file() or tail.stat().st_size <= 0:
            return None
        relative_tail = (Path(relative).parent / tail.name).as_posix()
        return {
            "file": relative_tail,
            "url": "/api/gen/file/" + relative_tail,
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _previous_shot_reference(conn, project_id, shots, shot_key):
    ordered = sorted(
        [item for item in shots if isinstance(item, dict)],
        key=lambda item: (
            int(item.get("sort_order") or 0), str(item.get("shot_key") or ""),
        ),
    )
    index = next(
        (position for position, item in enumerate(ordered)
         if str(item.get("shot_key") or "") == shot_key),
        None,
    )
    if index in (None, 0):
        return None
    previous_key = str(ordered[index - 1].get("shot_key") or "")
    row = conn.execute(
        "SELECT version.shot_key,version.file,version.url,job.result_json "
        "FROM short_drama_provider_shot_versions version "
        "JOIN short_drama_provider_shot_jobs job ON job.id=version.job_id "
        "LEFT JOIN short_drama_provider_shot_selections selected "
        "ON selected.project_id=version.project_id "
        "AND selected.shot_key=version.shot_key AND selected.version_id=version.id "
        "WHERE version.project_id=? AND version.shot_key=? AND version.status='ready' "
        "ORDER BY CASE WHEN selected.version_id IS NOT NULL THEN 0 ELSE 1 END, "
        "version.version DESC LIMIT 1",
        (project_id, previous_key),
    ).fetchone()
    if not row:
        raise AutodraftError(
            "provider_previous_shot_required",
            "请先生成上一个镜头，再生成当前镜头，以保持场景和动作连续。",
            409,
        )
    result = _json(row["result_json"], {})
    tail = {
        "file": str(result.get("continuity_tail_file") or "").strip(),
        "url": str(result.get("continuity_tail_url") or "").strip(),
    }
    if not tail["file"] and not tail["url"]:
        tail = _extract_tail_reference(row["file"]) or {}
    return {
        "shot_key": previous_key,
        "file": str(tail.get("file") or "").strip(),
        "url": str(tail.get("url") or "").strip(),
    }


def _character_binding_blockers(conn, project_id, plan, provider_name=""):
    """Require prepared standalone roles without breaking untouched legacy plans."""
    rows = conn.execute(
        "SELECT character_key,name,avatar_id,reference_file,reference_url,"
        "reference_locked FROM short_drama_characters "
        "WHERE project_id=? ORDER BY sort_order,id",
        (project_id,),
    ).fetchall()
    if not rows:
        return []
    bound = {}
    for row in rows:
        character_key = str(row["character_key"])
        if provider_name == "minimax_h3":
            bound[character_key] = _locked_character_reference_ready(
                row, provider_name,
            )
        elif provider_name == "grok":
            bound[character_key] = bool(
                row["avatar_id"]
                or _locked_character_reference_ready(row, provider_name)
            )
        else:
            bound[character_key] = bool(row["avatar_id"])
    names = {
        str(row["character_key"]): str(row["name"])
        for row in rows
    }
    required = []
    for shot in plan.get("material_plan") or []:
        if not isinstance(shot, dict):
            continue
        for dialogue in shot.get("dialogue") or []:
            if not isinstance(dialogue, dict):
                continue
            key = str(dialogue.get("character_key") or "").strip()
            if key and key not in required:
                required.append(key)
        for value in shot.get("character_keys") or []:
            key = str(value or "").strip()
            if key and key not in required:
                required.append(key)
    return [
        {
            "character_key": key,
            "name": names.get(key) or key,
            "code": "character_avatar_unbound",
        }
        for key in required
        if not bound.get(key)
    ]


def _visual_prompt(shot):
    provider_prompt = str(shot.get("provider_prompt") or "").strip()
    if provider_prompt:
        negative_prompt = str(shot.get("negative_prompt") or "").strip()
        if not negative_prompt:
            negative_prompt = "字幕、文字、Logo、水印、改变人物身份"
        return "%s 禁止项：%s。" % (
            provider_prompt.rstrip("。；; "),
            negative_prompt.rstrip("。；; "),
        )

    characters = "、".join(
        str(value).strip()
        for value in shot.get("character_names") or []
        if str(value).strip()
    )
    dialogue = str(shot.get("dialogue_text") or "").strip()
    parts = [
        "电影感写实短剧镜头。",
        "场景：" + (str(shot.get("scene") or "").strip() or "延续上一镜场景。"),
        "剧情动作：" + (str(shot.get("beat") or "").strip() or "自然推进当前剧情。"),
        "镜头语言：" + (str(shot.get("camera") or "").strip() or "稳定电影镜头。"),
        "画面要求：" + (
            str(shot.get("visual_prompt") or "").strip()
            or "严格按照锁定剧本呈现人物、环境和动作。"
        ),
    ]
    if characters:
        parts.append("出镜人物：" + characters + "，保持人物身份和外观一致。")
    if dialogue:
        parts.append("台词语境：" + dialogue)
    parts.append("不要生成字幕、文字、Logo或水印；不要改变人物身份。")
    return " ".join(parts)


def _native_audio_brief(shot, character_names=None):
    names = dict(character_names or {})
    dialogue_lines = []
    for item in shot.get("dialogue") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        character_key = str(item.get("character_key") or "").strip()
        speaker = (
            names.get(character_key)
            or str(item.get("speaker") or character_key or "旁白").strip()
        )
        timing = (
            "（与上一条同时说）"
            if item.get("timing_mode") == "simultaneous_with_previous"
            else ""
        )
        dialogue_lines.append("%s%s：%s" % (speaker, timing, text))
    sound_design = str(shot.get("sound_design") or "").strip()
    parts = ["声音必须由视频模型随画面同步生成，输出可听的原生双声道音频。"]
    if dialogue_lines:
        parts.append("人物台词与顺序：" + "；".join(dialogue_lines))
    else:
        parts.append("本镜头没有人物台词，不要擅自添加对白。")
    parts.append(
        "声音设计：" + sound_design
        if sound_design
        else "声音设计：生成与场景和动作匹配的自然环境声与必要动作音效。"
    )
    return " ".join(parts)


_EXECUTION_LIMITS = {
    "visual": 600, "camera": 300, "performance": 300, "scene": 160,
    "lighting": 240, "composition_style": 240, "continuity": 360,
    "sound_design": 600, "negative_prompt": 600, "provider_prompt": 1600,
}


def _clean_execution(value):
    if not isinstance(value, dict):
        raise AutodraftError("provider_execution_invalid", "镜头生成要求格式不正确", 422)
    result = {}
    for key, limit in _EXECUTION_LIMITS.items():
        text = str(value.get(key) or "").strip()
        if len(text) > limit:
            raise AutodraftError(
                "provider_execution_too_long", "镜头生成要求中的内容过长", 422,
            )
        result[key] = text
    # Character reference images are mandatory for identity consistency. The
    # continuity tail and locked scene image are optional diagnostic inputs:
    # users may temporarily disable either after a provider moderation failure
    # without changing the locked script or deleting any asset.
    result["include_continuity_reference"] = (
        value.get("include_continuity_reference") is not False
    )
    result["include_scene_reference"] = (
        value.get("include_scene_reference") is not False
    )
    result["scene_key"] = str(value.get("scene_key") or "").strip()[:160]
    if "character_keys" in value:
        raw_character_keys = value.get("character_keys")
        if not isinstance(raw_character_keys, list):
            raise AutodraftError(
                "provider_character_selection_invalid",
                "镜头绑定角色格式不正确", 422,
            )
        character_keys = []
        for raw_key in raw_character_keys:
            key = str(raw_key or "").strip()
            if key and key not in character_keys:
                character_keys.append(key)
        if not character_keys:
            raise AutodraftError(
                "provider_character_required", "请至少选择一个出镜角色", 422,
            )
        if len(character_keys) > 4:
            raise AutodraftError(
                "provider_character_limit_exceeded",
                "单个镜头最多绑定四个出镜角色", 422,
            )
        result["character_keys"] = character_keys
    if not result["provider_prompt"]:
        parts = [
            result["visual"], result["camera"], result["performance"],
            result["scene"], result["lighting"], result["composition_style"],
            result["continuity"],
        ]
        result["provider_prompt"] = "；".join(item for item in parts if item)
    if not result["provider_prompt"]:
        raise AutodraftError("provider_prompt_required", "请填写视频生成提示词", 422)
    return result


def _execution_override(conn, project_id, shot_key):
    row = conn.execute(
        "SELECT execution_json,updated_at FROM short_drama_provider_shot_execution_overrides "
        "WHERE project_id=? AND shot_key=?", (project_id, shot_key),
    ).fetchone()
    if not row:
        return None
    result = _json(row["execution_json"], {})
    result["updated_at"] = int(row["updated_at"])
    return result


def _save_execution_override(conn, project_id, shot_key, execution):
    now = int(time.time())
    conn.execute(
        "INSERT INTO short_drama_provider_shot_execution_overrides "
        "(project_id,shot_key,execution_json,updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(project_id,shot_key) DO UPDATE SET "
        "execution_json=excluded.execution_json,updated_at=excluded.updated_at",
        (project_id, shot_key, _json_text(execution), now),
    )
    return dict(execution, updated_at=now)


def preview_provider_request(
    db_factory, owner_username, actor_username, body, avatar_lookup=None,
    include_private=False, connection=None,
):
    """Compile one exact visual-provider request without billing or I/O."""
    project_id = str(body.get("project_id") or "").strip()
    plan_id = str(body.get("plan_id") or "").strip()
    shot_key = str(body.get("shot_key") or "").strip()
    avatar_id = str(body.get("avatar_id") or "").strip()
    character_key = str(body.get("character_key") or "").strip()
    provider = load_from_environment() or HeyGenCinematicShotProvider()
    with _read_connection(db_factory, connection) as conn:
        project = _project(conn, owner_username, project_id)
        plan = _confirmed_plan(conn, project_id, plan_id)
        source_row = conn.execute(
            "SELECT script_json FROM short_drama_script_snapshots "
            "WHERE id=? AND project_id=?",
            (plan["source_script_version_id"], project_id),
        ).fetchone()
        source_script = _json(source_row["script_json"], {}) if source_row else {}
    shots = [
        item for item in plan["plan"].get("material_plan") or []
        if isinstance(item, dict)
    ]
    shot = next(
        (item for item in shots if str(item.get("shot_key") or "") == shot_key),
        None,
    )
    if not shot:
        raise AutodraftError(
            "provider_shot_not_found", "请选择制作计划中的有效镜头", 422
        )
    execution = None
    with _read_connection(db_factory, connection) as conn:
        _project(conn, owner_username, project_id)
        if "execution" in body:
            execution = _clean_execution(body.get("execution"))
            execution = _save_execution_override(
                conn, project_id, shot_key, execution,
            )
            if connection is None:
                conn.commit()
        else:
            execution = _execution_override(conn, project_id, shot_key)
    if execution:
        shot = dict(shot)
        for key in (
            "scene", "camera", "continuity", "negative_prompt",
            "provider_prompt", "sound_design",
        ):
            if str(execution.get(key) or "").strip():
                shot[key] = execution[key]
        visual_parts = [
            str(execution.get("visual") or "").strip(),
            str(execution.get("performance") or "").strip(),
            str(execution.get("lighting") or "").strip(),
            str(execution.get("composition_style") or "").strip(),
        ]
        if any(visual_parts):
            shot["visual_prompt"] = "；".join(item for item in visual_parts if item)
        if "character_keys" in execution:
            shot["character_keys"] = list(execution["character_keys"])
            character_key = execution["character_keys"][0]
    if not str(shot.get("provider_prompt") or "").strip():
        source_shot = next(
            (
                item for item in source_script.get("shots") or []
                if isinstance(item, dict)
                and str(item.get("shot_key") or "") == shot_key
            ),
            None,
        )
        if source_shot:
            shot = dict(shot)
            shot["provider_prompt"] = str(
                source_shot.get("provider_prompt")
                or source_shot.get("visual")
                or shot.get("visual_prompt")
                or ""
            ).strip()
            shot["negative_prompt"] = str(
                source_shot.get("negative_prompt")
                or shot.get("negative_prompt")
                or ""
            ).strip()
    if execution and execution.get("character_keys"):
        character_key = execution["character_keys"][0]
    elif not character_key:
        dialogue = [
            item for item in shot.get("dialogue") or []
            if isinstance(item, dict)
        ]
        character_key = str(
            (dialogue[0].get("character_key") if dialogue else "")
            or ((shot.get("character_keys") or [""])[0])
            or ""
        ).strip()
    required_character_keys = []
    for item in shot.get("dialogue") or []:
        key = str(item.get("character_key") or "").strip() if isinstance(item, dict) else ""
        if key and key not in required_character_keys:
            required_character_keys.append(key)
    for value in shot.get("character_keys") or []:
        key = str(value or "").strip()
        if key and key not in required_character_keys:
            required_character_keys.append(key)
    if character_key and character_key not in required_character_keys:
        required_character_keys.insert(0, character_key)

    reference_images = []
    scene_reference = None
    previous_reference = None
    avatar = None
    character_names = {}
    if provider.name == "minimax_h3":
        if not required_character_keys:
            raise AutodraftError(
                "provider_character_required", "当前镜头没有可用于生成的出镜角色", 422
            )
        with _read_connection(db_factory, connection) as conn:
            scene_reference = short_drama_asset_graph.locked_scene_reference(
                conn, project_id, shot_key,
                execution.get("scene_key") if execution else None,
            )
            previous_reference = _previous_shot_reference(
                conn, project_id, shots, shot_key,
            )
        if execution and execution.get("include_continuity_reference") is False:
            previous_reference = None
        if execution and execution.get("include_scene_reference") is False:
            scene_reference = None
        # Historical optional references can be URL-only. MiniMax now accepts
        # only project-owned local bytes, so omit those optional hints instead
        # of advertising a request that its Provider must reject.
        if previous_reference and not str(
            previous_reference.get("file") or ""
        ).strip():
            previous_reference = None
        if scene_reference and not str(
            scene_reference.get("file") or ""
        ).strip():
            scene_reference = None
        extra_reference_count = int(bool(
            previous_reference
            and (previous_reference.get("file") or previous_reference.get("url"))
        ) or bool(scene_reference))
        maximum_characters = 5 - extra_reference_count
        if len(required_character_keys) > maximum_characters:
            raise AutodraftError(
                "provider_reference_limit_exceeded",
                "当前镜头的角色与场景参考图总数超过视频服务上限",
                422,
            )
        with _read_connection(db_factory, connection) as conn:
            placeholders = ",".join("?" for _ in required_character_keys)
            rows = conn.execute(
                "SELECT character_key,name,reference_file,reference_url,"
                "reference_version,reference_locked "
                "FROM short_drama_characters WHERE project_id=? AND character_key IN ("
                + placeholders + ")",
                tuple([project_id] + required_character_keys),
            ).fetchall()
        by_key = {str(row["character_key"]): row for row in rows}
        character_names = {
            key: str(row["name"] or key) for key, row in by_key.items()
        }
        for key in required_character_keys:
            row = by_key.get(key)
            if not _locked_character_reference_ready(row, provider.name):
                raise AutodraftError(
                    "provider_avatar_not_ready",
                    "请先为镜头中的全部角色确认并锁定标准图",
                    422,
                )
            reference_images.append({
                "character_key": key,
                "name": str(row["name"] or key),
                "file": str(row["reference_file"] or "").strip(),
                "url": str(row["reference_url"] or "").strip(),
                "reference_version": int(row["reference_version"] or 0),
            })
        if previous_reference and (
            previous_reference.get("file") or previous_reference.get("url")
        ):
            reference_images.append({
                "character_key": "__continuity_tail__",
                "continuity_shot_key": previous_reference["shot_key"],
                "name": "上一镜头尾帧",
                "file": previous_reference.get("file") or "",
                "url": previous_reference.get("url") or "",
            })
        elif scene_reference:
            reference_images.append({
                "character_key": "__scene_reference__",
                "scene_key": scene_reference["scene_key"],
                "name": scene_reference["name"],
                "file": scene_reference["file"],
                "url": scene_reference["url"],
            })
        primary = by_key[required_character_keys[0]]
        character_key = required_character_keys[0]
        avatar_id = "character:" + character_key
        avatar = {
            "id": avatar_id,
            "username": owner_username,
            "name": str(primary["name"] or character_key),
            "status": "ready",
            "image_file": str(primary["reference_file"] or ""),
            "image_url": str(primary["reference_url"] or ""),
        }
    if avatar is None and not avatar_id and character_key:
        with _read_connection(db_factory, connection) as conn:
            row = conn.execute(
                "SELECT avatar_id,reference_file,reference_url,reference_locked "
                "FROM short_drama_characters "
                "WHERE project_id=? AND character_key=?",
                (project_id, character_key),
            ).fetchone()
            avatar_id = str(row["avatar_id"] or "") if row else ""
            if (
                provider.name == "grok"
                and row
                and not avatar_id
                and row["reference_locked"]
                and (
                    str(row["reference_file"] or "").strip()
                    or str(row["reference_url"] or "").strip().startswith(
                        ("http://", "https://")
                    )
                )
            ):
                avatar_id = "character:" + character_key
    if avatar is None and not avatar_id:
        raise AutodraftError(
            "provider_avatar_required", "请先为当前角色锁定一张标准形象图", 422
        )
    if avatar is None and provider.name == "grok" and avatar_id == "character:" + character_key:
        with _read_connection(db_factory, connection) as conn:
            reference = conn.execute(
                "SELECT name,reference_file,reference_url,reference_locked "
                "FROM short_drama_characters "
                "WHERE project_id=? AND character_key=?",
                (project_id, character_key),
            ).fetchone()
        if not reference:
            raise AutodraftError(
                "provider_avatar_not_found", "当前角色的标准形象图不存在", 422
            )
        avatar = {
            "id": avatar_id,
            "username": owner_username,
            "name": str(reference["name"] or "未命名角色"),
            "status": "ready" if reference["reference_locked"] else "pending",
            "image_file": str(reference["reference_file"] or ""),
            "image_url": str(reference["reference_url"] or ""),
        }
    elif avatar is None:
        if not callable(avatar_lookup):
            raise AutodraftError(
                "provider_avatar_lookup_unavailable", "形象库服务暂不可用", 503
            )
        try:
            avatar = avatar_lookup(owner_username, avatar_id)
        except Exception as error:
            raise AutodraftError(
                "provider_avatar_not_found", "所选电影化身不存在或已不可用", 422
            ) from error
    if (
        not isinstance(avatar, dict)
        or str(avatar.get("username") or "") != owner_username
    ):
        raise AutodraftError(
            "provider_avatar_forbidden", "无权使用所选电影化身", 403
        )
    reference_image_url = str(avatar.get("image_url") or "").strip()
    reference_image_file = str(avatar.get("image_file") or "").strip()
    provider_identity_ready = bool(
        reference_images
        if provider.name == "minimax_h3"
        else reference_image_url or reference_image_file
        if provider.name == "grok"
        else str(avatar.get("provider_avatar_id") or "").strip()
    )
    if str(avatar.get("status") or "") != "ready" or not provider_identity_ready:
        raise AutodraftError(
            "provider_avatar_not_ready", "所选电影化身缺少当前 Provider 所需的形象资产", 422
        )
    duration_ms = int(shot.get("duration_ms") or 0)
    timeline_duration_seconds = max(1, (duration_ms + 999) // 1000)
    # MiniMax accepts 4-15 second source clips. Legacy locked scripts can
    # contain shorter shots; generate the minimum supported clip and let the
    # assembly timeline trim it back to the authored duration. This preserves
    # the locked script and any already-generated neighbouring shots.
    duration_seconds = (
        max(4, min(15, timeline_duration_seconds))
        if provider.name == "minimax_h3"
        else timeline_duration_seconds
    )
    prompt = _visual_prompt(shot)
    if provider.name == "minimax_h3":
        prompt += " " + _native_audio_brief(shot, character_names)
    speech_rates = [
        float(item.get("speech_rate") or 1.0)
        for item in shot.get("dialogue") or []
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    if speech_rates:
        speech_rate = max(speech_rates)
        prompt += (
            " 台词语速要求：%.2g倍语速，保持吐字清楚、情绪自然并尽量匹配口型。"
            % speech_rate
        )
    prompt += (
        " 全片统一视觉基线：画面比例%s，视觉风格%s；保持人物脸部、发型、年龄、"
        "服装，道具外观，主光方向、色温和场景空间布局跨镜头一致。"
        % (
            str(project.get("ratio") or "16:9"),
            str(project.get("visual_style") or "电影感写实"),
        )
    )
    if previous_reference:
        prompt += (
            " 本镜头必须直接承接上一镜头%s结束时的人物站位、朝向、动作结果、"
            "道具位置、背景结构和光线；只推进新动作，不重置场景。"
            % previous_reference["shot_key"]
        )
    if scene_reference:
        prompt += (
            " 场景环境必须与锁定场景参考图保持一致，包括空间布局、背景物体、"
            "光线和色调；场景参考图只用于环境，不要把图中可能出现的人物复制到视频。"
        )
    outbound = {
        "provider_avatar_id": str(avatar.get("provider_avatar_id") or ""),
        "reference_image_url": reference_image_url,
        "reference_image_file": reference_image_file,
        "reference_images": reference_images,
        "prompt": prompt,
        "ratio": str(project.get("ratio") or "16:9"),
        "resolution": (
            "2k" if provider.name == "minimax_h3" else str(
                (plan["plan"].get("estimate") or {}).get("resolution") or "720p"
            ).lower()
        ),
        "duration_seconds": duration_seconds,
    }
    try:
        validated = provider.validate_request(outbound)
    except Exception as error:
        raise AutodraftError(
            getattr(error, "code", "provider_request_invalid"),
            str(error),
            422,
        ) from error
    capability = capability_snapshot()
    request_hash = _hash({
        "project_id": project_id,
        "plan_id": plan["id"],
        "plan_hash": plan["input_hash"],
        "shot_key": shot_key,
        "avatar_id": avatar_id,
        "provider": provider.name,
        "request": validated,
    })
    result = {
        "contract_version": "short-drama-provider-preflight-v1",
        "ready": bool(
            capability.get("selected") == provider.name
            and capability.get("configured")
        ),
        "provider": provider.name,
        "provider_configured": bool(capability.get("configured")),
        "provider_status": capability.get("code"),
        "project_id": project_id,
        "plan_id": plan["id"],
        "shot": {
            "shot_key": shot_key,
            "sort_order": int(shot.get("sort_order") or 0),
            "scene": str(shot.get("scene") or ""),
        },
        "avatar": {
            "id": avatar_id,
            "name": str(avatar.get("name") or "未命名形象"),
            "provider_bound": True,
        },
        "character_key": character_key,
        "character_keys": required_character_keys,
        "scene_reference": ({
            "locked": True, "name": scene_reference["name"],
            "scene_key": scene_reference["scene_key"],
        } if scene_reference else {"locked": False}),
        "continuity_reference": ({
            "ready": bool(previous_reference.get("file") or previous_reference.get("url")),
            "previous_shot_key": previous_reference["shot_key"],
            "mode": (
                "previous_shot_tail"
                if previous_reference.get("file") or previous_reference.get("url")
                else "text_and_scene_fallback"
            ),
        } if previous_reference else {
            "ready": True, "previous_shot_key": "", "mode": "scene_baseline",
        }),
        "request": {
            "prompt": validated["prompt"],
            "ratio": validated["ratio"],
            "resolution": validated["resolution"],
            "duration_seconds": validated["duration_seconds"],
            "timeline_duration_seconds": timeline_duration_seconds,
            "assembly_trim_required": (
                validated["duration_seconds"] != timeline_duration_seconds
            ),
            "reference_count": len(reference_images) if reference_images else 1,
            "reference_inputs": [
                {
                    "type": (
                        "continuity"
                        if item.get("character_key") == "__continuity_tail__"
                        else "scene"
                        if item.get("character_key") == "__scene_reference__"
                        else "character"
                    ),
                    "name": str(item.get("name") or "参考图"),
                    "required": not str(item.get("character_key") or "").startswith("__"),
                }
                for item in reference_images
            ],
            "provider_avatar": "[已绑定]",
        },
        "execution": execution,
        "request_hash": request_hash,
        "billable": False,
        "external_submission": False,
        "next_action": (
            "可进入单镜头付费确认"
            if capability.get("configured")
            else "配置短剧画面 Provider 及其 API Key"
        ),
        "message": (
            "预检通过；本次没有调用 Provider，也没有扣点。"
            if capability.get("configured")
            else "镜头请求已编译通过，但 Provider 尚未配置；本次没有外部调用。"
        ),
    }
    if validated.get("model"):
        result["request"]["model"] = str(validated["model"])
    if include_private:
        result["_provider_request"] = validated
    return result


def _provider_shot_cost(provider_request):
    request = provider_request if isinstance(provider_request, dict) else {}
    provider_name = str(request.get("provider") or "").strip()
    if provider_name == "heygen_cinematic":
        return points_domain.cost_of("cinematic", {
            "cine_mode": "open",
            "duration": int(request.get("duration_seconds") or 0),
        })
    if provider_name == "minimax_h3":
        duration = int(request.get("duration_seconds") or 0)
        if duration <= 0:
            raise AutodraftError(
                "provider_quote_request_invalid",
                "麦克视频规范化请求缺少必要计费参数",
                500,
            )
        return points_domain.cost_of("xiaole_video", {
            "channel": "minimax",
            "model": "MiniMax-H3",
            "resolution": "2k",
            "duration": duration,
        })
    if provider_name != "grok":
        raise AutodraftError(
            "provider_quote_request_invalid",
            "Provider 规范化请求缺少有效渠道",
            500,
        )
    model = str(request.get("model") or "").strip()
    resolution = str(request.get("resolution") or "").strip().lower()
    duration = int(request.get("duration_seconds") or 0)
    if not model or not resolution or duration <= 0:
        raise AutodraftError(
            "provider_quote_request_invalid",
            "Grok 规范化请求缺少必要计费参数",
            500,
        )
    return points_domain.cost_of("xiaole_video", {
        "channel": "grok",
        "model": model,
        "resolution": resolution,
        "duration": duration,
    })


def _provider_job(row):
    if not row:
        return None
    item = dict(row)
    item["request"] = _json(item.pop("request_json"), {})
    item["result"] = _json(item.pop("result_json"), None)
    item["error"] = _json(item.pop("error_json"), None)
    item["progress"] = int(item["progress"])
    item["poll_count"] = int(item["poll_count"])
    item["cost"] = int(item["cost"])
    item["terminal"] = item["status"] in {
        "succeeded", "failed", "canceled", "submit_unknown",
    }
    return item


def _minimax_runtime_diagnostics(item, diagnostics=None):
    """Attach the shared video worker's live MiniMax phase to one public job."""
    if not item or item.get("provider") != "minimax_h3":
        return item
    if item.get("status") not in PROVIDER_ACTIVE:
        return item
    if diagnostics is None:
        from . import video as video_domain
        diagnostics = video_domain.get_video_job_diagnostics(int(item["id"]))
    diagnostics = diagnostics or {}
    phase = str(diagnostics.get("phase") or "").strip().lower()
    provider_job_id = str(
        diagnostics.get("provider_video_id") or item.get("provider_job_id") or ""
    ).strip()
    if phase:
        item["phase"] = phase
    if provider_job_id:
        item["provider_job_id"] = provider_job_id
    item["progress_indeterminate"] = True
    return item


def _minimax_projected_status(shared_status, phase):
    phase = str(phase or "").strip().lower()
    if str(shared_status or "").strip().lower() != "running":
        return "queued"
    if phase == "minimax_submitting":
        return "submitting"
    if phase in {
        "minimax_queued", "minimax_queueing", "minimax_preparing",
    }:
        return "queued"
    return "running"


def _attach_provider_attempt_state(conn, item):
    if not item:
        return item
    attempt = _provider_attempt_for_job(conn, item["id"])
    if not attempt:
        return item
    state = str(attempt["state"] or "")
    item["billing_recovery"] = {
        "state": state,
        "cost": int(attempt["cost"] or 0),
        "refund_pending": state == "refund_pending",
        "refunded": state == "refunded",
    }
    return item


def _provider_failure_error(provider_state):
    failure = (provider_state or {}).get("failure")
    if not isinstance(failure, dict):
        raw = (provider_state or {}).get("raw")
        task = raw.get("task") if isinstance(raw, dict) else None
        raw_error = task.get("error") if isinstance(task, dict) else None
        if isinstance(raw_error, dict):
            failure = {
                "code": raw_error.get("code") or raw_error.get("error_code"),
                "message": (
                    raw_error.get("message") or raw_error.get("detail")
                    or raw_error.get("error_msg")
                ),
            }
        elif raw_error:
            failure = {"message": raw_error}
        else:
            failure = {}
    provider_code = str(failure.get("code") or "").strip()[:120]
    provider_message = str(failure.get("message") or "").strip()[:500]
    moderation_rejected = (
        provider_code == "1026"
        or "new_sensitive" in provider_message.lower()
        or "text sensitive" in provider_message.lower()
    )
    if moderation_rejected:
        detail = (
            "输入内容未通过审核，请调整镜头文字或参考图后重新预检。"
        )
    else:
        detail = "视频生成服务未能完成当前镜头"
        if provider_message:
            detail += "：" + provider_message
        detail += "。请调整镜头动作、参考图或生成要求后重新生成。"
    error = AutodraftError("provider_generation_failed", detail, 502)
    error.provider_code = provider_code
    error.provider_message = provider_message
    return error


def _provider_version(row):
    if not row:
        return None
    item = dict(row)
    item["version"] = int(item["version"])
    if "request_json" in item:
        request = _json(item.pop("request_json"), {})
        item["request_snapshot"] = {
            "prompt": str(request.get("prompt") or ""),
            "negative_prompt": str(request.get("negative_prompt") or ""),
            "ratio": str(request.get("ratio") or ""),
            "resolution": str(request.get("resolution") or ""),
            "duration_seconds": int(request.get("duration_seconds") or 0),
        }
    if "result_json" in item:
        result = _json(item.pop("result_json"), {})
        item["native_media"] = _sanitized_native_media(
            result.get("native_media")
        )
        item["native_audio"] = _sanitized_native_audio(
            result.get("native_audio")
            or (item["native_media"].get("audio") if item["native_media"] else None)
        )
    item["selected"] = bool(item.pop("selected", 0))
    return item


def select_provider_version(db_factory, owner_username, body):
    project_id = str(body.get("project_id") or "").strip()
    shot_key = str(body.get("shot_key") or "").strip()
    version_id = str(body.get("version_id") or "").strip()
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner_username, project_id)
        row = conn.execute(
            "SELECT * FROM short_drama_provider_shot_versions "
            "WHERE id=? AND project_id=? AND shot_key=? AND status='ready'",
            (version_id, project_id, shot_key),
        ).fetchone()
        if not row:
            raise AutodraftError(
                "provider_version_not_found", "所选镜头视频版本不存在", 404,
            )
        conn.execute(
            "INSERT INTO short_drama_provider_shot_selections "
            "(project_id,shot_key,version_id,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(project_id,shot_key) DO UPDATE SET "
            "version_id=excluded.version_id,updated_at=excluded.updated_at",
            (project_id, shot_key, version_id, int(time.time())),
        )
        conn.commit()
        result = _provider_version(row)
        result["selected"] = True
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_provider_quote(
    db_factory, owner_username, actor_username, body, avatar_lookup=None,
):
    preview = preview_provider_request(
        db_factory, owner_username, actor_username, body,
        avatar_lookup=avatar_lookup, include_private=True,
    )
    provider_request = preview.pop("_provider_request")
    if not preview["ready"]:
        raise AutodraftError(
            "provider_not_configured",
            "真实画面 Provider 尚未配置，不能创建付费报价",
            503,
        )
    now = int(time.time())
    token = uuid.uuid4().hex
    cost = _provider_shot_cost(provider_request)
    conn = _connection(db_factory)
    try:
        conn.execute(
            "INSERT INTO short_drama_provider_shot_quotes "
            "(token,actor_username,owner_username,project_id,plan_id,shot_key,"
            "character_key,avatar_id,request_hash,request_json,cost,expires_at,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                token, actor_username, owner_username, preview["project_id"],
                preview["plan_id"], preview["shot"]["shot_key"],
                preview["character_key"], preview["avatar"]["id"],
                preview["request_hash"], _json_text(provider_request), cost,
                now + PROVIDER_QUOTE_TTL_SECONDS, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "quote_token": token,
        "project_id": preview["project_id"],
        "plan_id": preview["plan_id"],
        "shot": preview["shot"],
        "avatar": preview["avatar"],
        "character_key": preview["character_key"],
        "provider": preview["provider"],
        "request_hash": preview["request_hash"],
        "request": preview["request"],
        "cost": cost,
        "expires_at": now + PROVIDER_QUOTE_TTL_SECONDS,
        "message": "报价已生成；确认后才会扣点并提交 Provider",
    }


def _mark_provider_attempt_failure(
    db_factory, attempt_id, job_id, error, charged=False, refund_points=None,
):
    state = "failed"
    attempt = None
    conn = _connection(db_factory)
    try:
        attempt = conn.execute(
            "SELECT * FROM short_drama_provider_shot_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
    finally:
        conn.close()
    if charged and attempt and int(attempt["cost"] or 0) > 0:
        state = "refund_pending"
        if callable(refund_points):
            try:
                refund_points(
                    attempt["actor_username"], int(attempt["cost"]),
                    "短剧单镜头生成失败补偿", attempt["refund_key"],
                )
                state = "refunded"
            except Exception:
                state = "refund_pending"
    payload = {
        "code": getattr(error, "code", "provider_job_failed"),
        "detail": str(error)[:500],
    }
    provider_code = str(getattr(error, "provider_code", "") or "").strip()
    provider_message = str(
        getattr(error, "provider_message", "") or ""
    ).strip()
    if provider_code:
        payload["provider_code"] = provider_code[:120]
    if provider_message:
        payload["provider_message"] = provider_message[:500]
    if payload["code"] == "provider_generation_failed":
        payload["retryable"] = True
        payload["next_action"] = (
            "检查镜头文字和参考图，修改后重新预检并生成"
            if provider_code == "1026"
            else "修改镜头生成要求后重新预检并生成"
        )
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE short_drama_provider_shot_attempts SET state=?,error_json=?,"
            "updated_at=? WHERE id=? AND state NOT IN ('done','refunded')",
            (state, _json_text(payload), int(time.time()), attempt_id),
        )
        if job_id:
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET status='failed',"
                "error_json=?,updated_at=? WHERE id=? AND status!='succeeded'",
                (_json_text(payload), int(time.time()), job_id),
            )
        conn.commit()
    finally:
        conn.close()


def _minimax_xiaole_payload(provider_request, quote, actor_username):
    request = dict(provider_request or {})
    provider = load_by_name("minimax_h3")
    references = provider.resolve_reference_values(
        request.get("reference_images") or []
    )
    public_payload = {
        "channel": "minimax",
        "operation": "generate",
        "model": "MiniMax-H3",
        "prompt": str(request.get("prompt") or "").strip(),
        "ratio": str(request.get("ratio") or "9:16").strip(),
        "resolution": "2k",
        "duration": int(request.get("duration_seconds") or 0),
        "reference_images": references,
        "generate_audio": True,
    }
    from . import video

    validated = video.validate_xiaole_video_payload(
        public_payload, actor_username
    )
    # The provider-ready Data URIs are validation-only. Persisting them in the
    # shared jobs table would copy private project images into a generic queue
    # and every database backup. The worker resolves the project-owned compact
    # references from short_drama_provider_shot_jobs immediately before submit.
    validated["reference_images"] = []
    validated["_short_drama_provider_binding"] = {
        "project_id": str(quote["project_id"]),
        "plan_id": str(quote["plan_id"]),
        "shot_key": str(quote["shot_key"]),
        "request_hash": str(quote["request_hash"]),
    }
    validated["_short_drama_native_audio_required"] = True
    return validated


def resolve_shared_xiaole_payload(
    db_factory, shared_job_id, actor_username, payload,
):
    """Resolve project-owned MiniMax references for one worker invocation only."""
    resolved = dict(payload or {})
    binding = resolved.get("_short_drama_provider_binding")
    if not isinstance(binding, dict):
        return resolved
    conn = _connection(db_factory)
    try:
        row = conn.execute(
            "SELECT project_id,actor_username,plan_id,shot_key,provider,input_hash,"
            "request_json FROM short_drama_provider_shot_jobs WHERE id=?",
            (str(shared_job_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row or str(row["provider"] or "") != "minimax_h3":
        raise AutodraftError(
            "provider_binding_invalid",
            "麦克视频任务缺少有效的短剧资产绑定",
            409,
        )
    expected = {
        "project_id": str(row["project_id"] or ""),
        "plan_id": str(row["plan_id"] or ""),
        "shot_key": str(row["shot_key"] or ""),
        "request_hash": str(row["input_hash"] or ""),
    }
    if (
        str(row["actor_username"] or "") != str(actor_username or "")
        or any(str(binding.get(key) or "") != value for key, value in expected.items())
    ):
        raise AutodraftError(
            "provider_binding_invalid",
            "麦克视频任务与短剧项目绑定不一致",
            409,
        )
    request = _json(row["request_json"], {})
    provider = load_by_name("minimax_h3")
    references = provider.resolve_reference_values(
        request.get("reference_images") or []
    )
    if not references:
        raise AutodraftError(
            "provider_reference_required",
            "麦克视频任务缺少可用的项目参考图",
            422,
        )
    resolved["reference_images"] = references
    return resolved


def _enforce_shared_video_submission_limit(
    checker, connection, actor_username, pending_job_included=False,
):
    if not callable(checker):
        return
    limit = checker(
        connection,
        actor_username,
        pending_job_included=pending_job_included,
    )
    if not limit:
        return
    raise AutodraftError(
        str(limit.get("code") or "active_job_cap"),
        str(limit.get("detail") or "当前任务过多，请等待部分完成后重试"),
        429,
    )


def _start_minimax_xiaole_job(
    db_factory, owner_username, actor_username, quote, idempotency_key,
    deduct_points, refund_points, enqueue_job, project_usage,
    shared_video_submission_limit,
    video_asset_recorder=None, video_asset_phase_updater=None,
):
    if not callable(deduct_points) or not callable(refund_points):
        raise AutodraftError(
            "billing_unavailable", "单镜头扣点服务暂不可用", 503
        )
    if not callable(enqueue_job):
        raise AutodraftError(
            "provider_queue_unavailable", "视频任务队列暂不可用", 503
        )
    now = int(time.time())
    key = _key(idempotency_key)
    conn = _connection(db_factory)
    try:
        existing = conn.execute(
            "SELECT * FROM short_drama_provider_shot_attempts "
            "WHERE actor_username=? AND idempotency_key=?",
            (actor_username, key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != quote["request_hash"]:
                raise AutodraftError(
                    "idempotency_conflict", "该幂等键已用于另一单镜头任务", 409
                )
            result = _provider_job(conn.execute(
                "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
                (existing["job_id"],),
            ).fetchone())
            if not result:
                raise AutodraftError(
                    "provider_charge_recovery_pending",
                    "扣点状态正在恢复，请稍后重试",
                    409,
                )
            result["replayed"] = True
            return result
        if int(quote["expires_at"]) < now:
            raise AutodraftError("provider_quote_expired", "单镜头报价已过期", 409)
        if quote["consumed_job_id"]:
            raise AutodraftError("provider_quote_consumed", "单镜头报价已被使用", 409)
        _project(conn, owner_username, quote["project_id"])
        _confirmed_plan(conn, quote["project_id"], quote["plan_id"])
        _enforce_shared_video_submission_limit(
            shared_video_submission_limit,
            conn,
            actor_username,
        )
        if conn.execute(
            "SELECT 1 FROM short_drama_provider_shot_jobs "
            "WHERE project_id=? AND shot_key=? AND status IN "
            "('billing','queued','submitting','running','submit_unknown')",
            (quote["project_id"], quote["shot_key"]),
        ).fetchone():
            raise AutodraftError(
                "active_provider_shot_job", "当前镜头已有生成任务处理中", 409
            )
        project = _project(conn, owner_username, quote["project_id"])
        usage = (
            project_usage(conn, quote["project_id"])
            if callable(project_usage)
            else {
                "spent_points": int(project.get("spent_points") or 0),
                "reserved_points": 0,
            }
        )
        budget = int(project.get("point_budget") or 0)
        cost = int(quote["cost"])
        if (
            budget
            and int(usage.get("spent_points") or 0)
            + int(usage.get("reserved_points") or 0)
            + cost > budget
        ):
            raise AutodraftError(
                "point_budget_exceeded", "项目点数预算不足，无法生成当前镜头", 409
            )
    finally:
        conn.close()

    attempt_id = uuid.uuid4().hex
    charge_key = "short-drama-provider-shot-charge:" + attempt_id
    provider_request = _json(quote["request_json"], {})
    payload = _minimax_xiaole_payload(
        provider_request, quote, actor_username
    )

    def associate(connection, shared_job_id):
        connection.row_factory = sqlite3.Row
        current = connection.execute(
            "SELECT token,project_id,plan_id,shot_key,character_key,avatar_id,"
            "request_hash,request_json,cost,consumed_job_id,expires_at "
            "FROM short_drama_provider_shot_quotes WHERE token=? "
            "AND actor_username=? AND owner_username=?",
            (quote["token"], actor_username, owner_username),
        ).fetchone()
        if not current or current["consumed_job_id"]:
            raise AutodraftError(
                "provider_quote_consumed", "单镜头报价已被使用", 409
            )
        immutable_fields = (
            "project_id", "plan_id", "shot_key", "character_key", "avatar_id",
            "request_hash", "request_json", "cost",
        )
        if any(current[field] != quote[field] for field in immutable_fields):
            raise AutodraftError(
                "provider_quote_changed",
                "单镜头报价在提交期间发生变化，本次任务已安全终止",
                409,
            )
        commit_now = int(time.time())
        if int(current["expires_at"] or 0) < commit_now:
            raise AutodraftError(
                "provider_quote_expired", "单镜头报价已过期", 409
            )
        _enforce_shared_video_submission_limit(
            shared_video_submission_limit,
            connection,
            actor_username,
            pending_job_included=True,
        )
        refreshed = preview_provider_request(
            db_factory,
            owner_username,
            actor_username,
            {
                "project_id": current["project_id"],
                "plan_id": current["plan_id"],
                "shot_key": current["shot_key"],
                "character_key": current["character_key"],
                "avatar_id": current["avatar_id"],
            },
            include_private=True,
            connection=connection,
        )
        if refreshed["request_hash"] != current["request_hash"]:
            raise AutodraftError(
                "provider_quote_stale",
                "镜头或锁定角色参考图在扣点期间发生变化，本次任务已安全终止",
                409,
            )
        project = _project(connection, owner_username, current["project_id"])
        _confirmed_plan(connection, current["project_id"], current["plan_id"])
        if connection.execute(
            "SELECT 1 FROM short_drama_provider_shot_jobs "
            "WHERE project_id=? AND shot_key=? AND status IN "
            "('billing','queued','submitting','running','submit_unknown')",
            (current["project_id"], current["shot_key"]),
        ).fetchone():
            raise AutodraftError(
                "active_provider_shot_job",
                "当前镜头已有生成任务处理中",
                409,
            )
        usage = (
            project_usage(connection, current["project_id"])
            if callable(project_usage)
            else {
                "spent_points": int(project.get("spent_points") or 0),
                "reserved_points": 0,
            }
        )
        budget = int(project.get("point_budget") or 0)
        if (
            budget
            and int(usage.get("spent_points") or 0)
            + int(usage.get("reserved_points") or 0)
            + int(current["cost"]) > budget
        ):
            raise AutodraftError(
                "point_budget_exceeded",
                "项目点数预算不足，无法生成当前镜头",
                409,
            )
        shared_id = str(shared_job_id)
        connection.execute(
            "INSERT INTO short_drama_provider_shot_jobs "
            "(id,project_id,owner_username,actor_username,plan_id,shot_key,"
            "character_key,avatar_id,provider,status,progress,poll_count,input_hash,"
            "request_json,cost,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'queued',5,0,?,?,?,?,?)",
            (
                shared_id, current["project_id"], owner_username, actor_username,
                current["plan_id"], current["shot_key"], current["character_key"],
                current["avatar_id"], "minimax_h3", current["request_hash"],
                current["request_json"], int(current["cost"]), commit_now, commit_now,
            ),
        )
        connection.execute(
            "INSERT INTO short_drama_provider_shot_attempts "
            "(id,actor_username,owner_username,project_id,idempotency_key,"
            "request_hash,quote_token,cost,charge_key,refund_key,state,job_id,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'linked',?,?,?)",
            (
                attempt_id, actor_username, owner_username, current["project_id"],
                key, current["request_hash"], current["token"], int(current["cost"]),
                charge_key,
                jobs_store.refund_transaction_key(shared_job_id, actor_username),
                shared_id, commit_now, commit_now,
            ),
        )
        changed = connection.execute(
            "UPDATE short_drama_provider_shot_quotes SET consumed_job_id=? "
            "WHERE token=? AND consumed_job_id IS NULL",
            (shared_id, current["token"]),
        ).rowcount
        if changed != 1:
            raise AutodraftError(
                "provider_quote_consumed", "单镜头报价已被使用", 409
            )

    try:
        shared_job_id, _points_left = jobs_store.create_paid_job(
            db_factory,
            deduct_points,
            refund_points,
            "xiaole_video",
            actor_username,
            int(quote["cost"]),
            payload,
            "content",
            before_commit=associate,
            charge_transaction_key=charge_key,
        )
    except jobs_store.PaidJobInsertError as error:
        if isinstance(error.__cause__, AutodraftError):
            raise error.__cause__
        raise
    def reject_before_enqueue(reason):
        claimed = jobs_store.set_terminal(
            db_factory,
            shared_job_id,
            "error",
            error=reason,
            from_states=("pending",),
        )
        if claimed:
            refund_key = jobs_store.refund_transaction_key(
                shared_job_id, actor_username
            )

            def refund_shared(username, cost):
                refund_points(
                    username,
                    cost,
                    reason,
                    transaction_key=refund_key,
                )
                return True

            jobs_store.refund_once(
                db_factory,
                shared_job_id,
                actor_username,
                int(quote["cost"]),
                refund_shared,
            )
        if callable(video_asset_phase_updater):
            try:
                video_asset_phase_updater(
                    shared_job_id,
                    "failed",
                    status="failed",
                    error=reason,
                )
            except Exception:
                pass
        reconcile_shared_xiaole_job(db_factory, shared_job_id)

    if callable(video_asset_recorder):
        try:
            video_asset_recorder(shared_job_id, actor_username, payload)
        except Exception as error:
            reason = "麦克视频资产登记失败，请稍后重试"
            reject_before_enqueue(reason)
            raise AutodraftError(
                "video_asset_register_failed", reason, 503
            ) from error
    try:
        queued = bool(enqueue_job(shared_job_id, "xiaole_video", None))
    except Exception:
        queued = False
    if not queued:
        reason = "麦克视频任务队列已满，请稍后重试"
        reject_before_enqueue(reason)
        raise AutodraftError(
            "provider_queue_full", "视频任务队列已满，请稍后重试", 429
        )
    conn = _connection(db_factory)
    try:
        result = _provider_job(conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
            (str(shared_job_id),),
        ).fetchone())
    finally:
        conn.close()
    result["replayed"] = False
    return result


def start_provider_job(
    db_factory, owner_username, actor_username, body, idempotency_key,
    avatar_lookup=None, deduct_points=None, refund_points=None,
    charge_lookup=None, project_usage=None, enqueue_job=None,
    shared_video_submission_limit=None,
    video_asset_recorder=None, video_asset_phase_updater=None,
):
    token = str(body.get("quote_token") or "").strip()
    key = _key(idempotency_key)
    now = int(time.time())
    requested_project_id = str(body.get("project_id") or "").strip()
    inspect_conn = _connection(db_factory)
    try:
        inspect_quote = inspect_conn.execute(
            "SELECT * FROM short_drama_provider_shot_quotes WHERE token=? "
            "AND actor_username=? AND owner_username=?",
            (token, actor_username, owner_username),
        ).fetchone()
        inspect_existing = inspect_conn.execute(
            "SELECT * FROM short_drama_provider_shot_attempts "
            "WHERE actor_username=? AND idempotency_key=?",
            (actor_username, key),
        ).fetchone()
    finally:
        inspect_conn.close()
    if not inspect_quote:
        raise AutodraftError("provider_quote_not_found", "单镜头报价不存在", 404)
    if (
        requested_project_id
        and requested_project_id != str(inspect_quote["project_id"])
    ):
        raise AutodraftError(
            "provider_quote_project_mismatch", "报价不属于当前短剧项目", 409
        )
    # Idempotent replays must return the original task even if the source
    # binding changed later. A genuinely new request is recompiled so a stale
    # quote can never charge against an unbound/replaced avatar or changed shot.
    if not inspect_existing:
        refreshed = preview_provider_request(
            db_factory,
            owner_username,
            actor_username,
            {
                "project_id": inspect_quote["project_id"],
                "plan_id": inspect_quote["plan_id"],
                "shot_key": inspect_quote["shot_key"],
                "character_key": inspect_quote["character_key"],
                "avatar_id": inspect_quote["avatar_id"],
            },
            avatar_lookup=avatar_lookup,
            include_private=True,
        )
        refreshed.pop("_provider_request", None)
        if refreshed["request_hash"] != inspect_quote["request_hash"]:
            raise AutodraftError(
                "provider_quote_stale",
                "镜头或角色形象已变化，请重新预检并报价",
                409,
            )
    prepared_request_json = inspect_quote["request_json"]
    prepared_provider_name = str(
        _json(prepared_request_json, {}).get("provider") or "heygen_cinematic"
    ).strip()
    if prepared_provider_name == "minimax_h3":
        return _start_minimax_xiaole_job(
            db_factory,
            owner_username,
            actor_username,
            inspect_quote,
            key,
            deduct_points,
            refund_points,
            enqueue_job,
            project_usage,
            shared_video_submission_limit,
            video_asset_recorder,
            video_asset_phase_updater,
        )
    if not inspect_existing:
        provider = load_by_name(prepared_provider_name)
        if provider is None or not provider.configured:
            raise AutodraftError(
                "provider_not_configured",
                "真实画面 Provider 配置已失效，任务未扣点",
                503,
            )
        prepare_job = getattr(provider, "prepare_job", None)
        if callable(prepare_job):
            try:
                prepared_request_json = _json_text(
                    prepare_job(_json(prepared_request_json, {}))
                )
            except VisualProviderError as error:
                raise AutodraftError(
                    error.code,
                    str(error),
                    503,
                ) from error
            except Exception as error:
                raise AutodraftError(
                    "provider_not_configured",
                    "真实画面 Provider 密钥保险箱不可用，任务未扣点",
                    503,
                ) from error
    conn = _connection(db_factory)
    attempt_id = None
    job_id = None
    cost = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        quote = conn.execute(
            "SELECT * FROM short_drama_provider_shot_quotes WHERE token=? "
            "AND actor_username=? AND owner_username=?",
            (token, actor_username, owner_username),
        ).fetchone()
        if not quote:
            raise AutodraftError("provider_quote_not_found", "单镜头报价不存在", 404)
        _project(conn, owner_username, quote["project_id"])
        existing = conn.execute(
            "SELECT * FROM short_drama_provider_shot_attempts "
            "WHERE actor_username=? AND idempotency_key=?",
            (actor_username, key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != quote["request_hash"]:
                raise AutodraftError(
                    "idempotency_conflict", "该幂等键已用于另一单镜头任务", 409
                )
            result = _provider_job(conn.execute(
                "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
                (existing["job_id"],),
            ).fetchone())
            conn.commit()
            if not result:
                raise AutodraftError(
                    "provider_charge_recovery_pending",
                    "扣点状态正在恢复，请稍后重试",
                    409,
                )
            result["replayed"] = True
            return result
        if int(quote["expires_at"]) < now:
            raise AutodraftError("provider_quote_expired", "单镜头报价已过期", 409)
        if quote["consumed_job_id"]:
            raise AutodraftError("provider_quote_consumed", "单镜头报价已被使用", 409)
        plan = _confirmed_plan(conn, quote["project_id"], quote["plan_id"])
        if plan["id"] != quote["plan_id"]:
            raise AutodraftError("provider_quote_stale", "制作计划已变化，请重新报价", 409)
        if conn.execute(
            "SELECT 1 FROM short_drama_provider_shot_jobs "
            "WHERE project_id=? AND shot_key=? AND status IN "
            "('billing','queued','submitting','running','submit_unknown')",
            (quote["project_id"], quote["shot_key"]),
        ).fetchone():
            raise AutodraftError(
                "active_provider_shot_job", "当前镜头已有生成任务处理中", 409
            )
        cost = int(quote["cost"])
        project = _project(conn, owner_username, quote["project_id"])
        usage = (
            project_usage(conn, quote["project_id"])
            if callable(project_usage)
            else {
                "spent_points": int(project.get("spent_points") or 0),
                "reserved_points": 0,
            }
        )
        budget = int(project.get("point_budget") or 0)
        if (
            budget
            and int(usage.get("spent_points") or 0)
            + int(usage.get("reserved_points") or 0)
            + cost > budget
        ):
            raise AutodraftError(
                "point_budget_exceeded", "项目点数预算不足，无法生成当前镜头", 409
            )
        attempt_id = uuid.uuid4().hex
        job_id = uuid.uuid4().hex
        charge_key = "short-drama-provider-shot-charge:" + attempt_id
        refund_key = "short-drama-provider-shot-refund:" + attempt_id
        provider_name = prepared_provider_name
        conn.execute(
            "INSERT INTO short_drama_provider_shot_jobs "
            "(id,project_id,owner_username,actor_username,plan_id,shot_key,"
            "character_key,avatar_id,provider,status,progress,poll_count,input_hash,"
            "request_json,cost,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'billing',0,0,?,?,?,?,?)",
            (
                job_id, quote["project_id"], owner_username, actor_username,
                quote["plan_id"], quote["shot_key"], quote["character_key"],
                quote["avatar_id"], provider_name, quote["request_hash"],
                prepared_request_json, cost, now, now,
            ),
        )
        conn.execute(
            "INSERT INTO short_drama_provider_shot_attempts "
            "(id,actor_username,owner_username,project_id,idempotency_key,"
            "request_hash,quote_token,cost,charge_key,refund_key,state,job_id,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'accepted',?,?,?)",
            (
                attempt_id, actor_username, owner_username, quote["project_id"],
                key, quote["request_hash"], token, cost, charge_key, refund_key,
                job_id, now, now,
            ),
        )
        conn.execute(
            "UPDATE short_drama_provider_shot_quotes SET consumed_job_id=? "
            "WHERE token=? AND consumed_job_id IS NULL",
            (job_id, token),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    charged = False
    try:
        if cost:
            if not callable(deduct_points):
                raise AutodraftError(
                    "billing_unavailable", "单镜头扣点服务暂不可用", 503
                )
            try:
                deduct_points(
                    actor_username, cost, "短剧单镜头真实生成",
                    "short-drama-provider-shot-charge:" + attempt_id,
                )
            except Exception:
                ledger = None
                if callable(charge_lookup):
                    try:
                        ledger = charge_lookup(
                            "short-drama-provider-shot-charge:" + attempt_id
                        )
                    except Exception:
                        pass
                if not _charge_ledger_matches(actor_username, cost, ledger):
                    observed_at = int(time.time())
                    conn = _connection(db_factory)
                    try:
                        conn.execute(
                            "UPDATE short_drama_provider_shot_jobs "
                            "SET error_json=?,updated_at=? "
                            "WHERE id=? AND status='billing'",
                            (
                                _json_text({
                                    "code": "billing_reconciliation_pending",
                                    "detail": "扣点响应不确定，等待权威流水二次确认",
                                    "retryable": True,
                                }),
                                observed_at,
                                job_id,
                            ),
                        )
                        conn.commit()
                        result = _provider_job(conn.execute(
                            "SELECT * FROM short_drama_provider_shot_jobs "
                            "WHERE id=?", (job_id,),
                        ).fetchone())
                    finally:
                        conn.close()
                    result["replayed"] = False
                    return result
            charged = True
        conn = _connection(db_factory)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE short_drama_provider_shot_attempts SET state='linked',"
                "updated_at=? WHERE id=? AND state='accepted'",
                (int(time.time()), attempt_id),
            )
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET status='queued',"
                "progress=5,updated_at=? WHERE id=? AND status='billing'",
                (int(time.time()), job_id),
            )
            conn.commit()
            result = _provider_job(conn.execute(
                "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
                (job_id,),
            ).fetchone())
        finally:
            conn.close()
        result["replayed"] = False
        return result
    except Exception as error:
        _mark_provider_attempt_failure(
            db_factory, attempt_id, job_id, error, charged=charged,
            refund_points=refund_points,
        )
        raise


def reconcile_unknown_provider_submission(
    db_factory, owner_username, actor_username, actor_role, job_id, body,
    refund_points=None,
):
    """Resolve a submission whose upstream response was lost.

    Both actions are admin-only because a raw provider job id cannot prove
    ownership, and an incorrect absence claim could refund a still-running job.
    """
    project_id = str(body.get("project_id") or "").strip()
    action = str(body.get("action") or "").strip()
    if str(actor_role or "").lower() != "admin":
        raise AutodraftError(
            "provider_reconciliation_forbidden",
            "仅可信管理员可处理无法由 Provider 证明归属的任务",
            403,
        )
    if action == "confirm_not_submitted":
        conn = _connection(db_factory)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _project(conn, owner_username, project_id)
            row = conn.execute(
                "SELECT * FROM short_drama_provider_shot_jobs "
                "WHERE id=? AND project_id=?", (job_id, project_id),
            ).fetchone()
            if not row:
                raise LookupError("single-shot provider job does not exist")
            attempt = _provider_attempt_for_job(conn, job_id)
            if not attempt:
                raise AutodraftError(
                    "provider_reconciliation_evidence_invalid",
                    "Provider 对账尝试不存在", 409,
                )
            if row["status"] == "submit_unknown":
                now = int(time.time())
                payload = _json_text({
                    "code": "provider_submission_confirmed_absent",
                    "detail": "已由可信管理员确认上游未创建任务，开始安全退款",
                })
                changed = conn.execute(
                    "UPDATE short_drama_provider_shot_jobs SET status='failed',"
                    "error_json=?,updated_at=? WHERE id=? AND status='submit_unknown'",
                    (payload, now, job_id),
                ).rowcount
                if changed != 1:
                    raise AutodraftError(
                        "provider_reconciliation_conflict",
                        "任务已被另一项对账操作处理", 409,
                    )
                conn.execute(
                    "UPDATE short_drama_provider_shot_attempts SET state=?,"
                    "error_json=?,updated_at=? WHERE id=? "
                    "AND state NOT IN ('done','refunded')",
                    (
                        "refund_pending" if int(attempt["cost"] or 0) > 0 else "failed",
                        payload, now, attempt["id"],
                    ),
                )
            elif not (
                row["status"] == "failed"
                and attempt["state"] in {"refund_pending", "refunded", "failed"}
            ):
                raise AutodraftError(
                    "provider_reconciliation_not_allowed",
                    "该任务当前不允许确认未提交", 409,
                )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        _recover_provider_refund(db_factory, job_id, refund_points=refund_points)
        conn = _connection(db_factory)
        try:
            return _provider_job(conn.execute(
                "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
                (job_id,),
            ).fetchone())
        finally:
            conn.close()
    if action != "bind_provider_job":
        raise AutodraftError(
            "provider_reconciliation_action_invalid",
            "未知的 Provider 提交对账动作", 422,
        )
    provider_job_id = str(body.get("provider_job_id") or "").strip()
    if not provider_job_id or len(provider_job_id) > 200:
        raise AutodraftError("provider_job_id_invalid", "上游 Provider 任务 ID 无效", 422)
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner_username, project_id)
        row = conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs "
            "WHERE id=? AND project_id=?", (job_id, project_id),
        ).fetchone()
        if not row:
            raise LookupError("single-shot provider job does not exist")
        if not _provider_attempt_for_job(conn, job_id):
            raise AutodraftError(
                "provider_reconciliation_not_allowed",
                "Provider 对账尝试不存在", 409,
            )
        provider = load_by_name(row["provider"])
        if provider is None:
            raise AutodraftError(
                "provider_reconciliation_not_allowed",
                "任务的 Provider 不受支持，无法安全绑定上游任务",
                409,
            )
        try:
            provider_job_id = provider.bind_reconciled_job_id(
                provider_job_id, _json(row["request_json"], {}),
            )
        except VisualProviderError as error:
            raise AutodraftError(error.code, str(error), 422) from error
        if not provider_job_id or len(provider_job_id) > 200:
            raise AutodraftError(
                "provider_job_id_invalid", "上游 Provider 任务 ID 无效", 422,
            )
        existing_provider_job_id = str(row["provider_job_id"] or "").strip()
        if row["status"] == "running" and existing_provider_job_id == provider_job_id:
            conn.commit()
            return _provider_job(row)
        if row["status"] != "submit_unknown":
            raise AutodraftError(
                "provider_reconciliation_not_allowed",
                "该任务当前不需要提交对账", 409,
            )
        if existing_provider_job_id and existing_provider_job_id != provider_job_id:
            raise AutodraftError(
                "provider_job_id_conflict", "任务已绑定另一上游 Provider 任务 ID", 409,
            )
        conn.execute(
            "UPDATE short_drama_provider_shot_jobs SET status='running',"
            "provider_job_id=?,progress=MAX(progress,20),error_json=NULL,updated_at=? "
            "WHERE id=? AND status='submit_unknown'",
            (provider_job_id, int(time.time()), job_id),
        )
        conn.commit()
        return _provider_job(conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?", (job_id,),
        ).fetchone())
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _provider_attempt_for_job(conn, job_id):
    return conn.execute(
        "SELECT * FROM short_drama_provider_shot_attempts WHERE job_id=?",
        (job_id,),
    ).fetchone()


def _matching_shared_xiaole_job(conn, row):
    """Return the shared paid job only when its private binding proves ownership."""
    if not row or row["provider"] != "minimax_h3":
        return None
    raw_job_id = str(row["id"] or "").strip()
    try:
        shared_job_id = int(raw_job_id)
    except (TypeError, ValueError):
        return None
    if str(shared_job_id) != raw_job_id:
        return None
    try:
        shared = conn.execute(
            "SELECT * FROM jobs WHERE id=?", (shared_job_id,)
        ).fetchone()
        if not shared:
            return None
        payload = _json(shared["payload"], {})
        if not isinstance(payload, dict):
            return None
        binding = payload.get("_short_drama_provider_binding") or {}
        if not isinstance(binding, dict):
            return None
        expected_binding = {
            "project_id": row["project_id"],
            "plan_id": row["plan_id"],
            "shot_key": row["shot_key"],
            "request_hash": row["input_hash"],
        }
        if (
            shared["kind"] != "xiaole_video"
            or shared["username"] != row["actor_username"]
            or payload.get("channel") != "minimax"
            or any(
                binding.get(key) != value
                for key, value in expected_binding.items()
            )
        ):
            return None
        return shared
    except (IndexError, KeyError, sqlite3.OperationalError, TypeError, ValueError):
        return None


def _refund_provider_job(db_factory, job_id, error, refund_points=None):
    conn = _connection(db_factory)
    try:
        attempt = _provider_attempt_for_job(conn, job_id)
    finally:
        conn.close()
    if not attempt:
        return
    _mark_provider_attempt_failure(
        db_factory, attempt["id"], job_id, error,
        charged=attempt["state"] in {"charged", "linked", "refund_pending"},
        refund_points=refund_points,
    )


def _recover_provider_refund(
    db_factory, job_id, refund_points=None, now=None, force=False,
):
    if not callable(refund_points):
        return False
    now = int(time.time() if now is None else now)
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        attempt = _provider_attempt_for_job(conn, job_id)
        if (
            not attempt
            or attempt["state"] != "refund_pending"
            or (
                int(attempt["refund_retry_at"] or 0) > now
                and not force
            )
        ):
            conn.commit()
            return False
        projected = conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        shared = _matching_shared_xiaole_job(conn, projected)
        if shared is not None:
            if int(shared["refunded"] or 0) == 1:
                conn.execute(
                    "UPDATE short_drama_provider_shot_attempts SET state='refunded',"
                    "refund_retry_at=0,updated_at=? WHERE id=? "
                    "AND state='refund_pending'",
                    (now, attempt["id"]),
                )
                conn.commit()
                return True
            conn.commit()
            return False
        claimed = conn.execute(
            "UPDATE short_drama_provider_shot_attempts SET refund_retry_at=?,"
            "updated_at=? WHERE id=? AND state='refund_pending' "
            "AND refund_retry_at=?",
            (
                now + 60, now, attempt["id"],
                int(attempt["refund_retry_at"] or 0),
            ),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    if claimed != 1:
        return False
    try:
        refund_points(
            attempt["actor_username"], int(attempt["cost"]),
            "短剧单镜头生成失败补偿", attempt["refund_key"],
        )
    except Exception:
        retry_count = int(attempt["refund_retry_count"] or 0) + 1
        delay = min(300, 2 ** min(retry_count, 8))
        conn = _connection(db_factory)
        try:
            conn.execute(
                "UPDATE short_drama_provider_shot_attempts "
                "SET refund_retry_count=?,refund_retry_at=?,updated_at=? "
                "WHERE id=? AND state='refund_pending'",
                (retry_count, now + delay, now, attempt["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        return False
    conn = _connection(db_factory)
    try:
        conn.execute(
            "UPDATE short_drama_provider_shot_attempts SET state='refunded',"
            "refund_retry_at=0,updated_at=? WHERE id=? AND state='refund_pending'",
            (now, attempt["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return True


def retry_provider_refunds(db_factory, points_domain, limit=100, now=None):
    """Recover due single-shot refunds without requiring workspace access."""
    refund_points = getattr(points_domain, "refund_points", None)
    if not callable(refund_points):
        return 0
    now = int(time.time() if now is None else now)
    limit = max(1, min(1000, int(limit or 100)))
    conn = _connection(db_factory)
    try:
        job_ids = [
            row[0] for row in conn.execute(
                "SELECT job_id FROM short_drama_provider_shot_attempts "
                "WHERE state='refund_pending' AND refund_retry_at<=? "
                "AND job_id IS NOT NULL ORDER BY refund_retry_at,updated_at LIMIT ?",
                (now, limit),
            ).fetchall()
        ]
    finally:
        conn.close()
    return sum(
        1 for job_id in job_ids
        if _recover_provider_refund(
            db_factory, job_id, refund_points=refund_points, now=now,
        )
    )


def _recover_project_provider_refunds(
    db_factory, project_id, refund_points=None,
):
    if not callable(refund_points):
        return
    conn = _connection(db_factory)
    try:
        pending = [
            (row["job_id"], int(row["refund_retry_at"] or 0))
            for row in conn.execute(
                "SELECT job_id,refund_retry_at FROM short_drama_provider_shot_attempts "
                "WHERE project_id=? AND state='refund_pending' AND job_id IS NOT NULL",
                (project_id,),
            ).fetchall()
        ]
    finally:
        conn.close()
    for job_id, retry_at in pending:
        _recover_provider_refund(
            db_factory, job_id, refund_points=refund_points,
            now=max(int(time.time()), retry_at),
        )


def _provider_job_timeout_reason(row, now=None, next_poll=False):
    now = int(time.time() if now is None else now)
    elapsed = max(0, now - int(row["created_at"] or now))
    poll_count = int(row["poll_count"] or 0) + (1 if next_poll else 0)
    if elapsed >= PROVIDER_SHOT_DEADLINE_SECONDS:
        return {
            "reason": "deadline",
            "elapsed_seconds": elapsed,
            "poll_count": poll_count,
        }
    if poll_count >= PROVIDER_SHOT_MAX_POLLS:
        return {
            "reason": "poll_limit",
            "elapsed_seconds": elapsed,
            "poll_count": poll_count,
        }
    return None


def _expire_provider_job(db_factory, job_id, reason, refund_points=None):
    """Claim a running job's timeout before issuing its idempotent refund."""
    now = int(time.time())
    inspect_conn = _connection(db_factory)
    try:
        inspected = inspect_conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    finally:
        inspect_conn.close()
    provider = load_by_name(inspected["provider"]) if inspected else None
    supports_cancel = bool(
        provider
        and getattr(getattr(provider, "capability", None), "supports_cancel", False)
    )
    if inspected and inspected["status"] == "running" and inspected["provider_job_id"]:
        if not supports_cancel:
            payload = {
                "code": "provider_reconciliation_pending",
                "detail": "Provider 任务仍可能在上游计费执行，已保留任务等待继续对账",
                "retryable": True,
                "requires_reconciliation": True,
                "timeout_reason": reason["reason"],
                "elapsed_seconds": int(reason["elapsed_seconds"]),
                "poll_count": int(reason["poll_count"]),
            }
            conn = _connection(db_factory)
            try:
                conn.execute(
                    "UPDATE short_drama_provider_shot_jobs SET error_json=?,"
                    "updated_at=? WHERE id=? AND status='running'",
                    (_json_text(payload), now, job_id),
                )
                conn.commit()
            finally:
                conn.close()
            return False
        try:
            provider.cancel_job(inspected["provider_job_id"])
        except Exception as error:
            payload = {
                "code": "provider_cancel_unconfirmed",
                "detail": str(error)[:500],
                "retryable": True,
                "requires_reconciliation": True,
                "timeout_reason": reason["reason"],
                "elapsed_seconds": int(reason["elapsed_seconds"]),
                "poll_count": int(reason["poll_count"]),
            }
            conn = _connection(db_factory)
            try:
                conn.execute(
                    "UPDATE short_drama_provider_shot_jobs SET error_json=?,"
                    "updated_at=? WHERE id=? AND status='running'",
                    (_json_text(payload), now, job_id),
                )
                conn.commit()
            finally:
                conn.close()
            return False
    payload = {
        "code": "provider_generation_timeout",
        "detail": "Provider 生成超过最长等待时间，任务已失败并退点",
        "retryable": False,
        "timeout_reason": reason["reason"],
        "elapsed_seconds": int(reason["elapsed_seconds"]),
        "poll_count": int(reason["poll_count"]),
    }
    claimed = False
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        attempt = _provider_attempt_for_job(conn, job_id)
        if job and job["status"] == "running":
            changed = conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET status='failed',"
                "error_json=?,updated_at=? WHERE id=? AND status='running'",
                (_json_text(payload), now, job_id),
            ).rowcount
            if changed == 1:
                claimed = True
                if attempt:
                    needs_refund = (
                        int(attempt["cost"] or 0) > 0
                        and attempt["state"] in {
                            "charged", "linked", "refund_pending",
                        }
                    )
                    conn.execute(
                        "UPDATE short_drama_provider_shot_attempts SET state=?,"
                        "error_json=?,updated_at=? WHERE id=? "
                        "AND state NOT IN ('done','refunded')",
                        (
                            "refund_pending" if needs_refund else "failed",
                            _json_text(payload), now, attempt["id"],
                        ),
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if claimed:
        _recover_provider_refund(
            db_factory, job_id, refund_points=refund_points,
        )
    return claimed


def _finish_provider_job(db_factory, row, provider, provider_state):
    token = uuid.uuid4().hex
    claimed_at = int(time.time())
    inspect_conn = _connection(db_factory)
    try:
        inspect_conn.execute("BEGIN IMMEDIATE")
        claimed = inspect_conn.execute(
            "UPDATE short_drama_provider_shot_jobs "
            "SET finalizing_token=?,finalizing_at=?,updated_at=? "
            "WHERE id=? AND status='running' AND (finalizing_token IS NULL "
            "OR finalizing_at<?)",
            (token, claimed_at, claimed_at, row["id"], claimed_at - 600),
        ).rowcount
        inspect_conn.commit()
    finally:
        inspect_conn.close()
    if claimed != 1:
        return
    result = None
    try:
        result = provider.fetch_result(
            row["provider_job_id"], provider_state.get("result_url")
        )
        tail_reference = _extract_tail_reference(result.get("file"))
        if tail_reference:
            result["continuity_tail_file"] = tail_reference["file"]
            result["continuity_tail_url"] = tail_reference["url"]
        now = int(time.time())
        conn = _connection(db_factory)
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
            (row["id"],),
        ).fetchone()
        if (
            not current or current["status"] != "running"
            or current["finalizing_token"] != token
        ):
            conn.commit()
            _discard_provider_result(result)
            return
        version = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 "
            "FROM short_drama_provider_shot_versions "
            "WHERE project_id=? AND shot_key=?",
            (row["project_id"], row["shot_key"]),
        ).fetchone()[0])
        version_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO short_drama_provider_shot_versions "
            "(id,project_id,job_id,shot_key,version,provider,provider_job_id,"
            "status,file,url,input_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,'ready',?,?,?,?)",
            (
                version_id, row["project_id"], row["id"], row["shot_key"],
                version, row["provider"], row["provider_job_id"],
                result["file"], result["url"], row["input_hash"], now,
            ),
        )
        final_result = dict(result, version_id=version_id, version=version)
        conn.execute(
            "UPDATE short_drama_provider_shot_jobs SET status='succeeded',"
            "progress=100,result_json=?,error_json=NULL,finalizing_token=NULL,"
            "finalizing_at=0,updated_at=? WHERE id=? AND status='running' "
            "AND finalizing_token=?",
            (_json_text(final_result), now, row["id"], token),
        )
        conn.execute(
            "UPDATE short_drama_provider_shot_attempts SET state='done',"
            "updated_at=? WHERE job_id=? AND state IN ('charged','linked')",
            (now, row["id"]),
        )
        conn.commit()
    except Exception:
        if "conn" in locals() and conn.in_transaction:
            conn.rollback()
        _discard_provider_result(result)
        release = _connection(db_factory)
        try:
            release.execute(
                "UPDATE short_drama_provider_shot_jobs SET finalizing_token=NULL,"
                "finalizing_at=0 WHERE id=? AND status='running' "
                "AND finalizing_token=?", (row["id"], token),
            )
            release.commit()
        finally:
            release.close()
        raise
    finally:
        if "conn" in locals():
            conn.close()


def _reconcile_minimax_xiaole_job(db_factory, row):
    """Project one existing HuangQue xiaole job into the short-drama view."""
    conn = _connection(db_factory)
    try:
        shared = conn.execute(
            "SELECT * FROM jobs WHERE id=?",
            (int(row["id"]),),
        ).fetchone()
        if not shared:
            raise AutodraftError(
                "shared_video_job_missing",
                "麦克视频任务记录不存在，请联系管理员核对",
                409,
            )
        payload = _json(shared["payload"], {})
        binding = payload.get("_short_drama_provider_binding") or {}
        expected_binding = {
            "project_id": row["project_id"],
            "plan_id": row["plan_id"],
            "shot_key": row["shot_key"],
            "request_hash": row["input_hash"],
        }
        if (
            shared["kind"] != "xiaole_video"
            or shared["username"] != row["actor_username"]
            or payload.get("channel") != "minimax"
            or any(binding.get(key) != value for key, value in expected_binding.items())
        ):
            raise AutodraftError(
                "shared_video_job_mismatch",
                "麦克视频任务与当前短剧镜头不匹配，已停止自动关联",
                409,
            )
        shared_status = str(shared["status"] or "pending").lower()
        shared_result = _json(shared["result"], {})
        shared_error = str(shared["error"] or "").strip()
        shared_refunded = int(shared["refunded"] or 0)
    finally:
        conn.close()

    from . import video as video_domain
    runtime = video_domain.get_video_job_diagnostics(int(row["id"])) or {}
    runtime_phase = str(runtime.get("phase") or "").strip().lower()
    runtime_provider_job_id = str(
        runtime.get("provider_video_id") or ""
    ).strip()
    now = int(time.time())
    if shared_status == "done":
        video_file = str(shared_result.get("video_file") or "").strip()
        video_url = str(shared_result.get("video_url") or "").strip()
        provider_job_id = str(
            shared_result.get("provider_video_id") or ""
        ).strip()
        native_audio = _sanitized_native_audio(
            shared_result.get("native_audio")
        )
        native_media = _sanitized_native_media(
            shared_result.get("native_media")
        )
        if (
            not video_file or not video_url or not provider_job_id
            or native_audio.get("audible") is not True
            or not native_media
            or native_media["derived"]["file"] != video_file
            or str(shared_result.get("raw_video_file") or "").strip()
            != native_media["raw"]["file"]
            or native_media["audio"] != native_audio
        ):
            raise AutodraftError(
                "shared_video_result_incomplete",
                "麦克视频已完成但产物记录不完整，请联系管理员核对",
                409,
            )
        conn = _connection(db_factory)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
                (row["id"],),
            ).fetchone()
            version_row = conn.execute(
                "SELECT * FROM short_drama_provider_shot_versions WHERE job_id=?",
                (row["id"],),
            ).fetchone()
            if not version_row:
                version = int(conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 "
                    "FROM short_drama_provider_shot_versions "
                    "WHERE project_id=? AND shot_key=?",
                    (row["project_id"], row["shot_key"]),
                ).fetchone()[0])
                version_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO short_drama_provider_shot_versions "
                    "(id,project_id,job_id,shot_key,version,provider,provider_job_id,"
                    "status,file,url,input_hash,created_at) "
                    "VALUES (?,?,?,?,?,?,?,'ready',?,?,?,?)",
                    (
                        version_id, row["project_id"], row["id"], row["shot_key"],
                        version, "minimax_h3", provider_job_id, video_file,
                        video_url, row["input_hash"], now,
                    ),
                )
            else:
                version = int(version_row["version"])
                version_id = version_row["id"]
            final_result = dict(
                shared_result, version_id=version_id, version=version,
            )
            if current and current["status"] != "succeeded":
                conn.execute(
                    "UPDATE short_drama_provider_shot_jobs "
                    "SET status='succeeded',progress=100,provider_job_id=?,"
                    "result_json=?,error_json=NULL,updated_at=? WHERE id=?",
                    (
                        provider_job_id, _json_text(final_result), now, row["id"],
                    ),
                )
            conn.execute(
                "UPDATE short_drama_provider_shot_attempts SET state='done',"
                "updated_at=? WHERE job_id=? AND state IN ('accepted','charged','linked')",
                (now, row["id"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    elif shared_status in {"error", "failed", "canceled", "cancelled"}:
        error_payload = {
            "code": "shared_video_generation_failed",
            "detail": shared_error or "麦克视频生成失败",
            "retryable": False,
        }
        attempt_state = "refunded" if shared_refunded == 1 else "refund_pending"
        conn = _connection(db_factory)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs "
                "SET status='failed',error_json=?,updated_at=? "
                "WHERE id=? AND status NOT IN ('succeeded','failed','canceled')",
                (_json_text(error_payload), now, row["id"]),
            )
            conn.execute(
                "UPDATE short_drama_provider_shot_attempts "
                "SET state=?,error_json=?,updated_at=? "
                "WHERE job_id=? AND state NOT IN ('done','refunded')",
                (
                    attempt_state, _json_text(error_payload), now, row["id"],
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        projected_status = _minimax_projected_status(
            shared_status, runtime_phase,
        )
        projected_progress = {
            "queued": 5, "submitting": 15, "running": 35,
        }[projected_status]
        conn = _connection(db_factory)
        try:
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET status=?,progress=?,"
                "provider_job_id=COALESCE(?,provider_job_id),"
                "error_json=NULL,updated_at=? WHERE id=? "
                "AND status NOT IN ('succeeded','failed','canceled')",
                (
                    projected_status, projected_progress,
                    runtime_provider_job_id or None, now, row["id"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    conn = _connection(db_factory)
    try:
        item = _attach_provider_attempt_state(conn, _provider_job(conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
            (row["id"],),
        ).fetchone()))
        return _minimax_runtime_diagnostics(item, runtime)
    finally:
        conn.close()


def reconcile_shared_xiaole_job(db_factory, job_id):
    """Reconcile a worker-owned xiaole terminal without any provider call."""
    conn = _connection(db_factory)
    try:
        row = conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs "
            "WHERE id=? AND provider='minimax_h3'",
            (str(job_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return _reconcile_minimax_xiaole_job(db_factory, row)


def reconcile_provider_job(
    db_factory, owner_username, project_id, job_id,
    refund_points=None, charge_lookup=None,
):
    conn = _connection(db_factory)
    try:
        _project(conn, owner_username, project_id)
        row = conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs "
            "WHERE id=? AND project_id=?",
            (job_id, project_id),
        ).fetchone()
        if not row:
            raise LookupError("single-shot provider job does not exist")
        current = _provider_job(row)
        shared_owned = _matching_shared_xiaole_job(conn, row) is not None
    finally:
        conn.close()
    if shared_owned:
        return _reconcile_minimax_xiaole_job(db_factory, row)
    if current["status"] == "billing":
        conn = _connection(db_factory)
        try:
            attempt = _provider_attempt_for_job(conn, job_id)
        finally:
            conn.close()
        ledger = None
        ledger_checked = False
        if attempt and callable(charge_lookup):
            try:
                ledger = charge_lookup(attempt["charge_key"])
                ledger_checked = True
            except Exception:
                pass
        if attempt and _charge_ledger_matches(
            attempt["actor_username"], int(attempt["cost"]), ledger
        ):
            conn = _connection(db_factory)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE short_drama_provider_shot_attempts SET state='linked',"
                    "updated_at=? WHERE id=? AND state='accepted'",
                    (int(time.time()), attempt["id"]),
                )
                conn.execute(
                    "UPDATE short_drama_provider_shot_jobs SET status='queued',"
                    "progress=5,updated_at=? WHERE id=? AND status='billing'",
                    (int(time.time()), job_id),
                )
                conn.commit()
            finally:
                conn.close()
            current["status"] = "queued"
        else:
            age = int(time.time()) - int(current.get("created_at") or 0)
            error = current.get("error") or {}
            first_observed_at = int(error.get("first_observed_at") or 0)
            if (
                attempt
                and ledger_checked
                and ledger is None
                and age >= PROVIDER_BILLING_OBSERVE_AFTER_SECONDS
            ):
                observed_at = int(time.time())
                if not first_observed_at:
                    conn = _connection(db_factory)
                    try:
                        conn.execute(
                            "UPDATE short_drama_provider_shot_jobs "
                            "SET error_json=?,updated_at=? "
                            "WHERE id=? AND status='billing'",
                            (
                                _json_text({
                                    "code": "billing_ledger_not_found",
                                    "detail": "扣点流水暂未查到，等待二次权威确认",
                                    "first_observed_at": observed_at,
                                    "retryable": True,
                                }),
                                observed_at,
                                job_id,
                            ),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                elif (
                    observed_at - first_observed_at
                    >= PROVIDER_BILLING_CONFIRM_SECONDS
                ):
                    _mark_provider_attempt_failure(
                        db_factory,
                        attempt["id"],
                        job_id,
                        AutodraftError(
                            "billing_not_committed",
                            "两次权威查询均未发现扣点流水，任务已安全终止",
                            409,
                        ),
                        charged=False,
                        refund_points=refund_points,
                    )
                    conn = _connection(db_factory)
                    try:
                        return _provider_job(conn.execute(
                            "SELECT * FROM short_drama_provider_shot_jobs "
                            "WHERE id=?",
                            (job_id,),
                        ).fetchone())
                    finally:
                        conn.close()
            return current
    if current["status"] == "queued":
        conn = _connection(db_factory)
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET status='submitting',"
                "progress=10,updated_at=? WHERE id=? AND status='queued'",
                (int(time.time()), job_id),
            )
            conn.commit()
        finally:
            conn.close()
        if changed.rowcount == 1:
            provider = load_by_name(current.get("provider"))
            if provider is None or not provider.configured:
                error = AutodraftError(
                    "provider_not_configured",
                    "真实画面 Provider 配置已失效，任务未提交",
                    503,
                )
                _refund_provider_job(
                    db_factory, job_id, error, refund_points=refund_points
                )
            else:
                try:
                    submitted = provider.create_job(current["request"])
                    provider_job_id = str(
                        submitted.get("provider_job_id") or ""
                    ).strip()
                    conn = _connection(db_factory)
                    try:
                        conn.execute(
                            "UPDATE short_drama_provider_shot_jobs "
                            "SET status='running',progress=20,provider_job_id=?,"
                            "error_json=NULL,updated_at=? "
                            "WHERE id=? AND status='submitting'",
                            (provider_job_id, int(time.time()), job_id),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                except Exception as error:
                    if bool(getattr(error, "submitted", False)):
                        conn = _connection(db_factory)
                        try:
                            conn.execute(
                                "UPDATE short_drama_provider_shot_jobs "
                                "SET status='submit_unknown',error_json=?,updated_at=? "
                                "WHERE id=? AND status='submitting'",
                                (
                                    _json_text({
                                        "code": getattr(
                                            error, "code", "provider_submit_unknown"
                                        ),
                                        "detail": str(error)[:500],
                                        "requires_reconciliation": True,
                                    }),
                                    int(time.time()), job_id,
                                ),
                            )
                            conn.commit()
                        finally:
                            conn.close()
                    else:
                        _refund_provider_job(
                            db_factory, job_id, error,
                            refund_points=refund_points,
                        )
    conn = _connection(db_factory)
    try:
        row = conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    if row and row["status"] == "running":
        timeout_reason = _provider_job_timeout_reason(row)
        if timeout_reason:
            expired = _expire_provider_job(
                db_factory, job_id, timeout_reason,
                refund_points=refund_points,
            )
            if expired:
                row = None
        if row is not None:
            provider = load_by_name(row["provider"])
            if provider is None:
                return _provider_job(row)
            try:
                provider_state = provider.get_job(row["provider_job_id"])
                status = str(provider_state.get("status") or "unknown").lower()
                if status in {"completed", "complete", "succeeded", "success"}:
                    _finish_provider_job(db_factory, row, provider, provider_state)
                elif status in {"failed", "error", "canceled", "cancelled"}:
                    error = _provider_failure_error(provider_state)
                    _refund_provider_job(
                        db_factory, job_id, error, refund_points=refund_points
                    )
                else:
                    timeout_reason = _provider_job_timeout_reason(
                        row, next_poll=True,
                    )
                    if timeout_reason:
                        _expire_provider_job(
                            db_factory, job_id, timeout_reason,
                            refund_points=refund_points,
                        )
                    else:
                        conn = _connection(db_factory)
                        try:
                            conn.execute(
                                "UPDATE short_drama_provider_shot_jobs SET progress=?,"
                                "poll_count=poll_count+1,error_json=NULL,updated_at=? "
                                "WHERE id=? AND status='running'",
                                (
                                    min(90, 30 + int(row["poll_count"] or 0) * 5),
                                    int(time.time()), job_id,
                                ),
                            )
                            conn.commit()
                        finally:
                            conn.close()
            except Exception as error:
                code = getattr(error, "code", "provider_poll_failed")
                recovery_required = code == "provider_key_unavailable"
                timeout_reason = (
                    None if recovery_required else _provider_job_timeout_reason(
                        row, next_poll=True,
                    )
                )
                if timeout_reason:
                    _expire_provider_job(
                        db_factory, job_id, timeout_reason,
                        refund_points=refund_points,
                    )
                else:
                    conn = _connection(db_factory)
                    try:
                        conn.execute(
                            "UPDATE short_drama_provider_shot_jobs SET status=?,"
                            "poll_count=poll_count+1,error_json=?,updated_at=? "
                            "WHERE id=? AND status='running'",
                            (
                                "submit_unknown" if recovery_required else "running",
                                _json_text({
                                    "code": code,
                                    "detail": str(error)[:500],
                                    "retryable": not recovery_required,
                                    "requires_reconciliation": recovery_required,
                                }),
                                int(time.time()), job_id,
                            ),
                        )
                        conn.commit()
                    finally:
                        conn.close()
    _recover_provider_refund(
        db_factory, job_id, refund_points=refund_points,
        force=True,
    )
    conn = _connection(db_factory)
    try:
        return _attach_provider_attempt_state(conn, _provider_job(conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
            (job_id,),
        ).fetchone()))
    finally:
        conn.close()


def _job(row):
    if not row:
        return None
    item = dict(row)
    item["request"] = _json(item.pop("request_json"), {})
    item["result"] = _json(item.pop("result_json"), None)
    item["error"] = _json(item.pop("error_json"), None)
    item["progress"] = int(item["progress"])
    item["poll_count"] = int(item["poll_count"])
    item["cost"] = int(item["cost"])
    return item


def _version(row):
    if not row:
        return None
    item = dict(row)
    item["manifest"] = _json(item.pop("manifest_json"), {})
    item["version"] = int(item["version"])
    item["is_demo"] = (
        item["url"] == FALLBACK_URL
        or item["manifest"].get("artifact_kind") == "demo_placeholder"
    )
    return item


def _shot_cards(plan):
    shots = plan.get("material_plan") or (
        (plan.get("duration") or {}).get("shots") or []
    )
    assets = plan.get("assets") or []
    uses_recommendations = any(
        isinstance(item, dict) and item.get("source") == "system_recommendation"
        for item in assets
    )
    cards, issues = [], []
    for index, shot in enumerate(shots):
        shot_key = str(shot.get("shot_key") or "shot_%02d" % (index + 1))
        degraded = uses_recommendations and index == len(shots) - 1
        issue = None
        if degraded:
            issue = {
                "code": "safe_visual_fallback", "severity": "warning",
                "shot_key": shot_key,
                "message": "参考素材尚未绑定，已使用安全替代画面交付可播放草稿。",
                "recommended_action": "后续替换该镜头的角色或场景参考素材。",
            }
            issues.append(issue)
        cards.append({
            "shot_key": shot_key,
            "sort_order": int(shot.get("sort_order") or index + 1),
            "start_ms": int(shot.get("start_ms") or 0),
            "end_ms": int(shot.get("end_ms") or 0),
            "status": "degraded" if degraded else "ready",
            "visual_source": "demo_placeholder",
            "scene": str(shot.get("scene") or ""),
            "visual_prompt": str(shot.get("visual_prompt") or ""),
            "dialogue": shot.get("dialogue") or [],
            "input_hash": str(shot.get("input_hash") or ""),
            "issue": issue,
        })
    return cards, issues


def _complete(conn, row):
    current = _job(row)
    if current["status"] not in ACTIVE:
        return current
    plan = _confirmed_plan(conn, current["project_id"], current["plan_id"])
    if current["request"].get("production_mode") == "provider_assembly":
        project_row = conn.execute(
            "SELECT id,title,ratio,target_duration,shot_count,point_budget,spent_points "
            "FROM short_drama_projects WHERE id=? AND deleted=0",
            (current["project_id"],),
        ).fetchone()
        project = dict(project_row) if project_row else None
        if not project:
            raise AutodraftError("project_missing", "短剧项目不存在", 404)
        duration_ms = sum(
            int(item.get("duration_ms") or 0)
            for item in plan["plan"].get("material_plan") or []
            if isinstance(item, dict)
        ) or int((plan["plan"].get("duration") or {}).get("target_ms") or 0)
        media_contract = _locked_media_contract(conn, project)
        assembly = {
            "all_ready": True,
            "shots": list(current["request"].get("provider_assets") or []),
            "ratio": project["ratio"],
            "duration_ms": duration_ms,
            "media_contract": media_contract,
        }
        rendered = _render_provider_preview(
            current["project_id"], current["id"], assembly
        )
        rendered_duration_ms = int(rendered.get("duration_ms") or duration_ms)
        material = {
            str(item.get("shot_key") or ""): item
            for item in plan["plan"].get("material_plan") or []
            if isinstance(item, dict)
        }
        cards = []
        for index, asset in enumerate(assembly["shots"]):
            shot_key = str(asset.get("shot_key") or "")
            source = material.get(shot_key) or {}
            native_media = _sanitized_native_media(asset.get("native_media"))
            selected_file = str(asset.get("file") or "").replace("\\", "/")
            selected_evidence = next((
                native_media[key] for key in ("raw", "derived")
                if native_media and native_media[key]["file"] == selected_file
            ), {})
            cards.append({
                "shot_key": shot_key,
                "sort_order": int(source.get("sort_order") or index + 1),
                "start_ms": int(source.get("start_ms") or 0),
                "end_ms": int(source.get("end_ms") or 0),
                "status": "ready",
                "visual_source": "provider",
                "provider": str(asset.get("provider") or ""),
                "provider_version_id": str(asset.get("id") or ""),
                "provider_version": int(asset.get("version") or 0),
                "provider_job_id": str(asset.get("provider_job_id") or ""),
                "file": str(asset.get("file") or ""),
                "url": str(asset.get("url") or ""),
                "file_hash": str(selected_evidence.get("sha256") or ""),
                "native_media": native_media,
                "input_hash": str(asset.get("input_hash") or ""),
                "issue": None,
            })
        version_number = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_autodraft_versions "
            "WHERE project_id=?", (current["project_id"],)
        ).fetchone()[0])
        version_id = uuid.uuid4().hex
        media_issues = [] if media_contract["delivery_eligible"] else [{
            "code": media_contract["reason"], "severity": "error",
            "message": "缺少完整锁定的真实音轨或字幕时间线，不能进入正式验收",
            "recommended_action": "锁定配音与字幕时间线后重新生成自动草稿。",
        }]
        media_contract["material_hash"] = _hash([{
            "id": item.get("id"), "shot_key": item.get("shot_key"),
            "input_hash": item.get("input_hash"),
        } for item in assembly["shots"]])
        manifest = {
            "contract_version": "standalone-autodraft-v2",
            "artifact_kind": "provider_assembly_preview",
            "production_mode": "provider_assembly",
            "plan_id": plan["id"],
            "plan_version": int(plan["version"]),
            "resolution": "1080p",
            "duration_ms": rendered_duration_ms,
            "playback_url": rendered["url"],
            "playback_file": rendered["file"],
            "shots": cards,
            "issues": media_issues,
            "ratio": project["ratio"],
            "media_contract": media_contract,
            "media_validation": rendered["probe"],
            "degradation_policy": "no_implicit_fallback",
        }
        now = int(time.time())
        result = {
            "version_id": version_id, "version": version_number,
            "url": rendered["url"],
            "status": "ready" if not media_issues else "degraded",
            "issues": media_issues, "shot_cards": cards,
        }
        conn.execute(
            "INSERT INTO short_drama_autodraft_versions "
            "(id,project_id,job_id,version,plan_id,status,url,manifest_json,input_hash,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                version_id, current["project_id"], current["id"], version_number,
                plan["id"], result["status"], rendered["url"], _json_text(manifest),
                current["input_hash"], now,
            ),
        )
        conn.execute(
            "UPDATE short_drama_autodraft_jobs SET status='succeeded',"
            "phase='completed',progress=100,result_json=?,updated_at=? "
            "WHERE id=? AND status IN ('queued','running')",
            (_json_text(result), now, current["id"]),
        )
        return _job(conn.execute(
            "SELECT * FROM short_drama_autodraft_jobs WHERE id=?", (current["id"],)
        ).fetchone())
    cards, issues = _shot_cards(plan["plan"])
    version_number = int(conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_autodraft_versions "
        "WHERE project_id=?", (current["project_id"],)
    ).fetchone()[0])
    version_id = uuid.uuid4().hex
    manifest = {
        "contract_version": "standalone-autodraft-v1",
        "artifact_kind": "demo_placeholder",
        "production_mode": "demo",
        "plan_id": plan["id"], "plan_version": int(plan["version"]),
        "resolution": "720p",
        "duration_ms": int((plan["plan"].get("duration") or {}).get("target_ms") or 0),
        "playback_url": FALLBACK_URL, "shots": cards, "issues": issues,
        "degradation_policy": "demo_only",
    }
    final_status = "degraded" if issues else "succeeded"
    version_status = "degraded" if issues else "ready"
    now = int(time.time())
    result = {
        "version_id": version_id, "version": version_number,
        "url": FALLBACK_URL, "status": version_status,
        "issues": issues, "shot_cards": cards,
    }
    conn.execute(
        "INSERT INTO short_drama_autodraft_versions "
        "(id,project_id,job_id,version,plan_id,status,url,manifest_json,input_hash,"
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            version_id, current["project_id"], current["id"], version_number,
            plan["id"], version_status, FALLBACK_URL, _json_text(manifest),
            current["input_hash"], now,
        ),
    )
    conn.execute(
        "UPDATE short_drama_autodraft_jobs SET status=?,phase='completed',"
        "progress=100,result_json=?,updated_at=? WHERE id=? AND status IN "
        "('queued','running')",
        (final_status, _json_text(result), now, current["id"]),
    )
    return _job(conn.execute(
        "SELECT * FROM short_drama_autodraft_jobs WHERE id=?", (current["id"],)
    ).fetchone())


def _advance(conn, row):
    item = _job(row)
    if not item or item["status"] not in ACTIVE:
        return item
    poll = item["poll_count"] + 1
    phase, progress = PHASES[min(poll, len(PHASES) - 1)]
    if phase == "completed":
        conn.execute("SAVEPOINT autodraft_complete")
        try:
            completed = _complete(conn, row)
            conn.execute("RELEASE SAVEPOINT autodraft_complete")
            return completed
        except Exception as error:
            conn.execute("ROLLBACK TO SAVEPOINT autodraft_complete")
            conn.execute("RELEASE SAVEPOINT autodraft_complete")
            request = item.get("request") or {}
            target = (
                _content_root() / "short_drama_autodraft" / item["project_id"] /
                item["id"]
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
                "code": getattr(error, "code", "autodraft_completion_failed"),
                "detail": str(error)[:500],
                "retryable": True,
                "stage": "finishing",
                "temporary_output_cleaned": not any(
                    path.exists() for path in cleanup_targets
                ),
                "compensation": (
                    "provider_assets_retained_no_automatic_refund"
                    if request.get("production_mode") == "provider_assembly"
                    else "not_applicable"
                ),
            }
            if cleanup_error:
                failure["cleanup_error"] = cleanup_error
            now = int(time.time())
            conn.execute(
                "UPDATE short_drama_autodraft_jobs SET status='failed',"
                "phase='failed',progress=100,poll_count=?,result_json=NULL,"
                "error_json=?,updated_at=? WHERE id=? AND status IN "
                "('queued','running')",
                (poll, _json_text(failure), now, item["id"]),
            )
            return _job(conn.execute(
                "SELECT * FROM short_drama_autodraft_jobs WHERE id=?",
                (item["id"],),
            ).fetchone())
    conn.execute(
        "UPDATE short_drama_autodraft_jobs SET status='running',phase=?,progress=?,"
        "poll_count=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
        (phase, progress, poll, int(time.time()), item["id"]),
    )
    return _job(conn.execute(
        "SELECT * FROM short_drama_autodraft_jobs WHERE id=?", (item["id"],)
    ).fetchone())


def _versions(conn, project_id):
    return [
        _version(row) for row in conn.execute(
            "SELECT * FROM short_drama_autodraft_versions WHERE project_id=? "
            "ORDER BY version DESC", (project_id,)
        ).fetchall()
    ]


def workspace(
    db_factory, owner_username, actor_username, project_id, can_edit=True,
    avatar_list=None, refund_points=None,
):
    _recover_project_provider_refunds(
        db_factory, project_id, refund_points=refund_points,
    )
    conn = _connection(db_factory)
    try:
        project = _project(conn, owner_username, project_id)
        try:
            plan = _confirmed_plan(conn, project_id)
        except AutodraftError:
            plan = None
        row = conn.execute(
            "SELECT * FROM short_drama_autodraft_jobs WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT 1", (project_id,)
        ).fetchone()
        current = _advance(conn, row) if row else None
        conn.commit()
        all_versions = _versions(conn, project_id)
        provider_jobs = [
            _minimax_runtime_diagnostics(
                _attach_provider_attempt_state(conn, _provider_job(provider_row))
            )
            for provider_row in conn.execute(
                "SELECT j.* FROM short_drama_provider_shot_jobs j "
                "WHERE j.project_id=? AND (j.status IN "
                "('billing','queued','submitting','running','submit_unknown') "
                "OR NOT EXISTS (SELECT 1 FROM short_drama_provider_shot_jobs newer "
                "WHERE newer.project_id=j.project_id AND newer.shot_key=j.shot_key "
                "AND (newer.created_at>j.created_at OR "
                "(newer.created_at=j.created_at AND newer.id>j.id)))) "
                "ORDER BY CASE WHEN j.status IN "
                "('billing','queued','submitting','running','submit_unknown') "
                "THEN 0 ELSE 1 END,j.created_at DESC,j.id DESC",
                (project_id,),
            ).fetchall()
        ]
        provider_job = provider_jobs[0] if provider_jobs else None
        provider_versions = [
            _provider_version(row) for row in conn.execute(
                "SELECT v.*,j.request_json,j.result_json,"
                "CASE WHEN s.version_id=v.id THEN 1 ELSE 0 END selected "
                "FROM short_drama_provider_shot_versions v "
                "JOIN short_drama_provider_shot_jobs j ON j.id=v.job_id "
                "LEFT JOIN short_drama_provider_shot_selections s "
                "ON s.project_id=v.project_id AND s.shot_key=v.shot_key "
                "WHERE v.project_id=? ORDER BY v.created_at DESC",
                (project_id,),
            ).fetchall()
        ]
        provider_execution_overrides = {
            str(row["shot_key"]): dict(
                _json(row["execution_json"], {}),
                updated_at=int(row["updated_at"]),
            )
            for row in conn.execute(
                "SELECT shot_key,execution_json,updated_at "
                "FROM short_drama_provider_shot_execution_overrides "
                "WHERE project_id=?", (project_id,),
            ).fetchall()
        }
        capability = _production_capability()
        selected_provider = str(
            (capability.get("provider") or {}).get("selected") or ""
        )
        assembly = (
            _provider_assembly_snapshot(
                conn, project_id, plan["plan"], selected_provider
            )
            if plan else {
                "required_shot_keys": [], "ready_shot_keys": [],
                "required_count": 0, "ready_count": 0,
                "missing_shot_keys": [], "assets_ready": False,
                "quality_ready": True, "low_resolution_shot_keys": [],
                "all_ready": False, "shots": [],
                "continuity_required_count": 0,
                "continuity_ready_count": 0,
                "continuity_ready_shot_keys": [],
                "continuity_missing_shot_keys": [],
                "continuity_ready": True,
            }
        )
        if capability["mode"] == "provider_poc":
            capability["assembly"] = {
                key: value for key, value in assembly.items() if key != "shots"
            }
            capability["ready"] = bool(assembly["all_ready"])
            if assembly["all_ready"]:
                capability["message"] = "全部镜头已生成，可合成 1080p 草稿"
            elif assembly.get("low_resolution_shot_keys"):
                capability["message"] = (
                    "历史 768p 镜头不会冒充原生 2K；请重新生成："
                    + "、".join(assembly["low_resolution_shot_keys"])
                )
            else:
                capability["message"] = (
                    "请先完成全部镜头生成，再合成 1080p 草稿"
                )
        versions = (
            all_versions
            if capability["mode"] == "demo"
            else [item for item in all_versions if not item["is_demo"]]
        )
        state = (
            "producing" if current and current["status"] in ACTIVE
            else "draft_ready" if versions
            else "ready_to_start" if plan
            else "plan_required"
        )
        return {
            "project": project, "state": state,
            "confirmed_plan": ({
                "id": plan["id"], "version": int(plan["version"]),
                "quality_route": plan["quality_route"], "plan": plan["plan"],
            } if plan else None),
            "current_job": current,
            "current_version": versions[0] if versions else None,
            "versions": versions,
            "permissions": {"can_edit": bool(can_edit), "actor": actor_username},
            "billing": {
                "cost": (
                    0 if capability["mode"] == "provider_poc"
                    else _cost(plan["plan"]) if plan else 0
                ),
                "mode": (
                    "provider_assets_already_charged"
                    if capability["mode"] == "provider_poc"
                    else "development_free"
                    if os.getenv("HQ_SHORT_DRAMA_AUTODRAFT_DEV_FREE") == "1"
                    else "charged_on_start"
                ),
            },
            "production": capability,
            "provider_job": provider_job,
            "provider_jobs": provider_jobs,
            "provider_versions": provider_versions,
            "provider_execution_overrides": provider_execution_overrides,
            "provider_poc": (
                _provider_poc_inputs(
                    plan["plan"], owner_username, avatar_list,
                    conn=conn, project_id=project_id,
                )
                if plan else None
            ),
        }
    finally:
        conn.close()


def start_job(
    db_factory, owner_username, actor_username, body, idempotency_key,
    deduct_points=None, refund_points=None, charge_lookup=None,
    project_usage=None,
):
    project_id = str(body.get("project_id") or "").strip()
    plan_id = str(body.get("plan_id") or "").strip()
    key = _key(idempotency_key)
    now = int(time.time())
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner_username, project_id)
        plan = _confirmed_plan(conn, project_id, plan_id)
        capability = _production_capability()
        selected_provider = str(
            (capability.get("provider") or {}).get("selected") or ""
        )
        binding_blockers = _character_binding_blockers(
            conn, project_id, plan["plan"], selected_provider
        )
        if binding_blockers:
            raise AutodraftError(
                "character_bindings_incomplete",
                "请先为所有出镜和说话角色准备可用的锁定形象",
                422,
            )
        assembly = _provider_assembly_snapshot(
            conn, project_id, plan["plan"], selected_provider
        )
        provider_assembly = (
            capability["mode"] == "provider_poc" and assembly["all_ready"]
        )
        if not capability["ready"] and not provider_assembly:
            if capability["mode"] == "provider_poc" and assembly.get(
                "low_resolution_shot_keys"
            ):
                detail = (
                    "以下镜头是历史 768p 版本，请重新生成原生 2K 后再合成："
                    + "、".join(assembly["low_resolution_shot_keys"])
                )
            else:
                detail = (
                    "请先完成全部镜头生成；缺少："
                    + "、".join(assembly["missing_shot_keys"])
                    if capability["mode"] == "provider_poc"
                    else capability["message"]
                )
            raise AutodraftError(
                "provider_shots_incomplete"
                if capability["mode"] == "provider_poc"
                else "autodraft_provider_unavailable",
                detail,
                409 if capability["mode"] == "provider_poc" else 503,
            )
        request_hash = _hash({
            "project_id": project_id, "plan_id": plan["id"],
            "plan_hash": plan["input_hash"],
            "provider_versions": [
                {
                    "id": item["id"], "shot_key": item["shot_key"],
                    "input_hash": item["input_hash"],
                }
                for item in assembly["shots"]
            ] if provider_assembly else [],
        })
        existing = conn.execute(
            "SELECT * FROM short_drama_autodraft_attempts "
            "WHERE actor_username=? AND idempotency_key=?",
            (actor_username, key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise AutodraftError(
                    "idempotency_conflict", "该幂等键已用于不同的自动草稿请求", 409
                )
            if existing["job_id"]:
                result = _job(conn.execute(
                    "SELECT * FROM short_drama_autodraft_jobs WHERE id=?",
                    (existing["job_id"],),
                ).fetchone())
                conn.commit()
                result["replayed"] = True
                return result
            raise AutodraftError(
                "charge_recovery_pending", "扣点状态正在恢复，请稍后重试", 409
            )
        if conn.execute(
            "SELECT 1 FROM short_drama_autodraft_jobs WHERE project_id=? "
            "AND status IN ('queued','running')", (project_id,)
        ).fetchone():
            raise AutodraftError(
                "active_autodraft_job", "项目已有自动草稿任务处理中", 409
            )
        cost = 0 if provider_assembly else _cost(plan["plan"])
        project = _project(conn, owner_username, project_id)
        if callable(project_usage):
            usage = project_usage(conn, project_id)
        else:
            usage = {
                "spent_points": int(project.get("spent_points") or 0),
                "reserved_points": 0,
            }
        budget = int(project.get("point_budget") or 0)
        if (
            budget
            and int(usage.get("spent_points") or 0)
            + int(usage.get("reserved_points") or 0)
            + cost
            > budget
        ):
            raise AutodraftError(
                "point_budget_exceeded",
                "项目点数预算不足，请提高预算或选择更低成本路线",
                409,
            )
        attempt_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO short_drama_autodraft_attempts "
            "(id,actor_username,owner_username,project_id,idempotency_key,"
            "request_hash,plan_id,cost,charge_key,refund_key,state,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'accepted',?,?)",
            (
                attempt_id, actor_username, owner_username, project_id, key,
                request_hash, plan["id"], cost,
                "short-drama-autodraft-charge:" + attempt_id,
                "short-drama-autodraft-refund:" + attempt_id, now, now,
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
        if cost:
            if not callable(deduct_points):
                raise AutodraftError(
                    "billing_unavailable", "自动草稿扣点服务暂不可用", 503
                )
            charge_key = "short-drama-autodraft-charge:" + attempt_id
            try:
                deduct_points(
                    actor_username, cost, "短剧自动草稿", charge_key,
                )
            except Exception:
                ledger = None
                if callable(charge_lookup):
                    try:
                        ledger = charge_lookup(charge_key)
                    except Exception:
                        pass
                if not _charge_ledger_matches(actor_username, cost, ledger):
                    raise
            charged = True
        job_conn = _connection(db_factory)
        job_conn.execute("BEGIN IMMEDIATE")
        job_conn.execute(
            "UPDATE short_drama_autodraft_attempts SET state='charged',updated_at=? "
            "WHERE id=? AND state='accepted'", (int(time.time()), attempt_id)
        )
        job_id = uuid.uuid4().hex
        request = {
            "contract_version": "standalone-autodraft-v1",
            "plan_id": plan["id"], "plan_version": int(plan["version"]),
            "quality_route": plan["quality_route"],
            "production_mode": (
                "provider_assembly" if provider_assembly else "demo"
            ),
            "provider_assets": assembly["shots"] if provider_assembly else [],
        }
        job_conn.execute(
            "INSERT INTO short_drama_autodraft_jobs "
            "(id,project_id,owner_username,actor_username,plan_id,status,phase,"
            "progress,poll_count,input_hash,request_json,cost,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'queued','queued',5,0,?,?,?,?,?)",
            (
                job_id, project_id, owner_username, actor_username, plan["id"],
                request_hash, _json_text(request), cost, now, now,
            ),
        )
        job_conn.execute(
            "UPDATE short_drama_autodraft_attempts SET state='linked',job_id=?,"
            "updated_at=? WHERE id=? AND state='charged' AND job_id IS NULL",
            (job_id, int(time.time()), attempt_id),
        )
        job_conn.commit()
        result = _job(job_conn.execute(
            "SELECT * FROM short_drama_autodraft_jobs WHERE id=?", (job_id,)
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
                    actor_username, cost, "短剧自动草稿建单失败补偿",
                    "short-drama-autodraft-refund:" + attempt_id,
                )
                state = "refunded"
            except Exception:
                state = "refund_pending"
        recovery = _connection(db_factory)
        try:
            recovery.execute(
                "UPDATE short_drama_autodraft_attempts SET state=?,error=?,"
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


def get_job(db_factory, owner_username, project_id, job_id):
    conn = _connection(db_factory)
    try:
        _project(conn, owner_username, project_id)
        row = conn.execute(
            "SELECT * FROM short_drama_autodraft_jobs WHERE id=? AND project_id=?",
            (job_id, project_id),
        ).fetchone()
        if not row:
            raise LookupError("automatic draft job does not exist")
        result = _advance(conn, row)
        conn.commit()
        return result
    finally:
        conn.close()


def retry_job(db_factory, owner_username, actor_username, body):
    project_id = str(body.get("project_id") or "").strip()
    job_id = str(body.get("job_id") or "").strip()
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner_username, project_id)
        current = _job(conn.execute(
            "SELECT * FROM short_drama_autodraft_jobs WHERE id=? AND project_id=?",
            (job_id, project_id),
        ).fetchone())
        if not current or current["status"] not in {"failed", "canceled"}:
            raise AutodraftError("job_not_retryable", "当前任务不能重试", 409)
        if conn.execute(
            "SELECT 1 FROM short_drama_autodraft_jobs WHERE project_id=? "
            "AND status IN ('queued','running')", (project_id,)
        ).fetchone():
            raise AutodraftError("active_autodraft_job", "已有任务处理中", 409)
        conn.execute(
            "UPDATE short_drama_autodraft_jobs SET status='queued',phase='queued',"
            "progress=5,poll_count=0,error_json=NULL,actor_username=?,updated_at=? "
            "WHERE id=?", (actor_username, int(time.time()), job_id)
        )
        conn.commit()
        return _job(conn.execute(
            "SELECT * FROM short_drama_autodraft_jobs WHERE id=?", (job_id,)
        ).fetchone())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_job(db_factory, owner_username, body):
    project_id = str(body.get("project_id") or "").strip()
    job_id = str(body.get("job_id") or "").strip()
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, owner_username, project_id)
        updated = conn.execute(
            "UPDATE short_drama_autodraft_jobs SET status='canceled',"
            "phase='canceled',updated_at=? WHERE id=? AND project_id=? "
            "AND status IN ('queued','running')",
            (int(time.time()), job_id, project_id),
        )
        if updated.rowcount != 1:
            raise AutodraftError("job_not_cancelable", "当前任务不能取消", 409)
        conn.commit()
        return _job(conn.execute(
            "SELECT * FROM short_drama_autodraft_jobs WHERE id=?", (job_id,)
        ).fetchone())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
