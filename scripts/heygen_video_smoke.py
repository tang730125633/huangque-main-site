#!/usr/bin/env python3
"""Smoke test for HeyGen image talking video flow.

Usage:
  HEYGEN_API_KEY=... python scripts/heygen_video_smoke.py --image person.jpg --audio voice.mp3 --out output.mp4
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import pathlib
import sys
import time

try:
    import requests
except ImportError:
    print("Missing dependency: requests", file=sys.stderr)
    sys.exit(2)


API_BASE = "https://api.heygen.com/v3"


def require_file(path: str, label: str) -> pathlib.Path:
    p = pathlib.Path(path).expanduser().resolve()
    if not p.is_file():
        raise SystemExit(f"{label} not found: {p}")
    return p


def request_json(method: str, url: str, api_key: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["x-api-key"] = api_key
    resp = requests.request(method, url, headers=headers, timeout=180, **kwargs)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text[:500]}
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {url} failed: HTTP {resp.status_code} {data}")
    return data


def upload_asset(path: pathlib.Path, api_key: str) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as fh:
        data = request_json(
            "POST",
            f"{API_BASE}/assets",
            api_key,
            files={"file": (path.name, fh, mime)},
        )
    asset_id = ((data.get("data") or {}).get("asset_id") or "").strip()
    if not asset_id:
        raise RuntimeError(f"asset_id missing from upload response: {data}")
    print(f"uploaded {path.name}: {asset_id}")
    return asset_id


def create_video(args, image_asset_id: str, audio_asset_id: str, api_key: str) -> str:
    body = {
        "title": args.title,
        "type": "image",
        "image": {"type": "asset_id", "asset_id": image_asset_id},
        "audio_asset_id": audio_asset_id,
        "resolution": args.resolution,
        "aspect_ratio": args.aspect_ratio,
        "fit": args.fit,
        "expressiveness": args.expressiveness,
        "output_format": "mp4",
    }
    data = request_json(
        "POST",
        f"{API_BASE}/videos",
        api_key,
        headers={"Content-Type": "application/json"},
        json=body,
    )
    video_id = ((data.get("data") or {}).get("video_id") or "").strip()
    if not video_id:
        raise RuntimeError(f"video_id missing from create response: {data}")
    print(f"created video: {video_id}")
    return video_id


def poll_video(video_id: str, api_key: str, interval: int, timeout: int) -> dict:
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        data = request_json("GET", f"{API_BASE}/videos/{video_id}", api_key)
        info = data.get("data") or {}
        status = str(info.get("status") or "")
        if status != last_status:
            print(f"status: {status or 'unknown'}")
            last_status = status
        if status == "completed":
            return info
        if status in {"failed", "error"}:
            raise RuntimeError(f"video failed: {info}")
        time.sleep(interval)
    raise TimeoutError(f"video not completed within {timeout}s: {video_id}")


def download_video(url: str, out_path: pathlib.Path) -> None:
    with requests.get(url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with out_path.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 512):
                if chunk:
                    fh.write(chunk)
    print(f"downloaded: {out_path} ({out_path.stat().st_size} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run HeyGen image talking video smoke test.")
    parser.add_argument("--image", required=True, help="Path to jpg/png/webp person image")
    parser.add_argument("--audio", required=True, help="Path to mp3/wav/m4a narration audio")
    parser.add_argument("--out", default="heygen_output.mp4", help="Output mp4 path")
    parser.add_argument("--title", default="huangque smoke test")
    parser.add_argument("--resolution", default="4k", choices=["720p", "1080p", "4k"])
    parser.add_argument("--aspect-ratio", default="9:16", choices=["9:16", "16:9", "1:1", "4:5", "5:4"])
    parser.add_argument("--fit", default="cover", choices=["cover", "contain"])
    parser.add_argument("--expressiveness", default="low", choices=["low", "medium", "high"])
    parser.add_argument("--poll-interval", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--proxy", help="Optional HTTPS proxy, for example http://127.0.0.1:1082")
    args = parser.parse_args()

    api_key = os.environ.get("HEYGEN_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("HEYGEN_API_KEY is required")
    if args.proxy:
        os.environ["HTTPS_PROXY"] = args.proxy
        os.environ["HTTP_PROXY"] = args.proxy

    image = require_file(args.image, "image")
    audio = require_file(args.audio, "audio")
    out_path = pathlib.Path(args.out).expanduser().resolve()

    image_asset_id = upload_asset(image, api_key)
    audio_asset_id = upload_asset(audio, api_key)
    video_id = create_video(args, image_asset_id, audio_asset_id, api_key)
    info = poll_video(video_id, api_key, args.poll_interval, args.timeout)
    video_url = (info.get("video_url") or "").strip()
    if not video_url:
        raise RuntimeError(f"video_url missing from completed response: {info}")
    download_video(video_url, out_path)
    print(f"video_id={video_id}")
    print(f"video_url={video_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
