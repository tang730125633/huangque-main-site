"""Fixed-origin HTTPS client and local credential storage for HQ CLI."""

import base64
import hashlib
import http.client
import json
import os
import re
from pathlib import Path
import secrets
import stat
import urllib.error
import urllib.parse
import urllib.request

from . import __version__


API_BASE = "https://huangquechuanmei.com"
ALLOWED_PATHS = {
    "/api/auth/cli/device/start",
    "/api/auth/cli/device/poll",
    "/api/auth/cli/status",
    "/api/auth/cli/logout",
    "/api/auth/cli/action",
}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_VIDEO_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_AUDIO_UPLOAD_BYTES = 10 * 1024 * 1024
IMAGE_UPLOAD_PATH = "/api/auth/cli/image-upload"
VIDEO_UPLOAD_PATH = "/api/auth/cli/video-upload"
AUDIO_UPLOAD_PATH = "/api/auth/cli/audio-upload"
DIGITAL_HUMAN_MATERIAL_UPLOAD_PATH = "/api/auth/cli/digital-human-material-upload"
DIGITAL_HUMAN_AUDIO_UPLOAD_PATH = "/api/auth/cli/digital-human-audio-upload"
DIRECTOR_BREAKDOWN_IMAGE_PATH = "/api/auth/cli/director-breakdown-image"
DIRECTOR_BREAKDOWN_VIDEO_PATH = "/api/auth/cli/director-breakdown-video"
DIRECTOR_BREAKDOWN_QUOTE_PATH = "/api/auth/cli/director-breakdown-quote"
ALLOWED_PATHS.add(DIRECTOR_BREAKDOWN_QUOTE_PATH)
DOWNLOAD_PATH = "/api/gen/dl"
BATCH_DOWNLOAD_PATH = "/api/auth/cli/asset-batch-download"
VIDEO_IMPORT_PATH = "/api/auth/cli/video-import"
PROFILE_AVATAR_UPLOAD_PATH = "/api/auth/cli/profile-avatar-upload"
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024


