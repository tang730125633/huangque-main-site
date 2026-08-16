"""Versioned master timeline and speaker orchestration for short drama."""

import json
import sqlite3
import time
import uuid

from . import short_drama_alignment, short_drama_voice
from .short_drama_timeline_hashes import (
    CONTRACT_VERSION,
    canonical_hash,
    canonical_json,
    downstream_input_hash,
    timeline_hash,
)
from .short_drama_timeline_rules import (
    WRITABLE_STAGES,
    blocker,
    stale_impact,
    validate_timeline,
)
from .short_drama_timeline_schema import init_db


SPEAKER_HASH_VERSION = 2
SPEAKER_MIGRATION_BLOCKER = "timeline_speaker_identity_unverified"


class TimelineError(ValueError):
    def __init__(self, code, message, *, status=400, blockers=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.blockers = list(blockers or [])


class TimelineRevisionConflict(TimelineError):
    def __init__(self):
        super().__init__(
            "timeline_revision_conflict",
            "主时间轴已被其他页面更新，请刷新后重新应用本地修改",
            status=409,
        )


def _json(value, fallback):
    if isinstance(value, type(fallback)):
        return value
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _project(conn, username, project_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM short_drama_projects "
        "WHERE id=? AND username=? AND deleted=0",
        (project_id, username),
    ).fetchone()
    if not row:
        raise LookupError("short drama project does not exist")
    return row


def _video_identity(conn, project_id):
    rows = conn.execute(
        "SELECT shot.shot_id,shot.current_version,shot.locked,"
        "version.id,version.input_hash,version.visual_spec_hash,"
        "version.semantic_status "
        "FROM short_drama_video_shots shot "
        "LEFT JOIN short_drama_video_versions version "
        "ON version.video_shot_id=shot.id "
        "AND version.version=shot.current_version "
        "WHERE shot.project_id=? ORDER BY shot.shot_id",
        (project_id,),
    ).fetchall()
    return [
        {
            "shot_id": row[0],
            "version": row[1],
            "locked": bool(row[2]),
            "version_id": row[3],
            "input_hash": row[4],
            "visual_spec_hash": row[5],
            "semantic_status": row[6],
        }
        for row in rows
    ]


def _speaker_hashes(characters, cast, voice_shots):
    legacy_hash = canonical_hash({
        "characters": characters,
        "cast": cast,
    })
    current_hash = canonical_hash({
        "characters": characters,
        "cast": cast,
        "shot_characters": [{
            "shot_id": str(shot.get("id") or ""),
            "character_keys": sorted({
                str(item) for item in shot.get("character_keys") or []
                if item
            }),
        } for shot in voice_shots or []],
    })
    return legacy_hash, current_hash


def _legacy_source(conn, project):
    """Describe pre-PR-C projects without inventing ready dependencies."""
    characters = [
        {
            "character_key": row[0],
            "name": row[1],
            "reference_version": int(row[2] or 0),
            "reference_locked": bool(row[3]),
        }
        for row in conn.execute(
            "SELECT character_key,name,reference_version,reference_locked "
            "FROM short_drama_characters WHERE project_id=? "
            "ORDER BY sort_order,id",
            (project["id"],),
        )
    ]
    cast = [
        {"character_key": row[0], "avatar_id": int(row[1])}
        for row in conn.execute(
            "SELECT character_key,avatar_id FROM short_drama_video_cast "
            "WHERE project_id=? ORDER BY character_key",
            (project["id"],),
        )
    ]
    videos = _video_identity(conn, project["id"])
    legacy_speaker_hash, speaker_hash = _speaker_hashes(characters, cast, [])
    shot_bounds = []
    cursor = 0
    for row in conn.execute(
            "SELECT id,shot_key,duration FROM short_drama_shots "
            "WHERE project_id=? ORDER BY sort_order,id",
            (project["id"],)):
        end = cursor + int(row[2] or 0) * 1000
        shot_bounds.append({
            "shot_id": row[0], "shot_key": row[1],
            "start_ms": cursor, "end_ms": end,
        })
        cursor = end
    return {
        "project_id": project["id"],
        "project_revision": int(project["revision"]),
        "stage": project["stage"],
        "duration_ms": cursor,
        "characters": characters,
        "voice": {"shots": [], "handoff_blocked": True},
        "alignment": None,
        "alignment_ready": False,
        "voice_ready": False,
        "dependencies_ready": False,
        "source_hashes": {
            "speaker_hash_version": SPEAKER_HASH_VERSION,
            "transcript_hash": "",
            "master_audio_hash": "",
            "alignment_hash": "",
            "speaker_hash": speaker_hash,
            "visual_hash": canonical_hash(videos),
        },
        "legacy_speaker_hash": legacy_speaker_hash,
        "shot_bounds": shot_bounds,
    }


def _authoritative_source(conn, project):
    script = conn.execute(
        "SELECT 1 FROM short_drama_scripts WHERE project_id=? LIMIT 1",
        (project["id"],),
    ).fetchone()
    if not script:
        return _legacy_source(conn, project)
    short_drama_voice.ensure_voice_workspace(conn, project["id"])
    short_drama_voice.reconcile_voice_jobs(conn, project["id"])
    voice = short_drama_voice.build_voice_snapshot(conn, project)
    _, alignment_contract = short_drama_alignment._current_contract(conn, project)
    alignment_row = conn.execute(
        "SELECT version.* FROM short_drama_alignment_current current "
        "JOIN short_drama_alignment_versions version "
        "ON version.id=current.version_id "
        "WHERE current.project_id=?",
        (project["id"],),
    ).fetchone()
    alignment = (
        short_drama_alignment._row_version(alignment_row)
        if alignment_row else None
    )
    alignment_ready = bool(
        alignment
        and alignment["status"] == "locked"
        and short_drama_alignment.version_matches_contract(
            alignment, alignment_contract
        )
        and short_drama_alignment._review_audit_complete_in_db(conn, alignment)
    )
    voice_ready = not voice.get("handoff_blocked")
    characters = [
        {
            "character_key": row["character_key"],
            "name": row["name"],
            "reference_version": int(row["reference_version"] or 0),
            "reference_locked": bool(row["reference_locked"]),
        }
        for row in conn.execute(
            "SELECT character_key,name,reference_version,reference_locked "
            "FROM short_drama_characters WHERE project_id=? "
            "ORDER BY sort_order,id",
            (project["id"],),
        )
    ]
    cast = [
        {"character_key": row[0], "avatar_id": int(row[1])}
        for row in conn.execute(
            "SELECT character_key,avatar_id FROM short_drama_video_cast "
            "WHERE project_id=? ORDER BY character_key",
            (project["id"],),
        )
    ]
    videos = _video_identity(conn, project["id"])
    legacy_speaker_hash, speaker_hash = _speaker_hashes(
        characters, cast, voice.get("shots") or []
    )
    source_hashes = {
        "speaker_hash_version": SPEAKER_HASH_VERSION,
        "transcript_hash": alignment_contract["transcript_hash"],
        "master_audio_hash": alignment_contract["master_audio_hash"],
        "alignment_hash": alignment["alignment_hash"] if alignment_ready else "",
        "speaker_hash": speaker_hash,
        "visual_hash": canonical_hash(videos),
    }
    shot_bounds = []
    cursor = 0
    for shot in sorted(
            voice.get("shots") or [],
            key=lambda item: (item.get("sort_order", 0), item.get("id", ""))):
        end = cursor + int(shot.get("duration") or 0) * 1000
        shot_bounds.append({
            "shot_id": shot["id"],
            "shot_key": shot.get("shot_key"),
            "start_ms": cursor,
            "end_ms": end,
        })
        cursor = end
    return {
        "project_id": project["id"],
        "project_revision": int(project["revision"]),
        "stage": project["stage"],
        "duration_ms": cursor,
        "characters": characters,
        "voice": voice,
        "alignment": alignment if alignment_ready else None,
        "alignment_ready": alignment_ready,
        "voice_ready": voice_ready,
        "dependencies_ready": voice_ready and alignment_ready,
        "source_hashes": source_hashes,
        "legacy_speaker_hash": legacy_speaker_hash,
        "shot_bounds": shot_bounds,
    }


def _segment_id(shot_id, dialogue_line_id):
    digest = canonical_hash({
        "shot_id": str(shot_id),
        "line_id": str(dialogue_line_id),
    })
    return "segment-" + digest[:24]


def _suggested_timeline(source):
    voice_lines = {}
    visible_characters = {}
    for shot in source["voice"].get("shots") or []:
        visible_characters[str(shot.get("id") or "")] = {
            str(item) for item in shot.get("character_keys") or [] if item
        }
        for line in shot.get("lines") or []:
            voice_lines[line["id"]] = line
    segments = []
    cues = []
    alignment = source.get("alignment") or {}
    for item in alignment.get("timeline") or []:
        line = voice_lines.get(item.get("line_id"))
        if not line:
            continue
        dialogue_line_id = str(line.get("dialogue_line_id") or "").strip()
        if not dialogue_line_id:
            continue
        version = next(
            (
                candidate for candidate in line.get("versions") or []
                if candidate.get("version") == line.get("current_version")
            ),
            None,
        )
        character_key = str(line.get("character_key") or "")
        mode = (
            "narration"
            if line.get("line_type") == "narration"
            or line.get("character_key") == "narrator"
            else "visible"
            if character_key in visible_characters.get(
                str(item.get("shot_id") or ""), set()
            )
            else "offscreen"
        )
        segments.append({
            "id": _segment_id(item["shot_id"], dialogue_line_id),
            "shot_id": item["shot_id"],
            "line_id": dialogue_line_id,
            "character_key": character_key,
            "voice_asset_id": str((version or {}).get("id") or ""),
            "start_ms": int(item["audio_start_ms"]),
            "end_ms": int(item["audio_end_ms"]),
            "speaking_mode": mode,
            "face_target": (
                {"type": "character", "value": character_key}
                if mode == "visible" and character_key else None
            ),
        })
        cues.append({
            "shot_id": item["shot_id"],
            "line_id": dialogue_line_id,
            "text": str(item.get("text") or ""),
            "start_ms": int(item["subtitle_start_ms"]),
            "end_ms": int(item["subtitle_end_ms"]),
        })
    return segments, cues


def _row_version(conn, row, source=None):
    result = dict(row)
    result["source_hashes"] = _json(result.pop("source_hashes_json"), {})
    result["subtitle_cues"] = _json(result.pop("subtitle_cues_json"), [])
    result["blockers"] = _json(result.pop("blockers_json"), [])
    result["segments"] = []
    for segment in conn.execute(
            "SELECT * FROM short_drama_timeline_segments "
            "WHERE version_id=? ORDER BY sort_order,start_ms,id",
            (result["id"],)):
        item = dict(segment)
        item["face_target"] = (
            _json(item.pop("face_target_json"), {})
            if item.get("face_target_json") else None
        )
        item.pop("version_id", None)
        item.pop("project_id", None)
        result["segments"].append(item)
    result["effective_status"] = result["status"]
    result["stale_impact"] = {"changed_sources": [], "downstream": []}
    if source and result["source_hashes"] != source["source_hashes"]:
        result["effective_status"] = "stale"
        result["stale_impact"] = stale_impact(
            result["source_hashes"], source["source_hashes"]
        )
    return result


def _current(conn, project_id, source=None):
    row = conn.execute(
        "SELECT version.*,current.revision AS timeline_revision "
        "FROM short_drama_timeline_current current "
        "JOIN short_drama_timeline_versions version "
        "ON version.id=current.version_id WHERE current.project_id=?",
        (project_id,),
    ).fetchone()
    return _row_version(conn, row, source) if row else None


def _legacy_speaker_hash_matches(stored_hashes, source):
    current_hashes = source["source_hashes"]
    if "speaker_hash_version" in stored_hashes:
        return False
    if set(stored_hashes) != set(current_hashes) - {"speaker_hash_version"}:
        return False
    if stored_hashes.get("speaker_hash") != source.get("legacy_speaker_hash"):
        return False
    return all(
        stored_hashes.get(key) == value
        for key, value in current_hashes.items()
        if key not in {"speaker_hash_version", "speaker_hash"}
    )


def _current_uses_legacy_speaker_hash(conn, project_id):
    row = conn.execute(
        "SELECT version.source_hashes_json "
        "FROM short_drama_timeline_current current "
        "JOIN short_drama_timeline_versions version "
        "ON version.id=current.version_id WHERE current.project_id=?",
        (project_id,),
    ).fetchone()
    return bool(
        row and "speaker_hash_version" not in _json(row[0], {})
    )


def _reconcile_legacy_speaker_segments(segments, source):
    visible_by_shot = {
        str(shot.get("id") or ""): {
            str(key) for key in shot.get("character_keys") or [] if key
        }
        for shot in source["voice"].get("shots") or []
    }
    reconciled = []
    adjustments = []
    for segment in segments:
        item = dict(segment)
        if item.get("speaking_mode") == "visible":
            shot_id = str(item.get("shot_id") or "")
            character_key = str(item.get("character_key") or "")
            if character_key not in visible_by_shot.get(shot_id, set()):
                item["speaking_mode"] = "offscreen"
                item["face_target"] = None
                adjustments.append({
                    "segment_id": item["id"],
                    "reason": "character_not_visible_in_current_shot",
                })
            else:
                expected = {"type": "character", "value": character_key}
                if item.get("face_target") != expected:
                    item["face_target"] = expected
                    adjustments.append({
                        "segment_id": item["id"],
                        "reason": "face_target_reconciled",
                    })
        reconciled.append(item)
    return reconciled, adjustments


def _speaker_migration_pending(current):
    return bool(
        current
        and current["source_hashes"].get("speaker_hash_version")
        == SPEAKER_HASH_VERSION
        and any(
            item.get("code") == SPEAKER_MIGRATION_BLOCKER
            for item in current.get("blockers") or []
        )
    )


def _migrate_legacy_speaker_hash(conn, project, source):
    """Create a reviewable v2 successor without claiming unknown history."""
    current = _current(conn, project["id"])
    if not current or not _legacy_speaker_hash_matches(
            current["source_hashes"], source):
        return _current(conn, project["id"], source)
    migration_source = dict(source)
    migration_source["duration_ms"] = current["duration_ms"]
    segments, adjustments = _reconcile_legacy_speaker_segments(
        current["segments"], source
    )
    migration_blockers = [blocker(SPEAKER_MIGRATION_BLOCKER)]
    _insert_version(
        conn, project, "system", "blocked", migration_source,
        segments, current["subtitle_cues"], migration_blockers,
        parent_id=current["id"], action="speaker_hash_v2_migration_pending",
        bump_project_revision=False,
        audit_details={
            "from_speaker_hash_version": 1,
            "to_speaker_hash_version": SPEAKER_HASH_VERSION,
            "reason": "shot_character_identity_added",
            "before_speaker_hash": current["source_hashes"]["speaker_hash"],
            "after_speaker_hash": source["source_hashes"]["speaker_hash"],
            "segment_adjustments": adjustments,
        },
    )
    return _current(conn, project["id"], source)


def require_ready_if_started_in_transaction(conn, project):
    """Gate voice handoff once a durable master timeline exists.

    Projects without a current pointer predate PR-C and keep the legacy handoff
    path. Once a timeline exists, its effective status is recalculated from the
    authoritative sources inside the caller's write transaction so a stale or
    unconfirmed version cannot be bypassed with a direct stage-confirm request.
    """
    started = conn.execute(
        "SELECT 1 FROM short_drama_timeline_current WHERE project_id=?",
        (project["id"],),
    ).fetchone()
    if not started:
        return None
    source = _authoritative_source(conn, project)
    current = _migrate_legacy_speaker_hash(conn, project, source)
    if current and current["effective_status"] == "ready":
        return current
    effective_status = (
        current["effective_status"] if current else "missing"
    )
    items = [blocker(
        "timeline_handoff_not_ready",
        effective_status=effective_status,
    )]
    if current:
        items.extend(current.get("blockers") or [])
    raise TimelineError(
        "timeline_handoff_not_ready",
        "主时间轴尚未确认或已经失效，请先完成主时间轴确认",
        status=409,
        blockers=items,
    )


def _snapshot(conn, project, *, history_limit=20):
    source = _authoritative_source(conn, project)
    current = _migrate_legacy_speaker_hash(conn, project, source)
    versions = [
        _row_version(conn, row, source) for row in conn.execute(
            "SELECT * FROM short_drama_timeline_versions "
            "WHERE project_id=? ORDER BY version DESC LIMIT ?",
            (project["id"], int(history_limit)),
        )
    ]
    current_revision = current["timeline_revision"] if current else 0
    source_blockers = []
    if not source["voice_ready"]:
        source_blockers.append(blocker("timeline_voice_not_locked"))
    if not source["alignment_ready"]:
        source_blockers.append(blocker("timeline_alignment_stale"))
    if not source["voice"].get("shots"):
        source_blockers.append(blocker("timeline_legacy_incomplete"))
    if project["stage"] not in WRITABLE_STAGES:
        source_blockers.append(blocker("timeline_stage_readonly"))
    current_blockers = list(current.get("blockers") or []) if current else []
    if current and current["effective_status"] == "stale":
        current_blockers.append(blocker("timeline_alignment_stale"))
    writable = project["stage"] in WRITABLE_STAGES
    migration_pending = _speaker_migration_pending(current)
    return {
        "contract_version": CONTRACT_VERSION,
        "project_id": project["id"],
        "project_revision": int(project["revision"]),
        "timeline_revision": int(current_revision),
        "stage": project["stage"],
        "duration_ms": source["duration_ms"],
        "shot_bounds": source["shot_bounds"],
        "characters": source["characters"],
        "source_hashes": source["source_hashes"],
        "status": current["effective_status"] if current else "legacy",
        "current_version": current,
        "versions": versions,
        "blockers": source_blockers + current_blockers,
        "capabilities": {
            "rebuild": writable and source["dependencies_ready"],
            "save": bool(
                writable and current
                and current["effective_status"] in {"draft", "blocked"}
            ),
            "confirm": bool(
                current
                and current["effective_status"] in {"draft", "blocked"}
                and (
                    migration_pending
                    or (writable and not current.get("blockers"))
                )
                and source["dependencies_ready"]
            ),
            "confirm_speaker_migration": bool(
                migration_pending
                and current["effective_status"] == "blocked"
                and source["dependencies_ready"]
            ),
            "view_history": True,
        },
    }


def get_snapshot(db_factory, username, project_id):
    conn = db_factory()
    migration_transaction = False
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        project = _project(conn, username, project_id)
        migration_transaction = _current_uses_legacy_speaker_hash(
            conn, project_id
        )
        if migration_transaction:
            conn.execute("BEGIN IMMEDIATE")
            project = _project(conn, username, project_id)
        result = _snapshot(conn, project)
        if migration_transaction:
            conn.commit()
        return result
    except Exception:
        if migration_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def get_versions(db_factory, username, project_id):
    snapshot = get_snapshot(db_factory, username, project_id)
    return {
        "project_id": snapshot["project_id"],
        "project_revision": snapshot["project_revision"],
        "timeline_revision": snapshot["timeline_revision"],
        "versions": snapshot["versions"],
    }


def _request_identity(payload, expected):
    if not isinstance(payload, dict) or set(payload) != expected:
        raise TimelineError("invalid_request", "主时间轴请求字段不正确")
    project_id = str(payload.get("project_id") or "").strip()
    if (
        not project_id or type(payload.get("revision")) is not int
        or type(payload.get("timeline_revision")) is not int
        or payload["timeline_revision"] < 0
    ):
        raise TimelineError("invalid_request", "主时间轴版本参数无效")
    return project_id


def _idempotency_replay(conn, actor, key, action, project_id, request_hash):
    if not key:
        raise TimelineError("invalid_request", "缺少 Idempotency-Key")
    row = conn.execute(
        "SELECT project_id,action,request_hash,response_json "
        "FROM short_drama_timeline_requests "
        "WHERE actor=? AND idempotency_key=?",
        (actor, key),
    ).fetchone()
    if not row:
        return None
    if row[0] != project_id or row[1] != action or row[2] != request_hash:
        raise TimelineError(
            "idempotency_conflict",
            "同一个 Idempotency-Key 不能用于不同的主时间轴请求",
            status=409,
        )
    return _json(row[3], {})


def _assert_write(
        conn, project, revision, timeline_revision, *, allow_readonly=False
):
    if project["stage"] not in WRITABLE_STAGES and not allow_readonly:
        raise TimelineError(
            "timeline_stage_readonly", "当前短剧阶段只能查看主时间轴",
            status=409,
        )
    if int(project["revision"]) != revision:
        raise TimelineRevisionConflict()
    row = conn.execute(
        "SELECT revision FROM short_drama_timeline_current WHERE project_id=?",
        (project["id"],),
    ).fetchone()
    actual = int(row[0]) if row else 0
    if actual != timeline_revision:
        raise TimelineRevisionConflict()


def _insert_version(
        conn, project, actor, status, source, segments, subtitle_cues,
        blockers, *, parent_id=None, action, bump_project_revision=True,
        audit_details=None):
    version_number = int(conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 "
        "FROM short_drama_timeline_versions WHERE project_id=?",
        (project["id"],),
    ).fetchone()[0])
    version_id = str(uuid.uuid4())
    digest = timeline_hash(source["duration_ms"], segments, subtitle_cues)
    input_digest = downstream_input_hash(
        project["id"], source["source_hashes"], digest
    )
    now = int(time.time())
    conn.execute(
        "INSERT INTO short_drama_timeline_versions "
        "(id,project_id,version,parent_id,status,revision,contract_version,"
        "duration_ms,source_hashes_json,timeline_hash,input_hash,"
        "subtitle_cues_json,blockers_json,created_by,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            version_id, project["id"], version_number, parent_id, status, 1,
            CONTRACT_VERSION, source["duration_ms"],
            canonical_json(source["source_hashes"]), digest, input_digest,
            canonical_json(subtitle_cues), canonical_json(blockers), actor, now,
        ),
    )
    for order, segment in enumerate(sorted(
            segments,
            key=lambda item: (
                item["start_ms"], item["end_ms"], item["shot_id"], item["id"]
            ))):
        target = segment.get("face_target")
        conn.execute(
            "INSERT INTO short_drama_timeline_segments "
            "(id,version_id,project_id,shot_id,line_id,character_key,"
            "voice_asset_id,start_ms,end_ms,speaking_mode,face_target_json,"
            "sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                segment["id"], version_id, project["id"], segment["shot_id"],
                segment["line_id"], segment["character_key"],
                segment["voice_asset_id"], segment["start_ms"],
                segment["end_ms"], segment["speaking_mode"],
                canonical_json(target) if target else None, order,
            ),
        )
    pointer = conn.execute(
        "SELECT version_id,revision FROM short_drama_timeline_current "
        "WHERE project_id=?",
        (project["id"],),
    ).fetchone()
    before_hash = ""
    if pointer:
        before = conn.execute(
            "SELECT timeline_hash FROM short_drama_timeline_versions WHERE id=?",
            (pointer[0],),
        ).fetchone()
        before_hash = before[0] if before else ""
        conn.execute(
            "UPDATE short_drama_timeline_current "
            "SET version_id=?,revision=revision+1,updated_at=? WHERE project_id=?",
            (version_id, now, project["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO short_drama_timeline_current "
            "(project_id,version_id,revision,updated_at) VALUES (?,?,1,?)",
            (project["id"], version_id, now),
        )
    conn.execute(
        "INSERT INTO short_drama_timeline_audit "
        "(id,project_id,version_id,actor,action,before_hash,after_hash,"
        "details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()), project["id"], version_id, actor, action,
            before_hash, digest, canonical_json({
                "status": status, "blocker_codes": [
                    item.get("code") for item in blockers
                ],
                **(audit_details or {}),
            }), now,
        ),
    )
    if bump_project_revision:
        updated = conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1,updated_at=? "
            "WHERE id=? AND revision=? AND deleted=0",
            (now, project["id"], int(project["revision"])),
        )
        if updated.rowcount != 1:
            raise TimelineRevisionConflict()
    return version_id


