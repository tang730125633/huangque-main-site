"""子 Agent 运行时：每个业务域一个独立 LLM 实例。

- system prompt = 角色（AGENTS.md）+ 业务 SKILL 全文 + use-huangque-cli 全文
  + SpecialistResult 协议 + 参数纪律（不追问/不编造/用默认）；
- 工具集 = hq_status / hq_capabilities / hq_describe / hq_run / finish；
- 运行时强制「先报价后确认」：付费能力的 confirm 必须匹配最近一次同参数报价；
- 每轮以 finish(SpecialistResult) 收尾，只把摘要回流主 Agent。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit

from .. import config, hq_cli
from . import livecaps, observability, protocol, skills, state

log = logging.getLogger("hq.v4.subagent")

MAX_STEPS = 14
_APPROVAL_SCOPE = threading.local()
_TURN_SCOPE = threading.local()
_JOB_IDS = ("job_id", "task_id", "run_id", "request_id")
_TASK_TERMINALS = {"ready": protocol.COMPLETED, "completed": protocol.COMPLETED,
                   "succeeded": protocol.COMPLETED, "done": protocol.COMPLETED,
                   "error": protocol.FAILED, "failed": protocol.FAILED,
                   "cancelled": protocol.CANCELLED, "canceled": protocol.CANCELLED}


def quote_summary(quote):
    """Payment-card description comes from frozen runtime data, never model prose."""
    cap = str(quote.get("capability") or "当前操作")
    inputs = quote.get("inputs") or {}
    parts = [cap]
    for key, label in (("platform", "平台"), ("keyword", "关键词"), ("page", "页码"),
                       ("title", "标题"), ("text", "文案")):
        value = inputs.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            parts.append(label + "：" + str(value)[:120])
    if isinstance(inputs.get("url"), str):
        try:
            url = urlsplit(inputs["url"])
            if url.hostname:
                parts.append("素材：" + (url.hostname + url.path)[:160])
        except ValueError:
            pass
    parts.append("报价 " + approval_id(quote)[:8])
    if quote.get("cost") is not None:
        parts.append("本次 " + str(quote["cost"]) + " 点")
    return "；".join(parts) + "。请确认当前操作。"


def _job_records(last):
    records = last.get("_runtime_jobs") or []
    if not records and last.get("state") == protocol.RUNNING:
        receipt = {k: last.get("result", {}).get(k) for k in _JOB_IDS
                   if last.get("result", {}).get(k) is not None}
        if receipt:  # compatible with already-persisted pre-upgrade receipts
            records = [{"state": protocol.RUNNING, "result": receipt}]
    return [dict(r, result=dict(r["result"])) for r in records]


def _bound_outcome(sess, candidate):
    res = dict(candidate)
    res.pop("_runtime_jobs", None)  # model output never owns the receipt ledger
    jobs = _job_records(sess.get("last_result") or {})
    pending = sess.get("pending_quote") or {}
    if res.get("state") == protocol.NEEDS_APPROVAL and pending:
        res["quote"] = dict(pending)
        res["summary"] = quote_summary(pending)
    else:
        active = [j for j in jobs if j["state"] == protocol.RUNNING]
        observed = getattr(_TURN_SCOPE, "observed", None) or set()
        current = [j for j in jobs if _job_key(j["result"]) in observed]
        # A model finish, timeout, or exception is not a business-task status.
        truth = active[-1] if active else (current[-1] if current else None)
        if truth and res.get("state") != protocol.NEEDS_USER_INPUT:
            status = truth["state"]
            label = {protocol.RUNNING: "已提交，等待原任务状态更新",
                     protocol.COMPLETED: "查询确认已完成", protocol.FAILED: "查询确认失败",
                     protocol.CANCELLED: "查询确认已取消"}[status]
            error = res.get("error_code")
            result = dict(truth.get("output") or {})
            result.update(truth["result"])
            res = protocol.make(status, "任务 " + _job_label(result) + "：" + label + "。", result=result)
            if error:
                res["continuation_error"] = error
                res["summary"] += "本轮回复处理中断，已保留原任务编号，不会重新提交。"
    if jobs:
        res["_runtime_jobs"] = jobs
        res["result"] = dict(res.get("result") or {})
        res["result"]["submitted_jobs"] = [dict(j["result"], observed_state=j["state"]) for j in jobs]
    return res


def _job_key(receipt):
    # A later task/delivery response may omit the original request/run ID.
    return next(((k, str(receipt[k])) for k in _JOB_IDS if receipt.get(k) is not None), ())


def _job_label(receipt):
    return "/".join(str(receipt[k]) for k in _JOB_IDS if receipt.get(k) is not None)


def _save_outcome(sid, domain, res, messages=None):
    def update(sess):
        sess["last_result"] = _bound_outcome(sess, res)
        if messages is not None:
            sess["messages"] = list(messages)
    return state.update_subagent(sid, domain, update)["last_result"]


def _observe_job(sid, domain, receipt, status=protocol.RUNNING, *, submit=False, output=None):
    def update(sess):
        last = sess.get("last_result") or {}
        jobs = _job_records(last)
        key = _job_key(receipt)
        previous = next((j for j in jobs if _job_key(j["result"]) == key), None)
        if previous and previous["state"] != protocol.RUNNING and status == protocol.RUNNING:
            return  # an older queued/running response cannot undo an observed terminal
        actual_receipt = dict(previous["result"] if previous else {})
        actual_receipt.update(receipt)
        jobs = [j for j in jobs if _job_key(j["result"]) != key]
        jobs.append({"state": status, "result": actual_receipt, "output": dict(output or {})})
        if submit:
            sess["pending_quote"] = {}
        actual = dict(output or {})
        actual.update(actual_receipt)
        res = protocol.make(status, "任务 " + _job_label(receipt) + " 状态已更新。", result=actual)
        res["_runtime_jobs"] = jobs
        sess["last_result"] = res
        if not submit and sess.get("pending_quote"):
            # Polling an earlier task must not overwrite a newer approval card.
            res = protocol.make(protocol.NEEDS_APPROVAL, "当前报价等待确认。")
        sess["last_result"] = _bound_outcome(sess, res)
    observed = getattr(_TURN_SCOPE, "observed", None)
    if observed is not None:
        observed.add(_job_key(receipt))
    state.update_subagent(sid, domain, update)


def _observe_task_query(sid, inputs, payload):
    job_id = inputs.get("job_id")
    if job_id is None or (payload.get("job_id") is not None and str(payload["job_id"]) != str(job_id)):
        return
    status = str(payload.get("phase") or payload.get("status") or "").lower()
    if not status:
        return
    for owner in state.all_domains(sid):
        last = (state.get_subagent(sid, owner) or {}).get("last_result") or {}
        for job in _job_records(last):
            if str(job["result"].get("job_id")) != str(job_id):
                continue
            receipt = dict(job["result"], status=status)
            _observe_job(sid, owner, receipt, _TASK_TERMINALS.get(status, protocol.RUNNING), output=payload)

try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except Exception:  # pragma: no cover
    _OPENAI_AVAILABLE = False

try:
    import httpx
    _HTTPX_AVAILABLE = True
except Exception:  # pragma: no cover
    _HTTPX_AVAILABLE = False

# ---------------------------------------------------------------------------
# System prompt 组装
# ---------------------------------------------------------------------------

_TASK_RULES = """## 本轮任务（来自主 Agent）

