#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把历史 jobs 行回填进统一 assets 表。

背景：image/copy/collect/leads 四类产物此前没有资产表，只躺在 jobs.result 里。
好在 jobs_gc.py 只清 payload、从不碰 result 也不删行（线上 12 天前的
copy/collect/leads 任务 result 全都还在），所以历史资产可以完整重建。

幂等：assets 表 UNIQUE(kind, job_id) + INSERT OR IGNORE，重复跑不会产生重复行。
只回填 status='done' 且 result 非空的任务；error / 被 reaper 判死的任务不该有资产。

用法：
    python3 scripts/backfill_assets.py --dry          # 只统计，不写
    python3 scripts/backfill_assets.py                # 实际回填
    python3 scripts/backfill_assets.py --kind image   # 只回填某一类
"""
import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from content_domains import assets_store  # noqa: E402

JOB_DB = os.environ.get("CONTENT_JOB_DB", str(Path(__file__).resolve().parents[1] / "server" / "content_jobs.db"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只统计不写入")
    ap.add_argument("--kind", choices=sorted(assets_store.KINDS), help="只回填指定 kind")
    ap.add_argument("--job-db", default=JOB_DB)
    args = ap.parse_args()

    # sqlite3.connect 对不存在的文件是静默创建空库而非报错：路径写错时脚本会顺利跑完、
    # 打印「扫描 0 行」看起来像成功，实际一行生产数据都没碰到。ship 的部署映射表里也没有
    # scripts/，这个脚本不会被自动同步到 /home/ubuntu/content-api/，路径尤其容易搞错。
    if not Path(args.job_db).is_file():
        sys.exit("任务库不存在：%s\n请用 --job-db 指定，或设 CONTENT_JOB_DB 环境变量。" % args.job_db)
    if not args.dry and not Path(assets_store.ASSET_DB).parent.is_dir():
        sys.exit("资产库目录不存在：%s\n请设 CONTENT_ASSET_DB 环境变量。" % assets_store.ASSET_DB)
    print("任务库: %s\n资产库: %s\n" % (args.job_db, assets_store.ASSET_DB))

    kinds = [args.kind] if args.kind else sorted(assets_store.KINDS)
    if not args.dry:
        assets_store.init_assets()

    total_seen = total_written = 0
    with closing(sqlite3.connect(args.job_db, timeout=20)) as c:
        c.row_factory = sqlite3.Row
        for kind in kinds:
            rows = c.execute(
                """SELECT id, username, result, created_at FROM jobs
                   WHERE kind=? AND status='done' AND result IS NOT NULL AND length(result) > 2
                   ORDER BY id""", (kind,)).fetchall()
            written = skipped = broken = 0
            for r in rows:
                total_seen += 1
                try:
                    result = json.loads(r["result"])
                except Exception:
                    broken += 1
                    continue
                if not r["username"]:
                    skipped += 1
                    continue
                if args.dry:
                    written += 1
                    continue
                if assets_store.record_asset(r["id"], r["username"], kind, result,
                                             created_at=r["created_at"]):
                    written += 1
                else:
                    skipped += 1     # 已存在（幂等命中）
            total_written += written
            print("%-8s 候选 %4d 行 → %s %4d，跳过(已存在/无归属) %3d，result 解析失败 %d"
                  % (kind, len(rows), "将写入" if args.dry else "已写入", written, skipped, broken))

    print("\n%s：扫描 %d 行，%s %d 条资产" % (
        "[dry] 预演" if args.dry else "[done] 完成", total_seen,
        "预计写入" if args.dry else "实际写入", total_written))
    if not args.dry:
        with closing(assets_store.adb()) as c:
            for row in c.execute("SELECT kind, stage, COUNT(*) n FROM assets GROUP BY kind, stage ORDER BY n DESC"):
                print("  assets 表现有  %-8s %-9s %d 条" % (row[0], row[1], row[2]))


if __name__ == "__main__":
    main()
