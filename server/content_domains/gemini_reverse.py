# -*- coding: utf-8 -*-
"""Strict Gemini 3.1 video reverse analysis.

The provider may describe evidence, but it never owns the server timeline.
Invalid or incomplete output is retried once against the original media and is
never repaired, expanded, or routed to another provider.
"""

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from difflib import SequenceMatcher


MODEL = "gemini-3.1-pro-preview"
API_BASE = "https://generativelanguage.googleapis.com"
ANALYSIS_BUDGET_SECONDS = max(
    60,
    min(
        540,
        int(os.environ.get("BREAKDOWN_ANALYSIS_BUDGET", "540") or "540"),
    ),
)
REQUEST_TIMEOUT_SECONDS = max(
    30,
    min(
        240,
        int(os.environ.get("BREAKDOWN_GEMINI_TIMEOUT", "180") or "180"),
    ),
)
INLINE_MAX_BYTES = 14 * 1024 * 1024
INLINE_MAX_DURATION_SECONDS = 15.0
INLINE_MAX_REQUEST_BYTES = 18_000_000
MAX_MEDIA_BYTES = 200 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
FILE_POLL_INITIAL_DELAY_SECONDS = 1.0
FILE_POLL_MAX_DELAY_SECONDS = 8.0
CLEANUP_RETRY_DELAYS_SECONDS = (1.0, 3.0)

UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"
FACT_FIELDS = (
    "subject_identity",
    "subject_appearance",
    "wardrobe",
    "position_scale",
    "action_start",
    "action_process",
    "action_end",
    "direction_speed",
    "foreground",
    "midground",
    "background",
    "shot_scale",
    "camera_angle",
    "camera_movement",
    "composition",
    "lighting_color",
    "style_texture",
    "rhythm",
    "sound",
    "subtitles",
    "continuity",
)
OPTIONAL_FACT_FIELDS = {"wardrobe", "sound", "subtitles", "continuity"}
GENERATION_ADVICE_FIELDS = (
    "aspect_ratio",
    "fps",
    "camera_control",
    "negative_prompt",
)
SUBJECTIVE_PATTERN = re.compile(
    r"(?:似乎|可能|大概|仿佛|应该|看起来像|推测|猜测|或许|疑似)"
)
TOKEN_PATTERN = re.compile(
    r"(?i)\b(?:authorization|api[_ -]?key|access[_ -]?token|token|secret)\b"
    r"\s*[:=]\s*[\"']?[^,\s;\"'}]+"
)


def _timeline_label(milliseconds):
    minutes, remaining = divmod(max(0, int(milliseconds)), 60_000)
    seconds, millis = divmod(remaining, 1000)
    seconds_text = "%02d" % seconds
    if millis:
        seconds_text += (".%03d" % millis).rstrip("0")
    return "%02d:%s" % (minutes, seconds_text)


def fixed_windows(duration, max_segments=4):
    """Build deterministic server-owned windows for the foundation release."""
    total_milliseconds = max(1, int(max(0.0, float(duration or 0)) * 1000 + 0.5))
    count = min(
        max(1, int(max_segments or 1)),
        3 if total_milliseconds <= 9000 else 4,
        total_milliseconds,
    )
    edges = [
        (index * total_milliseconds + count // 2) // count
        for index in range(count + 1)
    ]
    windows = []
    for index in range(count):
        start_ms, end_ms = edges[index], edges[index + 1]
        windows.append((
            start_ms / 1000.0,
            end_ms / 1000.0,
            "[%s-%s]" % (
                _timeline_label(start_ms),
                _timeline_label(end_ms),
            ),
        ))
    return windows


def _schema():
    fact_row = {
        "type": "object",
        "additionalProperties": False,
        "required": ["key", "value", "evidence_seconds"],
        "properties": {
            "key": {"type": "string", "enum": list(FACT_FIELDS)},
            "value": {"type": "string"},
            "evidence_seconds": {
                "type": "array",
                "items": {"type": "number", "minimum": 0},
                "maxItems": 3,
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["shots"],
        "properties": {
            "shots": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "segment_id",
                        "facts",
                        "generation_advice",
                    ],
                    "properties": {
                        "segment_id": {"type": "integer", "minimum": 1},
                        "facts": {
                            "type": "array",
                            "minItems": len(FACT_FIELDS),
                            "maxItems": len(FACT_FIELDS),
                            "items": fact_row,
                        },
                        "generation_advice": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": list(GENERATION_ADVICE_FIELDS),
                            "properties": {
                                key: {"type": "string"}
                                for key in GENERATION_ADVICE_FIELDS
                            },
                        },
                    },
                },
            },
        },
    }


