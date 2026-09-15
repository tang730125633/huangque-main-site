#!/usr/bin/env python3
"""Dry-run or import admin_config.db into the ops schema (M3D).

Dry-run is the default and never writes. ``--apply`` upserts every source row
into its ``ops.admin_*`` table inside one transaction, then reads every row back
and compares it with the source; any mismatch rolls the whole import back.
Re-running with identical source data is a no-op.

设计要点（与 M3A 回填器同一契约）：

- 源库 ``admin_config.db`` 有 7 张表，其中 ``admin_channel_config`` /
  ``inspiration_cases`` 生产为空表，因此「全部为空」才拒绝导入。
- 表名映射只做前缀补齐：``provider_api_keys`` -> ``ops.admin_provider_api_keys``，
  ``inspiration_cases`` -> ``ops.admin_inspiration_cases``，其余同名。
- ``ciphertext`` / ``nonce`` 是密钥池的加密列：只参与校验和与逐字节比对，
  **任何输出与报错都只打印长度，绝不打印内容**。
- 自增主键（``admin_audit.id`` / ``admin_inspiration_cases.id``）显式带源编号回填，
  结束前把 PostgreSQL 序列推到源最大编号之后，否则新插入会撞主键。
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

# (源表名, 目标表名, 主键列, 是否比较 updated_at 冲突护栏)
TABLES = (
    ("provider_api_keys", "admin_provider_api_keys", "id", True),
    ("admin_channel_config", "admin_channel_config", "channel", True),
    ("admin_audit", "admin_audit", "id", False),
    ("admin_e2e_runs", "admin_e2e_runs", "run_id", True),
    ("admin_e2e_fixture_attempts", "admin_e2e_fixture_attempts", "fixture_key", True),
    ("admin_e2e_delivery_checks", "admin_e2e_delivery_checks", "job_id", True),
    ("inspiration_cases", "admin_inspiration_cases", "id", True),
)

# SQLite 0/1 整数列 -> PostgreSQL boolean 列，必须显式归一化（psycopg3 不接受 0/1）
BOOLEAN_COLUMNS = {"enabled", "featured"}
# 加密列：比对按字节，输出按长度（绝不回显）
BINARY_COLUMNS = {"ciphertext", "nonce"}
# 显式带源编号回填后需要对齐序列的表
IDENTITY_TABLES = ("admin_audit", "admin_inspiration_cases")


def canonical_checksum(rows: dict) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe(column: str, value):
    """报错信息里的取值：加密列只给长度，其它原样。"""
    if column in BINARY_COLUMNS and value is not None:
        return "<binary %d bytes>" % len(bytes(value))
    return value


def read_source(path: Path) -> dict:
    if not path.is_file():
        raise RuntimeError("SQLite 源不存在: %s" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    data = {}
    try:
        for source_table, _target, key_column, _guard in TABLES:
            columns = [
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(%s)" % source_table).fetchall()
            ]
            if not columns:
                raise RuntimeError("源表不存在或没有列: %s" % source_table)
            rows = {}
            for row in conn.execute("SELECT * FROM %s" % source_table):
                record = {column: row[column] for column in columns}
                rows[str(record[key_column])] = record
            data[source_table] = {"columns": columns, "rows": rows}
    finally:
        conn.close()
    return data


def summary(data: dict) -> dict:
    report = {table: len(data[table]["rows"]) for table, _t, _k, _g in TABLES}
    report["source_checksum"] = canonical_checksum(
        {table: data[table]["rows"] for table, _t, _k, _g in TABLES}
    )
    return report


def apply(data: dict, source_path: Path, code_sha: str) -> dict:
    total = sum(len(data[table]["rows"]) for table, _t, _k, _g in TABLES)
    if not total:
        raise RuntimeError("SQLite 源没有任何行，拒绝导入")
    if not code_sha or len(code_sha) < 7:
        raise RuntimeError("--apply 必须带 --code-sha（迁移代码 commit）")
    report = summary(data)
    run_id = "ops-admin-config-" + uuid.uuid4().hex
    with postgres.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ops.data_migration_runs
              (run_id, domain, source_kind, source_locator, source_fingerprint,
               code_sha, state, source_count, details)
            VALUES (%s, 'ops', 'sqlite', %s, %s, %s, 'running', %s, %s)
            """,
            (run_id, str(source_path), report["source_checksum"], code_sha,
             total, json.dumps(report)),
        )
        imported = 0
        for source_table, target_table, key_column, guard in TABLES:
            entry = data[source_table]
            imported += _import_table(
                conn, run_id, source_table, target_table, key_column, guard,
                entry["columns"], entry["rows"],
            )
        for target_table in IDENTITY_TABLES:
            conn.execute(
                """
                SELECT setval(
                    pg_get_serial_sequence('ops.%s', 'id'),
                    COALESCE((SELECT MAX(id) FROM ops.%s), 0) + 1,
                    false)
                """ % (target_table, target_table)
            )
        conn.execute(
            """
            UPDATE ops.data_migration_runs
               SET state = 'verified', target_count = %s,
                   finished_at = CURRENT_TIMESTAMP,
                   details = details || %s::jsonb
             WHERE run_id = %s
            """,
            (imported, json.dumps({"verified": True}), run_id),
        )
    return {**report, "run_id": run_id, "imported": imported}


