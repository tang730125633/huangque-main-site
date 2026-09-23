"""Public text-style vocabulary; renderer owns layer/default/font availability."""
import copy
import math
import re

BOUNDS = {"font_size_px": (16, 240), "offset_x_px": (-60, 60), "offset_y_px": (-60, 60), "stroke_width_px": (0, 24)}
COLORS = {"color", "stroke_color"}
PROPERTIES = {**{key: {"type": "integer" if key == "font_size_px" else "number", "minimum": low, "maximum": high} for key, (low, high) in BOUNDS.items()},
              **{key: {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"} for key in sorted(COLORS)},
              "font_family": {"type": "string", "minLength": 1, "maxLength": 80}}
SCHEMA = {"type": "object", "maxProperties": 8, "additionalProperties": {
    "type": "object", "additionalProperties": False, "properties": PROPERTIES}}


def parse_controls(raw):
    if raw is None:
        return None
    if (not isinstance(raw, dict) or type(raw.get("contract_version")) is not int or raw.get("contract_version") != 1
            or raw.get("text_tunable") is not True
            or not re.fullmatch(r"[0-9a-f]{64}", str(raw.get("text_revision") or ""))
            or not isinstance(raw.get("layers"), dict) or not 1 <= len(raw["layers"]) <= 8
            or not isinstance(raw.get("fields"), dict)
            or set(raw["fields"]) != set(PROPERTIES)):
        raise RuntimeError("模板文字微调合同无效")
    fonts = raw["fields"].get("font_family", {}).get("enum")
    if not isinstance(fonts, list) or not 1 <= len(fonts) <= 100 or any(not isinstance(f, str) or not 1 <= len(f) <= 80 for f in fonts):
        raise RuntimeError("模板微调字体列表无效")
    for name, layer in raw["layers"].items():
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,31}", name) or not isinstance(layer, dict) or not isinstance(layer.get("defaults"), dict):
            raise RuntimeError("模板微调文字层无效")
    return copy.deepcopy(raw)


def normalize_values(value, controls=None):
    if value is None or value == {}:
        return {}
    if not isinstance(value, dict) or not 1 <= len(value) <= 8:
        raise ValueError("text_overrides 必须是文字层参数对象")
    result = {}
    for layer, values in sorted(value.items()):
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,31}", layer):
            raise ValueError("文字层名称无效")
        if controls is not None and layer not in controls["layers"]:
            raise ValueError("模板不支持文字层：" + layer)
        if not isinstance(values, dict) or set(values) - set(PROPERTIES):
            raise ValueError("文字层参数格式或字段无效：" + layer)
        cleaned = {}
        for key, item in sorted(values.items()):
            if key in BOUNDS:
                low, high = BOUNDS[key]
                if type(item) not in (int, float) or not low <= item <= high or not math.isfinite(item):
                    raise ValueError(f"{layer}.{key} 必须在 {low} 到 {high} 范围内")
                if key == "font_size_px" and item != int(item):
                    raise ValueError(f"{layer}.{key} 必须是整数像素")
                cleaned[key] = int(item) if key == "font_size_px" else round(float(item), 3)
            elif key in COLORS:
                if not isinstance(item, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", item):
                    raise ValueError(f"{layer}.{key} 必须是 #RRGGBB 颜色")
                cleaned[key] = item.upper()
            else:
                if not isinstance(item, str) or not 1 <= len(item) <= 80:
                    raise ValueError("字体名称无效")
                if controls is not None and item not in controls["fields"]["font_family"]["enum"]:
                    raise ValueError("字体不可用，请从模板参数返回的字体列表选择")
                cleaned[key] = item
        if cleaned:
            result[layer] = cleaned
    return result


def normalize_request(raw, controls):
    values = normalize_values(raw.get("text_overrides"), controls)
    if not values:
        return {}
    if not controls:
        raise ValueError("当前生成节点尚未开放文字微调，请稍后重试")
    if raw.get("overrides") or raw.get("preview_id"):
        raise ValueError("逐层文字微调不能与旧版 overrides 或 preview_id 混用")
    if raw.get("text_revision") != controls["text_revision"]:
        raise ValueError("文字样式版本不匹配，请重新读取模板可调参数 text_revision")
    return {"text_revision": controls["text_revision"], "text_overrides": values}
