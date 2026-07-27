"""Digital IP questionnaire analysis through OpenAI Structured Outputs."""

import base64
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import threading
import time
import urllib.error
import uuid
from contextlib import closing

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
MAX_FILES = 6
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 20 * 1024 * 1024
MAX_PROJECT_BODY_BYTES = 29 * 1024 * 1024
PROJECT_DAILY_LIMIT = 12
MAX_PROJECTS_PER_USER = 20
PROJECT_TITLE_MAX = 120
PROJECT_STATE_MAX = 200000
PROJECT_MODULE_STEPS = (5, 5, 5, 5, 4, 3, 3, 4, 5, 5, 5, 5)
PROJECT_FILE_TYPES = {
    "application/pdf": {"pdf"},
    "application/msword": {"doc"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {"docx"},
    "application/vnd.ms-powerpoint": {"ppt"},
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": {"pptx"},
    "application/vnd.ms-excel": {"xls"},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {"xlsx"},
    "text/csv": {"csv"}, "text/plain": {"txt", "md"}, "text/markdown": {"md"},
    "image/png": {"png"}, "image/jpeg": {"jpg", "jpeg"}, "image/webp": {"webp"},
}
PROJECT_STATE_KEYS = {"questionnaire_state", "module_index", "step_index", "completed_modules"}
# 优先独立路径；默认复用已纳入生产备份的内容任务库，只新增独立表、不改 jobs schema。
PROJECT_DB = pathlib.Path(os.environ.get("DIGITAL_IP_DB") or os.environ.get("CONTENT_JOB_DB") or str(
    pathlib.Path(__file__).resolve().parents[1] / "content_jobs.db"
))

# ponytail: 单进程内存限流足够覆盖当前泽龙单实例试点；扩成多实例时再换共享限流。
_recent_requests = {}
_guide_recent_requests = {}
_guide_daily_requests = {}
_project_daily_requests = {}
_guide_cache = {}
_rate_lock = threading.Lock()
_project_db_init_lock = threading.Lock()
_project_db_initialized = set()

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

PROJECT_ANALYSIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "confirmed_facts": {"type": "array", "items": {"type": "string"}},
        "inferred_signals": {"type": "array", "items": {"type": "string"}},
        "business_pains": ANALYSIS_SCHEMA["properties"]["business_pains"],
        "positioning_candidates": {
            "type": "array", "minItems": 3, "maxItems": 3,
            "items": ANALYSIS_SCHEMA["properties"]["positioning_candidates"]["items"],
        },
        "recommended_index": {"type": "integer"},
        "source_evidence": {
            "type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"claim": {"type": "string"}, "evidence": {"type": "string"},
                               "file_name": {"type": "string"}, "location": {"type": "string"}},
                "required": ["claim", "evidence", "file_name", "location"],
            },
        },
        "gaps": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "image_plan": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "goal": {"type": "string"}, "prompt": {"type": "string"},
                "references_needed": {"type": "array", "items": {"type": "string"}},
                "steps": {"type": "array", "items": {"type": "string"}},
            }, "required": ["goal", "prompt", "references_needed", "steps"],
        },
        "video_plan": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "goal": {"type": "string"}, "format": {"type": "string"},
                "duration_seconds": {"type": "integer"},
                "shots": {"type": "array", "items": {"type": "string"}},
                "steps": {"type": "array", "items": {"type": "string"}},
            }, "required": ["goal", "format", "duration_seconds", "shots", "steps"],
        },
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "follow_up_question": {"type": "string"},
        "ready_to_confirm": {"type": "boolean"},
        "uncertainty_note": {"type": "string"},
    },
    "required": ["summary", "confirmed_facts", "inferred_signals", "business_pains",
                 "positioning_candidates", "recommended_index", "source_evidence", "gaps",
                 "conflicts", "image_plan", "video_plan", "next_steps", "follow_up_question",
                 "ready_to_confirm", "uncertainty_note"],
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


