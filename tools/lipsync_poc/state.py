"""Atomic, redacted state and report persistence for recoverable PoC jobs."""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .redaction import redact


STATE_VERSION = "1.0"
LOCK_STALE_SECONDS = 30


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    safe_payload = redact(payload)
    try:
        temporary.write_text(
            json.dumps(
                safe_payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_json(path):
    path = Path(path)
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid JSON object: {path.name}")
    return value


@contextmanager
def exclusive_lock(path, timeout=5.0, poll_interval=0.01):
    """Hold a small cross-process lock beside a state file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + float(timeout)
    descriptor = None
    while descriptor is None:
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(
                descriptor,
                f"{os.getpid()} {time.time():.6f}".encode("ascii"),
            )
        except FileExistsError:
            try:
                stale = (
                    time.time() - lock_path.stat().st_mtime
                    > LOCK_STALE_SECONDS
                )
            except FileNotFoundError:
                continue
            if stale:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"state lock timed out: {path.name}")
            time.sleep(float(poll_interval))
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
