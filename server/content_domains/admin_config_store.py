"""PostgreSQL backend for the admin configuration database (``admin_config.db``).

由 ``provider_keys``（密钥池）与 ``admin_api``（渠道开关 + 管理审计）在
``HQ_ADMIN_CONFIG_STORE=postgres`` 时调用；默认（未配置）走各模块内既有的
SQLite 路径，行为与迁移前逐字节一致。

本模块绝不吞错：任何连接或执行失败都抛异常，由上层沿用既有「fail-closed /
安全缓存 / 只读降级」语义。

本域是**按表切换**的：同一时刻每张表只能有一个权威。M3D 已完成
``admin_provider_api_keys``（密钥池）、``admin_channel_config``、``admin_audit``
三个模块内路径的改造；``admin_e2e_*`` 与 ``inspiration_cases`` 仍在 SQLite，
切换前不得把它们的目标表当权威（见 M3D Runbook）。
"""

from __future__ import annotations

import json
import logging
import os
import threading

_MODES = {"sqlite", "postgres"}
_pool = None
_pool_lock = threading.Lock()

# 密钥池与审计表的时间口径：秒级 Unix 时间戳（BIGINT），与 SQLite 源一致。
_KEY_COLUMNS = (
    "id", "provider", "label", "last4", "ciphertext", "nonce", "base_url",
    "priority", "state", "health_status", "last_checked_at", "last_latency_ms",
    "last_error", "use_count", "last_used_at", "created_by", "created_at",
    "updated_at",
)

_log = logging.getLogger("hq.admin_config_store")
_MODE_ANNOUNCED = False


def _announce_mode(env_name: str, value: str) -> None:
    """进程内一次性权威声明：第一次解析出模式时留一条日志。

    环境变量缺失/为空 = 静默退回默认旧存储，是切写后最危险的情形，
    用 WARNING 保证默认日志级别可见；显式配置用 INFO。
    """
    global _MODE_ANNOUNCED
    if _MODE_ANNOUNCED:
        return
    _MODE_ANNOUNCED = True
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        _log.warning(
            "%s not set, falling back to default %r (legacy storage)",
            env_name, value,
        )
    else:
        _log.info("%s authority announced: mode=%s", env_name, value)


def mode() -> str:
    value = (os.environ.get("HQ_ADMIN_CONFIG_STORE") or "sqlite").strip().lower()
    if value not in _MODES:
        raise RuntimeError("HQ_ADMIN_CONFIG_STORE must be sqlite or postgres")
    _announce_mode("HQ_ADMIN_CONFIG_STORE", value)
    return value


def enabled() -> bool:
    """是否已把本域切到 PostgreSQL 权威。"""
    return mode() == "postgres"


