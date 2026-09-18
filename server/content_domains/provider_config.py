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
# 接线开关：不设/空 = 完全不接管（行为与改造前逐字节一致）。
# 取值：all / 1 / true，或逗号分隔的 target_id 白名单。
WIRING_ENV = "HQ_PROVIDER_CONFIG_WIRING"
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
        "features": ["图片生成 → 果肉生图（已下架）"],
        "env_keys": ("XIAOLEVIDEO_API_KEY",),
        "url_env": ("XIAOLEVIDEO_API_BASE",),
        "url_default": "https://api.xiaolevideo.cn",
        "pool_provider": "",
        "url_allowlist_env": "HQ_PROVIDER_BASE_HOST_ALLOWLIST",
        "editable": False,
        "deprecated": True,
        "deprecated_reason": "该生图 API 已下架（image.py::validate_image_payload）",
    },
    "xiaolevideo": {
        # 凭据线路目标：video.py::_xiaole_request 是唯一咽喉，果肉生图与
        # generate_xiaole_video（视频）共用同一份 Key/URL。
        # 果肉生图已下架；本目标保留为“接线模式”的参考实现，默认不出可编辑按钮。
        "provider": "xiaolevideo", "kind": "image+video",
        "features": ["图片生成 → 果肉生图（已下架）",
                      "视频生成 → 果肉视频（generate_xiaole_video 历史路径）"],
        "env_keys": ("XIAOLEVIDEO_API_KEY",),
        "url_env": ("XIAOLEVIDEO_API_BASE",),
        "url_default": "https://api.xiaolevideo.cn",
        "pool_provider": "",
        "choke_point": "content_domains/video.py::_xiaole_request",
        "url_allowlist_env": "HQ_PROVIDER_BASE_HOST_ALLOWLIST",
        "editable": False,
        "deprecated": True,
        "deprecated_reason": "果肉生图已下架；仅保留为历史视频路径",
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
        "features": ["视频模块 → 换装换背景 · 线路二",
                      "视频模块 → Seedance AI 超清"],
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


_SCHEMA_READY = set()
_SCHEMA_LOCK = threading.Lock()


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
    # 每个进程第一次访问时自建表（只 CREATE IF NOT EXISTS，不改变已有列），
    # 这样任何解析凭据的服务都不会因为漏调 init_db 而报 no such table。
    key = str(path)
    if key not in _SCHEMA_READY:
        with _SCHEMA_LOCK:
            if key not in _SCHEMA_READY:
                _ensure_schema(conn)
                conn.commit()
                _SCHEMA_READY.add(key)
    return conn


def _ensure_schema(conn) -> None:
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
            source TEXT NOT NULL DEFAULT 'backend',
            created_at INTEGER NOT NULL,
            published_at INTEGER,
            UNIQUE(target_id, seq)
        )"""
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(provider_config_versions)")}
    if "source" not in cols:
        conn.execute(
            "ALTER TABLE provider_config_versions "
            "ADD COLUMN source TEXT NOT NULL DEFAULT 'backend'"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS provider_config_versions_active "
        "ON provider_config_versions(target_id, status, seq DESC)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS provider_config_versions_op "
        "ON provider_config_versions(target_id, op_id) WHERE op_id IS NOT NULL"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS provider_config_runtime(
            target_id TEXT NOT NULL,
            instance_id TEXT NOT NULL,
            version INTEGER,
            source TEXT NOT NULL,
            loaded_at INTEGER NOT NULL,
            PRIMARY KEY(target_id, instance_id)
        )"""
    )


