#!/usr/bin/env python3
"""Dry-run or import the SQLite Creator Agent database into the agent schema.

Dry-run is the default and never writes. --apply upserts every source row into the
six ``agent.creator_*`` tables inside one transaction, then reads every row back
and compares it with the source; any mismatch rolls the whole import back.
Re-running with identical source data is a no-op.

源库：``/var/lib/huangque-creator-agent/creator_agent.db``（creator-agent 服务的
``CREATOR_AGENT_DB``，六张表：``creator_account_state`` / ``creator_workspaces`` /
``creator_messages`` / ``creator_batches`` / ``creator_jobs`` /
``creator_model_calls``）。目标：同名表落在 ``agent`` schema。
时间口径与原库一致：所有 ``*_at`` / ``*_expires_at`` 都是秒级 Unix 时间戳（BIGINT）。

与 M3A/M3E 回填器一致的契约：

* dry-run 默认且绝不写；``--apply`` 必须带 ``--code-sha``；单事务；
  ``--apply`` 结束时写 ``ops.data_migration_runs`` / ``ops.data_migration_items``
  审计（``domain='agent'``）；
* 逐行读回与源比对（含类型归一），任何不一致整批回滚；
* 冲突护栏：带 ``updated_at`` 的四张表，目标行比源新即整批中止（说明 PostgreSQL
  侧已有切换后的写入）；``creator_messages`` / ``creator_model_calls`` 没有
  ``updated_at``（消息的 ``public_json``、账本的 ``state`` 会在切换后被改写），
  因此这两张表只插入缺失行、绝不覆盖既有行，读到不一致同样整批中止；
* 源校验和 = 规范化 JSON 的 SHA-256（``sort_keys=True``、``separators=(",",":")``、
  ``default=str``）。

审计粒度：``ops.data_migration_items`` 按**表**记 6 行（chunk_key = 表名，含该表
行数与整表校验和）；逐行一致性由导入时的读回比对保证，不再逐行写审计行——用量账本
可能有上万行，逐行审计会把审计表撑大。

``creator_batches.insert_seq`` 是 PostgreSQL 为 SQLite 隐式 ``rowid`` 准备的替身
（``ORDER BY created_at DESC, rowid DESC`` 的稳定排序需要它），回填时照抄源 rowid，
导入后把 ``creator_messages`` 的自增序列推到 ``max(id)``，避免切换后新消息撞号。
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

DOMAIN = "agent"
SCHEMA = "agent"

# (源表名, 主键列, 全部列, 是否带可比较新鲜度的 updated_at)
TABLES = (
    ("creator_account_state", ("username",),
     ("username", "active_project_id", "updated_at"), "updated_at"),
    ("creator_workspaces", ("username", "project_id"),
     ("username", "project_id", "alias", "platforms_json", "preferences_json",
      "profile_overrides_json", "profile_json", "profile_state_json",
      "deliverables_json", "flow_json", "created_at", "updated_at"), "updated_at"),
    ("creator_messages", ("id",),
     ("id", "username", "project_id", "role", "content", "source_key", "request_id",
      "request_hash", "public_json", "created_at"), None),
    ("creator_batches", ("id",),
     ("id", "username", "project_id", "insert_seq", "source_message_id",
      "last_mutation_message_id", "topic", "goal", "status", "plan_json",
      "quote_json", "confirmation_id", "revision", "plan_hash", "quoted_revision",
      "quote_expires_at", "claim_id", "created_at", "updated_at"), "updated_at"),
    ("creator_jobs", ("id",),
     ("id", "batch_id", "username", "project_id", "platform", "version", "status",
      "input_json", "input_hash", "quote_token", "quote_json", "quote_cost",
      "quote_expires_at", "idempotency_key", "revision", "submit_input_json",
      "submit_input_hash", "submit_quote_token", "submit_quote_cost",
      "submit_quote_expires_at", "submit_idempotency_key", "confirmation_id",
      "job_id", "result_json", "error", "refund_status", "created_at",
      "updated_at"), "updated_at"),
    ("creator_model_calls", ("id",),
     ("id", "username", "ip_hash", "kind", "day", "estimated_tokens",
      "estimated_cost_micro_usd", "price_version",
      "input_price_micro_usd_per_million", "output_price_micro_usd_per_million",
      "state", "created_at", "lease_until", "finished_at"), None),
)

# 导入顺序：外键依赖（工作区 → 消息/批次 → 子任务）在前，账本最后。
IMPORT_ORDER = tuple(spec[0] for spec in TABLES)


def canonical_checksum(rows) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def row_key(table: str, row: dict) -> str:
    if table == "creator_account_state":
        return str(row["username"])
    if table == "creator_workspaces":
        return "%s/%s" % (row["username"], row["project_id"])
    return str(row["id"])


def read_source(path: Path) -> dict:
    if not path.is_file():
        raise RuntimeError("SQLite 源不存在: %s" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    data = {}
    try:
        for table, _keys, columns, _fresh in TABLES:
            if table == "creator_batches":
                # rowid 是 SQLite 隐式的插入顺序，落库为 insert_seq
                query = "SELECT rowid AS insert_seq, * FROM %s ORDER BY rowid" % table
            else:
                query = "SELECT * FROM %s" % table
            rows = {}
            for raw in conn.execute(query):
                present = set(raw.keys())
                missing = [column for column in columns if column not in present]
                if missing:
                    raise RuntimeError(
                        "源表 %s 缺少列 %s：先用 creator-agent 打开一次源库，"
                        "让它在源库上跑完自迁移，再回填" % (table, missing)
                    )
                item = {column: raw[column] for column in columns}
                rows[row_key(table, item)] = item
            data[table] = rows
    finally:
        conn.close()
    return data


def orphans(data: dict):
    """返回 [(说明, 样例键)]：源库里指向不存在父行的行（PG 外键会直接拒收）。"""
    found = []
    workspace_keys = set(data["creator_workspaces"])
    for table in ("creator_messages", "creator_batches"):
        bad = [
            key for key, row in data[table].items()
            if "%s/%s" % (row["username"], row["project_id"]) not in workspace_keys
        ]
        if bad:
            found.append(("%s 指向不存在的工作区" % table, sorted(bad)[:5]))
    batch_keys = set(data["creator_batches"])
    bad_jobs = [
        key for key, row in data["creator_jobs"].items() if str(row["batch_id"]) not in batch_keys
    ]
    if bad_jobs:
        found.append(("creator_jobs 指向不存在的批次", sorted(bad_jobs)[:5]))
    return found


def summary(data: dict) -> dict:
    counts = {table: len(data[table]) for table in IMPORT_ORDER}
    return {
        "tables": counts,
        "rows": sum(counts.values()),
        "table_checksums": {
            table: canonical_checksum(data[table]) for table in IMPORT_ORDER
        },
    }


def apply(data: dict, source_path: Path, code_sha: str) -> dict:
    if not data["creator_workspaces"] and not data["creator_messages"]:
        raise RuntimeError("SQLite 源没有任何工作区或消息，拒绝导入")
    if not code_sha or len(code_sha) < 7:
        raise RuntimeError("--apply 必须带 --code-sha（迁移代码 commit）")
    missing = orphans(data)
    if missing:
        raise RuntimeError("源库存在孤儿行，先修源库再回填: %s" % json.dumps(missing))
    report = summary(data)
    run_id = "agent-creator-" + uuid.uuid4().hex
    with postgres.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ops.data_migration_runs
              (run_id, domain, source_kind, source_locator, source_fingerprint,
               code_sha, state, source_count, details)
            VALUES (%s, %s, 'sqlite', %s, %s, %s, 'running', %s, %s)
            """,
            (run_id, DOMAIN, str(source_path),
             canonical_checksum(report["table_checksums"]), code_sha,
             report["rows"], json.dumps(report)),
        )
        for table in IMPORT_ORDER:
            spec = next(item for item in TABLES if item[0] == table)
            _import_table(conn, run_id, spec, data[table])
        _reset_message_sequence(conn)
        conn.execute(
            """
            UPDATE ops.data_migration_runs
               SET state = 'verified', target_count = %s,
                   finished_at = CURRENT_TIMESTAMP,
                   details = details || %s::jsonb
             WHERE run_id = %s
            """,
            (report["rows"], json.dumps({"verified": True}), run_id),
        )
    return {**report, "run_id": run_id}