def _pool_instance():
    global _pool
    if _pool is not None:
        return _pool
    url = (os.environ.get("HQ_DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError("HQ_DATABASE_URL is not configured")
    with _pool_lock:
        if _pool is None:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            maximum = int(os.environ.get("HQ_ADMIN_CONFIG_DB_POOL_MAX") or "4")
            if maximum < 1 or maximum > 20:
                raise RuntimeError("HQ_ADMIN_CONFIG_DB_POOL_MAX must be between 1 and 20")
            candidate = ConnectionPool(
                conninfo=url,
                min_size=1,
                max_size=maximum,
                timeout=5,
                kwargs={"autocommit": False, "row_factory": dict_row},
                open=False,
            )
            candidate.open(wait=True, timeout=5)
            _pool = candidate
    return _pool


def close_pool() -> None:
    """测试与进程退出清理用；生产进程长驻不需要调用。"""
    global _pool
    with _pool_lock:
        current, _pool = _pool, None
    if current is not None:
        current.close()


def _rows(sql, params=None) -> list:
    with _pool_instance().connection() as conn:
        return [dict(row) for row in conn.execute(sql, params or ()).fetchall()]


def _write(sql, params=None) -> int:
    """单语句写；返回受影响行数。"""
    with _pool_instance().connection() as conn:
        with conn.transaction():
            return conn.execute(sql, params or ()).rowcount


# ---------------------------------------------------------------------------
# ops.admin_provider_api_keys —— 托管密钥池
# ---------------------------------------------------------------------------

def fetch_keys() -> dict:
    """全表读回 {id: {列名: 值}}；排序由调用方在内存中完成（与 SQLite 口径一致）。"""
    return {row["id"]: row for row in _rows(
        "SELECT * FROM ops.admin_provider_api_keys")}


def fetch_key(key_id) -> dict | None:
    rows = _rows(
        "SELECT * FROM ops.admin_provider_api_keys WHERE id = %s", (str(key_id),))
    return rows[0] if rows else None


def count_by_provider() -> dict:
    return {row["provider"]: int(row["n"]) for row in _rows(
        "SELECT provider, COUNT(*) AS n FROM ops.admin_provider_api_keys "
        "GROUP BY provider")}


def _insert_key_sql(guard: str) -> str:
    return (
        "INSERT INTO ops.admin_provider_api_keys(" + ",".join(_KEY_COLUMNS) + ") "
        "SELECT %s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s,0,NULL,%s,%s,%s "
        + guard
    )


def _key_values(key_id, provider, label, last4, ciphertext, nonce, base_url,
                priority, health_status, last_checked_at, last_latency_ms,
                last_error, created_by, now) -> tuple:
    return (
        str(key_id), str(provider), str(label), str(last4),
        bytes(ciphertext), bytes(nonce), str(base_url), int(priority),
        str(health_status), last_checked_at, last_latency_ms,
        str(last_error or "")[:180], str(created_by), int(now), int(now),
    )


def insert_key(key_id, provider, label, last4, ciphertext, nonce, base_url,
               priority, health_status, last_checked_at, last_latency_ms,
               last_error, created_by, now) -> None:
    """新增一行；列与 SQLite 的 INSERT 逐列对应（use_count=0, last_used_at=NULL）。"""
    _write(
        _insert_key_sql(""),
        _key_values(key_id, provider, label, last4, ciphertext, nonce, base_url,
                    priority, health_status, last_checked_at, last_latency_ms,
                    last_error, created_by, now),
    )


def insert_env_key_once(key_id, provider, label, last4, ciphertext, nonce,
                        base_url, priority, health_status, last_checked_at,
                        last_latency_ms, last_error, created_by, now) -> bool:
    """环境变量一次性托管：同渠道已有 system-env-migration 行时不再插入。

    与 SQLite 侧 ``BEGIN IMMEDIATE`` 的语义一致：并发进程里只会成功一次。
    """
    guard = (
        "WHERE NOT EXISTS (SELECT 1 FROM ops.admin_provider_api_keys "
        "WHERE provider = %s AND created_by = 'system-env-migration')"
    )
    values = _key_values(key_id, provider, label, last4, ciphertext, nonce,
                         base_url, priority, health_status, last_checked_at,
                         last_latency_ms, last_error, created_by, now)
    return _write(_insert_key_sql(guard), values + (str(provider),)) > 0


def reactivate_key(key_id, label, base_url, priority, health_status,
                   last_checked_at, last_latency_ms, now) -> bool:
    """重新启用一条已下架的密钥（同明文再次添加时恢复原编号）。"""
    return _write(
        """
        UPDATE ops.admin_provider_api_keys
           SET label = %s, base_url = %s, priority = %s, state = 'active',
               health_status = %s, last_checked_at = %s, last_latency_ms = %s,
               last_error = '', updated_at = %s
         WHERE id = %s
        """,
        (str(label), str(base_url), int(priority), str(health_status),
         last_checked_at, last_latency_ms, int(now), str(key_id)),
    ) > 0


def write_health(key_id, ok, latency_ms, error, now) -> bool:
    """写回一次健康探测结果；未命中返回 False（与 SQLite rowcount 语义一致）。"""
    return _write(
        """
        UPDATE ops.admin_provider_api_keys
           SET health_status = %s, last_checked_at = %s, last_latency_ms = %s,
               last_error = %s, updated_at = %s
         WHERE id = %s
        """,
        ("healthy" if ok else "unhealthy", int(now),
         int(latency_ms) if latency_ms is not None else None,
         str(error or "")[:180], int(now), str(key_id)),
    ) > 0


def retire_key(key_id, now) -> bool:
    """下架一条密钥；已下架返回 False（不可重复下架）。"""
    return _write(
        "UPDATE ops.admin_provider_api_keys "
        "SET state = 'retired', updated_at = %s "
        "WHERE id = %s AND state <> 'retired'",
        (int(now), str(key_id)),
    ) > 0


def claim_key(provider, blocked_ids, now) -> dict | None:
    """原子认领一条最少使用且健康的密钥，并把 use_count 加一。

    单条 UPDATE ... RETURNING 完成「挑选 + 占用」，与 SQLite 侧 ``BEGIN IMMEDIATE``
    的效果一致：并发进程不会重复认领同一条密钥。``blocked_ids`` 是各进程内存里的
    临时隔离集合，不落库。
    """
    blocked = [str(item) for item in (blocked_ids or ())]
    rows = _rows(
        """
        UPDATE ops.admin_provider_api_keys
           SET use_count = use_count + 1, last_used_at = %s
         WHERE id = (
               SELECT id FROM ops.admin_provider_api_keys
                WHERE provider = %s
                  AND state = 'active'
                  AND health_status <> 'unhealthy'
                  AND NOT (id = ANY(%s::text[]))
                ORDER BY use_count, priority, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
         )
         RETURNING *
        """,
        (int(now), str(provider), blocked),
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# ops.admin_channel_config —— 渠道开关与参数
# ---------------------------------------------------------------------------

def read_channels() -> dict:
    """{channel: {channel, enabled, config, updated_by, updated_at}}。"""
    return {row["channel"]: row for row in _rows(
        "SELECT * FROM ops.admin_channel_config")}


def save_channel(actor, channel, enabled, config_json, detail_json, now) -> None:
    """写渠道配置并同时追加审计行：与 SQLite 侧同一事务的语义一致。

    渠道配置与审计必须同一个事务落库，否则会出现「配置改了但审计缺失」。
    审计的 detail 是 {"enabled":…,"config":…,"reason":…}，不是 config 本身。
    """
    with _pool_instance().connection() as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO ops.admin_channel_config(
                    channel, enabled, config, updated_by, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (channel) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    config = EXCLUDED.config,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = EXCLUDED.updated_at
                """,
                (str(channel), bool(enabled), str(config_json), str(actor), int(now)),
            )
            conn.execute(
                "INSERT INTO ops.admin_audit(actor, action, target, detail, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (str(actor), "channel.save", str(channel), str(detail_json), int(now)),
            )


# ---------------------------------------------------------------------------
# ops.admin_audit —— 管理操作审计台账
# ---------------------------------------------------------------------------

def write_audit(actor, action, target, detail_json, now) -> None:
    _write(
        "INSERT INTO ops.admin_audit(actor, action, target, detail, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (str(actor)[:80], str(action)[:80], str(target)[:120],
         str(detail_json), int(now)),
    )


def read_audit_by_actions(actions, limit=None) -> list:
    """按动作键读审计行，最新在前（created_at DESC, id DESC）。"""
    sql = (
        "SELECT actor, action, target, detail, created_at FROM ops.admin_audit "
        "WHERE action = ANY(%s::text[]) ORDER BY created_at DESC, id DESC"
    )
    params = [sorted(str(item) for item in actions)]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(int(limit))
    return _rows(sql, tuple(params))


def read_legacy_audit(prefixes, limit) -> list:
    """按动作前缀读审计行（运行历史页用），最新在前，列名与 SQLite 查询一致。"""
    sql = (
        "SELECT actor, action, target, created_at AS created FROM ops.admin_audit "
        "WHERE action LIKE ANY(%s::text[]) ORDER BY created_at DESC, id DESC LIMIT %s"
    )
    patterns = ["%s%%" % str(item) for item in prefixes]
    return _rows(sql, (patterns, int(limit)))


# ---------------------------------------------------------------------------
# ops.provider_config_versions / ops.provider_config_runtime
# ---------------------------------------------------------------------------
# 「环境变量型线路」的版本化后台配置（见 content_domains/provider_config.py）。
# ciphertext/nonce 是 AES-GCM 密文，任何日志或响应都不得输出。

class ProviderConfigConflict(RuntimeError):
    """发布冲突：expected_seq 与当前发布版本不一致。"""


class ProviderConfigNotVerified(RuntimeError):
    """目标版本没有通过的验证证据。"""


_PC_COLUMNS = (
    "target_id", "provider", "seq", "url", "ciphertext", "nonce", "key_present",
    "key_last4", "status", "evidence", "evidence_at", "op_id", "actor", "reason",
    "source", "created_at", "published_at",
)


def pc_list_versions(target_id) -> list:
    return _rows(
        "SELECT seq, url, source, key_present, key_last4, status, evidence, evidence_at, "
        "op_id, actor, reason, created_at, published_at "
        "FROM ops.provider_config_versions WHERE target_id = %s ORDER BY seq DESC",
        (str(target_id),),
    )


def pc_get_version(target_id, seq) -> dict | None:
    rows = _rows(
        "SELECT * FROM ops.provider_config_versions WHERE target_id = %s AND seq = %s",
        (str(target_id), int(seq)))
    return rows[0] if rows else None


def pc_active_version(target_id) -> dict | None:
    rows = _rows(
        "SELECT * FROM ops.provider_config_versions WHERE target_id = %s "
        "AND status = 'published' ORDER BY seq DESC LIMIT 1", (str(target_id),))
    return rows[0] if rows else None


def _pc_next_seq(conn, target_id) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS s FROM ops.provider_config_versions "
        "WHERE target_id = %s", (str(target_id),)).fetchone()
    return int(row["s"]) + 1


def _pc_want(expected_seq):
    return None if expected_seq in (None, "", 0, "0") else int(expected_seq)


def pc_save_draft(target_id, provider, url, ciphertext, nonce, key_present,
                  key_last4, actor, reason, now) -> dict:
    with _pool_instance().connection() as conn:
        with conn.transaction():
            seq = _pc_next_seq(conn, target_id)
            conn.execute(
                "INSERT INTO ops.provider_config_versions(" + ",".join(_PC_COLUMNS) + ") "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'draft',NULL,NULL,NULL,%s,%s,'backend',%s,NULL)",
                (str(target_id), str(provider), int(seq), str(url), ciphertext, nonce,
                 bool(key_present), str(key_last4), str(actor),
                 str(reason or "")[:200], int(now)))
    return {"seq": int(seq)}


def pc_set_evidence(target_id, seq, evidence_json, at) -> bool:
    return _write(
        "UPDATE ops.provider_config_versions SET evidence = %s, evidence_at = %s "
        "WHERE target_id = %s AND seq = %s AND status = 'draft'",
        (str(evidence_json), int(at), str(target_id), int(seq))) > 0


def _pc_guard(conn, target_id, op_id, expected_seq):
    done = conn.execute(
        "SELECT seq, url, status FROM ops.provider_config_versions "
        "WHERE target_id = %s AND op_id = %s", (str(target_id), str(op_id))).fetchone()
    if done:
        return {"seq": done["seq"], "url": done["url"], "status": done["status"],
                "idempotent": True}
    cur = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS s FROM ops.provider_config_versions "
        "WHERE target_id = %s AND status = 'published'", (str(target_id),)).fetchone()
    current = int(cur["s"]) or None
    want = _pc_want(expected_seq)
    if current != want:
        raise ProviderConfigConflict(
            "配置已被他人修改：当前发布版本 %s，你基于 %s" % (current, want))
    return None


def pc_publish(target_id, seq, expected_seq, op_id, actor, now) -> dict:
    with _pool_instance().connection() as conn:
        with conn.transaction():
            hit = _pc_guard(conn, target_id, op_id, expected_seq)
            if hit:
                return hit
            draft = conn.execute(
                "SELECT * FROM ops.provider_config_versions WHERE target_id = %s AND seq = %s "
                "FOR UPDATE", (str(target_id), int(seq))).fetchone()
            if not draft:
                raise ValueError("待发布版本不存在")
            if draft["status"] != "draft":
                raise ValueError("该版本不是草稿，无法发布")
            ok = False
            if draft["evidence"]:
                try:
                    ok = bool(json.loads(draft["evidence"]).get("ok"))
                except (TypeError, ValueError):
                    ok = False
            if not ok:
                raise ProviderConfigNotVerified("该版本没有通过的验证证据，禁止发布")
            conn.execute(
                "UPDATE ops.provider_config_versions SET status = 'superseded' "
                "WHERE target_id = %s AND status = 'published'", (str(target_id),))
            conn.execute(
                "UPDATE ops.provider_config_versions SET status = 'published', "
                "published_at = %s, op_id = %s, actor = %s "
                "WHERE target_id = %s AND seq = %s",
                (int(now), str(op_id), str(actor), str(target_id), int(seq)))
    return {"seq": int(seq), "url": draft["url"], "status": "published",
            "idempotent": False, "published_at": int(now)}


def pc_insert_published(target_id, provider, url, ciphertext, nonce, key_present,
                        key_last4, source, evidence_json, reason, expected_seq,
                        op_id, actor, now) -> dict:
    with _pool_instance().connection() as conn:
        with conn.transaction():
            hit = _pc_guard(conn, target_id, op_id, expected_seq)
            if hit:
                return hit
            seq = _pc_next_seq(conn, target_id)
            conn.execute(
                "UPDATE ops.provider_config_versions SET status = 'superseded' "
                "WHERE target_id = %s AND status = 'published'", (str(target_id),))
            conn.execute(
                "INSERT INTO ops.provider_config_versions(" + ",".join(_PC_COLUMNS) + ") "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'published',%s,%s,%s,%s,%s,%s,%s,%s)",
                (str(target_id), str(provider), int(seq), str(url), ciphertext, nonce,
                 bool(key_present), str(key_last4), str(evidence_json), int(now),
                 str(op_id), str(actor), str(reason), str(source), int(now), int(now)))
    return {"seq": int(seq), "url": str(url), "status": "published",
            "source": str(source), "idempotent": False}


def pc_runtime_upsert(target_id, instance_id, version, source, now) -> None:
    _write(
        "INSERT INTO ops.provider_config_runtime(target_id, instance_id, version, source, loaded_at) "
        "VALUES(%s,%s,%s,%s,%s) ON CONFLICT (target_id, instance_id) DO UPDATE SET "
        "version = EXCLUDED.version, source = EXCLUDED.source, loaded_at = EXCLUDED.loaded_at",
        (str(target_id), str(instance_id),
         None if version is None else int(version), str(source), int(now)))


def pc_runtime_list(target_id) -> list:
    return _rows(
        "SELECT instance_id, version, source, loaded_at FROM ops.provider_config_runtime "
        "WHERE target_id = %s ORDER BY loaded_at DESC", (str(target_id),))
