"""模块5（选题生成）+ 模块6（文案生成）。

与报告生成同一套纪律：
1. JSON 模板严格对齐需求：选题 ≥15 个且故事/干货/案例三类齐备 + 推荐 3 个重点选题；
   文案 3 种风格（共情/震撼/故事型）各含 3 秒钩子 / 逻辑递进中段 / 金句 / CTA 结尾；
2. 内容 100% 由 LLM 产出，本文件不写任何选题或文案内容；
3. 严格验证，缺口反馈给 LLM 修订重跑（最多 3 轮），绝不手工补；
4. 复用报告 PDF 渲染管线（HTML → headless Chrome）。

模块5 的输入直接从「已确认的模块1-4 报告 JSON」提取（代码只搬运原文，不做生成）：
目标人群、核心领域、核心优势、长期标签、近期目标。
"""
from __future__ import annotations

import datetime
import html
import json
import os
import re
import time

from . import report, state

OUTPUT_DIR = report.OUTPUT_DIR
MAX_ROUNDS = 3
MAX_TOKENS = 8000

TOPIC_TYPES = ("故事型", "干货型", "案例型")
SCRIPT_STYLES = ("共情型", "震撼型", "故事型")
CTA_WORDS = ("扣", "加我", "私", "评论", "关注", "点赞", "领取", "联系", "扫码", "666", "获取", "点击", "回复", "下单")

# ---------------------------------------------------------------------------
# 生成提示词（结构模板，不含任何内容）
# ---------------------------------------------------------------------------

TOPICS_SPEC = """你是短视频选题策划师。基于用户已确认的《IP人设定位｜模块1-4》报告和已采集信息，为这位 IP 生成一份选题方案。

只输出一个 JSON 对象（不要 markdown 代码块、不要任何解释文字），结构必须严格如下：

{
  "topics": [
    {"title": "选题标题（短视频风格、自带钩子）", "type": "故事型|干货型|案例型", "goal": "目标效果（这期内容想让观众产生什么反应/行为）"}
  ],
  "recommended": [
    {"title": "重点选题标题（必须与 topics 里的标题完全一致）", "reasons": ["推荐原因1", "推荐原因2"]}
  ],
  "required_info": ["发现缺少、需要向用户补充采集的原始信息；没有则为空数组"]
}

写作铁律：
1. topics 不少于 15 个，且「故事型」「干货型」「案例型」三类每类至少 3 个；每个选题标题必须具体、可拍、自带钩子，严禁空泛（如"聊聊跨境电商"这种不合格）。
2. 所有选题必须扎根于用户的真实信息：真实故事、真实领域、真实优势、真实目标；严禁编造用户没说过的事实和数据。
3. 每个选题的 goal 要写清目标效果（如"建立信任/引发共鸣/引流加粉/转化咨询"等，要具体到行为）。
4. recommended 恰好 3 个重点选题，必须是 topics 里已有的标题；推荐原因结合目标人群与近期目标逐条论证（每个选题至少 2 条原因）。
5. 信息不足时：正文仍给出你的专业建议，并把缺失的原始信息写进 required_info。
6. 输出必须是完整 JSON，所有字段全覆盖，不留空、不写"待补充/略/TODO"之类占位。"""

SCRIPTS_SPEC = """你是短视频口播文案写手。针对用户选定的重点选题，基于其《IP人设定位｜模块1-4》报告里的真实故事与人设，写三份口播文案。

只输出一个 JSON 对象（不要 markdown 代码块、不要任何解释文字），结构必须严格如下：

{
  "topic": "选定选题的标题",
  "scripts": [
    {
      "style": "共情型|震撼型|故事型",
      "hook": "3秒钩子开头（一句话抓住注意力，不超过60字）",
      "body": "逻辑递进的中段（观点+真实细节+转折，层层推进）",
      "quote": "金句（一句能传播、能截图的话）",
      "cta": "行动号召结尾（如「评论区扣666」「想要资料就私信我」）",
      "full_text": "完整口播文案（把 hook/body/quote/cta 串成可直接口播的整篇）"
    }
  ],
  "recommended": {"style": "推荐风格（必须与 scripts 里某个 style 一致）", "reasons": ["推荐原因1", "..."]},
  "required_info": ["发现缺少、需要向用户补充采集的原始信息；没有则为空数组"]
}

写作铁律：
1. scripts 恰好 3 份，style 恰好覆盖「共情型」「震撼型」「故事型」三种，各写各的，不许互相复制。
2. 三份文案必须结合用户的具体真实故事（用报告故事库里的真实细节），严禁编造没采集过的事实和数据。
3. 每份结构完整：3秒钩子 → 逻辑递进中段 → 金句 → CTA 结尾；full_text 至少 120 字，适合 1-2 分钟口播，口语化。
4. CTA 必须具体可执行（评论互动/私信/领取/关注等号召动作）。
5. recommended 推荐一份最优文案，理由至少 3 条（结合人设定位、目标人群和选题目标效果）。
6. 信息不足时：正文仍给出你的专业建议，并把缺失的原始信息写进 required_info。
7. 输出必须是完整 JSON，所有字段全覆盖，不留空、不写"待补充/略/TODO"之类占位。"""


