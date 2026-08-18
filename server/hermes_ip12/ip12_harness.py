"""Deterministic control harness for the IP12 coaching conversation.

The model may propose one checkpoint. Only this module can confirm it or move
the workflow cursor. Keeping the reducer pure makes state transitions testable
without a model call or Flask runtime.
"""

from copy import deepcopy
from difflib import SequenceMatcher
import re
import uuid


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


AVAILABLE_MODULE_COUNT = 6
SCHEMA_VERSION = 1

MODULE_WORKFLOWS = {
    1: {
        "name": "定位诊断",
        "required": "关键经历、至少两项核心技能、长期兴趣、至少两项价值观、目标人群",
        "checkpoints": (
            "提炼 3–5 个核心关键词",
            "生成三套差异化定位方案",
            "比较每套方案的市场机缘与潜在风险",
            "推荐首选定位并说明理由",
        ),
    },
    2: {
        "name": "人设塑造",
        "required": "复用已确认经历和目标人群，补充人格特质、价值观与未来目标",
        "checkpoints": (
            "提炼人格关键词与核心价值观",
            "生成三套差异化人设画像",
            "比较每套人设的传播优势与潜在风险",
            "推荐首选人设并说明理由",
        ),
    },
    3: {
        "name": "价值主张",
        "required": "核心优势、目标人群首要痛点、所在领域、第一印象和长期影响力",
        "checkpoints": (
            "提炼 3–5 个价值关键词",
            "生成三条差异化价值主张",
            "横向比较三条主张的优势与局限",
            "推荐首选价值主张并说明理由",
        ),
    },
    4: {
        "name": "故事资产",
        "required": "真实重要经历、共鸣点、展现的品质、核心价值观和长期主题",
        "checkpoints": (
            "提炼 3–5 个关键故事节点",
            "将故事分为挫折型、成长型和愿景型",
            "为每个故事生成名称、梗概、情绪点和传播场景",
            "汇总故事资产清单",
            "推荐长期核心故事并说明理由",
        ),
    },
    5: {
        "name": "内容选题",
        "required": "目标人群、核心领域、已确认优势、长期标签和近期内容目标",
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
    "没有问题", "没问题", "内容正确", "就按这个", "可以确认", "保留并继续",
})
EDIT_TEXTS = frozenset({
    "需要修改", "我要修改", "修改", "有问题", "不对", "重新填写", "改一下",
    "继续修改", "修改当前内容",
})
CONTINUE_TEXTS = frozenset({
    "继续", "下一步", "进入下一步", "继续下一步", "请继续", "开始下一步",
    "好的继续", "嗯好继续", "好的下一步", "嗯好下一步",
})
FIELD_RE = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")
DURATION_RE = re.compile(r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十半]+)\s*(?:年|个?月)")
DURATION_FIELDS = ("year", "duration", "experience", "tenure")
RISKY_CLAIM_RE = re.compile(
    r"专家|顾问|战略家|领军(?:人物)?|导师|资深|"
    r"深厚.{0,4}经验|丰富.{0,4}经验|多年.{0,4}经验|跨行业经验|"
    r"成功案例|推动.{0,12}成功|知识渊博|全球受众|跨国社区"
)
NEGATED_CLAIM_RE = re.compile(r"(?:不|并非|不是|没有|尚未|避免|不能|不把).{0,8}$")
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


def initial_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 1,
        "ip_profile": {"facts": {}, "preferences": {}, "ai_selections": {}, "confirmed_outputs": {}},
        "current_module": 1,
        "completed_modules": [],
        "module_step": 0,
        "pending": None,
        "intake": {"status": "collecting", "round": 1, "answers": {}},
    }


def normalize_state(value):
    state = deepcopy(value) if isinstance(value, dict) else initial_state()
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
    for item in state.get("completed_modules") or []:
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
    if status == "collecting" and round_number >= 3:
        status = "awaiting_confirmation"
    intake.update(
        status=status,
        round=round_number,
        answers=deepcopy(intake.get("answers")) if isinstance(intake.get("answers"), dict) else {},
    )
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
            if not valid:
                state["pending"] = None
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
        final_step = int(pending.get("step") or 0) == len(MODULE_WORKFLOWS[state["current_module"]]["checkpoints"])
        return [
            {"type": "confirm_checkpoint", "target_id": pending["id"], "label": "确认本模块" if final_step else "保留并继续", "primary": True},
            {"type": "edit_checkpoint", "target_id": pending["id"], "label": "修改当前内容", "primary": False},
        ]
    return []