class NetworkError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(path, method="GET", body=None, token="", timeout=30):
    if path not in ALLOWED_PATHS or method not in {"GET", "POST"}:
        raise ValueError("HQ CLI only calls fixed main-site endpoints")
    headers = {"Accept": "application/json", "User-Agent": "hq-cli/%s" % __version__}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(API_BASE + path, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw, status = response.read(MAX_RESPONSE_BYTES + 1), response.getcode()
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(MAX_RESPONSE_BYTES + 1), exc.code
    except (urllib.error.URLError, OSError) as exc:
        raise NetworkError(str(exc))
    if len(raw) > MAX_RESPONSE_BYTES:
        raise NetworkError("server response exceeds 2 MiB")
    try:
        payload = json.loads(raw or b"{}")
    except (UnicodeDecodeError, ValueError):
        payload = {"detail": "server returned invalid JSON"}
        status = 502
    return int(status), payload


def download_file(url, output, token, name="video", decode_key="", timeout=300, post_payload=None, direct_path=""):
    if not isinstance(token, str) or not token:
        raise ValueError("missing access token")
    if not isinstance(output, str) or not os.path.isabs(output):
        raise ValueError("--output must be an absolute path")
    requested_target = Path(os.path.normpath(output))
    try:
        current = Path(requested_target.anchor)
        for part in requested_target.parent.parts[1:]:
            current /= part
            entry = os.lstat(current)
            is_link = stat.S_ISLNK(entry.st_mode) or bool(
                getattr(entry, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            system_alias = (
                os.name != "nt" and str(current) in {"/tmp", "/var"}
                and entry.st_uid == 0 and stat.S_ISLNK(entry.st_mode)
            )
            if is_link and not system_alias:
                raise ValueError("output directory cannot contain symlinks")
        parent = Path(os.path.realpath(requested_target.parent))
    except OSError:
        raise ValueError("output directory does not exist")
    target = parent / requested_target.name
    if not parent.is_dir():
        raise ValueError("output directory does not exist")
    if target.exists() or target.is_symlink():
        raise ValueError("output already exists")
    if post_payload is None and not direct_path and (not isinstance(url, str) or not url or len(url) > 4096):
        raise ValueError("download URL is invalid")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
        raise ValueError("download name is invalid")
    if not isinstance(decode_key, str) or len(decode_key) > 4096:
        raise ValueError("decode key is invalid")
    headers = {
            "Accept": "application/octet-stream,image/*,video/*",
            "Authorization": "Bearer " + token,
            "User-Agent": "hq-cli/%s" % __version__,
            **({"X-HQ-Decode-Key": decode_key} if decode_key else {}),
    }
    if direct_path:
        if post_payload is not None or not re.fullmatch(r"/api/auth/cli/creator-agent-background-pdf\?project_id=[0-9a-f]{12}", direct_path):
            raise ValueError("direct download path is invalid")
        headers["Accept"] = "application/pdf"
        request = urllib.request.Request(API_BASE + direct_path, headers=headers, method="GET")
    elif post_payload is None:
        query = urllib.parse.urlencode({"url": url, "name": name.strip()})
        request = urllib.request.Request(API_BASE + DOWNLOAD_PATH + "?" + query, headers=headers, method="GET")
    else:
        if not isinstance(post_payload, dict) or set(post_payload) != {"assets"}:
            raise ValueError("batch download input is invalid")
        data = json.dumps(post_payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers.update({"Accept": "application/zip", "Content-Type": "application/json"})
        request = urllib.request.Request(API_BASE + BATCH_DOWNLOAD_PATH, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    temp = parent / (".%s.%s.tmp" % (target.name, secrets.token_hex(6)))
    descriptor = None
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raw = exc.read(8193)
            try:
                detail = json.loads(raw or b"{}").get("detail")
            except (UnicodeDecodeError, ValueError, AttributeError):
                detail = "download failed"
            raise NetworkError("HTTP %s: %s" % (exc.code, str(detail or "download failed")[:220]))
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_DOWNLOAD_BYTES:
            response.close()
            raise ValueError("download exceeds 2 GiB")
        descriptor = os.open(
            str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
        )
        digest = hashlib.sha256()
        total = 0
        with response:
            content_type = (response.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0]
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("download exceeds 2 GiB")
                pending = memoryview(chunk)
                while pending:
                    pending = pending[os.write(descriptor, pending):]
                digest.update(chunk)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temp, target)
        return {
            "path": output, "bytes": total,
            "sha256": digest.hexdigest(), "content_type": content_type,
        }
    except (ValueError, NetworkError):
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise NetworkError(str(exc))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _image_mime(header):
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _video_mime(header):
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/quicktime" if header[8:12] == b"qt  " else "video/mp4"
    return ""


def _audio_mime(header):
    if len(header) >= 2 and header[0] == 0xFF and header[1] & 0xF6 == 0xF0:
        return "audio/aac"
    if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "audio/mp4"
    return ""


def _digital_human_audio_mime(header):
    mime = _audio_mime(header)
    return "" if mime == "audio/ogg" else mime


def _open_media(path, max_bytes, mime_detector, size_error, type_error):
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("--file must be an absolute path")
    if os.name == "nt":
        return _open_media_windows(path, max_bytes, mime_detector, size_error, type_error)
    parts = Path(path).parts
    # macOS 的 /tmp 与 /var 是 root 管理的系统别名；先固定到真实路径，再逐级拒绝用户 symlink。
    if len(parts) > 1 and parts[1] in {"tmp", "var"}:
        system_alias = os.path.sep + parts[1]
        try:
            alias_stat = os.lstat(system_alias)
            if alias_stat.st_uid == 0 and stat.S_ISLNK(alias_stat.st_mode):
                alias_target = Path(os.readlink(system_alias))
                if not alias_target.is_absolute():
                    alias_target = Path(os.path.sep) / alias_target
                parts = alias_target.parts + parts[2:]
        except OSError:
            pass
    if len(parts) < 2 or parts[0] != os.path.sep or any(part in {"", ".", ".."} for part in parts[1:]):
        raise ValueError("upload path is invalid")
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory = -1
    try:
        directory = os.open(os.path.sep, directory_flags)
        for part in parts[1:-1]:
            child = os.open(part, directory_flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], flags, dir_fd=directory)
    except OSError:
        raise ValueError("cannot open upload file")
    finally:
        if directory >= 0:
            os.close(directory)
    return _inspect_media_descriptor(descriptor, max_bytes, mime_detector, size_error, type_error)


def _open_media_windows(path, max_bytes, mime_detector, size_error, type_error):
    candidate = Path(os.path.normpath(path))
    if not candidate.drive or any(part in {"", ".", ".."} for part in candidate.parts[1:]):
        raise ValueError("upload path is invalid")
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    try:
        for entry in (candidate, *candidate.parents[:-1]):
            entry_stat = os.lstat(entry)
            if stat.S_ISLNK(entry_stat.st_mode) or getattr(entry_stat, "st_file_attributes", 0) & reparse_point:
                raise ValueError("upload path cannot contain a symlink or junction")
        descriptor = os.open(
            str(candidate),
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
        )
    except ValueError:
        raise
    except OSError:
        raise ValueError("cannot open upload file")
    return _inspect_media_descriptor(descriptor, max_bytes, mime_detector, size_error, type_error)


def _inspect_media_descriptor(descriptor, max_bytes, mime_detector, size_error, type_error):
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("upload file must be a regular file")
        if not 0 < before.st_size <= max_bytes:
            raise ValueError(size_error)
        header = os.read(descriptor, 32)
        mime = mime_detector(header)
        if not mime:
            raise ValueError(type_error)
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise ValueError("upload file changed while reading")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("upload file changed while reading")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, before, mime, digest.hexdigest()
    except Exception:
        os.close(descriptor)
        raise


def _open_image(path):
    return _open_media(
        path, MAX_IMAGE_UPLOAD_BYTES, _image_mime,
        "upload image must be between 1 byte and 10 MiB",
        "upload file must be PNG, JPG, or WebP",
    )


def _open_video(path):
    return _open_media(
        path, MAX_VIDEO_UPLOAD_BYTES, _video_mime,
        "upload video must be between 1 byte and 32 MiB",
        "upload file must be MP4, MOV, or WebM",
    )


def _open_video_import(path):
    return _open_media(
        path, 100 * 1024 * 1024,
        lambda header: "video/mp4" if _video_mime(header) == "video/mp4" else "",
        "import video must be between 1 byte and 100 MiB",
        "import video must be MP4",
    )


def _open_profile_avatar(path):
    return _open_media(
        path, 4 * 1024 * 1024, _image_mime,
        "profile avatar must be between 1 byte and 4 MiB",
        "profile avatar must be PNG, JPG, or WebP",
    )


def _open_audio(path):
    return _open_media(
        path, MAX_AUDIO_UPLOAD_BYTES, _audio_mime,
        "upload audio must be between 1 byte and 10 MiB",
        "upload file must be MP3, WAV, M4A, AAC, or OGG",
    )


def _open_digital_human_audio(path):
    return _open_media(
        path, 30 * 1024 * 1024, _digital_human_audio_mime,
        "digital-human audio must be between 1 byte and 30 MiB",
        "digital-human audio must be MP3, WAV, M4A, or AAC",
    )


def _breakdown_mime(header):
    return _image_mime(header) or _video_mime(header)


def _open_director_breakdown(path):
    descriptor, file_stat, mime, digest = _open_media(
        path, 200 * 1024 * 1024, _breakdown_mime,
        "director breakdown file must be between 1 byte and 200 MiB",
        "director breakdown file must be PNG, JPG, WebP, MP4, MOV, or WebM",
    )
    if mime.startswith("image/") and file_stat.st_size > 20 * 1024 * 1024:
        os.close(descriptor)
        raise ValueError("director breakdown image must not exceed 20 MiB")
    return descriptor, file_stat, mime, digest


def inspect_director_breakdown(path):
    descriptor, _file_stat, mime, digest = _open_director_breakdown(path)
    os.close(descriptor)
    return {
        "media_type": "image" if mime.startswith("image/") else "video",
        "sha256": digest,
    }


def quote_director_breakdown(path, token, timeout=30):
    descriptor = inspect_director_breakdown(path)
    status, payload = request_json(
        DIRECTOR_BREAKDOWN_QUOTE_PATH, method="POST", body=descriptor,
        token=token, timeout=timeout,
    )
    if 200 <= int(status) < 300:
        if (not isinstance(payload, dict)
                or payload.get("media_type") != descriptor["media_type"]
                or payload.get("sha256") != descriptor["sha256"]):
            raise NetworkError("server quote does not match the selected file")
    return status, payload


def _upload_media(path, token, upload_path, digest_header, opener, timeout, extra_headers=None):
    if not isinstance(token, str) or not token:
        raise ValueError("missing access token")
    descriptor, file_stat, mime, digest = opener(path)
    if isinstance(upload_path, dict):
        upload_path = upload_path.get(mime)
    if isinstance(digest_header, dict):
        digest_header = digest_header.get(mime)
    if upload_path not in {
            IMAGE_UPLOAD_PATH, VIDEO_UPLOAD_PATH, AUDIO_UPLOAD_PATH,
            DIGITAL_HUMAN_MATERIAL_UPLOAD_PATH, DIGITAL_HUMAN_AUDIO_UPLOAD_PATH,
            DIRECTOR_BREAKDOWN_IMAGE_PATH, DIRECTOR_BREAKDOWN_VIDEO_PATH,
            VIDEO_IMPORT_PATH, PROFILE_AVATAR_UPLOAD_PATH,
    } or not digest_header:
        os.close(descriptor)
        raise ValueError("HQ CLI only uploads to fixed main-site endpoints")
    target = urllib.parse.urlsplit(API_BASE)
    if target.scheme != "https" or target.hostname != "huangquechuanmei.com" or target.path not in {"", "/"}:
        os.close(descriptor)
        raise ValueError("HQ CLI only uploads to the fixed main-site origin")
    director_upload = upload_path in {
        DIRECTOR_BREAKDOWN_IMAGE_PATH, DIRECTOR_BREAKDOWN_VIDEO_PATH,
    }
    digital_human_audio = upload_path == DIGITAL_HUMAN_AUDIO_UPLOAD_PATH
    video_import = upload_path == VIDEO_IMPORT_PATH
    expected_extra = (
        {"X-HQ-Quote-Token", "X-HQ-Expected-Cost", "Idempotency-Key"}
        if director_upload else {"X-HQ-Run-ID"} if digital_human_audio else set()
    )
    if set((extra_headers or {}).keys()) != expected_extra:
        os.close(descriptor)
        raise ValueError("upload metadata does not match the fixed endpoint")
    connection = http.client.HTTPSConnection(target.hostname, target.port or 443, timeout=timeout)
    try:
        connection.putrequest("POST", upload_path, skip_accept_encoding=True)
        connection.putheader("Authorization", "Bearer " + token)
        connection.putheader("Content-Type", mime)
        connection.putheader("Content-Length", str(file_stat.st_size))
        connection.putheader(digest_header, digest)
        if director_upload:
            connection.putheader("X-HQ-File-Name", urllib.parse.quote(os.path.basename(path), safe="._-"))
            for key in ("X-HQ-Quote-Token", "X-HQ-Expected-Cost", "Idempotency-Key"):
                connection.putheader(key, extra_headers[key])
        elif digital_human_audio:
            connection.putheader("X-HQ-Run-ID", extra_headers["X-HQ-Run-ID"])
        elif video_import:
            connection.putheader("X-Video-Title", urllib.parse.quote(os.path.basename(path).removesuffix(".mp4"), safe="._-"))
        connection.putheader("X-HQ-Confirm", "true")
        connection.putheader("Accept", "application/json")
        connection.putheader("User-Agent", "hq-cli/%s" % __version__)
        connection.endheaders()
        remaining = file_stat.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise ValueError("upload file changed while sending")
            connection.send(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("upload file changed while sending")
        response = connection.getresponse()
        raw, status = response.read(MAX_RESPONSE_BYTES + 1), response.status
    except (OSError, http.client.HTTPException) as exc:
        raise NetworkError(str(exc))
    finally:
        connection.close()
        os.close(descriptor)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise NetworkError("server response exceeds 2 MiB")
    try:
        payload = json.loads(raw or b"{}")
    except (UnicodeDecodeError, ValueError):
        payload, status = {"detail": "server returned invalid JSON"}, 502
    if not isinstance(payload, dict):
        payload, status = {"detail": "server returned invalid JSON"}, 502
    response_digest = payload.get("source_sha256") if digital_human_audio else payload.get("sha256")
    if 200 <= int(status) < 300 and not video_import and response_digest != digest:
        raise NetworkError("server upload digest mismatch")
    return int(status), payload


def upload_image(path, token, timeout=120):
    return _upload_media(
        path, token, IMAGE_UPLOAD_PATH, "X-HQ-Image-SHA256", _open_image, timeout,
    )


def upload_video(path, token, timeout=120):
    return _upload_media(
        path, token, VIDEO_UPLOAD_PATH, "X-HQ-Video-SHA256", _open_video, timeout,
    )


def upload_video_import(path, token, timeout=180):
    return _upload_media(
        path, token, VIDEO_IMPORT_PATH, "X-HQ-Video-SHA256", _open_video_import, timeout,
    )


def upload_profile_avatar(path, token, timeout=120):
    return _upload_media(
        path, token, PROFILE_AVATAR_UPLOAD_PATH,
        "X-HQ-Image-SHA256", _open_profile_avatar, timeout,
    )


def upload_audio(path, token, timeout=120):
    return _upload_media(
        path, token, AUDIO_UPLOAD_PATH, "X-HQ-Audio-SHA256", _open_audio, timeout,
    )


def upload_digital_human_material(path, token, timeout=120):
    return _upload_media(
        path, token, DIGITAL_HUMAN_MATERIAL_UPLOAD_PATH,
        "X-HQ-Image-SHA256", _open_image, timeout,
    )


def upload_digital_human_audio(path, token, run_id, timeout=180):
    if (not isinstance(run_id, str)
            or not re.fullmatch(r"dh-run-[A-Za-z0-9._:-]{8,128}", run_id)):
        raise ValueError("digital-human audio upload requires a valid run_id")
    return _upload_media(
        path, token, DIGITAL_HUMAN_AUDIO_UPLOAD_PATH,
        "X-HQ-Audio-SHA256", _open_digital_human_audio, timeout,
        extra_headers={"X-HQ-Run-ID": run_id},
    )


def upload_director_breakdown(path, token, quote_token, expected_cost, timeout=180):
    if (not isinstance(quote_token, str) or not 1 <= len(quote_token) <= 4096
            or "\r" in quote_token or "\n" in quote_token):
        raise ValueError("Director breakdown upload requires a valid quote token")
    try:
        quote_token.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("Director breakdown upload requires a valid quote token")
    if isinstance(expected_cost, bool) or not isinstance(expected_cost, int) or expected_cost < 0:
        raise ValueError("Director breakdown upload requires a non-negative expected cost")
    idempotency_key = "hqcli-du-" + hashlib.sha256(quote_token.encode("utf-8")).hexdigest()[:32]
    return _upload_media(
        path, token,
        {
            "image/jpeg": DIRECTOR_BREAKDOWN_IMAGE_PATH,
            "image/png": DIRECTOR_BREAKDOWN_IMAGE_PATH,
            "image/webp": DIRECTOR_BREAKDOWN_IMAGE_PATH,
            "video/mp4": DIRECTOR_BREAKDOWN_VIDEO_PATH,
            "video/quicktime": DIRECTOR_BREAKDOWN_VIDEO_PATH,
            "video/webm": DIRECTOR_BREAKDOWN_VIDEO_PATH,
        },
        {
            "image/jpeg": "X-HQ-Image-SHA256", "image/png": "X-HQ-Image-SHA256",
            "image/webp": "X-HQ-Image-SHA256", "video/mp4": "X-HQ-Video-SHA256",
            "video/quicktime": "X-HQ-Video-SHA256", "video/webm": "X-HQ-Video-SHA256",
        },
        _open_director_breakdown, timeout,
        {
            "X-HQ-Quote-Token": quote_token,
            "X-HQ-Expected-Cost": str(expected_cost),
            "Idempotency-Key": idempotency_key,
        },
    )


def credentials_path():
    configured = os.environ.get("HQ_CLI_CONFIG_DIR")
    if configured:
        base = Path(configured).expanduser()
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", "~/AppData/Roaming")).expanduser() / "Huangque" / "hq-cli"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "hq-cli"
    return base / "credentials.json"


def _windows_dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    source = ctypes.create_string_buffer(data)
    source_blob = DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    result_blob = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    arguments = [ctypes.byref(source_blob), None, None, None, None, 0x1, ctypes.byref(result_blob)]
    if not function(*arguments):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI failed")
    try:
        return ctypes.string_at(result_blob.pbData, result_blob.cbData)
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(ctypes.cast(result_blob.pbData, ctypes.c_void_p))


def save_credentials(token, expires_at, scopes):
    if not isinstance(token, str) or len(token) < 20:
        raise ValueError("invalid access token")
    path = credentials_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temp = path.with_name(".%s.%s.tmp" % (path.name, secrets.token_hex(6)))
    payload = json.dumps({"access_token": token, "expires_at": int(expires_at), "scopes": list(scopes)},
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    if os.name == "nt":
        payload = json.dumps({
            "protected_data": base64.b64encode(_windows_dpapi(payload, True)).decode("ascii"),
            "protection": "windows-dpapi-current-user",
        }, sort_keys=True, separators=(",", ":")).encode("ascii")
    descriptor = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def load_credentials():
    path = credentials_path()
    protected_with_dpapi = False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("protection") == "windows-dpapi-current-user":
            protected_with_dpapi = True
            protected = base64.b64decode(payload["protected_data"], validate=True)
            payload = json.loads(_windows_dpapi(protected, False).decode("utf-8"))
    except (OSError, ValueError, KeyError):
        return None
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not 20 <= len(token) <= 200:
        return None
    if os.name == "nt" and not protected_with_dpapi:
        save_credentials(token, payload.get("expires_at", 0), payload.get("scopes", []))
    return payload


def delete_credentials():
    try:
        credentials_path().unlink()
    except FileNotFoundError:
        pass
