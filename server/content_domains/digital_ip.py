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
from . import ip12_pdf


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
MAX_GUIDE_TURNS = 6
# 按 38 个字段各一轮回答加一轮追问估算为 76 轮；正常连续访谈不能被防刷阈值打断。
GUIDE_RATE_LIMIT_PER_MINUTE = 12
GUIDE_DAILY_LIMIT = 120
GUIDE_CACHE_SECONDS = 600
MAX_FILES = 6
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 20 * 1024 * 1024
MAX_PROJECT_BODY_BYTES = 29 * 1024 * 1024
PROJECT_DAILY_LIMIT = 12
REPORT_RATE_LIMIT_PER_MINUTE = 2
REPORT_DAILY_LIMIT = 5
MAX_PROJECTS_PER_USER = 20
PROJECT_TITLE_MAX = 120
PROJECT_STATE_MAX = 200000
PROJECT_MANAGED_STATE_MAX = 500000
MAX_CONFIRMED_ATTACHMENT_EVIDENCE = 12
PROJECT_MODULE_STEPS = (18, 4, 3, 5, 5, 3, 0, 0, 0, 0, 0, 0)
ACTIVE_PROJECT_MODULES = 6
FOUNDATION_MODULES = 4
FOUNDATION_STAGE = "foundation_v1"
ACTIVE_MODULE_NAMES = (
    "定位诊断", "人设塑造", "价值主张", "故事资产",
    "内容选题", "文案口播",
)
MODULE_PROMPT_RULES = (
    "定位诊断：参考采集表，逐项关注当前职业身份、从业经历、低谷、成就、被夸与被吐槽、最强能力、赛道、目标受众、所解问题、差异化和已有账号；每轮只追问一个缺失字段。只从用户原话提炼事实，再给三套定位及机会、风险和推荐理由。",
    "人设塑造：参考采集表，逐项关注三个性格词、说话风格、讨厌的博主风格、朋友圈或聊天习惯；每轮只追问一个缺失字段。基于已确认经历和价值观给三套可长期坚持的人设，并比较传播优势、风险和表演成本。",
    "价值主张：参考采集表，逐项关注最想让人记住的一句话、一句话自我介绍、客户或朋友为何追随；每轮只追问一个缺失字段。把优势与受众痛点转成可兑现的价值主张，并说明依据与边界。",
    "故事资产：参考采集表，逐项关注绝境翻身、踩坑、逆袭、戏剧经历、带团队或项目的真实故事；每轮只追问一个缺失字段。只从用户真实经历提炼故事主线、情绪点、可用场景和待核实缺口。",
    "内容选题：结合已确认的做 IP 目的、可投入时间、现有产品或服务、三个月与一年目标，以及目标人群高频问题形成选题方向；每轮只追问一个缺失字段，禁止把趋势或流量效果写成事实。",
    "文案口播：围绕已确认主题、目标人群、身份、传播目标和说话风格，比较共情型、观点型和故事型方案；包含三秒钩子、节奏、字幕点和克制的行动引导，不新增未经确认的人生事实。",
)
FOUNDATION_STEP_META = (
    tuple((title, "fact") for title in (
        "姓名或昵称", "性别与年龄段", "所在城市", "当前职业或身份", "从业年限", "行业与岗位经历",
        "主要收入来源", "年收入区间", "最大挫折或低谷", "最有成就感的事", "被夸最多的特点",
        "被吐槽最多的特点", "最强能力", "IP 赛道", "目标受众", "解决的问题", "差异化", "已有内容账号",
    )),
    tuple((title, "fact") for title in ("三个性格词", "说话风格", "讨厌的博主风格", "聊天与朋友圈习惯")),
    tuple((title, "fact") for title in ("记忆金句", "一句话自我介绍", "追随理由")),
    tuple((title, "fact") for title in ("绝境翻身", "踩过的大坑", "逆袭或突破", "戏剧性经历", "团队或项目故事")),
)
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
REPORT_STATE_KEY = "generated_report"
PRODUCT_CATALOG = (
    {
        "id": "image_studio", "name": "图片生成", "page": "banana.html",
        "capability": "根据文字或用户主动提供的参考图生成营销图片和视觉素材",
        "limits": "只生成候选素材；发布、投放和效果判断仍需用户确认",
    },
    {
        "id": "script_studio", "name": "文案编导", "page": "script.html",
        "capability": "生成可拍摄的分镜脚本，并对用户提供的内容做结构拆解后继续创作",
        "limits": "脚本是创作建议，不代表平台流量或成交结果",
    },
    {
        "id": "voice_studio", "name": "音频创作", "page": "audio.html",
        "capability": "基于已确认文案，选择页面可用的公共或个人音色生成配音音频",
        "limits": "可用音色和生成条件以页面当前实际状态为准",
    },
    {
        "id": "video_studio", "name": "视频创作", "page": "video.html",
        "capability": "根据页面当前开放功能生成口播或其他视频内容",
        "limits": "具体渠道、模型和可用性以页面当前实际状态为准；不承诺生成效果",
    },
    {
        "id": "workflow_canvas", "name": "创作画布", "page": "canvas.html",
        "capability": "把文本、图片、反推、作图和视频等页面可用节点组成可复用流程",
        "limits": "流程仍需用户逐步检查和执行，不代表自动发布或自动经营",
    },
)
PRODUCT_IDS = {item["id"] for item in PRODUCT_CATALOG}
# 优先独立路径；默认复用已纳入生产备份的内容任务库，只新增独立表、不改 jobs schema。
PROJECT_DB = pathlib.Path(os.environ.get("DIGITAL_IP_DB") or os.environ.get("CONTENT_JOB_DB") or str(
    pathlib.Path(__file__).resolve().parents[1] / "content_jobs.db"
))

# ponytail: 单进程内存限流足够覆盖当前泽龙单实例试点；扩成多实例时再换共享限流。
_recent_requests = {}
_guide_recent_requests = {}
_guide_daily_requests = {}
_project_daily_requests = {}
_report_recent_requests = {}
_report_daily_requests = {}
_guide_cache = {}
_rate_lock = threading.Lock()
_inflight_lock = threading.Lock()
# 单进程去重：进程重启或多实例不共享，生产扩容时应迁移到共享锁/队列。
_project_inflight = {}
# ponytail: 仅用短暂内存标记拒绝同项目编辑与付费调用并发；不等待模型的 120 秒，扩成多实例时换共享锁。
_project_actions = set()
_project_mutations = set()
_project_db_init_lock = threading.Lock()
_project_db_initialized = set()
_pdf_lock = threading.Lock()
_pdf_recent_renders = {}
_pdf_cache = {}

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

REPORT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "executive_summary": {"type": "string"},
        "evidence": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "evidence_id": {"type": "string"}, "claim": {"type": "string"},
                "source_ref": {"type": "string"}, "source_excerpt": {"type": "string"},
            },
            "required": ["evidence_id", "claim", "source_ref", "source_excerpt"],
        }},
        "modules": {"type": "array", "minItems": 4, "maxItems": 4, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "module_id": {"type": "integer", "enum": [1, 2, 3, 4]},
                "title": {"type": "string"}, "summary": {"type": "string"},
                "findings": {"type": "array", "maxItems": 10, "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string", "enum": ["fact", "inference", "option", "recommendation"]},
                        "title": {"type": "string"}, "detail": {"type": "string"},
                        "evidence_ids": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "string"}},
                        "risks": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
                    },
                    "required": ["kind", "title", "detail", "evidence_ids", "risks"],
                }},
            },
            "required": ["module_id", "title", "summary", "findings"],
        }},
        "execution_priorities": {"type": "array", "maxItems": 4, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                "module_id": {"type": "integer", "enum": [1, 2, 3, 4]},
                "task": {"type": "string"}, "output": {"type": "string"},
                "evidence_ids": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "string"}},
            },
            "required": ["priority", "module_id", "task", "output", "evidence_ids"],
        }},
        "confirmation_items": {"type": "array", "maxItems": 12, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "item": {"type": "string"}, "reason": {"type": "string"},
                "evidence_ids": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
                "required": {"type": "boolean"},
            },
            "required": ["item", "reason", "evidence_ids", "required"],
        }},
        "material_gaps": {"type": "array", "maxItems": 12, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "gap": {"type": "string"}, "why_needed": {"type": "string"},
                "how_to_collect": {"type": "string"}, "blocking": {"type": "boolean"},
                "source_refs": {"type": "array", "maxItems": 54, "items": {"type": "string"}},
            },
            "required": ["gap", "why_needed", "how_to_collect", "blocking", "source_refs"],
        }},
        "disclaimer": {"type": "string"},
    },
    "required": ["title", "executive_summary", "evidence", "modules", "execution_priorities",
                 "confirmation_items", "material_gaps", "disclaimer"],
}

