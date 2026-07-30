"""Fixed-origin HTTPS client and local credential storage for HQ CLI."""

import hashlib
import http.client
import json
import os
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
IMAGE_UPLOAD_PATH = "/api/auth/cli/image-upload"


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


def _image_mime(header):
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _open_image(path):
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("--file must be an absolute path")
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
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("upload file must be a regular file")
        if not 0 < before.st_size <= MAX_IMAGE_UPLOAD_BYTES:
            raise ValueError("upload image must be between 1 byte and 10 MiB")
        header = os.read(descriptor, 16)
        mime = _image_mime(header)
        if not mime:
            raise ValueError("upload file must be PNG, JPG, or WebP")
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


def upload_image(path, token, timeout=120):
    if not isinstance(token, str) or not token:
        raise ValueError("missing access token")
    descriptor, file_stat, mime, digest = _open_image(path)
    target = urllib.parse.urlsplit(API_BASE)
    if target.scheme != "https" or target.hostname != "huangquechuanmei.com" or target.path not in {"", "/"}:
        os.close(descriptor)
        raise ValueError("HQ CLI only uploads to the fixed main-site origin")
    connection = http.client.HTTPSConnection(target.hostname, target.port or 443, timeout=timeout)
    try:
        connection.putrequest("POST", IMAGE_UPLOAD_PATH, skip_accept_encoding=True)
        connection.putheader("Authorization", "Bearer " + token)
        connection.putheader("Content-Type", mime)
        connection.putheader("Content-Length", str(file_stat.st_size))
        connection.putheader("X-HQ-Image-SHA256", digest)
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
    if 200 <= int(status) < 300 and payload.get("sha256") != digest:
        raise NetworkError("server upload digest mismatch")
    return int(status), payload


def credentials_path():
    configured = os.environ.get("HQ_CLI_CONFIG_DIR")
    base = Path(configured).expanduser() if configured else Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "hq-cli"
    return base / "credentials.json"


def save_credentials(token, expires_at, scopes):
    if not isinstance(token, str) or len(token) < 20:
        raise ValueError("invalid access token")
    path = credentials_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temp = path.with_name(".%s.%s.tmp" % (path.name, secrets.token_hex(6)))
    payload = json.dumps({"access_token": token, "expires_at": int(expires_at), "scopes": list(scopes)},
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
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
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not 20 <= len(token) <= 200:
        return None
    return payload


def delete_credentials():
    try:
        credentials_path().unlink()
    except FileNotFoundError:
        pass
