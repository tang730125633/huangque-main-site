"""Fixed-origin HTTPS client and local credential storage for HQ CLI."""

import json
import os
from pathlib import Path
import secrets
import urllib.error
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
