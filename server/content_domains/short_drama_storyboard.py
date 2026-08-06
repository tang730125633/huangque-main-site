"""Story-beat and provider-prompt compiler for conversational short dramas.

The module is intentionally deterministic.  It produces an auditable, editable
storyboard contract and remains a safe fallback when no text-generation
provider is configured.
"""

import math
import re


SCHEMA_VERSION = "short-drama-conversation-script-v4"
MODEL_VERSION = "conversation-storyboard-v4"
PHASES = (
    ("setup", "建立", "交代时间、地点和人物当前处境"),
    ("reaction", "反应", "人物用一个可见动作回应刚发生的事件"),
    ("change", "变化", "用具体细节表现情绪或关系的变化"),
    ("conflict", "冲突", "把人物关系中的矛盾推到台前"),
    ("choice", "选择", "呈现人物必须作出的关键选择"),
    ("resolution", "收束", "回收前文细节并完成情绪落点"),
)
CAMERAS = {
    "setup": "环境建立镜头转中景，交代空间关系",
    "reaction": "中近景缓慢推近，捕捉即时反应",
    "change": "近景与细节特写交替，突出动作证据",
    "conflict": "双人中景或正反打，保持视线连续",
    "choice": "稳定近景，停留在决定发生的瞬间",
    "resolution": "克制的中近景缓慢拉远，保留情绪余味",
}
_QUOTE_RE = re.compile(r"[“\"]([^”\"]{1,80})[”\"]")
_PUNCTUATION_RE = re.compile(r"[。！？!?；;\n]+")
_NORMALIZE_RE = re.compile(r"[\W_]+", re.UNICODE)
_CHAT_FRAGMENTS = ("你的推荐", "剧情是怎么样", "怎么写", "怎么拍")


def split_story_clauses(values):
    clauses = []
    for value in values:
        for clause in _PUNCTUATION_RE.split(str(value or "")):
            clause = clause.strip(" ，、：:")
            if clause and clause not in clauses:
                clauses.append(clause)
    return clauses or ["故事发生"]


def allocate_durations(total_seconds, shot_count):
    total_seconds = max(int(total_seconds or 0), shot_count)
    shot_count = max(1, int(shot_count or 1))
    base_weights = (0.12, 0.14, 0.17, 0.18, 0.18, 0.21)
    if shot_count == 1:
        return [total_seconds]
    weights = []
    for index in range(shot_count):
        position = index * (len(base_weights) - 1) / float(shot_count - 1)
        left = int(math.floor(position))
        right = min(len(base_weights) - 1, left + 1)
        fraction = position - left
        weights.append(
            base_weights[left] * (1.0 - fraction)
            + base_weights[right] * fraction
        )
    weight_total = sum(weights)
    raw = [total_seconds * value / weight_total for value in weights]
    durations = [max(1, int(math.floor(value))) for value in raw]
    remaining = total_seconds - sum(durations)
    fractions = sorted(
        range(shot_count),
        key=lambda index: (raw[index] - math.floor(raw[index]), index),
        reverse=True,
    )
    while remaining > 0:
        for index in fractions:
            if remaining <= 0:
                break
            durations[index] += 1
            remaining -= 1
    while remaining < 0:
        for index in reversed(fractions):
            if remaining >= 0:
                break
            if durations[index] > 1:
                durations[index] -= 1
                remaining += 1
    return durations


def _phase(index, shot_count):
    if shot_count <= 1:
        return PHASES[-1]
    phase_index = int(round(index * (len(PHASES) - 1) / float(shot_count - 1)))
    return PHASES[min(len(PHASES) - 1, phase_index)]


def _clause(index, shot_count, clauses):
    if len(clauses) == 1 or shot_count <= 1:
        return clauses[0]
    clause_index = int(round(index * (len(clauses) - 1) / float(shot_count - 1)))
    return clauses[min(len(clauses) - 1, clause_index)]


def _location(clause, fallback):
    candidates = (
        "卧室", "房间", "客厅", "家中", "医院", "学校", "教室", "公园",
        "车站", "办公室", "街道", "天台", "餐厅", "雨夜", "清晨", "深夜",
    )
    return next((item for item in candidates if item in clause), fallback)


def _speaker(characters, clause, phase_key):
    mentioned = [item for item in characters if item["name"] in clause]
    if mentioned:
        if phase_key == "resolution" and len(mentioned) > 1:
            return mentioned[-1]
        return mentioned[0]
    return characters[min(len(characters) - 1, 1 if phase_key == "resolution" else 0)]


