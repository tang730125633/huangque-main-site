"""Normalized, shell-free FFprobe inspection for PoC inputs and outputs."""

import json
import os
import subprocess
from pathlib import Path


class MediaProbeError(ValueError):
    pass


def build_ffprobe_command(path):
    return [
        os.environ.get("FFPROBE_BIN", "ffprobe"),
        "-v", "error",
        "-show_streams",
        "-show_format",
        "-of", "json",
        str(path),
    ]


def _integer_rate(value):
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        numerator, denominator = value.split("/", 1)
        return round(float(numerator) / float(denominator), 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def probe_media(path, runner=subprocess.run, timeout=30):
    path = Path(path)
    if not path.is_file():
        raise MediaProbeError("media file does not exist")
    try:
        completed = runner(
            build_ffprobe_command(path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise MediaProbeError("ffprobe is unavailable") from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MediaProbeError("ffprobe failed or timed out") from error
    if completed.returncode != 0:
        raise MediaProbeError("ffprobe returned a non-zero exit code")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise MediaProbeError("ffprobe returned invalid JSON") from error
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise MediaProbeError("ffprobe response is missing streams")

    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    video = videos[0] if videos else {}
    audio = audios[0] if audios else {}
    format_data = payload.get("format") or {}
    try:
        duration_ms = round(float(format_data.get("duration") or 0) * 1000)
    except (TypeError, ValueError):
        duration_ms = 0
    return {
        "duration_ms": duration_ms,
        "video_stream_count": len(videos),
        "audio_stream_count": len(audios),
        "video": {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": _integer_rate(
                video.get("avg_frame_rate") or video.get("r_frame_rate")
            ),
            "pixel_format": video.get("pix_fmt"),
        } if videos else None,
        "audio": {
            "codec": audio.get("codec_name"),
            "sample_rate": int(audio["sample_rate"])
            if str(audio.get("sample_rate") or "").isdigit() else None,
            "channels": audio.get("channels"),
            "channel_layout": audio.get("channel_layout"),
        } if audios else None,
        "format": format_data.get("format_name"),
        "size_bytes": path.stat().st_size,
    }
