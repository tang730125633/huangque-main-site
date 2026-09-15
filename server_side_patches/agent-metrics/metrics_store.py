#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-metrics 数据层分发：PostgreSQL 权威侧（默认仍是 SQLite，行为不变）。

部署位置：``/home/ubuntu/agent-metrics/metrics_store.py``（与 collect.py、collect_traj.py、
board.py、export_json.py 同目录）。本模块**只实现 PostgreSQL 侧**：SQLite 代码原样留在
各脚本里，未打开开关时一行都不走这里，行为与迁移前逐字节一致
（对应主站迁移契约：「新 store 模块必须 PG-only；SQLite 代码保留在原模块内」）。

开关（环境变量）：

==============================  ==========================================
``HQ_METRICS_STORE``            ``sqlite``（默认）| ``postgres``；非法值直接抛错
``HQ_DATABASE_URL``             ``postgresql://…``，仅 postgres 模式需要；空值即报错
``HQ_METRICS_DB_POOL_MIN/MAX``  懒加载连接池下限/上限，默认 1/4
``HQ_METRICS_DB_POOL_TIMEOUT``  取连接超时秒数，默认 5
==============================  ==========================================

PG 连接方式参照主站 ``server/db/postgres.py`` 的懒加载池思路：**import 本模块不连库**，
第一次真正执行语句才建池。密码只存在于服务器受保护 env 文件
（``/etc/huangque/postgresql/migrator.env`` 之类），绝不写进本文件或任何仓库文件。

脚本里只有三处改动，其余 SQL 与逻辑保持不变：

1. **连接点**：

   .. code-block:: python

      if metrics_store.enabled():
          con, cur = metrics_store.connect()          # PG 权威（表由 alembic 建好）
      else:
          con = sqlite3.connect(DB); cur = con.cursor()   # ← 原样保留

2. **表名**：源表名经 ``metrics_store.table('events')`` 映射
   （sqlite → ``events``；postgres → ``ops.metrics_events``）。
   脚本里的 ``CREATE TABLE IF NOT EXISTS`` 只在 sqlite 分支执行，
   postgres 模式的表由 ``20260915_0010_ops_metrics.py`` 迁移建好。
3. **方言**：写 SQL 时遵守下面的「SQL 约定」。

SQL 约定（脚本里允许写的写法，PG 侧全部合法）：

* 占位符统一用 ``?``（PG 模式自动换成 ``%s``）。SQL 文本里不得出现 ``?`` 字面量，
  也不要写裸 ``%``（需要 LIKE 通配时把 ``'oc_%'`` 当参数传）。
* ``INSERT OR IGNORE INTO <表> …``（PG 模式自动改写成
  ``INSERT INTO <表> … ON CONFLICT DO NOTHING``）。两表除主键外没有别的唯一约束，
  所以「忽略冲突」的语义与 ``OR IGNORE`` 一致。
* 布尔聚合写成 ``sum(case when <条件> then 1 else 0 end)``。SQLite 的
  ``sum(<列>='x')`` 在 PG 下是 ``sum(boolean)``，不存在该函数；CASE 写法两边等价。
* ``GROUP BY`` 里不要写输出别名、也不要留裸列（SQLite 允许，PG 报错）；
  需要按某表达式分组就把表达式原样写进 ``GROUP BY``。
* 结果行同时支持 ``r['列名']`` 与 ``r[0]`` 两种读法（沿用 sqlite3.Row 的使用习惯）。

已知语义差异（PG 模式，刻意保留并已写入 Runbook）：

* **语句级自动提交**：PG 模式下每条语句自成事务，``con.commit()`` 是空操作
  （脚本最终落库的数据与 SQLite 模式一致，只是更早可见）。
* **报错一律抛出**，绝不静默吞掉：连不上库、SQL 方言没改干净都会让脚本以非零码退出。
  cron 里这些脚本的输出被重定向到 /dev/null，所以切换前必须先手动跑一遍看输出。
