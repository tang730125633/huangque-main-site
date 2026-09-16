#!/usr/bin/env python3
"""Settle expired invite reward claims in bounded, idempotent batches."""
import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from server import invites  # noqa: E402
except ModuleNotFoundError as exc:
    if exc.name != "server":
        raise
    import invites  # noqa: E402


def _auth_store():
    """惰性 import PG 后端（生产平铺目录 / git 包布局双路径）。"""
    try:
        from server.content_domains import auth_store
        return auth_store
    except ModuleNotFoundError as exc:
        if exc.name not in ("server", "server.content_domains"):
            raise
        from content_domains import auth_store
        return auth_store


def _open_db(database):
    """M6 切写分发：两开关任一为 postgres 时走 PG 后端，否则原样走 SQLite。"""
    identity = (os.environ.get("HQ_IDENTITY_STORE") or "sqlite").strip().lower()
    ledger = (os.environ.get("HQ_LEDGER_STORE") or "sqlite").strip().lower()
    for name, value in (("HQ_IDENTITY_STORE", identity), ("HQ_LEDGER_STORE", ledger)):
        if value not in ("sqlite", "postgres"):
            raise RuntimeError("%s must be sqlite or postgres" % name)
    if identity == "postgres" or ledger == "postgres":
        return _auth_store().connect()
    conn = sqlite3.connect(database, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def process(database, now=None, limit=100):
    conn = _open_db(database)
    try:
        conn.execute("BEGIN IMMEDIATE")
        invites.init_schema(conn, now=now)
        result = invites.expire_pending_claims(
            conn, now=int(now or time.time()), limit=int(limit),
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--now", type=int)
    args = parser.parse_args(argv)
    try:
        result = process(args.database, args.now, args.limit)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
