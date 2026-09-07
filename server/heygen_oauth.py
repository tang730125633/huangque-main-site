"""Small, server-side HeyGen OAuth 2.0 + PKCE lifecycle.

Tokens never cross the admin browser boundary.  The module deliberately keeps
authorization transactions in memory: they are short-lived, one-shot, and a
service restart safely invalidates an unfinished login.
"""

import base64
import hashlib
import json
import os
import pathlib
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


DEFAULT_CLIENT_ID = "q2A2QRSke2LrFTPJhoDbHtXh"
DEFAULT_AUTHORIZE_URL = "https://app.heygen.com/oauth/authorize"
DEFAULT_TOKEN_URL = "https://api2.heygen.com/v1/oauth/token"
DEFAULT_REVOKE_URL = "https://api2.heygen.com/v1/oauth/revoke"
DEFAULT_RESOURCE = "https://mcp.heygen.com/mcp/v1"
DEFAULT_SCOPE = "openid profile email"
FLOW_TTL_SECONDS = 600
MAX_PENDING_FLOWS = 16


class HeyGenOAuthError(RuntimeError):
    """A safe, user-facing OAuth failure."""


_flows = {}
_flow_lock = threading.Lock()


def _now(now=None):
    return float(time.time() if now is None else now)


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _client_id():
    return (os.environ.get("HEYGEN_OAUTH_CLIENT_ID") or DEFAULT_CLIENT_ID).strip()


def _endpoint(name, default):
    return (os.environ.get(name) or default).strip()


def _resource():
    return (os.environ.get("HEYGEN_OAUTH_RESOURCE") or DEFAULT_RESOURCE).strip().rstrip("/")


def _validate_redirect_uri(value):
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
        port = parsed.port
    except ValueError as exc:
        raise HeyGenOAuthError("HeyGen OAuth 回调地址无效") from exc
    host = (parsed.hostname or "").lower()
    is_loopback = host in {"127.0.0.1", "localhost", "::1"}
    if (
        not parsed.scheme
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback))
        or port is None and parsed.scheme not in {"http", "https"}
    ):
        raise HeyGenOAuthError("HeyGen OAuth 回调必须是 HTTPS，或本机 loopback HTTP 地址")
    return urllib.parse.urlunsplit(parsed)


def _read_credentials(path):
    path = pathlib.Path(path)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise HeyGenOAuthError("HeyGen OAuth 凭据文件无法读取") from exc
    if not isinstance(value, dict):
        raise HeyGenOAuthError("HeyGen OAuth 凭据格式无效")
    # Accept the official CLI's nested format as an import-compatible input.
    nested = value.get("oauth")
    if isinstance(nested, dict):
        merged = dict(nested)
        merged["user"] = value.get("user") if isinstance(value.get("user"), dict) else {}
        return merged
    return value


def _expiry_timestamp(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError):
            return 0


def _atomic_write_credentials(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), delete=False
        ) as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
            temporary = pathlib.Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        if temporary and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def credential_status(path, now=None):
    if not path:
        return {"configured": False, "status": "path_not_configured"}
    try:
        credentials = _read_credentials(path)
    except HeyGenOAuthError:
        return {"configured": False, "status": "credential_rejected"}
    if not credentials:
        return {"configured": False, "status": "not_connected"}
    access = str(credentials.get("access_token") or "").strip()
    refresh = str(credentials.get("refresh_token") or "").strip()
    expires_at = _expiry_timestamp(credentials.get("expires_at"))
    fresh = bool(access and (not expires_at or expires_at > _now(now) + 60))
    refreshable = bool(refresh and (credentials.get("client_id") or _client_id()))
    status = "connected" if fresh else ("refresh_required" if refreshable else "credential_rejected")
    user = credentials.get("user") if isinstance(credentials.get("user"), dict) else {}
    return {
        "configured": fresh or refreshable,
        "status": status,
        "expires_at": int(expires_at) if expires_at else None,
        "refreshable": refreshable,
        "scope": str(credentials.get("scope") or ""),
        "account": {
            key: str(user.get(key) or "")
            for key in ("email", "username", "first_name", "last_name")
            if user.get(key)
        },
    }


