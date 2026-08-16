import re
import unittest
from pathlib import Path


PAGE = Path(__file__).resolve().parents[1] / "site" / "workbench" / "ip12-report.html"


class IP12ReportUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")

    def test_generation_requires_explicit_saved_answer_consent(self):
        html = self.html
        self.assertIn('id="consent" type="checkbox"', html)
        self.assertIn("已经保存", html)
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

    def test_foundation_report_gate_uses_the_first_four_modules_without_internal_counts(self):
        html = self.html
        self.assertIn("完成模块 1–4 的采访后", html)
        self.assertIn("`已记录 ${progress.confirmed||0} · 待补 ${progress.skipped||0}`", html)
        self.assertNotIn("progress.total||30", html)
        foundation = html[html.index("function renderFoundation"):html.index("function render(payload)")]
        self.assertNotIn("progress.total||34", foundation)

    def test_report_confirmation_is_explicit_and_uses_the_owned_api(self):
        html = self.html
        self.assertIn("/report-confirm", html)
        self.assertRegex(html, r'<button[^>]+id="[^\"]*confirm[^\"]*"[^>]*>[^<]*确认')
        self.assertIn("pending_confirmation", html)
        index = html.index("/report-confirm")
        confirm_source = html[index - 300:index + 500]
        self.assertIn('method:"POST",credentials:"include"', confirm_source)
        self.assertIn("&module=5&step=1", html)

    def test_foundation_report_content_is_rendered_as_text(self):
        html = self.html
        self.assertIn("function node(tag,className,text)", html)
        self.assertIn("el.textContent=String(text)", html)
        self.assertNotIn("innerHTML", html)
        for field in ("modules", "execution_priorities", "confirmation_items", "material_gaps"):
            self.assertIn(f"content.{field}", html)
        for label in ("执行优先级", "待确认事项", "资料缺口", "使用边界"):
            self.assertIn(label, html)
        self.assertIn('class="brand" href="/" aria-label="返回黄雀主站首页"', html)

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

    def test_confirmation_items_gaps_and_stale_state_are_visible(self):
        html = self.html
        for text in ("事实依据", "待确认事项", "资料缺口", "使用边界"):
            self.assertIn(text, html)
        self.assertIn('id="stale"', html)
        self.assertIn("项目内容已在报告生成后发生变化", html)

    def test_evidence_prefers_authoritative_source_name_and_location(self):
        html = self.html
        self.assertIn("item.source_name&&item.source_location?`${item.source_name} · ${item.source_location}`:item.source_ref", html)
        self.assertIn("`来源：${source}`", html)

    def test_visual_system_stays_minimal_on_mobile_and_accessible(self):
        html = self.html
        self.assertIn("@media(max-width:520px)", html)
        self.assertIn("grid-template-columns:1fr", html)
        self.assertIn(":focus-visible", html)
        self.assertIn("@media(prefers-reduced-motion:reduce)", html)
        self.assertNotIn('behavior:"smooth"', html)


if __name__ == "__main__":
    unittest.main()
