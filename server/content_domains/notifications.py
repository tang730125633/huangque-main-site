"""内容 worker -> auth 服务的作品完成通知。

这里只负责发内网事件；OpenID、用户授权次数和微信模板都由 auth 服务统一处理。
"""
import json
import os
import threading
import urllib.request


AUTH_BASE = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095").rstrip("/")
INTERNAL_TOKEN = os.environ.get("HQ_INTERNAL_TOKEN", "")


def _post_work_complete(username, job_id, kind, timeout=6):
    if not INTERNAL_TOKEN:
        return False
    payload = json.dumps({
        "username": str(username or ""),
        "job_id": str(job_id or ""),
        "kind": str(kind or ""),
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        AUTH_BASE + "/api/auth/internal/subscription/work-complete",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-HQ-Internal-Token": INTERNAL_TOKEN,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except Exception as exc:
        print("[work-subscribe] job=%s notify failed: %s" % (job_id, str(exc)[:200]), flush=True)
        return False


def notify_work_complete_async(username, job_id, kind):
    thread = threading.Thread(
        target=_post_work_complete,
        args=(username, job_id, kind),
        name="work-subscribe-%s" % job_id,
        daemon=True,
    )
    thread.start()
    return thread
