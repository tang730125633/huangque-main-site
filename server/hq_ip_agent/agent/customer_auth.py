"""Website-session authentication and per-session CLI credentials.

Only huangque-auth may decide who the browser user is.  Browser credentials
are forwarded to loopback auth, never persisted.  The resulting CLI access
token lives in memory and is scoped to one IP12 session/account pair.
"""
from __future__ import annotations

from dataclasses import dataclass
from http import cookies
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


AUTH_COOKIE_NAME = os.environ.get("HQ_AUTH_COOKIE_NAME", "hq_session")
MAX_AUTH_RESPONSE = 64 * 1024
LOCAL_CLI_TTL = 15 * 60


class AuthError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)


@dataclass(frozen=True)
class CustomerCredential:
    username: str
    account_id: str
    access_token: str
    expires_at: int
    scopes: tuple[str, ...]


def _loopback_base(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("HQ_AUTH_BASE 配置无效") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise RuntimeError("HQ_AUTH_BASE 必须是本机 HTTP 地址")
    return raw


def _website_headers(headers) -> dict:
    result = {}
    for name in ("Authorization", "Cookie"):
        value = str((headers or {}).get(name) or "").strip()
        if value:
            result[name] = value
    return result


def _session_cookie(headers) -> str:
    try:
        jar = cookies.SimpleCookie()
        jar.load(str((headers or {}).get("Cookie") or ""))
        value = jar.get(AUTH_COOKIE_NAME)
        token = value.value.strip() if value and value.value else ""
    except Exception:
        token = ""
    if not token:
        raise AuthError(401, "cli_identity_required", "请先登录黄雀账号，再使用工作台能力")
    return "%s=%s" % (AUTH_COOKIE_NAME, token)


class AuthClient:
    def __init__(self, base_url: str, timeout: int = 10, opener=None):
        self.base_url = _loopback_base(base_url)
        self.timeout = int(timeout)
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    def _json(self, path: str, method: str, headers: dict, body=None) -> dict:
        data = None
        request_headers = dict(headers)
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=request_headers, method=method,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_AUTH_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise AuthError(401, "unauthorized", "请先登录黄雀账号") from exc
            raise AuthError(503, "auth_unavailable", "黄雀账号服务暂时不可用") from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise AuthError(503, "auth_unavailable", "黄雀账号服务暂时不可用") from exc
        if len(raw) > MAX_AUTH_RESPONSE:
            raise AuthError(502, "auth_response_too_large", "黄雀账号服务返回异常")
        try:
            result = json.loads(raw or b"{}")
        except (UnicodeDecodeError, ValueError) as exc:
            raise AuthError(502, "auth_response_invalid", "黄雀账号服务返回异常") from exc
        if not isinstance(result, dict):
            raise AuthError(502, "auth_response_invalid", "黄雀账号服务返回异常")
        return result

    def verify(self, headers) -> dict:
        result = self._json("/api/auth/me", "GET", _website_headers(headers))
        user = result.get("user")
        if not isinstance(user, dict):
            raise AuthError(401, "unauthorized", "请先登录黄雀账号")
        username = str(user.get("username") or "").strip()
        account_id = str(user.get("account_id") or "").strip()
        if not username or not account_id:
            raise AuthError(401, "unauthorized", "请先登录黄雀账号")
        return {"username": username, "account_id": account_id}

    def issue_cli(self, headers, user: dict) -> CustomerCredential:
        result = self._json(
            "/api/auth/session/cli-token", "POST",
            {"Cookie": _session_cookie(headers)}, body=None,
        )
        token = str(result.get("access_token") or "").strip()
        username = str(result.get("username") or "").strip()
        try:
            expires_at = int(
                result.get("access_expires_at") or result.get("expires_at")
                or int(time.time()) + int(result.get("expires_in") or 0)
            )
        except (TypeError, ValueError):
            expires_at = 0
        scopes = result.get("scopes") or []
        if (
            not 20 <= len(token) <= 200
            or username != user["username"]
            or expires_at <= int(time.time()) + 30
            or not isinstance(scopes, list)
        ):
            raise AuthError(502, "cli_identity_invalid", "黄雀账号身份授权返回异常")
        return CustomerCredential(
            username=username,
            account_id=user["account_id"],
            access_token=token,
            # 生产接口当前签发 8 小时 token；IP12 本地最多保留 15 分钟，
            # 正常闲置/登出由 registry + logout 提前撤销。
            expires_at=min(expires_at, int(time.time()) + LOCAL_CLI_TTL),
            scopes=tuple(str(scope) for scope in scopes),
        )

    def revoke(self, credential: CustomerCredential) -> bool:
        try:
            self._json(
                "/api/auth/cli/logout", "POST",
                {"Authorization": "Bearer " + credential.access_token}, body={},
            )
            return True
        except AuthError:
            return False


class CredentialRegistry:
    """In-memory credential map. Tokens never enter session JSON or logs."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, tuple[CustomerCredential, float]] = {}

    def get(self, sid: str) -> CustomerCredential | None:
        now = time.time()
        with self._lock:
            item = self._items.get(sid)
            if not item:
                return None
            credential, _ = item
            if credential.expires_at <= int(now) + 30:
                return None
            self._items[sid] = (credential, now)
            return credential

    def put(self, sid: str, credential: CustomerCredential) -> CustomerCredential | None:
        with self._lock:
            previous = self._items.get(sid)
            if previous and previous[0].account_id != credential.account_id:
                raise AuthError(403, "session_forbidden", "这段会话不属于当前登录账号")
            self._items[sid] = (credential, time.time())
            return previous[0] if previous else None

    def drop(self, sid: str) -> CustomerCredential | None:
        with self._lock:
            item = self._items.pop(sid, None)
            return item[0] if item else None

    def trim(self, now: float, max_idle: float = 1800) -> list[CustomerCredential]:
        removed = []
        with self._lock:
            for sid, (credential, touched) in list(self._items.items()):
                if now - touched > max_idle or credential.expires_at <= int(now) + 30:
                    self._items.pop(sid, None)
                    removed.append(credential)
        return removed


REGISTRY = CredentialRegistry()
