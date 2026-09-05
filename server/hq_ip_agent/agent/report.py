"""报告生成器：《{姓名}IP人设定位｜模块1-4》PDF 报告。

职责（严格对齐 PDF 样例《朱珠IP人设定位_模块1-4》）：
1. 定义报告的 JSON 结构模板（REPORT_SPEC）——模块一~四的所有分析层，缺一不可；
2. 由 LLM 按模板生成报告全文（内容 100% 由 LLM 产出，本文件不写任何报告内容）；
3. 严格验证（validate）：缺模块/缺分析层/缺数量/留占位符都算不通过；
4. 验证不通过 → 把缺口清单反馈给 LLM，让它修改后重新生成（自动修订循环，
   最多 3 轮），而不是由代码补内容；
5. 渲染 HTML → headless Chrome 打印成 PDF，同时留一份 Markdown 便于对照。

输出目录：项目根目录 output/
"""
from __future__ import annotations

import datetime
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from . import config, state

OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output"
)


def _find_chrome() -> str | None:
    """探测 headless Chrome：环境变量 CHROME_BIN > 常见安装路径 > PATH。"""
    env = os.environ.get("CHROME_BIN")
    if env and os.path.exists(env):
        return env
    for path in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if os.path.exists(path):
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


CHROME_BIN = _find_chrome()

MAX_ROUNDS = 3          # 初稿最多生成轮数（首轮 + 修订）
MAX_FINAL_ROUNDS = 2    # 定稿修订最多轮数
MAX_TOKENS = 8000

# ---------------------------------------------------------------------------
# 报告结构模板（与样例逐层对齐，写入生成提示词）
# ---------------------------------------------------------------------------

REPORT_SPEC = """你是资深 IP 人设定位分析师。根据下方「已采集的用户信息」，生成一份《IP人设定位｜模块1-4》完整分析报告。

只输出一个 JSON 对象（不要 markdown 代码块、不要任何解释文字）。结构必须严格如下，样例里的模块与分析层一个都不能少：

{
  "meta": {"name": "用户称呼/姓名", "date": "YYYY-MM-DD", "framework": "AI做IP十二模块框架"},
  "m1_positioning": {
    "keywords": [{"name": "关键词名", "desc": "一句话解读"}],            // 必须恰好 7 个
    "final": {"name": "定位名称", "slogan": "定位语", "strategy": "三合一策略"},
    "market_opportunities": ["市场机缘1", "..."],                        // 至少 4 条
    "risks": ["潜在风险1", "..."]                                        // 至少 4 条
  },
  "m2_persona": {
    "options": [                                                          // 必须恰好 3 套：A/B/C
      {"id": "A", "title": "方案名", "traits": "核心特质", "story_tone": "故事基调",
       "tags": "标签", "formula": "人设公式", "pros": "优势", "cons": "劣势"}
    ],
    "recommendation": {"chosen": "C", "title": "被推荐方案名", "reasons": ["理由1", "..."]}, // 理由至少 4 条
    "core": {"traits": "核心特质", "story_tone": "故事基调", "tags": "核心标签",
             "quote": "人设金句", "image": "对外形象"}
  },
  "m3_value": {
    "diagnosis": [{"original": "原金句", "problem": "问题", "suggestion": "建议"}],  // 至少 2 行
    "final": {"core": "主张核心", "stance": "主张立场"},
    "slogan": {"text": "一句话金句", "reasons": ["理由1", "..."]},           // 理由至少 3 条
    "slogan_alternatives": [{"scenario": "场景", "slogan": "金句"}],         // 至少 4 行
    "self_intro": {"original": "原版", "optimized": "优化版", "reasons": ["优化理由", "..."]}, // 理由至少 2 条
    "monetization": [{"path": "路径", "position": "定位", "script": "承接话术"}] // 必须恰好 3 条
  },
  "m4_story": {
    "stories": [                                                            // 至少 5 个故事
      {"title": "故事名", "one_liner": "一句话", "emotion_curve": "情绪词（状态）→情绪词（状态）→…",
       "scenarios": "适用场景", "hook": "钩子设计（必须是一个以问号结尾的钩子问题）",
       "spread": "星级（⭐1-5）+一句传播价值点评"}
    ],
    "main_storyline": {"primary": "首选故事线", "reasons": ["组合理由", "..."]}, // 理由至少 4 条
    "optimization": {
      "slogan_upgrades": [{"type": "类型", "original": "原文", "optimized": "优化版", "reason": "理由"}], // 至少 2 行
      "edge_strategy": {"problem": "原文问题（争议点）", "idea": "转化思路", "content": ["可发布内容1", "..."]}, // 至少 2 条
      "pit_deepdive": {"directions": ["深挖方向1", "..."], "quote": "金句提炼", "series": "系列延伸"}, // 方向至少 2 条
      "narrative": {"original": "原叙事", "optimized": "优化叙事框架"}
    },
    "priority": [{"p": "P0", "module": "模块", "task": "关键任务", "output": "预计产出"}], // 至少 4 行（P0~P3）
    "doc_status": "文档状态"
  },
  "required_info": ["你分析时发现仍缺少、需要向用户补充采集的原始信息；没有则为空数组"]
}

写作铁律：
1. 只能基于已采集信息做分析，严禁编造未采集的事实；分析缺原料时，把缺的原始信息写进 required_info，但报告正文仍要给出你的专业分析与建议（如有多套合理方向，给出明确推荐）。
2. 分析层内容必须具体、可落地、有判断：关键词提炼要带解读、推荐理由要逐条论证、潜在风险要写清成因与应对、情绪曲线必须是「情绪词（状态）→情绪词（状态）→…」链式表达、钩子设计必须是能勾住点击的问题（以“？”结尾）。
3. 三套人设方案 A/B/C 必须有实质差异：核心特质、故事基调、标签、人设公式、优势、劣势六项各自不同，且每套都要写优势与劣势。
4. 每个故事的传播价值给星级（⭐1-5）+一句点评。
5. 输出必须是完整 JSON，所有字段全部覆盖，不留空、不写“待补充/略/TODO”之类的占位。"""


