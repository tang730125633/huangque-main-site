"""PostgreSQL backend for the auth identity + ledger domains (M6 cutover).

auth_server.db() 在 HQ_IDENTITY_STORE / HQ_LEDGER_STORE 任一为 postgres 时改发
本模块的连接；两开关都是默认 sqlite 时本模块完全不被 import，SQLite 路径
逐字节不变（本模块也因此绝不 import sqlite3 —— sqlite_inventory 棘轮保证它
是纯 PG 后端）。

连接包装器模拟 sqlite3.Connection 接口（execute/fetchone/fetchall/commit/
rollback/close、行对象兼容），并把 SQLite 方言翻译成 PG：

* ``?`` → ``%s``（引号感知，字符串字面量里的问号不动）
* ``BEGIN IMMEDIATE`` → 进程级写锁（RLock，可重入）：SQLite 单写者语义在本
  进程内等价保留；跨进程一致性由 PG 行锁 / 唯一约束兜底
* DML 隐式开事务并持写锁，commit/rollback/close 释放
* ``INSERT OR IGNORE`` → ``ON CONFLICT DO NOTHING``
* ``LIMIT -1`` → ``LIMIT ALL``
* 布尔列（PG boolean）：SQL 内 0/1 字面量 → TRUE/FALSE；INSERT 列清单 /
  UPDATE SET 列映射的参数 0/1 → bool()。``virtual_pay_orders.env`` 是支付
  环境枚举（0=正式/1=沙箱），绝不在归一化名单里
* ``INSERT INTO <白名单表> ... VALUES ...`` 自动追加 ``RETURNING id``，
  cursor.lastrowid 返回它 —— 白名单 = 代码里读 lastrowid 的 8 张表
* PRAGMA / CREATE / ALTER / DROP 等 DDL 直接抛错（schema 由 alembic 管）

双开关红线：users + points_audit 等跨域事务要求两个域同库同权威。任一开关
仍是 sqlite 时，连接照样是 PG，但每条语句按表域校验 —— 命中「sqlite 开关」
那个域的任何表立即抛错，绝不静默双权威。
"""

from __future__ import annotations

import logging
import os
import re
import threading

_log = logging.getLogger("hq.auth_store")

# ---------------------------------------------------------------------------
# 域清单（与 alembic 20260916_0011 / 0012 的建表一一对应）
# ---------------------------------------------------------------------------

_IDENTITY_TABLES = frozenset({
    "announcement_campaigns", "business_cards", "canvas_boards",
    "canvas_members", "canvas_ops", "canvas_presence",
    "card_referral_journeys", "cli_action_requests", "cli_device_grants",
    "cli_refresh_tokens", "friend_requests", "friendships",
    "invite_admin_audit", "invite_campaigns", "invite_codes",
    "invite_reward_claims", "invite_reward_notifications",
    "membership_audit", "membership_voice_slot_entitlements",
    "network_node_ids", "tokens", "user_invites", "user_notifications",
    "users", "wechat_subscription_grants", "wechat_subscription_outbox",
})

_LEDGER_TABLES = frozenset({
    "points_audit", "point_transfers", "invite_reward_point_records",
    "recharge_orders", "membership_recharge_records",
    "membership_upgrade_records", "virtual_pay_orders",
})

_ALL_TABLES = _IDENTITY_TABLES | _LEDGER_TABLES

# 布尔列：PG 里是 boolean；SQLite 代码传 0/1 会 DatatypeMismatch，必须归一化。
_BOOL_COLUMNS = {
    "users": frozenset({"must_change", "card_initial_password"}),
    "invite_campaigns": frozenset({"code_required"}),
    "business_cards": frozenset({
        "phone_public", "email_public", "address_public",
        "wechat_qr_public", "discoverable_in_network",
    }),
    "announcement_campaigns": frozenset({"wechat_push_requested"}),
}

# INSERT..RETURNING id 追加白名单（代码读 cur.lastrowid 的表，全有 id 列）。
_LASTROWID_TABLES = frozenset({
    "users", "membership_audit", "user_notifications", "announcement_campaigns",
    "cli_device_grants", "invite_reward_claims", "membership_upgrade_records",
    "invite_reward_point_records",
})

_MODES = ("sqlite", "postgres")
_ANNOUNCED = set()
_ANNOUNCED_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# 模式开关（每个进程一次性宣布，与 flags_store 同一纪律）
# ---------------------------------------------------------------------------

