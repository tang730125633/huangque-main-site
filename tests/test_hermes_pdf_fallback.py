import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
HERMES = ROOT / "server" / "hermes_ip12"


@unittest.skipUnless(
    importlib.util.find_spec("reportlab") and importlib.util.find_spec("pypdf"),
    "reportlab and pypdf are required",
)
class HermesPdfFallbackTests(unittest.TestCase):
    def test_browser_free_renderer_produces_a_parseable_editorial_report(self):
        import sys
        sys.path.insert(0, str(HERMES))
        from pdf_fallback import (
            _styled_lines, _wrap, render_foundation_consulting_pdf, render_foundation_pdf_fallback,
        )
        from pypdf import PdfReader

        content = "# 周岚 IP 定位初稿\n" + "\n".join(
            "## 第%d节\n- 事实原话：整理不是把东西藏起来，而是让人少一点被混乱追着跑。" % index
            for index in range(1, 45)
        ) + "\n### 传播表达建议（AI包装建议）\n#### 故事传播卡\nP0 先记录真实问题"
        with tempfile.TemporaryDirectory() as tmp:
            path = render_foundation_pdf_fallback(content, Path(tmp) / "report.pdf")
            self.assertGreater(path.stat().st_size, 10_000)
            reader = PdfReader(path, strict=True)
            self.assertTrue(6 <= len(reader.pages) <= 10)
            self.assertIn("周岚", "".join(page.extract_text() or "" for page in reader.pages))
            self.assertEqual(
                [kind for kind, _ in _styled_lines("### 传播表达建议（AI包装建议）\n#### 故事传播卡\nP0 先记录")],
                ["advice", "card_title", "priority"],
            )
            self.assertEqual(_styled_lines("#### 开场钩子")[0][0], "card_detail")
            self.assertEqual(_styled_lines("#### P0｜起步")[0][0], "card_title")
            self.assertEqual(
                [kind for kind, _ in _styled_lines("## 首页｜IP结论总览\n#### 定位\n身份定位：顾问")],
                ["section", "summary_card_title", "summary_card_body"],
            )

            class FixedWidth:
                @staticmethod
                def stringWidth(text, *_):
                    return len(text) * 10

            self.assertEqual(_wrap("一二。", "font", 10, 20, FixedWidth()), ["一二。"])

            consulting = render_foundation_consulting_pdf(
                "## 首页｜IP结论总览\n#### 定位\n内容顾问\n## 模块一｜定位诊断\n### 最终结论\n真实日常变内容",
                Path(tmp) / "consulting.pdf",
            )
            self.assertEqual(len(PdfReader(consulting, strict=True).pages), 2)

            renderer = shutil.which("pdftoppm")
            if renderer and importlib.util.find_spec("PIL"):
                from PIL import Image
                preview = Path(tmp) / "preview"
                subprocess.run(
                    [renderer, "-f", "1", "-singlefile", "-png", str(path), str(preview)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.assertLess(Image.open(preview.with_suffix(".png")).convert("L").getextrema()[0], 240)


if __name__ == "__main__":
    unittest.main()
