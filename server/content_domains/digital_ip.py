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
MAX_ANSWER_CHARS = 6000
MAX_CONTEXT_ITEMS = 12
RATE_LIMIT_PER_MINUTE = 6

# ponytail: 单进程内存限流足够覆盖当前泽龙单实例试点；扩成多实例时再换共享限流。
_recent_requests = {}
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


def _check_rate_limit(username):
    now = time.time()
    with _rate_lock:
        recent = [stamp for stamp in _recent_requests.get(username, []) if now - stamp < 60]
        if len(recent) >= RATE_LIMIT_PER_MINUTE:
            raise DigitalIPRateLimited("AI 分析过于频繁，请一分钟后再试")
        recent.append(now)
        _recent_requests[username] = recent


def _extract_output(response):
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
        analysis = json.loads(output_text)
    except Exception as exc:
        raise DigitalIPError("AI 返回格式异常，请重试") from exc
    if not isinstance(analysis, dict):
        raise DigitalIPError("AI 返回格式异常，请重试")
    candidates = analysis.get("positioning_candidates")
    if not isinstance(candidates, list) or len(candidates) != 3:
        raise DigitalIPError("AI 没有形成完整的三套定位候选，请补充资料后重试")
    recommended = analysis.get("recommended_index")
    if not isinstance(recommended, int) or recommended < 0 or recommended >= len(candidates):
        analysis["recommended_index"] = 0
    return analysis


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