def identity_mode() -> str:
    return _mode("HQ_IDENTITY_STORE")

def ledger_mode() -> str:
    return _mode("HQ_LEDGER_STORE")

def _mode(env_name: str) -> str:
    raw = os.environ.get(env_name)
    explicit = raw is not None and raw.strip() != ""
    value = (raw or "sqlite").strip().lower()
    if value not in _MODES:
        raise RuntimeError("%s must be sqlite or postgres" % env_name)
    with _ANNOUNCED_LOCK:
        if env_name not in _ANNOUNCED:
            _ANNOUNCED.add(env_name)
            if explicit:
                _log.info("%s authority announced: mode=%s", env_name, value)
            else:
                _log.warning(
                    "%s not set, defaulting to %r (legacy storage)", env_name, value)
    return value

def any_postgres() -> bool:
    return identity_mode() == "postgres" or ledger_mode() == "postgres"

# ---------------------------------------------------------------------------
# 连接池（惰性：本模块被 import 时不连库、不 import psycopg）
# ---------------------------------------------------------------------------

_pool = None
_pool_lock = threading.Lock()


class IndexableRow(dict):
    """dict_row 的行对象 + 数字下标（等价 sqlite3.Row 的 row[n]）。"""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)


def _row_factory(cursor):
    from psycopg.rows import dict_row
    maker = dict_row(cursor)

    def make(values):
        return IndexableRow(maker(values))

    return make


