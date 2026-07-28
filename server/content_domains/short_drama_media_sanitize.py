"""Controlled media sanitization for short-drama visual sources.

Provider video is never trusted as an audio source.  This module preserves the
downloaded provider asset and atomically publishes a separate video-only
mezzanine that downstream short-drama stages may consume.
"""

import os
import subprocess
from pathlib import Path

from . import short_drama_assembly_plan as media_plan


SANITIZE_TIMEOUT_SECONDS = 180


class MediaSanitizeError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def build_silent_video_command(source, destination):
    """Build a shell-free, video-copy command that explicitly drops all audio."""
    return [
        os.environ.get("FFMPEG_BIN", "ffmpeg"),
        "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source),
        "-map", "0:v:0",
        "-an",
        "-c:v", "copy",
        "-movflags", "+faststart",
        str(destination),
    ]


def _run(command, runner, timeout):
    try:
        result = runner(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise MediaSanitizeError(
            "ffmpeg_unavailable", "服务器未安装或无法调用 FFmpeg"
        ) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MediaSanitizeError(
            "visual_sanitize_failed", "无声画面净化执行失败或超时"
        ) from error
    if result.returncode != 0:
        raise MediaSanitizeError(
            "visual_sanitize_failed", "无法生成无声画面中间件"
        )
    return result


def sanitize_visual_source(
    source,
    destination,
    runner=subprocess.run,
    probe=media_plan.probe_media,
    timeout=SANITIZE_TIMEOUT_SECONDS,
):
    """Publish a verified video-only mezzanine while retaining the raw source."""
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination:
        raise MediaSanitizeError(
            "visual_sanitize_failed", "无声画面不得覆盖供应商原始资产"
        )
    if not source.is_file():
        raise MediaSanitizeError(
            "missing_source_file", "供应商视频文件不存在"
        )

    source_report = probe(source)
    if not isinstance(source_report, dict) or source_report.get("video") is None:
        raise MediaSanitizeError(
            "media_probe_failed", "供应商视频缺少可用画面"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        destination.stem + ".part" + destination.suffix
    )
    try:
        _run(
            build_silent_video_command(source, temporary),
            runner,
            timeout,
        )
        silent_report = probe(temporary)
        if (
            not isinstance(silent_report, dict)
            or silent_report.get("video") is None
            or silent_report.get("audio") is not None
        ):
            raise MediaSanitizeError(
                "visual_audio_present",
                "无声画面中间件仍包含音轨",
            )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()

    return {
        "source_report": source_report,
        "silent_report": silent_report,
        "source_file": str(source),
        "silent_file": str(destination),
    }
