# -*- coding: utf-8 -*-
"""测试用隔离 PostgreSQL。

目的：让需要 PostgreSQL 的用例**不再因为“没有数据库”而跳过**。
- 若已提供 ``HQ_DATABASE_URL``（staging / CI），直接用它；
- 否则在本机用 ``embedded-postgres``（自带 PG 二进制）拉起一个只属于测试的实例，
  数据目录默认 ``E:\\AI\\pgdata-pc``（**必须 ASCII 路径**：Windows 上 initdb 遇到非 ASCII
  路径会在 bootstrap 阶段报 `invalid byte sequence for encoding "UTF8"`）。

安装（一次性，装到 E:\\AI 下，不污染系统环境）：

    pip install --target "E:\\AI\\pg-tools\\embedded-pg" embedded-postgres "psycopg[binary]" psycopg-pool

用法：

    from tests.pg_harness import postgres_url
    url = postgres_url()          # 保证可用；不可用则抛 RuntimeError（不跳过）
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
DEFAULT_DATA = r"E:\AI\pgdata-pc"
DEFAULT_DB = "huangque_staging"
DEFAULT_TOOLS = r"E:\AI\pg-tools\embedded-pg"


class PostgresUnavailable(RuntimeError):
    """没有可用的 PostgreSQL（本机也拉不起来）。用例应当 **失败**，而不是跳过。"""


def _prepare_path() -> None:
    for p in (str(SERVER), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    tools = os.environ.get("HQ_PG_TOOLS_DIR") or DEFAULT_TOOLS
    if tools and os.path.isdir(tools) and tools not in sys.path:
        sys.path.append(tools)


def _start_local(data_dir: str):
    _prepare_path()
    try:
        import embedded_postgres as ep
    except ImportError as exc:  # pragma: no cover - 环境缺失
        raise PostgresUnavailable(
            "既没有 HQ_DATABASE_URL，也没有 embedded-postgres；"
            "请先安装：pip install --target \"%s\" embedded-postgres \"psycopg[binary]\" psycopg-pool"
            % DEFAULT_TOOLS
        ) from exc
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    server = ep.get_server(str(path), cleanup_mode=None)
    if os.name == "nt" and not (path / "PG_VERSION").exists():
        # Windows locale defaults may contain a non-UTF8 locale name. Documented
        # --locale=C must also be applied by the automated harness itself.
        from embedded_postgres._commands import initdb
        initdb(['--auth=trust', '--auth-local=trust', '--encoding=utf8',
                '--locale=C', '-U', server.postgres_user], pgdata=path)
    server.ensure_pgdata_inited()
    server.ensure_postgres_running()
    return server


def _with_db(base_uri: str, name: str) -> str:
    head, sep, tail = base_uri.partition("?")
    head = head.rsplit("/", 1)[0] + "/" + name
    return head + (sep + tail if sep else "")


def _alembic_upgrade(url: str) -> None:
    env = dict(os.environ)
    env["HQ_DATABASE_URL"] = url
    parts = [str(SERVER), str(ROOT)]
    extra = env.get("PYTHONPATH") or ""
    if extra:
        parts.extend(p for p in extra.split(os.pathsep) if p)
    # Explicit interpreter-compatible dependencies take precedence over the
    # optional embedded-tools directory (which may target another Python ABI).
    parts.append(os.environ.get("HQ_PG_TOOLS_DIR") or DEFAULT_TOOLS)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(ROOT), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PostgresUnavailable(
            "alembic upgrade head 失败：\n%s\n%s" % (proc.stdout[-1200:], proc.stderr[-1500:]))


def postgres_url(*, fresh: bool = False, migrate: bool = True) -> str:
    """返回一个可用于测试的 PostgreSQL URL（**不会跳过**）。

    ``fresh=True`` 会重建目标库（仅用于本机 embedded 实例）。
    """
    given = (os.environ.get("HQ_DATABASE_URL") or "").strip()
    if given:
        if migrate:
            _alembic_upgrade(given)
        return given

    data_dir = os.environ.get("PG_LOCAL_DATA") or DEFAULT_DATA
    dbname = os.environ.get("PG_LOCAL_DB") or DEFAULT_DB
    server = _start_local(data_dir)
    base = server.get_uri()
    import psycopg
    with psycopg.connect(base, autocommit=True) as conn:
        if fresh:
            conn.execute('DROP DATABASE IF EXISTS "%s"' % dbname)
        try:
            conn.execute('CREATE DATABASE "%s"' % dbname)
        except Exception:  # noqa: BLE001 - 已存在时忽略
            pass
    url = _with_db(base, dbname)
    if migrate:
        _alembic_upgrade(url)
    return url


if __name__ == "__main__":
    print(postgres_url(fresh="--fresh" in sys.argv))
