"""Deterministic control harness for the IP12 coaching conversation.

The model may propose one checkpoint. Only this module can confirm it or move
the workflow cursor. Keeping the reducer pure makes state transitions testable
without a model call or Flask runtime.
"""

from copy import deepcopy
from difflib import SequenceMatcher
import re
import uuid


AVAILABLE_MODULE_COUNT = 6
SCHEMA_VERSION = 2
CHOICE_COUNT = 3
CHOICE_FIELD_LIMITS = {"title": 24, "summary": 36, "reason": 16, "caution": 16}
CHOICE_REQUIRED_FIELD_ORDER = (*CHOICE_FIELD_LIMITS, "recommended")
CHOICE_REQUIRED_FIELDS = frozenset(CHOICE_REQUIRED_FIELD_ORDER)
CHOICE_MATERIALIZED_FIELDS = CHOICE_REQUIRED_FIELDS | {"choice_id", "display_index"}
AGENT_RELEASE_MANIFEST = {
    "agent_release": "ip12-a1-persona",
    "state_schema": SCHEMA_VERSION,
    "skills": {
        "intake": {"contract_version": "1.1.0", "prompt_version": "intake-v2"},
        "module_checkpoint": {"contract_version": "1.1.0", "prompt_version": "module-checkpoint-v2"},
        "diagnostic_choice": {"contract_version": "1.0.0", "prompt_version": "diagnostic-choice-v1"},
        "migration": {"contract_version": "1.0.0", "prompt_version": None},
        "harness_action": {"contract_version": "1.0.0", "prompt_version": None},
        "safety_fallback": {"contract_version": "1.0.0", "prompt_version": None},
        "foundation_review": {"contract_version": "1.1.0", "prompt_version": "foundation-review-v2"},
        "content_revision": {"contract_version": "1.0.0", "prompt_version": "content-revision-v1"},
        "production_bridge": {"contract_version": "1.1.0", "prompt_version": None},
        "talking_head_video_agent": {"contract_version": "1.0.0", "prompt_version": None},
        "semantic_master_agent": {"contract_version": "1.0.0", "prompt_version": "semantic-master-v1"},
    },
}


# Production remains a suggestion until the server has checked the selected
# action against the first-party capability bridge.  Keeping this small map in
# the pure harness lets the conversation layer make a deterministic, testable
# recommendation without turning a natural-language reply into an execution.
PRODUCTION_FAMILIES = {
    "image": ("image-generate",),
    "audio": ("audio-generate",),
    "video": ("digital-ip-text-generate", "video-generate"),
    "canvas": ("canvas-ops",),
}

INTAKE_FOLLOW_UP_PATTERNS = (
    ("preferred_name", r"(?:怎么称呼|如何称呼|称呼你|叫什么|名字)"),
    ("gender", r"性别|男士|女士"),
    ("age", r"(?:年龄段|年龄|多大|几岁)"),
    ("city", r"(?:哪座城市|哪个城市|所在城市|目前在(?:哪|哪里)|生活在(?:哪|哪里))"),
    ("mobile", r"手机号|电话|联系方式"),
    ("experience_years", r"(?:从业|入行|做这行).{0,8}(?:多久|几年|多长时间)"),
    ("prior_roles", r"(?:做过|从事过).{0,12}(?:行业|岗位|工作)"),
    ("income", r"大致收入"),
    ("current_role", r"(?:目前|现在|当前).{0,12}(?:从事什么|做什么|职业.{0,4}是什么|身份.{0,4}是什么|主要工作)"),
    ("core_skill_1", r"最厉害.{0,8}(?:能力|技能)|最擅长"),
    ("core_skill_2", r"第二.{0,6}(?:能力|技能)|另一项.{0,6}(?:能力|技能)"),
    ("target_audience", r"目标受众|目标人群|想服务谁|帮助谁"),
    ("help_goal", r"解决什么问题|帮他们.{0,8}什么|带来什么帮助"),
    ("content_account", r"(?:内容账号|自媒体账号|抖音|小红书|视频号).{0,12}(?:有吗|做过|粉丝|运营)"),
    ("offer", r"(?:产品|服务|课程|项目).{0,12}(?:可以卖|在卖|提供|是什么|有哪些)"),
    ("business_goal", r"(?:商业目标|做IP的目的|做IP.{0,8}为了什么|引流|获客|品牌)"),
    ("primary_platform", r"(?:主要平台|准备在哪|打算在哪).{0,8}(?:发布|做内容)"),
    ("desired_action", r"(?:希望用户|希望观众|希望客户).{0,12}(?:做什么|采取|私信|咨询|购买|关注)"),
    ("time_budget", r"(?:投入多少时间|每周.{0,6}时间|每天.{0,6}时间)"),
    ("three_month_goal", r"(?:三个月|3个月).{0,12}(?:目标|做到|希望)"),
    ("tone_preference", r"(?:表达风格|说话风格|内容风格).{0,12}(?:喜欢|偏好|希望)"),
    ("disliked_style", r"(?:讨厌|不喜欢|反感).{0,12}(?:博主|表达|风格|内容)"),
    ("income_source", r"(?:主要|目前).{0,8}收入来源|靠什么收入"),
    ("income_range", r"年收入|收入区间|收入范围"),
    ("biggest_setback", r"最大.{0,6}(?:挫折|低谷)|最难的时候"),
    ("biggest_achievement", r"最有成就感|最自豪"),
    ("most_praised", r"被.{0,4}夸最多|别人最常夸"),
    ("most_criticized", r"被.{0,4}吐槽最多|别人最常吐槽"),
    ("niche", r"想做什么赛道|内容方向|哪个赛道"),
    ("differentiation", r"差异化|为什么听你|为什么选你"),
    ("personality_traits", r"三个词.{0,6}性格|3个词.{0,6}性格"),
    ("content_habits", r"朋友圈|聊天.{0,8}(?:发什么|内容习惯)"),
    ("memorable_line", r"最想让人记住.{0,8}(?:一句话|什么)"),
    ("self_intro", r"一句话.{0,6}(?:介绍自己|自我介绍)"),
    ("trust_reason", r"为什么.{0,8}(?:信任你|跟着你)"),
    ("story_comeback", r"绝境翻身|怎么扛过来"),
    ("story_pitfall", r"踩过.{0,4}(?:坑|大坑)|亏了多少"),
    ("story_success", r"逆袭成功|关键转折"),
    ("story_unusual", r"奇葩|戏剧性|离谱经历"),
    ("team_project_experience", r"带过团队|做过项目|团队规模"),
    ("one_year_goal", r"一年|1年.{0,8}(?:目标|做到|希望)"),
    ("long_term_interest", r"长期兴趣|长期.{0,6}(?:研究|关注)|一直感兴趣"),
)

INTAKE_FIELDS = (
    "preferred_name", "current_identity", "previous_work_experience",
    "core_skill_1", "core_skill_2", "long_term_interest", "target_audience",
    "help_goal", "content_account", "offer", "business_goal", "primary_platform",
    "desired_action", "time_budget", "three_month_goal", "tone_preference",
    "disliked_style", "team_project_experience", "age", "city", "income", "gender",
    "mobile", "experience_years", "income_source", "income_range",
    "biggest_setback", "biggest_achievement", "most_praised", "most_criticized",
    "niche", "differentiation", "personality_traits", "content_habits",
    "memorable_line", "self_intro", "trust_reason", "story_comeback",
    "story_pitfall", "story_success", "story_unusual", "one_year_goal",
)
INTAKE_FIELD_SET = frozenset(INTAKE_FIELDS)
INTAKE_COVERAGE_FIELDS = (
    "preferred_name", "gender", "age", "city", "mobile",
    "current_identity", "experience_years", "previous_work_experience",
    "income_source", "income_range", "biggest_setback", "biggest_achievement",
    "most_praised", "most_criticized", "core_skill_1", "core_skill_2",
    "niche", "target_audience", "help_goal", "differentiation", "content_account",
    "personality_traits", "tone_preference", "disliked_style", "content_habits",
    "memorable_line", "self_intro", "trust_reason", "story_comeback",
    "story_pitfall", "story_success", "story_unusual", "team_project_experience",
    "business_goal", "time_budget", "offer", "three_month_goal", "one_year_goal",
    "long_term_interest", "primary_platform", "desired_action",
)
INTAKE_COVERAGE_LABELS = {
    "preferred_name": "姓名或昵称", "gender": "性别", "age": "年龄", "city": "所在城市",
    "mobile": "手机号（可跳过）", "current_identity": "当前职业或身份",
    "experience_years": "从业年限", "previous_work_experience": "做过的行业或岗位",
    "income_source": "主要收入来源", "income_range": "年收入区间",
    "biggest_setback": "最大挫折或低谷", "biggest_achievement": "最有成就感的事",
    "most_praised": "被夸最多的特点", "most_criticized": "被吐槽最多的特点",
    "core_skill_1": "最厉害的实战能力", "core_skill_2": "第二项核心能力",
    "niche": "想做的赛道", "target_audience": "目标受众", "help_goal": "能解决的问题",
    "differentiation": "差异化", "content_account": "现有内容账号",
    "personality_traits": "三个性格词", "tone_preference": "说话风格偏好",
    "disliked_style": "讨厌的博主风格", "content_habits": "朋友圈或聊天内容习惯",
    "memorable_line": "最想让人记住的一句话", "self_intro": "一句话自我介绍",
    "trust_reason": "客户或朋友信任原因", "story_comeback": "绝境翻身故事",
    "story_pitfall": "踩坑故事", "story_success": "逆袭成功故事",
    "story_unusual": "奇葩或戏剧性经历", "team_project_experience": "团队或项目经历",
    "business_goal": "做 IP 的目的", "time_budget": "预计投入时间",
    "offer": "现有产品或服务", "three_month_goal": "三个月目标",
    "one_year_goal": "一年目标", "long_term_interest": "长期兴趣",
    "primary_platform": "主要发布平台", "desired_action": "希望用户采取的动作",
}
INTAKE_TOPIC_TO_FIELD = {
    "current_role": "current_identity",
    "experience_years": "experience_years",
    "prior_roles": "previous_work_experience",
    "income": "income_range",
}
DECLINE_RE = re.compile(r"不想|不方便|拒绝|跳过|不回答|不提供|不记录|不要记录|先不|不知道|不清楚|暂时没有")
DECLINE_FIELD_PATTERNS = {
    "preferred_name": r"姓名|名字|称呼|昵称",
    "age": r"年龄|多大|几岁",
    "city": r"城市|哪里人|在哪",
    "income": r"收入",
    "income_source": r"收入来源|怎么赚钱",
    "income_range": r"年收入|收入区间|收入",
    "gender": r"性别",
    "mobile": r"手机号|电话|联系方式",
    "experience_years": r"从业年限|做了几年",
    "previous_work_experience": r"行业|岗位|工作经历",
    "biggest_setback": r"挫折|低谷",
    "biggest_achievement": r"成就感|自豪",
    "most_praised": r"夸|表扬",
    "most_criticized": r"吐槽|批评",
    "niche": r"赛道|内容方向",
    "differentiation": r"差异化|为什么选你",
    "content_account": r"内容账号|自媒体账号|抖音|小红书|视频号",
    "personality_traits": r"性格|三个词",
    "offer": r"产品|服务|课程",
    "business_goal": r"商业目标|做IP的目的",
    "primary_platform": r"平台",
    "desired_action": r"用户做什么|观众做什么|行动目标",
    "time_budget": r"投入时间|每周时间|每天时间",
    "three_month_goal": r"三个月|3个月",
    "tone_preference": r"表达风格|说话风格",
    "disliked_style": r"讨厌|不喜欢|反感",
    "content_habits": r"朋友圈|聊天内容|内容习惯",
    "memorable_line": r"记住的一句话|最想让人记住",
    "self_intro": r"一句话介绍|自我介绍",
    "trust_reason": r"信任你|跟着你",
    "story_comeback": r"绝境翻身|扃过来",
    "story_pitfall": r"踩坑|大坑",
    "story_success": r"逆袭|成功故事|关键转折",
    "story_unusual": r"奇葩|戏剧性|离谱经历",
    "team_project_experience": r"团队|项目经历",
    "one_year_goal": r"一年目标|1年目标",
}
COMMERCIAL_FIELD_ALIASES = {
    "business_goal": ("business_goal", "commercial_goal", "ip_goal"),
    "offer": ("offer", "product", "service", "current_offer"),
    "primary_platform": ("primary_platform", "content_platform", "platform"),
    "desired_action": ("desired_action", "conversion_goal", "cta"),
}


def _declined_intake_fields(message, last_topic=""):
    text = str(message or "")
    refusal_segments = [
        segment for segment in re.split(r"[。！？!?；;\n]+", text)
        if DECLINE_RE.search(segment)
    ]
    if not refusal_segments:
        return []
    matches = [
        field for field, pattern in DECLINE_FIELD_PATTERNS.items()
        if any(re.search(pattern, segment) for segment in refusal_segments)
    ]
    if "income" in matches:
        matches.extend(field for field in ("income_source", "income_range") if field not in matches)
    fallback = INTAKE_TOPIC_TO_FIELD.get(last_topic, last_topic)
    if not matches and fallback in INTAKE_FIELD_SET and len(text.strip()) <= 24:
        matches.append(fallback)
    return matches


