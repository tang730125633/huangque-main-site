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
            {"key": "basic_context", "question": "了解用户希望使用的姓名或昵称、年龄阶段和所在城市。", "template": "你可以介绍昵称、年龄阶段和所在城市；不想公开的可以跳过。"},
            {"key": "career_identity", "question": "了解当前职业、身份和主要在做的事情。", "template": "我目前是___，主要负责或正在做___。", "quality": "必须是人的真实职业或角色，并说明正在做的工作；不能只回答‘我是AI’。"},
            {"key": "career_history", "question": "了解从业年限以及做过的行业和岗位轨迹。", "template": "我从业___年，先后做过___。"},
            {"key": "income_context", "question": "可选了解主要收入来源和收入阶段，用于判断商业化基础。", "template": "主要收入来自___；收入阶段可选：10万以下、10-30万、30-50万、50-100万、100万以上。", "options": ["暂不透露", "10万以下", "10-30万", "30-50万", "50-100万", "100万以上"]},
            {"key": "low_point", "question": "了解人生中最大的挫折或低谷，以及如何走出来。", "template": "当时发生了___，我跌到___，后来通过___走了出来。", "quality": "必须同时包含真实处境和走出来的行动，只有‘找不到工作’等结果不够。"},
            {"key": "achievement", "question": "了解最有成就感且有具体结果的一件事。", "template": "我做过___，最终获得了___结果。", "quality": "必须包含做了什么和可核实的结果。"},
            {"key": "praised_traits", "question": "了解别人最常称赞的能力或特质。", "template": "别人经常夸我___，通常因为___。"},
            {"key": "criticized_traits", "question": "了解别人最常吐槽的缺点或争议点。", "template": "别人常说我___，我认为其中___。", "quality": "必须是真实缺点、争议或负面反馈；赞美、自夸或玩笑不能当作批评。"},
            {"key": "proven_ability", "question": "了解经过实战验证的最强能力及证据。", "template": "我最擅长___，曾经做到___。", "quality": "必须同时包含能力和真实案例或结果证据。"},
            {"key": "content_track", "question": "了解计划长期创作的行业或内容赛道。", "template": "我想长期做___赛道，主要因为___。"},
            {"key": "target_audience", "question": "明确最具体的目标受众。", "template": "我想服务___人群，他们通常处于___阶段。"},
            {"key": "audience_pain", "question": "明确能为目标受众解决的1-3个核心痛点。", "template": "他们最困扰的是___；我能帮助他们___。"},
            {"key": "differentiation", "question": "明确为什么目标用户应该相信和选择该用户。", "template": "与其他人相比，我的真实差异是___，证据是___。", "quality": "必须说明具体差异和证据；只有‘有成功案例’不够。"},
            {"key": "existing_accounts", "question": "了解已有内容平台、粉丝规模、运营时长和现状。", "template": "我目前在___平台有账号，约___粉丝，运营了___；没有也可以直接说明。"},
        ),
    },
    2: {
        "name": "人设塑造",
        "questions": (
            {"key": "personality_words", "question": "用三个真实词语概括用户的性格。", "template": "三个词：___、___、___。", "quality": "必须提供三个不同且真实的性格词。"},
            {"key": "communication_style", "question": "了解希望呈现的说话风格，可选择1-2个并补充。", "options": ["犀利直接", "温暖走心", "专业理性", "幽默轻松"], "template": "我希望表达偏___，但不要显得___。"},
            {"key": "disliked_style", "question": "了解特别不喜欢的博主风格和原因。", "template": "我不喜欢___类型，因为___。"},
            {"key": "content_habits", "question": "了解朋友圈、聊天和日常发内容的习惯与禁忌。", "template": "我平时爱发___，不爱发___，绝不会___。"},
        ),
    },
    3: {
        "name": "价值主张",
        "questions": (
            {"key": "memorable_statement", "question": "了解最想让别人记住的一句话或核心信念。", "template": "我最希望别人记住：___。", "quality": "必须给出一句可以直接展示的完整原话；‘有’或‘一句话的雏形’不算答案。"},
            {"key": "self_intro", "question": "提炼一句真实的自我介绍。", "template": "我是___，专门帮助___解决___。"},
            {"key": "trust_reason", "question": "了解客户或朋友愿意信任和跟随的真实原因。", "template": "他们愿意相信我，是因为我___，并且做到了___。"},
            {"key": "ip_goal", "question": "明确做IP的主要商业目标。", "options": ["引流获客", "卖课或卖产品", "打造个人品牌", "其他"], "template": "我做IP最想实现___。"},
            {"key": "time_commitment", "question": "了解每周可稳定投入的时间。", "template": "我预计每天投入___小时，或每周投入___天。"},
            {"key": "products_services", "question": "了解当前可销售或承接的产品和服务。", "template": "我目前可以提供___，主要价格或交付方式是___。"},
            {"key": "short_term_goal", "question": "明确未来3个月可验证的阶段目标。", "template": "未来3个月，我希望做到___。"},
            {"key": "long_term_goal", "question": "明确未来1年的长期目标。", "template": "未来1年，我希望成为___，并实现___。"},
        ),
    },
    4: {
        "name": "故事资产",
        "questions": (
            {"key": "comeback_story", "question": "挖掘一次绝境翻身的完整故事。", "template": "当时的处境___；最难的是___；我通过___扛过来；结果___。", "quality": "必须是真实故事，至少包含处境、行动和结果；‘参考’、‘回顾模块’等操作文字不是答案。"},
            {"key": "pitfall_story", "question": "挖掘一次踩过的大坑及真实教训。", "template": "我曾经因为___损失或失败___；后来明白___。", "quality": "必须是真实故事，至少包含事件、损失或失败以及教训。"},
            {"key": "success_story", "question": "挖掘一次从普通状态到成功结果的逆袭故事。", "template": "起点___；关键转折___；行动___；最终结果___。", "quality": "必须是真实故事，至少包含起点、行动和结果。"},
            {"key": "dramatic_story", "question": "挖掘特别奇葩、戏剧化或反差强烈的真实经历。", "template": "最出乎意料的一次经历是___，当时___，结果___。", "quality": "必须是真实经历和结果，不能记录用户的页面操作或选择。"},
            {"key": "team_project", "question": "了解带团队或负责项目的规模、结果和踩坑经验。", "template": "我带过___人的团队或负责___项目，结果___，最大的教训是___。", "quality": "必须包含真实项目或团队规模、结果和经验；没有可以明确跳过。"},
        ),
    },
}


