#!/usr/bin/env python3
"""Dry-run or import SQLite runtime_observability evidence into the ops schema.

Dry-run is the default and never writes. --apply upserts every source row into
``ops.traces`` (from ``task_trace``) / ``ops.alert_outbox`` inside one
transaction, then reads every row back and compares it with the source; any
mismatch rolls the whole import back. Re-running with identical source data is a
no-op.

源库只有这两张表：``task_trace``（复合主键 job_id+stage）与 ``alert_outbox``
（主键 event_id）。时间列为秒级 REAL（Unix 时间戳带小数），与 PG
``double precision`` 逐位一致。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.db import postgres  # noqa: E402

SOURCE_TABLES = ("task_trace", "alert_outbox")
# 源库 REAL 列：回读比对按数值比，其它列按字符串比（与 M3A 回填器同口径）。
FLOAT_COLUMNS = {"started", "updated", "duration", "next_try"}
ITEM_CHUNK_KEY_LIMIT = 128  # ops.data_migration_items.chunk_key 上限


def canonical_checksum(rows: dict) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def trace_key(row: dict) -> str:
    for column in ("job_id", "stage"):
        if row[column] in (None, ""):
            raise RuntimeError("task_trace 存在空 %s，主键不完整，拒绝导入" % column)
    return "%s|%s" % (row["job_id"], row["stage"])


def read_source(path: Path) -> dict:
    if not path.is_file():
        raise RuntimeError("SQLite 源不存在: %s" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in SOURCE_TABLES if name not in tables]
        if missing:
            raise RuntimeError("SQLite 源缺少表: %s" % "、".join(missing))
        traces = {trace_key(dict(row)): dict(row)
                  for row in conn.execute("SELECT * FROM task_trace")}
        alerts = {}
        for row in conn.execute("SELECT * FROM alert_outbox"):
            record = dict(row)
            if record["event_id"] in (None, ""):
                raise RuntimeError("alert_outbox 存在空 event_id，主键不完整，拒绝导入")
            alerts[str(record["event_id"])] = record
    finally:
        conn.close()
    return {"traces": traces, "alerts": alerts}


def summary(data: dict) -> dict:
    return {
        "traces": len(data["traces"]),
        "alerts": len(data["alerts"]),
        "source_checksum": canonical_checksum(data),
    }


def apply(data: dict, source_path: Path, code_sha: str) -> dict:
    if not data["traces"] and not data["alerts"]:
        raise RuntimeError("SQLite 源没有任何行，拒绝导入")
    if not code_sha or len(code_sha) < 7:
        raise RuntimeError("--apply 必须带 --code-sha（迁移代码 commit）")
    report = summary(data)
    run_id = "ops-observability-" + uuid.uuid4().hex
    with postgres.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ops.data_migration_runs
              (run_id, domain, source_kind, source_locator, source_fingerprint,
               code_sha, state, source_count, details)
            VALUES (%s, 'ops', 'sqlite', %s, %s, %s, 'running', %s, %s)
            """,
            (run_id, str(source_path), report["source_checksum"], code_sha,
             report["traces"] + report["alerts"], json.dumps(report)),
        )
        _import_table(
            conn, run_id, "traces", ("job_id", "stage"), data["traces"],
            columns=("state", "started", "updated", "duration", "metadata"),
        )
        _import_table(
            conn, run_id, "alert_outbox", ("event_id",), data["alerts"],
            columns=("payload", "state", "attempts", "next_try", "updated", "error"),
        )
        conn.execute(
            """
            UPDATE ops.data_migration_runs
               SET state = 'verified', target_count = %s,
                   finished_at = CURRENT_TIMESTAMP,
                   details = details || %s::jsonb
             WHERE run_id = %s
            """,
            (report["traces"] + report["alerts"],
             json.dumps({"verified": True}), run_id),
        )
    return {**report, "run_id": run_id}


def same_value(column: str, source_val, target_val) -> bool:
    if column in FLOAT_COLUMNS:
        if source_val is None or target_val is None:
            return source_val is None and target_val is None
        return float(source_val) == float(target_val)
    return str(source_val) == str(target_val)


def _import_table(conn, run_id, table, key_columns, rows, columns):
    where = " AND ".join("%s = %%s" % column for column in key_columns)
    for key in sorted(rows):
        source_row = rows[key]
        key_values = tuple(source_row[column] for column in key_columns)
        if sum(len(str(value)) for value in key_values) + len(key_values) > ITEM_CHUNK_KEY_LIMIT:
            raise RuntimeError("行键过长，超过 ops.data_migration_items.chunk_key 上限: %s" % key)
        if source_row["updated"] is None:
            raise RuntimeError("源行 %s %s 的 updated 为空，无法判定新旧，停止整批导入"
                               % (table, key))
        existing = conn.execute(
            "SELECT * FROM ops.%s WHERE %s" % (table, where), key_values,
        ).fetchone()
        if existing is not None and existing["updated"] is None:
            raise RuntimeError("目标行 %s %s 的 updated 为空，无法判定新旧，停止整批导入"
                               % (table, key))
        if existing is not None and float(existing["updated"]) > float(source_row["updated"]):
            # 目标行比源新：说明 PostgreSQL 侧已有切换后的新写入，回填器绝不覆盖
            raise RuntimeError(
                "冲突：ops.%s %s 目标 updated(%s) 比源(%s) 新，停止整批导入"
                % (table, key, existing["updated"], source_row["updated"])
            )
        values = tuple(source_row[column] for column in columns)
        updates = ",".join("%s = EXCLUDED.%s" % (column, column) for column in columns)
        conn.execute(
            "INSERT INTO ops.%s(%s, %s) VALUES (%s, %s) "
            "ON CONFLICT (%s) DO UPDATE SET %s"
            % (table, ",".join(key_columns), ",".join(columns),
               ",".join("%s" for _ in key_columns),
               ",".join("%s" for _ in columns), ",".join(key_columns), updates),
            key_values + values,
        )
        target = conn.execute(
            "SELECT * FROM ops.%s WHERE %s" % (table, where), key_values,
        ).fetchone()
        if target is None:
            raise RuntimeError("导入后读回失败: %s %s" % (table, key))
        for column in columns:
            if not same_value(column, source_row[column], target[column]):
                raise RuntimeError(
                    "行不一致: %s %s 字段 %s 源=%r 目标=%r"
                    % (table, key, column, source_row[column], target[column])
                )
        conn.execute(
            """
            INSERT INTO ops.data_migration_items
              (run_id, source_table, chunk_key, source_count, target_count,
               source_checksum, target_checksum, state)
            VALUES (%s, %s, %s, 1, 1, %s, %s, 'verified')
            """,
            (run_id, table, str(key),
             canonical_checksum({key: source_row}), canonical_checksum({key: dict(target)})),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path,
                        help="SQLite 源路径（默认 HQ_OBSERVABILITY_DB 环境变量或 "
                             "仓库内 server/runtime_observability.db）")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--code-sha", default="")
    args = parser.parse_args()
    source = args.source or Path(
        os.environ.get("HQ_OBSERVABILITY_DB")
        or str(ROOT / "server" / "runtime_observability.db")
    )
    data = read_source(source)
    report = apply(data, source, args.code_sha) if args.apply else summary(data)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **report},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
