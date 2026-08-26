"""Planning and preference rules for the standalone AI Creator Agent."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request


ALLOWED_PLATFORMS = ("douyin", "xiaohongshu", "wechat_channels")
PLATFORM_LABELS = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "wechat_channels": "视频号",
}
MVP_ACTIONS = frozenset({"matrix-template-generate"})
_LONG_TERM_RE = re.compile(r"(?:以后|今后|默认|一直|每次|我(?:更)?喜欢|我(?:更)?偏好|不要再|都要|都不要)")


class PlannerError(RuntimeError):
    pass


def sanitize_platforms(values):
    result = []
    for value in values or []:
        key = str(value or "").strip()
        if key in ALLOWED_PLATFORMS and key not in result:
            result.append(key)
    return result


def sanitize_preferences(value):
    value = value if isinstance(value, dict) else {}
    global_items = value.get("global") if isinstance(value.get("global"), list) else []
    platform_value = value.get("platforms") if isinstance(value.get("platforms"), dict) else {}

    def clean(items):
        result = []
        for item in items or []:
            text = re.sub(r"\s+", " ", str(item or "")).strip()[:240]
            if text and text not in result:
                result.append(text)
        return result[-30:]

    return {
        "global": clean(global_items),
        "platforms": {key: clean(platform_value.get(key)) for key in ALLOWED_PLATFORMS},
    }


def remember_preference(preferences, message):
    """Persist only explicit long-term language; one-off instructions stay local."""
    current = sanitize_preferences(preferences)
    text = re.sub(r"\s+", " ", str(message or "")).strip()[:240]
    if not text or not _LONG_TERM_RE.search(text):
        return current, False
    mentioned = [key for key, label in PLATFORM_LABELS.items() if label in text]
    targets = [current["platforms"][key] for key in mentioned] if mentioned else [current["global"]]
    changed = False
    for target in targets:
        if text in target:
            continue
        target.append(text)
        del target[:-30]
        changed = True
    return current, changed


def clear_preferences(preferences, platform=""):
    current = sanitize_preferences(preferences)
    if platform in ALLOWED_PLATFORMS:
        current["platforms"][platform] = []
    else:
        current = sanitize_preferences({})
    return current


def preference_context(preferences, platform):
    value = sanitize_preferences(preferences)
    return value["global"] + value["platforms"].get(platform, [])


def _clean_text(value, maximum):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]


def _template_ids(templates):
    return [
        str(item.get("id") or "") for item in templates or []
        if isinstance(item, dict) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(item.get("id") or ""))
    ]


def _choose_template(templates, platform):
    ids = _template_ids(templates)
    preferred = {
        "douyin": ("native-bold", "kinetic-punch"),
        "xiaohongshu": ("minimal-headline", "soft-editorial", "native-bold"),
        "wechat_channels": ("editorial-clean", "native-bold"),
    }[platform]
    return next((item for item in preferred if item in ids), ids[0] if ids else "native-bold")


class GuidedPlanner:
    def video_plan(self, ip12_context, topic, platforms, templates, preferences):
        topic = _clean_text(topic, 120) or "本次主题"
        plans = []
        for platform in sanitize_platforms(platforms):
            label = PLATFORM_LABELS[platform]
            if platform == "douyin":
                top = "%s：先看这一步" % topic
                bottom = "关注我，持续分享可执行的方法"
                reason = "抖音优先用直接结论和明确行动，便于快速停留。"
            elif platform == "xiaohongshu":
                top = "%s｜这份经验请收好" % topic
                bottom = "先收藏，下一次直接照着做"
                reason = "小红书更适合经验感标题和收藏型行动引导。"
            else:
                top = "%s，关键在这里" % topic
                bottom = "关注视频号，继续看真实经验"
                reason = "视频号使用稳健表达，突出可信度和持续价值。"
            remembered = preference_context(preferences, platform)
            if remembered:
                reason += " 已结合%s偏好。" % label
            plans.append({
                "platform": platform,
                "platform_label": label,
                "top_text": top[:60],
                "bottom_text": bottom[:80],
                "template_id": _choose_template(templates, platform),
                "template_reason": reason[:160],
                "content_goal": "根据画像与本次主题建立信任并引导后续互动",
                "material_pack": {"id": "platform-default-" + platform, "name": label + "平台素材库"},
            })
        return {"goal": "建立信任并引导互动", "platform_plans": plans}

    def revise_video_plan(self, current, instruction, templates, preferences):
        instruction = _clean_text(instruction, 400)
        mentioned = [key for key, label in PLATFORM_LABELS.items() if label in instruction]
        targets = set(mentioned or [str(item.get("platform") or "") for item in current or []])
        top_match = re.search(r"(?:顶部标题|标题)(?:改成|换成|使用|用)[：: ]*[“\"']?([^；;。\n]{2,60})", instruction)
        bottom_match = re.search(r"(?:底部文案|行动文案|底部)(?:改成|换成|使用|用)[：: ]*[“\"']?([^；;。\n]{2,80})", instruction)
        plans = []
        for item in current or []:
            item = dict(item)
            if item.get("platform") in targets and top_match:
                item["top_text"] = top_match.group(1).strip(" ”\"'")[:60]
            if item.get("platform") in targets and bottom_match:
                item["bottom_text"] = bottom_match.group(1).strip(" ”\"'")[:80]
            if instruction:
                item["template_reason"] = (
                    _clean_text(item.get("template_reason"), 120) + " 已按本次要求调整。"
                )[:160]
            plans.append(item)
        return {"goal": "按用户反馈调整", "platform_plans": plans}

    def reply(self, ip12_context, message):
        return "我已读取当前画像。你可以开始制作视频、生成选题计划，或告诉我需要修改哪部分画像。"


class OpenAICompatiblePlanner:
    def __init__(self, base_url, api_key, model, timeout=90):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self.model = str(model or "")
        self.timeout = max(10, min(180, int(timeout)))

    def _call(self, system, context):
        if not self.base_url or not self.api_key or not self.model:
            raise PlannerError("creator agent model is not configured")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            "temperature": 0.35,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
            return json.loads(result["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError, urllib.error.URLError) as exc:
            raise PlannerError("creator agent model request failed") from exc

    def video_plan(self, ip12_context, topic, platforms, templates, preferences):
        system = (
            "你是黄雀 AI 创作助手的模板视频策划器。根据已确认 IP12 画像和用户选题，"
            "为每个平台分别生成可直接进入模板成片的方案。只返回 JSON：goal 和 platform_plans。"
            "platform_plans 每项必须包含 platform、top_text、bottom_text、template_id、"
            "template_reason、content_goal。禁止增加平台，禁止编造画像事实。"
            "顶部标题 2-60 字，底部行动文案 2-80 字；模板只能从 templates 选择。"
            "抖音、小红书、视频号必须采用各自适合的表达，平台偏好优先于全局偏好。"
        )
        return self._call(system, {
            "ip12_profile": ip12_context,
            "topic": _clean_text(topic, 400),
            "platforms": sanitize_platforms(platforms),
            "templates": templates,
            "preferences": sanitize_preferences(preferences),
        })

    def revise_video_plan(self, current, instruction, templates, preferences):
        system = (
            "你是模板视频方案修改器。只修改用户明确要求的字段，保留原平台集合。"
            "只返回 JSON：goal 和 platform_plans；字段与原方案一致，template_id 只能来自 templates。"
        )
        return self._call(system, {
            "current_plans": current,
            "instruction": _clean_text(instruction, 1200),
            "templates": templates,
            "preferences": sanitize_preferences(preferences),
        })

    def reply(self, ip12_context, message):
        system = (
            "你是黄雀 AI 创作助手。只围绕当前 IP12 画像、选题文案和模板视频回答。"
            "回复简短、具体，每次最多提出一个问题；不得声称已生成、已扣点或已完成任务。"
            "返回 JSON：reply。"
        )
        return self._call(system, {
            "ip12_profile": ip12_context,
            "message": _clean_text(message, 4000),
        })


class CreatorPlanner:
    def __init__(self, provider=None, fallback=None):
        self.provider = provider
        self.fallback = fallback or GuidedPlanner()

    @classmethod
    def from_environment(cls, environment=None):
        environment = environment or os.environ
        base = environment.get("CREATOR_AGENT_BASE_URL", "")
        key = environment.get("CREATOR_AGENT_API_KEY", "")
        model = environment.get("CREATOR_AGENT_MODEL", "")
        provider = OpenAICompatiblePlanner(base, key, model) if base and key and model else None
        return cls(provider=provider)

    def video_plan(self, ip12_context, topic, platforms, templates, preferences):
        value = None
        if self.provider:
            try:
                value = self.provider.video_plan(ip12_context, topic, platforms, templates, preferences)
            except PlannerError:
                value = None
        if value is None:
            value = self.fallback.video_plan(ip12_context, topic, platforms, templates, preferences)
        return self._validate_video_plan(value, platforms, templates)

    def revise_video_plan(self, current, instruction, templates, preferences):
        value = None
        if self.provider:
            try:
                value = self.provider.revise_video_plan(current, instruction, templates, preferences)
            except PlannerError:
                value = None
        if value is None:
            value = self.fallback.revise_video_plan(current, instruction, templates, preferences)
        return self._validate_video_plan(value, [item.get("platform") for item in current], templates)

    def reply(self, ip12_context, message):
        if self.provider:
            try:
                value = self.provider.reply(ip12_context, message)
                reply = _clean_text((value or {}).get("reply"), 1200)
                if reply:
                    return reply
            except PlannerError:
                pass
        return self.fallback.reply(ip12_context, message)

    @staticmethod
    def _validate_video_plan(value, platforms, templates):
        if not isinstance(value, dict):
            raise PlannerError("video plan must be an object")
        expected = sanitize_platforms(platforms)
        raw_items = value.get("platform_plans")
        if not isinstance(raw_items, list):
            raise PlannerError("platform plans are required")
        template_ids = set(_template_ids(templates))
        if not template_ids:
            raise PlannerError("template catalog is empty")
        by_platform = {}
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            platform = str(raw.get("platform") or "")
            if platform not in expected or platform in by_platform:
                continue
            top = _clean_text(raw.get("top_text"), 60)
            bottom = _clean_text(raw.get("bottom_text"), 80)
            template_id = str(raw.get("template_id") or "")
            if not 2 <= len(top) <= 60 or not 2 <= len(bottom) <= 80:
                raise PlannerError("platform copy length is invalid")
            if template_id not in template_ids:
                template_id = _choose_template(templates, platform)
            by_platform[platform] = {
                "platform": platform,
                "platform_label": PLATFORM_LABELS[platform],
                "top_text": top,
                "bottom_text": bottom,
                "template_id": template_id,
                "template_reason": _clean_text(raw.get("template_reason"), 160),
                "content_goal": _clean_text(raw.get("content_goal"), 120),
                "material_pack": {
                    "id": "platform-default-" + platform,
                    "name": PLATFORM_LABELS[platform] + "平台素材库",
                },
                "input": {
                    "top_text": top,
                    "bottom_text": bottom,
                    "template_id": template_id,
                },
            }
        if set(by_platform) != set(expected):
            raise PlannerError("platform plans are incomplete")
        return {
            "goal": _clean_text(value.get("goal"), 160),
            "platform_plans": [by_platform[key] for key in expected],
        }