def _intake_field_complete(field, item):
    value = str((item or {}).get("value") or "").strip() if isinstance(item, dict) else str(item or "").strip()
    if field == "personality_traits":
        return len({part for part in re.split(r"[,，、/；;\s]+", value) if part}) >= 3
    return bool(value)


def intake_field_statuses(value):
    state = normalize_state(value)
    intake = state["intake"]
    candidate_items = {
        str(item.get("field") or ""): item
        for item in intake.get("profile_updates") or [] if isinstance(item, dict)
    }
    confirmed_items = {}
    for bucket in ("facts", "preferences"):
        confirmed_items.update(state["ip_profile"].get(bucket) or {})
    declined_fields = set(intake.get("declined_fields") or [])
    return {
        field: (
            "confirmed" if _intake_field_complete(field, confirmed_items.get(field)) else
            "declined" if field in declined_fields else
            "candidate" if _intake_field_complete(field, candidate_items.get(field)) else
            "unknown"
        )
        for field in INTAKE_FIELDS
    }


def intake_coverage_gaps(value, updates=()):
    statuses = intake_field_statuses(value)
    for item in updates or ():
        if (
            isinstance(item, dict)
            and item.get("field") in INTAKE_FIELD_SET
            and statuses[item["field"]] == "unknown"
            and _intake_field_complete(item["field"], item)
        ):
            statuses[item["field"]] = "candidate"
    return [field for field in INTAKE_COVERAGE_FIELDS if statuses[field] == "unknown"]


def intake_incomplete_fields(value):
    state = normalize_state(value)
    provided = set()
    for bucket in ("facts", "preferences"):
        provided.update((state["ip_profile"].get(bucket) or {}).keys())
    provided.update(
        str(item.get("field") or "")
        for item in state["intake"].get("profile_updates") or [] if isinstance(item, dict)
    )
    statuses = intake_field_statuses(state)
    return [field for field in INTAKE_COVERAGE_FIELDS if field in provided and statuses[field] == "unknown"]


def commercial_goal_gaps(value, updates=()):
    state = normalize_state(value)
    fields = set()
    for bucket in ("facts", "preferences"):
        fields.update((state["ip_profile"].get(bucket) or {}).keys())
    fields.update(
        str(item.get("field") or "") for item in updates or [] if isinstance(item, dict)
    )
    return [
        canonical for canonical, aliases in COMMERCIAL_FIELD_ALIASES.items()
        if not any(alias in fields for alias in aliases)
    ]


def persona_contract(value):
    profile = profile_for_model(value)
    outputs = profile.get("confirmed_outputs") or {}
    return {
        "identity_facts": deepcopy(profile.get("facts") or {}),
        "preferences": deepcopy(profile.get("preferences") or {}),
        "positioning": deepcopy(outputs.get("1-2") or {}),
        "persona": deepcopy(outputs.get("2-2") or {}),
        "value_proposition": deepcopy(outputs.get("3-2") or {}),
        "story_assets": deepcopy(outputs.get("4-4") or {}),
        "commercial_goal_gaps": commercial_goal_gaps(value),
    }


def grounded_story_node_decision(value):
    state = normalize_state(value)
    facts = state["ip_profile"].get("facts") or {}
    preferred = (
        "story_comeback", "story_pitfall", "story_success", "story_unusual",
        "biggest_setback", "biggest_achievement", "previous_work_experience",
        "team_project_experience", "key_experience", "career_transition",
        "turning_point", "project_experience",
    )
    items = []
    for field in preferred:
        item = facts.get(field)
        quote = str((item or {}).get("evidence_quote") or "").strip() if isinstance(item, dict) else ""
        if quote and not any(quote in existing or existing in quote for existing in items):
            items.append(quote)
    if not items:
        for field, item in facts.items():
            if not any(token in str(field) for token in ("experience", "career", "story", "project", "turning")):
                continue
            quote = str((item or {}).get("evidence_quote") or "").strip() if isinstance(item, dict) else ""
            if quote and quote not in items:
                items.append(quote)
    items = items[:8]
    if not items:
        raise HarnessError("模块 4 还缺少一段可回查的真实经历")
    draft = "\n\n".join(
        "### 节点 %d：真实经历节点（包装建议）\n事实原话：%s\n包装建议：围绕这段真实经历提炼起点、行动、结果和反思。"
        % (index, quote)
        for index, quote in enumerate(items, 1)
    )
    return {
        "decision": "propose_checkpoint", "checkpoint": 1,
        "reply": "我已从确认资料中整理出可回查的真实故事节点，请先核对。",
        "draft": draft, "self_review": "所有节点逐字引用已确认原话。",
        "choices": [], "profile_updates": [], "confidence": 1.0,
    }


def intake_follow_up_topic(reply):
    """Classify one of the bounded intake questions from the visible reply."""
    segments = [item for item in re.split(r"[。！？!?\n]+", str(reply or "")) if item.strip()]
    for segment in reversed(segments):
        for topic, pattern in INTAKE_FOLLOW_UP_PATTERNS:
            if re.search(pattern, segment):
                return topic
    return ""


def _intake_has_core_profile(value, updates, evidence_text):
    state = normalize_state(value)
    fields = {
        str(item.get("field") or "").lower()
        for item in list(state["intake"].get("profile_updates") or []) + list(updates or [])
        if isinstance(item, dict)
    }
    required_groups = (
        ("identity", "role", "current"),
        ("experience", "career", "work"),
        ("interest",),
        ("audience",),
    )
    has_help_goal = any(
        token in field for field in fields for token in ("goal", "benefit", "value", "help")
    ) or re.search(r"(?:希望|想要|目标).{0,30}(?:帮助|让|解决|服务)", str(evidence_text or ""))
    return (
        all(any(token in field for field in fields for token in group) for group in required_groups)
        and sum("skill" in field for field in fields) >= 2
        and bool(has_help_goal)
    )


def production_recommendation(requested_result, preferred_action=None):
    """Return the bounded production candidates for an IP12 project.

    This deliberately does not validate provider parameters or submit work;
    those are account-scoped responsibilities of the production bridge.
    """
    family = str(requested_result or "").strip().lower()
    candidates = PRODUCTION_FAMILIES.get(family)
    if not candidates:
        raise HarnessError("暂不支持该制作类型")
    preferred = str(preferred_action or "").strip()
    if preferred and preferred not in candidates:
        raise HarnessError("所选能力与制作类型不匹配")
    return {
        "capability_family": family,
        "recommended_action": preferred or candidates[0],
        "candidate_actions": list(candidates),
    }


def production_intent(message):
    """Recognize an explicit request to turn the selected script into an asset."""
    text = re.sub(r"\s+", "", str(message or "")).lower()
    text = re.sub(
        r"(?:不要|无需|不需要|别)(?:再)?(?:生成|制作|做成|转成|转换成|配成)?(?:一张|一个|一条)?"
        r"(?:图片|海报|封面|配图|音频|配音|语音|视频|短片|数字人)"
        r"(?:(?:、|,|，|或|和|以及)(?:图片|海报|封面|配图|音频|配音|语音|视频|短片|数字人))*",
        "",
        text,
    )
    patterns = (
        ("canvas", r"(?:放到|放进|整理到|添加到|转到).{0,32}(?:canvas|画布)|(?:canvas|画布).{0,32}(?:放入|整理|添加)"),
        ("audio", r"(?:生成|制作|做成|转成|转换成|配成).{0,32}(?:音频|配音|语音)|(?:音频|配音|语音).{0,32}(?:生成|制作)"),
        ("video", r"(?:生成|制作|做成|转成|转换成).{0,32}(?:视频|短片|数字人)|(?:视频|短片|数字人).{0,32}(?:生成|制作)"),
        ("image", r"(?:生成|制作|做成|转成|转换成|配(?:一张|张|个)?).{0,32}(?:图片|海报|封面|配图)|(?:图片|海报|封面|配图).{0,32}(?:生成|制作)"),
    )
    for family, pattern in patterns:
        if re.search(pattern, text, re.I):
            preferred = ""
            if family == "video" and re.search(r"grok|文生视频|画面视频", text, re.I):
                preferred = "video-generate"
            elif family == "video" and re.search(r"数字人|口播视频", text):
                preferred = "digital-ip-text-generate"
            return production_recommendation(family, preferred)
    return None


MODULE_WORKFLOWS = {
    1: {
        "name": "定位诊断",
        "required": "关键经历、至少两项核心技能、长期兴趣、目标人群，以及明确表达的价值观或帮助目标",
        "checkpoints": (
            "提炼 3–5 个核心关键词",
            "从三项差异化定位方向中选择一个",
        ),
    },
    2: {
        "name": "人设塑造",
        "required": "从模块 1 已确认的经历、行为、价值观和目标中提炼人格候选，并确认表达偏好、反感风格与内容边界",
        "checkpoints": (
            "提炼人格关键词与核心价值观",
            "从三项差异化人设方向中选择一个",
        ),
    },
    3: {
        "name": "价值主张",
        "required": "复用已确认的定位、人设、核心优势、价值观、目标人群，并了解已有或计划提供的产品/服务与商业目的",
        "checkpoints": (
            "提炼 3–5 个价值关键词",
            "从三项差异化价值主张中选择一个",
        ),
    },
    4: {
        "name": "故事资产",
        "required": "复用已确认的真实经历、职业转折、团队或项目经历、共鸣点、核心价值观和未来目标",
        "checkpoints": (
            "提炼 3–5 个关键故事节点",
            "将故事分为挫折型、成长型和愿景型",
            "为每个故事生成名称、梗概、情绪点和传播场景",
            "汇总故事资产清单，推荐长期核心故事并说明理由",
        ),
    },
    5: {
        "name": "内容选题",
        "required": "目标人群、核心领域、已确认优势、商业目的、已有或计划产品/服务、主要平台和希望用户采取的动作；投入时间与三个月目标可选",
        "checkpoints": (
            "确定 3 个长期选题种类并说明各自边界",
            "为每个种类设计 10 个具体选题，共 30 个",
            "确认 3×10 选题库并精选每类 1 个重点选题",
        ),
    },
    6: {
        "name": "文案口播",
        "required": "主题、目标人群、身份设定、传播目标和表达风格偏好",
        "checkpoints": (
            "确认 3 篇精选口播文案的表达风格、时长和行动目标",
            "审阅 3 个精选选题及对应完整口播文案",
            "确认首批 3 篇完整口播文案成果",
        ),
    },
}

CONFIRM_TEXTS = frozenset({
    "确认", "确认无误", "确认资料", "确认补充", "确认这一步", "确认本模块",
    "没有问题", "没问题", "内容正确", "就按这个", "可以确认", "嗯好", "保留并继续",
})
EDIT_TEXTS = frozenset({
    "需要修改", "我要修改", "修改", "有问题", "不对", "重新填写", "改一下",
    "继续修改", "修改当前内容", "都不合适", "都不合适我要修改",
})
CONTINUE_TEXTS = frozenset({
    "继续", "下一步", "进入下一步", "继续下一步", "请继续", "开始下一步",
    "好的继续", "嗯好继续", "好的下一步", "嗯好下一步", "保留并继续",
})
FIELD_RE = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")
DURATION_RE = re.compile(r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十半]+)\s*(?:年|个?月)")
DURATION_FIELDS = ("year", "duration", "experience", "tenure")
RISKY_CLAIM_RE = re.compile(
    r"专家|顾问|战略家|领军(?:人物)?|导师|资深|"
    r"深厚.{0,4}经验|丰富.{0,4}经验|多年.{0,4}经验|跨行业经验|"
    r"成功案例|推动.{0,12}成功|知识渊博|全球受众|跨国社区"
)
CLAIM_BOUNDARY_RE = re.compile(r"[，,。.;；！？!?\n：:]|但是|但|可是|而是|却是|却|然而|反而|不过|所以|因此")
NEGATED_CLAIM_RE = re.compile(
    r"(?:不(?:把|会|能|要|应|该|得|以|做|当|作为|属于|包装|声称|自称|冒充|夸大|具备|拥有)|"
    r"并非|不是|没有|尚未|避免|不能|无需|拒绝)"
)
FUTURE_CLAIM_RE = re.compile(r"(?:未来|希望|想要?|目标|计划|愿景|以后|准备|打算|争取|成为).{0,12}$")
PAST_CLAIM_RE = re.compile(r"(?:以前|过去|曾经|原来|做过|曾任|前任).{0,12}$")
TOPIC_FIELD_RE = re.compile(r"topic_[123]_(?:0[1-9]|10)\Z")
TOPIC_EVIDENCE_TERMS = (
    "零售", "制造", "医疗", "教育", "物流", "金融", "餐饮", "地产", "美业", "电商",
    "农业", "旅游", "法律", "保险", "银行", "医院", "学校", "工厂", "门店", "客户",
    "案例", "营收", "增长", "成功", "业绩", "转化率", "效率提升",
)
TOPIC_SAFE_REPLACEMENTS = {
    **dict.fromkeys(TOPIC_EVIDENCE_TERMS[:19], "垂直行业"),
    "客户": "具体对象",
    "案例": "过程记录",
    "营收": "待验证结果",
    "增长": "待验证结果",
    "成功": "实践",
    "业绩": "待验证结果",
    "转化率": "待验证结果",
    "效率提升": "待验证结果",
}
MODULE_FOUR_EXAGGERATION_RE = re.compile(
    r"过人(?:的)?天赋|天赋异禀|非凡(?:的)?天赋|卓越(?:的)?天赋|远超常人"
)
MODULE_FOUR_PAST_CLIENT_RE = re.compile(
    r"(?:在服务客户的过程中|在服务过程中|你(?:曾经|过去)?遇到(?:过)?(?:很多|许多)?|"
    r"你(?:曾经|过去)?发现(?:过)?|很多客户|许多客户|客户们|她们).{0,100}"
    r"(?:自我怀疑|害怕|恐惧|内疚|羞耻|焦虑|承受.{0,12}指责|被迫.{0,8}丢弃)"
)
MODULE_FOUR_PAST_CLIENT_EVIDENCE_RE = re.compile(
    r"(?:我|我的|本人|曾经|过去|之前|工作中|服务时).{0,40}"
    r"(?:服务过|接触过|遇到过|客户说|客户反馈|有客户).{0,100}"
    r"(?:自我怀疑|害怕|恐惧|内疚|羞耻|焦虑|指责|被迫.{0,8}丢弃)"
)
MODULE_FOUR_INNER_STATE_TERMS = ("自我怀疑", "害怕", "恐惧", "内疚", "羞耻", "成就感")
MODULE_FOUR_QUOTE_RE = re.compile(
    r"(?:事实原话|未来方向原话)[*_`\s]*[：:][*_`\s]*([^\n]+)"
)

DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["ask_follow_up", "propose_checkpoint", "revise_intake", "answer_only"],
        },
        "checkpoint": {"type": "integer", "minimum": 0, "maximum": 5},
        "reply": {"type": "string", "maxLength": 4000},
        "draft": {"type": "string", "maxLength": 12000},
        "self_review": {"type": "string", "maxLength": 500},
        "choices": {
            "type": "array",
            "minItems": CHOICE_COUNT,
            "maxItems": CHOICE_COUNT,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "maxLength": CHOICE_FIELD_LIMITS["title"]},
                    "summary": {"type": "string", "maxLength": CHOICE_FIELD_LIMITS["summary"]},
                    "reason": {"type": "string", "maxLength": CHOICE_FIELD_LIMITS["reason"]},
                    "caution": {"type": "string", "maxLength": CHOICE_FIELD_LIMITS["caution"]},
                    "recommended": {"type": "boolean"},
                },
                "required": ["title", "summary", "reason", "caution", "recommended"],
            },
        },
        "profile_updates": {
            "type": "array",
            "maxItems": 40,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "field": {"type": "string", "maxLength": 64},
                    "value": {"type": "string", "maxLength": 1200},
                    "kind": {
                        "type": "string",
                        "enum": ["user_fact", "user_preference", "ai_option"],
                    },
                    "evidence_quote": {"type": "string", "maxLength": 300},
                },
                "required": ["field", "value", "kind", "evidence_quote"],
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "decision",
        "checkpoint",
        "reply",
        "draft",
        "self_review",
        "choices",
        "profile_updates",
        "confidence",
    ],
}

MODULE_FIVE_TOPIC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["ask_follow_up", "answer_only", "propose_checkpoint"],
        },
        "reply": {"type": "string", "maxLength": 4000},
        "categories": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "maxLength": 120},
                    "topics": {
                        "type": "array",
                        "minItems": 10,
                        "maxItems": 10,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "title": {"type": "string", "maxLength": 200},
                                "evidence_id": {"type": "string", "maxLength": 16},
                            },
                            "required": ["title", "evidence_id"],
                        },
                    },
                },
                "required": ["name", "topics"],
            },
        },
        "self_review": {"type": "string", "maxLength": 500},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["decision", "reply", "categories", "self_review", "confidence"],
}


class HarnessError(ValueError):
    pass


class HarnessConflict(HarnessError):
    pass


class ChoiceValidationError(HarnessError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = str(code)


def source_schema_version(value):
    if not isinstance(value, dict) or "schema_version" not in value:
        return 1
    version = value.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version not in {1, SCHEMA_VERSION}:
        raise HarnessError("Project schema 版本无效或高于当前服务")
    return version


def _validate_source_containers(state):
    expected = {
        "completed_modules": list,
        "ip_profile": dict,
        "intake": dict,
        "migration_archive": dict,
    }
    for field, field_type in expected.items():
        if field in state and state[field] is not None and not isinstance(state[field], field_type):
            raise HarnessError("Project %s 数据结构损坏，已停止迁移" % field)
    if "pending" in state and state["pending"] is not None and not isinstance(state["pending"], dict):
        raise HarnessError("Project pending 数据结构损坏，已停止迁移")
    profile = state.get("ip_profile") or {}
    for field in ("facts", "preferences", "ai_selections", "confirmed_outputs"):
        if field in profile and not isinstance(profile[field], dict):
            raise HarnessError("Project ip_profile.%s 数据结构损坏，已停止迁移" % field)
    for field in ("revision", "current_module", "module_step"):
        if field not in state:
            continue
        if isinstance(state[field], bool):
            raise HarnessError("Project %s 数值损坏，已停止迁移" % field)
        try:
            int(state[field])
        except (TypeError, ValueError) as exc:
            raise HarnessError("Project %s 数值损坏，已停止迁移" % field) from exc


def agent_trace(skills, *, prompt_version=None, model=None, release_sha=None):
    skill_ids = [skills] if isinstance(skills, str) else list(skills or [])
    traced = []
    for skill_id in skill_ids:
        skill_id = str(skill_id)
        spec = AGENT_RELEASE_MANIFEST["skills"].get(skill_id)
        if spec is None:
            raise HarnessError("未知 Agent Skill：%s" % skill_id)
        traced.append({"id": skill_id, "version": spec["contract_version"]})
    if not traced:
        raise HarnessError("assistant message 缺少 Agent Skill 来源")
    if prompt_version is None and len(skill_ids) == 1:
        prompt_version = AGENT_RELEASE_MANIFEST["skills"][skill_ids[0]].get("prompt_version")
    return {
        "agent_release": AGENT_RELEASE_MANIFEST["agent_release"],
        "skills": traced,
        "state_schema": AGENT_RELEASE_MANIFEST["state_schema"],
        "prompt_version": prompt_version or None,
        "model": str(model) if model else None,
        "release_sha": str(release_sha) if release_sha else None,
    }


def profile_for_model(value):
    """Return confirmed facts without historical, unselected choice alternatives."""
    profile = deepcopy(normalize_state(value)["ip_profile"])
    for output in (profile.get("confirmed_outputs") or {}).values():
        if isinstance(output, dict):
            output.pop("choice_snapshot", None)
    return profile


def is_choice_checkpoint(module, step):
    try:
        return int(module) in {1, 2, 3} and int(step) == 2
    except (TypeError, ValueError):
        return False


def _choice_title_key(value):
    return re.sub(r"[\W_]+", "", str(value or "")).lower()


def validate_choices(value, *, materialized=False):
    if not isinstance(value, list) or len(value) != CHOICE_COUNT:
        raise ChoiceValidationError("choice_count", "候选方案必须恰好为 %s 项" % CHOICE_COUNT)
    clean = []
    for item in value:
        expected_fields = CHOICE_MATERIALIZED_FIELDS if materialized else CHOICE_REQUIRED_FIELDS
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise ChoiceValidationError("choice_shape", "候选方案结构无效")
        if materialized and (
            not isinstance(item.get("choice_id"), str)
            or isinstance(item.get("display_index"), bool)
            or not isinstance(item.get("display_index"), int)
        ):
            raise ChoiceValidationError("choice_shape", "候选方案标识结构无效")
        labels = {"title": "标题", "summary": "摘要", "reason": "适合原因", "caution": "注意事项"}
        choice = {}
        for field, limit in CHOICE_FIELD_LIMITS.items():
            if not isinstance(item.get(field), str):
                raise ChoiceValidationError("choice_shape", "%s必须是文本" % labels[field])
            text = item[field].strip()
            if not text or len(text) > limit:
                raise ChoiceValidationError(
                    "choice_content_length", "%s必须为 1–%s 个 Unicode 字符" % (labels[field], limit)
                )
            choice[field] = text
        if not isinstance(item.get("recommended"), bool):
            raise ChoiceValidationError("choice_shape", "推荐标记必须是布尔值")
        choice["recommended"] = item["recommended"]
        clean.append(choice)
    keys = [_choice_title_key(item["title"]) for item in clean]
    if any(not key for key in keys) or len(set(keys)) != 3:
        raise ChoiceValidationError("choice_duplicate", "%s 个候选标题不能重复" % CHOICE_COUNT)
    if sum(bool(item["recommended"]) for item in clean) > 1:
        raise ChoiceValidationError("choice_recommendation_count", "最多只能推荐一个候选")
    return clean


def compile_choice_content(choice):
    return (
        "### {title}\n\n{summary}\n\n"
        "- 适合你的原因：{reason}\n"
        "- 需要注意：{caution}"
    ).format(**choice)


def _materialize_choices(choices):
    return [
        {**item, "choice_id": "choice-%s" % index, "display_index": index}
        for index, item in enumerate(choices, 1)
    ]


def migrate_state_v1_to_v2(value):
    state = deepcopy(value) if isinstance(value, dict) else initial_state()
    version = source_schema_version(state)
    _validate_source_containers(state)
    if version == SCHEMA_VERSION:
        return state

    try:
        module = int(state.get("current_module", 1))
    except (TypeError, ValueError):
        module = 1
    completed = set()
    completed_items = state.get("completed_modules")
    if not isinstance(completed_items, (list, tuple, set)):
        completed_items = []
    for item in completed_items:
        try:
            completed.add(int(item))
        except (TypeError, ValueError):
            pass
    try:
        old_step = max(0, int(state.get("module_step", 0)))
    except (TypeError, ValueError):
        old_step = 0
    pending = state.get("pending") if isinstance(state.get("pending"), dict) else None
    pending_step = 0
    if pending:
        try:
            pending_step = int(pending.get("step", 0))
        except (TypeError, ValueError):
            pending_step = 0

    needs_choice = module in {1, 2, 3} and module not in completed and max(old_step, pending_step - 1) >= 1
    profile = state.get("ip_profile") if isinstance(state.get("ip_profile"), dict) else {}
    outputs = profile.get("confirmed_outputs", {})
    if isinstance(outputs, dict):
        migration_archive = state.get("migration_archive")
        if not isinstance(migration_archive, dict):
            migration_archive = {}
            state["migration_archive"] = migration_archive
        archived = migration_archive.get("v1_confirmed_outputs")
        if not isinstance(archived, dict):
            archived = {}
            migration_archive["v1_confirmed_outputs"] = archived
        archived_text = []
        for legacy_module in (1, 2, 3):
            final_key = "%s-4" % legacy_module
            legacy_final = deepcopy(outputs.get(final_key) or archived.get(final_key))
            for key in list(outputs):
                match = re.fullmatch(r"([123])-(\d+)", str(key))
                if not match or int(match.group(1)) != legacy_module or int(match.group(2)) < 2:
                    continue
                old_output = outputs.pop(key)
                archived.setdefault(key, old_output)
                archived_text.append(str((old_output or {}).get("content") or "") if isinstance(old_output, dict) else str(old_output or ""))
            if legacy_module in completed and isinstance(legacy_final, dict) and str(legacy_final.get("content") or "").strip():
                legacy_final.update(
                    module=legacy_module,
                    step=2,
                    title=MODULE_WORKFLOWS[legacy_module]["checkpoints"][1],
                )
                legacy_final.pop("choice_snapshot", None)
                outputs["%s-2" % legacy_module] = legacy_final
        selections = profile.get("ai_selections")
        if isinstance(selections, dict) and any(archived_text):
            archived_selections = migration_archive.get("v1_ai_selections")
            if not isinstance(archived_selections, dict):
                archived_selections = {}
                migration_archive["v1_ai_selections"] = archived_selections
            for field, item in list(selections.items()):
                value_text = str(item.get("value") or "") if isinstance(item, dict) else str(item or "")
                if value_text and any(value_text in text for text in archived_text):
                    archived_selections.setdefault(field, selections.pop(field))
    if module in {1, 2, 3} and module not in completed:
        state["module_step"] = 1 if needs_choice else min(old_step, 1)
        if pending_step >= 2:
            state["pending"] = None

    state["revision"] = max(1, int(state.get("revision", 1))) + 1
    state["schema_version"] = SCHEMA_VERSION
    state["migration"] = {
        "from": version,
        "to": SCHEMA_VERSION,
        "notice_id": "ip12-schema-v2-migration",
        "needs_choice_generation": bool(needs_choice),
        "module": module if needs_choice else None,
        "step": 2 if needs_choice else None,
    }
    if needs_choice:
        state["choice_generation"] = {
            "target_id": "choicegen-%s-2" % module,
            "module": module,
            "step": 2,
        }
    return state


def initial_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 1,
        "ip_profile": {"facts": {}, "preferences": {}, "ai_selections": {}, "confirmed_outputs": {}},
        "current_module": 1,
        "completed_modules": [],
        "module_step": 0,
        "pending": None,
        "intake": {"status": "collecting", "round": 1, "answers": {}, "asked_follow_ups": [], "declined_fields": []},
    }