# ---------------------------------------------------------------------------
# 严格验证器：对照样例模板逐项检查，返回缺口清单
# ---------------------------------------------------------------------------

_PLACEHOLDERS = ("待补充", "待填写", "略。", "略）", "同上", "TODO", "TBD", "XXX", "（无）", "暂无", "占位")


def validate(report: dict) -> list[str]:
    """返回缺口/问题清单；空列表 = 通过。"""
    gaps: list[str] = []

    def need(cond, msg):
        if not cond:
            gaps.append(msg)

    if not isinstance(report, dict):
        return ["报告不是 JSON 对象"]

    for top in ("meta", "m1_positioning", "m2_persona", "m3_value", "m4_story"):
        need(report.get(top), f"缺少顶层模块 {top}")

    # ---- meta ----
    need((report.get("meta") or {}).get("name"), "meta.name 缺失（用户称呼/姓名）")
    need((report.get("meta") or {}).get("date"), "meta.date 缺失（整理日期）")

    # ---- 模块一 · 定位诊断 ----
    m1 = report.get("m1_positioning") or {}
    kws = m1.get("keywords") or []
    need(len(kws) == 7, f"模块一·核心关键词必须恰好 7 个（当前 {len(kws)} 个）")
    for i, k in enumerate(kws):
        need(k.get("name") and k.get("desc"), f"模块一·关键词 {i + 1} 缺名称或解读")
    f1 = m1.get("final") or {}
    need(all(f1.get(x) for x in ("name", "slogan", "strategy")), "模块一·最终定位缺「名称/定位语/三合一策略」")
    need(len(m1.get("market_opportunities") or []) >= 4, "模块一·市场机缘少于 4 条")
    risks = m1.get("risks") or []
    need(len(risks) >= 4, f"模块一·潜在风险少于 4 条（当前 {len(risks)} 条）——样例必含潜在风险，不可偷懒")

    # ---- 模块二 · 人设塑造 ----
    m2 = report.get("m2_persona") or {}
    opts = m2.get("options") or []
    need(len(opts) == 3, f"模块二·三套人设方案必须恰好 3 套（当前 {len(opts)} 套）——样例必含三套方案，不可偷懒")
    for o in opts:
        oid = o.get("id") if isinstance(o, dict) else "?"
        for k2 in ("id", "title", "traits", "story_tone", "tags", "formula", "pros", "cons"):
            need(isinstance(o, dict) and o.get(k2), f"模块二·方案 {oid} 缺「{k2}」")
    ids = [o.get("id") for o in opts if isinstance(o, dict)]
    need(sorted(ids) == ["A", "B", "C"], f"模块二·方案 id 必须是 A/B/C（当前 {ids}）")
    rec = m2.get("recommendation") or {}
    need(rec.get("chosen") in ("A", "B", "C"), "模块二·最终推荐必须指定方案 A/B/C")
    need(rec.get("title"), "模块二·最终推荐缺方案名")
    need(len(rec.get("reasons") or []) >= 4, f"模块二·推荐理由少于 4 条（当前 {len(rec.get('reasons') or [])} 条）")
    core = m2.get("core") or {}
    for k2 in ("traits", "story_tone", "tags", "quote", "image"):
        need(core.get(k2), f"模块二·核心人设要素缺「{k2}」")

    # ---- 模块三 · 价值主张提炼 ----
    m3 = report.get("m3_value") or {}
    dg = m3.get("diagnosis") or []
    need(len(dg) >= 2, "模块三·当前问题诊断少于 2 行")
    for i, row in enumerate(dg):
        for k3 in ("original", "problem", "suggestion"):
            need(isinstance(row, dict) and row.get(k3), f"模块三·问题诊断第 {i + 1} 行缺「{k3}」")
    f3 = m3.get("final") or {}
    need(all(f3.get(x) for x in ("core", "stance")), "模块三·最终价值主张缺「主张核心/主张立场」")
    sl = m3.get("slogan") or {}
    need(sl.get("text"), "模块三·一句话金句（推荐）缺失")
    need(len(sl.get("reasons") or []) >= 3, f"模块三·推荐金句理由少于 3 条（当前 {len(sl.get('reasons') or [])} 条）")
    alts = m3.get("slogan_alternatives") or []
    need(len(alts) >= 4, f"模块三·备选金句少于 4 行（当前 {len(alts)} 行）")
    for i, a in enumerate(alts):
        need(isinstance(a, dict) and a.get("scenario") and a.get("slogan"),
             f"模块三·备选金句第 {i + 1} 行缺「场景/金句」")
    si = m3.get("self_intro") or {}
    need(all(si.get(x) for x in ("original", "optimized")), "模块三·自我介绍优化缺「原版/优化版」")
    need(len(si.get("reasons") or []) >= 2, "模块三·自我介绍优化理由少于 2 条")
    mp = m3.get("monetization") or []
    need(len(mp) == 3, f"模块三·三条变现路径映射必须恰好 3 条（当前 {len(mp)} 条）")
    for i, row in enumerate(mp):
        for k3 in ("path", "position", "script"):
            need(isinstance(row, dict) and row.get(k3), f"模块三·变现路径第 {i + 1} 条缺「{k3}」")

    # ---- 模块四 · 故事资产挖掘 ----
    m4 = report.get("m4_story") or {}
    stories = m4.get("stories") or []
    need(len(stories) >= 5, f"模块四·故事库至少 5 个（当前 {len(stories)} 个）")
    for i, s in enumerate(stories):
        stitle = (s.get("title") if isinstance(s, dict) else "?") or f"第{i + 1}个"
        for k4 in ("title", "one_liner", "emotion_curve", "scenarios", "hook", "spread"):
            need(isinstance(s, dict) and s.get(k4), f"模块四·故事「{stitle}」缺「{k4}」")
        if isinstance(s, dict):
            hook = s.get("hook") or ""
            need("？" in hook or "?" in hook, f"模块四·故事「{stitle}」的钩子设计必须是一个以问号结尾的钩子问题")
            curve = s.get("emotion_curve") or ""
            need(curve.count("→") >= 2, f"模块四·故事「{stitle}」的情绪曲线必须是链式表达（至少 2 个 →）")
            spread = s.get("spread") or ""
            need(("⭐" in spread) or ("★" in spread) or ("星" in spread),
                 f"模块四·故事「{stitle}」的传播价值必须带星级")
    ms = m4.get("main_storyline") or {}
    need(ms.get("primary"), "模块四·推荐核心故事主线缺失")
    need(len(ms.get("reasons") or []) >= 4, f"模块四·故事主线组合理由少于 4 条（当前 {len(ms.get('reasons') or [])} 条）")
    opt = m4.get("optimization") or {}
    su = opt.get("slogan_upgrades") or []
    need(len(su) >= 2, "模块四·金句升级表少于 2 行")
    for i, row in enumerate(su):
        for k4 in ("type", "original", "optimized", "reason"):
            need(isinstance(row, dict) and row.get(k4), f"模块四·金句升级第 {i + 1} 行缺「{k4}」")
    es = opt.get("edge_strategy") or {}
    need(es.get("problem") and es.get("idea"), "模块四·争议点转化策略缺「原文问题/转化思路」")
    need(len(es.get("content") or []) >= 2, "模块四·争议点可发布内容少于 2 条")
    pd_ = opt.get("pit_deepdive") or {}
    need(len(pd_.get("directions") or []) >= 2, "模块四·踩坑故事深挖方向少于 2 条")
    need(pd_.get("quote") and pd_.get("series"), "模块四·踩坑故事深挖缺「金句提炼/系列延伸」")
    nar = opt.get("narrative") or {}
    need(nar.get("original") and nar.get("optimized"), "模块四·逆袭叙事框架缺「原叙事/优化叙事」")
    pri = m4.get("priority") or []
    need(len(pri) >= 4, f"模块四·执行优先级建议少于 4 行（当前 {len(pri)} 行）")
    for i, row in enumerate(pri):
        for k4 in ("p", "module", "task", "output"):
            need(isinstance(row, dict) and row.get(k4), f"模块四·执行优先级第 {i + 1} 行缺「{k4}」")
    need(m4.get("doc_status"), "模块四·文档状态缺失")

    # ---- 占位/空话检测 ----
    flat = json.dumps(report, ensure_ascii=False)
    for bad in _PLACEHOLDERS:
        need(bad not in flat, f"报告里出现占位文字「{bad}」——不允许留空，必须给出真实分析")

    return gaps


