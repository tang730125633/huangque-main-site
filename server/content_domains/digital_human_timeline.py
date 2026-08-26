# -*- coding: utf-8 -*-
"""Authoritative planning for the duration-driven digital-human workflow.

The customer's authorized portrait is used directly for every presenter
appearance.  Presenter appearances are derived from the expected final
duration and are placed at narration boundaries roughly every 20-30 seconds.
"""
import hashlib
import json
import math
import re


PIPELINE = "digital_human_material_v2"
WORKFLOW_VERSION = 3
MAX_SCRIPT_CHARS = 6000
MAX_DURATION_SECONDS = 180.0
MIN_AUDIO_SECONDS = 6.0
TARGET_APPEARANCE_INTERVAL = 25.0
MIN_APPEARANCE_INTERVAL = 20.0
MAX_APPEARANCE_INTERVAL = 30.0
PRESENTER_WINDOW_SECONDS = 3.0
TARGET_MATERIAL_SECONDS = 5.5
MAX_MATERIAL_COUNT = 40
# Customer reference images are handled before remote retrieval by the browser
# workflow.  Keeping that first step in the authoritative plan makes the full
# material policy explicit and binds it into plan_digest.
SOURCE_PRIORITY = ("customer_reference", "feishu", "public_web", "ai")


class TimelinePlanError(ValueError):
    def __init__(self, message, code="invalid_digital_human_plan", status=400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


def clean_script(value):
    text = re.sub(r"[ \t\r\f\v]+", " ", str(value or ""))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 12:
        raise TimelinePlanError("口播文案太短，请至少输入 12 个字")
    if len(text) > MAX_SCRIPT_CHARS:
        raise TimelinePlanError("口播文案最多支持 6000 个字")
    return text


def _sentences(text):
    chunks = [part.strip() for part in re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", text)]
    return [part for part in chunks if part]


def _speech_units(text):
    """Estimate Mandarin narration length without trusting a client duration."""
    compact = re.sub(r"\s+", "", text)
    latin_words = re.findall(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*", compact)
    latin_chars = sum(len(word) for word in latin_words)
    cjk_like = max(0, len(compact) - latin_chars)
    return float(cjk_like) + len(latin_words) * 1.8


def estimate_duration(text):
    # 4.15 Chinese characters/s is a natural explainer pace.  Punctuation
    # pauses are included separately so cuts land conservatively.
    units = _speech_units(text)
    pauses = len(re.findall(r"[。！？!?；;，,：:]", text)) * 0.16
    duration = max(MIN_AUDIO_SECONDS, units / 4.15 + pauses)
    if duration > MAX_DURATION_SECONDS:
        raise TimelinePlanError(
            "预计口播超过 180 秒，请缩短文案后再生成",
            "digital_human_duration_exceeded",
        )
    return round(duration, 3)


def narration_segment_count(duration):
    duration = float(duration)
    if duration <= MAX_APPEARANCE_INTERVAL:
        return 1
    return max(2, int(math.ceil(duration / TARGET_APPEARANCE_INTERVAL)))


def _split_by_weight(text, count):
    if count <= 1:
        return [text]
    sentences = _sentences(text)
    if len(sentences) < count:
        boundaries = [round(len(text) * index / float(count)) for index in range(count + 1)]
        return [text[boundaries[index]:boundaries[index + 1]].strip()
                for index in range(count)]
    weights = [_speech_units(sentence) for sentence in sentences]
    cumulative = []
    cursor = 0.0
    for weight in weights:
        cursor += weight
        cumulative.append(cursor)
    cuts = []
    previous = 0
    for group_index in range(1, count):
        remaining = count - group_index
        candidates = range(previous + 1, len(sentences) - remaining + 1)
        cut = min(candidates, key=lambda value: abs(
            cumulative[value - 1] - cursor * group_index / float(count)
        ))
        cuts.append(cut)
        previous = cut
    starts = [0] + cuts
    ends = cuts + [len(sentences)]
    return ["".join(sentences[start:end]).strip() for start, end in zip(starts, ends)]


def _normalize_durations(parts, total_duration):
    weights = [max(0.1, _speech_units(part)) for part in parts]
    weight_total = sum(weights)
    raw = [float(total_duration) * value / weight_total for value in weights]
    if len(raw) == 1:
        return [round(float(total_duration), 3)]
    # Clamp every presenter-to-presenter interval to 20-30 seconds. The final
    # narration tail may be shorter because the dedicated ending window is
    # added independently; forcing that tail to 20 seconds would move a 31-39
    # second video's middle appearance too early.
    normalized = []
    remaining = float(total_duration)
    for index, value in enumerate(raw):
        slots_left = len(raw) - index - 1
        if slots_left == 0:
            normalized.append(round(remaining, 3))
            break
        low = MIN_APPEARANCE_INTERVAL
        high = min(MAX_APPEARANCE_INTERVAL, remaining - slots_left * 8.0)
        chosen = min(high, max(low, value))
        normalized.append(round(chosen, 3))
        remaining -= chosen
    return normalized


def presenter_windows(segment_durations, total_duration):
    total_duration = float(total_duration)
    starts = [0.0]
    cursor = 0.0
    for duration in segment_durations[:-1]:
        cursor += float(duration)
        starts.append(cursor)
    windows = []
    for start in starts:
        end = min(total_duration, start + PRESENTER_WINDOW_SECONDS)
        if end - start > 0.05:
            windows.append([round(start, 3), round(end, 3)])
    ending = [round(max(0.0, total_duration - PRESENTER_WINDOW_SECONDS), 3), round(total_duration, 3)]
    if not windows or ending[0] > windows[-1][1] + 0.05:
        windows.append(ending)
    else:
        windows[-1][1] = ending[1]
    return windows


def _material_intervals(windows, total_duration):
    intervals = []
    cursor = 0.0
    for start, end in windows:
        if start > cursor + 0.05:
            intervals.append([round(cursor, 3), round(start, 3)])
        cursor = max(cursor, end)
    if cursor < total_duration - 0.05:
        intervals.append([round(cursor, 3), round(total_duration, 3)])
    return intervals


def material_slots(windows, total_duration, count=None):
    intervals = _material_intervals(windows, total_duration)
    available = sum(end - start for start, end in intervals)
    if not intervals:
        if count is not None and (type(count) is not int or count != 0):
            raise TimelinePlanError("全程真人画面不接受正文素材镜头")
        return []
    if count is None:
        count = min(MAX_MATERIAL_COUNT, max(1, int(math.ceil(available / TARGET_MATERIAL_SECONDS))))
    elif type(count) is not int or count < 1 or count > MAX_MATERIAL_COUNT:
        raise TimelinePlanError("正文素材镜头数量无效")
    slots = []
    remaining_slots = count
    remaining_duration = available
    slot_index = 0
    for interval_index, (start, end) in enumerate(intervals):
        span = end - start
        if interval_index == len(intervals) - 1:
            local_count = remaining_slots
        else:
            local_count = max(1, round(remaining_slots * span / max(remaining_duration, 0.001)))
            local_count = min(local_count, remaining_slots - (len(intervals) - interval_index - 1))
        step = span / float(local_count)
        for local_index in range(local_count):
            slot_start = start + local_index * step
            slot_end = end if local_index == local_count - 1 else start + (local_index + 1) * step
            slots.append({
                "index": slot_index,
                "start": round(slot_start, 3),
                "end": round(slot_end, 3),
                "duration": round(slot_end - slot_start, 3),
            })
            slot_index += 1
        remaining_slots -= local_count
        remaining_duration -= span
    return slots


def _digest(value):
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_text(script):
    copy = clean_script(script)
    duration = estimate_duration(copy)
    part_count = narration_segment_count(duration)
    parts = _split_by_weight(copy, part_count)
    durations = _normalize_durations(parts, duration)
    windows = presenter_windows(durations, duration)
    planned_material_slots = material_slots(windows, duration)
    infographic_limit = 1 if duration < 75 else 2
    infographic_indexes = set()
    if planned_material_slots:
        infographic_indexes.add(max(0, len(planned_material_slots) // 3))
        if infographic_limit == 2:
            infographic_indexes.add(min(
                len(planned_material_slots) - 1,
                (len(planned_material_slots) * 2) // 3,
            ))
    segments = []
    cursor = 0.0
    roles = ("hook", "explain", "cta")
    for index, (part, part_duration) in enumerate(zip(parts, durations)):
        segments.append({
            "index": index,
            "text": part,
            "start": round(cursor, 3),
            "end": round(cursor + part_duration, 3),
            "duration": part_duration,
            "role": roles[min(index, 2)] if len(parts) <= 3 else ("hook" if index == 0 else "cta" if index == len(parts) - 1 else "explain"),
        })
        cursor += part_duration
    excerpts = _sentences(copy) or [copy]
    materials = []
    for slot in planned_material_slots:
        excerpt = excerpts[slot["index"] % len(excerpts)]
        scene_type = "infographic" if slot["index"] in infographic_indexes else (
            "video" if slot["index"] % 3 == 1 else "image"
        )
        prompt_prefix = (
            "为竖屏知识短视频制作一张简洁的信息图表，只展示本段关键关系，"
            if scene_type == "infographic" else
            "为竖屏知识短视频制作真实、自然、具有现场感的内容画面，"
        )
        materials.append(dict(slot, **{
            "scene_type": scene_type,
            "material_query": re.sub(r"\s+", " ", excerpt)[:220],
            "prompt": prompt_prefix + "不要出现数字人口播人物、文字水印或品牌标识。画面准确表达：" + excerpt[:220],
            "source_priority": list(SOURCE_PRIORITY),
        }))
    core = {
        "pipeline": PIPELINE,
        "workflow_version": WORKFLOW_VERSION,
        "narration_mode": "text",
        "copy": copy,
        "ratio": "9:16",
        "expected_duration": duration,
        "segments": segments,
        "presenter_windows": windows,
        "materials": materials,
        "infographic_limit": infographic_limit,
        "source_priority": list(SOURCE_PRIORITY),
    }
    return dict(core, segment_count=len(segments), material_count=len(materials), plan_digest=_digest(core))


def plan_response(payload):
    if not isinstance(payload, dict):
        raise TimelinePlanError("请求体必须是 JSON 对象")
    allowed = {"script", "copy", "text", "narration_mode"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise TimelinePlanError("方案提交包含不支持字段：" + ", ".join(unknown))
    mode = str(payload.get("narration_mode") or "text").strip().lower()
    if mode != "text":
        raise TimelinePlanError(
            "录音驱动请先上传完整音频后再分析",
            "audio_upload_required",
        )
    result = plan_text(payload.get("script") or payload.get("copy") or payload.get("text"))
    return {"ok": True, "plan": result}
