#!/usr/bin/env python3
"""Dry-run or import the SQLite lead CRM table into the crm schema.

Dry-run is the default and never writes. --apply upserts every source row into
``crm.leads`` inside one transaction, then reads every row back and compares it
with the source; any mismatch rolls the whole import back. Re-running with
identical source data is a no-op.

源库：``content-api/leads_crm.db``（单表 ``lead_crm``，主键 (username, lead_id)）。
目标：``crm.leads``。时间口径与原库一致：``updated_at`` 是秒级 Unix 时间戳（BIGINT）。
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

SOURCE_TABLE = "lead_crm"
TARGET_TABLE = "crm.leads"
DOMAIN = "crm"
COLUMNS = ("intent", "follow_status", "follow_note", "updated_at")


def row_key(username, lead_id) -> str:
    """审计与校验和用的稳定字符串键（JSON 只接受字符串键，故不用元组）。"""
    return "%s/%s" % (username, lead_id)


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
        leads = {}
        for row in conn.execute(
            "SELECT username, lead_id, intent, follow_status, follow_note, updated_at "
            "FROM %s" % SOURCE_TABLE
        ):
            item = dict(row)
            leads[row_key(item["username"], item["lead_id"])] = item
    finally:
        conn.close()
    return {"leads": leads}


def summary(data: dict) -> dict:
    return {
        "leads": len(data["leads"]),
        "source_checksum": canonical_checksum(data["leads"]),
    }


def apply(data: dict, source_path: Path, code_sha: str) -> dict:
    if not data["leads"]:
        raise RuntimeError("SQLite 源没有任何行，拒绝导入")
    if not code_sha or len(code_sha) < 7:
        raise RuntimeError("--apply 必须带 --code-sha（迁移代码 commit）")
    report = summary(data)
    run_id = "crm-leads-" + uuid.uuid4().hex
    with postgres.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ops.data_migration_runs
              (run_id, domain, source_kind, source_locator, source_fingerprint,
               code_sha, state, source_count, details)
            VALUES (%s, %s, 'sqlite', %s, %s, %s, 'running', %s, %s)
            """,
            (run_id, DOMAIN, str(source_path), report["source_checksum"], code_sha,
             report["leads"], json.dumps(report)),
        )
        _import_table(conn, run_id, data["leads"])
        conn.execute(
            """
            UPDATE ops.data_migration_runs
               SET state = 'verified', target_count = %s,
                   finished_at = CURRENT_TIMESTAMP,
                   details = details || %s::jsonb
             WHERE run_id = %s
            """,
            (report["leads"], json.dumps({"verified": True}), run_id),
        )
    return {**report, "run_id": run_id}


def _import_table(conn, run_id, rows):
    for key, source_row in sorted(rows.items()):
        username = source_row["username"]
        lead_id = source_row["lead_id"]
        existing = conn.execute(
            "SELECT * FROM %s WHERE username = %%s AND lead_id = %%s" % TARGET_TABLE,
            (username, lead_id),
        ).fetchone()
        if existing is not None and int(existing["updated_at"]) > int(source_row["updated_at"]):
            # 目标行比源新：说明 PostgreSQL 侧已有切换后的新写入，回填器绝不覆盖
            raise RuntimeError(
                "冲突：%s %s 目标 updated_at(%s) 比源(%s) 新，停止整批导入"
                % (TARGET_TABLE, key, existing["updated_at"], source_row["updated_at"])
            )
        values = [source_row[col] for col in COLUMNS]
        updates = ",".join("%s = EXCLUDED.%s" % (c, c) for c in COLUMNS)
        conn.execute(
            "INSERT INTO %s(username, lead_id, %s) VALUES (%%s, %%s, %s) "
            "ON CONFLICT (username, lead_id) DO UPDATE SET %s"
            % (TARGET_TABLE, ",".join(COLUMNS),
               ",".join("%s" for _ in COLUMNS), updates),
            (username, lead_id) + tuple(values),
        )
        target = conn.execute(
            "SELECT * FROM %s WHERE username = %%s AND lead_id = %%s" % TARGET_TABLE,
            (username, lead_id),
        ).fetchone()
        if target is None:
            raise RuntimeError("导入后读回失败: %s %s" % (TARGET_TABLE, key))
        for col in COLUMNS:
            # 全部为文本/整数列，按字符串比较即可（无布尔列，无需真假归一）
            if str(source_row[col]) != str(target[col]):
                raise RuntimeError(
                    "行不一致: %s %s 字段 %s 源=%r 目标=%r"
                    % (TARGET_TABLE, key, col, source_row[col], target[col])
                )
        conn.execute(
            """
            INSERT INTO ops.data_migration_items
              (run_id, source_table, chunk_key, source_count, target_count,
               source_checksum, target_checksum, state)
            VALUES (%s, %s, %s, 1, 1, %s, %s, 'verified')
            """,
            (run_id, SOURCE_TABLE, key,
             canonical_checksum({key: source_row}), canonical_checksum({key: dict(target)})),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path,
                        help="SQLite 源路径（默认 LEADS_CRM_DB 环境变量或仓库根 leads_crm.db；"
                             "生产为 /home/ubuntu/content-api/leads_crm.db 的只读快照）")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--code-sha", default="")
    args = parser.parse_args()
    import os
    source = args.source or Path(
        os.environ.get("LEADS_CRM_DB")
        or str(ROOT / "leads_crm.db")
    )
    data = read_source(source)
    report = apply(data, source, args.code_sha) if args.apply else summary(data)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **report},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
