import math
import os
import re
import subprocess

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
