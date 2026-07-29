"""Render an already-generated IP12 report as a private A4 PDF."""

import html
import os
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
import time


MAX_PDF_BYTES = 20 * 1024 * 1024
PRODUCTS = {
    "image_studio": ("图片生成", "banana.html"),
    "script_studio": ("文案编导", "script.html"),
    "voice_studio": ("音频创作", "audio.html"),
    "video_studio": ("视频创作", "video.html"),
    "workflow_canvas": ("创作画布", "canvas.html"),
}
_PROVIDER_PATTERN = re.compile(
    r"OpenAI|GPT(?:-[A-Za-z0-9.]+)*(?:\s+\d[A-Za-z0-9.-]*)?|Structured(?: Outputs?)?|"
    r"(?:Claude|Anthropic|Gemini|Seedance|Doubao|Grok|Sora)(?:[-\s][A-Za-z0-9.]+)*",
    re.I,
)


def _neutral(value, fallback=""):
    return re.sub(r"(?:AI 服务\s*){2,}", "AI 服务 ", _PROVIDER_PATTERN.sub("AI 服务", str(value or fallback or ""))).strip()


def _escape(value, fallback=""):
    return html.escape(_neutral(value, fallback), quote=True)


def _list(value):
    return value if isinstance(value, list) else []


def _bullets(values):
    items = "".join("<li>%s</li>" % _escape(item) for item in _list(values) if str(item or "").strip())
    return "<ul>%s</ul>" % items if items else ""