def provider_schema():
    """Remove only array bounds rejected by the live Gemini REST endpoint."""
    def compatible(value):
        if isinstance(value, dict):
            return {
                key: compatible(item)
                for key, item in value.items()
                if key not in {"minItems", "maxItems"}
            }
        if isinstance(value, list):
            return [compatible(item) for item in value]
        return value

    return compatible(_schema())


def _safe_text(value, limit=300):
    text = " ".join(str(value or "").replace("\r", " ").split())
    text = re.sub(r"https?://\S+", "[redacted-url]", text)
    text = TOKEN_PATTERN.sub("[redacted-credential]", text)
    text = re.sub(
        r"\b(?:AIza|AQ\.)[A-Za-z0-9._-]{8,}\b",
        "[redacted-credential]",
        text,
    )
    return text[:max(0, int(limit))]


def _response_hash(raw):
    return hashlib.sha256(
        str(raw or "").encode("utf-8", "replace")
    ).hexdigest()


def _remaining(deadline):
    if deadline is None:
        return None
    remaining = float(deadline) - time.monotonic()
    if remaining <= 1:
        raise TimeoutError("Gemini reverse analysis exceeded its total budget")
    return remaining


def _timeout(deadline):
    remaining = _remaining(deadline)
    if remaining is None:
        return REQUEST_TIMEOUT_SECONDS
    return max(1, min(REQUEST_TIMEOUT_SECONDS, int(remaining - 1)))


def _http_error_summary(error):
    code = int(getattr(error, "code", 0) or 0)
    status = ""
    message = ""
    try:
        raw = error.read(8193)
        if len(raw) <= 8192:
            payload = json.loads(raw.decode("utf-8"))
            detail = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(detail, dict):
                code = int(detail.get("code") or code)
                status = str(detail.get("status") or "")
                message = str(detail.get("message") or "")
    except Exception:
        pass
    status = status if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", status) else ""
    parts = ["Gemini HTTP %d" % code]
    if status:
        parts.append(status)
    if message:
        parts.append(_safe_text(message, 180))
    return ": ".join(parts)


def _open(request, deadline=None, heartbeat=None, retry_transient=True):
    attempts = 2 if retry_transient else 1
    last_error = None
    for attempt in range(attempts):
        _remaining(deadline)
        if heartbeat:
            heartbeat()
        try:
            return urllib.request.urlopen(request, timeout=_timeout(deadline))
        except urllib.error.HTTPError as error:
            last_error = error
            code = int(getattr(error, "code", 0) or 0)
            if attempt + 1 < attempts and (code == 429 or 500 <= code < 600):
                continue
            raise RuntimeError(_http_error_summary(error)) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt + 1 < attempts:
                continue
            raise RuntimeError(
                "Gemini request failed: %s" % type(error).__name__
            ) from error
    raise RuntimeError("Gemini request failed") from last_error


def _read_json_response(response):
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Gemini transport response exceeds the safety limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as error:
        raise ValueError("Gemini returned invalid JSON transport response") from error