def shortcut_action(value, message):
    state = normalize_state(value)
    normalized = re.sub(r"[\s，,。.!！?？]+", "", str(message or "")).lower()
    actions = available_actions(state)
    if not actions:
        return None
    by_type = {item["type"]: item for item in actions}
    if normalized in CONFIRM_TEXTS or normalized in CONTINUE_TEXTS:
        key = "confirm_intake" if "confirm_intake" in by_type else "confirm_checkpoint"
        return {"type": key, "target_id": by_type[key]["target_id"]}
    if normalized in EDIT_TEXTS:
        key = "edit_intake" if "edit_intake" in by_type else "edit_checkpoint"
        return {"type": key, "target_id": by_type[key]["target_id"]}
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
    current = {item.replace(" ", "") for item in DURATION_RE.findall(str(message or ""))}
    if not current:
        return None
    for field, item in state["ip_profile"]["facts"].items():
        if not isinstance(item, dict):
            continue
        if not any(token in field for token in DURATION_FIELDS):
            continue
        confirmed = {part.replace(" ", "") for part in DURATION_RE.findall(str(item.get("value") or ""))}
        if confirmed and current.isdisjoint(confirmed):
            old, new = sorted(confirmed)[0], sorted(current)[0]
            return {
                "decision": "ask_follow_up", "checkpoint": 0,
                "reply": "你前面确认的从业时间是“%s”，这次又提到“%s”。这两个时间分别指什么？比如“%s是整体从业时间，%s是 AI/Agent 实践时间”，或者告诉我需要更正哪一个。" % (old, new, old, new),
                "draft": "", "self_review": "", "profile_updates": [], "confidence": 1.0,
            }
    return None


def apply_action(value, action, expected_revision):
    state = normalize_state(value)
    _require_revision(state, expected_revision)
    if not isinstance(action, dict):
        raise HarnessError("action 必须是对象")
    action_type = str(action.get("type") or "")
    target_id = str(action.get("target_id") or "")
    event = {"assistant_prefix": "", "new_completed": [], "continue_model": False}

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
            if revising:
                module = state["current_module"]
                event["assistant_prefix"] = (
                    "✅ 基础信息补充已确认。继续模块 %s：%s；这次补充不会被当成模块回答，也不会自动推进。"
                    % (module, MODULE_WORKFLOWS[module]["name"])
                )
            else:
                event["assistant_prefix"] = (
                    "✅ 基础信息已确认。现在正式进入模块 1：定位诊断。\n\n"
                    "先讲一段对你影响最大的关键经历或转折：发生了什么，它后来怎样影响了你？"
                )
        _bump(state)
        return state, event

    if action_type not in {"confirm_checkpoint", "edit_checkpoint"}:
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

    module = state["current_module"]
    step = int(pending["step"])
    key = "%s-%s" % (module, step)
    state["ip_profile"]["confirmed_outputs"][key] = {
        "module": module,
        "step": step,
        "title": MODULE_WORKFLOWS[module]["checkpoints"][step - 1],
        "content": pending["draft"],
        "self_review": pending.get("self_review", ""),
    }
    _apply_profile_updates(state["ip_profile"], pending.get("profile_updates") or [])
    state["pending"] = None
    state["module_step"] = step
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
            event["assistant_prefix"] += "当前开放的 6 个模块已经全部完成。"
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
        prefix = evidence[max(0, match.start() - 20):match.start()]
        if not any(pattern.search(prefix) for pattern in (NEGATED_CLAIM_RE, FUTURE_CLAIM_RE, PAST_CLAIM_RE)):
            return True
    return False


