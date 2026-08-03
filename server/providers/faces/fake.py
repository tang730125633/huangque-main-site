"""Deterministic non-biometric provider for PR-J development and tests."""

import hashlib
import json

from .base import FaceAnalysisProvider, FaceProviderCapabilities


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _fraction(value, floor=0.0, span=1.0):
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return round(floor + (int(digest[:8], 16) / 0xFFFFFFFF) * span, 4)


class FakeFaceAnalysisProvider(FaceAnalysisProvider):
    """Produces stable tracks from the requested project character set."""

    capabilities = FaceProviderCapabilities(
        name="fake-face-analysis",
        contract_version="short-drama-face-analysis-v1",
        detector_version="fake-detector-v1",
        tracker_version="fake-tracker-v1",
        matcher_version="fake-matcher-v1",
        supports_occlusion=True,
        supports_reid=False,
    )

    def __init__(self, fixture=None):
        self.fixture = fixture

    def analyze(self, request):
        if isinstance(self.fixture, dict):
            return json.loads(_canonical(self.fixture))
        segments = sorted(
            list(request.get("segments") or []),
            key=lambda item: (int(item["start_ms"]), str(item["id"])),
        )
        references = sorted(
            list(request.get("character_references") or []),
            key=lambda item: str(item["character_key"]),
        )
        characters = [str(item["character_key"]) for item in references]
        if not characters:
            characters = sorted({
                str(item.get("character_key") or "") for item in segments
                if str(item.get("character_key") or "")
            })
        tracks = []
        matches = []
        detections = []
        for index, character_key in enumerate(characters):
            track_id = "track-%02d-%s" % (index + 1, character_key[:24])
            own_segments = [
                item for item in segments
                if str(item.get("character_key") or "") == character_key
            ]
            if own_segments:
                first_ms = min(int(item["start_ms"]) for item in own_segments)
                last_ms = max(int(item["end_ms"]) for item in own_segments)
            elif segments:
                first_ms = min(int(item["start_ms"]) for item in segments)
                last_ms = max(int(item["end_ms"]) for item in segments)
            else:
                first_ms, last_ms = 0, 1
            confidence = _fraction(character_key + ":confidence", 0.88, 0.1)
            x = round(0.08 + (index % 3) * 0.29, 4)
            bbox = [x, 0.18, 0.22, 0.55]
            tracks.append({
                "track_id": track_id,
                "spans": [{"start_ms": first_ms, "end_ms": last_ms}],
                "first_ms": first_ms,
                "last_ms": last_ms,
                "coverage": 1.0,
                "gap_ms": 0,
                "stability": confidence,
                "bbox": bbox,
                "tracker_version": self.capabilities.tracker_version,
            })
            detections.append({
                "time_ms": first_ms,
                "frame": max(0, first_ms // 40),
                "track_id": track_id,
                "bbox": bbox,
                "landmarks": [],
                "pose": {"yaw": 0.0, "pitch": 0.0, "roll": 0.0},
                "occlusion": 0.0,
                "blur": 0.0,
                "visibility": 1.0,
                "confidence": confidence,
            })
            matches.append({
                "track_id": track_id,
                "character_key": character_key,
                "score": confidence,
                "margin_to_second": round(max(0.0, confidence - 0.55), 4),
                "reference_set_hash": request.get("reference_set_hash"),
                "model_version": self.capabilities.matcher_version,
            })
        track_by_character = {
            item["character_key"]: item["track_id"] for item in matches
        }
        proposals = []
        for segment in segments:
            character_key = str(segment.get("character_key") or "")
            candidates = []
            own_track = track_by_character.get(character_key)
            for match in matches:
                score = (
                    float(match["score"])
                    if match["track_id"] == own_track
                    else min(0.45, float(match["score"]) * 0.4)
                )
                candidates.append({
                    "face_track_id": match["track_id"],
                    "character_key": match["character_key"],
                    "score": round(score, 4),
                })
            candidates.sort(key=lambda item: (-item["score"], item["face_track_id"]))
            top = candidates[0] if candidates else None
            confidence = float(top["score"]) if top else 0.0
            proposals.append({
                "segment_id": str(segment["id"]),
                "candidates": candidates,
                "confidence": confidence,
                "reason_codes": [] if confidence >= 0.8 else ["low_match_confidence"],
                "recommended_action": (
                    "confirm" if confidence >= 0.8 else "manual_review"
                ),
            })
        return {
            "detections": detections,
            "tracks": tracks,
            "matches": matches,
            "proposals": proposals,
            "limitations": ["deterministic_fake_no_biometrics"],
        }
