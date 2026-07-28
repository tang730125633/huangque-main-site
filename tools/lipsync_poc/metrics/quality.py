"""Deterministic media-contract metrics; perceptual scoring remains human-reviewed."""


def media_contract_metrics(source_video, provider_output, expected):
    source_duration = int(source_video.get("duration_ms") or 0)
    output_duration = int(provider_output.get("duration_ms") or 0)
    output_video = provider_output.get("video") or {}
    expected_width = expected.get("width")
    expected_height = expected.get("height")
    resolution_matches = None
    if expected_width and expected_height:
        resolution_matches = (
            output_video.get("width") == expected_width
            and output_video.get("height") == expected_height
        )
    return {
        "has_video": int(provider_output.get("video_stream_count") or 0) > 0,
        "output_audio_stream_count": int(
            provider_output.get("audio_stream_count") or 0
        ),
        "duration_delta_ms": output_duration - source_duration,
        "absolute_duration_delta_ms": abs(output_duration - source_duration),
        "fps_delta": round(
            float(output_video.get("fps") or 0) - float(expected.get("fps") or 0),
            3,
        ),
        "resolution_matches": resolution_matches,
    }


def empty_human_review():
    """Stable fields for blinded review; no score is invented by automation."""
    return {
        "review_status": "pending",
        "lip_sync_score_1_to_5": None,
        "identity_score_1_to_5": None,
        "visual_quality_score_1_to_5": None,
        "whole_sentence_offset": None,
        "av_offset_ms": None,
        "reviewer_id": None,
        "reviewed_at": None,
        "notes": "",
    }
