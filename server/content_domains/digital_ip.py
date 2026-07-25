"""Digital IP questionnaire analysis through OpenAI Structured Outputs."""

import hashlib
import json
import os
import threading
import time
import urllib.error

from .core import OPENAI_KEY, _post


MODEL = os.environ.get("DIGITAL_IP_MODEL", "gpt-5.6-sol").strip() or "gpt-5.6-sol"
REASONING_EFFORT = os.environ.get("DIGITAL_IP_REASONING_EFFORT", "low").strip() or "low"
GUIDE_MODEL = os.environ.get("DIGITAL_IP_GUIDE_MODEL", "gpt-5.6-terra").strip() or "gpt-5.6-terra"
GUIDE_REASONING_EFFORT = os.environ.get("DIGITAL_IP_GUIDE_REASONING_EFFORT", "low").strip() or "low"
MAX_ANSWER_CHARS = 6000
MAX_CONTEXT_ITEMS = 12
RATE_LIMIT_PER_MINUTE = 6
MAX_GUIDE_MESSAGE_CHARS = 1200
MAX_GUIDE_ANSWER_CHARS = 1200
MAX_GUIDE_SUMMARY_CHARS = 800
MAX_GUIDE_TURNS = 3
GUIDE_RATE_LIMIT_PER_MINUTE = 3
GUIDE_DAILY_LIMIT = 30
GUIDE_CACHE_SECONDS = 600

# ponytail: 单进程内存限流足够覆盖当前泽龙单实例试点；扩成多实例时再换共享限流。
_recent_requests = {}
_guide_recent_requests = {}
_guide_daily_requests = {}
_guide_cache = {}
_rate_lock = threading.Lock()

ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "confirmed_facts": {"type": "array", "items": {"type": "string"}},
        "inferred_signals": {"type": "array", "items": {"type": "string"}},
        "business_pains": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string"},
                    "evidence": {"type": "string"},
                    "impact": {"type": "string"},
                },
                "required": ["label", "evidence", "impact"],
            },
        },
        "positioning_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "one_liner": {"type": "string"},
                    "reasons": {"type": "array", "items": {"type": "string"}},
                    "risks": {"type": "array", "items": {"type": "string"}},
                    "content_angles": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "one_liner", "reasons", "risks", "content_angles"],
            },
        },
        "recommended_index": {"type": "integer"},
        "follow_up_question": {"type": "string"},
        "ready_to_confirm": {"type": "boolean"},
        "uncertainty_note": {"type": "string"},
    },
    "required": [
        "summary",
        "confirmed_facts",
        "inferred_signals",
        "business_pains",
        "positioning_candidates",
        "recommended_index",
        "follow_up_question",
        "ready_to_confirm",
        "uncertainty_note",
    ],
}

GUIDE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "fill_help",
                "simplify",
                "example",
                "organize",
                "next_step",
                "completeness",
                "general_guidance",
            ],
        },
        "reply": {"type": "string"},
        "follow_up_questions": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string"},
        },
        "suggested_answer": {"type": "string"},
        "recommended_actions": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "fill_answer",
                            "show_example",
                            "continue_chat",
                            "open_step",
                            "run_diagnosis",
                            "none",
                        ],
                    },
                    "label": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["type", "label", "value"],
            },
        },
        "needs_diagnosis": {"type": "boolean"},
        "uncertainty_note": {"type": "string"},
    },
    "required": [
        "intent",
        "reply",
        "follow_up_questions",
        "suggested_answer",
        "recommended_actions",
        "needs_diagnosis",
        "uncertainty_note",
    ],
}

INSTRUCTIONS = """你是黄雀数字化 IP 的美业经营诊断教练。

目标：根据美业门店老板当前回答和已确认上下文，提炼可追溯的经营事实、痛点与三套差异化定位候选。

成功标准：
- 事实只能来自用户原话；推断必须单独放在 inferred_signals
- business_pains 优先覆盖获客、到店、咨询成交、服务、复购转介绍、员工带教和老板 IP 信任
- 输出 3 套真正不同的 positioning_candidates，并说明依据、风险和可持续内容方向
- 资料不足时不要编造，ready_to_confirm=false，并提出一个最有价值的 follow_up_question
- 不制造容貌焦虑，不承诺医疗效果或经营结果，不把 AI 推荐写成用户已确认结论
- 使用简体中文，表达具体、直接、可执行
"""

