#!/usr/bin/env python3
"""Dry-run or import IP Agent v4 JSON snapshots into PostgreSQL.

Dry-run is the default. --apply never overwrites a different snapshot already
stored for the same session; a conflict rolls back the entire import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from server.db import postgres


def canonical_checksum(snapshot: dict) -> str:
    encoded = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def scan_sessions(source_dir: Path) -> list[dict]:
    sessions = []
    for path in sorted(source_dir.glob("v4-*.json")):
        raw = path.read_bytes()
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict):
            raise ValueError(f"{path.name}: snapshot must be a JSON object")
        sid = path.name[3:-5]
        if not sid or len(sid) > 128:
            raise ValueError(f"{path.name}: invalid session id")
        owner = snapshot.get("owner") or {}
        if not isinstance(owner, dict):
            raise ValueError(f"{path.name}: owner must be an object")
        sessions.append({
            "session_id": sid,
            "owner_username": str(owner.get("username") or "").strip() or None,
            "owner_account_id": str(owner.get("account_id") or "").strip() or None,
            "legacy_owner_missing": not (
                str(owner.get("username") or "").strip()
                and str(owner.get("account_id") or "").strip()
            ),
            "snapshot": snapshot,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "snapshot_checksum": canonical_checksum(snapshot),
            "source_updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc),
            "message_count": len(snapshot.get("main") or []),
        })
    return sessions


def summary(sessions: list[dict]) -> dict:
    return {
        "sessions": len(sessions),
        "messages": sum(item["message_count"] for item in sessions),
        "owners_missing": sum(bool(item["legacy_owner_missing"]) for item in sessions),
        "source_checksum": hashlib.sha256(
            "".join(item["source_sha256"] for item in sessions).encode()
        ).hexdigest(),
    }


def apply_sessions(sessions: list[dict], source_dir: Path, code_sha: str) -> dict:
    if not sessions:
        raise RuntimeError("no v4 session snapshots found")
    if not code_sha or len(code_sha) < 7:
        raise RuntimeError("--code-sha is required for --apply")
    report = summary(sessions)
    run_id = "ip-agent-" + uuid.uuid4().hex
    with postgres.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ops.data_migration_runs
              (run_id, domain, source_kind, source_locator, source_fingerprint,
               code_sha, state, source_count, details)
            VALUES (%s, 'agent', 'json_snapshot', %s, %s, %s, 'running', %s, %s)
            """,
            (run_id, str(source_dir), report["source_checksum"], code_sha,
             report["sessions"], json.dumps(report)),
        )
        inserted = skipped = 0
        for item in sessions:
            row = conn.execute(
                """
                INSERT INTO agent.sessions
                  (session_id, owner_username, owner_account_id, legacy_owner_missing,
                   snapshot, source_sha256, snapshot_checksum, source_updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id) DO NOTHING
                RETURNING session_id
                """,
                (item["session_id"], item["owner_username"], item["owner_account_id"],
                 item["legacy_owner_missing"], json.dumps(item["snapshot"]),
                 item["source_sha256"], item["snapshot_checksum"],
                 item["source_updated_at"]),
            ).fetchone()
            if row:
                inserted += 1
            else:
                skipped += 1
            existing = conn.execute(
                "SELECT source_sha256, snapshot FROM agent.sessions WHERE session_id = %s",
                (item["session_id"],),
            ).fetchone()
            if not existing or existing["source_sha256"] != item["source_sha256"]:
                raise RuntimeError(
                    f"session conflict: {item['session_id']} already has a different snapshot"
                )
            target_checksum = canonical_checksum(existing["snapshot"])
            if target_checksum != item["snapshot_checksum"]:
                raise RuntimeError(f"session verification failed: {item['session_id']}")
            conn.execute(
                """
                INSERT INTO ops.data_migration_items
                  (run_id, source_table, chunk_key, source_count, target_count,
                   source_checksum, target_checksum, state)
                VALUES (%s, 'ip_agent_session_json', %s, 1, 1, %s, %s, 'verified')
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
    sessions = scan_sessions(args.source_dir)
    report = (
        apply_sessions(sessions, args.source_dir, args.code_sha)
        if args.apply else summary(sessions)
    )
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **report},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