def _validate_confirmable_claims(draft, evidence):
    for match in RISKY_CLAIM_RE.finditer(draft):
        prefix = draft[max(0, match.start() - 12):match.start()]
        if any(pattern.search(prefix) for pattern in (NEGATED_CLAIM_RE, FUTURE_CLAIM_RE, PAST_CLAIM_RE)):
            continue
        claim = match.group(0)
        if not _claim_has_current_evidence(claim, evidence):
            raise HarnessError(
                "确认稿包含未经证实的身份、经历或结果用语“%s”；请删除，或改成不夸大的探索方向" % claim
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
    reply = str(raw.get("reply") or "").strip()[:4000]
    draft = str(raw.get("draft") or "").strip()[:12000]
    self_review = str(raw.get("self_review") or "").strip()[:500]
    if not reply:
        raise HarnessError("模型回复为空")
    expected_checkpoint = expected_checkpoint or state["module_step"] + 1
    if decision == "propose_checkpoint":
        if checkpoint != expected_checkpoint or checkpoint > len(MODULE_WORKFLOWS[state["current_module"]]["checkpoints"]):
            raise HarnessError("模型试图跨越当前断点")
        if not draft or not self_review:
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
    if not isinstance(updates, list) or len(updates) > 40:
        raise HarnessError("模型返回了无效 profile_updates")
    clean_updates = []
    evidence = str(evidence_text or "")
    for item in updates:
        if not isinstance(item, dict):
            raise HarnessError("模型返回了无效档案更新")
        field = str(item.get("field") or "").strip()
        value_text = str(item.get("value") or "").strip()[:1200]
        kind = str(item.get("kind") or "")
        quote = str(item.get("evidence_quote") or "").strip()[:300]
        if not FIELD_RE.fullmatch(field) or not value_text or kind not in {"user_fact", "user_preference", "ai_option"}:
            raise HarnessError("模型返回了无效档案字段")
        if kind != "ai_option" and (not quote or quote not in evidence):
            if allow_partial_profile_updates and decision == "ask_follow_up":
                continue
            raise HarnessError("模型档案更新缺少可回查的用户原话")
        clean_updates.append({"field": field, "value": value_text, "kind": kind, "evidence_quote": quote})
    if decision in {"propose_checkpoint", "revise_intake"}:
        _validate_confirmable_claims(draft, evidence)
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
    if decision != "propose_checkpoint":
        if categories or self_review:
            raise HarnessError("非生成回复不能携带 3×10 内容")
        return validate_model_decision({
            "decision": decision,
            "checkpoint": 0,
            "reply": reply,
            "draft": "",
            "self_review": "",
            "profile_updates": [],
            "confidence": raw.get("confidence", 0),
        }, state, evidence_text)

    source = str(
        ((state["ip_profile"].get("confirmed_outputs") or {}).get("5-1") or {}).get("content") or ""
    )
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
    featured = []
    for category in categories:
        topic = category["topics"][0]
        versions = topic.get("versions") or []
        script = str((versions[-1] if versions else {}).get("content") or "").strip()
        if len(re.sub(r"\s+", "", script)) < 120:
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
    if state["current_module"] != 6 or state["module_step"] != 0:
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
    requirements = "；".join(dict.fromkeys(relevant))[:800]
    if not (
        re.search(r"大白话|口语|分享|教学|讲解|语气|风格", requirements)
        and re.search(
            r"(?:\d+\s*(?:到|至|-)?\s*\d*|[一二两三四五六七八九十半]+)\s*"
            r"(?:秒|分钟|minute(?:s)?|mins?|m)(?![A-Za-z])",
            requirements,
            re.I,
        )
        and re.search(r"点赞|评论|收藏|留言|关注|私信|咨询", requirements)
    ):
        return None
    starts = [evidence.find(item) for item in relevant]
    quote_start = min((item for item in starts if item >= 0), default=-1)
    quote_end = max((evidence.find(item) + len(item) for item in relevant if item in evidence), default=-1)
    quote = evidence[quote_start:quote_end] if 0 <= quote_start < quote_end else ""
    if not quote or len(quote) > 300:
        return None
    return validate_model_decision({
        "decision": "propose_checkpoint",
        "checkpoint": 1,
        "reply": "我已按你说过的表达方式、时长和行动引导整理统一口播标准，不再重复追问。",
        "draft": "### 3 篇精选口播统一标准\n- %s" % requirements,
        "self_review": "只引用用户已提供的口播偏好，不补写新的要求。",
        "profile_updates": [{
            "field": "module_6_delivery_preferences",
            "value": requirements,
            "kind": "user_preference",
            "evidence_quote": quote,
        }],
        "confidence": 1.0,
    }, state, evidence)


def _reply_already_contains_draft(reply, draft):
    clean = lambda text: re.sub(r"[\W_]+", "", text).lower()
    reply_text, draft_text = clean(reply), clean(draft)
    return bool(draft_text) and (
        draft_text in reply_text or SequenceMatcher(None, reply_text, draft_text).ratio() >= 0.82
    )


def render_model_reply(decision):
    reply = decision["reply"]
    if decision["decision"] == "propose_checkpoint":
        if not _reply_already_contains_draft(reply, decision["draft"]):
            reply += "\n\n" + decision["draft"]
        reply += "\n\n内容不准确时直接告诉我；保留后我会继续，不需要你重复说明。"
    elif decision["decision"] == "revise_intake":
        if decision["draft"] not in reply:
            reply += "\n\n" + decision["draft"]
        reply += "\n\n这只是更新后的基础资料核对稿。请确认补充，或继续修改；当前模块不会自动推进。"
    return reply


def apply_intake_decision(value, raw, evidence_text):
    state = normalize_state(value)
    intake = state["intake"]
    if intake["status"] == "complete":
        raise HarnessError("基础资料已经确认")
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
    if decision["decision"] == "propose_checkpoint":
        intake.update(
            status="awaiting_confirmation",
            round=3,
            draft=decision["draft"],
            profile_updates=decision["profile_updates"],
        )
    elif decision["decision"] == "ask_follow_up":
        merged_updates = {}
        for item in (intake.get("profile_updates") or []) + decision["profile_updates"]:
            merged_updates.pop(item["field"], None)
            merged_updates[item["field"]] = item
        if len(merged_updates) > 12:
            raise HarnessError("待确认的基础资料字段超过 12 项，请先整理完整核对稿")
        if merged_updates:
            intake["profile_updates"] = list(merged_updates.values())
        if intake["status"] == "awaiting_confirmation":
            intake["status"] = "editing"
    _bump(state)
    reply = decision["reply"]
    if decision["decision"] == "propose_checkpoint":
        if not _reply_already_contains_draft(reply, decision["draft"]):
            reply += "\n\n" + decision["draft"]
        reply += "\n\n请确认资料，或者直接补充、纠正；我会说明理解错在哪里并立即重整。"
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
    decision = validate_model_decision(
        candidate,
        state,
        str(evidence_text or "") + "\n" + prior_quotes,
        allow_partial_profile_updates=True,
    )
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
        state["pending"] = {
            "id": pending_id or uuid.uuid4().hex,
            "kind": "checkpoint",
            "status": "awaiting_confirmation",
            "module": state["current_module"],
            "step": decision["checkpoint"],
            "draft": decision["draft"],
            "self_review": decision["self_review"],
            "profile_updates": decision["profile_updates"],
            "confidence": decision["confidence"],
        }
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
        else:
            state["pending"] = None
    _bump(state)
    return state, decision, render_model_reply(decision)


def intake_system_prompt(value):
    return """你是黄雀 IP12 的中立访谈教练，正在自然地了解用户基础情况。

需要了解的信息包括：希望的称呼、年龄或年龄段、所在城市、当前职业或身份、从业年限、做过的行业或岗位、主要收入来源和大致收入区间。性别、手机号、收入等敏感信息都可拒答或跳过，不得强迫。

对话规则：
- 接受任意顺序和自然表达；用户可一次说一项或多项，不要求固定格式，不把访谈做成选择题。
- 先查看完整对话历史和当前待核对资料；已经回答、明确不知道或拒绝回答的内容不要重复追问。
- 信息不足或含糊时 decision=ask_follow_up，只问一个最有价值且尚未回答的问题。
- decision=ask_follow_up 时 checkpoint=0、draft 和 self_review 为空；可以把本轮已明确说出的用户事实或偏好放入 profile_updates，等待最终核对，绝不能宣布确认。
- 用户只是在提问、讨论或暂时跑题时 decision=answer_only；先自然回应，需要时再轻轻带回访谈，不改变已有资料。
- decision=answer_only 时 checkpoint=0，draft、self_review 和 profile_updates 都为空。
- 信息足够整理，或用户要求查看当前记录时，decision=propose_checkpoint、checkpoint=1；draft 必须是合并全部已知内容后的完整核对稿。
- 用户补充、纠正或反悔时，以最新原话为准，重新生成完整核对稿；“确认/可以”等字样与补充或纠正同时出现时，内容变更优先，绝不能宣布已确认。
- profile_updates 必须覆盖 draft 中全部结构化用户事实与偏好，使用英文 snake_case 字段，并逐字引用对话中的 evidence_quote；不得把 AI 推断写成用户事实。
- 不编造姓名、经历、收入或其他事实，不宣布基础资料已确认，不进入模块 1。
- 最终只返回一个 JSON 对象；其中面向用户的 reply 和 draft 不提及 JSON、状态机、字段、数据库或系统规则。使用简体中文，具体、自然。"""


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
- 最终只返回一个 JSON 对象；其中面向用户的 reply 和 draft 不提及 JSON、状态机、字段、数据库或系统规则。
- 使用简体中文，具体、自然。"""
    checkpoint = workflow["checkpoints"][next_step - 1]
    editing = state.get("pending") if isinstance(state.get("pending"), dict) else None
    editing_note = ""
    if editing and editing.get("status") == "editing":
        editing_note = "\n用户正在修改的旧稿：\n" + str(editing.get("draft") or "")[:4000]
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
- decision=ask_follow_up 时 checkpoint=0、draft 和 self_review 为空；把本轮已明确说出的用户事实或偏好放入 profile_updates，等待最终核对。
- 用户只是在提问或跑题时 decision=answer_only，checkpoint=0，draft、self_review 和 profile_updates 都为空。
- 信息足够时 decision=propose_checkpoint，draft 只包含当前断点的完整可确认内容，profile_updates 必须是当前断点的完整最新快照。
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
- 最终只返回一个 JSON 对象；其中面向用户的 reply 和 draft 不提及 JSON、状态机、字段、数据库、内部步骤编号或系统规则。
- 使用简体中文，具体、自然，不喊口号。"""