# ---------------------------------------------------------------------------
# 严格验证器
# ---------------------------------------------------------------------------

def validate_topics(obj: dict) -> list[str]:
    gaps: list[str] = []

    def need(cond, msg):
        if not cond:
            gaps.append(msg)

    if not isinstance(obj, dict):
        return ["选题方案不是 JSON 对象"]
    topics = obj.get("topics") or []
    need(len(topics) >= 15, f"选题总数少于 15 个（当前 {len(topics)} 个）")
    by_type: dict[str, int] = {}
    titles = []
    for i, t in enumerate(topics):
        if not isinstance(t, dict):
            gaps.append(f"选题第 {i + 1} 个不是对象")
            continue
        for k in ("title", "type", "goal"):
            need(t.get(k), f"选题「{t.get('title', f'第{i + 1}个')}」缺「{k}」")
        if t.get("type"):
            if t.get("type") not in TOPIC_TYPES:
                gaps.append(f"选题「{t.get('title')}」类型必须是 故事型/干货型/案例型（当前：{t.get('type')}）")
            else:
                by_type[t["type"]] = by_type.get(t["type"], 0) + 1
        if t.get("title"):
            titles.append(t["title"])
    for ty in TOPIC_TYPES:
        need(by_type.get(ty, 0) >= 3, f"「{ty}」类选题少于 3 个（当前 {by_type.get(ty, 0)} 个）")
    need(len(set(titles)) == len(titles), "存在重复的选题标题")

    recs = obj.get("recommended") or []
    need(len(recs) == 3, f"重点推荐必须恰好 3 个（当前 {len(recs)} 个）")
    for i, r in enumerate(recs):
        if not isinstance(r, dict):
            gaps.append(f"推荐第 {i + 1} 个不是对象")
            continue
        rt = r.get("title")
        need(rt, f"推荐第 {i + 1} 个缺标题")
        if rt:
            need(rt in titles, f"推荐选题「{rt}」不在 topics 列表里，不允许推荐不存在的选题")
        need(len(r.get("reasons") or []) >= 2, f"推荐选题「{rt}」的原因少于 2 条")
    for bad in report._PLACEHOLDERS:
        need(bad not in json.dumps(obj, ensure_ascii=False), f"选题方案里出现占位文字「{bad}」")
    return gaps


