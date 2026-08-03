"""Read models and persistence contracts for short-drama assembly."""

import json
import sqlite3
import time
import uuid
from contextlib import closing

from . import short_drama_assembly_plan as media_plan
from . import short_drama_assembly_artifacts as assembly_artifacts
from . import short_drama_assembly_lipsync as lipsync_assembly
from . import short_drama_master_audio as master_audio
from . import short_drama_alignment as subtitle_alignment


ASSEMBLY_STAGES = {"assembly_review", "completed"}
COMPOSITION_KINDS = {"preview", "final"}
FINAL_CHARGE_RECOVERY_SECONDS = 120
FINAL_CHARGE_LEDGER_ABSENT_ONCE = "final_charge_ledger_absent_once"

DEFAULT_ASSEMBLY_CONFIG = {
    "subtitle": {
        "enabled": True,
        "preset": "white_outline",
        "position": "bottom",
        "delivery": "external_vtt",
    },
    "bgm": {
        "asset_id": None,
        "volume": 0.18,
        "fade_in_ms": 500,
        "fade_out_ms": 800,
    },
    "sound_cues": [],
    "profiles": {
        "preview": "short_drama_preview_v1",
        "final": "short_drama_final_v1",
    },
}

_BLOCKER_MESSAGES = {
    "missing_locked_voice_shot": "镜头尚未锁定配音与字幕",
    "missing_locked_video_shot": "镜头尚无已确认的电影化身视频版本",
    "missing_current_voice_version": "台词缺少可用的当前配音版本",
    "missing_current_video_version": "镜头缺少可用的当前视频版本",
    "stale_voice_version": "当前配音版本已过期，请重新生成并锁定",
    "stale_video_version": "当前视频版本已过期，请重新生成并确认",
    "missing_source_file": "服务端媒体源文件不存在",
    "source_file_untrusted": "媒体文件不在服务端受控输出目录中",
    "ffprobe_unavailable": "服务器未安装或无法调用 FFprobe",
    "media_probe_failed": "媒体文件无法完成规格探测",
    "ratio_mismatch": "视频画幅与项目画幅不一致",
    "clip_duration_mismatch": "媒体文件时长与已确认版本元数据不一致",
    "timeline_invalid": "配音或字幕时间线无效或超出镜头范围",
    "audio_overlap": "同一镜头内存在配音区间重叠",
    "subtitle_overlap": "同一镜头内存在字幕区间重叠",
    "project_duration_mismatch": "镜头总时长与项目目标时长不一致",
    "source_changed_during_probe": "媒体源在探测期间发生变化，请重试",
    "missing_d1_media_plan": "D-1 媒体计划尚未就绪",
    "bgm_asset_missing": "背景音乐资产不存在或已删除",
    "bgm_asset_forbidden": "当前项目无权使用该背景音乐",
    "bgm_source_untrusted": "背景音乐文件不在服务端受控目录中",
    "bgm_probe_failed": "背景音乐文件无法完成规格探测",
    "sound_asset_missing": "音效资产不存在或已删除",
    "sound_asset_forbidden": "当前项目无权使用该音效资产",
    "sound_source_untrusted": "音效文件不在服务端受控目录中",
    "sound_probe_failed": "音效文件无法完成规格探测",
    "sound_cue_invalid": "音效时间线配置无效或超出所属镜头",
    "active_composition_job": "项目仍有合成任务处理中",
    "preview_missing": "尚未生成可用预览版本",
    "final_missing": "尚未生成可用正式成片",
}