def _json_request(url, body, api_key, deadline=None, heartbeat=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    with _open(request, deadline=deadline, heartbeat=heartbeat) as response:
        return _read_json_response(response)


def _upload_file(path, mime_type, api_key, deadline=None, heartbeat=None):
    size = os.path.getsize(path)
    if size <= 0 or size > MAX_MEDIA_BYTES:
        raise ValueError("Gemini reverse media size is outside the allowed range")
    start = urllib.request.Request(
        API_BASE + "/upload/v1beta/files",
        data=json.dumps({
            "file": {"display_name": "breakdown-reverse-input"},
        }).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime_type,
        },
        method="POST",
    )
    with _open(start, deadline=deadline, heartbeat=heartbeat) as response:
        upload_url = response.headers.get("X-Goog-Upload-URL")
    if not upload_url or not str(upload_url).startswith(API_BASE + "/"):
        raise RuntimeError("Gemini Files API did not return a trusted upload URL")
    offset = 0
    result = None
    with open(path, "rb") as source:
        while offset < size:
            expected = min(UPLOAD_CHUNK_BYTES, size - offset)
            payload = source.read(expected)
            if len(payload) != expected:
                raise ValueError("Gemini reverse media changed during upload")
            final_chunk = offset + len(payload) == size
            command = "upload, finalize" if final_chunk else "upload"
            upload = urllib.request.Request(
                upload_url,
                data=payload,
                headers={
                    "Content-Type": mime_type,
                    "x-goog-api-key": api_key,
                    "X-Goog-Upload-Offset": str(offset),
                    "X-Goog-Upload-Command": command,
                },
                method="POST",
            )
            # Retrying a chunk blindly can duplicate bytes when the provider
            # committed the chunk but its response was lost. Fail the task
            # instead of risking an invalid remote object.
            with _open(
                upload,
                deadline=deadline,
                heartbeat=heartbeat,
                retry_transient=False,
            ) as response:
                if final_chunk:
                    result = _read_json_response(response)
                else:
                    response.read(MAX_RESPONSE_BYTES + 1)
            offset += len(payload)
        if source.read(1):
            raise ValueError("Gemini reverse media changed during upload")
    if offset != size or not isinstance(result, dict):
        raise RuntimeError("Gemini Files API upload did not complete")
    file_info = result.get("file") if isinstance(result, dict) else None
    if not isinstance(file_info, dict):
        file_info = result if isinstance(result, dict) else {}
    name = str(file_info.get("name") or "")
    uri = str(file_info.get("uri") or "")
    if not re.fullmatch(
        r"files/[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", name
    ) or not uri.startswith("https://"):
        raise RuntimeError("Gemini Files API returned an invalid file reference")
    return {"name": name, "uri": uri, "mime_type": mime_type}


def _wait_for_file(file_info, api_key, deadline=None, heartbeat=None):
    request = urllib.request.Request(
        API_BASE + "/v1beta/" + file_info["name"],
        headers={"x-goog-api-key": api_key},
        method="GET",
    )
    poll_deadline = deadline or (time.monotonic() + ANALYSIS_BUDGET_SECONDS)
    delay = FILE_POLL_INITIAL_DELAY_SECONDS
    while True:
        _remaining(poll_deadline)
        with _open(
            request,
            deadline=poll_deadline,
            heartbeat=heartbeat,
            retry_transient=True,
        ) as response:
            result = _read_json_response(response)
        state = str(result.get("state") or "").upper()
        if state == "ACTIVE":
            active = dict(file_info)
            active["uri"] = str(result.get("uri") or active["uri"])
            return active
        if state in {"FAILED", "ERROR"}:
            raise RuntimeError("Gemini Files API could not process the media")
        remaining = _remaining(poll_deadline)
        sleep_for = delay if remaining is None else min(delay, remaining - 1.0)
        if sleep_for <= 0:
            raise TimeoutError("Gemini reverse analysis exceeded its total budget")
        if heartbeat:
            heartbeat()
        time.sleep(sleep_for)
        delay = min(delay * 2.0, FILE_POLL_MAX_DELAY_SECONDS)