def normalize_state(value):
    state = migrate_state_v1_to_v2(value)
    state["schema_version"] = SCHEMA_VERSION
    try:
        state["revision"] = max(1, int(state.get("revision", 1)))
    except (TypeError, ValueError):
        state["revision"] = 1
    try:
        module = int(state.get("current_module", 1))
    except (TypeError, ValueError):
        module = 1
    state["current_module"] = min(AVAILABLE_MODULE_COUNT, max(1, module))
    completed = []
    completed_items = state.get("completed_modules")
    if not isinstance(completed_items, (list, tuple, set)):
        completed_items = []
    for item in completed_items:
        try:
            item = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= item <= AVAILABLE_MODULE_COUNT and item not in completed:
            completed.append(item)
    state["completed_modules"] = sorted(completed)
    while state["current_module"] in state["completed_modules"] and state["current_module"] < AVAILABLE_MODULE_COUNT:
        if state["current_module"] == 4 and (state.get("foundation_report") or {}).get("status") != "confirmed":
            break
        state["current_module"] += 1
    try:
        step = int(state.get("module_step", 0))
    except (TypeError, ValueError):
        step = 0
    if module != state["current_module"]:
        step = 0
    checkpoint_count = len(MODULE_WORKFLOWS[state["current_module"]]["checkpoints"])
    if state["current_module"] not in state["completed_modules"]:
        checkpoint_count -= 1
    state["module_step"] = min(checkpoint_count, max(0, step))

    profile = state.get("ip_profile")
    profile = deepcopy(profile) if isinstance(profile, dict) else {}
    for key in ("facts", "preferences", "ai_selections", "confirmed_outputs"):
        if not isinstance(profile.get(key), dict):
            profile[key] = {}
    state["ip_profile"] = profile

    intake = state.get("intake")
    intake = deepcopy(intake) if isinstance(intake, dict) else {"status": "complete", "round": 3, "answers": {}}
    status = str(intake.get("status") or "collecting")
    if status not in {"collecting", "awaiting_confirmation", "editing", "complete"}:
        status = "collecting"
    try:
        round_number = min(3, max(1, int(intake.get("round", 1))))
    except (TypeError, ValueError):
        round_number = 1
    if status == "collecting" and round_number >= 3 and str(intake.get("draft") or "").strip():
        status = "awaiting_confirmation"
    intake.update(
        status=status,
        round=round_number,
        answers=deepcopy(intake.get("answers")) if isinstance(intake.get("answers"), dict) else {},
        asked_follow_ups=[
            item for item in intake.get("asked_follow_ups", [])
            if isinstance(item, str) and item in {topic for topic, _ in INTAKE_FOLLOW_UP_PATTERNS}
        ],
        declined_fields=[
            item for item in intake.get("declined_fields", [])
            if isinstance(item, str) and item in INTAKE_FIELD_SET
        ],
    )
    confirmed_intake_items = {
        **(profile.get("facts") or {}), **(profile.get("preferences") or {}),
    }
    candidate_intake_items = {
        str(item.get("field") or ""): item
        for item in intake.get("profile_updates") or [] if isinstance(item, dict)
    }
    intake["field_statuses"] = {
        field: (
            "confirmed" if _intake_field_complete(field, confirmed_intake_items.get(field)) else
            "declined" if field in set(intake.get("declined_fields") or []) else
            "candidate" if _intake_field_complete(field, candidate_intake_items.get(field)) else
            "unknown"
        )
        for field in INTAKE_FIELDS
    }
    state["intake"] = intake

    pending = state.get("pending")
    if not isinstance(pending, dict) or pending.get("kind") != "checkpoint":
        state["pending"] = None
    else:
        try:
            pending_module = int(pending.get("module"))
            pending_step = int(pending.get("step"))
        except (TypeError, ValueError):
            state["pending"] = None
        else:
            valid = (
                pending_module == state["current_module"]
                and pending_step == state["module_step"] + 1
                and pending_step <= len(MODULE_WORKFLOWS[pending_module]["checkpoints"])
                and pending.get("status") in {"collecting", "awaiting_confirmation", "editing"}
                and isinstance(pending.get("id"), str)
            )
            if valid and is_choice_checkpoint(pending_module, pending_step):
                if pending.get("choices") is None:
                    valid = pending.get("status") in {"collecting", "editing"}
                else:
                    try:
                        clean_choices = validate_choices(pending.get("choices"), materialized=True)
                    except ChoiceValidationError:
                        valid = False
                    else:
                        pending["choices"] = _materialize_choices(clean_choices)
            elif valid:
                pending.pop("choices", None)
            if not valid:
                state["pending"] = None
    migration = state.get("migration")
    try:
        migration_version = int((migration or {}).get("to") or 0)
    except (AttributeError, TypeError, ValueError):
        migration_version = 0
    if not isinstance(migration, dict) or migration_version != SCHEMA_VERSION:
        state.pop("migration", None)
    generation = state.get("choice_generation")
    try:
        generation_module = int((generation or {}).get("module") or 0)
        generation_step = int((generation or {}).get("step") or 0)
    except (AttributeError, TypeError, ValueError):
        generation_module = generation_step = 0
    if not (
        isinstance(generation, dict)
        and str(generation.get("target_id") or "")
        and generation_module == state["current_module"]
        and generation_step == state["module_step"] + 1
        and is_choice_checkpoint(generation_module, generation_step)
    ):
        state.pop("choice_generation", None)
    return state


def _bump(state):
    state["revision"] = int(state.get("revision", 1)) + 1
    return state


def available_actions(value):
    state = normalize_state(value)
    intake = state["intake"]
    if intake["status"] == "awaiting_confirmation":
        target = "intake-%s" % state["revision"]
        revising = intake.get("mode") == "revision"
        return [
            {"type": "confirm_intake", "target_id": target, "label": "确认补充" if revising else "确认资料", "primary": True},
            {"type": "edit_intake", "target_id": target, "label": "继续修改" if revising else "我要修改", "primary": False},
        ]
    pending = state.get("pending")
    if isinstance(pending, dict) and pending.get("status") == "awaiting_confirmation":
        if pending.get("choices"):
            actions = [
                {
                    "type": "select_checkpoint_choice",
                    "target_id": pending["id"],
                    "choice_id": item["choice_id"],
                    "display_index": item["display_index"],
                    "title": item["title"],
                    "summary": item["summary"],
                    "reason": item["reason"],
                    "caution": item["caution"],
                    "recommended": item["recommended"],
                    "label": "%s. %s" % (item["display_index"], item["title"]),
                    "primary": False,
                }
                for item in pending["choices"]
            ]
            actions.append({
                "type": "edit_checkpoint", "target_id": pending["id"],
                "label": "都不合适，我要修改", "primary": False,
            })
            return actions
        final_step = int(pending.get("step") or 0) == len(MODULE_WORKFLOWS[state["current_module"]]["checkpoints"])
        return [
            {"type": "confirm_checkpoint", "target_id": pending["id"], "label": "确认本模块" if final_step else "保留并继续", "primary": True},
            {"type": "edit_checkpoint", "target_id": pending["id"], "label": "修改当前内容", "primary": False},
        ]
    generation = state.get("choice_generation") or {}
    if generation and not pending:
        return [{
            "type": "resume_choice_generation",
            "target_id": generation.get("target_id"),
            "label": "生成新的三个方案",
            "primary": True,
        }]
    return []


def _choice_index(message):
    normalized = re.sub(r"[\s，,。.!！?？]+", "", str(message or "")).lower()
    direct = {"1": 1, "一": 1, "2": 2, "二": 2, "3": 3, "三": 3}
    if normalized in direct:
        return direct[normalized]
    match = (
        re.fullmatch(
            r"(?:我)?(?:要|想)?(?:选|选择)?(?:第)?([123一二三])(?:个|项|个方案|项方案|方案)?",
            normalized,
        )
        or re.fullmatch(r"(?:方案|选项)([123一二三])", normalized)
        or re.fullmatch(r"(?:我)?(?:要|想)?(?:选|选择)(?:方案|选项)([123一二三])", normalized)
    )
    return direct.get(match.group(1)) if match else None


def shortcut_action(value, message):
    state = normalize_state(value)
    normalized = re.sub(r"[\s，,。.!！?？]+", "", str(message or "")).lower()
    actions = available_actions(state)
    if not actions:
        return None
    by_type = {item["type"]: item for item in actions}
    choices = [item for item in actions if item["type"] == "select_checkpoint_choice"]
    if choices:
        index = _choice_index(message)
        if index:
            item = next((item for item in choices if item["display_index"] == index), None)
            if item:
                return {
                    "type": item["type"], "target_id": item["target_id"],
                    "choice_id": item["choice_id"],
                }
        if normalized in EDIT_TEXTS:
            item = by_type["edit_checkpoint"]
            return {"type": item["type"], "target_id": item["target_id"]}
        return None
    if "resume_choice_generation" in by_type:
        if normalized in {
            "生成新的三个方案", "继续生成方案", "生成三个方案",
        } | CONFIRM_TEXTS | CONTINUE_TEXTS:
            item = by_type["resume_choice_generation"]
            return {"type": item["type"], "target_id": item["target_id"]}
        return None
    if normalized in CONFIRM_TEXTS or normalized in CONTINUE_TEXTS:
        key = "confirm_intake" if "confirm_intake" in by_type else "confirm_checkpoint"
        item = by_type.get(key)
        return {"type": key, "target_id": item["target_id"]} if item else None
    if normalized in EDIT_TEXTS:
        key = "edit_intake" if "edit_intake" in by_type else "edit_checkpoint"
        item = by_type.get(key)
        return {"type": key, "target_id": item["target_id"]} if item else None
    return None


def is_continue_message(message):
    normalized = re.sub(r"[\s，,。.!！?？]+", "", str(message or "")).lower()
    return normalized in CONTINUE_TEXTS


def is_content_review_message(message):
    text = re.sub(r"\s+", "", str(message or "")).lower()
    return bool(re.search(
        r"(?:想看|要看|先看|看看|看一下|看一看|查看|展示|发我|给我看|给我).{0,8}(?:文案|口播|正文|文章)|"
        r"(?:文案|口播|正文|文章).{0,8}(?:我)?(?:想看|要看|先看|看看|看一下|看一看|查看|展示|发来|给我看)",
        text,
    ))


def _require_revision(state, expected_revision):
    if isinstance(expected_revision, bool):
        raise HarnessConflict("页面状态已经变化，请刷新后重试")
    try:
        expected = int(expected_revision)
    except (TypeError, ValueError) as exc:
        raise HarnessConflict("确认动作缺少有效 revision") from exc
    if expected != state["revision"]:
        raise HarnessConflict("页面状态已经变化，请刷新后重试")


def _apply_profile_updates(profile, updates):
    buckets = {
        "user_fact": profile["facts"],
        "user_preference": profile["preferences"],
        "ai_option": profile["ai_selections"],
    }
    for item in updates:
        if TOPIC_FIELD_RE.fullmatch(item["field"]):
            continue  # Validation metadata; the confirmed 3×10 draft is the durable source.
        buckets[item["kind"]][item["field"]] = {
            "value": item["value"],
            "evidence_quote": item["evidence_quote"],
        }


def duration_conflict_decision(value, message):
    state = normalize_state(value)
    message_text = str(message or "")
    current = {item.replace(" ", "") for item in DURATION_RE.findall(message_text)}
    if not current:
        return None

    def contexts(text):
        source = str(text or "")
        result = []
        for match in DURATION_RE.finditer(source):
            fragment = source[max(0, match.start() - 28):match.end() + 28]
            fragment = DURATION_RE.sub("", fragment)
            fragment = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "", fragment).lower()
            if fragment:
                result.append(fragment)
        return result

    current_contexts = contexts(message_text)
    for field, item in state["ip_profile"]["facts"].items():
        if not isinstance(item, dict):
            continue
        if not any(token in field for token in DURATION_FIELDS):
            continue
        confirmed_text = str(item.get("evidence_quote") or item.get("value") or "")
        confirmed = {part.replace(" ", "") for part in DURATION_RE.findall(confirmed_text)}
        confirmed_contexts = contexts(confirmed_text)
        same_topic = any(
            SequenceMatcher(None, old_context, new_context).find_longest_match(
                0, len(old_context), 0, len(new_context)
            ).size >= 4
            for old_context in confirmed_contexts
            for new_context in current_contexts
        )
        if confirmed and current.isdisjoint(confirmed) and same_topic:
            old, new = sorted(confirmed)[0], sorted(current)[0]
            return {
                "decision": "ask_follow_up", "checkpoint": 0,
                "reply": "你前面确认的相关时间是“%s”，这次又提到“%s”。请说明这两个时间分别对应哪段经历或阶段，或者告诉我需要更正哪一个。" % (old, new),
                "draft": "", "self_review": "", "profile_updates": [], "confidence": 1.0,
            }
    return None


