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
        self.assertIn("已经保存的 IP12 回答", html)
        self.assertIn("未勾选不会发送资料、不会调用模型", html)
        self.assertIn("generateBtn.disabled=!consent.checked", html)
        self.assertIn("if(!consent.checked||!project)return", html)
        self.assertIn("JSON.stringify({revision:project.revision,consent:true})", html)

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

    def test_print_is_native_pdf_capable_without_file_claim(self):
        html = self.html
        self.assertIn("@page{size:A4", html)
        self.assertIn("@media print", html)
        self.assertIn("window.print()", html)
        self.assertIn("打印 / 保存为 PDF", html)
        self.assertIn("不声称已生成 PDF", html)
        self.assertIn("不提供 Word/DOCX 文件", html)
        self.assertNotIn("下载 PDF", html)
        self.assertNotIn("生成 DOCX", html)

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

    def test_evidence_gaps_metrics_and_stale_state_are_visible(self):
        html = self.html
        for text in ["事实依据", "行业痛点与匹配产品", "执行路线", "复盘指标", "材料缺口", "使用边界"]:
            self.assertIn(text, html)
        self.assertIn('id="stale"', html)
        self.assertIn("项目内容已在报告生成后发生变化", html)
        self.assertIn("没有直接能力匹配时", html)


if __name__ == "__main__":
    unittest.main()
