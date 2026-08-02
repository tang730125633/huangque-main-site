# -*- coding: utf-8 -*-
"""Deterministic speech cleanup suggestions and non-destructive EDL building."""

import hashlib
import json
import re


MAX_DURATION_MS = 4 * 60 * 60 * 1000
MAX_WORDS = 20000
MAX_CANDIDATES = 2000
INNER_SILENCE_MS = 500
DEFAULT_REMOVE_SILENCE_MS = 700
LEADING_SILENCE_MS = 120
TRAILING_SILENCE_MS = 220
REPETITION_MAX_GAP_MS = 450
HARD_FILLERS = {"嗯", "呃", "额", "呃啊", "嗯嗯", "啊嗯"}
DECISIONS = {"keep", "remove"}


class AnalysisError(ValueError):
    pass


def _integer(value, field, minimum=0, maximum=MAX_DURATION_MS):
    if isinstance(value, bool):
        raise AnalysisError("%s格式无效" % field)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise AnalysisError("%s格式无效" % field)
    if not minimum <= number <= maximum:
        raise AnalysisError("%s超出范围" % field)
    return number


def _confidence(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(1.0, number)), 4)


def _normalized_text(value):
    text = re.sub(r"\s+", "", str(value or "")).strip()
    if len(text) > 120:
        raise AnalysisError("逐词文本过长")
    return text


def normalize_words(words, duration_ms):
    duration_ms = _integer(duration_ms, "视频时长", minimum=1)
    if not isinstance(words, list) or len(words) > MAX_WORDS:
        raise AnalysisError("逐词时间轴格式无效")
    normalized = []
    previous_start = 0
    for index, item in enumerate(words):
        if not isinstance(item, dict):
            raise AnalysisError("逐词时间轴第%d项格式无效" % (index + 1))
        if set(item) - {"text", "start_ms", "end_ms", "confidence"}:
            raise AnalysisError("逐词时间轴包含未支持字段")
        text = _normalized_text(item.get("text"))
        if not text:
            continue
        start_ms = _integer(item.get("start_ms"), "词开始时间")
        end_ms = _integer(item.get("end_ms"), "词结束时间")
        if end_ms <= start_ms or end_ms > duration_ms:
            raise AnalysisError("逐词时间范围无效")
        if normalized and start_ms < previous_start:
            raise AnalysisError("逐词时间轴顺序无效")
        previous_start = start_ms
        normalized.append({
            "text": text,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "confidence": _confidence(item.get("confidence")),
        })
    return duration_ms, normalized


def _candidate_id(kind, start_ms, end_ms, text):
    raw = "%s:%d:%d:%s" % (kind, start_ms, end_ms, text)
    return "candidate_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _candidate(kind, start_ms, end_ms, text, reason, confidence, default_selected):
    return {
        "id": _candidate_id(kind, start_ms, end_ms, text),
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "type": kind,
        "text": text,
        "reason": reason,
        "confidence": round(float(confidence), 4),
        "default_selected": bool(default_selected),
        "user_decision": "pending",
    }


