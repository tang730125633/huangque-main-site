"""主 Agent 运行时：只做「识别意图 → 派发子 Agent → 把六态结果翻译成自然回复」。

- 不碰子 Agent 的任何参数：delegate 只收 `task`（用户需求原话或整理后的需求）；
- 渐进披露：system prompt 只含 12 个业务 skill 的 frontmatter 摘要，
  需要时可调 read_skill 展开全文；
- 本地 IP 定位管线（采集→报告→模块5/6）是主 Agent 内建能力，不经子 Agent；
- 对话记忆：子 Agent 的六态结果留在主对话历史里，后续代词/省略说法靠上下文解析。
"""
from __future__ import annotations

import json
import logging
import threading
import time

from .. import config, state as v3_state, tools as v3_tools
from . import observability, protocol, skills, state, subagent

log = logging.getLogger("hq.v4.main_agent")

MAX_STEPS = 12

# ---------------------------------------------------------------------------
# 报告类重活异步化：这些工具内部是「LLM 多轮 + 模板校验 + PDF 渲染」的同步长任务
# （一分钟到数分钟）。若在主 Agent 轮次里同步执行，整条会话被串行锁占住，
# 用户发任何消息都只能排队等——违背「随时能对话」的体验原则。
# 方案：主 Agent 调用后立即拿回「已启动」，马上回复用户；任务在后台线程跑，
# 完成后通过 _TURN_SPAWNER 把「（系统事件）」回注会话，主 Agent 再读结果告知用户。
# ---------------------------------------------------------------------------

_ASYNC_REPORT_TOOLS = {
    "generate_report", "finalize_report", "report_revise",
    "m5_topics", "m6_scripts", "script_revise",
}

_ASYNC_DONE_NOTE = {
    "generate_report": "报告初稿生成完成（三套方案待用户选）",
    "finalize_report": "报告定稿完成",
    "report_revise": "报告修订完成",
    "m5_topics": "模块5 选题生成完成",
    "m6_scripts": "模块6 文案生成完成",
    "script_revise": "文案修订完成",
}

# 各任务完成后的主 Agent 动作指引：把产物内容直接发进对话（体验优先），
# 下载链接由前端状态区自动渲染，不要编造 URL。
_ASYNC_DONE_GUIDANCE = {
    "generate_report": "请立即调用 get_report 读取 options，把三套人设方案（A/B/C 的标题与核心特质，推荐款标⭐）"
                       "完整发进对话让用户选。PDF/Markdown 下载链接在页面下方状态区，说一句「下载链接在下方」即可，不要自己编 URL。",
    "finalize_report": "请立即调用 get_report 确认定稿状态，告知用户定稿完成、下载链接在下方状态区，"
                       "然后直接调用 m5_topics 启动选题生成（全程自动推进，不要停下来问）。",
    "report_revise": "请立即调用 get_report 读取修订后的 options，把更新的三套方案发进对话让用户选，下载链接在下方状态区。",
    "m5_topics": "请立即调用 get_m5m6 读取 m5_topics，把选题清单（标题/类型/目标）完整发进对话，"
                 "推荐款附理由，请用户挑一个作为重点选题。PDF/Markdown 下载链接在下方状态区。",
    "m6_scripts": "请立即调用 get_m5m6 读取 m6_scripts，把三版文案**全文原样**发进对话"
                  "（用标题标出风格：共情型/逻辑型/故事型），说明每版结构与推荐理由。"
                  "PDF/Markdown 下载链接在下方状态区。用户可逐条提修改意见，用 script_revise 修订。",
    "script_revise": "请立即调用 get_m5m6 读取修订后的 m6_scripts，把修订后的文案全文原样发进对话，下载链接在下方状态区。",
}

_ASYNC_JOBS = {}          # sid -> {tool_name: 启动时间戳}（同名任务去重，防止重复启动）
_ASYNC_JOBS_LOCK = threading.Lock()
_TURN_SPAWNER = None      # 由 app.py 注入：fn(sid, event_text) -> seq


def set_turn_spawner(fn):
    """app.py 启动时注入后台轮次入口，报告任务完成后回注系统事件。"""
    global _TURN_SPAWNER
    _TURN_SPAWNER = fn


def list_running_jobs(sid: str) -> list:
    with _ASYNC_JOBS_LOCK:
        return [n for n, on in _ASYNC_JOBS.get(sid, {}).items() if on]


