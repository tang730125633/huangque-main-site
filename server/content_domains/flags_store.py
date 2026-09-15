"""PostgreSQL backend for platform feature flags and pricing rules.

由 ``feature_flags`` / ``pricing`` 在 ``HQ_FLAGS_STORE=postgres`` 时调用；
默认（未配置）走两模块内既有的 SQLite 路径，行为与迁移前完全一致。

本模块绝不吞错：任何连接或执行失败都抛异常，由上层沿用既有
「安全缓存 + 目录默认 / fail-closed」语义。

切换纪律：同一时刻只能有一个权威。切换时所有读方（content/auth/admin/
imggen/leadgen）必须同一版本、同一开关一起切，禁止双权威并存。
"""

from __future__ import annotations

import os
import threading

_MODES = {"sqlite", "postgres"}
_pool = None
_pool_lock = threading.Lock()


def mode() -> str:
    value = (os.environ.get("HQ_FLAGS_STORE") or "sqlite").strip().lower()
    if value not in _MODES:
        raise RuntimeError("HQ_FLAGS_STORE must be sqlite or postgres")
    return value


def enabled() -> bool:
    """是否已切换 PostgreSQL 权威。"""
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

            maximum = int(os.environ.get("HQ_FLAGS_DB_POOL_MAX") or "4")
            if maximum < 1 or maximum > 20:
                raise RuntimeError("HQ_FLAGS_DB_POOL_MAX must be between 1 and 20")
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


def _read(table, key_column) -> dict:
    with _pool_instance().connection() as conn:
        rows = conn.execute("SELECT * FROM ops.%s" % table).fetchall()
    return {row[key_column]: dict(row) for row in rows}


def _write(table, key_column, key, columns, values) -> None:
    updates = ",".join("%s = EXCLUDED.%s" % (c, c) for c in columns)
    sql = (
        "INSERT INTO ops.%s(%s, %s) VALUES (%%s, %s) "
        "ON CONFLICT (%s) DO UPDATE SET %s"
        % (table, key_column, ",".join(columns),
           ",".join("%s" for _ in columns), key_column, updates)
    )
    with _pool_instance().connection() as conn:
        with conn.transaction():
            conn.execute(sql, (key,) + tuple(values))


def read_flags() -> dict:
    """{feature: {feature, enabled, updated_by, updated_at}}。"""
    return _read("feature_flags", "feature")


def write_flag(feature: str, enabled: bool, actor: str, now: int) -> None:
    _write("feature_flags", "feature", feature,
           ("enabled", "updated_by", "updated_at"),
           (bool(enabled), actor or "admin", now))


def read_prices() -> dict:
    """{rule: {rule, points, updated_by, updated_at}}。"""
    return _read("pricing_rules", "rule")


def write_price(rule: str, points: int, actor: str, now: int) -> None:
    _write("pricing_rules", "rule", rule,
           ("points", "updated_by", "updated_at"),
           (points, actor or "admin", now))


def close_pool() -> None:
    """测试与进程退出清理用；生产进程长驻不需要调用。"""
    global _pool
    with _pool_lock:
        current, _pool = _pool, None
    if current is not None:
        current.close()
