"""Deterministic dependency snapshot for short-drama lipsync."""

import hashlib
import json
import os
import sqlite3
import time

from providers.lipsync import catalog_snapshot

from . import (
    short_drama_alignment,
    short_drama_lipsync_versions,
    short_drama_timeline,
)


CONTRACT_VERSION = "short-drama-lipsync-v1"
HASH_ALGORITHM_VERSION = "canonical-json-sha256-v1"


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def canonical_hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def blocker(code, scope="project", entity_id="", **details):
    messages = {
        "missing_master_timeline": "主时间轴尚未就绪",
        "timeline_stale": "主时间轴依赖已变化，请重新确认",
        "missing_locked_visual": "可见对白镜头缺少已锁定画面或媒体报告",
        "missing_locked_audio": "主音轨尚未锁定",
        "missing_face_target": "可见对白缺少人物脸部目标",
        "overlapping_visible_speech": "同一镜头存在无法唯一分配的可见对白重叠",
        "alignment_stale": "字幕对齐版本缺失、未锁定或已经失效",
        "alignment_review_incomplete": "字幕对齐人工审核记录不完整",
        "active_lipsync_job": "同一项目已有口型任务处理中",
        "provider_not_selected": "尚未选择可用的口型 Provider",
        "project_stage_readonly": "当前项目阶段或画布角色仅允许查看",
    }
    value = {
        "code": code,
        "scope": scope,
        "entity_id": str(entity_id or ""),
        "message": messages.get(code, code),
        "repair_action": {
            "missing_master_timeline": "open_master_timeline",
            "timeline_stale": "open_master_timeline",
            "missing_locked_visual": "open_video_stage",
            "missing_locked_audio": "open_voice_stage",
            "missing_face_target": "open_master_timeline",
            "overlapping_visible_speech": "open_master_timeline",
            "alignment_stale": "open_alignment",
            "alignment_review_incomplete": "open_alignment",
            "active_lipsync_job": "wait_for_job",
            "provider_not_selected": "select_provider",
            "project_stage_readonly": "request_edit_access",
        }.get(code, "refresh"),
    }
    value.update(details)
    return value


def _timeline(conn, project):
    source = short_drama_timeline._authoritative_source(conn, project)
    value = short_drama_timeline._current(conn, project["id"], source)
    if not value:
        return None, []
    segments = list(value.get("segments") or [])
    return value, [
        item for item in segments if item["speaking_mode"] == "visible"
    ]


def _alignment(conn, project):
    row = conn.execute(
        "SELECT version.* FROM short_drama_alignment_current current "
        "JOIN short_drama_alignment_versions version "
        "ON version.id=current.version_id WHERE current.project_id=?",
        (project["id"],),
    ).fetchone()
    if not row:
        return None
    value = dict(row)
    try:
        _, contract = short_drama_alignment._current_contract(conn, project)
    except (LookupError, ValueError):
        contract = None
    value["effective_status"] = (
        value["status"]
        if contract and value["input_hash"] == contract["input_hash"]
        else "stale"
    )
    value["authoritative_input_hash"] = (
        contract["input_hash"] if contract else None
    )
    value["review_audit_complete"] = (
        short_drama_alignment._review_audit_complete_in_db(conn, value)
    )
    return value


def _visuals(conn, project_id, shot_ids):
    result = {}
    for shot_id in sorted(set(shot_ids)):
        row = conn.execute(
            "SELECT source.*,report.id AS report_id,report.probe_version,"
            "report.width,report.height,report.fps,report.duration_ms,"
            "report.codec,report.source_format,report.report_hash "
            "FROM short_drama_lipsync_visual_sources source "
            "LEFT JOIN short_drama_lipsync_media_reports report "
            "ON report.source_id=source.id "
            "WHERE source.project_id=? AND source.shot_id=? "
            "AND source.is_current=1 AND source.locked_at IS NOT NULL "
            "ORDER BY source_kind='video' DESC,report.created_at DESC LIMIT 1",
            (project_id, shot_id),
        ).fetchone()
        if row:
            item = dict(row)
            item["locked"] = True
            result[shot_id] = item
    return result


def _voice_versions(conn, segments):
    ids = sorted({
        str(item.get("voice_asset_id") or "")
        for item in segments
        if str(item.get("voice_asset_id") or "")
    })
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    return {
        str(row["id"]): dict(row)
        for row in conn.execute(
            "SELECT id,audio_file,duration_ms,input_hash,status "
            "FROM short_drama_voice_versions WHERE id IN (%s)" % placeholders,
            ids,
        )
    }