ANSWER_KEY_ALIASES = {
    1: {"basic_context": ("identity",)},
}

_MIN_ANSWER_LENGTH = {
    "career_identity": 6,
    "low_point": 12,
    "achievement": 10,
    "praised_traits": 6,
    "criticized_traits": 8,
    "proven_ability": 10,
    "content_track": 8,
    "target_audience": 8,
    "audience_pain": 12,
    "differentiation": 10,
    "personality_words": 5,
    "communication_style": 6,
    "disliked_style": 8,
    "content_habits": 8,
    "memorable_statement": 6,
    "self_intro": 8,
    "trust_reason": 10,
    "products_services": 10,
    "short_term_goal": 6,
    "long_term_goal": 6,
    "comeback_story": 24,
    "pitfall_story": 24,
    "success_story": 24,
    "dramatic_story": 24,
    "team_project": 24,
}

_MODULE_REQUIRED_GROUPS = {
    1: (
        ("career_identity",),
        ("achievement", "proven_ability"),
        ("target_audience",),
        ("audience_pain",),
        ("differentiation",),
    ),
    2: (("personality_words",), ("communication_style",)),
    3: (
        ("memorable_statement",), ("self_intro",), ("trust_reason",),
        ("ip_goal",), ("products_services",),
        ("short_term_goal",), ("long_term_goal",),
    ),
    4: ((
        "comeback_story", "pitfall_story", "success_story",
        "dramatic_story", "team_project",
    ),),
}