def trim_async_jobs(now: float, max_age: float = 43200, cap: int = 300):
    """看护线程回收任务登记表：正常完成项已由 run 线程 finally 弹出；
    这里清空 map、兜底超龄登记（线程已死但没走到 finally 的极端情况）
    并给会话数封顶——登记只影响「同名去重」，超龄误删最多导致重复启动一次。"""
    with _ASYNC_JOBS_LOCK:
        for sid in list(_ASYNC_JOBS.keys()):
            jobs = _ASYNC_JOBS[sid]
            if not jobs:
                _ASYNC_JOBS.pop(sid, None)
                continue
            for name in [n for n, ts in jobs.items() if now - ts > max_age]:
                jobs.pop(name, None)
            if not jobs:
                _ASYNC_JOBS.pop(sid, None)
        if len(_ASYNC_JOBS) > cap:
            def newest(sid):
                ts = _ASYNC_JOBS[sid].values()
                return max(ts) if ts else 0
            for sid in sorted(_ASYNC_JOBS, key=newest)[: len(_ASYNC_JOBS) - cap]:
                _ASYNC_JOBS.pop(sid, None)


def _report_ready_guard(name: str, args: dict, sid: str) -> dict | None:
    """幂等门禁：产物已经生成完毕且需求没变时，不再重跑重活。
    LLM 偶尔会「为了保险」重复调用，运行时直接拒绝，省时间也省 token。"""
    meta = v3_state.get_report(sid) or {}
    if name == "m5_topics":
        if (meta.get("m5") or {}).get("status") == "ready":
            return {
                "ok": True, "started": False,
                "note": "模块5 选题已经生成完毕（ready），不要重新生成：请用 get_m5m6 读取并把选题清单转述给用户。",
            }
    if name == "m6_scripts":
        m6 = meta.get("m6") or {}
        topic = (args.get("topic") or "").strip()
        if m6.get("status") == "ready" and topic and m6.get("topic") == topic:
            return {
                "ok": True, "started": False,
                "note": "该选题的文案已经生成完毕（ready），不要重新生成：请用 get_m5m6 读取并转述给用户。",
            }
    if name == "generate_report":
        if meta.get("status") == "final":
            return {
                "ok": True, "started": False,
                "note": "模块1-4 报告已经定稿（final），不要重新生成。用户要修改请用 report_revise，用户要换方案请用 finalize_report。",
            }
    return None


def _dispatch_report_async(name: str, args: dict, sid: str) -> dict:
    """把重活工具丢到后台线程，主 Agent 立刻拿回「已启动」继续回复用户。"""
    guarded = _report_ready_guard(name, args, sid)
    if guarded:
        return guarded
    with _ASYNC_JOBS_LOCK:
        if _ASYNC_JOBS.get(sid, {}).get(name):
            return {
                "ok": True, "started": False,
                "note": f"{name} 已经在后台生成中。请先回复用户：正在生成，进度实时可见，可以继续聊；"
                        "生成结束系统会通知你。",
            }
        _ASYNC_JOBS.setdefault(sid, {})[name] = time.time()

    def run():
        try:
            result = v3_tools.dispatch(name, args, sid)
        except Exception as err:  # pragma: no cover
            result = {"ok": False, "error": f"{type(err).__name__}: {err}"}
        # 先回注事件轮次、再退出任务登记表：前端状态轮询不会看到「无任务也无轮次」的空档
        try:
            if _TURN_SPAWNER:
                ok = bool(result.get("ok"))
                detail = result.get("error") or _ASYNC_DONE_NOTE.get(name, name)
                guidance = _ASYNC_DONE_GUIDANCE.get(name, (
                    "请立即调用 get_report（模块5/6 用 get_m5m6）读取最新状态，"
                    "把产物（PDF 链接、方案选项、选题/文案）自然告知用户并推进流程；"
                    "若失败则说明原因与下一步。不要重复问候。"))
                _TURN_SPAWNER(sid, (
                    f"（系统事件，用户不可见：后台任务 {name} 已结束，结果"
                    f"{'成功' if ok else '失败'}：{detail}。{guidance}）"
                ))
        except Exception:
            pass
        finally:
            with _ASYNC_JOBS_LOCK:
                _ASYNC_JOBS.setdefault(sid, {}).pop(name, None)

    threading.Thread(target=run, daemon=True, name=f"report-{sid[:8]}-{name}").start()
    return {
        "ok": True, "started": True,
        "note": f"{name} 已在后台启动。请立即回复用户：报告正在后台生成，进度实时可见，"
                "我们可以继续聊或处理其他需求；生成结束系统会通知你。",
    }


_RESUME_STALE_SECONDS = 150  # 生成中阶段 150 秒没推进且无在跑任务 → 判定中断，自动重跑