REPORT_INSTRUCTIONS = """你是黄雀数字化 IP 的定位报告审查员。根据用户已经确认的模块 1–4 资料，生成一份可追溯的 IP 人设定位阶段报告。

硬性规则：
- confirmed_answers 和 confirmed_attachment_evidence 是用户确认过的事实来源；skipped_steps 只代表资料缺口，不能据此推断事实
- evidence.source_ref 必须来自输入来源；source_excerpt 必须是可逐字回查的短摘录，不得改写
- source_kind=fact 才能支持 kind=fact；preference 只表示用户选择偏好，ai_option 只表示用户阅读过系统备选，二者都不得写成个人经历或业绩事实
- modules 必须按 1 定位诊断、2 人设塑造、3 价值主张、4 故事资产的顺序各出现一次
- 每条事实、推断、候选和推荐都必须引用 evidence_id，并用 kind 明确区分；AI 推断和创意备选不得写成用户事实
- 模块 1 覆盖定位关键词、候选定位、机会和风险；模块 2 覆盖三套人设候选、推荐与表演成本；模块 3 覆盖价值主张、场景口径、自我介绍与兑现边界；模块 4 只基于真实经历形成故事卡、叙事风险和推荐主线
- execution_priorities 只能给 P0–P3 的下一步任务与预计产出，不得承诺流量、粉丝、成交、营收或经营结果
- 数字、收入、客户或团队规模、敏感经历、第三方评价和公开范围必须进入 confirmation_items 再由本人确认
- skipped_steps 必须全部被 material_gaps.source_refs 覆盖，可将同类步骤合并为一个缺口；非跳过型缺口的 source_refs 为空
- 不得生成模块 5–12 的执行结论，不得自动生成素材、发布、投放、支付或联系第三方
- 若没有可信证据，不得为了凑内容补写；应放入 material_gaps
- disclaimer 必须说明报告仅基于用户确认资料，AI 推断与创意需本人复核，不构成经营效果保证或公开授权
"""

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
            "maxItems": 1,
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

INSTRUCTIONS = """你是黄雀数字化 IP 的阶段分析教练，不预设用户行业。

目标：根据当前模块规则、用户回答、已确认上下文和用户主动提供的资料，提炼可追溯事实、关键问题与三套差异化阶段候选。

成功标准：
- 事实只能来自用户原话；推断必须单独放在 inferred_signals
- business_pains 表示当前模块需要解决的关键问题，不能强行套用某个行业或经营场景
- 输出 3 套真正不同的 positioning_candidates；字段名仅为兼容接口，内容必须服从当前模块规则，并说明依据、风险和下一步方向
- 资料不足时不要编造，ready_to_confirm=false，并提出一个最有价值的 follow_up_question
- 不承诺医疗、流量、粉丝、成交或经营结果，不把 AI 推荐写成用户已确认结论
- 使用简体中文，表达具体、直接、可执行
"""

GUIDE_INSTRUCTIONS = """你是正在认真倾听的真人 IP 咨询师“小黄雀”，不预设用户行业，也不是表单助手。

你的唯一任务是接住用户刚说的真实信息，帮助他更容易地补充当前主题，并忠于原话整理结果。

对话方式：
- reply 只用一句自然短话承接，不复述、不评价、不喊口号，也不能包含问号；唯一追问只放在 follow_up_questions
- 每轮只聚焦一个主题；复杂主题最多拆成 2–3 个短确认点，仍放在同一条追问里
- 宏观问题必须改成容易回答的具体问题，并优先给 2–4 个可直接选择的方向或简短例子；允许用户改写或回答“其他”
- 先检查 current_answer、ip_summary 和 recent_turns；用户已经说过的信息不得重复询问
- 绝不向用户提及报告、表格、字段、采集表、当前步骤、下一步、确认稿、填空、补齐缺口或“如何呈现”等内部工作方式

硬性边界：
- 只使用当前主题、当前回答、简短 IP 摘要和最近六条对话，不讨论无关话题
- 不生成完整诊断报告或替用户确定人设；需要诊断时 needs_diagnosis=true
- 不承诺医疗、流量、成交、营收或粉丝增长，不编造案例、趋势和经营数据
- 信息不足时只提出 1 个最有价值的短问题；不索取身份证、联系方式、支付信息等无关敏感资料
- 只判断当前主题的信息是否够用：够用时 follow_up_questions 必须为空，并在 suggested_answer 中给出忠于用户原话的简短记录；不够时才追问
- recommended_actions 最多 2 个，只能从白名单选择；模型只推荐，不能声称已经填入、确认、跳转、扣费、生成或发布
- reply 不超过 80 个汉字，suggested_answer 不超过 500 个汉字；使用简体中文，温暖、具体、像一位耐心的咨询师
"""

