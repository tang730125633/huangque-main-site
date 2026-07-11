#!/usr/bin/env python3
"""Remove obsolete assigned slot-pool rows and used redemption codes."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def _count(conn, table, status):
    row = conn.execute(
        "SELECT COUNT(*) FROM %s WHERE status=?" % table, (status,)
    ).fetchone()
    return int(row[0] if row else 0)


def _require_table(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not row:
        raise RuntimeError("missing table: %s" % table)


def cleanup(db_path, apply=False):
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("audio database does not exist: %s" % path)

    with closing(sqlite3.connect(str(path), timeout=20)) as conn:
        _require_table(conn, "voice_slot_pool")
        _require_table(conn, "voice_slot_codes")
        before = {
            "assigned_pool": _count(conn, "voice_slot_pool", "assigned"),
            "used_codes": _count(conn, "voice_slot_codes", "used"),
        }
        if apply:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM voice_slot_pool WHERE status='assigned'")
            conn.execute("DELETE FROM voice_slot_codes WHERE status='used'")
            conn.commit()
        after = {
            "assigned_pool": _count(conn, "voice_slot_pool", "assigned"),
            "used_codes": _count(conn, "voice_slot_codes", "used"),
        }
    return {"database": str(path), "applied": bool(apply), "before": before, "after": after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Existing audio_assets.db path")
    parser.add_argument("--apply", action="store_true", help="Delete the matched legacy rows")
    args = parser.parse_args()
    print(json.dumps(cleanup(args.db, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
