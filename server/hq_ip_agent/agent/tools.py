"""Function Calling 工具定义与分发（交付物之二）。

「黄雀 CLI 定义为 Function」：把 hq 的能力声明成 OpenAI function schema，
并把每个函数名分发到对应的 CLI 调用。主 Agent 只看到函数签名与返回结果，
由它自主决定何时调用、传什么参数。
"""
import json
import uuid
from typing import Optional

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
            "name": "load_ip12_profile",
            "description": (
                "从黄雀主站拉取用户已保存的 IP 定位项目档案（基础资料、模块进度、已存报告），"
                "老系统（Hermes IP12）与新系统（数字化 IP）自动切换：老系统不可用时自动从新系统拉。"
                "使用时机：用户说「按我主站的定位/档案/IP12 项目做」；或新会话信息表为空、"
                "用户希望直接沿用主站已有定位（如「按主站已有的 IP12 定位写口播」）。"
                "project_id 不传时自动取最近一个项目；主站有多个项目时先让用户确认用哪个。"
                "返回主站档案原文（JSON）——从中提取人设事实用 update_profile 逐项写入内部信息表，"
                "并在回复里向用户复述档案要点，确认无误后再用于创作/报告。"
                "主站无项目/两边接口都不可用时如实告知用户，绝不编造档案内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "主站 IP12 项目 ID；不传则自动取最近项目",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_report",
            "description": (
                "用已采集信息生成《IP人设定位｜模块1-4》报告初稿 PDF。"
                "用户已明确选定方案时带 chosen 参数，直接出定稿（final）。"
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
                    "chosen": {
                        "type": "string",
                        "enum": ["A", "B", "C"],
                        "description": (
                            "用户已在对话里明确选定人设方案时传 A/B/C："
                            "报告将直接按所选方案生成定稿（status=final），"
                            "不再生成三套方案让用户重新选。用户还没选定时不要传。"
                        ),
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


def _seg(key: str) -> str:
    """键的末段（模块 id 之后的部分），用于模糊匹配。"""
    return key.split(".", 1)[1] if "." in key else key


def _seg_match(a: str, b: str) -> bool:
    """两段文本是否「同一个词」：相等 / 互为子串（长段 ≥5 字符）/ 公共前缀 ≥4。"""
    if not a or not b:
        return False
    if a == b:
        return True
    # 互为子串只认长段（≥5 字符），避免 rise⊂praised 这类短段误匹配
    if len(a) >= 5 and a in b:
        return True
    if len(b) >= 5 and b in a:
        return True
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n >= 4


def _key_covers(field_key: str, fact_key: str) -> bool:
    """判断一个已写入的事实键是否「覆盖」某规范字段。

    LLM 写信息表时常自造键（如 audience.target、basic.personality、advantage.differentiator），
    与规范键（direction.audience、style.personality、direction.differentiation）对不上，
    导致明明答过的题被判「未采」而重复提问。这里拿字段末段同时比对事实键的
    整键与末段（前缀模块名可能正是规范字段名，如 audience.target → audience）。
    """
    fseg = _seg(field_key)
    return _seg_match(fseg, fact_key) or _seg_match(fseg, _seg(fact_key))


def _profile_status(session_id: str) -> dict:
    """按采集表字段粒度回报采集进度（软提示，供 Agent 判断下一个问题）。

    只展开「核心字段」的缺失清单，非核心字段只给计数——防止主 Agent 把
    整张 43 问清单贴给用户（用户投诉：出现大量的重复问题）。
    """
    profile = state.get_profile(session_id)
    covered = set()
    for key, val in profile.items():
        if not key.startswith("__") and val:
            covered.add(key)
            # 事实键（含自然语言键）与规范字段做双向模糊匹配：
            # 已经答过的字段（哪怕写了「无/没有」）一律算覆盖，绝不重问
            for f in FIELDS:
                if _key_covers(f["key"], key):
                    covered.add(f["key"])

    modules = []
    for m in MODULES:
        collected = []
        missing_core = []
        n_missing_noncore = 0
        for f in FIELDS:
            if f["module"] != m["id"]:
                continue
            if f["key"] in covered:
                collected.append(f["key"])
            elif f["core"]:
                missing_core.append({"key": f["key"], "label": f["label"]})
            else:
                n_missing_noncore += 1
        modules.append({
            "module": m["id"], "name": m["name"],
            "collected": collected,
            "missing_core": missing_core,
            "missing_noncore_count": n_missing_noncore,
            "collected_count": len(collected),
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
        "note": "本结果仅供你内部判断下一个问题：一次只挑一个未采的核心字段问；"
                "已采的字段（含用户答过「没有/无」的）绝不重问；"
                "绝不把字段/问题清单整段贴给用户。",
    }


def _pluck_project_ids(node):
    """容错提取项目 ID 列表（主站返回结构未文档化，多种形状都认）。"""
    ids = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("project_id", "id", "pid") and isinstance(v, str) and v:
                ids.append(v)
            ids += _pluck_project_ids(v)
    elif isinstance(node, list):
        for item in node:
            ids += _pluck_project_ids(item)
    out = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out


_CLI_AUTH_ERRORS = {
    "auth_error", "auth_required", "expired_token", "refresh_failed",
    "customer_identity_required", "unauthorized",
}


def _cli_auth_error(resp: dict) -> bool:
    data = (resp or {}).get("data") or {}
    return isinstance(data, dict) and str(data.get("error") or "") in _CLI_AUTH_ERRORS


def _load_ip12_profile(session_id: str, project_id: Optional[str]) -> dict:
    """从黄雀主站拉取 IP 定位档案，返回档案原文供主 Agent 提取写入。

    解决「新会话读不到主站已有 IP 定位」：用户换会话/换设备后，
    说「按我主站的定位做」时由主 Agent 调本工具把主站档案拉回对话。

    双通道：优先老系统（Hermes IP12：ip12-projects/ip12-project）；
    老系统不可用（上游 502/维护）或无项目时自动降级新系统
    （数字化 IP：digital-ip-projects/digital-ip-project/digital-ip-report）。
    """
    err_ip12 = None
    projects_resp = hq_cli.run("ip12-projects", session_id=session_id)
    if _cli_auth_error(projects_resp):
        return _err("当前网页登录身份无法取得专属黄雀 CLI 授权。请重新登录后再试；不会回退公共账号。")
    proj_data = projects_resp.get("data") or {}
    all_ids = []
    if isinstance(proj_data, dict) and not proj_data.get("error"):
        all_ids = _pluck_project_ids(proj_data)
        if not all_ids:
            err_ip12 = "IP12 老系统没有项目"
    else:
        err_ip12 = str(proj_data.get("message") or proj_data.get("error")) if isinstance(proj_data, dict) else "未知错误"

    if all_ids:
        if project_id and project_id not in all_ids:
            # 老系统可用且用户指定的项目不在其中：直接列选项，不降级
            return _err("主站没有找到项目 %s。现有项目：%s —— 请用户确认用哪一个。"
                        % (project_id, "、".join(all_ids[:8])))
        pid = project_id or all_ids[0]  # 最近项目排在最前（主站列表按更新时间倒序）
        one_resp = hq_cli.run("ip12-project", {"project_id": pid}, session_id=session_id)
        one_data = one_resp.get("data") or {}
        if isinstance(one_data, dict) and not one_data.get("error"):
            archive = one_data.get("result") if isinstance(one_data, dict) else one_data
            n_projects = len(all_ids)
            return _ok(
                {"source": "ip12", "project_id": pid, "archive": archive},
                note=("主站 IP12 档案原文（JSON，result 字段）。请从中提取人设事实，"
                      "用 update_profile 逐项写入内部信息表（键尽量用规范字段键，如 basic.name、"
                      "career.current_job、direction.track、style.tone、business.goal），"
                      "然后向用户复述档案要点（两三句），确认无误后再用于后续创作/报告。"
                      "档案缺的字段不要脑补，需要的继续访谈补齐。"
                      + ("主站共有 %d 个项目：%s；本次取的是 %s。" % (n_projects, "、".join(all_ids[:8]), pid)
                         if n_projects > 1 else "本次取的是主站唯一项目 %s。" % pid)),
            )

    # ---- 降级：新系统（数字化 IP）----
    dig_resp = hq_cli.run("digital-ip-projects", session_id=session_id)
    if _cli_auth_error(dig_resp):
        return _err("当前网页登录身份无法取得专属黄雀 CLI 授权。请重新登录后再试；不会回退公共账号。")
    dig_data = dig_resp.get("data") or {}
    if not isinstance(dig_data, dict) or dig_data.get("error"):
        if err_ip12:
            msg = ("主站档案接口两边都不可用：IP12（%s）；数字化 IP（%s）。"
                   % (err_ip12, str(dig_data.get("message") or dig_data.get("error"))))
        else:
            msg = "主站档案接口两边都没有找到可用数据。"
        return _err(msg + " 稍后再试，或先按对话继续采集，不编造档案内容。")
    dig_result = dig_data.get("result") if isinstance(dig_data, dict) else {}
    dig_items = dig_result.get("items") if isinstance(dig_result, dict) else None
    if not isinstance(dig_items, list) or not dig_items:
        return _err("您的主站账号下还没有 IP 定位项目档案。"
                    "可以就在这里重新做一份定位（我一步步问您），或先去主站创建项目。")
    dig_ids = [str(i.get("id")) for i in dig_items if isinstance(i, dict) and i.get("id")]
    dig_titles = {str(i.get("id")): (i.get("title") or "未命名")
                  for i in dig_items if isinstance(i, dict) and i.get("id")}
    dig_reports = {str(i.get("id")): bool((i.get("foundation_stage") or {}).get("report_id"))
                   for i in dig_items if isinstance(i, dict) and i.get("id")}
    if project_id:
        if project_id not in dig_ids:
            return _err("主站没有找到项目 %s。现有项目：%s —— 请用户确认用哪一个。"
                        % (project_id, "、".join((dig_ids + all_ids)[:8])))
        pid = project_id
    else:
        pid = dig_ids[0]

    one_resp = hq_cli.run("digital-ip-project", {"project_id": pid}, session_id=session_id)
    one_data = one_resp.get("data") or {}
    if not isinstance(one_data, dict) or one_data.get("error"):
        return _err("读取项目 %s 失败：%s。稍后再试。"
                    % (pid, one_data.get("message") or one_data.get("error")))
    proj = one_data.get("result") if isinstance(one_data, dict) else {}
    archive = {"project": proj}

    # 有已存报告时一并拉回（报告里是人设定位的成稿）
    try:
        found_stage = (proj.get("project") or {}).get("foundation_stage") or {}
        if found_stage.get("report_id"):
            rep_resp = hq_cli.run("digital-ip-report", {"project_id": pid}, session_id=session_id)
            rep_data = rep_resp.get("data") or {}
            if isinstance(rep_data, dict) and not rep_data.get("error"):
                archive["report"] = rep_data.get("result") if isinstance(rep_data, dict) else {}
    except Exception:
        pass  # 报告拉取失败不阻塞：项目资料本身已可用

    n_projects = len(dig_ids)
    proj_list = "、".join(
        "%s%s" % (dig_titles.get(i, i), "（有已存报告）" if dig_reports.get(i) else "")
        for i in dig_ids[:8])
    src_note = ("（主站 IP12 老系统暂时不可用：%s，本次从新系统「数字化 IP」拉的档案）" % err_ip12) if err_ip12 else ""
    return _ok(
        {"source": "digital-ip", "project_id": pid, "archive": archive},
        note=("主站 IP 定位档案原文（JSON，result 字段；project 是项目资料（问卷答案/对话/进度），"
              "report 是已存报告成稿）。请从中提取人设事实，用 update_profile 逐项写入内部信息表"
              "（键尽量用规范字段键，如 basic.name、career.current_job、direction.track、style.tone、"
              "business.goal），然后向用户复述档案要点（两三句），确认无误后再用于后续创作/报告。"
              "档案缺的字段不要脑补，需要的继续访谈补齐。" + src_note
              + ("主站共有 %d 个项目：%s。本次自动取的是最近更新的「%s」；"
                 "如果用户说的定位像是另一个项目（比如另一个有已存报告的），"
                 "先向用户确认要用哪个，再带 project_id 重新拉。"
                 % (n_projects, proj_list, dig_titles.get(pid, pid))
                 if n_projects > 1 else "本次取的是主站唯一项目「%s」。" % dig_titles.get(pid, pid))),
    )


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

    if name == "load_ip12_profile":
        return _load_ip12_profile(session_id, args.get("project_id"))

    # 报告生成（严格按样例模板，内容全部由 LLM 产出）
    if name == "generate_report":
        chosen = str(args.get("chosen") or "").strip().upper()
        if chosen not in ("A", "B", "C"):
            chosen = None
        return report.generate_draft(session_id, (args.get("instruction") or "").strip(),
                                     chosen)

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
        for key in ("_json", "_m5_json", "_m6_json", "_pending_review"):
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
            if meta.get("status") == "final":
                note = ("当前报告状态：已定稿（final）。最终推荐=用户所选方案"
                        f"{meta.get('chosen') or ''}《{meta.get('chosen_title') or ''}》——"
                        "只提这一个方案，不要再把三套方案摆出来让用户重新选，"
                        "也不要重提草稿阶段的推荐款。")
            else:
                note = ("当前报告状态。三套人设方案已在 options 里，可直接列给用户选；"
                        "若 status 还不是 draft_ready/final，故事与细节仍在最终核对中，"
                        "先只展示方案选项，等状态就绪再转述细节。")
        else:
            note = ("当前报告状态（还没有三套方案内容）。报告生成中或尚未启动："
                    "告诉用户正在生成、进度实时可见，稍后再查 get_report 就能拿到三套方案列给用户，"
                    "不要甩一句「去开 PDF」让用户自己翻。")
        return _ok(meta, note=note)

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
        for key in ("_json", "_m5_json", "_m6_json", "_pending_review"):
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
        return _fmt_hq(hq_cli.status(session_id=session_id))

    if name == "hq_ip12_projects":
        return _fmt_hq(hq_cli.run("ip12-projects", session_id=session_id))

    if name == "hq_ip12_create":
        title = (args.get("title") or "").strip()
        if not title:
            return _err("缺少参数 title")
        return _fmt_hq(hq_cli.run(
            "ip12-create", {"title": title}, confirm=True, session_id=session_id,
        ))

    if name == "hq_ip12_project":
        pid = (args.get("project_id") or "").strip()
        if not pid:
            return _err("缺少参数 project_id")
        return _fmt_hq(hq_cli.run(
            "ip12-project", {"project_id": pid}, session_id=session_id,
        ))

    if name == "hq_ip12_report":
        pid = (args.get("project_id") or "").strip()
        if not pid:
            return _err("缺少参数 project_id")
        return _fmt_hq(hq_cli.run(
            "ip12-report", {"project_id": pid}, session_id=session_id,
        ))

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
                session_id=session_id,
            )
        )

    return _err(f"未知工具：{name}")


def tool_schema_by_name(name: str):
    for t in TOOLS:
        if t["function"]["name"] == name:
            return t
    return None
