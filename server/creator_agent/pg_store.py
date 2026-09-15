"""PostgreSQL backend for the independent Creator Agent workspace.

由 ``CreatorAgentStore``（``server/creator_agent/store.py``）在
``HQ_CREATOR_STORE=postgres`` 时调用；默认（未配置）完全走 store.py 内既有的
SQLite 路径（``/var/lib/huangque-creator-agent/creator_agent.db``），行为与迁移前
逐字节一致。

本模块绝不吞错：任何连接或执行失败都抛异常，由上层沿用既有语义（``StoreError`` /
``StateConflict`` / ``IdempotencyConflict`` / ``QuoteExpired`` → HTTP 409，
``health()`` 之外不兜底）。异常类型直接复用 store.py 的定义，保证 ``service.py``
的 ``except StoreError`` 分支对两种后端一视同仁。

连接池**自建**（懒加载 ``psycopg_pool``），刻意不 import ``server.db.postgres``：
生产 creator-agent 进程（``/opt/huangque/creator-agent/current/``，独立 venv、独立
systemd 单元）的 ``sys.path`` 里没有 ``server/`` 包，运行时会直接 import 失败。
池上限走 ``HQ_CREATOR_DB_POOL_MAX``（默认 4，取值 1~20），连接串走
``HQ_DATABASE_URL``；导入与建连都推迟到第一次真正使用 PostgreSQL 时发生。

**并发口径**：SQLite 路径用 ``BEGIN IMMEDIATE`` 拿整库写锁串行化「读-改-写」。
PostgreSQL 侧用**行锁**复现同一语义，且全局统一加锁次序（先 ``creator_batches``
行，再 ``creator_jobs`` 行 / 直接先 ``creator_workspaces`` 行），避免死锁：

* 批次读写（报价、确认、编辑、回写子任务）先锁批次行，再锁该批次的子任务行；
* 工作区读写（人设 revision、建批次）先锁工作区行。

**用量账本（``creator_model_calls``）**：写入方 ``ModelUsageGuard``
（``server/creator_agent/model_usage.py``）拿到的是 ``store.db`` 这个连接工厂，
而它按 SQLite 方言发语句（``?`` 占位符、``BEGIN IMMEDIATE``、``PRAGMA table_info``、
标量 ``MAX(a,b)``）。``model_usage.py`` 不在本域修改边界内，因此这里提供
``usage_connection()``：把那一小组语句**显式、可枚举**地翻译成 PostgreSQL，遇到
未登记的语句一律抛错（绝不静默给错结果）。见 ``_UsageConnection``。

切换纪律：同一时刻只能有一个权威。切换时 creator-agent 服务（唯一写者）整体切，
禁止双权威并存。
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
import os
import re
import threading
import time
import uuid

from .store import (
    CreatorAgentStore, IdempotencyConflict, QuoteExpired, STALE_CLAIM_SECONDS,
    StateConflict, StoreError, _digest, _json, _loads,
)

_MODES = {"sqlite", "postgres"}
_pool = None
_pool_lock = threading.Lock()

_log = logging.getLogger("hq.creator_store")
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

# 工作区更新允许的字段（与 CreatorAgentStore.update_workspace 同一张映射表）。
_WORKSPACE_COLUMNS = {
    "alias": "alias",
    "platforms": "platforms_json",
    "template_video_preferences": "preferences_json",
    "profile_overrides": "profile_overrides_json",
    "profile": "profile_json",
    "profile_state": "profile_state_json",
    "deliverables": "deliverables_json",
    "flow": "flow_json",
}

# 批次 / 子任务更新允许的字段（与 store.py 的同名方法一致）。
_BATCH_COLUMNS = {
    "status": "status", "plans": "plan_json", "quote": "quote_json",
    "confirmation_id": "confirmation_id", "goal": "goal", "topic": "topic",
}
_JOB_COLUMNS = {
    "status": "status", "input": "input_json", "quote_token": "quote_token",
    "quote": "quote_json", "confirmation_id": "confirmation_id",
    "job_id": "job_id", "result": "result_json", "error": "error",
    "refund_status": "refund_status",
}
_JSON_JOB_KEYS = {"input", "quote", "result"}
_JSON_BATCH_KEYS = {"plans", "quote"}


def mode() -> str:
    """读写权威：sqlite（默认）/ postgres；非法值直接抛错。"""
    value = (os.environ.get("HQ_CREATOR_STORE") or "sqlite").strip().lower()
    if value not in _MODES:
        raise RuntimeError("HQ_CREATOR_STORE must be sqlite or postgres")
    _announce_mode("HQ_CREATOR_STORE", value)
    return value


def enabled() -> bool:
    """是否已切换 PostgreSQL 权威。"""
    return mode() == "postgres"


def _pool_instance():
    """懒加载单例连接池；未配置 ``HQ_DATABASE_URL`` 即抛错（绝不退回 SQLite）。"""
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

            maximum = int(os.environ.get("HQ_CREATOR_DB_POOL_MAX") or "4")
            if maximum < 1 or maximum > 20:
                raise RuntimeError("HQ_CREATOR_DB_POOL_MAX must be between 1 and 20")
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


@contextmanager
def _connection():
    """读路径：借一个池连接（无显式事务，失败一律抛错）。"""
    with _pool_instance().connection() as conn:
        yield conn


@contextmanager
def _transaction():
    """写路径：一个池连接 + 一个显式事务，异常自动回滚。"""
    with _pool_instance().connection() as conn:
        with conn.transaction():
            yield conn


def close_pool() -> None:
    """测试与进程退出清理用；生产进程长驻不需要调用。"""
    global _pool
    with _pool_lock:
        current, _pool = _pool, None
    if current is not None:
        current.close()


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


def health() -> bool:
    """等价 SQLite 路径：能读、能写（写测试回滚，绝不落哨兵行）。"""
    try:
        with _connection() as conn:
            conn.execute("SELECT 1 FROM agent.creator_account_state LIMIT 1").fetchone()
            conn.execute(
                "INSERT INTO agent.creator_account_state"
                "(username, active_project_id, updated_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (username) DO UPDATE SET updated_at = EXCLUDED.updated_at",
                ("__creator_health__", "", int(time.time())),
            )
            conn.rollback()
        return True
    except Exception:
        # 与 SQLite 路径同口径：任何连接/执行失败都只是「不健康」，由 /health 暴露
        return False


# ---------------------------------------------------------------------------
# 账号状态与工作区
# ---------------------------------------------------------------------------


def set_active_project(username, project_id) -> None:
    now = int(time.time())
    with _transaction() as conn:
        conn.execute(
            "INSERT INTO agent.creator_account_state"
            "(username, active_project_id, updated_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (username) DO UPDATE SET "
            "active_project_id = EXCLUDED.active_project_id, "
            "updated_at = EXCLUDED.updated_at",
            (username, project_id, now),
        )


def active_project(username) -> str:
    with _connection() as conn:
        row = conn.execute(
            "SELECT active_project_id FROM agent.creator_account_state WHERE username=%s",
            (username,),
        ).fetchone()
    return str(row["active_project_id"] or "") if row else ""


def ensure_workspace(username, project_id, alias=""):
    now = int(time.time())
    with _transaction() as conn:
        conn.execute(
            "INSERT INTO agent.creator_workspaces"
            "(username, project_id, alias, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (username, project_id) DO NOTHING",
            (username, project_id, str(alias or "")[:120], now, now),
        )
    return workspace(username, project_id)


def workspace(username, project_id):
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM agent.creator_workspaces WHERE username=%s AND project_id=%s",
            (username, project_id),
        ).fetchone()
    return CreatorAgentStore._workspace(row)


def workspaces(username):
    with _connection() as conn:
        rows = conn.execute(
            "SELECT * FROM agent.creator_workspaces WHERE username=%s "
            "ORDER BY updated_at DESC, created_at ASC",
            (username,),
        ).fetchall()
    return [CreatorAgentStore._workspace(row) for row in rows]


def update_workspace(username, project_id, **changes):
    mapping = _WORKSPACE_COLUMNS
    if not changes or set(changes) - set(mapping):
        raise StoreError("invalid workspace update")
    values = {}
    for key, value in changes.items():
        values[mapping[key]] = str(value or "")[:120] if key == "alias" else _json(value)
    values["updated_at"] = int(time.time())
    fields = ",".join("%s=%%s" % key for key in values)
    with _transaction() as conn:
        changed = conn.execute(
            "UPDATE agent.creator_workspaces SET %s WHERE username=%%s AND project_id=%%s"
            % fields,
            tuple(values.values()) + (username, project_id),
        )
        if changed.rowcount != 1:
            raise StoreError("workspace not found")
    return workspace(username, project_id)


def commit_profile_opening(username, project_id, state, reply, public, source_key,
                           flow=None):
    now = int(time.time())
    with _transaction() as conn:
        existing = conn.execute(
            "SELECT id FROM agent.creator_messages "
            "WHERE username=%s AND project_id=%s AND source_key=%s",
            (username, project_id, source_key),
        ).fetchone()
        if not existing:
            assignments = ["profile_state_json=%s", "updated_at=%s"]
            values = [_json(state), now]
            if flow is not None:
                assignments.append("flow_json=%s")
                values.append(_json(flow))
            changed = conn.execute(
                "UPDATE agent.creator_workspaces SET %s WHERE username=%%s AND project_id=%%s"
                % ",".join(assignments),
                tuple(values) + (username, project_id),
            )
            if changed.rowcount != 1:
                raise StoreError("workspace not found")
            conn.execute(
                "INSERT INTO agent.creator_messages"
                "(username, project_id, role, content, source_key, request_id,"
                " request_hash, public_json, created_at) "
                "VALUES (%s, %s, 'assistant', %s, %s, NULL, '', %s, %s)",
                (username, project_id, str(reply or "")[:8000], source_key,
                 _json(public or {}), now),
            )
    return workspace(username, project_id)


def update_profile_state(username, project_id, state, expected_revision, *,
                         profile=None, profile_overrides=None,
                         deliverables=None, flow=None):
    now = int(time.time())
    with _transaction() as conn:
        row = conn.execute(
            "SELECT profile_state_json FROM agent.creator_workspaces "
            "WHERE username=%s AND project_id=%s FOR UPDATE",
            (username, project_id),
        ).fetchone()
        if not row:
            raise StoreError("workspace not found")
        current = _loads(row["profile_state_json"], {})
        if int(current.get("revision") or 1) != int(expected_revision):
            raise StateConflict("profile revision changed")
        if int(state.get("revision") or 0) != int(expected_revision) + 1:
            raise StateConflict("profile revision did not advance exactly once")
        assignments = ["profile_state_json=%s", "updated_at=%s"]
        values = [_json(state), now]
        for column, value in (
            ("profile_json", profile),
            ("profile_overrides_json", profile_overrides),
            ("deliverables_json", deliverables),
            ("flow_json", flow),
        ):
            if value is not None:
                assignments.append(column + "=%s")
                values.append(_json(value))
        changed = conn.execute(
            "UPDATE agent.creator_workspaces SET %s WHERE username=%%s AND project_id=%%s"
            % ",".join(assignments),
            tuple(values) + (username, project_id),
        )
        if changed.rowcount != 1:
            raise StateConflict("profile update lost")
    return workspace(username, project_id)


def commit_profile_turn(username, project_id, user_message_id, state,
                        expected_revision, reply, public, *, profile=None,
                        profile_overrides=None, deliverables=None, flow=None,
                        fault_hook=None):
    now = int(time.time())
    with _transaction() as conn:
        row = conn.execute(
            "SELECT profile_state_json FROM agent.creator_workspaces "
            "WHERE username=%s AND project_id=%s FOR UPDATE",
            (username, project_id),
        ).fetchone()
        if not row:
            raise StoreError("workspace not found")
        current = _loads(row["profile_state_json"], {})
        if int(current.get("revision") or 1) != int(expected_revision):
            raise StateConflict("profile revision changed")
        if int(state.get("revision") or 0) != int(expected_revision) + 1:
            raise StateConflict("profile revision did not advance exactly once")
        assignments = ["profile_state_json=%s", "updated_at=%s"]
        values = [_json(state), now]
        for column, value in (
            ("profile_json", profile),
            ("profile_overrides_json", profile_overrides),
            ("deliverables_json", deliverables),
            ("flow_json", flow),
        ):
            if value is not None:
                assignments.append(column + "=%s")
                values.append(_json(value))
        conn.execute(
            "UPDATE agent.creator_workspaces SET %s WHERE username=%%s AND project_id=%%s"
            % ",".join(assignments),
            tuple(values) + (username, project_id),
        )
        if fault_hook:
            fault_hook("after_state")
        assistant = conn.execute(
            "INSERT INTO agent.creator_messages"
            "(username, project_id, role, content, source_key, request_id,"
            " request_hash, public_json, created_at) "
            "VALUES (%s, %s, 'assistant', %s, %s, NULL, '', %s, %s) RETURNING id",
            (username, project_id, str(reply or "")[:8000],
             "profile-turn:%d" % int(user_message_id), _json(public or {}), now),
        ).fetchone()
        if fault_hook:
            fault_hook("after_assistant")
        turn = {
            "reply": str(reply or "")[:8000],
            "message_public": public or {},
            "assistant_message_id": int(assistant["id"]),
        }
        changed = conn.execute(
            "UPDATE agent.creator_messages SET public_json=%s "
            "WHERE id=%s AND username=%s AND project_id=%s AND role='user'",
            (_json({"turn": turn}), int(user_message_id), username, project_id),
        )
        if changed.rowcount != 1:
            raise StateConflict("profile user request claim disappeared")
        if fault_hook:
            fault_hook("before_commit")
    return turn


def commit_message_turn(username, project_id, user_message_id, reply, public,
                        fault_hook=None):
    now = int(time.time())
    source_key = "message-turn:%d" % int(user_message_id)
    with _transaction() as conn:
        user_row = conn.execute(
            "SELECT role FROM agent.creator_messages "
            "WHERE id=%s AND username=%s AND project_id=%s FOR UPDATE",
            (int(user_message_id), username, project_id),
        ).fetchone()
        if not user_row or user_row["role"] != "user":
            raise StateConflict("user request claim disappeared")
        assistant = conn.execute(
            "SELECT * FROM agent.creator_messages "
            "WHERE username=%s AND project_id=%s AND source_key=%s",
            (username, project_id, source_key),
        ).fetchone()
        if not assistant:
            assistant = conn.execute(
                "INSERT INTO agent.creator_messages"
                "(username, project_id, role, content, source_key, request_id,"
                " request_hash, public_json, created_at) "
                "VALUES (%s, %s, 'assistant', %s, %s, NULL, '', %s, %s) RETURNING *",
                (username, project_id, str(reply or "")[:8000], source_key,
                 _json(public or {}), now),
            ).fetchone()
        if fault_hook:
            fault_hook("after_assistant")
        turn = {
            "reply": str(assistant["content"] or "")[:8000],
            "message_public": _loads(assistant["public_json"], {}),
            "assistant_message_id": int(assistant["id"]),
        }
        changed = conn.execute(
            "UPDATE agent.creator_messages SET public_json=%s "
            "WHERE id=%s AND username=%s AND project_id=%s AND role='user'",
            (_json({"turn": turn}), int(user_message_id), username, project_id),
        )
        if changed.rowcount != 1:
            raise StateConflict("user request turn commit lost")
        if fault_hook:
            fault_hook("before_commit")
    return turn


# ---------------------------------------------------------------------------
# 会话消息
# ---------------------------------------------------------------------------


def add_message(username, project_id, role, content, *, source_key=None,
                request_id=None, request_hash="", public=None, created_at=None):
    now = int(created_at or time.time())
    with _transaction() as conn:
        if not conn.execute(
            "SELECT 1 FROM agent.creator_workspaces WHERE username=%s AND project_id=%s",
            (username, project_id),
        ).fetchone():
            raise StoreError("workspace not found")
        row = conn.execute(
            "INSERT INTO agent.creator_messages"
            "(username, project_id, role, content, source_key, request_id,"
            " request_hash, public_json, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING *",
            (username, project_id, role, content, source_key, request_id,
             str(request_hash or ""), _json(public or {}), now),
        ).fetchone()
        if row is None:
            # 撞上 (username,project_id,request_id) 或 (username,project_id,source_key)
            # 唯一键：按幂等键回读既有行，语义与 SQLite 捕获 IntegrityError 一致。
            if request_id:
                row = conn.execute(
                    "SELECT * FROM agent.creator_messages "
                    "WHERE username=%s AND project_id=%s AND request_id=%s",
                    (username, project_id, request_id),
                ).fetchone()
            elif source_key:
                row = conn.execute(
                    "SELECT * FROM agent.creator_messages "
                    "WHERE username=%s AND project_id=%s AND source_key=%s",
                    (username, project_id, source_key),
                ).fetchone()
            else:
                raise StoreError("message insert conflict without idempotency key")
            if row is None:
                raise StoreError("message insert conflict without matching row")
            if request_id and str(row["request_hash"] or "") != str(request_hash or ""):
                raise IdempotencyConflict("request_id is bound to different input")
            return CreatorAgentStore._message(row), False
        conn.execute(
            "UPDATE agent.creator_workspaces SET updated_at=%s "
            "WHERE username=%s AND project_id=%s",
            (now, username, project_id),
        )
    return CreatorAgentStore._message(row), True


def messages(username, project_id, limit=300):
    with _connection() as conn:
        rows = conn.execute(
            "SELECT * FROM agent.creator_messages WHERE username=%s AND project_id=%s "
            "ORDER BY id DESC LIMIT %s",
            (username, project_id, max(1, min(800, int(limit)))),
        ).fetchall()
    return [CreatorAgentStore._message(row) for row in reversed(rows)]


def update_message_public(username, message_id, public):
    with _transaction() as conn:
        changed = conn.execute(
            "UPDATE agent.creator_messages SET public_json=%s WHERE id=%s AND username=%s",
            (_json(public or {}), int(message_id), username),
        )
        if changed.rowcount != 1:
            raise StoreError("message not found")


def delete_message_if_unanswered(username, message_id):
    with _transaction() as conn:
        row = conn.execute(
            "SELECT role, public_json FROM agent.creator_messages "
            "WHERE id=%s AND username=%s FOR UPDATE",
            (int(message_id), username),
        ).fetchone()
        public = _loads(row["public_json"], {}) if row else {}
        if (
            row and row["role"] == "user"
            and not (public or {}).get("response")
            and not (public or {}).get("turn")
        ):
            conn.execute(
                "DELETE FROM agent.creator_messages WHERE id=%s AND username=%s",
                (int(message_id), username),
            )
            return True
    return False


# ---------------------------------------------------------------------------
# 批次与子任务
# ---------------------------------------------------------------------------


def create_batch(username, project_id, topic, goal, platform_plans,
                 source_message_id=0):
    if not platform_plans:
        raise StoreError("platform plans are required")
    now = int(time.time())
    batch_id = "creator_batch_" + uuid.uuid4().hex
    plan_hash = _digest(platform_plans)
    source_message_id = max(0, int(source_message_id or 0))
    duplicate_id = None
    with _transaction() as conn:
        # 先锁工作区行：既串行化「同一消息只能建一个批次」的抢占，也让
        # insert_seq / version 的「先查最大值再写入」不会互相踩。
        conn.execute(
            "SELECT username FROM agent.creator_workspaces "
            "WHERE username=%s AND project_id=%s FOR UPDATE",
            (username, project_id),
        ).fetchone()
        if source_message_id:
            existing = conn.execute(
                "SELECT id FROM agent.creator_batches "
                "WHERE username=%s AND project_id=%s AND source_message_id=%s",
                (username, project_id, source_message_id),
            ).fetchone()
            if existing:
                duplicate_id = existing["id"]
        if duplicate_id is None:
            next_seq = conn.execute(
                "SELECT COALESCE(MAX(insert_seq), 0) + 1 AS next_seq "
                "FROM agent.creator_batches WHERE username=%s AND project_id=%s",
                (username, project_id),
            ).fetchone()["next_seq"]
            conn.execute(
                "INSERT INTO agent.creator_batches"
                "(id, username, project_id, insert_seq, source_message_id, topic, goal,"
                " status, plan_json, plan_hash, revision, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (batch_id, username, project_id, int(next_seq), source_message_id,
                 topic, goal, "draft", _json(platform_plans), plan_hash, 1, now, now),
            )
            for plan in platform_plans:
                platform = str(plan.get("platform") or "")
                previous = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS max_version FROM agent.creator_jobs "
                    "WHERE username=%s AND project_id=%s AND platform=%s "
                    "AND status NOT IN ('draft','ready','quoted','failed_submission')",
                    (username, project_id, platform),
                ).fetchone()["max_version"]
                conn.execute(
                    "INSERT INTO agent.creator_jobs"
                    "(id, batch_id, username, project_id, platform, version, status,"
                    " input_json, input_hash, idempotency_key, revision, created_at,"
                    " updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    ("creator_job_" + uuid.uuid4().hex, batch_id, username, project_id,
                     platform, int(previous) + 1, "draft", _json(plan.get("input") or {}),
                     _digest(plan.get("input") or {}), "creator-agent-" + uuid.uuid4().hex,
                     1, now, now),
                )
    return batch(username, duplicate_id or batch_id, include_private=True)


def batch_for_source_message(username, project_id, message_id):
    if int(message_id or 0) <= 0:
        return None
    with _connection() as conn:
        row = conn.execute(
            "SELECT id FROM agent.creator_batches "
            "WHERE username=%s AND project_id=%s AND source_message_id=%s",
            (username, project_id, int(message_id)),
        ).fetchone()
    return batch(username, row["id"], include_private=True) if row else None


def batch_for_mutation_message(username, project_id, message_id):
    if int(message_id or 0) <= 0:
        return None
    with _connection() as conn:
        row = conn.execute(
            "SELECT id FROM agent.creator_batches "
            "WHERE username=%s AND project_id=%s AND last_mutation_message_id=%s",
            (username, project_id, int(message_id)),
        ).fetchone()
    return batch(username, row["id"], include_private=True) if row else None


def batch(username, batch_id, include_private=False):
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM agent.creator_batches WHERE id=%s AND username=%s",
            (batch_id, username),
        ).fetchone()
        jobs = conn.execute(
            "SELECT * FROM agent.creator_jobs WHERE batch_id=%s AND username=%s "
            "ORDER BY platform",
            (batch_id, username),
        ).fetchall() if row else []
    value = CreatorAgentStore._batch_row(row)
    if value is not None:
        value["jobs"] = [CreatorAgentStore._job(item, include_private) for item in jobs]
        if not include_private:
            value.pop("confirmation_id", None)
        else:
            value.update({
                "plan_hash": row["plan_hash"],
                "quoted_revision": int(row["quoted_revision"]),
                "claim_id": row["claim_id"],
                "source_message_id": int(row["source_message_id"] or 0),
                "last_mutation_message_id": int(row["last_mutation_message_id"] or 0),
            })
    return value


def batches(username, project_id, limit=30):
    with _connection() as conn:
        rows = conn.execute(
            "SELECT id FROM agent.creator_batches WHERE username=%s AND project_id=%s "
            "ORDER BY created_at DESC, insert_seq DESC LIMIT %s",
            (username, project_id, max(1, min(100, int(limit)))),
        ).fetchall()
    return [batch(username, row["id"]) for row in rows]


def latest_batch(username, project_id, include_private=False):
    with _connection() as conn:
        row = conn.execute(
            "SELECT id FROM agent.creator_batches WHERE username=%s AND project_id=%s "
            "ORDER BY created_at DESC, insert_seq DESC LIMIT 1",
            (username, project_id),
        ).fetchone()
    return batch(username, row["id"], include_private) if row else None


def _lock_batch(conn, username, batch_id):
    """锁住批次行与它的全部子任务行；所有批次写路径的第一把锁。"""
    row = conn.execute(
        "SELECT * FROM agent.creator_batches WHERE id=%s AND username=%s FOR UPDATE",
        (batch_id, username),
    ).fetchone()
    jobs = conn.execute(
        "SELECT * FROM agent.creator_jobs WHERE batch_id=%s AND username=%s "
        "ORDER BY platform FOR UPDATE",
        (batch_id, username),
    ).fetchall() if row else []
    return row, jobs


def _clear_quote_locked(conn, username, batch_id, now) -> None:
    conn.execute(
        "UPDATE agent.creator_jobs SET status='ready', quote_token='', quote_json='{}',"
        " quote_cost=0, quote_expires_at=0, confirmation_id='', job_id='',"
        " result_json='{}', error='', refund_status='', submit_input_json='{}',"
        " submit_input_hash='', submit_quote_token='', submit_quote_cost=0,"
        " submit_quote_expires_at=0, submit_idempotency_key='',"
        " revision=revision+1, updated_at=%s"
        " WHERE batch_id=%s AND username=%s AND status='quoted'",
        (now, batch_id, username),
    )
    conn.execute(
        "UPDATE agent.creator_batches SET status='ready', quote_json='{}',"
        " quote_expires_at=0, confirmation_id='', quoted_revision=0, claim_id='',"
        " updated_at=%s WHERE id=%s AND username=%s AND status='quoted'",
        (now, batch_id, username),
    )


def claim_quote(username, batch_id, expected_revision, now=None,
                minimum_validity=0):
    claim_id = "quote_" + uuid.uuid4().hex
    now = int(time.time() if now is None else now)
    reused = False
    with _transaction() as conn:
        row, jobs = _lock_batch(conn, username, batch_id)
        if not row:
            raise StoreError("batch not found")
        if (
            row["status"] == "quoting" and row["claim_id"]
            and int(row["updated_at"] or 0) <= now - STALE_CLAIM_SECONDS
        ):
            conn.execute(
                "UPDATE agent.creator_batches SET status='ready', claim_id='',"
                " updated_at=%s WHERE id=%s AND username=%s AND status='quoting'"
                " AND claim_id=%s",
                (now, batch_id, username, row["claim_id"]),
            )
            row, jobs = _lock_batch(conn, username, batch_id)
        if int(row["revision"]) != int(expected_revision):
            raise StateConflict("batch revision changed")
        if row["status"] == "quoted":
            if CreatorAgentStore._quote_valid(row, jobs, now, minimum_validity):
                reused = True
            else:
                _clear_quote_locked(conn, username, batch_id, now)
                row, jobs = _lock_batch(conn, username, batch_id)
        if not reused:
            if row["status"] not in {"draft", "ready"} or row["claim_id"]:
                raise StateConflict("batch is not quoteable")
            plans = _loads(row["plan_json"], [])
            if row["plan_hash"] != _digest(plans):
                raise StateConflict("batch plan hash changed")
            if not jobs or any(
                item["input_hash"] != _digest(_loads(item["input_json"], {}))
                or item["status"] not in {"draft", "ready"}
                for item in jobs
            ):
                raise StateConflict("batch jobs are not quoteable")
            changed = conn.execute(
                "UPDATE agent.creator_batches SET status='quoting', claim_id=%s,"
                " updated_at=%s WHERE id=%s AND username=%s AND revision=%s"
                " AND status IN ('draft','ready') AND claim_id=''",
                (claim_id, now, batch_id, username, int(expected_revision)),
            )
            if changed.rowcount != 1:
                raise StateConflict("batch quote claim lost")
    value = batch(username, batch_id, include_private=True)
    if reused:
        value["quote_reused"] = True
        return value
    value["claim_id"] = claim_id
    value["quote_reused"] = False
    return value


def finish_quote(username, batch_id, claim_id, job_quotes, quote, now=None):
    now = int(time.time() if now is None else now)
    with _transaction() as conn:
        row, jobs = _lock_batch(conn, username, batch_id)
        if not row or row["status"] != "quoting" or row["claim_id"] != claim_id:
            raise StateConflict("quote claim is stale")
        quotes = {str(item.get("id") or ""): item for item in job_quotes or []}
        if set(quotes) != {job["id"] for job in jobs}:
            raise StateConflict("quote set is incomplete")
        expirations = []
        for job in jobs:
            item = quotes[job["id"]]
            try:
                cost = int(item.get("cost") or 0)
                expires_at = int(item.get("expires_at") or 0)
            except (TypeError, ValueError):
                cost, expires_at = 0, 0
            if (
                not item.get("quote_token")
                or item.get("input_hash") != job["input_hash"]
                or cost <= 0 or expires_at <= now
            ):
                raise StateConflict("quote does not match current input")
            expirations.append(expires_at)
            changed = conn.execute(
                "UPDATE agent.creator_jobs SET status='quoted', quote_token=%s,"
                " quote_json=%s, quote_cost=%s, quote_expires_at=%s, error='',"
                " revision=revision+1, updated_at=%s"
                " WHERE id=%s AND username=%s AND revision=%s"
                " AND status IN ('draft','ready') AND input_hash=%s",
                (item["quote_token"], _json(item.get("quote") or {}), cost, expires_at,
                 now, job["id"], username, int(job["revision"]), job["input_hash"]),
            )
            if changed.rowcount != 1:
                raise StateConflict("job quote finalize lost")
        earliest_expiry = min(expirations)
        frozen_quote = dict(quote or {})
        frozen_quote["expires_at"] = earliest_expiry
        frozen_quote["expires_in"] = max(0, earliest_expiry - now)
        changed = conn.execute(
            "UPDATE agent.creator_batches SET status='quoted', quote_json=%s,"
            " quote_expires_at=%s, quoted_revision=revision, claim_id='', updated_at=%s"
            " WHERE id=%s AND username=%s AND status='quoting' AND claim_id=%s",
            (_json(frozen_quote), earliest_expiry, now, batch_id, username, claim_id),
        )
        if changed.rowcount != 1:
            raise StateConflict("batch quote finalize lost")
    return batch(username, batch_id)


def abort_quote(username, batch_id, claim_id):
    with _transaction() as conn:
        conn.execute(
            "UPDATE agent.creator_batches SET status='ready', claim_id='', updated_at=%s"
            " WHERE id=%s AND username=%s AND status='quoting' AND claim_id=%s",
            (int(time.time()), batch_id, username, claim_id),
        )


def claim_confirmation(username, batch_id, confirmation_id, expected_revision,
                       expected_quote_expires_at, now=None, safety_margin_seconds=0):
    now = int(time.time() if now is None else now)
    claimed_ids = []
    with _transaction() as conn:
        row, jobs = _lock_batch(conn, username, batch_id)
        if not row:
            raise StoreError("batch not found")
        if int(row["revision"]) != int(expected_revision):
            raise StateConflict("batch revision changed")
        if int(row["quote_expires_at"] or 0) != int(expected_quote_expires_at):
            raise QuoteExpired("batch quote changed")
        existing_confirmation = str(row["confirmation_id"] or "")
        if existing_confirmation:
            if existing_confirmation != confirmation_id:
                raise IdempotencyConflict("confirmation_id conflict")
            for job in jobs:
                if job["status"] != "submission_unknown":
                    continue
                changed = conn.execute(
                    "UPDATE agent.creator_jobs SET status='submit_claimed',"
                    " revision=revision+1, updated_at=%s"
                    " WHERE id=%s AND username=%s AND revision=%s"
                    " AND status='submission_unknown'",
                    (now, job["id"], username, int(job["revision"])),
                )
                if changed.rowcount == 1:
                    claimed_ids.append(job["id"])
        else:
            if (
                row["status"] != "quoted"
                or int(row["quoted_revision"]) != int(row["revision"])
                or row["plan_hash"] != _digest(_loads(row["plan_json"], []))
                or not jobs
            ):
                raise StateConflict("batch quote is stale")
            if any(
                job["status"] != "quoted"
                or not job["quote_token"]
                or int(job["quote_cost"] or 0) <= 0
                or job["input_hash"] != _digest(_loads(job["input_json"], {}))
                for job in jobs
            ):
                raise StateConflict("job quote is incomplete")
            if not CreatorAgentStore._quote_valid(row, jobs, now, safety_margin_seconds):
                raise QuoteExpired("batch quote has insufficient validity")
            changed = conn.execute(
                "UPDATE agent.creator_batches SET status='submitting', confirmation_id=%s,"
                " claim_id='', updated_at=%s WHERE id=%s AND username=%s AND revision=%s"
                " AND status='quoted' AND quoted_revision=revision",
                (confirmation_id, now, batch_id, username, int(expected_revision)),
            )
            if changed.rowcount != 1:
                raise StateConflict("confirmation claim lost")
            for job in jobs:
                changed = conn.execute(
                    "UPDATE agent.creator_jobs SET status='submit_claimed',"
                    " confirmation_id=%s, submit_input_json=input_json,"
                    " submit_input_hash=input_hash, submit_quote_token=quote_token,"
                    " submit_quote_cost=quote_cost,"
                    " submit_quote_expires_at=quote_expires_at,"
                    " submit_idempotency_key=idempotency_key,"
                    " revision=revision+1, updated_at=%s"
                    " WHERE id=%s AND username=%s AND revision=%s AND status='quoted'"
                    " AND input_hash=%s",
                    (confirmation_id + ":" + job["platform"], now, job["id"], username,
                     int(job["revision"]), job["input_hash"]),
                )
                if changed.rowcount != 1:
                    raise StateConflict("job confirmation claim lost")
                claimed_ids.append(job["id"])
    result = batch(username, batch_id, include_private=True)
    result["claimed_jobs"] = [
        job for job in result["jobs"] if job["id"] in set(claimed_ids)
    ]
    return result


def claim_recovery(username, batch_id, now=None):
    now = int(time.time() if now is None else now)
    claimed_ids = []
    with _transaction() as conn:
        row, jobs = _lock_batch(conn, username, batch_id)
        if not row or not row["confirmation_id"]:
            return []
        for job in jobs:
            recoverable = job["status"] == "submission_unknown" or (
                job["status"] == "submit_claimed"
                and int(job["updated_at"] or 0) <= now - STALE_CLAIM_SECONDS
            )
            if not recoverable:
                continue
            changed = conn.execute(
                "UPDATE agent.creator_jobs SET status='submit_claimed',"
                " revision=revision+1, updated_at=%s"
                " WHERE id=%s AND username=%s AND revision=%s"
                " AND status IN ('submission_unknown','submit_claimed')",
                (now, job["id"], username, int(job["revision"])),
            )
            if changed.rowcount == 1:
                claimed_ids.append(job["id"])
    result = batch(username, batch_id, include_private=True)
    return [job for job in result["jobs"] if job["id"] in set(claimed_ids)]


def finish_submit_claim(username, record_id, expected_revision, *, status,
                        job_id="", result=None, error="", refund_status=""):
    now = int(time.time())
    with _transaction() as conn:
        row = conn.execute(
            "SELECT batch_id FROM agent.creator_jobs WHERE id=%s AND username=%s",
            (record_id, username),
        ).fetchone()
        if not row:
            return False
        # 统一加锁次序：先批次行、再子任务行（与 claim_* 一致，避免死锁）。
        _lock_batch(conn, username, row["batch_id"])
        current = conn.execute(
            "SELECT status, revision FROM agent.creator_jobs WHERE id=%s AND username=%s",
            (record_id, username),
        ).fetchone()
        if (not current or current["status"] != "submit_claimed"
                or int(current["revision"]) != int(expected_revision)):
            return False
        conn.execute(
            "UPDATE agent.creator_jobs SET status=%s, job_id=%s, result_json=%s,"
            " error=%s, refund_status=%s, revision=revision+1, updated_at=%s"
            " WHERE id=%s AND username=%s AND revision=%s AND status='submit_claimed'",
            (status, str(job_id or ""), _json(result or {}), str(error or "")[:500],
             str(refund_status or ""), now, record_id, username, int(expected_revision)),
        )
        batch_row, jobs = _lock_batch(conn, username, row["batch_id"])
        derived = CreatorAgentStore._derived_batch_status(jobs, batch_row["status"])
        conn.execute(
            "UPDATE agent.creator_batches SET status=%s, updated_at=%s "
            "WHERE id=%s AND username=%s",
            (derived, now, row["batch_id"], username),
        )
    return True


def finish_task_poll(username, record_id, expected_revision, *, status,
                     result=None, error="", refund_status=""):
    now = int(time.time())
    allowed = {"submitted", "queued", "running", "verifying", "processing"}
    with _transaction() as conn:
        row = conn.execute(
            "SELECT batch_id, status, revision FROM agent.creator_jobs "
            "WHERE id=%s AND username=%s",
            (record_id, username),
        ).fetchone()
        if not row:
            return False
        _lock_batch(conn, username, row["batch_id"])
        current = conn.execute(
            "SELECT status, revision FROM agent.creator_jobs WHERE id=%s AND username=%s",
            (record_id, username),
        ).fetchone()
        if (not current or current["status"] not in allowed
                or int(current["revision"]) != int(expected_revision)):
            return False
        conn.execute(
            "UPDATE agent.creator_jobs SET status=%s, result_json=%s, error=%s,"
            " refund_status=%s, revision=revision+1, updated_at=%s"
            " WHERE id=%s AND username=%s AND revision=%s"
            " AND status IN ('submitted','queued','running','verifying','processing')",
            (status, _json(result or {}), str(error or "")[:500], str(refund_status or ""),
             now, record_id, username, int(expected_revision)),
        )
        batch_row, jobs = _lock_batch(conn, username, row["batch_id"])
        derived = CreatorAgentStore._derived_batch_status(jobs, batch_row["status"])
        conn.execute(
            "UPDATE agent.creator_batches SET status=%s, updated_at=%s "
            "WHERE id=%s AND username=%s",
            (derived, now, row["batch_id"], username),
        )
    return True


def recompute_batch(username, batch_id):
    with _transaction() as conn:
        row, jobs = _lock_batch(conn, username, batch_id)
        if not row:
            raise StoreError("batch not found")
        derived = CreatorAgentStore._derived_batch_status(jobs, row["status"])
        conn.execute(
            "UPDATE agent.creator_batches SET status=%s, updated_at=%s "
            "WHERE id=%s AND username=%s",
            (derived, int(time.time()), batch_id, username),
        )
    return batch(username, batch_id)


def update_batch(username, batch_id, **changes):
    mapping = _BATCH_COLUMNS
    if not changes or set(changes) - set(mapping):
        raise StoreError("invalid batch update")
    values = {}
    for key, value in changes.items():
        values[mapping[key]] = _json(value) if key in _JSON_BATCH_KEYS else value
    values["updated_at"] = int(time.time())
    fields = ",".join("%s=%%s" % key for key in values)
    with _transaction() as conn:
        changed = conn.execute(
            "UPDATE agent.creator_batches SET %s WHERE id=%%s AND username=%%s" % fields,
            tuple(values.values()) + (batch_id, username),
        )
        if changed.rowcount != 1:
            raise StoreError("batch not found")
    return batch(username, batch_id, include_private=True)


def update_job(username, record_id, **changes):
    mapping = _JOB_COLUMNS
    if not changes or set(changes) - set(mapping):
        raise StoreError("invalid job update")
    values = {}
    for key, value in changes.items():
        values[mapping[key]] = _json(value) if key in _JSON_JOB_KEYS else value
    values["updated_at"] = int(time.time())
    fields = ",".join("%s=%%s" % key for key in values)
    with _transaction() as conn:
        changed = conn.execute(
            "UPDATE agent.creator_jobs SET %s WHERE id=%%s AND username=%%s" % fields,
            tuple(values.values()) + (record_id, username),
        )
        if changed.rowcount != 1:
            raise StoreError("job not found")
        row = conn.execute(
            "SELECT * FROM agent.creator_jobs WHERE id=%s AND username=%s",
            (record_id, username),
        ).fetchone()
    return CreatorAgentStore._job(row, include_private=True)


def replace_batch_plans(username, batch_id, platform_plans, expected_revision,
                        mutation_message_id=0):
    now = int(time.time())
    incoming = {str(item.get("platform") or ""): item for item in platform_plans}
    mutation_message_id = max(0, int(mutation_message_id or 0))
    replay_id = None
    with _transaction() as conn:
        row, jobs = _lock_batch(conn, username, batch_id)
        if not row:
            raise StoreError("batch not found")
        if (
            mutation_message_id
            and int(row["last_mutation_message_id"] or 0) == mutation_message_id
        ):
            replay_id = batch_id
        else:
            if int(row["revision"]) != int(expected_revision):
                raise StateConflict("batch revision changed")
            if row["status"] not in {"draft", "ready", "quoted"} or row["claim_id"]:
                raise StateConflict("batch is not editable")
            if set(incoming) != {job["platform"] for job in jobs}:
                raise StateConflict("platform set cannot change during revision")
            next_revision = int(row["revision"]) + 1
            for job in jobs:
                tool_input = incoming[job["platform"]].get("input") or {}
                changed = conn.execute(
                    "UPDATE agent.creator_jobs SET status='draft', input_json=%s,"
                    " input_hash=%s, quote_token='', quote_json='{}', quote_cost=0,"
                    " quote_expires_at=0, confirmation_id='', job_id='', result_json='{}',"
                    " error='', refund_status='', submit_input_json='{}',"
                    " submit_input_hash='', submit_quote_token='', submit_quote_cost=0,"
                    " submit_quote_expires_at=0, submit_idempotency_key='',"
                    " revision=revision+1, updated_at=%s"
                    " WHERE id=%s AND username=%s AND revision=%s"
                    " AND status IN ('draft','ready','quoted')",
                    (_json(tool_input), _digest(tool_input), now, job["id"], username,
                     int(job["revision"])),
                )
                if changed.rowcount != 1:
                    raise StateConflict("job edit claim lost")
            changed = conn.execute(
                "UPDATE agent.creator_batches SET status='ready', plan_json=%s,"
                " plan_hash=%s, quote_json='{}', quote_expires_at=0, confirmation_id='',"
                " quoted_revision=0, claim_id='', revision=%s,"
                " last_mutation_message_id=%s, updated_at=%s"
                " WHERE id=%s AND username=%s AND revision=%s"
                " AND status IN ('draft','ready','quoted') AND claim_id=''",
                (_json(platform_plans), _digest(platform_plans), next_revision,
                 mutation_message_id, now, batch_id, username, int(expected_revision)),
            )
            if changed.rowcount != 1:
                raise StateConflict("batch edit claim lost")
    return batch(username, replay_id or batch_id, include_private=True)


# ---------------------------------------------------------------------------
# 用量账本：ModelUsageGuard 的连接工厂
# ---------------------------------------------------------------------------

_USAGE_BEGIN = re.compile(r"^BEGIN\s+IMMEDIATE$", re.I)
_USAGE_TABLE_INFO = re.compile(
    r"^PRAGMA\s+table_info\(\s*creator_model_calls\s*\)$", re.I)
_USAGE_TABLE_NAME = re.compile(r"(?<![\w.])creator_model_calls\b")
class _UsageResult:
    """``sqlite3.Cursor`` 的最小等价物：守卫只用 ``fetchone`` / ``fetchall``。"""

    def __init__(self, cursor=None):
        self._cursor = cursor

    def fetchone(self):
        return None if self._cursor is None else self._cursor.fetchone()

    def fetchall(self):
        return [] if self._cursor is None else self._cursor.fetchall()

    def __iter__(self):
        return iter(()) if self._cursor is None else iter(self._cursor)

    @property
    def rowcount(self):
        return 0 if self._cursor is None else self._cursor.rowcount


class _UsageConnection:
    """把 ``ModelUsageGuard`` 的 SQLite 方言调用翻译成 PostgreSQL。

    只接受下面列出的语句形态，任何未登记的语句**直接抛错**（绝不静默执行错语义）：

    * ``BEGIN IMMEDIATE`` → ``LOCK TABLE agent.creator_model_calls IN EXCLUSIVE MODE``：
      源语义是「拿整库写锁后再查额度、再写调用记录」，PostgreSQL 侧用表级排他锁复现
      同一串行化（锁随本事务 commit/rollback 释放）。
    * ``PRAGMA table_info(creator_model_calls)`` → ``information_schema.columns``：
      返回 (序号, 列名)，第 1 个字段即 SQLite 的行号 1 = 列名，守卫的 ``row[1]`` 取法不变。
    * ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` → **空操作**：
      PostgreSQL 模式下 schema 由 Alembic 迁移文件负责，运行角色不该也不必有 CREATE
      权限（PostgreSQL 在建对象前先查 schema 的 CREATE 权限，照发会让最小权限角色
      直接报错）。表真缺失时紧随其后的语句会立刻报「relation does not exist」。
    * ``ALTER TABLE ... ADD COLUMN`` → **抛错**：它只在目标缺列时才会发出，说明 schema
      已漂移，必须回去跑 ``alembic upgrade head``，绝不静默跳过。
    * 其余语句：表名限定为 ``agent.creator_model_calls``、``?`` → ``%s``、
      ``UPDATE`` 里的标量 ``MAX(a,b)`` → ``GREATEST(a,b)``（PostgreSQL 没有二参 MAX）。

    事务语义与 SQLite 一致：调用方显式 ``commit()`` 才落库，``close()`` 时若还有未提交
    事务则回滚（SQLite 关闭连接同样回滚），随后把连接还给共享池。
    """

    def __init__(self, manager, connection, row_factory):
        self._manager = manager
        self._connection = connection
        self._row_factory = row_factory
        self._dirty = False
        self._closed = False

    def execute(self, statement, params=()):
        sql = self._translate(statement)
        if sql is None:
            return _UsageResult()
        self._dirty = True
        cursor = self._connection.cursor(row_factory=self._row_factory)
        cursor.execute(sql, tuple(params or ()))
        return _UsageResult(cursor)

    def commit(self):
        self._connection.commit()
        self._dirty = False

    def rollback(self):
        self._connection.rollback()
        self._dirty = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._dirty:
                self._connection.rollback()
                self._dirty = False
        finally:
            self._manager.__exit__(None, None, None)

    @staticmethod
    def _translate(statement):
        """SQLite 方言 → PostgreSQL；``None`` 表示「空操作」（schema 归 Alembic）。"""
        sql = str(statement)
        stripped = sql.strip().rstrip(";").strip()
        if _USAGE_BEGIN.match(stripped):
            return "LOCK TABLE agent.creator_model_calls IN EXCLUSIVE MODE"
        if _USAGE_TABLE_INFO.match(stripped):
            return (
                "SELECT ordinal_position, column_name FROM information_schema.columns "
                "WHERE table_schema = 'agent' AND table_name = 'creator_model_calls' "
                "ORDER BY ordinal_position"
            )
        upper = stripped.upper()
        if upper.startswith(("CREATE TABLE", "CREATE INDEX")):
            return None
        if upper.startswith("ALTER TABLE"):
            raise RuntimeError(
                "creator pg_store：目标库缺列/缺索引（schema 漂移），请先执行 "
                "alembic upgrade head，再启动服务: %s" % stripped[:120]
            )
        if not upper.startswith(("SELECT", "INSERT", "UPDATE", "DELETE")):
            raise RuntimeError(
                "creator pg_store 用量守卫适配器不支持该语句，请同步登记翻译规则: %s"
                % stripped[:120]
            )
        sql = _USAGE_TABLE_NAME.sub("agent.creator_model_calls", sql)
        if upper.startswith("UPDATE"):
            sql = sql.replace("=MAX(", "=GREATEST(")
        sql = sql.replace("?", "%s")
        if "?" in sql:
            raise RuntimeError(
                "creator pg_store 用量守卫适配器未能翻译占位符: %s" % stripped[:120]
            )
        return sql


def usage_connection():
    """``ModelUsageGuard`` 的 db_factory：返回 PostgreSQL 权威的连接包装。

    调用方拿到的对象与 SQLite 连接同形（``execute`` / ``commit`` / ``rollback`` /
    ``close``），失败一律抛异常，绝不吞错。
    """
    from psycopg.rows import tuple_row

    manager = _pool_instance().connection()
    return _UsageConnection(manager, manager.__enter__(), tuple_row)