def _delete_file(file_info, api_key):
    if not file_info:
        return {"status": "not_needed", "attempts": 0}
    name = str(file_info.get("name") or "")
    resource_sha256 = hashlib.sha256(
        name.encode("utf-8", "replace")
    ).hexdigest()
    request = urllib.request.Request(
        API_BASE + "/v1beta/" + name,
        headers={"x-goog-api-key": api_key},
        method="DELETE",
    )
    attempts = len(CLEANUP_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(attempts):
        try:
            with _open(
                request,
                deadline=time.monotonic() + 15,
                retry_transient=False,
            ) as response:
                response.read(1024)
            audit = {
                "resource_sha256": resource_sha256,
                "attempt": attempt + 1,
                "status": "deleted",
                "cleanup_pending": False,
            }
            print(
                "[breakdown] gemini cleanup audit=%s"
                % json.dumps(audit, ensure_ascii=True),
                flush=True,
            )
            return {"status": "deleted", "attempts": attempt + 1}
        except Exception as error:
            final_attempt = attempt + 1 == attempts
            audit = {
                "resource_sha256": resource_sha256,
                "attempt": attempt + 1,
                "status": "pending_provider_cleanup",
                "cleanup_pending": True,
                "error": _safe_text(type(error).__name__, 80),
            }
            if not final_attempt:
                audit["retry_in_seconds"] = CLEANUP_RETRY_DELAYS_SECONDS[attempt]
            print(
                "[breakdown] gemini cleanup audit=%s"
                % json.dumps(audit, ensure_ascii=True),
                flush=True,
            )
            if not final_attempt:
                time.sleep(CLEANUP_RETRY_DELAYS_SECONDS[attempt])
    return {"status": "pending_provider_cleanup", "attempts": attempts}


def _instruction(title, duration, platform, transcript, windows, retry_error=""):
    authoritative = [
        {
            "segment_id": index,
            "start_seconds": start,
            "end_seconds": end,
            "display_range": label,
        }
        for index, (start, end, label) in enumerate(windows, 1)
    ]
    retry = ""
    if retry_error:
        retry = (
            "The previous response failed strict validation: %s. "
            "Re-analyze the original media; do not reuse or quote the rejected "
            "draft. Never invent facts merely to pass validation. "
            % _safe_text(retry_error, 500)
        )
    return (
        "Analyze the complete original video using exactly the server-owned "
        "segments below. Return only one complete minified JSON object with "
        "exactly the root key shots; no markdown, wrapper, commentary, timeline "
        "fields, start_seconds, or end_seconds. Return exactly one shot for each "
        "segment_id in ascending order. Each shot has exactly segment_id, facts, "
        "and generation_advice. Facts must contain exactly one row for every key "
        "in this order: %s. Every fact row has exactly key, value, and "
        "evidence_seconds. Use 1-3 timestamps inside the segment for every "
        "observed value and [] only for unknown or not_applicable. Use the exact "
        "sentinel unknown when evidence is insufficient. Use not_applicable only "
        "for wardrobe, sound, subtitles, or continuity when absent. Do not infer "
        "identity, emotion, brand, place, intent, text, sound, objects, or motion. "
        "Describe visible subject identity/appearance/clothing/position, action "
        "start/process/end/direction/speed, foreground/midground/background, shot "
        "scale/angle/movement/composition, lighting/color/style/texture/rhythm, "
        "verified sound/subtitles/continuity. Static states must cite both segment "
        "endpoints. Do not copy descriptions across segments. generation_advice "
        "has exactly aspect_ratio, fps, camera_control, and negative_prompt; write "
        "all non-sentinel values in concise Chinese. Server segments: %s. "
        "Title: %s. Platform: %s. Verified ASR: %s. %s"
    ) % (
        ", ".join(FACT_FIELDS),
        json.dumps(authoritative, ensure_ascii=False, separators=(",", ":")),
        str(title or "")[:200],
        str(platform or "")[:40],
        str(transcript or "(none)")[:3000],
        retry,
    )


def _request_body(
    media_part,
    title,
    duration,
    platform,
    transcript,
    windows,
    validation_error="",
):
    return {
        "systemInstruction": {
            "parts": [{
                "text": "You are an evidence-bound video prompt reverse director.",
            }],
        },
        "contents": [{
            "role": "user",
            "parts": [
                media_part,
                {"text": _instruction(
                    title,
                    duration,
                    platform,
                    transcript,
                    windows,
                    validation_error,
                )},
            ],
        }],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 32768,
            "thinkingConfig": {"thinkingLevel": "medium"},
            "responseMimeType": "application/json",
            "responseJsonSchema": provider_schema(),
        },
    }