def _visible_characters(characters, clause, phase_key):
    mentioned = [item for item in characters if item["name"] in clause]
    if mentioned:
        return mentioned
    if phase_key in {"conflict", "resolution"}:
        return characters[:2]
    return characters[:1]


def _concrete_action(clause, phase_key):
    rules = (
        (("查分", "成绩", "分数"), "人物盯着刚刷新出的成绩页面，手指停住，呼吸短暂凝滞"),
        (("复读",), "人物面对家人强忍情绪，平静说出重新开始的决定"),
        (("掉泪", "落泪", "哭"), "人物关上房门后卸下伪装，眼泪落下"),
        (("照片", "相片"), "人物逐张取下墙上的照片，只保留与选择有关的一张"),
        (("字条", "纸条", "留言"), "人物写下简短字条，将它压在桌面醒目位置"),
        (("复诊单", "病历", "检查单"), "家人把字条和复诊单放在一起，意识到选择背后的原因"),
        (("支持",), "家人收起手中的纸张，走到人物身边给出明确支持"),
        (("重逢",), "两个人隔着雨幕认出彼此，脚步同时停下"),
        (("误会",), "人物拿出能证明往事的物件，让旧误会出现裂缝"),
        (("真相", "录音", "线索"), "人物播放或摊开关键证据，另一人的神情随信息改变"),
        (("选择", "决定"), "人物停下犹豫，完成一个不可撤回的选择动作"),
    )
    for keywords, action in rules:
        if any(keyword in clause for keyword in keywords):
            return action
    fallback = {
        "setup": "人物进入场景并与关键物件建立明确关系",
        "reaction": "人物停下手中动作，用表情和姿态回应刚发生的事",
        "change": "人物触碰一件关键物件，情绪在动作过程中发生变化",
        "conflict": "人物与关系对象正面相对，矛盾通过动作和距离显现",
        "choice": "人物完成一个能够体现决定的具体动作",
        "resolution": "人物回看前文关键物件，以克制动作结束当前事件",
    }
    return fallback[phase_key]


def _dialogue(clause, phase_key, speaker):
    quote = _QUOTE_RE.search(clause)
    if quote:
        quote_text = quote.group(1).strip()
        kind = "on_screen_text" if any(
            value in clause for value in ("字条", "纸条", "信", "屏幕")
        ) else "dialogue"
        return {
            "kind": kind,
            "character_key": "" if kind == "on_screen_text" else speaker["character_key"],
            "speaker": "画面文字" if kind == "on_screen_text" else speaker["name"],
            "text": quote_text,
        }
    choices = (
        (("复读",), "dialogue", "我想再考一年。"),
        (("照顾", "离家近"), "dialogue", "离家近一点，也挺好。"),
        (("支持",), "dialogue", "这一次，按你真正想走的路来。"),
        (("重逢",), "dialogue", "好久不见。"),
        (("误会",), "dialogue", "原来我们都误会了。"),
        (("真相",), "dialogue", "我终于明白了。"),
        (("选择", "决定"), "dialogue", "我已经想好了。"),
    )
    for keywords, kind, line in choices:
        if any(keyword in clause for keyword in keywords):
            return {
                "kind": kind,
                "character_key": speaker["character_key"],
                "speaker": speaker["name"],
                "text": line,
            }
    return {
        "kind": "silence",
        "character_key": "",
        "speaker": "",
        "text": "",
    }


def _reading_seconds(line):
    if line.get("kind") == "silence":
        return 0.0
    characters = len(re.sub(r"[\s，。！？、；：“”\"…]+", "", line.get("text") or ""))
    return round(0.45 + characters / 3.5, 2)


def _fit_dialogue_to_duration(line, duration_seconds):
    """Keep generated dialogue inside the shot before the quality gate runs."""
    if line.get("kind") == "silence" or _reading_seconds(line) <= duration_seconds:
        return line
    budget = max(0, int((float(duration_seconds) - 0.45) * 3.5))
    if budget < 2:
        return {
            "kind": "silence",
            "character_key": "",
            "speaker": "",
            "text": "",
        }
    kept = []
    counted = 0
    for character in str(line.get("text") or ""):
        if not re.match(r"[\s，。！？、；：“”\"…]", character):
            if counted >= budget:
                break
            counted += 1
        kept.append(character)
    fitted = dict(line)
    fitted["text"] = "".join(kept).rstrip("，；：、 ") + "…"
    return fitted


def _emotional_shift(phase_key):
    return {
        "setup": "平静或未知 → 意识到问题",
        "reaction": "震动 → 暂时掩饰",
        "change": "克制 → 情绪显露",
        "conflict": "回避 → 矛盾公开",
        "choice": "犹豫 → 作出决定",
        "resolution": "紧张 → 获得新的理解",
    }[phase_key]