_META_ANSWER_PATTERNS = (
    re.compile(r"^(?:参考|回顾|修改|继续|下一题|下一个问题|当前模块|待补充|以后再说)$"),
    re.compile(r"^(?:用户)?(?:选择|要求|希望).{0,30}(?:回顾|修改|跳过|模块|故事)$"),
    re.compile(r"(?:用户选择回顾|用户选择修改|回顾或修改模块|当前问题|页面操作)"),
)


def _compact_answer(value):
    return re.sub(r"[\s，。,.!！?？:：;；\"'“”‘’（）()【】\[\]]+", "", str(value or "")).lower()


def profile_answer_value(answers, module, key):
    module_answers = (
        (answers or {}).get(str(module))
        if isinstance(answers, dict) else {}
    ) or {}
    value = module_answers.get(key)
    if value not in (None, ""):
        return value
    for alias in ANSWER_KEY_ALIASES.get(int(module), {}).get(str(key), ()):
        value = module_answers.get(alias)
        if value not in (None, ""):
            return value
    return ""


def answer_quality_issue(state, module, key, value, source=""):
    text = str(value or "").strip()
    source_text = str(source or "").strip()
    compact = _compact_answer(text)
    source_compact = _compact_answer(source_text)
    for candidate in (compact, source_compact):
        if candidate and any(pattern.search(candidate) for pattern in _META_ANSWER_PATTERNS):
            return "这条内容是操作指令或占位文字，不是人物画像事实"
    minimum = int(_MIN_ANSWER_LENGTH.get(str(key)) or 1)
    if len(compact) < minimum:
        return "信息过少，尚不足以形成可用画像"
    if key == "career_identity" and re.match(r"^(?:我)?(?:目前)?(?:是)?ai(?:[,，。]|$)", text.lower()):
        return "需要说明真实职业或角色，不能只写‘我是AI’"
    if key == "low_point" and not re.search(r"后来|通过|走出|解决|调整|转折|重新", text):
        return "需要补充后来采取的行动以及如何走出低谷"
    if key == "achievement" and not re.search(r"完成|获得|实现|提升|降低|结果|做到", text):
        return "需要补充具体行动和结果"
    if key == "proven_ability" and not re.search(r"曾经|完成|做到|案例|结果|项目|证明", text):
        return "需要补充能证明能力的真实案例或结果"
    if key == "differentiation" and (
            compact in {"有成功案例", "我有成功案例", "我有经验", "专业"}
            or not re.search(r"因为|证据|案例|结果|相比|不同|独立|做到", text)):
        return "需要同时说明具体差异和证据"
    if key == "personality_words":
        words = [item.strip() for item in re.split(r"[、,，;；/|和]+", text) if item.strip()]
        if len(set(words)) < 3:
            return "需要提供三个不同的真实性格词"
    if key == "criticized_traits" and not re.search(
            r"缺点|不足|容易|不够|拖延|急躁|固执|啰嗦|争议|问题|吐槽|批评|过于|影响|但是", text):
        return "需要描述真实缺点或负面反馈，赞美和玩笑不能代替"
    if key == "memorable_statement" and (
            "雏形" in text or compact in {"有", "有一句话", "还没想好"}):
        return "需要给出一句可以直接展示的完整原话"
    if key in {
            "comeback_story", "pitfall_story", "success_story",
            "dramatic_story", "team_project"} and (
            not re.search(r"后来|随后|通过|行动|尝试|决定|开始|调整|协调|负责", text)
            or not re.search(r"结果|最终|因此|成功|失败|损失|完成|上线|交付|教训", text)):
        return "真实故事需要同时包含采取的行动和最终结果或教训"
    return ""


