"""Data-only 口播网感模板 contract. Mirrored verbatim into the public HQ client."""
import copy
import math
import re

TEMPLATE_ID = "ip-editorial-serif-v1"
TEMPLATE_NAME = "口播网感模板"
TEMPLATE_VERSION = "1.0.0"
HYPERFRAMES_VERSION = "0.8.33"
SCHEMA_ID = "hq.editorial-plan/v1"
LEGACY_TEMPLATES = ("viral-talking-head-v1", "professional-explainer-v1", "clean-talking-v1")
TEMPLATE_SCHEMA = {"type": "string", "enum": [*LEGACY_TEMPLATES, TEMPLATE_ID]}


def _object(properties, required=None):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(properties) if required is None else required}


def _array(items, minimum, maximum):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


def _text_schema(maximum):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


TIME = {"type": "number", "minimum": 0, "maximum": 180}
PLAN_SCHEMA = _object({
    "schema": {"type": "string", "enum": [SCHEMA_ID]},
    "timebase": {"type": "string", "enum": ["edited_output"]},
    "transcript_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "edit_decision_version": {"type": "integer", "minimum": 1},
    "title": _array(_text_schema(16), 2, 2),
    "captions": _array(_object({"start": TIME, "end": TIME,
                                "text": _text_schema(24), "en": _text_schema(58)}), 1, 500),
    "keywords": _array(_text_schema(10), 0, 50),
    "camera": _array(_object({"at": TIME,
                              "scale": {"type": "number", "minimum": 1, "maximum": 1.24},
                              "transition": {"type": "string", "enum": ["cut", "whip"]}}), 1, 30),
    "keyword_punches": _array(_object({"at": TIME, "word": _text_schema(10),
                                      "strength": {"type": "number", "minimum": 1.04,
                                                   "maximum": 1.1}}), 0, 30),
    "callouts": _array(_object({"start": TIME, "end": TIME, "text": _text_schema(5),
                                "side": {"type": "string", "enum": ["left", "right"]}}), 0, 20),
})


def _validate(value, rule, path):
    kind = rule["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) - set(rule["properties"]):
            raise ValueError(path + ": unsupported fields or invalid object")
        if set(rule["required"]) - set(value):
            raise ValueError(path + ": missing required fields")
        for key, child in value.items():
            _validate(child, rule["properties"][key], path + "." + key)
    elif kind == "array":
        if not isinstance(value, list) or not rule["minItems"] <= len(value) <= rule["maxItems"]:
            raise ValueError(path + ": invalid item count")
        for index, child in enumerate(value):
            _validate(child, rule["items"], "%s[%d]" % (path, index))
    elif kind == "string":
        if not isinstance(value, str) or not value.strip() or any(
                ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError(path + ": invalid text")
        if len(value) > rule.get("maxLength", 256):
            raise ValueError(path + ": text too long")
        if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
            raise ValueError(path + ": invalid format")
    elif kind in {"integer", "number"}:
        types = (int,) if kind == "integer" else (int, float)
        if isinstance(value, bool) or not isinstance(value, types) or (
                isinstance(value, float) and not math.isfinite(value)):
            raise ValueError(path + ": finite number required")
        if value < rule.get("minimum", 0) or value > rule.get("maximum", 2**63 - 1):
            raise ValueError(path + ": number outside range")
    if "enum" in rule and value not in rule["enum"]:
        raise ValueError(path + ": unsupported value")


def width_units(value):
    return sum(0.58 if ord(c) < 128 else 1 for c in value)


def validate_plan(plan, duration=180):
    """No filesystem, shell, network, imports, or user-controlled executable content."""
    _validate(plan, PLAN_SCHEMA, "editorial_plan")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 3 <= duration <= 180:
        raise ValueError("口播网感模板只支持 3–180 秒视频")
    if any(width_units(line) > 12 for line in plan["title"]):
        raise ValueError("标题超出安全宽度，请拆为两行短标题")
    previous_end = 0
    for cue in plan["captions"]:
        if cue["start"] < previous_end or cue["end"] - cue["start"] < .08 - 1e-8 or cue["end"] > duration:
            raise ValueError("双语字幕时间重叠、过短或超出片长")
        if width_units(cue["text"]) > 10.5:
            raise ValueError("中文字幕过长，请在上游按短句拆分")
        if not re.search("[A-Za-z]", cue["en"]) or re.search("[\u4e00-\u9fff]", cue["en"]):
            raise ValueError("英文字幕缺少有效翻译，不能用中文占位")
        previous_end = cue["end"]
    for word in plan["keywords"]:
        if not any(word in c["text"] for c in plan["captions"]):
            raise ValueError("强调词必须来自本片字幕")
    if plan["camera"][0]["at"] != 0:
        raise ValueError("第一景别必须从 0 秒开始")
    previous = -6
    for shot in plan["camera"]:
        if shot["at"] < previous + 6 or shot["at"] > duration - 1:
            raise ValueError("景别切换间隔至少 6 秒且不可超过片尾")
        previous = shot["at"]
    previous = -1.5
    for punch in plan["keyword_punches"]:
        at = punch["at"]
        if at < previous + 1.5 or at + .67 > duration + 1e-8:
            raise ValueError("关键词推近时间过密或超出片尾")
        if punch["word"] not in plan["keywords"] or not any(
                cue["start"] - .08 <= at <= cue["end"] and punch["word"] in cue["text"]
                for cue in plan["captions"]):
            raise ValueError("关键词推近缺少对应字幕证据")
        if any(at < shot["at"] + .3 and at + .67 > shot["at"] for shot in plan["camera"][1:]):
            raise ValueError("关键词推近不能与景别切换重叠")
        base = next(shot["scale"] for shot in reversed(plan["camera"]) if shot["at"] <= at)
        if base * punch["strength"] > 1.33:
            raise ValueError("组合缩放超过 1.33")
        previous = at
    previous_end = 0
    for cue in plan["callouts"]:
        if cue["start"] < previous_end or not .4 - 1e-8 <= cue["end"] - cue["start"] <= 4 or cue["end"] > duration:
            raise ValueError("侧边词卡时间无效")
        previous_end = cue["end"]
    return copy.deepcopy(plan)


def validate_selection(payload):
    """Validate optional render selection without needing project/database access."""
    selected = payload.get("template_id")
    if "template_id" in payload:
        _validate(selected, TEMPLATE_SCHEMA, "template_id")
    if selected == TEMPLATE_ID:
        return validate_plan(payload.get("editorial_plan"))
    if "editorial_plan" in payload:
        raise ValueError("editorial_plan 仅用于口播网感模板")
    return None