def apply_action(value, action, expected_revision, *, request_id="", selected_at=""):
    state = normalize_state(value)
    _require_revision(state, expected_revision)
    if not isinstance(action, dict):
        raise HarnessError("action 必须是对象")
    action_type = str(action.get("type") or "")
    target_id = str(action.get("target_id") or "")
    event = {
        "assistant_prefix": "", "new_completed": [], "continue_model": False,
        "user_label": "", "continuation_message": "",
    }

    if action_type == "resume_choice_generation":
        generation = state.get("choice_generation") or {}
        if (
            not generation
            or state.get("pending")
            or target_id != str(generation.get("target_id") or "")
            or not is_choice_checkpoint(state["current_module"], state["module_step"] + 1)
        ):
            raise HarnessConflict("候选生成状态已经更新，请查看最新页面")
        event.update(
            continue_model=True,
            user_label="生成新的三个方案",
            continuation_message="请基于已确认内容生成当前断点的三个新方案。",
        )
        _bump(state)
        return state, event

    if action_type in {"confirm_intake", "edit_intake"}:
        intake = state["intake"]
        revising = intake.get("mode") == "revision"
        expected_target = "intake-%s" % state["revision"]
        if intake["status"] != "awaiting_confirmation" or target_id != expected_target:
            raise HarnessConflict("这份基础资料已经更新，请查看最新版本")
        if action_type == "edit_intake":
            intake["status"] = "editing"
            event["assistant_prefix"] = "请直接补充或纠正，怎么说都可以；我会重新整理后再请你确认。"
        else:
            gaps = intake_coverage_gaps(state)
            if gaps:
                raise HarnessError(
                    "采集表尚未全部覆盖：%s；请继续访谈或明确跳过"
                    % "、".join(INTAKE_COVERAGE_LABELS[field] for field in gaps[:6])
                )
            intake["status"] = "complete"
            intake.pop("mode", None)
            draft = str(intake.get("draft") or "").strip()
            if not draft:
                draft = "\n".join(
                    "%s：%s" % (key, value)
                    for key, value in (intake.get("answers") or {}).items()
                    if str(value or "").strip()
                )
            state["ip_profile"]["intake"] = {"summary": draft}
            _apply_profile_updates(state["ip_profile"], intake.get("profile_updates") or [])
            intake["field_statuses"] = intake_field_statuses(state)
            if revising:
                module = state["current_module"]
                event["assistant_prefix"] = (
                    "✅ 基础信息补充已确认。继续模块 %s：%s；这次补充不会被当成模块回答，也不会自动推进。"
                    % (module, MODULE_WORKFLOWS[module]["name"])
                )
            else:
                event["assistant_prefix"] = "✅ 基础信息已确认。现在正式进入模块 1：定位诊断。"
                if _intake_has_core_profile(state, [], draft):
                    event.update(
                        continue_model=True,
                        continuation_message="基础资料已经完整，请直接提炼模块 1 的核心关键词，不要重复追问。",
                    )
                else:
                    event["assistant_prefix"] += (
                        "\n\n先讲一段对你影响最大的关键经历或转折：发生了什么，它后来怎样影响了你？"
                    )
        _bump(state)
        return state, event

    if action_type not in {"confirm_checkpoint", "edit_checkpoint", "select_checkpoint_choice"}:
        raise HarnessError("不支持的 action")
    pending = state.get("pending")
    if not isinstance(pending, dict) or pending.get("status") != "awaiting_confirmation" or pending.get("id") != target_id:
        raise HarnessConflict("这个确认项已经更新，请查看最新版本")
    if action_type == "edit_checkpoint":
        pending["status"] = "editing"
        event["assistant_prefix"] = (
            "收到，我刚才的整理没有符合你的意思。请直接说希望怎样改；"
            "我会先说明理解偏差，再立即更新当前内容，不会让你自己找入口。"
        )
        _bump(state)
        return state, event

    has_choices = bool(pending.get("choices"))
    if has_choices and action_type != "select_checkpoint_choice":
        raise HarnessConflict("当前断点必须选择一个候选，不能直接确认")
    if not has_choices and action_type == "select_checkpoint_choice":
        raise HarnessConflict("当前确认项没有可选择的候选")

    module = state["current_module"]
    step = int(pending["step"])
    key = "%s-%s" % (module, step)
    choice = None
    if action_type == "select_checkpoint_choice":
        choice_id = str(action.get("choice_id") or "")
        choice = next(
            (item for item in pending.get("choices") or [] if item.get("choice_id") == choice_id),
            None,
        )
        if choice is None:
            raise HarnessConflict("这个候选已经更新，请查看最新版本")
        content = compile_choice_content(choice)
    else:
        content = pending["draft"]
    state["ip_profile"]["confirmed_outputs"][key] = {
        "module": module,
        "step": step,
        "title": MODULE_WORKFLOWS[module]["checkpoints"][step - 1],
        "content": content,
        "self_review": pending.get("self_review", ""),
    }
    if choice is not None:
        state["ip_profile"]["confirmed_outputs"][key]["choice_snapshot"] = {
            "target_id": pending["id"],
            "module": module,
            "step": step,
            "choices": deepcopy(pending["choices"]),
            "selected_choice_id": choice["choice_id"],
            "selected_at": str(selected_at or ""),
            "request_id": str(request_id or ""),
            "confirmed_revision": state["revision"] + 1,
        }
        event["user_label"] = "我选择 %s：%s" % (choice["display_index"], choice["title"])
    else:
        _apply_profile_updates(state["ip_profile"], pending.get("profile_updates") or [])
    state["pending"] = None
    state["module_step"] = step
    if is_choice_checkpoint(module, step + 1):
        state["choice_generation"] = {
            "target_id": "choicegen-%s-%s" % (module, step + 1),
            "module": module,
            "step": step + 1,
        }
    event["assistant_prefix"] = ""
    event["continue_model"] = True

    if step == len(MODULE_WORKFLOWS[module]["checkpoints"]):
        if module not in state["completed_modules"]:
            state["completed_modules"].append(module)
            state["completed_modules"].sort()
            event["new_completed"] = [module]
        event["assistant_prefix"] = "✅ 模块 %s 完成。" % module
        if module == 4:
            state["foundation_report"] = {"status": "generating"}
            event["assistant_prefix"] += "正在生成模块 1–4 的 IP 定位初稿，请查看后再确认进入模块 5。"
            event["continue_model"] = False
        elif module == AVAILABLE_MODULE_COUNT:
            event["assistant_prefix"] += (
                "人设定位、模块 1–4 PDF、30 个选题和 3 篇口播文案已经交付。"
                "当前版本到这里停止，不会自动进入素材、报价或制作。"
            )
            event["continue_model"] = False
        else:
            state["current_module"] = module + 1
            state["module_step"] = 0
            event["assistant_prefix"] += "接下来进入模块 %s：%s。" % (
                state["current_module"], MODULE_WORKFLOWS[state["current_module"]]["name"]
            )
    _bump(state)
    return state, event


def _claim_has_current_evidence(claim, evidence):
    for match in re.finditer(re.escape(claim), evidence):
        prefix = CLAIM_BOUNDARY_RE.split(evidence[:match.start()])[-1]
        if not any(pattern.search(prefix) for pattern in (NEGATED_CLAIM_RE, FUTURE_CLAIM_RE, PAST_CLAIM_RE)):
            return True
    return False


def _strip_unsupported_acronym_expansions(text, evidence):
    """Keep user acronyms verbatim unless the user supplied the expansion."""
    return re.sub(
        r"\b([A-Z][A-Z0-9]{1,7})\s*[（(]([^()（）\n]{2,80})[）)]",
        lambda match: match.group(0) if match.group(2).strip() in evidence else match.group(1),
        str(text or ""),
    )


def _validate_confirmable_claims(draft, evidence):
    for match in RISKY_CLAIM_RE.finditer(draft):
        prefix = CLAIM_BOUNDARY_RE.split(draft[:match.start()])[-1]
        if any(pattern.search(prefix) for pattern in (NEGATED_CLAIM_RE, FUTURE_CLAIM_RE, PAST_CLAIM_RE)):
            continue
        claim = match.group(0)
        if not _claim_has_current_evidence(claim, evidence):
            raise HarnessError(
                "确认稿包含未经证实的身份、经历或结果用语“%s”；请删除，或改成不夸大的探索方向" % claim
            )


def _validate_module_four_story_claims(draft, evidence):
    draft_text = str(draft or "")
    evidence_text = str(evidence or "")
    compact_evidence = re.sub(r"\s+", "", evidence_text)
    if "故事内容" in draft_text:
        raise HarnessError(
            "模块 4 不再允许模型自由扩写过去式故事内容；每个节点必须改用逐字的事实原话或未来方向原话"
        )
    quotes = []
    for match in MODULE_FOUR_QUOTE_RE.finditer(draft_text):
        quote = re.sub(r"[*_`]+", "", match.group(1)).strip()
        quote = quote.strip("。！？!?；;，,").strip().strip("“”\"'")
        if quote:
            quotes.append(quote)
    if not quotes or len(quotes) > 8:
        raise HarnessError("模块 4 必须提供 1–8 条逐字的事实原话或未来方向原话")
    compact_quotes = [re.sub(r"[\W_]+", "", quote) for quote in quotes]
    if any(
        left != right and left in right
        for index, left in enumerate(compact_quotes)
        for other_index, right in enumerate(compact_quotes)
        if index != other_index
    ):
        raise HarnessError("模块 4 不能把一条已包含在长故事里的事实再单独列为重复故事")
    for quote in quotes:
        if re.sub(r"\s+", "", quote) not in compact_evidence:
            raise HarnessError("模块 4 的故事节点缺少可回查原话：“%s”" % quote[:120])
    exaggeration = MODULE_FOUR_EXAGGERATION_RE.search(draft_text)
    if exaggeration and exaggeration.group(0) not in evidence_text:
        raise HarnessError(
            "模块 4 把用户原话夸大成“%s”；请改回用户实际表达" % exaggeration.group(0)
        )
    if (
        MODULE_FOUR_PAST_CLIENT_RE.search(draft_text)
        and not MODULE_FOUR_PAST_CLIENT_EVIDENCE_RE.search(evidence_text)
    ):
        raise HarnessError(
            "模块 4 把目标人群或内容偏好编造成过去的客户经历；请改写为未来想服务的人群或待验证方向"
        )
    for term in MODULE_FOUR_INNER_STATE_TERMS:
        if term in draft_text and term not in evidence_text:
            raise HarnessError(
                "模块 4 编写了用户没有说过的内心感受“%s”；请删除或改为未来假设" % term
            )


def _validate_module_five_topics(updates, draft, evidence):
    topics = [item for item in updates if TOPIC_FIELD_RE.fullmatch(item["field"])]
    expected_fields = {
        "topic_%d_%02d" % (category, index)
        for category in range(1, 4)
        for index in range(1, 11)
    }
    if len(updates) != 30 or {item["field"] for item in topics} != expected_fields:
        raise HarnessError("模块 5 必须按 3 个种类、每类 10 个选题提供 topic_1_01 到 topic_3_10 的原话依据")
    if len({item["value"] for item in topics}) != 30:
        raise HarnessError("模块 5 的 30 个具体选题不能重复")
    unsupported_terms = set()
    for item in topics:
        quote = item["evidence_quote"]
        if item["kind"] != "ai_option" or not quote or quote not in evidence or item["value"] not in draft:
            raise HarnessError("模块 5 的每个具体选题都必须绑定用户原话或已确认的种类边界")
        unsupported_terms.update(
            term for term in TOPIC_EVIDENCE_TERMS
            if term in item["value"] and term not in quote
        )
    if unsupported_terms:
        raise HarnessError(
            "选题中的“%s”没有出现在对应的用户原话或已确认种类边界里；请一次性删除或改写这些词"
            % "、".join(sorted(unsupported_terms))
        )


