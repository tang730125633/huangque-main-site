#!/usr/bin/env python3
"""Stamp workbench shell asset references with a content hash.

The drift sentinel compares git with production, so generated stamps must be
committed to git instead of being rewritten during deployment.

Every asset listed in ``ASSETS`` gets its own content hash. Adding a new shared
asset (a stylesheet, an init script) means adding it here — otherwise it ships
with a hand-written ``?v=1`` that never changes, and browsers keep serving the
cached copy long after the file was updated.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_DIR = ROOT / "site" / "workbench"
STANDALONE_PAGES = {"device.html"}  # 设备授权页不能注入工作台外壳、登录弹窗或任务脚本。


class Asset:
    """A shared workbench asset whose ``?v=`` stamp tracks its content hash.

    required: every workbench page must reference it, so a missing stamp is an
              error. Page-scoped assets stay optional — pages that don't use them
              simply carry no reference.

    Deliberately a plain class, not a dataclass: dataclass resolves annotations via
    ``sys.modules[cls.__module__]``, which blows up when this file is loaded through
    ``spec_from_file_location`` (as the tests do).
    """

    def __init__(self, name: str, required: bool) -> None:
        self.name = name
        self.required = required

    def __repr__(self) -> str:
        return f"Asset({self.name!r}, required={self.required})"

    @property
    def path(self) -> Path:
        return WORKBENCH_DIR / self.name

    @property
    def pattern(self) -> "re.Pattern[bytes]":
        return re.compile(re.escape(self.name.encode()) + rb"(\?v=)([^\"'<>\s]+)")

    def stamp(self) -> str:
        content = self.path.read_bytes().replace(b"\r\n", b"\n")
        return hashlib.md5(content).hexdigest()[:8]

    def rewrite(self, content: bytes, stamp: str) -> tuple[bytes, int]:
        replacement = self.name.encode() + rb"\g<1>" + stamp.encode("ascii")
        return self.pattern.subn(replacement, content)


ASSETS = (
    Asset("cloud-shell.js", required=True),
    Asset("theme.css", required=False),
    Asset("theme-init.js", required=False),
    Asset("short-drama-center.css", required=False),
    Asset("short-drama-center.js", required=False),
    Asset("short-drama-workspace.css", required=False),
    Asset("short-drama-workspace.js", required=False),
    Asset("canvas/canvas.css", required=False),
    Asset("canvas/canvas-graph.js", required=False),
    Asset("canvas/canvas-state.js", required=False),
    Asset("canvas/canvas-storage.js", required=False),
    Asset("canvas/canvas-api.js", required=False),
    Asset("canvas/canvas-agent.js", required=False),
    Asset("canvas/canvas-export.js", required=False),
    Asset("canvas-collab-sync.js", required=False),
    Asset("canvas/canvas-app.js", required=False),
    Asset("canvas/canvas-short-drama.js", required=False),
    Asset("canvas/canvas-short-drama.css", required=False),
    Asset("canvas/canvas-short-drama-production.js", required=False),
    Asset("canvas/canvas-short-drama-production.css", required=False),
    Asset("canvas/canvas-short-drama-voice.js", required=False),
    Asset("canvas/canvas-short-drama-voice.css", required=False),
    Asset("canvas/canvas-short-drama-timeline.js", required=False),
    Asset("canvas/canvas-short-drama-timeline.css", required=False),
    Asset("canvas/canvas-short-drama-lipsync.js", required=False),
    Asset("canvas/canvas-short-drama-lipsync.css", required=False),
    Asset("canvas/canvas-short-drama-video.js", required=False),
    Asset("canvas/canvas-short-drama-video.css", required=False),
    Asset("canvas/canvas-short-drama-assembly.js", required=False),
    Asset("canvas/canvas-short-drama-assembly.css", required=False),
    Asset("canvas/canvas-short-drama-store.js", required=False),
    Asset("canvas/canvas-short-drama-api.js", required=False),
    Asset("canvas/canvas-short-drama-poller.js", required=False),
    Asset("canvas/canvas-short-drama-player.js", required=False),
    Asset("canvas/canvas-short-drama-versions.js", required=False),
    Asset("canvas/canvas-short-drama-locks.js", required=False),
    Asset("canvas/canvas-short-drama-forms.js", required=False),
    Asset("canvas/canvas-short-drama-completion.js", required=False),
    Asset("canvas/canvas-short-drama-workspace.js", required=False),
    Asset("canvas/canvas-short-drama-workspace.css", required=False),
    Asset("canvas/canvas-digital-presenter.js", required=False),
    Asset("canvas/canvas-digital-presenter.css", required=False),
)


def html_files() -> list[Path]:
    return sorted(path for path in WORKBENCH_DIR.glob("*.html") if path.name not in STANDALONE_PAGES)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update or verify shared workbench asset cache stamps in HTML.",
    )
    parser.add_argument("--check", action="store_true", help="verify stamps without writing files")
    args = parser.parse_args()

    for asset in ASSETS:
        if not asset.path.exists():
            print(f"missing asset: {asset.path.relative_to(ROOT)}", file=sys.stderr)
            return 2

    stamps = {asset.name: asset.stamp() for asset in ASSETS}
    missing: list[str] = []
    stale: list[str] = []
    changed: set[str] = set()

    for path in html_files():
        content = path.read_bytes()
        updated = content
        rel = path.relative_to(ROOT).as_posix()

        for asset in ASSETS:
            updated, count = asset.rewrite(updated, stamps[asset.name])
            if count == 0 and asset.required:
                missing.append(f"{rel} ({asset.name})")

        if updated != content:
            stale.append(rel)
            if not args.check:
                path.write_bytes(updated)
                changed.add(rel)

    summary = ", ".join(f"{name}={stamp}" for name, stamp in stamps.items())

    if missing:
        print("missing required stamp:")
        for item in missing:
            print(f"  {item}")
        return 1

    if args.check:
        if stale:
            print(f"stale stamp, expected {summary}:")
            for item in stale:
                print(f"  {item}")
            return 1
        print(f"cache stamps OK: {summary}")
        return 0

    if changed:
        print(f"updated cache stamps to {summary}:")
        for item in sorted(changed):
            print(f"  {item}")
    else:
        print(f"cache stamps already current: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