def _check_project_daily_limit(username):
    day = time.strftime("%Y-%m-%d", time.localtime())
    with _rate_lock:
        for key in [key for key in _project_daily_requests if key[1] != day]:
            _project_daily_requests.pop(key, None)
        key = (username, day)
        if _project_daily_requests.get(key, 0) >= PROJECT_DAILY_LIMIT:
            raise DigitalIPRateLimited("今日分析次数已用完，请明天继续")
        _project_daily_requests[key] = _project_daily_requests.get(key, 0) + 1


def _parse_structured_output(response):
    status = response.get("status")
    if status not in (None, "completed"):
        if status == "incomplete":
            reason = (response.get("incomplete_details") or {}).get("reason")
            raise DigitalIPError("AI 分析未完成%s，请重试" % ("（%s）" % reason if reason else ""))
        raise DigitalIPError("AI 分析失败，请重试")
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


class DigitalIPNotFound(DigitalIPError):
    status = 404


class DigitalIPRevisionConflict(DigitalIPError):
    status = 409


def _project_db():
    if not PROJECT_DB.parent.exists():
        PROJECT_DB.parent.mkdir(parents=True, mode=0o700)
    db_existed = PROJECT_DB.exists()
    db_path = str(PROJECT_DB)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    if db_path not in _project_db_initialized:
        with _project_db_init_lock:
            if db_path not in _project_db_initialized:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""CREATE TABLE IF NOT EXISTS digital_ip_projects(
                    id TEXT PRIMARY KEY, username TEXT NOT NULL, title TEXT NOT NULL,
                    state_json TEXT NOT NULL DEFAULT '{}', last_analysis_json TEXT NOT NULL DEFAULT '{}',
                    confirmed_json TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_digital_ip_projects_owner_updated ON digital_ip_projects(username, updated_at DESC)")
                conn.commit()
                if not db_existed:
                    try:
                        os.chmod(PROJECT_DB, 0o600)
                    except OSError:
                        conn.close()
                        raise
                _project_db_initialized.add(db_path)
    return conn


def _json_object(value):
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _project_public(row):
    project = {
        "id": row["id"], "title": row["title"], "revision": int(row["revision"]),
        "created_at": int(row["created_at"]), "updated_at": int(row["updated_at"]),
        "state": _json_object(row["state_json"]),
    }
    analysis = _json_object(row["last_analysis_json"])
    confirmed = _json_object(row["confirmed_json"])
    project["status"] = "confirmed" if (analysis and confirmed and analysis.get("analysis_id") == confirmed.get("analysis_id")) else "candidate_ready" if analysis else "draft"
    if analysis:
        project["last_analysis"] = analysis
    if confirmed:
        project["confirmed_profile"] = confirmed.get("profile")
        project["confirmed_plans"] = confirmed.get("plans")
        project["confirmed_candidate_index"] = confirmed.get("candidate_index")
    return project


def _project_state_answer(row, module_index, step_index):
    if isinstance(module_index, bool) or isinstance(step_index, bool) or not isinstance(module_index, int) or not isinstance(step_index, int):
        return ""
    questionnaire = _json_object(row["state_json"]).get("questionnaire_state") or {}
    answers = questionnaire.get("answers") if isinstance(questionnaire, dict) else {}
    value = answers.get("%d-%d" % (module_index, step_index)) if isinstance(answers, dict) else None
    if isinstance(value, dict):
        if value.get("text"):
            return str(value["text"]).strip()
        choice = value.get("choice")
        return "、".join(str(item) for item in choice).strip() if isinstance(choice, list) else str(choice or "").strip()
    return str(value or "").strip()


def _owned_project(username, project_id):
    with closing(_project_db()) as conn:
        row = conn.execute("SELECT * FROM digital_ip_projects WHERE id=? AND username=?", (project_id, username)).fetchone()
    if not row:
        raise DigitalIPNotFound("项目不存在")
    return row


def _clean_project_title(value):
    title = str(value or "").strip()[:PROJECT_TITLE_MAX]
    return title or "未命名数字 IP"


def _contains_data_url(value):
    if isinstance(value, str):
        return value.strip().lower().startswith("data:")
    if isinstance(value, dict):
        return any(_contains_data_url(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_data_url(item) for item in value)
    return False


def _clean_state(value):
    if not isinstance(value, dict):
        raise DigitalIPValidationError("state 必须是对象")
    unknown = set(value) - PROJECT_STATE_KEYS
    if unknown:
        raise DigitalIPValidationError("state 只允许问卷草稿字段")
    if _contains_data_url(value):
        raise DigitalIPValidationError("草稿不能保存原始文件内容")
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > PROJECT_STATE_MAX:
        raise DigitalIPValidationError("草稿内容过大")
    return value, encoded


def _revision(value):
    if isinstance(value, bool):
        raise DigitalIPValidationError("revision 无效")
    try:
        result = int(value)
    except Exception as exc:
        raise DigitalIPValidationError("revision 无效") from exc
    if result < 1:
        raise DigitalIPValidationError("revision 无效")
    return result


def _index(value, field):
    if isinstance(value, bool):
        raise DigitalIPValidationError("%s 无效" % field)
    try:
        result = int(value)
    except Exception as exc:
        raise DigitalIPValidationError("%s 无效" % field) from exc
    if result < 0:
        raise DigitalIPValidationError("%s 无效" % field)
    return result


def create_project(username, payload):
    if not isinstance(payload, dict):
        raise DigitalIPValidationError("请求体必须是 JSON 对象")
    now = int(time.time())
    with closing(_project_db()) as conn:
        if conn.execute("SELECT COUNT(*) FROM digital_ip_projects WHERE username=?", (username,)).fetchone()[0] >= MAX_PROJECTS_PER_USER:
            raise DigitalIPValidationError("每个账号最多保留 %d 个数字 IP 项目" % MAX_PROJECTS_PER_USER)
        conn.execute("INSERT INTO digital_ip_projects(id,username,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (uuid.uuid4().hex, username, _clean_project_title(payload.get("title")), now, now))
        conn.commit()
        row = conn.execute("SELECT * FROM digital_ip_projects WHERE rowid=last_insert_rowid()").fetchone()
    return _project_public(row)


def list_projects(username):
    with closing(_project_db()) as conn:
        rows = conn.execute("SELECT * FROM digital_ip_projects WHERE username=? ORDER BY updated_at DESC, id DESC", (username,)).fetchall()
    return [_project_public(row) for row in rows]


def get_project(username, project_id):
    return _project_public(_owned_project(username, project_id))


def patch_project(username, project_id, payload):
    if not isinstance(payload, dict):
        raise DigitalIPValidationError("请求体必须是 JSON 对象")
    revision = _revision(payload.get("revision"))
    has_title, has_state = "title" in payload, "state" in payload
    if not has_title and not has_state:
        raise DigitalIPValidationError("请提供 title 或 state")
    state_json = None
    if has_state:
        _, state_json = _clean_state(payload["state"])
    row = _owned_project(username, project_id)
    if int(row["revision"]) != revision:
        raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
    fields, values = [], []
    if has_title:
        fields.extend(["title=?"]); values.append(_clean_project_title(payload["title"]))
    if has_state:
        fields.extend(["state_json=?"]); values.append(state_json)
    now = int(time.time())
    fields.extend(["revision=revision+1", "updated_at=?"]); values.append(now)
    with closing(_project_db()) as conn:
        cursor = conn.execute("UPDATE digital_ip_projects SET %s WHERE id=? AND username=? AND revision=?" % ",".join(fields),
                              (*values, project_id, username, revision))
        conn.commit()
        if cursor.rowcount != 1:
            raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
        updated = conn.execute("SELECT * FROM digital_ip_projects WHERE id=? AND username=?", (project_id, username)).fetchone()
    return _project_public(updated)


def _clean_project_files(files):
    if files is None:
        return []
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise DigitalIPValidationError("最多上传 %d 份资料" % MAX_FILES)
    clean, total = [], 0
    for item in files:
        if not isinstance(item, dict):
            raise DigitalIPValidationError("资料格式无效")
        name, mime, data_url = str(item.get("name") or "").strip(), str(item.get("type") or "").lower().strip(), str(item.get("data_url") or "").strip()
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if not name or len(name) > 180 or mime not in PROJECT_FILE_TYPES or extension not in PROJECT_FILE_TYPES[mime]:
            raise DigitalIPValidationError("不支持的资料类型")
        match = re.fullmatch(r"data:([^;,]+);base64,([A-Za-z0-9+/=]+)", data_url, flags=re.I)
        if not match or match.group(1).lower() != mime:
            raise DigitalIPValidationError("资料内容格式无效")
        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except Exception as exc:
            raise DigitalIPValidationError("资料内容格式无效") from exc
        size = len(raw)
        if not size or size > MAX_FILE_BYTES:
            raise DigitalIPValidationError("单份资料不能超过 %d MiB" % (MAX_FILE_BYTES // 1024 // 1024))
        total += size
        if total > MAX_TOTAL_FILE_BYTES:
            raise DigitalIPValidationError("资料总量不能超过 20 MiB")
        clean.append({"name": name, "type": mime, "data_url": data_url})
    return clean


def _clean_analysis_payload(payload):
    if not isinstance(payload, dict):
        raise DigitalIPValidationError("请求体必须是 JSON 对象")
    if payload.get("consent") is not True:
        raise DigitalIPValidationError("请先明确同意将所选资料发送给 AI 分析")
    context = payload.get("context") or {}
    if _contains_data_url(context):
        raise DigitalIPValidationError("上下文不能包含原始文件内容")
    try:
        context_text = json.dumps(context, ensure_ascii=False, separators=(",", ":")) if not isinstance(context, str) else context.strip()
    except Exception as exc:
        raise DigitalIPValidationError("context 格式无效") from exc
    if len(context_text) > 8000:
        raise DigitalIPValidationError("context 过长")
    module_index = _index(payload.get("module_index"), "module_index")
    step_index = _index(payload.get("step_index"), "step_index")
    if module_index >= len(PROJECT_MODULE_STEPS) or step_index >= PROJECT_MODULE_STEPS[module_index]:
        raise DigitalIPValidationError("问卷步骤无效")
    return {
        "revision": _revision(payload.get("revision")),
        "module_index": module_index,
        "step_index": step_index,
        "answer": _clean_text(payload.get("answer"), MAX_ANSWER_CHARS, "当前回答"),
        "context": context_text,
        "files": _clean_project_files(payload.get("files")),
    }


def _extract_project_output(response):
    analysis = _parse_structured_output(response)
    candidates = analysis.get("positioning_candidates")
    if not isinstance(candidates, list) or len(candidates) != 3:
        raise DigitalIPError("AI 没有形成完整的三套定位候选，请补充资料后重试")
    recommended = analysis.get("recommended_index")
    if not isinstance(recommended, int) or recommended not in range(3):
        analysis["recommended_index"] = 0
    return analysis


def _project_analysis(clean, username):
    if not OPENAI_KEY:
        raise DigitalIPError("服务端尚未配置 OpenAI")
    _check_rate_limit(username)
    _check_project_daily_limit(username)
    prompt = {"industry_preset": "美业门店老板", "module_index": clean["module_index"],
              "step_index": clean["step_index"], "answer": clean["answer"], "context": clean["context"]}
    content = [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}]
    for item in clean["files"]:
        if item["type"].startswith("image/"):
            content.append({"type": "input_image", "image_url": item["data_url"], "detail": "high"})
        else:
            content.append({"type": "input_file", "filename": item["name"], "file_data": item["data_url"]})
    request = {
        "model": MODEL, "instructions": INSTRUCTIONS + "\n必须逐条标明资料来源 file_name 和位置 location；无附件时 file_name 写“用户当前回答”，无法精确定位写“未定位”，绝不编造页码。必须补齐资料来源证据、缺口/冲突和可执行的图片、视频计划；不得自动生成图片或视频。",
        "input": [{"role": "user", "content": content}], "reasoning": {"effort": REASONING_EFFORT},
        "text": {"verbosity": "medium", "format": {"type": "json_schema", "name": "digital_ip_project_analysis", "strict": True, "schema": PROJECT_ANALYSIS_SCHEMA}},
        "max_output_tokens": 3200, "store": False,
        "safety_identifier": hashlib.sha256(username.encode()).hexdigest()[:32],
    }
    try:
        response = _post("/v1/responses", json.dumps(request, ensure_ascii=False).encode(), "application/json", timeout=120)
    except urllib.error.HTTPError as exc:
        print("[digital-ip-project] OpenAI HTTP %s" % exc.code, flush=True)
        raise DigitalIPError("AI 分析服务暂时不可用，请稍后重试") from exc
    except Exception as exc:
        print("[digital-ip-project] OpenAI request failed: %s" % type(exc).__name__, flush=True)
        raise DigitalIPError("AI 分析服务暂时不可用，请稍后重试") from exc
    return _extract_project_output(response), str(response.get("model") or MODEL), response.get("usage") or {}


def analyze_project(username, project_id, payload):
    clean = _clean_analysis_payload(payload)
    row = _owned_project(username, project_id)
    if int(row["revision"]) != clean["revision"]:
        raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
    if _project_state_answer(row, clean["module_index"], clean["step_index"]) != clean["answer"]:
        raise DigitalIPRevisionConflict("当前回答尚未保存或已经变更，请保存后重新分析")
    analysis, model, usage = _project_analysis(clean, username)
    now = int(time.time())
    stored = json.dumps({"analysis_id": uuid.uuid4().hex, "analysis": analysis, "model": model, "created_at": now,
                         "input": {"module_index": clean["module_index"], "step_index": clean["step_index"], "answer": clean["answer"]}}, ensure_ascii=False)
    with closing(_project_db()) as conn:
        cursor = conn.execute("UPDATE digital_ip_projects SET last_analysis_json=?, revision=revision+1, updated_at=? WHERE id=? AND username=? AND revision=?",
                              (stored, now, project_id, username, clean["revision"]))
        conn.commit()
        if cursor.rowcount != 1:
            raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
        updated = conn.execute("SELECT * FROM digital_ip_projects WHERE id=? AND username=?", (project_id, username)).fetchone()
    return {"project": _project_public(updated), "analysis": analysis, "model": model, "usage": usage, "ok": True}


def confirm_project(username, project_id, payload):
    if not isinstance(payload, dict):
        raise DigitalIPValidationError("请求体必须是 JSON 对象")
    revision = _revision(payload.get("revision"))
    candidate_index = payload.get("candidate_index")
    if isinstance(candidate_index, bool) or not isinstance(candidate_index, int) or candidate_index not in range(3):
        raise DigitalIPValidationError("candidate_index 无效")
    row = _owned_project(username, project_id)
    if int(row["revision"]) != revision:
        raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
    analysis_record = _json_object(row["last_analysis_json"])
    analysis = analysis_record.get("analysis") if isinstance(analysis_record.get("analysis"), dict) else {}
    candidates = analysis.get("positioning_candidates") if isinstance(analysis.get("positioning_candidates"), list) else []
    if len(candidates) != 3 or candidate_index >= len(candidates):
        raise DigitalIPValidationError("请先完成一次有效分析，再确认候选")
    analyzed_input = analysis_record.get("input") if isinstance(analysis_record.get("input"), dict) else {}
    if _project_state_answer(row, analyzed_input.get("module_index"), analyzed_input.get("step_index")) != str(analyzed_input.get("answer") or "").strip():
        raise DigitalIPRevisionConflict("当前回答已经变更，请重新分析后再确认")
    confirmed = {"analysis_id": analysis_record.get("analysis_id"), "candidate_index": candidate_index, "profile": candidates[candidate_index],
                 "plans": {"image_plan": analysis.get("image_plan"), "video_plan": analysis.get("video_plan"), "next_steps": analysis.get("next_steps")},
                 "confirmed_at": int(time.time())}
    if analyzed_input.get("answer"):
        confirmed["answer"] = analyzed_input["answer"]
    now = int(time.time())
    with closing(_project_db()) as conn:
        cursor = conn.execute("UPDATE digital_ip_projects SET confirmed_json=?, revision=revision+1, updated_at=? WHERE id=? AND username=? AND revision=?",
                              (json.dumps(confirmed, ensure_ascii=False), now, project_id, username, revision))
        conn.commit()
        if cursor.rowcount != 1:
            raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
        updated = conn.execute("SELECT * FROM digital_ip_projects WHERE id=? AND username=?", (project_id, username)).fetchone()
    return {"project": _project_public(updated), "ok": True}
