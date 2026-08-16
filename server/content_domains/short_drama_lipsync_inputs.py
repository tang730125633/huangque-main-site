"""Build immutable local inputs for a real lipsync provider submission."""

import copy
import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from . import short_drama_assembly_plan
from .short_drama_lipsync_snapshot import canonical_hash, canonical_json


class LipsyncInputError(RuntimeError):
    pass


PROVIDER_CONTRACT_VERSION = "short-drama-lipsync-provider-input-v2"
_COPY_CHUNK_BYTES = 1024 * 1024


def file_fingerprint(path):
    """Hash one media file without buffering it in memory."""
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as source:
            while True:
                chunk = source.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise LipsyncInputError("locked lipsync media file is unavailable") from error
    if size <= 0:
        raise LipsyncInputError("locked lipsync media file is empty")
    return {"sha256": digest.hexdigest(), "size": size}


def _valid_fingerprint(value):
    return bool(
        isinstance(value, dict)
        and isinstance(value.get("sha256"), str)
        and len(value["sha256"]) == 64
        and type(value.get("size")) is int
        and value["size"] > 0
    )


def _media_fingerprint_hash(visual, segments):
    fingerprints = [{
        "kind": "video",
        "id": str(visual.get("visual_source_id") or ""),
        **visual["file_fingerprint"],
    }]
    fingerprints.extend({
        "kind": "voice",
        "id": str(segment.get("voice_asset_id") or ""),
        **segment["file_fingerprint"],
    } for segment in segments)
    return canonical_hash(fingerprints)


def _resolved_fingerprint(file_key, resolver):
    try:
        return file_fingerprint(resolver(file_key))
    except LipsyncInputError:
        raise
    except Exception as error:
        raise LipsyncInputError(
            "locked lipsync media file is unavailable"
        ) from error


def freeze_provider_contract(
        contract, *, resolver=short_drama_assembly_plan.resolve_controlled_file):
    """Attach the actual immutable bytes used by a paid provider quote."""
    frozen = copy.deepcopy(contract)
    visual = frozen.get("visual")
    segments = frozen.get("segments")
    if not isinstance(visual, dict) or not isinstance(segments, list) or not segments:
        raise LipsyncInputError("provider media contract is incomplete")
    visual["file_fingerprint"] = _resolved_fingerprint(
        visual.get("uri"), resolver
    )
    if str(visual.get("source_hash") or "") != visual["file_fingerprint"]["sha256"]:
        raise LipsyncInputError("locked lipsync video fingerprint changed")
    for segment in segments:
        if not isinstance(segment, dict):
            raise LipsyncInputError("provider speech contract is invalid")
        segment["file_fingerprint"] = _resolved_fingerprint(
            segment.get("audio_file"), resolver
        )
    frozen["contract_version"] = PROVIDER_CONTRACT_VERSION
    frozen["media_fingerprint_hash"] = _media_fingerprint_hash(
        visual, segments
    )
    return frozen


def _copy_verified(source_path, expected, destination):
    """Copy and verify the exact bytes that the provider will receive."""
    if not _valid_fingerprint(expected):
        raise LipsyncInputError("immutable media fingerprint is unavailable")
    destination = Path(destination)
    partial = destination.with_name(destination.name + ".partial")
    digest = hashlib.sha256()
    size = 0
    try:
        partial.unlink(missing_ok=True)
        with open(source_path, "rb") as source, open(partial, "xb") as output:
            while True:
                chunk = source.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                output.write(chunk)
        actual = {"sha256": digest.hexdigest(), "size": size}
        if actual != expected:
            raise LipsyncInputError("locked lipsync media fingerprint changed")
        os.replace(str(partial), str(destination))
        return destination
    except OSError as error:
        raise LipsyncInputError("locked lipsync media staging failed") from error
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass


def _object(value):
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_audio_command(inputs, duration_ms, destination, *, ffmpeg="ffmpeg"):
    """Return an argv-only FFmpeg graph; caller controls execution and timeout."""
    if not inputs:
        raise LipsyncInputError("lipsync target has no locked speech audio")
    command = [str(ffmpeg), "-y", "-v", "error"]
    filters = []
    labels = []
    for index, item in enumerate(inputs):
        command.extend(["-i", str(item["file"])])
        label = "speech%d" % index
        delay = max(0, int(item["start_ms"]))
        maximum = max(1, int(item["end_ms"]) - int(item["start_ms"])) / 1000.0
        filters.append(
            "[%d:a]atrim=duration=%.3f,asetpts=PTS-STARTPTS,adelay=%d|%d[%s]"
            % (index, maximum, delay, delay, label)
        )
        labels.append("[%s]" % label)
    filters.append(
        "%samix=inputs=%d:normalize=0:dropout_transition=0,"
        "atrim=duration=%.3f,apad=whole_dur=%.3f[aout]"
        % (
            "".join(labels), len(labels), duration_ms / 1000.0,
            duration_ms / 1000.0,
        )
    )
    command.extend([
        "-filter_complex", ";".join(filters), "-map", "[aout]",
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "-t", "%.3f" % (duration_ms / 1000.0), str(destination),
    ])
    return command