def validate_model_decision(
    raw, value, evidence_text, expected_checkpoint=None, allow_partial_profile_updates=False
):
    state = normalize_state(value)
    if not isinstance(raw, dict):
        raise HarnessError("模型没有返回结构化对象")
    decision = str(raw.get("decision") or "")
    if decision not in {"ask_follow_up", "propose_checkpoint", "revise_intake", "answer_only"}:
        raise HarnessError("模型返回了无效 decision")
    try:
        checkpoint = int(raw.get("checkpoint", 0))
    except (TypeError, ValueError) as exc:
        raise HarnessError("模型返回了无效 checkpoint") from exc
    evidence = str(evidence_text or "")
    reply = _strip_unsupported_acronym_expansions(raw.get("reply"), evidence).strip()[:4000]
    draft = _strip_unsupported_acronym_expansions(raw.get("draft"), evidence).strip()[:12000]
    self_review = str(raw.get("self_review") or "").strip()[:500]
    expected_checkpoint = expected_checkpoint or state["module_step"] + 1
    expected_choice_checkpoint = is_choice_checkpoint(
        state["current_module"], expected_checkpoint
    )
    choice_checkpoint = decision == "propose_checkpoint" and is_choice_checkpoint(
        state["current_module"], checkpoint
    )
    raw_choices = raw.get("choices") or []
    if raw_choices and expected_choice_checkpoint and not choice_checkpoint:
        decision = "propose_checkpoint"
        checkpoint = expected_checkpoint
        choice_checkpoint = True
    if choice_checkpoint:
        if draft:
            raise ChoiceValidationError("choice_draft_present", "选择断点不能同时返回 Markdown draft")
        if raw.get("profile_updates"):
            raise ChoiceValidationError("choice_profile_updates", "选择断点不能携带 profile_updates")
        choices = validate_choices(raw_choices)
    else:
        if raw_choices:
            raise ChoiceValidationError("choice_unexpected", "当前断点不接受候选卡")
        choices = []
    if decision == "propose_checkpoint":
        if not choice_checkpoint and not draft and len(re.sub(r"\s+", "", reply)) >= 120:
            draft = reply
        if (draft or choices) and not self_review:
            self_review = "已按当前断点和现有证据整理，仍需用户本人确认。"
    if not reply:
        raise HarnessError("模型回复为空")
    if decision == "ask_follow_up" and state["module_step"] == 0:
        profile = state["ip_profile"]
        facts = profile["facts"]
        selections = profile["ai_selections"]
        pending = state.get("pending") if isinstance(state.get("pending"), dict) else {}
        known_fields = set(facts) | {
            str(item.get("field") or "")
            for item in pending.get("profile_updates") or [] if isinstance(item, dict)
        }
        confirmed_modules = {
            str(key).split("-", 1)[0] for key in profile["confirmed_outputs"]
        }
        if state["current_module"] == 1 and all(
            any(token in field for field in known_fields) for token in (
                "skill", "interest", "audience", "experience",
            )
        ) and any(
            token in field for field in known_fields for token in ("goal", "benefit", "value")
        ):
            raise HarnessError("模块 1 已有经历、能力、兴趣、目标人群和帮助目标，必须直接提炼候选关键词")
        if state["current_module"] == 2 and "1" in confirmed_modules:
            raise HarnessError("模块 1 已确认，必须从已有经历、行为和价值观直接提炼人格候选")
        if state["current_module"] == 3 and (
            {"1", "2"}.issubset(confirmed_modules)
            or (
                any(key.startswith("core_value_") for key in selections)
                and any(key in facts for key in ("target_audience", "target_audience_basis"))
                and any(key in facts for key in ("current_occupation", "current_identity_basis", "work_focus"))
            )
        ):
            raise HarnessError("模块 3 已有确认的价值观、目标人群和领域，必须直接提炼候选关键词")
        if state["current_module"] == 4 and (
            {"1", "2", "3"}.issubset(confirmed_modules)
            or (
                any(key in facts for key in ("turning_point_reflection", "career_transition", "career_transition_basis"))
                and any(key in facts for key in ("previous_jobs", "career_background", "past_workplace_feelings"))
            )
        ):
            raise HarnessError("模块 4 已有确认的真实经历和职业转折，必须直接提炼候选故事节点")
    if decision == "propose_checkpoint":
        if checkpoint != expected_checkpoint or checkpoint > len(MODULE_WORKFLOWS[state["current_module"]]["checkpoints"]):
            raise HarnessError("模型试图跨越当前断点")
        if (not choice_checkpoint and not draft) or not self_review:
            raise HarnessError("模型没有返回可确认内容或自评")
    elif decision == "revise_intake":
        if state["intake"]["status"] != "complete":
            raise HarnessError("基础资料尚未确认，不能重复发起修订")
        if state["completed_modules"] or state["module_step"]:
            raise HarnessError("模块已有确认结果，请通过新诊断修改基础资料")
        if checkpoint != 0 or not draft or not self_review:
            raise HarnessError("基础资料修订缺少完整核对稿或自评")
    elif checkpoint != 0 or draft:
        raise HarnessError("非断点回复不能携带推进内容")

    updates = raw.get("profile_updates") or []
    if not isinstance(updates, list) or len(updates) > 64:
        raise HarnessError("模型返回了无效 profile_updates")
    clean_updates = []
    for item in updates:
        if not isinstance(item, dict):
            raise HarnessError("模型返回了无效档案更新")
        field = str(item.get("field") or "").strip()
        value_text = _strip_unsupported_acronym_expansions(item.get("value"), evidence).strip()[:1200]
        kind = str(item.get("kind") or "")
        quote = str(item.get("evidence_quote") or "").strip()[:300]
        if not FIELD_RE.fullmatch(field) or not value_text or kind not in {"user_fact", "user_preference", "ai_option"}:
            raise HarnessError("模型返回了无效档案字段")
        if kind != "ai_option" and (not quote or quote not in evidence):
            if allow_partial_profile_updates and (
                decision == "ask_follow_up"
                or (
                    decision == "propose_checkpoint"
                    and (
                        state.get("pending")
                        or state["intake"]["status"] in {"collecting", "awaiting_confirmation", "editing"}
                        or (
                            state["current_module"] in {1, 2, 3, 4}
                            and not choice_checkpoint
                        )
                    )
                )
            ):
                continue
            raise HarnessError("模型档案更新缺少可回查的用户原话")
        clean_updates.append({"field": field, "value": value_text, "kind": kind, "evidence_quote": quote})
    if choice_checkpoint:
        choice_text = "\n".join(
            "%s\n%s\n%s\n%s" % (
                item["title"], item["summary"], item["reason"], item["caution"]
            )
            for item in choices
        )
        _validate_confirmable_claims(choice_text, evidence)
    elif decision in {"propose_checkpoint", "revise_intake"}:
        _validate_confirmable_claims(draft, evidence)
    if decision == "propose_checkpoint" and state["current_module"] == 4:
        _validate_module_four_story_claims(draft, evidence)
    if decision == "propose_checkpoint" and state["current_module"] == 5 and checkpoint == 1:
        if commercial_goal_gaps(state, clean_updates):
            raise HarnessError("生成选题前还需确认商业目的、产品或服务、主要平台和希望用户采取的动作")
    if decision == "propose_checkpoint" and state["current_module"] == 5 and checkpoint == 2:
        _validate_module_five_topics(clean_updates, draft, evidence)
    if (
        decision not in {"propose_checkpoint", "revise_intake"}
        and clean_updates
        and not (allow_partial_profile_updates and decision == "ask_follow_up")
    ):
        raise HarnessError("只有待确认断点或基础资料修订可以携带档案更新")
    try:
        confidence = min(1.0, max(0.0, float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "decision": decision,
        "checkpoint": checkpoint,
        "reply": reply,
        "draft": draft,
        "self_review": self_review,
        "choices": choices,
        "profile_updates": clean_updates,
        "confidence": confidence,
    }


def compile_module_five_topics(raw, value, evidence_text, evidence_sources):
    state = normalize_state(value)
    if state["current_module"] != 5 or state["module_step"] != 1:
        raise HarnessError("当前不在模块 5 的 3×10 选题断点")
    if not isinstance(raw, dict):
        raise HarnessError("模型没有返回模块 5 结构化对象")
    decision = str(raw.get("decision") or "")
    if decision not in {"ask_follow_up", "answer_only", "propose_checkpoint"}:
        raise HarnessError("模型返回了无效 decision")
    reply = str(raw.get("reply") or "").strip()[:4000]
    categories = raw.get("categories") or []
    self_review = str(raw.get("self_review") or "").strip()[:500]
    if not reply or not isinstance(categories, list):
        raise HarnessError("模型没有返回可用的模块 5 内容")
    source = str(
        ((state["ip_profile"].get("confirmed_outputs") or {}).get("5-1") or {}).get("content") or ""
    )
    if decision != "propose_checkpoint":
        if categories or self_review:
            raise HarnessError("非生成回复不能携带 3×10 内容")
        if decision == "ask_follow_up" and source and not state.get("pending"):
            raise HarnessError("模块 5 的 3 个种类已经确认，必须直接生成完整 3×10 选题")
        return validate_model_decision({
            "decision": decision,
            "checkpoint": 0,
            "reply": reply,
            "draft": "",
            "self_review": "",
            "profile_updates": [],
            "confidence": raw.get("confidence", 0),
        }, state, evidence_text)

    if len(categories) != 3 or not self_review or not source:
        raise HarnessError("模块 5 必须返回已确认 3 个种类下的完整 3×10 选题")
    names = [str(item.get("name") or "").strip() for item in categories if isinstance(item, dict)]
    if len(names) != 3 or len(set(names)) != 3 or any(not name or name not in source for name in names):
        raise HarnessError("模块 5 的 3 个种类名称必须与已确认版本一致")

    evidence = str(evidence_text or "")
    if not isinstance(evidence_sources, dict) or not evidence_sources:
        raise HarnessError("模块 5 缺少可引用证据")
    updates = []
    draft_parts = []
    for category_index, category in enumerate(categories, 1):
        topics = category.get("topics") or []
        if not isinstance(topics, list) or len(topics) != 10:
            raise HarnessError("模块 5 必须按每个种类 10 个选题生成")
        draft_parts.append("### %s" % names[category_index - 1])
        for topic_index, topic in enumerate(topics, 1):
            if not isinstance(topic, dict):
                raise HarnessError("模块 5 返回了无效选题")
            title = str(topic.get("title") or "").strip()[:200]
            evidence_id = str(topic.get("evidence_id") or "").strip()
            quote = str(evidence_sources.get(evidence_id) or "")[:300]
            for term, replacement in TOPIC_SAFE_REPLACEMENTS.items():
                if term not in quote:
                    title = title.replace(term, replacement)
            if not title or not quote or quote not in evidence:
                raise HarnessError("模块 5 的每个具体选题都必须绑定用户原话或已确认的种类边界")
            draft_parts.append("%d. %s" % (topic_index, title))
            updates.append({
                "field": "topic_%d_%02d" % (category_index, topic_index),
                "value": title,
                "kind": "ai_option",
                "evidence_quote": quote,
            })
        draft_parts.append("")
    if len({item["value"] for item in updates}) != 30:
        raise HarnessError("模块 5 的 30 个具体选题不能重复")
    return validate_model_decision({
        "decision": "propose_checkpoint",
        "checkpoint": 2,
        "reply": reply,
        "draft": "\n".join(draft_parts).strip(),
        "self_review": self_review,
        "profile_updates": updates,
        "confidence": raw.get("confidence", 0),
    }, state, evidence)


def confirmed_module_five_topics(value):
    state = normalize_state(value)
    source = str(
        ((state["ip_profile"].get("confirmed_outputs") or {}).get("5-2") or {}).get("content") or ""
    )
    categories = []
    current = None
    for line in source.splitlines():
        heading = re.fullmatch(r"###\s+(.+?)\s*", line)
        if heading:
            current = {"name": heading.group(1), "topics": []}
            categories.append(current)
            continue
        topic = re.fullmatch(r"\d+\.\s+(.+?)\s*", line)
        if current is not None and topic:
            current["topics"].append(topic.group(1))
    if len(categories) != 3 or any(len(item["topics"]) != 10 for item in categories):
        raise HarnessError("已确认的模块 5 选题库不是完整的 3×10 结构")
    return categories


def compile_module_five_confirmation(value):
    state = normalize_state(value)
    if state["current_module"] != 5 or state["module_step"] != 2:
        raise HarnessError("当前不在模块 5 的选题库确认断点")
    source = str(
        ((state["ip_profile"].get("confirmed_outputs") or {}).get("5-2") or {}).get("content") or ""
    )
    categories = confirmed_module_five_topics(state)

    featured = [(category["name"], category["topics"][0]) for category in categories]
    draft = "\n".join([
        "### 已确认的 3×10 选题库",
        *("- %s：10 个选题" % category["name"] for category in categories),
        "",
        "### 精选 3 个重点选题",
        *("%d. 【%s】%s" % (index, category, title)
          for index, (category, title) in enumerate(featured, 1)),
    ])
    return validate_model_decision({
        "decision": "propose_checkpoint",
        "checkpoint": 3,
        "reply": "30 个备选题已经保存，并从每个种类精选了 1 个重点选题；确认后会直接写成 3 篇完整口播文案。",
        "draft": draft,
        "self_review": "保留已确认的 3×10 题库，每个种类只精选排序第一的重点选题。",
        "profile_updates": [],
        "confidence": 1.0,
    }, state, source)


def compile_module_six_checkpoint(value, pack):
    state = normalize_state(value)
    if state["current_module"] != 6 or state["module_step"] not in {1, 2}:
        raise HarnessError("当前不在模块 6 的内容库确认断点")
    categories = (pack or {}).get("categories") if isinstance(pack, dict) else None
    if (
        (pack or {}).get("kind") != "content_pack_v1"
        or (pack or {}).get("format") != "featured_3_v1"
        or not isinstance(categories, list)
        or len(categories) != 3
        or any(len((item or {}).get("topics") or []) != 1 for item in categories)
    ):
        raise HarnessError("模块 6 尚未形成 3 个精选选题及对应完整文案")
    length_contract = (pack or {}).get("script_length") or {}
    minimum_chars = int(length_contract.get("min") or 120)
    maximum_chars = int(length_contract.get("max") or 1600)
    featured = []
    for category in categories:
        topic = category["topics"][0]
        versions = topic.get("versions") or []
        script = str((versions[-1] if versions else {}).get("content") or "").strip()
        script_chars = len(re.sub(r"\s+", "", script))
        if not minimum_chars <= script_chars <= maximum_chars:
            raise HarnessError("模块 6 的精选选题缺少完整文案")
        featured.append((category, topic, script))
    evidence = "\n".join(
        text
        for category, topic, script in featured
        for text in (str(category.get("name") or ""), str(topic.get("title") or ""), script)
    )
    if state["module_step"] == 1:
        draft = "\n\n".join(
            "### %d. %s｜%s\n**精选理由：** %s\n\n%s" % (
                index,
                category.get("name"),
                topic.get("title"),
                category.get("description") or "最符合当前定位与内容方向",
                script,
            )
            for index, (category, topic, script) in enumerate(featured, 1)
        )
        reply = "我已从 3×10 备选题库中按每个种类精选 1 个选题，并把 3 篇完整口播文案直接列在下面；右侧也会同时打开全文。"
        checkpoint = 2
    else:
        draft = "### 已完成的 3 篇完整文案\n" + "\n".join(
            "- 【%s】%s" % (category.get("name"), topic.get("title"))
            for category, topic, _script in featured
        )
        reply = "30 个备选题仍完整保留；本轮交付的是每个种类 1 篇、共 3 篇完整口播文案，下面只确认这 3 篇成品。"
        checkpoint = 3
    return validate_model_decision({
        "decision": "propose_checkpoint",
        "checkpoint": checkpoint,
        "reply": reply,
        "draft": draft,
        "self_review": "只读取 3 个精选选题及其完整正文，30 个其余选题仅作为备选题库。",
        "profile_updates": [],
        "confidence": 1.0,
    }, state, evidence)


def compile_module_six_style(value, evidence_text):
    state = normalize_state(value)
    if state["current_module"] != 6 or state["module_step"] != 0 or state.get("pending"):
        return None
    evidence = str(evidence_text or "")
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?；;])|\n+", evidence)
        if item.strip()
    ]
    relevant = [
        item for item in sentences
        if re.search(r"大白话|口语|分享|教学|讲解|语气|风格|口播|秒|分钟|minute|mins?|点赞|评论|收藏|留言|关注|私信|咨询", item, re.I)
    ]
    requirements = "；".join(
        dict.fromkeys(item.rstrip("。！？!?；;").strip() for item in relevant)
    )[:800]
    has_duration = bool(re.search(
        r"(?:\d+\s*(?:到|至|-)?\s*\d*|[一二两三四五六七八九十半]+)\s*"
        r"(?:秒|分钟|minute(?:s)?|mins?|m)(?![A-Za-z])",
        requirements,
        re.I,
    ))
    if not (
        re.search(r"大白话|口语|分享|教学|讲解|语气|风格", requirements)
        and re.search(r"点赞|评论|收藏|留言|关注|私信|咨询", requirements)
    ):
        return None
    defaulted_duration = not has_duration
    if defaulted_duration:
        requirements = (requirements + "；默认单篇 60–90 秒、250–350 字").strip("；")
    starts = [evidence.find(item) for item in relevant]
    quote_start = min((item for item in starts if item >= 0), default=-1)
    quote_end = max((evidence.find(item) + len(item) for item in relevant if item in evidence), default=-1)
    quote = evidence[quote_start:quote_end] if 0 <= quote_start < quote_end else ""
    if not defaulted_duration and (not quote or len(quote) > 300):
        return None
    return validate_model_decision({
        "decision": "propose_checkpoint",
        "checkpoint": 1,
        "reply": (
            "我已沿用你说过的表达方式和行动引导，并采用默认单篇 60–90 秒、250–350 字。"
            if defaulted_duration else
            "我已按你说过的表达方式、时长和行动引导整理统一口播标准，不再重复追问。"
        ),
        "draft": "### 3 篇精选口播统一标准\n- %s" % requirements,
        "self_review": (
            "沿用用户已提供的表达和行动偏好；仅在用户未指定时长时应用产品默认值。"
            if defaulted_duration else "只引用用户已提供的口播偏好，不补写新的要求。"
        ),
        "profile_updates": [{
            "field": "module_6_delivery_preferences",
            "value": requirements,
            "kind": "ai_option" if defaulted_duration else "user_preference",
            "evidence_quote": "" if defaulted_duration else quote,
        }],
        "confidence": 1.0,
    }, state, evidence)


