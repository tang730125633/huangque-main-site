#!/usr/bin/env python3
"""会员系统上线存量用户迁移。

安全流程分为两步：

1. 发现候选人（只读）：
   python scripts/backfill_launch_experience_members.py --db /path/users.db \
       --discovery-out /secure/path/candidates.csv
2. 人工核对 CSV，把确认升级的行 ``approved`` 改为 ``yes``，记录文件 SHA256，
   再按显式名单执行：
   python scripts/backfill_launch_experience_members.py --db /path/users.db \
       --manifest /secure/path/approved.csv --manifest-sha256 <sha256> \
       --apply --confirm UPGRADE-EXPERIENCE-MEMBERS

正式执行绝不会重新按宽泛条件选人；只有清单中明确 approved=yes 的账号会升级。
PR 中不包含、也不会执行生产名单。
"""

import argparse
import csv
import datetime as dt
import hashlib
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
APPROVED_VALUES = {"1", "yes", "true", "approved", "确认", "是"}
DISCOVERY_FIELDS = ("username", "created_at", "reason", "approved")


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
        "id", "username", "created_at", "membership_tier",
        "membership_started_at", "membership_expires_at",
    }
    missing = sorted(required_columns - user_columns)
    if missing:
        raise RuntimeError("会员字段尚未迁移：" + ",".join(missing))
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required_tables = {
        "recharge_orders", "membership_audit", "membership_upgrade_records",
        "membership_voice_slot_entitlements",
    }
    missing_tables = sorted(required_tables - tables)
    if missing_tables:
        raise RuntimeError("会员表尚未迁移：" + ",".join(missing_tables))


def discover_candidates(conn, cutoff):
    """只读发现候选人，供人工审核，不直接作为正式执行输入。"""
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
            reasons.append("registered_since_2026-07-21")
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
            "created_at": str(row["created_at"] or ""),
            "reasons": reasons,
        })
    return {"candidates": candidates, "skipped_existing_members": skipped_members}


def write_discovery(path, candidates):
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=DISCOVERY_FIELDS)
        writer.writeheader()
        for item in candidates:
            writer.writerow({
                "username": item["username"],
                "created_at": item["created_at"],
                "reason": ",".join(item["reasons"]),
                "approved": "",
            })
    return output


def manifest_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_manifest(path):
    source = Path(path).resolve()
    if not source.is_file():
        raise RuntimeError("确认清单不存在：" + str(source))
    with source.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"username", "approved", "reason"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise RuntimeError("确认清单缺少列：" + ",".join(sorted(missing)))
        rows = []
        seen = set()
        for line, raw in enumerate(reader, start=2):
            username = str(raw.get("username") or "").strip()
            approved = str(raw.get("approved") or "").strip().lower()
            if not username or approved not in APPROVED_VALUES:
                continue
            if username in seen:
                raise RuntimeError("确认清单账号重复（第%d行）：%s" % (line, username))
            seen.add(username)
            rows.append({
                "username": username,
                "created_at": str(raw.get("created_at") or "").strip(),
                "reasons": [
                    value.strip()
                    for value in str(raw.get("reason") or "manual_approval").split(",")
                    if value.strip()
                ] or ["manual_approval"],
            })
    if not rows:
        raise RuntimeError("确认清单没有 approved=yes 的账号")
    return rows


def explicit_plan(conn, manifest_rows):
    """只按确认清单构建计划；名单外用户永远不会进入正式计划。"""
    require_schema(conn)
    candidates = []
    skipped_members = 0
    for item in manifest_rows:
        row = conn.execute(
            """SELECT id,username,created_at,membership_tier,membership_expires_at
                 FROM users WHERE username=?""",
            (item["username"],),
        ).fetchone()
        if not row:
            raise RuntimeError("确认清单账号不存在：" + item["username"])
        if str(row["membership_tier"] or "") in KNOWN_TIERS:
            skipped_members += 1
            continue
        candidates.append({
            "id": int(row["id"]),
            "username": row["username"],
            "created_at": str(row["created_at"] or ""),
            "reasons": item["reasons"],
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
            reason = "会员上线显式名单迁移：" + ",".join(item["reasons"])
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
            conn.execute(
                """INSERT OR IGNORE INTO membership_voice_slot_entitlements(
                       username,source,source_order_id,created_at
                   ) VALUES(?,'launch_backfill',?,?)""",
                (item["username"], "launch-backfill:user:%d" % item["id"], now),
            )
            updated += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return updated


def run(db_path, cutoff=DEFAULT_CUTOFF, now=None, apply=False, confirm="",
        manifest=None, expected_manifest_sha256="", discovery_out=None):
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise RuntimeError("数据库不存在：" + str(db_path))
    now = now_timestamp(now)
    manifest_digest = ""

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if manifest:
            manifest_digest = manifest_sha256(manifest)
            if expected_manifest_sha256 and manifest_digest.lower() != expected_manifest_sha256.lower():
                raise RuntimeError("确认清单 SHA256 不匹配")
            plan = explicit_plan(conn, read_manifest(manifest))
        else:
            if apply:
                raise RuntimeError("正式执行必须传入 --manifest 显式确认清单")
            plan = discover_candidates(conn, cutoff_timestamp(cutoff))
    finally:
        conn.close()

    discovery_file = ""
    if discovery_out:
        discovery_file = str(write_discovery(discovery_out, plan["candidates"]))
    result = {
        "matched": len(plan["candidates"]),
        "candidates": plan["candidates"],
        "skipped_existing_members": plan["skipped_existing_members"],
        "updated": 0,
        "backup": "",
        "dry_run": not apply,
        "discovery_file": discovery_file,
        "manifest_sha256": manifest_digest,
    }
    if not apply:
        return result
    if confirm != CONFIRM_TEXT:
        raise RuntimeError("正式执行必须传入 --confirm " + CONFIRM_TEXT)
    if not expected_manifest_sha256:
        raise RuntimeError("正式执行必须传入 --manifest-sha256")

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
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF, help="候选发现使用的上海时区日期")
    parser.add_argument("--now", help="指定迁移时间，默认当前时间")
    parser.add_argument("--discovery-out", help="输出候选审核 CSV；不会修改数据库")
    parser.add_argument("--manifest", help="人工确认后的显式 CSV 清单")
    parser.add_argument("--manifest-sha256", default="", help="已确认清单的 SHA256")
    parser.add_argument("--apply", action="store_true", help="实际写入；默认仅预览")
    parser.add_argument("--confirm", default="", help="高风险操作确认文本")
    args = parser.parse_args()
    result = run(
        args.db, cutoff=args.cutoff, now=args.now,
        apply=args.apply, confirm=args.confirm,
        manifest=args.manifest, expected_manifest_sha256=args.manifest_sha256,
        discovery_out=args.discovery_out,
    )
    print(
        "mode=%s matched=%d updated=%d skipped_existing_members=%d"
        % (
            "apply" if args.apply else "dry-run",
            result["matched"], result["updated"],
            result["skipped_existing_members"],
        )
    )
    for item in result["candidates"]:
        print(
            "candidate username=%s created_at=%s reason=%s"
            % (item["username"], item["created_at"], ",".join(item["reasons"]))
        )
    if result["discovery_file"]:
        print("discovery_file=" + result["discovery_file"])
    if result["manifest_sha256"]:
        print("manifest_sha256=" + result["manifest_sha256"])
    if result["backup"]:
        print("backup=" + result["backup"])


if __name__ == "__main__":
    main()
