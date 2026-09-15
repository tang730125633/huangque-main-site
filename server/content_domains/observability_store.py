"""PostgreSQL backend for runtime observation evidence and the alert outbox.

由 ``runtime_observability`` 在 ``HQ_OBS_STORE=postgres`` 时调用；默认（未配置）
走该模块内既有的 SQLite 路径（``runtime_observability.db`` 的 ``task_trace`` /
``alert_outbox``），行为与迁移前完全一致。

本模块绝不吞错：任何连接或执行失败都抛异常，由上层沿用既有降级语义：
证据写入失败不影响已付费任务结论（``record`` / ``call`` 静默丢弃），读取失败按
「无证据」返回空，通知入队/投递失败必须暴露（``enqueue`` / ``dispatch`` 不兜底）。

连接池自建（与 M3A 的 ``flags_store`` 同一做法）：生产 content-api 进程的 sys.path
里没有 ``server`` 包（``server/db`` 没有任何部署映射），因此本模块**不得**
import ``server.db.postgres``；只在这里懒加载 ``psycopg_pool.ConnectionPool``，
上限走 ``HQ_OBS_DB_POOL_MAX``（默认 4）。导入本模块本身不建连。

切换纪律：同一时刻只能有一个权威。切换时所有读方（content / admin）必须同一
版本、同一开关一起切，禁止双权威并存。
"""

from __future__ import annotations

import logging
import os
import threading

_MODES = {"sqlite", "postgres"}
_pool = None
_pool_lock = threading.Lock()

_log = logging.getLogger("hq.observability_store")
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
    value = (os.environ.get("HQ_OBS_STORE") or "sqlite").strip().lower()
    if value not in _MODES:
        raise RuntimeError("HQ_OBS_STORE must be sqlite or postgres")
    _announce_mode("HQ_OBS_STORE", value)
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

            maximum = int(os.environ.get("HQ_OBS_DB_POOL_MAX") or "4")
            if maximum < 1 or maximum > 20:
                raise RuntimeError("HQ_OBS_DB_POOL_MAX must be between 1 and 20")
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


def read_traces(job_id):
    """某任务的证据行，按 ``started`` 升序（与 SQLite 路径同一排序口径）。"""
    with _pool_instance().connection() as conn:
        return conn.execute(
            "SELECT * FROM ops.traces WHERE job_id = %s ORDER BY started",
            (str(job_id),),
        ).fetchall()


def search_trace_job_ids(needle):
    """按已脱敏 metadata 模糊搜索任务编号（needle 由调用方小写并带 %）。"""
    with _pool_instance().connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT job_id FROM ops.traces WHERE LOWER(metadata) LIKE %s",
            (needle,),
        ).fetchall()
    return [row["job_id"] for row in rows]


def write_trace(job_id, stage, state, started, updated, duration, metadata_json):
    """同一 (job_id, stage) 覆盖写，语义与 SQLite 的 upsert 一致。"""
    with _pool_instance().connection() as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO ops.traces
                  (job_id, stage, state, started, updated, duration, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id, stage) DO UPDATE SET
                    state = EXCLUDED.state,
                    updated = EXCLUDED.updated,
                    duration = EXCLUDED.duration,
                    metadata = EXCLUDED.metadata
                """,
                (str(job_id), stage, state, started, updated, duration, metadata_json),
            )


def enqueue_alert(event_id, payload_json, updated):
    """入队一条通知；同一 event_id 重复入队按主键忽略（INSERT OR IGNORE 等价）。"""
    with _pool_instance().connection() as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO ops.alert_outbox(event_id, payload, updated)
                VALUES (%s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (event_id, payload_json, updated),
            )


def pending_alerts(now):
    """到点可投递的通知行，按 ``updated`` 升序（与 SQLite 路径同一筛选口径）。"""
    with _pool_instance().connection() as conn:
        return conn.execute(
            "SELECT * FROM ops.alert_outbox WHERE state='pending' AND next_try<=%s "
            "ORDER BY updated",
            (now,),
        ).fetchall()


def update_alert(event_id, state, attempts, next_try, updated, error):
    """回写一次投递结果；一次调用一个事务，单行进度立即持久化。"""
    with _pool_instance().connection() as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE ops.alert_outbox SET state=%s, attempts=%s, next_try=%s, "
                "updated=%s, error=%s WHERE event_id=%s",
                (state, attempts, next_try, updated, error, event_id),
            )


def alert_counts(prefix=None):
    """{状态: 条数}，与 SQLite 的 ``GROUP BY state`` 口径一致。

    ``prefix`` 非空时只统计 ``event_id LIKE <prefix>``（如 ``'channel.%'``），
    与 SQLite 路径的 LIKE 语义一致。"""
    sql = "SELECT state, COUNT(*) AS n FROM ops.alert_outbox"
    params = ()
    if prefix:
        sql += " WHERE event_id LIKE %s"
        params = (prefix,)
    sql += " GROUP BY state"
    with _pool_instance().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {row["state"]: row["n"] for row in rows}


def close_pool() -> None:
    """测试与进程退出清理用；生产进程长驻不需要调用。"""
    global _pool
    with _pool_lock:
        current, _pool = _pool, None
    if current is not None:
        current.close()
