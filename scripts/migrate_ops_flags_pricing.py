#!/usr/bin/env python3
"""Dry-run or import SQLite feature_flags/pricing_rules into ops schema.

Dry-run is the default and never writes. --apply upserts every source row into
``ops.feature_flags`` / ``ops.pricing_rules`` inside one transaction, then
reads every row back and compares it with the source; any mismatch rolls the
whole import back. Re-running with identical source data is a no-op.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.db import postgres  # noqa: E402


def canonical_checksum(rows: dict) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_source(path: Path) -> dict:
    if not path.is_file():
        raise RuntimeError("SQLite 源不存在: %s" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        flags = {r["feature"]: dict(r) for r in conn.execute("SELECT * FROM feature_flags")}
        prices = {r["rule"]: dict(r) for r in conn.execute("SELECT * FROM pricing_rules")}
    finally:
        conn.close()
    return {"flags": flags, "prices": prices}


def summary(data: dict) -> dict:
    return {
        "flags": len(data["flags"]),
        "prices": len(data["prices"]),
        "source_checksum": canonical_checksum(data),
    }


def apply(data: dict, source_path: Path, code_sha: str) -> dict:
    if not data["flags"] and not data["prices"]:
        raise RuntimeError("SQLite 源没有任何行，拒绝导入")
    if not code_sha or len(code_sha) < 7:
        raise RuntimeError("--apply 必须带 --code-sha（迁移代码 commit）")
    report = summary(data)
    run_id = "ops-flags-" + uuid.uuid4().hex
    with postgres.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ops.data_migration_runs
              (run_id, domain, source_kind, source_locator, source_fingerprint,
               code_sha, state, source_count, details)
            VALUES (%s, 'ops', 'sqlite', %s, %s, %s, 'running', %s, %s)
            """,
            (run_id, str(source_path), report["source_checksum"], code_sha,
             report["flags"] + report["prices"], json.dumps(report)),
        )
        _import_table(
            conn, run_id, "feature_flags", "feature", data["flags"],
            columns=("enabled", "updated_by", "updated_at"),
        )
        _import_table(
            conn, run_id, "pricing_rules", "rule", data["prices"],
            columns=("points", "updated_by", "updated_at"),
        )
        conn.execute(
            """
            UPDATE ops.data_migration_runs
               SET state = 'verified', target_count = %s,
                   finished_at = CURRENT_TIMESTAMP,
                   details = details || %s::jsonb
             WHERE run_id = %s
            """,
            (report["flags"] + report["prices"],
             json.dumps({"verified": True}), run_id),
        )
    return {**report, "run_id": run_id}


def _import_table(conn, run_id, table, key_column, rows, columns):
    for key, source_row in sorted(rows.items()):
        existing = conn.execute(
            "SELECT * FROM ops.%s WHERE %s = %%s" % (table, key_column),
            (key,),
        ).fetchone()
        if existing is not None and int(existing["updated_at"]) > int(source_row["updated_at"]):
            # 目标行比源新：说明 PostgreSQL 侧已有切换后的新写入，回填器绝不覆盖
            raise RuntimeError(
                "冲突：ops.%s.%s 目标 updated_at(%s) 比源(%s) 新，停止整批导入"
                % (table, key, existing["updated_at"], source_row["updated_at"])
            )
        values = [bool(source_row[col]) if col == "enabled" else source_row[col] for col in columns]
        updates = ",".join("%s = EXCLUDED.%s" % (c, c) for c in columns)
        conn.execute(
            "INSERT INTO ops.%s(%s, %s) VALUES (%%s, %s) "
            "ON CONFLICT (%s) DO UPDATE SET %s"
            % (table, key_column, ",".join(columns),
               ",".join("%s" for _ in columns), key_column, updates),
            (key,) + tuple(values),
        )
        target = conn.execute(
            "SELECT * FROM ops.%s WHERE %s = %%s" % (table, key_column),
            (key,),
        ).fetchone()
        if target is None:
            raise RuntimeError("导入后读回失败: %s %s" % (table, key))
        for col in ("enabled", "points", "updated_by", "updated_at"):
            if col not in columns:
                continue
            source_val = source_row[col]
            target_val = target[col]
            # SQLite 布尔是 0/1，PostgreSQL 是 bool；统一按真假比较
            if col == "enabled":
                source_val, target_val = bool(source_val), bool(target_val)
            if str(source_val) != str(target_val):
                raise RuntimeError(
                    "行不一致: %s %s 字段 %s 源=%r 目标=%r"
                    % (table, key, col, source_val, target_val)
                )
        conn.execute(
            """
            INSERT INTO ops.data_migration_items
              (run_id, source_table, chunk_key, source_count, target_count,
               source_checksum, target_checksum, state)
            VALUES (%s, %s, %s, 1, 1, %s, %s, 'verified')
            """,
            (run_id, table, key,
             canonical_checksum({key: source_row}), canonical_checksum({key: dict(target)})),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path,
                        help="SQLite 源路径（默认 FEATURE_FLAGS_DB 环境变量或仓库根 feature_flags.db）")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--code-sha", default="")
    args = parser.parse_args()
    import os
    source = args.source or Path(
        os.environ.get("FEATURE_FLAGS_DB")
        or str(ROOT / "feature_flags.db")
    )
    data = read_source(source)
    report = apply(data, source, args.code_sha) if args.apply else summary(data)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **report},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