def _reply_already_contains_draft(reply, draft):
    clean = lambda text: re.sub(r"[\W_]+", "", text).lower()
    reply_text, draft_text = clean(reply), clean(draft)
    draft_items = [
        clean(item) for item in re.findall(
            r"(?m)^\s*(?:\d+[.、)]|[-*])\s*(.+?)\s*$", draft
        ) if clean(item)
    ]
    repeated_items = sum(item in reply_text for item in draft_items)
    return bool(draft_text) and (
        draft_text in reply_text or SequenceMatcher(None, reply_text, draft_text).ratio() >= 0.75
        or (len(draft_items) >= 3 and repeated_items >= max(3, (len(draft_items) * 4 + 4) // 5))
    )


def _render_confirmable_reply(reply, draft, suffix):
    if _reply_already_contains_draft(reply, draft):
        reply = "请核对这版内容。"
    return reply + "\n\n" + draft + "\n\n" + suffix


def render_model_reply(decision):
    reply = decision["reply"]
    if decision["decision"] == "propose_checkpoint":
        if decision.get("choices"):
            return reply
        reply = _render_confirmable_reply(
            reply, decision["draft"],
            "内容不准确时直接告诉我；保留后我会继续，不需要你重复说明。",
        )
    elif decision["decision"] == "revise_intake":
        reply = _render_confirmable_reply(
            reply, decision["draft"],
            "这只是更新后的基础资料核对稿。请确认补充，或继续修改；当前模块不会自动推进。",
        )
    return reply


def apply_intake_decision(value, raw, evidence_text, current_message=""):
    state = normalize_state(value)
    intake = state["intake"]
    if intake["status"] == "complete":
        raise HarnessError("基础资料已经确认")
    declined = list(intake.get("declined_fields") or [])
    last_topic = (intake.get("asked_follow_ups") or [""])[-1]
    for field in _declined_intake_fields(current_message or evidence_text, last_topic):
        if field not in declined:
            declined.append(field)
    intake["declined_fields"] = declined
    candidate = deepcopy(raw) if isinstance(raw, dict) else raw
    if isinstance(candidate, dict):
        candidate_kind = str(candidate.get("decision") or "")
        if candidate_kind in {"ask_follow_up", "answer_only"}:
            candidate.update(checkpoint=0, draft="", self_review="")
            if candidate_kind == "answer_only":
                candidate["profile_updates"] = []
        elif candidate_kind == "revise_intake":
            candidate.update(decision="propose_checkpoint", checkpoint=1)
    prior_quotes = "\n".join(
        str(item.get("evidence_quote") or "")
        for item in intake.get("profile_updates") or []
        if isinstance(item, dict)
    )
    decision = validate_model_decision(
        candidate,
        state,
        str(evidence_text or "") + "\n" + prior_quotes,
        expected_checkpoint=1,
        allow_partial_profile_updates=True,
    )
    for item in (intake.get("profile_updates") or []) + decision["profile_updates"]:
        if (
            item.get("field") in INTAKE_FIELD_SET
            and DECLINE_RE.search(str(item.get("value") or ""))
            and item["field"] not in declined
        ):
            declined.append(item["field"])
    intake["declined_fields"] = declined
    merged_updates = {}
    for item in (intake.get("profile_updates") or []) + decision["profile_updates"]:
        if item["field"] not in declined:
            merged_updates[item["field"]] = item
    merged_update_list = list(merged_updates.values())
    if decision["decision"] in {"ask_follow_up", "propose_checkpoint"}:
        decision["profile_updates"] = merged_update_list
    coverage_gaps = intake_coverage_gaps(state, merged_update_list)
    gap_labels = "、".join(INTAKE_COVERAGE_LABELS[field] for field in coverage_gaps[:6])
    if (
        intake["status"] in {"collecting", "editing"}
        and decision["decision"] == "answer_only"
        and coverage_gaps
    ):
        raise HarnessError("采集表仍有未覆盖项目；回答用户后必须继续只追问一项")
    if (
        intake["status"] in {"collecting", "editing"}
        and decision["decision"] in {"ask_follow_up", "answer_only"}
        and not coverage_gaps
    ):
        raise HarnessError(
            "采集表已全部覆盖，不要继续追问或只作解释，直接生成完整核对稿"
        )
    if decision["decision"] == "propose_checkpoint":
        if coverage_gaps:
            raise HarnessError(
                "采集表仍有未覆盖项目：%s%s；不能生成核对稿，请只追问一个未覆盖项目"
                % (gap_labels, "等" if len(coverage_gaps) > 6 else "")
            )
        intake.update(
            status="awaiting_confirmation",
            round=3,
            draft=decision["draft"],
            profile_updates=decision["profile_updates"],
        )
    elif decision["decision"] == "ask_follow_up":
        follow_up_topic = intake_follow_up_topic(decision["reply"])
        canonical_topic = INTAKE_TOPIC_TO_FIELD.get(follow_up_topic, follow_up_topic)
        asked_follow_ups = intake.setdefault("asked_follow_ups", [])
        asked_canonical = {
            INTAKE_TOPIC_TO_FIELD.get(topic, topic) for topic in asked_follow_ups
        }
        if canonical_topic in intake.get("declined_fields", []):
            raise HarnessError("基础访谈追问了用户已经拒答的信息；请改问其他未回答项")
        if follow_up_topic in asked_follow_ups:
            raise HarnessError("基础访谈重复追问了已经问过的可选信息；请改问其他未回答项，或直接整理现有资料")
        if coverage_gaps and (not follow_up_topic or canonical_topic not in coverage_gaps):
            remaining = [field for field in coverage_gaps if field not in asked_canonical]
            if not remaining:
                raise HarnessError("采集表仍有未回答项，但所有项目都已询问；请让用户明确回答或跳过")
            canonical_topic = follow_up_topic = remaining[0]
            decision["reply"] = (
                "我已记下你这轮补充。接下来想了解“%s”，"
                "请按真实情况说说；不方便可以明确跳过。"
                % INTAKE_COVERAGE_LABELS[canonical_topic]
            )
        if follow_up_topic:
            asked_follow_ups.append(follow_up_topic)
        if merged_updates:
            intake["profile_updates"] = list(merged_updates.values())
        if intake["status"] == "awaiting_confirmation":
            intake["status"] = "editing"
    intake["field_statuses"] = intake_field_statuses(state)
    _bump(state)
    reply = decision["reply"]
    if decision["decision"] == "propose_checkpoint":
        reply = _render_confirmable_reply(
            reply, decision["draft"],
            "请确认资料，或者直接补充、纠正；我会说明理解错在哪里并立即重整。",
        )
    return state, decision, reply


def apply_model_decision(value, raw, evidence_text, pending_id=None, discard_pending=False):
    state = normalize_state(value)
    if discard_pending:
        state["pending"] = None
    candidate = deepcopy(raw) if isinstance(raw, dict) else raw
    if isinstance(candidate, dict):
        candidate_kind = str(candidate.get("decision") or "")
        if candidate_kind in {"ask_follow_up", "answer_only"}:
            candidate.update(checkpoint=0, draft="", self_review="")
            if candidate_kind == "answer_only":
                candidate["profile_updates"] = []
    pending = state.get("pending") if isinstance(state.get("pending"), dict) else None
    prior_updates = pending.get("profile_updates") or [] if pending else []
    prior_quotes = "\n".join(
        str(item.get("evidence_quote") or "")
        for item in prior_updates
        if isinstance(item, dict)
    )
    try:
        decision = validate_model_decision(
            candidate,
            state,
            str(evidence_text or "") + "\n" + prior_quotes,
            allow_partial_profile_updates=True,
        )
    except ChoiceValidationError:
        raise
    except HarnessError as exc:
        if is_choice_checkpoint(state["current_module"], state["module_step"] + 1):
            raise ChoiceValidationError("choice_invalid", str(exc)) from exc
        raise
    if decision["decision"] == "revise_intake":
        state["intake"].update(
            status="awaiting_confirmation",
            round=3,
            mode="revision",
            draft=decision["draft"],
            profile_updates=decision["profile_updates"],
        )
        # Any unconfirmed module draft used the old profile and must be rebuilt.
        state["pending"] = None
    elif decision["decision"] == "propose_checkpoint":
        target_id = pending_id or uuid.uuid4().hex
        choices = _materialize_choices(decision.get("choices") or [])
        state["pending"] = {
            "id": target_id,
            "kind": "checkpoint",
            "status": "awaiting_confirmation",
            "module": state["current_module"],
            "step": decision["checkpoint"],
            "draft": decision["draft"],
            "self_review": decision["self_review"],
            "profile_updates": decision["profile_updates"],
            "confidence": decision["confidence"],
        }
        if choices:
            state["pending"]["choices"] = choices
            state.pop("choice_generation", None)
            migration = state.get("migration")
            if isinstance(migration, dict):
                migration["needs_choice_generation"] = False
    elif decision["decision"] == "ask_follow_up":
        merged_updates = {}
        for item in prior_updates + decision["profile_updates"]:
            merged_updates.pop(item["field"], None)
            merged_updates[item["field"]] = item
        update_limit = 40 if state["current_module"] == 5 and state["module_step"] + 1 == 2 else 12
        if len(merged_updates) > update_limit:
            raise HarnessError("当前断点的待确认资料过多，请先整理完整核对稿")
        if pending or merged_updates:
            prior_status = pending.get("status") if pending else ""
            state["pending"] = {
                "id": (pending or {}).get("id") or pending_id or uuid.uuid4().hex,
                "kind": "checkpoint",
                "status": "editing" if prior_status in {"awaiting_confirmation", "editing"} else "collecting",
                "module": state["current_module"],
                "step": state["module_step"] + 1,
                "draft": (pending or {}).get("draft") or "",
                "self_review": (pending or {}).get("self_review") or "",
                "profile_updates": list(merged_updates.values()),
                "confidence": decision["confidence"],
            }
            if pending and pending.get("choices"):
                state["pending"]["choices"] = deepcopy(pending["choices"])
        else:
            state["pending"] = None
    _bump(state)
    return state, decision, render_model_reply(decision)


def intake_system_prompt(value):
    state = normalize_state(value)
    declined = "、".join(
        INTAKE_COVERAGE_LABELS.get(field, field)
        for field in state["intake"].get("declined_fields") or []
    ) or "无"
    gaps = intake_coverage_gaps(state)
    field_catalog = "、".join(
        "%s=%s" % (field, INTAKE_COVERAGE_LABELS[field])
        for field in gaps
    ) or "无"
    return """你是黄雀 IP12 的中立访谈教练，正在自然地了解用户基础情况。

必须覆盖《黄雀IP人设定位采集表》的全部项目，再补充两项核心能力、长期兴趣、主要平台和行动目标。年龄、性别、收入和手机号是敏感可选项：仍需自然询问一次，同时明说可以跳过，不得强迫；用户拒答、不知道或暂时没有时，记录为已跳过，不得再问。

当前已明确跳过：%s。
当前尚未覆盖（必须继续问或由用户明确跳过）：%s。格式为“字段名=含义”，不自创同义字段。

对话规则：
- 接受任意顺序和自然表达；用户可一次说一项或多项，不要求固定格式，不把访谈做成选择题。
- 先查看完整对话历史和当前待核对资料；已经回答、明确不知道或拒绝回答的内容不要重复追问。
- 每个问题最多主动问一次；本轮只问一个尚未覆盖的项目，优先顺着用户刚说的内容自然穿插。
- 用户没有正面回答时，不得猜测答案；只有明确说“跳过、不知道、暂时没有、不方便”才记为已跳过。
- 只要尚有未覆盖项，decision=ask_follow_up，只问一个问题；不得生成核对稿。
- 除核对稿和三选一候选外，reply 最多两句、约 35–70 个汉字；先简短承接本轮信息，再问一个问题，不罗列示例或重复已有资料。
- decision=ask_follow_up 时 checkpoint=0、draft 和 self_review 为空；可以把本轮已明确说出的用户事实或偏好放入 profile_updates，等待最终核对，绝不能宣布确认。
- 用户在采集阶段提问时先直接回答；仍有未覆盖项时，同一条回复再只问一个缺失问题。
- decision=answer_only 仅用于解释已经展示的待确认稿，或确实没有合适下一步的情况；不能让仍在采集或编辑中的访谈停在解释上。
- decision=answer_only 时 checkpoint=0，draft、self_review 和 profile_updates 都为空。
- 只有待覆盖列表为“无”时，才能 decision=propose_checkpoint、checkpoint=1；draft 必须合并全部已回答内容，并对跳过项只写“本人选择跳过”。
- 用户补充、纠正或反悔时，以最新原话为准，重新生成完整核对稿；“确认/可以”等字样与补充或纠正同时出现时，内容变更优先，绝不能宣布已确认。
- profile_updates 必须覆盖 draft 中全部结构化用户事实与偏好，使用英文 snake_case 字段，并逐字引用对话中的 evidence_quote；不得把 AI 推断写成用户事实。
- 不编造姓名、经历、收入或其他事实，不宣布基础资料已确认，不进入模块 1。
- 基础访谈不使用候选卡，所有回复 choices=[]。
- 最终只返回一个 JSON 对象；其中面向用户的 reply 和 draft 不提及 JSON、状态机、字段、数据库或系统规则。使用简体中文，具体、自然。""" % (declined, field_catalog)


def system_prompt(value):
    state = normalize_state(value)
    module = state["current_module"]
    workflow = MODULE_WORKFLOWS[module]
    next_step = state["module_step"] + 1
    if next_step > len(workflow["checkpoints"]):
        return f"""你是黄雀 IP12 的中立 IP 咨询教练，适用于任何职业和行业。

当前模块：{module}. {workflow['name']}，已经确认完成。

工作规则：
- 只回答用户对已确认内容的复盘或解释，decision=answer_only，checkpoint=0，draft 和 profile_updates 为空。
- 不重启模块，不宣布新的完成状态，不进入尚未开放的模块。
- 不预设行业，不编造事实、案例、趋势或效果承诺。
- 当前没有候选卡，choices=[]。
- 最终只返回一个 JSON 对象；其中面向用户的 reply 和 draft 不提及 JSON、状态机、字段、数据库或系统规则。
- 使用简体中文，具体、自然。"""
    checkpoint = workflow["checkpoints"][next_step - 1]
    editing = state.get("pending") if isinstance(state.get("pending"), dict) else None
    editing_note = ""
    if editing and editing.get("status") == "editing":
        if editing.get("choices"):
            editing_note = "\n用户正在修改上一组三个候选；候选正文会作为不可信资料单独提供。"
        else:
            editing_note = "\n用户正在修改的旧稿：\n" + str(editing.get("draft") or "")[:4000]
    choice_rules = ""
    if is_choice_checkpoint(module, next_step):
        choice_rules = f"""
- 当前是固定三选一断点。信息足够时 decision=propose_checkpoint、checkpoint=2，choices 必须恰好 {CHOICE_COUNT} 项，draft 和 profile_updates 必须为空。
- 每项 choices 只含 title、summary、reason、caution、recommended；长度分别不超过 {CHOICE_FIELD_LIMITS['title']}、{CHOICE_FIELD_LIMITS['summary']}、{CHOICE_FIELD_LIMITS['reason']}、{CHOICE_FIELD_LIMITS['caution']} 个 Unicode 字符，标题不得重复，最多一项 recommended=true。
- reply 只用一句话说明现在要选择什么，不在 reply 中重复三项内容，不替用户选择。"""
    story_rules = ""
    if module == 4:
        story_rules = """
- 模块 4 的过去经历只能来自用户明确说过的真实事件；不得补写客户数量、客户反馈、他人心理状态、个人感受或天赋评价。
- 目标人群、希望帮助的人和内容偏好只能写成未来想服务的人群、未来内容方向或待验证假设，绝不能改写成已经服务过的客户经历。
- 每个节点必须单独包含“事实原话：”或“未来方向原话：”，后面逐字引用对话或已确认资料；不得使用“故事内容：”自由改写过去经历。
- 标题、故事类型、情绪点和传播场景可以是 AI 建议，但必须明确作为包装建议，不能夹带新的过去事实。
- 不使用“过人的天赋”“自我怀疑”“害怕”“成就感”等用户没有亲口说过的评价或内心感受。证据不足时少写节点，不能为了凑数量编故事。"""
    commercial_rules = ""
    if module == 5 and next_step == 1:
        commercial_rules = """
- 生成内容种类前必须确认 business_goal、offer、primary_platform、desired_action；缺一项就只追问一个最重要缺口。
- time_budget 和 three_month_goal 是推荐信息，用户跳过时不阻塞选题。
- 本断点的完整核对稿必须同时写清商业目标、产品或服务、主要平台、希望用户采取的动作和 3 个内容种类。"""
    script_rules = ""
    if module == 6 and next_step == 1:
        script_rules = """
- 本断点只确认 3 篇口播共用的表达风格、单篇时长和行动目标，不提前写口播正文。
- 用户没有明确指定其他时长时，默认单篇 60–90 秒、250–350 字；只有用户明确选择其他时长才覆盖默认值。"""
    return f"""你是黄雀 IP12 的中立 IP 咨询教练，适用于任何职业和行业。

当前模块：{module}. {workflow['name']}
当前唯一允许处理的断点：{next_step}. {checkpoint}
本模块所需资料：{workflow['required']}
{editing_note}

工作规则：
- 每轮只处理当前断点，禁止输出后续断点、宣布模块完成或切换模块。
- 接受用户任意顺序、一次一项或多项的自然表达，不要求固定格式，不把访谈做成选择题。
- 先查看完整对话历史和已确认资料；已经回答、明确不知道或拒绝回答的内容不要重复追问。
- 信息不足时 decision=ask_follow_up，只问一个最有价值、容易回答且尚未回答的问题。
- 除当前断点的完整核对稿或三选一候选外，reply 最多两句、约 35–70 个汉字；先承接本轮新增信息，再只问一个问题。
- decision=ask_follow_up 时 checkpoint=0、draft 和 self_review 为空；把本轮已明确说出的用户事实或偏好放入 profile_updates，等待最终核对。
- 用户只是在提问或跑题时 decision=answer_only，checkpoint=0，draft、self_review 和 profile_updates 都为空。
- 非固定三选一断点信息足够时 decision=propose_checkpoint，draft 只包含当前断点的完整可确认内容，profile_updates 必须是当前断点的完整最新快照。
{choice_rules}
{story_rules}
{commercial_rules}
{script_rules}
- 用户只是询问、讨论现有草稿或暂时跑题时 decision=answer_only，不改变断点；用户补充、纠正或反悔时，重做当前断点的完整草稿。
- 用户指出重复、理解错误、遗漏或体验问题时，先用一句话明确说明刚才错在哪里，再给出已经采取的修正；能从上下文判断时立即修改，不能把定位和操作责任推回用户。
- 不复述已经确认的完整内容。只说明本轮新增、删除或改变的部分；需要核对完整稿时再展示当前完整稿一次。
- 仅当模块 1 尚无任何已确认断点，用户明确补充或纠正已经确认的基础资料时，decision=revise_intake、checkpoint=0；draft 必须合并原基础资料与本轮补充形成完整核对稿。这不是模块回答，不得推进模块。模块已有确认结果时不要使用 revise_intake，应说明需要新建诊断以免污染既有产出。
- “确认/可以”等字样与补充或纠正同时出现时，内容变更优先，绝不能宣布确认或推进。
- profile_updates 使用英文 snake_case 字段；用户事实和偏好必须逐字引用用户原话，AI 方案用 ai_option 且 evidence_quote 为空（模块 5 断点 2 的选题来源除外）。
- draft 中的当前身份、经历、能力和结果必须能在用户原话中直接找到依据；未来目标只能写成未来目标，AI 候选只能写成候选，不得包装成既有成绩。
- 不得把用户称为专家、顾问、战略家、导师、资深人士或领军人物，除非用户明确说这是自己当前真实身份；不得把“希望成为”改写成“现在就是”。
- 不预设美业、直销或创业身份；不得编造市场趋势、案例、收入、经历或效果承诺。
- 模块 5 断点 2 必须严格输出 3 个种类、每类 10 个具体选题，并在 profile_updates 中按种类使用 topic_1_01 到 topic_1_10、topic_2_01 到 topic_2_10、topic_3_01 到 topic_3_10：kind=ai_option、value 与 draft 题目完全一致、evidence_quote 逐字引用直接支撑该题目的用户原话。泛泛的“垂直行业”不能支撑医疗、金融等具体行业，也不能支撑客户案例或成功结果；证据不足时只围绕用户真实过程、边界、反思和待验证计划出题。
- 知识资料只学方法和结构，不能抄写示例人物内容。
- self_review 只供内部校验，不得在 reply 或 draft 中显示“自评”字样。
- 所有回复都必须返回 choices 字段；非三选一断点 choices=[]。
- 最终只返回一个 JSON 对象；其中面向用户的 reply 和 draft 不提及 JSON、状态机、字段、数据库、内部步骤编号或系统规则。
- 使用简体中文，具体、自然，不喊口号。"""
