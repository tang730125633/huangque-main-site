#!/usr/bin/env python3
"""
One-time repair script (F group) for #365 VID-FRAME-COVER.
Scan completed videos missing image_file (first frame cover), extract using ffmpeg,
backfill image_file in video_assets / jobs.

Run on server after `apt install ffmpeg` (or the 运维 step).
Idempotent, rate limited, only public outputs.
Re-runnable safely.

Usage:
  python scripts/fix_video_first_frame_covers.py --dry-run
  python scripts/fix_video_first_frame_covers.py
"""

import argparse
import os
import sqlite3
import subprocess
import time
from pathlib import Path

# Try to reuse app helpers if available
try:
    from server.content_domains.video import _extract_first_frame_cover, _out_path, public_url
    from server.content_domains.core import adb as get_adb
    HAVE_APP = True
except Exception:
    HAVE_APP = False

VIDEO_OUT = Path(os.environ.get("VIDEO_OUT_DIR", "video_out"))  # adjust if needed
DB_PATH = os.environ.get("CONTENT_DB", "content.db")  # or jobs.db; adapt to env


def find_missing():
    """Return list of (job_id, video_file) needing cover from video_assets or jobs."""
    items = []
    # Prefer video_assets if present
    try:
        if HAVE_APP:
            with get_adb() as c:  # type: ignore
                rows = c.execute("""
                    SELECT job_id, video_file FROM video_assets
                    WHERE (image_file IS NULL OR image_file = '')
                      AND video_file IS NOT NULL
                      AND status IN ('done', 'completed')
                    ORDER BY created_at DESC
                    LIMIT 500
                """).fetchall()
                for r in rows:
                    items.append((r[0], r[1]))
        else:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            try:
                cur.execute("""
                    SELECT job_id, video_file FROM video_assets
                    WHERE (image_file IS NULL OR image_file = '')
                      AND video_file IS NOT NULL
                      AND status IN ('done', 'completed')
                    LIMIT 500
                """)
                items = cur.fetchall()
            except sqlite3.OperationalError:
                # fallback to jobs table
                cur.execute("""
                    SELECT id, video_file FROM jobs
                    WHERE type='video' AND status='done'
                      AND (image_file IS NULL OR image_file='')
                      AND video_file IS NOT NULL
                    ORDER BY created_at DESC LIMIT 500
                """)
                items = cur.fetchall()
            conn.close()
    except Exception as e:
        print("DB query failed:", e)
    return items


def extract_and_backfill(job_id, video_rel, dry=False):
    if not video_rel:
        return False
    cover = None
    if HAVE_APP:
        cover = _extract_first_frame_cover(video_rel)
    else:
        # standalone fallback
        src = VIDEO_OUT / video_rel if not Path(video_rel).is_absolute() else Path(video_rel)
        if src.is_file() and src.suffix.lower() in {".mp4", ".mov"}:
            cover_p = src.with_name(src.stem + "_cover.jpg")
            try:
                subprocess.run([
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", "1", "-i", str(src), "-vframes", "1", "-q:v", "3", str(cover_p)
                ], check=True, timeout=60)
                if cover_p.is_file():
                    cover = cover_p.name if not "/" in video_rel else video_rel.rsplit("/",1)[0] + "/" + cover_p.name
            except Exception as e:
                print("  ffmpeg failed for", video_rel, e)

    if cover and HAVE_APP:
        try:
            pub = public_url(cover, "image/jpeg")
            print(f"    uploaded to COS public: {pub}")
        except Exception as e:
            print("    public_url(COS) failed (cover may stay local):", e)
    if not cover:
        return False

    print(f"  job={job_id} video={video_rel} -> cover={cover}")
    if dry:
        return True

    # backfill DB
    try:
        if HAVE_APP:
            with get_adb() as c:  # type: ignore
                c.execute("UPDATE video_assets SET image_file=? , updated_at=? WHERE job_id=?",
                          (cover, int(time.time()), job_id))
                # also try jobs if column exists
                try:
                    c.execute("UPDATE jobs SET image_file=? WHERE id=? AND type='video'", (cover, job_id))
                except Exception:
                    pass
        else:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("UPDATE video_assets SET image_file=? WHERE job_id=?", (cover, job_id))
            try:
                c.execute("UPDATE jobs SET image_file=? WHERE id=? AND type='video'", (cover, job_id))
            except Exception:
                pass
            conn.commit()
            conn.close()
        print("    updated DB")
        return True
    except Exception as e:
        print("    DB update error:", e)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()

    print("=== VID-FRAME-COVER repair (step5 style, F group) ===")
    items = find_missing()[:args.limit]
    print(f"Found {len(items)} candidates (limit {args.limit})")
    fixed = 0
    for job_id, vfile in items:
        if extract_and_backfill(job_id, vfile, dry=args.dry_run):
            fixed += 1
            time.sleep(0.2)  # rate limit
    print(f"Done. fixed={fixed} dry={args.dry_run}")
    print("If ffmpeg missing: 运维 install it first (also helps VID-PLAY-SLOW P2).")
    print("Re-run to continue. Only public outputs were considered.")


if __name__ == "__main__":
    main()
