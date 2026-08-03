#!/usr/bin/env python3
"""Read-only D-6 completion consistency checker."""

import argparse
import json
import sqlite3
import time


def _table_exists(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def _issue(code, project_id, detail):
    return {
        "code": code,
        "project_id": project_id,
        "detail": detail,
    }


def inspect_connection(conn, now=None, stale_seconds=300):
    """Return inconsistencies without changing any database state."""
    conn.row_factory = sqlite3.Row
    now = int(now or time.time())
    issues = []
    required = {
        "short_drama_projects",
        "short_drama_completions",
        "short_drama_completion_attempts",
    }
    missing = sorted(name for name in required if not _table_exists(conn, name))
    if missing:
        return {
            "ok": False,
            "checked_at": now,
            "issues": [_issue(
                "schema_missing", None, "缺少数据表：" + ",".join(missing)
            )],
        }

    for row in conn.execute(
        "SELECT p.id,p.completion_id,c.completion_id AS snapshot_id "
        "FROM short_drama_projects p "
        "LEFT JOIN short_drama_completions c ON c.project_id=p.id "
        "WHERE p.stage='completed' AND (p.completion_id IS NULL "
        "OR c.completion_id IS NULL OR p.completion_id<>c.completion_id)"
    ):
        issues.append(_issue(
            "completed_snapshot_mismatch", row["id"],
            "completed 项目与不可变快照的 completion_id 不一致",
        ))

    for row in conn.execute(
        "SELECT c.project_id,c.completion_id,p.stage,p.completion_id AS project_completion_id "
        "FROM short_drama_completions c "
        "LEFT JOIN short_drama_projects p ON p.id=c.project_id "
        "WHERE p.id IS NULL OR p.stage<>'completed' "
        "OR p.completion_id IS NULL OR p.completion_id<>c.completion_id"
    ):
        issues.append(_issue(
            "snapshot_project_mismatch", row["project_id"],
            "不可变快照没有匹配的 completed 项目指针",
        ))

    if _table_exists(conn, "short_drama_final_assets"):
        for row in conn.execute(
            "SELECT c.project_id,c.asset_id,a.id AS found_asset,"
            "a.archive_status,a.deleted "
            "FROM short_drama_completions c "
            "LEFT JOIN short_drama_final_assets a "
            "ON a.id=c.asset_id AND a.project_id=c.project_id "
            "WHERE a.id IS NULL OR a.deleted<>0 OR a.archive_status<>'ready'"
        ):
            issues.append(_issue(
                "delivery_asset_invalid", row["project_id"],
                "交付快照引用的正式资产不存在、已删除或未完成归档",
            ))

    cutoff = now - max(60, int(stale_seconds or 300))
    for row in conn.execute(
        "SELECT project_id,id FROM short_drama_completion_attempts "
        "WHERE state='started' AND updated_at<=?",
        (cutoff,),
    ):
        issues.append(_issue(
            "completion_attempt_stale", row["project_id"],
            "完成确认 attempt 长时间停留在 started：" + str(row["id"]),
        ))

    return {"ok": not issues, "checked_at": now, "issues": issues}


def main():
    parser = argparse.ArgumentParser(description="只读检查 D-6 完成交付一致性")
    parser.add_argument("--db", required=True, help="短剧 SQLite 数据库路径")
    parser.add_argument("--stale-seconds", type=int, default=300)
    args = parser.parse_args()
    uri = "file:%s?mode=ro" % args.db.replace("\\", "/")
    with sqlite3.connect(uri, uri=True) as conn:
        result = inspect_connection(
            conn, stale_seconds=max(60, args.stale_seconds)
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result["ok"] else 2)


if __name__ == "__main__":
    main()
