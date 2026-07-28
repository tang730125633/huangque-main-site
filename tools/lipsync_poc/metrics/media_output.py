"""Hash and remove provider audio before a PoC result becomes a candidate."""

import hashlib
import os
import subprocess
from pathlib import Path


class MediaOutputError(ValueError):
    pass


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_strip_audio_command(source, destination):
    return [
        os.environ.get("FFMPEG_BIN", "ffmpeg"),
        "-v", "error",
        "-y",
        "-i", str(source),
        "-map", "0:v:0",
        "-c:v", "copy",
        "-an",
        "-movflags", "+faststart",
        str(destination),
    ]


def ensure_silent_video(
    path,
    media_probe,
    *,
    runner=subprocess.run,
    timeout=120,
):
    path = Path(path)
    before = media_probe(path)
    audio_streams = int(before.get("audio_stream_count") or 0)
    if audio_streams <= 0:
        return {
            "audio_removed": False,
            "source_audio_stream_count": 0,
            "output_sha256": file_sha256(path),
            "size_bytes": path.stat().st_size,
        }

    temporary = path.with_suffix(".silent.part.mp4")
    try:
        try:
            completed = runner(
                build_strip_audio_command(path, temporary),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise MediaOutputError("ffmpeg is unavailable") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaOutputError(
                "ffmpeg audio removal failed or timed out"
            ) from exc
        if completed.returncode != 0 or not temporary.is_file():
            raise MediaOutputError("ffmpeg could not remove provider audio")
        after = media_probe(temporary)
        if int(after.get("video_stream_count") or 0) < 1:
            raise MediaOutputError("silent output has no video stream")
        if int(after.get("audio_stream_count") or 0) != 0:
            raise MediaOutputError("provider audio remains after sanitization")
        os.replace(temporary, path)
        return {
            "audio_removed": True,
            "source_audio_stream_count": audio_streams,
            "output_sha256": file_sha256(path),
            "size_bytes": path.stat().st_size,
        }
    finally:
        if temporary.exists():
            temporary.unlink()