def detect_candidates(duration_ms, words):
    duration_ms, words = normalize_words(words, duration_ms)
    candidates = []
    if words:
        leading = words[0]["start_ms"]
        if leading >= LEADING_SILENCE_MS:
            candidates.append(_candidate(
                "leading_silence", 0, leading, "",
                "开头静音 %.2f 秒" % (leading / 1000.0),
                min(0.99, 0.78 + leading / 5000.0), True,
            ))
        for left, right in zip(words, words[1:]):
            gap_start = max(left["end_ms"], left["start_ms"])
            gap_end = right["start_ms"]
            gap = gap_end - gap_start
            if gap >= INNER_SILENCE_MS:
                candidates.append(_candidate(
                    "silence", gap_start, gap_end, "",
                    "句中静音 %.2f 秒" % (gap / 1000.0),
                    min(0.99, 0.72 + gap / 6000.0),
                    gap >= DEFAULT_REMOVE_SILENCE_MS,
                ))
        trailing = duration_ms - words[-1]["end_ms"]
        if trailing >= TRAILING_SILENCE_MS:
            candidates.append(_candidate(
                "trailing_silence", words[-1]["end_ms"], duration_ms, "",
                "结尾静音 %.2f 秒" % (trailing / 1000.0),
                min(0.99, 0.78 + trailing / 5000.0), True,
            ))
    for index, word in enumerate(words):
        if word["text"] in HARD_FILLERS:
            candidates.append(_candidate(
                "filler_word", word["start_ms"], word["end_ms"], word["text"],
                "检测到明确语气词“%s”" % word["text"],
                word["confidence"] if word["confidence"] is not None else 0.82,
                False,
            ))
        if index == 0:
            continue
        previous = words[index - 1]
        gap = word["start_ms"] - previous["end_ms"]
        if (word["text"] == previous["text"] and
                -100 <= gap <= REPETITION_MAX_GAP_MS and len(word["text"]) <= 8):
            candidates.append(_candidate(
                "repetition", previous["start_ms"], previous["end_ms"], previous["text"],
                "相邻内容重复，建议确认是否删除前一个“%s”" % previous["text"],
                0.76, False,
            ))
    candidates.sort(key=lambda item: (item["start_ms"], item["end_ms"], item["type"]))
    unique = []
    seen = set()
    for item in candidates:
        key = (item["type"], item["start_ms"], item["end_ms"], item["text"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    if len(unique) > MAX_CANDIDATES:
        raise AnalysisError("删除建议过多，请缩短视频后重试")
    return {"duration_ms": duration_ms, "words": words, "candidates": unique}


def enrich_candidates(duration_ms, words, candidates, silence_ranges=None):
    duration_ms = _integer(duration_ms, "视频时长", minimum=1)
    result = [dict(item) for item in (candidates or [])]
    occupied = {(item.get("start_ms"), item.get("end_ms"), item.get("type")) for item in result}
    for item in silence_ranges or []:
        try:
            start_ms = int(item.get("start_ms")); end_ms = int(item.get("end_ms"))
        except (TypeError, ValueError):
            continue
        span = end_ms - start_ms
        if span < 160 or start_ms < 0 or end_ms > duration_ms:
            continue
        if start_ms <= 400:
            kind, reason, confidence, default = (
                "leading_silence", "开头静音 %.2f 秒" % (span / 1000.0), 0.98, True)
        elif duration_ms - end_ms <= 450:
            kind, reason, confidence, default = (
                "trailing_silence", "结尾静音 %.2f 秒" % (span / 1000.0), 0.98, True)
        elif span >= 500:
            kind, reason, confidence, default = (
                "silence", "句中静音 %.2f 秒" % (span / 1000.0), min(0.99, 0.88 + span / 10000.0), True)
        else:
            kind, reason, confidence, default = (
                "breath_pause", "短气口 %.2f 秒，请确认是否删除" % (span / 1000.0), 0.78, False)
        overlap_ms = sum(max(0, min(end_ms, int(word.get("end_ms") or 0)) -
                             max(start_ms, int(word.get("start_ms") or 0))) for word in words)
        if overlap_ms > max(80, int(span * .25)):
            default = False
            confidence = min(confidence, .72)
            reason += "；ASR 时间轴与该区间有重叠，必须试听确认"
        key = (start_ms, end_ms, kind)
        if key in occupied:
            continue
        occupied.add(key)
        result.append(_candidate(kind, start_ms, end_ms, "", reason, confidence, default))

    compact = "".join(_normalized_text(item.get("text")) for item in words)
    tail_markers = ("那我可以了", "我可以了", "可以了", "好了", "结束了", "再来一遍", "重来")
    marker = next((value for value in tail_markers if compact.endswith(value)), "")
    if marker:
        collected = ""
        start_ms = None
        end_ms = None
        for word in reversed(words):
            collected = _normalized_text(word.get("text")) + collected
            start_ms = int(word.get("start_ms") or 0)
            end_ms = end_ms or int(word.get("end_ms") or 0)
            if collected.endswith(marker) or len(collected) >= len(marker) + 4:
                break
        if start_ms is not None and end_ms and end_ms > start_ms:
            result.append(_candidate(
                "suspected_misspeaking", start_ms, min(duration_ms, end_ms), marker,
                "检测到疑似正片后的补充话语“%s”，必须由用户确认" % marker,
                0.86, False,
            ))
    result.sort(key=lambda item: (item["start_ms"], item["end_ms"], item["type"]))
    unique = []
    seen = set()
    for item in result:
        key = (item["type"], item["start_ms"], item["end_ms"], item.get("text") or "")
        if key not in seen:
            seen.add(key); unique.append(item)
    if len(unique) > MAX_CANDIDATES:
        raise AnalysisError("删除建议过多，请缩短视频后重试")
    return unique


def normalize_decisions(candidates, decisions):
    if not isinstance(decisions, dict):
        raise AnalysisError("粗剪确认格式无效")
    known = {item["id"] for item in candidates}
    if set(decisions) != known:
        raise AnalysisError("必须确认全部粗剪建议")
    result = {}
    for candidate_id, decision in decisions.items():
        value = str(decision or "").strip().lower()
        if value not in DECISIONS:
            raise AnalysisError("粗剪确认值无效")
        result[candidate_id] = value
    return result


def _merge_ranges(ranges):
    merged = []
    for start_ms, end_ms in sorted(ranges):
        if end_ms <= start_ms:
            continue
        if merged and start_ms <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end_ms)
        else:
            merged.append([start_ms, end_ms])
    return merged


def build_edl(duration_ms, candidates, decisions):
    duration_ms = _integer(duration_ms, "视频时长", minimum=1)
    decisions = normalize_decisions(candidates, decisions)
    removed = []
    removed_ids = []
    for item in candidates:
        if decisions[item["id"]] != "remove":
            continue
        start_ms = _integer(item.get("start_ms"), "删除开始时间")
        end_ms = _integer(item.get("end_ms"), "删除结束时间")
        if end_ms <= start_ms or end_ms > duration_ms:
            raise AnalysisError("删除区间无效")
        removed.append((start_ms, end_ms))
        removed_ids.append(item["id"])
    removed = _merge_ranges(removed)
    keep_ranges = []
    cursor = 0
    for start_ms, end_ms in removed:
        if start_ms > cursor:
            keep_ranges.append({"source_start_ms": cursor, "source_end_ms": start_ms})
        cursor = max(cursor, end_ms)
    if cursor < duration_ms:
        keep_ranges.append({"source_start_ms": cursor, "source_end_ms": duration_ms})
    output_duration_ms = sum(
        item["source_end_ms"] - item["source_start_ms"] for item in keep_ranges
    )
    if output_duration_ms <= 0:
        raise AnalysisError("不能删除整条视频")
    return {
        "keep_ranges": keep_ranges,
        "removed_ranges": [
            {"source_start_ms": start_ms, "source_end_ms": end_ms}
            for start_ms, end_ms in removed
        ],
        "removed_candidate_ids": removed_ids,
        "source_duration_ms": duration_ms,
        "output_duration_ms": output_duration_ms,
    }


def transcript_hash(duration_ms, words):
    payload = {"duration_ms": duration_ms, "words": words}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