_BLOCKER_MESSAGES.update({
    "lipsync_source_hash_mismatch":
        "口型素材实际文件与不可变合成清单不一致",
    "lipsync_manifest_invalid": "口型合成证据清单格式无效",
    "lipsync_manifest_mismatch":
        "口型合成证据清单与不可变素材清单不一致",
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_compositions (
  project_id TEXT PRIMARY KEY
    REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  assembly_revision INTEGER NOT NULL DEFAULT 1
    CHECK (assembly_revision >= 1),
  config_json TEXT NOT NULL DEFAULT '{}',
  current_preview_version INTEGER,
  current_final_version INTEGER,
  preview_locked INTEGER NOT NULL DEFAULT 0
    CHECK (preview_locked IN (0,1)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS short_drama_composition_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL
    REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('preview','final')),
  version INTEGER NOT NULL CHECK (version >= 1),
  job_id TEXT NOT NULL UNIQUE,
  input_hash TEXT NOT NULL,
  config_json TEXT NOT NULL,
  plan_hash TEXT NOT NULL DEFAULT '',
  manifest_hash TEXT NOT NULL DEFAULT '',
  manifest_json TEXT NOT NULL DEFAULT '{}',
  file TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  cover_file TEXT NOT NULL DEFAULT '',
  duration_ms INTEGER CHECK (
    duration_ms IS NULL OR (typeof(duration_ms)='integer' AND duration_ms > 0)
  ),
  width INTEGER CHECK (
    width IS NULL OR (typeof(width)='integer' AND width > 0)
  ),
  height INTEGER CHECK (
    height IS NULL OR (typeof(height)='integer' AND height > 0)
  ),
  fps REAL CHECK (fps IS NULL OR fps > 0),
  video_codec TEXT NOT NULL DEFAULT '',
  audio_codec TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL
    CHECK (status IN ('rendering','succeeded','failed','stale')),
  global_video_asset_id INTEGER UNIQUE,
  created_at INTEGER NOT NULL,
  UNIQUE(project_id, kind, version)
);

CREATE TABLE IF NOT EXISTS short_drama_composition_jobs (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  project_id TEXT NOT NULL
    REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('preview','final')),
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('queued','running','succeeded','failed')
  ),
  phase TEXT NOT NULL DEFAULT 'queued',
  progress INTEGER NOT NULL DEFAULT 0
    CHECK (progress BETWEEN 0 AND 100),
  error_code TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  attempt_count INTEGER NOT NULL DEFAULT 0
    CHECK (attempt_count >= 0),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  finished_at INTEGER,
  UNIQUE(username, kind, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_short_drama_composition_versions_project
  ON short_drama_composition_versions(project_id, kind, version DESC);
CREATE INDEX IF NOT EXISTS idx_short_drama_composition_jobs_project
  ON short_drama_composition_jobs(username, project_id, status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_short_drama_composition_jobs_active
  ON short_drama_composition_jobs(project_id)
  WHERE status IN ('queued','running');

CREATE TABLE IF NOT EXISTS short_drama_final_quotes (
  token TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  assembly_revision INTEGER NOT NULL,
  preview_version INTEGER NOT NULL,
  input_hash TEXT NOT NULL,
  cover_time_ms INTEGER NOT NULL,
  cost INTEGER NOT NULL CHECK (cost >= 0),
  expires_at INTEGER NOT NULL,
  consumed_job_id TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS short_drama_final_attempts (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  owner_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  quote_token TEXT NOT NULL,
  cost INTEGER NOT NULL CHECK (cost >= 0),
  charge_key TEXT NOT NULL UNIQUE,
  refund_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL,
  job_id TEXT UNIQUE,
  asset_id TEXT UNIQUE,
  error TEXT NOT NULL DEFAULT '',
  recovery_token TEXT,
  recovery_started_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(actor_username,idempotency_key)
);

CREATE TABLE IF NOT EXISTS short_drama_final_assets (
  id TEXT PRIMARY KEY,
  owner_username TEXT NOT NULL,
  created_by TEXT NOT NULL,
  project_id TEXT,
  composition_version_id TEXT NOT NULL UNIQUE,
  job_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  object_key TEXT NOT NULL,
  cover_key TEXT NOT NULL,
  video_url TEXT NOT NULL DEFAULT '',
  cover_url TEXT NOT NULL DEFAULT '',
  mime TEXT NOT NULL DEFAULT 'video/mp4',
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  fps REAL NOT NULL,
  duration_ms INTEGER NOT NULL,
  video_codec TEXT NOT NULL,
  audio_codec TEXT NOT NULL,
  archive_status TEXT NOT NULL DEFAULT 'ready',
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_final_assets_owner
  ON short_drama_final_assets(owner_username,archive_status,created_at DESC);
"""


class PreviewIdempotencyConflict(ValueError):
    pass


class ActiveCompositionJob(ValueError):
    pass


class PreviewBlocked(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _require_render_environment():
    """Fail before creating a composition task when subtitle fonts are absent."""
    from . import short_drama_assembly_subtitles as subtitles

    try:
        return subtitles.inspect_font()
    except subtitles.SubtitleError as error:
        raise PreviewBlocked(error.code, str(error)) from error


def init_db(db_factory):
    with closing(db_factory()) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        version_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(short_drama_composition_versions)"
            )
        }
        for name, declaration in {
            "cover_url": "TEXT NOT NULL DEFAULT ''",
            "object_key": "TEXT NOT NULL DEFAULT ''",
            "cover_key": "TEXT NOT NULL DEFAULT ''",
            "sha256": "TEXT NOT NULL DEFAULT ''",
            "size": "INTEGER",
            "plan_hash": "TEXT NOT NULL DEFAULT ''",
            "manifest_hash": "TEXT NOT NULL DEFAULT ''",
            "manifest_json": "TEXT NOT NULL DEFAULT '{}'",
        }.items():
            if name not in version_columns:
                conn.execute(
                    "ALTER TABLE short_drama_composition_versions "
                    "ADD COLUMN %s %s" % (name, declaration)
                )
        attempt_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(short_drama_final_attempts)"
            )
        }
        for name, declaration in {
            "recovery_token": "TEXT",
            "recovery_started_at": "INTEGER",
        }.items():
            if name not in attempt_columns:
                conn.execute(
                    "ALTER TABLE short_drama_final_attempts "
                    "ADD COLUMN %s %s" % (name, declaration)
                )
        conn.commit()
    assembly_artifacts.init_db(db_factory)
    lipsync_assembly.init_db(db_factory)


def _json_value(value, default):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _merge_config(value):
    saved = _json_value(value, {}) if value is not None else {}
    config = {
        "subtitle": dict(DEFAULT_ASSEMBLY_CONFIG["subtitle"]),
        "bgm": dict(DEFAULT_ASSEMBLY_CONFIG["bgm"]),
        "sound_cues": [],
        "profiles": dict(DEFAULT_ASSEMBLY_CONFIG["profiles"]),
    }
    for section in ("subtitle", "bgm", "profiles"):
        candidate = saved.get(section)
        if isinstance(candidate, dict):
            config[section].update(candidate)
    if isinstance(saved.get("sound_cues"), list):
        config["sound_cues"] = [
            dict(item) for item in saved["sound_cues"]
            if isinstance(item, dict)
        ]
    return config


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _normalize_sound_cues(value, shot_durations):
    if not isinstance(value, list) or len(value) > 120:
        raise ValueError("音效列表必须是数组且最多 120 条")
    allowed_kinds = {"ambience", "foley", "transition", "impact"}
    result = []
    identities = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict) or set(raw) != {
            "id", "shot_id", "kind", "asset_id", "start_ms", "end_ms",
            "loop", "volume", "fade_in_ms", "fade_out_ms", "enabled",
        }:
            raise ValueError("第 %d 条音效字段不正确" % (index + 1))
        cue_id = str(raw.get("id") or "").strip()
        shot_id = str(raw.get("shot_id") or "").strip()
        kind = str(raw.get("kind") or "").strip()
        volume = _finite_number(raw.get("volume"))
        if (
            not cue_id
            or len(cue_id) > 80
            or cue_id in identities
            or shot_id not in shot_durations
            or kind not in allowed_kinds
            or type(raw.get("asset_id")) is not int
            or raw["asset_id"] <= 0
            or type(raw.get("start_ms")) is not int
            or type(raw.get("end_ms")) is not int
            or raw["start_ms"] < 0
            or raw["end_ms"] <= raw["start_ms"]
            or raw["end_ms"] > shot_durations[shot_id]
            or type(raw.get("loop")) is not bool
            or volume is None
            or volume < 0
            or volume > 1
            or type(raw.get("fade_in_ms")) is not int
            or type(raw.get("fade_out_ms")) is not int
            or raw["fade_in_ms"] < 0
            or raw["fade_out_ms"] < 0
            or raw["fade_in_ms"] + raw["fade_out_ms"]
            > raw["end_ms"] - raw["start_ms"]
            or type(raw.get("enabled")) is not bool
        ):
            raise ValueError("第 %d 条音效时间线配置无效" % (index + 1))
        identities.add(cue_id)
        result.append({
            "id": cue_id,
            "shot_id": shot_id,
            "kind": kind,
            "asset_id": raw["asset_id"],
            "start_ms": raw["start_ms"],
            "end_ms": raw["end_ms"],
            "loop": raw["loop"],
            "volume": round(volume, 4),
            "fade_in_ms": raw["fade_in_ms"],
            "fade_out_ms": raw["fade_out_ms"],
            "enabled": raw["enabled"],
        })
    return result


def _normalize_editable_config(value, shot_durations):
    if not isinstance(value, dict) or set(value) != {
        "subtitle", "bgm", "sound_cues"
    }:
        raise ValueError("装配配置字段不正确")
    subtitle = value.get("subtitle")
    bgm = value.get("bgm")
    if not isinstance(subtitle, dict) or set(subtitle) != {
        "enabled", "preset", "position"
    }:
        raise ValueError("字幕配置字段不正确")
    if (
        type(subtitle.get("enabled")) is not bool
        or str(subtitle.get("preset") or "") not in {"white_outline"}
        or str(subtitle.get("position") or "") not in {"bottom", "middle"}
    ):
        raise ValueError("字幕配置无效")
    if not isinstance(bgm, dict) or set(bgm) != {
        "asset_id", "volume", "fade_in_ms", "fade_out_ms"
    }:
        raise ValueError("背景音乐配置字段不正确")
    bgm_volume = _finite_number(bgm.get("volume"))
    if (
        bgm.get("asset_id") is not None
        and (type(bgm.get("asset_id")) is not int or bgm["asset_id"] <= 0)
    ) or (
        bgm_volume is None or bgm_volume < 0 or bgm_volume > 1
        or type(bgm.get("fade_in_ms")) is not int
        or type(bgm.get("fade_out_ms")) is not int
        or bgm["fade_in_ms"] < 0 or bgm["fade_out_ms"] < 0
    ):
        raise ValueError("背景音乐配置无效")
    return {
        "subtitle": {
            "enabled": subtitle["enabled"],
            "preset": subtitle["preset"],
            "position": subtitle["position"],
        },
        "bgm": {
            "asset_id": bgm["asset_id"],
            "volume": round(bgm_volume, 4),
            "fade_in_ms": bgm["fade_in_ms"],
            "fade_out_ms": bgm["fade_out_ms"],
        },
        "sound_cues": _normalize_sound_cues(
            value.get("sound_cues"), shot_durations
        ),
        "profiles": dict(DEFAULT_ASSEMBLY_CONFIG["profiles"]),
    }


def _blocker(code, shot_id=None, line_id=None):
    item = {
        "code": code,
        "message": _BLOCKER_MESSAGES.get(
            code,
            "主音轨时间线无效，请返回配音字幕阶段检查镜头和台词时间",
        ),
    }
    if shot_id is not None:
        item["shot_id"] = shot_id
    if line_id is not None:
        item["line_id"] = line_id
    return item


def _append_blocker(items, code, shot_id=None, line_id=None):
    item = _blocker(code, shot_id, line_id)
    identity = (code, shot_id, line_id)
    if not any(
        (saved["code"], saved.get("shot_id"), saved.get("line_id")) == identity
        for saved in items
    ):
        items.append(item)


def _row_dict(row):
    return dict(row) if row is not None else None


def _collect_sources(conn, project_id):
    """Capture all immutable inputs used by the planner for a later CAS check."""
    result = []
    shots = conn.execute(
        "SELECT id,shot_key,sort_order,duration "
        "FROM short_drama_shots WHERE project_id=? ORDER BY sort_order,id",
        (project_id,),
    )
    for shot_row in shots:
        shot = _row_dict(shot_row)
        voice_slot = _row_dict(conn.execute(
            "SELECT shot_id,locked,timeline_revision FROM short_drama_voice_shots "
            "WHERE project_id=? AND shot_id=?",
            (project_id, shot["id"]),
        ).fetchone())
        video_slot = _row_dict(conn.execute(
            "SELECT id,shot_id,current_version,locked,video_revision "
            "FROM short_drama_video_shots WHERE project_id=? AND shot_id=?",
            (project_id, shot["id"]),
        ).fetchone())
        video_version = None
        if video_slot and video_slot["current_version"] is not None:
            video_version = _row_dict(conn.execute(
                "SELECT id,version,file,duration_ms,ratio,input_hash,status "
                "FROM short_drama_video_versions "
                "WHERE video_shot_id=? AND version=?",
                (video_slot["id"], video_slot["current_version"]),
            ).fetchone())
        lines = []
        for line_row in conn.execute(
            "SELECT id,sort_order,subtitle_text,subtitle_visible,current_version,"
            "start_ms,end_ms,input_hash FROM short_drama_voice_lines "
            "WHERE project_id=? AND shot_id=? ORDER BY sort_order,id",
            (project_id, shot["id"]),
        ):
            line = _row_dict(line_row)
            version = None
            if line["current_version"] is not None:
                version = _row_dict(conn.execute(
                    "SELECT id,version,audio_file,duration_ms,input_hash,status "
                    "FROM short_drama_voice_versions "
                    "WHERE voice_line_id=? AND version=?",
                    (line["id"], line["current_version"]),
                ).fetchone())
            line["version"] = version
            lines.append(line)
        shot.update({
            "voice_slot": voice_slot,
            "voice_lines": lines,
            "video_slot": video_slot,
            "video_version": video_version,
        })
        result.append(shot)
    return result


def _default_source_inspector(file_key):
    path = media_plan.resolve_controlled_file(file_key)
    return media_plan.stable_probe(path)


def _inspect_source(file_key, source_inspector):
    try:
        return source_inspector(file_key), None
    except media_plan.MediaPlanError as error:
        return None, error.code
    except (OSError, ValueError):
        return None, "media_probe_failed"


def _validate_lipsync_render_sources(sources, source_inspector):
    """Recheck immutable lipsync bytes immediately before render handoff."""
    for shot in sources:
        source = shot.get("lipsync_source")
        if not source:
            continue
        inspected, error_code = _inspect_source(
            shot["video_version"]["file"], source_inspector
        )
        if error_code:
            raise PreviewBlocked(error_code, _BLOCKER_MESSAGES[error_code])
        try:
            lipsync_assembly.validate_source_fingerprint(source, inspected)
        except lipsync_assembly.LipsyncAssemblyBlocked as error:
            raise PreviewBlocked(error.code, str(error))


def _source_identity(project, sources):
    return media_plan.canonical_hash({
        "project": {
            "id": project["id"],
            "revision": project["revision"],
            "ratio": project["ratio"],
            "target_duration": project["target_duration"],
        },
        "sources": sources,
    })


def _source_identity_from_conn(conn, project, plan=None):
    sources = _collect_sources(conn, project["id"])
    lipsync_assembly.apply_to_sources(sources, plan, project["ratio"])
    return _source_identity(project, sources)


def _version_snapshot(row):
    return {
        "id": row["id"],
        "kind": row["kind"],
        "version": row["version"],
        "job_id": row["job_id"],
        "input_hash": row["input_hash"],
        "config": _json_value(row["config_json"], {}),
        "plan_hash": row["plan_hash"] if "plan_hash" in row.keys() else "",
        "manifest_hash": (
            row["manifest_hash"] if "manifest_hash" in row.keys() else ""
        ),
        "manifest": (
            _json_value(row["manifest_json"], {})
            if "manifest_json" in row.keys() else {}
        ),
        "url": row["url"],
        "cover_url": (
            row["cover_url"] if "cover_url" in row.keys() and row["cover_url"]
            else "/api/gen/file/" + row["cover_file"]
            if row["cover_file"] else ""
        ),
        "duration_ms": row["duration_ms"],
        "width": row["width"],
        "height": row["height"],
        "fps": row["fps"],
        "video_codec": row["video_codec"],
        "audio_codec": row["audio_codec"],
        "status": row["status"],
        "global_video_asset_id": row["global_video_asset_id"],
        "object_key": row["object_key"] if "object_key" in row.keys() else "",
        "cover_key": row["cover_key"] if "cover_key" in row.keys() else "",
        "sha256": row["sha256"] if "sha256" in row.keys() else "",
        "size": row["size"] if "size" in row.keys() else None,
        "created_at": row["created_at"],
    }


def _composition_manifest(snapshot, kind):
    lipsync = snapshot.get("lipsync_assembly") or {}
    value = {
        "contract_version": lipsync_assembly.MANIFEST_CONTRACT_VERSION,
        "kind": kind,
        "project_id": snapshot["project_id"],
        "project_revision": snapshot["revision"],
        "assembly_revision": snapshot["assembly_revision"],
        "input_hash": snapshot["input_hash"],
        "d2_input_hash": snapshot["audio_subtitle"]["input_hash"],
        "plan_hash": str(lipsync.get("plan_hash") or ""),
        "selected_sources": [
            dict(item) for item in lipsync.get("selected_sources") or []
        ],
        "master_audio_hash": (
            snapshot.get("master_audio") or {}
        ).get("master_audio_hash"),
        "profile": snapshot["config"]["profiles"][kind],
    }
    value["manifest_hash"] = media_plan.canonical_hash(value)
    lipsync_assembly.validate_composition_manifest(
        value,
        expected_kind=kind,
        expected_project_id=snapshot["project_id"],
        expected_input_hash=snapshot["input_hash"],
        plan=lipsync or None,
    )
    return value


def _final_manifest_from_preview(preview_manifest, config):
    if not isinstance(preview_manifest, dict) or not preview_manifest:
        return {}
    value = {
        key: json.loads(json.dumps(item, ensure_ascii=False))
        for key, item in preview_manifest.items()
        if key != "manifest_hash"
    }
    value["kind"] = "final"
    value["profile"] = config["profiles"]["final"]
    value["manifest_hash"] = media_plan.canonical_hash(value)
    lipsync_assembly.validate_composition_manifest(
        value, expected_kind="final"
    )
    return value


def _active_job_snapshot(row):
    if row is None:
        return None
    return {
        "job_id": row["job_id"],
        "kind": row["kind"],
        "status": row["status"],
        "phase": row["phase"],
        "progress": row["progress"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "attempt_count": row["attempt_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
    }


def build_assembly_snapshot(
    conn, project, source_inspector=None, bgm_lookup=None
):
    """Build the D-1 read model without creating or mutating a workspace row."""
    conn.row_factory = sqlite3.Row
    source_inspector = source_inspector or _default_source_inspector
    composition = conn.execute(
        "SELECT * FROM short_drama_compositions WHERE project_id=?",
        (project["id"],),
    ).fetchone()
    assembly_config = _merge_config(
        composition["config_json"] if composition else None
    )
    lipsync_plan = None
    lipsync_blockers = []
    try:
        lipsync_plan = lipsync_assembly.load_plan(conn, project)
    except lipsync_assembly.LipsyncAssemblyBlocked as error:
        lipsync_blockers = error.blockers or [{
            "code": error.code, "message": str(error),
        }]
    sources = _collect_sources(conn, project["id"])
    lipsync_assembly.apply_to_sources(
        sources, lipsync_plan, project["ratio"]
    )
    source_identity = _source_identity(project, sources)
    blockers = list(lipsync_blockers)
    shots = []
    plan_inputs = []
    for shot in sources:
        shot_blockers = []
        voice_slot = shot["voice_slot"]
        voice_locked = bool(voice_slot and voice_slot["locked"])
        if not voice_locked:
            _append_blocker(
                shot_blockers, "missing_locked_voice_shot", shot["id"]
            )
        voice_lines = []
        if voice_locked:
            for line in shot["voice_lines"]:
                version = line["version"]
                if not version or version["status"] != "done":
                    _append_blocker(
                        shot_blockers, "missing_current_voice_version",
                        shot["id"], line["id"],
                    )
                    continue
                if version["input_hash"] != line["input_hash"]:
                    _append_blocker(
                        shot_blockers, "stale_voice_version",
                        shot["id"], line["id"],
                    )
                    continue
                inspected, error_code = _inspect_source(
                    version["audio_file"], source_inspector
                )
                if error_code:
                    _append_blocker(
                        shot_blockers, error_code, shot["id"], line["id"]
                    )
                    continue
                probe = inspected["probe"]
                if probe.get("audio") is None or probe.get("video") is not None:
                    _append_blocker(
                        shot_blockers, "media_probe_failed",
                        shot["id"], line["id"],
                    )
                    continue
                if (
                    type(version["duration_ms"]) is not int
                    or abs(version["duration_ms"] - probe["duration_ms"])
                    > media_plan.DURATION_TOLERANCE_MS
                ):
                    _append_blocker(
                        shot_blockers, "clip_duration_mismatch",
                        shot["id"], line["id"],
                    )
                voice_lines.append({
                    "id": line["id"],
                    "start_ms": line["start_ms"],
                    "end_ms": line["end_ms"],
                    "audio_duration_ms": probe["duration_ms"],
                    "subtitle_visible": bool(line["subtitle_visible"]),
                    "subtitle_text": line["subtitle_text"],
                    "version": version["version"],
                    "source": inspected["fingerprint"],
                    "probe": probe,
                })
            normalized_lines, timeline_blockers = media_plan.validate_timeline(
                voice_lines, int(shot["duration"]) * 1000
            )
            voice_lines = normalized_lines
            for item in timeline_blockers:
                _append_blocker(
                    shot_blockers, item["code"], shot["id"],
                    item.get("line_id"),
                )
        video_slot = shot["video_slot"]
        video_version = shot["video_version"]
        video_locked = bool(video_slot and video_slot["locked"])
        video_inspected = None
        if not video_locked:
            _append_blocker(
                shot_blockers, "missing_locked_video_shot", shot["id"]
            )
        elif not video_version:
            _append_blocker(
                shot_blockers, "missing_current_video_version", shot["id"]
            )
        elif video_version["status"] != "done":
            _append_blocker(
                shot_blockers, "stale_video_version", shot["id"]
            )
        else:
            video_inspected, error_code = _inspect_source(
                video_version["file"], source_inspector
            )
            if error_code:
                _append_blocker(shot_blockers, error_code, shot["id"])
            else:
                if shot.get("lipsync_source"):
                    try:
                        lipsync_assembly.validate_source_fingerprint(
                            shot["lipsync_source"], video_inspected
                        )
                    except lipsync_assembly.LipsyncAssemblyBlocked:
                        _append_blocker(
                            shot_blockers,
                            "lipsync_source_hash_mismatch",
                            shot["id"],
                        )
                probe = video_inspected["probe"]
                if probe.get("video") is None:
                    _append_blocker(
                        shot_blockers, "media_probe_failed", shot["id"]
                    )
                if (
                    video_version["ratio"] != project["ratio"]
                    or not media_plan.ratio_matches(probe, project["ratio"])
                ):
                    _append_blocker(
                        shot_blockers, "ratio_mismatch", shot["id"]
                    )
                if (
                    type(video_version["duration_ms"]) is not int
                    or abs(video_version["duration_ms"] - probe["duration_ms"])
                    > media_plan.DURATION_TOLERANCE_MS
                ):
                    _append_blocker(
                        shot_blockers, "clip_duration_mismatch", shot["id"]
                    )
        shot_ready = not shot_blockers
        if shot_ready:
            plan_inputs.append({
                "id": shot["id"],
                "sort_order": shot["sort_order"],
                "duration_ms": int(shot["duration"]) * 1000,
                "video_probe": video_inspected["probe"],
                "voice_lines": voice_lines,
            })
        video_blocked = any(
            item["code"] in {
                "missing_locked_video_shot",
                "missing_current_video_version",
                "stale_video_version",
                "missing_source_file",
                "source_file_untrusted",
                "ffprobe_unavailable",
                "media_probe_failed",
                "ratio_mismatch",
                "clip_duration_mismatch",
                "source_changed_during_probe",
                "lipsync_source_hash_mismatch",
            }
            and item.get("line_id") is None
            for item in shot_blockers
        )
        blockers.extend(shot_blockers)
        shots.append({
            "id": shot["id"],
            "shot_key": shot["shot_key"],
            "sort_order": shot["sort_order"],
            "duration": shot["duration"],
            "voice": {
                "locked": voice_locked,
                "status": (
                    "ready"
                    if voice_locked and not any(
                        item["code"] in {
                            "missing_current_voice_version",
                            "stale_voice_version",
                            "missing_source_file",
                            "source_file_untrusted",
                            "ffprobe_unavailable",
                            "media_probe_failed",
                            "clip_duration_mismatch",
                            "timeline_invalid",
                            "audio_overlap",
                            "subtitle_overlap",
                            "source_changed_during_probe",
                        }
                        and item.get("line_id") is not None
                        for item in shot_blockers
                    )
                    else "blocked"
                ),
                "timeline_revision": (
                    voice_slot["timeline_revision"] if voice_slot else None
                ),
                "lines": voice_lines,
            },
            "video": {
                "confirmed": video_locked,
                "status": (
                    "ready"
                    if video_locked and video_inspected and not video_blocked
                    else "blocked"
                ),
                "current_version": (
                    video_slot["current_version"] if video_slot else None
                ),
                "video_revision": (
                    video_slot["video_revision"] if video_slot else None
                ),
                "source": (
                    video_inspected["fingerprint"] if video_inspected else None
                ),
                "probe": video_inspected["probe"] if video_inspected else None,
                "source_kind": (
                    "lipsync" if shot.get("lipsync_source") else "standard"
                ),
                "lipsync": shot.get("lipsync_source"),
            },
            "ready": shot_ready,
            "blockers": shot_blockers,
        })
    expected_duration_ms = int(project["target_duration"]) * 1000
    actual_duration_ms = sum(int(item["duration"]) * 1000 for item in sources)
    if abs(actual_duration_ms - expected_duration_ms) > (
        media_plan.DURATION_TOLERANCE_MS
    ):
        blockers.append(_blocker("project_duration_mismatch"))
    current_sources = _collect_sources(conn, project["id"])
    lipsync_assembly.apply_to_sources(
        current_sources, lipsync_plan, project["ratio"]
    )
    current_project = conn.execute(
        "SELECT id,revision,ratio,target_duration FROM short_drama_projects "
        "WHERE id=?",
        (project["id"],),
    ).fetchone()
    if (
        current_project is None
        or _source_identity(current_project, current_sources) != source_identity
    ):
        blockers.append(_blocker("source_changed_during_probe"))
    planning_blockers = [
        item for item in blockers
        if item["code"] not in {"preview_missing", "final_missing"}
    ]
    normalization = None
    input_hash = None
    if not planning_blockers and len(plan_inputs) == len(sources):
        normalization = media_plan.build_normalization_plan(
            project["ratio"], project["target_duration"], plan_inputs
        )
        hash_payload = {
            "planner_version": media_plan.PLANNER_VERSION,
            "project": {
                "id": project["id"],
                "revision": project["revision"],
                "ratio": project["ratio"],
                "target_duration": project["target_duration"],
            },
            "config": _merge_config(
                composition["config_json"] if composition else None
            ),
            "sources": [{
                "id": item["id"],
                "voice": [
                    {
                        "id": line["id"],
                        "version": line["version"],
                        "source": line["source"],
                    }
                    for line in item["voice"]["lines"]
                ],
                "video": {
                    "version": item["video"]["current_version"],
                    "source": item["video"]["source"],
                    "source_kind": item["video"]["source_kind"],
                    "lipsync": item["video"]["lipsync"],
                },
            } for item in shots],
            "plan": normalization,
        }
        input_hash = media_plan.canonical_hash(hash_payload)
    d2_blockers = []
    d2_input_hash = None
    bgm_fingerprint = None
    sound_sources = []
    master_audio_contract = None
    if normalization is None or input_hash is None:
        d2_blockers.append(_blocker("missing_d1_media_plan"))
    else:
        bgm_asset_id = assembly_config["bgm"].get("asset_id")
        if bgm_asset_id is not None:
            asset = (
                bgm_lookup(project["username"], bgm_asset_id)
                if callable(bgm_lookup)
                else None
            )
            if not asset:
                d2_blockers.append(_blocker("bgm_asset_missing"))
            elif str(asset.get("username") or "") != project["username"]:
                d2_blockers.append(_blocker("bgm_asset_forbidden"))
            else:
                inspected, error_code = _inspect_source(
                    asset.get("file"), source_inspector
                )
                if error_code:
                    code = (
                        "bgm_source_untrusted"
                        if error_code == "source_file_untrusted"
                        else "bgm_asset_missing"
                        if error_code == "missing_source_file"
                        else "bgm_probe_failed"
                    )
                    d2_blockers.append(_blocker(code))
                elif (
                    inspected["probe"].get("audio") is None
                    or inspected["probe"].get("video") is not None
                ):
                    d2_blockers.append(_blocker("bgm_probe_failed"))
                else:
                    bgm_fingerprint = {
                        "id": bgm_asset_id,
                        "sha256": inspected["fingerprint"]["sha256"],
                        "size": inspected["fingerprint"]["size"],
                        "duration_ms": inspected["probe"]["duration_ms"],
                    }
        shot_windows = {
            str(item.get("id") or ""): (
                int(item.get("start_ms") or 0),
                int(item.get("duration_ms") or 0),
            )
            for item in normalization.get("shots") or []
            if isinstance(item, dict)
        }
        for cue in assembly_config.get("sound_cues") or []:
            if cue.get("enabled") is not True:
                continue
            shot_window = shot_windows.get(str(cue.get("shot_id") or ""))
            if not shot_window:
                d2_blockers.append(_blocker("sound_cue_invalid"))
                continue
            try:
                normalized_cue = _normalize_sound_cues(
                    [cue], {cue["shot_id"]: shot_window[1]}
                )[0]
            except (KeyError, ValueError):
                d2_blockers.append(_blocker("sound_cue_invalid"))
                continue
            asset = (
                bgm_lookup(project["username"], normalized_cue["asset_id"])
                if callable(bgm_lookup)
                else None
            )
            if not asset:
                d2_blockers.append(_blocker("sound_asset_missing"))
                continue
            if str(asset.get("username") or "") != project["username"]:
                d2_blockers.append(_blocker("sound_asset_forbidden"))
                continue
            inspected, error_code = _inspect_source(
                asset.get("file"), source_inspector
            )
            if error_code:
                code = (
                    "sound_source_untrusted"
                    if error_code == "source_file_untrusted"
                    else "sound_asset_missing"
                    if error_code == "missing_source_file"
                    else "sound_probe_failed"
                )
                d2_blockers.append(_blocker(code))
                continue
            if (
                inspected["probe"].get("audio") is None
                or inspected["probe"].get("video") is not None
            ):
                d2_blockers.append(_blocker("sound_probe_failed"))
                continue
            sound_sources.append({
                **normalized_cue,
                "timeline_start_ms": shot_window[0]
                + normalized_cue["start_ms"],
                "timeline_end_ms": shot_window[0]
                + normalized_cue["end_ms"],
                "sha256": inspected["fingerprint"]["sha256"],
                "size": inspected["fingerprint"]["size"],
                "source_duration_ms": inspected["probe"]["duration_ms"],
            })
        if not d2_blockers:
            voice_sources = [
                {
                    "shot_id": shot["id"],
                    "line_id": line["id"],
                    "version": line["version"],
                    "sha256": line["source"]["sha256"],
                    "size": line["source"]["size"],
                }
                for shot in shots
                for line in shot["voice"]["lines"]
            ]
            try:
                master_audio_contract = master_audio.build_contract(
                    normalization,
                    voice_sources,
                    bgm_fingerprint,
                    assembly_config["bgm"],
                    sound_sources,
                )
            except master_audio.MasterAudioContractError as error:
                d2_blockers.append(_blocker(error.code))
        if not d2_blockers:
            normalization = subtitle_alignment.apply_locked_timeline(
                conn, project["id"], normalization
            )
            hash_payload["plan"] = normalization
            input_hash = media_plan.canonical_hash(hash_payload)
        if not d2_blockers:
            d2_input_hash = assembly_artifacts.compute_input_hash(
                input_hash,
                assembly_config,
                voice_sources,
                bgm_fingerprint,
                sound_sources,
            )
    blockers.extend(
        item for item in d2_blockers
        if item["code"] != "missing_d1_media_plan"
    )
    audio_subtitle = assembly_artifacts.build_snapshot_from_conn(
        conn, project["id"], input_hash, d2_input_hash
    )
    audio_subtitle["blockers"] = d2_blockers
    audio_subtitle["bgm_source"] = bgm_fingerprint
    audio_subtitle["sound_sources"] = sound_sources
    if d2_blockers:
        audio_subtitle["status"] = "blocked"
    master_audio_snapshot = master_audio.build_snapshot(
        master_audio_contract, audio_subtitle
    )
    active_job = conn.execute(
        "SELECT * FROM short_drama_composition_jobs "
        "WHERE project_id=? AND status IN ('queued','running') "
        "ORDER BY updated_at DESC,job_id DESC LIMIT 1",
        (project["id"],),
    ).fetchone()
    latest_job = conn.execute(
        "SELECT * FROM short_drama_composition_jobs WHERE project_id=? "
        "ORDER BY updated_at DESC,job_id DESC LIMIT 1",
        (project["id"],),
    ).fetchone()
    if active_job:
        blockers.append(_blocker("active_composition_job"))
    versions = [
        _version_snapshot(row)
        for row in conn.execute(
            "SELECT * FROM short_drama_composition_versions "
            "WHERE project_id=? ORDER BY created_at DESC,kind,version DESC "
            "LIMIT 10",
            (project["id"],),
        )
    ]
    assets_by_job = {
        str(row["job_id"]): dict(row)
        for row in conn.execute(
            "SELECT * FROM short_drama_final_assets WHERE project_id=? "
            "AND archive_status='ready' AND deleted=0",
            (project["id"],),
        )
    }
    for version_item in versions:
        asset = assets_by_job.get(str(version_item["job_id"]))
        if asset:
            version_item["asset_id"] = asset["id"]
            version_item["created_by"] = asset["created_by"]
        if version_item["kind"] == "final" and version_item["object_key"]:
            # 私有桶 URL 会过期；每次读取工作区时刷新短期签名。
            try:
                from . import cos
                if cos.enabled():
                    version_item["url"] = cos.object_url(
                        version_item["object_key"], private=True
                    )
                    if version_item["cover_key"]:
                        version_item["cover_url"] = cos.object_url(
                            version_item["cover_key"], private=True
                        )
            except Exception:
                # COS 短暂不可用时保留数据库中的最近一次 URL，工作区其余
                # 数据仍可读取；导出/确认的服务端校验不依赖该展示 URL。
                pass
    preview_versions = [
        item for item in versions
        if item["kind"] == "preview" and item["status"] == "succeeded"
    ]
    final_versions = [
        item for item in versions
        if item["kind"] == "final" and item["status"] == "succeeded"
    ]
    if not preview_versions:
        blockers.append(_blocker("preview_missing"))
    if not final_versions:
        blockers.append(_blocker("final_missing"))
    current_preview = (
        composition["current_preview_version"] if composition else None
    )
    current_final = (
        composition["current_final_version"] if composition else None
    )
    preview_locked = bool(composition["preview_locked"]) if composition else False
    readiness_blockers = [
        item for item in blockers
        if item["code"] not in {"preview_missing", "final_missing"}
    ]
    return {
        "project_id": project["id"],
        "revision": project["revision"],
        "stage": project["stage"],
        "ratio": project["ratio"],
        "target_duration": project["target_duration"],
        "assembly_revision": (
            composition["assembly_revision"] if composition else 1
        ),
        "config": assembly_config,
        "current_preview_version": current_preview,
        "current_final_version": current_final,
        "preview_locked": preview_locked,
        "implementation_status": "formal_export",
        "rendering_enabled": True,
        "planner_version": media_plan.PLANNER_VERSION,
        "input_hash": input_hash,
        "media_plan": normalization,
        "audio_subtitle": audio_subtitle,
        "master_audio": master_audio_snapshot,
        "lipsync_assembly": lipsync_plan,
        "shots": shots,
        "versions": versions,
        "active_job": _active_job_snapshot(active_job),
        "latest_job": _active_job_snapshot(latest_job),
        "readiness": {
            "ready": not readiness_blockers,
            "blockers": readiness_blockers,
        },
        "actions": {
            "can_save_config": (
                project["stage"] == "assembly_review" and active_job is None
            ),
            "can_preview": (
                project["stage"] == "assembly_review"
                and not readiness_blockers
                and active_job is None
            ),
            "can_lock_preview": False,
            "can_export": (
                project["stage"] == "assembly_review"
                and bool(preview_versions)
                and not readiness_blockers
                and active_job is None
            ),
            "can_confirm": (
                project["stage"] == "assembly_review"
                and any(item.get("asset_id") for item in final_versions)
                and active_job is None
            ),
        },
        "blockers": blockers,
    }


def _reconcile_project_composition_jobs(db_factory, project_id):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        active_jobs = []
        for row in conn.execute(
            "SELECT job_id,kind FROM short_drama_composition_jobs "
            "WHERE project_id=? AND status IN ('queued','running')",
            (project_id,),
        ):
            try:
                active_jobs.append((int(row["job_id"]), row["kind"]))
            except (TypeError, ValueError):
                pass
    for job_id, kind in active_jobs:
        if kind == "preview":
            reconcile_preview_job(db_factory, job_id)
        elif kind == "final":
            reconcile_final_job(db_factory, job_id)
    reconcile_final_refunds(db_factory)


def get_assembly_workspace(
    db_factory, username, project_id, source_inspector=None, bgm_lookup=None
):
    _reconcile_project_composition_jobs(db_factory, project_id)
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        project = conn.execute(
            "SELECT * FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if project["stage"] not in ASSEMBLY_STAGES:
            raise ValueError("短剧项目尚未进入合成阶段")
        return build_assembly_snapshot(
            conn, project, source_inspector, bgm_lookup
        )


def save_assembly_config(
    db_factory, owner_username, body, audio_asset_lookup=None
):
    if not isinstance(body, dict) or set(body) != {
        "project_id", "revision", "assembly_revision", "config"
    }:
        raise ValueError("装配配置请求字段不正确")
    project_id = str(body.get("project_id") or "").strip()
    if (
        not project_id
        or type(body.get("revision")) is not int
        or type(body.get("assembly_revision")) is not int
    ):
        raise ValueError("装配配置版本无效")
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? "
            "AND deleted=0", (project_id, owner_username),
        ).fetchone()
        if not project:
            conn.rollback()
            raise LookupError("短剧项目不存在")
        if project["stage"] != "assembly_review":
            conn.rollback()
            raise PreviewBlocked("stage_not_writable", "当前阶段不可修改装配配置")
        if project["revision"] != body["revision"]:
            conn.rollback()
            raise PreviewBlocked("revision_conflict", "项目已更新，请刷新后重试")
        active = conn.execute(
            "SELECT 1 FROM short_drama_composition_jobs WHERE project_id=? "
            "AND status IN ('queued','running') LIMIT 1", (project_id,),
        ).fetchone()
        if active:
            conn.rollback()
            raise ActiveCompositionJob("项目已有合成任务处理中")
        shot_durations = {
            str(row["id"]): int(row["duration"]) * 1000
            for row in conn.execute(
                "SELECT id,duration FROM short_drama_shots WHERE project_id=?",
                (project_id,),
            )
        }
        config = _normalize_editable_config(body["config"], shot_durations)
        asset_ids = {
            cue["asset_id"] for cue in config["sound_cues"] if cue["enabled"]
        }
        if config["bgm"]["asset_id"] is not None:
            asset_ids.add(config["bgm"]["asset_id"])
        for asset_id in asset_ids:
            asset = (
                audio_asset_lookup(owner_username, asset_id)
                if callable(audio_asset_lookup) else None
            )
            if (
                not asset
                or str(asset.get("username") or "") != owner_username
                or not str(asset.get("file") or "")
            ):
                conn.rollback()
                raise PreviewBlocked(
                    "sound_asset_missing", "所选音频资产不存在或无权使用"
                )
        composition = conn.execute(
            "SELECT * FROM short_drama_compositions WHERE project_id=?",
            (project_id,),
        ).fetchone()
        current_revision = (
            int(composition["assembly_revision"]) if composition else 1
        )
        if current_revision != body["assembly_revision"]:
            conn.rollback()
            raise PreviewBlocked(
                "revision_conflict", "装配版本已更新，请刷新后重试"
            )
        current_config = _merge_config(
            composition["config_json"] if composition else None
        )
        if current_config == config:
            conn.commit()
            return {
                "project_id": project_id,
                "revision": project["revision"],
                "assembly_revision": current_revision,
                "config": config,
                "changed": False,
            }
        next_revision = current_revision + 1
        now = int(time.time())
        encoded = json.dumps(config, ensure_ascii=False, sort_keys=True)
        if composition:
            updated = conn.execute(
                "UPDATE short_drama_compositions SET assembly_revision=?,"
                "config_json=?,current_preview_version=NULL,"
                "current_final_version=NULL,preview_locked=0,updated_at=? "
                "WHERE project_id=? AND assembly_revision=?",
                (
                    next_revision, encoded, now, project_id, current_revision,
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                raise PreviewBlocked(
                    "revision_conflict", "装配版本已更新，请刷新后重试"
                )
        else:
            conn.execute(
                "INSERT INTO short_drama_compositions "
                "(project_id,assembly_revision,config_json,created_at,updated_at)"
                " VALUES (?,?,?,?,?)",
                (project_id, next_revision, encoded, now, now),
            )
        conn.commit()
        return {
            "project_id": project_id,
            "revision": project["revision"],
            "assembly_revision": next_revision,
            "config": config,
            "changed": True,
        }


def _preview_request(body):
    if not isinstance(body, dict) or set(body) != {
        "project_id", "revision", "assembly_revision"
    }:
        raise ValueError("预览请求字段不正确")
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("短剧项目 ID 无效")
    if type(body.get("revision")) is not int:
        raise ValueError("项目版本无效")
    if type(body.get("assembly_revision")) is not int:
        raise ValueError("装配版本无效")
    return {
        "project_id": project_id,
        "revision": body["revision"],
        "assembly_revision": body["assembly_revision"],
    }


def _replay_preview_job(conn, actor_username, key, request_hash, project_id):
    existing = conn.execute(
        "SELECT * FROM short_drama_composition_jobs "
        "WHERE username=? AND kind='preview' AND idempotency_key=?",
        (actor_username, key),
    ).fetchone()
    if not existing:
        return None
    if existing["request_hash"] != request_hash:
        raise PreviewIdempotencyConflict(
            "同一 Idempotency-Key 不能用于不同预览请求"
        )
    return {
        "project_id": project_id,
        "job_id": int(existing["job_id"]),
        "status": existing["status"],
        "replayed": True,
    }


def create_preview_job(
    db_factory, actor_username, owner_username, body, idempotency_key,
    enqueue=None, bgm_lookup=None,
):
    """Atomically persist the free generic job, composition job and version."""
    request = _preview_request(body)
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 200:
        raise ValueError("预览生成必须提供有效的 Idempotency-Key")
    project_id = request["project_id"]
    _reconcile_project_composition_jobs(db_factory, project_id)
    # Probe outside the write transaction; revision/source identity is checked
    # again under BEGIN IMMEDIATE before the durable job is created.
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? "
            "AND deleted=0", (project_id, owner_username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if project["stage"] != "assembly_review":
            raise PreviewBlocked("stage_not_writable", "当前阶段不可生成预览")
        if project["revision"] != request["revision"]:
            raise PreviewBlocked("revision_conflict", "项目已更新，请刷新后重试")
        snapshot = build_assembly_snapshot(
            conn, project, bgm_lookup=bgm_lookup
        )
        blockers = [
            item for item in snapshot["readiness"]["blockers"]
            if item["code"] != "active_composition_job"
        ]
        if blockers:
            first = blockers[0]
            raise PreviewBlocked(first["code"], first["message"])
        source_identity = _source_identity_from_conn(
            conn, project, snapshot.get("lipsync_assembly")
        )
    manifest = _composition_manifest(snapshot, "preview")
    request_hash = media_plan.canonical_hash({
        "project_id": project_id,
        "revision": request["revision"],
        "assembly_revision": request["assembly_revision"],
        "input_hash": snapshot["input_hash"],
        "d2_input_hash": snapshot["audio_subtitle"]["input_hash"],
        "manifest_hash": manifest["manifest_hash"],
    })
    # Replaying an already-persisted request must not depend on the current
    # render environment. The transactional lookup below remains the race
    # check for requests that overlap this probe.
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        replayed = _replay_preview_job(
            conn, actor_username, key, request_hash, project_id
        )
        if replayed:
            return replayed
    # Keep the potentially slow fontconfig probes outside BEGIN IMMEDIATE.
    _require_render_environment()
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        replayed = _replay_preview_job(
            conn, actor_username, key, request_hash, project_id
        )
        if replayed:
            conn.commit()
            return replayed
        locked_project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? "
            "AND deleted=0", (project_id, owner_username),
        ).fetchone()
        if (
            not locked_project
            or locked_project["revision"] != request["revision"]
            or _source_identity_from_conn(
                conn, locked_project, snapshot.get("lipsync_assembly")
            ) != source_identity
        ):
            conn.rollback()
            raise PreviewBlocked("revision_conflict", "项目已更新，请刷新后重试")
        active = conn.execute(
            "SELECT job_id FROM short_drama_composition_jobs "
            "WHERE project_id=? AND status IN ('queued','running') LIMIT 1",
            (project_id,),
        ).fetchone()
        if active:
            conn.rollback()
            raise ActiveCompositionJob("项目已有预览任务处理中")
        composition = conn.execute(
            "SELECT * FROM short_drama_compositions WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if composition is None:
            if request["assembly_revision"] != 1:
                conn.rollback()
                raise PreviewBlocked(
                    "revision_conflict", "装配版本已更新，请刷新后重试"
                )
            config = snapshot["config"]
            conn.execute(
                "INSERT INTO short_drama_compositions "
                "(project_id,assembly_revision,config_json,created_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (project_id, 1, json.dumps(config, ensure_ascii=False), now, now),
            )
        elif composition["assembly_revision"] != request["assembly_revision"]:
            conn.rollback()
            raise PreviewBlocked(
                "revision_conflict", "装配版本已更新，请刷新后重试"
            )
        else:
            config = _merge_config(composition["config_json"])
        payload = {
            "mode": "short_drama_preview",
            "project_id": project_id,
            "owner_username": owner_username,
            "actor_username": actor_username,
            "revision": request["revision"],
            "assembly_revision": request["assembly_revision"],
            "input_hash": snapshot["input_hash"],
            "d2_input_hash": snapshot["audio_subtitle"]["input_hash"],
            "manifest": manifest,
        }
        bgm_asset_id = snapshot["config"]["bgm"].get("asset_id")
        if bgm_asset_id is not None and callable(bgm_lookup):
            bgm_asset = bgm_lookup(owner_username, bgm_asset_id)
            if bgm_asset:
                payload["bgm_file"] = str(bgm_asset.get("file") or "")
        payload["sound_cues"] = []
        for cue in snapshot["audio_subtitle"].get("sound_sources") or []:
            asset = (
                bgm_lookup(owner_username, cue["asset_id"])
                if callable(bgm_lookup) else None
            )
            if asset:
                payload["sound_cues"].append({
                    key: cue[key]
                    for key in (
                        "id", "shot_id", "kind", "asset_id",
                        "timeline_start_ms", "timeline_end_ms", "loop",
                        "volume", "fade_in_ms", "fade_out_ms",
                    )
                } | {"file": str(asset.get("file") or "")})
        cursor = conn.execute(
            "INSERT INTO jobs(kind,username,cost,status,payload,created_at,"
            "updated_at,owner) VALUES ('short_drama_preview',?,0,'pending',?,?,?,"
            "'content')",
            (actor_username, json.dumps(payload, ensure_ascii=False), now, now),
        )
        job_id = int(cursor.lastrowid)
        version = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS n "
            "FROM short_drama_composition_versions "
            "WHERE project_id=? AND kind='preview'", (project_id,),
        ).fetchone()["n"])
        version_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO short_drama_composition_versions "
            "(id,project_id,kind,version,job_id,input_hash,config_json,"
            "plan_hash,manifest_hash,manifest_json,status,created_at) "
            "VALUES (?,?,'preview',?,?,?,?,?,?,?,'rendering',?)",
            (
                version_id, project_id, version, str(job_id),
                snapshot["input_hash"], json.dumps(config, ensure_ascii=False),
                manifest["plan_hash"], manifest["manifest_hash"],
                json.dumps(manifest, ensure_ascii=False),
                now,
            ),
        )
        conn.execute(
            "INSERT INTO short_drama_composition_jobs "
            "(id,username,project_id,job_id,kind,idempotency_key,request_hash,"
            "status,phase,progress,created_at,updated_at) "
            "VALUES (?,?,?,?, 'preview',?,?,'queued','queued',0,?,?)",
            (
                uuid.uuid4().hex, actor_username, project_id, str(job_id),
                key, request_hash, now, now,
            ),
        )
        conn.commit()
    if callable(enqueue):
        enqueue(job_id, "short_drama_preview")
    return {
        "project_id": project_id, "job_id": job_id,
        "status": "queued", "replayed": False,
    }


def set_preview_progress(db_factory, job_id, phase, progress):
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.execute(
            "UPDATE short_drama_composition_jobs SET status='running',"
            "phase=?,progress=?,attempt_count=attempt_count+"
            "CASE WHEN ?='preparing' THEN 1 ELSE 0 END,updated_at=? "
            "WHERE job_id=? AND status IN ('queued','running')",
            (
                str(phase), max(0, min(99, int(progress))), str(phase),
                now, str(job_id),
            ),
        )
        conn.commit()


def reconcile_preview_job(db_factory, job_id):
    """Publish generic-job terminal state into the D-3 read model idempotently."""
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT status,result,error FROM jobs WHERE id=? "
            "AND kind='short_drama_preview'", (job_id,),
        ).fetchone()
        linked = conn.execute(
            "SELECT * FROM short_drama_composition_jobs WHERE job_id=?",
            (str(job_id),),
        ).fetchone()
        if not job or not linked or linked["status"] in {"succeeded", "failed"}:
            conn.commit()
            return False
        invalid_result_error = None
        if job["status"] == "done":
            try:
                result = json.loads(job["result"] or "{}")
            except (TypeError, ValueError):
                result = {}
            required = {"file", "url", "cover_file", "duration_ms", "width",
                        "height", "fps", "video_codec", "audio_codec"}
            if not required.issubset(result):
                invalid_result_error = "预览任务结果不完整"
            else:
                conn.execute(
                    "UPDATE short_drama_composition_versions SET file=?,url=?,"
                    "cover_file=?,duration_ms=?,width=?,height=?,fps=?,"
                    "video_codec=?,audio_codec=?,status='succeeded' "
                    "WHERE job_id=? AND status='rendering'",
                    tuple(result[name] for name in (
                        "file", "url", "cover_file", "duration_ms", "width",
                        "height", "fps", "video_codec", "audio_codec"
                    )) + (str(job_id),),
                )
                row = conn.execute(
                    "SELECT project_id,version FROM "
                    "short_drama_composition_versions WHERE job_id=?",
                    (str(job_id),),
                ).fetchone()
                conn.execute(
                    "UPDATE short_drama_compositions SET "
                    "current_preview_version=?,preview_locked=0,updated_at=? "
                    "WHERE project_id=?", (row["version"], now, row["project_id"]),
                )
                conn.execute(
                    "UPDATE short_drama_composition_jobs SET status='succeeded',"
                    "phase='completed',progress=100,error_code='',error_message='',"
                    "updated_at=?,finished_at=? WHERE job_id=?",
                    (now, now, str(job_id)),
                )
                conn.commit()
                return True
        if job["status"] == "error" or invalid_result_error:
            error = str(
                invalid_result_error or job["error"] or "预览生成失败"
            )[:300]
            conn.execute(
                "UPDATE short_drama_composition_versions SET status='failed' "
                "WHERE job_id=? AND status='rendering'", (str(job_id),),
            )
            conn.execute(
                "UPDATE short_drama_composition_jobs SET status='failed',"
                "phase='failed',error_code='preview_render_failed',"
                "error_message=?,updated_at=?,finished_at=? WHERE job_id=?",
                (error, now, now, str(job_id)),
            )
            conn.commit()
            return True
        conn.commit()
        return False


def preview_render_context(db_factory, job_id):
    """Rebuild trusted local file inputs and reject stale jobs before rendering."""
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        linked = conn.execute(
            "SELECT * FROM short_drama_composition_jobs WHERE job_id=?",
            (str(job_id),),
        ).fetchone()
        generic = conn.execute(
            "SELECT payload FROM jobs WHERE id=?", (job_id,),
        ).fetchone()
        if not linked or not generic:
            raise PreviewBlocked("preview_render_failed", "预览任务不存在")
        payload = json.loads(generic["payload"] or "{}")
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? "
            "AND deleted=0", (linked["project_id"], payload["owner_username"]),
        ).fetchone()
        bgm_file = str(payload.get("bgm_file") or "")
        payload_sound_cues = [
            dict(item) for item in payload.get("sound_cues") or []
            if isinstance(item, dict)
        ]
        asset_files = {
            int(item["asset_id"]): str(item.get("file") or "")
            for item in payload_sound_cues
            if type(item.get("asset_id")) is int and item.get("file")
        }
        bgm_asset_id = (
            _merge_config(
                conn.execute(
                    "SELECT config_json FROM short_drama_compositions "
                    "WHERE project_id=?", (project["id"],),
                ).fetchone()["config_json"]
            )["bgm"].get("asset_id")
        )
        if bgm_file and bgm_asset_id is not None:
            asset_files[int(bgm_asset_id)] = bgm_file
        snapshot = build_assembly_snapshot(
            conn, project,
            bgm_lookup=(
                lambda username, asset_id: {
                    "username": username, "id": asset_id,
                    "file": asset_files.get(int(asset_id), "")
                }
                if int(asset_id) in asset_files else None
            ),
        )
        if snapshot["input_hash"] != payload.get("input_hash"):
            raise PreviewBlocked("revision_conflict", "预览输入已更新")
        if (
            not payload.get("d2_input_hash")
            or snapshot["audio_subtitle"]["input_hash"]
            != payload.get("d2_input_hash")
        ):
            raise PreviewBlocked(
                "audio_input_changed",
                "预览音频输入已更新，请重新生成预览",
            )
        _require_preview_manifest(
            snapshot,
            payload.get("manifest") or {},
            expected_kind=(
                "final"
                if payload.get("mode") == "short_drama_final"
                else "preview"
            ),
        )
        raw = _collect_sources(conn, project["id"])
        lipsync_assembly.apply_to_sources(
            raw, snapshot.get("lipsync_assembly"), project["ratio"]
        )
        _validate_lipsync_render_sources(raw, _default_source_inspector)
        videos = []
        shot_inputs = {}
        for shot in raw:
            videos.append({
                "id": shot["id"],
                "duration_ms": int(shot["duration"]) * 1000,
                "file": str(media_plan.resolve_controlled_file(
                    shot["video_version"]["file"]
                )),
            })
            shot_inputs[shot["id"]] = [{
                "id": line["id"],
                "start_ms": line["start_ms"],
                "file": str(media_plan.resolve_controlled_file(
                    line["version"]["audio_file"]
                )),
            } for line in shot["voice_lines"]]
        return {
            "project": dict(project), "payload": payload, "snapshot": snapshot,
            "videos": videos, "shot_inputs": shot_inputs,
            "bgm_source": (
                str(media_plan.resolve_controlled_file(bgm_file))
                if bgm_file else None
            ),
            "sound_cues": [
                {
                    **item,
                    "file": str(media_plan.resolve_controlled_file(item["file"])),
                }
                for item in payload_sound_cues
            ],
        }


FINAL_QUOTE_TTL_SECONDS = 300


def _final_preview(conn, project_id, preview_version):
    return conn.execute(
        "SELECT * FROM short_drama_composition_versions "
        "WHERE project_id=? AND kind='preview' AND version=? "
        "AND status='succeeded'",
        (project_id, preview_version),
    ).fetchone()


def _preview_audio_payload(conn, preview):
    """Load the immutable audio identity captured by a succeeded preview."""
    generic = conn.execute(
        "SELECT payload FROM jobs WHERE id=?", (preview["job_id"],),
    ).fetchone()
    try:
        payload = json.loads(generic["payload"] or "{}") if generic else {}
    except (TypeError, ValueError):
        payload = {}
    d2_input_hash = payload.get("d2_input_hash")
    if (
        payload.get("mode") != "short_drama_preview"
        or not isinstance(d2_input_hash, str)
        or len(d2_input_hash) != 64
    ):
        raise PreviewBlocked(
            "preview_stale",
            "预览缺少可信音频身份，请重新生成预览",
        )
    return payload


def _require_preview_audio_identity(snapshot, preview_payload):
    if (
        snapshot["audio_subtitle"]["input_hash"]
        != preview_payload["d2_input_hash"]
    ):
        raise PreviewBlocked(
            "preview_stale",
            "预览音频输入已变化，请重新生成预览",
        )


def _require_preview_manifest(snapshot, manifest, expected_kind):
    current = snapshot.get("lipsync_assembly") or {}
    try:
        lipsync_assembly.validate_composition_manifest(
            manifest,
            expected_kind=expected_kind,
            expected_project_id=snapshot["project_id"],
            expected_input_hash=snapshot["input_hash"],
            plan=current or None,
        )
    except lipsync_assembly.LipsyncAssemblyBlocked as error:
        raise PreviewBlocked(
            error.code,
            "预览使用的口型素材清单已变化，请重新生成预览",
        )


def _final_cover_time(duration_ms, requested):
    if requested is None:
        return min(max(1000, int(duration_ms * 0.1)), max(0, duration_ms - 500))
    if type(requested) is not int or requested < 0 or requested > duration_ms - 200:
        raise PreviewBlocked("cover_time_invalid", "封面时间超出成片范围")
    return requested


def create_final_quote(
    db_factory, actor_username, owner_username, body, cost=0,
    storage_available=True, bgm_lookup=None,
):
    if not isinstance(body, dict) or set(body) - {
        "project_id", "revision", "assembly_revision", "preview_version",
        "cover_time_ms",
    }:
        raise ValueError("正式导出询价字段不正确")
    if storage_available is False:
        raise PreviewBlocked("export_unavailable", "正式导出对象存储暂不可用")
    project_id = str(body.get("project_id") or "").strip()
    if not project_id or any(
        type(body.get(name)) is not int
        for name in ("revision", "assembly_revision", "preview_version")
    ):
        raise ValueError("正式导出询价参数无效")
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? "
            "AND deleted=0", (project_id, owner_username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if project["stage"] != "assembly_review":
            raise PreviewBlocked("export_stage_invalid", "当前阶段不可正式导出")
        composition = conn.execute(
            "SELECT * FROM short_drama_compositions WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if (
            project["revision"] != body["revision"]
            or not composition
            or composition["assembly_revision"] != body["assembly_revision"]
        ):
            raise PreviewBlocked("revision_conflict", "项目或装配版本已更新")
        preview = _final_preview(conn, project_id, body["preview_version"])
        if not preview:
            raise PreviewBlocked("preview_invalid", "预览版本不存在或未成功")
        preview_payload = _preview_audio_payload(conn, preview)
        snapshot = build_assembly_snapshot(
            conn, project, bgm_lookup=bgm_lookup
        )
        if snapshot["input_hash"] != preview["input_hash"]:
            raise PreviewBlocked("preview_stale", "预览输入已变化，请重新生成预览")
        _require_preview_audio_identity(snapshot, preview_payload)
        duration_ms = int(preview["duration_ms"] or project["target_duration"] * 1000)
        cover_time_ms = _final_cover_time(
            duration_ms, body.get("cover_time_ms")
        )
        _require_render_environment()
        total_cost = max(0, int(cost or 0))
        token = uuid.uuid4().hex
        now = int(time.time())
        conn.execute(
            "INSERT INTO short_drama_final_quotes "
            "(token,actor_username,owner_username,project_id,revision,"
            "assembly_revision,preview_version,input_hash,cover_time_ms,cost,"
            "expires_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                token, actor_username, owner_username, project_id,
                body["revision"], body["assembly_revision"],
                body["preview_version"], preview["input_hash"], cover_time_ms,
                total_cost, now + FINAL_QUOTE_TTL_SECONDS, now,
            ),
        )
        conn.commit()
    return {
        "quote_token": token, "total_cost": total_cost,
        "expires_at": now + FINAL_QUOTE_TTL_SECONDS,
        "project_id": project_id, "preview_version": body["preview_version"],
        "cover_time_ms": cover_time_ms,
        "profile": "short_drama_final_v1",
        "resolution": "1080x1920" if project["ratio"] == "9:16" else "1920x1080",
    }


def _enforce_final_budget(
    conn, project_id, cost, include_cost=True, point_usage=None
):
    project = conn.execute(
        "SELECT point_budget FROM short_drama_projects WHERE id=?",
        (project_id,),
    ).fetchone()
    budget = int(project[0] or 0) if project else 0
    if budget <= 0:
        return
    if not callable(point_usage):
        from . import short_drama as short_drama_domain
        point_usage = short_drama_domain._project_point_usage
    usage = point_usage(conn, project_id)
    spent = max(0, int(usage.get("spent_points") or 0))
    reserved = max(0, int(usage.get("reserved_points") or 0))
    requested = max(0, int(cost or 0)) if include_cost else 0
    if spent + reserved + requested > budget:
        raise PreviewBlocked(
            "point_budget_exceeded",
            "短剧点数预算不足：已用 %d 点、已预留 %d 点、本次 %d 点、预算 %d 点"
            % (spent, reserved, requested, budget),
        )


def _final_charge_ledger_matches(attempt, ledger):
    if not isinstance(ledger, dict):
        return False
    try:
        return (
            str(ledger.get("username") or "")
            == str(attempt["actor_username"])
            and int(ledger.get("delta") or 0) == -int(attempt["cost"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def _claim_final_attempt_charge(db_factory, attempt_id):
    """Lease an accepted/charged attempt before touching the points ledger."""
    token = "submission:" + str(uuid.uuid4())
    now = int(time.time())
    cutoff = now - FINAL_CHARGE_RECOVERY_SECONDS
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE short_drama_final_attempts "
            "SET recovery_token=?,recovery_started_at=?,updated_at=? "
            "WHERE id=? AND state IN ('accepted','charged') AND job_id IS NULL "
            "AND (recovery_token IS NULL OR recovery_started_at<=?)",
            (token, now, now, attempt_id, cutoff),
        )
        attempt = conn.execute(
            "SELECT * FROM short_drama_final_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        conn.commit()
    if not attempt or attempt["recovery_token"] != token:
        raise PreviewBlocked(
            "charge_in_progress",
            "正式导出扣点或资金恢复仍在处理中，请稍后重试",
        )
    return dict(attempt), token


def _release_final_charge_lease(db_factory, attempt_id, token):
    with closing(db_factory()) as conn:
        conn.execute(
            "UPDATE short_drama_final_attempts SET recovery_token=NULL,"
            "recovery_started_at=NULL,updated_at=? "
            "WHERE id=? AND job_id IS NULL AND recovery_token=?",
            (int(time.time()), attempt_id, token),
        )
        conn.commit()


def _mark_final_charge_rejected(db_factory, attempt_id, token, error):
    with closing(db_factory()) as conn:
        conn.execute(
            "UPDATE short_drama_final_attempts SET state='failed',error=?,"
            "recovery_token=NULL,recovery_started_at=NULL,updated_at=? "
            "WHERE id=? AND state='accepted' AND job_id IS NULL "
            "AND recovery_token=?",
            (str(error or "正式导出扣点被拒绝")[:300],
             int(time.time()), attempt_id, token),
        )
        conn.commit()


def _mark_final_charge_inconsistent(db_factory, attempt_id, token):
    with closing(db_factory()) as conn:
        conn.execute(
            "UPDATE short_drama_final_attempts SET error=?,"
            "recovery_token=NULL,recovery_started_at=NULL,updated_at=? "
            "WHERE id=? AND state='accepted' AND job_id IS NULL "
            "AND recovery_token=?",
            (
                "final_charge_ledger_inconsistent",
                int(time.time()), attempt_id, token,
            ),
        )
        conn.commit()


def _mark_final_attempt_charged(db_factory, attempt_id, token):
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        updated = conn.execute(
            "UPDATE short_drama_final_attempts SET state='charged',"
            "updated_at=? WHERE id=? AND state='accepted' AND job_id IS NULL "
            "AND recovery_token=?",
            (int(time.time()), attempt_id, token),
        )
        attempt = conn.execute(
            "SELECT * FROM short_drama_final_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        conn.commit()
    if updated.rowcount != 1:
        raise PreviewBlocked(
            "charge_recovery_owned",
            "正式导出扣点状态已由恢复流程接管，请稍后重试",
        )
    return dict(attempt)


def create_final_job(
    db_factory, actor_username, owner_username, body, idempotency_key,
    deduct_points=None, refund_points=None, enqueue=None, bgm_lookup=None,
    point_usage=None, charge_lookup=None,
):
    expected = {
        "project_id", "revision", "assembly_revision", "preview_version",
        "cover_time_ms", "quote_token",
    }
    if not isinstance(body, dict) or set(body) != expected:
        raise ValueError("正式导出请求字段不正确")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 200:
        raise ValueError("正式导出必须提供有效的 Idempotency-Key")
    project_id = str(body.get("project_id") or "").strip()
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM short_drama_final_attempts "
            "WHERE actor_username=? AND idempotency_key=?",
            (actor_username, key),
        ).fetchone()
        quote = conn.execute(
            "SELECT * FROM short_drama_final_quotes WHERE token=?",
            (str(body.get("quote_token") or ""),),
        ).fetchone()
        if not quote or (quote["expires_at"] < now and not existing):
            conn.rollback()
            raise PreviewBlocked("quote_invalid", "正式导出报价无效或已过期")
        bound = (
            quote["actor_username"], quote["owner_username"],
            quote["project_id"], quote["revision"],
            quote["assembly_revision"], quote["preview_version"],
            quote["cover_time_ms"],
        )
        supplied = (
            actor_username, owner_username, project_id, body.get("revision"),
            body.get("assembly_revision"), body.get("preview_version"),
            body.get("cover_time_ms"),
        )
        if bound != supplied:
            conn.rollback()
            raise PreviewBlocked("quote_invalid", "报价与当前导出请求不匹配")
        preview = _final_preview(conn, project_id, body["preview_version"])
        if not preview or preview["input_hash"] != quote["input_hash"]:
            conn.rollback()
            raise PreviewBlocked("preview_stale", "导出基线已变化")
        preview_payload = _preview_audio_payload(conn, preview)
        preview_manifest = (
            preview_payload.get("manifest")
            or _json_value(preview["manifest_json"], {})
        )
        final_manifest = _final_manifest_from_preview(
            preview_manifest, _merge_config(preview["config_json"])
        )
        request_hash = media_plan.canonical_hash({
            "actor": actor_username, "owner": owner_username,
            "project_id": project_id, "revision": body["revision"],
            "assembly_revision": body["assembly_revision"],
            "preview_version": body["preview_version"],
            "input_hash": quote["input_hash"],
            "d2_input_hash": preview_payload["d2_input_hash"],
            "manifest_hash": final_manifest.get("manifest_hash", ""),
            "cover_time_ms": body["cover_time_ms"], "cost": quote["cost"],
        })
        if existing:
            if existing["request_hash"] != request_hash:
                conn.rollback()
                raise PreviewIdempotencyConflict(
                    "同一 Idempotency-Key 不能用于不同正式导出请求"
                )
            consumed = str(quote["consumed_job_id"] or "")
            allowed_consumers = {"attempt:" + existing["id"]}
            if existing["job_id"]:
                allowed_consumers.add(str(existing["job_id"]))
            if consumed and consumed not in allowed_consumers:
                conn.rollback()
                raise PreviewBlocked(
                    "quote_consumed", "正式导出报价已被其他请求消费"
                )
            if not consumed:
                recovered_consumer = (
                    str(existing["job_id"]) if existing["job_id"]
                    else "attempt:" + existing["id"]
                )
                claimed = conn.execute(
                    "UPDATE short_drama_final_quotes SET consumed_job_id=? "
                    "WHERE token=? AND consumed_job_id IS NULL",
                    (recovered_consumer, quote["token"]),
                )
                if claimed.rowcount != 1:
                    conn.rollback()
                    raise PreviewBlocked(
                        "quote_consumed",
                        "正式导出报价已被其他请求消费",
                    )
            if existing["job_id"]:
                conn.commit()
                return {
                    "project_id": project_id, "job_id": int(existing["job_id"]),
                    "status": "queued", "cost": existing["cost"],
                    "replayed": True,
                }
            if existing["state"] in {
                "failed", "refund_pending", "refunded"
            }:
                conn.rollback()
                raise PreviewBlocked(
                    "quote_consumed", "原正式导出请求已终止，请重新询价"
                )
        else:
            if quote["consumed_job_id"]:
                conn.rollback()
                raise PreviewBlocked(
                    "quote_consumed", "正式导出报价已被消费，请重新询价"
                )
            project = conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=? AND username=? "
                "AND deleted=0", (project_id, owner_username),
            ).fetchone()
            composition = conn.execute(
                "SELECT * FROM short_drama_compositions WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if (
                not project or project["stage"] != "assembly_review"
                or project["revision"] != body["revision"]
                or not composition
                or composition["assembly_revision"] != body["assembly_revision"]
            ):
                conn.rollback()
                raise PreviewBlocked("preview_stale", "导出基线已变化")
            active = conn.execute(
                "SELECT 1 FROM short_drama_composition_jobs "
                "WHERE project_id=? AND status IN ('queued','running')",
                (project_id,),
            ).fetchone()
            if active:
                conn.rollback()
                raise ActiveCompositionJob("项目已有合成任务处理中")
            snapshot = build_assembly_snapshot(
                conn, project, bgm_lookup=bgm_lookup
            )
            if snapshot["input_hash"] != preview["input_hash"]:
                conn.rollback()
                raise PreviewBlocked("preview_stale", "导出基线已变化")
            _require_preview_audio_identity(snapshot, preview_payload)
            _require_preview_manifest(
                snapshot, final_manifest, expected_kind="final"
            )
            _enforce_final_budget(
                conn, project_id, quote["cost"], include_cost=True,
                point_usage=point_usage,
            )
            attempt_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO short_drama_final_attempts "
                "(id,actor_username,owner_username,project_id,idempotency_key,"
                "request_hash,quote_token,cost,charge_key,refund_key,state,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id, actor_username, owner_username, project_id, key,
                    request_hash, quote["token"], quote["cost"],
                    "short-drama-final-charge:" + attempt_id,
                    "short-drama-final-refund:" + attempt_id,
                    "accepted", now, now,
                ),
            )
            claimed = conn.execute(
                "UPDATE short_drama_final_quotes SET consumed_job_id=? "
                "WHERE token=? AND consumed_job_id IS NULL",
                ("attempt:" + attempt_id, quote["token"]),
            )
            if claimed.rowcount != 1:
                conn.rollback()
                raise PreviewBlocked(
                    "quote_consumed", "正式导出报价已被消费，请重新询价"
                )
            conn.commit()
            existing = conn.execute(
                "SELECT * FROM short_drama_final_attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()
        if conn.in_transaction:
            conn.commit()
    attempt, charge_claim = _claim_final_attempt_charge(
        db_factory, existing["id"]
    )
    if (
        attempt["state"] == "accepted"
        and attempt["cost"]
        and callable(deduct_points)
    ):
        try:
            deduct_points(
                actor_username, attempt["cost"], "短剧 1080p 正式导出",
                attempt["charge_key"],
            )
        except Exception as error:
            ledger = None
            ledger_checked = False
            if callable(charge_lookup):
                try:
                    ledger = charge_lookup(attempt["charge_key"])
                    ledger_checked = True
                except Exception:
                    pass
            if _final_charge_ledger_matches(attempt, ledger):
                pass
            else:
                status = int(getattr(error, "status", 0) or 0)
                if status in {400, 402, 403, 409}:
                    _mark_final_charge_rejected(
                        db_factory, attempt["id"], charge_claim, str(error)
                    )
                elif ledger_checked and ledger is not None:
                    _mark_final_charge_inconsistent(
                        db_factory, attempt["id"], charge_claim
                    )
                else:
                    _release_final_charge_lease(
                        db_factory, attempt["id"], charge_claim
                    )
                raise
    if attempt["state"] == "accepted":
        attempt = _mark_final_attempt_charged(
            db_factory, attempt["id"], charge_claim
        )
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        attempt = conn.execute(
            "SELECT * FROM short_drama_final_attempts WHERE id=?",
            (existing["id"],),
        ).fetchone()
        if attempt["recovery_token"] != charge_claim:
            conn.rollback()
            raise PreviewBlocked(
                "charge_recovery_owned",
                "正式导出扣点状态已由恢复流程接管，请稍后重试",
            )
        if attempt["state"] in {
            "failed", "refund_pending", "refunded"
        }:
            conn.rollback()
            raise PreviewBlocked(
                "quote_consumed", "原正式导出请求已终止，请重新询价"
            )
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? "
            "AND deleted=0", (project_id, owner_username),
        ).fetchone()
        composition = conn.execute(
            "SELECT * FROM short_drama_compositions WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if (
            not project or project["stage"] != "assembly_review"
            or project["revision"] != body["revision"]
            or not composition
            or composition["assembly_revision"] != body["assembly_revision"]
        ):
            conn.rollback()
            raise PreviewBlocked("preview_stale", "导出基线已变化")
        snapshot = build_assembly_snapshot(
            conn, project, bgm_lookup=bgm_lookup
        )
        if snapshot["input_hash"] != preview["input_hash"]:
            conn.rollback()
            raise PreviewBlocked("preview_stale", "导出基线已变化")
        _require_preview_audio_identity(snapshot, preview_payload)
        _require_preview_manifest(
            snapshot, final_manifest, expected_kind="final"
        )
        active = conn.execute(
            "SELECT 1 FROM short_drama_composition_jobs WHERE project_id=? "
            "AND status IN ('queued','running')", (project_id,),
        ).fetchone()
        if active:
            conn.rollback()
            raise ActiveCompositionJob("项目已有合成任务处理中")
        try:
            _enforce_final_budget(
                conn, project_id, attempt["cost"], include_cost=False,
                point_usage=point_usage,
            )
        except PreviewBlocked:
            conn.rollback()
            raise
        payload = {
            "mode": "short_drama_final", "project_id": project_id,
            "owner_username": owner_username, "actor_username": actor_username,
            "revision": body["revision"],
            "assembly_revision": body["assembly_revision"],
            "preview_version": body["preview_version"],
            "input_hash": preview["input_hash"],
            "d2_input_hash": preview_payload["d2_input_hash"],
            "cover_time_ms": body["cover_time_ms"],
            "attempt_id": attempt["id"],
            "bgm_file": str(preview_payload.get("bgm_file") or ""),
            "sound_cues": [
                dict(item) for item in preview_payload.get("sound_cues") or []
                if isinstance(item, dict)
            ],
            "manifest": final_manifest,
        }
        cursor = conn.execute(
            "INSERT INTO jobs(kind,username,cost,status,payload,created_at,"
            "updated_at,owner) VALUES ('short_drama_final',?,?,'pending',?,?,?,"
            "'content')",
            (
                actor_username, attempt["cost"],
                json.dumps(payload, ensure_ascii=False), now, now,
            ),
        )
        job_id = int(cursor.lastrowid)
        version = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM "
            "short_drama_composition_versions WHERE project_id=? AND kind='final'",
            (project_id,),
        ).fetchone()[0])
        version_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO short_drama_composition_versions "
            "(id,project_id,kind,version,job_id,input_hash,config_json,"
            "plan_hash,manifest_hash,manifest_json,status,created_at) "
            "VALUES (?,?,'final',?,?,?,?,?,?,?,'rendering',?)",
            (
                version_id, project_id, version, str(job_id),
                preview["input_hash"], preview["config_json"],
                final_manifest.get("plan_hash", ""),
                final_manifest.get("manifest_hash", ""),
                json.dumps(final_manifest, ensure_ascii=False), now,
            ),
        )
        conn.execute(
            "INSERT INTO short_drama_composition_jobs "
            "(id,username,project_id,job_id,kind,idempotency_key,request_hash,"
            "status,phase,progress,created_at,updated_at) "
            "VALUES (?,?,?,?, 'final',?,?,'queued','queued',0,?,?)",
            (
                uuid.uuid4().hex, actor_username, project_id, str(job_id),
                key, attempt["request_hash"], now, now,
            ),
        )
        linked_attempt = conn.execute(
            "UPDATE short_drama_final_attempts SET state='charged',job_id=?,"
            "recovery_token=NULL,recovery_started_at=NULL,updated_at=? "
            "WHERE id=? AND state='charged' AND job_id IS NULL "
            "AND recovery_token=?",
            (str(job_id), now, attempt["id"], charge_claim),
        )
        linked_quote = conn.execute(
            "UPDATE short_drama_final_quotes SET consumed_job_id=? "
            "WHERE token=? AND consumed_job_id=?",
            (str(job_id), quote["token"], "attempt:" + attempt["id"]),
        )
        if linked_attempt.rowcount != 1 or linked_quote.rowcount != 1:
            raise PreviewBlocked(
                "charge_recovery_owned",
                "正式导出建单账本已发生变化，请稍后重试",
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
        _compensate_final_attempt(
            db_factory, existing["id"], refund_points,
            "正式导出已扣点但建单失败", claim_token=charge_claim,
        )
        raise
    else:
        conn.close()
    if callable(enqueue):
        enqueue(job_id, "short_drama_final")
    return {
        "project_id": project_id, "job_id": job_id, "status": "queued",
        "cost": int(attempt["cost"]), "replayed": False,
    }


def _compensate_final_attempt(
    db_factory, attempt_id, refund_points, reason, claim_token=None
):
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        attempt = conn.execute(
            "SELECT * FROM short_drama_final_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        if (
            not attempt
            or attempt["job_id"]
            or attempt["state"] in {"refunded", "done", "archived"}
        ):
            conn.commit()
            return attempt is not None and attempt["state"] == "refunded"
        if (
            claim_token is not None
            and attempt["recovery_token"] not in {None, claim_token}
        ):
            conn.commit()
            return False
        updated = conn.execute(
            "UPDATE short_drama_final_attempts SET state='refund_pending',"
            "error=?,recovery_token=NULL,recovery_started_at=NULL,updated_at=? "
            "WHERE id=? AND job_id IS NULL "
            "AND state IN ('accepted','charged','refund_pending')",
            (str(reason)[:300], now, attempt_id),
        )
        attempt = conn.execute(
            "SELECT * FROM short_drama_final_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        conn.commit()
    if updated.rowcount != 1 or attempt["state"] != "refund_pending":
        return False
    if attempt["cost"]:
        if not callable(refund_points):
            return False
        try:
            refund_points(
                attempt["actor_username"], attempt["cost"],
                "短剧正式导出建单补偿", attempt["refund_key"],
            )
        except Exception:
            return False
    with closing(db_factory()) as conn:
        updated = conn.execute(
            "UPDATE short_drama_final_attempts SET state='refunded',"
            "updated_at=? WHERE id=? AND state='refund_pending'",
            (int(time.time()), attempt_id),
        )
        conn.commit()
    return updated.rowcount == 1


def set_final_progress(db_factory, job_id, phase, progress):
    set_preview_progress(db_factory, job_id, phase, progress)


def final_render_context(db_factory, job_id):
    context = preview_render_context(db_factory, job_id)
    payload = context["payload"]
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        preview = _final_preview(
            conn, payload["project_id"], payload["preview_version"]
        )
        if not preview or preview["input_hash"] != payload["input_hash"]:
            raise PreviewBlocked("preview_stale", "正式导出的预览基线已过期")
        preview_payload = _preview_audio_payload(conn, preview)
        if preview_payload["d2_input_hash"] != payload.get("d2_input_hash"):
            raise PreviewBlocked("preview_stale", "正式导出的预览音频基线已过期")
        version = conn.execute(
            "SELECT id,version FROM short_drama_composition_versions "
            "WHERE job_id=? AND kind='final'", (str(job_id),),
        ).fetchone()
    context["final_version_id"] = version["id"]
    context["final_version"] = version["version"]
    return context


def archive_final_asset(db_factory, job_id, result):
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM short_drama_final_assets WHERE job_id=?",
            (str(job_id),),
        ).fetchone()
        if existing:
            conn.commit()
            return dict(existing)
        linked = conn.execute(
            "SELECT v.id AS version_id,v.project_id,j.username,"
            "a.owner_username,a.actor_username,p.title "
            "FROM short_drama_composition_versions v "
            "JOIN short_drama_composition_jobs j ON j.job_id=v.job_id "
            "JOIN short_drama_final_attempts a ON a.job_id=v.job_id "
            "JOIN short_drama_projects p ON p.id=v.project_id "
            "WHERE v.job_id=? AND v.kind='final'", (str(job_id),),
        ).fetchone()
        if not linked:
            conn.rollback()
            raise PreviewBlocked("archive_failed", "正式导出归档上下文不存在")
        asset_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO short_drama_final_assets "
            "(id,owner_username,created_by,project_id,composition_version_id,"
            "job_id,title,object_key,cover_key,video_url,cover_url,size,sha256,"
            "width,height,fps,duration_ms,video_codec,audio_codec,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                asset_id, linked["owner_username"], linked["actor_username"],
                linked["project_id"], linked["version_id"], str(job_id),
                linked["title"], result["object_key"], result["cover_key"],
                result["url"], result["cover_url"], result["size"],
                result["sha256"], result["width"], result["height"],
                result["fps"], result["duration_ms"], result["video_codec"],
                result["audio_codec"], now,
            ),
        )
        conn.execute(
            "UPDATE short_drama_final_attempts SET state='archived',asset_id=?,"
            "updated_at=? WHERE job_id=?", (asset_id, now, str(job_id)),
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM short_drama_final_assets WHERE id=?", (asset_id,)
        ).fetchone())


def final_asset_project_id(db_factory, asset_id):
    asset_id = str(asset_id or "").strip()
    if not asset_id or len(asset_id) > 128:
        raise LookupError("short drama final asset does not exist")
    with closing(db_factory()) as conn:
        row = conn.execute(
            "SELECT project_id FROM short_drama_final_assets "
            "WHERE id=? AND archive_status='ready' AND deleted=0",
            (asset_id,),
        ).fetchone()
    if not row or not row[0]:
        raise LookupError("short drama final asset does not exist")
    return str(row[0])


def get_final_asset(db_factory, owner_username, asset_id):
    asset_id = str(asset_id or "").strip()
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT a.*,v.version,p.ratio "
            "FROM short_drama_final_assets a "
            "LEFT JOIN short_drama_composition_versions v "
            "ON v.id=a.composition_version_id "
            "LEFT JOIN short_drama_projects p ON p.id=a.project_id "
            "WHERE a.id=? AND a.owner_username=? "
            "AND a.archive_status='ready' AND a.deleted=0",
            (asset_id, owner_username),
        ).fetchone()
    if not row:
        raise LookupError("short drama final asset does not exist")
    item = dict(row)
    video_url = item.get("video_url") or ""
    cover_url = item.get("cover_url") or ""
    try:
        from . import cos
        if cos.enabled():
            video_url = cos.object_url(item["object_key"], private=True)
            cover_url = cos.object_url(item["cover_key"], private=True)
    except Exception:
        # Keep discovery available during a transient signing outage. A later
        # retry can obtain a fresh private URL without exposing object keys.
        pass
    width = int(item.get("width") or 0)
    height = int(item.get("height") or 0)
    return {
        "id": item["id"],
        "asset_id": item["id"],
        "source_type": "short_drama_final",
        "project_id": item["project_id"],
        "composition_version_id": item["composition_version_id"],
        "version": item.get("version"),
        "job_id": item["job_id"],
        "title": item["title"],
        "status": "done",
        "video_url": video_url,
        "url": video_url,
        "image_url": cover_url,
        "cover_url": cover_url,
        "mime": item["mime"],
        "size": item["size"],
        "sha256": item["sha256"],
        "width": width,
        "height": height,
        "resolution": "%dx%d" % (width, height),
        "ratio": item.get("ratio") or "",
        "fps": item["fps"],
        "duration_ms": item["duration_ms"],
        "video_codec": item["video_codec"],
        "audio_codec": item["audio_codec"],
        "created_by": item["created_by"],
        "created_at": item["created_at"],
    }


def reconcile_final_job(db_factory, job_id):
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT status,result,error FROM jobs WHERE id=? "
            "AND kind='short_drama_final'", (job_id,),
        ).fetchone()
        linked = conn.execute(
            "SELECT * FROM short_drama_composition_jobs WHERE job_id=?",
            (str(job_id),),
        ).fetchone()
        if not job or not linked or linked["status"] in {"succeeded", "failed"}:
            conn.commit()
            return False
        if job["status"] == "done":
            result = _json_value(job["result"], {})
            required = {
                "file", "url", "cover_file", "cover_url", "object_key",
                "cover_key", "duration_ms", "width", "height", "fps",
                "video_codec", "audio_codec", "size", "sha256", "asset_id",
            }
            if required.issubset(result):
                conn.execute(
                    "UPDATE short_drama_composition_versions SET file=?,url=?,"
                    "cover_file=?,cover_url=?,object_key=?,cover_key=?,"
                    "duration_ms=?,width=?,height=?,fps=?,video_codec=?,"
                    "audio_codec=?,size=?,sha256=?,global_video_asset_id=NULL,"
                    "status='succeeded' WHERE job_id=?",
                    tuple(result[name] for name in (
                        "file", "url", "cover_file", "cover_url", "object_key",
                        "cover_key", "duration_ms", "width", "height", "fps",
                        "video_codec", "audio_codec", "size", "sha256",
                    )) + (str(job_id),),
                )
                row = conn.execute(
                    "SELECT project_id,version FROM "
                    "short_drama_composition_versions WHERE job_id=?",
                    (str(job_id),),
                ).fetchone()
                conn.execute(
                    "UPDATE short_drama_compositions SET current_final_version=?,"
                    "preview_locked=1,updated_at=? WHERE project_id=?",
                    (row["version"], now, row["project_id"]),
                )
                conn.execute(
                    "UPDATE short_drama_composition_jobs SET status='succeeded',"
                    "phase='completed',progress=100,updated_at=?,finished_at=? "
                    "WHERE job_id=?", (now, now, str(job_id)),
                )
                conn.execute(
                    "UPDATE short_drama_final_attempts SET state='done',"
                    "asset_id=?,updated_at=? WHERE job_id=?",
                    (result["asset_id"], now, str(job_id)),
                )
                conn.commit()
                return True
        if job["status"] == "error":
            error = str(job["error"] or "正式导出失败")[:300]
            conn.execute(
                "UPDATE short_drama_composition_versions SET status='failed' "
                "WHERE job_id=? AND status='rendering'", (str(job_id),),
            )
            conn.execute(
                "UPDATE short_drama_composition_jobs SET status='failed',"
                "phase='failed',error_code='export_failed',error_message=?,"
                "updated_at=?,finished_at=? WHERE job_id=?",
                (error, now, now, str(job_id)),
            )
            conn.execute(
                "UPDATE short_drama_final_attempts SET state='refund_pending',"
                "error=?,updated_at=? WHERE job_id=?",
                (error, now, str(job_id)),
            )
            conn.commit()
            return True
        conn.commit()
        return False


def reconcile_final_refunds(db_factory, limit=100):
    """Mirror the generic jobs refund ledger into the D-4 attempt ledger."""
    now = int(time.time())
    with closing(db_factory()) as conn:
        try:
            cursor = conn.execute(
                "UPDATE short_drama_final_attempts SET state='refunded',"
                "updated_at=? WHERE id IN ("
                "SELECT a.id FROM short_drama_final_attempts a "
                "JOIN jobs j ON CAST(j.id AS TEXT)=a.job_id "
                "WHERE a.state='refund_pending' AND COALESCE(j.refunded,0)=1 "
                "LIMIT ?)", (now, max(1, min(1000, int(limit or 100)))),
            )
            conn.commit()
            return cursor.rowcount
        except sqlite3.OperationalError:
            return 0


def _claim_stale_final_attempts(db_factory, limit):
    now = int(time.time())
    cutoff = now - FINAL_CHARGE_RECOVERY_SECONDS
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        candidates = conn.execute(
            "SELECT id FROM short_drama_final_attempts "
            "WHERE state IN ('accepted','charged') AND job_id IS NULL "
            "AND updated_at<=? AND (recovery_token IS NULL "
            "OR COALESCE(recovery_started_at,0)<=?) "
            "ORDER BY updated_at,id LIMIT ?",
            (
                cutoff, cutoff,
                max(1, min(1000, int(limit or 64))),
            ),
        ).fetchall()
        claimed = []
        for candidate in candidates:
            token = "recovery:" + str(uuid.uuid4())
            updated = conn.execute(
                "UPDATE short_drama_final_attempts "
                "SET recovery_token=?,recovery_started_at=? "
                "WHERE id=? AND state IN ('accepted','charged') "
                "AND job_id IS NULL AND updated_at<=? "
                "AND (recovery_token IS NULL "
                "OR COALESCE(recovery_started_at,0)<=?)",
                (token, now, candidate["id"], cutoff, cutoff),
            )
            if updated.rowcount == 1:
                row = conn.execute(
                    "SELECT * FROM short_drama_final_attempts "
                    "WHERE id=? AND recovery_token=?",
                    (candidate["id"], token),
                ).fetchone()
                if row:
                    claimed.append((dict(row), token))
        conn.commit()
    return claimed


def _mark_final_refund_pending_claimed(
    db_factory, attempt_id, token, reason
):
    with closing(db_factory()) as conn:
        updated = conn.execute(
            "UPDATE short_drama_final_attempts SET state='refund_pending',"
            "error=?,recovery_token=NULL,recovery_started_at=NULL,updated_at=? "
            "WHERE id=? AND state IN ('accepted','charged') "
            "AND job_id IS NULL AND recovery_token=?",
            (str(reason)[:300], int(time.time()), attempt_id, token),
        )
        conn.commit()
    return updated.rowcount == 1


def retry_final_charge_attempts(db_factory, points_domain, limit=64):
    """Recover D-4 attempts interrupted between consent and job creation."""
    claimed = _claim_stale_final_attempts(db_factory, limit)
    observed = failed = 0
    for row, token in claimed:
        if row["state"] == "charged":
            observed += int(_mark_final_refund_pending_claimed(
                db_factory, row["id"], token,
                "正式导出已扣点但任务未创建，正在退款",
            ))
            continue
        try:
            ledger = points_domain.get_points_transaction(row["charge_key"])
        except Exception:
            _release_final_charge_lease(db_factory, row["id"], token)
            continue
        if _final_charge_ledger_matches(row, ledger):
            observed += int(_mark_final_refund_pending_claimed(
                db_factory, row["id"], token,
                "正式导出扣点已完成但任务未创建，正在退款",
            ))
            continue
        if ledger is not None:
            _mark_final_charge_inconsistent(db_factory, row["id"], token)
            continue
        with closing(db_factory()) as conn:
            if row["error"] == FINAL_CHARGE_LEDGER_ABSENT_ONCE:
                updated = conn.execute(
                    "UPDATE short_drama_final_attempts "
                    "SET state='failed',error=?,recovery_token=NULL,"
                    "recovery_started_at=NULL,updated_at=? "
                    "WHERE id=? AND state='accepted' AND job_id IS NULL "
                    "AND recovery_token=?",
                    (
                        "正式导出扣点流水连续两次未出现，请重新询价",
                        int(time.time()), row["id"], token,
                    ),
                )
                failed += int(updated.rowcount == 1)
            else:
                updated = conn.execute(
                    "UPDATE short_drama_final_attempts SET error=?,"
                    "recovery_token=NULL,recovery_started_at=NULL,updated_at=? "
                    "WHERE id=? AND state='accepted' AND job_id IS NULL "
                    "AND recovery_token=?",
                    (
                        FINAL_CHARGE_LEDGER_ABSENT_ONCE,
                        int(time.time()), row["id"], token,
                    ),
                )
                observed += int(updated.rowcount == 1)
            conn.commit()
    with closing(db_factory()) as conn:
        refund_rows = conn.execute(
            "SELECT id FROM short_drama_final_attempts "
            "WHERE state='refund_pending' AND job_id IS NULL "
            "ORDER BY updated_at,id LIMIT ?",
            (max(1, min(1000, int(limit or 64))),),
        ).fetchall()
    refunded = 0
    for row in refund_rows:
        refunded += int(_compensate_final_attempt(
            db_factory, row[0], points_domain.refund_points,
            "短剧正式导出孤立扣点恢复",
        ))
    return {
        "observed": observed, "failed": failed, "refunded": refunded,
    }


def confirm_final(db_factory, owner_username, body):
    from . import short_drama_completion
    short_drama_completion.reject_legacy_completion()
    if not isinstance(body, dict) or set(body) != {
        "project_id", "revision", "final_version"
    }:
        raise ValueError("确认成片请求字段不正确")
    now = int(time.time())
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? "
            "AND deleted=0", (str(body.get("project_id") or ""), owner_username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if project["stage"] == "completed":
            conn.commit()
            return {
                "project_id": project["id"], "stage": "completed",
                "revision": project["revision"], "replayed": True,
            }
        if (
            project["stage"] != "assembly_review"
            or project["revision"] != body.get("revision")
        ):
            raise PreviewBlocked("revision_conflict", "项目状态已更新")
        version = conn.execute(
            "SELECT v.version,a.id AS asset_id FROM "
            "short_drama_composition_versions v "
            "JOIN short_drama_final_assets a ON a.composition_version_id=v.id "
            "WHERE v.project_id=? AND v.kind='final' AND v.version=? "
            "AND v.status='succeeded' AND a.archive_status='ready' "
            "AND a.deleted=0",
            (project["id"], body.get("final_version")),
        ).fetchone()
        if not version:
            raise PreviewBlocked("final_missing", "尚无可确认的正式成片资产")
        conn.execute(
            "UPDATE short_drama_projects SET stage='completed',"
            "revision=revision+1,updated_at=? WHERE id=?",
            (now, project["id"]),
        )
        conn.commit()
        return {
            "project_id": project["id"], "stage": "completed",
            "revision": project["revision"] + 1,
            "asset_id": version["asset_id"], "replayed": False,
        }
