#!/usr/bin/env python3
"""Stamp workbench shell asset references with a content hash.

The drift sentinel compares git with production, so generated stamps must be
committed to git instead of being rewritten during deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_DIR = ROOT / "site" / "workbench"
SHELL_FILE = WORKBENCH_DIR / "cloud-shell.js"
STAMP_RE = re.compile(rb"(cloud-shell\.js\?v=)([^\"'<>\s]+)")


def shell_hash() -> str:
    content = SHELL_FILE.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.md5(content).hexdigest()[:8]


def html_files() -> list[Path]:
    return sorted(WORKBENCH_DIR.glob("*.html"))


def rewrite(content: bytes, stamp: str) -> tuple[bytes, int]:
    return STAMP_RE.subn(rb"\g<1>" + stamp.encode("ascii"), content)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update or verify cloud-shell.js cache stamps in workbench HTML."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify stamps without writing files",
    )
    args = parser.parse_args()

    if not SHELL_FILE.exists():
        print(f"missing shell asset: {SHELL_FILE.relative_to(ROOT)}", file=sys.stderr)
        return 2

    stamp = shell_hash()
    stale: list[str] = []
    missing: list[str] = []
    changed: list[str] = []

    for path in html_files():
        content = path.read_bytes()
        updated, count = rewrite(content, stamp)
        rel = path.relative_to(ROOT).as_posix()
        if count == 0:
            missing.append(rel)
            continue
        if updated != content:
            stale.append(rel)
            if not args.check:
                path.write_bytes(updated)
                changed.append(rel)

    if args.check:
        if missing or stale:
            if missing:
                print("missing cloud-shell.js stamp:")
                for item in missing:
                    print(f"  {item}")
            if stale:
                print(f"stale cloud-shell.js stamp, expected v={stamp}:")
                for item in stale:
                    print(f"  {item}")
            return 1
        print(f"cloud-shell.js cache stamp OK: {stamp}")
        return 0

    if missing:
        print("missing cloud-shell.js stamp:")
        for item in missing:
            print(f"  {item}")
        return 1

    if changed:
        print(f"updated cloud-shell.js cache stamp to {stamp}:")
        for item in changed:
            print(f"  {item}")
    else:
        print(f"cloud-shell.js cache stamp already current: {stamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