def begin_authorization(path, redirect_uri, actor, now=None):
    if not path:
        raise HeyGenOAuthError("服务器尚未配置 HeyGen OAuth 凭据路径")
    redirect_uri = _validate_redirect_uri(redirect_uri)
    current = _now(now)
    state = secrets.token_urlsafe(32)
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    with _flow_lock:
        expired = [key for key, item in _flows.items() if item["expires_at"] <= current]
        for key in expired:
            _flows.pop(key, None)
        while len(_flows) >= MAX_PENDING_FLOWS:
            oldest = min(_flows, key=lambda key: _flows[key]["created_at"])
            _flows.pop(oldest, None)
        _flows[state] = {
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "credential_path": str(pathlib.Path(path)),
            "actor": str(actor or "admin")[:120],
            "created_at": current,
            "expires_at": current + FLOW_TTL_SECONDS,
        }
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": redirect_uri,
        "scope": (os.environ.get("HEYGEN_OAUTH_SCOPE") or DEFAULT_SCOPE).strip(),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": _resource(),
    })
    return {
        "authorization_url": _endpoint("HEYGEN_OAUTH_AUTHORIZE_URL", DEFAULT_AUTHORIZE_URL) + "?" + query,
        "expires_in": FLOW_TTL_SECONDS,
    }


def _consume_flow(state, now=None):
    current = _now(now)
    with _flow_lock:
        flow = _flows.pop(str(state or ""), None)
    if not flow:
        raise HeyGenOAuthError("HeyGen 授权已失效，请返回后台重新连接")
    if flow["expires_at"] <= current:
        raise HeyGenOAuthError("HeyGen 授权已超时，请返回后台重新连接")
    return flow


def complete_authorization(state, code, opener=None, now=None):
    flow = _consume_flow(state, now=now)
    if not code:
        raise HeyGenOAuthError("HeyGen 未返回授权码")
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": str(code),
        "redirect_uri": flow["redirect_uri"],
        "client_id": _client_id(),
        "code_verifier": flow["verifier"],
        "resource": _resource(),
    }).encode("utf-8")
    request = urllib.request.Request(
        _endpoint("HEYGEN_OAUTH_TOKEN_URL", DEFAULT_TOKEN_URL),
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "huangque-admin/1.0",
        },
        method="POST",
    )
    try:
        with (opener or urllib.request.build_opener()).open(request, timeout=30) as response:
            raw = response.read(1024 * 1024)
        token = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HeyGenOAuthError("HeyGen 拒绝了授权，请返回后台重试") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise HeyGenOAuthError("HeyGen 授权服务暂时不可用，请稍后重试") from exc
    access = str(token.get("access_token") or "").strip() if isinstance(token, dict) else ""
    if not access or any(char in access for char in "\r\n\0"):
        raise HeyGenOAuthError("HeyGen 返回的授权凭据无效")
    refresh = str(token.get("refresh_token") or "").strip()
    if any(char in refresh for char in "\r\n\0"):
        raise HeyGenOAuthError("HeyGen 返回的刷新凭据无效")
    issued_at = int(_now(now))
    try:
        expires_in = max(60, int(token.get("expires_in") or 3600))
    except (TypeError, ValueError):
        expires_in = 3600
    credentials = {
        "client_id": _client_id(),
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": issued_at + expires_in,
        "scope": str(token.get("scope") or ""),
        "token_type": str(token.get("token_type") or "Bearer"),
        "resource": _resource(),
        "authorized_at": issued_at,
        "authorized_by": flow["actor"],
    }
    _atomic_write_credentials(flow["credential_path"], credentials)
    return {"ok": True, "actor": flow["actor"]}


def disconnect(path):
    if not path:
        return False
    target = pathlib.Path(path)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise HeyGenOAuthError("无法移除本地 HeyGen 授权") from exc
