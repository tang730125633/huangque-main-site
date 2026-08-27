#!/usr/bin/env python3
"""Fail-closed production preflight for the digital-human v2 runtime."""

import json
import os
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))


EXPECTED_LIBRARY_ROOT = "/home/ubuntu/material-libraries/huangque-media"
EXPECTED_LIBRARY_COUNT = 204


class PreflightError(RuntimeError):
    pass


def run():
    configured = str(os.environ.get(
        "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT", "",
    ) or "").strip()
    if configured != EXPECTED_LIBRARY_ROOT:
        raise PreflightError("digital-human local material root is not locked")

    from content_domains import core, digital_human_v2, video

    library = digital_human_v2.local_material_library_operational_probe(
        EXPECTED_LIBRARY_COUNT,
    )
    subtitle = video.subtitle_runtime_preflight()
    provider = core._domains()[2]
    heygen = provider.heygen_upload_preflight()
    if not isinstance(heygen, dict) or not heygen.get("ok"):
        raise PreflightError("HeyGen upload preflight did not confirm readiness")
    return {
        "ok": True,
        "library_count": int(library["count"]),
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