def _browser_path():
    configured = os.environ.get("DIGITAL_IP_PDF_BROWSER", "").strip()
    candidates = [
        configured,
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    return next((item for item in candidates if item and pathlib.Path(item).is_file()), "")


def _build_legacy_report_html(payload):
    project = payload.get("project") if isinstance(payload, dict) else {}
    envelope = payload.get("report") if isinstance(payload, dict) else {}
    content = envelope.get("content") if isinstance(envelope, dict) else {}
    project = project if isinstance(project, dict) else {}
    envelope = envelope if isinstance(envelope, dict) else {}
    content = content if isinstance(content, dict) else {}
    progress = envelope.get("progress") if isinstance(envelope.get("progress"), dict) else {}
    generated_at = envelope.get("generated_at")
    try:
        generated_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(generated_at)))
    except (TypeError, ValueError, OSError):
        generated_text = "未记录"

    evidence_html = "".join(
        "<article class='card'><span class='tag'>%s</span><h3>%s</h3>"
        "<blockquote>“%s”</blockquote><p class='source'>来源：%s</p></article>" % (
            _escape(item.get("evidence_id"), "证据"),
            _escape(item.get("claim")),
            _escape(item.get("source_excerpt")),
            _escape(
                "%s · %s" % (item.get("source_name"), item.get("source_location"))
                if item.get("source_name") and item.get("source_location") else item.get("source_ref")
            ),
        )
        for item in _list(content.get("evidence")) if isinstance(item, dict)
    ) or "<p class='empty'>暂无可追溯证据。</p>"

    pain_html = []
    for pain in _list(content.get("industry_pains")):
        if not isinstance(pain, dict):
            continue
        matches = []
        for match in _list(pain.get("product_matches")):
            if not isinstance(match, dict):
                continue
            product = PRODUCTS.get(match.get("product_id"))
            if not product:
                continue
            name, page = product
            url = "https://huangquechuanmei.com/workbench/%s" % page
            matches.append(
                "<div class='product'><a href='%s'>%s <span>→</span></a>"
                "<p class='click-hint'>可点击跳转使用我们的网站功能</p><p>%s</p>%s</div>" % (
                    url, _escape(name), _escape(match.get("fit_reason")), _bullets(match.get("execution_steps")),
                )
            )
        refs = " ".join("<span class='tag'>%s</span>" % _escape(item) for item in _list(pain.get("evidence_ids")))
        pain_html.append(
            "<article class='card'><div>%s</div><h3>%s</h3><p>%s</p>%s</article>" % (
                refs, _escape(pain.get("pain")), _escape(pain.get("why_it_matters")), "".join(matches),
            )
        )

    execution_html = "".join(
        "<article class='card'><span class='tag'>%s</span><h3>%s</h3>%s</article>" % (
            _escape(item.get("phase"), "行动阶段"), _escape(item.get("goal")), _bullets(item.get("steps")),
        )
        for item in _list(content.get("execution_plan")) if isinstance(item, dict)
    ) or "<p class='empty'>暂无执行路线。</p>"

    metrics_html = "".join(
        "<article class='card metric'><h3>%s</h3><dl>"
        "<dt>定义</dt><dd>%s</dd><dt>当前基线</dt><dd>%s</dd>"
        "<dt>规划目标</dt><dd>%s</dd><dt>复盘周期</dt><dd>%s</dd></dl></article>" % (
            _escape(item.get("name")), _escape(item.get("definition")), _escape(item.get("baseline")),
            _escape(item.get("target")), _escape(item.get("review_cycle")),
        )
        for item in _list(content.get("metrics")) if isinstance(item, dict)
    ) or "<p class='empty'>暂无复盘指标。</p>"

    gaps_html = "".join(
        "<article class='card'><span class='tag %s'>%s</span><h3>%s</h3><p>%s</p>"
        "<p class='source'>补充方式：%s</p></article>" % (
            "danger" if item.get("blocking") else "", "阻塞项" if item.get("blocking") else "可后补",
            _escape(item.get("gap")), _escape(item.get("why_needed")), _escape(item.get("how_to_collect")),
        )
        for item in _list(content.get("material_gaps")) if isinstance(item, dict)
    ) or "<p class='empty'>报告未识别到额外资料缺口。</p>"

    stale = "<div class='stale'>项目内容已在报告生成后发生变化；本 PDF 是历史报告快照。</div>" if payload.get("stale") else ""
    title = _escape(content.get("title"), "IP12 产品方案报告")
    project_title = _escape(project.get("title"), "我的数字化 IP")
    summary = _escape(content.get("executive_summary"), "暂无摘要。")
    disclaimer = _escape(content.get("disclaimer"), "报告仅供规划参考，不构成经营效果保证。")
    meta = "确认 %s · 跳过 %s · 共 %s" % (
        int(progress.get("confirmed") or 0), int(progress.get("skipped") or 0), int(progress.get("total") or 54),
    )
    return """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><style>
@page{size:A4;margin:0}*{box-sizing:border-box}html,body{margin:0;padding:0;background:#f3efe6;color:#102033;font-family:'Noto Sans SC','WenQuanYi Zen Hei','PingFang SC','Microsoft YaHei',sans-serif;font-size:10.5pt;line-height:1.65;-webkit-print-color-adjust:exact;print-color-adjust:exact}a{color:inherit}.cover{min-height:297mm;padding:28mm 20mm 20mm;color:#fff;background:radial-gradient(circle at 86%% 12%%,rgba(121,89,229,.34),transparent 32%%),linear-gradient(148deg,#061827,#102b3c 58%%,#071827);position:relative;break-after:page}.brand{color:#efc766;font-size:13pt;font-weight:800;letter-spacing:.18em}.route{margin-top:28mm;color:#b8c8d4;letter-spacing:.08em}.route b{color:#efc766}.cover h1{max-width:165mm;margin:8mm 0 5mm;font-family:'Noto Serif SC','Songti SC',serif;font-size:31pt;line-height:1.2}.cover .summary{max-width:154mm;color:#d8e0e7;font-size:13pt}.cover .meta{display:flex;flex-wrap:wrap;gap:3mm;margin-top:16mm}.pill{border:1px solid rgba(239,199,102,.45);border-radius:99px;padding:1.5mm 3.5mm;color:#e9eef2;background:rgba(4,18,30,.38)}.cover .foot{position:absolute;left:20mm;right:20mm;bottom:18mm;padding-top:5mm;border-top:1px solid rgba(239,199,102,.3);color:#96aab8}.content{padding:12mm 16mm 14mm}.stale{margin-bottom:5mm;border-left:3px solid #c98718;padding:3.5mm;background:#fff0c9;color:#6c4b13}.section{padding:5mm 0;border-bottom:1px solid #d9d1c2;break-inside:avoid-page}.section:last-child{border-bottom:0}.section-head{display:grid;grid-template-columns:13mm 1fr;gap:4mm;margin-bottom:4mm;break-inside:avoid-page;break-after:avoid-page}.num{display:grid;place-items:center;width:11mm;height:11mm;border-radius:3mm;background:#102b3c;color:#efc766;font-size:8pt;font-weight:800}.section h2{margin:0;font-family:'Noto Serif SC','Songti SC',serif;font-size:19pt;line-height:1.25}.lead{margin:1mm 0 0;color:#6d7780}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:3.5mm}.card{break-inside:avoid;margin-bottom:3mm;padding:4mm;border:1px solid #ddd5c7;border-radius:4mm;background:#fffdf9;box-shadow:0 2mm 7mm rgba(24,36,43,.045)}.card h3{margin:2mm 0;font-size:12.5pt;line-height:1.45}.card p{margin:1.5mm 0;color:#4d5a63}.tag{display:inline-block;margin:0 1mm 1mm 0;border-radius:99px;padding:1mm 2.4mm;background:#f8e8b8;color:#6a4d13;font-size:8pt;font-weight:750}.tag.danger{background:#ffe0d9;color:#8b2f20}blockquote{margin:2mm 0;padding:3mm;border-left:2px solid #d5a83c;background:#f8f4eb;color:#36444e}.source{font-size:8.5pt!important;color:#75818a!important}.product{margin-top:3mm;padding:3.5mm;border:1px solid #ddd2fb;border-radius:3mm;background:#f4efff}.product a{color:#5a35b9;font-size:11pt;font-weight:800;text-decoration:underline;text-underline-offset:2px}.click-hint{margin:.5mm 0 2mm!important;color:#7356ba!important;font-size:8pt!important}.product ul,ul{margin:2mm 0 0;padding-left:5mm}.product li,li{margin:1mm 0}.metric dl{display:grid;grid-template-columns:24mm 1fr;gap:1mm 3mm;margin:2mm 0}.metric dt{color:#6640c4;font-size:8pt;font-weight:800}.metric dd{margin:0;color:#46525b}.empty{border:1px dashed #b9b09e;border-radius:3mm;padding:5mm;color:#7b817f;background:#faf7f0}.disclaimer{break-inside:avoid;margin-top:3mm;border-left:3px solid #d5a83c;padding:4mm;background:#fff2ca;color:#634d20}
</style></head><body><section class='cover'><div class='brand'>黄雀 · 数字化 IP</div><div class='route'>真实资料 <b>→</b> 痛点诊断 <b>→</b> 产品行动</div><h1>%s</h1><p class='summary'>%s</p><div class='meta'><span class='pill'>项目：%s</span><span class='pill'>%s</span><span class='pill'>生成：%s</span></div><div class='foot'>证据型方案 · 所有产品操作仍需用户主动确认</div></section><main class='content'>%s<section class='section'><div class='section-head'><span class='num'>01</span><div><h2>事实依据</h2><p class='lead'>只引用已经确认的回答或资料证据。</p></div></div><div class='grid'>%s</div></section><section class='section'><div class='section-head'><span class='num'>02</span><div><h2>行业痛点与黄雀产品匹配</h2><p class='lead'>先说明问题，再给出可执行的产品路径。</p></div></div><div class='grid'>%s</div></section><section class='section'><div class='section-head'><span class='num'>03</span><div><h2>行动阶段</h2><p class='lead'>从最小验证开始，逐步形成可复用内容资产。</p></div></div><div class='grid'>%s</div></section><section class='section'><div class='section-head'><span class='num'>04</span><div><h2>复盘指标</h2><p class='lead'>以用户确认的数据为基线，不编造经营目标。</p></div></div><div class='grid'>%s</div></section><section class='section'><div class='section-head'><span class='num'>05</span><div><h2>资料缺口</h2><p class='lead'>缺少的资料会明确列出，方便后续回补。</p></div></div><div class='grid'>%s</div></section><section class='section'><div class='section-head'><span class='num'>06</span><div><h2>使用边界</h2></div></div><div class='disclaimer'>%s</div></section></main></body></html>""" % (
        title, summary, project_title, _escape(meta), _escape(generated_text), stale, evidence_html,
        "".join(pain_html) or "<p class='empty'>资料不足时不会为了推荐而推荐。</p>",
        execution_html, metrics_html, gaps_html, disclaimer,
    )