GUIDE_INSTRUCTIONS = """你是常驻在黄雀 IP 十二模块页面旁边的“小黄雀”，是一名美业老板的 IP 成长引导助手。

你的唯一任务是帮助用户理解当前问题、回忆真实经历、整理当前回答，并告诉用户下一步怎样操作。

硬性边界：
- 只使用当前步骤、当前草稿、简短 IP 摘要和最近三轮对话，不讨论无关话题
- 不生成完整诊断报告或替用户确定人设；需要诊断时 needs_diagnosis=true，并建议用户主动点击本步诊断
- 不制造容貌焦虑，不承诺医疗效果、成交、营收或粉丝增长，不编造案例和经营数据
- 资料不足时明确说明，并提出最多 3 个短问题；不索取身份证、联系方式、支付信息等无关敏感资料
- recommended_actions 最多 2 个，只能从白名单选择；模型只推荐，不能声称已经填入、确认、跳转、扣费、生成或发布
- reply 不超过 280 个汉字，suggested_answer 不超过 500 个汉字；使用简体中文，温暖、具体、像陪伴用户的小教练
"""

GUIDE_INTENTS = set(GUIDE_SCHEMA["properties"]["intent"]["enum"])
GUIDE_ACTIONS = set(
    GUIDE_SCHEMA["properties"]["recommended_actions"]["items"]["properties"]["type"]["enum"]
)


class DigitalIPError(Exception):
    status = 502


class DigitalIPValidationError(DigitalIPError):
    status = 400


class DigitalIPRateLimited(DigitalIPError):
    status = 429


def _clean_text(value, limit, field):
    text = str(value or "").strip()
    if not text:
        raise DigitalIPValidationError("%s不能为空" % field)
    if len(text) > limit:
        raise DigitalIPValidationError("%s不能超过 %d 个字符" % (field, limit))
    return text


def _optional_text(value, limit):
    return str(value or "").strip()[:limit]


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise DigitalIPValidationError("请求体必须是 JSON 对象")
    answer = _clean_text(payload.get("answer"), MAX_ANSWER_CHARS, "当前回答")
    module = _clean_text(payload.get("module"), 80, "模块名称")
    step = _clean_text(payload.get("step"), 120, "步骤名称")
    context = payload.get("confirmed_context") or []
    if not isinstance(context, list):
        raise DigitalIPValidationError("已确认上下文必须是数组")
    clean_context = []
    for item in context[-MAX_CONTEXT_ITEMS:]:
        if not isinstance(item, dict):
            continue
        prior_answer = str(item.get("answer") or "").strip()[:1200]
        if not prior_answer:
            continue
        clean_context.append({
            "module": str(item.get("module") or "")[:80],
            "step": str(item.get("step") or "")[:120],
            "answer": prior_answer,
        })
    return {
        "module": module,
        "step": step,
        "answer": answer,
        "confirmed_context": clean_context,
    }


def validate_guide_payload(payload):
    if not isinstance(payload, dict):
        raise DigitalIPValidationError("请求体必须是 JSON 对象")
    turns = payload.get("recent_turns") or []
    if not isinstance(turns, list):
        raise DigitalIPValidationError("最近对话必须是数组")
    clean_turns = []
    for item in turns[-MAX_GUIDE_TURNS:]:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        content = _optional_text(item.get("content"), 600)
        if content:
            clean_turns.append({"role": item["role"], "content": content})
    return {
        "module": _clean_text(payload.get("module"), 80, "模块名称"),
        "step": _clean_text(payload.get("step"), 120, "步骤名称"),
        "step_instruction": _optional_text(payload.get("step_instruction"), 500),
        "step_why": _optional_text(payload.get("step_why"), 500),
        "current_answer": _optional_text(payload.get("current_answer"), MAX_GUIDE_ANSWER_CHARS),
        "ip_summary": _optional_text(payload.get("ip_summary"), MAX_GUIDE_SUMMARY_CHARS),
        "next_step": _optional_text(payload.get("next_step"), 160),
        "message": _clean_text(payload.get("message"), MAX_GUIDE_MESSAGE_CHARS, "问题"),
        "recent_turns": clean_turns,
    }


