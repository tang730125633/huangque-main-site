# -*- coding: utf-8 -*-
"""Encrypted provider API-key pool shared by admin and content services."""

import base64
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
from contextlib import closing
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PROVIDERS = {"xai", "deepseek", "sora", "seedance", "omni", "minimax"}
ENV_KEYS = {
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "sora": "OPENAI_API_KEY",
    "seedance": "ARK_API_KEY",
    "omni": "GEMINI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
}
BASE_URLS = {
    "xai": "https://api.x.ai/v1",
    "deepseek": "https://api.deepseek.com",
    "sora": "https://api.openai.com",
    "seedance": "https://ark.cn-beijing.volces.com/api/v3",
    "omni": "https://generativelanguage.googleapis.com",
    "minimax": "https://metaso.cn/api/minimax",
}
BASE_URL_ENVS = {
    "xai": ("XAI_API_BASE",),
    "deepseek": ("DEEPSEEK_API_BASE",),
    "sora": ("OPENAI_BASE",),
    "seedance": ("ARK_BASE",),
    "omni": ("GEMINI_OMNI_BASE", "GEMINI_BASE"),
    "minimax": (),
}
OFFICIAL_BASE_HOSTS = {
    "xai": {"api.x.ai"},
    "deepseek": {"api.deepseek.com"},
    "sora": {"api.openai.com"},
    "seedance": {"ark.cn-beijing.volces.com"},
    "omni": {"generativelanguage.googleapis.com"},
    # New MiniMax jobs are deliberately pinned to MetaSo. Historical jobs
    # retain their origin marker and do not use this configurable endpoint.
    "minimax": {"metaso.cn"},
}
DB_PATH = Path(
    os.environ.get(
        "ADMIN_DB",
        str(Path(__file__).resolve().parent.parent / "admin_config.db"),
    )
)
MASTER_KEY_ENV = "HQ_PROVIDER_KEYS_MASTER_KEY"
_LEGACY_IMPORT_LOCK = threading.Lock()
_LEGACY_IMPORT_PATHS = set()
_RUNTIME_HEALTH_LOCK = threading.Lock()
_RUNTIME_UNHEALTHY_UNTIL = {}
_RUNTIME_UNHEALTHY_SECONDS = 300


class KeyStoreUnavailable(RuntimeError):
    pass


def _environment_base_url(provider):
    for name in BASE_URL_ENVS[provider]:
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return BASE_URLS[provider]


def normalize_base_url(provider, value=None):
    """Validate one immutable provider-line endpoint without making a request."""
    provider = _provider(provider)
    supplied = str(value or "").strip()
    from_environment = not supplied
    value = str(supplied or _environment_base_url(provider)).strip().rstrip("/")
    if not value:
        raise ValueError("Base URL 不能为空")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Base URL 必须是有效的 HTTPS 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URL 不能包含账号、密码、查询参数或片段")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Base URL 端口无效") from exc
    if port not in (None, 443):
        raise ValueError("Base URL 仅允许 HTTPS 443 端口")
    host = parsed.hostname.rstrip(".").lower()
    blocked = (
        host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
        or host.replace(".", "").isdigit()
        or ":" in host
    )
    if blocked:
        raise ValueError("Base URL 不允许指向本机、内网或 IP 地址")
    configured_hosts = {
        item.strip().rstrip(".").lower()
        for item in str(os.environ.get("HQ_PROVIDER_BASE_HOST_ALLOWLIST") or "").split(",")
        if item.strip()
    }
    # MiniMax paid submissions intentionally stay on the audited MetaSo route;
    # a generic custom-host allowlist must not weaken that origin pin.
    allowed_hosts = OFFICIAL_BASE_HOSTS[provider] | (
        set() if provider == "minimax" else configured_hosts
    )
    # Existing environment files are root-managed deployment configuration.
    # Preserve them during one-time migration while keeping browser-submitted
    # custom hosts behind an explicit server allowlist.
    if not from_environment and host not in allowed_hosts:
        raise ValueError("Base URL 域名未在服务器允许名单中")
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _provider(value):
    value = str(value or "").strip().lower()
    if value not in PROVIDERS:
        raise ValueError("不支持的视频渠道")
    return value


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        conn.close()
        raise
    return conn


