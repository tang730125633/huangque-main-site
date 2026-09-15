#!/usr/bin/env python3
"""Dry-run or import IP Agent profile/report JSON snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from server.db import postgres


def checksum(snapshot: dict) -> str:
    encoded = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def scan(source_dir: Path) -> list[dict]:
    items = []
    for path in sorted(source_dir.glob("*.json")):
        if path.name.startswith("v4-"):
            continue
        raw = path.read_bytes()
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict):
            raise ValueError(f"{path.name}: snapshot must be a JSON object")
        sid = path.stem
        if not sid or len(sid) > 128:
            raise ValueError(f"{path.name}: invalid session id")
        items.append({
            "session_id": sid,
            "snapshot": snapshot,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "snapshot_checksum": checksum(snapshot),
            "source_updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc),
        })
    return items


def summary(items: list[dict]) -> dict:
    return {
        "sessions": len(items),
        "profiles": sum(bool((item["snapshot"].get("profile") or {})) for item in items),
        "reports": sum(bool((item["snapshot"].get("report") or {})) for item in items),
        "source_checksum": hashlib.sha256(
            "".join(item["source_sha256"] for item in items).encode()
        ).hexdigest(),
    }


def apply(items: list[dict], source_dir: Path, code_sha: str) -> dict:
    if not items:
        raise RuntimeError("no auxiliary session snapshots found")
    if not code_sha or len(code_sha) < 7:
        raise RuntimeError("--code-sha is required for --apply")
    report = summary(items)
    run_id = "ip-agent-aux-" + uuid.uuid4().hex
    with postgres.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ops.data_migration_runs
              (run_id, domain, source_kind, source_locator, source_fingerprint,
               code_sha, state, source_count, details)
            VALUES (%s, 'agent_aux', 'json_snapshot', %s, %s, %s, 'running', %s, %s)
            """,
            (run_id, str(source_dir), report["source_checksum"], code_sha,
             report["sessions"], json.dumps(report)),
        )
        inserted = skipped = 0
        for item in items:
            row = conn.execute(
                """
                INSERT INTO agent.session_aux
                  (session_id, snapshot, source_sha256, snapshot_checksum, source_updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (session_id) DO NOTHING
                RETURNING session_id
                """,
                (item["session_id"], json.dumps(item["snapshot"], ensure_ascii=False),
                 item["source_sha256"], item["snapshot_checksum"], item["source_updated_at"]),
            ).fetchone()
            if row:
                inserted += 1
            else:
                skipped += 1
            existing = conn.execute(
                "SELECT source_sha256, snapshot FROM agent.session_aux WHERE session_id = %s",
                (item["session_id"],),
            ).fetchone()
            if not existing or existing["source_sha256"] != item["source_sha256"]:
                raise RuntimeError(
                    f"aux session conflict: {item['session_id']} has a different snapshot"
                )
            target_checksum = checksum(existing["snapshot"])
            if target_checksum != item["snapshot_checksum"]:
                raise RuntimeError(f"aux session verification failed: {item['session_id']}")
            conn.execute(
                """
                INSERT INTO ops.data_migration_items
                  (run_id, source_table, chunk_key, source_count, target_count,
                   source_checksum, target_checksum, state)
                VALUES (%s, 'ip_agent_aux_json', %s, 1, 1, %s, %s, 'verified')
                """,
                (run_id, item["session_id"], item["snapshot_checksum"], target_checksum),
            )
        conn.execute(
            """
            UPDATE ops.data_migration_runs
               SET state = 'verified', target_count = %s,
                   finished_at = CURRENT_TIMESTAMP,
                   details = details || %s::jsonb
             WHERE run_id = %s
            """,
            (inserted + skipped, json.dumps({"inserted": inserted, "skipped": skipped}), run_id),
        )
    return {**report, "run_id": run_id, "inserted": inserted, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--code-sha", default="")
    args = parser.parse_args()
    items = scan(args.source_dir)
    report = apply(items, args.source_dir, args.code_sha) if args.apply else summary(items)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **report},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
