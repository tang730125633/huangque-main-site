#!/usr/bin/env python3
"""Build and transactionally install the digital-human material mirror.

The test server remains the only authoring source.  Customer requests never
read it directly: a trusted operator builds an immutable release directory,
transports that directory through the approved private artifact channel, and
installs it into the production-local read-only mirror.
"""

import argparse
import contextlib
import gzip
import hashlib
import importlib
import json
import os
import pathlib
import shutil
import stat
import sys
import tarfile
import time
import uuid


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import digital_human_v2_release_preflight as preflight


MODULE_ROOT = preflight._module_root(ROOT)
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))


RELEASE_SCHEMA_VERSION = 1
RELEASE_MANIFEST_NAME = "manifest.json"
RELEASE_BUNDLE_NAME = "bundle.tar.gz"
RELEASE_DIRECTORY_PREFIX = "huangque-media-"
TRANSACTION_SCHEMA_VERSION = 1
MAX_RELEASE_MANIFEST_BYTES = 64 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024 * 1024
MAX_BUNDLE_MEMBERS = 1100
RELEASE_MANIFEST_KEYS = {
    "schema_version", "release_id", "source_host", "source_root",
    "mirror_root", "entry_count", "index_sha256", "bundle_name",
    "bundle_sha256", "file_count", "payload_bytes",
}
TRANSACTION_KEYS = {
    "schema_version", "phase", "target", "staging", "backup", "failed",
    "release_id",
}


class MirrorReleaseError(RuntimeError):
    pass


class InjectedMirrorFailure(MirrorReleaseError):
    pass


def _digital_human_domain():
    return importlib.import_module("content_domains.digital_human_v2")


def _absolute(value, label):
    path = pathlib.Path(str(value or ""))
    if not path.is_absolute():
        raise MirrorReleaseError("%s must be an absolute path" % label)
    normalized = pathlib.Path(os.path.abspath(str(path)))
    if normalized != path:
        raise MirrorReleaseError("%s must be normalized" % label)
    return normalized


def _is_relative_to(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _lstat_regular(path, maximum, label):
    path = pathlib.Path(path)
    try:
        before = os.lstat(str(path))
        if (not stat.S_ISREG(before.st_mode) or before.st_size <= 0
                or before.st_size > maximum):
            raise MirrorReleaseError("%s is not a bounded regular file" % label)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(path), flags)
        opened = os.fstat(descriptor)
        if ((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)):
            os.close(descriptor)
            raise MirrorReleaseError("%s changed while opening" % label)
        return descriptor, opened
    except MirrorReleaseError:
        raise
    except OSError as exc:
        raise MirrorReleaseError("%s is unavailable" % label) from exc


def _read_regular(path, maximum, label):
    descriptor, opened = _lstat_regular(path, maximum, label)
    chunks = []
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise MirrorReleaseError("%s exceeds its size limit" % label)
        after = os.fstat(descriptor)
        if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)):
            raise MirrorReleaseError("%s changed while reading" % label)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _hash_regular(path, maximum, label):
    descriptor, opened = _lstat_regular(path, maximum, label)
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise MirrorReleaseError("%s exceeds its size limit" % label)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)):
            raise MirrorReleaseError("%s changed while hashing" % label)
        return digest.hexdigest(), total
    finally:
        os.close(descriptor)


def _write_new(path, raw, mode=0o600):
    path = pathlib.Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags, mode)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise MirrorReleaseError("short write while creating release")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name == "posix":
        os.chmod(str(path), mode)


def _fsync_directory(path):
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace(source, destination):
    source = pathlib.Path(source)
    destination = pathlib.Path(destination)
    os.replace(str(source), str(destination))
    _fsync_directory(destination.parent)
    if source.parent != destination.parent:
        _fsync_directory(source.parent)


def _durable_unlink(path):
    path = pathlib.Path(path)
    path.unlink()
    _fsync_directory(path.parent)


def _canonical_json(value):
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ) + "\n").encode("utf-8")