def resume_stale_reports(sid: str):
    """报告任务中断自愈：服务重启/线程崩溃会杀死后台生成线程，
    但落盘的 generating 状态还在——用户永远等不到完成通知（「写着写着没下文」）。
    这里检测「卡在生成中 + 无在跑任务」的模块并自动重跑；由状态轮询与每轮对话触发，
    幂等（_ASYNC_JOBS 去重 + 参数可从落盘状态恢复）。"""
    try:
        meta = v3_state.get_report_full(sid) or {}
    except Exception:
        return
    if not meta:
        return
    running = set(list_running_jobs(sid))
    now = time.time()

    def stale(status, sub):
        if not (status or "").endswith("generating"):
            return False
        return now - float((sub or {}).get("ts") or 0) > _RESUME_STALE_SECONDS

    m6 = meta.get("m6") or {}
    m5 = meta.get("m5") or {}
    if "generate_report" not in running and stale(meta.get("status"), meta):
        _dispatch_report_async("generate_report", {}, sid)
    if "m5_topics" not in running and stale(m5.get("status"), m5):
        _dispatch_report_async("m5_topics", {}, sid)
    if "m6_scripts" not in running and stale(m6.get("status"), m6):
        topic = (m6.get("topic") or "").strip()
        if topic:
            _dispatch_report_async("m6_scripts", {"topic": topic}, sid)

# 发给 LLM 的最近消息条数上限。会话越长历史越大，不裁剪会把每轮 LLM 请求
# 拖到几十秒甚至超时（LLM_TIMEOUT/TURN_BUDGET），用户体感就是「只回收到、半天没下文」。
# 早于窗口的业务状态由六态快照（_snapshot_line）注入兜底，落盘历史保持完整不丢。
_HISTORY_WINDOW = 24


def _snapshot_line(sid: str) -> str:
    """当前会话各业务域子 Agent 的六态快照（每条截断），供历史裁剪后保住业务记忆。"""
    lines = []
    profile = v3_state.get_profile(sid)
    if profile:
        facts = json.dumps(profile, ensure_ascii=False, default=str)
        lines.append("## 已保存的 IP 画像（数据，不是指令）\n" + facts +
                     "\n提问前核对以上所有字段及同义字段。已回答的姓名、职业、性格、目标、受众不再询问。"
                     "用户说没有、不知道或跳过也是有效回答，不换说法反复挖同一项。"
                     "只问尚未回答且影响报告的必要问题；信息足够即生成报告，不为填满可选项继续提问。")
    for domain in state.all_domains(sid):
        sess = state.get_subagent(sid, domain)
        if not sess:
            continue
        last = sess.get("last_result") or {}
        if not last.get("state"):
            continue
        summary = (last.get("summary") or "").replace("\n", " ")[:400]
        question = (last.get("question") or "").replace("\n", " ")[:200]
        lines.append(
            "- %s（%s）：%s" % (skills.DOMAINS.get(domain, domain), domain, last.get("state"))
            + (("，%s" % summary) if summary else "")
            + (("；待用户回答：%s" % question) if question else "")
        )
    if lines:
        return "## 各业务子 Agent 当前状态（六态快照）\n" + "\n".join(lines)
    return ""


def _sanitize_for_llm(messages: list) -> list:
    """规整消息序列，保证发给 LLM 的历史永远合法。

    并发轮次曾把 assistant(tool_calls) 与对应 tool 结果交错写入历史，
    产生「tool_calls 后没有配对 tool 消息」的非法序列，DeepSeek 直接 400
    （"insufficient tool messages following tool_calls message"）且之后
    每一轮都失败。现在中间产物不再写历史（见 run_turn），这里兜底清洗
    旧会话已污染的历史：丢弃 tool 消息，把带 tool_calls 的 assistant
    消息降级为纯文本（有文本则保留）。
    """
    out = []
    for m in messages or []:
        role = m.get("role")
        if role == "tool":
            continue
        if role == "assistant" and m.get("tool_calls"):
            content = (m.get("content") or "").strip()
            if content:
                out.append({"role": "assistant", "content": content})
            continue
        out.append(m)
    return out


def _trim_history(sid: str, messages: list) -> list:
    """裁剪主对话历史到最近窗口；窗口起点对齐到 user 消息，
    保证窗口内 assistant(tool_calls)/tool 往返完整、LLM 请求不因缺 tool 结果报错。"""
    start = max(1, len(messages) - _HISTORY_WINDOW)
    while start > 1 and start < len(messages) and messages[start].get("role") != "user":
        start -= 1
    head = list(messages[:1])  # 保留 system prompt
    snapshot = _snapshot_line(sid)
    if snapshot:
        head.append({"role": "system", "content": snapshot})
    return _sanitize_for_llm(head + messages[start:])

_LOCAL_V3_TOOLS = {  # 本地 IP 管线工具（不派发给子 Agent）
    "update_profile", "get_profile", "profile_status",
    "generate_report", "finalize_report", "report_revise", "get_report",
    "m5_topics", "m6_scripts", "script_revise", "get_m5m6",
}

