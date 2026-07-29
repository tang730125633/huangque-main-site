#!/usr/bin/env python3
"""
Hermes 12模块IP孵化教练 v3 — 诊断→交付闭环
新增：模块完成自动生成交付物 / GEO分析 / Humanizer / 一键导出
"""
import html, json, os, pathlib, re, shutil, subprocess, tempfile, uuid
from datetime import datetime
from flask import Flask, request, jsonify, Response, render_template, send_file
import requests
from runtime_paths import DATA_DIR, ROOT_DIR
from werkzeug.middleware.proxy_fix import ProxyFix

# ── 配置 ──
API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.environ.get("HERMES_MODEL", "gpt-4o")
PORT = 3000
PROJECT_DIR = ROOT_DIR
CONVOS_DIR = DATA_DIR / "conversations"
REPORTS_DIR = DATA_DIR / "reports"
DELIVERABLES_DIR = DATA_DIR / "deliverables"
FOUNDATION_REPORTS_DIR = DATA_DIR / "foundation_reports"
CONVERSATION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_prefix=1)
from routes_extra import register_v6_routes
register_v6_routes(app)
if os.environ.get("HERMES_ENABLE_INTERNAL_TOOLS", "1") == "1":
    try:
        from agnes_routes import register_agnes_routes
        register_agnes_routes(app, PROJECT_DIR, DATA_DIR)
    except Exception as _agnes_error:
        print('agnes routes disabled', _agnes_error)

    try:
        from team_workbench_routes import register_team_workbench_routes
        register_team_workbench_routes(app, PROJECT_DIR, DATA_DIR)
    except Exception as _team_workbench_error:
        print('team workbench routes disabled', _team_workbench_error)

from routes_extra import *  # v6 route extensions

# ── 12 模块定义 ──
MODULES = [
    {"id": 1,  "name": "定位诊断",   "icon": "🎯", "desc": "找到你是谁，为谁而来"},
    {"id": 2,  "name": "人设塑造",   "icon": "🎭", "desc": "打造让人记住的人格"},
    {"id": 3,  "name": "价值主张",   "icon": "💎", "desc": "提炼不可替代的核心价值"},
    {"id": 4,  "name": "故事资产",   "icon": "📖", "desc": "挖掘能打动人心的故事"},
    {"id": 5,  "name": "选题策划",   "icon": "📋", "desc": "构建持续输出的选题库"},
    {"id": 6,  "name": "文案口播",   "icon": "✍️", "desc": "写出让人停下来的文案"},
    {"id": 7,  "name": "形象设计",   "icon": "🖼️", "desc": "设计一眼记住的视觉IP"},
    {"id": 8,  "name": "脚本分镜",   "icon": "🎬", "desc": "把想法变成可拍的脚本"},
    {"id": 9,  "name": "私域矩阵",   "icon": "🔗", "desc": "搭建自动运转的私域系统"},
    {"id": 10, "name": "朋友圈运营", "icon": "💬", "desc": "让朋友圈变成成交阵地"},
    {"id": 11, "name": "销售策略",   "icon": "💰", "desc": "从信任到成交的完整链路"},
    {"id": 12, "name": "公众号变现", "icon": "📝", "desc": "长内容到持续变现的闭环"},
]

MODULE_REPORT_TYPES = {
    1: "定位诊断报告", 2: "人设画像报告", 3: "价值主张报告",
    4: "故事资产清单", 5: "选题策划方案", 6: "文案模板集",
    7: "视觉IP指南", 8: "分镜脚本模板", 9: "私域矩阵方案",
    10: "朋友圈运营手册", 11: "销售策略方案", 12: "公众号变现方案",
}