def next_pending_question_index(state, module, start_index=None):
    module = int(module)
    start = int(state.get("question_index") or 0) if start_index is None else int(start_index)
    skipped = set(state.get("skipped_questions") or [])
    answers = state.get("answers") or {}
    for index in range(start + 1, len(MODULES[module]["questions"])):
        question = MODULES[module]["questions"][index]
        key = question["key"]
        value = profile_answer_value(answers, module, key)
        if value not in (None, ""):
            if answer_quality_issue(state, module, key, value):
                return index
            continue
        if "%d:%s" % (module, key) in skipped:
            continue
        return index
    return None


def _module_required_issues(state, module):
    module = int(module)
    answers = state.get("answers") or {}
    questions = MODULES[module]["questions"]
    by_key = {item["key"]: index for index, item in enumerate(questions)}
    issues = []
    for group in _MODULE_REQUIRED_GROUPS[module]:
        valid = False
        reasons = []
        for key in group:
            value = profile_answer_value(answers, module, key)
            issue = answer_quality_issue(state, module, key, value) if value else "缺少回答"
            if not issue:
                valid = True
                break
            reasons.append(issue)
        if not valid:
            key = group[0]
            issues.append({
                "module": module,
                "key": key,
                "question_index": by_key[key],
                "reason": reasons[0] if len(group) == 1 else "至少需要补充一项真实案例或故事",
            })
    return issues


def module_completion_issue(state, module):
    module = int(module)
    answers = state.get("answers") or {}
    questions = MODULES[module]["questions"]
    for index, question in enumerate(questions):
        key = question["key"]
        value = profile_answer_value(answers, module, key)
        if not value:
            continue
        reason = answer_quality_issue(state, module, key, value)
        if reason:
            return {
                "module": module, "key": key,
                "question_index": index, "reason": reason,
            }
    required = _module_required_issues(state, module)
    return required[0] if required else None


def profile_quality_issues(state):
    issues = []
    seen = set()
    answers = state.get("answers") or {}
    for module in range(1, 5):
        for index, question in enumerate(MODULES[module]["questions"]):
            key = question["key"]
            value = profile_answer_value(answers, module, key)
            if not value:
                continue
            reason = answer_quality_issue(state, module, key, value)
            if reason:
                issues.append({
                    "module": module, "key": key,
                    "question_index": index, "reason": reason,
                })
                seen.add((module, key))
        for completion in _module_required_issues(state, module):
            if (module, completion["key"]) not in seen:
                issues.append(completion)
                seen.add((module, completion["key"]))
    return issues


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
    base = {"module": module, "module_name": MODULES[module]["name"], **questions[index]}
    active = state.get("active_question")
    if (
        isinstance(active, dict)
        and int(active.get("module") or 0) == module
        and str(active.get("key") or "") == base["key"]
        and isinstance(active.get("question"), str)
        and active["question"].strip()
    ):
        return {
            **base,
            "question": active["question"].strip()[:500],
            "template": str(active.get("template") or "").strip()[:500],
            "options": list(active.get("options") or [])[:6],
        }
    return base


def next_question_goal(state):
    module = max(1, min(4, int(state.get("current_module") or 1)))
    index = next_pending_question_index(state, module)
    if index is None:
        return None
    return {
        "module": module, "module_name": MODULES[module]["name"],
        **MODULES[module]["questions"][index],
    }