def _normalize(column: str, value):
    if value is None:
        return None
    if column in BOOLEAN_COLUMNS:
        return bool(value)
    if column in BINARY_COLUMNS:
        return bytes(value)
    return value


def _comparable(column: str, value):
    """比对口径：布尔按真假、加密列按字节、其余按字符串（与 M3A 一致）。"""
    if column in BOOLEAN_COLUMNS:
        return bool(value)
    if column in BINARY_COLUMNS:
        return bytes(value) if value is not None else b""
    return str(value)


def _checksum_row(columns, row) -> dict:
    """校验和用的一行：加密列先归一成 bytes（memoryview 的 repr 不稳定）。"""
    return {
        column: (bytes(row[column]) if column in BINARY_COLUMNS
                 and row[column] is not None else row[column])
        for column in columns
    }


def _import_table(conn, run_id, source_table, target_table, key_column, guard,
                  columns, rows) -> int:
    imported = 0
    for key in sorted(rows):
        source_row = rows[key]
        key_value = _normalize(key_column, source_row[key_column])
        existing = conn.execute(
            "SELECT * FROM ops.%s WHERE %s = %%s" % (target_table, key_column),
            (key_value,),
        ).fetchone()
        if (
            guard
            and existing is not None
            and "updated_at" in columns
            and int(existing["updated_at"]) > int(source_row["updated_at"])
        ):
            # 目标行比源新：说明 PostgreSQL 侧已有切换后的新写入，回填器绝不覆盖
            raise RuntimeError(
                "冲突：ops.%s.%s 目标 updated_at(%s) 比源(%s) 新，停止整批导入"
                % (target_table, key, existing["updated_at"], source_row["updated_at"])
            )
        values = [_normalize(column, source_row[column]) for column in columns]
        updates = ",".join("%s = EXCLUDED.%s" % (c, c) for c in columns)
        conn.execute(
            "INSERT INTO ops.%s(%s) VALUES (%s) "
            "ON CONFLICT (%s) DO UPDATE SET %s"
            % (target_table, ",".join(columns),
               ",".join("%s" for _ in columns), key_column, updates),
            tuple(values),
        )
        target = conn.execute(
            "SELECT * FROM ops.%s WHERE %s = %%s" % (target_table, key_column),
            (key_value,),
        ).fetchone()
        if target is None:
            raise RuntimeError("导入后读回失败: %s %s" % (target_table, key))
        for column in columns:
            source_val = _comparable(column, source_row[column])
            target_val = _comparable(column, target[column])
            if source_val != target_val:
                raise RuntimeError(
                    "行不一致: %s %s 字段 %s 源=%r 目标=%r"
                    % (target_table, key, column,
                       _safe(column, source_row[column]), _safe(column, target[column]))
                )
        conn.execute(
            """
            INSERT INTO ops.data_migration_items
              (run_id, source_table, chunk_key, source_count, target_count,
               source_checksum, target_checksum, state)
            VALUES (%s, %s, %s, 1, 1, %s, %s, 'verified')
            """,
            (run_id, source_table, key,
             canonical_checksum({key: _checksum_row(columns, source_row)}),
             canonical_checksum({key: _checksum_row(columns, target)})),
        )
        imported += 1
    return imported


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path,
                        help="SQLite 源路径（默认 ADMIN_DB 环境变量或仓库根 admin_config.db）")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--code-sha", default="")
    args = parser.parse_args()
    import os
    source = args.source or Path(
        os.environ.get("ADMIN_DB")
        or str(ROOT / "admin_config.db")
    )
    data = read_source(source)
    report = apply(data, source, args.code_sha) if args.apply else summary(data)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **report},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
