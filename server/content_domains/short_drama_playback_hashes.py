"""Canonical identities for short-drama playback bundles and remux jobs."""

import hashlib
import json


CONTRACT_VERSION = "short_drama_playback_bundle_v1"
REMUX_ENGINE_VERSION = "short_drama_remux_v1"


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def canonical_hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def media_hash(
    *, composition_input_hash, master_audio_hash, ratio, profile,
    source_file_hash="", engine_version=REMUX_ENGINE_VERSION
):
    return canonical_hash({
        "composition_input_hash": str(composition_input_hash or ""),
        "master_audio_hash": str(master_audio_hash or ""),
        "ratio": str(ratio or ""),
        "profile": str(profile or ""),
        "source_file_hash": str(source_file_hash or ""),
        "engine_version": str(engine_version or ""),
    })


def subtitle_hash(
    *, alignment_version, transcript_hash, timeline_hash, cues,
    language="zh-CN"
):
    return canonical_hash({
        "alignment_version": str(alignment_version or ""),
        "transcript_hash": str(transcript_hash or ""),
        "timeline_hash": str(timeline_hash or ""),
        "cues": list(cues or []),
        "language": str(language or "zh-CN"),
    })


def bundle_hash(media_identity, subtitle_identity):
    return canonical_hash({
        "contract_version": CONTRACT_VERSION,
        "media_hash": str(media_identity or ""),
        "subtitle_hash": str(subtitle_identity or ""),
    })


def remux_hash(media_identity, source_file_hash):
    return canonical_hash({
        "engine_version": REMUX_ENGINE_VERSION,
        "media_hash": str(media_identity or ""),
        "source_file_hash": str(source_file_hash or ""),
    })
