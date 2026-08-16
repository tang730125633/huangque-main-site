"""Authentication, authorization, rate limiting and auditing for Hermes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict, deque
from http import cookies
from pathlib import Path

from flask import g, jsonify, request


AUTH_BASE = os.environ.get("HQ_AUTH_BASE", "http://127.0.0.1:8095").rstrip("/")
AUTH_COOKIE_NAME = os.environ.get("HQ_AUTH_COOKIE_NAME", "hq_session")
AUTH_CACHE_SECONDS = max(0, int(os.environ.get("HERMES_AUTH_CACHE_SECONDS", "15")))
INTERNAL_ROLES = {
    role.strip()
    for role in os.environ.get("HERMES_INTERNAL_ROLES", "admin").split(",")
    if role.strip()
}
RATE_WINDOW_SECONDS = max(1, int(os.environ.get("HERMES_RATE_WINDOW_SECONDS", "60")))
RATE_REQUESTS = max(1, int(os.environ.get("HERMES_RATE_REQUESTS", "20")))
USER_CONCURRENCY = max(1, int(os.environ.get("HERMES_USER_CONCURRENCY", "2")))

INTERNAL_PREFIXES = (
    "/api/agnes/",
    "/api/team-workbench/",
    "/media/agnes/",
    "/media/team-workbench/",
)
INTERNAL_PATHS = {"/agnes-lab", "/team-workbench"}
UPLOAD_PATHS = {
    "/api/pipeline-upload",
    "/api/media/upload",
    "/api/agnes/upload-image",
    "/api/team-workbench/upload",
}

_auth_cache = {}
_auth_lock = threading.Lock()
_rate_hits = defaultdict(deque)
_rate_lock = threading.Lock()
_active = defaultdict(int)
_active_lock = threading.Lock()
_audit_lock = threading.Lock()
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")


def _token_from_request():
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization.split(None, 1)[1].strip()
        if token and token != "__cookie__":
            return token
    try:
        jar = cookies.SimpleCookie()
        jar.load(request.headers.get("Cookie", ""))
        morsel = jar.get(AUTH_COOKIE_NAME)
        return morsel.value.strip() if morsel and morsel.value else ""
    except Exception:
        return ""


def _validate_token(token):
    cache_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.monotonic()
    with _auth_lock:
        cached = _auth_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

    req = urllib.request.Request(
        AUTH_BASE + "/api/auth/me",
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None
        raise RuntimeError("authentication service rejected the request") from exc
    except Exception as exc:
        raise RuntimeError("authentication service unavailable") from exc

    user = payload.get("user")
    if not isinstance(user, dict):
        return None
    username = str(user.get("username") or "").strip()
    account_id = str(user.get("account_id") or "").strip()
    if not username or not account_id:
        return None
    identity = {
        "account_id": account_id,
        "username": username,
        "role": str(user.get("role") or "member").strip(),
    }
    with _auth_lock:
        _auth_cache[cache_key] = (now + AUTH_CACHE_SECONDS, identity)
    return identity


def _is_internal(path):
    return path in INTERNAL_PATHS or any(path.startswith(prefix) for prefix in INTERNAL_PREFIXES)


def _is_metered(method):
    return method in {"POST", "PUT", "PATCH", "DELETE"}


def _client_ip():
    return (request.headers.get("X-Real-IP") or request.remote_addr or "").strip()


def _audit(data_dir, event, status, username="", detail=""):
    g.hermes_audit_recorded = True
    started_at = getattr(g, "hermes_started_at", None)
    record = {
        "time": int(time.time()),
        "event": event,
        "status": status,
        "username": username,
        "role": getattr(g, "hermes_user", {}).get("role", ""),
        "method": request.method,
        "path": request.path,
        "ip": _client_ip(),
        "detail": str(detail)[:200],
        "duration_ms": round((time.monotonic() - started_at) * 1000, 1) if started_at else None,
        "request_id": getattr(g, "hermes_request_id", ""),
    }
    audit_dir = Path(data_dir) / "audit"
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        with _audit_lock:
            with (audit_dir / "security.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _consume_rate(username):
    key = username + "|" + _client_ip() + "|" + request.path
    now = time.monotonic()
    with _rate_lock:
        hits = _rate_hits[key]
        while hits and now - hits[0] >= RATE_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= RATE_REQUESTS:
            return False
        hits.append(now)
        return True


def _acquire_concurrency(username):
    with _active_lock:
        if _active[username] >= USER_CONCURRENCY:
            return False
        _active[username] += 1
        return True


def _release_concurrency(username):
    with _active_lock:
        _active[username] = max(0, _active[username] - 1)


def current_identity():
    identity = getattr(g, "hermes_user", None)
    if not identity:
        raise RuntimeError("authenticated Hermes request required")
    return identity


def current_username():
    return current_identity()["username"]


def current_role():
    return current_identity().get("role", "")


def register_security(app, data_dir):
    max_upload_mb = max(1, int(os.environ.get("HERMES_MAX_UPLOAD_MB", "50")))
    app.config["MAX_CONTENT_LENGTH"] = max_upload_mb * 1024 * 1024

    @app.before_request
    def authenticate_request():
        g.hermes_started_at = time.monotonic()
        incoming_request_id = (request.headers.get("X-Request-ID") or "").strip()
        g.hermes_request_id = (
            incoming_request_id if _REQUEST_ID_RE.fullmatch(incoming_request_id) else uuid.uuid4().hex
        )
        if request.path == "/healthz":
            return None
        token = _token_from_request()
        if not token:
            _audit(data_dir, "authentication", "denied", detail="missing token")
            return jsonify({"ok": False, "error": "authentication required"}), 401
        try:
            identity = _validate_token(token)
        except RuntimeError:
            _audit(data_dir, "authentication", "unavailable")
            return jsonify({"ok": False, "error": "authentication service unavailable"}), 503
        if not identity:
            _audit(data_dir, "authentication", "denied", detail="invalid token")
            return jsonify({"ok": False, "error": "invalid or expired login"}), 401

        g.hermes_user = identity
        username = identity["username"]
        if _is_internal(request.path) and identity.get("role") not in INTERNAL_ROLES:
            _audit(data_dir, "authorization", "denied", username, "internal route")
            return jsonify({"ok": False, "error": "administrator permission required"}), 403

        if request.method == "POST" and request.path in UPLOAD_PATHS:
            import artifact_store
            incoming = max(0, request.content_length or 0)
            try:
                artifact_store.ensure_capacity(incoming)
            except artifact_store.StorageQuotaExceeded:
                _audit(data_dir, "storage_quota", "denied", username)
                return jsonify({"ok": False, "error": "Hermes storage quota exceeded"}), 507

        if _is_metered(request.method):
            if not _consume_rate(username):
                _audit(data_dir, "rate_limit", "denied", username)
                return jsonify({"ok": False, "error": "too many requests"}), 429
            if not _acquire_concurrency(username):
                _audit(data_dir, "concurrency_limit", "denied", username)
                return jsonify({"ok": False, "error": "too many concurrent requests"}), 429
            g.hermes_concurrency_user = username

    @app.after_request
    def finish_request(response):
        concurrency_user = getattr(g, "hermes_concurrency_user", "")
        if concurrency_user:
            if response.is_streamed:
                response.call_on_close(lambda: _release_concurrency(concurrency_user))
            else:
                _release_concurrency(concurrency_user)
        identity = getattr(g, "hermes_user", {})
        username = identity.get("username", "")
        auditable = _is_metered(request.method) or request.path.startswith("/api/foundation-report/")
        if username and auditable and not getattr(g, "hermes_audit_recorded", False):
            event = "metered_request" if _is_metered(request.method) else "api_request"
            _audit(data_dir, event, response.status_code, username)
        response.headers["X-Request-ID"] = getattr(g, "hermes_request_id", uuid.uuid4().hex)
        return response