# 路由表（与 docs/subagent-routing.md 同源；边界裁决见该文档）
_ROUTING = [
    ("image", "hq-image", "图片", "海报/封面/头像/概念图生成、图生图、多图合成、改比例重出、看图"),
    ("video", "hq-video", "视频", "文生/图生视频、口型同步、动作模仿、电影化身、换装"),
    ("audio", "hq-audio", "音频", "文案配音、口播音频、声音克隆"),
    ("copy", "hq-copy", "文案编导", "写口播脚本、爆款拆解、分镜出图、脚本成片、同款复刻"),
    ("digital-human", "hq-digital-human", "数字人", "形象/音色/口播成片、文案成片、数字人讲解员、一键生成"),
    ("short-drama", "hq-short-drama", "短剧", "短剧立项、剧本共创、开拍预检、生产交付"),
    ("compose", "hq-compose", "成片", "一键成片剪辑、模板成片（单条/批量）"),
    ("canvas", "hq-canvas", "画布", "画布管理、节点写入、创作计划、节点内生成"),
    ("leads", "hq-leads", "获客", "平台获客名单、线索跟进"),
    ("collect", "hq-collect", "采集", "链接内容/评论/原视频/口播稿采集、关键词搜索"),
    ("ip-positioning", "hq-ip-positioning", "IP 定位", "IP12/数字化 IP 项目管理与报告、灵感案例"),
    ("system", "hq-system", "系统", "账号点数、任务轮询、资产库、成品下载、价格、导航"),
]


def _routing_table_text() -> str:
    lines = ["| 业务结果（用户想要什么） | 子 Agent id | 典型需求示例 |", "| --- | --- | --- |"]
    for domain, agent_id, name, examples in _ROUTING:
        lines.append(f"| {name} | `{agent_id}` | {examples} |")
    return "\n".join(lines)


def _skill_summary_text() -> str:
    lines = []
    for domain, agent_id, name, examples in _ROUTING:
        s = skills.business_skill_summary(domain)
        desc = s.get("short_description_zh") or s.get("short_description") or name
        lines.append(f"- `{agent_id}`（{name}）：{desc}")
    return "\n".join(lines)


def _load_routing_doc() -> str:
    """路由表全文（含边界裁决与六态处理规则）；读不到用内联版。"""
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "docs", "subagent-routing.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""  # 走内联


_SYSTEM_PROMPT_CACHE = None


