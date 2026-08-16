"""Constrained Chinese alignment mapping and quality calculation."""

from __future__ import annotations

import statistics
import unicodedata


PUNCTUATION = set("，。！？；：、,.!?;:（）()【】[]《》“”‘’\"'")


def normalized_tokens(value):
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return [
        character for character in normalized
        if not character.isspace() and character not in PUNCTUATION
    ]


def _provider_words(segment):
    expanded = []
    for source_index, word in enumerate(segment.get("words") or []):
        tokens = normalized_tokens(word.get("token"))
        if not tokens:
            continue
        start_ms = int(word.get("start_ms") or 0)
        end_ms = int(word.get("end_ms") or 0)
        if end_ms <= start_ms:
            continue
        confidence = max(0.0, min(1.0, float(word.get("confidence") or 0)))
        duration = end_ms - start_ms
        for index, token in enumerate(tokens):
            start = start_ms + round(duration * index / len(tokens))
            end = start_ms + round(duration * (index + 1) / len(tokens))
            expanded.append({
                "token": token,
                "start_ms": start,
                "end_ms": max(start + 1, end),
                "confidence": confidence,
                "matched_source_index": source_index,
            })
    return expanded


def _merge_ranges(words, threshold):
    ranges = []
    for word in words:
        if word["confidence"] >= threshold:
            continue
        if ranges and word["start_ms"] <= ranges[-1]["end_ms"] + 120:
            ranges[-1]["end_ms"] = max(ranges[-1]["end_ms"], word["end_ms"])
            ranges[-1]["tokens"].append(word["token"])
        else:
            ranges.append({
                "start_ms": word["start_ms"],
                "end_ms": word["end_ms"],
                "tokens": [word["token"]],
            })
    return ranges


def normalize_result(contract, result, capabilities, *, thresholds=None):
    thresholds = {
        "project_coverage": 0.98,
        "line_coverage": 0.95,
        "word_confidence": 0.65,
        "mean_confidence": 0.80,
        **(thresholds or {}),
    }
    provider_segments = {
        str(item.get("line_id") or ""): item
        for item in result.segments
    }
    timeline = []
    all_confidences = []
    unmatched_tokens = []
    degradations = []
    matched_count = 0
    total_count = 0
    for shot in contract.get("shots") or []:
        for line in shot.get("lines") or []:
            line_id = str(line.get("line_id") or "")
            locked = normalized_tokens(line.get("text"))
            total_count += len(locked)
            segment = provider_segments.get(line_id) or {}
            provider = _provider_words(segment)
            words = []
            cursor = 0
            line_unmatched = []
            for locked_index, token in enumerate(locked):
                match_index = next(
                    (
                        index for index in range(cursor, len(provider))
                        if provider[index]["token"] == token
                    ),
                    None,
                )
                if match_index is None:
                    line_unmatched.append({
                        "line_id": line_id,
                        "token": token,
                        "locked_index": locked_index,
                    })
                    continue
                match = dict(provider[match_index])
                match["locked_index"] = locked_index
                match["match_type"] = "exact"
                words.append(match)
                cursor = match_index + 1
            matched_count += len(words)
            all_confidences.extend(item["confidence"] for item in words)
            unmatched_tokens.extend(line_unmatched)
            line_coverage = len(words) / len(locked) if locked else 0.0
            status = "matched"
            degradation = segment.get("degradation")
            if line_unmatched:
                status = "unmatched" if not words else "partial_match"
                degradation = degradation or status
            if not capabilities.supports_word_timestamps:
                degradation = degradation or "word_to_cue"
            if degradation:
                degradations.append({"line_id": line_id, "reason": degradation})
            timeline.append({
                "shot_id": str(line.get("shot_id") or ""),
                "line_id": line_id,
                "text": str(line.get("text") or ""),
                "audio_start_ms": int(line.get("audio_start_ms") or 0),
                "audio_end_ms": int(line.get("audio_end_ms") or 0),
                "subtitle_start_ms": words[0]["start_ms"] if words else None,
                "subtitle_end_ms": words[-1]["end_ms"] if words else None,
                "confidence": (
                    round(statistics.fmean(
                        item["confidence"] for item in words
                    ), 6)
                    if words else 0.0
                ),
                "coverage": round(line_coverage, 6),
                "status": status,
                "words": words,
                "tokens": words,
                "unmatched_tokens": line_unmatched,
                "low_confidence_ranges": _merge_ranges(
                    words, thresholds["word_confidence"]
                ),
                "degradation": degradation,
                "provider_transcript": segment.get("transcript"),
            })
    coverage = matched_count / total_count if total_count else 0.0
    mean_confidence = (
        statistics.fmean(all_confidences) if all_confidences else 0.0
    )
    low_ranges = [
        {"line_id": item["line_id"], **value}
        for item in timeline
        for value in item["low_confidence_ranges"]
    ]
    blockers = []
    if unmatched_tokens:
        blockers.append({
            "code": "alignment_unmatched_transcript",
            "message": "存在未匹配的锁定台词，必须人工设置或确认边界",
        })
    if (
        coverage < thresholds["project_coverage"]
        or mean_confidence < thresholds["mean_confidence"]
        or low_ranges
        or degradations
    ):
        blockers.append({
            "code": "alignment_low_confidence",
            "message": "真实对齐结果需要人工复核",
        })
    return timeline, {
        "coverage": round(coverage, 6),
        "mean_confidence": round(mean_confidence, 6),
        "low_confidence_ranges": low_ranges,
        "unmatched_tokens": unmatched_tokens,
        "degradation": degradations,
        "matched_token_count": matched_count,
        "token_count": total_count,
        "thresholds": thresholds,
        "review_required": True,
        "blockers": blockers,
        "provider_diagnostics": dict(result.diagnostics or {}),
    }