def _store_request(conn, actor, key, project_id, action, request_hash, response):
    conn.execute(
        "INSERT INTO short_drama_timeline_requests "
        "(actor,idempotency_key,project_id,action,request_hash,response_json,"
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (
            actor, key, project_id, action, request_hash,
            canonical_json(response), int(time.time()),
        ),
    )


def _mutate(
        db_factory, username, actor, payload, key, action, callback,
        *, allow_readonly=False
):
    request_hash = canonical_hash(payload)
    project_id = str(payload.get("project_id") or "").strip()
    conn = db_factory()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        replay = _idempotency_replay(
            conn, actor, key, action, project_id, request_hash
        )
        if replay is not None:
            conn.commit()
            replay["replayed"] = True
            return replay
        project = _project(conn, username, project_id)
        _assert_write(
            conn, project, payload["revision"], payload["timeline_revision"],
            allow_readonly=allow_readonly,
        )
        callback(conn, project)
        refreshed = _project(conn, username, project_id)
        result = _snapshot(conn, refreshed)
        result["replayed"] = False
        _store_request(
            conn, actor, key, project_id, action, request_hash, result
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rebuild(db_factory, username, actor, payload, idempotency_key):
    _request_identity(
        payload, {"project_id", "revision", "timeline_revision"}
    )

    def operation(conn, project):
        source = _authoritative_source(conn, project)
        if not source["dependencies_ready"]:
            items = []
            if not source["voice_ready"]:
                items.append(blocker("timeline_voice_not_locked"))
            if not source["alignment_ready"]:
                items.append(blocker("timeline_alignment_stale"))
            raise TimelineError(
                "timeline_dependencies_not_ready",
                "请先锁定全部配音和字幕对齐版本",
                status=422, blockers=items,
            )
        segments, cues = _suggested_timeline(source)
        blockers = validate_timeline(
            source["duration_ms"], source["shot_bounds"],
            [item["character_key"] for item in source["characters"]],
            segments, cues,
        )
        _insert_version(
            conn, project, actor, "blocked" if blockers else "draft",
            source, segments, cues, blockers, action="rebuild",
        )

    return _mutate(
        db_factory, username, actor, payload, idempotency_key,
        "rebuild", operation,
    )


def _normalize_change(item):
    if not isinstance(item, dict) or set(item) != {
        "id", "start_ms", "end_ms", "character_key",
        "speaking_mode", "face_target",
    }:
        raise TimelineError("invalid_request", "主时间轴区间变更字段不正确")
    target = item.get("face_target")
    if target is not None and (
        not isinstance(target, dict) or set(target) != {"type", "value"}
    ):
        raise TimelineError("invalid_request", "可见角色绑定格式不正确")
    return {
        "id": str(item.get("id") or "").strip(),
        "start_ms": item.get("start_ms"),
        "end_ms": item.get("end_ms"),
        "character_key": str(item.get("character_key") or "").strip(),
        "speaking_mode": str(item.get("speaking_mode") or "").strip(),
        "face_target": (
            {
                "type": str(target.get("type") or "").strip(),
                "value": str(target.get("value") or "").strip(),
            }
            if target is not None else None
        ),
    }


def save_changes(db_factory, username, actor, payload, idempotency_key):
    _request_identity(
        payload, {"project_id", "revision", "timeline_revision", "changes"}
    )
    if not isinstance(payload.get("changes"), list) or not payload["changes"]:
        raise TimelineError("invalid_request", "至少提交一个主时间轴区间变更")
    changes = [_normalize_change(item) for item in payload["changes"]]
    if len({item["id"] for item in changes}) != len(changes):
        raise TimelineError("invalid_request", "主时间轴区间变更不能重复")

    def operation(conn, project):
        source = _authoritative_source(conn, project)
        current = _current(conn, project["id"], source)
        if not current or current["effective_status"] not in {"draft", "blocked"}:
            raise TimelineError(
                "timeline_version_not_editable",
                "当前主时间轴版本不能编辑，请先重建草稿",
                status=409,
            )
        segment_map = {item["id"]: dict(item) for item in current["segments"]}
        if any(item["id"] not in segment_map for item in changes):
            raise TimelineError("invalid_request", "主时间轴区间不存在")
        for change in changes:
            segment_map[change["id"]].update(change)
        segments = list(segment_map.values())
        blockers = validate_timeline(
            source["duration_ms"], source["shot_bounds"],
            [item["character_key"] for item in source["characters"]],
            segments, current["subtitle_cues"],
            dependencies_ready=source["dependencies_ready"],
        )
        _insert_version(
            conn, project, actor, "blocked" if blockers else "draft",
            source, segments, current["subtitle_cues"], blockers,
            parent_id=current["id"], action="save",
        )

    return _mutate(
        db_factory, username, actor, payload, idempotency_key, "save", operation
    )


def save_lipsync_changes(
        db_factory, username, actor, payload, idempotency_key):
    """Apply PR-G speaker/face-target changes while preserving ready timeline."""
    _request_identity(
        payload, {"project_id", "revision", "timeline_revision", "changes"}
    )
    if not isinstance(payload.get("changes"), list) or not payload["changes"]:
        raise TimelineError(
            "invalid_request", "至少提交一个说话人或人脸目标变更"
        )
    changes = [_normalize_change(item) for item in payload["changes"]]
    if len({item["id"] for item in changes}) != len(changes):
        raise TimelineError("invalid_request", "说话人变更不能重复")

    def operation(conn, project):
        source = _authoritative_source(conn, project)
        current = _current(conn, project["id"], source)
        if not current or current["effective_status"] not in {
            "ready", "draft", "blocked"
        }:
            raise TimelineError(
                "timeline_version_not_editable",
                "当前主时间轴不能编辑，请刷新后重试",
                status=409,
            )
        segment_map = {item["id"]: dict(item) for item in current["segments"]}
        if any(item["id"] not in segment_map for item in changes):
            raise TimelineError("invalid_request", "说话人区间不存在")
        for change in changes:
            segment_map[change["id"]].update(change)
        segments = list(segment_map.values())
        blockers = validate_timeline(
            source["duration_ms"], source["shot_bounds"],
            [item["character_key"] for item in source["characters"]],
            segments, current["subtitle_cues"],
            dependencies_ready=source["dependencies_ready"],
        )
        rejected_blockers = blockers
        if current["effective_status"] == "ready":
            baseline_blockers = validate_timeline(
                source["duration_ms"], source["shot_bounds"],
                [item["character_key"] for item in source["characters"]],
                current["segments"], current["subtitle_cues"],
                dependencies_ready=source["dependencies_ready"],
            )
            baseline_keys = {
                canonical_hash(item) for item in baseline_blockers
            }
            rejected_blockers = [
                item for item in blockers
                if canonical_hash(item) not in baseline_keys
            ]
        if rejected_blockers:
            raise TimelineError(
                "timeline_blocked",
                "说话人变更仍有时间轴冲突",
                status=422,
                blockers=rejected_blockers,
            )
        _insert_version(
            conn, project, actor, "ready", source, segments,
            current["subtitle_cues"], [], parent_id=current["id"],
            action="lipsync_speakers",
        )

    return _mutate(
        db_factory, username, actor, payload, idempotency_key,
        "lipsync_speakers", operation,
    )


def confirm(db_factory, username, actor, payload, idempotency_key):
    _request_identity(
        payload, {"project_id", "revision", "timeline_revision"}
    )

    def operation(conn, project):
        source = _authoritative_source(conn, project)
        current = _current(conn, project["id"], source)
        migration_pending = _speaker_migration_pending(current)
        if (
            project["stage"] not in WRITABLE_STAGES
            and not migration_pending
        ):
            raise TimelineError(
                "timeline_stage_readonly",
                "当前短剧阶段只能确认待处理的角色映射迁移",
                status=409,
            )
        if not current or current["effective_status"] not in {"draft", "blocked"}:
            raise TimelineError(
                "timeline_version_not_confirmable",
                "当前主时间轴版本不能确认",
                status=409,
            )
        blockers = validate_timeline(
            source["duration_ms"], source["shot_bounds"],
            [item["character_key"] for item in source["characters"]],
            current["segments"], current["subtitle_cues"],
            dependencies_ready=source["dependencies_ready"],
        )
        if blockers:
            raise TimelineError(
                "timeline_blocked", "主时间轴仍有阻断问题",
                status=422, blockers=blockers,
            )
        _insert_version(
            conn, project, actor, "ready", source, current["segments"],
            current["subtitle_cues"], [], parent_id=current["id"],
            action=(
                "speaker_hash_v2_migration_confirm"
                if migration_pending else "confirm"
            ),
        )

    return _mutate(
        db_factory, username, actor, payload, idempotency_key,
        "confirm", operation, allow_readonly=True,
    )