# ---------------------------------------------------------------------------
# LLM 调用与 JSON 解析
# ---------------------------------------------------------------------------

def _llm_chat(messages: list, max_tokens: int = MAX_TOKENS, temperature: float = 0.5) -> str:
    if config.LLM_MODE != "openai":
        raise RuntimeError("当前为演示模式（未配置 LLM_API_KEY），无法生成报告 PDF；请在 .env 里配置 Key")
    from openai import OpenAI

    kwargs = {"api_key": config.LLM_API_KEY, "timeout": 600.0}
    if config.LLM_BASE_URL:
        kwargs["base_url"] = config.LLM_BASE_URL
    client = OpenAI(**kwargs)

    last_err = None
    for use_json_mode in (True, False):
        try:
            params = dict(model=config.LLM_MODEL, messages=messages,
                          temperature=temperature, max_tokens=max_tokens)
            if use_json_mode:
                params["response_format"] = {"type": "json_object"}
            resp = client.chat.completions.create(**params)
            return resp.choices[0].message.content or ""
        except Exception as e:  # 网络/模式不支持等，退避一次
            last_err = e
    raise RuntimeError(f"报告生成调用 LLM 失败：{last_err}")


def _parse_json(text: str):
    """从 LLM 输出里提取第一个平衡的 JSON 对象。"""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _profile_for_generation(session_id: str) -> dict:
    p = state.get_profile(session_id)
    return {k: v for k, v in p.items() if not k.startswith("__") and v}