你是独立干活的专家：主 Agent 只把用户需求交给你，参数全部由你自己填。
1. 开工顺序：先 hq_status 确认登录 → hq_capabilities 实时发现能力 → 对要用到的能力逐一 hq_describe 读契约（参数/约束/费用）。
2. 参数自己填：用户没给的参数用本域 skill 的默认值；可选参数不追问，直接用默认。
3. 不编造：严禁编造用户没提供的事实、素材、链接、id；确实缺影响结果的必填参数才 needs_user_input。
4. 付费两段式：先不带 confirm 跑一次拿报价 → needs_approval 把 cost/points 报给用户 → 用户明确同意后才用完全相同的 inputs + confirm=true 提交恰好一次（quote_token 由运行时自动附上，不要自己抄写传递）。
5. 响应不确定绝不重复提交；只按原 job_id/request_id/run_id 查询或恢复。
6. 每轮最后必须调用 finish(...) 把结果摘要交给主 Agent，除 finish 外不要输出闲聊文本（文本不会送达用户）。
7. 交互卡片：你查询形象（video-avatars）或音色（voices / text-video-voices / audio-slots）时，页面会自动渲染成缩略图/试听卡片给用户点选，不用你手动处理；需要用户做选择的其他内容（如三版文案）用 attach_widgets 注册 script_pick 卡片；用 option_pick 列形象等带预览图的选项时，每项必须给 image_url（形象图完整地址，hq 返回的 image_url 是相对路径时拼 https://huangquechuanmei.com 前缀）。用户点选后会以「【点选】<卡片标题>：<选项>」形式回来，把它当作用户的选择继续。**素材卡纪律**：只有用户明确要出片（或模块六文案已确认要出成片）时才查询形象/音色——查询即渲染卡片，平时不要为了展示而查询；只推真人形象（插画/原画/大师/patreon 类运行时自动过滤，不要推荐）；用户关掉卡片后不要重复查询注册同一批素材。**文案未确认绝不出片**：digital-ip-text-generate / digital-ip-batch-generate / text-video-generate 等任何生成调用前，必须已有一条用户确认的文案；用户没确认就先 attach_widgets 注册三版 script_pick 让人点选，并以 finish(needs_user_input) 收尾问「文案用哪一版」，绝不允许带着未确认文案直接提交生成。默认形象/音色（本人形象、本人声音/克隆音色）由前端自动勾选，你不需要在文字里指定。
8. 供应商超时退款 ≠ 最终失败：task 查到 error + refunded 且错误含「超时」（如 HeyGen 超时）时，按工具返回里的 note 口径向用户报告——已全额退款（净扣 0）、成片可能稍后回主站原任务变 ready（站内可下载，CLI 读不到补回的 URL）；**不要自动重试同参数**（重开=重新扣点），是否重开由用户决定；不要只说「失败、无成片」。
"""

_WIDGET_CAP_ITEMS = {
    "video-avatars": ("avatar_pick", "数字人形象（点选使用）"),
    "voices": ("voice_pick", "音色（点选使用，▶ 可试听）"),
    "text-video-voices": ("voice_pick", "文案成片音色（点选使用，▶ 可试听）"),
    "audio-slots": ("voice_pick", "声音克隆槽位（点选使用，▶ 可试听）"),
}


def _register_widgets_from_result(sid: str, cap: str, payload: dict):
    """把查询类能力的返回自动注册成交互卡片（前端渲染成可点选组件）。"""
    if cap not in _WIDGET_CAP_ITEMS or not isinstance(payload, dict):
        return
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return
    wtype, title = _WIDGET_CAP_ITEMS[cap]
    cards = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if wtype == "avatar_pick":
            url = it.get("image_url") or ""
            if url.startswith("/"):
                url = config.HQ_SITE_BASE + url
            if not url:
                continue
            raw = it.get("name") or ""
            # 插画/原画不是真人数字人素材（Wlop 大师/patreon/原画类）：过滤掉，别混进「我的形象」
            if re.search(r"大师|patreon|原画|插画", raw, re.I):
                continue
            # 编号名（「形象 19」）是黄雀库存编号不是给人看的名字：前端对编号型只显示脸不写字，
            # 编号保留在 raw_label 供前端识别；有名字的形象（如「本人形象」）原样展示。
            if re.match(r"^(形象|avatar|voice)\s*\d+$", raw.strip(), re.I):
                name, raw_label = "我的形象", raw.strip()
            else:
                name, raw_label = raw or f"形象 {it.get('id')}", ""
            cards.append({
                "id": str(it.get("id", "")), "name": name,
                "image_url": url, "status": it.get("status", ""),
                "raw_label": raw_label,
                "created_at": it.get("created_at") or "",
            })
        else:  # voice_pick
            name = it.get("display_name") or it.get("name") or it.get("title")
            raw_label = ""
            if not name and cap == "audio-slots":
                # 声音克隆槽位没有名字字段（只有 id + 试听 + created_at）：
                # 叫「我的克隆音色」，用创建日期区分，严禁编造音色名
                name = "我的克隆音色"
            elif name and re.match(r"^(音色|槽位|voice)\s*\d+$", name.strip(), re.I):
                # 编号名（「音色 17」）不是给人看的：主名降级为「我的克隆音色/我的音色」，
                # 编号彻底丢弃（前端用创建日期区分同名项）
                name = "我的克隆音色" if cap == "audio-slots" else "我的音色"
            if not name:
                name = f"音色 {it.get('id')}"
            cards.append({
                "id": str(it.get("id", "")),
                "name": name,
                "preview_url": it.get("preview_url") or "",
                "kind": "clone" if cap == "audio-slots" else "voice",
                "scope": it.get("scope", ""),
                "raw_label": raw_label,
                "created_at": it.get("created_at") or "",
            })
    if not cards:
        return
    state.add_widgets(sid, [{
        "type": wtype, "id": f"{wtype}:{cap}",
        "title": title,
        "hint": "点击卡片即可选中，无需打字。",
        "items": cards[:24],
    }])

_SPECIALIST_PROTOCOL = """## SpecialistResult 六态（finish 的参数）