def _foundation_cards(items):
    cards = []
    labels = {"fact": "事实", "inference": "AI 判断", "option": "备选", "recommendation": "推荐"}
    for item in _list(items):
        if not isinstance(item, dict):
            continue
        refs = "、".join(_escape(ref) for ref in _list(item.get("evidence_ids")))
        risks = _bullets(item.get("risks"))
        cards.append(
            "<article class='finding'><h3><span class='kind'>%s</span>%s</h3><p>%s</p>%s%s</article>" % (
                _escape(labels.get(item.get("kind"), "待核对")), _escape(item.get("title")),
                _escape(item.get("detail")),
                "<p class='source'>依据：%s</p>" % refs if refs else "",
                risks,
            )
        )
    return "".join(cards) or "<p class='empty'>该模块暂无可引用结论。</p>"


def _build_foundation_report_html(payload):
    project = payload.get("project") if isinstance(payload, dict) else {}
    envelope = payload.get("report") if isinstance(payload, dict) else {}
    project = project if isinstance(project, dict) else {}
    envelope = envelope if isinstance(envelope, dict) else {}
    content = envelope.get("content") if isinstance(envelope.get("content"), dict) else {}
    progress = envelope.get("progress") if isinstance(envelope.get("progress"), dict) else {}
    generated_at = envelope.get("generated_at")
    try:
        generated_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(generated_at)))
    except (TypeError, ValueError, OSError):
        generated_text = "未记录"

    evidence_html = "".join(
        "<article class='evidence'><h3><span class='evidence-id'>%s</span>%s</h3><blockquote>“%s”</blockquote><p class='source'>来源：%s</p></article>" % (
            _escape(item.get("evidence_id"), "证据"), _escape(item.get("claim")), _escape(item.get("source_excerpt")),
            _escape("%s · %s" % (item.get("source_name"), item.get("source_location"))
                    if item.get("source_name") and item.get("source_location") else item.get("source_ref")),
        )
        for item in _list(content.get("evidence")) if isinstance(item, dict)
    ) or "<p class='empty'>暂无可追溯证据。</p>"
    module_html = []
    for index, module in enumerate(sorted(
            (item for item in _list(content.get("modules")) if isinstance(item, dict)),
            key=lambda item: item.get("module_id", 0))):
        module_number = ("一", "二", "三", "四")[index] if index < 4 else str(index + 1)
        module_html.append("<section class='section module'><div class='section-head'><h2>模块%s｜%s</h2><p class='lead'>%s</p></div><div>%s</div></section>" % (
            module_number, _escape(module.get("title"), "模块 %s" % (index + 1)),
            _escape(module.get("summary"), "待本人复核的阶段判断。"), _foundation_cards(module.get("findings")),
        ))
    priority_rows = "".join(
        "<tr><td>%s</td><td>模块 %s</td><td>%s<small>依据：%s</small></td><td>%s</td></tr>" % (
            _escape(item.get("priority")), _escape(item.get("module_id"), "-"), _escape(item.get("task")),
            _escape("、".join(str(ref) for ref in _list(item.get("evidence_ids"))), "待补"),
            _escape(item.get("output")),
        ) for item in _list(content.get("execution_priorities")) if isinstance(item, dict)
    )
    priorities_html = (
        "<table><thead><tr><th>优先级</th><th>模块</th><th>关键任务</th><th>预计产出</th></tr></thead>"
        "<tbody>%s</tbody></table>" % priority_rows if priority_rows else "<p class='empty'>暂无下一步优先级。</p>"
    )
    confirmations_html = "".join(
        "<article class='finding'><h3><span class='kind'>%s</span>%s</h3><p>%s</p><p class='source'>依据：%s</p></article>" % (
            "必须确认" if item.get("required") else "建议确认", _escape(item.get("item")), _escape(item.get("reason")),
            _escape("、".join(str(ref) for ref in _list(item.get("evidence_ids")))),
        ) for item in _list(content.get("confirmation_items")) if isinstance(item, dict)
    ) or "<p class='empty'>报告未列出额外确认项。</p>"
    gaps_html = "".join(
        "<article class='finding'><h3><span class='kind %s'>%s</span>%s</h3><p>%s</p><p class='source'>补充方式：%s</p></article>" % (
            "danger" if item.get("blocking") else "", "阻塞项" if item.get("blocking") else "可后补",
            _escape(item.get("gap")), _escape(item.get("why_needed")), _escape(item.get("how_to_collect")),
        ) for item in _list(content.get("material_gaps")) if isinstance(item, dict)
    ) or "<p class='empty'>报告未识别到额外资料缺口。</p>"
    stale = "<div class='stale'>模块 1–4 资料已变化；本 PDF 是历史报告快照。</div>" if payload.get("stale") else ""
    status = "已确认 IP 底座" if envelope.get("status") == "confirmed" else "待本人确认"
    meta = "确认 %s · 跳过 %s · 共 %s · %s" % (
        int(progress.get("confirmed") or 0), int(progress.get("skipped") or 0), int(progress.get("total") or 30), status,
    )
    return """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><style>
@page{size:A4;margin:18mm 18mm 20mm;@top-left{content:"IP 人设定位｜模块 1–4";color:#7a818b;font-size:8pt}@top-right{content:"黄雀";color:#7a818b;font-size:8pt}@bottom-left{content:"";border-top:1.5px solid #173d78}@bottom-center{content:"";border-top:1.5px solid #173d78}@bottom-right{content:counter(page) "/" counter(pages);border-top:1.5px solid #173d78;color:#202631;font-size:8pt;padding-top:2mm}}*{box-sizing:border-box}html,body{margin:0;padding:0;background:#fff;color:#414750;font-family:'Noto Sans SC','WenQuanYi Zen Hei','PingFang SC','Microsoft YaHei',sans-serif;font-size:10.5pt;line-height:1.75;-webkit-print-color-adjust:exact;print-color-adjust:exact}.report-head{padding:2mm 0 8mm;border-bottom:3px solid #e2e5e9}.report-head h1{margin:0 0 3mm;color:#202631;font-size:22pt;line-height:1.35}.summary{margin:0 0 5mm;color:#687280;font-size:11.5pt}.meta{border-left:3px solid #e2e5e9;padding-left:4mm;color:#707987;font-size:9.5pt}.meta p{margin:1mm 0}.stale{margin:5mm 0 0;border-left:3px solid #8b6a2f;padding:2mm 4mm;color:#705829;background:#faf7ed}.section{padding:8mm 0 7mm;border-bottom:3px solid #e2e5e9}.section-head{margin-bottom:4mm;break-inside:avoid-page;break-after:avoid-page;page-break-inside:avoid;page-break-after:avoid}.section-head+div>:first-child{break-before:avoid-page;page-break-before:avoid}.section h2{margin:0;color:#202631;font-size:17pt;line-height:1.35}.lead{margin:1mm 0 0;color:#707987}.finding,.evidence{break-inside:avoid;padding:3mm 0;border-bottom:1px solid #e2e5e9}.finding:last-child,.evidence:last-child{border-bottom:0}.finding h3,.evidence h3{margin:0 0 1mm;color:#282e38;font-size:11.5pt}.finding p,.evidence p{margin:1mm 0}.kind,.evidence-id{display:inline-block;margin-right:2mm;color:#173d78;font-size:8.5pt;font-weight:700;vertical-align:1px}.kind.danger{color:#9b3028}blockquote{margin:2mm 0;padding:1.5mm 0 1.5mm 4mm;border-left:3px solid #e2e5e9;color:#687280}.source{color:#77818e!important;font-size:8.5pt!important}ul{margin:1.5mm 0 0;padding-left:6mm}.empty{margin:2mm 0;color:#77818e}table{width:100%%;border-collapse:collapse;break-inside:avoid;font-size:9.5pt}th,td{border:1px solid #d9e3f4;padding:2.6mm 3mm;text-align:left;vertical-align:top}th{background:#eef3fd;color:#39414c;font-weight:700}td small{display:block;margin-top:1mm;color:#77818e}.disclaimer{break-inside:avoid;border-left:3px solid #e2e5e9;padding:2mm 4mm;color:#687280}.document-status{margin-top:6mm;font-weight:700;color:#4a515c}
</style></head><body><main><header class='report-head'><h1>%s</h1><p class='summary'>%s</p><div class='meta'><p>项目：%s</p><p>整理日期：%s</p><p>资料进度：%s</p></div>%s</header><section class='section evidence-section'><div class='section-head'><h2>资料依据</h2><p class='lead'>只引用已经确认的回答或资料证据。</p></div>%s</section>%s<section class='section'><div class='section-head'><h2>执行优先级建议</h2><p class='lead'>P0–P3 是待确认的工作顺序，不代表效果承诺。</p></div>%s</section><section class='section'><div class='section-head'><h2>待确认事项</h2><p class='lead'>确认后才可进入模块 5–6。</p></div>%s</section><section class='section'><div class='section-head'><h2>资料缺口</h2></div>%s</section><section class='section'><div class='section-head'><h2>使用边界</h2></div><div class='disclaimer'>%s</div><p class='document-status'>文档状态：模块 1–4 初稿完成，待本人确认后进入模块 5–6 执行。</p></section></main></body></html>""" % (
        _escape(content.get("title"), "IP 人设定位｜模块 1–4"),
        _escape(content.get("executive_summary"), "暂无摘要。"),
        _escape(project.get("title"), "我的数字化 IP"), _escape(generated_text), _escape(meta), stale,
        evidence_html, "".join(module_html), priorities_html, confirmations_html, gaps_html,
        _escape(content.get("disclaimer"), "报告仅基于用户确认资料，不构成经营效果保证。"),
    )


