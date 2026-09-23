"""Submit one operation, poll, and download the final video and cover.

Environment: MATRIX_BASE_URL, HQ_USER_TOKEN, HQ_INTERNAL_TOKEN.
Input JSON must have a durable dedupe_key. This command creates a paid job.
"""
import argparse
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    body = json.loads(args.request.read_text(encoding="utf-8-sig"))
    if not body.get("dedupe_key"):
        parser.error("request must contain a persistent dedupe_key")
    base = os.environ["MATRIX_BASE_URL"].rstrip("/")
    origin = urllib.parse.urlsplit(base)
    if origin.scheme != "https" and not (origin.scheme == "http" and origin.hostname in {"localhost", "127.0.0.1", "::1"}):
        parser.error("use HTTPS or a loopback HTTP endpoint")
    headers = {"Authorization": "Bearer " + os.environ["HQ_USER_TOKEN"],
        "X-HQ-Internal-Token": os.environ["HQ_INTERNAL_TOKEN"], "Content-Type": "application/json"}
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def api(path, payload=None):
        url = urllib.parse.urljoin(base + "/", path)
        if urllib.parse.urlsplit(url)[:2] != origin[:2]:
            raise RuntimeError("refusing to send credentials to another origin")
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        for attempt in range(3):
            try:
                with opener.open(urllib.request.Request(url, data=data, headers=headers), timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 502, 503, 504} or attempt == 2:
                    raise RuntimeError(f"API HTTP {exc.code}; retry with the same request/key") from None
            except (TimeoutError, urllib.error.URLError):
                if attempt == 2:
                    raise RuntimeError("response unknown; retry with the same request/key") from None
            time.sleep(3)

    accepted = api("/internal/matrix-template/jobs", body)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "accepted.json").write_text(json.dumps(accepted, ensure_ascii=False, indent=2), encoding="utf-8")
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        status = api(accepted["query_url"])
        print("job", accepted["job_id"], status["status"], flush=True)
        if status["status"] in {"ready", "failed", "canceled"}:
            (args.output / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            if status["status"] != "ready":
                raise SystemExit("generation failed; see status.json")
            for field, name in (("video_url", "video.mp4"), ("cover_url", "cover.jpg")):
                url = status["result"][field]
                if urllib.parse.urlsplit(url).scheme != "https":
                    raise RuntimeError("result is not an HTTPS media URL")
                # Signed media requests never receive service/user credentials.
                with opener.open(url, timeout=120) as source, (args.output / name).open("wb") as target:
                    while chunk := source.read(1024*1024):
                        target.write(chunk)
            if (args.output / "video.mp4").stat().st_size != status["result"]["file_size"]:
                raise RuntimeError("download size mismatch")
            return
        time.sleep(3)
    raise SystemExit("polling timeout; original job may still exist, do not create a new key")


if __name__ == "__main__":
    main()
