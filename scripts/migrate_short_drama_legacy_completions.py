#!/usr/bin/env python3
"""Audit and migrate pre-D-6 completed short-drama projects."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import short_drama_completion  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        description=(
            "???????????? archived ????????"
            "?? completed ????? legacy ??? D-6 ??"
        )
    )
    parser.add_argument("--db", required=True, help="?? SQLite ?????")
    parser.add_argument("--limit", type=int, default=64)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="?????????? dry-run ????",
    )
    args = parser.parse_args()
    database = str(Path(args.db).resolve())

    def db_factory():
        return sqlite3.connect(database, timeout=30)

    result = short_drama_completion.migrate_legacy_completions(
        db_factory,
        limit=max(1, min(1000, args.limit)),
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result["manual_review"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
