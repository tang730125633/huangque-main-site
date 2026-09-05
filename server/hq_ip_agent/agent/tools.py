"""Function Calling 工具定义与分发（交付物之二）。

「黄雀 CLI 定义为 Function」：把 hq 的能力声明成 OpenAI function schema，
并把每个函数名分发到对应的 CLI 调用。主 Agent 只看到函数签名与返回结果，
由它自主决定何时调用、传什么参数。
"""
import json
import uuid

from . import hq_cli, modules56, report, state
from .info_schema import FIELDS, MODULES, core_ratio

# ---------------------------------------------------------------------------
# 工具签名（OpenAI function calling 的 tools 数组）
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "update_profile",
            "description": (
                "把从对话中采集到的事实写入内部信息表（用户不可见）。"
                "facts 的键尽量用采集表规范字段键（如 basic.name、career.current_job、"
                "experience.setback、direction.track、style.tone、value.self_intro、"
                "story.comeback、business.goal），值写用户原话要点。"
                "每得到用户一段回答就调用一次，按「模块/问题」粒度更新。"
                "只记录已经确认的事实；模糊或矛盾的信息先不要写入。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "facts": {
                        "type": "object",
                        "description": "键值对：键=规范字段键（模块id.字段key）或信息主题，值=采集到的内容",
                        "additionalProperties": True,
                    }
                },
                "required": ["facts"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_profile",
            "description": (
                "读取内部信息表当前已采集的内容，用于判断哪些已经采集、哪些缺失或矛盾。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "profile_status",
            "description": (
                "按采集表 8 大模块、43 个字段回报内部信息表的采集进度：每个模块哪些字段已采、"
                "哪些缺失（含问题原文）、核心字段覆盖率。用来决定下一个问什么、"
                "以及信息是否足够生成报告。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_report",
            "description": (
                "用已采集信息生成《IP人设定位｜模块1-4》报告初稿 PDF。"
                "工具内部严格按样例模板校验（模块一核心关键词×7/最终定位/市场机缘/潜在风险，"
                "模块二三套人设方案+推荐理由+核心人设要素，模块三诊断/价值主张/推荐金句/备选金句/"
                "自我介绍优化/变现路径，模块四故事库≥5（含情绪曲线/钩子设计/传播价值）/故事主线/"
                "优化建议/执行优先级/文档状态），缺一不可；校验不通过会自动把缺口反馈给生成模型"
                "修订重跑（最多3轮）。返回 gaps 表示最终仍缺失的部分。"
                "调用前先用 get_profile / profile_status 自查信息是否足够；信息不足就先追问。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "可选的生成要求，例如用户刚补充的重点信息或特别要求",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_report",
            "description": (
                "用户从三套人设方案中选定一套后，生成定稿 PDF：只把「最终推荐」更新为"
                "用户选定的方案并重写推荐理由，其余内容保留初稿。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chosen": {
                        "type": "string",
                        "enum": ["A", "B", "C"],
                        "description": "用户选定的方案编号 A / B / C",
                    },
                },
                "required": ["chosen"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_revise",
            "description": (
                "用户对已生成的报告初稿提出修改意见时，让生成模型按意见修订并重新校验出稿。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "feedback": {
                        "type": "string",
                        "description": "用户的修改意见原文或整理后的要求",
                    },
                },
                "required": ["feedback"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_report",
            "description": (
                "查看当前会话的报告状态：生成进度、是否已出稿、文件（PDF/MD）与校验情况。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "m5_topics",
            "description": (
                "模块5·选题生成：用户已在 UI 确认模块1-4 报告后调用。输入直接从已确认 PDF 提取"
                "（目标人群/核心领域/核心优势/长期标签/近期目标），严禁重复询问已采集信息。"
                "输出不少于 15 个选题（故事型/干货型/案例型三类齐备，各含标题/类型/目标效果）"
                "和 3 个重点推荐选题+原因；工具内部严格校验并按需修订重跑。"
                "启动后全程自动推进，无中间断点；生成完成后把选题清单与重点推荐完整转述给用户，"
                "并请用户选定一个重点选题（触发模块6）。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "m6_scripts",
            "description": (
                "模块6·文案生成：用户选定重点选题后调用。针对该选题生成 3 种风格"
                "（共情型/震撼型/故事型）的口播文案，各含 3秒钩子开头、逻辑递进中段、金句、"
                "CTA 行动号召结尾，并自动推荐一份最优文案+原因。"
                "文案必须结合用户真实故事，严禁编造。生成完成后转述三份文案与推荐理由，"
                "并告知用户可逐条提修改意见（用 script_revise 修订）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "用户选定的重点选题标题（与模块5选题清单一致）",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "script_revise",
            "description": (
                "用户对模块6 文案逐条提出修改意见时，让生成模型按意见修订相关部分"
                "（其余内容保留），并重新校验出稿。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "feedback": {
                        "type": "string",
                        "description": "用户的修改意见原文或整理后的要求",
                    },
                },
                "required": ["feedback"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_m5m6",
            "description": (
                "查看模块5（选题）与模块6（文案）的当前状态、产出文件与校验情况。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hq_status",
            "description": "检查黄雀 CLI 的登录状态与当前账号信息。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hq_ip12_projects",
            "description": "列出当前黄雀账号下的 IP12（IP 人设定位）项目。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hq_ip12_create",
            "description": (
                "在当前黄雀账号创建一个新的 IP12 项目（写入操作，自动传 --confirm，0 扣点）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "项目标题，例如「张三 · IP 定位」"}
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hq_ip12_project",
            "description": "读取一个 IP12 项目的基础资料、对话、模块进度与已存报告。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "项目 ID"}
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hq_ip12_report",
            "description": "读取一个 IP12 项目已保存的定位报告（不会重新生成）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "项目 ID"}
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hq_ip12_message",
            "description": (
                "向一个 IP12 项目提交一轮回答并调用黄雀 AI 教练（写入并调用 AI，0 扣点）。"
                "用于把采集到的信息喂给黄雀、生成定位内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "项目 ID"},
                    "message": {
                        "type": "string",
                        "description": "要提交给黄雀 AI 教练的内容（一段整理好的个人介绍或回答）",
                    },
                    "request_id": {
                        "type": "string",
                        "description": "本轮唯一 ID；留空则自动生成",
                    },
                },
                "required": ["project_id", "message"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 结果格式（供工具返回，会被 JSON 序列化喂给模型）
# ---------------------------------------------------------------------------

def _ok(data, note=None):
    return {"ok": True, "note": note, "result": data}


def _err(message, data=None):
    return {"ok": False, "error": message, "result": data}


def _profile_status(session_id: str) -> dict:
    """按采集表字段粒度回报采集进度（软提示，供 Agent 判断下一个问题）。"""
    profile = state.get_profile(session_id)
    covered = set()
    for key, val in profile.items():
        if not key.startswith("__") and val:
            covered.add(key)
            if "." not in key:
                # 自然语言键：如果值里提到某个字段 key，也算覆盖到该字段
                for f in FIELDS:
                    if f["key"].split(".", 1)[1] in key:
                        covered.add(f["key"])

    modules = []
    for m in MODULES:
        entries = []
        for f in FIELDS:
            if f["module"] != m["id"]:
                continue
            done = f["key"] in covered
            entries.append({"key": f["key"], "label": f["label"], "core": f["core"], "done": done})
        collected = [e for e in entries if e["done"]]
        missing = [e for e in entries if not e["done"]]
        modules.append({
            "module": m["id"], "name": m["name"],
            "collected": [e["key"] for e in collected],
            "missing": [{"key": e["key"], "label": e["label"], "core": e["core"]} for e in missing],
            "collected_count": len(collected), "total": len(entries),
        })

    core_done = [f["key"] for f in FIELDS if f["core"] and f["key"] in covered]
    missing_core = [{"key": f["key"], "label": f["label"], "module": f["module_name"]}
                    for f in FIELDS if f["core"] and f["key"] not in covered]
    return {
        "core_covered": len(core_done),
        "core_total": len([f for f in FIELDS if f["core"]]),
        "core_ratio": round(core_ratio(covered), 2),
        "missing_core": missing_core,
        "modules": modules,
        "hint": "核心字段覆盖率越高，越适合生成报告；生成前建议 core_ratio ≥ 0.7，"
                "且故事资产、职业背景、内容方向、性格风格、价值主张、商业目标均有核心字段已采。",
    }


def _fmt_hq(resp):
    """把 hq_cli 返回统一成简洁结构：成功时直接返回 `result` 载荷。"""
    if resp.get("exit_code") not in (0, None):
        d = resp.get("data", {})
        if isinstance(d, dict) and d.get("error"):
            msg = str(d.get("error"))
            if d.get("message"):
                msg += "：" + str(d["message"])
            return _err(msg, d)
        return _err(f"黄雀 CLI 调用失败（exit={resp['exit_code']}）", resp.get("data"))
    d = resp.get("data", {})
    payload = d.get("result") if isinstance(d, dict) else d
    return _ok(payload, note=(d.get("next_actions") if isinstance(d, dict) else None))


# ---------------------------------------------------------------------------
# 分发器
# ---------------------------------------------------------------------------

def dispatch(name: str, args: dict, session_id: str) -> dict:
    args = args or {}

    # 内部信息表（用户不可见）
    if name == "update_profile":
        profile = state.update_profile(session_id, args.get("facts") or {})
        return _ok(profile, note="已更新内部信息表（用户不可见）")

    if name == "get_profile":
        profile = state.get_profile(session_id)
        return _ok(profile, note="当前内部信息表")

    if name == "profile_status":
        return _ok(_profile_status(session_id), note="采集进度（按模块/字段）")

    # 报告生成（严格按样例模板，内容全部由 LLM 产出）
    if name == "generate_report":
        return report.generate_draft(session_id, (args.get("instruction") or "").strip())

    if name == "finalize_report":
        return report.finalize(session_id, (args.get("chosen") or "").strip().upper())

    if name == "report_revise":
        fb = (args.get("feedback") or "").strip()
        if not fb:
            return _err("缺少参数 feedback")
        return report.revise(session_id, fb)

    if name == "get_report":
        full = state.get_report_full(session_id)
        meta = dict(full)
        for key in ("_json", "_m5_json", "_m6_json"):
            meta.pop(key, None)  # 全文只留在服务端，不外发
        # 三套人设方案 + 推荐：主 Agent 可直接发进对话让用户选（初稿异步完成后用）
        rep = full.get("_json") or {}
        opts = ((rep.get("m2_persona") or {}).get("options") or [])
        if opts:
            meta["options"] = [
                {"id": o.get("id"), "title": o.get("title"),
                 "traits": o.get("traits"), "tags": o.get("tags")}
                for o in opts
            ]
            rec = (rep.get("m2_persona") or {}).get("recommendation") or {}
            meta["recommended"] = {
                "chosen": rec.get("chosen"), "title": rec.get("title"),
                "reasons": rec.get("reasons"),
            }
        return _ok(meta, note="当前报告状态")

    # 模块5（选题）/ 模块6（文案）：生成+校验+修订循环，内容全部由 LLM 产出
    if name == "m5_topics":
        return modules56.generate_topics(session_id)

    if name == "m6_scripts":
        return modules56.generate_scripts(session_id, (args.get("topic") or "").strip())

    if name == "script_revise":
        fb = (args.get("feedback") or "").strip()
        if not fb:
            return _err("缺少参数 feedback")
        return modules56.revise_scripts(session_id, fb)

    if name == "get_m5m6":
        full = state.get_report_full(session_id)
        meta = dict(full)
        for key in ("_json", "_m5_json", "_m6_json"):
            meta.pop(key, None)  # 全文只留在服务端，不外发
        out = {"m5": meta.get("m5"), "m6": meta.get("m6"), "confirmed": meta.get("confirmed")}
        # 内容全文：主 Agent 拿到后原样发进对话（选题清单/三版文案），不用只甩链接
        m5j = full.get("_m5_json") or {}
        if m5j.get("topics"):
            out["m5_topics"] = m5j["topics"]
            out["m5_recommended"] = m5j.get("recommended")
        m6j = full.get("_m6_json") or {}
        if m6j.get("scripts"):
            out["m6_scripts"] = [
                {"style": s.get("style"), "hook": s.get("hook"), "quote": s.get("quote"),
                 "cta": s.get("cta"), "full_text": s.get("full_text")}
                for s in m6j["scripts"]
            ]
            out["m6_recommended"] = m6j.get("recommended")
        return _ok(out, note="模块5/6 当前状态与内容全文")

    # 黄雀 CLI
    if name == "hq_status":
        return _fmt_hq(hq_cli.status())

    if name == "hq_ip12_projects":
        return _fmt_hq(hq_cli.run("ip12-projects"))

    if name == "hq_ip12_create":
        title = (args.get("title") or "").strip()
        if not title:
            return _err("缺少参数 title")
        return _fmt_hq(hq_cli.run("ip12-create", {"title": title}, confirm=True))

    if name == "hq_ip12_project":
        pid = (args.get("project_id") or "").strip()
        if not pid:
            return _err("缺少参数 project_id")
        return _fmt_hq(hq_cli.run("ip12-project", {"project_id": pid}))

    if name == "hq_ip12_report":
        pid = (args.get("project_id") or "").strip()
        if not pid:
            return _err("缺少参数 project_id")
        return _fmt_hq(hq_cli.run("ip12-report", {"project_id": pid}))

    if name == "hq_ip12_message":
        pid = (args.get("project_id") or "").strip()
        msg = (args.get("message") or "").strip()
        if not pid or not msg:
            return _err("缺少参数 project_id 或 message")
        rid = (args.get("request_id") or "").strip() or uuid.uuid4().hex[:24]
        return _fmt_hq(
            hq_cli.run(
                "ip12-message",
                {"project_id": pid, "message": msg, "request_id": rid},
                confirm=True,
            )
        )

    return _err(f"未知工具：{name}")


def tool_schema_by_name(name: str):
    for t in TOOLS:
        if t["function"]["name"] == name:
            return t
    return None
