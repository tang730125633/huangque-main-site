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
    def test_browser_free_renderer_produces_eight_parseable_pages(self):
        import sys
        sys.path.insert(0, str(HERMES))
        from pdf_fallback import _styled_lines, render_foundation_pdf_fallback
        from pypdf import PdfReader

        content = "# 周岚 IP 定位初稿\n" + "\n".join(
            "## 第%d节\n- 事实原话：整理不是把东西藏起来，而是让人少一点被混乱追着跑。" % index
            for index in range(1, 45)
        ) + "\n### 传播表达建议（AI包装建议）\n#### 故事传播卡\nP0 先记录真实问题"
        with tempfile.TemporaryDirectory() as tmp:
            path = render_foundation_pdf_fallback(content, Path(tmp) / "report.pdf")
            self.assertGreater(path.stat().st_size, 10_000)
            reader = PdfReader(path, strict=True)
            self.assertEqual(len(reader.pages), 8)
            self.assertIn("周岚", "".join(page.extract_text() or "" for page in reader.pages))
            self.assertEqual(
                [kind for kind, _ in _styled_lines("### 传播表达建议（AI包装建议）\n#### 故事传播卡\nP0 先记录")],
                ["advice", "card_title", "priority"],
            )

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