def validate_scripts(obj: dict) -> list[str]:
    gaps: list[str] = []

    def need(cond, msg):
        if not cond:
            gaps.append(msg)

    if not isinstance(obj, dict):
        return ["文案方案不是 JSON 对象"]
    need(obj.get("topic"), "缺「topic」选定选题")
    scripts = obj.get("scripts") or []
    need(len(scripts) == 3, f"文案必须恰好 3 份（当前 {len(scripts)} 份）")
    styles = []
    for i, s in enumerate(scripts):
        if not isinstance(s, dict):
            gaps.append(f"第 {i + 1} 份文案不是对象")
            continue
        for k in ("style", "hook", "body", "quote", "cta", "full_text"):
            need(s.get(k), f"文案「{s.get('style', f'第{i + 1}份')}」缺「{k}」")
        if s.get("style"):
            styles.append(s["style"])
            if s["style"] not in SCRIPT_STYLES:
                gaps.append(f"文案风格必须是 共情型/震撼型/故事型（当前：{s['style']}）")
        hook = s.get("hook") or ""
        need(len(hook) <= 60, f"文案「{s.get('style')}」的钩子超过 60 字，不是 3 秒钩子")
        cta = s.get("cta") or ""
        need(any(w in cta for w in CTA_WORDS), f"文案「{s.get('style')}」的 CTA 没有具体号召动作（如扣666/私信/领取等）")
        ft = s.get("full_text") or ""
        need(len(ft) >= 120, f"文案「{s.get('style')}」完整文案少于 120 字（当前 {len(ft)} 字）")
    need(sorted(styles) == sorted(SCRIPT_STYLES), f"三份文案必须覆盖三种风格各一次（当前 {styles}）")

    rec = obj.get("recommended") or {}
    need(rec.get("style"), "缺推荐风格")
    if rec.get("style"):
        need(rec["style"] in styles, f"推荐风格「{rec['style']}」不在三份文案里")
    need(len(rec.get("reasons") or []) >= 3, f"推荐理由少于 3 条（当前 {len(rec.get('reasons') or [])} 条）")
    for bad in report._PLACEHOLDERS:
        need(bad not in json.dumps(obj, ensure_ascii=False), f"文案方案里出现占位文字「{bad}」")
    return gaps


# ---------------------------------------------------------------------------
# 模块5：输入提取（只搬运已确认报告/采集表里的原文，不做生成）
# ---------------------------------------------------------------------------

def _extract_m5_inputs(session_id: str) -> dict:
    rep = state.get_report_json(session_id) or {}
    profile = {k: v for k, v in state.get_profile(session_id).items()
               if not k.startswith("__") and v}
    m1 = rep.get("m1_positioning") or {}
    m2 = rep.get("m2_persona") or {}
    return {
        "目标人群": profile.get("direction.audience") or "",
        "核心领域": m1.get("final", {}).get("name") or profile.get("direction.track") or "",
        "核心优势": profile.get("direction.differentiation") or profile.get("experience.strength") or "",
        "长期标签": (m2.get("core") or {}).get("tags") or "",
        "近期目标": profile.get("business.short_goal") or profile.get("business.long_goal") or "",
    }


def _report_context(session_id: str) -> dict:
    rep = state.get_report_json(session_id) or {}
    profile = {k: v for k, v in state.get_profile(session_id).items()
               if not k.startswith("__") and v}
    return {"report": rep, "profile": profile}


def _report_essence(rep: dict) -> dict:
    """报告精华（喂给模块6 的紧凑版）：整份报告 JSON 动辄数万字符，
    全量灌进 LLM 会把单轮请求拖到分钟级甚至挂起——只取定位/人设/金句/变现要点。"""
    m1 = (rep.get("m1_positioning") or {}).get("final") or {}
    m2c = (rep.get("m2_persona") or {}).get("core") or {}
    m2r = (rep.get("m2_persona") or {}).get("recommendation") or {}
    m3 = rep.get("m3_value") or {}
    return {
        "定位名称": m1.get("name"),
        "定位语": m1.get("slogan"),
        "三合一策略": m1.get("strategy"),
        "核心特质": m2c.get("traits"),
        "长期标签": m2c.get("tags"),
        "人设金句": m2c.get("quote"),
        "被推荐方案": m2r.get("title"),
        "主张核心": (m3.get("final") or {}).get("core"),
        "主张立场": (m3.get("final") or {}).get("stance"),
        "一句话金句": (m3.get("slogan") or {}).get("text"),
        "变现路径": [(p.get("path"), p.get("position"), p.get("script"))
                     for p in (m3.get("monetization") or [])],
    }


def _user_name(session_id: str) -> str:
    rep = state.get_report_json(session_id) or {}
    return report._safe_name((rep.get("meta") or {}).get("name") or "用户")


# ---------------------------------------------------------------------------
# 通用生成循环：生成 → 校验 → 缺口反馈 LLM 修订重跑
# ---------------------------------------------------------------------------