def build_system_prompt() -> str:
    """主 Agent system prompt。静态内容，进程内缓存（每轮不再重建/读盘）。"""
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is not None:
        return _SYSTEM_PROMPT_CACHE
    routing_doc = _load_routing_doc()
    if not routing_doc:
        routing_doc = f"""## 业务结果 → 子 Agent 路由表
{_routing_table_text()}
"""

    prompt = f"""你是黄雀主 Agent（IP 定位顾问 + 业务结果路由员）。你陪用户聊 IP 定位，也能把用户的「产物需求」派给 12 个黄雀子 Agent 干成实际业务结果。

{routing_doc}

## 渐进披露

你只看到每个子 Agent 业务 skill 的摘要（如下）。识别出意图后直接把需求派给对应子 Agent，不必先读全文；只有在你拿不准该派给谁、需要核对某域边界时，才用 read_skill 展开那个域的 skill 全文再决定。

{_skill_summary_text()}

## 派发纪律（delegate_<domain>）

1. 只传任务：把用户需求整理成一段 task 文字传给子 Agent；工具、参数、报价、轮询全由子 Agent 自己闭环。**绝不替子 Agent 填参数、绝不编造用户没给的信息**（素材、链接、id 一律用用户原话）。
2. 付费自动确认执行：子 Agent 返回 needs_approval 时，运行时已按用户授权自动确认报价并继续执行（体验优先），你会直接拿到执行后的终态；只需把实际费用（cost/points）在回复里如实告知用户，不要停下来让用户再确认。
3. 不重复派发：同一产物需求在未拿到终态（completed/failed/cancelled）前，不换子 Agent 重复提交。
4. 六态处理：
   - completed → 把 summary 与成果（资产/URL/文件）自然转述给用户，任务闭环；
   - running → 告诉用户已提交 + job_id，之后可交给 hq-system 轮询（或等子 Agent 查）；
   - needs_user_input → 把 question 原样转问用户；用户回答后把回答作为 task 回派给**同一个**子 Agent；
   - needs_approval → 报价已由运行时自动确认并提交执行（用户已授权）；按执行后的结果继续，并在回复中说明本次实际费用；
   - failed → retryable=true 且用户还想要时回派重试；否则把原因转述用户，不擅自换方案重复扣点；若原因是供应商超时退款（已全额退款），向用户说明成片可能稍后回主站原任务变 ready，别把话说死成「无成片」，也别擅自重开（重开=重新扣点）；
   - cancelled → 转述取消，结束该业务。
5. 跨域串接：一次需求跨多域时按顺序派发，把上一跳返回的凭据（job_id/资产 id/quote_token 等）写进下一跳的 task。
6. 子 Agent 续会话：needs_user_input / needs_approval / running 之后再收到用户回应，**只有当这条回应确实在回答该域的问题、推进该域的任务**时，才 delegate 给**同一个域**（系统自动续接该域会话），不要开新需求。用户只是闲聊、感叹、岔开话题（如「刚才那条口播就是我做的，现在想聊点别的」「先不弄了，聊会儿天」）时，**绝不为续接而 delegate**：正常聊天回应即可，子 Agent 的待办状态原地保留，等用户真想继续时再续接。delegate 会让页面重新挂出对应业务的卡片/待办，闲聊时重挂是打扰，必须避免。
7. 出片流程的「三版文案」是**免费选择卡**：用户要出数字人口播/成片（出片需求）后，这个流程里的一切子任务（三版文案、查形象、查音色、最终生成）**都只派 `hq-digital-human`**，绝不派 `hq-copy` 开付费写稿，也绝不把文案写进回复正文代替卡片；`hq-copy` 只用于与出片无关的独立写稿需求。

## 本地 IP 定位管线（你内建，不经子 Agent）

《IP人设定位》报告是你自己的内建能力：通过 update_profile/get_profile/profile_status 采集 8 模块信息 → generate_report 出模块1-4 PDF（三套人设方案让用户选）→ finalize_report 定稿 → 定稿后 m5_topics 出选题 → 用户选定后 m6_scripts 出文案 → script_revise 逐条修订。这段流程你自己干，不派给 hq-ip-positioning；用户要「打开/读/管理主站 IP12 项目」这类主站业务才派 hq-ip-positioning。

generate_report / finalize_report / report_revise / m5_topics / m6_scripts / script_revise 都是**后台异步任务**：调用后工具立刻返回「已启动」，你**马上回复用户**（正在生成、进度实时可见、可以继续聊），绝不停在原地等；任务结束后系统会以「（系统事件）」通知你，届时再调用 get_report / get_m5m6 读取结果告知用户。用户在生成期间发来的任何消息都要正常回应。同一产物已经 ready/final 时**绝不重复调用**这些工具（运行时也会拒绝）：先查 get_report / get_m5m6，已生成就直接把结果转述给用户。

## 交互卡片（页面组件）

- 子 Agent 查询数字人形象/音色时，页面会自动把结果渲染成缩略图/试听卡片，用户直接点击即可选中；你只需在回复里说一句「卡片在下方，点一下就行」，不要把列表抄成文字让用户打字选。
- 子 Agent 也可以注册「文案多版」等选择卡片；用户点选后，消息会以「【点选】…」开头回到你这里——把它当作用户的选择，原样转给对应子 Agent 续会话（同一域）。
- 需要用户上传图片时，页面支持在输入框贴图后随文字发送；子 Agent 会拿到图片信息，不用你处理路径。

## 素材卡三纪律（形象/音色/文案卡片，必须遵守）

1. 只在该出现时出现：只有用户明确要出片（如「用我的形象录口播」「这段文案做成视频」），或模块六文案已确认要出成片时，才让子 Agent 查询形象/音色（查询才会渲染素材卡）；平时聊天绝不为了展示而查询，保持零卡片。查询/注册卡片前，必须先按下方「出片引导四句」把话说清楚——先引导、后卡片。用户在出片流程中途转去闲聊（哪怕消息里带「口播/出片」字样，如「刚才那条口播就是我做的，现在想聊点别的」）时，当前轮**绝不派 `hq-digital-human`、绝不重查形象/音色**——派了页面就会把出片卡重新挂出来，必须避免；只有用户再说要出片（生成数字人/做口播视频/出片），或点了卡片按钮（消息以「【点选】」「【已选汇总】」开头），才再派。
2. 每张卡都能关：素材卡和「本次出片配置」卡都带 ×，用户关掉=这次不出片、对话继续；用户关掉后不要追问，也不要让子 Agent 重复查询注册同一批素材。
3. 卡片是一次性附件：它们跟在消息流里，下一条普通回复发出后旧卡自动收起；回复里不要复述卡片内容、不要把卡片钉成常驻栏。

## 出片引导四句（用户说出片需求时的固定结构，写死，必须遵守）

用户说出片需求（如「要十条秒数字人口播」）后，你的第一段回复必须**四句齐备、顺序固定**（缺一句都不行），说完之后再让子 Agent 查素材出卡片。卡片是附件不是主角：引导在上、选择在下，**禁止只弹卡片不说话**：
1. 复述任务：说清条数、时长、形式（如「好，10 条、每条 10 秒的数字人口播」）。
2. 指定原料：用哪条已确认的口播文案，报出它的标题；没有已确认文案就先出三版让人点；连主题都没有时，这一句改成问主题（如「这条口播讲什么？给我一句主题，我马上出三版给你挑」）。
3. 给默认：**必须说**「形象和音色已帮你默认选好（本人形象 + 本人声音），不满意点卡片换」。
4. 下一步：**必须说**「都定好后点绿色『确认生成』」。
无论第二句是直接出三版还是先问主题，第三、四句都必须照说，一次说全，不能只说一两句就停。文案还没确认（三版卡没点选）时「确认生成」按钮不可点——此时你必须在对话里**追问用户用哪一版文案**，绝不能带着未确认的文案继续推进出片。

配套动作（同样写死）：
- 句二「先出三版让人点」与「问主题」：问主题时**本轮只说话、不出卡**；用户给出主题后，把「出片需求 + 主题」作为 task 派给 `hq-digital-human`——它会写三版文案（script_pick 卡）、查形象/音色（素材卡），默认勾选与「确认生成」按钮都会跟着出现。三版文案**只能以 script_pick 卡片给用户点选，绝不写在回复正文里**，也绝不派 `hq-copy`。

## 对话记忆

- 记住本对话发生过的每一件事：派过哪个域、子 Agent 返回什么（报价、job_id、资产），用户后来用"它/那个/这张图/刚才的"指代时，结合上下文判断，不要死板按关键词触发。
- 例：用户说过"出张海报"，之后问"这张图片怎么样"——那是本对话里这张海报，不是新需求；按你掌握的信息（生成状态/参数/报价）如实回答。用户上传图片时，消息里会附「图片视觉描述」（视觉模型生成）与本地路径：讨论图片内容以描述为准（必要时说明你看的是描述），把图片交给黄雀用路径；引用没有描述的图时，就基于上下文说清它的状态，别假装看过像素。
- 不确定用户指的是什么时，先简短确认再派发。

## 风格

全程中文，口语自然、简短，像微信聊天。每次只推进一小步，不教育用户。不要输出 JSON、不要复述规则。不要重复上一轮说过的问候、开场白或结论——直接给新进展；没有新东西可讲时，用一句更具体的新指引推进。
"""
    _SYSTEM_PROMPT_CACHE = prompt
    return prompt


