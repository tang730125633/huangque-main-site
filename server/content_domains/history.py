"""Pure helpers for turning completed jobs into history entries."""
import json


def _value(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def expand_job_results(rows, limit, offset=0, include_failed=False):
    """Expand every result URL into its own newest-first history item."""
    items = []
    limit = max(0, int(limit or 0))
    offset = max(0, int(offset or 0))
    if not limit:
        return items
    end = limit + offset
    for row in rows:
        status = str(_value(row, "status", "done") or "done").lower()
        if include_failed and status in {"error", "failed"}:
            try:
                payload = json.loads(_value(row, "payload", "{}") or "{}")
            except Exception:
                payload = {}
            items.append({
                "job_id": row["id"],
                "status": "error",
                "error": _value(row, "error", "") or "生成失败",
                "prompt": payload.get("prompt") if isinstance(payload, dict) else "",
                "created_at": row["created_at"],
            })
            if len(items) >= end:
                return items[offset:end]
            continue
        try:
            result = json.loads(row["result"])
        except Exception:
            continue
        if not isinstance(result, dict):
            continue

        urls = result.get("urls")
        if not isinstance(urls, list) or not urls:
            urls = [result.get("url")]

        seen = set()
        for url in urls:
            if not isinstance(url, str) or not url or url in seen:
                continue
            seen.add(url)
            items.append({
                "job_id": row["id"],
                "status": "done",
                "url": url,
                "mode": result.get("mode"),
                "prompt": result.get("prompt"),
                "text": result.get("text"),
                "ctype": result.get("ctype"),
                "voice": result.get("voice"),
                "speed": result.get("speed"),
                "pitch": result.get("pitch"),
                "volume": result.get("volume"),
                "emotion": result.get("emotion"),
                "created_at": row["created_at"],
            })
            if len(items) >= end:
                return items[offset:end]
    return items[offset:end]
