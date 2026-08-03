#!/usr/bin/env python3
"""Audit, migrate, verify or roll back legacy completed short dramas."""

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


def _write_manual_review(path, result):
    if not path:
        return
    payload = {
        "run_id": result.get("run_id"),
        "manual_review": result.get("manual_review") or [],
    }
    Path(path).resolve().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(
        description="审计、迁移、验证或回滚 D-6 之前完成的短剧项目",
    )
    parser.add_argument("--db", required=True, help="短剧 SQLite 数据库路径")
    parser.add_argument("--limit", type=int, default=64)
    parser.add_argument(
        "--run-id",
        help="发布批次 ID；apply、verify 和 rollback 使用同一个值",
    )
    parser.add_argument(
        "--manual-review-out",
        help="将不能自动迁移的项目及原因写入 UTF-8 JSON 文件",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true",
        help="迁移证据完整的项目；默认只执行 dry-run",
    )
    mode.add_argument(
        "--verify", action="store_true",
        help="只读验证所有完成项目及指定迁移批次",
    )
    mode.add_argument(
        "--rollback", action="store_true",
        help="原子回滚指定迁移批次，不改变原 completed stage/revision",
    )
    args = parser.parse_args()
    if (args.apply or args.rollback) and not str(args.run_id or "").strip():
        parser.error("--apply/--rollback 必须同时提供 --run-id")
    database = str(Path(args.db).resolve())

    def db_factory():
        return sqlite3.connect(database, timeout=30)

    if args.rollback:
        result = short_drama_completion.rollback_legacy_completions(
            db_factory, args.run_id,
        )
    elif args.verify:
        result = short_drama_completion.verify_legacy_completions(
            db_factory, args.run_id,
        )
    else:
        result = short_drama_completion.migrate_legacy_completions(
            db_factory,
            limit=max(1, min(1000, args.limit)),
            apply=args.apply,
            run_id=args.run_id,
        )
        _write_manual_review(args.manual_review_out, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.verify:
        raise SystemExit(0 if result["ok"] else 2)
    if result.get("manual_review"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
