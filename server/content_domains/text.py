# -*- coding: utf-8 -*-
from .core import COPY_MODEL, _post, json

def _chat(sysmsg, usermsg, temp):
    body = json.dumps({"model": COPY_MODEL,
                       "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": usermsg}],
                       "temperature": temp}).encode()
    d = _post("/v1/chat/completions", body, "application/json")
    return (d.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

def gen_copy(payload):
    brief = (payload.get("prompt") or "").strip()
    if not brief:
        raise ValueError("请输入文案需求")
    ctype = (payload.get("ctype") or payload.get("type") or "通用").strip()
    # 编导：结构化分镜脚本（返回 scenes 数组）
    if (payload.get("format") or "") == "script":
        style = payload.get("style") or "口播"; dur = payload.get("dur") or "30s"; plat = payload.get("platform") or "抖音"
        raw = _chat("你是黄雀传媒资深短视频编导。只输出 JSON 本身，不要解释、不要 markdown 代码块。",
                    ("为以下选题生成一套可拍的%s短视频分镜脚本（平台%s，总时长约%s）。\n选题/卖点：%s\n"
                     "严格输出 JSON：{\"scenes\":[{\"dur\":\"3s\",\"scene\":\"画面描述\",\"line\":\"口播台词\"}]}，"
                     "3-4 个分镜，各 dur 之和≈总时长，口播口语化有钩子可直接念。" % (style, plat, dur, brief)), 0.85)
        s, e = raw.find("{"), raw.rfind("}"); scenes = []
        if s >= 0 and e > s:
            try: scenes = json.loads(raw[s:e+1]).get("scenes", [])
            except Exception: scenes = []
        if not scenes: raise ValueError("脚本解析失败，请重试")
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
