"""Deterministic control harness for the IP12 coaching conversation.

The model may propose one checkpoint. Only this module can confirm it or move
the workflow cursor. Keeping the reducer pure makes state transitions testable
without a model call or Flask runtime.
"""

from copy import deepcopy
import re
import uuid


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
            "提炼目标人群的高频问题与需求",
            "设计不少于 15 个具体选题",
            "分类形成内容选题库",
            "推荐最优 3 个重点选题并说明理由",
        ),
    },
    6: {
        "name": "文案口播",
        "required": "主题、目标人群、身份设定、传播目标和表达风格偏好",
        "checkpoints": (
            "生成共情型、震撼型和故事型三版口播文案",
            "逐条优化口播节奏、字幕点和情绪张力",
            "推荐最优版本并说明理由",
        ),
    },
}

CONFIRM_TEXTS = frozenset({"确认", "确认无误", "没有问题", "没问题", "内容正确", "就按这个", "可以确认"})
EDIT_TEXTS = frozenset({"需要修改", "我要修改", "修改", "有问题", "不对", "重新填写", "改一下"})
FIELD_RE = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")

DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["ask_follow_up", "propose_checkpoint", "answer_only"],
        },
        "checkpoint": {"type": "integer", "minimum": 0, "maximum": 5},
        "reply": {"type": "string", "maxLength": 4000},
        "draft": {"type": "string", "maxLength": 12000},
        "self_review": {"type": "string", "maxLength": 500},
        "profile_updates": {
            "type": "array",
            "maxItems": 8,
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
                and pending.get("status") in {"awaiting_confirmation", "editing"}
                and isinstance(pending.get("id"), str)
            )
            if not valid:
                state["pending"] = None
    return state


def _bump(state):
    state["revision"] = int(state.get("revision", 1)) + 1
    return state


def _intake_summary(answers):
    correction = str(answers.get("确认或修正") or "").strip()
    rows = [
        "已记录。请核对以下基础资料：",
        "",
        "- **基本信息**：%s" % (answers.get("基本信息") or "未填写"),
        "- **职业背景**：%s" % (answers.get("职业背景") or "未填写"),
    ]
    if correction:
        rows.append("- **本轮修正**：%s" % correction)
    rows.extend(["", "请点击“确认资料”；需要调整时点击“我要修改”。"])
    return "\n".join(rows)


def handle_intake_message(value, message):
    state = normalize_state(value)
    intake = state["intake"]
    if intake["status"] == "complete":
        raise HarnessError("基础资料已经确认")
    text = str(message or "").strip()
    if not text:
        raise HarnessError("消息不能为空")
    normalized = re.sub(r"[\s，,。.!！?？]+", "", text).lower()
    if intake["status"] == "collecting" and intake["round"] == 1:
        if normalized in {"开始", "开始诊断", "开始吧", "你好", "您好", "hi", "hello"}:
            return state, "请先告诉我：称呼、年龄段、所在城市；手机号可以不填。"
        intake["answers"]["基本信息"] = text
        intake.update(round=2, status="collecting")
        _bump(state)
        return state, (
            "收到。继续补充职业背景：当前职业或身份、从业年限、做过的行业或岗位、"
            "主要收入来源和年收入区间。"
        )
    if intake["status"] == "collecting" and intake["round"] == 2:
        intake["answers"]["职业背景"] = text
        intake.update(round=3, status="awaiting_confirmation")
        _bump(state)
        return state, _intake_summary(intake["answers"])
    if normalized in EDIT_TEXTS:
        intake["status"] = "editing"
        _bump(state)
        return state, "请直接写出正确内容，并说明要替换“基本信息”还是“职业背景”。"
    intake["answers"]["确认或修正"] = text
    intake.update(round=3, status="awaiting_confirmation")
    _bump(state)
    return state, _intake_summary(intake["answers"])


def available_actions(value):
    state = normalize_state(value)
    intake = state["intake"]
    if intake["status"] == "awaiting_confirmation":
        target = "intake-%s" % state["revision"]
        return [
            {"type": "confirm_intake", "target_id": target, "label": "确认资料", "primary": True},
            {"type": "edit_intake", "target_id": target, "label": "我要修改", "primary": False},
        ]
    pending = state.get("pending")
    if isinstance(pending, dict) and pending.get("status") == "awaiting_confirmation":
        return [
            {"type": "confirm_checkpoint", "target_id": pending["id"], "label": "确认这一步", "primary": True},
            {"type": "edit_checkpoint", "target_id": pending["id"], "label": "需要修改", "primary": False},
        ]
    return []


