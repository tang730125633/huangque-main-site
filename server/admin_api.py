#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Huangque operations admin API.

Stage 1 covers service/key/channel visibility and read-only job statistics.
All /api/admin/* routes require a platform token whose user role is admin.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
import json
import os
import pathlib
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from content_domains import feature_flags
except ImportError:
    feature_flags = None


BASE = pathlib.Path(__file__).resolve().parent
PORT = int(os.environ.get("ADMIN_API_PORT", "8099"))
AUTH_BASE = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095").rstrip("/")
AUTH_INTERNAL_TOKEN = os.environ.get("HQ_INTERNAL_TOKEN", "")
JOB_DB = pathlib.Path(os.environ.get("CONTENT_JOB_DB", str(BASE / "content_jobs.db")))
ADMIN_DB = pathlib.Path(os.environ.get("ADMIN_DB", str(BASE / "admin_config.db")))

ENV_FILES = [
    pathlib.Path("/home/ubuntu/content-api/content.env"),
    pathlib.Path("/home/ubuntu/content-api/runninghub.env"),
    pathlib.Path("/home/ubuntu/content-api/whisper.env"),
    pathlib.Path("/home/ubuntu/auth-service/auth.env"),
    pathlib.Path("/etc/leadgen-secrets.env"),
]

SERVICES = [
    {
        "key": "auth",
        "name": "认证服务",
        "port": 8095,
        "service_file": "deploy/systemd/huangque-auth.service",
        "health_url": "http://127.0.0.1:8095/api/auth/health",
    },
    {
        "key": "content",
        "name": "内容生成服务",
        "port": 8096,
        "service_file": "deploy/systemd/huangque-content.service",
        "health_url": "http://127.0.0.1:8096/api/gen/health",
    },
    {
        "key": "imggen",
        "name": "作图服务",
        "port": 8101,
        "service_file": "deploy/systemd/huangque-imggen-api.service",
        "health_url": "http://127.0.0.1:8101/api/gen/banana/health",
    },
    {
        "key": "leadgen",
        "name": "获客采集服务",
        "port": 8100,
        "service_file": "deploy/systemd/huangque-leadgen-api.service",
        "health_url": "http://127.0.0.1:8100/api/gen/leadgen/health",
    },
    {
        "key": "dl",
        "name": "下载代理服务",
        "port": 8097,
        "service_file": "deploy/systemd/huangque-dl.service",
        "health_url": "http://127.0.0.1:8097/api/gen/dl/health",
    },
]

KEY_GROUPS = [
    {"key": "heygen", "name": "HeyGen", "env": ["HEYGEN_API_KEY"]},
    {"key": "runninghub", "name": "RunningHub", "env": ["RUNNINGHUB_API_KEY", "RUNNINGHUB_KEY"]},
    {"key": "openai", "name": "OpenAI", "env": ["OPENAI_API_KEY"]},
    {"key": "tikhub", "name": "TikHub", "env": ["TIKHUB_KEY", "TIKHUB_API_KEY"]},
    {"key": "cos", "name": "腾讯云 COS", "env": ["COS_SECRET_ID", "COS_SECRET_KEY", "COS_REGION", "COS_BUCKET"]},
    {"key": "doubao", "name": "豆包语音", "env": ["DOUBAO_APP_ID", "DOUBAO_ACCESS_TOKEN", "DOUBAO_APPID", "DOUBAO_TOKEN"]},
]

CHANNELS = {
    item["key"]: {
        "key": item["key"],
        "name": item["name"],
        "required_env": item["env"],
        "default_config": {"cost": "", "rate_limit": "", "defaults": ""},
    }
    for item in KEY_GROUPS
}

SECRET_RE = re.compile(r"(key|token|secret|password|passwd|pwd|credential)", re.I)


def db():
    ADMIN_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ADMIN_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS admin_channel_config(
                channel TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                config TEXT NOT NULL DEFAULT '{}',
                updated_by TEXT,
                updated_at INTEGER NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS admin_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        c.commit()
    if feature_flags is not None:
        feature_flags.init_db()


def verify(token):
    if not token:
        return None
    try:
        req = urllib.request.Request(
            AUTH_BASE + "/api/auth/me",
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read().decode("utf-8")).get("user")
    except Exception:
        return None


def auth_admin_request(path, token, method="GET", payload=None):
    if not AUTH_INTERNAL_TOKEN:
        raise RuntimeError("未配置内部点数接口密钥")
    data = None
    headers = {
        "Authorization": "Bearer " + (token or ""),
        "X-HQ-Internal-Token": AUTH_INTERNAL_TOKEN,
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(AUTH_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
        err = RuntimeError(body.get("detail") or "auth admin request failed")
        err.status = e.code
        err.body = body
        raise err


def auth_error_response(handler, exc):
    status = int(getattr(exc, "status", 502) or 502)
    body = getattr(exc, "body", None) or {"detail": str(exc)[:180]}
    return handler._send(status, body)


def _read_env_file(path):
    values = {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def env_sources():
    sources = [{"name": "process env", "values": dict(os.environ)}]
    for path in ENV_FILES:
        values = _read_env_file(path)
        if values:
            sources.append({"name": str(path), "values": values})
    return sources


def key_status():
    sources = env_sources()
    items = []
    for item in KEY_GROUPS:
        found = []
        for env_name in item["env"]:
            for src in sources:
                value = (src["values"].get(env_name) or "").strip()
                if value:
                    found.append({"env": env_name, "source": src["name"]})
                    break
        configured = len(found) == len(item["env"])
        if item["key"] in {"runninghub", "tikhub", "doubao"}:
            configured = bool(found)
        items.append(
            {
                "key": item["key"],
                "name": item["name"],
                "configured": configured,
                "required_env": item["env"],
                "sources": found,
            }
        )
    return items


def probe_service(svc):
    start = time.time()
    out = dict(svc)
    out.pop("health_url", None)
    try:
        req = urllib.request.Request(svc["health_url"])
        with urllib.request.urlopen(req, timeout=3) as r:
            raw = r.read(4096)
        latency = int((time.time() - start) * 1000)
        detail = {}
        try:
            detail = json.loads(raw.decode("utf-8"))
        except Exception:
            detail = {"raw": raw.decode("utf-8", "ignore")[:160]}
        out.update({"online": True, "status": "online", "latency_ms": latency, "detail": detail})
    except urllib.error.HTTPError as e:
        out.update(
            {
                "online": False,
                "status": "offline",
                "latency_ms": int((time.time() - start) * 1000),
                "error": "HTTP %s" % e.code,
            }
        )
    except Exception as e:
        out.update(
            {
                "online": False,
                "status": "offline",
                "latency_ms": int((time.time() - start) * 1000),
                "error": str(e)[:180],
            }
        )
    return out


def service_status():
    return [probe_service(svc) for svc in SERVICES]


def load_channels():
    saved = {}
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM admin_channel_config").fetchall()
    for row in rows:
        try:
            config = json.loads(row["config"] or "{}")
        except Exception:
            config = {}
        saved[row["channel"]] = {
            "enabled": bool(row["enabled"]),
            "config": config,
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
        }
    keys = {item["key"]: item for item in key_status()}
    items = []
    for channel, meta in CHANNELS.items():
        item = saved.get(channel, {})
        items.append(
            {
                "key": channel,
                "name": meta["name"],
                "enabled": bool(item.get("enabled", True)),
                "config": item.get("config") or meta["default_config"],
                "configured": bool(keys.get(channel, {}).get("configured")),
                "updated_by": item.get("updated_by"),
                "updated_at": item.get("updated_at"),
            }
        )
    return items


def load_features(services=None):
    if feature_flags is None:
        return []
    return feature_flags.list_features(services or service_status())


def _validate_config(value, prefix="config"):
    if not isinstance(value, dict):
        raise ValueError("config must be an object")
    clean = {}
    for key, val in value.items():
        key = str(key).strip()
        if not key:
            continue
        if SECRET_RE.search(key):
            raise ValueError("%s.%s cannot contain secret fields" % (prefix, key))
        if isinstance(val, dict):
            clean[key] = _validate_config(val, "%s.%s" % (prefix, key))
        elif isinstance(val, (str, int, float, bool)) or val is None:
            clean[key] = val
        else:
            raise ValueError("%s.%s must be scalar or object" % (prefix, key))
    return clean


def save_channel(actor, body):
    channel = str(body.get("channel") or body.get("key") or "").strip()
    if channel not in CHANNELS:
        raise ValueError("unknown channel")
    enabled = bool(body.get("enabled"))
    config = _validate_config(body.get("config") or {})
    reason = str(body.get("reason") or "").strip()[:200]
    now = int(time.time())
    detail = {"enabled": enabled, "config": config, "reason": reason}
    with closing(db()) as c:
        c.execute(
            """INSERT INTO admin_channel_config(channel, enabled, config, updated_by, updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(channel) DO UPDATE SET
                   enabled=excluded.enabled,
                   config=excluded.config,
                   updated_by=excluded.updated_by,
                   updated_at=excluded.updated_at""",
            (channel, 1 if enabled else 0, json.dumps(config, ensure_ascii=False), actor, now),
        )
        c.execute(
            "INSERT INTO admin_audit(actor, action, target, detail, created_at) VALUES(?,?,?,?,?)",
            (actor, "channel.save", channel, json.dumps(detail, ensure_ascii=False), now),
        )
        c.commit()
    return next(item for item in load_channels() if item["key"] == channel)


def save_feature(actor, body):
    if feature_flags is None:
        raise RuntimeError("feature flags unavailable")
    feature = str(body.get("feature") or body.get("key") or "").strip()
    enabled = bool(body.get("enabled"))
    reason = str(body.get("reason") or "").strip()[:200]
    item = feature_flags.set_enabled(feature, enabled, actor)
    now = int(time.time())
    detail = {"enabled": enabled, "reason": reason}
    with closing(db()) as c:
        c.execute(
            "INSERT INTO admin_audit(actor, action, target, detail, created_at) VALUES(?,?,?,?,?)",
            (actor, "feature.toggle", feature, json.dumps(detail, ensure_ascii=False), now),
        )
        c.commit()
    return item


def _empty_stats(message=None):
    return {
        "days": 7,
        "total": 0,
        "by_kind": [],
        "trend": [],
        "high_failure": [],
        "message": message,
    }


def job_stats(days=7):
    days = max(1, min(int(days or 7), 90))
    if not JOB_DB.exists():
        data = _empty_stats("content_jobs.db not found")
        data["days"] = days
        return data
    since = int(time.time()) - days * 86400
    with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            """SELECT kind, status, COUNT(*) AS n
               FROM jobs WHERE created_at >= ?
               GROUP BY kind, status ORDER BY kind, status""",
            (since,),
        ).fetchall()
        trend_rows = c.execute(
            """SELECT date(created_at, 'unixepoch') AS day, kind, status, COUNT(*) AS n
               FROM jobs WHERE created_at >= ?
               GROUP BY day, kind, status ORDER BY day, kind""",
            (since,),
        ).fetchall()
    by_kind = {}
    total = 0
    for row in rows:
        kind = row["kind"] or "unknown"
        status = row["status"] or "unknown"
        count = int(row["n"] or 0)
        total += count
        bucket = by_kind.setdefault(kind, {"kind": kind, "total": 0, "done": 0, "error": 0, "running": 0, "other": 0})
        bucket["total"] += count
        if status == "done":
            bucket["done"] += count
        elif status == "error":
            bucket["error"] += count
        elif status in {"queued", "running"}:
            bucket["running"] += count
        else:
            bucket["other"] += count
    items = []
    high_failure = []
    for item in by_kind.values():
        success_rate = item["done"] / item["total"] if item["total"] else 0
        failure_rate = item["error"] / item["total"] if item["total"] else 0
        item["success_rate"] = round(success_rate, 4)
        item["failure_rate"] = round(failure_rate, 4)
        items.append(item)
        if item["total"] >= 3 and failure_rate >= 0.5:
            high_failure.append(item)
    trend = [
        {"day": row["day"], "kind": row["kind"], "status": row["status"], "count": int(row["n"] or 0)}
        for row in trend_rows
    ]
    return {
        "days": days,
        "total": total,
        "by_kind": sorted(items, key=lambda x: x["total"], reverse=True),
        "trend": trend,
        "high_failure": sorted(high_failure, key=lambda x: x["failure_rate"], reverse=True),
    }


def call_logs(days=7, limit=200):
    days = max(1, min(int(days or 7), 90))
    limit = max(1, min(int(limit or 200), 500))
    if not JOB_DB.exists():
        return {"days": days, "limit": limit, "items": [], "message": "content_jobs.db not found"}
    since = int(time.time()) - days * 86400
    with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            """SELECT id, username, kind, cost, status, created_at, updated_at
               FROM jobs
               WHERE created_at >= ?
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (since, limit),
        ).fetchall()
    items = []
    for row in rows:
        created_at = int(row["created_at"] or 0)
        updated_at = int(row["updated_at"] or 0)
        duration = None
        if created_at and updated_at and updated_at >= created_at:
            duration = updated_at - created_at
        items.append(
            {
                "id": row["id"],
                "username": row["username"] or "-",
                "kind": row["kind"] or "unknown",
                "cost": int(row["cost"] or 0),
                "status": row["status"] or "unknown",
                "created_at": created_at,
                "updated_at": updated_at,
                "duration_sec": duration,
            }
        )
    return {"days": days, "limit": limit, "items": items}


class H(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token(self):
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth.lower().startswith("bearer ") else ""

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            raise ValueError("请求体不是合法 JSON")

    def _admin(self):
        user = verify(self._token())
        if not user:
            self._send(401, {"detail": "未登录或登录已过期"})
            return None
        if user.get("role") != "admin":
            self._send(403, {"detail": "需要管理员权限"})
            return None
        return user

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/admin/"):
            return self._send(404, {"detail": "not found"})
        user = self._admin()
        if not user:
            return
        if path == "/api/admin/health":
            return self._send(200, {"ok": True, "service": "huangque-admin"})
        if path == "/api/admin/services":
            return self._send(200, {"items": service_status()})
        if path == "/api/admin/keys":
            return self._send(200, {"items": key_status()})
        if path == "/api/admin/channels":
            return self._send(200, {"items": load_channels()})
        if path == "/api/admin/features":
            return self._send(200, {"items": load_features()})
        if path == "/api/admin/users":
            q = urllib.parse.urlparse(self.path).query
            suffix = "/api/auth/admin/users" + (("?" + q) if q else "")
            try:
                return self._send(200, auth_admin_request(suffix, self._token()))
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/points/audit":
            q = urllib.parse.urlparse(self.path).query
            suffix = "/api/auth/admin/points/audit" + (("?" + q) if q else "")
            try:
                return self._send(200, auth_admin_request(suffix, self._token()))
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/recharge/orders":
            q = urllib.parse.urlparse(self.path).query
            suffix = "/api/auth/admin/recharge/orders" + (("?" + q) if q else "")
            try:
                return self._send(200, auth_admin_request(suffix, self._token()))
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/stats":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(200, job_stats((q.get("days") or ["7"])[0]))
        if path == "/api/admin/call-logs":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(
                200,
                call_logs(
                    (q.get("days") or ["7"])[0],
                    (q.get("limit") or ["200"])[0],
                ),
            )
        if path == "/api/admin/overview":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            services = service_status()
            days = (q.get("days") or ["7"])[0]
            return self._send(
                200,
                {
                    "ok": True,
                    "user": {"username": user.get("username"), "name": user.get("name"), "role": user.get("role")},
                    "services": services,
                    "keys": key_status(),
                    "channels": load_channels(),
                    "features": load_features(services),
                    "stats": job_stats(days),
                    "call_logs": call_logs(days, 200),
                },
            )
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/admin/"):
            return self._send(404, {"detail": "not found"})
        user = self._admin()
        if not user:
            return
        if path == "/api/admin/channel":
            try:
                item = save_channel(user.get("username") or "admin", self._body())
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception:
                return self._send(500, {"detail": "保存失败"})
            return self._send(200, {"ok": True, "channel": item})
        if path == "/api/admin/features/toggle":
            try:
                item = save_feature(user.get("username") or "admin", self._body())
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception as e:
                return self._send(500, {"detail": str(e)[:160] or "保存失败"})
            return self._send(200, {"ok": True, "feature": item})
        if path == "/api/admin/points/adjust":
            try:
                return self._send(
                    200,
                    auth_admin_request("/api/auth/admin/points/adjust", self._token(), method="POST", payload=self._body()),
                )
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/recharge/review":
            try:
                return self._send(
                    200,
                    auth_admin_request("/api/auth/admin/recharge/review", self._token(), method="POST", payload=self._body()),
                )
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception as e:
                return auth_error_response(self, e)
        return self._send(404, {"detail": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()


if __name__ == "__main__":
    init_db()
    print("huangque-admin on 127.0.0.1:%d" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