def build_report_html(payload):
    envelope = payload.get("report") if isinstance(payload, dict) else {}
    if isinstance(envelope, dict) and envelope.get("stage") == "foundation_v1":
        return _build_foundation_report_html(payload)
    return _build_legacy_report_html(payload)


def render_report_pdf(payload, browser="", timeout=45):
    browser = browser or _browser_path()
    if not browser:
        raise RuntimeError("PDF renderer is unavailable")
    # Snap Chromium has a private /tmp mount; the service user's home is shared with the browser.
    with tempfile.TemporaryDirectory(prefix="hq-ip12-pdf-", dir=pathlib.Path.home()) as directory:
        root = pathlib.Path(directory)
        html_path, pdf_path = root / "report.html", root / "report.pdf"
        html_path.write_text(build_report_html(payload), encoding="utf-8")
        command = [
            browser, "--headless", "--disable-gpu", "--disable-dev-shm-usage",
            "--disable-background-networking", "--no-first-run", "--no-pdf-header-footer",
            "--user-data-dir=%s" % (root / "profile"), "--print-to-pdf=%s" % pdf_path,
            html_path.as_uri(),
        ]
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            command.insert(1, "--no-sandbox")
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        deadline, last_size, stable = time.monotonic() + timeout, -1, 0
        try:
            while time.monotonic() < deadline:
                size = pdf_path.stat().st_size if pdf_path.exists() else 0
                if size > MAX_PDF_BYTES:
                    raise RuntimeError("PDF renderer exceeded size limit")
                stable = stable + 1 if size > 0 and size == last_size else 0
                if stable >= 3:
                    break
                if process.poll() is not None and size <= 0:
                    raise RuntimeError("PDF renderer failed")
                last_size = size
                time.sleep(0.1)
            else:
                raise RuntimeError("PDF renderer timed out")
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=2)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
                    except ProcessLookupError:
                        pass
                    except subprocess.TimeoutExpired:
                        raise RuntimeError("PDF renderer did not stop")
        if pdf_path.stat().st_size > MAX_PDF_BYTES:
            raise RuntimeError("PDF renderer exceeded size limit")
        data = pdf_path.read_bytes()
    if not data.startswith(b"%PDF-") or not data.rstrip().endswith(b"%%EOF") or len(data) > MAX_PDF_BYTES:
        raise RuntimeError("PDF renderer returned invalid output")
    return data
