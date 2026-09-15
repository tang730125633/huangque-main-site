#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成片封面补截：给历史 done 视频任务批量生成封面。

流程：扫 jobs 里 status='done' 的视频类任务（无 cover_url 且有可用视频输入）
→ ffmpeg 截第 1 秒帧 → 上传 COS（video-covers/<job_id>.jpg）→ cover_url 写回 result。

用法（服务器 /home/ubuntu/content-api 目录下）：
    python3 scripts/backfill_video_covers.py --limit 20 --dry-run     # 先试 20 条，只看不写
    python3 scripts/backfill_video_covers.py --limit 0               # 0=全部
    python3 scripts/backfill_video_covers.py --limit 0 --workers 6

幂等：已有 cover_url 的跳过；中断重跑接着补。只写 result 的 cover_url 字段，
不动 status/refunded/其他字段（CAS 只认 done 行）。
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from content_domains import jobs_store  # noqa: E402  顶层只依赖标准库，import 无副作用
from content_domains import cos  # noqa: E402

JOB_DB = os.environ.get("CONTENT_JOBS_DB") or os.path.join(ROOT, "content_jobs.db")
FFMPEG_TIMEOUT = 90


def db():
    return sqlite3.connect(JOB_DB, timeout=30)


def collect_candidates(limit):
    with closing(db()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id,result FROM jobs "
            "WHERE status='done' AND COALESCE(deleted,0)=0 AND result IS NOT NULL "
            "ORDER BY id DESC" + (" LIMIT ?" if limit else ""),
            (int(limit),) if limit else (),
        ).fetchall()
    candidates = []
    for row in rows:
        try:
            result = json.loads(row["result"] or "{}")
        except Exception:
            continue
        if not isinstance(result, dict):
            continue
        if result.get("cover_url"):
            continue
        try:
            src, _kind = jobs_store._video_cover_input(result)
        except Exception:
            continue
        if src:
            candidates.append((int(row["id"]), src))
    return candidates


def generate_cover(job_id, src, dry_run):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg 不在 PATH")
    with tempfile.TemporaryDirectory(prefix="hq-cover-") as td:
        out_jpg = os.path.join(td, "cover.jpg")
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "1", "-i", src,
            "-frames:v", "1", "-vf", "scale=854:-2:flags=lanczos",
            "-q:v", "4", out_jpg,
        ]
        subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT, check=True)
        if not os.path.isfile(out_jpg) or os.path.getsize(out_jpg) <= 0:
            raise RuntimeError("ffmpeg 未产出封面文件")
        if dry_run:
            return "dry-run-ok (%d bytes)" % os.path.getsize(out_jpg)
        cover_url = cos.upload(out_jpg, "video-covers/%d.jpg" % job_id, content_type="image/jpeg")
    if not cover_url or not cover_url.startswith("http"):
        raise RuntimeError("COS 上传未返回直链")
    if not dry_run:
        jobs_store._write_cover_url(db, job_id, cover_url)
    return cover_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只处理最近 N 条（0=全部）")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="只统计候选不截帧")
    args = parser.parse_args()

    if not cos.enabled() and not args.dry_run:
        print("COS 未配置（缺 COS_SECRET_ID/KEY/REGION/BUCKET），无法上传封面", file=sys.stderr)
        return 2

    candidates = collect_candidates(args.limit)
    print("候选任务 %d 条（limit=%s, dry_run=%s, db=%s）"
          % (len(candidates), args.limit or "全部", args.dry_run, JOB_DB), flush=True)
    if args.dry_run:
        for jid, src in candidates[:20]:
            print("  job %d <- %s" % (jid, src[:80]), flush=True)
        return 0
    if not candidates:
        return 0

    ok = skip = fail = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(generate_cover, jid, src, args.dry_run): jid
                   for jid, src in candidates}
        for future in as_completed(futures):
            jid = futures[future]
            try:
                detail = future.result()
                ok += 1
                print("job %d OK %s" % (jid, detail), flush=True)
            except Exception as exc:
                fail += 1
                print("job %d FAIL %s: %s" % (jid, type(exc).__name__, str(exc)[:160]), flush=True)

    print("完成：成功 %d / 失败 %d / 跳过 %d" % (ok, fail, skip), flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