def _draft_messages(session_id: str, instruction: str = "", prev_draft: dict | None = None):
    profile = _profile_for_generation(session_id)
    msgs = [{"role": "system", "content": REPORT_SPEC}]
    parts = ["以下是已采集的用户信息（JSON）：", json.dumps(profile, ensure_ascii=False, indent=1)]
    if prev_draft:
        parts += ["", "以下是上一版已通过的完整报告 JSON（作为基底）：",
                  json.dumps(prev_draft, ensure_ascii=False)]
    if instruction:
        parts += ["", f"本次调整要求：{instruction}"]
    parts += ["", "请输出完整报告 JSON，所有模块与分析层缺一不可。"]
    msgs.append({"role": "user", "content": "\n".join(parts)})
    return msgs


def _revise_feedback(gaps: list[str]) -> str:
    listed = "\n".join(f"- {g}" for g in gaps)
    return (
        "你的上一版报告未通过模板校验，以下内容缺失或不达标：\n"
        f"{listed}\n\n"
        "请修改你的生成思路，重新输出【完整】JSON：\n"
        "1) 逐条补齐上述缺口，补出具体、可落地的真实分析，禁止占位与空话；\n"
        "2) 除修正缺口外，不得删减、不得改动已通过校验的其他内容；\n"
        "3) 若某条分析确实缺少采集原料，正文仍要给出你的专业分析判断，"
        "并把缺少的原始信息追加进 required_info；\n"
        "4) 只输出 JSON 本身。"
    )


# ---------------------------------------------------------------------------
# 生成循环：生成 → 严格校验 → 缺口反馈给 LLM 修订重跑（绝不手工补内容）
# ---------------------------------------------------------------------------

def _run_loop(session_id: str, instruction: str, prev_draft: dict | None,
              max_rounds: int, meta_prefix: str) -> tuple[dict | None, dict]:
    """通用循环：返回 (通过校验的报告或 None, 本轮元信息)。"""
    meta = {"status": meta_prefix + "generating", "round": 1, "gaps": [], "ts": time.time()}
    state.set_report(session_id, meta)

    msgs = _draft_messages(session_id, instruction, prev_draft)
    report = None
    gaps: list[str] = []

    for rnd in range(1, max_rounds + 1):
        meta = {"status": meta_prefix + "generating", "round": rnd,
                "rounds": rnd, "gaps": [], "phase": f"第 {rnd} 轮生成中…", "ts": time.time()}
        state.set_report(session_id, meta)
        try:
            raw = _llm_chat(msgs)
        except RuntimeError as e:
            meta.update(status="failed", error=str(e))
            state.set_report(session_id, meta)
            return None, meta

        report = _parse_json(raw)
        if report is None:
            gaps = ["LLM 输出不是合法 JSON"]
            msgs.append({"role": "assistant", "content": raw})
            msgs.append({"role": "user", "content": "你刚才的输出无法解析为 JSON。请只输出一个完整、合法的 JSON 对象，不要任何其他文字。"})
            continue

        gaps = validate(report)
        if not gaps:
            meta = {"status": meta_prefix + "validated", "round": rnd, "rounds": rnd,
                    "gaps": [], "phase": "模板校验通过"}
            state.set_report(session_id, meta)
            return report, meta

        meta = {"status": meta_prefix + "generating", "round": rnd, "rounds": rnd,
                "gaps": list(gaps), "phase": f"第 {rnd} 轮校验发现 {len(gaps)} 处缺口，让 LLM 修订重跑",
                "ts": time.time()}
        state.set_report(session_id, meta)
        msgs.append({"role": "assistant", "content": raw})
        msgs.append({"role": "user", "content": _revise_feedback(gaps)})

    meta = {"status": "incomplete", "rounds": max_rounds, "gaps": list(gaps),
            "phase": f"修订 {max_rounds} 轮后仍有缺口", "json": report}
    state.set_report(session_id, meta)
    return None, meta


def generate_draft(session_id: str, instruction: str = "") -> dict:
    """生成初稿（含自动修订循环），成功后渲染 PDF 并写入状态。"""
    profile = _profile_for_generation(session_id)
    if not profile:
        return {"ok": False, "status": "no_info",
                "error": "内部信息表还是空的，需要先聊出基础信息（至少职业/经历/方向/性格）再生成报告"}

    report, meta = _run_loop(session_id, instruction, prev_draft=None,
                             max_rounds=MAX_ROUNDS, meta_prefix="draft_")
    if report is None:
        return {"ok": False, "status": meta.get("status", "failed"),
                "error": meta.get("error"),
                "gaps": meta.get("gaps", []),
                "note": "报告未通过模板校验。请根据 gaps 判断：缺分析就带上缺口说明再调 generate_report；缺原始信息就继续追问用户。"}

    files = _persist(session_id, _normalize_meta(report), suffix="初稿")
    meta.update(status="draft_ready", files=files, gaps=[], phase="初稿已生成并通过模板校验")
    meta["_json"] = report
    state.set_report(session_id, meta)

    out = {"ok": True, "status": "draft_ready", "rounds": meta.get("rounds"),
           "title": report.get("meta", {}).get("name"),
           "files": {k: v for k, v in files.items()},
           "options": [{"id": o["id"], "title": o["title"], "traits": o["traits"]}
                       for o in (report.get("m2_persona", {}).get("options") or [])],
           "recommended": (report.get("m2_persona", {}).get("recommendation") or {}).get("chosen"),
           "required_info": report.get("required_info") or [],
           "note": "请务必在对话里把三套人设方案展示给用户，让用户选择或提修改意见（这是必须的交互环节）。"}
    return out


