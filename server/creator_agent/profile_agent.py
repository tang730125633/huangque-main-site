"""Independent profile interview powered by DeepSeek V4 Flash."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request


class ProfileAgentError(RuntimeError):
    pass


def _required_text(value, field, maximum, minimum=1):
    if not isinstance(value, str):
        raise ProfileAgentError("DeepSeek field %s must be text" % field)
    text = re.sub(r"\s+", " ", value).strip()
    if not minimum <= len(text) <= maximum:
        raise ProfileAgentError("DeepSeek field %s has invalid length" % field)
    return text


def _required_content(value, field, maximum, minimum=1):
    if not isinstance(value, str):
        raise ProfileAgentError("DeepSeek field %s must be text" % field)
    text = value.strip()
    if not minimum <= len(text) <= maximum:
        raise ProfileAgentError("DeepSeek field %s has invalid length" % field)
    return text


def _required_text_list(value, field, maximum_items=6, maximum_length=240):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum_items:
        raise ProfileAgentError("DeepSeek field %s must be a non-empty list" % field)
    result = [
        _required_text(item, "%s[%d]" % (field, index), maximum_length)
        for index, item in enumerate(value)
    ]
    if len(set(result)) != len(result):
        raise ProfileAgentError("DeepSeek field %s contains duplicates" % field)
    return result


def _profile_option(value, index):
    if not isinstance(value, dict):
        raise ProfileAgentError("DeepSeek profile option must be an object")
    prefix = "options[%d]" % index
    return {
        "title": _required_text(value.get("title"), prefix + ".title", 80),
        "one_liner": _required_text(
            value.get("one_liner"), prefix + ".one_liner", 300, minimum=2,
        ),
        "strengths": _required_text_list(
            value.get("strengths"), prefix + ".strengths",
        ),
        "risks": _required_text_list(value.get("risks"), prefix + ".risks"),
    }


MODULES = {
    1: {
        "name": "定位诊断",
        "questions": (
            {"key": "identity", "question": "先介绍一下你现在的身份、工作或正在做的事情。", "template": "我是___，目前主要在做___。"},
            {"key": "turning_points", "question": "你经历过哪些重要转折？请先讲一段最影响你的真实经历。", "template": "当时___，我遇到___，后来___，这让我___。"},
            {"key": "skills", "question": "你最能帮助别人的两项能力是什么？最好各举一个结果。", "template": "能力1：___，曾经做到___；能力2：___，曾经做到___。"},
            {"key": "interests_values", "question": "哪些方向你愿意长期投入？你最看重的原则是什么？", "template": "我长期关注___；我坚持___，不愿意___。"},
            {"key": "audience_problem", "question": "你最想帮助哪类人？他们现在最具体的困难是什么？", "template": "我想帮助___，他们常遇到___，希望最终___。"},
        ),
    },
    2: {
        "name": "人设塑造",
        "questions": (
            {"key": "communication_style", "question": "你希望别人感受到怎样的你？可以选择，也可以自己描述。", "options": ["专业可靠", "真实亲切", "犀利直接"], "template": "我希望呈现___，但不要显得___。"},
            {"key": "future_identity", "question": "未来三年，你希望自己因为什么被记住？", "template": "我希望成为___领域里，能够___的人。"},
        ),
    },
    3: {
        "name": "价值主张",
        "questions": (
            {"key": "desired_impression", "question": "别人提到你时，你最希望他们第一句话怎么评价你？", "template": "他/她是那个能帮我___的人。"},
            {"key": "proof", "question": "你有哪些真实经历、方法或结果，能证明这件事？", "template": "我的证明包括：经历___；方法___；结果___。"},
        ),
    },
    4: {
        "name": "故事资产",
        "questions": (
            {"key": "core_story", "question": "请讲一个最能代表你的完整故事：起点、冲突、转折和结果分别是什么？", "template": "起点___；冲突___；转折___；结果___。"},
            {"key": "story_meaning", "question": "你希望这个故事让目标用户感受到什么，并记住你什么品质？", "template": "我希望他们感受到___，记住我的___，相信___。"},
        ),
    },
}


def initial_state():
    return {
        "version": 1,
        "revision": 1,
        "current_module": 1,
        "question_index": 0,
        "phase": "collecting",
        "completed_modules": [],
        "answers": {},
        "module_reviews": {},
        "selected_profiles": {},
        "profile_ready": False,
    }


def current_question(state):
    module = max(1, min(4, int(state.get("current_module") or 1)))
    questions = MODULES[module]["questions"]
    index = max(0, min(len(questions) - 1, int(state.get("question_index") or 0)))
    return {"module": module, "module_name": MODULES[module]["name"], **questions[index]}


class DeepSeekProfileAgent:
    def __init__(self, api_key, base_url="https://api.deepseek.com",
                 model="deepseek-v4-flash", timeout=90, opener=None):
        self.api_key = str(api_key or "")
        self.base_url = str(base_url or "https://api.deepseek.com").rstrip("/")
        self.model = str(model or "deepseek-v4-flash")
        self.timeout = max(10, min(180, int(timeout)))
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._health_at = 0.0
        self._health_ready = False

    @property
    def configured(self):
        return bool(
            self.api_key
            and self.base_url == "https://api.deepseek.com"
            and self.model == "deepseek-v4-flash"
        )

    def health(self, force=False):
        now = time.monotonic()
        if not force and now - self._health_at < 60:
            return self._health_ready
        if not self.configured:
            self._health_at, self._health_ready = now, False
            return False
        request = urllib.request.Request(
            self.base_url + "/models",
            headers={"Authorization": "Bearer " + self.api_key, "Accept": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                result = json.load(response)
            models = {
                str(item.get("id") or "") for item in result.get("data") or []
                if isinstance(item, dict)
            }
            ready = self.model in models
        except (TypeError, ValueError, urllib.error.URLError, OSError):
            ready = False
        self._health_at, self._health_ready = now, ready
        return ready

    def _call(self, system, payload):
        if not self.configured:
            raise ProfileAgentError("DeepSeek V4 Flash is not configured")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "thinking": {"type": "disabled"},
            "temperature": 0.45,
            "response_format": {"type": "json_object"},
            "max_tokens": 2400,
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.load(response)
            value = json.loads(result["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError, urllib.error.URLError, OSError) as exc:
            raise ProfileAgentError("DeepSeek V4 Flash request failed") from exc
        if not isinstance(value, dict):
            raise ProfileAgentError("DeepSeek returned invalid JSON")
        return value

    def capture_answer(self, state, message):
        question = current_question(state)
        value = self._call(
            "你是黄雀独立个人画像 Agent。只理解用户对当前问题的回答，不编造事实。"
            "返回 JSON：accepted(bool)、value(string)、ack(string)、clarification(string)。"
            "回答含有效事实时 accepted=true；过于空泛才追问。value 是忠实、简洁的事实记录。",
            {
                "current_question": question,
                "known_answers": state.get("answers") or {},
                "user_answer": str(message or "")[:4000],
            },
        )
        if not isinstance(value.get("accepted"), bool):
            raise ProfileAgentError("DeepSeek accepted must be boolean")
        accepted = value["accepted"]
        captured = _required_text(
            value.get("value"), "value", 1600,
            minimum=1 if accepted else 0,
        )
        if accepted and not captured:
            accepted = False
        ack = _required_text(value.get("ack"), "ack", 300)
        clarification = _required_text(
            value.get("clarification"), "clarification", 300,
            minimum=0 if accepted else 1,
        )
        return {
            "accepted": accepted,
            "value": captured,
            "ack": ack,
            "clarification": clarification,
        }

    def build_module_review(self, state, module):
        name = MODULES[module]["name"]
        value = self._call(
            "你是个人品牌策略顾问。只能使用已提供的真实回答，不得补造经历、成绩或身份。"
            "返回 JSON：summary(string)、options(array)。模块1-3提供3个差异化候选，"
            "每项包含非空 title、one_liner，以及 strengths、risks 两个1-6项字符串数组；"
            "模块4只提供1-2条主线候选。",
            {
                "module": module,
                "module_name": name,
                "all_answers": state.get("answers") or {},
                "confirmed_modules": state.get("selected_profiles") or {},
            },
        )
        raw_options = value.get("options")
        if not isinstance(raw_options, list):
            raise ProfileAgentError("DeepSeek module review is incomplete")
        if module < 4 and len(raw_options) != 3:
            raise ProfileAgentError("DeepSeek module review requires three options")
        if module == 4 and not 1 <= len(raw_options) <= 2:
            raise ProfileAgentError("DeepSeek story review requires one or two options")
        options = [_profile_option(item, index) for index, item in enumerate(raw_options)]
        summary = _required_text(value.get("summary"), "summary", 6000, minimum=2)
        return {"module": module, "module_name": name, "summary": summary, "options": options}

    def revise_module_review(self, state, module, instruction):
        current = (state.get("module_reviews") or {}).get(str(module)) or {}
        value = self._call(
            "根据用户修改要求更新当前画像模块。只能修改用户指出的内容，不得编造新事实。"
            "返回与 current_review 相同结构的 JSON：summary、options；每项必须保留非空"
            "title、one_liner 以及 strengths、risks 字符串数组。",
            {
                "module": module,
                "answers": state.get("answers") or {},
                "current_review": current,
                "instruction": str(instruction or "")[:2000],
            },
        )
        raw_options = value.get("options")
        if not isinstance(raw_options, list):
            raise ProfileAgentError("DeepSeek revised review is incomplete")
        if module < 4 and len(raw_options) != 3:
            raise ProfileAgentError("DeepSeek revised review requires three options")
        if module == 4 and not 1 <= len(raw_options) <= 2:
            raise ProfileAgentError("DeepSeek revised story review requires one or two options")
        options = [_profile_option(item, index) for index, item in enumerate(raw_options)]
        return {
            "module": module,
            "module_name": MODULES[module]["name"],
            "summary": _required_text(value.get("summary"), "summary", 6000, minimum=2),
            "options": options,
        }

    def topic_plan(self, profile, platforms, request):
        value = self._call(
            "你是多平台内容策划 Agent。基于已确认个人画像，为指定平台生成至少15个可执行选题，"
            "并推荐3个。只返回 JSON：reply、topics、recommended、scripts。"
            "topics 每项为含非空 title 的对象；recommended 是引用 topic title 的字符串数组；"
            "scripts 每项必须包含请求内 platform 和非空 content。用户明确要求文案时，scripts "
            "给出可直接发布的完整文案；不得编造画像事实。",
            {"profile": profile, "platforms": platforms, "request": str(request or "")[:4000]},
        )
        raw_topics = value.get("topics")
        if not isinstance(raw_topics, list) or not 15 <= len(raw_topics) <= 50:
            raise ProfileAgentError("DeepSeek topic plan is incomplete")
        topics = []
        topic_titles = set()
        for index, item in enumerate(raw_topics):
            if not isinstance(item, dict):
                raise ProfileAgentError("DeepSeek topic must be an object")
            title = _required_text(item.get("title"), "topics[%d].title" % index, 160, 2)
            if title in topic_titles:
                raise ProfileAgentError("DeepSeek topic titles must be unique")
            topic_titles.add(title)
            normalized = {"title": title}
            for field in ("angle", "reason", "format"):
                if field in item:
                    normalized[field] = _required_text(
                        item.get(field), "topics[%d].%s" % (index, field), 500,
                    )
            topics.append(normalized)

        raw_recommended = value.get("recommended")
        if not isinstance(raw_recommended, list) or not 3 <= len(raw_recommended) <= 10:
            raise ProfileAgentError("DeepSeek recommendations are incomplete")
        recommended = []
        for index, item in enumerate(raw_recommended):
            title = item.get("title") if isinstance(item, dict) else item
            title = _required_text(title, "recommended[%d]" % index, 160, 2)
            if title not in topic_titles or title in recommended:
                raise ProfileAgentError("DeepSeek recommendation does not match topics")
            recommended.append(title)

        raw_scripts = value.get("scripts")
        if not isinstance(raw_scripts, list) or len(raw_scripts) > 12:
            raise ProfileAgentError("DeepSeek scripts must be a list")
        allowed_platforms = {str(item) for item in platforms}
        scripts = []
        for index, item in enumerate(raw_scripts):
            if not isinstance(item, dict):
                raise ProfileAgentError("DeepSeek script must be an object")
            platform = _required_text(
                item.get("platform"), "scripts[%d].platform" % index, 40,
            )
            if platform not in allowed_platforms:
                raise ProfileAgentError("DeepSeek script platform is not requested")
            script = {
                "platform": platform,
                "content": _required_content(
                    item.get("content"), "scripts[%d].content" % index, 12000, 2,
                ),
            }
            if "title" in item:
                script["title"] = _required_text(
                    item.get("title"), "scripts[%d].title" % index, 160,
                )
            scripts.append(script)
        return {
            "reply": _required_text(value.get("reply"), "reply", 1600, 2),
            "topics": topics,
            "recommended": recommended,
            "scripts": scripts,
        }

    def reply(self, profile, message):
        value = self._call(
            "你是黄雀独立创作 Agent。围绕用户已确认画像、内容策划和模板视频简洁回答。"
            "不要声称调用 IP12，不要代替用户确认付费。返回 JSON：reply。",
            {"profile": profile, "message": str(message or "")[:4000]},
        )
        return _required_text(value.get("reply"), "reply", 1600, 2)
