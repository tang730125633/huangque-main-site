# -*- coding: utf-8 -*-
"""环境变量型线路的「后台覆盖配置」——版本化存储 + 统一解析入口。

背景
----
现状：`image.py` / `core.py` / `video.py` / `wavespeed.py` 等模块在 **import 时**
把 `os.environ` 读成模块级常量，所以改 Key/URL 必须改服务器环境变量并重启服务。
本模块提供「后台配置优先、原环境变量兜底」的版本化覆盖层，业务方改为调用
``resolve(target_id)``，即可在不重启的情况下让新任务用上新配置。

硬规则（与改造方案一致）
------------------------
1. **URL 与 Key 作为同一个版本发布**，不允许先换 URL 再换 Key。
2. **验证证据绑定版本**：改了字段就是新版本，旧证据自动不适用。
3. **已发布后台配置后绝不静默回退环境变量**：读取/解密失败一律抛错。
4. **票面不允许出现明文 Key**：状态接口只给 `key_present` / `key_last4`。
5. 发布必须带 `expected_seq`（防两人互覆盖）与 `op_id`（防重复点击），且为原子操作。

存储
----
沿用 `provider_keys.py` 的 `admin_config.db`（`ADMIN_DB`）与主密钥
`HQ_PROVIDER_KEYS_MASTER_KEY`（AESGCM）。当前只实现 SQLite；当
``HQ_ADMIN_CONFIG_STORE=postgres`` 时**显式拒绝**（fail-closed），避免两处权威。

这个模块默认不生效：只有业务方调用 ``resolve()`` 且存在 published 版本时才覆盖。
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
import urllib.parse
from contextlib import closing
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MASTER_KEY_ENV = "HQ_PROVIDER_KEYS_MASTER_KEY"
CACHE_TTL_SECONDS = 5.0  # ≤10s 生效目标下，进程内缓存的保守上限

SOURCE_ENV = "env"
SOURCE_BACKEND = "backend"

STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"
STATUS_SUPERSEDED = "superseded"
STATUS_REVOKED = "revoked"

# ---------------------------------------------------------------------------
# 目标线路登记表
# ---------------------------------------------------------------------------
# ``pool_provider`` 非空表示：这个环境变量同时被号池当作运行兜底/快照来源
# （见 provider_keys.ENV_KEYS）。这类线路的影响范围会跨图片/视频，UI 必须显式提示。
TARGETS = {
    "image.banana.nb2": {
        "provider": "gemini", "kind": "image",
        "features": ["图片生成 → 纳米香蕉 2"],
        "env_keys": ("GEMINI_API_KEY",),
        "url_env": ("GEMINI_OFFICIAL_BASE", "GEMINI_BASE"),
        "url_default": "https://generativelanguage.googleapis.com",
        "pool_provider": "omni",
        "url_allowlist_env": "HQ_PROVIDER_BASE_HOST_ALLOWLIST",
    },
    "image.seedream": {
        "provider": "seedance", "kind": "image",
        "features": ["图片生成 → 黄雀引擎 1（Seedream）"],
        "env_keys": ("ARK_API_KEY",),
        "url_env": ("ARK_BASE",),
        "url_default": "https://ark.cn-beijing.volces.com/api/v3",
        "pool_provider": "seedance",
        "url_allowlist_env": "HQ_PROVIDER_BASE_HOST_ALLOWLIST",
    },
    "image.openai": {
        "provider": "openai", "kind": "image",
        "features": ["图片生成 → 黄雀引擎 2"],
        "env_keys": ("OPENAI_API_KEY",),
        "url_env": ("OPENAI_OFFICIAL_BASE", "OPENAI_BASE"),
        "url_default": "https://api.openai.com",
        "pool_provider": "sora",
        "url_allowlist_env": "HQ_PROVIDER_BASE_HOST_ALLOWLIST",
    },
    "image.xiaole": {
        "provider": "xiaolevideo", "kind": "image",
        "features": ["图片生成 → 果肉生图"],
        "env_keys": ("XIAOLEVIDEO_API_KEY",),
        "url_env": ("XIAOLEVIDEO_API_BASE",),
        "url_default": "https://api.xiaolevideo.cn",
        "pool_provider": "",
        "url_allowlist_env": "HQ_PROVIDER_BASE_HOST_ALLOWLIST",
    },
    "video.tryon.classic": {
        "provider": "runninghub", "kind": "video",
        "features": ["视频模块 → 换装换背景 · 线路一"],
        "env_keys": ("RUNNINGHUB_API_KEY", "RUNNINGHUB_KEY"),
        "url_env": (),
        "url_default": "",
        "pool_provider": "",
        "url_allowlist_env": "HQ_PROVIDER_BASE_HOST_ALLOWLIST",
    },
    "video.tryon.fast": {
        "provider": "wavespeed", "kind": "video",
        "features": ["视频模块 → 换装换背景 · 线路二"],
        "env_keys": ("WAVESPEED_API_KEY",),
        "url_env": (),
        "url_default": "",
        "pool_provider": "",
        "url_allowlist_env": "HQ_PROVIDER_BASE_HOST_ALLOWLIST",
    },
}

_ALLOWED_URL_PORT = (None, 443)


class ProviderConfigError(RuntimeError):
    """可预期的业务错误（校验失败、版本冲突、未验证等）。"""


class ProviderConfigUnavailable(ProviderConfigError):
    """存储/密钥不可用；调用方必须 fail-closed，不得回退环境变量。"""


class VersionConflict(ProviderConfigError):
    """``expected_seq`` 与当前发布版本不一致（并发修改）。"""


class NotVerified(ProviderConfigError):
    """该版本还没有可用验证证据，禁止发布。"""


def _db_path() -> Path:
    return Path(
        os.environ.get(
            "ADMIN_DB",
            str(Path(__file__).resolve().parent.parent / "admin_config.db"),
        )
    )


def _assert_sqlite_authority() -> None:
    mode = (os.environ.get("HQ_ADMIN_CONFIG_STORE") or "sqlite").strip().lower()
    if mode not in ("sqlite", "postgres"):
        raise ProviderConfigUnavailable("HQ_ADMIN_CONFIG_STORE 配置非法")
    if mode != "sqlite":
        raise ProviderConfigUnavailable(
            "provider_config 尚未迁移到 PostgreSQL 权威；"
            "请先以 sqlite 权威上线，勿双写"
        )


def _connect() -> sqlite3.Connection:
    _assert_sqlite_authority()
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        os.chmod(path, 0o600)
    except OSError:
        conn.close()
        raise
    return conn


def init_db() -> None:
    """建表（幂等）。列不可变，只追加新列。"""
    _assert_sqlite_authority()
    with closing(_connect()) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS provider_config_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                seq INTEGER NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                ciphertext BLOB,
                nonce BLOB,
                key_present INTEGER NOT NULL DEFAULT 0,
                key_last4 TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                evidence TEXT,
                evidence_at INTEGER,
                op_id TEXT,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                published_at INTEGER,
                UNIQUE(target_id, seq)
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS provider_config_versions_active "
            "ON provider_config_versions(target_id, status, seq DESC)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS provider_config_versions_op "
            "ON provider_config_versions(target_id, op_id) WHERE op_id IS NOT NULL"
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 密钥封装（与 provider_keys 共用主密钥，AAD 域分开）
# ---------------------------------------------------------------------------

def vault_ready() -> bool:
    try:
        _master_key()
        return True
    except ProviderConfigUnavailable:
        return False


def _master_key() -> bytes:
    value = str(os.environ.get(MASTER_KEY_ENV) or "").strip()
    if not value:
        raise ProviderConfigUnavailable("后台密钥保险箱尚未配置")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:  # noqa: BLE001
        raise ProviderConfigUnavailable("后台密钥保险箱配置无效") from exc
    if len(raw) != 32:
        raise ProviderConfigUnavailable("后台密钥保险箱配置无效")
    return raw


def _aad(target_id: str) -> bytes:
    # 只绑定目标线路，不绑定 seq：同一线路内“沿用旧 Key / 回滚”是合法语义，
    # 需要把旧版本的密文复制到新版本；跨线路重放仍被 AAD 阻断。
    return ("huangque-provider-config:%s" % target_id).encode("utf-8")


def _seal(target_id: str, secret: str):
    nonce = os.urandom(12)
    ciphertext = AESGCM(_master_key()).encrypt(
        nonce, secret.encode("utf-8"), _aad(target_id)
    )
    return ciphertext, nonce


def _open(target_id: str, ciphertext, nonce) -> str:
    try:
        return AESGCM(_master_key()).decrypt(
            bytes(nonce), bytes(ciphertext), _aad(target_id)
        ).decode("utf-8")
    except ProviderConfigUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProviderConfigUnavailable(
            "已发布配置的凭据解密失败；拒绝回退环境变量"
        ) from exc


def _last4(secret: str) -> str:
    return secret[-4:] if len(secret) >= 4 else secret


# ---------------------------------------------------------------------------
# URL 校验（不接受凭据入 URL；只接受 HTTPS 443；默认仅官方域名 + 服务器白名单）
# ---------------------------------------------------------------------------

def validate_url(target_id: str, value) -> str:
    spec = target(target_id)
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise ProviderConfigError("Base URL 不能为空")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ProviderConfigError("Base URL 必须是有效的 HTTPS 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderConfigError("Base URL 不能包含账号、密码、查询参数或片段")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderConfigError("Base URL 端口无效") from exc
    if port not in _ALLOWED_URL_PORT:
        raise ProviderConfigError("Base URL 仅允许 HTTPS 443 端口")
    host = parsed.hostname.rstrip(".").lower()
    if (
        host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
        or host.replace(".", "").isdigit()
        or ":" in host
    ):
        raise ProviderConfigError("Base URL 不允许指向本机或内网地址")
    official = set()
    if spec.get("url_default"):
        official.add(urllib.parse.urlsplit(spec["url_default"]).hostname.lower())
    allowed = official | {
        item.strip().rstrip(".").lower()
        for item in str(os.environ.get(spec["url_allowlist_env"]) or "").split(",")
        if item.strip()
    }
    if host not in allowed:
        raise ProviderConfigError("Base URL 域名未在服务器允许名单中")
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))


def url_hint(target_id: str) -> str:
    """给前端提示：应填域名还是含 /v1、/v3 的基础路径。"""
    spec = target(target_id)
    default = spec.get("url_default") or ""
    return default


# ---------------------------------------------------------------------------
# 登记表读取
# ---------------------------------------------------------------------------

def target(target_id: str) -> dict:
    spec = TARGETS.get(str(target_id or "").strip())
    if not spec:
        raise ProviderConfigError("未登记的配置目标")
    return spec


def targets() -> list:
    out = []
    for target_id, spec in TARGETS.items():
        out.append(
            {
                "target_id": target_id,
                "provider": spec["provider"],
                "kind": spec["kind"],
                "features": list(spec["features"]),
                "env_keys": list(spec["env_keys"]),
                "pool_provider": spec.get("pool_provider", ""),
                "pool_shared": bool(spec.get("pool_provider")),
                "url_default": spec.get("url_default", ""),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 版本读取
# ---------------------------------------------------------------------------

def _rows(sql, params=()):
    with closing(_connect()) as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def active_version(target_id: str):
    target(target_id)
    rows = _rows(
        "SELECT * FROM provider_config_versions "
        "WHERE target_id=? AND status=? ORDER BY seq DESC LIMIT 1",
        (str(target_id), STATUS_PUBLISHED),
    )
    return rows[0] if rows else None


def list_versions(target_id: str) -> list:
    target(target_id)
    rows = _rows(
        "SELECT seq, url, key_present, key_last4, status, evidence, evidence_at, "
        "op_id, actor, reason, created_at, published_at "
        "FROM provider_config_versions WHERE target_id=? ORDER BY seq DESC",
        (str(target_id),),
    )
    for row in rows:
        if row.get("evidence"):
            try:
                row["evidence"] = json.loads(row["evidence"])
            except (TypeError, ValueError):
                row["evidence"] = None
    return rows


def _env_value(names) -> str:
    for name in names:
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _env_url(target_id: str) -> str:
    spec = target(target_id)
    return _env_value(spec.get("url_env", ())) or str(spec.get("url_default") or "")


def status(target_id: str) -> dict:
    """给后台的状态：不含明文 Key。区分配置来源、版本、验证与生效。"""
    spec = target(target_id)
    row = active_version(target_id)
    backend_url = (row or {}).get("url") or ""
    url = backend_url or _env_url(target_id)
    if row:
        key_present = bool(row.get("key_present"))
        source = SOURCE_BACKEND
        version = row["seq"]
        key_last4 = row.get("key_last4") or ""
    else:
        key_present = bool(_env_value(spec["env_keys"]))
        source = SOURCE_ENV
        version = None
        key_last4 = _last4(_env_value(spec["env_keys"])) if key_present else ""
    return {
        "target_id": target_id,
        "provider": spec["provider"],
        "features": list(spec["features"]),
        "env_keys": list(spec["env_keys"]),
        "pool_provider": spec.get("pool_provider", ""),
        "pool_shared": bool(spec.get("pool_provider")),
        "source": source,
        "version": version,
        "url": url,
        "url_default": spec.get("url_default", ""),
        "key_present": key_present,
        "key_last4": key_last4,
        "editable": True,
        "vault_ready": vault_ready(),
    }


# ---------------------------------------------------------------------------
# 草稿 / 验证 / 发布 / 回滚
# ---------------------------------------------------------------------------

def _now(now=None) -> int:
    return int(now if now is not None else time.time())


def _next_seq(conn, target_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS s FROM provider_config_versions WHERE target_id=?",
        (str(target_id),),
    ).fetchone()
    return int(row["s"]) + 1


def save_draft(target_id: str, url=None, secret=None, actor="", reason="",
               now=None) -> dict:
    """保存候选 URL/Key 为**新版本草稿**；不改变生产使用版本。

    ``url`` 为 None 表示沿用当前有效 URL；``secret`` 为空表示沿用当前有效 Key。
    URL 与 Key 永远作为同一个版本；改任一字段都产生新版本（旧验证证据自动失效）。
    """
    spec = target(target_id)
    actor = str(actor or "").strip()
    if not actor:
        raise ProviderConfigError("缺少操作人")
    current = status(target_id)
    candidate_url = current["url"] if url is None else validate_url(target_id, url)
    if not candidate_url:
        raise ProviderConfigError("缺少 Base URL")

    secret_text = str(secret or "").strip()
    reuse = not secret_text
    ts = _now(now)

    with closing(_connect()) as conn:
        seq = _next_seq(conn, target_id)
        ciphertext = nonce = None
        key_present = 0
        key_last4 = ""
        if reuse:
            if current["source"] == SOURCE_BACKEND:
                prev = conn.execute(
                    "SELECT ciphertext, nonce, key_present, key_last4 "
                    "FROM provider_config_versions WHERE target_id=? AND seq=?",
                    (str(target_id), int(current["version"])),
                ).fetchone()
                if prev and prev["key_present"]:
                    ciphertext, nonce = prev["ciphertext"], prev["nonce"]
                    key_present, key_last4 = 1, prev["key_last4"] or ""
            else:
                env_secret = _env_value(spec["env_keys"])
                if env_secret:
                    ciphertext, nonce = _seal(target_id, env_secret)
                    key_present, key_last4 = 1, _last4(env_secret)
        else:
            ciphertext, nonce = _seal(target_id, secret_text)
            key_present, key_last4 = 1, _last4(secret_text)

        if not key_present:
            raise ProviderConfigError("缺少 API Key（留空仅当已有可用 Key）")

        conn.execute(
            "INSERT INTO provider_config_versions("
            "target_id, provider, seq, url, ciphertext, nonce, key_present, key_last4,"
            " status, evidence, evidence_at, op_id, actor, reason, created_at, published_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,NULL)",
            (str(target_id), spec["provider"], seq, candidate_url, ciphertext, nonce,
             key_present, key_last4, STATUS_DRAFT, actor, str(reason or "")[:200], ts),
        )
        conn.commit()
    invalidate(target_id)
    return {"target_id": target_id, "seq": seq, "url": candidate_url,
            "key_present": bool(key_present), "key_last4": key_last4,
            "status": STATUS_DRAFT}


def record_evidence(target_id: str, seq: int, checks: dict, actor="", now=None) -> dict:
    """把验证证据绑定到**指定版本**；只有 ``ok`` 为真才可用于发布。"""
    target(target_id)
    checks = checks or {}
    ok = bool(checks.get("ok"))
    payload = {
        "checks": checks.get("checks") or {},
        "ok": ok,
        "free_verification": bool(checks.get("free_verification")),
        "note": str(checks.get("note") or "")[:300],
        "at": _now(now),
        "actor": str(actor or ""),
    }
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT status FROM provider_config_versions WHERE target_id=? AND seq=?",
            (str(target_id), int(seq)),
        ).fetchone()
        if not row:
            raise ProviderConfigError("配置版本不存在")
        if row["status"] != STATUS_DRAFT:
            raise ProviderConfigError("只能给草稿版本写验证证据")
        conn.execute(
            "UPDATE provider_config_versions SET evidence=?, evidence_at=? "
            "WHERE target_id=? AND seq=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True),
             payload["at"], str(target_id), int(seq)),
        )
        conn.commit()
    return payload


def publish(target_id: str, seq: int, expected_seq, op_id, actor="", now=None) -> dict:
    """原子发布：校验证据 → 冲突检查 → supersede 旧版本 → 置 published。

    ``op_id`` 幂等：同一 target 重复调用返回首次结果，不产生第二次发布。
    """
    target(target_id)
    op_id = str(op_id or "").strip()
    if not op_id:
        raise ProviderConfigError("缺少操作 ID")
    actor = str(actor or "").strip()
    if not actor:
        raise ProviderConfigError("缺少操作人")
    ts = _now(now)

    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            done = conn.execute(
                "SELECT seq, url, status FROM provider_config_versions "
                "WHERE target_id=? AND op_id=?",
                (str(target_id), op_id),
            ).fetchone()
            if done:
                conn.commit()
                return {"target_id": target_id, "seq": done["seq"], "url": done["url"],
                        "status": done["status"], "idempotent": True}

            current = conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS s FROM provider_config_versions "
                "WHERE target_id=? AND status=?",
                (str(target_id), STATUS_PUBLISHED),
            ).fetchone()
            current_seq = int(current["s"]) or None
            want = None if expected_seq in (None, "", 0, "0") else int(expected_seq)
            if current_seq != want:
                raise VersionConflict(
                    "配置已被他人修改：当前发布版本 %s，你基于 %s"
                    % (current_seq, want)
                )

            draft = conn.execute(
                "SELECT * FROM provider_config_versions WHERE target_id=? AND seq=?",
                (str(target_id), int(seq)),
            ).fetchone()
            if not draft:
                raise ProviderConfigError("待发布版本不存在")
            if draft["status"] != STATUS_DRAFT:
                raise ProviderConfigError("该版本不是草稿，无法发布")
            evidence = None
            if draft["evidence"]:
                try:
                    evidence = json.loads(draft["evidence"])
                except (TypeError, ValueError):
                    evidence = None
            if not evidence or not evidence.get("ok"):
                raise NotVerified("该版本没有通过的验证证据，禁止发布")

            conn.execute(
                "UPDATE provider_config_versions SET status=? "
                "WHERE target_id=? AND status=?",
                (STATUS_SUPERSEDED, str(target_id), STATUS_PUBLISHED),
            )
            conn.execute(
                "UPDATE provider_config_versions "
                "SET status=?, published_at=?, op_id=?, actor=? "
                "WHERE target_id=? AND seq=?",
                (STATUS_PUBLISHED, ts, op_id, actor, str(target_id), int(seq)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    invalidate(target_id)
    return {"target_id": target_id, "seq": int(seq), "url": draft["url"],
            "status": STATUS_PUBLISHED, "idempotent": False, "published_at": ts}


def rollback(target_id: str, expected_seq, op_id, actor="", to_seq=None, now=None) -> dict:
    """回滚：把上一个可用版本作为**新版本**重新发布（保留历史）。"""
    target(target_id)
    versions = [v for v in list_versions(target_id) if v["status"] in (
        STATUS_PUBLISHED, STATUS_SUPERSEDED)]
    if not versions:
        raise ProviderConfigError("没有可回滚的历史版本")
    target_seq = int(to_seq) if to_seq is not None else None
    if target_seq is None:
        published = next((v for v in versions if v["status"] == STATUS_PUBLISHED), None)
        candidates = [v for v in versions if not published or v["seq"] < published["seq"]]
        if not candidates:
            raise ProviderConfigError("没有可回滚的历史版本")
        target_seq = candidates[0]["seq"]
    ts = _now(now)
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            src = conn.execute(
                "SELECT * FROM provider_config_versions WHERE target_id=? AND seq=?",
                (str(target_id), int(target_seq)),
            ).fetchone()
            if not src:
                raise ProviderConfigError("回滚目标版本不存在")
            cur = conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS s FROM provider_config_versions "
                "WHERE target_id=? AND status=?",
                (str(target_id), STATUS_PUBLISHED),
            ).fetchone()
            current_seq = int(cur["s"]) or None
            want = None if expected_seq in (None, "", 0, "0") else int(expected_seq)
            if current_seq != want:
                raise VersionConflict(
                    "配置已被他人修改：当前发布版本 %s，你基于 %s" % (current_seq, want)
                )
            seq = _next_seq(conn, target_id)
            conn.execute(
                "UPDATE provider_config_versions SET status=? "
                "WHERE target_id=? AND status=?",
                (STATUS_SUPERSEDED, str(target_id), STATUS_PUBLISHED),
            )
            conn.execute(
                "INSERT INTO provider_config_versions("
                "target_id, provider, seq, url, ciphertext, nonce, key_present, key_last4,"
                " status, evidence, evidence_at, op_id, actor, reason, created_at, published_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(target_id), src["provider"], seq, src["url"], src["ciphertext"],
                 src["nonce"], src["key_present"], src["key_last4"], STATUS_PUBLISHED,
                 src["evidence"], src["evidence_at"], op_id, actor,
                 "rollback to seq %s" % int(target_seq), ts, ts),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    invalidate(target_id)
    return {"target_id": target_id, "seq": seq, "url": src["url"],
            "status": STATUS_PUBLISHED, "rolled_back_from": current_seq,
            "restored_seq": int(target_seq)}


# ---------------------------------------------------------------------------
# 统一解析入口
# ---------------------------------------------------------------------------

_CACHE = {}
_CACHE_LOCK = threading.Lock()


def invalidate(target_id=None) -> None:
    with _CACHE_LOCK:
        if target_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(str(target_id), None)


def _resolve_uncached(target_id: str) -> dict:
    spec = target(target_id)
    row = active_version(target_id)
    if row:
        if not row.get("key_present") or row.get("ciphertext") is None:
            # 已发布但凭据不可用：fail-closed，绝不回退环境变量。
            raise ProviderConfigUnavailable(
                "已发布配置缺少可用凭据，拒绝回退环境变量"
            )
        secret = _open(target_id, row["ciphertext"], row["nonce"])
        return {
            "target_id": target_id,
            "provider": spec["provider"],
            "url": row["url"] or _env_url(target_id),
            "credential": secret,
            "version": int(row["seq"]),
            "source": SOURCE_BACKEND,
        }
    return {
        "target_id": target_id,
        "provider": spec["provider"],
        "url": _env_url(target_id),
        "credential": _env_value(spec["env_keys"]),
        "version": None,
        "source": SOURCE_ENV,
    }


def resolve(target_id: str, now=None) -> dict:
    """业务统一入口：返回 ``实际 URL / 有效凭据 / 配置版本 / 配置来源``。

    进程内 5 秒缓存；发布/回滚会显式失效。已发布后台配置后，存储或解密失败
    一律抛 ``ProviderConfigUnavailable``，调用方不得回退环境变量。
    """
    target(target_id)
    ts = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(str(target_id))
        if hit and ts - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    result = _resolve_uncached(target_id)
    with _CACHE_LOCK:
        _CACHE[str(target_id)] = (ts, result)
    return result


def reveal(target_id: str, seq: int, actor="", now=None) -> dict:
    """管理员查看指定版本的明文（调用方必须写审计）。"""
    target(target_id)
    rows = _rows(
        "SELECT ciphertext, nonce, key_present, url FROM provider_config_versions "
        "WHERE target_id=? AND seq=?",
        (str(target_id), int(seq)),
    )
    if not rows:
        raise ProviderConfigError("配置版本不存在")
    row = rows[0]
    if not row["key_present"] or row["ciphertext"] is None:
        return {"target_id": target_id, "seq": int(seq), "secret": "", "url": row["url"]}
    return {
        "target_id": target_id,
        "seq": int(seq),
        "secret": _open(target_id, row["ciphertext"], row["nonce"]),
        "url": row["url"],
    }
