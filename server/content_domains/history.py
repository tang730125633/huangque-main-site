"""Pure helpers for turning completed jobs into history entries."""
import json


def expand_job_results(rows, limit):
    """Expand every result URL into its own newest-first history item."""
    items = []
    limit = max(0, int(limit or 0))
    if not limit:
        return items
    for row in rows:
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
            if len(items) >= limit:
                return items
    return items