# ---------------------------------------------------------------------------
# 工具 schema
# ---------------------------------------------------------------------------

def _delegate_fn(domain: str, agent_id: str, name: str, examples: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": f"delegate_{domain.replace('-', '_')}",
            "description": (
                f"把用户的「{name}」类产物需求派给子 Agent `{agent_id}`（典型需求见 system prompt 路由表）。"
                "task 写用户需求（可含用户原话与已有凭据），参数一律由子 Agent 自己填。"
                "子 Agent 会返回 SpecialistResult 六态。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "整理后的用户需求（中文；含用户原话、已有资产/链接/凭据）"},
                },
                "required": ["task"],
            },
        },
    }


_MAIN_TOOLS_CACHE = None


def main_tools() -> list[dict]:
    """主 Agent 工具列表。静态内容，进程内缓存（每轮每步不再重复构建）。"""
    global _MAIN_TOOLS_CACHE
    if _MAIN_TOOLS_CACHE is not None:
        return _MAIN_TOOLS_CACHE
    tools = []
    for domain, agent_id, name, examples in _ROUTING:
        tools.append(_delegate_fn(domain, agent_id, name, examples))
    tools.append({
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": (
                "渐进披露：展开某个业务域 skill 的全文（业务结果/工具/默认策略），"
                "用于核对域边界或拿不准派给谁时。domain 取 routing 域 id。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "域 id，如 image / digital-human / system"},
                },
                "required": ["domain"],
            },
        },
    })
    # 本地 IP 管线：复用 v3 工具
    for t in v3_tools.TOOLS:
        name = t["function"]["name"]
        if name in _LOCAL_V3_TOOLS:
            tools.append(t)
    _MAIN_TOOLS_CACHE = tools
    return tools


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------

