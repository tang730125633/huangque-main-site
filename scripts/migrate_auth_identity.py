#!/usr/bin/env python3
"""Dry-run or import auth-service ``users.db`` into the identity schema (M6-I).

Dry-run is the default and never writes. ``--apply`` upserts every source row
into its ``identity.*`` table inside **one** transaction, then reads every row
back and compares it with the source; any mismatch rolls the whole import back.
Re-running with identical source data is a no-op.

源库：``/home/ubuntu/auth-service/users.db``（26 张表，见 ``TABLES``）。
目标：PostgreSQL ``identity`` schema，表名保持不变。

设计要点（与 M3A/M3D 回填器同一契约）：

- 每张表的列清单**逐表显式写死**，读源时先用 ``PRAGMA table_info`` 核对；
  源库列集合与清单不一致（有人 ALTER TABLE 加了列）立刻报错停下，绝不静默搬运。
- 时间口径与源完全一致：``users.created_at`` / ``tokens.created_at`` 是 TEXT
  ``YYYY-MM-DD HH:MM:SS``（UTC），其余时间列是秒级 Unix 时间戳（BIGINT）；
  回填只搬运字符串/整数，不做任何换算。
- ``users.points`` 是余额快照，**整行照搬、绝不重算**（点数流水在 ledger 域）。
- SQLite 0/1 整数列写进 PG ``boolean`` 前一律 ``bool()`` 归一化
  （psycopg3 收到 0/1 会 DatatypeMismatch）；NULL 仍然是 NULL，不变成 False。
- 密码哈希、令牌、设备码、openid、手机号/邮箱等**敏感列只参与比对与校验和，
  报错信息只打印长度**，绝不回显明文。
- 自增主键（源 ``INTEGER PRIMARY KEY AUTOINCREMENT``）显式带源编号回填，收尾把
  PostgreSQL 序列推到 ``max(源最大 id, 源 sqlite_sequence.seq) + 1``：SQLite 的
  AUTOINCREMENT 不复用已删除的编号，新序列必须跳过它们。
- ``sqlite_sequence`` 本身不迁移（SQLite 内部表，只用于上面的序列对齐）。
- 冲突护栏：目标行的 ``updated_at`` 比源新（说明切写后 PG 侧已有新写入）→ 整批中止。
- 源校验和 = 全部 26 张表规范化 JSON 的 SHA-256
  （``sort_keys=True, separators=(",", ":"), default=str``，与 M3A 一致）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.db import postgres  # noqa: E402

DOMAIN = "identity"
TARGET_SCHEMA = "identity"
DEFAULT_SOURCE = os.environ.get("HQ_AUTH_DB") or "/home/ubuntu/auth-service/users.db"

Table = namedtuple("Table", "source target key columns identity")


def _t(source, key, columns, identity=False, target=None):
    return Table(
        source=source,
        target=target or source,
        key=tuple(key),
        columns=tuple(columns),
        identity=bool(identity),
    )


# 表按「无外键依赖在前」排列（源库本身没有 FOREIGN KEY，这里按逻辑依赖排，
# 便于逐表顺序回填与人工核对）。identity=True 表示源表是
# INTEGER PRIMARY KEY AUTOINCREMENT，回填后需要对齐 PostgreSQL 序列。
TABLES = (
    _t("users", ("id",), (
        "id", "username", "pw_hash", "pw_salt", "display_name", "points", "role",
        "must_change", "created_at", "account_id", "wx_openid", "account_status",
        "membership_tier", "membership_started_at", "membership_expires_at",
        "card_initial_password"), identity=True),
    _t("tokens", ("token",), (
        "token", "username", "created_at", "expires_at", "scope")),
    _t("cli_device_grants", ("id",), (
        "id", "device_code_hash", "user_code_hash", "client_name",
        "requested_scopes_json", "approved_scopes_json", "username", "status",
        "created_at", "expires_at", "approved_at", "last_poll_at", "token_hash",
        "token_expires_at", "revoked_at"), identity=True),
    _t("cli_refresh_tokens", ("id",), (
        "id", "grant_id", "token_hash", "expires_at", "used_at"), identity=True),
    _t("cli_action_requests", ("username", "action", "request_id"), (
        "username", "action", "request_id", "project_id", "request_hash",
        "status", "http_status", "created_at", "updated_at")),
    _t("membership_audit", ("id",), (
        "id", "username", "before_tier", "after_tier", "before_expires_at",
        "after_expires_at", "operator", "reason", "created_at"), identity=True),
    _t("membership_voice_slot_entitlements", ("username",), (
        "username", "source", "source_order_id", "created_at")),
    _t("friendships", ("id",), (
        "id", "username", "friend_username", "created_at"), identity=True),
    _t("friend_requests", ("id",), (
        "id", "from_username", "to_username", "status", "created_at",
        "reviewed_at"), identity=True),
    _t("invite_campaigns", ("id",), (
        "id", "name", "status", "start_at", "end_at", "code_required",
        "daily_invite_limit", "created_at", "updated_at"), identity=True),
    _t("invite_codes", ("id",), (
        "id", "campaign_id", "inviter_user_id", "code", "status",
        "created_at"), identity=True),
    _t("user_invites", ("id",), (
        "id", "campaign_id", "inviter_user_id", "invitee_user_id",
        "invite_code", "source", "status", "risk_status", "bound_at", "ip_hash",
        "device_hash", "updated_at", "invalid_reason"), identity=True),
    _t("invite_reward_claims", ("id",), (
        "id", "upgrade_record_id", "source_order_id", "invite_relation_id",
        "direct_inviter_user_id", "invitee_user_id", "target_level",
        "event_type", "status", "created_at", "expires_at", "recipient_user_id",
        "recipient_level_snapshot", "reward_points", "transfer_depth",
        "settled_at", "voided_at", "reason", "updated_at"), identity=True),
    _t("invite_reward_notifications", ("id",), (
        "id", "user_id", "claim_id", "notice_type", "operation_key",
        "payload_json", "last_shown_day", "read_at", "created_at",
        "updated_at"), identity=True),
    _t("invite_admin_audit", ("id",), (
        "id", "operator_user_id", "invite_relation_id", "action", "reason",
        "before_json", "after_json", "created_at"), identity=True),
    _t("canvas_boards", ("id",), (
        "id", "owner_username", "name", "data_json", "version", "created_at",
        "updated_at")),
    _t("canvas_members", ("board_id", "username"), (
        "board_id", "username", "role", "invited_by", "created_at")),
    _t("canvas_ops", ("board_id", "version"), (
        "board_id", "version", "op_id", "client_id", "username", "ops_json",
        "created_at")),
    _t("canvas_presence", ("board_id", "client_id"), (
        "board_id", "client_id", "username", "last_seen")),
    _t("business_cards", ("user_id",), (
        "user_id", "public_id", "name", "headline", "company", "bio", "phone",
        "email", "address", "tags_json", "works_json", "links_json",
        "avatar_key", "wechat_qr_key", "phone_public", "email_public",
        "address_public", "wechat_qr_public", "discoverable_in_network",
        "status", "created_at", "updated_at", "published_at",
        "miniprogram_openid")),
    _t("card_referral_journeys", ("journey_id",), (
        "journey_id", "campaign_id", "card_public_id", "inviter_user_id",
        "source", "visited_at", "card_started_at", "registered_user_id",
        "invite_relation_id", "registered_at", "published_at", "expires_at")),
    _t("network_node_ids", ("user_id",), (
        "user_id", "node_id", "created_at")),
    _t("user_notifications", ("id",), (
        "id", "username", "kind", "title", "detail", "created_by", "created_at",
        "campaign_id", "read_at", "popup_snoozed_until"), identity=True),
    _t("announcement_campaigns", ("id",), (
        "id", "title", "detail", "audience_json", "status", "recipient_count",
        "breakdown_json", "created_by", "request_id", "created_at",
        "published_at", "recalled_at", "recalled_by", "wechat_push_requested",
        "wechat_recipient_count"), identity=True),
    _t("wechat_subscription_grants", ("username", "event_type", "template_id"), (
        "username", "event_type", "template_id", "openid", "remaining",
        "last_choice", "updated_at")),
    _t("wechat_subscription_outbox", ("id",), (
        "id", "username", "event_type", "business_id", "job_id", "kind",
        "template_id", "status", "attempts", "lease_until", "next_retry_at",
        "payload_json", "last_error", "created_at", "updated_at", "sent_at"),
        identity=True),
)

# SQLite INTEGER 0/1 -> PostgreSQL boolean：写入前必须归一化（psycopg3 严格类型）
BOOLEAN_COLUMNS = {
    "users": frozenset({"must_change", "card_initial_password"}),
    "invite_campaigns": frozenset({"code_required"}),
    "business_cards": frozenset({
        "phone_public", "email_public", "address_public", "wechat_qr_public",
        "discoverable_in_network",
    }),
    "announcement_campaigns": frozenset({"wechat_push_requested"}),
}

# 敏感列：只比对字节/字符串，任何输出与报错只打印长度（凭证、个人信息）
SENSITIVE_COLUMNS = frozenset({
    "pw_hash", "pw_salt", "token", "token_hash", "device_code_hash",
    "user_code_hash", "request_hash", "ip_hash", "device_hash", "openid",
    "wx_openid", "miniprogram_openid", "phone", "email",
})

# 冲突护栏列：目标比源新说明 PG 侧已有切写后的新写入，整批中止
GUARD_COLUMN = "updated_at"


def row_key(table: Table, row: dict) -> str:
    """审计与校验和用的稳定字符串键（JSON 只接受字符串键，故不用元组）。"""
    return "/".join(str(row[column]) for column in table.key)


def canonical_checksum(payload) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def updated_at_conflict(target_updated_at, source_updated_at) -> bool:
    """目标行是否比源新（None 视为 0；两侧都是秒级 epoch 整数）。"""
    target = 0 if target_updated_at is None else int(target_updated_at)
    source = 0 if source_updated_at is None else int(source_updated_at)
    return target > source


def _safe(column: str, value):
    """报错信息里的取值：敏感列只给长度，其余原样。"""
    if column in SENSITIVE_COLUMNS and value is not None:
        return "<redacted %d chars>" % len(str(value))
    return value


def _normalize(table_name: str, column: str, value):
    if value is None:
        return None
    if column in BOOLEAN_COLUMNS.get(table_name, frozenset()):
        return bool(value)
    return value


def _comparable(table_name: str, column: str, value):
    """比对口径：布尔按真假，其余按字符串（NULL 与 '' 保持不同）。"""
    if column in BOOLEAN_COLUMNS.get(table_name, frozenset()):
        return None if value is None else bool(value)
    return value if value is None else str(value)


def _checksum_row(table_name: str, columns, row) -> dict:
    """校验和用的一行：布尔列先归一，源(0/1)与目标(true/false)才能对上。"""
    return {
        column: _normalize(table_name, column, row[column]) for column in columns
    }


def read_source(path: Path, tables=None) -> dict:
    """只读打开源库，按显式列清单逐表读入（绝不写源库）。"""
    if tables is None:
        tables = TABLES
    if not path.is_file():
        raise RuntimeError("SQLite 源不存在: %s" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    data = {}
    try:
        has_sequence = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone() is not None
        for table in tables:
            actual = [
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(%s)" % table.source).fetchall()
            ]
            if not actual:
                raise RuntimeError("源表不存在或没有列: %s" % table.source)
            if set(actual) != set(table.columns):
                raise RuntimeError(
                    "源表 %s 列集合与回填清单不一致：多=%s 少=%s（先核对再迁移）"
                    % (table.source, sorted(set(actual) - set(table.columns)),
                       sorted(set(table.columns) - set(actual)))
                )
            rows = {}
            for row in conn.execute(
                "SELECT %s FROM %s" % (",".join(table.columns), table.source)
            ):
                record = {column: row[column] for column in table.columns}
                rows[row_key(table, record)] = record
            sequence = None
            if has_sequence:
                sequence = conn.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name=?", (table.source,)
                ).fetchone()
            data[table.source] = {
                "columns": list(table.columns),
                "rows": rows,
                "identity_seq": int(sequence[0]) if sequence else None,
            }
    finally:
        conn.close()
    return data


def summary(data: dict, tables=None) -> dict:
    if tables is None:
        tables = TABLES
    report = {table.source: len(data[table.source]["rows"]) for table in tables}
    report["source_checksum"] = canonical_checksum(
        {table.source: data[table.source]["rows"] for table in tables}
    )
    return report


def sequence_target(table: Table, entry: dict) -> int:
    """回填后 PostgreSQL 序列应到达的下一值（含源 sqlite_sequence 的已用编号）。"""
    ids = [row[table.key[0]] for row in entry["rows"].values()]
    maximum = max([0] + [int(value) for value in ids if value is not None])
    used = entry.get("identity_seq") or 0
    return max(maximum, int(used)) + 1


def _import_table(conn, run_id, table: Table, entry: dict) -> int:
    columns = list(table.columns)
    key_columns = list(table.key)
    updates = ",".join(
        "%s = EXCLUDED.%s" % (column, column)
        for column in columns if column not in key_columns
    )
    where = " AND ".join("%s = %%s" % column for column in key_columns)
    imported = 0
    for key in sorted(entry["rows"]):
        source_row = entry["rows"][key]
        key_values = tuple(
            _normalize(table.source, column, source_row[column])
            for column in key_columns
        )
        existing = conn.execute(
            "SELECT * FROM %s.%s WHERE %s" % (TARGET_SCHEMA, table.target, where),
            key_values,
        ).fetchone()
        if (
            existing is not None
            and GUARD_COLUMN in columns
            and updated_at_conflict(existing[GUARD_COLUMN], source_row[GUARD_COLUMN])
        ):
            raise RuntimeError(
                "冲突：%s.%s 键 %s 目标 updated_at(%s) 比源(%s) 新，停止整批导入"
                % (TARGET_SCHEMA, table.target, key,
                   existing[GUARD_COLUMN], source_row[GUARD_COLUMN])
            )
        values = [
            _normalize(table.source, column, source_row[column])
            for column in columns
        ]
        conn.execute(
            "INSERT INTO %s.%s(%s) VALUES (%s) ON CONFLICT (%s) DO UPDATE SET %s"
            % (TARGET_SCHEMA, table.target, ",".join(columns),
               ",".join("%s" for _ in columns), ",".join(key_columns), updates),
            tuple(values),
        )
        target = conn.execute(
            "SELECT %s FROM %s.%s WHERE %s"
            % (",".join(columns), TARGET_SCHEMA, table.target, where),
            key_values,
        ).fetchone()
        if target is None:
            raise RuntimeError("导入后读回失败: %s.%s 键 %s"
                               % (TARGET_SCHEMA, table.target, key))
        for column in columns:
            source_value = _comparable(table.source, column, source_row[column])
            target_value = _comparable(table.target, column, target[column])
            if source_value != target_value:
                raise RuntimeError(
                    "行不一致: %s.%s 键 %s 字段 %s 源=%r 目标=%r"
                    % (TARGET_SCHEMA, table.target, key, column,
                       _safe(column, source_row[column]), _safe(column, target[column]))
                )
        conn.execute(
            """
            INSERT INTO ops.data_migration_items
              (run_id, source_table, chunk_key, source_count, target_count,
               source_checksum, target_checksum, state)
            VALUES (%s, %s, %s, 1, 1, %s, %s, 'verified')
            """,
            (run_id, table.source, key,
             canonical_checksum(
                 {key: _checksum_row(table.source, columns, source_row)}),
             canonical_checksum(
                 {key: _checksum_row(table.target, columns, target)})),
        )
        imported += 1
    return imported


def apply(data: dict, source_path: Path, code_sha: str, tables=None) -> dict:
    if tables is None:
        tables = TABLES
    if not code_sha or len(code_sha) < 7:
        raise RuntimeError("--apply 必须带 --code-sha（迁移代码 commit）")
    total = sum(len(data[table.source]["rows"]) for table in tables)
    if not total:
        raise RuntimeError("SQLite 源没有任何行，拒绝导入")
    report = summary(data, tables)
    run_id = "auth-identity-" + uuid.uuid4().hex
    with postgres.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ops.data_migration_runs
              (run_id, domain, source_kind, source_locator, source_fingerprint,
               code_sha, state, source_count, details)
            VALUES (%s, %s, 'sqlite', %s, %s, %s, 'running', %s, %s)
            """,
            (run_id, DOMAIN, str(source_path), report["source_checksum"],
             code_sha, total, json.dumps(report)),
        )
        imported = 0
        for table in tables:
            imported += _import_table(conn, run_id, table, data[table.source])
        sequences = {}
        for table in tables:
            if not table.identity:
                continue
            next_value = sequence_target(table, data[table.source])
            conn.execute(
                "SELECT setval(pg_get_serial_sequence('%s.%s', %s), %%s, false)"
                % (TARGET_SCHEMA, table.target, "'%s'" % table.key[0]),
                (next_value,),
            )
            sequences[table.source] = next_value
        conn.execute(
            """
            UPDATE ops.data_migration_runs
               SET state = 'verified', target_count = %s,
                   finished_at = CURRENT_TIMESTAMP,
                   details = details || %s::jsonb
             WHERE run_id = %s
            """,
            (imported,
             json.dumps({"verified": True, "sequences": sequences}), run_id),
        )
    return {**report, "run_id": run_id, "imported": imported,
            "sequences": sequences}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path,
                        help="SQLite 源路径（默认 HQ_AUTH_DB 环境变量或 "
                             "/home/ubuntu/auth-service/users.db；"
                             "生产请用只读快照副本）")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--code-sha", default="")
    args = parser.parse_args()
    source = args.source or Path(DEFAULT_SOURCE)
    data = read_source(source)
    report = apply(data, source, args.code_sha) if args.apply else summary(data)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **report},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