def _shot_offsets(conn, project_id):
    result = {}
    offset_ms = 0
    for row in conn.execute(
        "SELECT id,duration FROM short_drama_shots WHERE project_id=? "
        "ORDER BY sort_order,id", (project_id,),
    ):
        result[str(row["id"])] = offset_ms
        offset_ms += max(0, int(row["duration"] or 0)) * 1000
    return result


def _active_jobs(conn, project_id):
    result = []
    for row in conn.execute(
            "SELECT id,attempt_id,provider,provider_job_id,state,progress,"
            "error_json,result_json,heartbeat_at,created_at,updated_at "
            "FROM short_drama_lipsync_jobs "
            "WHERE attempt_id IN ("
            "SELECT attempt.id FROM short_drama_lipsync_attempts attempt "
            "JOIN short_drama_lipsync_quotes quote ON quote.id=attempt.quote_id "
            "WHERE quote.project_id=?) "
            "AND state IN ('prepared','queued','running','cancel_pending') "
            "ORDER BY created_at",
            (project_id,),
        ):
        item = dict(row)
        item["error"] = _safe_object(item.pop("error_json"))
        item["result"] = _safe_object(item.pop("result_json"))
        item["allowed_actions"] = {
            "retry": item["state"] in {"prepared", "queued"},
            "cancel": item["state"] in {
                "prepared", "queued", "running", "cancel_pending"
            },
            "refresh": True,
        }
        result.append(item)
    return result


def _safe_object(value):
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _feature(name, default="0"):
    return str(os.environ.get(name, default) or default).strip() == "1"


