"""Durable content-side relay for video completion events."""
import json
import os
import time
import urllib.request
from contextlib import closing


AUTH_BASE = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095").rstrip("/")
INTERNAL_TOKEN = os.environ.get("HQ_INTERNAL_TOKEN", "")
LEASE_SECONDS = 60


def _claim(jdb):
    now = int(time.time())
    with closing(jdb()) as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("""UPDATE video_notification_outbox
                    SET status='pending',lease_until=0,
                        last_error='relay lease expired',updated_at=?
                    WHERE status='sending' AND lease_until<?""", (now, now))
        c.execute("UPDATE video_notification_outbox SET status='pending' WHERE status='failed' AND next_retry_at<=?", (now,))
        row = c.execute(
            """SELECT * FROM video_notification_outbox
               WHERE status='pending' AND next_retry_at<=?
               ORDER BY job_id LIMIT 1""", (now,)
        ).fetchone()
        if not row:
            c.commit()
            return None
        cur = c.execute(
            """UPDATE video_notification_outbox
               SET status='sending',lease_until=?,attempts=attempts+1,updated_at=?
               WHERE job_id=? AND status='pending'""",
            (now + LEASE_SECONDS, now, row["job_id"]),
        )
        c.commit()
        if not cur.rowcount:
            return None
        result = {key: row[key] for key in row.keys()}
        result["attempts"] = int(result["attempts"] or 0) + 1
        return result


def _post(row, timeout=8):
    if not INTERNAL_TOKEN:
        raise RuntimeError("HQ_INTERNAL_TOKEN is not configured")
    payload = json.dumps({
        "username": row["username"], "job_id": row["job_id"], "kind": row["kind"],
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        AUTH_BASE + "/api/auth/internal/subscription/video-complete",
        data=payload,
        headers={"Content-Type": "application/json", "X-HQ-Internal-Token": INTERNAL_TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8") or "{}")
        if response.status < 200 or response.status >= 300:
            raise RuntimeError("auth returned %s" % response.status)
        return body


def _finish(jdb, job_id, status, error=""):
    now = int(time.time())
    with closing(jdb()) as c:
        c.execute(
            """UPDATE video_notification_outbox
               SET status=?,lease_until=0,last_error=?,updated_at=?,sent_at=?
               WHERE job_id=? AND status='sending'""",
            (status, str(error)[:300], now, now if status == "sent" else None, job_id),
        )
        c.commit()


def _retry(jdb, row, error):
    now = int(time.time())
    attempts = int(row.get("attempts") or 0)
    delay = min(3600, 2 ** min(attempts, 10))
    with closing(jdb()) as c:
        c.execute(
            """UPDATE video_notification_outbox
               SET status=?,lease_until=0,next_retry_at=?,last_error=?,updated_at=?
               WHERE job_id=? AND status='sending'""",
            ("failed", now + delay, str(error)[:300], now, row["job_id"]),
        )
        c.commit()


def drain_once(jdb):
    row = _claim(jdb)
    if not row:
        return False
    try:
        _post(row)
        _finish(jdb, row["job_id"], "sent")
    except Exception as exc:
        _retry(jdb, row, exc)
    return True


def scanner(jdb, interval=5):
    while True:
        try:
            while drain_once(jdb):
                pass
        except Exception:
            pass
        time.sleep(interval)