def dispatch(name: str, args: dict, sid: str, log: list) -> dict:
    """主 Agent 工具分发。delegate_* → 子 Agent 运行时；本地工具 → v3 分发器。"""
    args = args or {}

    if name.startswith("delegate_"):
        domain = name[len("delegate_"):].replace("_", "-")
        if domain not in skills.DOMAINS:
            return {"ok": False, "error": f"未知业务域：{domain}"}
        task = (args.get("task") or "").strip()
        if not task:
            return {"ok": False, "error": "缺少 task"}
        res, sub_log = subagent.run_subagent_turn(sid, domain, task)
        for entry in sub_log:
            entry = dict(entry)
            entry["domain"] = domain
            entry["name"] = f"{domain}:{entry['name']}"
            log.append(entry)
        if res.get("state") == "needs_approval":
            # 体验优先：用户已授权，报价拿到后立即自动确认并提交执行，不打断用户
            quote = res.get("quote") or {}
            confirm_res, confirm_log = subagent.run_subagent_turn(
                sid, domain,
                "用户已确认报价，请用完全相同的 inputs 加上 confirm=true 提交恰好一次，"
                "不要重新报价，提交后按结果收尾。",
            )
            for entry in confirm_log:
                entry = dict(entry)
                entry["domain"] = domain
                entry["name"] = f"{domain}:{entry['name']}"
                log.append(entry)
            res = confirm_res
            if res.get("state") != "needs_approval":
                res = dict(res)
                res["auto_confirmed"] = {
                    "cost": quote.get("cost"),
                    "points": quote.get("points"),
                }
        out = protocol.strip_for_main(res)
        out["hint"] = {
            "completed": "转述成果给用户，任务闭环",
            "running": "告诉用户已提交（带 job_id），之后可派 hq-system 轮询",
            "needs_user_input": "把 question 原样转问用户；用户回答后 delegate 给同一域续会话",
            "needs_approval": "报价已自动确认并继续执行；把实际费用转述用户",
            "failed": "retryable 且用户还想要时回派重试；否则转述原因；供应商超时退款（已全额退款）时说明成片可能稍后回主站变 ready，别擅自重开",
            "cancelled": "转述取消，结束该业务",
        }.get(res.get("state"), "")
        return {"ok": res.get("state") != "failed", **out}

    if name == "read_skill":
        domain = (args.get("domain") or "").strip()
        if domain not in skills.DOMAINS:
            return {"ok": False, "error": f"未知业务域：{domain}；可选 {', '.join(skills.DOMAINS)}"}
        _, body = skills.load_business_skill(domain)
        # 只回传前半段足够判断边界/契约；全量塞进上下文会拖慢每轮 LLM（实测单轮 170s+）
        return {"ok": True, "domain": domain, "skill": body[:4000]}

    if name in _LOCAL_V3_TOOLS:
        if name in _ASYNC_REPORT_TOOLS:
            return _dispatch_report_async(name, args, sid)
        return v3_tools.dispatch(name, args, sid)

    return {"ok": False, "error": f"未知工具：{name}"}


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

_OPENING_INSTRUCTION = (
    "（用户刚进入对话。请用一句话开场：告诉用户你可以陪他做 IP 人设定位，"
    "也可以帮他出海报/视频/音频/文案/数字人/短剧等黄雀业务结果。只问一个问题作为钩子，"
    "例如「你现在主要做什么？」。不要一次问两个或更多问题。）"
)


