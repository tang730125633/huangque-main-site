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


def process(database, now=None, limit=100):
    conn = sqlite3.connect(database, timeout=30)
    conn.row_factory = sqlite3.Row
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