def build_snapshot(conn, project, *, can_write=True):
    conn.row_factory = sqlite3.Row
    project_id = project["id"]
    timeline, visible = _timeline(conn, project)
    alignment = _alignment(conn, project)
    visuals = _visuals(conn, project_id, [item["shot_id"] for item in visible])
    voice_versions = _voice_versions(conn, visible)
    shot_offsets = _shot_offsets(conn, project_id)
    catalog = catalog_snapshot()
    effective_can_write = bool(can_write and project["stage"] == "video_review")
    blockers = []
    if not timeline:
        blockers.append(blocker("missing_master_timeline"))
    elif timeline.get("effective_status") != "ready":
        blockers.append(blocker(
            "timeline_stale",
            effective_status=timeline.get("effective_status"),
        ))
    if not alignment or alignment.get("effective_status") != "locked":
        blockers.append(blocker("alignment_stale"))
    elif not alignment.get("review_audit_complete"):
        blockers.append(blocker("alignment_review_incomplete"))
    if not alignment or not alignment.get("master_audio_hash"):
        blockers.append(blocker("missing_locked_audio"))
    if not catalog.get("default_provider"):
        blockers.append(blocker("provider_not_selected"))
    if not visible:
        blockers.append(blocker("missing_face_target"))
    if not effective_can_write:
        blockers.append(blocker("project_stage_readonly"))

    visible_by_shot = {}
    for segment in visible:
        visible_by_shot.setdefault(segment["shot_id"], []).append(segment)
        target = segment.get("face_target")
        if not isinstance(target, dict) or not str(target.get("value") or ""):
            blockers.append(blocker(
                "missing_face_target", "segment", segment["id"],
                shot_id=segment["shot_id"],
            ))
    for shot_id, segments in visible_by_shot.items():
        visual = visuals.get(shot_id)
        if not visual or not visual.get("report_id"):
            blockers.append(blocker(
                "missing_locked_visual", "shot", shot_id, shot_id=shot_id
            ))
        ordered = sorted(segments, key=lambda item: (item["start_ms"], item["id"]))
        for previous, current in zip(ordered, ordered[1:]):
            if (
                current["start_ms"] < previous["end_ms"]
                and canonical_json(current.get("face_target"))
                != canonical_json(previous.get("face_target"))
            ):
                blockers.append(blocker(
                    "overlapping_visible_speech", "shot", shot_id,
                    shot_id=shot_id,
                ))
                break

    active_jobs = _active_jobs(conn, project_id)
    if active_jobs:
        blockers.append(blocker("active_lipsync_job"))
    billing = [
        dict(row) for row in conn.execute(
            "SELECT attempt.id,attempt.state,attempt.cost,attempt.updated_at "
            "FROM short_drama_lipsync_attempts attempt "
            "JOIN short_drama_lipsync_quotes quote ON quote.id=attempt.quote_id "
            "WHERE quote.project_id=? AND attempt.state IN "
            "('accepted','charged','linked','refund_pending','manual_review') "
            "ORDER BY attempt.updated_at",
            (project_id,),
        )
    ]

    dependencies = {
        "timeline": {
            "version_id": timeline.get("id") if timeline else None,
            "timeline_version": timeline.get("version") if timeline else None,
            "timeline_revision": timeline.get("timeline_revision") if timeline else 0,
            "contract_version": (
                timeline.get("contract_version") if timeline else None
            ),
            "timeline_hash": timeline.get("timeline_hash") if timeline else None,
            "effective_status": (
                timeline.get("effective_status") if timeline else "missing"
            ),
            "source_hashes": (
                timeline.get("source_hashes") if timeline else {}
            ),
            "segments": [{
                "id": item["id"],
                "shot_id": item["shot_id"],
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "character_key": item["character_key"],
                "speaking_mode": item["speaking_mode"],
                "face_target": item.get("face_target"),
            } for item in (timeline.get("segments") if timeline else [])],
            "visible_segments": [{
                "id": item["id"],
                "shot_id": item["shot_id"],
                "line_id": item.get("line_id"),
                "voice_asset_id": item.get("voice_asset_id"),
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "character_key": item["character_key"],
                "face_target": item.get("face_target"),
                "voice": voice_versions.get(str(item.get("voice_asset_id") or "")),
            } for item in visible],
        },
        "visual_sources": [{
            "shot_id": shot_id,
            "shot_offset_ms": shot_offsets.get(str(shot_id), 0),
            "visual_source_id": item["id"],
            "uri": item["uri"],
            "source_kind": item["source_kind"],
            "source_hash": item["source_hash"],
            "media_report_id": item["report_id"],
            "media_report_hash": item["report_hash"],
            "media_spec": {
                "width": item["width"],
                "height": item["height"],
                "fps": item["fps"],
                "duration_ms": item["duration_ms"],
                "codec": item["codec"],
                "format": item["source_format"],
            },
        } for shot_id, item in sorted(visuals.items())],
        "audio": {
            "master_audio_hash": (
                alignment.get("master_audio_hash") if alignment else None
            ),
            "transcript_hash": (
                alignment.get("transcript_hash") if alignment else None
            ),
        },
        "alignment": {
            "version_id": alignment.get("id") if alignment else None,
            "version": alignment.get("version") if alignment else None,
            "status": alignment.get("status") if alignment else "missing",
            "effective_status": (
                alignment.get("effective_status") if alignment else "missing"
            ),
            "input_hash": alignment.get("input_hash") if alignment else None,
            "authoritative_input_hash": (
                alignment.get("authoritative_input_hash") if alignment else None
            ),
            "review_audit_complete": bool(
                alignment and alignment.get("review_audit_complete")
            ),
            "alignment_hash": (
                alignment.get("alignment_hash") if alignment else None
            ),
        },
        "speakers": [{
            "segment_id": item["id"],
            "character_key": item["character_key"],
            "speaking_mode": item["speaking_mode"],
            "face_target": item.get("face_target"),
        } for item in (timeline.get("segments") if timeline else [])],
        "provider_catalog": catalog,
    }
    identity = {
        "contract_version": CONTRACT_VERSION,
        "hash_algorithm_version": HASH_ALGORITHM_VERSION,
        "project_id": project_id,
        "project_revision": int(project["revision"]),
        "dependencies": dependencies,
    }
    input_hash = canonical_hash(identity)
    versions = short_drama_lipsync_versions.list_versions(
        conn, project_id, input_hash
    )
    current = short_drama_lipsync_versions.current_versions(
        conn, project_id, versions
    )
    current_by_shot = {item["shot_id"]: item for item in current}
    for item in versions:
        pointer = current_by_shot.get(item["shot_id"])
        item["selected"] = bool(
            pointer and pointer["version_id"] == item["id"]
        )
        item["locked"] = bool(
            item["selected"] and pointer.get("locked_at") is not None
        )
        if item["selected"]:
            item["pointer_revision"] = int(pointer["revision"])
            item["locked_at"] = pointer.get("locked_at")
            item["locked_by"] = pointer.get("locked_by")
    mutable = _feature("HQ_SHORT_DRAMA_LIPSYNC_MUTATIONS_ENABLED")
    can_lock = bool(
        mutable and effective_can_write and not blockers
        and any(item.get("selected") and not item["stale"] for item in versions)
    )
    return {
        "project_id": project_id,
        "revision": int(project["revision"]),
        "generated_at": int(time.time()),
        "contract_version": CONTRACT_VERSION,
        "hash_algorithm_version": HASH_ALGORITHM_VERSION,
        "input_hash": input_hash,
        "dependencies": dependencies,
        "blockers": blockers,
        "can_quote": not blockers and bool(visible),
        "active_jobs": active_jobs,
        "billing": {
            "unsettled": billing,
            "refund_pending": sum(
                1 for item in billing if item["state"] == "refund_pending"
            ),
            "manual_review": sum(
                1 for item in billing if item["state"] == "manual_review"
            ),
        },
        "versions": versions,
        "current_version": current,
        "stale": any(item["stale"] for item in versions),
        "permissions": {
            "read": True,
            "quote": effective_can_write,
            "can_edit": bool(mutable and effective_can_write),
            "can_create_job": bool(mutable and effective_can_write),
            "can_select": bool(mutable and effective_can_write),
            "can_lock": can_lock,
        },
        "readiness": {
            "ready": can_lock,
            "blockers": blockers,
            "next_action": (
                "lock_version" if can_lock else
                (blockers[0]["repair_action"] if blockers else "select_version")
            ),
        },
        "features": {
            "ui_enabled": _feature("HQ_SHORT_DRAMA_LIPSYNC_UI_ENABLED"),
            "mutations_enabled": mutable,
            "batch_enabled": _feature(
                "HQ_SHORT_DRAMA_LIPSYNC_BATCH_ENABLED"
            ),
        },
    }