def _inline_payload_bytes(
    path,
    mime_type,
    title,
    duration,
    platform,
    transcript,
    windows,
):
    size = os.path.getsize(path)
    encoded_size = 4 * ((size + 2) // 3)
    placeholder = {"inline_data": {"mime_type": mime_type, "data": ""}}
    body = _request_body(
        placeholder,
        title,
        duration,
        platform,
        transcript,
        windows,
        "x" * 300,
    )
    return len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + encoded_size


def _media_part(
    path,
    mime_type,
    title,
    duration,
    platform,
    transcript,
    windows,
    api_key,
    deadline,
    heartbeat,
):
    size = os.path.getsize(path)
    if size <= 0 or size > MAX_MEDIA_BYTES:
        raise ValueError("Gemini reverse media size is outside the allowed range")
    projected = _inline_payload_bytes(
        path,
        mime_type,
        title,
        duration,
        platform,
        transcript,
        windows,
    )
    if (
        size <= INLINE_MAX_BYTES
        and float(duration or 0) <= INLINE_MAX_DURATION_SECONDS
        and projected <= INLINE_MAX_REQUEST_BYTES
    ):
        with open(path, "rb") as source:
            payload = source.read(INLINE_MAX_BYTES + 1)
        if len(payload) != size:
            raise ValueError("Gemini reverse media changed during read")
        return ({
            "inline_data": {
                "mime_type": mime_type,
                "data": base64.b64encode(payload).decode("ascii"),
            },
        }, None)
    uploaded = _upload_file(
        path,
        mime_type,
        api_key,
        deadline=deadline,
        heartbeat=heartbeat,
    )
    return ({
        "file_data": {
            "mime_type": mime_type,
            "file_uri": uploaded["uri"],
        },
    }, uploaded)


def _candidate_text(response):
    if not isinstance(response, dict):
        raise ValueError("Gemini response is not an object")
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise ValueError("Gemini response must contain exactly one candidate")
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise ValueError("Gemini candidate is invalid")
    finish_reason = str(candidate.get("finishReason") or "").upper()
    if finish_reason and finish_reason != "STOP":
        raise ValueError("Gemini response did not finish cleanly: %s" % finish_reason)
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list) or len(parts) != 1:
        raise ValueError("Gemini candidate must contain exactly one text part")
    text = parts[0].get("text") if isinstance(parts[0], dict) else None
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Gemini reverse output is empty")
    if len(text) > MAX_RESPONSE_BYTES:
        raise ValueError("Gemini reverse output exceeds the safety limit")
    return text.strip()


def _normalized_text(value):
    return re.sub(
        r"[\s，。；：、,.!！?？…~\-—_]+",
        "",
        str(value or ""),
    ).lower()


def _validate_fact_value(key, value, evidence, start, end, segment_id):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("第%d段字段%s为空" % (segment_id, key))
    value = value.strip()
    if len(value) > 600:
        raise ValueError("第%d段字段%s过长" % (segment_id, key))
    if value == NOT_APPLICABLE and key not in OPTIONAL_FACT_FIELDS:
        raise ValueError("第%d段必需字段%s不能为not_applicable" % (segment_id, key))
    if value not in {UNKNOWN, NOT_APPLICABLE} and SUBJECTIVE_PATTERN.search(value):
        raise ValueError(
            "第%d段字段%s包含无证据主观推断“%s”"
            % (segment_id, key, SUBJECTIVE_PATTERN.search(value).group(0))
        )
    if not isinstance(evidence, list) or len(evidence) > 3:
        raise ValueError(
            "第%d段字段%s的证据时间点必须为最多3项数组"
            % (segment_id, key)
        )
    normalized_evidence = []
    for point in evidence:
        if isinstance(point, bool) or not isinstance(point, (int, float)):
            raise ValueError("第%d段字段%s的证据时间点无效" % (segment_id, key))
        point = float(point)
        if point < float(start) - 0.11 or point > float(end) + 0.11:
            raise ValueError(
                "第%d段字段%s的证据时间点超出服务器区间"
                % (segment_id, key)
            )
        normalized_evidence.append(round(point, 3))
    if value in {UNKNOWN, NOT_APPLICABLE}:
        if normalized_evidence:
            raise ValueError(
                "第%d段字段%s为哨兵值时不能携带证据时间点"
                % (segment_id, key)
            )
    elif not normalized_evidence:
        raise ValueError(
            "第%d段字段%s缺少证据时间点" % (segment_id, key)
        )
    return value, normalized_evidence


