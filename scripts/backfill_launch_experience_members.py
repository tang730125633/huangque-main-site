#!/usr/bin/env python3
"""会员系统上线存量用户迁移。

默认只输出统计，不修改数据库。正式执行必须同时传入：

    --apply --confirm UPGRADE-EXPERIENCE-MEMBERS

升级范围：
1. 2026-07-21 00:00:00（Asia/Shanghai，含）以后注册的用户；
2. 存在已审核到账、且用户备注非空的充值订单的用户。

命中用户统一升级为一年体验官，但不修改点数、不发邀请奖励；已有任意会员
等级的用户保持不变，重复执行不会续期。
"""

import argparse
import datetime as dt
import shutil
import sqlite3
import time
from pathlib import Path
from zoneinfo import ZoneInfo


CONFIRM_TEXT = "UPGRADE-EXPERIENCE-MEMBERS"
DEFAULT_CUTOFF = "2026-07-21"
MEMBERSHIP_YEAR_SECONDS = 365 * 24 * 3600
KNOWN_TIERS = {"experience", "partner", "initiator"}
SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = dt.timezone.utc


def cutoff_timestamp(value):
    day = dt.date.fromisoformat(str(value))
    return int(dt.datetime.combine(day, dt.time.min, SHANGHAI).timestamp())


def now_timestamp(value=None):
    if not value:
        return int(time.time())
    parsed = dt.datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return int(parsed.timestamp())


def created_timestamp(value):
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        pass
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # SQLite datetime('now') 写入 UTC。
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def require_schema(conn):
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    required_columns = {
        "id", "created_at", "membership_tier",
        "membership_started_at", "membership_expires_at",
    }
    missing = sorted(required_columns - user_columns)
    if missing:
        raise RuntimeError("会员字段尚未迁移：" + ",".join(missing))
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    required_tables = {
        "recharge_orders", "membership_audit", "membership_upgrade_records",
    }
    missing_tables = sorted(required_tables - tables)
    if missing_tables:
        raise RuntimeError("会员表尚未迁移：" + ",".join(missing_tables))


def candidate_plan(conn, cutoff):
    require_schema(conn)
    noted_users = {
        row[0]
        for row in conn.execute(
            """SELECT DISTINCT username
                 FROM recharge_orders
                WHERE status='approved'
                  AND TRIM(COALESCE(note,''))<>''"""
        )
    }
    candidates = []
    skipped_members = 0
    for row in conn.execute(
        """SELECT id,username,created_at,membership_tier,membership_expires_at
             FROM users ORDER BY id"""
    ):
        reasons = []
        if created_timestamp(row["created_at"]) >= cutoff:
            reasons.append("registered_since_cutoff")
        if row["username"] in noted_users:
            reasons.append("approved_recharge_with_note")
        if not reasons:
            continue
        if str(row["membership_tier"] or "") in KNOWN_TIERS:
            skipped_members += 1
            continue
        candidates.append({
            "id": int(row["id"]),
            "username": row["username"],
            "reasons": reasons,
        })
    return {"candidates": candidates, "skipped_existing_members": skipped_members}


def backup_database(db_path, now):
    source = Path(db_path).resolve()
    backup = source.with_name(
        source.name + ".pre-membership-%s.bak"
        % dt.datetime.fromtimestamp(now, SHANGHAI).strftime("%Y%m%d-%H%M%S")
    )
    if backup.exists():
        raise RuntimeError("备份文件已存在：" + str(backup))
    shutil.copy2(source, backup)
    return backup


def apply_plan(conn, plan, now):
    expires_at = now + MEMBERSHIP_YEAR_SECONDS
    updated = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in plan["candidates"]:
            current = conn.execute(
                "SELECT membership_tier FROM users WHERE id=?", (item["id"],)
            ).fetchone()
            if not current or str(current["membership_tier"] or "") in KNOWN_TIERS:
                continue
            changed = conn.execute(
                """UPDATE users
                      SET membership_tier='experience',
                          membership_started_at=?,
                          membership_expires_at=?
                    WHERE id=? AND COALESCE(membership_tier,'')=''""",
                (now, expires_at, item["id"]),
            ).rowcount
            if not changed:
                continue
            reason = "会员上线存量迁移：" + ",".join(item["reasons"])
            conn.execute(
                """INSERT INTO membership_audit(
                       username,before_tier,after_tier,before_expires_at,
                       after_expires_at,operator,reason,created_at
                   ) VALUES(?,'','experience',NULL,?,'launch-migration',?,?)""",
                (item["username"], expires_at, reason, now),
            )
            conn.execute(
                """INSERT OR IGNORE INTO membership_upgrade_records(
                       user_id,from_level,to_level,source,source_order_id,
                       operator,status,created_at
                   ) VALUES(?,'','experience','launch_backfill',?,
                            'launch-migration','effective',?)""",
                (item["id"], "launch-backfill:user:%d" % item["id"], now),
            )
            updated += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return updated


def run(db_path, cutoff=DEFAULT_CUTOFF, now=None, apply=False, confirm=""):
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise RuntimeError("数据库不存在：" + str(db_path))
    now = now_timestamp(now)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        plan = candidate_plan(conn, cutoff_timestamp(cutoff))
    finally:
        conn.close()
    result = {
        "matched": len(plan["candidates"]),
        "skipped_existing_members": plan["skipped_existing_members"],
        "updated": 0,
        "backup": "",
        "dry_run": not apply,
    }
    if not apply:
        return result
    if confirm != CONFIRM_TEXT:
        raise RuntimeError("正式执行必须传入 --confirm " + CONFIRM_TEXT)
    backup = backup_database(db_path, now)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        result["updated"] = apply_plan(conn, plan, now)
    finally:
        conn.close()
    result["backup"] = str(backup)
    result["dry_run"] = False
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="users.db 的绝对路径")
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF, help="北京时间日期，默认 2026-07-21")
    parser.add_argument("--now", help="指定迁移时间，默认当前时间")
    parser.add_argument("--apply", action="store_true", help="实际写入；默认仅预览")
    parser.add_argument("--confirm", default="", help="高风险操作确认文本")
    args = parser.parse_args()
    result = run(
        args.db, cutoff=args.cutoff, now=args.now,
        apply=args.apply, confirm=args.confirm,
    )
    print(
        "mode=%s matched=%d updated=%d skipped_existing_members=%d"
        % (
            "apply" if args.apply else "dry-run",
            result["matched"], result["updated"],
            result["skipped_existing_members"],
        )
    )
    if result["backup"]:
        print("backup=" + result["backup"])


if __name__ == "__main__":
    main()