def init_db():
    with closing(_connect()) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS provider_api_keys(
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                label TEXT NOT NULL,
                last4 TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                nonce BLOB NOT NULL,
                base_url TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'active',
                health_status TEXT NOT NULL DEFAULT 'unknown',
                last_checked_at INTEGER,
                last_latency_ms INTEGER,
                last_error TEXT,
                use_count INTEGER NOT NULL DEFAULT 0,
                last_used_at INTEGER,
                created_by TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )"""
        )
        columns = {
            row["name"] for row in conn.execute(
                "PRAGMA table_info(provider_api_keys)"
            ).fetchall()
        }
        if "use_count" not in columns:
            conn.execute(
                "ALTER TABLE provider_api_keys "
                "ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0"
            )
        if "last_used_at" not in columns:
            conn.execute(
                "ALTER TABLE provider_api_keys ADD COLUMN last_used_at INTEGER"
            )
        if "base_url" not in columns:
            conn.execute(
                "ALTER TABLE provider_api_keys ADD COLUMN base_url TEXT NOT NULL DEFAULT ''"
            )
        # Freeze the endpoint for keys created before this column existed.
        # Doing this in the migration transaction prevents a later env change
        # from moving an already-paid task to a different upstream origin.
        for provider in PROVIDERS:
            conn.execute(
                """UPDATE provider_api_keys SET base_url=?
                   WHERE provider=? AND (base_url IS NULL OR base_url='')""",
                (normalize_base_url(provider), provider),
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS provider_api_keys_active "
            "ON provider_api_keys(provider,state,health_status,priority,id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS provider_api_keys_rotation "
            "ON provider_api_keys(provider,state,health_status,use_count,priority,id)"
        )
        conn.commit()
    _snapshot_legacy_env_keys()


def _master_key():
    value = str(os.environ.get(MASTER_KEY_ENV) or "").strip()
    if not value:
        raise KeyStoreUnavailable("后台密钥保险箱尚未配置")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise KeyStoreUnavailable("后台密钥保险箱配置无效") from exc
    if len(raw) != 32:
        raise KeyStoreUnavailable("后台密钥保险箱配置无效")
    return raw


def vault_ready():
    try:
        _master_key()
        return True
    except KeyStoreUnavailable:
        return False


def _aad(provider, key_id):
    return ("huangque-provider-key:%s:%s" % (provider, key_id)).encode("utf-8")


def _encrypt(provider, key_id, secret):
    nonce = os.urandom(12)
    ciphertext = AESGCM(_master_key()).encrypt(
        nonce, secret.encode("utf-8"), _aad(provider, key_id)
    )
    return ciphertext, nonce


def _snapshot_legacy_env_keys():
    """Import each existing env key once so future task resumes cannot switch keys."""
    path = str(DB_PATH)
    with _LEGACY_IMPORT_LOCK:
        if path in _LEGACY_IMPORT_PATHS:
            return
        values = {
            provider: str(os.environ.get(env_name) or "").strip()
            for provider, env_name in ENV_KEYS.items()
        }
        if not vault_ready() or not any(values.values()):
            _LEGACY_IMPORT_PATHS.add(path)
            return
        now = int(time.time())
        with closing(_connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for provider, secret in values.items():
                if not secret or conn.execute(
                    """SELECT 1 FROM provider_api_keys
                       WHERE provider=? AND created_by='system-env-migration'
                       LIMIT 1""",
                    (provider,),
                ).fetchone():
                    continue
                key_id = secrets.token_urlsafe(12)
                ciphertext, nonce = _encrypt(provider, key_id, secret)
                base_url = normalize_base_url(provider)
                conn.execute(
                    """INSERT INTO provider_api_keys(
                        id,provider,label,last4,ciphertext,nonce,base_url,priority,state,
                        health_status,last_checked_at,last_latency_ms,last_error,
                        created_by,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,0,'active','unknown',NULL,NULL,'',?,?,?)""",
                    (
                        key_id,
                        provider,
                        "服务器环境变量（已加密托管）",
                        secret[-4:],
                        ciphertext,
                        nonce,
                        base_url,
                        "system-env-migration",
                        now,
                        now,
                    ),
                )
            conn.commit()
        _LEGACY_IMPORT_PATHS.add(path)


def _decrypt(row):
    try:
        raw = AESGCM(_master_key()).decrypt(
            bytes(row["nonce"]),
            bytes(row["ciphertext"]),
            _aad(row["provider"], row["id"]),
        )
        return raw.decode("utf-8")
    except KeyStoreUnavailable:
        raise
    except Exception as exc:
        raise KeyStoreUnavailable("后台密钥无法解密，请检查保险箱配置") from exc


def add_key(provider, label, secret, actor, health=None, base_url=None):
    provider = _provider(provider)
    base_url = normalize_base_url(provider, base_url)
    label = str(label or "").strip()[:60] or (provider + " 线路")
    secret = str(secret or "").strip()
    if len(secret) < 8 or len(secret) > 4096:
        raise ValueError("API 密钥格式无效")
    actor = str(actor or "admin").strip()[:80] or "admin"
    init_db()
    key_id = secrets.token_urlsafe(12)
    ciphertext, nonce = _encrypt(provider, key_id, secret)
    now = int(time.time())
    health = health or {}
    restored_id = None
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM provider_api_keys WHERE provider=?", (provider,)
        ).fetchall()
        priority = 1 + int(
            conn.execute(
                "SELECT COALESCE(MAX(priority),0) FROM provider_api_keys WHERE provider=?",
                (provider,),
            ).fetchone()[0]
        )
        for row in rows:
            if _decrypt(row) == secret:
                if row["state"] != "retired":
                    raise ValueError("该 API 密钥已经添加")
                restored_id = row["id"]
                conn.execute(
                    """UPDATE provider_api_keys
                       SET label=?,base_url=?,priority=?,state='active',health_status=?,
                           last_checked_at=?,last_latency_ms=?,last_error='',updated_at=?
                       WHERE id=?""",
                    (
                        label,
                        base_url,
                        priority,
                        "healthy" if health.get("ok") else "unknown",
                        now if health else None,
                        health.get("latency_ms"),
                        now,
                        restored_id,
                    ),
                )
                break
        if restored_id is None:
            conn.execute(
                """INSERT INTO provider_api_keys(
                    id,provider,label,last4,ciphertext,nonce,base_url,priority,state,
                    health_status,last_checked_at,last_latency_ms,last_error,
                    created_by,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?)""",
                (
                    key_id,
                    provider,
                    label,
                    secret[-4:],
                    ciphertext,
                    nonce,
                    base_url,
                    priority,
                    "healthy" if health.get("ok") else "unknown",
                    now if health else None,
                    health.get("latency_ms"),
                    str(health.get("error") or "")[:180],
                    actor,
                    now,
                    now,
                ),
            )
        conn.commit()
    return public_key(restored_id or key_id)


def _public(row):
    return {
        "id": row["id"],
        "provider": row["provider"],
        "label": row["label"],
        "last4": row["last4"],
        "base_url": row["base_url"] or normalize_base_url(row["provider"]),
        "priority": row["priority"],
        "state": row["state"],
        "health_status": row["health_status"],
        "last_checked_at": row["last_checked_at"],
        "last_latency_ms": row["last_latency_ms"],
        "last_error": row["last_error"] or "",
        "use_count": int(row["use_count"] or 0),
        "last_used_at": row["last_used_at"],
        "managed": True,
    }


def public_key(key_id):
    init_db()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM provider_api_keys WHERE id=?", (str(key_id),)
        ).fetchone()
    if not row:
        raise ValueError("API 密钥不存在")
    return _public(row)


def list_public():
    init_db()
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM provider_api_keys WHERE state!='retired' "
            "ORDER BY provider,priority,id"
        ).fetchall()
        counts = {
            row["provider"]: row["n"]
            for row in conn.execute(
                "SELECT provider,COUNT(*) AS n FROM provider_api_keys GROUP BY provider"
            ).fetchall()
        }
    items = [_public(row) for row in rows]
    for provider in sorted(PROVIDERS):
        value = str(os.environ.get(ENV_KEYS[provider]) or "").strip()
        if not counts.get(provider) and value:
            items.append(
                {
                    "id": "env",
                    "provider": provider,
                    "label": "服务器环境变量（兼容线路）",
                    "last4": value[-4:],
                    "base_url": normalize_base_url(provider),
                    "priority": 0,
                    "state": "active",
                    "health_status": "unknown",
                    "last_checked_at": None,
                    "last_latency_ms": None,
                    "last_error": "",
                    "use_count": 0,
                    "last_used_at": None,
                    "managed": False,
                }
            )
    for provider in PROVIDERS:
        active = [
            item for item in items
            if item["provider"] == provider
            and item["state"] == "active"
            and item["health_status"] != "unhealthy"
        ]
        if not active:
            continue
        next_item = min(
            active,
            key=lambda item: (item["use_count"], item["priority"], item["id"]),
        )
        used = [item for item in active if item.get("last_used_at")]
        current = max(
            used,
            key=lambda item: (item["last_used_at"], item["id"]),
        ) if used else next_item
        next_item["next_in_rotation"] = True
        current["current"] = True
    return sorted(
        items,
        key=lambda item: (
            item["provider"], item["use_count"], item["priority"], item["id"]
        ),
    )


def candidates(provider, preferred_id=None):
    """Return decrypted candidates; a preferred id is also allowed after retirement."""
    provider = _provider(provider)
    init_db()
    with closing(_connect()) as conn:
        if preferred_id and str(preferred_id) != "env":
            rows = conn.execute(
                "SELECT * FROM provider_api_keys WHERE id=? AND provider=?",
                (str(preferred_id), provider),
            ).fetchall()
        elif preferred_id == "env":
            rows = conn.execute(
                """SELECT * FROM provider_api_keys
                   WHERE provider=? AND created_by='system-env-migration'
                   ORDER BY created_at,id LIMIT 1""",
                (provider,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM provider_api_keys
                   WHERE provider=? AND state='active' AND health_status!='unhealthy'
                   ORDER BY use_count,priority,id""",
                (provider,),
            ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM provider_api_keys WHERE provider=?", (provider,)
        ).fetchone()[0]
    if preferred_id and str(preferred_id) != "env" and not rows:
        raise KeyStoreUnavailable("任务绑定的 API 密钥已不存在")
    if rows and not preferred_id:
        rows = [row for row in rows if row["id"] not in _runtime_blocked_ids()]
    if rows:
        return [
            {
                "id": row["id"],
                "provider": provider,
                "secret": _decrypt(row),
                "base_url": row["base_url"] or normalize_base_url(provider),
            }
            for row in rows
        ]
    value = str(os.environ.get(ENV_KEYS[provider]) or "").strip()
    if preferred_id == "env":
        raise KeyStoreUnavailable("任务绑定的旧 API 密钥没有加密快照，已停止自动恢复")
    if not total and value:
        raise KeyStoreUnavailable("视频密钥保险箱未配置，已停止新付费任务")
    return []