@contextlib.contextmanager
def _library_environment(root):
    name = "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT"
    previous = os.environ.get(name)
    os.environ[name] = str(root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def _catalog_snapshot(source_root, expected_count):
    domain = _digital_human_domain()
    with _library_environment(source_root):
        root, records = domain._load_local_catalog(expected_count=expected_count)
        index_raw = preflight._read_locked_regular(
            pathlib.Path(root) / "index.jsonl", preflight.MAX_INDEX_BYTES,
        )

    def material_payloads():
        with _library_environment(source_root):
            for record in sorted(records, key=lambda item: item["relative"]):
                raw, _actual_mime = domain._read_local_record(root, record)
                try:
                    yield record["relative"], raw
                finally:
                    del raw

    return index_raw, material_payloads()


def _all_snapshot_files(root):
    root = pathlib.Path(root)
    actual = set()
    for current, directory_names, file_names in os.walk(str(root), followlinks=False):
        current_path = pathlib.Path(current)
        for name in list(directory_names):
            path = current_path / name
            info = os.lstat(str(path))
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise MirrorReleaseError("snapshot contains an unsafe directory")
        for name in file_names:
            path = current_path / name
            info = os.lstat(str(path))
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise MirrorReleaseError("snapshot contains a non-regular file")
            actual.add(path.relative_to(root).as_posix())
    return actual


def verify_snapshot(root, expected_count=None):
    expected_count = int(
        preflight.EXPECTED_LIBRARY_COUNT if expected_count is None else expected_count
    )
    root = _absolute(root, "snapshot root")
    mirror = preflight._verify_material_mirror_provenance(root)
    domain = _digital_human_domain()
    with _library_environment(root):
        result = domain.local_material_library_operational_probe(
            expected_count=expected_count, verify_all=True,
        )
        _catalog_root, records = domain._load_local_catalog(
            expected_count=expected_count,
        )
    expected = {
        "index.jsonl", preflight.MIRROR_PROVENANCE_NAME,
    }
    expected.update(record["relative"] for record in records)
    if _all_snapshot_files(root) != expected:
        raise MirrorReleaseError("snapshot contains missing or unindexed files")
    if int(result.get("verified_files") or 0) != expected_count:
        raise MirrorReleaseError("snapshot did not verify every indexed file")
    return {
        "count": expected_count,
        "index_sha256": mirror["index_sha256"],
        "source_host": mirror["source_host"],
    }


def _freeze_snapshot_permissions(root):
    if os.name != "posix":
        return
    root = pathlib.Path(root)
    for current, directory_names, file_names in os.walk(str(root), topdown=False):
        current_path = pathlib.Path(current)
        for name in file_names:
            os.chmod(str(current_path / name), 0o444)
        for name in directory_names:
            os.chmod(str(current_path / name), 0o555)
        os.chmod(str(current_path), 0o555)


def _safe_remove_generated(path, parent, prefix):
    path = pathlib.Path(path)
    parent = pathlib.Path(parent)
    if (path.parent != parent or not path.name.startswith(prefix)
            or not _is_relative_to(path, parent)):
        raise MirrorReleaseError("refusing to remove an unowned release path")
    if path.exists():
        for current, directory_names, file_names in os.walk(
                str(path), topdown=False, followlinks=False):
            current_path = pathlib.Path(current)
            for name in file_names:
                os.chmod(str(current_path / name), 0o600)
            for name in directory_names:
                os.chmod(str(current_path / name), 0o700)
            os.chmod(str(current_path), 0o700)
        shutil.rmtree(str(path))


def _write_deterministic_bundle(snapshot_root, destination):
    snapshot_root = pathlib.Path(snapshot_root)
    destination = pathlib.Path(destination)
    with destination.open("xb") as raw_output:
        with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_output, mtime=0) as compressed:
            with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                paths = sorted(
                    snapshot_root.rglob("*"), key=lambda item: item.relative_to(
                        snapshot_root).as_posix(),
                )
                for path in paths:
                    info = os.lstat(str(path))
                    if stat.S_ISLNK(info.st_mode):
                        raise MirrorReleaseError("snapshot contains a symbolic link")
                    relative = path.relative_to(snapshot_root).as_posix()
                    member = tarfile.TarInfo(relative)
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    member.mtime = 0
                    if stat.S_ISDIR(info.st_mode):
                        member.type = tarfile.DIRTYPE
                        member.mode = 0o555
                        archive.addfile(member)
                    elif stat.S_ISREG(info.st_mode):
                        member.size = info.st_size
                        member.mode = 0o444
                        with path.open("rb") as source:
                            archive.addfile(member, source)
                    else:
                        raise MirrorReleaseError("snapshot contains a special file")


