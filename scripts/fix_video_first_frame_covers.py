#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill first-frame covers for historical video assets (#365).

The video asset table lives in ``audio_assets.db`` for legacy reasons.  This
script is intentionally standalone so running it from ``scripts/`` cannot fall
back to an empty SQLite database when application imports are unavailable.

Usage:
  python3 scripts/fix_video_first_frame_covers.py --dry-run
  python3 scripts/fix_video_first_frame_covers.py --limit 20
"""

import argparse
import os
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_BASE = Path("/home/ubuntu/content-api")


def _default_path(env_name, production_path, local_path):
    configured = os.environ.get(env_name)
    if configured:
        return Path(configured)
    return production_path if production_path.exists() else local_path


DEFAULT_ASSET_DB = _default_path(
    "CONTENT_ASSET_DB",
    PRODUCTION_BASE / "audio_assets.db",
    ROOT / "server" / "audio_assets.db",
)
DEFAULT_CONTENT_OUT = _default_path(
    "CONTENT_OUT",
    PRODUCTION_BASE / "content_out",
    ROOT / "server" / "content_out",
)
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm"}


def _table_columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def find_missing(asset_db, limit=500):
    """Return missing-cover rows as (asset_id, job_id, video_file)."""
    uri = Path(asset_db).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=20)) as conn:
        columns = _table_columns(conn, "video_assets")
        required = {"id", "job_id", "video_file", "image_file", "status", "created_at"}
        if not required.issubset(columns):
            missing = ", ".join(sorted(required - columns))
            raise RuntimeError("video_assets table is missing columns: %s" % missing)
        deleted_clause = "AND COALESCE(deleted, 0)=0" if "deleted" in columns else ""
        return conn.execute(
            """SELECT id, job_id, video_file FROM video_assets
               WHERE COALESCE(image_file, '')=''
                 AND COALESCE(video_file, '')!=''
                 AND status IN ('done', 'completed')
                 %s
               ORDER BY created_at DESC, id DESC
               LIMIT ?""" % deleted_clause,
            (max(1, min(500, int(limit))),),
        ).fetchall()


def resolve_video_path(content_out, video_file):
    """Resolve a stored relative path while keeping it inside CONTENT_OUT."""
    raw = str(video_file or "").strip().replace("\\", "/")
    if not raw or "://" in raw or Path(raw).suffix.lower() not in VIDEO_SUFFIXES:
        return None
    root = Path(content_out).resolve()
    candidate = (root / raw.lstrip("/")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def extract_cover(content_out, video_file, runner=subprocess.run):
    src = resolve_video_path(content_out, video_file)
    if not src or not src.is_file():
        return None
    cover = src.with_name(src.stem + "_cover.jpg")
    runner(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", "1", "-i", str(src), "-vframes", "1", "-q:v", "3", str(cover),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=120,
    )
    if not cover.is_file() or cover.stat().st_size <= 0:
        return None
    return cover.relative_to(Path(content_out).resolve()).as_posix()


def backfill_asset(asset_db, asset_id, cover_file):
    """Set a cover only if the row is still missing one (idempotent/CAS-like)."""
    with closing(sqlite3.connect(str(asset_db), timeout=20)) as conn:
        columns = _table_columns(conn, "video_assets")
        if "updated_at" in columns:
            cur = conn.execute(
                """UPDATE video_assets SET image_file=?, updated_at=?
                   WHERE id=? AND COALESCE(image_file, '')=''""",
                (cover_file, int(time.time()), asset_id),
            )
        else:
            cur = conn.execute(
                """UPDATE video_assets SET image_file=?
                   WHERE id=? AND COALESCE(image_file, '')=''""",
                (cover_file, asset_id),
            )
        conn.commit()
        return cur.rowcount == 1


def repair(asset_db, content_out, limit=100, dry_run=False, sleep_seconds=0.2,
           runner=subprocess.run, sleeper=time.sleep):
    rows = find_missing(asset_db, limit)
    stats = {"candidates": len(rows), "ready": 0, "fixed": 0, "skipped": 0}
    for asset_id, job_id, video_file in rows:
        src = resolve_video_path(content_out, video_file)
        if not src or not src.is_file():
            stats["skipped"] += 1
            print("  skip asset=%s job=%s: source missing or unsafe: %s" % (
                asset_id, job_id, video_file))
            continue
        stats["ready"] += 1
        if dry_run:
            print("  [dry] asset=%s job=%s video=%s" % (asset_id, job_id, video_file))
            continue
        try:
            cover_file = extract_cover(content_out, video_file, runner=runner)
        except Exception as exc:
            stats["skipped"] += 1
            print("  ffmpeg failed asset=%s job=%s: %s" % (asset_id, job_id, exc))
            continue
        if cover_file and backfill_asset(asset_db, asset_id, cover_file):
            stats["fixed"] += 1
            print("  fixed asset=%s job=%s cover=%s" % (asset_id, job_id, cover_file))
            if sleep_seconds:
                sleeper(sleep_seconds)
        else:
            stats["skipped"] += 1
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", "--dry", action="store_true", help="只检查候选，不生成文件或写数据库")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--asset-db", type=Path, default=DEFAULT_ASSET_DB)
    parser.add_argument("--content-out", type=Path, default=DEFAULT_CONTENT_OUT)
    args = parser.parse_args()

    if not args.asset_db.is_file():
        raise SystemExit(
            "资产库不存在：%s\n请用 --asset-db 指定，或设置 CONTENT_ASSET_DB。" % args.asset_db
        )
    if not args.content_out.is_dir():
        raise SystemExit(
            "产物目录不存在：%s\n请用 --content-out 指定，或设置 CONTENT_OUT。" % args.content_out
        )
    if not args.dry_run and not shutil.which("ffmpeg"):
        raise SystemExit("未找到 ffmpeg；请先安装后再执行实际回填。")

    print("asset db: %s" % args.asset_db.resolve())
    print("content out: %s" % args.content_out.resolve())
    stats = repair(
        args.asset_db,
        args.content_out,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(
        "%s candidates=%d ready=%d fixed=%d skipped=%d" % (
            "[dry-run]" if args.dry_run else "[done]",
            stats["candidates"], stats["ready"], stats["fixed"], stats["skipped"],
        )
    )


if __name__ == "__main__":
    main()