def _run_loop(session_id: str, spec: str, user_parts: list, validate_fn,
              prev_draft, meta_key: str, status_prefix: str, max_tokens: int = MAX_TOKENS):
    meta = state.get_report_full(session_id)

    def put(sub: dict):
        """状态更新合并保留既有字段（如 m6 的 topic），中断自愈靠它找回参数。"""
        cur = dict((state.get_report_full(session_id) or {}).get(meta_key) or {})
        cur.update(sub)
        state.set_report(session_id, {meta_key: cur})

    put({"status": status_prefix + "generating", "round": 1,
         "gaps": [], "ts": time.time()})

    msgs = [{"role": "system", "content": spec}]
    parts = list(user_parts)
    if prev_draft:
        parts += ["", "以下是上一版已通过校验的内容（作为基底，仅按本次要求修改相关部分）：",
                  json.dumps(prev_draft, ensure_ascii=False)]
    parts += ["", "请输出完整 JSON，所有字段缺一不可。"]
    msgs.append({"role": "user", "content": "\n".join(parts)})

    obj = None
    gaps: list[str] = []
    for rnd in range(1, MAX_ROUNDS + 1):
        put({
            "status": status_prefix + "generating", "round": rnd, "rounds": rnd,
            "gaps": [], "phase": f"第 {rnd} 轮生成中…", "ts": time.time()})
        try:
            raw = report._llm_chat(msgs, max_tokens=max_tokens)
        except RuntimeError as e:
            put({"status": "failed", "error": str(e)})
            return None, {"status": "failed", "error": str(e)}

        obj = report._parse_json(raw)
        if obj is None:
            msgs.append({"role": "assistant", "content": raw})
            msgs.append({"role": "user", "content": "你刚才的输出无法解析为 JSON。请只输出一个完整、合法的 JSON 对象，不要任何其他文字。"})
            continue

        gaps = validate_fn(obj)
        if not gaps:
            put({
                "status": status_prefix + "validated", "round": rnd, "rounds": rnd,
                "gaps": [], "phase": "模板校验通过"})
            return obj, {"rounds": rnd, "gaps": []}

        put({
            "status": status_prefix + "generating", "round": rnd, "rounds": rnd,
            "gaps": list(gaps), "phase": f"第 {rnd} 轮校验发现 {len(gaps)} 处缺口，让 LLM 修订重跑",
            "ts": time.time()})
        msgs.append({"role": "assistant", "content": raw})
        msgs.append({"role": "user", "content": report._revise_feedback(gaps)})

    state.set_report(session_id, {meta_key: {"status": "incomplete", "rounds": MAX_ROUNDS, "gaps": list(gaps)}})
    return None, {"status": "incomplete", "gaps": list(gaps)}


def _persist(session_id: str, name: str, suffix: str, obj: dict) -> dict:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    json_path = os.path.join(OUTPUT_DIR, f"{name}_{suffix}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    md_path = os.path.join(OUTPUT_DIR, f"{name}_{suffix}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_md(obj, suffix))
    pdf_path = os.path.join(OUTPUT_DIR, f"{name}_{suffix}.pdf")
    ok, err = report.chrome_print(render_html(obj, suffix), pdf_path)
    return {"pdf": os.path.basename(pdf_path) if ok else None,
            "md": os.path.basename(md_path),
            "json": os.path.basename(json_path),
            "pdf_error": err}


# ---------------------------------------------------------------------------
# 模块5：选题生成
# ---------------------------------------------------------------------------