GUIDE_INTENTS = set(GUIDE_SCHEMA["properties"]["intent"]["enum"])
GUIDE_ACTIONS = set(
    GUIDE_SCHEMA["properties"]["recommended_actions"]["items"]["properties"]["type"]["enum"]
)
GUIDE_INTERNAL_TERMS = (
    "报告", "表格", "字段", "采集表", "当前步骤", "下一步", "确认稿", "填空", "补齐缺口", "如何呈现",
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


def _module_rule(module_name):
    try:
        return MODULE_PROMPT_RULES[ACTIVE_MODULE_NAMES.index(module_name)]
    except ValueError:
        return ""


def _require_active_module(module_name):
    if module_name not in ACTIVE_MODULE_NAMES:
        raise DigitalIPValidationError("该模块正在开发中，敬请期待")


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise DigitalIPValidationError("请求体必须是 JSON 对象")
    answer = _clean_text(payload.get("answer"), MAX_ANSWER_CHARS, "当前回答")
    module = _clean_text(payload.get("module"), 80, "模块名称")
    _require_active_module(module)
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
    if payload.get("consent") is not True:
        raise DigitalIPValidationError("请先明确同意将当前回答发送给 AI 分析")
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
    if payload.get("consent") is not True:
        raise DigitalIPValidationError("请先明确同意将当前回答发送给 AI 引导")
    module = _clean_text(payload.get("module"), 80, "模块名称")
    _require_active_module(module)
    return {
        "module": module,
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
    return now


def _release_rate_limit(username, stamp):
    with _rate_lock:
        recent = _recent_requests.get(username, [])
        if stamp in recent:
            recent.remove(stamp)
        if recent:
            _recent_requests[username] = recent
        else:
            _recent_requests.pop(username, None)


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
    return now, daily_key


def _release_guide_rate_limit(username, stamp, daily_key):
    with _rate_lock:
        recent = _guide_recent_requests.get(username, [])
        if stamp in recent:
            recent.remove(stamp)
        if recent:
            _guide_recent_requests[username] = recent
        else:
            _guide_recent_requests.pop(username, None)
        daily = _guide_daily_requests.get(daily_key, 0)
        if daily <= 1:
            _guide_daily_requests.pop(daily_key, None)
        else:
            _guide_daily_requests[daily_key] = daily - 1


def _check_project_daily_limit(username):
    day = time.strftime("%Y-%m-%d", time.localtime())
    with _rate_lock:
        for key in [key for key in _project_daily_requests if key[1] != day]:
            _project_daily_requests.pop(key, None)
        key = (username, day)
        if _project_daily_requests.get(key, 0) >= PROJECT_DAILY_LIMIT:
            raise DigitalIPRateLimited("今日分析次数已用完，请明天继续")
        _project_daily_requests[key] = _project_daily_requests.get(key, 0) + 1
    return key


def _release_project_daily_limit(key):
    with _rate_lock:
        daily = _project_daily_requests.get(key, 0)
        if daily <= 1:
            _project_daily_requests.pop(key, None)
        else:
            _project_daily_requests[key] = daily - 1


def _check_report_rate_limit(username):
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    with _rate_lock:
        for key in [key for key in _report_daily_requests if key[1] != day]:
            _report_daily_requests.pop(key, None)
        recent = [stamp for stamp in _report_recent_requests.get(username, []) if now - stamp < 60]
        if len(recent) >= REPORT_RATE_LIMIT_PER_MINUTE:
            raise DigitalIPRateLimited("报告生成过于频繁，请一分钟后再试")
        daily_key = (username, day)
        if _report_daily_requests.get(daily_key, 0) >= REPORT_DAILY_LIMIT:
            raise DigitalIPRateLimited("今日报告生成次数已用完，请明天继续")
        recent.append(now)
        _report_recent_requests[username] = recent
        _report_daily_requests[daily_key] = _report_daily_requests.get(daily_key, 0) + 1
    return now, daily_key


def _release_report_rate_limit(username, stamp, daily_key):
    with _rate_lock:
        recent = _report_recent_requests.get(username, [])
        if stamp in recent:
            recent.remove(stamp)
        if recent:
            _report_recent_requests[username] = recent
        else:
            _report_recent_requests.pop(username, None)
        daily = _report_daily_requests.get(daily_key, 0)
        if daily <= 1:
            _report_daily_requests.pop(daily_key, None)
        else:
            _report_daily_requests[daily_key] = daily - 1


def _run_project_inflight(kind, username, project_id, revision, action):
    key = (kind, username, project_id, revision)
    project_key = (username, project_id)
    with _inflight_lock:
        entry = _project_inflight.get(key)
        if entry is None:
            if project_key in _project_mutations or project_key in _project_actions:
                raise DigitalIPRevisionConflict("项目正在进行 AI 处理，请完成后刷新再编辑")
            entry = {"event": threading.Event(), "result": None, "error": None}
            _project_inflight[key] = entry
            _project_actions.add(project_key)
            owner = True
        else:
            owner = False
    if not owner:
        entry["event"].wait()
        if entry["error"] is not None:
            raise entry["error"]
        return entry["result"]
    try:
        entry["result"] = action()
        return entry["result"]
    except BaseException as exc:
        entry["error"] = exc
        raise
    finally:
        with _inflight_lock:
            _project_inflight.pop(key, None)
            _project_actions.discard(project_key)
        entry["event"].set()


def _run_project_mutation(username, project_id, action):
    project_key = (username, project_id)
    with _inflight_lock:
        if project_key in _project_actions or project_key in _project_mutations:
            raise DigitalIPRevisionConflict("项目正在进行 AI 处理，请完成后刷新再编辑")
        _project_mutations.add(project_key)
    try:
        return action()
    finally:
        with _inflight_lock:
            _project_mutations.discard(project_key)


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
    if any(token in reply for token in ("?", "？", *GUIDE_INTERNAL_TERMS)):
        reply = "我明白了，我们继续把这一点说清楚。"
    questions = [
        _optional_text(item, 180)
        for item in (result.get("follow_up_questions") or [])[:1]
        if _optional_text(item, 180)
    ]
    if questions:
        first = re.match(r"^.*?[？?]", questions[0])
        questions[0] = first.group(0) if first else questions[0].rstrip("。.!！") + "？"
        if any(token in questions[0] for token in GUIDE_INTERNAL_TERMS):
            questions[0] = "关于这一点，你更接近哪一种情况？可以直接选一个方向，也可以补充自己的答案。"
    suggested_answer = _optional_text(result.get("suggested_answer"), 500)
    if any(token in suggested_answer for token in GUIDE_INTERNAL_TERMS):
        suggested_answer = ""
    actions = []
    for item in (result.get("recommended_actions") or [])[:2]:
        if not isinstance(item, dict) or item.get("type") not in GUIDE_ACTIONS:
            continue
        action_type = item["type"]
        if action_type == "none":
            continue
        label = _optional_text(item.get("label"), 40)
        value = _optional_text(item.get("value"), 500)
        if label and not any(token in label + value for token in GUIDE_INTERNAL_TERMS):
            actions.append({
                "type": action_type,
                "label": label,
                "value": value,
            })
    return {
        "intent": intent,
        "reply": reply,
        "follow_up_questions": questions,
        "suggested_answer": suggested_answer,
        "recommended_actions": actions,
        "needs_diagnosis": bool(result.get("needs_diagnosis")),
        "uncertainty_note": _optional_text(result.get("uncertainty_note"), 240),
    }


def diagnose(payload, username):
    if not OPENAI_KEY:
        raise DigitalIPError("AI 分析服务尚未配置")
    clean = validate_payload(payload)
    rate_stamp = _check_rate_limit(username)
    user_input = {
        "industry_context": "由用户资料判断，不预设行业",
        "module_rule": _module_rule(clean["module"]),
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
        _release_rate_limit(username, rate_stamp)
        print("[digital-ip] OpenAI HTTP %s" % exc.code, flush=True)
        raise DigitalIPError("AI 分析服务暂时不可用，请稍后重试") from exc
    except Exception as exc:
        _release_rate_limit(username, rate_stamp)
        print("[digital-ip] OpenAI request failed: %s" % type(exc).__name__, flush=True)
        raise DigitalIPError("AI 分析服务暂时不可用，请稍后重试") from exc
    try:
        analysis = _extract_output(response)
    except DigitalIPError:
        _release_rate_limit(username, rate_stamp)
        raise
    return {
        "ok": True,
        "analysis": analysis,
        "usage": response.get("usage") or {},
        "ai_recommendation": True,
        "user_confirmed": False,
    }


def guide(payload, username):
    if not OPENAI_KEY:
        raise DigitalIPError("AI 分析服务尚未配置")
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
    rate_stamp, daily_key = _check_guide_rate_limit(username)
    request = {
        "model": GUIDE_MODEL,
        "instructions": GUIDE_INSTRUCTIONS,
        "input": json.dumps({
            "industry_context": "由用户资料判断，不预设行业",
            "module_rule": _module_rule(clean["module"]),
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
        _release_guide_rate_limit(username, rate_stamp, daily_key)
        print("[digital-ip-guide] OpenAI HTTP %s" % exc.code, flush=True)
        raise DigitalIPError("小黄雀暂时无法回复，请稍后重试") from exc
    except Exception as exc:
        _release_guide_rate_limit(username, rate_stamp, daily_key)
        print("[digital-ip-guide] OpenAI request failed: %s" % type(exc).__name__, flush=True)
        raise DigitalIPError("小黄雀暂时无法回复，请稍后重试") from exc
    try:
        guide_reply = _extract_guide_output(response)
    except DigitalIPError:
        _release_guide_rate_limit(username, rate_stamp, daily_key)
        raise
    result = {
        "ok": True,
        "guide": guide_reply,
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


class DigitalIPPDFBusy(DigitalIPError):
    status = 429


class DigitalIPPDFUnavailable(DigitalIPError):
    status = 503


def _secure_project_db_files(db_path):
    path = pathlib.Path(db_path)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(descriptor)
        for candidate in (path, pathlib.Path(str(path) + "-wal"), pathlib.Path(str(path) + "-shm")):
            try:
                os.chmod(candidate, 0o600)
            except FileNotFoundError:
                pass
    except OSError as exc:
        raise DigitalIPError("无法收紧项目档案数据库权限") from exc


def _project_db():
    if not PROJECT_DB.parent.exists():
        PROJECT_DB.parent.mkdir(parents=True, mode=0o700)
    db_path = str(PROJECT_DB)
    _secure_project_db_files(PROJECT_DB)
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
                _project_db_initialized.add(db_path)
    try:
        _secure_project_db_files(PROJECT_DB)
    except DigitalIPError:
        conn.close()
        raise
    return conn


def _json_object(value):
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_hash(source):
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _foundation_stage(row):
    report = _json_object(row["state_json"]).get(REPORT_STATE_KEY)
    if not isinstance(report, dict) or not isinstance(report.get("content"), dict):
        return {"status": "missing", "report_id": "", "stale": False}
    if report.get("stage") != FOUNDATION_STAGE:
        return {
            "status": "legacy", "report_id": str(report.get("report_id") or ""),
            "stale": int(row["revision"]) != report.get("project_revision"),
        }
    current_hash = _source_hash(_report_source(row))
    stale = current_hash != report.get("source_hash")
    return {
        "status": "stale" if stale else str(report.get("status") or "pending_confirmation"),
        "report_id": str(report.get("report_id") or ""),
        "stale": stale,
        "generated_at": report.get("generated_at"),
        "confirmed_at": report.get("confirmed_at"),
    }


def _foundation_is_confirmed(row):
    stage = _foundation_stage(row)
    return stage["status"] == "confirmed" and not stage["stale"]


def _project_public(row):
    state = _json_object(row["state_json"])
    state.pop(REPORT_STATE_KEY, None)
    project = {
        "id": row["id"], "title": row["title"], "revision": int(row["revision"]),
        "created_at": int(row["created_at"]), "updated_at": int(row["updated_at"]),
        "state": state,
        "foundation_stage": _foundation_stage(row),
    }
    analysis = _json_object(row["last_analysis_json"])
    confirmed = _json_object(row["confirmed_json"])
    project["status"] = "confirmed" if (analysis and confirmed and analysis.get("analysis_id") == confirmed.get("analysis_id")) else "candidate_ready" if analysis else "draft"
    if analysis:
        project["last_analysis"] = dict(analysis)
        project["last_analysis"].pop("model", None)
    if confirmed:
        project["confirmed_profile"] = confirmed.get("profile")
        project["confirmed_plans"] = confirmed.get("plans")
        project["confirmed_candidate_index"] = confirmed.get("candidate_index")
    return project


def _state_answer(state, module_index, step_index):
    if isinstance(module_index, bool) or isinstance(step_index, bool) or not isinstance(module_index, int) or not isinstance(step_index, int):
        return ""
    questionnaire = state.get("questionnaire_state") if isinstance(state, dict) else {}
    answers = questionnaire.get("answers") if isinstance(questionnaire, dict) else {}
    value = answers.get("%d-%d" % (module_index, step_index)) if isinstance(answers, dict) else None
    if isinstance(value, dict):
        if value.get("text"):
            return str(value["text"]).strip()
        choice = value.get("choice")
        return "、".join(str(item) for item in choice).strip() if isinstance(choice, list) else str(choice or "").strip()
    return str(value or "").strip()


def _project_state_answer(row, module_index, step_index):
    return _state_answer(_json_object(row["state_json"]), module_index, step_index)


def _answer_content(value):
    if not isinstance(value, dict):
        return str(value or "").strip()
    text = str(value.get("text") or "").strip()
    if text:
        return {"text": text}
    choice = value.get("choice")
    if isinstance(choice, list):
        return {"choice": [str(item).strip() for item in choice]}
    return {"choice": str(choice or "").strip()}


def _confirmed_answers_snapshot(state):
    questionnaire = state.get("questionnaire_state") if isinstance(state, dict) else {}
    answers = questionnaire.get("answers") if isinstance(questionnaire, dict) else {}
    if not isinstance(answers, dict):
        return {}
    return {
        key: _answer_content(value)
        for key, value in answers.items()
        if isinstance(value, dict) and value.get("confirmed") is True
    }


def _confirmed_answers_changed(previous_state, next_state, analyzed_input):
    previous_snapshot = _confirmed_answers_snapshot(previous_state)
    next_snapshot = _confirmed_answers_snapshot(next_state)
    if previous_snapshot == next_snapshot:
        return False
    module_index, step_index = analyzed_input.get("module_index"), analyzed_input.get("step_index")
    if isinstance(module_index, bool) or isinstance(step_index, bool) or not isinstance(module_index, int) or not isinstance(step_index, int):
        return True
    step_key = "%d-%d" % (module_index, step_index)
    previous_answers = ((previous_state.get("questionnaire_state") or {}).get("answers") or {}) if isinstance(previous_state, dict) else {}
    next_answers = ((next_state.get("questionnaire_state") or {}).get("answers") or {}) if isinstance(next_state, dict) else {}
    before, after = previous_answers.get(step_key), next_answers.get(step_key)
    if not (isinstance(before, dict) and isinstance(after, dict)
            and before.get("confirmed") is not True and before.get("skipped") is not True
            and after.get("confirmed") is True
            and _answer_content(before) == _answer_content(after)):
        return True
    expected_snapshot = dict(previous_snapshot)
    expected_snapshot[step_key] = _answer_content(after)
    return next_snapshot != expected_snapshot


def _attachment_evidence_source_key(item):
    match = re.fullmatch(r"answer:(\d+)-(\d+):attachment:\d+", str(item.get("source_ref") or "")) if isinstance(item, dict) else None
    return "%s-%s" % (match.group(1), match.group(2)) if match else ""


def _current_attachment_evidence(evidence, previous_state, next_state):
    previous_answers = ((previous_state.get("questionnaire_state") or {}).get("answers") or {}) if isinstance(previous_state, dict) else {}
    next_answers = ((next_state.get("questionnaire_state") or {}).get("answers") or {}) if isinstance(next_state, dict) else {}
    clean = []
    for item in evidence or []:
        step_key = _attachment_evidence_source_key(item)
        before, after = previous_answers.get(step_key), next_answers.get(step_key)
        if not step_key or not isinstance(after, dict) or after.get("confirmed") is not True:
            continue
        if isinstance(before, dict) and _answer_content(before) != _answer_content(after):
            continue
        clean.append(item)
    return clean[:MAX_CONFIRMED_ATTACHMENT_EVIDENCE]


def _merge_attachment_evidence(existing, current, current_step_key=""):
    current_steps = {_attachment_evidence_source_key(item) for item in current}
    if current_step_key:
        current_steps.add(current_step_key)
    merged = [item for item in existing or [] if _attachment_evidence_source_key(item) not in current_steps]
    merged.extend(current)
    return merged[-MAX_CONFIRMED_ATTACHMENT_EVIDENCE:]


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
        return bool(re.search(r"(?:^|[^a-z0-9+.-])data:[^,\s\"'<>]*,", value, flags=re.I))
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


def _encode_managed_state(value):
    if not isinstance(value, dict) or _contains_data_url(value):
        raise DigitalIPValidationError("项目状态无效")
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > PROJECT_MANAGED_STATE_MAX:
        raise DigitalIPValidationError("项目状态内容过大")
    return encoded


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
    clean_state = None
    if has_state:
        clean_state, _ = _clean_state(payload["state"])
    return _run_project_mutation(
        username, project_id,
        lambda: _patch_project(username, project_id, revision, has_title, has_state, clean_state, payload),
    )


def _patch_project(username, project_id, revision, has_title, has_state, clean_state, payload):
    row = _owned_project(username, project_id)
    if int(row["revision"]) != revision:
        raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
    fields, values = [], []
    if has_title:
        fields.extend(["title=?"]); values.append(_clean_project_title(payload["title"]))
    if has_state:
        previous_state = _json_object(row["state_json"])
        previous_questionnaire = previous_state.get("questionnaire_state")
        next_questionnaire = clean_state.get("questionnaire_state")
        previous_answers = previous_questionnaire.get("answers") if isinstance(previous_questionnaire, dict) else {}
        next_answers = next_questionnaire.get("answers") if isinstance(next_questionnaire, dict) else {}
        previous_answers = previous_answers if isinstance(previous_answers, dict) else {}
        next_answers = next_answers if isinstance(next_answers, dict) else {}
        previous_interview_v2 = (
            isinstance(previous_questionnaire, dict)
            and previous_questionnaire.get("interviewVersion") == 2
        )
        upgrading_to_interview_v2 = (
            isinstance(next_questionnaire, dict)
            and next_questionnaire.get("interviewVersion") == 2
            and not previous_interview_v2
        )
        migrating_to_interview_v2 = (
            upgrading_to_interview_v2
            and not any(
                "%d-%d" % (module_index, step_index) in next_answers
                for module_index in range(FOUNDATION_MODULES, ACTIVE_PROJECT_MODULES)
                for step_index in range(PROJECT_MODULE_STEPS[module_index])
            )
        )
        foundation_changed = any(
            previous_answers.get("%d-%d" % (module_index, step_index)) != next_answers.get("%d-%d" % (module_index, step_index))
            for module_index, step_count in enumerate(PROJECT_MODULE_STEPS[:FOUNDATION_MODULES])
            for step_index in range(step_count)
        )
        content_answer_keys = tuple(
            "%d-%d" % (module_index, step_index)
            for module_index in range(FOUNDATION_MODULES, ACTIVE_PROJECT_MODULES)
            for step_index in range(PROJECT_MODULE_STEPS[module_index])
        )
        content_changed = any(previous_answers.get(key) != next_answers.get(key) for key in content_answer_keys)
        has_content_answers = any(key in next_answers for key in content_answer_keys)
        if ((has_content_answers and not _foundation_is_confirmed(row)
             and (content_changed or upgrading_to_interview_v2))
                or (not migrating_to_interview_v2 and content_changed and foundation_changed)):
            raise DigitalIPValidationError("请先确认未变更的模块 1–4 阶段报告，再填写模块 5–6")
        managed_report = _json_object(row["state_json"]).get(REPORT_STATE_KEY)
        if isinstance(managed_report, dict):
            clean_state = dict(clean_state)
            clean_state[REPORT_STATE_KEY] = managed_report
        fields.extend(["state_json=?"]); values.append(_encode_managed_state(clean_state))
        analysis = _json_object(row["last_analysis_json"])
        confirmed = _json_object(row["confirmed_json"])
        attachment_evidence = _current_attachment_evidence(confirmed.get("attachment_evidence"), previous_state, clean_state)
        analyzed_input = analysis.get("input") if isinstance(analysis.get("input"), dict) else {}
        module_index, step_index = analyzed_input.get("module_index"), analyzed_input.get("step_index")
        questionnaire = clean_state.get("questionnaire_state") if isinstance(clean_state, dict) else {}
        answers = questionnaire.get("answers") if isinstance(questionnaire, dict) else {}
        step_state = answers.get("%s-%s" % (module_index, step_index)) if isinstance(answers, dict) else None
        confirmed_answers_changed = _confirmed_answers_changed(previous_state, clean_state, analyzed_input)
        if analysis and (confirmed_answers_changed
                         or (isinstance(step_state, dict) and step_state.get("skipped") is True)
                         or _state_answer(clean_state, module_index, step_index) != str(analyzed_input.get("answer") or "").strip()):
            confirmed = {"attachment_evidence": attachment_evidence} if attachment_evidence else {}
            fields.extend(["last_analysis_json=?", "confirmed_json=?"]); values.extend(["{}", json.dumps(confirmed, ensure_ascii=False)])
        elif attachment_evidence != confirmed.get("attachment_evidence", []):
            confirmed = dict(confirmed)
            if attachment_evidence:
                confirmed["attachment_evidence"] = attachment_evidence
            else:
                confirmed.pop("attachment_evidence", None)
            fields.append("confirmed_json=?"); values.append(json.dumps(confirmed, ensure_ascii=False))
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


def _confirmed_attachment_evidence(analysis_record):
    if not isinstance(analysis_record, dict):
        return []
    input_data = analysis_record.get("input") if isinstance(analysis_record.get("input"), dict) else {}
    attachment_names = {
        str(name).strip() for name in (input_data.get("attachment_names") or [])
        if str(name).strip()
    }
    analysis = analysis_record.get("analysis") if isinstance(analysis_record.get("analysis"), dict) else {}
    source_ref = "answer:%s-%s" % (input_data.get("module_index"), input_data.get("step_index"))
    clean, seen = [], set()
    for item in analysis.get("source_evidence") or []:
        if not isinstance(item, dict):
            continue
        file_name = str(item.get("file_name") or "").strip()
        evidence = _optional_text(item.get("evidence") or item.get("claim"), 1200)
        if file_name not in attachment_names or not evidence:
            continue
        location = _optional_text(item.get("location"), 160) or "未定位"
        key = (file_name, location, evidence)
        if key in seen:
            continue
        seen.add(key)
        clean.append({
            "source_ref": "%s:attachment:%d" % (source_ref, len(clean) + 1),
            "file_name": file_name,
            "location": location,
            "claim": _optional_text(item.get("claim"), 400),
            "evidence": evidence,
        })
        if len(clean) >= MAX_CONFIRMED_ATTACHMENT_EVIDENCE:
            break
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
    if module_index >= ACTIVE_PROJECT_MODULES or step_index >= PROJECT_MODULE_STEPS[module_index]:
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
        raise DigitalIPError("AI 分析服务尚未配置")
    rate_stamp = _check_rate_limit(username)
    try:
        daily_key = _check_project_daily_limit(username)
    except Exception:
        _release_rate_limit(username, rate_stamp)
        raise
    module_rule = MODULE_PROMPT_RULES[clean["module_index"]]
    prompt = {"industry_context": "由用户资料判断，不预设行业", "module_index": clean["module_index"],
              "module_rule": module_rule, "step_index": clean["step_index"],
              "answer": clean["answer"], "context": clean["context"]}
    content = [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}]
    for item in clean["files"]:
        if item["type"].startswith("image/"):
            content.append({"type": "input_image", "image_url": item["data_url"], "detail": "high"})
        else:
            content.append({"type": "input_file", "filename": item["name"], "file_data": item["data_url"]})
    request = {
        "model": MODEL, "instructions": INSTRUCTIONS + "\n当前模块规则：" + module_rule + "\n必须逐条标明资料来源 file_name 和位置 location；无附件时 file_name 写“用户当前回答”，无法精确定位写“未定位”，绝不编造页码。必须补齐资料来源证据、缺口/冲突和可执行的图片、视频计划；不得自动生成图片或视频。",
        "input": [{"role": "user", "content": content}], "reasoning": {"effort": REASONING_EFFORT},
        "text": {"verbosity": "low", "format": {"type": "json_schema", "name": "digital_ip_project_analysis", "strict": True, "schema": PROJECT_ANALYSIS_SCHEMA}},
        "max_output_tokens": 25000, "store": False,
        "safety_identifier": hashlib.sha256(username.encode()).hexdigest()[:32],
    }
    try:
        response = _post("/v1/responses", json.dumps(request, ensure_ascii=False).encode(), "application/json", timeout=120)
    except urllib.error.HTTPError as exc:
        _release_rate_limit(username, rate_stamp)
        _release_project_daily_limit(daily_key)
        print("[digital-ip-project] OpenAI HTTP %s" % exc.code, flush=True)
        raise DigitalIPError("AI 分析服务暂时不可用，请稍后重试") from exc
    except Exception as exc:
        _release_rate_limit(username, rate_stamp)
        _release_project_daily_limit(daily_key)
        print("[digital-ip-project] OpenAI request failed: %s" % type(exc).__name__, flush=True)
        raise DigitalIPError("AI 分析服务暂时不可用，请稍后重试") from exc
    try:
        analysis = _extract_project_output(response)
        if _contains_data_url(analysis):
            raise DigitalIPError("AI 返回结果包含不可保存的文件内容，请重试")
    except DigitalIPError:
        _release_rate_limit(username, rate_stamp)
        _release_project_daily_limit(daily_key)
        raise
    return analysis, str(response.get("model") or MODEL), response.get("usage") or {}


def _require_content_unlocked(row, module_index):
    if isinstance(module_index, bool) or not isinstance(module_index, int) or module_index not in range(ACTIVE_PROJECT_MODULES):
        raise DigitalIPValidationError("当前模块无效")
    if module_index >= FOUNDATION_MODULES and not _foundation_is_confirmed(row):
        raise DigitalIPValidationError("请先生成并确认模块 1–4 阶段报告，再进入模块 5–6")


def _analyze_project(username, project_id, clean):
    row = _owned_project(username, project_id)
    if int(row["revision"]) != clean["revision"]:
        raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
    _require_content_unlocked(row, clean["module_index"])
    if _project_state_answer(row, clean["module_index"], clean["step_index"]) != clean["answer"]:
        raise DigitalIPRevisionConflict("当前回答尚未保存或已经变更，请保存后重新分析")
    analysis, model, usage = _project_analysis(clean, username)
    now = int(time.time())
    stored = json.dumps({"analysis_id": uuid.uuid4().hex, "analysis": analysis, "model": model, "created_at": now,
                         "input": {"module_index": clean["module_index"], "step_index": clean["step_index"], "answer": clean["answer"],
                                   "attachment_names": [item["name"] for item in clean["files"]]}}, ensure_ascii=False)
    with closing(_project_db()) as conn:
        cursor = conn.execute("UPDATE digital_ip_projects SET last_analysis_json=?, revision=revision+1, updated_at=? WHERE id=? AND username=? AND revision=?",
                              (stored, now, project_id, username, clean["revision"]))
        conn.commit()
        if cursor.rowcount != 1:
            raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
        updated = conn.execute("SELECT * FROM digital_ip_projects WHERE id=? AND username=?", (project_id, username)).fetchone()
    return {"project": _project_public(updated), "analysis": analysis, "usage": usage, "ok": True}


def analyze_project(username, project_id, payload):
    clean = _clean_analysis_payload(payload)
    return _run_project_inflight(
        "analyze", username, project_id, clean["revision"],
        lambda: _analyze_project(username, project_id, clean),
    )


def confirm_project(username, project_id, payload):
    if not isinstance(payload, dict):
        raise DigitalIPValidationError("请求体必须是 JSON 对象")
    revision = _revision(payload.get("revision"))
    candidate_index = payload.get("candidate_index")
    if isinstance(candidate_index, bool) or not isinstance(candidate_index, int) or candidate_index not in range(3):
        raise DigitalIPValidationError("candidate_index 无效")
    return _run_project_mutation(
        username, project_id,
        lambda: _confirm_project(username, project_id, revision, candidate_index),
    )


def _confirm_project(username, project_id, revision, candidate_index):
    row = _owned_project(username, project_id)
    if int(row["revision"]) != revision:
        raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
    analysis_record = _json_object(row["last_analysis_json"])
    analysis = analysis_record.get("analysis") if isinstance(analysis_record.get("analysis"), dict) else {}
    analyzed_input = analysis_record.get("input") if isinstance(analysis_record.get("input"), dict) else {}
    _require_content_unlocked(row, analyzed_input.get("module_index", 0))
    candidates = analysis.get("positioning_candidates") if isinstance(analysis.get("positioning_candidates"), list) else []
    if len(candidates) != 3 or candidate_index >= len(candidates):
        raise DigitalIPValidationError("请先完成一次有效分析，再确认候选")
    if _project_state_answer(row, analyzed_input.get("module_index"), analyzed_input.get("step_index")) != str(analyzed_input.get("answer") or "").strip():
        raise DigitalIPRevisionConflict("当前回答已经变更，请重新分析后再确认")
    prior_confirmed = _json_object(row["confirmed_json"])
    confirmed = {"analysis_id": analysis_record.get("analysis_id"), "candidate_index": candidate_index, "profile": candidates[candidate_index],
                 "plans": {"image_plan": analysis.get("image_plan"), "video_plan": analysis.get("video_plan"), "next_steps": analysis.get("next_steps")},
                 "attachment_evidence": _merge_attachment_evidence(
                     prior_confirmed.get("attachment_evidence"),
                     _confirmed_attachment_evidence(analysis_record),
                     "%s-%s" % (analyzed_input.get("module_index"), analyzed_input.get("step_index")),
                 ),
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


def _report_answer_text(value):
    if isinstance(value, dict):
        text = str(value.get("text") or "").strip()
        if text:
            return text[:1600]
        choice = value.get("choice")
        if isinstance(choice, list):
            return "、".join(str(item).strip() for item in choice if str(item).strip())[:1600]
        return str(choice or "").strip()[:1600]
    return str(value or "").strip()[:1600]


def _report_source(row):
    state = _json_object(row["state_json"])
    questionnaire = state.get("questionnaire_state")
    questionnaire = questionnaire if isinstance(questionnaire, dict) else {}
    answers = questionnaire.get("answers")
    answers = answers if isinstance(answers, dict) else {}
    confirmed_answers, skipped_steps, unresolved = [], [], []
    for module_index, step_count in enumerate(PROJECT_MODULE_STEPS[:FOUNDATION_MODULES]):
        for step_index in range(step_count):
            key = "%d-%d" % (module_index, step_index)
            value = answers.get(key)
            answer = _report_answer_text(value)
            if isinstance(value, dict) and value.get("confirmed") is True and answer:
                step_title, source_kind = FOUNDATION_STEP_META[module_index][step_index]
                confirmed_answers.append({
                    "source_ref": "answer:%s" % key,
                    "module_id": module_index + 1,
                    "module_name": ACTIVE_MODULE_NAMES[module_index],
                    "step_index": step_index + 1,
                    "step_title": step_title,
                    "source_kind": source_kind,
                    "answer": answer,
                })
            elif isinstance(value, dict) and value.get("skipped") is True:
                skipped_steps.append("answer:%s" % key)
            else:
                unresolved.append("answer:%s" % key)
    confirmed = _json_object(row["confirmed_json"])
    attachment_evidence = confirmed.get("attachment_evidence") if isinstance(confirmed.get("attachment_evidence"), list) else []
    attachment_evidence = [
        item for item in attachment_evidence
        if _attachment_evidence_source_key(item)
        and int(_attachment_evidence_source_key(item).split("-", 1)[0]) < FOUNDATION_MODULES
    ]
    return {
        "confirmed_answers": confirmed_answers,
        "confirmed_attachment_evidence": attachment_evidence,
        "skipped_steps": skipped_steps,
        "unresolved_steps": unresolved,
        "progress": {
            "total": sum(PROJECT_MODULE_STEPS[:FOUNDATION_MODULES]),
            "confirmed": len(confirmed_answers),
            "skipped": len(skipped_steps),
            "unresolved": len(unresolved),
        },
    }


def _validate_report(report, source):
    if not isinstance(report, dict):
        raise DigitalIPError("AI 返回的报告格式异常，请重试")
    evidence = report.get("evidence")
    modules = report.get("modules")
    priorities = report.get("execution_priorities")
    confirmation_items = report.get("confirmation_items")
    gaps = report.get("material_gaps")
    if not all(isinstance(value, list) for value in (evidence, modules, priorities, confirmation_items, gaps)):
        raise DigitalIPError("AI 返回的报告格式异常，请重试")
    source_texts = {
        item["source_ref"]: " ".join(item["answer"].split())
        for item in source["confirmed_answers"] if item.get("answer")
    }
    source_texts.update({
        str(item.get("source_ref") or ""): " ".join(str(item.get("evidence") or "").split())
        for item in source.get("confirmed_attachment_evidence", [])
        if isinstance(item, dict) and item.get("source_ref") and item.get("evidence")
    })
    source_display = {
        item["source_ref"]: {
            "source_name": "已确认问卷回答",
            "source_location": "模块 %s · %s" % (item.get("module_id"), item.get("step_title")),
            "source_kind": item.get("source_kind") or "fact",
        }
        for item in source["confirmed_answers"] if item.get("source_ref")
    }
    source_display.update({
        str(item["source_ref"]): {
            "source_name": str(item.get("file_name") or "已确认附件"),
            "source_location": str(item.get("location") or "未定位"),
            "source_kind": "fact",
        }
        for item in source.get("confirmed_attachment_evidence", [])
        if isinstance(item, dict) and item.get("source_ref")
    })
    evidence_ids, evidence_kinds = set(), {}
    for item in evidence:
        if not isinstance(item, dict):
            raise DigitalIPError("AI 返回的报告证据格式异常，请重试")
        evidence_id = str(item.get("evidence_id") or "").strip()
        source_ref = str(item.get("source_ref") or "").strip()
        excerpt = " ".join(str(item.get("source_excerpt") or "").split())
        if not evidence_id or evidence_id in evidence_ids or source_ref not in source_texts:
            raise DigitalIPError("AI 返回的报告证据无法追溯，请重试")
        if not excerpt or excerpt not in source_texts[source_ref]:
            raise DigitalIPError("AI 返回的报告引用与已确认资料不一致，请重试")
        item.update(source_display[source_ref])
        evidence_ids.add(evidence_id)
        evidence_kinds[evidence_id] = source_display[source_ref]["source_kind"]
    module_ids = [item.get("module_id") for item in modules if isinstance(item, dict)]
    if module_ids != [1, 2, 3, 4]:
        raise DigitalIPError("AI 返回的报告模块不完整，请重试")
    for module in modules:
        findings = module.get("findings")
        if not isinstance(findings, list):
            raise DigitalIPError("AI 返回的报告模块格式异常，请重试")
        for finding in findings:
            refs = finding.get("evidence_ids") if isinstance(finding, dict) else None
            if not isinstance(refs, list) or not refs or not set(refs).issubset(evidence_ids):
                raise DigitalIPError("AI 返回的报告结论缺少有效证据，请重试")
            if finding.get("kind") == "fact" and any(evidence_kinds[ref] != "fact" for ref in refs):
                raise DigitalIPError("AI 把用户偏好或系统备选误写成了事实，请重试")
    for item in priorities:
        refs = item.get("evidence_ids") if isinstance(item, dict) else None
        if not isinstance(refs, list) or not refs or not set(refs).issubset(evidence_ids):
            raise DigitalIPError("AI 返回的执行建议缺少有效证据，请重试")
    for item in confirmation_items:
        refs = item.get("evidence_ids") if isinstance(item, dict) else None
        if not isinstance(refs, list) or not set(refs).issubset(evidence_ids):
            raise DigitalIPError("AI 返回的待确认项缺少有效证据，请重试")
    skipped_refs = set(source["skipped_steps"])
    covered_skips = set()
    for item in gaps:
        refs = item.get("source_refs") if isinstance(item, dict) else None
        if not isinstance(refs, list) or not set(refs).issubset(skipped_refs):
            raise DigitalIPError("AI 返回的资料缺口无法追溯，请重试")
        covered_skips.update(refs)
    if not skipped_refs.issubset(covered_skips):
        raise DigitalIPError("AI 没有完整标明被跳过的资料缺口，请重试")
    return report


def _generate_report_content(source, username, project_title):
    if not OPENAI_KEY:
        raise DigitalIPError("AI 分析服务尚未配置")
    rate_stamp, daily_key = _check_report_rate_limit(username)
    prompt = {
        "project_title": project_title,
        "confirmed_answers": source["confirmed_answers"],
        "confirmed_attachment_evidence": source.get("confirmed_attachment_evidence", []),
        "skipped_steps": source["skipped_steps"],
        "coming_soon_modules": ["IP 形象设计", "脚本分镜", "私域矩阵", "朋友圈运营", "销售策略", "公众号变现"],
    }
    request = {
        "model": MODEL,
        "instructions": REPORT_INSTRUCTIONS,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}]}],
        "reasoning": {"effort": REASONING_EFFORT},
        "text": {"verbosity": "low", "format": {
            "type": "json_schema", "name": "digital_ip_foundation_report",
            "strict": True, "schema": REPORT_SCHEMA,
        }},
        "max_output_tokens": 30000,
        "store": False,
        "safety_identifier": hashlib.sha256(username.encode()).hexdigest()[:32],
    }
    try:
        response = _post("/v1/responses", json.dumps(request, ensure_ascii=False).encode(), "application/json", timeout=120)
    except urllib.error.HTTPError as exc:
        _release_report_rate_limit(username, rate_stamp, daily_key)
        print("[digital-ip-report] OpenAI HTTP %s" % exc.code, flush=True)
        raise DigitalIPError("报告生成服务暂时不可用，请稍后重试") from exc
    except Exception as exc:
        _release_report_rate_limit(username, rate_stamp, daily_key)
        print("[digital-ip-report] OpenAI request failed: %s" % type(exc).__name__, flush=True)
        raise DigitalIPError("报告生成服务暂时不可用，请稍后重试") from exc
    try:
        report = _validate_report(_parse_structured_output(response), source)
    except DigitalIPError:
        _release_report_rate_limit(username, rate_stamp, daily_key)
        raise
    return report, str(response.get("model") or MODEL), response.get("usage") or {}, request


def _generate_report(username, project_id, revision):
    row = _owned_project(username, project_id)
    if int(row["revision"]) != revision:
        raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
    source = _report_source(row)
    if source["unresolved_steps"]:
        raise DigitalIPValidationError("请先完成或跳过模块 1–4 的采访问题，再生成阶段报告")
    report, model, usage, _ = _generate_report_content(source, username, row["title"])
    source_hash = _source_hash(source)
    now = int(time.time())
    envelope = {
        "stage": FOUNDATION_STAGE,
        "status": "pending_confirmation",
        "report_id": uuid.uuid4().hex,
        "source_revision": revision,
        "project_revision": revision + 1,
        "source_hash": source_hash,
        "confirmed_source_hash": "",
        "generated_at": now,
        "confirmed_at": None,
        "model": model,
        "usage": usage,
        "progress": source["progress"],
        "content": report,
    }
    state = _json_object(row["state_json"])
    state[REPORT_STATE_KEY] = envelope
    state_json = _encode_managed_state(state)
    with closing(_project_db()) as conn:
        cursor = conn.execute(
            "UPDATE digital_ip_projects SET state_json=?, revision=revision+1, updated_at=? WHERE id=? AND username=? AND revision=?",
            (state_json, now, project_id, username, revision),
        )
        conn.commit()
        if cursor.rowcount != 1:
            raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
        updated = conn.execute("SELECT * FROM digital_ip_projects WHERE id=? AND username=?", (project_id, username)).fetchone()
    public_report = dict(envelope)
    public_report.pop("model", None)
    public_report["pdf_url"] = "/api/gen/digital-ip/projects/%s/report.pdf" % project_id
    return {"ok": True, "project": _project_public(updated), "report": public_report, "stale": False}


def generate_report(username, project_id, payload):
    if not isinstance(payload, dict):
        raise DigitalIPValidationError("请求体必须是 JSON 对象")
    if payload.get("consent") is not True:
        raise DigitalIPValidationError("请先明确同意将已保存的 IP12 回答发送给 AI 分析服务生成报告")
    revision = _revision(payload.get("revision"))
    return _run_project_inflight(
        "report", username, project_id, revision,
        lambda: _generate_report(username, project_id, revision),
    )


def get_report(username, project_id):
    row = _owned_project(username, project_id)
    report = _json_object(row["state_json"]).get(REPORT_STATE_KEY)
    if not isinstance(report, dict) or not isinstance(report.get("content"), dict):
        raise DigitalIPNotFound("报告尚未生成")
    public_report = dict(report)
    public_report.pop("model", None)
    public_report["pdf_url"] = "/api/gen/digital-ip/projects/%s/report.pdf" % project_id
    stage_status = _foundation_stage(row)
    return {
        "project": {"id": row["id"], "title": row["title"], "revision": int(row["revision"])},
        "report": public_report,
        "stage_status": stage_status,
        "stale": stage_status["stale"],
    }


def confirm_report(username, project_id, payload):
    if not isinstance(payload, dict):
        raise DigitalIPValidationError("请求体必须是 JSON 对象")
    revision = _revision(payload.get("revision"))
    report_id = str(payload.get("report_id") or "").strip()
    if not report_id or len(report_id) > 64:
        raise DigitalIPValidationError("report_id 无效")
    return _run_project_mutation(
        username, project_id,
        lambda: _confirm_report(username, project_id, revision, report_id),
    )


def _confirm_report(username, project_id, revision, report_id):
    row = _owned_project(username, project_id)
    if int(row["revision"]) != revision:
        raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
    state = _json_object(row["state_json"])
    report = state.get(REPORT_STATE_KEY)
    if not isinstance(report, dict) or report.get("stage") != FOUNDATION_STAGE:
        raise DigitalIPValidationError("请先重新生成模块 1–4 阶段报告")
    if report.get("report_id") != report_id:
        raise DigitalIPRevisionConflict("阶段报告已经更新，请刷新后重试")
    source_hash = _source_hash(_report_source(row))
    if report.get("source_hash") != source_hash:
        raise DigitalIPRevisionConflict("模块 1–4 资料已经变化，请重新生成报告")
    if report.get("status") == "confirmed" and report.get("confirmed_source_hash") == source_hash:
        return {"ok": True, "project": _project_public(row)}
    now = int(time.time())
    report.update(status="confirmed", confirmed_source_hash=source_hash, confirmed_at=now, project_revision=revision + 1)
    state[REPORT_STATE_KEY] = report
    with closing(_project_db()) as conn:
        cursor = conn.execute(
            "UPDATE digital_ip_projects SET state_json=?, revision=revision+1, updated_at=? WHERE id=? AND username=? AND revision=?",
            (_encode_managed_state(state), now, project_id, username, revision),
        )
        conn.commit()
        if cursor.rowcount != 1:
            raise DigitalIPRevisionConflict("项目已在另一端更新，请刷新后重试")
        updated = conn.execute("SELECT * FROM digital_ip_projects WHERE id=? AND username=?", (project_id, username)).fetchone()
    return {"ok": True, "project": _project_public(updated)}


def export_report_pdf(username, project_id):
    payload = get_report(username, project_id)
    report_id = str(payload["report"].get("report_id") or "report")
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", report_id)[:16] or "report"
    filename = "huangque-ip12-%s.pdf" % safe_id
    cache_key = (username, project_id, report_id, int(payload["project"]["revision"]))
    if not _pdf_lock.acquire(blocking=False):
        raise DigitalIPPDFBusy("另一份 PDF 正在生成，请稍后重试")
    try:
        if _pdf_cache.get("key") == cache_key:
            return _pdf_cache["data"], filename
        now = time.monotonic()
        for owner in [owner for owner, stamp in _pdf_recent_renders.items() if now - stamp >= 60]:
            _pdf_recent_renders.pop(owner, None)
        last_render = _pdf_recent_renders.get(username)
        if last_render is not None and now - last_render < 30:
            raise DigitalIPPDFBusy("PDF 生成过于频繁，请稍后重试")
        _pdf_recent_renders[username] = now
        data = ip12_pdf.render_report_pdf(payload)
    except DigitalIPPDFBusy:
        raise
    except Exception as exc:
        _pdf_recent_renders.pop(username, None)
        print("[digital-ip-pdf] render failed: %s" % type(exc).__name__, flush=True)
        raise DigitalIPPDFUnavailable("PDF 暂时无法生成，请稍后重试") from exc
    else:
        # ponytail: single-entry cache caps memory; use a shared bounded cache only if PDF traffic grows.
        _pdf_cache.update(key=cache_key, data=data)
    finally:
        _pdf_lock.release()
    return data, filename


def _send_pdf(handler, data, filename):
    handler.send_response(200)
    handler.send_header("Content-Type", "application/pdf")
    handler.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "private, no-store, max-age=0")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(data)


def _digital_ip_membership_required(user):
    return bool(user.get("_membership_enforcement_enabled") and not user.get("membership_active"))


def _send_membership_required(handler):
    handler._send(403, {"detail": "请先开通会员后再使用数字化 IP AI 分析", "code": "membership_required",
                        "membership_url": "/workbench/recharge", "membership_enforcement_enabled": True})


def dispatch_http(handler, method, verify_token, must_change_password):
    """Thin HTTP adapter; project and AI behavior stays in this domain."""
    path = handler.path.split("?")[0]
    root = "/api/gen/digital-ip/projects"
    legacy = method == "POST" and path in {"/api/gen/digital-ip/diagnose", "/api/gen/digital-ip/guide"}
    if not legacy and path != root and not path.startswith(root + "/"):
        return False
    user = verify_token(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录或登录已过期"})
        return True
    if method in {"POST", "PATCH"} and must_change_password(user):
        handler._send(403, {"detail": "请先修改初始密码"})
        return True
    try:
        if legacy:
            if _digital_ip_membership_required(user):
                _send_membership_required(handler)
            else:
                action = guide if path.endswith("/guide") else diagnose
                handler._send(200, action(handler._json_body_strict(), user["username"]))
            return True
        if method == "GET":
            if path == root:
                handler._send(200, {"items": list_projects(user["username"])})
            else:
                parts = path[len(root) + 1:].split("/")
                if len(parts) == 2 and parts[0] and parts[1] == "report.pdf":
                    data, filename = export_report_pdf(user["username"], parts[0])
                    _send_pdf(handler, data, filename)
                elif len(parts) == 2 and parts[0] and parts[1] == "report":
                    handler._send(200, get_report(user["username"], parts[0]))
                elif len(parts) == 1 and parts[0]:
                    handler._send(200, {"project": get_project(user["username"], parts[0])})
                else:
                    handler._send(404, {"detail": "not found"})
            return True
        content_length = int(handler.headers.get("Content-Length") or 0)
        if content_length <= 0:
            handler._send(400, {"detail": "请求体不能为空"})
            return True
        if content_length > MAX_PROJECT_BODY_BYTES:
            handler._send(413, {"detail": "资料请求不能超过 20 MiB" if method == "POST" else "请求过大"})
            return True
        body = handler._json_body_strict()
        if method == "PATCH":
            project_id = path[len(root) + 1:]
            handler._send(404, {"detail": "not found"}) if not project_id or "/" in project_id else handler._send(200, {"project": patch_project(user["username"], project_id, body)})
            return True
        if method == "POST" and path == root:
            handler._send(200, {"project": create_project(user["username"], body)})
            return True
        parts = path[len(root) + 1:].split("/")
        if method != "POST" or len(parts) != 2 or not parts[0]:
            handler._send(404, {"detail": "not found"})
            return True
        project_id, action = parts
        if action == "analyze":
            if _digital_ip_membership_required(user):
                _send_membership_required(handler)
            else:
                handler._send(200, analyze_project(user["username"], project_id, body))
        elif action == "confirm":
            handler._send(200, confirm_project(user["username"], project_id, body))
        elif action == "report":
            if _digital_ip_membership_required(user):
                _send_membership_required(handler)
            else:
                handler._send(200, generate_report(user["username"], project_id, body))
        elif action == "report-confirm":
            handler._send(200, confirm_report(user["username"], project_id, body))
        else:
            handler._send(404, {"detail": "not found"})
    except ValueError as exc:
        handler._send(400, {"detail": str(exc)[:220]})
    except DigitalIPError as exc:
        handler._send(exc.status, {"detail": str(exc)})
    except Exception:
        handler._send(502, {"detail": "数字化 IP 项目服务暂时不可用，请稍后重试"})
    return True
