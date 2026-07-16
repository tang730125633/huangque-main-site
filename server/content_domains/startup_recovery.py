"""Recover content jobs left running when the service restarts."""

import time
from contextlib import closing


def requeue_running_job(jdb, job_id):
    """Atomically return one still-running job to the pending queue."""
    with closing(jdb()) as conn:
        cursor = conn.execute(
            "UPDATE jobs SET status='pending', error=NULL, updated_at=? "
            "WHERE id=? AND status='running'",
            (int(time.time()), job_id),
        )
        conn.commit()
        return cursor.rowcount == 1


def _valid_request_id(resumable):
    if not isinstance(resumable, dict):
        return None
    request_id = resumable.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        return None
    return request_id.strip()


def reclaim_orphaned_running(
    *, jdb, service_owner, domains, set_terminal, refund_once,
    mark_video_asset_failed, requeue_job, logger=print,
):
    """Resume persisted xAI jobs and fail/refund other running orphans."""
    try:
        with closing(jdb()) as conn:
            rows = conn.execute(
                "SELECT id, username, cost, kind FROM jobs "
                "WHERE status='running' AND COALESCE(owner,?)=?",
                (service_owner, service_owner),
            ).fetchall()
    except Exception:
        return 0

    handled = requeued = failed = 0
    for row in rows:
        request_id = None
        if row["kind"] == "xiaole_video":
            try:
                request_id = _valid_request_id(
                    domains()[2].get_resumable_xai_request(row["id"])
                )
            except Exception:
                request_id = None

        if request_id:
            try:
                won_requeue = requeue_job(row["id"])
            except Exception as exc:
                logger(
                    "[startup] 恢复xAI视频任务 CAS 异常 job=%s: %s" %
                    (row["id"], exc),
                    flush=True,
                )
                continue
            if won_requeue:
                logger(
                    "[startup] 恢复xAI视频任务 job=%s request_id=%s" %
                    (row["id"], request_id),
                    flush=True,
                )
                requeued += 1
                handled += 1
            # A lost CAS means another actor already changed the job. Never
            # overwrite or refund based on the stale row selected above.
            continue

        error = "服务重启中断，已退点，请重新提交"
        if set_terminal(row["id"], "error", error=error):
            refund_once(row["id"], row["username"], row["cost"])
            mark_video_asset_failed(row["id"], row["kind"], error)
            failed += 1
            handled += 1

    if handled:
        logger(
            "[startup] 处理重启遗留任务 %d 个(恢复排队 %d，失败退点 %d)" %
            (handled, requeued, failed),
            flush=True,
        )
    return handled