def _load_contract(conn, job_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT job.project_id,job.shot_id,job.input_hash,"
        "quote.provider_contract_json FROM short_drama_lipsync_jobs job "
        "JOIN short_drama_lipsync_attempts attempt ON attempt.id=job.attempt_id "
        "JOIN short_drama_lipsync_quotes quote ON quote.id=attempt.quote_id "
        "WHERE job.id=?",
        (job_id,),
    ).fetchone()
    if not row:
        raise LipsyncInputError("lipsync job does not exist")
    contract = _object(row["provider_contract_json"])
    if contract.get("contract_version") != PROVIDER_CONTRACT_VERSION:
        raise LipsyncInputError("immutable provider contract is unavailable")
    if not isinstance(contract.get("media_fingerprint_hash"), str):
        raise LipsyncInputError("immutable media fingerprints are unavailable")
    if (
        str(contract.get("project_id") or "") != str(row["project_id"])
        or str(contract.get("shot_id") or "") != str(row["shot_id"])
        or str(contract.get("input_hash") or "") != str(row["input_hash"])
    ):
        raise LipsyncInputError("immutable provider contract identity is invalid")
    timeline_id = str(contract.get("timeline_version_id") or "")
    timeline = conn.execute(
        "SELECT id,timeline_hash FROM short_drama_timeline_versions "
        "WHERE id=? AND project_id=?",
        (timeline_id, row["project_id"]),
    ).fetchone()
    if (
        not timeline
        or str(timeline["timeline_hash"] or "")
        != str(contract.get("timeline_hash") or "")
    ):
        raise LipsyncInputError("immutable timeline version is unavailable")
    visual = contract.get("visual")
    if (
        not isinstance(visual, dict)
        or not _valid_fingerprint(visual.get("file_fingerprint"))
    ):
        raise LipsyncInputError("immutable visual source is unavailable")
    source = conn.execute(
        "SELECT source.id,source.uri,source.source_hash,report.id AS report_id,"
        "report.duration_ms,report.report_hash FROM short_drama_lipsync_visual_sources source "
        "JOIN short_drama_lipsync_media_reports report ON report.source_id=source.id "
        "WHERE source.id=? AND source.project_id=? AND source.shot_id=? "
        "AND report.id=?",
        (
            str(visual.get("visual_source_id") or ""), row["project_id"],
            row["shot_id"], str(visual.get("media_report_id") or ""),
        ),
    ).fetchone()
    if (
        not source
        or str(source["uri"] or "") != str(visual.get("uri") or "")
        or str(source["source_hash"] or "") != str(visual.get("source_hash") or "")
        or str(source["report_hash"] or "")
        != str(visual.get("media_report_hash") or "")
    ):
        raise LipsyncInputError("immutable visual source has changed")
    target = contract.get("face_target")
    if not isinstance(target, dict):
        raise LipsyncInputError("immutable face target is invalid")
    duration_ms = int(contract.get("duration_ms") or 0)
    if duration_ms <= 0:
        raise LipsyncInputError("lipsync visual duration is invalid")
    if int(contract.get("visual_duration_ms") or 0) != int(source["duration_ms"] or 0):
        raise LipsyncInputError("immutable visual duration has changed")
    speech = []
    segments = contract.get("segments")
    if not isinstance(segments, list) or not segments:
        raise LipsyncInputError("immutable speech segments are unavailable")
    for expected in segments:
        if not isinstance(expected, dict):
            raise LipsyncInputError("immutable speech segment is invalid")
        segment = conn.execute(
            "SELECT id,line_id,voice_asset_id,start_ms,end_ms,face_target_json "
            "FROM short_drama_timeline_segments WHERE id=? AND version_id=? "
            "AND project_id=? AND shot_id=? AND speaking_mode='visible'",
            (
                str(expected.get("segment_id") or ""), timeline_id,
                row["project_id"], row["shot_id"],
            ),
        ).fetchone()
        if (
            not segment
            or str(segment["line_id"] or "") != str(expected.get("line_id") or "")
            or str(segment["voice_asset_id"] or "")
            != str(expected.get("voice_asset_id") or "")
            or int(segment["start_ms"]) != int(expected.get("timeline_start_ms") or 0)
            or int(segment["end_ms"]) != int(expected.get("timeline_end_ms") or 0)
            or canonical_json(_object(segment["face_target_json"]))
            != canonical_json(expected.get("face_target") or {})
        ):
            raise LipsyncInputError("immutable timeline segment has changed")
        version = conn.execute(
            "SELECT id,audio_file,duration_ms,input_hash,status "
            "FROM short_drama_voice_versions WHERE id=?",
            (str(expected.get("voice_asset_id") or ""),),
        ).fetchone()
        if (
            not version or version["status"] != "done" or not version["audio_file"]
            or str(version["audio_file"]) != str(expected.get("audio_file") or "")
            or str(version["input_hash"] or "")
            != str(expected.get("audio_input_hash") or "")
            or int(version["duration_ms"] or 0)
            != int(expected.get("audio_duration_ms") or 0)
            or not _valid_fingerprint(expected.get("file_fingerprint"))
        ):
            raise LipsyncInputError("locked voice asset is unavailable")
        speech.append({
            "segment_id": str(segment["id"]),
            "line_id": str(segment["line_id"] or ""),
            "voice_asset_id": str(version["id"]),
            "audio_file": str(version["audio_file"]),
            "audio_input_hash": str(version["input_hash"] or ""),
            "file_fingerprint": dict(expected["file_fingerprint"]),
            "start_ms": int(expected.get("start_ms") or 0),
            "end_ms": int(expected.get("end_ms") or 0),
        })
    if not speech:
        raise LipsyncInputError("selected face has no visible locked dialogue")
    if contract["media_fingerprint_hash"] != _media_fingerprint_hash(
            visual, segments):
        raise LipsyncInputError("immutable media fingerprint contract is invalid")
    return {
        "project_id": str(row["project_id"]),
        "shot_id": str(row["shot_id"]),
        "input_hash": str(row["input_hash"]),
        "video_file": str(source["uri"]),
        "video_hash": str(source["source_hash"]),
        "video_fingerprint": dict(visual["file_fingerprint"]),
        "media_fingerprint_hash": str(contract["media_fingerprint_hash"]),
        "duration_ms": duration_ms,
        "face_target": target,
        "speech": speech,
    }