def _dynamic_question(value, target):
    if not isinstance(value, dict) or not isinstance(target, dict):
        raise ProfileAgentError("DeepSeek next question is missing")
    raw_options = value.get("options")
    if not isinstance(raw_options, list) or len(raw_options) > 6:
        raise ProfileAgentError("DeepSeek question options must be a list")
    options = []
    for index, item in enumerate(raw_options):
        text = _required_text(item, "question.options[%d]" % index, 80)
        if text in options:
            raise ProfileAgentError("DeepSeek question options contain duplicates")
        options.append(text)
    return {
        "module": int(target["module"]),
        "module_name": str(target["module_name"]),
        "key": str(target["key"]),
        "question": _required_content(value.get("question"), "question.question", 500, 4),
        "template": _required_content(
            value.get("template"), "question.template", 500, 0,
        ),
        "options": options,
    }


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

    def ask_question(self, state, transition="开始画像访谈"):
        target = {
            key: value for key, value in current_question({
                **state, "active_question": None,
            }).items()
            if key in {
                "module", "module_name", "key", "question", "template",
                "options", "quality",
            }
        }
        value = self._call(
            "你是黄雀独立个人画像 Agent，负责通过自然对话逐步了解用户。"
            "根据字段目标、已知回答和已确认模块，提出一个贴合用户上下文的问题。"
            "不要照抄字段目标，不要一次问多个问题，不得编造用户信息。"
            "返回 JSON：reply(string)、question(object)。reply 是你本轮对用户说的完整内容；"
            "question 包含 question(string)、template(string)、options(array)。",
            {
                "transition": str(transition or "")[:500],
                "question_goal": target,
                "known_answers": state.get("answers") or {},
                "confirmed_modules": state.get("selected_profiles") or {},
                "skipped_questions": state.get("skipped_questions") or [],
            },
        )
        return {
            "reply": _required_content(value.get("reply"), "reply", 1600, 4),
            "question": _dynamic_question(value.get("question"), target),
        }

    def capture_answer(self, state, message):
        question = current_question(state)
        current_goal = current_question({**state, "active_question": None})
        next_goal = next_question_goal(state)
        value = self._call(
            "你是黄雀独立个人画像 Agent。理解用户对当前问题的真实意图，不编造事实。"
            "返回 JSON：action(string)、accepted(bool)、value(string)、reply(string)、"
            "next_question(object|null)。action 只能是 answer、skip、clarify。"
            "有效事实用 action=answer、accepted=true，value 忠实提炼用户事实；"
            "明确跳过、下一题、暂时或不想回答时用 action=skip；含义不清、信息不足、"
            "只有玩笑或不满足 current_question.quality 时必须用 action=clarify。"
            "‘参考’、‘回顾模块’、‘用户选择’等页面操作或元描述绝不是人物事实；"
            "不能把赞美当缺点，不能把‘一句话的雏形’当作实际原话，故事必须有真实细节。"
            "reply 必须是你直接回复用户的完整自然语言。若 action 为 answer/skip 且存在"
            "next_question_goal，next_question 必须按目标生成 question、template、options；"
            "不得照抄目标问题。clarify 时 next_question 必须围绕 current_question_goal"
            "重新组织本轮追问，不推进字段。",
            {
                "current_question": question,
                "current_question_goal": current_goal,
                "next_question_goal": next_goal,
                "known_answers": state.get("answers") or {},
                "confirmed_modules": state.get("selected_profiles") or {},
                "user_answer": str(message or "")[:4000],
            },
        )
        if not isinstance(value.get("accepted"), bool):
            raise ProfileAgentError("DeepSeek accepted must be boolean")
        action = _required_text(value.get("action"), "action", 16)
        if action not in {"answer", "skip", "clarify"}:
            raise ProfileAgentError("DeepSeek action is invalid")
        accepted = value["accepted"]
        captured = _required_content(
            value.get("value"), "value", 1600,
            minimum=1 if action == "answer" else 0,
        )
        if accepted is not (action == "answer"):
            raise ProfileAgentError("DeepSeek action and accepted disagree")
        if action != "answer" and captured:
            raise ProfileAgentError("DeepSeek non-answer action contains a value")
        raw_next = value.get("next_question")
        if action == "clarify":
            next_question = _dynamic_question(raw_next, current_goal)
        elif action in {"answer", "skip"} and next_goal:
            next_question = _dynamic_question(raw_next, next_goal)
        else:
            if raw_next is not None:
                raise ProfileAgentError("DeepSeek returned an unexpected next question")
            next_question = None
        return {
            "action": action,
            "accepted": accepted,
            "value": captured,
            "reply": _required_content(value.get("reply"), "reply", 1600, 2),
            "next_question": next_question,
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
            "返回 JSON：reply、summary、options。reply 是直接回复用户的完整自然语言；"
            "summary、options 与 current_review 结构相同，每项必须保留非空"
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
            "reply": _required_content(value.get("reply"), "reply", 1600, 2),
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

    def complete_profile(self, profile):
        value = self._call(
            "你是黄雀独立个人画像 Agent。用户已完成画像模块选择。"
            "基于提供的真实画像，生成一段简洁、有完成感的回复，说明画像已保存，并自然引导"
            "用户继续生成选题计划或制作模板视频。不得编造成果。返回 JSON：reply(string)。",
            {"profile": profile},
        )
        return _required_content(value.get("reply"), "reply", 1600, 4)

    def interpret_intent(self, profile, flow, message):
        allowed = [
            "chat", "topic_plan", "revise_copy", "view_preferences",
            "clear_preferences", "start_video", "regenerate_video",
            "modify_profile", "repair_profile", "confirm_plan",
        ]
        value = self._call(
            "你是黄雀独立创作 Agent 的意图路由器。根据用户画像、当前流程和本轮原话理解意图。"
            "只返回 JSON：intent(string)、payload(object)。intent 必须来自 allowed_intents。"
            "payload 只可包含 topic(string)、platforms(array)、platform(string)，并忠实提取用户原话；"
            "flow.mode=template_collect 时，用户提供主题应返回 start_video 并提取 topic；"
            "flow.mode=template_review 时，用户提出修改应返回 regenerate_video。"
            "用户要求完善、补齐或修复低质量画像时返回 repair_profile；普通自由修改返回 modify_profile。"
            "不要把普通聊天误判为执行动作；涉及扣点的 confirm_payment 不能由自由文本触发。",
            {
                "profile": profile,
                "flow": flow if isinstance(flow, dict) else {},
                "message": str(message or "")[:4000],
                "allowed_intents": allowed,
            },
        )
        intent = _required_text(value.get("intent"), "intent", 40)
        if intent not in allowed:
            raise ProfileAgentError("DeepSeek intent is invalid")
        raw_payload = value.get("payload")
        if not isinstance(raw_payload, dict) or set(raw_payload) - {
            "topic", "platforms", "platform",
        }:
            raise ProfileAgentError("DeepSeek intent payload is invalid")
        payload = {}
        if "topic" in raw_payload:
            payload["topic"] = _required_content(
                raw_payload.get("topic"), "payload.topic", 400, 0,
            )
        allowed_platforms = {"douyin", "xiaohongshu", "wechat_channels"}
        if "platforms" in raw_payload:
            values = raw_payload.get("platforms")
            if not isinstance(values, list):
                raise ProfileAgentError("DeepSeek platforms must be a list")
            platforms = []
            for item in values:
                platform = _required_text(item, "payload.platforms", 40)
                if platform not in allowed_platforms:
                    raise ProfileAgentError("DeepSeek platform is invalid")
                if platform not in platforms:
                    platforms.append(platform)
            payload["platforms"] = platforms
        if "platform" in raw_payload:
            platform = _required_text(raw_payload.get("platform"), "payload.platform", 40)
            if platform not in allowed_platforms:
                raise ProfileAgentError("DeepSeek platform is invalid")
            payload["platform"] = platform
        return {"intent": intent, "payload": payload}

    def compose_reply(self, profile, message, event, draft_reply):
        value = self._call(
            "你是黄雀独立创作 Agent。根据用户原话、个人画像和后端已完成的事件，生成本轮最终回复。"
            "必须忠实于 event 和 draft_reply，不得声称未发生的扣点、生成或发布；"
            "一次最多提出一个必要问题，语言自然简洁。返回 JSON：reply(string)。",
            {
                "profile": profile,
                "user_message": str(message or "")[:4000],
                "event": event if isinstance(event, dict) else {},
                "draft_reply": str(draft_reply or "")[:2000],
            },
        )
        return _required_content(value.get("reply"), "reply", 2000, 2)