def _check_rate_limit(username):
    now = time.time()
    with _rate_lock:
        recent = [stamp for stamp in _recent_requests.get(username, []) if now - stamp < 60]
        if len(recent) >= RATE_LIMIT_PER_MINUTE:
            raise DigitalIPRateLimited("AI 分析过于频繁，请一分钟后再试")
        recent.append(now)
        _recent_requests[username] = recent


def _check_guide_rate_limit(username):
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    with _rate_lock:
        for key in [key for key in _guide_daily_requests if key[1] != day]:
            _guide_daily_requests.pop(key, None)
        recent = [
            stamp for stamp in _guide_recent_requests.get(username, [])
            if now - stamp < 60
        ]
        if len(recent) >= GUIDE_RATE_LIMIT_PER_MINUTE:
            raise DigitalIPRateLimited("小黄雀回复得太频繁，请一分钟后再试")
        daily_key = (username, day)
        daily = _guide_daily_requests.get(daily_key, 0)
        if daily >= GUIDE_DAILY_LIMIT:
            raise DigitalIPRateLimited("今天的小黄雀引导次数已用完，请明天继续")
        recent.append(now)
        _guide_recent_requests[username] = recent
        _guide_daily_requests[daily_key] = daily + 1


def _parse_structured_output(response):
    refusal = ""
    output_text = ""
    for output in response.get("output") or []:
        if not isinstance(output, dict) or output.get("type") != "message":
            continue
        for item in output.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "refusal":
                refusal = str(item.get("refusal") or "").strip()
            elif item.get("type") == "output_text":
                output_text = str(item.get("text") or "").strip()
    if refusal:
        raise DigitalIPValidationError("这份回答暂时无法分析，请调整内容后重试")
    if not output_text:
        raise DigitalIPError("AI 没有返回可用分析，请重试")
    try:
        result = json.loads(output_text)
    except Exception as exc:
        raise DigitalIPError("AI 返回格式异常，请重试") from exc
    if not isinstance(result, dict):
        raise DigitalIPError("AI 返回格式异常，请重试")
    return result


def _extract_output(response):
    analysis = _parse_structured_output(response)
    candidates = analysis.get("positioning_candidates")
    if not isinstance(candidates, list) or len(candidates) != 3:
        raise DigitalIPError("AI 没有形成完整的三套定位候选，请补充资料后重试")
    recommended = analysis.get("recommended_index")
    if not isinstance(recommended, int) or recommended < 0 or recommended >= len(candidates):
        analysis["recommended_index"] = 0
    return analysis


def _extract_guide_output(response):
    result = _parse_structured_output(response)
    intent = result.get("intent")
    if intent not in GUIDE_INTENTS:
        intent = "general_guidance"
    reply = _optional_text(result.get("reply"), 280)
    if not reply:
        raise DigitalIPError("小黄雀没有返回可用建议，请重试")
    questions = [
        _optional_text(item, 180)
        for item in (result.get("follow_up_questions") or [])[:3]
        if _optional_text(item, 180)
    ]
    actions = []
    for item in (result.get("recommended_actions") or [])[:2]:
        if not isinstance(item, dict) or item.get("type") not in GUIDE_ACTIONS:
            continue
        action_type = item["type"]
        if action_type == "none":
            continue
        label = _optional_text(item.get("label"), 40)
        if label:
            actions.append({
                "type": action_type,
                "label": label,
                "value": _optional_text(item.get("value"), 500),
            })
    return {
        "intent": intent,
        "reply": reply,
        "follow_up_questions": questions,
        "suggested_answer": _optional_text(result.get("suggested_answer"), 500),
        "recommended_actions": actions,
        "needs_diagnosis": bool(result.get("needs_diagnosis")),
        "uncertainty_note": _optional_text(result.get("uncertainty_note"), 240),
    }