def prepare_provider_request(
    db_factory, job_id, work_root, *, runner=subprocess.run,
    resolver=short_drama_assembly_plan.resolve_controlled_file,
):
    """Resolve controlled media and build a speech-only WAV for one face."""
    with closing(db_factory()) as conn:
        contract = _load_contract(conn, job_id)
    work_dir = Path(work_root) / str(job_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    source_video = Path(resolver(contract["video_file"]))
    video_suffix = source_video.suffix.lower()
    if video_suffix not in {".mp4", ".mov"}:
        video_suffix = ".bin"
    video_path = _copy_verified(
        source_video, contract["video_fingerprint"],
        work_dir / ("provider-video" + video_suffix),
    )
    audio_inputs = []
    for index, item in enumerate(contract["speech"]):
        source_audio = Path(resolver(item["audio_file"]))
        suffix = source_audio.suffix.lower()
        if not suffix or len(suffix) > 10:
            suffix = ".bin"
        path = _copy_verified(
            source_audio, item["file_fingerprint"],
            work_dir / ("provider-voice-%d%s" % (index, suffix)),
        )
        audio_inputs.append({**item, "file": path})
    destination = work_dir / "provider-input.wav"
    partial = work_dir / "provider-input.partial.wav"
    try:
        partial.unlink(missing_ok=True)
        command = build_audio_command(
            audio_inputs, contract["duration_ms"], partial,
            ffmpeg=os.environ.get("FFMPEG_BIN", "ffmpeg"),
        )
        result = runner(
            command, capture_output=True, text=True, timeout=180,
        )
        if result.returncode or not partial.is_file() or partial.stat().st_size <= 0:
            raise LipsyncInputError(
                "speech audio preparation failed: "
                + str(result.stderr or "")[-300:]
            )
        os.replace(str(partial), str(destination))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LipsyncInputError("speech audio preparation could not run") from error
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
    audio_fingerprint = file_fingerprint(destination)
    return {
        "video_path": str(video_path),
        "audio_path": str(destination),
        "face_target": contract["face_target"],
        "metadata": {
            "project_id": contract["project_id"],
            "shot_id": contract["shot_id"],
            "input_hash": contract["input_hash"],
            "video_hash": contract["video_hash"],
            "video_fingerprint": contract["video_fingerprint"],
            "audio_fingerprint": audio_fingerprint,
            "media_fingerprint_hash": contract["media_fingerprint_hash"],
            "duration_ms": contract["duration_ms"],
            "segments": [{
                key: item[key] for key in (
                    "segment_id", "line_id", "voice_asset_id",
                    "audio_input_hash", "file_fingerprint",
                    "start_ms", "end_ms",
                )
            } for item in contract["speech"]],
        },
    }
