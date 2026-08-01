"""Canonical hashing helpers for the short-drama master timeline."""

import hashlib
import json


CONTRACT_VERSION = "short_drama_speaker_timeline_v1"


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def canonical_hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_segment(segment):
    face_target = segment.get("face_target")
    normalized_target = None
    if isinstance(face_target, dict):
        normalized_target = {
            "type": str(face_target.get("type") or "").strip(),
            "value": str(face_target.get("value") or "").strip(),
        }
    return {
        "id": str(segment.get("id") or "").strip(),
        "shot_id": str(segment.get("shot_id") or "").strip(),
        "line_id": str(segment.get("line_id") or "").strip(),
        "character_key": str(segment.get("character_key") or "").strip(),
        "voice_asset_id": str(segment.get("voice_asset_id") or "").strip(),
        "start_ms": int(segment.get("start_ms") or 0),
        "end_ms": int(segment.get("end_ms") or 0),
        "speaking_mode": str(segment.get("speaking_mode") or "").strip(),
        "face_target": normalized_target,
    }


def canonical_timeline(duration_ms, segments, subtitle_cues):
    normalized_segments = sorted(
        (normalize_segment(item) for item in segments),
        key=lambda item: (
            item["start_ms"], item["end_ms"], item["shot_id"],
            item["line_id"], item["id"],
        ),
    )
    normalized_cues = sorted(
        ({
            "shot_id": str(item.get("shot_id") or "").strip(),
            "line_id": str(item.get("line_id") or "").strip(),
            "text": str(item.get("text") or "").strip(),
            "start_ms": int(item.get("start_ms") or 0),
            "end_ms": int(item.get("end_ms") or 0),
        } for item in subtitle_cues),
        key=lambda item: (
            item["start_ms"], item["end_ms"], item["shot_id"], item["line_id"]
        ),
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "duration_ms": int(duration_ms),
        "segments": normalized_segments,
        "subtitle_cues": normalized_cues,
    }


def timeline_hash(duration_ms, segments, subtitle_cues):
    return canonical_hash(canonical_timeline(duration_ms, segments, subtitle_cues))


def downstream_input_hash(project_id, source_hashes, timeline_digest):
    return canonical_hash({
        "contract_version": CONTRACT_VERSION,
        "project_id": str(project_id),
        "source_hashes": dict(source_hashes),
        "timeline_hash": str(timeline_digest),
    })