def _provider_prompt(project, visible, scene, action, camera, phase_key):
    names = "、".join(item["name"] for item in visible) or "无人物空镜"
    return (
        "%s短剧镜头。%s，角色：%s。具体动作：%s。"
        "镜头设计：%s。情绪变化：%s。画幅%s，保持已绑定角色的脸部、"
        "发型、服装与相邻镜头连续，动作自然、光线真实、画面可拍摄。"
    ) % (
        project.get("visual_style") or "电影感写实",
        scene,
        names,
        action,
        camera,
        _emotional_shift(phase_key),
        project.get("ratio") or "16:9",
    )


def analyze_quality(script):
    blockers = []
    warnings = []
    shots = list(script.get("shots") or [])
    lines = {str(item.get("id")): item for item in script.get("dialogue_lines") or []}
    expected = int((script.get("overview") or {}).get("duration_seconds") or 0)
    actual = sum(int(item.get("duration_seconds") or 0) for item in shots)
    if not shots:
        blockers.append({"code": "shots_missing", "message": "至少需要一个镜头"})
    if expected != actual:
        blockers.append({
            "code": "duration_mismatch",
            "message": "镜头总时长必须等于目标时长",
        })
    normalized_visuals = [
        _NORMALIZE_RE.sub("", str(item.get("visual") or "")).lower()
        for item in shots
    ]
    if len(normalized_visuals) > 1 and len(set(normalized_visuals)) < len(normalized_visuals):
        blockers.append({"code": "duplicate_visual", "message": "存在重复镜头画面"})
    purposes = [str(item.get("purpose") or "").strip() for item in shots]
    if purposes and len(set(purposes)) < max(2, len(purposes) - 1):
        warnings.append({"code": "weak_story_progression", "message": "部分镜头剧情任务过于接近"})
    known_characters = {
        str(item.get("character_key") or "")
        for item in script.get("characters") or []
    }
    for shot in shots:
        if not str(shot.get("provider_prompt") or "").strip():
            blockers.append({
                "code": "provider_prompt_missing",
                "shot_key": shot.get("shot_key"),
                "message": "镜头缺少可执行的 Provider 提示词",
            })
        for line_id in shot.get("dialogue_line_ids") or []:
            line = lines.get(str(line_id))
            if not line:
                blockers.append({
                    "code": "dialogue_missing",
                    "shot_key": shot.get("shot_key"),
                    "message": "镜头引用的台词不存在",
                })
                continue
            if line.get("character_key") and line["character_key"] not in known_characters:
                blockers.append({
                    "code": "speaker_unknown",
                    "shot_key": shot.get("shot_key"),
                    "message": "台词说话人不在角色表中",
                })
            reading = _reading_seconds(line)
            if reading > float(shot.get("duration_seconds") or 0):
                blockers.append({
                    "code": "dialogue_too_long",
                    "shot_key": shot.get("shot_key"),
                    "message": "台词超过镜头可用时长",
                    "reading_seconds": reading,
                })
        if len(str(shot.get("visual") or "")) > 180:
            warnings.append({
                "code": "visual_too_dense",
                "shot_key": shot.get("shot_key"),
                "message": "单镜头动作描述较密，建议人工检查可拍性",
            })
    status = "blocked" if blockers else ("warning" if warnings else "pass")
    return {
        "status": status,
        "blockers": blockers,
        "warnings": warnings,
        "metrics": {
            "shot_count": len(shots),
            "duration_seconds": actual,
            "silent_shots": sum(
                1 for item in lines.values() if item.get("kind") == "silence"
            ),
            "provider_ready_shots": sum(
                1 for item in shots if str(item.get("provider_prompt") or "").strip()
            ),
        },
    }