def init_db() -> None:
    """建表（幂等）。列不可变，只追加新列。"""
    _assert_sqlite_authority()
    with closing(_connect()) as conn:
        _ensure_schema(conn)
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
                "choke_point": spec.get("choke_point", ""),
                "editable": spec.get("editable", True),
                "deprecated": bool(spec.get("deprecated")),
                "deprecated_reason": spec.get("deprecated_reason", ""),
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
        "SELECT seq, url, source, key_present, key_last4, status, evidence, evidence_at, "
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
        if (row.get("source") or "backend") == "env":
            key_present = bool(_env_value(spec["env_keys"]))
            source = SOURCE_ENV
            version = row["seq"]
            key_last4 = _last4(_env_value(spec["env_keys"])) if key_present else ""
        else:
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
        "editable": spec.get("editable", True),
        "deprecated": bool(spec.get("deprecated")),
        "deprecated_reason": spec.get("deprecated_reason", ""),
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
    if not spec.get("editable", True):
        raise ProviderConfigError(
            spec.get("deprecated_reason") or "该线路当前不可在后台编辑")
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
            " status, evidence, evidence_at, op_id, actor, reason, source, created_at, published_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,'backend',?,NULL)",
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
        # 只有一次后台发布（或从未发布）：回滚 = 回到环境变量状态。
        return _publish_env_version(target_id, expected_seq, op_id, actor, now)
    target_seq = int(to_seq) if to_seq is not None else None
    if target_seq is None:
        published = next((v for v in versions if v["status"] == STATUS_PUBLISHED), None)
        candidates = [v for v in versions if not published or v["seq"] < published["seq"]]
        if not candidates:
            # 当前发布就是首个后台版本 → 回滚 = 回到环境变量
            return _publish_env_version(target_id, expected_seq, op_id, actor, now)
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
                " status, evidence, evidence_at, op_id, actor, reason, source, created_at, published_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(target_id), src["provider"], seq, src["url"], src["ciphertext"],
                 src["nonce"], src["key_present"], src["key_last4"], STATUS_PUBLISHED,
                 src["evidence"], src["evidence_at"], op_id, actor,
                 "rollback to seq %s" % int(target_seq),
                 (src["source"] if "source" in src.keys() else "backend") or "backend", ts, ts),
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
        if (row.get("source") or "backend") == "env":
            # 已发布的“回到环境变量”版本：凭据仍取自环境变量，但版本号已固定。
            env_secret = _env_value(spec["env_keys"])
            return {
                "target_id": target_id,
                "provider": spec["provider"],
                "url": _env_url(target_id),
                "credential": env_secret,
                "version": int(row["seq"]),
                "source": SOURCE_ENV,
            }
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
    存储瞬时不可用时，若进程内已有 **任意年龄** 的成功读数（安全缓存），
    返回该读数——它仍是后台配置，不是任意环境变量，不会造成双份不一致。
    """
    target(target_id)
    ts = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(str(target_id))
        if hit and ts - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    try:
        result = _resolve_uncached(target_id)
    except ProviderConfigUnavailable:
        with _CACHE_LOCK:
            stale = _CACHE.get(str(target_id))
        if stale is not None:
            return stale[1]  # 安全缓存：宁用上一次真实读数，也不回退环境变量
        raise
    with _CACHE_LOCK:
        _CACHE[str(target_id)] = (ts, result)
    return result


def wiring_enabled(target_id=None) -> bool:
    """接线开关：默认关闭。未开启时业务方保持旧行为（读进程启动时的 env 常量）。"""
    raw = str(os.environ.get(WIRING_ENV) or "").strip().lower()
    if not raw:
        return False
    if raw in ("all", "1", "true", "yes", "on"):
        return True
    ids = {item.strip() for item in raw.split(",") if item.strip()}
    if target_id is None:
        return bool(ids)
    return str(target_id) in ids


def credentials_for(target_id: str, legacy_key="", legacy_url="") -> dict:
    """生成/提交路径的唯一取凭据入口（各模块统一调它，避免各自读 env）。

    - 未开启接线：原样返回旧常量（行为零变化）；
    - 已开启但尚无已发布后台配置：返回环境变量，``source='env'``；
    - 已开启且有已发布后台配置：返回后台版本（不返回环境变量）；
    - 存储/解密失败：抛 ``ProviderConfigUnavailable``（fail-closed）。
    """
    if not wiring_enabled(target_id):
        return {"target_id": target_id, "url": legacy_url, "credential": legacy_key,
                "version": None, "source": SOURCE_ENV, "wired": False}
    out = dict(resolve(target_id))
    out["wired"] = True
    if not out.get("credential"):
        out["credential"] = legacy_key
    if not out.get("url"):
        out["url"] = legacy_url
    # 生效反馈来自“实际使用它的服务”：这里上报本次真正加载的版本。
    # 上报失败不影响业务，但生效状态会诚实地显示“结果待核对”。
    try:
        report_loaded(target_id, out.get("version"), out.get("source"))
    except Exception:  # noqa: BLE001
        pass
    return out


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


# ---------------------------------------------------------------------------
# 生效反馈：由**实际运行的服务**上报自己加载的版本
# ---------------------------------------------------------------------------
# “配置已生效”必须来自运行服务的加载结果，不能由保存/发布成功推断。
RUNTIME_FRESH_SECONDS = 900  # 超过此时间未上报的实例视为“未刷新/已下线”

# 生效状态（与改造方案第五节一致）
STATE_DRAFT_ONLY = "draft_only"            # 草稿已保存（尚未发布）
STATE_VERIFIED = "verified"                # 验证通过（尚未发布）
STATE_PUBLISHING = "publishing"             # 已发布，部分实例尚未确认
STATE_EFFECTIVE = "effective"               # 配置已生效（新任务将使用新版本）
STATE_UNCONFIRMED = "unconfirmed"           # 结果待核对（无运行实例上报）
STATE_FAILED = "failed"                     # 发布失败（未完成发布）

STATE_LABELS = {
    STATE_DRAFT_ONLY: "草稿已保存",
    STATE_VERIFIED: "验证通过（未发布）",
    STATE_PUBLISHING: "正在生效",
    STATE_EFFECTIVE: "配置已生效",
    STATE_UNCONFIRMED: "结果待核对",
    STATE_FAILED: "发布失败",
}


def _instance_id(instance_id=None) -> str:
    if instance_id:
        return str(instance_id)[:120]
    try:
        import socket
        return "%s:%d" % (socket.gethostname(), os.getpid())
    except Exception:  # noqa: BLE001
        return "pid:%d" % os.getpid()


def report_loaded(target_id: str, version, source: str, instance_id=None,
                  now=None) -> None:
    """运行服务在真正使用某个版本时上报。生效判断只看这张表。"""
    target(target_id)
    ts = _now(now)
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO provider_config_runtime("
            "target_id, instance_id, version, source, loaded_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(target_id, instance_id) DO UPDATE SET "
            "version=excluded.version, source=excluded.source, loaded_at=excluded.loaded_at",
            (str(target_id), _instance_id(instance_id),
             None if version is None else int(version), str(source), ts),
        )
        conn.commit()


def runtime_instances(target_id: str, now=None) -> list:
    target(target_id)
    ts = _now(now)
    rows = _rows(
        "SELECT instance_id, version, source, loaded_at FROM provider_config_runtime "
        "WHERE target_id=? ORDER BY loaded_at DESC",
        (str(target_id),),
    )
    for row in rows:
        row["fresh"] = (ts - int(row["loaded_at"] or 0)) <= RUNTIME_FRESH_SECONDS
    return rows


def effective_status(target_id: str, now=None) -> dict:
    """把“发布状态”与“运行服务实际加载结果”合起来给出一个诚实的生效结论。"""
    spec = target(target_id)
    ts = _now(now)
    published = active_version(target_id)
    versions = list_versions(target_id)
    latest_draft = next((v for v in versions if v["status"] == STATUS_DRAFT), None)
    instances = runtime_instances(target_id, ts)
    fresh = [i for i in instances if i["fresh"]]

    if not published:
        if latest_draft and (latest_draft.get("evidence") or {}).get("ok"):
            state = STATE_VERIFIED
        elif latest_draft:
            state = STATE_DRAFT_ONLY
        else:
            # 从未发布过后台配置：仍在用环境变量，不算“后台配置生效”。
            state = SOURCE_ENV
        return {
            "target_id": target_id, "provider": spec["provider"],
            "state": state, "label": STATE_LABELS.get(state, "使用环境变量"),
            "published_version": None, "published_at": None,
            "instances": instances, "fresh_instances": len(fresh),
            "editable": spec.get("editable", True),
            "pool_shared": bool(spec.get("pool_provider")),
            "checked_at": ts,
        }

    want = int(published["seq"])
    if not fresh:
        state = STATE_UNCONFIRMED
    elif all(int(i["version"] or 0) == want for i in fresh):
        state = STATE_EFFECTIVE
    else:
        state = STATE_PUBLISHING
    return {
        "target_id": target_id, "provider": spec["provider"],
        "state": state, "label": STATE_LABELS[state],
        "published_version": want,
        "published_at": published.get("published_at"),
        "instances": instances, "fresh_instances": len(fresh),
        "stale_instances": len(instances) - len(fresh),
        "not_loaded_instances": [i["instance_id"] for i in fresh
                                 if int(i["version"] or 0) != want],
        "editable": spec.get("editable", True),
        "pool_shared": bool(spec.get("pool_provider")),
        "checked_at": ts,
    }


# ---------------------------------------------------------------------------
# 任务版本固定：任务创建时定版，后续查询/下载/恢复用同一版本
# ---------------------------------------------------------------------------

def pin(target_id: str) -> dict:
    """任务创建时调用：返回本任务应固定的版本引用（不含明文）。"""
    target(target_id)
    row = active_version(target_id)
    if not row:
        return {"target_id": target_id, "version": None, "source": SOURCE_ENV}
    return {"target_id": target_id, "version": int(row["seq"]),
            "url": row["url"], "source": SOURCE_BACKEND}


def resolve_pinned(target_id: str, version, legacy_key="", legacy_url="") -> dict:
    """按任务固定的版本解析凭据；版本不存在一律报错（不回退环境变量）。"""
    if version in (None, "", 0, "0"):
        return {"target_id": target_id, "url": legacy_url, "credential": legacy_key,
                "version": None, "source": SOURCE_ENV}
    target(target_id)
    rows = _rows(
        "SELECT * FROM provider_config_versions WHERE target_id=? AND seq=?",
        (str(target_id), int(version)),
    )
    if not rows:
        raise ProviderConfigError("任务引用的配置版本不存在（可能未迁移）")
    row = rows[0]
    if row["status"] not in (STATUS_PUBLISHED, STATUS_SUPERSEDED):
        raise ProviderConfigError("任务引用的配置版本不可用")
    if (row.get("source") or "backend") == "env":
        return {"target_id": target_id, "url": legacy_url, "credential": legacy_key,
                "version": int(row["seq"]), "source": SOURCE_ENV}
    if not row["key_present"] or row["ciphertext"] is None:
        raise ProviderConfigUnavailable("任务引用的配置版本缺少凭据")
    return {
        "target_id": target_id,
        "url": row["url"] or legacy_url,
        "credential": _open(target_id, row["ciphertext"], row["nonce"]),
        "version": int(row["seq"]),
        "source": SOURCE_BACKEND,
    }


def _publish_env_version(target_id, expected_seq, op_id, actor, now=None):
    """发布一条 source='env' 的版本：语义上等于「回到服务器环境变量」。

    用于“只有一次后台发布”时也能回滚：回滚目标不是更早的后台版本，而是环境变量状态。
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
                "WHERE target_id=? AND op_id=?", (str(target_id), op_id)).fetchone()
            if done:
                conn.commit()
                return {"target_id": target_id, "seq": done["seq"], "url": done["url"],
                        "status": done["status"], "idempotent": True, "source": "env"}
            cur = conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS s FROM provider_config_versions "
                "WHERE target_id=? AND status=?",
                (str(target_id), STATUS_PUBLISHED)).fetchone()
            current_seq = int(cur["s"]) or None
            want = None if expected_seq in (None, "", 0, "0") else int(expected_seq)
            if current_seq != want:
                raise VersionConflict(
                    "配置已被他人修改：当前发布版本 %s，你基于 %s" % (current_seq, want))
            seq = _next_seq(conn, target_id)
            conn.execute(
                "UPDATE provider_config_versions SET status=? "
                "WHERE target_id=? AND status=?",
                (STATUS_SUPERSEDED, str(target_id), STATUS_PUBLISHED))
            evidence = json.dumps(
                {"ok": True, "checks": {"source": "env"}, "free_verification": True,
                 "note": "回滚到服务器环境变量"},
                ensure_ascii=False, sort_keys=True)
            conn.execute(
                "INSERT INTO provider_config_versions("
                "target_id, provider, seq, url, ciphertext, nonce, key_present, key_last4,"
                " status, evidence, evidence_at, op_id, actor, reason, source, created_at, published_at)"
                " VALUES(?,?,?,?,NULL,NULL,0,'',?,?,?,?,?,?,'env',?,?)",
                (str(target_id), target(target_id)["provider"], seq, "", STATUS_PUBLISHED,
                 evidence, ts, op_id, actor, "rollback to environment variables", ts, ts))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    invalidate(target_id)
    return {"target_id": target_id, "seq": seq, "url": "", "status": STATUS_PUBLISHED,
            "source": "env", "rolled_back_from": current_seq}


