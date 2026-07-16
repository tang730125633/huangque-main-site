#!/usr/bin/env python3
"""Safely requeue one already-refunded xAI video job for polling."""

import argparse
import sqlite3
import time
from pathlib import Path


DEFAULT_JOB_DB = "/opt/huangque-test-server/server/content_jobs.db"
DEFAULT_ASSET_DB = "/opt/huangque-test-server/server/audio_assets.db"


def _existing_rw_uri(path, label):
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError("%s不存在: %s" % (label, resolved))
    return resolved.as_uri() + "?mode=rw"


def _load_candidate(db, job_id):
    job = db.execute(
        "SELECT id,kind,status,refunded FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    asset = db.execute(
        "SELECT provider_video_id,model,status,phase "
        "FROM assets.video_assets WHERE job_id=?",
        (job_id,),
    ).fetchone()
    request_id = str(asset[0] or "").strip() if asset else ""
    valid = (
        job
        and job[1] == "xiaole_video"
        and job[2] == "error"
        and job[3] == 1
        and asset
        and request_id
        and asset[2] == "failed"
        and asset[3] == "failed"
    )
    if not valid:
        raise ValueError("任务不满足补偿恢复条件")
    return request_id, asset[1]


def recover_job(job_id, apply=False, job_db=DEFAULT_JOB_DB, asset_db=DEFAULT_ASSET_DB):
    """Inspect or atomically requeue an eligible xAI job without changing refunds."""
    job_uri = _existing_rw_uri(job_db, "任务数据库")
    asset_uri = _existing_rw_uri(asset_db, "视频资产数据库")
    db = sqlite3.connect(job_uri, isolation_level=None, uri=True)
    try:
        db.execute("ATTACH DATABASE ? AS assets", (asset_uri,))
        if apply:
            db.execute("BEGIN IMMEDIATE")
        request_id, model = _load_candidate(db, job_id)
        result = {
            "job_id": job_id,
            "request_id": request_id,
            "model": model,
            "apply": bool(apply),
        }
        if not apply:
            return result

        now = int(time.time())
        job_cur = db.execute(
            "UPDATE jobs SET status='pending',error=NULL,updated_at=? "
            "WHERE id=? AND kind='xiaole_video' AND status='error' AND refunded=1",
            (now, job_id),
        )
        if job_cur.rowcount != 1:
            raise RuntimeError("任务状态已变化，未执行恢复")
        asset_cur = db.execute(
            "UPDATE assets.video_assets "
            "SET status='running',phase='xai_pending',error=NULL,updated_at=? "
            "WHERE job_id=? AND status='failed' AND phase='failed' "
            "AND provider_video_id=? AND trim(provider_video_id)<>''",
            (now, job_id, request_id),
        )
        if asset_cur.rowcount != 1:
            raise RuntimeError("资产状态已变化，未执行恢复")
        db.execute("COMMIT")
        return result
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(
        description="恢复已退点但仍有 xAI request_id 的视频任务"
    )
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = recover_job(args.job_id, apply=args.apply)
    print(
        {
            "job_id": result["job_id"],
            "request_id": result["request_id"],
            "model": result["model"],
            "apply": result["apply"],
        }
    )


if __name__ == "__main__":
    main()