def _import_table(conn, run_id, spec, rows):
    table, keys, columns, fresh = spec
    target = "%s.%s" % (SCHEMA, table)
    key_columns = list(keys)
    payload = [column for column in columns if column not in keys]
    conflict = "(%s)" % ",".join(key_columns)
    if fresh:
        updates = ",".join("%s = EXCLUDED.%s" % (column, column) for column in payload)
        statement = (
            "INSERT INTO %s(%s) VALUES (%s) ON CONFLICT %s DO UPDATE SET %s"
            % (target, ",".join(columns), ",".join("%s" for _ in columns),
               conflict, updates)
        )
    else:
        # 无 updated_at 的表：只补缺失行，绝不覆盖目标已有行
        statement = (
            "INSERT INTO %s(%s) VALUES (%s) ON CONFLICT %s DO NOTHING"
            % (target, ",".join(columns), ",".join("%s" for _ in columns), conflict)
        )
    for key, source_row in sorted(rows.items()):
        where = " AND ".join("%s = %%s" % column for column in key_columns)
        key_values = tuple(source_row[column] for column in key_columns)
        existing = conn.execute(
            "SELECT * FROM %s WHERE %s" % (target, where), key_values
        ).fetchone()
        if existing is not None and fresh:
            if int(existing[fresh]) > int(source_row[fresh]):
                # 目标行比源新：说明 PostgreSQL 侧已有切换后的新写入，回填器绝不覆盖
                raise RuntimeError(
                    "冲突：%s %s 目标 %s(%s) 比源(%s) 新，停止整批导入"
                    % (target, key, fresh, existing[fresh], source_row[fresh])
                )
        conn.execute(statement, tuple(source_row[column] for column in columns))
        read_back = conn.execute(
            "SELECT * FROM %s WHERE %s" % (target, where), key_values
        ).fetchone()
        if read_back is None:
            raise RuntimeError("导入后读回失败: %s %s" % (target, key))
        for column in columns:
            # 全表都是 TEXT/INTEGER，无布尔列，按字符串比较即可
            if str(source_row[column]) != str(read_back[column]):
                raise RuntimeError(
                    "行不一致: %s %s 字段 %s 源=%r 目标=%r"
                    % (target, key, column, source_row[column], read_back[column])
                )
    conn.execute(
        """
        INSERT INTO ops.data_migration_items
          (run_id, source_table, chunk_key, source_count, target_count,
           source_checksum, target_checksum, state)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'verified')
        """,
        (run_id, table, table, len(rows), len(rows),
         canonical_checksum(rows),
         canonical_checksum({
             key: {column: row[column] for column in columns}
             for key, row in rows.items()
         })),
    )


def _reset_message_sequence(conn):
    """把 creator_messages 的自增序列推到 max(id)：显式 id 插入不会自己推进序列。

    表名只能拼进 SQL（PostgreSQL 不接受把标识符当参数绑定），口令化的只有序列名。
    """
    conn.execute(
        "SELECT setval(pg_get_serial_sequence(%%s, 'id'), "
        "GREATEST(COALESCE((SELECT MAX(id) FROM %s.creator_messages), 1), 1))" % SCHEMA,
        ("%s.creator_messages" % SCHEMA,),
    ).fetchone()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path,
                        help="SQLite 源路径（默认 CREATOR_AGENT_DB 环境变量或 "
                             "/var/lib/huangque-creator-agent/creator_agent.db）")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--code-sha", default="")
    args = parser.parse_args()
    import os
    source = args.source or Path(
        os.environ.get("CREATOR_AGENT_DB")
        or "/var/lib/huangque-creator-agent/creator_agent.db"
    )
    data = read_source(source)
    report = summary(data)
    report["orphans"] = orphans(data)
    if args.apply:
        report = apply(data, source, args.code_sha)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **report},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
