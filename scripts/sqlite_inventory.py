#!/usr/bin/env python3
"""Deterministic SQLite dependency inventory and no-growth CI ratchet."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
BASELINE = ROOT / "docs" / "database" / "sqlite-runtime-inventory.json"
PATTERNS = {
    "connect_calls": re.compile(r"sqlite3\.connect"),
    "begin_immediate": re.compile(r"BEGIN\s+IMMEDIATE", re.I),
    "pragma": re.compile(r"\bPRAGMA\b", re.I),
    "insert_or": re.compile(r"INSERT\s+OR\s+", re.I),
    "autoincrement": re.compile(r"\bAUTOINCREMENT\b", re.I),
    "last_insert_rowid": re.compile(r"last_insert_rowid\s*\(", re.I),
}
SQLITE_IMPORT = re.compile(r"(?m)^\s*(?:import\s+sqlite3\b|from\s+sqlite3\s+import\b)")


def inventory() -> dict:
    files = []
    totals = {name: 0 for name in PATTERNS}
    for path in sorted(SERVER.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not SQLITE_IMPORT.search(text):
            continue
        counts = {name: len(pattern.findall(text)) for name, pattern in PATTERNS.items()}
        for name, count in counts.items():
            totals[name] += count
        files.append({"path": path.relative_to(ROOT).as_posix(), **counts})
    return {
        "schema": "huangque.sqlite-runtime-inventory/v1",
        "scope": "server/**/*.py",
        "sqlite_files": len(files),
        "totals": totals,
        "files": files,
    }


def ratchet_violations(current: dict, baseline: dict) -> list[str]:
    allowed = {item["path"]: item for item in baseline.get("files", [])}
    violations = []
    for item in current.get("files", []):
        path = item["path"]
        old = allowed.get(path)
        if old is None:
            violations.append(f"new SQLite-dependent file: {path}")
            continue
        for metric in PATTERNS:
            if item[metric] > old.get(metric, 0):
                violations.append(
                    f"SQLite usage increased: {path} {metric} "
                    f"{old.get(metric, 0)} -> {item[metric]}"
                )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    current = inventory()
    encoded = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.write:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(encoded, encoding="utf-8")
    if args.check:
        try:
            baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"SQLite inventory baseline is missing: {BASELINE.relative_to(ROOT)}")
            return 1
        violations = ratchet_violations(current, baseline)
        if violations:
            print("SQLite dependency ratchet failed. Existing usage may only decrease:")
            for violation in violations:
                print(f"- {violation}")
            return 1
    if not args.write and not args.check:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