- completed：任务完成。summary 写成了什么（中文一句话），result 带成果（资产 id/URL/内容/文件路径等）。
- running：已提交异步任务。summary 写已提交什么，result 带 job_id（或 request_id/run_id），主 Agent 之后会轮询。
- needs_user_input：缺必填参数。question 用中文向用户提问（一次最多问 3 个小问题，通常 1 个），missing_inputs 列缺的字段名。
- needs_approval：已取得报价。summary 报价格，quote 带 {quote_token, cost, points, capability, expires_in}，等用户明确同意；用户拒绝时下一轮收到拒绝后 finish(cancelled)。
- failed：失败。summary 写原因，error_code 短码，retryable 表示是否值得重试；按本域容错策略重试过仍失败再上报。供应商超时退款（task 查到 error+refunded、错误含「超时」）时，summary 必须按工具 note 口径说明已全额退款、成片可能稍后回主站变 ready，不要只报「失败、无成片」。
- cancelled：用户取消，summary 简述。

每轮只报一个状态。参数细节（工具名、完整 inputs）不要写进 summary；只写「成了什么 / 没成什么 / 要不要重试」。

图片类成果（采集的图片等）：把图片原始 URL 列表放进 result.images（字符串数组）——运行时会自动下载到本地并直接贴给用户（本地图片链接不过期、不受防盗链影响），summary 里不要贴链接、更不要让用户自己点外链。
"""


def build_system_prompt(domain: str) -> str:
    role = skills.load_agents_md(domain) or (
        f"你是黄雀主站子 Agent（id：{skills.DOMAINS[domain]}）。"
        "主 Agent 会把一类「产物需求」交给你，你负责把它变成黄雀主站上的对应业务结果。"
    )
    meta, business_body = skills.load_business_skill(domain)
    cli_body = skills.load_use_huangque_cli(domain)
    if not cli_body:
        cli_body = (
            "## CLI 安全用法（底座缺失时的兜底）\n"
            "- `hq capabilities --json` / `hq describe <id> --json` 是唯一权威契约。\n"
            "- 付费先报价（不带 --confirm）→ 报点数等用户同意 → 相同输入 + --confirm --quote-token 恰好一次。\n"
            "- 响应不确定只查原 job_id/request_id；update/delete 先读最新 revision。\n"
        )
    return "\n\n".join([
        role.strip(),
        _TASK_RULES.strip(),
        f"## 本域业务规则（技能：{meta.get('name', domain)}）\n{business_body.strip()}",
        cli_body.strip(),
        _SPECIALIST_PROTOCOL.strip(),
    ])


# ---------------------------------------------------------------------------
# 工具 schema（子 Agent 可见）
# ---------------------------------------------------------------------------

_SUBAGENT_TOOLS_CACHE = None


def subagent_tools() -> list[dict]:
    def fn(name, desc, props, required):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        }

    global _SUBAGENT_TOOLS_CACHE
    if _SUBAGENT_TOOLS_CACHE is not None:
        return _SUBAGENT_TOOLS_CACHE

    tools = [
        fn(
            "hq_status",
            "检查黄雀 CLI 登录状态与当前账号（点数/会员/授权范围）。任何账号相关操作前先调用。",
            {}, [],
        ),
        fn(
            "hq_capabilities",
            "实时能力目录（hq capabilities 的唯一权威投影）。query 可按关键词过滤（如 image、director）。"
            "列出 id/中文名/一句话描述/扣费种类；确定要用某个能力后先 hq_describe 读完整契约。",
            {"query": {"type": "string", "description": "可选：按关键词过滤能力目录"}}, [],
        ),
        fn(
            "hq_describe",
            "读取一个能力的完整契约：参数 schema、约束、费用、工作流与恢复策略。"
            "运行任何能力前必须先读契约，绝不凭记忆猜参数。",
            {"capability_id": {"type": "string", "description": "能力 id"}},
            ["capability_id"],
        ),
        fn(
            "hq_run",
            "执行一个 hq 能力。付费能力两段式：confirm=false 先取得报价（返回 quote），"
            "用户同意后必须用完全相同的 inputs 且 confirm=true 提交恰好一次（quote_token 由运行时自动附上，不要自己抄写）。"
            "免费写入/外部 AI 也要先向用户说明再 confirm。"
            "file_path 用于：① 上传类能力（hq_describe 契约有 file_input 字段）传本地文件绝对路径；"
            "② input_schema 里有声明「data URL」的 string 字段（如 image_data）时，同样传本地图片路径"
            "（CLI 会把文件转成 data URL 发服务端，不要自己写 base64）。其他能力不要传 file_path。"
            "超过 600KB 的图片运行时会自动等比压缩，无需你处理。"
            "若返回 ok=false，先读 error 字段里的 CLI 原始错误（message）——按它调整参数重试，不要原样重发。",
            {
                "capability_id": {"type": "string"},
                "inputs": {"type": "object", "description": "按 hq_describe 的 input_schema 填的参数（不含未声明字段）"},
                "confirm": {"type": "boolean", "description": "是否确认提交；默认 false"},
                "quote_token": {"type": "string", "description": "不需要传，留空即可：确认时运行时自动附上最近一次同参数报价的 quote_token（长 token 不要自己抄写）"},
                "expected_cost": {"type": "string", "description": "仅当该能力报价说明（hq_describe 的 cost.confirmation）含 expected-cost 时才传，其他能力不要传"},
                "file_path": {"type": "string", "description": "可选：本地文件绝对路径（上传类能力，或 data URL 字段由运行时代转）"},
                "output": {"type": "string", "description": "可选：落盘类能力（如 dl 成品下载）的绝对输出路径。"
                    "路径自动生成：/home/ubuntu/hq-ip-agent/data/downloads/<任务号>-<时间戳>.<扩展名>，"
                    "绝不要向用户索要保存路径。"},
            },
            ["capability_id"],
        ),
        fn(
            "attach_widgets",
            "注册给用户点选的交互卡片（页面渲染成可点击组件，不要只是写 Markdown 文字让用户打字）。"
            "type 只支持 script_pick（文案多版让用户点选）或 option_pick（通用选项）。"
            "script_pick 的 items 每项 {id, title, summary, body}（body 为全文）；"
            "option_pick 若选项有预览图（如数字人形象），每项必须给 image_url（图片完整地址），页面会渲染成缩略图。"
            "用户点选后以「【点选】<卡片标题>：<选项title>」消息回来，把 id 作为该选择继续。",
            {
                "widgets": {
                    "type": "array",
                    "description": "卡片数组",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["script_pick", "option_pick"]},
                            "id": {"type": "string", "description": "卡片唯一 id（同 id 覆盖旧卡片）"},
                            "title": {"type": "string"},
                            "hint": {"type": "string"},
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "title": {"type": "string"},
                                        "summary": {"type": "string"},
                                        "body": {"type": "string"},
                                        "image_url": {"type": "string", "description": "选项预览图完整地址（如形象图），无则省略"},
                                    },
                                },
                            },
                        },
                    },
                },
            },
            ["widgets"],
        ),
        fn(
            "finish",
            "结束本轮：把结果摘要按 SpecialistResult 六态交给主 Agent。每轮必须调用且只调用一次。",
            {
                "state": {"type": "string", "enum": list(protocol.STATES)},
                "summary": {"type": "string", "description": "中文摘要：成了什么/没成什么/要不要重试"},
                "result": {"type": "object", "description": "成果凭据（job_id/asset id/url/文件路径等），没有则 {}"},
                "question": {"type": "string", "description": "needs_user_input 时：向用户提的问题（中文）"},
                "missing_inputs": {"type": "array", "items": {"type": "string"}},
                "quote": {"type": "object", "description": "needs_approval 时：{quote_token, cost, points, capability, expires_in}"},
                "error_code": {"type": "string"},
                "retryable": {"type": "boolean"},
            },
            ["state", "summary"],
        ),
    ]

    _SUBAGENT_TOOLS_CACHE = tools
    return tools


# ---------------------------------------------------------------------------
# 工具分发（带会话上下文：报价门禁）
# ---------------------------------------------------------------------------

def _inputs_hash(capability: str, inputs: dict) -> str:
    raw = json.dumps({"capability": capability, "inputs": inputs},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _compact(payload, limit: int = 4000) -> str:
    try:
        s = json.dumps(payload, ensure_ascii=False)
    except Exception:
        s = str(payload)
    return s[:limit]


def dispatch_tool(name: str, args: dict, sid: str, domain: str) -> dict:
    """子 Agent 工具分发。返回将被 JSON 序列化喂回模型的 dict。"""
    args = args or {}
    sess = state.get_subagent(sid, domain) or {"pending_quote": None}

    if name == "hq_status":
        resp = hq_cli.status()
        ok = resp.get("exit_code") == 0
        data = resp.get("data") or {}
        if not ok and data.get("error") == "auth_error":
            # 授权过期/未登录：给主 Agent 明确的下一步，不要让它盲目重试
            return {
                "ok": False,
                "error": "auth_error",
                "hint": ("黄雀 CLI 授权已过期或未登录。不要重试本任务：请用户先在服务器执行 "
                         "`hq login --json` 并打开返回的 verification_uri 完成设备授权（10 分钟内），"
                         "授权完成后重新发消息即可继续。"),
            }
        payload = (data.get("result") if isinstance(data, dict) else data) or {}
        return {"ok": ok, "result": payload, "error": None if ok else "hq 登录状态异常"}

    if name == "hq_capabilities":
        try:
            items = livecaps.compact_directory(args.get("query"))
            return {"ok": True, "count": len(items), "capabilities": items}
        except Exception as e:  # pragma: no cover
            return {"ok": False, "error": f"能力目录读取失败：{e}"}

    if name == "hq_describe":
        cap = (args.get("capability_id") or "").strip()
        if not cap:
            return {"ok": False, "error": "缺少 capability_id"}
        info = livecaps.describe(cap)
        if "error" in info:
            return {"ok": False, "error": info["error"]}
        return {"ok": True, **info}

    if name == "hq_run":
        return _hq_run(args, sid, domain, sess)

    if name == "attach_widgets":
        widgets = args.get("widgets") or []
        clean = []
        for w in widgets:
            if not isinstance(w, dict) or w.get("type") not in ("script_pick", "option_pick"):
                continue
            w = dict(w)
            w["items"] = [
                dict(i) for i in (w.get("items") or [])
                if isinstance(i, dict) and (i.get("id") or i.get("title"))
            ]
            clean.append(w)
        if clean:
            state.add_widgets(sid, clean)
        return {"ok": True, "note": f"已注册 {len(clean)} 张交互卡片（用户点选后会以【点选】消息回来）"}

    if name == "finish":
        return _finish(args, sid, domain)

    return {"ok": False, "error": f"未知工具：{name}"}


_MAX_UPLOAD_BYTES = 600 * 1024  # 实测服务端 API 对 base64 后 ~900KB 起返回 502，压到 600KB 留余量


def _shrink_image(path: str) -> str | None:
    """图片超过 _MAX_UPLOAD_BYTES 时等比缩放+降质压缩，返回新临时文件路径；
    无需压缩或处理失败返回 None（原路径可用）。"""
    try:
        if os.path.getsize(path) <= _MAX_UPLOAD_BYTES:
            return None
    except OSError:
        return None
    try:
        from PIL import Image  # 延迟导入：无 PIL 时降级为不压缩
    except ImportError:
        return None
    try:
        img = Image.open(path)
        img.load()
    except Exception:
        return None
    fmt = (img.format or "JPEG").upper()
    if fmt in ("GIF", "WEBP"):
        return None  # 动图/动图格式不碰，避免转坏
    out_fmt = "PNG" if fmt == "PNG" else "JPEG"
    # 等比缩放：最长边压到 1400px 以内（人脸识别足够，同时控制体积）
    max_side = 1400
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize(
            (max(1, int(img.size[0] * ratio)), max(1, int(img.size[1] * ratio))),
            Image.LANCZOS,
        )
    if out_fmt == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    fd, tmp_name = tempfile.mkstemp(suffix=f".{out_fmt.lower()}", prefix="hq_shrink_")
    os.close(fd)
    try:
        quality = 88
        img.save(tmp_name, out_fmt, quality=quality, optimize=True)
        while os.path.getsize(tmp_name) > _MAX_UPLOAD_BYTES and quality > 55:
            quality -= 10
            img.save(tmp_name, out_fmt, quality=quality, optimize=True)
        if os.path.getsize(tmp_name) > _MAX_UPLOAD_BYTES:
            os.unlink(tmp_name)
            return None
        return tmp_name
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        return None


def _adapt_file_input(cap: str, inputs: dict, file_path: str | None):
    """把本地文件适配成能力契约要求的形式，返回 (inputs, 新file_path)。

    - 能力契约有 file_input（上传类，--file 流式上传）→ 走 --file；
    - 没有 file_input 但 input_schema 有声明「data URL」的 string 字段
      （如 video-avatar-create 的 image_data）→ CLI 同样接受 --file（实测
      CLI 内部会把文件转成 data URL 发给服务端），也走 --file；
      不自己转 base64 注入 inputs——hq CLI 的 --input JSON 有 64KB 上限。
    - 大图先压到安全大小（服务端 API 对大 payload 返回 502，边界约 900KB）。
    """
    if not file_path:
        return inputs, None
    info = livecaps.describe(cap)
    has_file_input = bool(info.get("file_input"))
    schema = (info.get("input_schema") or {}).get("properties") or {}
    has_data_url_field = any(
        v.get("type") == "string" and "data url" in str(v.get("description", "")).lower()
        for v in schema.values()
    )
    if not has_file_input and not has_data_url_field:
        return inputs, file_path  # 该能力不收文件：原样交给 CLI，错误会透传给 LLM
    shrunk = _shrink_image(file_path)
    return inputs, shrunk or file_path


def _hq_run(args: dict, sid: str, domain: str, sess: dict) -> dict:
    cap = (args.get("capability_id") or "").strip()
    inputs = args.get("inputs") or {}
    confirm = bool(args.get("confirm"))
    quote_token = (args.get("quote_token") or "").strip() or None
    expected_cost = args.get("expected_cost")
    file_path = (args.get("file_path") or "").strip() or None
    output = (args.get("output") or "").strip() or None
    if not cap:
        return {"ok": False, "error": "缺少 capability_id"}

    # 落盘类能力（dl）：输出路径白名单——只允许写到系统下载目录内，
    # 目录不存在自动创建；防 LLM 乱指路径写坏别处文件。
    if output:
        downloads_root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data", "downloads",
        )
        real_root = os.path.realpath(downloads_root)
        target = os.path.realpath(output)
        if target != os.path.normpath(output) or not target.startswith(real_root + os.sep):
            return {"ok": False, "error": (
                "output 必须位于系统下载目录 %s 内（如 %s/task-7622-<时间戳>.mp4）" % (downloads_root, downloads_root)
            )}
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
        except OSError as err:
            return {"ok": False, "error": "无法创建下载目录：%s" % err}

    # 本地文件适配：上传类走 --file；data URL 字段能力 CLI 也接受 --file；大图先压缩
    orig_file_path = file_path
    inputs, file_path = _adapt_file_input(cap, inputs, file_path)
    try:
        return _hq_run_with_file(cap, inputs, confirm, quote_token, expected_cost,
                                 file_path, output, sess, sid, domain)
    finally:
        # 压缩生成的临时文件用完即删（原上传文件不动）
        if file_path and file_path != orig_file_path and os.path.exists(file_path):
            try:
                os.unlink(file_path)
            except OSError:
                pass


def _hq_run_with_file(cap: str, inputs: dict, confirm: bool, quote_token,
                      expected_cost, file_path, output, sess: dict, sid: str, domain: str) -> dict:
    # ---- 先报价后确认门禁（运行时强制）----
    kind = livecaps.cost_kind(cap)
    pending = sess.get("pending_quote")
    accepted = getattr(_APPROVAL_SCOPE, "quote", None)
    if accepted and cap == accepted.get("capability"):
        if not confirm or _inputs_hash(cap, inputs) != accepted.get("inputs_hash"):
            return {"ok": False, "error": "当前正在确认原报价，只能使用原输入 confirm=true 提交；不得重新报价或搜索。"}
    if confirm and kind in ("server_quote", "native_quote"):
        h = _inputs_hash(cap, inputs)
        if not pending or pending.get("capability") != cap or pending.get("inputs_hash") != h:
            return {
                "ok": False,
                "error": (
                    "未报价先确认被运行时拒绝：请先用完全相同 inputs 且 confirm=false 运行一次"
                    "取得报价（quote_token），把 cost/points 报给用户并得到明确同意后，"
                    "再用相同 inputs + confirm=true 提交恰好一次（quote_token 由运行时自动附上，不要自己写）。"
                ),
            }
        # token 一律以运行时保存的最近报价为准：LLM 转述 284 字符长 token 常抄错，
        # 传入值不参与比较也不使用（capability + inputs hash 已由上面门禁校验过）。
        quote_token = pending.get("quote_token")
        # 只有能力契约明确要求 --expected-cost（如文件上传类 director-breakdown-upload）才自动补
        if expected_cost is None and pending.get("cost") is not None \
                and "expected-cost" in (livecaps.confirmation(cap) or ""):
            expected_cost = pending.get("cost")

    resp = hq_cli.run(
        cap, inputs, confirm=confirm, quote_token=quote_token,
        expected_cost=expected_cost, file_path=file_path, output=output,
    )
    exit_code = resp.get("exit_code")
    data = resp.get("data") or {}
    # hq CLI 的 hq.error/v1 错误包在 JSON 里（data.error + data.message），
    # 且 0.15.x 对 usage_error 类错误进程退出码仍为 0——必须同时看 data.error，
    # 否则错误被吞、LLM 收到「成功但结果为空」的假信号，只能盲目重试到超时。
    data_error = data.get("error") if isinstance(data, dict) else None
    payload = data.get("result") if isinstance(data, dict) else data
    if not isinstance(payload, dict):
        payload = {"raw": payload}
    ok = exit_code == 0 and not data_error

    out = {
        "ok": ok,
        "exit_code": exit_code,
        "capability": cap,
        "confirm": confirm,
        "result": _compact(payload),
        "next_actions": (data.get("next_actions") if isinstance(data, dict) else None) or [],
        # 错误全文透传给 LLM：message 是 CLI 给出的可操作原因（如
        # "upload capabilities do not accept --input"），必须让模型看到才能自修复。
        "error": None if ok else (
            (data.get("message") if isinstance(data, dict) else None)
            or data_error
            or payload.get("error") or payload.get("message")
            or (resp.get("stderr") or "CLI 调用失败")
        ),
    }

    # 供应商超时退款 ≠ 最终失败：HeyGen 等供应商超时后退款，但渲染可能仍在继续，
    # 主站事后与供应商对账会把原任务变 ready 并放出成片，而账号 API 不回写
    # （CLI 永远读不到补回的成片 URL）。注意：task 查询本身 ok=true（查询成功），
    # 判断依据是查询内容 status=error + refunded + 「超时」。
    # 检测到即强制注入说明，防止子 Agent 把「失败退点」当死局。
    if cap == "task" and isinstance(payload, dict) \
            and str(payload.get("status")) == "error" and payload.get("refunded") \
            and "超时" in str(payload.get("error") or ""):
        out["note"] = (
            "【超时退款 ≠ 最终失败】这是「供应商生成超时 + 自动全额退款」（扣点已退回，净扣 0），"
            "不代表成片一定失败：供应商侧可能仍在渲染，成片可能稍后出现在主站原任务上"
            "（状态变 ready，站内可下载）。账号 API/CLI 读不到补回的成片 URL。"
            "向用户报告时：① 说明已全额退款、净扣 0；② 说明成片可能稍后回主站变 ready，"
            "建议稍后回主站查看原任务；③ 不要自动重试同参数（重开=重新扣点），是否重开由用户决定；"
            "④ 不要把话说死成「失败、无成片」。"
        )

    # 查询类能力 → 自动注册交互卡片（形象缩略图/音色试听）
    if ok:
        _register_widgets_from_result(sid, cap, payload)

    # 报价识别：result 带 quote_token + confirmation_required → 记 pending quote
    if ok and payload.get("quote_token") and payload.get("confirmation_required"):
        quote = {
            "capability": cap,
            "inputs_hash": _inputs_hash(cap, inputs),
            "inputs": inputs,
            "quote_token": payload["quote_token"],
            "cost": payload.get("cost"),
            "points": payload.get("points"),
            "expires_in": payload.get("expires_in"),
        }
        # Publish quote identity and its description together: a next-step quote
        # must never be paired with the previous step's summary/price.
        quoted = protocol.make(protocol.NEEDS_APPROVAL, quote_summary(quote), quote=quote)
        def save_quote(current):
            current["pending_quote"] = quote
            current["last_result"] = _bound_outcome(current, quoted)
        state.update_subagent(sid, domain, save_quote)
        out["quote"] = quote
        out["hint"] = (
            "已取得报价（未扣点）。用户明确同意后，用完全相同的 inputs + "
            "confirm=true 提交恰好一次（quote_token 由运行时自动附上）。"
        )
    elif confirm and ok:
        # Publish the receipt with quote consumption under one state lock. The
        # following LLM turn can take seconds (or fail); it must not leave an
        # executable-looking needs_approval snapshot without a pending quote.
        receipt = {k: payload[k] for k in ("job_id", "task_id", "run_id", "request_id", "status")
                   if payload.get(k) is not None}
        if any(receipt.get(k) is not None for k in _JOB_IDS):
            _observe_job(sid, domain, receipt,
                _TASK_TERMINALS.get(str(payload.get("phase") or receipt.get("status") or "").lower(), protocol.RUNNING),
                submit=True, output=payload)
        else:
            def save_sync(current):
                current["pending_quote"] = {}
                current["last_result"] = _bound_outcome(current,
                    protocol.make(protocol.RUNNING, "已确认提交，正在处理结果。", result=receipt))
            state.update_subagent(sid, domain, save_sync)
        out["hint"] = "已确认提交。若返回 job_id/task_id，后续用 task 轮询到终态。"
    if ok and cap == "task":
        _observe_task_query(sid, inputs, payload)
    return out


def _finish(args: dict, sid: str, domain: str) -> dict:
    res = protocol.make(
        state=args.get("state") or protocol.FAILED,
        summary=(args.get("summary") or "").strip(),
        result=args.get("result") or {},
        question=(args.get("question") or "").strip(),
        missing_inputs=args.get("missing_inputs") or [],
        quote=args.get("quote") or {},
        error_code=(args.get("error_code") or "").strip(),
        retryable=bool(args.get("retryable")),
    )
    if res["state"] == protocol.NEEDS_APPROVAL:
        sess = state.get_subagent(sid, domain) or {}
        pending = sess.get("pending_quote") or {}
        if pending:
            # The runtime quote, not a model's paraphrase, owns price/identity.
            res["quote"] = dict(pending)
        else:
            previous = sess.get("last_result") or {}
            res = previous if previous.get("state") in protocol.STATES \
                and previous["state"] != protocol.NEEDS_APPROVAL else protocol.make(
                    protocol.NEEDS_USER_INPUT, "当前没有待确认报价，不会重复提交原任务。")
    res = _save_outcome(sid, domain, res)
    return {"ok": True, "note": f"已按 {res['state']} 收尾", "result": protocol.strip_for_main(res)}


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def _client():
    if not _OPENAI_AVAILABLE:
        raise RuntimeError("未安装 openai 包")
    kwargs = {"api_key": config.LLM_API_KEY, "timeout": config.LLM_TIMEOUT}
    if config.LLM_BASE_URL:
        kwargs["base_url"] = config.LLM_BASE_URL
    if config.MAIN_LLM_PROXY and _HTTPX_AVAILABLE:
        # 显式代理（如本机 SSH 隧道）：绕过环境代理与系统 TUN 透明劫持
        kwargs["http_client"] = httpx.Client(
            proxy=config.MAIN_LLM_PROXY, trust_env=False, timeout=config.LLM_TIMEOUT)
    return OpenAI(**kwargs)


def _is_transient_llm_error(err: Exception) -> bool:
    """网络/超时类错误（值得快速重试一次）；4xx/参数类错误不重试。"""
    name = type(err).__name__
    return name in ("APITimeoutError", "APIConnectionError", "ConnectionError",
                    "TimeoutError", "ReadTimeout", "ConnectTimeout",
                    "RemoteProtocolError", "ProtocolError") \
        or any(t in name for t in ("Timeout", "Connection", "Transport"))


def llm_turn(messages: list, tools: list, temperature: float = 0.4, client_cfg: dict = None,
             max_tokens: int = None):
    """调用 LLM（openai 兼容接口）。返回 choices[0].message。

    client_cfg 为按域模型覆盖（{model, base_url, api_key, proxy?}），
    缺省用主 LLM 配置。max_tokens 可限制输出上限（主 Agent 回复短，设上限省时）。
    网络/超时错误重试一次（防止偶发挂起拖死整轮）；
    两次都失败则抛出，由上层把本轮快速收尾成可读错误，而不是永远卡住。
    """
    def _make_client():
        if not client_cfg:
            return _client(), config.LLM_MODEL
        kwargs = {"api_key": client_cfg["api_key"], "timeout": config.LLM_TIMEOUT}
        if client_cfg.get("base_url"):
            kwargs["base_url"] = client_cfg["base_url"]
        proxy = client_cfg.get("proxy")
        if proxy and _HTTPX_AVAILABLE:
            # 显式代理（如本机 SSH 隧道出境）：绕过环境代理与 NO_PROXY 设置
            kwargs["http_client"] = httpx.Client(
                proxy=proxy, trust_env=False, timeout=config.LLM_TIMEOUT)
        return OpenAI(**kwargs), client_cfg["model"]

    last_err = None
    for attempt in (1, 2):
        try:
            client, model = _make_client()
            create_kwargs = dict(client_cfg.get("create_kwargs") or {}) if client_cfg else {}
            if max_tokens:
                create_kwargs["max_tokens"] = max_tokens
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=temperature,
                **create_kwargs,
            )
            return resp.choices[0].message
        except Exception as err:  # noqa: BLE001
            last_err = err
            if not _is_transient_llm_error(err) or attempt == 2:
                raise
    raise last_err


def serialize_assistant(msg) -> dict:
    item = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        item["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for tc in msg.tool_calls
        ]
    return item


def _synthesize_finish(sid: str, domain: str, content: str, last_tool_ok: bool) -> dict:
    """模型没调 finish 就输出文本时的兜底映射（重新读最新会话状态）。"""
    sess = state.get_subagent(sid, domain) or {}
    pq = sess.get("pending_quote")
    if pq:
        return protocol.make(
            protocol.NEEDS_APPROVAL, summary=content or "已取得报价，等待确认",
            quote={k: pq.get(k) for k in ("quote_token", "cost", "points", "capability", "expires_in")},
        )
    if last_tool_ok:
        return protocol.make(protocol.COMPLETED, summary=content or "任务已完成")
    return protocol.make(protocol.NEEDS_USER_INPUT, question=content or "还需要补充什么信息？",
                         summary=content or "需要向用户确认信息")


# 实时进度：{sid: {"domain": ..., "tool": ..., "ts": ...}}，供 UI 状态轮询展示「正在干什么」
PROGRESS = {}


def trim_progress(now: float, max_age: float = 600, cap: int = 1000):
    """看护线程回收进度注册表：进度只在 20 秒内有效，超龄的整条清掉；
    兜底会话数上限，防异常情况下无界增长。"""
    for sid in list(PROGRESS.keys()):
        ts = (PROGRESS.get(sid) or {}).get("ts", 0)
        if now - ts > max_age:
            PROGRESS.pop(sid, None)
    if len(PROGRESS) > cap:
        for sid in sorted(PROGRESS, key=lambda s: (PROGRESS[s] or {}).get("ts", 0))[: len(PROGRESS) - cap]:
            PROGRESS.pop(sid, None)


# 子 Agent 会话按 (sid, domain) 串行：同一业务域的两个并发轮次不能同时改写该域会话
# （主 Agent 轮次已并发化，聊天不排队；业务重活只跟同域的自己排队，互不干扰）
# 值带引用计数：看护线程只回收「无人持有且久未使用」的锁，回收瞬间起新调用拿新锁，互斥不破。
_DOMAIN_LOCKS = {}          # (sid, domain) -> {"lock": Lock, "refs": 使用计数, "touch": 最近使用}
_DOMAIN_LOCKS_GUARD = threading.Lock()


def _domain_lock_acquire(key) -> dict:
    with _DOMAIN_LOCKS_GUARD:
        e = _DOMAIN_LOCKS.setdefault(key, {"lock": threading.Lock(), "refs": 0, "touch": time.time()})
        e["refs"] += 1
        e["touch"] = time.time()
        return e


def _domain_lock_release(e: dict):
    with _DOMAIN_LOCKS_GUARD:
        e["refs"] -= 1


def trim_domain_locks(now: float, max_idle: float = 1800, cap: int = 500):
    """看护线程回收闲置域锁：refs=0 且久未使用（或超出会话数上限）的 (sid, domain) 摘除。
    先摘字典再等锁：摘走瞬间起新调用拿新锁，互斥不破；等锁确保摘走时无人持有。"""
    removed = []
    with _DOMAIN_LOCKS_GUARD:
        idle = [(k, e["touch"]) for k, e in _DOMAIN_LOCKS.items() if e["refs"] == 0]
        idle.sort(key=lambda x: x[1])
        if len(_DOMAIN_LOCKS) > cap:
            idle = idle[: len(_DOMAIN_LOCKS) - cap]
        else:
            idle = [(k, t) for k, t in idle if now - t > max_idle]
        for k, _ in idle:
            e = _DOMAIN_LOCKS.pop(k, None)
            if e is not None:
                removed.append(e)
    for e in removed:
        with e["lock"]:
            pass


def run_subagent_turn(sid: str, domain: str, task: str) -> tuple[dict, list]:
    """执行一轮子 Agent：新开会话或续接（needs_* 后续轮）。返回 (SpecialistResult, tool_log)。"""
    e = _domain_lock_acquire((sid, domain))
    try:
        with e["lock"]:
            return _run_subagent_turn_locked(sid, domain, task)
    finally:
        _domain_lock_release(e)


def approval_id(quote: dict) -> str:
    """公开报价身份：不把供应商 token 传给浏览器。"""
    if not quote:
        return ""
    raw = json.dumps([quote.get("capability"), quote.get("inputs_hash"), quote.get("quote_token")])
    return hashlib.sha256(raw.encode()).hexdigest()


def respond_to_approval(sid: str, domain: str, quote_id: str | None, decision: str):
    e = _domain_lock_acquire((sid, domain))
    try:
        with e["lock"]:
            sess = state.get_subagent(sid, domain) or {}
            quote = sess.get("pending_quote") or {}
            last = sess.get("last_result") or {}
            if not quote:
                return protocol.make(last.get("state") or protocol.NEEDS_USER_INPUT,
                    "当前没有待确认报价，不会重复提交。" + (last.get("summary") or "请先获取报价。"),
                    result=last.get("result") or {}), []
            if quote_id and quote_id != approval_id(quote):
                return protocol.make(protocol.NEEDS_USER_INPUT, "这张报价已经更新，请查看当前报价后再确认。"), []
            if decision == "cancel":
                res = protocol.make(protocol.CANCELLED, "已取消这张报价，未提交新任务。")
                def cancel(current):
                    current["pending_quote"] = {}
                    current["last_result"] = _bound_outcome(current, res)
                res = state.update_subagent(sid, domain, cancel)["last_result"]
                return res, []
            _APPROVAL_SCOPE.quote = quote
            try:
                return _run_subagent_turn_locked(sid, domain,
                    "用户确认当前已保存的报价。只继续原任务，以 pending_quote 的完全相同 inputs 和 token "
                    "提交一次；不要重新搜索、重新报价或创建另一个需求。若已提交则返回原任务状态。")
            finally:
                _APPROVAL_SCOPE.quote = None
    finally:
        _domain_lock_release(e)


def _run_subagent_turn_locked(sid: str, domain: str, task: str) -> tuple[dict, list]:
    previous = getattr(_TURN_SCOPE, "observed", None)
    _TURN_SCOPE.observed = set()
    try:
        return _run_subagent_turn_impl(sid, domain, task)
    finally:
        _TURN_SCOPE.observed = previous


def _run_subagent_turn_impl(sid: str, domain: str, task: str) -> tuple[dict, list]:
    """run_subagent_turn 的锁内实现（见上：同域串行，防并发覆盖会话）。"""
    if config.LLM_MODE != "openai":
        return _save_outcome(sid, domain, protocol.make(
            protocol.FAILED,
            summary="未配置 LLM API Key，子 Agent 不可用（当前运行在演示模式）",
            error_code="no_llm", retryable=False,
        )), []

    tools = subagent_tools()
    client_cfg = config.SUBAGENT_MODELS.get(domain)  # 按域模型覆盖（如 system→gpt-5.6-luna）
    sess = state.get_subagent(sid, domain)
    if sess is None or not sess.get("messages"):
        messages = [{"role": "system", "content": build_system_prompt(domain)}]
        messages.append({"role": "user", "content": f"（主 Agent 派发的任务）{task}"})
    else:
        messages = sess["messages"]
        messages.append({"role": "user", "content": f"（用户本轮回应）{task}"})

    tool_log = []
    last_tool_ok = False

    t0 = time.monotonic()  # 本轮墙钟预算：超时即收尾
    for _ in range(MAX_STEPS):
        if time.monotonic() - t0 > config.TURN_BUDGET:
            res = protocol.make(
                protocol.FAILED,
                summary="本轮处理超时（已达时间预算）。任务如有 job_id 不会重复扣费，"
                        "请稍后再查或重发。",
                error_code="turn_timeout", retryable=True,
            )
            res = _save_outcome(sid, domain, res, messages)
            return res, tool_log
        try:
            msg = llm_turn(messages, tools, client_cfg=client_cfg)
        except Exception as err:
            # 真实错误必须可见（进 journalctl，按 sid 过滤，summary 带错误类型）
            log.error("subagent:%s LLM 调用失败: %s: %s", domain, type(err).__name__, err,
                      extra=observability.ctx(sid))
            res = protocol.make(
                protocol.FAILED,
                summary=f"模型接口调用失败（错误类型：{type(err).__name__}，已自动重试一次仍失败）。"
                        "任务未受影响（如有 job_id 不会重复扣费），请稍后再发一次。",
                error_code="llm_error", retryable=True,
            )
            res = _save_outcome(sid, domain, res, messages)
            return res, tool_log
        messages.append(serialize_assistant(msg))

        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                PROGRESS[sid] = {"domain": domain, "tool": tc.function.name, "ts": time.time()}
                result = dispatch_tool(tc.function.name, args, sid, domain)
                last_tool_ok = bool(result.get("ok"))
                tool_log.append({"name": tc.function.name, "args": args, "ok": last_tool_ok})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
                if tc.function.name == "finish":
                    res = _save_outcome(sid, domain, result.get("result"), messages)
                    return res, tool_log
            continue

        # 文本收尾兜底
        res = _synthesize_finish(sid, domain, msg.content or "", last_tool_ok)
        res = _save_outcome(sid, domain, res, messages)
        return res, tool_log

    res = protocol.make(protocol.FAILED, summary="处理步骤超限，本轮未完成",
                        error_code="max_steps", retryable=True)
    res = _save_outcome(sid, domain, res, messages)
    return res, tool_log