def _merged_duration_ms(segments):
    ranges = sorted(
        (int(item["start_ms"]), int(item["end_ms"])) for item in segments
    )
    merged = []
    for start_ms, end_ms in ranges:
        if not merged or start_ms > merged[-1][1]:
            merged.append([start_ms, end_ms])
        else:
            merged[-1][1] = max(merged[-1][1], end_ms)
    return sum(end_ms - start_ms for start_ms, end_ms in merged)


def shot_contract(snapshot, shot_id, face_target):
    shot_id = str(shot_id or "")
    target_identity = canonical_json(face_target)
    segments = [
        item for item in snapshot["dependencies"]["timeline"]["visible_segments"]
        if (
            item["shot_id"] == shot_id
            and canonical_json(item.get("face_target")) == target_identity
        )
    ]
    visual = next(
        (
            item for item in snapshot["dependencies"]["visual_sources"]
            if item["shot_id"] == shot_id
        ),
        None,
    )
    if not segments or not visual:
        return None
    duration_ms = int(visual["media_spec"]["duration_ms"] or 0)
    if duration_ms <= 0 or any(not item.get("voice") for item in segments):
        return None
    offset_ms = int(visual.get("shot_offset_ms") or 0)
    immutable_segments = []
    for item in segments:
        start_ms = int(item["start_ms"])
        local_start = start_ms - offset_ms
        if not 0 <= local_start < duration_ms:
            local_start = start_ms
        if not 0 <= local_start < duration_ms:
            return None
        voice = item["voice"]
        local_end = min(
            duration_ms,
            local_start + min(
                max(1, int(voice.get("duration_ms") or 0)),
                max(1, int(item["end_ms"]) - int(item["start_ms"])),
            ),
        )
        immutable_segments.append({
            "segment_id": item["id"],
            "line_id": item.get("line_id"),
            "voice_asset_id": item.get("voice_asset_id"),
            "timeline_start_ms": int(item["start_ms"]),
            "timeline_end_ms": int(item["end_ms"]),
            "start_ms": local_start,
            "end_ms": local_end,
            "face_target": item.get("face_target"),
            "audio_file": voice.get("audio_file"),
            "audio_duration_ms": int(voice.get("duration_ms") or 0),
            "audio_input_hash": voice.get("input_hash"),
        })
    return {
        "contract_version": "short-drama-lipsync-provider-input-v1",
        "input_hash": snapshot["input_hash"],
        "project_id": snapshot["project_id"],
        "shot_id": shot_id,
        "timeline_version_id": snapshot["dependencies"]["timeline"]["version_id"],
        "timeline_hash": snapshot["dependencies"]["timeline"]["timeline_hash"],
        "segments": immutable_segments,
        "face_target": face_target,
        "duration_ms": _merged_duration_ms(segments),
        "visual_duration_ms": duration_ms,
        "visual": visual,
    }
