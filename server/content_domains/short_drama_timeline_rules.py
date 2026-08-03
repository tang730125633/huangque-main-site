"""Pure validation and stale rules for the short-drama master timeline."""


SPEAKING_MODES = {"visible", "offscreen", "narration"}
WRITABLE_STAGES = {"voice_review", "video_review"}

_MESSAGES = {
    "timeline_segment_overlap": "同一时间点只能有一个画面内说话人",
    "timeline_out_of_bounds": "说话区间超出镜头或项目时长",
    "timeline_missing_character": "说话角色不存在或已失效",
    "timeline_missing_face_target": "画面内说话必须绑定可见角色",
    "timeline_voice_not_locked": "配音版本未锁定或已经失效",
    "timeline_alignment_stale": "字幕对齐版本未锁定或已经失效",
    "timeline_legacy_incomplete": "历史项目缺少构建主时间轴所需证据",
    "timeline_stage_readonly": "当前短剧阶段只能查看主时间轴",
    "timeline_handoff_not_ready": "主时间轴尚未确认或已经失效",
    "timeline_speaker_identity_unverified": (
        "历史时间轴缺少镜头角色快照，请核对迁移后的说话模式并确认"
    ),
    "timeline_invalid_segment": "说话区间字段无效",
    "timeline_invalid_subtitle": "字幕时间区间无效",
}


def blocker(code, *, shot_id=None, line_id=None, segment_id=None, **details):
    item = {"code": code, "message": _MESSAGES[code]}
    if shot_id:
        item["shot_id"] = shot_id
    if line_id:
        item["line_id"] = line_id
    if segment_id:
        item["segment_id"] = segment_id
    item.update({key: value for key, value in details.items() if value is not None})
    return item


def validate_timeline(
        duration_ms, shot_bounds, characters, segments, subtitle_cues,
        *, dependencies_ready=True):
    blockers = []
    bounds = {
        str(item["shot_id"]): (int(item["start_ms"]), int(item["end_ms"]))
        for item in shot_bounds
    }
    character_keys = {str(item) for item in characters}
    visible = []
    seen_ids = set()
    for segment in segments:
        segment_id = str(segment.get("id") or "")
        shot_id = str(segment.get("shot_id") or "")
        line_id = str(segment.get("line_id") or "")
        mode = str(segment.get("speaking_mode") or "")
        start_ms = segment.get("start_ms")
        end_ms = segment.get("end_ms")
        if (
            not segment_id or segment_id in seen_ids or not shot_id or not line_id
            or mode not in SPEAKING_MODES or type(start_ms) is not int
            or type(end_ms) is not int or start_ms < 0 or end_ms <= start_ms
        ):
            blockers.append(blocker(
                "timeline_invalid_segment", shot_id=shot_id, line_id=line_id,
                segment_id=segment_id,
            ))
            continue
        seen_ids.add(segment_id)
        shot_range = bounds.get(shot_id)
        if (
            not shot_range or start_ms < shot_range[0]
            or end_ms > shot_range[1] or end_ms > duration_ms
        ):
            blockers.append(blocker(
                "timeline_out_of_bounds", shot_id=shot_id, line_id=line_id,
                segment_id=segment_id,
            ))
        character_key = str(segment.get("character_key") or "")
        if mode != "narration" and character_key not in character_keys:
            blockers.append(blocker(
                "timeline_missing_character", shot_id=shot_id, line_id=line_id,
                segment_id=segment_id,
            ))
        if mode == "visible":
            target = segment.get("face_target")
            if (
                not isinstance(target, dict)
                or target.get("type") != "character"
                or str(target.get("value") or "") != character_key
            ):
                blockers.append(blocker(
                    "timeline_missing_face_target", shot_id=shot_id,
                    line_id=line_id, segment_id=segment_id,
                ))
            visible.append((start_ms, end_ms, segment_id, shot_id, line_id))
        elif segment.get("face_target") not in (None, {}):
            blockers.append(blocker(
                "timeline_invalid_segment", shot_id=shot_id, line_id=line_id,
                segment_id=segment_id,
            ))
    visible.sort()
    for previous, current in zip(visible, visible[1:]):
        if current[0] < previous[1]:
            blockers.append(blocker(
                "timeline_segment_overlap", shot_id=current[3],
                line_id=current[4], segment_id=current[2],
                overlaps_segment_id=previous[2],
            ))
    for cue in subtitle_cues:
        shot_id = str(cue.get("shot_id") or "")
        start_ms, end_ms = cue.get("start_ms"), cue.get("end_ms")
        shot_range = bounds.get(shot_id)
        if (
            type(start_ms) is not int or type(end_ms) is not int
            or start_ms < 0 or end_ms <= start_ms or not shot_range
            or start_ms < shot_range[0] or end_ms > shot_range[1]
            or end_ms > duration_ms
        ):
            blockers.append(blocker(
                "timeline_invalid_subtitle", shot_id=shot_id,
                line_id=str(cue.get("line_id") or ""),
            ))
    if not dependencies_ready:
        blockers.append(blocker("timeline_voice_not_locked"))
    return blockers


def stale_impact(previous_hashes, current_hashes):
    changed = sorted(
        key for key in set(previous_hashes) | set(current_hashes)
        if previous_hashes.get(key) != current_hashes.get(key)
    )
    impact = set()
    for key in changed:
        if key in {"transcript_hash", "master_audio_hash"}:
            impact.update({"subtitles", "lipsync", "preview"})
        elif key == "alignment_hash":
            impact.update({"subtitles", "preview"})
        elif key == "speaker_hash":
            impact.update({"lipsync", "preview"})
        elif key == "visual_hash":
            impact.update({"lipsync", "preview"})
    return {"changed_sources": changed, "downstream": sorted(impact)}