# ── 模块完成 → 自动交付物映射 ──
MODULE_DELIVERABLES = {
    6: {  # 文案口播 → 3种文案
        "title": "📝 你的专属文案包",
        "types": [
            {"name": "朋友圈文案 (3条)", "prompt": "基于对话内容，为学员写3条朋友圈文案。要求：每条50-150字，第一句必须用痛点/悬念/反常识抓住注意力，口语化，带emoji，适合美业/直销人群。直接输出文案，不要说明。"},
            {"name": "短视频口播脚本 (2条)", "prompt": "基于对话内容，为学员写2条短视频口播脚本。要求：前3秒制造悬念或痛点，中间给出观点/方法，结尾行动号召。标注[停顿]和[重音]。直接输出脚本。"},
            {"name": "私信激活话术 (3条)", "prompt": "基于对话内容，为学员写3条微信私信话术。要求：针对不同客户类型（新加好友/见过面但没成交/老客户激活），每条50字以内，让对方主动回复。直接输出话术。"},
        ]
    },
    7: {  # 形象设计 → 视觉方案
        "title": "🎨 你的视觉IP方案",
        "types": [
            {"name": "配色方案", "prompt": "基于学员的定位和人设，推荐3组配色方案。每组包含：主色+辅色+点缀色的HEX色值，以及这组颜色传达的情绪和适合的行业。直接输出，Markdown格式。"},
            {"name": "头像/封面建议", "prompt": "基于学员的定位，给出3个微信头像设计方向和3个抖音/视频号封面模板建议。每个方向包含：画面元素、构图方式、字体风格。直接输出。"},
            {"name": "视觉统一规范", "prompt": "为学员制定一套视觉IP规范：推荐字体(1款标题+1款正文)、滤镜风格、LOGO设计建议、朋友圈配图风格。简洁实用，直接输出。"},
        ]
    },
    5: {  # 选题策划 → 选题日历
        "title": "📅 你的30天选题日历",
        "types": [
            {"name": "7天内容排期", "prompt": "基于学员的定位和当前诊断信息，生成未来7天的内容日历。每天包含：选题标题、内容类型(引流/信任/成交)、一句话钩子、发布平台建议。直接输出表格。"},
        ]
    },
    10: {  # 朋友圈运营 → 朋友圈排期
        "title": "💬 你的朋友圈7天排期",
        "types": [
            {"name": "7天朋友圈排期表", "prompt": "为学员规划未来7天每天3条朋友圈的内容方向。早中晚各一条，类型覆盖：生活展示(30%)、专业输出(40%)、成交引导(30%)。每条给主题和一句话内容方向。直接输出。"},
        ]
    },
}

def load_coach_prompt():
    path = PROJECT_DIR / "prompt.md"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    idx = text.find("## System Prompt")
    if idx == -1:
        return text
    rest = text[idx:]
    positions = []
    pos = -1
    while True:
        pos = rest.find("```", pos + 1)
        if pos == -1: break
        positions.append(pos)
    if len(positions) < 2: return ""
    return rest[positions[0]+3:positions[-1]].strip("\n").strip()

COACH_PROMPT_BASE = load_coach_prompt()

def build_system_prompt(convo_id):
    convo = load_conversation(convo_id)
    state = convo.get("coach_state", {"ip_profile": {}, "current_module": 1, "completed_modules": [], "module_step": 0})
    cm = state["current_module"]
    mod = MODULES[cm - 1] if 1 <= cm <= 12 else MODULES[0]
    done = state.get("completed_modules", [])
    profile_summary = json.dumps(state.get("ip_profile", {}), ensure_ascii=False)[:300]

    module_protocol = f"""
## 当前模块：{mod['id']}. {mod['name']} {mod['icon']}

**你的任务**：严格按以下步骤推进 {mod['name']} 的诊断。每完成一步，等待学员确认后再进入下一步。
**禁止**：跳过步骤、一次给多步方案、泛泛而谈不追问。

**已采集信息**：{profile_summary if profile_summary != '{}' else '尚未采集'}

**核心原则**：
- 信息不够就追问，宁可多问一轮也不瞎猜
- 每一步给学员具体的选择或确认点，不要开放式"你觉得呢"
- 用学员已提供的信息来回溯，让他感觉你在认真听
"""
    completed_summary = ""
    if done:
        done_names = [f"{m}. {MODULES[m-1]['name']}" for m in sorted(done)]
        completed_summary = f"\n**已完成模块**：{', '.join(done_names)}\n请勿重复诊断这些模块的内容。\n"

    state_block = f"""## 状态追踪
- current_module: {cm}（{mod['name']}）
- module_step: {state.get('module_step', 0)}
- completed_modules: {done}
**请从模块 {cm} 的第 {state.get('module_step', 0) + 1} 步开始执行。按诊断协议一步步来，不要跳。**
"""
    prompt = COACH_PROMPT_BASE or "你是大鹏的 IP 孵化教练。"
    prompt = re.sub(r'# 状态追踪协议.*?(?=# |---|\Z)', '', prompt, flags=re.DOTALL)
    prompt = re.sub(r'CURRENT_STATE:.*?(?=\n\n|\Z)', '', prompt, flags=re.DOTALL)
    return prompt + "\n\n" + module_protocol + completed_summary + "\n" + state_block

# ── 对话管理 ──
CONVOS_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
DELIVERABLES_DIR.mkdir(parents=True, exist_ok=True)


class InvalidConversationId(ValueError):
    pass


def conversation_path(convo_id):
    if not isinstance(convo_id, str) or not CONVERSATION_ID_RE.fullmatch(convo_id):
        raise InvalidConversationId("invalid conversation id")
    path = (CONVOS_DIR / f"{convo_id}.json").resolve()
    if path.parent != CONVOS_DIR.resolve():
        raise InvalidConversationId("invalid conversation id")
    return path


