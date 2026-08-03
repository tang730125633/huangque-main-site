"""Worker for PR-D zero-cost lightweight MP4 remux and external WebVTT."""

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import short_drama_assembly_plan as media_plan


class RemuxError(ValueError):
    pass


def _output_root():
    server_dir = Path(__file__).resolve().parents[1]
    return Path(os.environ.get(
        "CONTENT_OUT", str(server_dir / "content_out")
    )).resolve()


def _timestamp(ms):
    value = max(0, int(ms))
    hours, remainder = divmod(value, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _webvtt(cues):
    lines = ["WEBVTT", ""]
    for index, cue in enumerate(cues or [], 1):
        start = cue.get("start_ms")
        end = cue.get("end_ms")
        if type(start) is not int or type(end) is not int or end <= start:
            continue
        text = str(cue.get("text") or "").replace("\r", " ").strip()
        if not text:
            continue
        lines.extend([
            str(index),
            f"{_timestamp(start)} --> {_timestamp(end)}",
            text,
            "",
        ])
    return "\n".join(lines)


def _run(command):
    try:
        result = subprocess.run(
            [str(item) for item in command],
            capture_output=True, text=True, timeout=300,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
        raise RemuxError("轻量重封装执行失败") from error
    if result.returncode != 0:
        raise RemuxError(
            str(result.stderr or "轻量重封装执行失败").strip()[-500:]
        )


def run_remux_job(payload):
    root = _output_root()
    source = media_plan.resolve_controlled_file(str(payload["source_file"]))
    project_id = str(payload["project_id"])
    bundle_hash = str(payload["bundle_hash"])
    target = root / "short_drama_playback" / project_id / bundle_hash
    temp = target.with_name(f".{target.name}.tmp")
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True, exist_ok=True)
    media = temp / "playback.mp4"
    subtitle = temp / "subtitles.vtt"
    manifest = temp / "manifest.json"
    try:
        ffmpeg = os.environ.get("FFMPEG_BIN", "ffmpeg")
        _run([
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", source, "-map", "0:v:0", "-map", "0:a:0",
            "-c", "copy", "-movflags", "+faststart", media,
        ])
        subtitle.write_text(
            _webvtt(payload.get("subtitle_cues")), encoding="utf-8"
        )
        result = {
            "contract_version": "short_drama_playback_bundle_v1",
            "project_id": project_id,
            "source_version_id": payload["source_version_id"],
            "timeline_version_id": payload["timeline_version_id"],
            "media_hash": payload["media_hash"],
            "subtitle_hash": payload["subtitle_hash"],
            "bundle_hash": bundle_hash,
            "media_file": (
                Path("short_drama_playback") / project_id /
                bundle_hash / "playback.mp4"
            ).as_posix(),
            "subtitle_file": (
                Path("short_drama_playback") / project_id /
                bundle_hash / "subtitles.vtt"
            ).as_posix(),
            "duration_ms": int(payload["duration_ms"]),
        }
        result["media_url"] = "/api/gen/file/" + result["media_file"]
        result["subtitle_url"] = "/api/gen/file/" + result["subtitle_file"]
        manifest.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.rename(target)
        return result
    except Exception:
        if temp.exists():
            shutil.rmtree(temp)
        raise


HANDLERS = {"short_drama_remux": run_remux_job}
