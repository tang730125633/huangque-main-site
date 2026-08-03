"""Safe, callback-driven media acceptance and immutable publication for PR-F."""

import hashlib
import ipaddress
import os
import socket
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


class LipsyncMediaError(ValueError):
    pass


class LipsyncMediaValidationError(LipsyncMediaError):
    """The complete provider artifact is definitively outside the contract."""


class LipsyncMediaInfrastructureError(RuntimeError):
    """Local probing, remuxing, or filesystem work can be retried safely."""


def _exception_chain(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = getattr(error, "__cause__", None) or getattr(
            error, "__context__", None
        )


def _probe_media(probe, path):
    try:
        return probe(str(path))
    except LipsyncMediaError:
        raise
    except LipsyncMediaInfrastructureError:
        raise
    except Exception as error:
        code = str(getattr(error, "code", "") or "")
        if (
            code == "ffprobe_unavailable"
            or any(
                isinstance(item, (OSError, subprocess.TimeoutExpired))
                for item in _exception_chain(error)
            )
        ):
            raise LipsyncMediaInfrastructureError(
                "media probe infrastructure is unavailable"
            ) from error
        raise LipsyncMediaValidationError(
            "provider result cannot be decoded"
        ) from error


def validate_result_url(url, resolver=socket.getaddrinfo):
    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname:
        raise LipsyncMediaError("provider result must use public HTTPS")
    try:
        port = parsed.port or 443
    except ValueError as error:
        raise LipsyncMediaError("provider result URL is malformed") from error
    for entry in resolver(parsed.hostname, port, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(entry[4][0])
        if not address.is_global:
            raise LipsyncMediaError("provider result resolves to private network")
    return {
        "url": parsed.geturl(),
        "hostname": parsed.hostname,
        "port": port,
    }


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def accept_and_publish(
    *, job_id, project_id, shot_id, provider, source_file, output_root,
    expected_spec, probe, remux=None, max_bytes=1024 * 1024 * 1024,
):
    source_file = Path(source_file)
    if not source_file.is_file():
        raise LipsyncMediaInfrastructureError("provider result is missing")
    try:
        source_size = source_file.stat().st_size
    except OSError as error:
        raise LipsyncMediaInfrastructureError(
            "provider result cannot be read"
        ) from error
    if source_size <= 0 or source_size > max_bytes:
        raise LipsyncMediaValidationError("provider result size is invalid")
    first = _probe_media(probe, source_file)
    video = dict((first or {}).get("video") or {})
    if not video:
        raise LipsyncMediaValidationError(
            "provider result has no video stream"
        )
    expected = dict(expected_spec or {})
    if expected.get("width") and int(video.get("width") or 0) != int(expected["width"]):
        raise LipsyncMediaValidationError("provider result width mismatch")
    if expected.get("height") and int(video.get("height") or 0) != int(expected["height"]):
        raise LipsyncMediaValidationError("provider result height mismatch")
    duration = int((first or {}).get("duration_ms") or 0)
    expected_duration = int(expected.get("duration_ms") or 0)
    if expected_duration and abs(duration - expected_duration) > 250:
        raise LipsyncMediaValidationError("provider result duration mismatch")

    root = Path(output_root)
    relative = Path("lipsync") / str(project_id) / str(shot_id) / (
        str(job_id) + ".mp4"
    )
    destination = root / relative
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=str(job_id) + "-", suffix=".mp4",
            dir=str(destination.parent),
        )
        os.close(fd)
    except OSError as error:
        raise LipsyncMediaInfrastructureError(
            "lipsync publication directory is unavailable"
        ) from error
    temporary = Path(temporary)
    try:
        try:
            if callable(remux):
                remux(str(source_file), str(temporary))
            else:
                temporary.write_bytes(source_file.read_bytes())
        except Exception as error:
            raise LipsyncMediaInfrastructureError(
                "lipsync media remux failed"
            ) from error
        second = _probe_media(probe, temporary)
        if (second or {}).get("audio"):
            raise LipsyncMediaValidationError(
                "published lipsync video must not contain audio"
            )
        if not (second or {}).get("video"):
            raise LipsyncMediaValidationError(
                "published lipsync result has no video"
            )
        try:
            file_hash = file_sha256(temporary)
            os.replace(str(temporary), str(destination))
        except OSError as error:
            raise LipsyncMediaInfrastructureError(
                "lipsync publication filesystem failed"
            ) from error
        return {
            "file": relative.as_posix(),
            "file_hash": file_hash,
            "media_spec": {
                "width": int(second["video"]["width"]),
                "height": int(second["video"]["height"]),
                "fps": float(second["video"].get("fps") or 0),
                "duration_ms": int(second.get("duration_ms") or 0),
                "codec": str(second["video"].get("codec") or ""),
                "format": "mp4",
            },
            "provider": str(provider),
        }
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
