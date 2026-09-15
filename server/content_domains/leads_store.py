"""PostgreSQL backend for the lead follow-up CRM (crm.leads).

由 ``leads`` 在 ``HQ_LEADS_STORE=postgres`` 时调用；默认（未配置）走 leads.py 内既有的
SQLite 路径（``content-api/leads_crm.db``），行为与迁移前完全一致。

本模块绝不吞错：任何连接或执行失败都抛异常，由上层沿用既有 HTTP 报错语义
（core.py 把异常转成 400/500 给前端）。

池自建（``psycopg_pool``）而不复用仓库里的 ``server/db/postgres.py``：content_domains
整目录部署到 ``/home/ubuntu/content-api/content_domains/``（见 ``ship`` 与
``drift_sentinel.CONTENT_DOMAINS_RUNTIME``），而 ``server/db`` 没有任何运行时映射，
content-api 里并不存在该包。M3A 的 ``flags_store`` 同理。连接串仍统一取
``HQ_DATABASE_URL``。

切换纪律：同一时刻只能有一个权威。本域的两个读方进程（``huangque-content`` 与
``huangque-leadgen-api``）共用 ``/home/ubuntu/content-api/content.env`` 一份环境文件，
切换时改这一处、两个单元一起重启，禁止双权威并存。
"""

from __future__ import annotations

import logging
import os
import threading

_MODES = {"sqlite", "postgres"}
_pool = None
_pool_lock = threading.Lock()

_LIST_LIMIT = 500

_log = logging.getLogger("hq.leads_store")
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
    value = (os.environ.get("HQ_LEADS_STORE") or "sqlite").strip().lower()
    if value not in _MODES:
        raise RuntimeError("HQ_LEADS_STORE must be sqlite or postgres")
    _announce_mode("HQ_LEADS_STORE", value)
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

            maximum = int(os.environ.get("HQ_LEADS_DB_POOL_MAX") or "4")
            if maximum < 1 or maximum > 20:
                raise RuntimeError("HQ_LEADS_DB_POOL_MAX must be between 1 and 20")
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


def read_crm(username, lead_ids) -> list:
    """按账号取线索行，返回 dict 列表（键名与 SQLite 同表一致，含 username）。

    ``lead_ids`` 非空时只取这些线索；为空时按 ``updated_at`` 倒序取最近 500 条
    ——与 leads.py 的 SQLite 查询逐条对齐。
    """
    with _pool_instance().connection() as conn:
        if lead_ids:
            rows = conn.execute(
                "SELECT * FROM crm.leads WHERE username = %s AND lead_id = ANY(%s)",
                (username, list(lead_ids)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM crm.leads WHERE username = %s ORDER BY updated_at DESC LIMIT %s",
                (username, _LIST_LIMIT),
            ).fetchall()
    return [dict(row) for row in rows]


def read_one(username, lead_id):
    """取单行用于合并既有跟进状态；不存在返回 None。"""
    with _pool_instance().connection() as conn:
        row = conn.execute(
            "SELECT * FROM crm.leads WHERE username = %s AND lead_id = %s",
            (username, lead_id),
        ).fetchone()
    return dict(row) if row is not None else None


def write_crm(username, lead_id, intent, follow_status, follow_note, updated_at) -> None:
    """整行写入（INSERT ... ON CONFLICT），等价 SQLite 的 INSERT OR REPLACE。"""
    with _pool_instance().connection() as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO crm.leads
                  (username, lead_id, intent, follow_status, follow_note, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (username, lead_id) DO UPDATE SET
                    intent = EXCLUDED.intent,
                    follow_status = EXCLUDED.follow_status,
                    follow_note = EXCLUDED.follow_note,
                    updated_at = EXCLUDED.updated_at
                """,
                (username, lead_id, intent, follow_status, follow_note, int(updated_at)),
            )


def delete_crm(username, lead_ids) -> list:
    """删除这些线索，返回真正被删掉的 lead_id（不存在的不计入）。"""
    with _pool_instance().connection() as conn:
        with conn.transaction():
            rows = conn.execute(
                "DELETE FROM crm.leads WHERE username = %s AND lead_id = ANY(%s) "
                "RETURNING lead_id",
                (username, list(lead_ids)),
            ).fetchall()
    return [row["lead_id"] for row in rows]


def close_pool() -> None:
    """测试与进程退出清理用；生产进程长驻不需要调用。"""
    global _pool
    with _pool_lock:
        current, _pool = _pool, None
    if current is not None:
        current.close()