"""

from __future__ import annotations

import os
import re
import threading

_MODES = {"sqlite", "postgres"}

# 源表名 → PostgreSQL 目标表名（PG 权威侧的映射集中在这里）
_TABLES = {
    "events": "ops.metrics_events",
    "runs": "ops.metrics_runs",
}

# 只做两条机械 SQL 改写，绝不改写查询结构
_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.I)

_pool = None
_pool_lock = threading.Lock()


def mode() -> str:
    value = (os.environ.get("HQ_METRICS_STORE") or "sqlite").strip().lower()
    if value not in _MODES:
        raise RuntimeError("HQ_METRICS_STORE must be sqlite or postgres")
    return value


def enabled() -> bool:
    """是否已切到 PostgreSQL 权威。"""
    return mode() == "postgres"


def table(name: str) -> str:
    """把源表名映射成当前权威里的表名。

    sqlite 模式原样返回 ``events`` / ``runs``（脚本 SQL 与迁移前一致）；
    postgres 模式返回 ``ops.metrics_events`` / ``ops.metrics_runs``。
    """
    if not enabled():
        return name
    try:
        return _TABLES[name]
    except KeyError:
        raise RuntimeError("unknown metrics table: %r" % (name,))


def translate(sql: str) -> str:
    """把脚本里的 SQLite 方言 SQL 改写成 PostgreSQL 能跑的等价语句。

    只做两件机械改写（见模块开头「SQL 约定」）：

    1. ``INSERT OR IGNORE INTO t …`` → ``INSERT INTO t … ON CONFLICT DO NOTHING``
    2. ``?`` 占位符 → ``%s``

    改写不彻底时 PostgreSQL 会直接报语法错（fail-loud），不会静默改变语义。
    """
    statement = sql.rstrip()
    trailing = ""
    if statement.endswith(";"):
        statement, trailing = statement[:-1].rstrip(), ";"
    if _INSERT_OR_IGNORE.search(statement):
        statement = _INSERT_OR_IGNORE.sub("INSERT INTO", statement)
        statement = statement + " ON CONFLICT DO NOTHING"
    if "?" in statement:
        statement = statement.replace("?", "%s")
    return statement + trailing


class Row(dict):
    """同时支持 ``r['列名']`` 与 ``r[0]`` 的结果行（沿用 sqlite3.Row 的两种读法）。"""

    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return dict.__getitem__(self, key)


def _row_factory(cursor):
    names = [column.name for column in (cursor.description or ())]

    def make(values):
        return Row(names, values)

    return make


def _positive_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError("%s must be an integer" % name)
    if value < 1:
        raise RuntimeError("%s must be positive" % name)
    return value


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

            minimum = _positive_int("HQ_METRICS_DB_POOL_MIN", 1)
            maximum = _positive_int("HQ_METRICS_DB_POOL_MAX", 4)
            timeout = _positive_int("HQ_METRICS_DB_POOL_TIMEOUT", 5)
            if minimum > maximum:
                raise RuntimeError("HQ_METRICS_DB_POOL_MIN cannot exceed HQ_METRICS_DB_POOL_MAX")
            candidate = ConnectionPool(
                conninfo=url,
                min_size=minimum,
                max_size=maximum,
                timeout=timeout,
                kwargs={"autocommit": False},
                open=False,
            )
            candidate.open(wait=True, timeout=timeout)
            _pool = candidate
    return _pool


class _PgCursor:
    """sqlite3.Cursor 的最小兼容外壳：语句立即执行并把结果取回本地。"""

    def __init__(self, connection):
        self._connection = connection
        self._rows = []
        self.rowcount = -1

    def execute(self, sql, params=None):
        connection = self._connection._raw
        if connection is None:
            raise RuntimeError("connection is closed")
        with connection.cursor(row_factory=_row_factory) as cursor:
            cursor.execute(translate(sql), params if params else None)
            self.rowcount = cursor.rowcount
            if cursor.description is not None:
                self._rows = cursor.fetchall()
            else:
                self._rows = []
        # 语句级自动提交：SQLite 模式由 con.commit() 收尾，这里每条自成事务
        connection.commit()
        return self

    def executemany(self, sql, seq_of_params):
        for params in seq_of_params:
            self.execute(sql, params)
        return self

    def fetchone(self):
        if not self._rows:
            return None
        return self._rows.pop(0)

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def __iter__(self):
        rows, self._rows = self._rows, []
        return iter(rows)

    def close(self):
        self._rows = []


class _PgConnection:
    """sqlite3.Connection 的最小兼容外壳（commit() 为空操作，见模块开头说明）。"""

    row_factory = None

    def __init__(self):
        self._pool = _pool_instance()
        self._raw = self._pool.getconn()

    def cursor(self, **_ignored):
        return _PgCursor(self)

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def commit(self):
        if self._raw is not None:
            self._raw.commit()

    def rollback(self):
        if self._raw is not None:
            self._raw.rollback()

    def close(self):
        connection, self._raw = self._raw, None
        if connection is not None:
            try:
                connection.rollback()
            finally:
                self._pool.putconn(connection)


def connect():
    """打开 PostgreSQL 权威连接，返回 ``(con, cur)`` 元组。

    调用前必须 ``enabled()`` 为真；表由 alembic 迁移建好。
    """
    if not enabled():
        raise RuntimeError("metrics_store.connect() 只在 HQ_METRICS_STORE=postgres 时可用")
    connection = _PgConnection()
    return connection, connection.cursor()


def close_pool() -> None:
    """测试与进程退出清理用；生产 cron 脚本是短命进程，可不调用。"""
    global _pool
    with _pool_lock:
        current, _pool = _pool, None
    if current is not None:
        current.close()


def _selftest() -> int:
    """本地语法/映射自检：不需要数据库，验证 SQL 改写与表名映射。"""
    cases = [
        (
            "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            "INSERT INTO events VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT DO NOTHING",
        ),
        (
            "INSERT OR IGNORE INTO runs VALUES(?,?)",
            "INSERT INTO runs VALUES(%s,%s) ON CONFLICT DO NOTHING",
        ),
        (
            "select count(*) from events where kind=?",
            "select count(*) from events where kind=%s",
        ),
        (
            "select sum(case when kind='received' then 1 else 0 end) from events",
            "select sum(case when kind='received' then 1 else 0 end) from events",
        ),
    ]
    for sql, expected in cases:
        got = translate(sql)
        assert got == expected, "translate(%r) = %r != %r" % (sql, got, expected)
    os.environ["HQ_METRICS_STORE"] = "postgres"
    assert table("events") == "ops.metrics_events"
    assert table("runs") == "ops.metrics_runs"
    assert enabled() is True
    os.environ["HQ_METRICS_STORE"] = "sqlite"
    assert table("events") == "events"
    assert table("runs") == "runs"
    assert enabled() is False
    os.environ["HQ_METRICS_STORE"] = "bogus"
    try:
        mode()
    except RuntimeError:
        pass
    else:
        raise AssertionError("非法 HQ_METRICS_STORE 必须抛错")
    os.environ.pop("HQ_METRICS_STORE", None)
    print("metrics_store selftest: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
