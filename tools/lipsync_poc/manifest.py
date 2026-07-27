"""Strict sample-manifest loading and deterministic input hashing."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .adapters.base import LipsyncRequest


MANIFEST_VERSION = "1.0"
SAMPLE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
RATIOS = {"9:16", "16:9", "1:1"}
SPEAKING_MODES = {"visible", "offscreen", "narration"}
MANIFEST_KEYS = {"manifest_version", "dataset_name", "samples"}
SAMPLE_KEYS = {
    "sample_id",
    "video_file",
    "audio_file",
    "transcript",
    "speaking_mode",
    "character_key",
    "face_target",
    "duration_ms",
    "ratio",
    "output_spec",
    "tags",
    "notes",
}
OUTPUT_KEYS = {"resolution", "fps"}


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class PocSample:
    sample_id: str
    video_path: Path
    audio_path: Path
    transcript: str
    speaking_mode: str
    character_key: Optional[str]
    face_target: Optional[Mapping[str, Any]]
    duration_ms: int
    ratio: str
    resolution: str
    fps: int
    tags: tuple
    notes: str
    input_hash: str

    def to_request(self):
        return LipsyncRequest(
            sample_id=self.sample_id,
            video_path=self.video_path,
            audio_path=self.audio_path,
            transcript=self.transcript,
            speaking_mode=self.speaking_mode,
            character_key=self.character_key,
            face_target=self.face_target,
            duration_ms=self.duration_ms,
            ratio=self.ratio,
            resolution=self.resolution,
            fps=self.fps,
            input_hash=self.input_hash,
        )


def _relative_asset(root, value, label):
    candidate = Path(str(value or ""))
    if not value or candidate.is_absolute() or ".." in candidate.parts:
        raise ManifestError(f"{label} must be a safe relative path")
    root = root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ManifestError(f"{label} escapes assets_root") from error
    if not resolved.is_file():
        raise ManifestError(f"{label} does not exist: {candidate.as_posix()}")
    return resolved


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _input_hash(sample, video_hash, audio_hash):
    immutable = {
        "audio_hash": audio_hash,
        "character_key": sample.get("character_key"),
        "duration_ms": sample["duration_ms"],
        "face_target": sample.get("face_target"),
        "output_spec": sample["output_spec"],
        "ratio": sample["ratio"],
        "speaking_mode": sample["speaking_mode"],
        "transcript": sample["transcript"],
        "video_hash": video_hash,
    }
    encoded = json.dumps(
        immutable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_sample(raw, seen):
    if not isinstance(raw, dict):
        raise ManifestError("every sample must be an object")
    unknown = set(raw) - SAMPLE_KEYS
    if unknown:
        raise ManifestError("unsupported sample fields: " + ", ".join(sorted(unknown)))
    sample_id = str(raw.get("sample_id") or "")
    if not SAMPLE_ID_PATTERN.fullmatch(sample_id):
        raise ManifestError("sample_id must be lowercase and filesystem-safe")
    if sample_id in seen:
        raise ManifestError(f"duplicate sample_id: {sample_id}")
    seen.add(sample_id)
    if raw.get("speaking_mode") not in SPEAKING_MODES:
        raise ManifestError("invalid speaking_mode")
    if raw.get("ratio") not in RATIOS:
        raise ManifestError("invalid ratio")
    duration_ms = raw.get("duration_ms")
    if not isinstance(duration_ms, int) or not 1_000 <= duration_ms <= 60_000:
        raise ManifestError("duration_ms must be an integer between 1000 and 60000")
    transcript = str(raw.get("transcript") or "").strip()
    if not transcript:
        raise ManifestError("transcript is required")
    output = raw.get("output_spec")
    if not isinstance(output, dict) or set(output) - OUTPUT_KEYS:
        raise ManifestError("output_spec only supports resolution and fps")
    if output.get("resolution") not in {"720p", "1080p"}:
        raise ManifestError("unsupported output resolution")
    if output.get("fps") not in {24, 25, 30}:
        raise ManifestError("unsupported output fps")
    if raw["speaking_mode"] == "visible" and not raw.get("character_key"):
        raise ManifestError("visible speech requires character_key")
    tags = raw.get("tags") or []
    if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
        raise ManifestError("tags must be a list of strings")
    return transcript, output, tuple(tags)


def load_manifest(path, assets_root):
    path = Path(path)
    assets_root = Path(assets_root)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError("manifest is not readable JSON") from error
    if not isinstance(document, dict):
        raise ManifestError("manifest root must be an object")
    unknown = set(document) - MANIFEST_KEYS
    if unknown:
        raise ManifestError("unsupported manifest fields: " + ", ".join(sorted(unknown)))
    if document.get("manifest_version") != MANIFEST_VERSION:
        raise ManifestError(f"manifest_version must be {MANIFEST_VERSION}")
    dataset_name = document.get("dataset_name")
    if (
        not isinstance(dataset_name, str)
        or not dataset_name.strip()
        or len(dataset_name) > 100
    ):
        raise ManifestError("dataset_name must contain between 1 and 100 characters")
    rows = document.get("samples")
    if not isinstance(rows, list) or not rows or len(rows) > 100:
        raise ManifestError("samples must contain between 1 and 100 entries")

    seen = set()
    samples = []
    for raw in rows:
        transcript, output, tags = _validate_sample(raw, seen)
        video_path = _relative_asset(assets_root, raw.get("video_file"), "video_file")
        audio_path = _relative_asset(assets_root, raw.get("audio_file"), "audio_file")
        samples.append(PocSample(
            sample_id=raw["sample_id"],
            video_path=video_path,
            audio_path=audio_path,
            transcript=transcript,
            speaking_mode=raw["speaking_mode"],
            character_key=raw.get("character_key"),
            face_target=raw.get("face_target"),
            duration_ms=raw["duration_ms"],
            ratio=raw["ratio"],
            resolution=output["resolution"],
            fps=output["fps"],
            tags=tags,
            notes=str(raw.get("notes") or ""),
            input_hash=_input_hash(
                raw,
                _file_hash(video_path),
                _file_hash(audio_path),
            ),
        ))
    return samples
