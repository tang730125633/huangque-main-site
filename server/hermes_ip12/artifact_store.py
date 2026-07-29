"""Owner-scoped runtime artifact storage with atomic quota enforcement."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import uuid
from pathlib import Path

from runtime_paths import DATA_DIR


ASSET_ID_RE = re.compile(r"[0-9a-f]{10}\Z")
VIDEO_NAME_RE = re.compile(r"([0-9a-f]{10})\.mp4\Z")
DATA_QUOTA_BYTES = max(1, int(os.environ.get("HERMES_DATA_QUOTA_MB", "2048"))) * 1024 * 1024
_storage_lock = threading.RLock()


class StorageQuotaExceeded(OSError):
    pass


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


def directory_size(root=DATA_DIR):
    total = 0
    for path in Path(root).rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def ensure_capacity(extra_bytes, replacing=None):
    replacing_size = 0
    if replacing:
        try:
            replacing_size = Path(replacing).stat().st_size
        except OSError:
            pass
    if directory_size() - replacing_size + max(0, int(extra_bytes)) > DATA_QUOTA_BYTES:
        raise StorageQuotaExceeded("Hermes storage quota exceeded")


def atomic_write_bytes(destination, content):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with _storage_lock:
        ensure_capacity(len(content), replacing=destination)
        try:
            temp.write_bytes(content)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
    return destination


def finalize_file(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _storage_lock:
        source_inside_data = source.is_relative_to(DATA_DIR.resolve())
        ensure_capacity(0 if source_inside_data else source.stat().st_size, replacing=destination)
        os.replace(source, destination)
    return destination


def atomic_copy(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with _storage_lock:
        ensure_capacity(source.stat().st_size, replacing=destination)
        try:
            shutil.copy2(source, temp)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
    return destination