def finalize(session_id: str, chosen: str) -> dict:
    """用户选定方案后出定稿：只让 LLM 改写「最终推荐」，其余保留原稿。"""
    if chosen not in ("A", "B", "C"):
        return {"ok": False, "error": "chosen 必须是 A / B / C"}
    prev = state.get_report_json(session_id)
    if not prev:
        return {"ok": False, "error": "还没有已生成的初稿，请先调用 generate_report"}

    option = next((o for o in (prev.get("m2_persona", {}).get("options") or [])
                   if o.get("id") == chosen), None)
    title = (option or {}).get("title", f"方案{chosen}")
    instruction = (
        f"用户最终选择了方案{chosen}《{title}》。请只做以下两处修改后输出完整 JSON："
        f"(1) m2_persona.recommendation 改为：chosen=\"{chosen}\"、title=\"{title}\"，"
        f"reasons 针对《{title}》重写至少 4 条；(2) m4_story.doc_status 末尾追加："
        f"人设方案已按用户选择确定为方案{chosen}《{title}》。"
        f"其余所有字段逐字保留上一版内容，不得改动、不得删减、不得新增。"
    )
    report, meta = _run_loop(session_id, instruction, prev_draft=prev,
                             max_rounds=MAX_FINAL_ROUNDS, meta_prefix="final_")
    if report is None:
        return {"ok": False, "status": "incomplete", "gaps": meta.get("gaps", [])}

    files = _persist(session_id, _normalize_meta(report), suffix="定稿")
    meta.update(status="final", files=files, chosen=chosen, chosen_title=title,
                phase="定稿已生成")
    meta["_json"] = report
    state.set_report(session_id, meta)
    return {"ok": True, "status": "final", "chosen": chosen, "chosen_title": title,
            "files": {k: v for k, v in files.items()}}


def revise(session_id: str, feedback: str) -> dict:
    """用户对报告提出修改意见：以原稿为基底，让 LLM 按反馈改后重新校验。"""
    prev = state.get_report_json(session_id)
    if not prev:
        return {"ok": False, "error": "还没有已生成的报告，请先调用 generate_report"}
    instruction = f"用户对上一版报告提出以下意见：{feedback}。请据此修改相关内容，其余保留上一版，输出完整 JSON。"
    report, meta = _run_loop(session_id, instruction, prev_draft=prev,
                             max_rounds=MAX_ROUNDS, meta_prefix="draft_")
    if report is None:
        return {"ok": False, "status": "incomplete", "gaps": meta.get("gaps", [])}
    files = _persist(session_id, _normalize_meta(report), suffix="初稿")
    meta.update(status="draft_ready", files=files, phase="已按用户意见修订")
    meta["_json"] = report
    state.set_report(session_id, meta)
    return {"ok": True, "status": "draft_ready", "files": {k: v for k, v in files.items()}}


# ---------------------------------------------------------------------------
# 落盘：JSON + Markdown + HTML → PDF
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "", str(name or "")) or "未命名"
    return s[:20]


def _normalize_meta(report: dict) -> dict:
    """元数据服务端定：整理日期 = 今天，参考框架固定。分析内容不动。"""
    if not isinstance(report, dict):
        return report
    meta = report.setdefault("meta", {})
    meta["date"] = datetime.date.today().isoformat()
    meta["framework"] = meta.get("framework") or "AI做IP十二模块框架"
    return report


def _persist(session_id: str, report: dict, suffix: str) -> dict:
    name = _safe_name((report.get("meta") or {}).get("name"))
    base = f"{name}_IP人设定位_{suffix}"
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    json_path = os.path.join(OUTPUT_DIR, f"{name}_report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    md_path = os.path.join(OUTPUT_DIR, f"{base}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))

    pdf_path = os.path.join(OUTPUT_DIR, f"{base}.pdf")
    pdf_ok, err = _render_pdf(report, pdf_path)

    return {
        "pdf": os.path.basename(pdf_path) if pdf_ok else None,
        "md": os.path.basename(md_path),
        "json": os.path.basename(json_path),
        "pdf_error": err,
    }


# ---------------------------------------------------------------------------
# Markdown / HTML 渲染（结构与样例逐层对应）
# ---------------------------------------------------------------------------

def _li(items):
    return "\n".join(f"{i + 1}. {html.escape(str(x))}" for i, x in enumerate(items))


def _table(rows):
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in rows[0])
    body = ""
    for r in rows[1:]:
        body += "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>"
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _pairs_table(pairs):
    rows = [("项目", "内容")] + [(k, v) for k, v in pairs]
    return _table(rows)


