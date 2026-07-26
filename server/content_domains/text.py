# -*- coding: utf-8 -*-
import re

from .core import COPY_MODEL, _post, json

SCRIPT_FACT_GUARD = (
    "只使用用户明确提供的产品、品牌、参数、检测结果和优惠信息。"
    "未提供品牌名时不得虚构品牌或安排必须展示品牌文字的镜头；"
    "未提供功效依据时不得使用“最、第一、顶级、100%、完全、绝对、根治、"
    "不怕晒黑、超强”等绝对化或无法证实的承诺。"
    "信息不足时使用中性、可核实的表达，不得自行补造数据。"
)


def validate_copy_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    cleaned = dict(payload)
    brief = str(cleaned.get("prompt") or "").strip()
    if not brief:
        raise ValueError("请输入文案需求")
    cleaned["prompt"] = brief
    return cleaned


_SCRIPT_CLAIM_REPLACEMENTS = (
    ("不必害怕阳光直射", "面对日常通勤光照时"),
    ("不怕晒黑", "帮助减少日晒影响"),
    ("全天候守护", "帮助进行日常防护"),
    ("必不可少", "值得重视"),
    ("毫无负担", "使用感更轻盈"),
    ("100%", "尽量"),
    ("完全不", "不易"),
    ("超强", "良好"),
    ("绝对", "相对"),
    ("顶级", "优质"),
    ("根治", "改善"),
)
_SCRIPT_OFFER_MARKERS = ("活动", "优惠", "折扣", "立减", "到手价", "限时", "名额", "超划算")


def sanitize_script_scenes(scenes, brief):
    brief = str(brief or "")
    has_offer_facts = any(marker in brief for marker in _SCRIPT_OFFER_MARKERS)
    has_brand_facts = "品牌" in brief
    cleaned = []
    for scene in scenes or []:
        item = dict(scene) if isinstance(scene, dict) else {}
        for field in ("scene", "line"):
            value = str(item.get(field) or "")
            for source, replacement in _SCRIPT_CLAIM_REPLACEMENTS:
                value = value.replace(source, replacement)
            if not has_brand_facts:
                value = value.replace("品牌名称", "产品包装").replace("品牌标识", "产品包装")
            if not has_offer_facts:
                value = re.sub(
                    r"[^。！？]*(?:活动|优惠|折扣|立减|到手价|限时|名额|超划算)[^。！？]*[。！？]?",
                    "如需了解更多，请以产品实际信息为准。",
                    value,
                )
            item[field] = value
        cleaned.append(item)
    return cleaned


def _chat(sysmsg, usermsg, temp):
    body = json.dumps({"model": COPY_MODEL,
                       "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": usermsg}],
                       "temperature": temp}).encode()
    d = _post("/v1/chat/completions", body, "application/json")
    return (d.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

def gen_copy(payload):
    payload = validate_copy_payload(payload)
    brief = payload["prompt"]
    ctype = (payload.get("ctype") or payload.get("type") or "通用").strip()
    if (payload.get("format") or "") == "short_drama":
        from . import short_drama
        settings = short_drama.validate_planning_payload(payload)
        raw = _chat(
            "你是黄雀传媒短剧编导。只输出 JSON 本身，不要解释，不要 markdown 代码块。",
            short_drama.build_plan_prompt(settings),
            0.75,
        )
        plan = short_drama.parse_and_normalize_plan(raw, settings)
        return {"type": "copy", "mode": "short_drama", "plan": plan,
                "project_id": settings.get("project_id"),
                "project_revision": settings.get("project_revision"),
                "settings": {"ratio": settings["ratio"],
                             "target_duration": settings["target_duration"],
                             "shot_count": settings["shot_count"]},
                "prompt": settings["prompt"], "dur": str(settings["target_duration"]) + "s",
                "ratio": settings["ratio"], "shot_count": settings["shot_count"]}
    # 编导：结构化分镜脚本（返回 scenes 数组）
    if (payload.get("format") or "") == "script":
        style = payload.get("style") or "口播"; dur = payload.get("dur") or "30s"; plat = payload.get("platform") or "抖音"
        raw = _chat("你是黄雀传媒资深短视频编导。只输出 JSON 本身，不要解释、不要 markdown 代码块。"
                    + SCRIPT_FACT_GUARD,
                    ("为以下选题生成一套可拍的%s短视频分镜脚本（平台%s，总时长约%s）。\n选题/卖点：%s\n"
                     "严格输出 JSON：{\"scenes\":[{\"dur\":\"3s\",\"scene\":\"画面描述\",\"line\":\"口播台词\"}]}，"
                     "3-4 个分镜，各 dur 之和≈总时长，口播口语化有钩子可直接念。\n事实约束：%s"
                     % (style, plat, dur, brief, SCRIPT_FACT_GUARD)), 0.85)
        s, e = raw.find("{"), raw.rfind("}"); scenes = []
        if s >= 0 and e > s:
            try: scenes = json.loads(raw[s:e+1]).get("scenes", [])
            except Exception: scenes = []
        if not scenes: raise ValueError("脚本解析失败，请重试")
        scenes = sanitize_script_scenes(scenes, brief)
        return {"type": "copy", "mode": "script", "scenes": scenes, "ctype": ctype,
                "style": style, "dur": dur, "platform": plat, "prompt": brief}
    # 通用文案（多条，--- 分隔）
    try: n = max(1, min(3, int(payload.get("n") or 2)))
    except Exception: n = 2
    text = _chat("你是黄雀传媒资深美业/电商营销文案。输出简体中文，口语化、有钩子、能转化。直接给文案本身，不要任何解释说明、不要前后缀。",
                 ("文案类型：%s\n需求/主题：%s\n请给 %d 条不同风格的文案，每条之间用单独一行「---」分隔；可适当用 emoji 和话题标签。" % (ctype, brief, n)), 0.9)
    if not text: raise ValueError("文案生成为空")
    return {"type": "copy", "ctype": ctype, "text": text, "prompt": brief}

HANDLERS = {"copy": gen_copy}
