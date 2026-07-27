#!/usr/bin/env python3
"""Read-only membership launch checks for a production users.db snapshot."""

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path


KNOWN_TIERS = ("experience", "partner", "initiator")
REQUIRED_USER_COLUMNS = {
    "id", "username", "membership_tier", "membership_started_at",
    "membership_expires_at",
}
REQUIRED_TABLES = {
    "users", "membership_audit", "membership_upgrade_records",
    "membership_voice_slot_entitlements", "user_invites",
    "invite_reward_point_records",
}


def readonly_connection(db_path):
    path = Path(db_path).resolve()
    if not path.is_file():
        raise RuntimeError("数据库不存在：" + str(path))
    conn = sqlite3.connect("file:%s?mode=ro" % path.as_posix(), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def check(db_path, now=None, enforcement=None):
    now = int(now or time.time())
    enforcement = (
        os.environ.get("HQ_MEMBERSHIP_ENFORCEMENT_ENABLED", "0")
        if enforcement is None else str(enforcement)
    ).strip().lower() in {"1", "true", "yes", "on"}
    blockers = []
    warnings = []
    stats = {}

    conn = readonly_connection(db_path)
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing_tables = sorted(REQUIRED_TABLES - tables)
        if missing_tables:
            blockers.append("缺少会员表：" + ",".join(missing_tables))

        if "users" in tables:
            user_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(users)")
            }
            missing_columns = sorted(REQUIRED_USER_COLUMNS - user_columns)
            if missing_columns:
                blockers.append("users 缺少会员字段：" + ",".join(missing_columns))
            else:
                invalid = conn.execute(
                    """SELECT COUNT(*) FROM users
                       WHERE COALESCE(membership_tier,'')<>''
                         AND membership_tier NOT IN (?,?,?)""",
                    KNOWN_TIERS,
                ).fetchone()[0]
                stats["invalid_membership_tiers"] = int(invalid)
                if invalid:
                    blockers.append("存在 %d 个未知会员等级" % invalid)

                for tier in KNOWN_TIERS:
                    stats["active_" + tier] = int(conn.execute(
                        """SELECT COUNT(*) FROM users
                           WHERE membership_tier=? AND membership_expires_at>?""",
                        (tier, now),
                    ).fetchone()[0])
                stats["expired_memberships"] = int(conn.execute(
                    """SELECT COUNT(*) FROM users
                       WHERE membership_tier IN (?,?,?)
                         AND COALESCE(membership_expires_at,0)<=?""",
                    (*KNOWN_TIERS, now),
                ).fetchone()[0])

                if "membership_voice_slot_entitlements" in tables:
                    missing_slots = conn.execute(
                        """SELECT COUNT(*) FROM users u
                           WHERE u.membership_tier IN (?,?,?)
                             AND u.membership_expires_at>?
                             AND NOT EXISTS(
                                 SELECT 1 FROM membership_voice_slot_entitlements e
                                  WHERE e.username=u.username
                             )""",
                        (*KNOWN_TIERS, now),
                    ).fetchone()[0]
                    stats["active_members_without_voice_slot"] = int(missing_slots)
                    if missing_slots:
                        blockers.append(
                            "存在 %d 个有效会员缺少免费音色槽位权益" % missing_slots
                        )

        if "user_invites" in tables:
            duplicate_relations = conn.execute(
                """SELECT COUNT(*) FROM (
                       SELECT invitee_user_id FROM user_invites
                       GROUP BY invitee_user_id HAVING COUNT(*)>1
                   )"""
            ).fetchone()[0]
            stats["duplicate_invite_relations"] = int(duplicate_relations)
            if duplicate_relations:
                blockers.append("存在重复绑定的邀请关系")

        if "invite_reward_point_records" in tables:
            stats["reward_record_count"] = int(conn.execute(
                "SELECT COUNT(*) FROM invite_reward_point_records"
            ).fetchone()[0])
    finally:
        conn.close()

    if enforcement:
        warnings.append("当前环境已开启会员强制校验")
    else:
        warnings.append("当前环境尚未开启会员强制校验")
    return {
        "ready": not blockers,
        "enforcement_enabled": enforcement,
        "blockers": blockers,
        "warnings": warnings,
        "stats": stats,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="users.db 的绝对路径")
    parser.add_argument("--now", type=int, help="验收时间戳，默认当前时间")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    result = check(args.db, now=args.now)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("ready=%s enforcement_enabled=%s" % (
            str(result["ready"]).lower(),
            str(result["enforcement_enabled"]).lower(),
        ))
        for key, value in sorted(result["stats"].items()):
            print("%s=%s" % (key, value))
        for item in result["warnings"]:
            print("warning=" + item)
        for item in result["blockers"]:
            print("blocker=" + item)
    raise SystemExit(0 if result["ready"] else 2)


if __name__ == "__main__":
    main()