def generate_topics(session_id: str) -> dict:
    meta = state.get_report_full(session_id)
    if not meta.get("confirmed"):
        return {"ok": False, "error": "用户还没有在 UI 上确认模块1-4 报告；确认后才能进入模块5（选题生成）。"}
    rep = state.get_report_json(session_id)
    if not rep:
        return {"ok": False, "error": "没有可用的模块1-4 报告，请先生成报告。"}

    inputs = _extract_m5_inputs(session_id)
    missing = [k for k, v in inputs.items() if not v]
    ctx = _report_context(session_id)
    stories = ((rep.get("m4_story") or {}).get("stories") or [])
    story_briefs = [{"title": s.get("title"), "一句话": s.get("one_liner"), "钩子": s.get("hook")}
                    for s in stories]

    user_parts = [
        "以下是模块5 的输入（直接从用户已确认的 PDF 报告提取，不要重复询问这些信息）：",
        json.dumps(inputs, ensure_ascii=False, indent=1),
    ]
    if missing:
        user_parts += ["", f"注意：以下输入项尚未采集到（{ '、'.join(missing) }）。"
                           "选题仍要尽力产出，但请把它们写进 required_info，由顾问向用户追问。"]
    user_parts += [
        "",
        "用户已确认报告的精华（选题必须扎根其中的真实内容；定位/标签/金句/变现路径）：",
        json.dumps(_report_essence(rep), ensure_ascii=False),
        "",
        "已采集信息表：",
        json.dumps(ctx["profile"], ensure_ascii=False, indent=1),
        "",
        "故事库速览（用于故事型选题，引用这些真实故事）：",
        json.dumps(story_briefs, ensure_ascii=False, indent=1),
    ]

    obj, loop_meta = _run_loop(session_id, TOPICS_SPEC, user_parts, validate_topics,
                               prev_draft=None, meta_key="m5", status_prefix="m5_",
                               max_tokens=3000)
    if obj is None:
        return {"ok": False, "status": loop_meta.get("status", "failed"),
                "error": loop_meta.get("error"),
                "gaps": loop_meta.get("gaps", []),
                "note": "选题方案未通过模板校验。请根据 gaps 判断：缺分析就再调 m5_topics；缺原始信息就追问用户。"}

    name = _user_name(session_id)
    files = _persist(session_id, name, "选题生成_模块5", obj)
    state.set_report(session_id, {
        "m5": {"status": "ready", "files": files, "rounds": loop_meta.get("rounds"),
               "phase": "选题方案已生成并通过模板校验"},
        "_m5_json": obj,
    })

    by_type: dict[str, list] = {}
    for t in obj["topics"]:
        by_type.setdefault(t["type"], []).append(t["title"])
    return {
        "ok": True, "status": "ready",
        "count": len(obj["topics"]),
        "by_type": {k: len(v) for k, v in by_type.items()},
        "topics": obj["topics"],
        "recommended": obj["recommended"],
        "required_info": obj.get("required_info") or [],
        "files": {k: v for k, v in files.items()},
        "note": "请把选题清单与 3 个重点推荐完整转述给用户，并请用户从中选定一个重点选题（这是模块6 的触发条件）。",
    }


# ---------------------------------------------------------------------------
# 模块6：文案生成
# ---------------------------------------------------------------------------

def generate_scripts(session_id: str, topic: str) -> dict:
    meta = state.get_report_full(session_id)
    if not (meta.get("_m5_json") or {}).get("topics"):
        return {"ok": False, "error": "还没有生成模块5 的选题方案，请先完成模块5。"}
    topic = (topic or "").strip()
    if not topic:
        return {"ok": False, "error": "缺少参数 topic（用户选定的重点选题标题）"}
    rep = state.get_report_json(session_id) or {}
    ctx = _report_context(session_id)

    stories = ((rep.get("m4_story") or {}).get("stories") or [])
    m2 = (rep.get("m2_persona") or {})
    quote = (m2.get("core") or {}).get("quote") or ((m2.get("recommendation") or {}).get("title") or "")

    user_parts = [
        f"用户选定的重点选题：「{topic}」。请针对这个选题写三份口播文案。",
        "",
        "文案必须结合用户真实故事与人设（素材如下）：",
        "故事库：",
        json.dumps([{"title": s.get("title"), "一句话": s.get("one_liner"),
                     "情绪曲线": s.get("emotion_curve"), "钩子": s.get("hook")}
                    for s in stories], ensure_ascii=False, indent=1),
        "人设金句（文案金句要承接这个人设风格）：" + quote,
        "",
        "已确认报告精华（定位/标签/价值主张/变现路径）：",
        json.dumps(_report_essence(rep), ensure_ascii=False),
        "",
        "已采集信息表：",
        json.dumps(ctx["profile"], ensure_ascii=False, indent=1),
    ]

    # 把 topic 先落盘：生成中途服务重启时，中断自愈能据此恢复 m6 参数
    m6_cur = dict((state.get_report_full(session_id) or {}).get("m6") or {})
    m6_cur.update({"status": "m6_generating", "topic": topic, "ts": time.time()})
    state.set_report(session_id, {"m6": m6_cur})

    obj, loop_meta = _run_loop(session_id, SCRIPTS_SPEC, user_parts, validate_scripts,
                               prev_draft=None, meta_key="m6", status_prefix="m6_",
                               max_tokens=4000)
    if obj is None:
        return {"ok": False, "status": loop_meta.get("status", "failed"),
                "error": loop_meta.get("error"),
                "gaps": loop_meta.get("gaps", []),
                "note": "文案方案未通过模板校验。请根据 gaps 判断：缺分析就再调 m6_scripts；缺原始信息就追问用户。"}

    name = _user_name(session_id)
    files = _persist(session_id, name, "文案生成_模块6", obj)
    state.set_report(session_id, {
        "m6": {"status": "ready", "files": files, "rounds": loop_meta.get("rounds"),
               "topic": topic, "phase": "文案已生成并通过模板校验"},
        "_m6_json": obj,
    })

    rec = obj.get("recommended") or {}
    return {
        "ok": True, "status": "ready", "topic": topic,
        "scripts": [{"style": s["style"], "hook": s["hook"], "quote": s["quote"],
                     "cta": s["cta"], "full_text": s["full_text"]} for s in obj["scripts"]],
        "recommended": rec,
        "required_info": obj.get("required_info") or [],
        "files": {k: v for k, v in files.items()},
        "note": "请把三份文案与推荐理由转述给用户；用户可逐条提修改意见，用 script_revise 修订。",
    }


