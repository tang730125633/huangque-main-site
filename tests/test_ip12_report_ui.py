import unittest
from pathlib import Path


PAGE = Path(__file__).resolve().parents[1] / "site" / "workbench" / "ip12-report.html"
WORKBENCH = PAGE.parent


class IP12ReportUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")

    def test_generation_requires_explicit_saved_answer_consent(self):
        html = self.html
        self.assertIn('id="consent" type="checkbox"', html)
        self.assertIn("已经保存的 IP12 回答", html)
        self.assertIn("未勾选不会发送资料、不会调用模型", html)
        self.assertIn("generateBtn.disabled=!consent.checked", html)
        self.assertIn("if(!consent.checked||!project)return", html)
        self.assertIn("JSON.stringify({revision:project.revision,consent:true})", html)
        self.assertIn("发送给 AI 分析服务", html)
        self.assertNotIn("OpenAI", html)

    def test_report_uses_owned_api_and_does_not_auto_generate(self):
        html = self.html
        self.assertIn('const API="/api/gen/digital-ip/projects/"+encodeURIComponent(projectId)', html)
        self.assertIn('fetch(API+"/report",{credentials:"include",cache:"no-store"})', html)
        self.assertIn('fetch(API,{credentials:"include",cache:"no-store"})', html)
        self.assertIn('/login.html?next=', html)
        self.assertIn('method:"POST",credentials:"include"', html)
        self.assertIn("generateBtn.addEventListener", html)
        load_source = html[html.index("async function load()"):html.index('consent.addEventListener')]
        self.assertNotIn('method:"POST"', load_source)

    def test_pdf_download_uses_the_owned_same_origin_url_and_keeps_print_fallback(self):
        html = self.html
        self.assertIn("@page{size:A4", html)
        self.assertIn("@media print", html)
        self.assertIn("window.print()", html)
        self.assertIn("打印 / 保存为 PDF", html)
        self.assertIn('id="downloadBtn" href="#" hidden>下载 PDF</a>', html)
        self.assertIn("function sameOriginPdfUrl(value)", html)
        self.assertIn("currentEnvelope.pdf_url", html)
        self.assertIn("downloadBtn.hidden=!pdfUrl", html)
        self.assertIn("downloadBtn.href=pdfUrl||\"#\"", html)
        self.assertIn("url.origin===location.origin", html)
        self.assertIn("url.pathname===expected", html)
        self.assertIn("/report.pdf`;", html)
        self.assertIn("可下载服务器生成的私有 PDF", html)
        self.assertIn("不提供 Word/DOCX 文件", html)
        self.assertNotIn("生成 DOCX", html)

    def test_report_gate_and_progress_default_use_current_34_open_steps(self):
        html = self.html
        self.assertIn("当前开放的 34 步全部确认或跳过后即可生成", html)
        self.assertIn("共 ${progress.total||34}", html)
        self.assertNotIn("全部 54 步确认或跳过后即可生成", html)
        self.assertNotIn("progress.total||54", html)

    def test_dynamic_report_content_is_rendered_as_text(self):
        html = self.html
        self.assertIn("function node(tag,className,text)", html)
        self.assertIn("el.textContent=String(text)", html)
        self.assertNotIn("innerHTML", html)
        for product_id, page in {
            "image_studio": "banana.html",
            "script_studio": "script.html",
            "voice_studio": "audio.html",
            "video_studio": "video.html",
            "workflow_canvas": "canvas.html",
        }.items():
            self.assertIn(product_id, html)
            self.assertIn(page, html)
        self.assertIn("可点击跳转使用我们的网站功能", html)
        self.assertIn("点击后只预填待编辑内容，不会自动生成、扣点或发布", html)
        self.assertIn('class="brand" href="/" aria-label="返回黄雀主站首页"', html)

    def test_product_titles_offer_explicit_safe_prefill_for_text_image_and_video(self):
        html = self.html
        source = html[html.index("function productLink"):html.index("function render(payload)")]
        self.assertIn("hq_ip12_product_handoff_v1", html)
        self.assertIn("PRODUCT_HANDOFF_TARGETS", html)
        self.assertIn("sessionStorage.setItem", source)
        self.assertIn("sessionStorage.removeItem", source)
        self.assertIn("?prefill=ip12", source)
        self.assertIn("created_at:Date.now()", source)
        self.assertIn("if(stale)", source)
        self.assertIn("报告已过期，请重新生成后再带入产品页", source)
        self.assertIn("浏览器无法暂存待编辑方案，请刷新后重试", source)
        self.assertLess(source.index("sessionStorage.setItem"), source.index("location.href=link.href"))
        self.assertNotIn("projectId", source)
        self.assertNotIn("fetch(", source)

    def test_product_pages_consume_recent_handoff_once_and_only_fill_inputs(self):
        pages = {
            "banana.html": ("image_studio", "pr.value=String(handoff.prompt).slice(0,2000)"),
            "script.html": ("script_studio", "topic.value=String(handoff.prompt).slice(0,1000)"),
            "video.html": ("video_studio", "$('scriptText').value=String(handoff.prompt).slice(0,1000)"),
        }
        for filename, (target, fill) in pages.items():
            with self.subTest(page=filename):
                html = (WORKBENCH / filename).read_text(encoding="utf-8")
                self.assertIn("new URLSearchParams(location.search)", html)
                self.assertIn("get('prefill')", html)
                self.assertIn("hq_ip12_product_handoff_v1", html)
                self.assertIn("sessionStorage.removeItem('hq_ip12_product_handoff_v1')", html)
                self.assertTrue(
                    f"handoff.target==='{target}'" in html or f"handoff.target!=='{target}'" in html
                )
                self.assertIn("age", html.lower())
                self.assertIn("Number.isFinite", html)
                self.assertIn("600000", html)
                self.assertIn(fill, html)
                self.assertIn("已带入 IP12 待编辑方案，请检查后再生成", html)

        banana = (WORKBENCH / "banana.html").read_text(encoding="utf-8")
        banana_source = banana[banana.index("if(ip.get('prefill')==='ip12')"):banana.index("var pp=ip.get('prompt')")]
        self.assertIn("try{", banana_source)
        self.assertIn("}catch(e){}", banana_source)
        script = (WORKBENCH / "script.html").read_text(encoding="utf-8")
        script_source = script[script.index("function applyIP12Handoff"):script.index("function cardText")]
        video = (WORKBENCH / "video.html").read_text(encoding="utf-8")
        video_source = video[video.index("function applyIP12Handoff"):video.index("// 灵感页「一键跟创（视频）」")]
        for source in (banana_source, script_source, video_source):
            self.assertNotIn("fetch(", source)
            self.assertNotIn(".click()", source)

    def test_evidence_gaps_metrics_and_stale_state_are_visible(self):
        html = self.html
        for text in ["事实依据", "行业痛点与匹配产品", "执行路线", "复盘指标", "材料缺口", "使用边界"]:
            self.assertIn(text, html)
        self.assertIn('id="stale"', html)
        self.assertIn("项目内容已在报告生成后发生变化", html)
        self.assertIn("没有直接能力匹配时", html)

    def test_evidence_prefers_authoritative_source_name_and_location(self):
        html = self.html
        self.assertIn("item.source_name&&item.source_location?`${item.source_name} · ${item.source_location}`:item.source_ref", html)
        self.assertIn("`来源：${source}`", html)

    def test_visual_system_explains_the_evidence_chain_and_stays_accessible(self):
        html = self.html
        for text in ["真实资料", "痛点诊断", "产品行动", "证据型方案"]:
            self.assertIn(text, html)
        self.assertIn('class="evidence-route"', html)
        self.assertIn(":focus-visible", html)
        self.assertIn("@media(max-width:520px)", html)
        self.assertIn("@media(prefers-reduced-motion:reduce)", html)
        self.assertNotIn('behavior:"smooth"', html)


if __name__ == "__main__":
    unittest.main()