def build_release(source_root, output_root, source_host=None, source_root_contract=None,
                  mirror_root=None, expected_count=None):
    source_root = _absolute(source_root, "source root")
    output_root = _absolute(output_root, "output root")
    source_host = str(source_host or preflight.EXPECTED_LIBRARY_PRIMARY_HOST)
    source_root_contract = _absolute(
        source_root_contract or preflight.EXPECTED_LIBRARY_PRIMARY_ROOT,
        "source root contract",
    )
    mirror_root = _absolute(
        mirror_root or preflight.EXPECTED_LIBRARY_ROOT, "mirror root contract",
    )
    expected_count = int(
        preflight.EXPECTED_LIBRARY_COUNT if expected_count is None else expected_count
    )
    if source_root != source_root_contract:
        raise MirrorReleaseError("source root does not match the locked contract")
    if source_host != preflight.EXPECTED_LIBRARY_PRIMARY_HOST:
        raise MirrorReleaseError("source host does not match the locked contract")
    if expected_count != preflight.EXPECTED_LIBRARY_COUNT:
        raise MirrorReleaseError("entry count does not match the locked contract")
    output_root.mkdir(parents=True, exist_ok=True)
    if os.path.islink(str(output_root)) or not output_root.is_dir():
        raise MirrorReleaseError("output root is not a safe directory")

    temporary = output_root / (".huangque-media-build-" + uuid.uuid4().hex)
    snapshot = temporary / "snapshot"
    release_temporary = output_root / (".huangque-media-release-" + uuid.uuid4().hex)
    temporary.mkdir(mode=0o700)
    snapshot.mkdir(mode=0o700)
    try:
        index_raw, files = _catalog_snapshot(source_root, expected_count)
        index_sha256 = hashlib.sha256(index_raw).hexdigest()
        release_id = RELEASE_DIRECTORY_PREFIX + index_sha256[:16]
        release_final = output_root / release_id
        if release_final.exists():
            raise MirrorReleaseError("immutable release already exists")

        _write_new(snapshot / "index.jsonl", index_raw, 0o444)
        payload_bytes = len(index_raw)
        for relative, raw in files:
            destination = snapshot.joinpath(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            size = len(raw)
            try:
                _write_new(destination, raw, 0o444)
            finally:
                del raw
            payload_bytes += size
        provenance = {
            "schema_version": 1,
            "source_host": source_host,
            "source_root": str(source_root_contract),
            "mirror_root": str(mirror_root),
            "entry_count": expected_count,
            "index_sha256": index_sha256,
        }
        provenance_raw = _canonical_json(provenance)
        _write_new(
            snapshot / preflight.MIRROR_PROVENANCE_NAME, provenance_raw, 0o444,
        )
        payload_bytes += len(provenance_raw)
        _freeze_snapshot_permissions(snapshot)
        verify_snapshot(snapshot, expected_count=expected_count)

        release_temporary.mkdir(mode=0o700)
        bundle = release_temporary / RELEASE_BUNDLE_NAME
        _write_deterministic_bundle(snapshot, bundle)
        bundle_sha256, _bundle_bytes = _hash_regular(
            bundle, MAX_BUNDLE_BYTES, "release bundle",
        )
        manifest = {
            "schema_version": RELEASE_SCHEMA_VERSION,
            "release_id": release_id,
            "source_host": source_host,
            "source_root": str(source_root_contract),
            "mirror_root": str(mirror_root),
            "entry_count": expected_count,
            "index_sha256": index_sha256,
            "bundle_name": RELEASE_BUNDLE_NAME,
            "bundle_sha256": bundle_sha256,
            "file_count": expected_count + 2,
            "payload_bytes": payload_bytes,
        }
        manifest_raw = _canonical_json(manifest)
        manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
        _write_new(
            release_temporary / RELEASE_MANIFEST_NAME, manifest_raw, 0o444,
        )
        if os.name == "posix":
            os.chmod(str(bundle), 0o444)
            os.chmod(str(release_temporary), 0o555)
        _durable_replace(release_temporary, release_final)
        return dict(
            manifest,
            manifest_sha256=manifest_sha256,
            release_directory=str(release_final),
        )
    finally:
        _safe_remove_generated(temporary, output_root, ".huangque-media-build-")
        if release_temporary.exists():
            _safe_remove_generated(
                release_temporary, output_root, ".huangque-media-release-",
            )


def _load_release(release_directory, expected_source_host=None,
                  expected_source_root=None, expected_mirror_root=None,
                  expected_count=None, expected_manifest_sha256=None):
    release_directory = _absolute(release_directory, "release directory")
    info = os.lstat(str(release_directory))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise MirrorReleaseError("release path is not a regular directory")
    names = {item.name for item in release_directory.iterdir()}
    if names != {RELEASE_MANIFEST_NAME, RELEASE_BUNDLE_NAME}:
        raise MirrorReleaseError("release directory contains unexpected files")
    manifest_raw = _read_regular(
        release_directory / RELEASE_MANIFEST_NAME,
        MAX_RELEASE_MANIFEST_BYTES, "release manifest",
    )
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    if expected_manifest_sha256 is not None:
        expected_manifest_sha256 = str(expected_manifest_sha256)
        if (len(expected_manifest_sha256) != 64
                or any(character not in "0123456789abcdef"
                       for character in expected_manifest_sha256)
                or manifest_sha256 != expected_manifest_sha256):
            raise MirrorReleaseError(
                "release manifest digest does not match the approved value"
            )
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MirrorReleaseError("release manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != RELEASE_MANIFEST_KEYS:
        raise MirrorReleaseError("release manifest contract is invalid")
    if manifest_raw != _canonical_json(manifest):
        raise MirrorReleaseError("release manifest is not canonical")
    expected_source_host = str(
        expected_source_host or preflight.EXPECTED_LIBRARY_PRIMARY_HOST
    )
    expected_source_root = str(_absolute(
        expected_source_root or preflight.EXPECTED_LIBRARY_PRIMARY_ROOT,
        "expected source root",
    ))
    expected_mirror_root = str(_absolute(
        expected_mirror_root or preflight.EXPECTED_LIBRARY_ROOT,
        "expected mirror root",
    ))
    expected_count = int(
        preflight.EXPECTED_LIBRARY_COUNT if expected_count is None else expected_count
    )
    index_sha256 = str(manifest.get("index_sha256") or "")
    bundle_sha256 = str(manifest.get("bundle_sha256") or "")
    release_id = str(manifest.get("release_id") or "")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != RELEASE_SCHEMA_VERSION
        or manifest.get("source_host") != expected_source_host
        or manifest.get("source_root") != expected_source_root
        or manifest.get("mirror_root") != expected_mirror_root
        or type(manifest.get("entry_count")) is not int
        or manifest.get("entry_count") != expected_count
        or manifest.get("bundle_name") != RELEASE_BUNDLE_NAME
        or type(manifest.get("file_count")) is not int
        or manifest.get("file_count") != expected_count + 2
        or type(manifest.get("payload_bytes")) is not int
        or int(manifest["payload_bytes"]) <= 0
        or int(manifest["payload_bytes"]) > MAX_BUNDLE_BYTES
        or len(index_sha256) != 64
        or any(character not in "0123456789abcdef" for character in index_sha256)
        or len(bundle_sha256) != 64
        or any(character not in "0123456789abcdef" for character in bundle_sha256)
        or release_id != RELEASE_DIRECTORY_PREFIX + index_sha256[:16]
        or release_directory.name != release_id
    ):
        raise MirrorReleaseError("release manifest is not locked to this environment")
    bundle = release_directory / RELEASE_BUNDLE_NAME
    observed_sha256, bundle_bytes = _hash_regular(
        bundle, MAX_BUNDLE_BYTES, "release bundle",
    )
    if observed_sha256 != bundle_sha256:
        raise MirrorReleaseError("release bundle digest does not match its manifest")
    return manifest, bundle, bundle_bytes


def _safe_member_name(value):
    value = str(value or "")
    if not value or "\\" in value or value.startswith("/"):
        raise MirrorReleaseError("release bundle contains an unsafe path")
    path = pathlib.PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise MirrorReleaseError("release bundle contains an unsafe path")
    if value not in {"index.jsonl", preflight.MIRROR_PROVENANCE_NAME} \
            and path.parts[0] != "files":
        raise MirrorReleaseError("release bundle contains an unexpected path")
    return path


def _extract_bundle(bundle, destination):
    destination = pathlib.Path(destination)
    destination.mkdir(mode=0o700)
    seen = set()
    total = 0
    with tarfile.open(str(bundle), mode="r:*") as archive:
        members = archive.getmembers()
        if len(members) > MAX_BUNDLE_MEMBERS:
            raise MirrorReleaseError("release bundle contains too many members")
        for member in members:
            relative = _safe_member_name(member.name)
            normalized = relative.as_posix()
            if normalized in seen:
                raise MirrorReleaseError("release bundle contains duplicate paths")
            seen.add(normalized)
            target = destination.joinpath(*relative.parts)
            if not _is_relative_to(target, destination):
                raise MirrorReleaseError("release bundle escaped its staging root")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isreg() or member.size <= 0:
                raise MirrorReleaseError("release bundle contains a special file")
            maximum = (
                preflight.MAX_INDEX_BYTES if normalized == "index.jsonl" else
                preflight.MAX_PROVENANCE_BYTES
                if normalized == preflight.MIRROR_PROVENANCE_NAME else
                _digital_human_domain()._LOCAL_MATERIAL_MAX_BYTES
            )
            if member.size > maximum or total + member.size > MAX_BUNDLE_BYTES:
                raise MirrorReleaseError("release bundle member exceeds its size limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise MirrorReleaseError("release bundle member could not be read")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(str(target), flags, 0o600)
            remaining = member.size
            try:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise MirrorReleaseError("release bundle member was truncated")
                    view = memoryview(chunk)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise MirrorReleaseError("short write while extracting release")
                        view = view[written:]
                    remaining -= len(chunk)
                if source.read(1):
                    raise MirrorReleaseError("release bundle member exceeded its header")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
                source.close()
            total += member.size
    _freeze_snapshot_permissions(destination)


def _transaction_paths(target, release_id):
    parent = target.parent
    nonce = "%d-%s" % (int(time.time()), uuid.uuid4().hex[:8])
    return {
        "staging": parent / (".%s.staging.%s.%s" % (target.name, release_id, nonce)),
        "backup": parent / ("%s.backup.%s" % (target.name, nonce)),
        "failed": parent / (".%s.failed.%s.%s" % (target.name, release_id, nonce)),
        "journal": parent / (".%s.transaction.json" % target.name),
        "lock": parent / (".%s.release.lock" % target.name),
    }


def _write_journal(path, state):
    if set(state) != TRANSACTION_KEYS:
        raise MirrorReleaseError("transaction journal contract is invalid")
    temporary = path.parent / (path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        _write_new(temporary, _canonical_json(state), 0o600)
        _durable_replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_journal(path, target):
    raw = _read_regular(path, MAX_RELEASE_MANIFEST_BYTES, "transaction journal")
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MirrorReleaseError("transaction journal is invalid") from exc
    if (not isinstance(state, dict) or set(state) != TRANSACTION_KEYS
            or state.get("schema_version") != TRANSACTION_SCHEMA_VERSION
            or state.get("target") != str(target)
            or state.get("phase") not in {"prepared", "old_moved", "new_active"}):
        raise MirrorReleaseError("transaction journal is not recoverable")
    parent = target.parent
    prefixes = {
        "staging": ".%s.staging." % target.name,
        "backup": "%s.backup." % target.name,
        "failed": ".%s.failed." % target.name,
    }
    for key in ("staging", "backup", "failed"):
        if not state.get(key):
            if key == "backup":
                continue
            raise MirrorReleaseError("transaction journal path is missing")
        path = _absolute(state[key], "transaction " + key)
        if path.parent != parent or not path.name.startswith(prefixes[key]):
            raise MirrorReleaseError("transaction journal path escaped the mirror parent")
    return state


def _move_aside(path, destination):
    path = pathlib.Path(path)
    destination = pathlib.Path(destination)
    if path.exists():
        if destination.exists():
            raise MirrorReleaseError("transaction evidence path already exists")
        _durable_replace(path, destination)


def _rollback_state(state, target, journal):
    target = pathlib.Path(target)
    staging = pathlib.Path(state["staging"])
    failed = pathlib.Path(state["failed"])
    backup = pathlib.Path(state["backup"]) if state.get("backup") else None
    if backup is not None and backup.exists():
        if target.exists():
            _move_aside(target, failed)
        _durable_replace(backup, target)
    elif target.exists() and state.get("phase") == "new_active":
        _move_aside(target, failed)
    if staging.exists():
        staging_failed = pathlib.Path(str(failed) + ".staging")
        _move_aside(staging, staging_failed)
    if journal.exists():
        _durable_unlink(journal)


def _recover_incomplete_unlocked(target):
    journal = target.parent / (".%s.transaction.json" % target.name)
    if not journal.exists():
        return {"recovered": False}
    state = _read_journal(journal, target)
    _rollback_state(state, target, journal)
    return {"recovered": True, "release_id": state["release_id"]}


def recover_incomplete(target_root):
    target = _absolute(target_root, "target root")
    lock = target.parent / (".%s.release.lock" % target.name)
    with _release_lock(lock):
        return _recover_incomplete_unlocked(target)


@contextlib.contextmanager
def _release_lock(path):
    if os.name == "posix":
        import fcntl
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(path), flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise MirrorReleaseError("material mirror release lock is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MirrorReleaseError(
                    "another material mirror release is active"
                ) from exc
            yield
        finally:
            os.close(descriptor)
        return
    try:
        os.mkdir(str(path), 0o700)
    except FileExistsError as exc:
        raise MirrorReleaseError("another material mirror release is active") from exc
    try:
        yield
    finally:
        try:
            os.rmdir(str(path))
        except FileNotFoundError:
            pass


def _inject(fault_after, phase):
    if fault_after == phase:
        raise InjectedMirrorFailure("injected failure after %s" % phase)


def install_release(release_directory, target_root, runtime_probe=None,
                    expected_source_host=None, expected_source_root=None,
                    expected_mirror_root=None, expected_count=None,
                    expected_manifest_sha256=None, fault_after=None):
    target = _absolute(target_root, "target root")
    expected_mirror = _absolute(
        expected_mirror_root or preflight.EXPECTED_LIBRARY_ROOT,
        "expected mirror root",
    )
    if target != expected_mirror:
        raise MirrorReleaseError("target root does not match the locked mirror root")
    parent_info = os.lstat(str(target.parent))
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise MirrorReleaseError("mirror parent is not a safe directory")
    manifest, bundle, _bundle_bytes = _load_release(
        release_directory,
        expected_source_host=expected_source_host,
        expected_source_root=expected_source_root,
        expected_mirror_root=expected_mirror,
        expected_count=expected_count,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    expected_count = int(manifest["entry_count"])
    paths = _transaction_paths(target, manifest["release_id"])
    with _release_lock(paths["lock"]):
        _recover_incomplete_unlocked(target)
        if paths["staging"].exists() or paths["backup"].exists() \
                or paths["failed"].exists():
            raise MirrorReleaseError("transaction paths already exist")
        required_bytes = int(manifest["payload_bytes"]) + 64 * 1024 * 1024
        if shutil.disk_usage(str(target.parent)).free < required_bytes:
            raise MirrorReleaseError("mirror parent has insufficient staging space")
        try:
            _extract_bundle(bundle, paths["staging"])
            verified = verify_snapshot(
                paths["staging"], expected_count=expected_count,
            )
            if verified["index_sha256"] != manifest["index_sha256"]:
                raise MirrorReleaseError(
                    "staged index does not match the release manifest"
                )
            state = {
                "schema_version": TRANSACTION_SCHEMA_VERSION,
                "phase": "prepared",
                "target": str(target),
                "staging": str(paths["staging"]),
                "backup": str(paths["backup"]) if target.exists() else "",
                "failed": str(paths["failed"]),
                "release_id": manifest["release_id"],
            }
            _write_journal(paths["journal"], state)
            try:
                _inject(fault_after, "prepared")
                if target.exists():
                    _durable_replace(target, paths["backup"])
                    state["phase"] = "old_moved"
                    _write_journal(paths["journal"], state)
                    _inject(fault_after, "old_moved")
                _durable_replace(paths["staging"], target)
                state["phase"] = "new_active"
                _write_journal(paths["journal"], state)
                _inject(fault_after, "new_active")
                verified = verify_snapshot(target, expected_count=expected_count)
                if verified["index_sha256"] != manifest["index_sha256"]:
                    raise MirrorReleaseError(
                        "active mirror does not match the release manifest"
                    )
                if runtime_probe is not None:
                    with _library_environment(target):
                        runtime_probe(target)
                _inject(fault_after, "runtime_preflight")
                _durable_unlink(paths["journal"])
            except BaseException:
                _rollback_state(state, target, paths["journal"])
                raise
        except BaseException:
            if paths["staging"].exists():
                _move_aside(paths["staging"], paths["failed"])
            raise
    return {
        "ok": True,
        "release_id": manifest["release_id"],
        "index_sha256": manifest["index_sha256"],
        "target": str(target),
        "backup": state["backup"],
    }


def _runtime_probe(_target):
    result = preflight.run()
    if not isinstance(result, dict) or not result.get("ok"):
        raise MirrorReleaseError("production runtime preflight failed")


def _service_user_is_ubuntu():
    if os.name != "posix":
        return False
    import pwd
    return pwd.getpwuid(os.geteuid()).pw_name == "ubuntu"


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build an immutable source snapshot")
    build.add_argument("--source-root", required=True)
    build.add_argument("--output-root", required=True)
    install = commands.add_parser("install", help="install and fully preflight a release")
    install.add_argument("--release-directory", required=True)
    install.add_argument("--manifest-sha256", required=True)
    install.add_argument("--target-root", default=preflight.EXPECTED_LIBRARY_ROOT)
    recover = commands.add_parser("recover", help="recover an interrupted install")
    recover.add_argument("--target-root", default=preflight.EXPECTED_LIBRARY_ROOT)
    verify = commands.add_parser("verify", help="verify an immutable release")
    verify.add_argument("--release-directory", required=True)
    verify.add_argument("--manifest-sha256", required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_release(args.source_root, args.output_root)
        elif args.command == "verify":
            manifest, _bundle, bundle_bytes = _load_release(
                args.release_directory,
                expected_manifest_sha256=args.manifest_sha256,
            )
            result = dict(
                manifest,
                ok=True,
                manifest_sha256=args.manifest_sha256,
                bundle_bytes=bundle_bytes,
            )
        elif args.command == "recover":
            if not _service_user_is_ubuntu():
                raise MirrorReleaseError("recover must run as the ubuntu service user")
            result = recover_incomplete(args.target_root)
        else:
            if not _service_user_is_ubuntu():
                raise MirrorReleaseError("install must run as the ubuntu service user")
            result = install_release(
                args.release_directory, args.target_root,
                runtime_probe=_runtime_probe,
                expected_manifest_sha256=args.manifest_sha256,
            )
    except Exception as exc:
        print(json.dumps({
            "ok": False, "error": type(exc).__name__,
        }, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
