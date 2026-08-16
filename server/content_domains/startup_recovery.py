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
    """Resume paid video jobs with persisted provider ids; fail/refund others."""
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
        if row["kind"] in {
            "short_drama_preview", "short_drama_final", "short_drama_remux"
        }:
            try:
                won_requeue = requeue_job(row["id"])
            except Exception as exc:
                logger(
                    "[startup] 短剧合成任务恢复异常 kind=%s job=%s: %s"
                    % (row["kind"], row["id"], exc), flush=True,
                )
                continue
            if won_requeue:
                logger(
                    "[startup] 短剧合成任务恢复排队 kind=%s job=%s"
                    % (row["kind"], row["id"]),
                    flush=True,
                )
                requeued += 1
                handled += 1
            continue
        request_id = None
        provider = None
        if row["kind"] == "xiaole_video":
            provider = "Grok"
            try:
                video_domain = domains()[2]
                getter = getattr(video_domain, "get_resumable_grok_request", None)
                if getter is None:
                    getter = video_domain.get_resumable_xai_request
                resumable = getter(row["id"])
                if resumable and resumable.get("submission_unknown"):
                    logger(
                        "[startup] %s 提交结果未知，保留 running 待核对 job=%s"
                        % (str(resumable.get("provider") or "官方视频"), row["id"]),
                        flush=True,
                    )
                    continue
                if resumable and str(resumable.get("phase") or "").endswith(
                        "_recovery_required") and not resumable.get(
                            "upscale_prediction_id"):
                    logger(
                        "[startup] %s 任务需人工核对，保留 running job=%s"
                        % (str(resumable.get("provider") or "官方视频"), row["id"]),
                        flush=True,
                    )
                    continue
                request_id = _valid_request_id(resumable)
                if resumable:
                    provider = {
                        "openrouter": "OpenRouter",
                        "xai": "xAI",
                        "seedance": "Seedance",
                        "omni": "Omni",
                    }.get(resumable.get("provider"), "Grok")
            except Exception as exc:
                # 查询失败是“未知”，不是“确认没有上游任务”。后者才允许失败退款；
                # 否则 Auth/SQLite 短暂抖动会把仍在上游运行的付费视频免费送给用户。
                logger("[startup] 视频恢复信息查询失败，保留running不退款 job=%s: %s" %
                       (row["id"], exc), flush=True)
                continue
        elif row["kind"] == "sora_video":
            provider = "Sora"
            try:
                resumable = domains()[2].get_resumable_sora_request(row["id"])
                if resumable and resumable.get("submission_unknown"):
                    logger("[startup] Sora 提交结果未知，保留 running 待核对 job=%s" %
                           row["id"], flush=True)
                    continue
                if resumable and resumable.get("phase") == "sora_recovery_required":
                    logger("[startup] Sora 任务需人工核对，保留 running job=%s" %
                           row["id"], flush=True)
                    continue
                request_id = _valid_request_id(
                    {"request_id": (resumable or {}).get("video_id")}
                )
            except Exception as exc:
                logger("[startup] 查询Sora恢复信息失败，保留 running job=%s: %s" %
                       (row["id"], str(exc)[:200]), flush=True)
                continue

        if request_id:
            try:
                won_requeue = requeue_job(row["id"])
            except Exception as exc:
                logger(
                    "[startup] 恢复%s视频任务 CAS 异常 job=%s: %s" %
                    (provider or "上游", row["id"], exc),
                    flush=True,
                )
                continue
            if won_requeue:
                logger(
                    "[startup] 恢复%s视频任务 job=%s request_id=%s" %
                    (provider or "上游", row["id"], request_id),
                    flush=True,
                )
                requeued += 1
                handled += 1
            # A lost CAS means another actor already changed the job. Never
            # overwrite or refund based on the stale row selected above.
            continue

        error = "服务重启中断，退款处理中，请重新提交"
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