def render_markdown(report: dict) -> str:
    meta = report.get("meta") or {}
    m1 = report.get("m1_positioning") or {}
    m2 = report.get("m2_persona") or {}
    m3 = report.get("m3_value") or {}
    m4 = report.get("m4_story") or {}
    L = []
    A = L.append

    A(f"# {meta.get('name', '')}IP人设定位｜模块1-4")
    A(f"整理日期：{meta.get('date', '')}")
    A(f"参考框架：{meta.get('framework', 'AI做IP十二模块框架')}")
    A("")

    A("## 模块一｜定位诊断")
    A("### 核心关键词（7个）")
    for i, k in enumerate(m1.get("keywords", [])):
        A(f"{i + 1}. {k.get('name', '')} — {k.get('desc', '')}")
    A("")
    A("### 最终定位")
    A(f"- 名称：{m1.get('final', {}).get('name', '')}")
    A(f"- 定位语：{m1.get('final', {}).get('slogan', '')}")
    A(f"- 三合一策略：{m1.get('final', {}).get('strategy', '')}")
    A("")
    A("### 市场机缘")
    A(_li(m1.get("market_opportunities", [])))
    A("")
    A("### 潜在风险")
    A(_li(m1.get("risks", [])))
    A("")

    A("## 模块二｜人设塑造")
    A("### 三套人设方案")
    for o in m2.get("options", []):
        A(f"**方案{o.get('id', '')}：{o.get('title', '')}**")
        A(f"- 核心特质：{o.get('traits', '')}")
        A(f"- 故事基调：{o.get('story_tone', '')}")
        A(f"- 标签：{o.get('tags', '')}")
        A(f"- 人设公式：{o.get('formula', '')}")
        A(f"- 优势：{o.get('pros', '')}")
        A(f"- 劣势：{o.get('cons', '')}")
        A("")
    rec = m2.get("recommendation") or {}
    A(f"🏆 最终推荐：方案{rec.get('chosen', '')}「{rec.get('title', '')}」")
    A("推荐理由：")
    A(_li(rec.get("reasons", [])))
    A("")
    core = m2.get("core") or {}
    A("### 核心人设要素")
    for k, label in (("traits", "核心特质"), ("story_tone", "故事基调"), ("tags", "核心标签"),
                     ("quote", "人设金句"), ("image", "对外形象")):
        A(f"- {label}：{core.get(k, '')}")
    A("")

    A("## 模块三｜价值主张提炼")
    A("### 当前问题诊断")
    for i, row in enumerate(m3.get("diagnosis", [])):
        A(f"{i + 1}. 原金句「{row.get('original', '')}」→ 问题：{row.get('problem', '')} → 建议：{row.get('suggestion', '')}")
    A("")
    A("### 最终价值主张")
    A(f"- 主张核心：{m3.get('final', {}).get('core', '')}")
    A(f"- 主张立场：{m3.get('final', {}).get('stance', '')}")
    A("")
    sl = m3.get("slogan") or {}
    A(f"🏆 一句话金句（推荐）：「{sl.get('text', '')}」")
    A("理由：")
    A(_li(sl.get("reasons", [])))
    A("")
    A("### 备选金句")
    for a in m3.get("slogan_alternatives", []):
        A(f"- {a.get('scenario', '')}：「{a.get('slogan', '')}」")
    A("")
    si = m3.get("self_intro") or {}
    A("### 一句话自我介绍（优化版）")
    A(f"- 原版：「{si.get('original', '')}」")
    A(f"- 优化版：「{si.get('optimized', '')}」")
    A("优化理由：")
    A(_li(si.get("reasons", [])))
    A("")
    A("### 三条变现路径映射")
    for i, row in enumerate(m3.get("monetization", [])):
        A(f"{i + 1}. {row.get('path', '')}｜{row.get('position', '')}：「{row.get('script', '')}」")
    A("")

    A("## 模块四｜故事资产挖掘")
    A("### 故事库")
    for i, s in enumerate(m4.get("stories", [])):
        A(f"**故事{i + 1}：{s.get('title', '')}**")
        A(f"- 一句话：{s.get('one_liner', '')}")
        A(f"- 情绪曲线：{s.get('emotion_curve', '')}")
        A(f"- 适用场景：{s.get('scenarios', '')}")
        A(f"- 钩子设计：{s.get('hook', '')}")
        A(f"- 传播价值：{s.get('spread', '')}")
        A("")
    ms = m4.get("main_storyline") or {}
    A("### 推荐核心故事主线")
    A(f"首选故事线：{ms.get('primary', '')}")
    A("组合理由：")
    A(_li(ms.get("reasons", [])))
    A("")
    opt = m4.get("optimization") or {}
    A("### 优化建议汇总")
    A("1. 金句升级")
    for row in opt.get("slogan_upgrades", []):
        A(f"- {row.get('type', '')}：「{row.get('original', '')}」→「{row.get('optimized', '')}」（{row.get('reason', '')}）")
    es = opt.get("edge_strategy") or {}
    A(f"2. 争议点转化策略：{es.get('problem', '')} → {es.get('idea', '')}")
    for c in es.get("content", []):
        A(f"   - {c}")
    pd_ = opt.get("pit_deepdive") or {}
    A("3. 踩坑故事深挖")
    A(f"   建议方向：{'；'.join(pd_.get('directions', []))}")
    A(f"   金句提炼：{pd_.get('quote', '')}")
    A(f"   系列延伸：{pd_.get('series', '')}")
    nar = opt.get("narrative") or {}
    A("4. 逆袭叙事框架")
    A(f"   原叙事：{nar.get('original', '')}")
    A(f"   优化叙事框架：{nar.get('optimized', '')}")
    A("")
    A("### 执行优先级建议")
    for row in m4.get("priority", []):
        A(f"- {row.get('p', '')}｜{row.get('module', '')}：{row.get('task', '')} → 预计产出：{row.get('output', '')}")
    A("")
    A(f"文档状态：{m4.get('doc_status', '')}")
    A("")
    ri = report.get("required_info") or []
    if ri:
        A("### 待补充采集（required_info）")
        A(_li(ri))
    return "\n".join(L)


