import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


PAGE = Path(__file__).resolve().parents[1] / "site" / "workbench" / "ip12.html"
CORE = Path(__file__).resolve().parents[1] / "server" / "content_domains" / "core.py"


class IP12AIUITests(unittest.TestCase):
    def test_ai_is_explicit_structured_and_keeps_confirmation_separate(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn("/api/gen/digital-ip/diagnose", html)
        self.assertIn("/api/gen/digital-ip/guide", html)
        self.assertIn("AI 分析本步", html)
        self.assertIn("小黄雀 · IP 成长教练", html)
        self.assertIn("我不知道怎么填", html)
        self.assertIn("告诉我下一步", html)
        self.assertIn("不会监听输入", html)
        self.assertIn("OPENAI · STRUCTURED", html)
        self.assertIn("credentials:\"include\"", html)
        self.assertIn("AI 只给建议", html)
        self.assertNotIn("OPENAI_API_KEY", html)

    def test_project_recovery_consent_and_action_links_are_visible(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn("/api/gen/digital-ip/projects", html)
        self.assertIn("IP12 成长档案", html)
        self.assertIn("原始文件不会保存到项目档案", html)
        self.assertIn("PPT/Word 内嵌图表建议先导出为 PDF", html)
        self.assertIn("来源证据与定位", html)
        self.assertIn("current_module:module.name", html)
        self.assertIn("current_step:step.title", html)
        for extension, mime in {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "md": "text/markdown", "jpeg": "image/jpeg"}.items():
            self.assertIn(f'{extension}:"{mime}"', html)
        self.assertIn("type:mime", html)
        self.assertIn("data_url:`data:${mime};base64,${base64}`", html)
        self.assertIn("profile:state.profile", html)
        self.assertIn('state=remote&&typeof remote==="object"?{...initialState,...remote,analyses:{}', html)
        self.assertIn("async function flushProjectSave()", html)
        self.assertGreaterEqual(html.count("await flushProjectSave();"), 2)
        self.assertIn('`${STORAGE_KEY}:${project.id}`', html)
        self.assertIn("let state = structuredClone(initialState);", html)
        self.assertNotIn("localStorage.setItem(STORAGE_KEY,JSON.stringify(state))", html)
        self.assertIn("saveProject(true)", html)
        self.assertIn("项目已在另一端更新，请重新查看后再操作", html)
        self.assertIn("project.last_analysis?.input", html)
        self.assertIn('step.type==="review"?step.preview.join("\\n")', html)
        self.assertIn('textarea.addEventListener("input",event=>{', html)

    def test_paid_ip12_ai_routes_follow_membership_enforcement(self):
        source = CORE.read_text(encoding="utf-8") + CORE.with_name("digital_ip.py").read_text(encoding="utf-8")
        self.assertIn("_membership_enforcement_enabled", source)
        self.assertIn("_digital_ip_membership_required(user)", source)
        self.assertIn('"code": "membership_required"', source)

    def test_upload_mime_extension_fallbacks(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        html = PAGE.read_text(encoding="utf-8")
        source = re.search(r"const UPLOAD_MIME = .*?;", html).group(0) + "\n" + re.search(r"function uploadMime\(file\)\{.*?\}", html).group(0)
        names = ["a.pdf", "a.docx", "a.pptx", "a.xlsx", "a.md", "a.jpeg"]
        script = source + "\nconsole.log(JSON.stringify(%s.map(name=>uploadMime({name,type:'application/octet-stream'}))));" % json.dumps(names)
        got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got, ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/markdown", "image/jpeg"])
        self.assertIn("去既有图片工具", html)
        self.assertIn("去既有视频工具", html)
        self.assertIn('project?.status==="confirmed"', html)

    def test_inspiration_card_opens_ip12_not_video(self):
        inspiration = PAGE.parent / "inspiration.html"
        html = inspiration.read_text(encoding="utf-8")
        self.assertIn('href="ip12.html"', html)
        self.assertIn("开始制作", html)


if __name__ == "__main__":
    unittest.main()
