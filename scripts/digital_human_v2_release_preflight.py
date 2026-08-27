#!/usr/bin/env python3
"""Fail-closed production preflight for the digital-human v2 runtime."""

import hashlib
import json
import os
import pathlib
import stat
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))


EXPECTED_LIBRARY_ROOT = "/home/ubuntu/material-libraries/huangque-media"
EXPECTED_LIBRARY_COUNT = 204
EXPECTED_LIBRARY_PRIMARY_HOST = "8.148.158.106"
EXPECTED_LIBRARY_PRIMARY_ROOT = "/home/ubuntu/material-libraries/huangque-media"
EXPECTED_WHISPER_MODEL = "small"
EXPECTED_WHISPER_CACHE = "/home/ubuntu/.cache/huggingface/hub"
EXPECTED_SUBTITLE_FONT = "Noto Sans SC"
MIRROR_PROVENANCE_NAME = ".huangque-mirror-source.json"
MAX_PROVENANCE_BYTES = 16 * 1024
MAX_INDEX_BYTES = 8 * 1024 * 1024


class PreflightError(RuntimeError):
    pass


def _read_locked_regular(path, maximum):
    path = pathlib.Path(path)
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise PreflightError("digital-human material mirror record is invalid")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or opened.st_size > maximum
            ):
                raise PreflightError("digital-human material mirror record changed")
            chunks = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > maximum:
                raise PreflightError("digital-human material mirror record is too large")
            after = os.fstat(descriptor)
            if (
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            ):
                raise PreflightError("digital-human material mirror record changed")
            return raw
        finally:
            os.close(descriptor)
    except PreflightError:
        raise
    except (OSError, ValueError) as exc:
        raise PreflightError("digital-human material mirror record is unavailable") from exc


def _verify_material_mirror_provenance(root=None):
    root = pathlib.Path(root or EXPECTED_LIBRARY_ROOT)
    raw_provenance = _read_locked_regular(
        root / MIRROR_PROVENANCE_NAME, MAX_PROVENANCE_BYTES,
    )
    try:
        provenance = json.loads(raw_provenance.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError("digital-human material mirror provenance is invalid") from exc
    expected_keys = {
        "schema_version", "source_host", "source_root", "mirror_root",
        "entry_count", "index_sha256",
    }
    if not isinstance(provenance, dict) or set(provenance) != expected_keys:
        raise PreflightError("digital-human material mirror provenance is invalid")
    if (
        provenance.get("schema_version") != 1
        or provenance.get("source_host") != EXPECTED_LIBRARY_PRIMARY_HOST
        or provenance.get("source_root") != EXPECTED_LIBRARY_PRIMARY_ROOT
        or provenance.get("mirror_root") != EXPECTED_LIBRARY_ROOT
        or provenance.get("entry_count") != EXPECTED_LIBRARY_COUNT
    ):
        raise PreflightError("digital-human material mirror source is not locked")
    digest = str(provenance.get("index_sha256") or "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise PreflightError("digital-human material mirror digest is invalid")
    index_raw = _read_locked_regular(root / "index.jsonl", MAX_INDEX_BYTES)
    if hashlib.sha256(index_raw).hexdigest() != digest:
        raise PreflightError("digital-human material mirror index does not match source")
    return {"index_sha256": digest, "source_host": EXPECTED_LIBRARY_PRIMARY_HOST}


def run():
    configured = str(os.environ.get(
        "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT", "",
    ) or "").strip()
    if configured != EXPECTED_LIBRARY_ROOT:
        raise PreflightError("digital-human local material root is not locked")

    from content_domains import core, digital_human_v2, video

    if (
        video.WHISPER_MODEL_NAME != EXPECTED_WHISPER_MODEL
        or video.WHISPER_CACHE_DIR != EXPECTED_WHISPER_CACHE
        or video.SUBTITLE_FONT != EXPECTED_SUBTITLE_FONT
    ):
        raise PreflightError("digital-human subtitle runtime is not production locked")

    mirror = _verify_material_mirror_provenance()
    library = digital_human_v2.local_material_library_operational_probe(
        EXPECTED_LIBRARY_COUNT, verify_all=True,
    )
    if int(library.get("verified_files") or 0) != EXPECTED_LIBRARY_COUNT:
        raise PreflightError("digital-human material mirror was not fully verified")
    if _verify_material_mirror_provenance() != mirror:
        raise PreflightError("digital-human material mirror changed during preflight")
    subtitle = video.subtitle_runtime_preflight()
    provider = core._domains()[2]
    heygen = provider.heygen_upload_preflight()
    if not isinstance(heygen, dict) or not heygen.get("ok"):
        raise PreflightError("HeyGen upload preflight did not confirm readiness")
    return {
        "ok": True,
        "library_count": int(library["count"]),
        "library_index_sha256": mirror["index_sha256"],
        "library_primary_host": mirror["source_host"],
        "subtitle_model": str(subtitle["model"]),
        "heygen_upload": True,
    }


def main():
    try:
        result = run()
    except Exception as exc:
        print(json.dumps({
            "ok": False, "error": type(exc).__name__,
        }, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