def shortcut_action(value, message):
    state = normalize_state(value)
    normalized = re.sub(r"[\s，,。.!！?？]+", "", str(message or "")).lower()
    actions = available_actions(state)
    if not actions:
        return None
    by_type = {item["type"]: item for item in actions}
    if normalized in CONFIRM_TEXTS:
        key = "confirm_intake" if "confirm_intake" in by_type else "confirm_checkpoint"
        return {"type": key, "target_id": by_type[key]["target_id"]}
    if normalized in EDIT_TEXTS:
        key = "edit_intake" if "edit_intake" in by_type else "edit_checkpoint"
        return {"type": key, "target_id": by_type[key]["target_id"]}
    return None


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
        buckets[item["kind"]][item["field"]] = {
            "value": item["value"],
            "evidence_quote": item["evidence_quote"],
        }


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
        expected_target = "intake-%s" % state["revision"]
        if intake["status"] != "awaiting_confirmation" or target_id != expected_target:
            raise HarnessConflict("这份基础资料已经更新，请查看最新版本")
        if action_type == "edit_intake":
            intake["status"] = "editing"
            event["assistant_prefix"] = "请直接写出正确内容，并说明要替换“基本信息”还是“职业背景”。"
        else:
            intake["status"] = "complete"
            state["ip_profile"]["intake"] = deepcopy(intake["answers"])
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
        event["assistant_prefix"] = "可以。请告诉我具体要改哪一处，以及你希望改成什么。"
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
    event["assistant_prefix"] = "✅ 这一步已确认。"
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


def validate_model_decision(raw, value, evidence_text):
    state = normalize_state(value)
    if not isinstance(raw, dict):
        raise HarnessError("模型没有返回结构化对象")
    decision = str(raw.get("decision") or "")
    if decision not in {"ask_follow_up", "propose_checkpoint", "answer_only"}:
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
    expected_checkpoint = state["module_step"] + 1
    if decision == "propose_checkpoint":
        if checkpoint != expected_checkpoint or checkpoint > len(MODULE_WORKFLOWS[state["current_module"]]["checkpoints"]):
            raise HarnessError("模型试图跨越当前断点")
        if not draft or not self_review:
            raise HarnessError("模型没有返回可确认内容或自评")
    elif checkpoint != 0 or draft:
        raise HarnessError("非断点回复不能携带推进内容")

    updates = raw.get("profile_updates") or []
    if not isinstance(updates, list) or len(updates) > 8:
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
            raise HarnessError("模型档案更新缺少可回查的用户原话")
        clean_updates.append({"field": field, "value": value_text, "kind": kind, "evidence_quote": quote})
    if decision != "propose_checkpoint" and clean_updates:
        raise HarnessError("只有待确认断点可以携带档案更新")
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


def render_model_reply(decision):
    reply = decision["reply"]
    if decision["decision"] == "propose_checkpoint":
        if decision["draft"] not in reply:
            reply += "\n\n" + decision["draft"]
        reply += "\n\n**自评**：" + decision["self_review"]
        reply += "\n\n请确认这一步，或告诉我需要修改的地方。"
    return reply


def apply_model_decision(value, raw, evidence_text, pending_id=None):
    state = normalize_state(value)
    decision = validate_model_decision(raw, state, evidence_text)
    if decision["decision"] == "propose_checkpoint":
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
    elif decision["decision"] == "ask_follow_up" and not (
        isinstance(state.get("pending"), dict) and state["pending"].get("status") == "editing"
    ):
        state["pending"] = None
    _bump(state)
    return state, decision, render_model_reply(decision)


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
- 不提及 JSON、状态机、字段、数据库或系统规则。
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
- 信息不足时 decision=ask_follow_up，只问一个最有价值、容易回答的问题。
- 信息足够时 decision=propose_checkpoint，draft 只包含当前断点的完整可确认内容。
- 用户只是询问或讨论现有草稿时 decision=answer_only，不改变断点。
- profile_updates 使用英文 snake_case 字段；用户事实和偏好必须逐字引用用户原话，AI 方案用 ai_option 且 evidence_quote 为空。
- 不预设美业、直销或创业身份；不得编造市场趋势、案例、收入、经历或效果承诺。
- 知识资料只学方法和结构，不能抄写示例人物内容。
- 每个可确认产出都要给一句具体自评，说明完整性或仍需本人核对之处。
- 不提及 JSON、状态机、字段、数据库、内部步骤编号或系统规则。
- 使用简体中文，具体、自然，不喊口号。"""
