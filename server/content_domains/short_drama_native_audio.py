import hashlib
import math
import os
import re
import subprocess
import time
from pathlib import Path

from . import short_drama_assembly_plan as media_plan


NATIVE_AUDIO_SILENCE_DBFS = -60.0
_VOLUME_PATTERN = re.compile(
    r"\b(mean_volume|max_volume):\s*(-?inf|[-+]?\d+(?:\.\d+)?)\s*dB\b",
    re.IGNORECASE,
)


class NativeAudioError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = str(code)


def sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def inspect_native_media(path, expected_resolution="2K"):
    target = Path(path)
    try:
        before = target.stat()
        resolution = inspect_native_resolution(target, expected_resolution)
        audio = inspect_native_audio(target)
        sha256, size = sha256_file(target)
        after = target.stat()
    except NativeAudioError:
        raise
    except (OSError, ValueError) as error:
        raise NativeAudioError(
            "provider_media_probe_failed",
            "视频媒体校验失败，请重新生成当前镜头",
        ) from error
    identity_before = (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
    )
    if identity_before != identity_after or size != after.st_size:
        raise NativeAudioError(
            "provider_media_changed", "媒体校验期间文件发生变化，请重新生成当前镜头"
        )
    return {
        "sha256": sha256,
        "size_bytes": size,
        "resolution": resolution,
        "audio": audio,
        "inspected_at": int(time.time()),
    }


def inspect_native_resolution(path, expected_resolution, probe=media_plan.probe_media):
    expected = str(expected_resolution or "").strip().lower()
    try:
        media = probe(path)
    except Exception as error:
        raise NativeAudioError(
            "provider_resolution_probe_failed",
            "视频清晰度校验失败，请重新生成当前镜头",
        ) from error
    width, height = media_plan.dimensions_for_ratio(
        media if isinstance(media, dict) else {}
    )
    if not width or not height:
        raise NativeAudioError(
            "provider_resolution_probe_failed",
            "视频清晰度校验失败，请重新生成当前镜头",
        )
    if expected == "2k" and (max(width, height) < 2500 or min(width, height) < 1400):
        raise NativeAudioError(
            "provider_resolution_below_2k",
            "麦克视频未返回原生 2K 画面，已停止保存，请重新生成当前镜头",
        )
    return {"width": int(width), "height": int(height)}


def _volume_value(raw):
    return -math.inf if str(raw).lower() == "-inf" else float(raw)


def inspect_native_audio(path, probe=media_plan.probe_media, runner=subprocess.run):
    try:
        media = probe(path)
    except Exception as error:
        raise NativeAudioError(
            "provider_audio_probe_failed", "视频声音校验失败，请重新生成当前镜头"
        ) from error
    audio = media.get("audio") if isinstance(media, dict) else None
    if not media or not media.get("video") or not isinstance(audio, dict):
        raise NativeAudioError(
            "provider_audio_missing", "生成的视频没有声音，请调整声音设计后重新生成"
        )
    command = [
        os.environ.get("FFMPEG_BIN", "ffmpeg"),
        "-hide_banner", "-nostats", "-i", str(path),
        "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-",
    ]
    try:
        completed = runner(
            command, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NativeAudioError(
            "provider_audio_probe_failed", "视频声音校验失败，请重新生成当前镜头"
        ) from error
    if completed.returncode != 0:
        raise NativeAudioError(
            "provider_audio_probe_failed", "视频声音校验失败，请重新生成当前镜头"
        )
    volumes = {
        key.lower(): _volume_value(value)
        for key, value in _VOLUME_PATTERN.findall(str(completed.stderr or ""))
    }
    if "mean_volume" not in volumes or "max_volume" not in volumes:
        raise NativeAudioError(
            "provider_audio_probe_failed", "视频声音校验失败，请重新生成当前镜头"
        )
    if volumes["max_volume"] <= NATIVE_AUDIO_SILENCE_DBFS:
        raise NativeAudioError(
            "provider_audio_silent", "生成的视频声音不可听，请调整声音设计后重新生成"
        )
    return {
        "audible": True,
        "codec": str(audio.get("codec") or ""),
        "sample_rate": int(audio.get("sample_rate") or 0),
        "channels": int(audio.get("channels") or 0),
        "mean_volume_dbfs": volumes["mean_volume"],
        "max_volume_dbfs": volumes["max_volume"],
    }
