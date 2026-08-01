"""Synchronize locked C-3 video versions into the lipsync dependency ledger."""

import hashlib
import json
import sqlite3
import time

from . import short_drama_assembly_plan as media_plan


PROBE_VERSION = "stable-ffprobe-v1"


def _canonical_hash(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_id(version_id):
    return "video-version:" + str(version_id)


def _source_format(file_key):
    suffix = str(file_key or "").rsplit(".", 1)
    return suffix[-1].lower() if len(suffix) == 2 else "unknown"


def _inspect_media(file_key):
    path = media_plan.resolve_controlled_file(file_key)
    return media_plan.stable_probe(path)


def _report(inspected, version):
    probe = inspected.get("probe") or {}
    video = probe.get("video") or {}
    fingerprint = inspected.get("fingerprint") or {}
    width, height = media_plan.dimensions_for_ratio(probe)
    fps = video.get("fps")
    if (
        not fingerprint.get("sha256")
        or not width
        or not height
        or not fps
        or not probe.get("duration_ms")
        or not video.get("codec")
    ):
        return None
    value = {
        "probe_version": PROBE_VERSION,
        "width": int(width),
        "height": int(height),
        "fps": float(fps),
        "duration_ms": int(probe["duration_ms"]),
        "codec": str(video["codec"]),
        "source_format": _source_format(version["file"]),
        "source_hash": str(fingerprint["sha256"]),
        "source_size": int(fingerprint.get("size") or 0),
        "video_version_id": str(version["id"]),
    }
    value["report_hash"] = _canonical_hash(value)
    return value


def sync_shot(conn, project_id, shot_id, *, source_inspector=None, now=None):
    """Make the current locked video the only current lipsync visual source.

    Media inspection is deliberately best-effort: a missing/unprobeable file
    keeps the lipsync snapshot blocked without breaking the existing C-3 lock.
    """
    conn.row_factory = sqlite3.Row
    now = int(time.time()) if now is None else int(now)
    slot = conn.execute(
        "SELECT * FROM short_drama_video_shots "
        "WHERE project_id=? AND shot_id=?",
        (project_id, shot_id),
    ).fetchone()
    if not slot or not bool(slot["locked"]) or not slot["current_version"]:
        conn.execute(
            "UPDATE short_drama_lipsync_visual_sources "
            "SET is_current=0,locked_at=NULL "
            "WHERE project_id=? AND shot_id=? AND is_current=1",
            (project_id, shot_id),
        )
        return False
    version = conn.execute(
        "SELECT version.* FROM short_drama_video_versions version "
        "WHERE version.video_shot_id=? AND version.version=? "
        "AND version.status='done'",
        (slot["id"], slot["current_version"]),
    ).fetchone()
    if not version or not str(version["file"] or ""):
        return False
    source_id = _source_id(version["id"])
    existing = conn.execute(
        "SELECT report.id FROM short_drama_lipsync_visual_sources source "
        "JOIN short_drama_lipsync_media_reports report ON report.source_id=source.id "
        "WHERE source.id=? AND source.project_id=? AND source.shot_id=? "
        "AND source.is_current=1 AND source.locked_at IS NOT NULL "
        "ORDER BY report.created_at DESC LIMIT 1",
        (source_id, project_id, shot_id),
    ).fetchone()
    if existing:
        return True
    inspector = source_inspector or _inspect_media
    try:
        inspected = inspector(version["file"])
        report = _report(inspected, version)
    except (media_plan.MediaPlanError, OSError, ValueError):
        return False
    if not report:
        return False
    conn.execute(
        "UPDATE short_drama_lipsync_visual_sources "
        "SET is_current=0,locked_at=NULL "
        "WHERE project_id=? AND shot_id=? AND is_current=1 AND id<>?",
        (project_id, shot_id, source_id),
    )
    conn.execute(
        "INSERT INTO short_drama_lipsync_visual_sources "
        "(id,project_id,shot_id,source_kind,uri,source_hash,is_current,"
        "locked_at,created_at) VALUES (?,?,?,?,?,?,1,?,?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "uri=excluded.uri,source_hash=excluded.source_hash,"
        "is_current=1,locked_at=excluded.locked_at",
        (
            source_id, project_id, shot_id, "video", str(version["file"]),
            report["source_hash"], now, now,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO short_drama_lipsync_media_reports "
        "(id,source_id,probe_version,width,height,fps,duration_ms,codec,"
        "source_format,report_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "media-report:" + report["report_hash"], source_id,
            report["probe_version"], report["width"], report["height"],
            report["fps"], report["duration_ms"], report["codec"],
            report["source_format"], report["report_hash"], now,
        ),
    )
    return True


def sync_project(conn, project_id, *, source_inspector=None, now=None):
    result = {}
    rows = conn.execute(
        "SELECT shot_id FROM short_drama_video_shots "
        "WHERE project_id=? ORDER BY shot_id",
        (project_id,),
    ).fetchall()
    for row in rows:
        shot_id = row["shot_id"] if hasattr(row, "keys") else row[0]
        result[str(shot_id)] = sync_shot(
            conn, project_id, shot_id,
            source_inspector=source_inspector, now=now,
        )
    return result