def revise_scripts(session_id: str, feedback: str) -> dict:
    meta = state.get_report_full(session_id)
    prev = meta.get("_m6_json")
    if not prev:
        return {"ok": False, "error": "还没有生成模块6 的文案，请先选定选题生成文案。"}
    feedback = (feedback or "").strip()
    if not feedback:
        return {"ok": False, "error": "缺少参数 feedback（用户的修改意见）"}

    rep = state.get_report_json(session_id) or {}
    ctx = _report_context(session_id)
    user_parts = [
        f"用户对上一版文案提出以下修改意见：{feedback}",
        "请据此修改相关部分（其余内容逐字保留），并重新输出完整 JSON。",
        "",
        "素材（保持事实一致，不编造）：",
        "已确认报告精华（定位/标签/价值主张/变现路径）：",
        json.dumps(_report_essence(rep), ensure_ascii=False),
        "",
        "已采集信息表：",
        json.dumps(ctx["profile"], ensure_ascii=False, indent=1),
    ]

    # 中断自愈需要 topic：从上一版内容里找回并先落盘
    m6_cur = dict((state.get_report_full(session_id) or {}).get("m6") or {})
    m6_cur.update({"status": "m6_generating",
                   "topic": prev.get("topic") or m6_cur.get("topic") or "", "ts": time.time()})
    state.set_report(session_id, {"m6": m6_cur})

    obj, loop_meta = _run_loop(session_id, SCRIPTS_SPEC, user_parts, validate_scripts,
                               prev_draft=prev, meta_key="m6", status_prefix="m6_",
                               max_tokens=4000)
    if obj is None:
        return {"ok": False, "status": "incomplete", "gaps": loop_meta.get("gaps", [])}

    name = _user_name(session_id)
    files = _persist(session_id, name, "文案生成_模块6", obj)
    state.set_report(session_id, {
        "m6": {"status": "ready", "files": files, "rounds": loop_meta.get("rounds"),
               "topic": obj.get("topic", ""), "phase": "已按用户意见修订"},
        "_m6_json": obj,
    })
    return {"ok": True, "status": "ready", "files": {k: v for k, v in files.items()}}


# ---------------------------------------------------------------------------
# Markdown / HTML 渲染（复用报告版式）
# ---------------------------------------------------------------------------