_HTML_CSS = """
body{font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
     font-size:12.5px;line-height:1.65;color:#1a1a1a;margin:36px 44px;}
h1{font-size:21px;text-align:center;margin:0 0 4px;}
.meta{text-align:center;color:#666;font-size:12px;margin-bottom:22px;}
h2{font-size:16px;margin:26px 0 10px;padding-left:9px;border-left:4px solid #2563eb;
   page-break-after:avoid;}
h3{font-size:13.5px;margin:14px 0 6px;page-break-after:avoid;}
p{margin:4px 0;}
ol,ul{margin:4px 0 8px;padding-left:22px;}
li{margin:3px 0;}
table{border-collapse:collapse;width:100%;margin:6px 0 12px;page-break-inside:avoid;}
th,td{border:1px solid #444;padding:5px 8px;text-align:left;vertical-align:top;font-size:12px;}
th{background:#f1f5f9;}
.tag{color:#2563eb;font-weight:600;}
.story{border:1px solid #e2e8f0;border-radius:6px;padding:8px 12px;margin:8px 0;
       page-break-inside:avoid;}
.win{font-weight:700;color:#b45309;}
.note{color:#666;font-size:11px;}
"""


def _render_html(report: dict, chosen_note: bool = False) -> str:
    meta = report.get("meta") or {}
    m1 = report.get("m1_positioning") or {}
    m2 = report.get("m2_persona") or {}
    m3 = report.get("m3_value") or {}
    m4 = report.get("m4_story") or {}
    E = html.escape
    parts = []
    A = parts.append

    A(f"<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
      f"<style>{_HTML_CSS}</style></head><body>")
    A(f"<h1>{E(meta.get('name', ''))}IP人设定位｜模块1-4</h1>")
    A(f"<div class=\"meta\">整理日期：{E(meta.get('date', ''))}　·　参考框架：{E(meta.get('framework', ''))}</div>")

    # 模块一
    A("<h2>模块一｜定位诊断</h2>")
    A("<h3>核心关键词（7个）</h3><ol>")
    for k in m1.get("keywords", []):
        A(f"<li><b>{E(k.get('name', ''))}</b> — {E(k.get('desc', ''))}</li>")
    A("</ol>")
    f1 = m1.get("final") or {}
    A("<h3>最终定位</h3><ul>")
    A(f"<li><b>名称</b>：{E(f1.get('name', ''))}</li>")
    A(f"<li><b>定位语</b>：{E(f1.get('slogan', ''))}</li>")
    A(f"<li><b>三合一策略</b>：{E(f1.get('strategy', ''))}</li>")
    A("</ul>")
    A("<h3>市场机缘</h3><ol>")
    for x in m1.get("market_opportunities", []):
        A(f"<li>{E(x)}</li>")
    A("</ol>")
    A("<h3>潜在风险</h3><ol>")
    for x in m1.get("risks", []):
        A(f"<li>{E(x)}</li>")
    A("</ol>")

    # 模块二
    A("<h2>模块二｜人设塑造</h2>")
    A("<h3>三套人设方案</h3>")
    rec = m2.get("recommendation") or {}
    chosen_id = rec.get("chosen") if chosen_note else None
    for o in m2.get("options", []):
        oid = o.get("id", "")
        win = "　🏆 用户已选定" if (chosen_note and oid == chosen_id) else ""
        A(f"<div class=\"story\"><p><b>方案{oid}：{E(o.get('title', ''))}</b>{win}</p><ul>")
        for k2, lab in (("traits", "核心特质"), ("story_tone", "故事基调"), ("tags", "标签"),
                        ("formula", "人设公式")):
            A(f"<li><b>{lab}</b>：{E(o.get(k2, ''))}</li>")
        A(f"<li><b>优势</b>：{E(o.get('pros', ''))}</li>")
        A(f"<li><b>劣势</b>：{E(o.get('cons', ''))}</li>")
        A("</ul></div>")
    A(f"<p class=\"win\">🏆 最终推荐：方案{rec.get('chosen', '')}「{E(rec.get('title', ''))}」</p>")
    A("<p>推荐理由：</p><ol>")
    for x in rec.get("reasons", []):
        A(f"<li>{E(x)}</li>")
    A("</ol>")
    core = m2.get("core") or {}
    A("<h3>核心人设要素</h3>")
    A(_pairs_table([("核心特质", core.get("traits", "")), ("故事基调", core.get("story_tone", "")),
                    ("核心标签", core.get("tags", "")), ("人设金句", core.get("quote", "")),
                    ("对外形象", core.get("image", ""))]))

    # 模块三
    A("<h2>模块三｜价值主张提炼</h2>")
    A("<h3>当前问题诊断</h3>")
    A(_table([("原金句", "问题", "建议")] +
             [[row.get("original", ""), row.get("problem", ""), row.get("suggestion", "")]
              for row in m3.get("diagnosis", [])]))
    f3 = m3.get("final") or {}
    A("<h3>最终价值主张</h3><ul>")
    A(f"<li><b>主张核心</b>：{E(f3.get('core', ''))}</li>")
    A(f"<li><b>主张立场</b>：{E(f3.get('stance', ''))}</li>")
    A("</ul>")
    sl = m3.get("slogan") or {}
    A(f"<p class=\"win\">🏆 一句话金句（推荐）：「{E(sl.get('text', ''))}」</p>")
    A("<p>理由：</p><ol>")
    for x in sl.get("reasons", []):
        A(f"<li>{E(x)}</li>")
    A("</ol>")
    A("<h3>备选金句（可多场景使用）</h3>")
    A(_table([("场景", "金句")] +
             [[a.get("scenario", ""), a.get("slogan", "")] for a in m3.get("slogan_alternatives", [])]))
    si = m3.get("self_intro") or {}
    A("<h3>一句话自我介绍（优化版）</h3><ul>")
    A(f"<li><b>原版</b>：「{E(si.get('original', ''))}」</li>")
    A(f"<li><b>优化版</b>：「{E(si.get('optimized', ''))}」</li>")
    A("</ul><p>优化理由：</p><ol>")
    for x in si.get("reasons", []):
        A(f"<li>{E(x)}</li>")
    A("</ol>")
    A("<h3>三条变现路径映射</h3>")
    A(_table([("路径", "定位", "承接话术")] +
             [[row.get("path", ""), row.get("position", ""), row.get("script", "")]
              for row in m3.get("monetization", [])]))

    # 模块四
    A("<h2>模块四｜故事资产挖掘</h2>")
    A("<h3>故事库</h3>")
    for i, s in enumerate(m4.get("stories", [])):
        A(f"<div class=\"story\"><p><b>故事{i + 1}：{E(s.get('title', ''))}</b></p>")
        A(_pairs_table([("一句话", s.get("one_liner", "")), ("情绪曲线", s.get("emotion_curve", "")),
                        ("适用场景", s.get("scenarios", "")), ("钩子设计", s.get("hook", "")),
                        ("传播价值", s.get("spread", ""))]))
        A("</div>")
    ms = m4.get("main_storyline") or {}
    A("<h3>推荐核心故事主线</h3>")
    A(f"<p><b>首选故事线</b>：{E(ms.get('primary', ''))}</p>")
    A("<p>组合理由：</p><ol>")
    for x in ms.get("reasons", []):
        A(f"<li>{E(x)}</li>")
    A("</ol>")
    opt = m4.get("optimization") or {}
    A("<h3>优化建议汇总</h3>")
    A("<p><b>1. 金句升级</b></p>")
    A(_table([("类型", "原文", "优化版", "理由")] +
             [[row.get("type", ""), row.get("original", ""), row.get("optimized", ""), row.get("reason", "")]
              for row in opt.get("slogan_upgrades", [])]))
    es = opt.get("edge_strategy") or {}
    A(f"<p><b>2. 争议点转化策略</b>：{E(es.get('problem', ''))} → {E(es.get('idea', ''))}</p>")
    A("<p>可发布内容：</p><ul>")
    for x in es.get("content", []):
        A(f"<li>{E(x)}</li>")
    A("</ul>")
    pd_ = opt.get("pit_deepdive") or {}
    A("<p><b>3. 踩坑故事深挖</b></p><ul>")
    A(f"<li>建议方向：{'；'.join(E(x) for x in pd_.get('directions', []))}</li>")
    A(f"<li>金句提炼：{E(pd_.get('quote', ''))}</li>")
    A(f"<li>系列延伸：{E(pd_.get('series', ''))}</li>")
    A("</ul>")
    nar = opt.get("narrative") or {}
    A("<p><b>4. 逆袭叙事框架</b></p><ul>")
    A(f"<li>原叙事：{E(nar.get('original', ''))}</li>")
    A(f"<li>优化叙事框架：{E(nar.get('optimized', ''))}</li>")
    A("</ul>")
    A("<h3>执行优先级建议</h3>")
    A(_table([("优先级", "模块", "关键任务", "预计产出")] +
             [[row.get("p", ""), row.get("module", ""), row.get("task", ""), row.get("output", "")]
              for row in m4.get("priority", [])]))
    A(f"<p><b>文档状态</b>：{E(m4.get('doc_status', ''))}</p>")

    A("</body></html>")
    return "".join(parts)


def chrome_print(html_text: str, pdf_path: str) -> tuple[bool, str | None]:
    """HTML → headless Chrome 打印 PDF（公开：模块5/6 复用）。"""
    if not CHROME_BIN:
        return False, "未找到 Chrome，无法渲染 PDF"
    try:
        fd, html_path = tempfile.mkstemp(suffix=".html", prefix="hq_report_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html_text)
        cmd = [CHROME_BIN, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--no-pdf-header-footer", "--virtual-time-budget=10000",
               f"--print-to-pdf={pdf_path}", f"file://{html_path}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        ok = proc.returncode == 0 and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 1000
        return ok, (None if ok else (proc.stderr or "")[:400])
    except subprocess.TimeoutExpired:
        return False, "Chrome 渲染超时"
    finally:
        try:
            os.remove(html_path)
        except OSError:
            pass


def _render_pdf(report: dict, pdf_path: str) -> tuple[bool, str | None]:
    """HTML → headless Chrome 打印 PDF。"""
    return chrome_print(_render_html(report), pdf_path)
