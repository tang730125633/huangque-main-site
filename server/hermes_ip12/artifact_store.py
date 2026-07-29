"""Owner-scoped runtime artifact storage with atomic quota enforcement."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from runtime_paths import DATA_DIR


ASSET_ID_RE = re.compile(r"[0-9a-f]{10}\Z")
LEGACY_ROLLBACK_DIRS = frozenset({"videos", "analyses", "uploads"})
# New files use ``<id>.mp4``. The two prefixed forms were emitted by the
# pre-isolation analyzer and replica pipelines and remain valid after migration.
VIDEO_NAME_RE = re.compile(r"(?:ref_|replica_)?([0-9a-f]{10})\.mp4\Z")
DATA_QUOTA_BYTES = max(1, int(os.environ.get("HERMES_DATA_QUOTA_MB", "2048"))) * 1024 * 1024
RESERVATION_TTL_SECONDS = max(
    60, int(os.environ.get("HERMES_RESERVATION_TTL_SECONDS", "3600"))
)
LOCK_FILE = DATA_DIR / ".artifact-store.lock"
RESERVATIONS_FILE = DATA_DIR / ".quota-reservations.json"
_storage_lock = threading.RLock()
_transaction_state = threading.local()


class StorageQuotaExceeded(OSError):
    pass


def _lock_file(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def storage_transaction():
    """Serialize storage read-modify-write operations across threads/processes."""
    with _storage_lock:
        depth = getattr(_transaction_state, "depth", 0)
        if depth:
            _transaction_state.depth = depth + 1
            try:
                yield
            finally:
                _transaction_state.depth -= 1
            return

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOCK_FILE, "a+b") as handle:
            _lock_file(handle)
            _transaction_state.depth = 1
            try:
                yield
            finally:
                _transaction_state.depth = 0
                _unlock_file(handle)


def _load_reservations():
    try:
        data = json.loads(RESERVATIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    cutoff = time.time() - RESERVATION_TTL_SECONDS
    return {
        key: value for key, value in data.items()
        if float(value.get("created_at", 0)) >= cutoff
    }


def _save_reservations(data):
    RESERVATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = RESERVATIONS_FILE.with_name(
        f".{RESERVATIONS_FILE.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(temp, RESERVATIONS_FILE)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def reserve_capacity(byte_count):
    """Persist a quota reservation so concurrent workers cannot overcommit."""
    byte_count = max(0, int(byte_count))
    token = uuid.uuid4().hex
    with storage_transaction():
        reservations = _load_reservations()
        reserved = sum(max(0, int(item.get("bytes", 0))) for item in reservations.values())
        if directory_size() + reserved + byte_count > DATA_QUOTA_BYTES:
            raise StorageQuotaExceeded("Hermes storage quota exceeded")
        reservations[token] = {"bytes": byte_count, "created_at": time.time()}
        _save_reservations(reservations)
    try:
        yield token
    finally:
        with storage_transaction():
            reservations = _load_reservations()
            reservations.pop(token, None)
            _save_reservations(reservations)


def owner_key(username):
    return hashlib.sha256(str(username).encode("utf-8")).hexdigest()[:24]


def new_asset_id():
    return uuid.uuid4().hex[:10]


def user_root(username, create=True):
    root = (DATA_DIR / "users" / owner_key(username)).resolve()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def user_dir(username, kind, create=True):
    if kind not in {"videos", "analyses", "uploads", "media"}:
        raise ValueError("invalid artifact kind")
    path = (user_root(username, create=create) / kind).resolve()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def video_work_dir(username, asset_id=None):
    asset_id = asset_id or new_asset_id()
    if not ASSET_ID_RE.fullmatch(asset_id):
        raise ValueError("invalid asset id")
    path = (user_dir(username, "videos") / ".work" / asset_id).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return asset_id, path


def video_path(username, filename):
    match = VIDEO_NAME_RE.fullmatch(str(filename))
    if not match:
        raise FileNotFoundError("video not found")
    return (user_dir(username, "videos", create=False) / filename).resolve()


def analysis_dir(username, analysis_id=None, create=True):
    analysis_id = analysis_id or new_asset_id()
    if not ASSET_ID_RE.fullmatch(analysis_id):
        raise FileNotFoundError("analysis not found")
    path = (user_dir(username, "analyses", create=create) / analysis_id).resolve()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return analysis_id, path


def upload_path(username, asset_id, extension):
    if not ASSET_ID_RE.fullmatch(asset_id):
        raise ValueError("invalid asset id")
    extension = str(extension).lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
        raise ValueError("invalid extension")
    return (user_dir(username, "uploads") / f"{asset_id}{extension}").resolve()


def find_upload(username, asset_id):
    if not ASSET_ID_RE.fullmatch(str(asset_id)):
        raise FileNotFoundError("upload not found")
    matches = [
        path for path in user_dir(username, "uploads", create=False).glob(f"{asset_id}.*")
        if path.is_file()
    ]
    if len(matches) != 1:
        raise FileNotFoundError("upload not found")
    return matches[0].resolve()


def media_path(username, asset_id, extension):
    if not ASSET_ID_RE.fullmatch(asset_id):
        raise ValueError("invalid asset id")
    extension = str(extension).lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
        raise ValueError("invalid extension")
    return (user_dir(username, "media") / f"{asset_id}{extension}").resolve()


def _quota_paths():
    """Return every active data path except retained top-level rollback copies."""
    if not DATA_DIR.exists():
        return []
    return [
        path for path in DATA_DIR.iterdir()
        if path.name not in LEGACY_ROLLBACK_DIRS
    ]


def _is_quota_path(path):
    try:
        relative = Path(path).resolve().relative_to(DATA_DIR.resolve())
    except ValueError:
        return False
    parts = relative.parts
    if not parts:
        return False
    return parts[0] not in LEGACY_ROLLBACK_DIRS


def directory_size(root=None):
    """Count active data, or every file below an explicit test root."""
    roots = [Path(root)] if root is not None else _quota_paths()
    total = 0
    seen = set()
    for root_path in roots:
        candidates = [root_path] if root_path.is_file() else root_path.rglob("*")
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino)
                if identity in seen:
                    continue
                seen.add(identity)
                total += stat.st_size
            except OSError:
                continue
    return total


def ensure_capacity(extra_bytes, replacing=None, reservation=None):
    replacing_size = 0
    if replacing and _is_quota_path(replacing):
        try:
            replacing_size = Path(replacing).stat().st_size
        except OSError:
            pass
    reservations = _load_reservations()
    own_reserved = 0
    if reservation in reservations:
        own_reserved = max(0, int(reservations[reservation].get("bytes", 0)))
    reserved = sum(max(0, int(item.get("bytes", 0))) for item in reservations.values())
    projected = (
        directory_size() - replacing_size + reserved - own_reserved
        + max(0, int(extra_bytes))
    )
    if projected > DATA_QUOTA_BYTES:
        raise StorageQuotaExceeded("Hermes storage quota exceeded")


def atomic_write_bytes(destination, content, reservation=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with storage_transaction():
        ensure_capacity(len(content), replacing=destination, reservation=reservation)
        try:
            temp.write_bytes(content)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
    return destination


def atomic_append_bytes(destination, content, reservation=None):
    """Append bytes without bypassing quota or exposing a partial JSONL write."""
    destination = Path(destination)
    with storage_transaction():
        existing = destination.read_bytes() if destination.is_file() else b""
        return atomic_write_bytes(
            destination, existing + bytes(content), reservation=reservation
        )


def finalize_file(source, destination, reservation=None):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with storage_transaction():
        source_size = source.stat().st_size
        source_inside_quota = _is_quota_path(source)
        ensure_capacity(
            0 if source_inside_quota else source_size,
            replacing=destination,
            reservation=reservation,
        )
        try:
            os.replace(source, destination)
        except OSError as exc:
            if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
                raise

            # Cross-filesystem moves cannot use rename. Copy into the target
            # directory, then atomically publish there. The stricter capacity
            # check accounts for the temporary peak while source, old target,
            # and copied temporary file may coexist.
            temp = destination.with_name(
                f".{destination.name}.{uuid.uuid4().hex}.tmp"
            )
            ensure_capacity(
                source_size,
                reservation=reservation,
            )
            try:
                shutil.copy2(source, temp)
                if temp.stat().st_size != source_size:
                    raise OSError(errno.EIO, "incomplete cross-filesystem copy")
                os.replace(temp, destination)
                try:
                    source.unlink()
                except OSError:
                    # The destination is already committed. Callers that stage
                    # in a temporary directory will clean up the duplicate.
                    pass
            finally:
                temp.unlink(missing_ok=True)
    return destination


def atomic_copy(source, destination, reservation=None):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with storage_transaction():
        ensure_capacity(
            source.stat().st_size, replacing=destination, reservation=reservation
        )
        try:
            shutil.copy2(source, temp)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
    return destination