def render_md(obj: dict, suffix: str) -> str:
    L = []
    A = L.append
    if "topics" in obj:
        A(f"# 选题生成｜模块5")
        A(f"整理日期：{datetime.date.today().isoformat()}")
        A("")
        by_type: dict[str, list] = {}
        for t in obj.get("topics", []):
            by_type.setdefault(t["type"], []).append(t)
        for ty in TOPIC_TYPES:
            A(f"## {ty}")
            for i, t in enumerate(by_type.get(ty, []), 1):
                A(f"{i}. **{t.get('title', '')}** —— 目标效果：{t.get('goal', '')}")
            A("")
        A("## 🏆 重点推荐（3 个）")
        for i, r in enumerate(obj.get("recommended", []), 1):
            A(f"{i}. **{r.get('title', '')}**")
            for reason in r.get("reasons", []):
                A(f"   - {reason}")
        A("")
    else:
        A(f"# 口播文案｜模块6")
        if obj.get("topic"):
            A(f"选题：{obj['topic']}")
        A(f"整理日期：{datetime.date.today().isoformat()}")
        A("")
        for s in obj.get("scripts", []):
            A(f"## {s.get('style', '')}")
            A(f"- 3秒钩子：{s.get('hook', '')}")
            A(f"- 中段：{s.get('body', '')}")
            A(f"- 金句：{s.get('quote', '')}")
            A(f"- CTA：{s.get('cta', '')}")
            A("")
            A("完整口播文案：")
            A(s.get("full_text", ""))
            A("")
        rec = obj.get("recommended") or {}
        A(f"## 🏆 推荐最优：{rec.get('style', '')}")
        for i, r in enumerate(rec.get("reasons", []), 1):
            A(f"{i}. {r}")
        A("")
    ri = obj.get("required_info") or []
    if ri:
        A("### 待补充采集（required_info）")
        for i, x in enumerate(ri, 1):
            A(f"{i}. {x}")
    return "\n".join(L)


def _page(suffix: str, body: str, title_extra: str = "") -> str:
    return (
        f"<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<style>{report._HTML_CSS}</style></head><body>{body}</body></html>"
    )


def render_html(obj: dict, suffix: str) -> str:
    E = html.escape
    parts = []
    A = parts.append
    if "topics" in obj:
        A(f"<h1>选题生成｜模块5</h1>")
        A(f"<div class=\"meta\">整理日期：{datetime.date.today().isoformat()}</div>")
        by_type: dict[str, list] = {}
        for t in obj.get("topics", []):
            by_type.setdefault(t["type"], []).append(t)
        total = len(obj.get("topics", []))
        A(f"<p class=\"note\">共 {total} 个选题：故事型 / 干货型 / 案例型，均扎根已确认报告中的真实信息。</p>")
        for ty in TOPIC_TYPES:
            A(f"<h2>{ty}</h2><ol>")
            for t in by_type.get(ty, []):
                A(f"<li><b>{E(t.get('title', ''))}</b><br><span class=\"note\">目标效果：{E(t.get('goal', ''))}</span></li>")
            A("</ol>")
        A("<h2>🏆 重点推荐（3 个）</h2>")
        for i, r in enumerate(obj.get("recommended", []), 1):
            A(f"<div class=\"story\"><p><b>{i}. {E(r.get('title', ''))}</b></p><ul>")
            for reason in r.get("reasons", []):
                A(f"<li>{E(reason)}</li>")
            A("</ul></div>")
    else:
        A(f"<h1>口播文案｜模块6</h1>")
        if obj.get("topic"):
            A(f"<div class=\"meta\">选题：{E(obj['topic'])}　·　整理日期：{datetime.date.today().isoformat()}</div>")
        for s in obj.get("scripts", []):
            A(f"<h2>{E(s.get('style', ''))}</h2>")
            A("<ul>")
            A(f"<li><b>3秒钩子</b>：{E(s.get('hook', ''))}</li>")
            A(f"<li><b>逻辑递进中段</b>：{E(s.get('body', ''))}</li>")
            A(f"<li><b>金句</b>：{E(s.get('quote', ''))}</li>")
            A(f"<li><b>CTA 结尾</b>：{E(s.get('cta', ''))}</li>")
            A("</ul>")
            A(f"<div class=\"story\"><p><b>完整口播文案</b></p><p style=\"white-space:pre-wrap\">{E(s.get('full_text', ''))}</p></div>")
        rec = obj.get("recommended") or {}
        A(f"<p class=\"win\">🏆 推荐最优：{E(rec.get('style', ''))}</p><ol>")
        for r in rec.get("reasons", []):
            A(f"<li>{E(r)}</li>")
        A("</ol>")
    ri = obj.get("required_info") or []
    if ri:
        A("<h2>待补充采集</h2><ol>")
        for x in ri:
            A(f"<li>{E(x)}</li>")
        A("</ol>")
    return _page(suffix, "".join(parts))