def compile_storyboard(project, clauses, characters, instruction="", ending="", understanding=None):
    understanding = understanding or {}
    shot_count = max(1, int(project.get("shot_count") or 1))
    durations = allocate_durations(project.get("target_duration"), shot_count)
    title = str(project.get("title") or "未命名短剧")
    logline = str(
        understanding.get("creative_brief")
        or project.get("synopsis")
        or "故事发生"
    ).strip()
    story_beats = []
    shots = []
    dialogue_lines = []
    previous_scene = ""
    used_dialogue = set()
    for index in range(shot_count):
        phase_key, phase_name, purpose = _phase(index, shot_count)
        source = _clause(index, shot_count, clauses)
        scene = _location(source, previous_scene or "故事主要场景")
        previous_scene = scene
        visible = _visible_characters(characters, source, phase_key)
        speaker = _speaker(characters, source, phase_key)
        action = _concrete_action(source, phase_key)
        camera = CAMERAS[phase_key]
        line = _fit_dialogue_to_duration(
            _dialogue(source, phase_key, speaker), durations[index]
        )
        normalized_line = _NORMALIZE_RE.sub("", str(line.get("text") or "")).lower()
        if normalized_line and normalized_line in used_dialogue:
            line = {
                "kind": "silence",
                "character_key": "",
                "speaker": "",
                "text": "",
            }
        elif normalized_line:
            used_dialogue.add(normalized_line)
        line_id = "draft_line_%02d" % (index + 1)
        line.update({
            "id": line_id,
            "estimated_reading_seconds": _reading_seconds(line),
        })
        dialogue_lines.append(line)
        beat_key = "beat_%02d" % (index + 1)
        story_beats.append({
            "beat_key": beat_key,
            "sort_order": index + 1,
            "phase": phase_key,
            "label": phase_name,
            "purpose": purpose,
            "source_fact": source[:180],
            "action": action,
            "emotional_shift": _emotional_shift(phase_key),
        })
        visual = "%s；剧情事实：%s；本镜头重点：%s" % (
            action,
            source[:100],
            purpose,
        )
        shots.append({
            "shot_key": "shot_%02d" % (index + 1),
            "sort_order": index + 1,
            "duration_seconds": durations[index],
            "scene": scene,
            "beat": phase_name,
            "beat_key": beat_key,
            "purpose": purpose,
            "visual": visual,
            "camera": camera,
            "continuity": (
                "承接上一镜头的时间、服装、角色位置和关键道具"
                if index else "建立本场时间、空间、服装和关键道具基准"
            ),
            "character_keys": [item["character_key"] for item in visible],
            "dialogue_line_ids": [line_id],
            "provider_prompt": _provider_prompt(
                project, visible, scene, visual, camera, phase_key
            ),
            "negative_prompt": "字幕、文字、Logo、水印、多余人物、脸部漂移、服装突变、违背物理的动作",
            "locked": False,
        })
    act_indices = (0, max(0, shot_count // 2), shot_count - 1)
    acts = [
        {"act": 1, "name": "人物与事件", "summary": story_beats[act_indices[0]]["source_fact"]},
        {"act": 2, "name": "冲突与选择", "summary": story_beats[act_indices[1]]["source_fact"]},
        {
            "act": 3,
            "name": "情绪收束",
            "summary": "%s；结尾方向：%s" % (
                story_beats[act_indices[2]]["source_fact"],
                ending or "有情绪余味的收束",
            ),
        },
    ]
    scenes = [
        {
            "scene": index + 1,
            "location": shot["scene"],
            "summary": shot["visual"],
        }
        for index, shot in enumerate(shots)
    ]
    script = {
        "schema_version": SCHEMA_VERSION,
        "overview": {
            "title": title,
            "logline": logline[:280],
            "theme": str(instruction or understanding.get("tone") or "人物选择与关系变化")[:160],
            "duration_seconds": sum(durations),
            "ratio": project.get("ratio"),
            "visual_style": project.get("visual_style"),
        },
        "characters": characters,
        "acts": acts,
        "scenes": scenes,
        "story_beats": story_beats,
        "dialogue_lines": dialogue_lines,
        "shots": shots,
    }
    script["quality_gate"] = analyze_quality(script)
    return script


def validate_script(script):
    shots = script.get("shots") or []
    lines = script.get("dialogue_lines") or []
    if not shots or len(shots) != len(lines):
        return {
            "status": "blocked",
            "blockers": [{"code": "script_structure_invalid", "message": "镜头与台词结构不完整"}],
            "warnings": [],
            "metrics": {},
        }
    normalized_lines = [
        _NORMALIZE_RE.sub("", str(item.get("text") or "")).lower()
        for item in lines
        if str(item.get("text") or "").strip()
    ]
    if (
        len(normalized_lines) > 1
        and len(set(normalized_lines)) < max(2, len(normalized_lines) // 2)
    ):
        return {
            "status": "blocked",
            "blockers": [{"code": "script_dialogue_repeated", "message": "镜头台词重复度过高"}],
            "warnings": [],
            "metrics": {},
        }
    if any(
        any(fragment in str(item.get("text") or "") for fragment in _CHAT_FRAGMENTS)
        for item in lines
    ):
        return {
            "status": "blocked",
            "blockers": [{"code": "script_dialogue_contains_chat", "message": "剧本台词混入了创作对话"}],
            "warnings": [],
            "metrics": {},
        }
    return analyze_quality(script)