@app.errorhandler(InvalidConversationId)
def invalid_conversation_id(error):
    return jsonify({"ok": False, "error": str(error)}), 400

def load_conversation(convo_id):
    path = conversation_path(convo_id)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"id": convo_id, "title": "新诊断", "messages": [],
            "coach_state": {"ip_profile": {}, "current_module": 1, "completed_modules": [], "module_step": 0},
            "reports": {}, "deliverables": {}, "updated": ""}

def save_conversation(convo_id, data):
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    conversation_path(convo_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def list_convos():
    convos = []
    if CONVOS_DIR.exists():
        for f in sorted(CONVOS_DIR.glob("*.json"), reverse=True):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                cs = d.get("coach_state", {})
                convos.append({"id": f.stem, "title": d.get("title", "新诊断"),
                    "updated": d.get("updated", ""),
                    "message_count": len(d.get("messages", [])),
                    "current_module": cs.get("current_module", 1),
                    "completed_modules": cs.get("completed_modules", []),
                    "report_count": len(d.get("reports", {})),
                    "deliverable_count": len(d.get("deliverables", {}))})
            except: pass
    return convos

def parse_coach_state_updates(ai_response, current_state):
    text = ai_response
    updated_state = dict(current_state)
    updated_state["completed_modules"] = list(current_state.get("completed_modules", []))
    for m in MODULES:
        mid = m["id"]
        patterns = [f"模块 {mid} 完成", f"模块{mid} 完成", f"✅ 模块 {mid}", f"模块 {mid} ✅"]
        if any(p in text for p in patterns):
            if mid not in updated_state["completed_modules"]:
                updated_state["completed_modules"].append(mid)
            if updated_state["current_module"] == mid:
                if mid == 4:
                    updated_state["foundation_report"] = {"status": "generating"}
                else:
                    updated_state["current_module"] = mid + 1
                updated_state["module_step"] = 0
    if "全部完成" in text or "结业" in text:
        updated_state["completed_modules"] = list(range(1, 13))
    transition_match = re.search(
        r'(?:接下来(?:是|进入)?|(?:直接)?进入(?:到)?|开始(?:进入)?|切换(?:到|至)?)\s*第?\s*模块\s*(\d+)',
        text,
    )
    if transition_match:
        target = int(transition_match.group(1))
        current = updated_state.get("current_module", 1)
        # The coach has visibly started the next module. Keep the sidebar in
        # sync, but only accept the normal one-module forward transition.
        if target == current + 1 and target <= 12:
            if current not in updated_state["completed_modules"]:
                updated_state["completed_modules"].append(current)
            updated_state["current_module"] = target
            updated_state["module_step"] = 0
    return updated_state

def _foundation_html(markdown):
    rows = []
    source_rows = str(markdown or "").splitlines()
    if source_rows and source_rows[0].strip().startswith("# "):
        source_rows = source_rows[1:]
    table_rows = []

    def flush_table():
        nonlocal table_rows
        if not table_rows:
            return
        cells = [[html.escape(cell.strip()) for cell in row.strip().strip("|").split("|")] for row in table_rows]
        header, *body_rows = cells
        if body_rows and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in body_rows[0]):
            body_rows = body_rows[1:]
        rows.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (
            "".join("<th>%s</th>" % cell for cell in header),
            "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % cell for cell in row) for row in body_rows),
        ))
        table_rows = []

    for raw in source_rows:
        if raw.strip().startswith("|") and raw.strip().endswith("|"):
            table_rows.append(raw)
            continue
        flush_table()
        raw_line = raw.strip()
        if raw_line.startswith("> "):
            rows.append("<blockquote>%s</blockquote>" % html.escape(raw_line[2:]))
            continue
        line = html.escape(raw_line)
        if not line:
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        if line.startswith("#### "):
            rows.append("<h4>%s</h4>" % line[5:])
        elif line.startswith("### "):
            rows.append("<h3>%s</h3>" % line[4:])
        elif line.startswith("## "):
            rows.append("<h2>%s</h2>" % line[3:])
        elif line.startswith("# "):
            rows.append("<h1>%s</h1>" % line[2:])
        elif line.startswith(("- ", "* ")):
            rows.append("<li>%s</li>" % line[2:])
        elif line == "---":
            rows.append("<hr>")
        else:
            rows.append("<p>%s</p>" % line)
    flush_table()
    body = "\n".join(rows) or "<p>暂无已确认内容。</p>"
    return """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><style>
@page{size:A4;margin:16mm 18mm 18mm;@bottom-right{content:counter(page) '/' counter(pages);color:#69727d;font-size:8pt}}body{font-family:'Noto Sans SC','WenQuanYi Zen Hei','Microsoft YaHei',sans-serif;color:#29313b;line-height:1.75;font-size:10.2pt}.cover{border-bottom:2px solid #173d78;padding-bottom:5mm;margin-bottom:7mm}.cover h1{font-size:19pt;margin:0 0 3mm;color:#1d2632;border:0;padding:0}.meta{color:#69727d;font-size:9pt;line-height:1.7}.notice{margin:5mm 0 8mm;padding:3mm 4mm;background:#f5f7fa;border-left:3px solid #dce3ea;color:#566270}h1{font-size:18pt;margin:0 0 5mm;color:#1d2632;border-bottom:1px solid #dce3ea;padding-bottom:4mm}h2{font-size:15pt;margin:9mm 0 4mm;color:#1d2632;border-top:2px solid #dce3ea;padding-top:5mm}h3{font-size:11.5pt;margin:5mm 0 2mm;color:#1d2632}h4{font-size:10.5pt;margin:4mm 0 2mm;color:#29313b}p,li{margin:1.7mm 0}li{margin-left:5mm}strong{color:#1d2632}blockquote{margin:4mm 0;padding:3mm 4mm;border-left:3px solid #dce3ea;color:#687483;background:#fafbfd}hr{border:0;border-top:2px solid #dce3ea;margin:7mm 0}table{width:100%%;border-collapse:collapse;margin:4mm 0 7mm;font-size:9.3pt;page-break-inside:avoid}th{background:#edf3ff;color:#29313b;font-weight:700}th,td{border:1px solid #d8e2f4;padding:2.5mm 3mm;text-align:left;vertical-align:top}tr:nth-child(even){background:#fafcff}</style><body><div class='cover'><h1>IP 人设定位｜模块 1-4 初稿</h1><div class='meta'>黄雀 IP 孵化教练 · 基于本次对话整理 · 生成后请本人确认</div></div><div class='notice'>本报告用于确认 IP 底座。确认后才会开启模块 5 及后续内容生产。</div>%s</body></html>""" % body