def diagnose(payload, username):
    if not OPENAI_KEY:
        raise DigitalIPError("泽龙服务端尚未配置 OpenAI")
    clean = validate_payload(payload)
    _check_rate_limit(username)
    user_input = {
        "industry_preset": "美业门店老板",
        "current_module": clean["module"],
        "current_step": clean["step"],
        "current_answer": clean["answer"],
        "confirmed_context": clean["confirmed_context"],
    }
    request = {
        "model": MODEL,
        "instructions": INSTRUCTIONS,
        "input": json.dumps(user_input, ensure_ascii=False),
        "reasoning": {"effort": REASONING_EFFORT},
        "text": {
            "verbosity": "medium",
            "format": {
                "type": "json_schema",
                "name": "digital_ip_step_analysis",
                "strict": True,
                "schema": ANALYSIS_SCHEMA,
            },
        },
        "max_output_tokens": 2400,
        "store": False,
        "safety_identifier": hashlib.sha256(username.encode()).hexdigest()[:32],
    }
    try:
        response = _post(
            "/v1/responses",
            json.dumps(request, ensure_ascii=False).encode(),
            "application/json",
            timeout=120,
        )
    except urllib.error.HTTPError as exc:
        print("[digital-ip] OpenAI HTTP %s" % exc.code, flush=True)
        raise DigitalIPError("AI 分析服务暂时不可用，请稍后重试") from exc
    except Exception as exc:
        print("[digital-ip] OpenAI request failed: %s" % type(exc).__name__, flush=True)
        raise DigitalIPError("AI 分析服务暂时不可用，请稍后重试") from exc
    return {
        "ok": True,
        "analysis": _extract_output(response),
        "model": str(response.get("model") or MODEL),
        "usage": response.get("usage") or {},
        "ai_recommendation": True,
        "user_confirmed": False,
    }


def guide(payload, username):
    if not OPENAI_KEY:
        raise DigitalIPError("泽龙服务端尚未配置 OpenAI")
    clean = validate_guide_payload(payload)
    now = time.time()
    # ponytail: 试点流量小，按请求清理过期内存缓存；多实例时再换共享 TTL 缓存。
    cache_key = hashlib.sha256(
        (username + "\n" + json.dumps(clean, ensure_ascii=False, sort_keys=True)).encode()
    ).hexdigest()
    with _rate_lock:
        for key in [
            key for key, item in _guide_cache.items()
            if now - item["at"] >= GUIDE_CACHE_SECONDS
        ]:
            _guide_cache.pop(key, None)
        cached = _guide_cache.get(cache_key)
    if cached:
        return {**cached["result"], "cached": True, "usage": {}}
    _check_guide_rate_limit(username)
    request = {
        "model": GUIDE_MODEL,
        "instructions": GUIDE_INSTRUCTIONS,
        "input": json.dumps({
            "industry_preset": "美业门店老板",
            **clean,
        }, ensure_ascii=False),
        "reasoning": {"effort": GUIDE_REASONING_EFFORT},
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "digital_ip_guide_reply",
                "strict": True,
                "schema": GUIDE_SCHEMA,
            },
        },
        "max_output_tokens": 800,
        "store": False,
        "safety_identifier": hashlib.sha256(username.encode()).hexdigest()[:32],
    }
    try:
        response = _post(
            "/v1/responses",
            json.dumps(request, ensure_ascii=False).encode(),
            "application/json",
            timeout=60,
        )
    except urllib.error.HTTPError as exc:
        print("[digital-ip-guide] OpenAI HTTP %s" % exc.code, flush=True)
        raise DigitalIPError("小黄雀暂时无法回复，请稍后重试") from exc
    except Exception as exc:
        print("[digital-ip-guide] OpenAI request failed: %s" % type(exc).__name__, flush=True)
        raise DigitalIPError("小黄雀暂时无法回复，请稍后重试") from exc
    result = {
        "ok": True,
        "guide": _extract_guide_output(response),
        "model": str(response.get("model") or GUIDE_MODEL),
        "usage": response.get("usage") or {},
        "cached": False,
        "guide_only": True,
        "user_confirmed": False,
    }
    with _rate_lock:
        _guide_cache[cache_key] = {"at": time.time(), "result": result}
    return result