def _validate_advice(advice, segment_id):
    if not isinstance(advice, dict) or set(advice) != set(GENERATION_ADVICE_FIELDS):
        raise ValueError("第%d段生成建议字段不完整" % segment_id)
    normalized = {}
    for key in GENERATION_ADVICE_FIELDS:
        value = advice.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("第%d段生成建议%s为空" % (segment_id, key))
        value = value.strip()
        if len(value) > 300:
            raise ValueError("第%d段生成建议%s过长" % (segment_id, key))
        if SUBJECTIVE_PATTERN.search(value):
            raise ValueError("第%d段生成建议包含无证据主观推断" % segment_id)
        normalized[key] = value
    return normalized


def _validate_readiness(facts, segment_id):
    applicable = [
        key for key in FACT_FIELDS
        if facts[key]["value"] != NOT_APPLICABLE
    ]
    ready = [
        key for key in applicable
        if facts[key]["value"] != UNKNOWN
    ]
    if not applicable or len(ready) / float(len(applicable)) < 0.90:
        missing = [key for key in applicable if key not in ready]
        raise ValueError(
            "第%d段生成就绪度不足90%%，未确认槽位：%s"
            % (segment_id, "、".join(missing) or "无")
        )
    critical_groups = {
        "主体": ("subject_identity", "subject_appearance", "position_scale"),
        "动作": ("action_start", "action_end"),
        "场景": ("foreground", "midground", "background"),
        "构图": ("shot_scale", "camera_angle", "composition"),
        "光影风格": ("lighting_color", "style_texture"),
    }
    for label, keys in critical_groups.items():
        if all(facts[key]["value"] in {UNKNOWN, NOT_APPLICABLE} for key in keys):
            raise ValueError("第%d段缺少%s核心事实" % (segment_id, label))
    return {
        "ready": len(ready),
        "applicable": len(applicable),
        "percent": round(100.0 * len(ready) / len(applicable), 1),
    }


def _duplicate_error(entries):
    groups = (
        ("subject_identity", "subject_appearance", "wardrobe", "position_scale"),
        ("action_start", "action_process", "action_end", "direction_speed"),
        ("foreground", "midground", "background"),
        ("shot_scale", "camera_angle", "camera_movement", "composition"),
        ("lighting_color", "style_texture", "rhythm"),
    )
    for current_index, current in enumerate(entries):
        current_groups = [
            "|".join(
                _normalized_text(current["facts"][key]["value"])
                for key in group
            )
            for group in groups
        ]
        for previous_index in range(current_index):
            previous = entries[previous_index]
            previous_groups = [
                "|".join(
                    _normalized_text(previous["facts"][key]["value"])
                    for key in group
                )
                for group in groups
            ]
            similarities = [
                SequenceMatcher(None, left, right).ratio()
                for left, right in zip(current_groups, previous_groups)
            ]
            whole_ratio = SequenceMatcher(
                None,
                "|".join(current_groups),
                "|".join(previous_groups),
            ).ratio()
            action_same = similarities[1] >= 0.92
            group_matches = sum(score >= 0.92 for score in similarities)
            if whole_ratio >= 0.96 or (action_same and group_matches >= 4):
                return ValueError(
                    "第%d段与第%d段内容重复"
                    % (current_index + 1, previous_index + 1)
                )
    return None


