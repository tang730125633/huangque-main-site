"""Standalone short-drama automatic draft production (PR-4).

Consumes a confirmed PR-3 plan and records a durable paid attempt. A fixed
sample video may only be used when the explicit demo switch is enabled; it is
never represented as a real project result.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from providers.short_drama_visual import capability_snapshot
from providers.short_drama_visual.heygen_cinematic import (
    HeyGenCinematicShotProvider,
)
from providers.short_drama_visual.runtime import load_by_name, load_from_environment

from . import points as points_domain
from . import short_drama_assembly_plan as media_plan


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
        "SELECT id,title,ratio,target_duration,shot_count,point_budget,spent_points "
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


def _provider_assembly_snapshot(conn, project_id, plan):
    """Return one immutable latest ready Provider asset for every planned shot."""
    required = [
        str(item.get("shot_key") or "shot_%02d" % (index + 1))
        for index, item in enumerate(plan.get("material_plan") or [])
        if isinstance(item, dict)
    ]
    latest = {}
    for row in conn.execute(
        "SELECT * FROM short_drama_provider_shot_versions "
        "WHERE project_id=? AND status='ready' "
        "ORDER BY shot_key,version DESC,created_at DESC",
        (project_id,),
    ).fetchall():
        item = _provider_version(row)
        latest.setdefault(str(item["shot_key"]), item)
    shots = [latest[key] for key in required if key in latest]
    return {
        "required_shot_keys": required,
        "ready_shot_keys": [str(item["shot_key"]) for item in shots],
        "required_count": len(required),
        "ready_count": len(shots),
        "missing_shot_keys": [key for key in required if key not in latest],
        "all_ready": bool(required) and len(shots) == len(required),
        "shots": shots,
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


def _table_exists(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone())


def _locked_media_contract(conn, project):
    empty = {
        "contract_version": "short-drama-locked-media-v1",
        "delivery_eligible": False,
        "reason": "locked_voice_timeline_missing",
        "audio_tracks": [], "subtitles": [],
        "audio_hash": "", "subtitle_hash": "", "timeline_hash": "",
        "subtitle_required": False,
    }
    required = {
        "short_drama_shots", "short_drama_voice_shots",
        "short_drama_voice_lines", "short_drama_voice_versions",
    }
    if any(not _table_exists(conn, name) for name in required):
        return empty
    shot_rows = conn.execute(
        "SELECT shot.id,shot.shot_key,shot.sort_order,shot.duration,"
        "voice.locked,voice.timeline_revision FROM short_drama_shots shot "
        "LEFT JOIN short_drama_voice_shots voice ON voice.shot_id=shot.id "
        "WHERE shot.project_id=? ORDER BY shot.sort_order,shot.id",
        (project["id"],),
    ).fetchall()
    if not shot_rows or any(not row["locked"] for row in shot_rows):
        return empty
    cursor = 0
    tracks, subtitles, timeline = [], [], []
    for shot in shot_rows:
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
            "timeline_revision": int(shot["timeline_revision"]),
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


def _render_provider_preview(project_id, job_id, assembly):
    """Normalize paid shots with their locked audio/subtitle timeline."""
    sources = [_controlled_provider_file(item["file"]) for item in assembly["shots"]]
    if not sources:
        raise AutodraftError("provider_shots_required", "没有可合成的镜头", 409)
    root = _content_root()
    target_dir = root / "short_drama_autodraft" / project_id / job_id
    temp_dir = target_dir.with_name(".%s.tmp" % target_dir.name)
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    output = temp_dir / "preview-720p.mp4"
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
    width, height = ((720, 1280) if ratio == "9:16" else (1280, 720))
    filters = []
    labels = []
    for index in range(len(sources)):
        filters.append(
            "[%d:v:0]scale=%d:%d:force_original_aspect_ratio=decrease,"
            "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black,fps=25,setsar=1,"
            "setpts=PTS-STARTPTS[v%d]" % (
                index, width, height, width, height, index,
            )
        )
        labels.append("[v%d]" % index)
    filters.append(
        "%sconcat=n=%d:v=1:a=0[outv]" % ("".join(labels), len(labels))
    )
    duration_ms = int(assembly.get("duration_ms") or 0)
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
        for index, source in enumerate(sources):
            try:
                probe = media_plan.probe_media(source)
            except media_plan.MediaPlanError as error:
                raise AutodraftError(error.code, str(error), 409) from error
            label = "a%d" % index
            if probe.get("audio"):
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
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
    ])
    if subtitle_input:
        command.extend(["-c:s", "mov_text"])
    if duration_ms > 0:
        command.extend(["-t", "%.3f" % (duration_ms / 1000.0)])
    command.extend(["-movflags", "+faststart", str(output)])
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AutodraftError(
            "preview_renderer_unavailable", "720p 预览合成器不可用", 503
        ) from error
    if result.returncode != 0 or not output.is_file():
        raise AutodraftError(
            "preview_render_failed",
            str(result.stderr or "720p 预览合成失败").strip()[-500:], 409,
        )
    try:
        probe = media_plan.probe_media(output)
    except media_plan.MediaPlanError as error:
        raise AutodraftError(error.code, str(error), 409) from error
    actual_width, actual_height = media_plan.dimensions_for_ratio(probe)
    if (actual_width, actual_height) != (width, height) or not probe.get("audio"):
        raise AutodraftError(
            "preview_media_invalid", "720p 预览的画幅或音频流验证失败", 409
        )
    if duration_ms and abs(int(probe["duration_ms"]) - duration_ms) > 1500:
        raise AutodraftError(
            "preview_duration_invalid", "720p 预览时长与锁定时间线不一致", 409
        )
    if subtitle_input:
        try:
            subtitle_probe = subprocess.run(
                [
                    os.environ.get("FFPROBE_BIN", "ffprobe"), "-v", "error",
                    "-select_streams", "s", "-show_entries", "stream=index",
                    "-of", "csv=p=0", str(output),
                ], capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AutodraftError(
                "preview_probe_failed", "720p 预览字幕流验证失败", 409
            ) from error
        if subtitle_probe.returncode != 0 or not subtitle_probe.stdout.strip():
            raise AutodraftError(
                "preview_subtitle_missing", "720p 预览缺少锁定字幕流", 409
            )
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir.rename(target_dir)
    relative = output.relative_to(temp_dir)
    file_key = (Path("short_drama_autodraft") / project_id / job_id / relative).as_posix()
    return {
        "file": file_key, "url": "/api/gen/file/" + file_key,
        "probe": probe,
    }


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
            provider_ready = bool(
                str(avatar.get("image_url") or "").strip()
                if provider_name == "grok"
                else str(avatar.get("provider_avatar_id") or "").strip()
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
        avatar_by_id = {str(item["id"]): item for item in avatars}
        for row in conn.execute(
            "SELECT character_key,name,avatar_id,reference_file,reference_url,"
            "reference_locked "
            "FROM short_drama_characters WHERE project_id=? ORDER BY sort_order,id",
            (project_id,),
        ).fetchall():
            avatar = avatar_by_id.get(str(row["avatar_id"] or ""))
            character_reference_ready = bool(
                row["reference_locked"]
                and (
                    str(row["reference_file"] or "").strip()
                    or str(row["reference_url"] or "").strip().startswith(
                        ("http://", "https://")
                    )
                )
            )
            generation_identity_id = (
                str(avatar["id"])
                if avatar
                else "character:" + str(row["character_key"])
                if provider_name == "grok" and character_reference_ready
                else ""
            )
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
            keys = [
                str(value) for value in source.get("character_keys") or []
                if str(value)
            ]
            dialogue_keys = [
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


def _character_binding_blockers(conn, project_id, plan):
    """Require prepared standalone roles without breaking untouched legacy plans."""
    rows = conn.execute(
        "SELECT character_key,name,avatar_id FROM short_drama_characters "
        "WHERE project_id=? ORDER BY sort_order,id",
        (project_id,),
    ).fetchall()
    if not rows:
        return []
    bound = {
        str(row["character_key"]): bool(row["avatar_id"])
        for row in rows
    }
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


def preview_provider_request(
    db_factory, owner_username, actor_username, body, avatar_lookup=None,
    include_private=False,
):
    """Compile one exact visual-provider request without billing or I/O."""
    project_id = str(body.get("project_id") or "").strip()
    plan_id = str(body.get("plan_id") or "").strip()
    shot_key = str(body.get("shot_key") or "").strip()
    avatar_id = str(body.get("avatar_id") or "").strip()
    character_key = str(body.get("character_key") or "").strip()
    provider = load_from_environment() or HeyGenCinematicShotProvider()
    conn = _connection(db_factory)
    try:
        project = _project(conn, owner_username, project_id)
        plan = _confirmed_plan(conn, project_id, plan_id)
        source_row = conn.execute(
            "SELECT script_json FROM short_drama_script_snapshots "
            "WHERE id=? AND project_id=?",
            (plan["source_script_version_id"], project_id),
        ).fetchone()
        source_script = _json(source_row["script_json"], {}) if source_row else {}
    finally:
        conn.close()
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
    if not character_key:
        dialogue = [
            item for item in shot.get("dialogue") or []
            if isinstance(item, dict)
        ]
        character_key = str(
            (dialogue[0].get("character_key") if dialogue else "")
            or ((shot.get("character_keys") or [""])[0])
            or ""
        ).strip()
    if not avatar_id and character_key:
        conn = _connection(db_factory)
        try:
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
        finally:
            conn.close()
    if not avatar_id:
        raise AutodraftError(
            "provider_avatar_required", "请先为当前角色锁定一张标准形象图", 422
        )
    if provider.name == "grok" and avatar_id == "character:" + character_key:
        conn = _connection(db_factory)
        try:
            reference = conn.execute(
                "SELECT name,reference_file,reference_url,reference_locked "
                "FROM short_drama_characters "
                "WHERE project_id=? AND character_key=?",
                (project_id, character_key),
            ).fetchone()
        finally:
            conn.close()
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
    else:
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
        reference_image_url or reference_image_file
        if provider.name == "grok"
        else str(avatar.get("provider_avatar_id") or "").strip()
    )
    if str(avatar.get("status") or "") != "ready" or not provider_identity_ready:
        raise AutodraftError(
            "provider_avatar_not_ready", "所选电影化身缺少当前 Provider 所需的形象资产", 422
        )
    duration_ms = int(shot.get("duration_ms") or 0)
    duration_seconds = max(1, (duration_ms + 999) // 1000)
    outbound = {
        "provider_avatar_id": str(avatar.get("provider_avatar_id") or ""),
        "reference_image_url": reference_image_url,
        "reference_image_file": reference_image_file,
        "prompt": _visual_prompt(shot),
        "ratio": str(project.get("ratio") or "16:9"),
        "resolution": str(
            (plan["plan"].get("estimate") or {}).get("resolution") or "720p"
        ).lower(),
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
        "request": {
            "prompt": validated["prompt"],
            "ratio": validated["ratio"],
            "resolution": validated["resolution"],
            "duration_seconds": validated["duration_seconds"],
            "provider_avatar": "[已绑定]",
        },
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


def _provider_version(row):
    if not row:
        return None
    item = dict(row)
    item["version"] = int(item["version"])
    return item


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


def start_provider_job(
    db_factory, owner_username, actor_username, body, idempotency_key,
    avatar_lookup=None, deduct_points=None, refund_points=None,
    charge_lookup=None, project_usage=None,
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
                    raise
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


def _provider_attempt_for_job(conn, job_id):
    return conn.execute(
        "SELECT * FROM short_drama_provider_shot_attempts WHERE job_id=?",
        (job_id,),
    ).fetchone()


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


def _recover_provider_refund(db_factory, job_id, refund_points=None):
    if not callable(refund_points):
        return
    conn = _connection(db_factory)
    try:
        attempt = _provider_attempt_for_job(conn, job_id)
    finally:
        conn.close()
    if not attempt or attempt["state"] != "refund_pending":
        return
    try:
        refund_points(
            attempt["actor_username"], int(attempt["cost"]),
            "短剧单镜头生成失败补偿", attempt["refund_key"],
        )
    except Exception:
        return
    conn = _connection(db_factory)
    try:
        conn.execute(
            "UPDATE short_drama_provider_shot_attempts SET state='refunded',"
            "updated_at=? WHERE id=? AND state='refund_pending'",
            (int(time.time()), attempt["id"]),
        )
        conn.commit()
    finally:
        conn.close()


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
    inspect_conn = _connection(db_factory)
    try:
        current_status = inspect_conn.execute(
            "SELECT status FROM short_drama_provider_shot_jobs WHERE id=?",
            (row["id"],),
        ).fetchone()
    finally:
        inspect_conn.close()
    if not current_status or current_status["status"] != "running":
        return
    result = provider.fetch_result(
        row["provider_job_id"], provider_state.get("result_url")
    )
    now = int(time.time())
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
            (row["id"],),
        ).fetchone()
        if not current or current["status"] != "running":
            conn.commit()
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
            "progress=100,result_json=?,error_json=NULL,updated_at=? "
            "WHERE id=? AND status='running'",
            (_json_text(final_result), now, row["id"]),
        )
        conn.execute(
            "UPDATE short_drama_provider_shot_attempts SET state='done',"
            "updated_at=? WHERE job_id=? AND state IN ('charged','linked')",
            (now, row["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    finally:
        conn.close()
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
            _expire_provider_job(
                db_factory, job_id, timeout_reason,
                refund_points=refund_points,
            )
        else:
            provider = load_by_name(row["provider"])
            if provider is None:
                return _provider_job(row)
            try:
                provider_state = provider.get_job(row["provider_job_id"])
                status = str(provider_state.get("status") or "unknown").lower()
                if status in {"completed", "complete", "succeeded", "success"}:
                    _finish_provider_job(db_factory, row, provider, provider_state)
                elif status in {"failed", "error", "canceled", "cancelled"}:
                    error = AutodraftError(
                        "provider_generation_failed",
                        "Provider 未能生成当前镜头",
                        502,
                    )
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
    )
    conn = _connection(db_factory)
    try:
        return _provider_job(conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
            (job_id,),
        ).fetchone())
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
        duration_ms = int(
            (plan["plan"].get("duration") or {}).get("target_ms") or 0
        )
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
        material = {
            str(item.get("shot_key") or ""): item
            for item in plan["plan"].get("material_plan") or []
            if isinstance(item, dict)
        }
        cards = []
        for index, asset in enumerate(assembly["shots"]):
            shot_key = str(asset.get("shot_key") or "")
            source = material.get(shot_key) or {}
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
                "file": str(asset.get("file") or ""),
                "url": str(asset.get("url") or ""),
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
            "resolution": "720p",
            "duration_ms": int(
                (plan["plan"].get("duration") or {}).get("target_ms") or 0
            ),
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
    avatar_list=None,
):
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
        provider_job = _provider_job(conn.execute(
            "SELECT * FROM short_drama_provider_shot_jobs WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        ).fetchone())
        provider_versions = [
            _provider_version(row) for row in conn.execute(
                "SELECT * FROM short_drama_provider_shot_versions "
                "WHERE project_id=? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        ]
        capability = _production_capability()
        assembly = (
            _provider_assembly_snapshot(conn, project_id, plan["plan"])
            if plan else {
                "required_shot_keys": [], "ready_shot_keys": [],
                "required_count": 0, "ready_count": 0,
                "missing_shot_keys": [], "all_ready": False, "shots": [],
            }
        )
        if capability["mode"] == "provider_poc":
            capability["assembly"] = {
                key: value for key, value in assembly.items() if key != "shots"
            }
            capability["ready"] = bool(assembly["all_ready"])
            capability["message"] = (
                "全部镜头已生成，可合成 720p 预览"
                if assembly["all_ready"]
                else "请先完成全部镜头生成，再合成 720p 预览"
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
            "provider_versions": provider_versions,
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
        binding_blockers = _character_binding_blockers(
            conn, project_id, plan["plan"]
        )
        if binding_blockers:
            raise AutodraftError(
                "character_bindings_incomplete",
                "请先为所有出镜和说话角色绑定可用的电影化身",
                422,
            )
        capability = _production_capability()
        assembly = _provider_assembly_snapshot(conn, project_id, plan["plan"])
        provider_assembly = (
            capability["mode"] == "provider_poc" and assembly["all_ready"]
        )
        if not capability["ready"] and not provider_assembly:
            raise AutodraftError(
                "provider_shots_incomplete"
                if capability["mode"] == "provider_poc"
                else "autodraft_provider_unavailable",
                (
                    "请先完成全部镜头生成；缺少："
                    + "、".join(assembly["missing_shot_keys"])
                    if capability["mode"] == "provider_poc"
                    else capability["message"]
                ),
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
