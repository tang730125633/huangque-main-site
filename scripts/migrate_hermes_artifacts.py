#!/usr/bin/env python3
"""Migrate pre-isolation Hermes artifacts into one explicitly selected owner.

The migration is intentionally copy-only: legacy files remain in place until an
operator has verified the upgraded service. A manifest records every created
file and the media index before/after state so the operation can be rolled back.
Run this while the Hermes service is stopped.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


MIGRATION_ID = "hermes-owner-artifacts-v1"
MANIFEST_NAME = f"{MIGRATION_ID}.json"
LEGACY_ROLLBACK_DIRS = frozenset({"videos", "analyses", "uploads"})


class QuotaPreflightError(RuntimeError):
    pass


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _owner_key(username):
    return hashlib.sha256(str(username).encode("utf-8")).hexdigest()[:24]


def _safe_relative(path, root):
    path = Path(path).resolve()
    root = Path(root).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"legacy path escapes its storage root: {path}")
    return path.relative_to(root)


def _json_bytes(data):
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def _atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(content)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read_index(path):
    if not path.exists():
        return {"entries": {}, "keywords": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("media index must be a JSON object")
    if not isinstance(value.get("entries", {}), dict):
        raise ValueError("media index entries must be a JSON object")
    if not isinstance(value.get("keywords", {}), dict):
        raise ValueError("media index keywords must be a JSON object")
    value.setdefault("entries", {})
    value.setdefault("keywords", {})
    return value


def _legacy_index_entries(index_path):
    if not index_path.exists():
        return {}
    return _read_index(index_path)["entries"]


def _iter_files(root, excluded_roots=()):
    root = Path(root)
    if not root.exists():
        return
    excluded = [Path(item).resolve() for item in excluded_roots]
    for path in sorted(root.rglob("*")):
        resolved = path.resolve()
        if any(resolved == item or resolved.is_relative_to(item) for item in excluded):
            continue
        if path.is_file():
            yield path


def _operation(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination:
        return None
    checksum = _digest(source)
    existed = destination.exists()
    if existed and (not destination.is_file() or _digest(destination) != checksum):
        raise FileExistsError(f"migration destination conflict: {destination}")
    return {
        "source": str(source),
        "destination": str(destination),
        "sha256": checksum,
        "size_bytes": source.stat().st_size,
        "created": not existed,
    }


def _is_quota_path(path, data_dir):
    try:
        relative = Path(path).resolve().relative_to(Path(data_dir).resolve())
    except ValueError:
        return False
    parts = relative.parts
    if not parts:
        return False
    return parts[0] not in LEGACY_ROLLBACK_DIRS


def _quota_directory_size(data_dir):
    data_dir = Path(data_dir).resolve()
    roots = (
        [
            path for path in data_dir.iterdir()
            if path.name not in LEGACY_ROLLBACK_DIRS
        ]
        if data_dir.exists()
        else []
    )
    total = 0
    seen = set()
    for root in roots:
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            if not path.is_file():
                continue
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            total += stat.st_size
    return total


def _validate_plan_quota(plan):
    data_dir = Path(plan["data_dir"])
    quota = plan["quota"]
    current_bytes = _quota_directory_size(data_dir)
    remaining_bytes = sum(
        int(item["size_bytes"])
        for item in plan["operations"]
        if item["created"]
        and not Path(item["destination"]).exists()
        and _is_quota_path(item["destination"], data_dir)
    )
    index = plan["index"]
    index_path = Path(index["path"])
    current_index_bytes = index_path.stat().st_size if index_path.is_file() else 0
    final_index_bytes = len(base64.b64decode(index["after_base64"]))
    manifest_path = Path(plan["manifest_path"])
    current_manifest_bytes = (
        manifest_path.stat().st_size if manifest_path.is_file() else 0
    )
    projected_without_manifest = (
        current_bytes + remaining_bytes - current_index_bytes + final_index_bytes
        - current_manifest_bytes
    )
    manifest_bytes = 0
    for _attempt in range(10):
        projected_bytes = projected_without_manifest + manifest_bytes
        quota.update(
            current_bytes=current_bytes,
            remaining_copy_bytes=remaining_bytes,
            manifest_bytes=manifest_bytes,
            projected_bytes=projected_bytes,
        )
        completed = copy.deepcopy(plan)
        completed["state"] = "completed"
        completed["completed_at"] = "2000-01-01T00:00:00.000000+00:00"
        measured = len(_json_bytes(completed))
        if measured == manifest_bytes:
            break
        manifest_bytes = measured
    projected_bytes = projected_without_manifest + manifest_bytes
    quota.update(manifest_bytes=manifest_bytes, projected_bytes=projected_bytes)
    if projected_bytes > int(quota["limit_bytes"]):
        required_mb = (projected_bytes + 1024 * 1024 - 1) // (1024 * 1024)
        raise QuotaPreflightError(
            "migration would exceed Hermes artifact quota: "
            f"current={current_bytes} bytes, copies={remaining_bytes} bytes, "
            f"projected={projected_bytes} bytes, limit={quota['limit_bytes']} bytes; "
            f"set HERMES_DATA_QUOTA_MB to at least {required_mb} before retrying"
        )
    return plan


def build_plan(root_dir, data_dir, legacy_owner, quota_bytes=None):
    root_dir = Path(root_dir).resolve()
    data_dir = Path(data_dir).resolve()
    if not str(legacy_owner).strip():
        raise ValueError("legacy owner is required")

    owner = str(legacy_owner).strip()
    owner_id = _owner_key(owner)
    legacy_media = (root_dir / "media_library").resolve()
    media_root = (data_dir / "media_library").resolve()
    owner_media = (media_root / owner_id / "legacy").resolve()
    legacy_index_path = legacy_media / "index.json"
    target_index_path = media_root / "index.json"
    manifest_path = data_dir / ".migrations" / MANIFEST_NAME

    excluded_media = [owner_media]
    if target_index_path != legacy_index_path:
        excluded_media.append(target_index_path)

    operations = []
    media_destinations = {}
    for source in _iter_files(legacy_media, excluded_media):
        if source.resolve() == legacy_index_path.resolve():
            continue
        relative = _safe_relative(source, legacy_media)
        destination = owner_media / relative
        operation = _operation(source, destination)
        if operation:
            operations.append(operation)
        media_destinations[str(source.resolve())] = str(destination.resolve())

    directory_pairs = (
        (root_dir / "knowledge", data_dir / "knowledge"),
        (data_dir / "videos", data_dir / "users" / owner_id / "videos"),
        (data_dir / "analyses", data_dir / "users" / owner_id / "analyses"),
        (data_dir / "uploads", data_dir / "users" / owner_id / "uploads"),
    )
    excluded_runtime = [
        data_dir / "users",
        data_dir / ".migrations",
        data_dir / "media_library",
        data_dir / "knowledge",
    ]
    for source_root, destination_root in directory_pairs:
        source_root = source_root.resolve()
        destination_root = destination_root.resolve()
        exclusions = excluded_runtime if source_root == data_dir else [destination_root]
        for source in _iter_files(source_root, exclusions):
            relative = _safe_relative(source, source_root)
            operation = _operation(source, destination_root / relative)
            if operation:
                operations.append(operation)

    legacy_entries = _legacy_index_entries(legacy_index_path)
    target_index_existed = target_index_path.exists()
    target_index_before = (
        target_index_path.read_bytes() if target_index_existed else b""
    )
    merged = _read_index(target_index_path)

    indexed_sources = set()
    for old_id, old_entry in sorted(legacy_entries.items()):
        if not isinstance(old_entry, dict):
            continue
        raw_path = str(old_entry.get("file_path") or "")
        if not raw_path:
            continue
        source = Path(raw_path)
        if not source.is_absolute():
            source = legacy_media / source
        source = source.resolve()
        _safe_relative(source, legacy_media)
        destination = media_destinations.get(str(source))
        if not destination or not source.is_file():
            raise FileNotFoundError(f"legacy media index file is missing: {source}")
        indexed_sources.add(str(source))
        entry_id = f"{owner_id}_{hashlib.sha256(str(old_id).encode()).hexdigest()[:10]}"
        entry = dict(old_entry)
        entry.update(
            id=entry_id,
            owner_username=owner,
            file_path=destination,
            size_bytes=source.stat().st_size,
        )
        merged["entries"][entry_id] = entry

    for source_name, destination in sorted(media_destinations.items()):
        if source_name in indexed_sources:
            continue
        source = Path(source_name)
        relative = _safe_relative(source, legacy_media)
        fingerprint = hashlib.sha256(str(relative).encode("utf-8")).hexdigest()[:10]
        entry_id = f"{owner_id}_{fingerprint}"
        keyword = relative.parent.name if relative.parent != Path(".") else "legacy"
        merged["entries"][entry_id] = {
            "id": entry_id,
            "owner_username": owner,
            "keyword": keyword,
            "file_path": destination,
            "original_name": source.name,
            "source": "legacy-migration",
            "tags": [keyword, "legacy"],
            "size_bytes": source.stat().st_size,
            "format": source.suffix.lstrip("."),
            "added_at": datetime.now(timezone.utc).isoformat(),
            "fhash": f"{source.stat().st_size}_{source.name}",
            "use_count": 0,
        }

    keywords = {}
    for entry_id, entry in merged["entries"].items():
        keyword = str(entry.get("keyword") or "").strip()
        if keyword:
            keywords.setdefault(keyword, []).append(entry_id)
    merged["keywords"] = keywords
    target_index_after = _json_bytes(merged)

    if quota_bytes is None:
        quota_bytes = max(
            1, int(os.environ.get("HERMES_DATA_QUOTA_MB", "2048"))
        ) * 1024 * 1024
    plan = {
        "migration_id": MIGRATION_ID,
        "state": "prepared",
        "legacy_owner": owner,
        "owner_key": owner_id,
        "root_dir": str(root_dir),
        "data_dir": str(data_dir),
        "manifest_path": str(manifest_path),
        "operations": operations,
        "quota": {
            "limit_bytes": int(quota_bytes),
            "policy": "active-data-excluding-legacy-rollback-v2",
        },
        "index": {
            "path": str(target_index_path),
            "existed": target_index_existed,
            "before_base64": base64.b64encode(target_index_before).decode("ascii"),
            "before_sha256": hashlib.sha256(target_index_before).hexdigest(),
            "after_base64": base64.b64encode(target_index_after).decode("ascii"),
            "after_sha256": hashlib.sha256(target_index_after).hexdigest(),
        },
    }
    return _validate_plan_quota(plan)


def _load_manifest(data_dir):
    path = Path(data_dir).resolve() / ".migrations" / MANIFEST_NAME
    if not path.exists():
        raise FileNotFoundError(f"migration manifest not found: {path}")
    return path, json.loads(path.read_text(encoding="utf-8"))


def migrate(root_dir, data_dir, legacy_owner, dry_run=False, quota_bytes=None):
    data_dir = Path(data_dir).resolve()
    manifest_path = data_dir / ".migrations" / MANIFEST_NAME
    plan = None
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("legacy_owner") != str(legacy_owner).strip():
            raise ValueError("migration already belongs to a different legacy owner")
        if existing.get("state") == "completed":
            return existing
        if existing.get("state") == "prepared":
            plan = existing
        elif existing.get("state") == "rolled-back":
            raise ValueError("migration was rolled back; retain the manifest for audit")
        else:
            raise ValueError(f"unknown migration state: {existing.get('state')}")

    plan = plan or build_plan(
        root_dir, data_dir, legacy_owner, quota_bytes=quota_bytes
    )
    if quota_bytes is not None:
        plan["quota"]["limit_bytes"] = int(quota_bytes)
    _validate_plan_quota(plan)
    if dry_run:
        preview = dict(plan)
        preview["state"] = "dry-run"
        return preview

    if not manifest_path.exists():
        _atomic_write(manifest_path, _json_bytes(plan))
    for item in plan["operations"]:
        source = Path(item["source"])
        destination = Path(item["destination"])
        if destination.exists():
            if not destination.is_file() or _digest(destination) != item["sha256"]:
                raise FileExistsError(f"migration destination changed: {destination}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temp)
            if _digest(temp) != item["sha256"]:
                raise OSError(f"copied file checksum mismatch: {source}")
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)

    index = plan["index"]
    index_path = Path(index["path"])
    if index_path.exists():
        current_index_sha = _digest(index_path)
        if current_index_sha not in {
            index["before_sha256"],
            index["after_sha256"],
        }:
            raise RuntimeError(
                "media index changed during migration; stop Hermes and reconcile it"
            )
    elif index["existed"]:
        raise RuntimeError("media index disappeared during migration")
    _atomic_write(index["path"], base64.b64decode(index["after_base64"]))
    plan["state"] = "completed"
    plan["completed_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(manifest_path, _json_bytes(plan))
    return plan


def rollback(data_dir):
    manifest_path, manifest = _load_manifest(data_dir)
    if manifest.get("state") == "rolled-back":
        return manifest
    if manifest.get("state") not in {"prepared", "completed"}:
        raise ValueError(f"cannot roll back migration state: {manifest.get('state')}")

    index = manifest["index"]
    index_path = Path(index["path"])
    if index_path.exists():
        allowed_index_hashes = {index["after_sha256"]}
        if manifest.get("state") == "prepared":
            allowed_index_hashes.add(index["before_sha256"])
        if _digest(index_path) not in allowed_index_hashes:
            raise RuntimeError(
                "media index changed after migration; stop and reconcile it manually"
            )
    elif index["existed"]:
        raise RuntimeError("media index disappeared after migration")

    removable = []
    for item in reversed(manifest["operations"]):
        if not item["created"]:
            continue
        destination = Path(item["destination"])
        if not destination.exists():
            continue
        if not destination.is_file() or _digest(destination) != item["sha256"]:
            raise RuntimeError(f"migrated file changed; refusing to remove: {destination}")
        removable.append(destination)
    for destination in removable:
        destination.unlink()

    before = base64.b64decode(index["before_base64"])
    if index["existed"]:
        _atomic_write(index_path, before)
    else:
        index_path.unlink(missing_ok=True)

    data_root = Path(manifest["data_dir"]).resolve()
    for directory in sorted(
        (path for path in data_root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        if directory == manifest_path.parent:
            continue
        try:
            directory.rmdir()
        except OSError:
            pass

    manifest["state"] = "rolled-back"
    manifest["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(manifest_path, _json_bytes(manifest))
    return manifest


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root-dir",
        default=os.environ.get("HERMES_HOME", Path(__file__).parents[1] / "server" / "hermes_ip12"),
        help="legacy HERMES_HOME containing media_library/ and knowledge/",
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("HERMES_DATA_DIR", ""),
        help="current HERMES_DATA_DIR (required unless set in the environment)",
    )
    parser.add_argument(
        "--legacy-owner",
        help="existing account username that owns all pre-isolation artifacts",
    )
    parser.add_argument(
        "--quota-mb",
        type=int,
        default=max(1, int(os.environ.get("HERMES_DATA_QUOTA_MB", "2048"))),
        help="artifact quota used for the mandatory migration preflight",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args(argv)
    if not args.data_dir:
        parser.error("--data-dir or HERMES_DATA_DIR is required")
    if not args.rollback and not args.legacy_owner:
        parser.error("--legacy-owner is required for migration")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.rollback:
            result = rollback(args.data_dir)
        else:
            result = migrate(
                args.root_dir,
                args.data_dir,
                args.legacy_owner,
                dry_run=args.dry_run,
                quota_bytes=max(1, args.quota_mb) * 1024 * 1024,
            )
    except Exception as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    created = sum(1 for item in result.get("operations", []) if item.get("created"))
    print(
        json.dumps(
            {
                "migration_id": result["migration_id"],
                "state": result["state"],
                "legacy_owner": result["legacy_owner"],
                "created_files": created,
                "quota": result.get("quota"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