def retained_versions(target_id: str) -> list:
    """所有仍可被在途任务引用的版本（published + superseded）。只增不删。"""
    target(target_id)
    return [v["seq"] for v in list_versions(target_id)
            if v["status"] in (STATUS_PUBLISHED, STATUS_SUPERSEDED)]


# ---------------------------------------------------------------------------
# 草稿验证：只用连接 + 鉴权探测，**不触发生成、不产生费用**
# ---------------------------------------------------------------------------

def _http_probe(url, key, timeout=8):
    """默认探测器：对候选 URL 做连接探测，对 {url}/models 做鉴权探测。

    不使用付费生成，也不因验证失败关闭 HTTPS 校验。只用于“能不能用”的判定，
    不代表生成一定成功。
    """
    import ssl
    import urllib.error
    import urllib.request

    ctx = ssl.create_default_context()
    base = str(url).rstrip("/")
    out = {"connection": {"ok": False},
           "auth": {"ok": False}}
    try:
        req = urllib.request.Request(base, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            out["connection"] = {"ok": True, "status": int(resp.status)}
    except urllib.error.HTTPError as exc:
        # 能拿到 HTTP 状态就说明网络可达（401/403/404 都算可达）。
        out["connection"] = {"ok": True, "status": int(exc.code)}
    except Exception as exc:  # noqa: BLE001
        out["connection"] = {"ok": False, "error": str(exc)[:160]}
        out["auth"] = {"ok": False, "error": "连接未建立，鉴权未验证"}
        return out
    try:
        req = urllib.request.Request(
            base + "/models", method="GET",
            headers={"Authorization": "Bearer " + str(key or "")})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            out["auth"] = {"ok": int(resp.status) < 400, "status": int(resp.status)}
    except urllib.error.HTTPError as exc:
        out["auth"] = {"ok": int(exc.code) not in (401, 403), "status": int(exc.code),
                       "note": "非鉴权类错误" if int(exc.code) not in (401, 403) else "凭据被拒绝"}
    except Exception as exc:  # noqa: BLE001
        out["auth"] = {"ok": False, "error": str(exc)[:160]}
    return out


def validate_draft(target_id: str, seq: int, actor="", probe=None, now=None) -> dict:
    """用候选版本的 URL/Key 做验证，把证据绑定到**该版本**。

    只做连接与鉴权探测（默认 _http_probe），不做付费生成。
    验证后若再改字段会生成新版本，旧证据自动不适用。
    """
    target(target_id)
    rows = _rows(
        "SELECT url, ciphertext, nonce, key_present, status "
        "FROM provider_config_versions WHERE target_id=? AND seq=?",
        (str(target_id), int(seq)),
    )
    if not rows:
        raise ProviderConfigError("配置版本不存在")
    row = rows[0]
    if row["status"] != STATUS_DRAFT:
        raise ProviderConfigError("只能验证草稿版本")
    if not row["key_present"] or row["ciphertext"] is None:
        raise ProviderConfigError("草稿缺少凭据，无法验证")
    key = _open(target_id, row["ciphertext"], row["nonce"])
    runner = probe or _http_probe
    checks = runner(row["url"], key)
    connection = checks.get("connection") or {}
    auth = checks.get("auth") or {}
    ok = bool(connection.get("ok")) and bool(auth.get("ok"))
    note = "连接与鉴权探测（免费，不触发生成）"
    evidence = record_evidence(
        target_id, int(seq),
        {"ok": ok, "checks": {"connection": connection, "auth": auth},
         "free_verification": True, "note": note},
        actor=actor, now=now,
    )
    return {"target_id": target_id, "seq": int(seq), "ok": ok,
            "checks": {"connection": connection, "auth": auth},
            "evidence": evidence,
            "note": "验证通过仅代表连接与鉴权可用，不代表生成成功率"}