def has_candidate(provider):
    try:
        return bool(candidates(provider))
    except KeyStoreUnavailable:
        return False


def _runtime_blocked_ids():
    now = time.monotonic()
    with _RUNTIME_HEALTH_LOCK:
        expired = [
            key_id for key_id, until in _RUNTIME_UNHEALTHY_UNTIL.items()
            if until <= now
        ]
        for key_id in expired:
            _RUNTIME_UNHEALTHY_UNTIL.pop(key_id, None)
        return set(_RUNTIME_UNHEALTHY_UNTIL)


def claim_candidate(provider):
    """Atomically claim the least-used healthy key for one upstream attempt."""
    provider = _provider(provider)
    init_db()
    blocked = _runtime_blocked_ids()
    now = int(time.time())
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT * FROM provider_api_keys
               WHERE provider=? AND state='active' AND health_status!='unhealthy'
               ORDER BY use_count,priority,id""",
            (provider,),
        ).fetchall()
        row = next((item for item in rows if item["id"] not in blocked), None)
        total = conn.execute(
            "SELECT COUNT(*) FROM provider_api_keys WHERE provider=?", (provider,)
        ).fetchone()[0]
        if not row:
            conn.rollback()
            value = str(os.environ.get(ENV_KEYS[provider]) or "").strip()
            if not total and value:
                raise KeyStoreUnavailable("视频密钥保险箱未配置，已停止新付费任务")
            return None
        secret = _decrypt(row)
        conn.execute(
            """UPDATE provider_api_keys
               SET use_count=use_count+1,last_used_at=? WHERE id=?""",
            (now, row["id"]),
        )
        conn.commit()
    return {
        "id": row["id"],
        "provider": provider,
        "secret": secret,
        "base_url": row["base_url"] or normalize_base_url(provider),
    }


def reveal_key(key_id):
    init_db()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM provider_api_keys WHERE id=? AND state!='retired'",
            (str(key_id),),
        ).fetchone()
    if not row:
        raise ValueError("API 密钥不存在")
    return _decrypt(row)


def set_health(key_id, ok, latency_ms=None, error=""):
    if str(key_id) == "env":
        return None
    with _RUNTIME_HEALTH_LOCK:
        if ok:
            _RUNTIME_UNHEALTHY_UNTIL.pop(str(key_id), None)
        else:
            _RUNTIME_UNHEALTHY_UNTIL[str(key_id)] = (
                time.monotonic() + _RUNTIME_UNHEALTHY_SECONDS
            )
    now = int(time.time())
    with closing(_connect()) as conn:
        cur = conn.execute(
            """UPDATE provider_api_keys
               SET health_status=?,last_checked_at=?,last_latency_ms=?,last_error=?,updated_at=?
               WHERE id=?""",
            (
                "healthy" if ok else "unhealthy",
                now,
                int(latency_ms) if latency_ms is not None else None,
                str(error or "")[:180],
                now,
                str(key_id),
            ),
        )
        conn.commit()
    return public_key(key_id) if cur.rowcount else None


def retire_key(key_id):
    if str(key_id) == "env":
        raise ValueError("服务器环境变量不能在网页中删除")
    now = int(time.time())
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE provider_api_keys SET state='retired',updated_at=? WHERE id=? AND state!='retired'",
            (now, str(key_id)),
        )
        conn.commit()
    if cur.rowcount != 1:
        raise ValueError("API 密钥不存在")
    return True