def parse_result(raw, windows, transcript=""):
    """Parse the complete JSON object. No extraction or salvage is allowed."""
    try:
        payload = json.loads(str(raw or ""))
    except Exception as error:
        raise ValueError("Gemini reverse output is not complete JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"shots"}:
        raise ValueError("Gemini reverse root must contain only shots")
    shots = payload.get("shots")
    if not isinstance(shots, list) or len(shots) != len(windows):
        raise ValueError(
            "Gemini shots count must be %d" % len(windows)
        )
    entries = []
    for index, (shot, window) in enumerate(zip(shots, windows), 1):
        if not isinstance(shot, dict) or set(shot) != {
            "segment_id", "facts", "generation_advice",
        }:
            raise ValueError("第%d段结构字段不完整" % index)
        if shot.get("segment_id") != index:
            raise ValueError("第%d段segment_id无效" % index)
        rows = shot.get("facts")
        if not isinstance(rows, list) or len(rows) != len(FACT_FIELDS):
            raise ValueError("第%d段facts必须恰好包含%d项" % (index, len(FACT_FIELDS)))
        keys = []
        facts = {}
        start, end, _label = window
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "key", "value", "evidence_seconds",
            }:
                raise ValueError("第%d段fact结构无效" % index)
            key = row.get("key")
            if key not in FACT_FIELDS:
                raise ValueError("第%d段包含未知fact字段" % index)
            keys.append(key)
            value, evidence = _validate_fact_value(
                key,
                row.get("value"),
                row.get("evidence_seconds"),
                start,
                end,
                index,
            )
            facts[key] = {"value": value, "evidence_seconds": evidence}
        if keys != list(FACT_FIELDS) or len(set(keys)) != len(FACT_FIELDS):
            raise ValueError("第%d段fact字段缺失、重复或顺序错误" % index)
        start_points = facts["action_start"]["evidence_seconds"]
        end_points = facts["action_end"]["evidence_seconds"]
        if facts["action_start"]["value"] not in {UNKNOWN, NOT_APPLICABLE} and not any(
            point <= float(start) + 0.25 for point in start_points
        ):
            raise ValueError("第%d段action_start缺少起点证据" % index)
        if facts["action_end"]["value"] not in {UNKNOWN, NOT_APPLICABLE} and not any(
            point >= float(end) - 0.25 for point in end_points
        ):
            raise ValueError("第%d段action_end缺少终点证据" % index)
        if not str(transcript or "").strip() and facts["sound"]["value"] not in {
            UNKNOWN,
            NOT_APPLICABLE,
        }:
            raise ValueError("第%d段声音缺少ASR证据" % index)
        readiness = _validate_readiness(facts, index)
        entries.append({
            "segment_id": index,
            "start_seconds": start,
            "end_seconds": end,
            "display_range": window[2],
            "facts": facts,
            "generation_advice": _validate_advice(
                shot.get("generation_advice"),
                index,
            ),
            "readiness": readiness,
        })
    duplicate = _duplicate_error(entries)
    if duplicate:
        raise duplicate
    return entries


def _visible(value):
    return "" if value in {UNKNOWN, NOT_APPLICABLE} else value


def _group(entry, keys):
    values = [
        _visible(entry["facts"][key]["value"])
        for key in keys
    ]
    return "；".join(value for value in values if value)


def assemble_prompt(entries):
    lines = []
    for entry in entries:
        advice = entry["generation_advice"]
        sections = (
            ("主体", _group(entry, (
                "subject_identity", "subject_appearance", "wardrobe", "position_scale",
            ))),
            ("动作", _group(entry, (
                "action_start", "action_process", "action_end", "direction_speed",
            ))),
            ("场景", _group(entry, ("foreground", "midground", "background"))),
            ("构图", _group(entry, (
                "shot_scale", "camera_angle", "camera_movement", "composition",
            ))),
            ("光影", _group(entry, ("lighting_color",))),
            ("风格", _group(entry, ("style_texture",))),
            ("节奏", _group(entry, ("rhythm",))),
            ("声音", _group(entry, ("sound",))),
            ("字幕", _group(entry, ("subtitles",))),
            ("连续性", _group(entry, ("continuity",))),
            ("生成建议", "画幅%s；帧率%s；镜头控制%s；负面提示%s" % (
                advice["aspect_ratio"],
                advice["fps"],
                advice["camera_control"],
                advice["negative_prompt"],
            )),
        )
        content = "；".join(
            "%s：%s" % (label, value)
            for label, value in sections
            if value
        )
        if not content:
            raise ValueError("第%d段可生成提示词为空" % entry["segment_id"])
        lines.append("%s %s" % (entry["display_range"], content))
    return "\n".join(lines)


