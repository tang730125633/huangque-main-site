#!/usr/bin/env python3
"""Dry-run, import — and separately *audit* — the auth-service accounting tables (M6J).

源库：``/home/ubuntu/auth-service/users.db``（auth-service 用 ``/usr/bin/python3`` 直接跑，
无 store 分发代码 —— 代码分发留到切写阶段）。目标：PostgreSQL ``ledger`` schema 的 7 张表
（``server/db/migrations/versions/20260916_0012_auth_ledger.py``）。

三种运行方式，互斥：

1. ``python3 scripts/migrate_auth_ledger.py``（默认 dry-run）—— 只读 SQLite，打印 7 张表的
   行数与源校验和（规范化 JSON 的 SHA-256）。**默认绝不写库。**
2. ``--verify-balances`` —— 只读 SQLite，逐笔重算每个用户的可消费点数余额并与
   ``users.points`` 逐个比对；差异只写进报告，**绝不改任何一方**。不需要 PostgreSQL。
3. ``--apply --code-sha <sha> [--ack-balance-report <sha256>]`` —— 单事务把 7 张表逐行搬进
   ``ledger.*``，写 ``ops.data_migration_runs/items`` 审计（domain='ledger'），逐行读回比对，
   任何不一致整批回滚；幂等（相同数据重跑 no-op）；冲突护栏（目标行比源新则整批中止）。

账务零容忍（本域红线）：

- 回填器**只照搬 + 核对**：点数、退款、金额、状态任何值都不重算、不四舍五入、不修补。
- 余额核对发现的任何差异都只是**报告**；处置一律需老板批准后人工执行（见 M6J Runbook）。
- ``--apply`` 前必须已有一次人看过的余额核对报告：把 ``--verify-balances`` 打印的
  ``balance_report_checksum`` 原样传给 ``--ack-balance-report``，否则拒绝写入。硬报警
  （链路不符、流水损坏、重复主键）无论如何都拒绝写入。
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

DOMAIN = "ledger"

# (源表, 目标表, 键列, 冲突护栏用的「最新活动时间」列, 是否自增主键)
# 7 张表都没有 updated_at，护栏改用本表真正会变动的最大时间列（见 _row_activity）。
TABLES = (
    ("points_audit", "ledger.points_audit", "id",
     ("created_at",), True),
    ("point_transfers", "ledger.point_transfers", "id",
     ("created_at",), True),
    ("invite_reward_point_records", "ledger.invite_reward_point_records", "id",
     ("created_at", "voided_at"), True),
    ("recharge_orders", "ledger.recharge_orders", "order_id",
     ("created_at", "reviewed_at"), False),
    ("membership_recharge_records", "ledger.membership_recharge_records", "id",
     ("created_at",), True),
    ("membership_upgrade_records", "ledger.membership_upgrade_records", "id",
     ("created_at", "voided_at"), True),
    ("virtual_pay_orders", "ledger.virtual_pay_orders", "order_id",
     ("created_at", "paid_at", "credited_at", "delivered_at"), False),
)

IDENTITY_TABLES = tuple(
    target.replace("ledger.", "")
    for _source, target, _key, _activity, identity in TABLES if identity
)

# SQLite 0/1 列 -> PostgreSQL boolean 列必须 bool() 归一化（psycopg3 拒绝 0/1）。
# 本域为空：virtual_pay_orders.env 是支付环境枚举（0=正式/1=沙箱，代码按 int(env) 取密钥），
# 在迁移里刻意保留 BIGINT。保留这个集合是为了与 M3 其余域的写法一致。
BOOLEAN_COLUMNS = frozenset()

# 新用户注册赠送点数（auth_server.NEW_USER_TRIAL_POINTS，env HQ_AUTH_TRIAL_POINTS）。
# 它在注册时直接写 users.points，**不进 points_audit** —— 余额核对要靠它解释 +16 的差额。
DEFAULT_TRIAL_POINTS = 16

BALANCE_TABLES = (
    "points_audit",
    "point_transfers",
    "invite_reward_point_records",
    "users",
)


# ----------------------------------------------------------------------
# 校验和与键
# ----------------------------------------------------------------------

def canonical_checksum(rows: dict) -> str:
    """源/目标校验和：规范化 JSON 的 SHA-256（与 M3A/M3E 回填器同口径）。"""
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def row_key(key_column: str, value) -> str:
    """审计用的稳定字符串键（JSON 只接受字符串键）。"""
    if key_column == "id":
        return str(int(value))
    return str(value)


# ----------------------------------------------------------------------
# 读源库（只读）
# ----------------------------------------------------------------------

def source_db_default() -> Path:
    import os
    return Path(
        os.environ.get("HQ_AUTH_DB")
        or os.environ.get("AUTH_DB")
        or str(ROOT / "users.db")
    )


def connect_source(path: Path) -> sqlite3.Connection:
    if not Path(path).is_file():
        raise RuntimeError("SQLite 源不存在: %s" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_source(path: Path) -> dict:
    conn = connect_source(path)
    data = {}
    try:
        for source_table, _target, key_column, _activity, _identity in TABLES:
            columns = [
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(%s)" % source_table).fetchall()
            ]
            if not columns:
                raise RuntimeError("源表不存在或没有列: %s" % source_table)
            if key_column not in columns:
                raise RuntimeError("源表 %s 缺少键列 %s" % (source_table, key_column))
            rows = {}
            for row in conn.execute("SELECT * FROM %s" % source_table):
                record = {column: row[column] for column in columns}
                rows[row_key(key_column, record[key_column])] = record
            data[source_table] = {"columns": columns, "rows": rows}
    finally:
        conn.close()
    return data


def summary(data: dict) -> dict:
    report = {
        source_table: len(data[source_table]["rows"])
        for source_table, _t, _k, _a, _i in TABLES
    }
    report["source_checksum"] = canonical_checksum(
        {source_table: data[source_table]["rows"]
         for source_table, _t, _k, _a, _i in TABLES}
    )
    return report


# ----------------------------------------------------------------------
# 类型与比对口径
# ----------------------------------------------------------------------

def _normalize(column: str, value):
    if value is None:
        return None
    if column in BOOLEAN_COLUMNS:
        return bool(value)
    return value


def _comparable(column: str, value):
    """比对口径：布尔按真假，其余按字符串（整数/浮点/文本都能安全比）。"""
    if column in BOOLEAN_COLUMNS:
        return bool(value)
    return str(value)


def _row_activity(row: dict, activity_columns) -> int:
    """冲突护栏用的「最新活动时间」：本表会用到的最大秒级时间戳。"""
    values = [int(row[column] or 0) for column in activity_columns
              if column in row and row[column] is not None]
    return max(values) if values else 0


def _same_row(columns, source_row, target_row) -> bool:
    return all(
        _comparable(column, source_row[column]) == _comparable(column, target_row[column])
        for column in columns
    )


# ----------------------------------------------------------------------
# 导入（--apply）
# ----------------------------------------------------------------------

def apply(data: dict, source_path: Path, code_sha: str, balance: dict) -> dict:
    total = sum(len(data[table]["rows"]) for table, _t, _k, _a, _i in TABLES)
    if not total:
        raise RuntimeError("SQLite 源没有任何行，拒绝导入")
    if not code_sha or len(code_sha) < 7:
        raise RuntimeError("--apply 必须带 --code-sha（迁移代码 commit）")
    hard = balance_alarms(balance)
    if hard:
        raise RuntimeError("余额核对出现硬报警，拒绝导入: %s" % "; ".join(hard))
    report = summary(data)
    run_id = "ledger-" + uuid.uuid4().hex
    with postgres.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ops.data_migration_runs
              (run_id, domain, source_kind, source_locator, source_fingerprint,
               code_sha, state, source_count, details)
            VALUES (%s, %s, 'sqlite', %s, %s, %s, 'running', %s, %s)
            """,
            (run_id, DOMAIN, str(source_path), report["source_checksum"], code_sha,
             total, json.dumps({
                 "tables": {k: v for k, v in report.items() if k != "source_checksum"},
                 "balance": balance_summary(balance),
             }, ensure_ascii=False)),
        )
        imported = {}
        for source_table, target_table, key_column, activity, _identity in TABLES:
            entry = data[source_table]
            imported[source_table] = _import_table(
                conn, run_id, source_table, target_table, key_column, activity,
                entry["columns"], entry["rows"],
            )
        for target_table in IDENTITY_TABLES:
            # 显式带源编号回填后必须把序列推到源最大编号之后，否则新插入撞主键
            conn.execute(
                """
                SELECT setval(
                    pg_get_serial_sequence('ledger.%s', 'id'),
                    COALESCE((SELECT MAX(id) FROM ledger.%s), 0) + 1,
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
            (total,
             json.dumps({"verified": True,
                         "imported": imported,
                         "balance_report_checksum": balance.get("report_checksum", "")}),
             run_id),
        )
    return {**report, "run_id": run_id, "imported": imported}


def _import_table(conn, run_id, source_table, target_table, key_column, activity,
                  columns, rows) -> int:
    imported = 0
    for key in sorted(rows):
        source_row = rows[key]
        key_value = _normalize(key_column, source_row[key_column])
        existing = conn.execute(
            "SELECT * FROM %s WHERE %s = %%s" % (target_table, key_column),
            (key_value,),
        ).fetchone()
        if existing is not None and not _same_row(columns, source_row, existing):
            source_activity = _row_activity(source_row, activity)
            target_activity = _row_activity(dict(existing), activity)
            if target_activity > source_activity:
                # 目标行比源新：说明 PostgreSQL 侧已有切换后的新写入，回填器绝不覆盖
                raise RuntimeError(
                    "冲突：%s.%s 目标活动时间(%s) 比源(%s) 新，停止整批导入"
                    % (target_table, key, target_activity, source_activity)
                )
        values = [_normalize(column, source_row[column]) for column in columns]
        updates = ",".join("%s = EXCLUDED.%s" % (c, c) for c in columns if c != key_column)
        conn.execute(
            "INSERT INTO %s(%s) VALUES (%s) ON CONFLICT (%s) DO %s"
            % (target_table, ",".join(columns),
               ",".join("%s" for _ in columns), key_column,
               "UPDATE SET " + updates if updates else "NOTHING"),
            tuple(values),
        )
        target = conn.execute(
            "SELECT * FROM %s WHERE %s = %%s" % (target_table, key_column),
            (key_value,),
        ).fetchone()
        if target is None:
            raise RuntimeError("导入后读回失败: %s %s" % (target_table, key))
        target_row = dict(target)
        for column in columns:
            if _comparable(column, source_row[column]) != _comparable(column, target_row[column]):
                raise RuntimeError(
                    "行不一致: %s.%s 字段 %s 源=%r 目标=%r"
                    % (target_table, key, column, source_row[column], target_row[column])
                )
        conn.execute(
            """
            INSERT INTO ops.data_migration_items
              (run_id, source_table, chunk_key, source_count, target_count,
               source_checksum, target_checksum, state)
            VALUES (%s, %s, %s, 1, 1, %s, %s, 'verified')
            """,
            (run_id, source_table, key,
             canonical_checksum({key: source_row}),
             canonical_checksum({key: target_row})),
        )
        imported += 1
    return imported


# ----------------------------------------------------------------------
# 余额核对（逐笔重算，只报告不改数）
# ----------------------------------------------------------------------

def read_ledger_flows(conn) -> dict:
    """从 SQLite 连接读四类数据，供纯函数重算（不写、不改）。"""
    def rows(sql, table):
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)]
        if not columns:
            raise RuntimeError("源表不存在: %s" % table)
        return [dict(r) for r in conn.execute(sql)]

    users = rows("SELECT id, username, points FROM users", "users")
    audit = rows(
        "SELECT id, username, delta, before_points, after_points, transaction_key, created_at "
        "FROM points_audit ORDER BY id", "points_audit")
    transfers = rows(
        "SELECT id, transfer_id, sender_user_id, recipient_user_id, amount, created_at "
        "FROM point_transfers ORDER BY id", "point_transfers")
    invites = rows(
        "SELECT id, inviter_user_id, reward_points, status, created_at, voided_at "
        "FROM invite_reward_point_records ORDER BY id", "invite_reward_point_records")
    return {"users": users, "audit": audit, "transfers": transfers, "invites": invites}


def _dup_keys(rows) -> list:
    seen = {}
    dups = []
    for row in rows:
        key = int(row["id"])
        seen[key] = seen.get(key, 0) + 1
    for key, count in sorted(seen.items()):
        if count > 1:
            dups.append({"id": key, "count": count})
    return dups


def _dedupe(rows) -> list:
    """同一 id 只保留第一条（源库主键唯一，出现重复就是库异常；重复只报不重复计算）。"""
    seen = set()
    kept = []
    for row in rows:
        key = int(row["id"])
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    return kept


def reconcile(flows: dict, scope: str = "consume",
              trial_points: int = DEFAULT_TRIAL_POINTS) -> dict:
    """逐笔重算每个用户的可消费余额，并与 ``users.points`` 比对（纯函数，只读）。

    口径（源码依据，2026-09-16 核对）：

    - ``points_audit`` 是唯一权威流水：每条 ``delta`` 就是该用户余额的变动量。
    - ``point_transfers`` 的每一笔都会同时写两行 ``points_audit``
      （``points-transfer:<transfer_id>:out|in``）。因此 transfers 只计**未被镜像**的部分，
      否则每笔赠送都会被算两次；行数、被跳过的镜像笔数都写进报告。
    - ``invite_reward_point_records`` 是**独立奖励账本**，按业务约定不并入 users.points
      （``server/invites.py``：「绝不读取或修改 users.points」）。默认口径 ``consume``
      不把它计入余额；``strict`` 口径把它计入（任务书写法的三流水合计），差异单独列报。
    - ``before_points``/``after_points`` 是当时的余额快照。相邻两行之间
      「上一行 after ≠ 下一行 before」的落差分，以及第一条流水之前的期初余额，
      都是**未入流水的余额变动**（注册赠送 16 点、流水启用前的期初、兜底直写 users.db），
      逐条列出供人核对。
    - 每个用户最后一条流水的 ``after_points`` 必须等于 ``users.points``；
      不等即硬报警（说明有人绕过流水改了余额）。
    """
    if scope not in ("consume", "strict"):
        raise RuntimeError("scope 只能是 consume 或 strict")

    duplicates = (_dup_keys(flows["audit"]) + _dup_keys(flows["invites"])
                  + _dup_keys(flows["transfers"]))
    audit_rows = _dedupe(flows["audit"])
    invite_flows = _dedupe(flows["invites"])
    transfer_flows = _dedupe(flows["transfers"])
    audit_by_user = {}
    audit_keys = set()
    for row in audit_rows:
        audit_by_user.setdefault(str(row["username"]), []).append(row)
        key = row.get("transaction_key")
        if key:
            audit_keys.add(str(key))
    for rows in audit_by_user.values():
        rows.sort(key=lambda r: int(r["id"]))

    transfers_by_user = {}
    transfer_rows = 0
    for row in transfer_flows:
        transfer_rows += 1
        mirrored_out = ("points-transfer:%s:out" % row["transfer_id"]) in audit_keys
        mirrored_in = ("points-transfer:%s:in" % row["transfer_id"]) in audit_keys
        transfers_by_user.setdefault(int(row["sender_user_id"]), []).append(
            {"role": "out", "row": row, "mirrored": mirrored_out})
        transfers_by_user.setdefault(int(row["recipient_user_id"]), []).append(
            {"role": "in", "row": row, "mirrored": mirrored_in})

    invites_by_user = {}
    for row in invite_flows:
        invites_by_user.setdefault(int(row["inviter_user_id"]), []).append(row)

    users_by_id = {}
    users_by_name = {}
    for row in flows["users"]:
        users_by_id[int(row["id"])] = row
        users_by_name[str(row["username"])] = row

    entries = []
    for username in sorted(set(audit_by_user) | set(users_by_name)):
        user = users_by_name.get(username)
        user_id = int(user["id"]) if user else None
        sqlite_points = int(user["points"] or 0) if user else None
        rows = audit_by_user.get(username, [])

        audit_delta = 0
        row_inconsistent = []
        non_balance_rows = 0
        non_balance_delta = 0
        for row in rows:
            delta = int(row["delta"] or 0)
            before = int(row["before_points"] or 0)
            after = int(row["after_points"] or 0)
            audit_delta += delta
            if delta and before == after:
                # 免费内测期间 apply_balance=False：只记账不落余额（delta 记了、余额没动）。
                # 这是已知业务口径，不是损坏；但它会让「Σdelta」与余额天然不等，必须单列。
                non_balance_rows += 1
                non_balance_delta += delta
            elif after - before != delta:
                row_inconsistent.append({
                    "row_id": int(row["id"]),
                    "delta": delta,
                    "before": before,
                    "after": after,
                })

        opening_balance = int(rows[0]["before_points"] or 0) if rows else 0
        ledger_end = int(rows[-1]["after_points"] or 0) if rows else None
        gaps = []
        if rows and opening_balance != 0:
            gaps.append({"row_id": int(rows[0]["id"]), "kind": "opening_balance",
                         "delta": opening_balance})
        for previous, current in zip(rows, rows[1:]):
            gap = int(current["before_points"] or 0) - int(previous["after_points"] or 0)
            if gap:
                gaps.append({"row_id": int(current["id"]),
                             "kind": "between_ledger_rows", "delta": gap})
        untracked_gap_delta = sum(item["delta"] for item in gaps)

        transfer_mirrored = 0
        transfer_unmirrored = 0
        transfer_out = 0
        transfer_in = 0
        user_transfers = transfers_by_user.get(user_id, []) if user_id else []
        for item in user_transfers:
            amount = int(item["row"]["amount"] or 0)
            if item["role"] == "out":
                transfer_out += amount
            else:
                transfer_in += amount
            if item["mirrored"]:
                transfer_mirrored += 1
            else:
                transfer_unmirrored += -amount if item["role"] == "out" else amount

        invite_rows = invites_by_user.get(user_id, []) if user_id else []
        invite_recorded = sum(int(r["reward_points"] or 0) for r in invite_rows
                              if str(r["status"]) == "recorded")
        invite_voided = sum(int(r["reward_points"] or 0) for r in invite_rows
                            if str(r["status"]) == "voided")
        invite_delta = invite_recorded - invite_voided

        entry = {
            "username": username,
            "user_id": user_id,
            "sqlite_points": sqlite_points,
            "audit_rows": len(rows),
            "audit_delta": audit_delta,
            "opening_balance": opening_balance,
            "untracked_changes": gaps,
            "untracked_delta": untracked_gap_delta,
            "non_balance_rows": non_balance_rows,
            "non_balance_delta": non_balance_delta,
            "ledger_row_inconsistent_rows": row_inconsistent,
            "ledger_end": ledger_end,
            "transfer_out": transfer_out,
            "transfer_in": transfer_in,
            "transfer_mirrored_rows": transfer_mirrored,
            "transfer_unmirrored_rows": len(user_transfers) - transfer_mirrored,
            "transfer_unmirrored_delta": transfer_unmirrored,
            "invite_recorded": invite_recorded,
            "invite_voided": invite_voided,
            "invite_delta": invite_delta,
            "recomputed_consume": audit_delta + transfer_unmirrored,
            "recomputed_strict": audit_delta + transfer_unmirrored + invite_delta,
        }
        entry["diff_consume"] = (sqlite_points - entry["recomputed_consume"]
                                 if sqlite_points is not None else None)
        entry["diff_strict"] = (sqlite_points - entry["recomputed_strict"]
                                if sqlite_points is not None else None)

        tags = []
        if user is None:
            tags.append("ledger_rows_without_user")
        if non_balance_rows:
            tags.append("free_beta_rows_without_balance_effect")
        if row_inconsistent:
            tags.append("ledger_row_inconsistent")
        if scope == "strict":
            diff = entry["diff_strict"]
            recomputed = entry["recomputed_strict"]
        else:
            diff = entry["diff_consume"]
            recomputed = entry["recomputed_consume"]
        # 余额恒等式：points = 期初 + Σgaps + Σ(delta 真正落余额的部分)
        #             = 期初 + Σgaps + (Σdelta - Σ未落余额的 delta)
        explained_delta = untracked_gap_delta - non_balance_delta
        if user is not None:
            if diff == 0:
                tags.append("consume_balance_matched")
            elif diff == explained_delta and (untracked_gap_delta or non_balance_delta):
                tags.append("explained_by_untracked_changes")
            elif diff == trial_points and not rows and user_id is not None:
                tags.append("explained_by_trial_points")
            elif not rows and not transfers_by_user.get(user_id) and not invite_rows \
                    and scope == "consume":
                tags.append("untracked_balance_without_ledger_rows")
            else:
                tags.append("unexplained_difference")
            if ledger_end is not None and ledger_end != sqlite_points:
                tags.append("ledger_end_mismatch")
        if invite_delta:
            tags.append("invite_rewards_separate_ledger")
        entry["tags"] = tags
        entry["recomputed"] = recomputed
        entry["diff"] = diff
        entries.append(entry)

    difference_list = [
        {k: e[k] for k in (
            "username", "user_id", "sqlite_points", "audit_rows", "audit_delta",
            "opening_balance", "untracked_delta", "non_balance_rows",
            "non_balance_delta", "ledger_end", "recomputed",
            "diff", "invite_delta", "transfer_unmirrored_delta", "tags")}
        for e in entries if e["diff"] not in (0, None)
    ]
    report = {
        "scope": scope,
        "trial_points": trial_points,
        "users_total": len(flows["users"]),
        "users_checked": len(entries),
        "matched": sum(1 for e in entries if e["diff"] == 0),
        "diffs": len(difference_list),
        "explained": sum(1 for e in entries if "explained_by_untracked_changes" in e["tags"]
                         or "explained_by_trial_points" in e["tags"]),
        "untracked_balance_without_ledger_rows": sum(
            1 for e in entries if "untracked_balance_without_ledger_rows" in e["tags"]),
        "unexplained": sum(1 for e in entries if "unexplained_difference" in e["tags"]),
        "ledger_end_mismatch": sum(1 for e in entries if "ledger_end_mismatch" in e["tags"]),
        "ledger_row_inconsistent": {
            "users": sum(1 for e in entries if "ledger_row_inconsistent" in e["tags"]),
            "detail": {e["username"]: e["ledger_row_inconsistent_rows"]
                       for e in entries if "ledger_row_inconsistent" in e["tags"]},
        },
        "ledger_rows_without_user": sorted(
            e["username"] for e in entries if "ledger_rows_without_user" in e["tags"]),
        "non_balance_rows": sum(e["non_balance_rows"] for e in entries),
        "non_balance_delta": sum(e["non_balance_delta"] for e in entries),
        "duplicate_keys": duplicates,
        "transfer_rows": transfer_rows,
        "invite_reward_rows": len(invite_flows),
        "invite_rewards_are_separate_ledger": True,
        "difference_list": difference_list,
        "users": entries,
    }
    report["report_checksum"] = balance_checksum(report)
    return report


def balance_checksum(report: dict) -> str:
    """人批准用的报告指纹：只覆盖核对口径与差异清单，不含全量用户明细。"""
    payload = {
        "scope": report["scope"],
        "users_total": report["users_total"],
        "matched": report["matched"],
        "diffs": report["diffs"],
        "unexplained": report["unexplained"],
        "ledger_end_mismatch": report["ledger_end_mismatch"],
        "non_balance_rows": report["non_balance_rows"],
        "non_balance_delta": report["non_balance_delta"],
        "difference_list": report["difference_list"],
        "duplicate_keys": report["duplicate_keys"],
    }
    return canonical_checksum(payload)


def balance_summary(report: dict) -> dict:
    """写进 ops.data_migration_runs.details 的核对摘要（不含逐用户明细）。"""
    return {
        "scope": report["scope"],
        "users_total": report["users_total"],
        "matched": report["matched"],
        "diffs": report["diffs"],
        "explained": report["explained"],
        "untracked_balance_without_ledger_rows":
            report["untracked_balance_without_ledger_rows"],
        "unexplained": report["unexplained"],
        "ledger_end_mismatch": report["ledger_end_mismatch"],
        "ledger_row_inconsistent": report["ledger_row_inconsistent"]["users"],
        "ledger_rows_without_user": report["ledger_rows_without_user"],
        "non_balance_rows": report["non_balance_rows"],
        "non_balance_delta": report["non_balance_delta"],
        "duplicate_keys": report["duplicate_keys"],
        "report_checksum": report["report_checksum"],
    }


def balance_alarms(report: dict) -> list:
    """硬报警：出现即拒绝 --apply（不是「人批准就能放行」的那类差异）。"""
    alarms = []
    if report["unexplained"]:
        alarms.append("余额差异无法归因(unexplained=%d)" % report["unexplained"])
    if report["ledger_end_mismatch"]:
        alarms.append("最后一条流水 after_points 与 users.points 不符(%d 人)"
                      % report["ledger_end_mismatch"])
    if report["ledger_row_inconsistent"]["users"]:
        alarms.append("流水行自身不自洽(after-before<>delta) %d 人"
                      % report["ledger_row_inconsistent"]["users"])
    if report["duplicate_keys"]:
        alarms.append("流水存在重复主键 %s" % report["duplicate_keys"])
    return alarms


def verify_balances(source_path: Path, scope: str = "consume",
                    trial_points: int = DEFAULT_TRIAL_POINTS) -> dict:
    conn = connect_source(source_path)
    try:
        flows = read_ledger_flows(conn)
    finally:
        conn.close()
    report = reconcile(flows, scope=scope, trial_points=trial_points)
    report["source"] = str(source_path)
    return report


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path,
                        help="SQLite 源路径（默认 HQ_AUTH_DB / AUTH_DB 环境变量或仓库根 users.db；"
                             "生产为 /home/ubuntu/auth-service/users.db 的只读快照）")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--code-sha", default="")
    parser.add_argument("--ack-balance-report", default="",
                        help="人已审阅的余额核对报告指纹（--verify-balances 打印的 "
                             "balance_report_checksum）；存在差异时 --apply 必填")
    parser.add_argument("--verify-balances", action="store_true",
                        help="只做余额逐笔重算核对（只读 SQLite，不碰 PostgreSQL）")
    parser.add_argument("--balance-scope", choices=("consume", "strict"), default="consume",
                        help="consume=可消费余额（points_audit + 未镜像赠送）；"
                             "strict=再叠加邀请奖励独立账本（任务书三流水口径）")
    parser.add_argument("--trial-points", type=int, default=DEFAULT_TRIAL_POINTS,
                        help="注册赠送点数（默认 %d，与 auth_server.NEW_USER_TRIAL_POINTS 一致）"
                             % DEFAULT_TRIAL_POINTS)
    parser.add_argument("--report", type=Path,
                        help="把完整核对/导入报告写成 JSON 文件")
    parser.add_argument("--limit", type=int, default=50,
                        help="打印差异清单的条数上限（0 = 全部）")
    args = parser.parse_args()

    source = args.source or source_db_default()

    if args.verify_balances:
        report = verify_balances(source, scope=args.balance_scope,
                                 trial_points=args.trial_points)
        payload = {k: v for k, v in report.items() if k != "users"}
        if args.limit:
            payload["difference_list"] = payload["difference_list"][:args.limit]
        print(json.dumps({"mode": "verify-balances", **payload},
                         ensure_ascii=False, sort_keys=True, indent=2))
        if args.report:
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    data = read_source(source)
    if args.apply:
        balance = verify_balances(source, scope=args.balance_scope,
                                  trial_points=args.trial_points)
        if balance["diffs"] and args.ack_balance_report != balance["report_checksum"]:
            raise RuntimeError(
                "余额核对有 %d 个差异，--apply 需要老板批准：请先跑 --verify-balances，"
                "把打印的 balance_report_checksum（当前 %s）原样传给 --ack-balance-report"
                % (balance["diffs"], balance["report_checksum"])
            )
        report = apply(data, source, args.code_sha, balance)
    else:
        report = summary(data)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **report},
                     ensure_ascii=False, sort_keys=True))
    if args.report:
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
