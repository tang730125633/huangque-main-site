"""Final assembly for locked short-drama video clips."""

import hashlib
import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path


ASSEMBLY_STAGES = {"assembly_review", "completed"}
COMPOSITION_KINDS = {"preview", "final"}

DEFAULT_ASSEMBLY_CONFIG = {
    "subtitle": {
        "enabled": True,
        "preset": "white_outline",
        "position": "bottom",
    },
    "bgm": {
        "asset_id": None,
        "volume": 0.18,
        "fade_in_ms": 500,
        "fade_out_ms": 800,
    },
    "profiles": {
        "preview": "short_drama_preview_v1",
        "final": "short_drama_final_v1",
    },
}

_BLOCKER_MESSAGES = {
    "missing_locked_voice_shot": "镜头尚未锁定配音与字幕",
    "missing_locked_video_shot": "镜头尚无已确认的电影化身视频版本",
    "active_composition_job": "项目仍有合成任务处理中",
    "preview_missing": "尚未生成可用预览版本",
    "final_missing": "尚未生成可用正式成片",
}

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
"""


# ponytail: one encoder at a time protects the small test server; use a durable
# render queue only when concurrent exports become a real requirement.
_RENDER_LOCK = threading.Lock()


def init_db(db_factory):
    with closing(db_factory()) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_composition_jobs SET status='failed',"
            "phase='interrupted',error_code='service_restarted',"
            "error_message='合成服务重启，请重新导出',updated_at=?,finished_at=? "
            "WHERE status IN ('queued','running')",
            (now, now),
        )
        conn.execute(
            "UPDATE short_drama_composition_versions SET status='failed' "
            "WHERE status='rendering'"
        )
        conn.commit()


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
        "profiles": dict(DEFAULT_ASSEMBLY_CONFIG["profiles"]),
    }
    for section in config:
        candidate = saved.get(section)
        if isinstance(candidate, dict):
            config[section].update(candidate)
    return config


def _blocker(code, shot_id=None):
    item = {"code": code, "message": _BLOCKER_MESSAGES[code]}
    if shot_id is not None:
        item["shot_id"] = shot_id
    return item


def _version_snapshot(row):
    url = row["url"]
    try:
        from . import cos
        if row["file"] and row["status"] == "succeeded" and cos.enabled():
            url = cos.object_url(row["file"], private=True)
    except Exception:
        pass
    return {
        "id": row["id"],
        "kind": row["kind"],
        "version": row["version"],
        "job_id": row["job_id"],
        "input_hash": row["input_hash"],
        "config": _json_value(row["config_json"], {}),
        "url": url,
        "duration_ms": row["duration_ms"],
        "width": row["width"],
        "height": row["height"],
        "fps": row["fps"],
        "video_codec": row["video_codec"],
        "audio_codec": row["audio_codec"],
        "status": row["status"],
        "global_video_asset_id": row["global_video_asset_id"],
        "created_at": row["created_at"],
    }


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


def build_assembly_snapshot(conn, project):
    """Build the assembly read model without creating a workspace row."""
    conn.row_factory = sqlite3.Row
    composition = conn.execute(
        "SELECT * FROM short_drama_compositions WHERE project_id=?",
        (project["id"],),
    ).fetchone()
    voice_states = {
        row["shot_id"]: bool(row["locked"])
        for row in conn.execute(
            "SELECT shot_id,locked FROM short_drama_voice_shots "
            "WHERE project_id=?",
            (project["id"],),
        )
    }
    video_states = {
        row["shot_id"]: row
        for row in conn.execute(
            "SELECT a.shot_id,a.current_version,a.locked,v.file,v.url "
            "FROM short_drama_video_assets a "
            "LEFT JOIN short_drama_video_versions v "
            "ON v.asset_id=a.id AND v.version=a.current_version "
            "WHERE a.project_id=?",
            (project["id"],),
        )
    }
    blockers = []
    shots = []
    for shot in conn.execute(
        "SELECT id,shot_key,sort_order,duration "
        "FROM short_drama_shots WHERE project_id=? ORDER BY sort_order,id",
        (project["id"],),
    ):
        shot_blockers = []
        voice_locked = voice_states.get(shot["id"], False)
        if not voice_locked:
            shot_blockers.append(
                _blocker("missing_locked_voice_shot", shot["id"])
            )
        video = video_states.get(shot["id"])
        video_confirmed = bool(
            video and video["locked"] and video["current_version"] and video["file"]
        )
        if not video_confirmed:
            shot_blockers.append(
                _blocker("missing_locked_video_shot", shot["id"])
            )
        blockers.extend(shot_blockers)
        shots.append({
            "id": shot["id"],
            "shot_key": shot["shot_key"],
            "sort_order": shot["sort_order"],
            "duration": shot["duration"],
            "voice": {
                "locked": voice_locked,
                "status": "ready" if voice_locked else "blocked",
            },
            "video": {
                "confirmed": video_confirmed,
                "status": "ready" if video_confirmed else "blocked",
                "current_version": (
                    video["current_version"] if video_confirmed else None
                ),
            },
            "ready": voice_locked and video_confirmed,
            "blockers": shot_blockers,
        })
    active_job = conn.execute(
        "SELECT * FROM short_drama_composition_jobs "
        "WHERE project_id=? AND status IN ('queued','running') "
        "ORDER BY updated_at DESC,job_id DESC LIMIT 1",
        (project["id"],),
    ).fetchone()
    if active_job:
        blockers.append(_blocker("active_composition_job"))
    versions = [
        _version_snapshot(row)
        for row in conn.execute(
            "SELECT * FROM short_drama_composition_versions "
            "WHERE project_id=? ORDER BY created_at DESC,kind,version DESC",
            (project["id"],),
        )
    ]
    final_versions = [
        item for item in versions
        if item["kind"] == "final" and item["status"] == "succeeded"
    ]
    if not final_versions:
        blockers.append(_blocker("final_missing"))
    current_preview = (
        composition["current_preview_version"] if composition else None
    )
    current_final = (
        composition["current_final_version"] if composition else None
    )
    preview_locked = bool(composition["preview_locked"]) if composition else False
    current_final_ready = any(
        item["version"] == current_final for item in final_versions
    )
    readiness_blockers = [
        item for item in blockers
        if item["code"] in {
            "missing_locked_voice_shot",
            "missing_locked_video_shot",
            "active_composition_job",
        }
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
        "config": _merge_config(
            composition["config_json"] if composition else None
        ),
        "current_preview_version": current_preview,
        "current_final_version": current_final,
        "preview_locked": preview_locked,
        "implementation_status": "renderable",
        "rendering_enabled": True,
        "shots": shots,
        "versions": versions,
        "active_job": _active_job_snapshot(active_job),
        "readiness": {
            "ready": not readiness_blockers,
            "blockers": readiness_blockers,
        },
        "actions": {
            "can_save_config": False,
            "can_preview": False,
            "can_lock_preview": False,
            "can_export": (
                project["stage"] == "assembly_review" and not readiness_blockers
            ),
            "can_confirm": (
                project["stage"] == "assembly_review"
                and current_final_ready and active_job is None
            ),
        },
        "blockers": blockers,
    }


def get_assembly_workspace(db_factory, username, project_id):
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
        return build_assembly_snapshot(conn, project)


def user_owns_output_file(db_factory, username, file_name):
    try:
        with closing(db_factory()) as conn:
            return bool(conn.execute(
                "SELECT 1 FROM short_drama_composition_versions v "
                "JOIN short_drama_projects p ON p.id=v.project_id "
                "WHERE p.username=? AND p.deleted=0 AND v.status='succeeded' "
                "AND v.file=? LIMIT 1",
                (username, file_name),
            ).fetchone())
    except Exception:
        return False


def _locked_inputs(conn, project):
    rows = conn.execute(
        "SELECT s.id,s.duration,v.id version_id,v.file,video_job.username producer_username "
        "FROM short_drama_shots s "
        "LEFT JOIN short_drama_voice_shots voice "
        "ON voice.shot_id=s.id AND voice.project_id=s.project_id "
        "LEFT JOIN short_drama_video_assets a "
        "ON a.shot_id=s.id AND a.project_id=s.project_id "
        "LEFT JOIN short_drama_video_versions v "
        "ON v.asset_id=a.id AND v.version=a.current_version "
        "LEFT JOIN short_drama_video_jobs video_job ON video_job.job_id=v.job_id "
        "WHERE s.project_id=? AND voice.locked=1 AND a.locked=1 "
        "ORDER BY s.sort_order,s.id",
        (project["id"],),
    ).fetchall()
    expected = conn.execute(
        "SELECT COUNT(*) FROM short_drama_shots WHERE project_id=?",
        (project["id"],),
    ).fetchone()[0]
    if not rows or len(rows) != expected or any(not row["file"] for row in rows):
        raise ValueError("请先锁定全部镜头的配音和视频版本")
    if sum(int(row["duration"]) for row in rows) != int(project["target_duration"]):
        raise ValueError("镜头总时长与项目时长不一致")
    return rows


def _render_hash(project, rows, config):
    value = {
        "project_id": project["id"],
        "revision": int(project["revision"]),
        "ratio": project["ratio"],
        "versions": [row["version_id"] for row in rows],
        "config": config,
    }
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _run(command, timeout=1800):
    try:
        return subprocess.run(
            command, check=True, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("服务器未安装 FFmpeg，无法合成短剧") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()[-1:] or ["未知错误"]
        raise RuntimeError("短剧合成失败：%s" % detail[0][:180]) from exc


def _has_audio(path):
    result = _run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
    ], timeout=30)
    return bool(result.stdout.strip())


def _normalize_clip(source, target, duration, width, height):
    duration_text = str(int(duration))
    video_filter = (
        "scale=%d:%d:force_original_aspect_ratio=decrease,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=24,"
        "tpad=stop_mode=clone:stop_duration=%s,trim=duration=%s,setpts=PTS-STARTPTS"
        % (width, height, width, height, duration_text, duration_text)
    )
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
    ]
    if _has_audio(source):
        command += ["-map", "0:v:0", "-map", "0:a:0", "-vf", video_filter,
                    "-af", "aresample=async=1:first_pts=0,apad,atrim=duration=%s,"
                    "asetpts=PTS-STARTPTS" % duration_text]
    else:
        command += ["-f", "lavfi", "-t", duration_text, "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-map", "0:v:0", "-map", "1:a:0", "-vf", video_filter]
    command += [
        "-t", duration_text, "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000",
        "-ac", "2", "-movflags", "+faststart", str(target),
    ]
    _run(command)
    if not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError("镜头标准化产物为空")


def _probe(path):
    result = _run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
        "-of", "json", str(path),
    ], timeout=30)
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    numerator, _, denominator = str(video.get("r_frame_rate") or "0/1").partition("/")
    fps = float(numerator or 0) / float(denominator or 1)
    return {
        "duration_ms": round(float((data.get("format") or {}).get("duration") or 0) * 1000),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": fps,
        "video_codec": str(video.get("codec_name") or ""),
        "audio_codec": str(audio.get("codec_name") or ""),
    }


def _set_job_progress(db_factory, job_id, phase, progress):
    with closing(db_factory()) as conn:
        conn.execute(
            "UPDATE short_drama_composition_jobs SET status='running',phase=?,"
            "progress=?,attempt_count=1,updated_at=? WHERE job_id=? "
            "AND status IN ('queued','running')",
            (phase, progress, int(time.time()), job_id),
        )
        conn.commit()


def _render_worker(db_factory, username, project_id, job_id, rows, ratio, target_duration):
    from . import core

    output_rel = "video/short_drama_%s.mp4" % uuid.uuid4().hex
    output = core._out_path(output_rel)
    try:
        with _RENDER_LOCK:
            _set_job_progress(db_factory, job_id, "normalizing", 5)
            width, height = ((1080, 1920) if ratio == "9:16" else (1920, 1080))
            with tempfile.TemporaryDirectory(prefix="hq-short-drama-") as work:
                normalized = []
                for index, row in enumerate(rows):
                    producer = row.get("producer_username") or username
                    if not core._user_owns_output_file(producer, row["file"]):
                        raise ValueError("镜头视频不存在或不属于当前账号")
                    source = core._resolve_out_file(row["file"])
                    if not source:
                        raise ValueError("镜头视频文件不存在")
                    target = Path(work) / ("clip-%02d.mp4" % index)
                    _normalize_clip(source, target, row["duration"], width, height)
                    normalized.append(target)
                    _set_job_progress(
                        db_factory, job_id, "normalizing",
                        10 + round(70 * (index + 1) / len(rows)),
                    )
                manifest = Path(work) / "concat.txt"
                manifest.write_text("".join(
                    "file '%s'\n" % str(path).replace("'", "'\\''")
                    for path in normalized
                ), encoding="utf-8")
                _set_job_progress(db_factory, job_id, "assembling", 85)
                _run([
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "concat", "-safe", "0", "-i", str(manifest),
                    "-c", "copy", "-movflags", "+faststart", str(output),
                ])
            if not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError("正式成片为空")
            media = _probe(output)
            if abs(media["duration_ms"] - int(target_duration) * 1000) > 1200:
                raise RuntimeError("正式成片时长校验失败")
            if not media["video_codec"] or not media["audio_codec"]:
                raise RuntimeError("正式成片音视频轨道不完整")
            url = core.public_url(output_rel, "video/mp4", private=True)
            now = int(time.time())
            with closing(db_factory()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                version = conn.execute(
                    "SELECT version FROM short_drama_composition_versions "
                    "WHERE project_id=? AND job_id=? AND status='rendering'",
                    (project_id, job_id),
                ).fetchone()
                if not version:
                    raise RuntimeError("合成任务已失效")
                conn.execute(
                    "UPDATE short_drama_composition_versions SET file=?,url=?,"
                    "duration_ms=?,width=?,height=?,fps=?,video_codec=?,audio_codec=?,"
                    "status='succeeded' WHERE project_id=? AND job_id=?",
                    (output_rel, url, media["duration_ms"], media["width"],
                     media["height"], media["fps"], media["video_codec"],
                     media["audio_codec"], project_id, job_id),
                )
                conn.execute(
                    "UPDATE short_drama_compositions SET current_final_version=?,"
                    "assembly_revision=assembly_revision+1,updated_at=? WHERE project_id=?",
                    (version[0], now, project_id),
                )
                conn.execute(
                    "UPDATE short_drama_composition_jobs SET status='succeeded',"
                    "phase='completed',progress=100,updated_at=?,finished_at=? WHERE job_id=?",
                    (now, now, job_id),
                )
                conn.commit()
    except Exception as exc:
        try:
            if output.exists():
                output.unlink()
        except OSError:
            pass
        now = int(time.time())
        with closing(db_factory()) as conn:
            conn.execute(
                "UPDATE short_drama_composition_versions SET status='failed' "
                "WHERE project_id=? AND job_id=? AND status='rendering'",
                (project_id, job_id),
            )
            conn.execute(
                "UPDATE short_drama_composition_jobs SET status='failed',phase='failed',"
                "error_code='render_failed',error_message=?,updated_at=?,finished_at=? "
                "WHERE job_id=?",
                (str(exc)[:300], now, now, job_id),
            )
            conn.commit()


def start_final_render(db_factory, username, project_id, revision, idempotency_key):
    if (not isinstance(project_id, str) or not project_id.strip()
            or type(revision) is not int or revision < 1
            or not isinstance(idempotency_key, str) or not idempotency_key.strip()
            or len(idempotency_key) > 128):
        raise ValueError("正式成片请求参数无效")
    project_id = project_id.strip()
    idempotency_key = idempotency_key.strip()
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if int(project["revision"]) != revision:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已更新，请刷新后重试")
        if project["stage"] != "assembly_review":
            raise ValueError("短剧项目尚未进入合成阶段")
        rows = _locked_inputs(conn, project)
        composition = conn.execute(
            "SELECT * FROM short_drama_compositions WHERE project_id=?",
            (project_id,),
        ).fetchone()
        config = _merge_config(composition["config_json"] if composition else None)
        request_hash = _render_hash(project, rows, config)
        existing = conn.execute(
            "SELECT project_id,request_hash FROM short_drama_composition_jobs "
            "WHERE username=? AND kind='final' AND idempotency_key=?",
            (username, idempotency_key),
        ).fetchone()
        if existing:
            if existing["project_id"] != project_id or existing["request_hash"] != request_hash:
                raise ValueError("幂等键已用于其他合成请求")
            conn.commit()
            return get_assembly_workspace(db_factory, username, project_id)
        if conn.execute(
            "SELECT 1 FROM short_drama_composition_jobs WHERE project_id=? "
            "AND status IN ('queued','running') LIMIT 1", (project_id,),
        ).fetchone():
            raise ValueError("项目已有成片正在合成")
        now = int(time.time())
        if not composition:
            conn.execute(
                "INSERT INTO short_drama_compositions "
                "(project_id,config_json,created_at,updated_at) VALUES (?,?,?,?)",
                (project_id, json.dumps(config, ensure_ascii=False), now, now),
            )
        version = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_composition_versions "
            "WHERE project_id=? AND kind='final'", (project_id,),
        ).fetchone()[0])
        job_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO short_drama_composition_versions "
            "(id,project_id,kind,version,job_id,input_hash,config_json,status,created_at) "
            "VALUES (?,?,'final',?,?,?,?, 'rendering',?)",
            (uuid.uuid4().hex, project_id, version, job_id, request_hash,
             json.dumps(config, ensure_ascii=False), now),
        )
        conn.execute(
            "INSERT INTO short_drama_composition_jobs "
            "(id,username,project_id,job_id,kind,idempotency_key,request_hash,"
            "status,phase,progress,created_at,updated_at) "
            "VALUES (?,?,?,?, 'final',?,?,'queued','queued',0,?,?)",
            (uuid.uuid4().hex, username, project_id, job_id, idempotency_key,
             request_hash, now, now),
        )
        rows = [dict(row) for row in rows]
        ratio = project["ratio"]
        target_duration = int(project["target_duration"])
        conn.commit()
    threading.Thread(
        target=_render_worker,
        args=(db_factory, username, project_id, job_id, rows, ratio, target_duration),
        name="short-drama-render-" + job_id[:8], daemon=True,
    ).start()
    return get_assembly_workspace(db_factory, username, project_id)


def confirm_completed(db_factory, username, project_id, revision):
    if (not isinstance(project_id, str) or not project_id.strip()
            or type(revision) is not int or revision < 1):
        raise ValueError("确认成片请求参数无效")
    with closing(db_factory()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? AND deleted=0",
            (project_id.strip(), username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if int(project["revision"]) != revision:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已更新，请刷新后重试")
        if project["stage"] != "assembly_review":
            raise ValueError("短剧项目尚未进入成片确认阶段")
        ready = conn.execute(
            "SELECT 1 FROM short_drama_compositions c "
            "JOIN short_drama_composition_versions v ON v.project_id=c.project_id "
            "AND v.kind='final' AND v.version=c.current_final_version "
            "WHERE c.project_id=? AND v.status='succeeded'",
            (project_id.strip(),),
        ).fetchone()
        if not ready:
            raise ValueError("请先生成并检查正式成片")
        now = int(time.time())
        updated = conn.execute(
            "UPDATE short_drama_projects SET stage='completed',revision=revision+1,"
            "updated_at=? WHERE id=? AND revision=? AND stage='assembly_review'",
            (now, project_id.strip(), revision),
        )
        if updated.rowcount != 1:
            from .short_drama import RevisionConflict
            raise RevisionConflict("项目已更新，请刷新后重试")
        conn.commit()
    return get_assembly_workspace(db_factory, username, project_id.strip())