def generate_foundation_report(convo_id):
    convo = load_conversation(convo_id)
    messages = [{"role": "system", "content": """你是IP定位报告编辑。只基于对话中已经出现的信息，写一份可直接交给客户确认的中文Markdown《模块1-4定位初稿》。这不是摘要：信息充分时应形成约8-10页的策略报告，但绝不为凑页数编造。未知、未确认数字或事实必须写‘待本人确认’。\n\n严格按以下结构输出，不写开场客套，也不要输出总标题：\n## 模块一｜定位诊断\n### 核心关键词（5-7个）：每个用编号、关键词和一句解释。\n### 最终定位：名称、一句话定位语、三合一策略。\n### 市场机会：至少3点，写清目标人群共鸣、成交痛点、差异化和可验证资产。\n### 潜在风险：3-5点，给出对应控制建议。\n## 模块二｜人设塑造\n### 三套人设方案：每套包含名称、核心特质、故事基调、传播标签、人设公式、优势与风险。\n### 推荐人设：说明选择理由，并提供账号封面/置顶、引流钩子、成交主张、逆袭故事、个人口头禅五条口径。此处必须用Markdown表格，列为“场景｜建议口径”。\n## 模块三｜价值主张提炼\n### 核心价值主张：主张核心、一句话金句、服务对象、解决问题、可交付结果。\n### 差异化证明：把经历、能力、结果和价值观分别写清。\n### 一句话自我介绍（优化版）：给原始口径、优化口径与3条优化理由。\n## 模块四｜故事资产挖掘\n### 四个故事资产：每个含故事名称、起点、转折、结果/品质、核心情绪点、适用传播场景。\n### 三类传播故事：挫折型、成长型、愿景型各选一个，说明传播目的。\n### 执行优先级建议：必须用Markdown表格，列为“优先级｜模块｜关键任务｜预计产出”。\n## 确认页\n用一句话列出客户要确认的3-5项；最后固定写：‘文档状态：模块1-4初稿完成，待本人确认后进入模块5-6执行。’\n\n不要编造未在对话中出现的金额、人数、经历、客户结果或账号名称。"""}]
    messages.extend(convo.get("messages", [])[-40:])
    messages.append({"role": "user", "content": "生成《IP 人设定位｜模块 1-4 初稿》，直接输出报告。"})
    messages.append({"role": "user", "content": "补充硬性要求：这份报告应接近完整策略报告，而非摘要。目标约8-10页、5500字以上；可基于已知事实做清楚标注的策略推导，但绝不编造事实。每个字段独占一行。模块1必须有7个关键词、5个市场机会及5个风险/控制建议；模块2三套人设每套分开写名称、特质、故事基调、标签、公式、优势、风险和适用场景，并给5条口径表与人设边界；模块3给3条完整价值主张方案、最终推荐和变现路径表；模块4每个故事写起点、冲突、转折、结果、情绪、场景、开头句，并补一张至少6行的内容资产使用表。最后列5项确认点。"})
    content = call_ai(messages, stream=False, temperature=0.4, max_tokens=7000).json()["choices"][0]["message"]["content"]
    FOUNDATION_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    target = FOUNDATION_REPORTS_DIR / (convo_id + ".pdf")
    browser = next((item for item in (shutil.which("chromium"), shutil.which("chromium-browser"), "/snap/bin/chromium") if item and pathlib.Path(item).is_file()), "")
    if not browser:
        raise RuntimeError("PDF renderer is unavailable")
    with tempfile.TemporaryDirectory(prefix="hermes-foundation-", dir=str(pathlib.Path.home())) as directory:
        root = pathlib.Path(directory); html_path = root / "report.html"; pdf_path = root / "report.pdf"
        html_path.write_text(_foundation_html(content), encoding="utf-8")
        subprocess.run(
            [browser, "--headless", "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run", "--no-pdf-header-footer", "--user-data-dir=" + str(root / "profile"), "--print-to-pdf=" + str(pdf_path), html_path.as_uri()],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False,
        )
        if not pdf_path.exists() or not pdf_path.read_bytes().startswith(b"%PDF-"):
            raise RuntimeError("PDF renderer failed")
        target.write_bytes(pdf_path.read_bytes())
    record = {"status": "awaiting_confirmation", "filename": target.name, "content": content, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    convo = load_conversation(convo_id); convo.setdefault("coach_state", {})["foundation_report"] = record; save_conversation(convo_id, convo)
    return record

def call_ai(messages, stream=False, temperature=0.7, max_tokens=None):
    payload = {"model": MODEL, "messages": messages, "stream": stream, "temperature": temperature}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    resp = requests.post(f"{API_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=180, stream=stream)
    if resp.status_code != 200:
        raise Exception(f"API {resp.status_code}: {resp.text[:300]}")
    return resp

def generate_module_report(convo_id, module_id):
    convo = load_conversation(convo_id)
    mod = MODULES[module_id - 1]
    report_type = MODULE_REPORT_TYPES.get(module_id, f"模块{module_id}报告")
    relevant_msgs = [msg for msg in convo.get("messages", [])]
    report_prompt = f"""你刚完成了对学员的「{mod['name']}」模块诊断。
请基于上述诊断对话，生成一份结构化的 **{report_type}**。
要求：1.只输出报告内容 2.Markdown格式 3.含核心结论、关键发现、具体建议 4.引用学员具体信息 5.结尾给出下一步行动
直接输出报告："""
    messages = [{"role":"system","content":"你是专业的IP孵化教练。输出纯Markdown，不要客套话。"}]
    messages.extend(relevant_msgs[-30:])
    messages.append({"role":"user","content":report_prompt})
    resp = call_ai(messages, stream=False, temperature=0.5)
    report = resp.json()["choices"][0]["message"]["content"]
    convo2 = load_conversation(convo_id)
    if "reports" not in convo2: convo2["reports"] = {}
    convo2["reports"][str(module_id)] = {"title": report_type, "content": report, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    save_conversation(convo_id, convo2)
    return report

# ═══════════════════════════════════════════════
# 新功能 1: 自动交付物生成
# ═══════════════════════════════════════════════

def humanize_text(text):
    """洗掉AI塑料味，变成真人语气"""
    prompt = f"""把下面这段文字重写一遍。要求：
- 删掉所有"首先其次最后""总而言之""值得注意的是"这类AI标志性废话
- 把长句拆成短句，每句不超过20个字
- 加上口语化的语气词（真的！说实话！你想想……）
- 保持原意，但让人感觉是人在说话不是机器
- 如果原文是面向美业/直销人群的，用"姐""老板""团队长"这类称呼

原文：
{text}

直接输出重写后的文本："""
    try:
        resp = call_ai([{"role":"user","content":prompt}], stream=False, temperature=0.8)
        return resp.json()["choices"][0]["message"]["content"]
    except:
        return text  # 失败返回原文

def generate_deliverable(convo_id, module_id):
    """为指定模块生成可交付物（文案/视觉/选题日历等）"""
    config = MODULE_DELIVERABLES.get(module_id)
    if not config:
        return None

    convo = load_conversation(convo_id)
    results = {}

    for item in config["types"]:
        try:
            # 取最近对话作为上下文
            relevant = convo.get("messages", [])[-20:]
            messages = [
                {"role":"system","content":"你是一个专业的IP孵化教练助手。基于诊断对话为学员生成定制化交付物。只输出内容，不要解释。"},
            ]
            # 加入对话上下文
            for m in relevant:
                messages.append({"role": m["role"], "content": m["content"][:500]})
            messages.append({"role":"user","content": item["prompt"]})

            resp = call_ai(messages, stream=False, temperature=0.7)
            content = resp.json()["choices"][0]["message"]["content"]
            # Humanize 文案类内容
            if module_id in [6, 10]:
                content = humanize_text(content)
            results[item["name"]] = content
        except Exception as e:
            results[item["name"]] = f"(生成失败: {str(e)[:100]})"

    # 保存交付物
    convo2 = load_conversation(convo_id)
    if "deliverables" not in convo2:
        convo2["deliverables"] = {}
    convo2["deliverables"][str(module_id)] = {
        "title": config["title"],
        "items": results,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    save_conversation(convo_id, convo2)
    return {"title": config["title"], "items": results}

# ═══════════════════════════════════════════════
# 新功能 2: GEO 分析
# ═══════════════════════════════════════════════

@app.route("/api/geo-analyze", methods=["POST"])
def api_geo_analyze():
    """GEO分析：你的品牌在AI搜索引擎里的可见度"""
    body = request.get_json()
    business_type = body.get("business_type", "美业")
    business_name = body.get("business_name", "").strip()
    keywords = body.get("keywords", "").strip()

    if not business_name or not keywords:
        return jsonify({"ok": False, "error": "请提供品牌名称和核心关键词"}), 400

    prompt = f"""你是一个GEO（生成式引擎优化）专家。分析以下品牌在AI搜索引擎（如豆包、DeepSeek、ChatGPT）中的可见度。

品牌名称：{business_name}
行业：{business_type}
核心关键词：{keywords}

请从以下5个维度分析并给出优化建议：

1. **当前可见度评估**：用户用这些关键词问AI，这个品牌有多大可能出现在答案里？（1-10分）
2. **拦截漏洞**：竞争对手可能靠哪些信息比你更容易被AI推荐？
3. **内容优化**：应该在哪些平台发布什么内容来提高AI抓取率？
4. **结构化数据**：品牌官网/小程序/朋友圈应该怎么组织信息让AI更容易理解？
5. **行动清单**：未来7天可以做的5件具体事情来提高GEO排名

直接输出Markdown格式的分析报告。"""

    try:
        resp = call_ai([{"role":"user","content":prompt}], stream=False, temperature=0.5)
        report = resp.json()["choices"][0]["message"]["content"]
        return jsonify({"ok": True, "report": report})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── API 路由 ──
@app.route("/")
def index():
    return render_template("index.html", modules=MODULES)

@app.route("/classic")
def classic_index():
    """Keep the original report/deliverable workbench available unchanged."""
    return render_template("index_clean.html", modules=MODULES)

@app.route("/api/conversations", methods=["GET"])
def api_list_convos():
    return jsonify(list_convos())

@app.route("/api/conversations", methods=["POST"])
def api_create_convo():
    cid = uuid.uuid4().hex[:12]
    data = {"id": cid, "title": "新诊断", "messages": [],
            "coach_state": {"ip_profile": {}, "current_module": 1, "completed_modules": [], "module_step": 0},
            "reports": {}, "deliverables": {}, "updated": datetime.now().strftime("%Y-%m-%d %H:%M")}
    save_conversation(cid, data)
    return jsonify({"id": cid})

@app.route("/api/conversations/<cid>", methods=["GET"])
def api_get_convo(cid):
    return jsonify(load_conversation(cid))

@app.route("/api/conversations/<cid>", methods=["DELETE"])
def api_delete_convo(cid):
    path = conversation_path(cid)
    if path.exists(): path.unlink()
    return jsonify({"ok": True})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    """流式聊天 + 自动状态管理 + 模块完成自动生成交付物"""
    body = request.get_json()
    cid = body.get("conversation_id", "default")
    user_msg = body.get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "empty message"}), 400

    convo = load_conversation(cid)
    convo["messages"].append({"role": "user", "content": user_msg})
    if convo["title"] == "新诊断" and len(convo["messages"]) >= 2:
        for m in convo["messages"]:
            if m["role"] == "user":
                t = m["content"][:30].replace("\n", " ")
                convo["title"] = t if len(t) < 30 else t[:27] + "..."
                break
    save_conversation(cid, convo)

    system_prompt = build_system_prompt(cid)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(convo["messages"][-40:])
    old_completed = list(convo.get("coach_state", {}).get("completed_modules", []))

    def generate():
        full = ""
        try:
            resp = call_ai(messages, stream=True)
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "): continue
                d = line[6:]
                if d == "[DONE]": break
                try:
                    chunk = json.loads(d)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        full += content
                        yield f"data: {json.dumps({'content': content})}\n\n"
                except json.JSONDecodeError: continue

            convo2 = load_conversation(cid)
            convo2["messages"].append({"role": "assistant", "content": full})
            new_state = parse_coach_state_updates(full, convo2.get("coach_state", {}))
            convo2["coach_state"] = new_state

            new_completed = [m for m in new_state.get("completed_modules", []) if m not in old_completed]

            # ── 新：自动生成交付物 ──
            auto_deliverables = {}
            for mid in new_completed:
                if mid in MODULE_DELIVERABLES:
                    try:
                        d_result = generate_deliverable(cid, mid)
                        if d_result:
                            auto_deliverables[str(mid)] = d_result
                    except:
                        pass  # 交付物生成失败不影响主流程

            foundation_report = None
            if 4 in new_completed:
                save_conversation(cid, convo2)
                try:
                    foundation_report = generate_foundation_report(cid)
                    convo2 = load_conversation(cid)
                    new_state = convo2.get("coach_state", new_state)
                except Exception as exc:
                    new_state["foundation_report"] = {"status": "failed", "error": str(exc)[:120]}
                    convo2["coach_state"] = new_state

            save_conversation(cid, convo2)

            yield f"data: {json.dumps({'done': True, 'state': new_state, 'new_completed': new_completed, 'auto_deliverables': auto_deliverables, 'foundation_report': foundation_report})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

@app.route("/api/generate-deliverable", methods=["POST"])
def api_generate_deliverable():
    """手动生成某模块的交付物"""
    body = request.get_json()
    cid = body["conversation_id"]
    module_id = body["module"]
    conversation_path(cid)
    try:
        result = generate_deliverable(cid, module_id)
        if result:
            return jsonify({"ok": True, "module": module_id, "deliverable": result})
        return jsonify({"ok": False, "error": f"模块{module_id}暂无自动交付物"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/generate-report", methods=["POST"])
def api_generate_report():
    body = request.get_json()
    cid = body["conversation_id"]
    module_id = body["module"]
    conversation_path(cid)
    try:
        report = generate_module_report(cid, module_id)
        return jsonify({"ok": True, "module": module_id, "report": report})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/conversations/<cid>/reports", methods=["GET"])
def api_get_reports(cid):
    convo = load_conversation(cid)
    return jsonify(convo.get("reports", {}))

@app.route("/api/conversations/<cid>/deliverables", methods=["GET"])
def api_get_deliverables(cid):
    """获取某对话的所有交付物"""
    convo = load_conversation(cid)
    return jsonify(convo.get("deliverables", {}))

@app.route("/api/foundation-report/<cid>.pdf", methods=["GET"])
def api_foundation_pdf(cid):
    conversation_path(cid)
    path = FOUNDATION_REPORTS_DIR / (cid + ".pdf")
    if not path.is_file():
        return jsonify({"ok": False, "error": "PDF 尚未生成"}), 404
    return send_file(path, mimetype="application/pdf", as_attachment=True, download_name="IP人设定位_模块1-4初稿.pdf")

@app.route("/api/foundation-report/confirm", methods=["POST"])
def api_confirm_foundation_report():
    cid = request.get_json()["conversation_id"]
    convo = load_conversation(cid); state = convo.setdefault("coach_state", {})
    report = state.get("foundation_report", {})
    if report.get("status") != "awaiting_confirmation":
        return jsonify({"ok": False, "error": "请先生成并查看模块 1-4 初稿"}), 409
    report["status"] = "confirmed"; report["confirmed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    state["foundation_report"] = report; state["current_module"] = 5; state["module_step"] = 0
    save_conversation(cid, convo)
    return jsonify({"ok": True, "state": state})

@app.route("/api/jump-module", methods=["POST"])
def api_jump():
    body = request.get_json()
    cid = body["conversation_id"]
    target = body["module"]
    convo = load_conversation(cid)
    foundation = convo.get("coach_state", {}).get("foundation_report", {})
    if target >= 5 and foundation.get("status") != "confirmed":
        return jsonify({"ok": False, "error": "请先确认模块 1-4 的 IP 定位初稿 PDF"}), 409
    convo["coach_state"]["current_module"] = target
    convo["coach_state"]["module_step"] = 0
    convo["messages"].append({"role": "user", "content": f"跳到模块 {target}: {MODULES[target-1]['name']}"})
    save_conversation(cid, convo)
    return jsonify({"ok": True, "current_module": target})

@app.route("/api/humanize", methods=["POST"])
def api_humanize():
    """手动对一段文字去AI味"""
    body = request.get_json()
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"error": "empty text"}), 400
    try:
        result = humanize_text(text)
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ═══════════════════════════════════════════════
# 新功能 3: 选题雷达 Topic Radar
# ═══════════════════════════════════════════════

@app.route("/api/topic-radar", methods=["POST"])
def api_topic_radar():
    """选题雷达：扫描赛道，分析选题机会"""
    body = request.get_json()
    keywords = body.get("keywords", "").strip()
    niche = body.get("niche", "美业").strip()
    convo_id = body.get("conversation_id", "")

    if not keywords:
        return jsonify({"ok": False, "error": "请输入关键词"}), 400

    # 获取对话上下文
    context_msgs = []
    if convo_id:
        convo = load_conversation(convo_id)
        for m in convo.get("messages", [])[-10:]:
            context_msgs.append({"role": m["role"], "content": m["content"][:300]})

    prompt = f"""你是一个专业的内容选题分析师。请对以下赛道进行选题雷达扫描。

赛道：{niche}
核心关键词：{keywords}

请从以下5个维度分析：

1. 🔥 **当前热门选题 TOP 5**：这个赛道目前最火的话题是什么？为什么火？
2. 📊 **饱和度分析**：哪些选题已经被做烂了（红海）？哪些还有空间（蓝海）？
3. 🎯 **差异化切入**：基于关键词，给出3个别人没想到的角度
4. 🕳️ **选题陷阱**：哪些选题看起来好但转化率低？为什么？
5. 📅 **7天选题日历**：未来7天每天1个选题标题+一句话钩子

要求：
- 针对{niche}人群，选题要能直接落地
- 标注每个选题适合的平台（朋友圈/抖音/小红书/视频号）
- 优先推荐能带来直接转化（咨询、到店、成交）的选题
- 输出用Markdown格式"""

    try:
        messages = [
            {"role":"system","content":"你是资深内容选题策略师，擅长分析内容赛道趋势。输出结构清晰，直接可执行。"},
        ]
        if context_msgs:
            # Add context as a note
            messages.append({"role":"system","content":"以下是对该学员的诊断背景信息，请结合其具体情况给出个性化选题建议：\n" + json.dumps([m["content"][:200] for m in context_msgs if m["role"]=="user"], ensure_ascii=False)})
        messages.append({"role":"user","content":prompt})

        resp = call_ai(messages, stream=False, temperature=0.7)
        report = resp.json()["choices"][0]["message"]["content"]

        # Save to conversation if available
        if convo_id:
            convo = load_conversation(convo_id)
            if "deliverables" not in convo:
                convo["deliverables"] = {}
            convo["deliverables"]["topic_radar"] = {
                "title": f"📡 选题雷达：{keywords}",
                "items": {"选题分析报告": report},
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M")
            }
            save_conversation(convo_id, convo)

        return jsonify({"ok": True, "report": report})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/analytics")
def analytics():
    convos = list_convos()
    t_convos = len(convos)
    t_msgs = sum(c.get("message_count",0) for c in convos)
    t_reports = sum(c.get("report_count",0) for c in convos)
    mod_counts = {}
    for c in convos:
        for m in c.get("completed_modules", []):
            mod_counts[m] = mod_counts.get(m, 0) + 1
    persons = []
    for c in convos[:50]:
        cur = c.get("current_module", 1)
        mod = MODULES[cur-1] if 1 <= cur <= 12 else MODULES[0]
        done_count = len(c.get("completed_modules", []))
        persons.append({"id": c["id"][:8], "title": c["title"],
            "messages": c.get("message_count",0), "reports": c.get("report_count",0),
            "progress": str(done_count) + "/12", "current": mod["name"],
            "updated": c.get("updated",""),
            "completed": [MODULES[m-1]["name"] if 1<=m<=12 else str(m) for m in c.get("completed_modules",[])]})
    return render_template("analytics.html",
        total=t_convos, messages=t_msgs, reports=t_reports,
        module_stats=sorted(mod_counts.items()),
        persons=persons, modules=MODULES, module_names=[m["name"] for m in MODULES])

if __name__ == "__main__":
    has_prompt = "✅" if COACH_PROMPT_BASE else "❌ 未找到"
    print(f"""
╔══════════════════════════════════════════╗
║   Hermes 12模块 IP孵化教练 v3           ║
║   新增：自动交付物 | GEO | Humanizer     ║
║   http://localhost:{PORT}                  ║
╚══════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=PORT, debug=False)
