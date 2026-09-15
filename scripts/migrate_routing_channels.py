#!/usr/bin/env python3
"""Dry-run or import the channel_management.db tables into the routing schema.

Dry-run is the default and never writes. --apply upserts every source row of all
eleven tables into ``routing.*`` inside one transaction, then reads every row
back and compares it with the source; any mismatch rolls the whole import back.
Re-running with identical source data is a no-op (every row reported unchanged).

Conflict guard: when the target row is newer than the source row (its
``updated`` / ``created`` / ``occurred`` / ``light_due`` column is greater), the
insert-only SQLite was written after the backfill and the whole batch aborts.
Tables without a time column (``channels``, ``run_snapshots``, ``settings``)
abort when the target row content differs at all — before the cutover the
PostgreSQL side only ever holds what this script wrote.

Sensitive columns: ``routing.versions.secret`` is the AES-GCM ciphertext of the
channel API key. It is copied verbatim (the key never leaves provider_keys) and
never printed: reports only carry counts and checksums.
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

DOMAIN = "routing"

# 每张表：列顺序与 SQLite 源一致；keys=主键；numbers=数值列（REAL/INTEGER，按数值比对）；
# bools=0/1 布尔列；freshness=冲突护栏用的时间列（源库该表没有时间列时为 None）。
TABLES = (
    {"table": "channels", "keys": ("id",),
     "columns": ("id", "version", "enabled"),
     "numbers": ("version",), "bools": ("enabled",), "freshness": None},
    {"table": "versions", "keys": ("channel", "version"),
     "columns": ("channel", "version", "config", "secret", "actor", "created"),
     "numbers": ("version", "created"), "bools": (), "freshness": "created"},
    {"table": "mappings", "keys": ("selector",),
     "columns": ("selector", "config", "actor", "updated"),
     "numbers": ("updated",), "bools": (), "freshness": "updated"},
    {"table": "operation_mappings", "keys": ("operation_id",),
     "columns": ("operation_id", "revision", "state", "config", "actor", "updated"),
     "numbers": ("revision", "updated"), "bools": (), "freshness": "updated"},
    {"table": "operation_mapping_versions", "keys": ("operation_id", "revision"),
     "columns": ("operation_id", "revision", "state", "config", "actor", "created"),
     "numbers": ("revision", "created"), "bools": (), "freshness": "created"},
    {"table": "runs", "keys": ("id",),
     "columns": ("id", "channel", "version", "kind", "state", "started", "updated",
                 "duration", "detail", "job_id", "provider_id", "reservation"),
     "numbers": ("version", "started", "updated", "duration", "reservation"),
     "bools": (), "freshness": "updated"},
    {"table": "run_snapshots", "keys": ("run_id",),
     "columns": ("run_id", "operation_id", "mapping_revision", "invocation_source", "snapshot"),
     "numbers": ("mapping_revision",), "bools": (), "freshness": None},
    {"table": "events", "keys": ("id",),
     "columns": ("id", "action", "target", "actor", "created"),
     "numbers": ("created",), "bools": (), "freshness": "created"},
    {"table": "schedule", "keys": ("channel",),
     "columns": ("channel", "light_due", "full_due"),
     "numbers": ("light_due", "full_due"), "bools": (), "freshness": "light_due"},
    {"table": "settings", "keys": ("id",),
     "columns": ("id", "value"),
     "numbers": ("id",), "bools": (), "freshness": None},
    {"table": "channel_incidents", "keys": ("channel", "kind"),
     "columns": ("channel", "kind", "state", "action", "occurred"),
     "numbers": ("occurred",), "bools": (), "freshness": "occurred"},
)


def canonical_checksum(rows: dict) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _chunk_key(spec: dict, row: dict) -> str:
    return ":".join(str(row[column]) for column in spec["keys"])


def _normalize(column: str, spec: dict, value):
    """比对口径：布尔按真假、数值按数值、其余按文本；None 只与 None 相等。"""
    if column in spec["bools"]:
        return bool(value)
    if value is None:
        return None
    if column in spec["numbers"]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _missing_columns(conn, spec: dict) -> list[str]:
    present = {row[1] for row in conn.execute('PRAGMA table_info("%s")' % spec["table"])}
    return [column for column in spec["columns"] if column not in present]


def read_source(path: Path) -> dict:
    if not path.is_file():
        raise RuntimeError("SQLite 源不存在: %s" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        data = {}
        for spec in TABLES:
            missing = _missing_columns(conn, spec)
            if missing:
                raise RuntimeError(
                    "源库 %s 缺少列 %s；源库版本与本迁移不匹配"
                    % (spec["table"], ",".join(missing))
                )
            columns = ",".join(spec["columns"])
            rows = {}
            for raw in conn.execute('SELECT %s FROM "%s"' % (columns, spec["table"])):
                row = {column: raw[column] for column in spec["columns"]}
                rows[_chunk_key(spec, row)] = row
            data[spec["table"]] = rows
    finally:
        conn.close()
    return data


def summary(data: dict) -> dict:
    counts = {spec["table"]: len(data[spec["table"]]) for spec in TABLES}
    return {
        "tables": counts,
        "rows": sum(counts.values()),
        "source_checksum": canonical_checksum(data),
    }


def _target_row(conn, spec: dict, key):
    where = " AND ".join("%s=%%s" % column for column in spec["keys"])
    return conn.execute(
        "SELECT %s FROM routing.%s WHERE %s" % (",".join(spec["columns"]), spec["table"], where),
        key,
    ).fetchone()


def _rows_equal(spec: dict, source_row: dict, target_row) -> bool:
    for column in spec["columns"]:
        if _normalize(column, spec, source_row[column]) != _normalize(column, spec, target_row[column]):
            return False
    return True


def _import_table(conn, run_id: str, spec: dict, rows: dict, stats: dict) -> None:
    table, keys, columns = spec["table"], spec["keys"], spec["columns"]
    insert_columns = ",".join(columns)
    placeholders = ",".join("%s" for _ in columns)
    non_key = [column for column in columns if column not in keys]
    updates = ",".join("%s=EXCLUDED.%s" % (column, column) for column in non_key)
    conflict = "(%s)" % ",".join(keys)
    upsert = (
        "INSERT INTO routing.%s(%s) VALUES(%s) ON CONFLICT %s DO UPDATE SET %s"
        % (table, insert_columns, placeholders, conflict, updates)
    )
    for key_text in sorted(rows):
        source_row = rows[key_text]
        key = tuple(source_row[column] for column in keys)
        target = _target_row(conn, spec, key)
        if target is not None:
            freshness = spec["freshness"]
            if freshness is not None and target[freshness] is not None and source_row[freshness] is not None:
                if float(target[freshness]) > float(source_row[freshness]):
                    # 目标行比源新：切换前后有人写过 PostgreSQL，回填器绝不覆盖
                    raise RuntimeError(
                        "冲突：routing.%s.%s 目标 %s(%s) 比源(%s) 新，停止整批导入"
                        % (table, key_text, freshness, target[freshness], source_row[freshness])
                    )
            elif not _rows_equal(spec, source_row, target):
                # 该表没有时间列可判新旧：目标内容与源不一致即视为切换后已有写入
                raise RuntimeError(
                    "冲突：routing.%s.%s 目标内容与源不一致，停止整批导入" % (table, key_text)
                )
            if _rows_equal(spec, source_row, target):
                stats["unchanged"] += 1
            else:
                stats["updated"] += 1
        else:
            stats["inserted"] += 1
        values = []
        for column in columns:
            value = source_row[column]
            values.append(bool(value) if column in spec["bools"] else value)
        conn.execute(upsert, tuple(values))
        written = _target_row(conn, spec, key)
        if written is None:
            raise RuntimeError("导入后读回失败: %s %s" % (table, key_text))
        for column in columns:
            source_value = _normalize(column, spec, source_row[column])
            target_value = _normalize(column, spec, written[column])
            if source_value != target_value:
                raise RuntimeError(
                    "行不一致: %s %s 字段 %s 源=%r 目标=%r"
                    % (table, key_text, column, source_value, target_value)
                )
        conn.execute(
            """
            INSERT INTO ops.data_migration_items
              (run_id, source_table, chunk_key, source_count, target_count,
               source_checksum, target_checksum, state)
            VALUES (%s, %s, %s, 1, 1, %s, %s, 'verified')
            """,
            (run_id, table, key_text[:128],
             canonical_checksum({key_text: source_row}),
             canonical_checksum({key_text: dict(written)})),
        )


def apply(data: dict, source_path: Path, code_sha: str) -> dict:
    report = summary(data)
    if not report["rows"]:
        raise RuntimeError("SQLite 源没有任何行，拒绝导入")
    if not code_sha or len(code_sha) < 7:
        raise RuntimeError("--apply 必须带 --code-sha（迁移代码 commit）")
    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    run_id = "routing-channels-" + uuid.uuid4().hex
    with postgres.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ops.data_migration_runs
              (run_id, domain, source_kind, source_locator, source_fingerprint,
               code_sha, state, source_count, details)
            VALUES (%s, %s, 'sqlite', %s, %s, %s, 'running', %s, %s)
            """,
            (run_id, DOMAIN, str(source_path), report["source_checksum"], code_sha,
             report["rows"], json.dumps(report)),
        )
        for spec in TABLES:
            _import_table(conn, run_id, spec, data[spec["table"]], stats)
        conn.execute(
            """
            UPDATE ops.data_migration_runs
               SET state = 'verified', target_count = %s,
                   finished_at = CURRENT_TIMESTAMP,
                   details = details || %s::jsonb
             WHERE run_id = %s
            """,
            (report["rows"], json.dumps({"verified": True, **stats}), run_id),
        )
    return {**report, "run_id": run_id, **stats}


def main() -> int:
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path,
                        help="SQLite 源路径（默认 HQ_CHANNEL_DB 或仓库根 channel_management.db）")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--code-sha", default="")
    args = parser.parse_args()
    source = args.source or Path(
        os.environ.get("HQ_CHANNEL_DB") or str(ROOT / "channel_management.db")
    )
    data = read_source(source)
    report = apply(data, source, args.code_sha) if args.apply else summary(data)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **report},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