def _audit_entry(entry):
    return {
        "segment_id": entry["segment_id"],
        "start_seconds": entry["start_seconds"],
        "end_seconds": entry["end_seconds"],
        "readiness": entry["readiness"],
        "evidence_seconds": {
            key: list(value["evidence_seconds"])
            for key, value in entry["facts"].items()
        },
    }


def analyze_video(
    path,
    mime_type,
    title,
    duration,
    platform,
    transcript,
    heartbeat=None,
    deadline=None,
):
    api_key = str(os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    if not path or not os.path.isfile(path):
        raise ValueError("Gemini reverse requires the original media file")
    windows = fixed_windows(duration)
    deadline = deadline or (time.monotonic() + ANALYSIS_BUDGET_SECONDS)
    uploaded = None
    attempt_audit = []
    try:
        media_part, uploaded = _media_part(
            path,
            mime_type,
            title,
            duration,
            platform,
            transcript,
            windows,
            api_key,
            deadline,
            heartbeat,
        )
        if uploaded:
            uploaded = _wait_for_file(
                uploaded,
                api_key,
                deadline=deadline,
                heartbeat=heartbeat,
            )
            media_part = {
                "file_data": {
                    "mime_type": mime_type,
                    "file_uri": uploaded["uri"],
                },
            }
        validation_error = ""
        for attempt in range(2):
            started = time.monotonic()
            raw = ""
            try:
                response = _json_request(
                    API_BASE + "/v1beta/models/" + MODEL + ":generateContent",
                    _request_body(
                        media_part,
                        title,
                        duration,
                        platform,
                        transcript,
                        windows,
                        validation_error,
                    ),
                    api_key,
                    deadline=deadline,
                    heartbeat=heartbeat,
                )
                raw = _candidate_text(response)
                entries = parse_result(raw, windows, transcript=transcript)
                prompt = assemble_prompt(entries)
                attempt_audit.append({
                    "attempt": attempt + 1,
                    "http_status": 200,
                    "response_chars": len(raw),
                    "response_sha256": _response_hash(raw),
                    "elapsed_ms": int(round((time.monotonic() - started) * 1000)),
                    "validation": "passed",
                })
                print(
                    "[breakdown] gemini reverse audit=%s"
                    % json.dumps(attempt_audit[-1], ensure_ascii=True),
                    flush=True,
                )
                return {
                    "provider": "google",
                    "model": MODEL,
                    "attempts": attempt + 1,
                    "windows": windows,
                    "entries": entries,
                    "prompt": prompt,
                    "attempt_audit": attempt_audit,
                    "cross_provider_fallback": False,
                }
            except ValueError as error:
                validation_error = _safe_text(error, 500)
                audit = {
                    "attempt": attempt + 1,
                    "http_status": 200,
                    "response_chars": len(raw),
                    "response_sha256": _response_hash(raw),
                    "elapsed_ms": int(round((time.monotonic() - started) * 1000)),
                    "validation": "failed",
                    "error": validation_error,
                }
                attempt_audit.append(audit)
                print(
                    "[breakdown] gemini reverse audit=%s"
                    % json.dumps(audit, ensure_ascii=True),
                    flush=True,
                )
                if attempt:
                    raise ValueError(
                        "Gemini反推结果校验失败：%s" % validation_error
                    ) from error
            except RuntimeError as error:
                safe_error = _safe_text(error, 500)
                status_match = re.search(r"Gemini HTTP (\d{3})", safe_error)
                audit = {
                    "attempt": attempt + 1,
                    "http_status": (
                        int(status_match.group(1)) if status_match else None
                    ),
                    "response_chars": 0,
                    "response_sha256": _response_hash(""),
                    "elapsed_ms": int(round((time.monotonic() - started) * 1000)),
                    "validation": "request_failed",
                    "error": safe_error,
                }
                attempt_audit.append(audit)
                print(
                    "[breakdown] gemini reverse audit=%s"
                    % json.dumps(audit, ensure_ascii=True),
                    flush=True,
                )
                raise
        raise ValueError("Gemini反推结果校验失败")
    finally:
        if uploaded:
            _delete_file(uploaded, api_key)
