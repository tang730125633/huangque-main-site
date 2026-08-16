# -*- coding: utf-8 -*-
"""Encrypted provider API-key pool shared by admin and content services."""

import base64
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PROVIDERS = {"xai", "sora", "seedance", "omni", "minimax"}
ENV_KEYS = {
    "xai": "XAI_API_KEY",
    "sora": "OPENAI_API_KEY",
    "seedance": "ARK_API_KEY",
    "omni": "GEMINI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
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
                conn.execute(
                    """INSERT INTO provider_api_keys(
                        id,provider,label,last4,ciphertext,nonce,priority,state,
                        health_status,last_checked_at,last_latency_ms,last_error,
                        created_by,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,0,'active','unknown',NULL,NULL,'',?,?,?)""",
                    (
                        key_id,
                        provider,
                        "服务器环境变量（已加密托管）",
                        secret[-4:],
                        ciphertext,
                        nonce,
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


def add_key(provider, label, secret, actor, health=None):
    provider = _provider(provider)
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
                       SET label=?,priority=?,state='active',health_status=?,
                           last_checked_at=?,last_latency_ms=?,last_error='',updated_at=?
                       WHERE id=?""",
                    (
                        label,
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
                    id,provider,label,last4,ciphertext,nonce,priority,state,
                    health_status,last_checked_at,last_latency_ms,last_error,
                    created_by,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?)""",
                (
                    key_id,
                    provider,
                    label,
                    secret[-4:],
                    ciphertext,
                    nonce,
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
            {"id": row["id"], "provider": provider, "secret": _decrypt(row)}
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
    return {"id": row["id"], "provider": provider, "secret": secret}


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
