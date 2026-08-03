"""Paid worker handler for one generated short-drama sound effect."""

import json
import os
import pathlib
import subprocess
import time
import uuid

from server.providers.sound_effects import configured_provider

from .core import _out_path, public_url


def _probe(path):
    process = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_name,sample_rate,channels",
            "-of", "json", str(path),
        ],
        check=True,
        timeout=30,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    payload = json.loads(process.stdout or "{}")
    streams = payload.get("streams") or []
    stream = streams[0] if streams else {}
    duration_ms = int(round(float(
        (payload.get("format") or {}).get("duration") or 0
    ) * 1000))
    if duration_ms <= 0:
        raise ValueError("生成音效时长无效")
    return {
        "duration_ms": duration_ms,
        "codec": str(stream.get("codec_name") or ""),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
    }


def _normalize(source, target):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(source),
            "-vn", "-af",
            "highpass=f=30,alimiter=limit=0.95,loudnorm=I=-20:LRA=11:TP=-1.5",
            "-ar", "48000", "-ac", "2", "-codec:a", "libmp3lame",
            "-b:a", "192k", str(target),
        ],
        check=True,
        timeout=90,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _validate_payload(payload):
    allowed = {
        "prompt", "duration_seconds", "loop", "suggestion_id", "project_id",
        "shot_id", "kind", "owner_username", "_username", "_job_id",
    }
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("AI 音效任务字段不正确")
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt or len(prompt) > 450:
        raise ValueError("音效提示词必须为 1 到 450 个字符")
    try:
        duration = round(float(payload.get("duration_seconds")), 3)
    except (TypeError, ValueError):
        raise ValueError("音效时长无效")
    if duration < 0.5 or duration > 30:
        raise ValueError("音效时长必须在 0.5 到 30 秒之间")
    return {
        "prompt": prompt,
        "duration_seconds": duration,
        "loop": bool(payload.get("loop")),
        "suggestion_id": str(payload.get("suggestion_id") or ""),
        "project_id": str(payload.get("project_id") or ""),
        "shot_id": str(payload.get("shot_id") or ""),
        "kind": str(payload.get("kind") or "foley"),
        "username": str(payload.get("_username") or ""),
        "owner_username": str(payload.get("owner_username") or ""),
        "job_id": int(payload.get("_job_id") or 0),
    }


def gen_short_drama_sound_effect(payload):
    request = _validate_payload(payload)
    provider = configured_provider(os.environ)
    generated = provider.generate(
        prompt=request["prompt"],
        duration_seconds=request["duration_seconds"],
        loop=request["loop"],
    )
    suffix = ".wav" if generated.content_type in {
        "audio/wav", "audio/x-wav",
    } else ".mp3"
    stem = "sfx_%s_%s" % (
        request["job_id"] or int(time.time() * 1000),
        uuid.uuid4().hex[:10],
    )
    raw_rel = "audio/%s_source%s" % (stem, suffix)
    final_rel = "audio/%s.mp3" % stem
    raw_path = _out_path(raw_rel)
    final_path = _out_path(final_rel)
    working_path = final_path.with_name(final_path.stem + "_working.mp3")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(generated.data)
    try:
        _normalize(raw_path, working_path)
        quality = _probe(working_path)
        working_path.replace(final_path)
    except Exception:
        for path in (working_path, final_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        try:
            raw_path.unlink()
        except FileNotFoundError:
            pass
    expected_ms = int(round(request["duration_seconds"] * 1000))
    delta_ms = abs(int(quality["duration_ms"]) - expected_ms)
    review = []
    if delta_ms > max(500, int(expected_ms * 0.15)):
        review.append("duration_mismatch")
    if quality["sample_rate"] != 48000 or quality["channels"] != 2:
        review.append("media_spec_mismatch")
    return {
        "type": "audio",
        "asset_kind": "sound_effect",
        "file": final_rel,
        "url": public_url(final_rel, "audio/mpeg"),
        "voice": "",
        "text": request["prompt"],
        "prompt": request["prompt"],
        "duration_ms": quality["duration_ms"],
        "provider": generated.provider,
        "provider_model": generated.model,
        "provider_request_id": generated.request_id,
        "provider_billing_units": generated.billing_units,
        "quality": {
            **quality,
            "expected_duration_ms": expected_ms,
            "duration_delta_ms": delta_ms,
            "decision": "manual_review" if review else "passed",
            "reasons": review,
        },
        "sound_design": {
            "project_id": request["project_id"],
            "shot_id": request["shot_id"],
            "suggestion_id": request["suggestion_id"],
            "kind": request["kind"],
            "loop": request["loop"],
            "owner_username": request["owner_username"],
        },
    }


HANDLERS = {"short_drama_sound_effect": gen_short_drama_sound_effect}
