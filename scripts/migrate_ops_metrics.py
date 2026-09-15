#!/usr/bin/env python3
"""Dry-run or import the SQLite agent-metrics tables into the ops schema.

Source: ``/home/ubuntu/agent-metrics/metrics.db`` (``events`` / ``runs``, written
by the server-side cron scripts — not part of this repository). Target:
``ops.metrics_events`` / ``ops.metrics_runs``.

Dry-run is the default and never writes. ``--apply`` upserts every source row
into the target tables inside one transaction, then reads every row back and
compares it field by field with the source; any mismatch rolls the whole import
back. Re-running with identical source data is a no-op (nothing is written).

两表的例外口径（与 M3A 回填器的差异，务必看清）：

* 这两张表**没有** ``updated_at`` 列，行又只增不改（主键 = 内容指纹）。
  因此「目标比源新」的冲突护栏改成等价的**内容护栏**：同一主键若在目标里
  已存在但字段内容与源不同，说明切换后 PostgreSQL 侧已有新写入（或源文件
  被就地改动过），此时整批中止，绝不覆盖。
* 源里的 ``''`` 是有效值（表示该事件没有这个字段），只有 ``events.replies``
  在非 complete 行上是真正的 NULL；回填一律原样搬运，不做 ``''``→NULL 归一。
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

DEFAULT_SOURCE = "/home/ubuntu/agent-metrics/metrics.db"

# (目标表, 主键列, 源表, 除主键外的列)
TABLES = (
    ("metrics_events", "hash", "events", (
        "ts", "instance", "capability", "agent", "client", "grp", "session",
        "kind", "replies", "detail",
    )),
    ("metrics_runs", "run_key", "runs", (
        "ts", "instance", "capability", "agent", "chat_id", "session_key",
        "provider", "model", "tok_in", "tok_out", "tok_total", "cache_read",
        "status", "replied", "model_calls", "trigger_", "msg_provider",
    )),
)

# 源里是 INT 的列：读回比对时统一成 int，避免 str/int 假不一致
_INT_COLUMNS = frozenset((
    "replies", "tok_in", "tok_out", "tok_total", "cache_read", "replied",
    "model_calls",
))


def canonical_checksum(rows: dict) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_row(row: dict) -> dict:
    """把一行归一成可比较/可校验的形式：None 保持 None，整数列转 int，其余转 str。"""
    out = {}
    for name, value in row.items():
        if value is None:
            out[name] = None
        elif name in _INT_COLUMNS:
            out[name] = int(value)
        else:
            out[name] = str(value)
    return out


def _norm(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)          # 布尔按真假比（本域无布尔列，防御性保留）
    if isinstance(value, int):
        return int(value)
    return str(value)


def read_source(path: Path) -> dict:
    if not path.is_file():
        raise RuntimeError("SQLite 源不存在: %s" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        data = {}
        for target, key_column, source, _columns in TABLES:
            rows = {}
            for record in conn.execute("SELECT * FROM %s" % source):
                row = normalize_row(dict(record))
                rows[row[key_column]] = row
            data[target] = rows
    finally:
        conn.close()
    return data


def summary(data: dict) -> dict:
    return {
        "metrics_events": len(data["metrics_events"]),
        "metrics_runs": len(data["metrics_runs"]),
        "source_checksum": canonical_checksum(data),
    }


def same_row(source_row: dict, target_row: dict) -> bool:
    for name, source_value in source_row.items():
        if name not in target_row:
            return False
        if _norm(source_value) != _norm(target_row[name]):
            return False
    return True


def apply(data: dict, source_path: Path, code_sha: str) -> dict:
    total = sum(len(rows) for rows in data.values())
    if not total:
        raise RuntimeError("SQLite 源没有任何行，拒绝导入")
    if not code_sha or len(code_sha) < 7:
        raise RuntimeError("--apply 必须带 --code-sha（迁移代码 commit）")
    report = summary(data)
    run_id = "ops-metrics-" + uuid.uuid4().hex
    stats = {}
    with postgres.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ops.data_migration_runs
              (run_id, domain, source_kind, source_locator, source_fingerprint,
               code_sha, state, source_count, details)
            VALUES (%s, 'ops', 'sqlite', %s, %s, %s, 'running', %s, %s)
            """,
            (run_id, str(source_path), report["source_checksum"], code_sha, total,
             json.dumps(report)),
        )
        for target, key_column, _source, columns in TABLES:
            stats[target] = _import_table(
                conn, run_id, target, key_column, data[target], columns,
            )
        conn.execute(
            """
            UPDATE ops.data_migration_runs
               SET state = 'verified', target_count = %s,
                   finished_at = CURRENT_TIMESTAMP,
                   details = details || %s::jsonb
             WHERE run_id = %s
            """,
            (total, json.dumps({"verified": True, "tables": stats}), run_id),
        )
    return {**report, **stats, "run_id": run_id}


def _import_table(conn, run_id, table, key_column, rows, columns):
    written = unchanged = 0
    for key in sorted(rows):
        source_row = rows[key]
        existing = conn.execute(
            "SELECT * FROM ops.%s WHERE %s = %%s" % (table, key_column),
            (key,),
        ).fetchone()
        if existing is not None and not same_row(source_row, existing):
            # 同一内容指纹却内容不同：目标侧已有切换后的新写入，或源被就地改过。
            # 回填器绝不覆盖，整批中止。
            raise RuntimeError(
                "冲突：ops.%s 主键 %s 已存在但内容与源不同（切换期间的新写入？），停止整批导入"
                % (table, key)
            )
        if existing is None:
            values = tuple(source_row[column] for column in columns)
            conn.execute(
                "INSERT INTO ops.%s(%s, %s) VALUES (%%s, %s) "
                "ON CONFLICT (%s) DO NOTHING"
                % (table, key_column, ",".join(columns),
                   ",".join("%s" for _ in columns), key_column),
                (key,) + values,
            )
            written += 1
        else:
            unchanged += 1          # 幂等：内容一致就不写
        target = conn.execute(
            "SELECT * FROM ops.%s WHERE %s = %%s" % (table, key_column),
            (key,),
        ).fetchone()
        if target is None:
            raise RuntimeError("导入后读回失败: %s %s" % (table, key))
        for column, source_value in source_row.items():
            if _norm(source_value) != _norm(target[column]):
                raise RuntimeError(
                    "行不一致: %s %s 字段 %s 源=%r 目标=%r"
                    % (table, key, column, source_value, target[column])
                )
        conn.execute(
            """
            INSERT INTO ops.data_migration_items
              (run_id, source_table, chunk_key, source_count, target_count,
               source_checksum, target_checksum, state)
            VALUES (%s, %s, %s, 1, 1, %s, %s, 'verified')
            """,
            (run_id, table, key,
             canonical_checksum({key: source_row}),
             canonical_checksum({key: dict(target)})),
        )
    return {"written": written, "unchanged": unchanged}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path,
        help="SQLite 源路径（默认 HQ_METRICS_DB 环境变量或 %s）" % DEFAULT_SOURCE,
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--code-sha", default="")
    args = parser.parse_args()
    source = args.source or Path(os.environ.get("HQ_METRICS_DB") or DEFAULT_SOURCE)
    data = read_source(source)
    report = apply(data, source, args.code_sha) if args.apply else summary(data)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **report},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