def _pool_instance():
    global _pool
    if _pool is not None:
        return _pool
    url = (os.environ.get("HQ_DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError("HQ_DATABASE_URL is not configured")
    with _pool_lock:
        if _pool is None:
            from psycopg_pool import ConnectionPool

            maximum = int(os.environ.get("HQ_AUTH_DB_POOL_MAX") or "10")
            if maximum < 1 or maximum > 20:
                raise RuntimeError("HQ_AUTH_DB_POOL_MAX must be between 1 and 20")
            candidate = ConnectionPool(
                conninfo=url,
                min_size=1,
                max_size=maximum,
                timeout=10,
                kwargs={
                    "autocommit": False,
                    "row_factory": _row_factory,
                    "options": "-c search_path=identity,ledger",
                },
                open=False,
            )
            candidate.open(wait=True, timeout=10)
            _pool = candidate
    return _pool


_psycopg_errors_cache = None


def _psycopg_errors():
    global _psycopg_errors_cache
    if _psycopg_errors_cache is None:
        import psycopg.errors
        _psycopg_errors_cache = psycopg.errors
    return _psycopg_errors_cache


class IntegrityError(Exception):
    """psycopg 完整性冲突（UNIQUE/NOT NULL/CHECK…）的等价异常。

    auth_server 等调用方把它与 sqlite3.IntegrityError 并列捕获，两模式行为一致。
    """


def connect():
    """借一条 PG 连接并包成 sqlite3.Connection 兼容对象。调用方负责 close()。"""
    return _PgConnection(_pool_instance().getconn())


def close_pool() -> None:
    """测试与进程退出清理用；生产进程长驻不需要调用。"""
    global _pool
    with _pool_lock:
        current, _pool = _pool, None
    if current is not None:
        current.close()


def _return_connection(conn) -> None:
    with _pool_lock:
        pool = _pool
    if pool is not None:
        try:
            pool.putconn(conn)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SQL 方言翻译
# ---------------------------------------------------------------------------

_QUOTED_RE = re.compile(r"'(?:[^']|'')*'", re.S)
_TABLE_RE = re.compile(
    r"\b(" + "|".join(sorted(_ALL_TABLES, key=len, reverse=True)) + r")\b",
    re.I,
)
_OR_IGNORE_RE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.I | re.S)
_LIMIT_NEG_RE = re.compile(r"\bLIMIT\s+-1\b", re.I)
_INSERT_RE = re.compile(
    r"^\s*INSERT\s+INTO\s+([A-Za-z_][\w]*)\s*\(([^)]*)\)", re.I | re.S)
_UPDATE_RE = re.compile(
    r"^\s*UPDATE\s+([A-Za-z_][\w]*)\s+SET\s+(.*)$", re.I | re.S)
_VALUES_RE = re.compile(r"^\s*VALUES\s*\(", re.I | re.S)
_CONFLICT_SET_RE = re.compile(
    r"\bON\s+CONFLICT\b.*?\bDO\s+UPDATE\s+SET\s+(.*)$", re.I | re.S)
_DML_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
_FORBIDDEN_RE = re.compile(
    r"^\s*(PRAGMA|CREATE|ALTER|DROP|ATTACH|DETACH|VACUUM|REINDEX|ANALYZE)\b",
    re.I)


def _skip_string(sql: str, i: int) -> int:
    n = len(sql)
    i += 1
    while i < n:
        if sql[i] == "'":
            if i + 1 < n and sql[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _split_top(sql: str, sep: str = ","):
    """引号感知的顶层分割（不理会括号内的分隔符）。"""
    parts = []
    start = 0
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            i = _skip_string(sql, i)
            continue
        if ch == sep:
            parts.append(sql[start:i])
            start = i + 1
        i += 1
    parts.append(sql[start:])
    return parts


def _top_qs(sql: str):
    """顶层（字符串字面量外）? 的位置列表。"""
    positions = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            i = _skip_string(sql, i)
            continue
        if ch == "?":
            positions.append(i)
        i += 1
    return positions


def _top_keyword_index(sql: str, keyword: str) -> int:
    """顶层（引号外、括号深度 0）关键字的起始下标；找不到返回 -1。"""
    depth = 0
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            i = _skip_string(sql, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and sql[i:i + len(keyword)].upper() == keyword.upper():
            before = sql[i - 1] if i > 0 else ""
            after = sql[i + len(keyword)] if i + len(keyword) < n else ""
            if not (before.isalnum() or before == "_") and \
               not (after.isalnum() or after == "_"):
                return i
        i += 1
    return -1


def _param_offsets_in(q_positions, start, end):
    return [idx for idx, pos in enumerate(q_positions) if start <= pos < end]


def _count_top_qs(sql: str) -> int:
    """顶层 ? 的个数（引号感知）。"""
    return len(_top_qs(sql))


def _bools(sql: str):
    """布尔列感知翻译。

    返回 (新 sql, 需要 bool 归一化的参数偏移列表)：
    * INSERT 的 VALUES 字面量 0/1 → TRUE/FALSE（按列清单映射），参数按列归一化；
    * INSERT 的 ON CONFLICT DO UPDATE SET 段同样处理；
    * UPDATE 的 SET 段同样处理。
    """
    offsets = []
    q_positions = _top_qs(sql)
    m = _INSERT_RE.match(sql)
    if m:
        table = m.group(1).lower()
        bcols = _BOOL_COLUMNS.get(table)
        if bcols:
            cols = [c.strip().lower() for c in _split_top(m.group(2)) if c.strip()]
            vm = _VALUES_RE.match(sql[m.end():])
            if vm and cols:
                vstart = m.end() + vm.end()
                vend = _matching_paren(sql, vstart)
                if vend > vstart:
                    tokens = _split_top(sql[vstart:vend])
                    seg_idxs = [i for i, pos in enumerate(q_positions)
                                if vstart <= pos < vend]
                    new_tokens = []
                    cursor = 0
                    for idx, tok in enumerate(tokens):
                        stripped = tok.strip()
                        nq = _count_top_qs(tok)
                        if idx < len(cols) and cols[idx] in bcols:
                            if stripped == "?":
                                offsets.extend(seg_idxs[cursor:cursor + nq])
                            elif stripped in ("0", "1"):
                                tok = tok.replace(
                                    stripped, "TRUE" if stripped == "1" else "FALSE", 1)
                        cursor += nq
                        new_tokens.append(tok)
                    sql = sql[:vstart] + ",".join(new_tokens) + sql[vend:]
            cm = _CONFLICT_SET_RE.search(sql[m.end():])
            if cm:
                set_sql = cm.group(1)
                set_start = m.end() + cm.start(1)
                sql = _translate_set(sql, set_sql, set_start, bcols, q_positions, offsets)
        return sql, offsets
    m = _UPDATE_RE.match(sql)
    if m:
        table = m.group(1).lower()
        bcols = _BOOL_COLUMNS.get(table)
        if bcols:
            full_set = m.group(2)
            set_start = m.start(2)
            where_idx = _top_keyword_index(full_set, "WHERE")
            set_sql = full_set if where_idx < 0 else full_set[:where_idx]
            sql = _translate_set(sql, set_sql, set_start, bcols, q_positions, offsets)
    return sql, offsets


def _translate_set(sql, set_sql, set_start, bcols, q_positions, offsets):
    """处理 UPDATE/SET 段：布尔列 val 的 0/1 字面量换 TRUE/FALSE，? 参数记录偏移。"""
    set_end = set_start + len(set_sql)
    seg_idxs = [i for i, pos in enumerate(q_positions) if set_start <= pos < set_end]
    pairs = _split_top(set_sql)
    new_pairs = []
    cursor = 0
    for pair in pairs:
        if "=" not in pair:
            cursor += _count_top_qs(pair)
            new_pairs.append(pair)
            continue
        col, val = pair.split("=", 1)
        col = col.strip().lower()
        nq = _count_top_qs(val)
        if col in bcols:
            val_stripped = val.strip()
            if val_stripped == "?":
                offsets.extend(seg_idxs[cursor:cursor + nq])
            elif val_stripped in ("0", "1"):
                # 只替换 0/1 字面量本身，保留周围空白（否则 WHERE 等关键字粘连）
                val = val.replace(val_stripped, "TRUE" if val_stripped == "1" else "FALSE", 1)
                pair = col + "=" + val
        cursor += nq
        new_pairs.append(pair)
    return sql[:set_start] + ",".join(new_pairs) + sql[set_start + len(set_sql):]


def _matching_paren(sql: str, open_pos: int) -> int:
    """返回与 sql[open_pos-1] 的 '(' 匹配的 ')' 下标；找不到返回 -1。"""
    depth = 0
    i = open_pos - 1
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            i = _skip_string(sql, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _replace_qs(sql: str) -> str:
    """顶层 ? → %s（引号感知）。"""
    out = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = _skip_string(sql, i)
            out.append(sql[i:j])
            i = j
            continue
        if ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _append_suffix(sql: str, suffix: str) -> str:
    stripped = sql.rstrip()
    if stripped.endswith(";"):
        return stripped[:-1] + suffix + ";"
    return stripped + suffix


def _returning_eligible(sql: str) -> bool:
    m = _INSERT_RE.match(sql)
    if not m:
        return False
    if m.group(1).lower() not in _LASTROWID_TABLES:
        return False
    if _VALUES_RE.match(sql[m.end():]) is None:
        return False  # INSERT..SELECT：不追加 RETURNING
    return "RETURNING" not in sql.upper()


def _translate(sql: str, params):
    """把 SQLite 方言语句翻译成 PG，并归一化布尔参数。

    返回 (sql, params, return_id)；return_id=True 表示已追加 RETURNING id，
    调用方应立刻 fetchone 把 id 记到 lastrowid。
    """
    params = list(params) if params is not None else []
    or_ignore = bool(_OR_IGNORE_RE.search(sql))
    if or_ignore:
        sql = _OR_IGNORE_RE.sub("INSERT INTO", sql, count=1)
        sql = _append_suffix(sql, " ON CONFLICT DO NOTHING")
    sql, bool_offsets = _bools(sql)
    sql = _replace_qs(sql)
    sql = _LIMIT_NEG_RE.sub("LIMIT ALL", sql)
    return_id = False
    if not or_ignore and _returning_eligible(sql):
        sql = _append_suffix(sql, " RETURNING id")
        return_id = True
    for offset in bool_offsets:
        value = params[offset]
        if isinstance(value, int) and not isinstance(value, bool):
            params[offset] = bool(value)
    return sql, params, return_id


def _check_forbidden(sql: str) -> None:
    if _FORBIDDEN_RE.match(sql):
        raise RuntimeError(
            "DDL/PRAGMA is not allowed on the postgres backend: %r" % sql[:80])


def _check_domain(sql: str) -> None:
    """双权威防护：任一开关仍是 sqlite 时，命中该域的表直接抛错。"""
    identity_ok = identity_mode() == "postgres"
    ledger_ok = ledger_mode() == "postgres"
    if identity_ok and ledger_ok:
        return
    masked = _QUOTED_RE.sub(lambda m: " " * len(m.group(0)), sql)
    for m in _TABLE_RE.finditer(masked):
        table = m.group(1).lower()
        if table in _IDENTITY_TABLES and not identity_ok:
            raise RuntimeError(
                "identity table %r accessed while HQ_IDENTITY_STORE=%r "
                "(cutover refused: dual authority)" % (table, identity_mode()))
        if table in _LEDGER_TABLES and not ledger_ok:
            raise RuntimeError(
                "ledger table %r accessed while HQ_LEDGER_STORE=%r "
                "(cutover refused: dual authority)" % (table, ledger_mode()))


def _statement_kind(sql: str):
    stripped = sql.lstrip()
    upper = stripped[:12].upper()
    if upper.startswith("BEGIN") and (
            len(upper) == 5 or not upper[5].isalnum() and upper[5] != "_"):
        return "begin"
    if upper.startswith("COMMIT") and (
            len(upper) == 6 or not upper[6].isalnum() and upper[6] != "_"):
        return "commit"
    if upper.startswith("ROLLBACK") and (
            len(upper) == 8 or not upper[8].isalnum() and upper[8] != "_"):
        return "rollback"
    if upper.startswith("END") and (
            len(upper) == 3 or not upper[3].isalnum() and upper[3] != "_"):
        return "commit"
    return "sql"


# ---------------------------------------------------------------------------
# 连接包装器
# ---------------------------------------------------------------------------

_WRITE_LOCK = threading.RLock()  # 模拟 SQLite 单写者：进程内写事务串行


class _DummyCursor:
    """BEGIN/COMMIT/ROLLBACK 被包装器本地消化时返回的占位游标。"""
    lastrowid = None
    rowcount = -1

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())

    def close(self):
        pass


class _PgCursor:
    def __init__(self, pcursor, conn_wrapper):
        self._pc = pcursor
        self._conn = conn_wrapper
        self._returned_id = None

    @property
    def lastrowid(self):
        return self._returned_id

    @property
    def rowcount(self):
        return self._pc.rowcount

    @property
    def description(self):
        return self._pc.description

    def keys(self):
        desc = self._pc.description
        return [col.name for col in desc] if desc else []

    def fetchone(self):
        return self._pc.fetchone()

    def fetchall(self):
        return self._pc.fetchall()

    def __iter__(self):
        return iter(self._pc)

    def close(self):
        try:
            self._pc.close()
        except Exception:
            pass


class _PgConnection:
    """sqlite3.Connection 兼容包装。"""

    dialect = "postgres"

    def __init__(self, conn):
        self._conn = conn
        self._owns_lock = False
        self._cursor = None

    # -- 事务控制 ---------------------------------------------------------

    def commit(self):
        self._finish_tx(commit=True)

    def rollback(self):
        self._finish_tx(commit=False)

    def close(self):
        self._discard_cursor()
        self._release_write_lock()
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            try:
                if self._in_transaction(conn):
                    conn.rollback()
            except Exception:
                pass
            _return_connection(conn)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False

    # -- 执行 -------------------------------------------------------------

    def execute(self, sql, params=()):
        self._discard_cursor()
        kind = _statement_kind(sql)
        if kind == "begin":
            self._acquire_write_lock()
            self._cursor = _DummyCursor()
            return self._cursor
        if kind == "commit":
            self.commit()
            self._cursor = _DummyCursor()
            return self._cursor
        if kind == "rollback":
            self.rollback()
            self._cursor = _DummyCursor()
            return self._cursor
        _check_forbidden(sql)
        _check_domain(sql)
        sql, params, return_id = _translate(sql, params)
        if _DML_RE.match(sql):
            self._acquire_write_lock()
        try:
            pcursor = self._conn.execute(sql, params)
        except _psycopg_errors().IntegrityError as exc:
            raise IntegrityError(str(exc).strip()) from exc
        cursor = _PgCursor(pcursor, self)
        if return_id:
            row = pcursor.fetchone()
            cursor._returned_id = row["id"] if row is not None else None
        self._cursor = cursor
        return cursor

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _in_transaction(conn) -> bool:
        try:
            info = conn.info
        except Exception:
            return False
        status = getattr(info, "transaction_status", None) if info else None
        if status is None:
            return False
        from psycopg.pq import TransactionStatus
        return status in (TransactionStatus.ACTIVE, TransactionStatus.INTRANS,
                          TransactionStatus.INERROR)

    def _finish_tx(self, commit: bool) -> None:
        self._discard_cursor()
        self._release_write_lock()
        conn = self._conn
        if conn is None:
            return
        if not self._in_transaction(conn):
            return  # 无活动事务：no-op（SQLite 里多余 commit/rollback 无害）
        if commit:
            conn.commit()
        else:
            conn.rollback()

    def _acquire_write_lock(self) -> None:
        if not self._owns_lock:
            _WRITE_LOCK.acquire()
            self._owns_lock = True

    def _release_write_lock(self) -> None:
        if self._owns_lock:
            self._owns_lock = False
            _WRITE_LOCK.release()

    def _discard_cursor(self) -> None:
        if self._cursor is not None:
            try:
                self._cursor.close()
            except Exception:
                pass
            self._cursor = None