def run_turn(sid: str, user_text: str | None = None, context_note: str | None = None,
             approval: dict | None = None):
    """主 Agent 一轮对话。返回 (reply, tool_log, routing)。

    context_note：仅注入本轮 LLM 上下文的后台状态速览（不进历史）——
    主 Agent 按最新任务状态回答，杜绝「还在跑」错报。

    并发安全（聊天不排队）：历史只做原子追加，绝不整体覆盖；
    LLM 上下文用「本轮开始时的历史快照 + 本轮新增消息」的本地视图，
    其他并发轮次的消息互不干扰、互不丢失。
    """
    state.ensure_main_seed(sid, {"role": "system", "content": build_system_prompt()})
    history = state.get_main_history(sid)

    tool_log = []
    routing = []  # 本轮派发轨迹（供 UI/日志展示）

    def commit(*msgs):
        """本轮新增消息原子追加进落盘历史。"""
        if msgs:
            state.append_main_history(sid, list(msgs))

    # 报价回复走原业务会话，不能把「确认」重新交给主模型理解成一次新采集。
    decision = (user_text or "").strip().rstrip("。！! ")
    domains = state.all_domains(sid)
    pending = [d for d in domains if (state.get_subagent(sid, d) or {}).get("pending_quote")]
    target = (approval or {}).get("domain")
    if not approval and decision in ("确认", "确认生成", "确认采集", "先不生成，我再想想"):
        if len(pending) == 1:
            target = pending[0]
        elif len(pending) > 1:
            reply = "有多张待确认报价，请点击要执行的那张报价卡，我会继续对应任务。"
            commit({"role": "user", "content": user_text}, {"role": "assistant", "content": reply})
            return reply, tool_log, routing
        elif len(domains) == 1 and not v3_state.get_profile(sid):
            target = domains[0]
    if target:
        res, entries = subagent.respond_to_approval(
            sid, target, (approval or {}).get("quote_id"),
            (approval or {}).get("decision", "cancel" if decision.startswith("先不生成") else "confirm"))
        reply = res.get("summary") or "已收到，请查看原任务状态。"
        routing.append({"domain": target, "state": res.get("state"), "summary": reply})
        commit({"role": "user", "content": user_text}, {"role": "assistant", "content": reply})
        return reply, entries, routing

    if user_text is None:
        if config.LLM_MODE == "mock":
            reply = "你好，我是你的 IP 定位顾问，也可以帮你出海报、做视频、写文案。你现在主要做什么？"
            commit({"role": "user", "content": _OPENING_INSTRUCTION},
                   {"role": "assistant", "content": reply})
            return reply, tool_log, routing
        ctx = _trim_history(sid, history)
        ctx.append({"role": "user", "content": _OPENING_INSTRUCTION})
        try:
            msg = subagent.llm_turn(ctx, main_tools(), temperature=0.5, max_tokens=900)
        except Exception as err:
            # 真实错误必须可见：进 journalctl（按 sid 过滤），reply 带错误类型，
            # 否则下次故障仍然只能看到「没响应」而无法定位。
            log.error("LLM 调用失败: %s: %s", type(err).__name__, err, extra=observability.ctx(sid))
            reply = ("模型接口刚才没响应（错误类型：%s，已自动重试一次）。"
                     "稍等几秒再发一次，我会继续。" % type(err).__name__)
            commit({"role": "user", "content": _OPENING_INSTRUCTION},
                   {"role": "assistant", "content": reply})
            return reply, tool_log, routing
        ser = subagent.serialize_assistant(msg)
        ctx.append(ser)
        reply = msg.content or ""
        # 开场轮只跑一步：若有 tool_calls 也剥离（历史只存纯文本回复）
        if ser.get("tool_calls"):
            ser = {"role": "assistant", "content": reply}
        commit({"role": "user", "content": _OPENING_INSTRUCTION}, ser)
        return reply, tool_log, routing

    ctx = _trim_history(sid, history)
    user_msg = {"role": "user", "content": user_text}
    ctx.append(user_msg)
    # 后台任务状态速览：只进本轮 ctx，不进历史（历史只存用户原话+最终回复）
    if context_note:
        ctx.append({"role": "user", "content": context_note})
    # 注意：user_msg 不在这里单独 commit。最终回复产生后，与它「成对原子追加」
    # （一次 extend 两条）：并发轮次交错时历史永远是 user/assistant 合法交替，
    # 不会出现 user,user 插队导致 DeepSeek 400，也不会出现回复被吞。

    t0 = time.monotonic()  # 本轮墙钟预算：超时即收尾，绝不无限拖长
    for _ in range(MAX_STEPS):
        if time.monotonic() - t0 > config.TURN_BUDGET:
            reply = "这轮处理得有点久，我先停下来。你可以再发一条继续，任务不会重复扣费。"
            commit(user_msg, {"role": "assistant", "content": reply})
            return reply, tool_log, routing
        if config.LLM_MODE == "mock":
            reply = "演示模式未配置 LLM Key，主 Agent 无法运行。请配置 .env 后重试。"
            commit(user_msg, {"role": "assistant", "content": reply})
            return reply, tool_log, routing

        try:
            msg = subagent.llm_turn(ctx, main_tools(), temperature=0.5, max_tokens=900)
        except Exception as err:
            # 真实错误必须可见：进 journalctl（按 sid 过滤），reply 带错误类型
            log.error("LLM 调用失败: %s: %s", type(err).__name__, err, extra=observability.ctx(sid))
            reply = ("模型接口刚才没响应（错误类型：%s，已自动重试一次）。"
                     "稍等几秒再发一次，我会继续。" % type(err).__name__)
            commit(user_msg, {"role": "assistant", "content": reply})
            return reply, tool_log, routing
        ser = subagent.serialize_assistant(msg)
        ctx.append(ser)
        # 中间产物（assistant tool_calls 与 tool 结果）只留在本轮本地 ctx，
        # 不写历史：并发轮次会把这些消息交错写入，产生非法序列让 DeepSeek 400。
        # 历史里只保留「用户消息 + 最终纯文本回复」，且成对原子追加。

        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = dispatch(tc.function.name, args, sid, tool_log)
                if tc.function.name.startswith("delegate_"):
                    routing.append({
                        "domain": tc.function.name[len("delegate_"):].replace("_", "-"),
                        "task": (args.get("task") or "")[:80],
                        "state": result.get("state"),
                        "summary": (result.get("summary") or "")[:120],
                    })
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
                ctx.append(tool_msg)
            continue

        reply = (msg.content or "").strip()
        if not reply:
            # 模型给出空回复（不能留空给用户）：追加引导后继续本轮，直到给出文字
            ctx.append({"role": "user", "content": "（不要只给空回复：请把要跟用户说的话用中文写出来。）"})
            continue
        commit(user_msg, ser)
        return reply, tool_log, routing

    reply = "我这边处理得有点久，先停一下。你可以再发一句，我们继续。"
    commit(user_msg, {"role": "assistant", "content": reply})
    return reply, tool_log, routing
