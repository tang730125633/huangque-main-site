# -*- coding: utf-8 -*-
import os
import urllib.error
import urllib.request

from .core import COPY_MODEL, OPENAI_BASE, OPENAI_KEY, json


COPY_API_BASE = os.environ.get("COPY_API_BASE", "").strip()
COPY_API_KEY = os.environ.get("COPY_API_KEY", "").strip()


def _provider_config():
    dedicated_base = str(COPY_API_BASE or "").strip()
    dedicated_key = str(COPY_API_KEY or "").strip()
    if bool(dedicated_base) != bool(dedicated_key):
        raise RuntimeError("COPY_API_BASE 与 COPY_API_KEY 必须同时配置，不能只配置其中一项")
    if dedicated_base:
        return dedicated_base, dedicated_key, "COPY_API_BASE", "COPY_API_KEY"
    return (
        str(OPENAI_BASE or "").strip(), str(OPENAI_KEY or "").strip(),
        "OPENAI_BASE", "OPENAI_API_KEY",
    )


def _chat_url(base, base_env):
    base = str(base or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("文案模型接口未配置，请检查 %s" % base_env)
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _http_error_message(status, base_env, key_env):
    if status in (401, 403):
        return "文案模型鉴权失败，请检查 %s" % key_env
    if status == 404:
        return "文案模型接口或模型不存在，请检查 %s 和 COPY_MODEL" % base_env
    if status == 429:
        return "文案模型请求过于频繁，请稍后重试"
    if status >= 500:
        return "文案模型服务暂时不可用，请稍后重试"
    return "文案模型请求失败（HTTP %s）" % status


def _post_chat(body):
    base, key, base_env, key_env = _provider_config()
    if not key:
        raise RuntimeError("文案模型密钥未配置，请检查 %s" % key_env)
    request = urllib.request.Request(
        _chat_url(base, base_env), data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError(_http_error_message(error.code, base_env, key_env)) from error

def _chat(sysmsg, usermsg, temp):
    body = json.dumps({"model": COPY_MODEL,
                       "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": usermsg}],
                       "temperature": temp}).encode()
    d = _post_chat(body)
    return (d.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

def gen_copy(payload):
    brief = (payload.get("prompt") or "").strip()
    if not brief:
        raise ValueError("请输入文案需求")
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
