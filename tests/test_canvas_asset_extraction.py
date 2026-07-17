import hashlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "site" / "workbench" / "canvas.html"
CSS_PATH = ROOT / "site" / "workbench" / "canvas" / "canvas.css"
APP_PATH = ROOT / "site" / "workbench" / "canvas" / "canvas-app.js"

EXPECTED_CSS_SHA256 = "96c2cf4a29c2fcd04113c920f198783f07a2794d3a6959582986b46a95353396"


def normalized_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CanvasAssetExtractionTests(unittest.TestCase):
    def test_inline_payloads_remain_external(self):
        self.assertTrue(CSS_PATH.is_file(), CSS_PATH)
        self.assertTrue(APP_PATH.is_file(), APP_PATH)
        self.assertEqual(EXPECTED_CSS_SHA256, normalized_sha256(CSS_PATH))

        html = HTML_PATH.read_text(encoding="utf-8")
        self.assertNotRegex(html, r"(?s)<style>.*?</style>")
        self.assertNotRegex(html, r"(?s)<script>\s*/\* 节点生产画布")
        self.assertRegex(html, r'src="canvas/canvas-app\.js\?v=[0-9a-f]{8}"')

    def test_five_modules_are_versioned_and_loaded_in_exact_order(self):
        html = HTML_PATH.read_text(encoding="utf-8")
        css = re.search(r'href="canvas/canvas\.css\?v=([0-9a-f]{8})"', html)
        self.assertIsNotNone(css, "canvas stylesheet must have a content stamp")
        assets = [
            "canvas/canvas-graph.js?v=",
            "canvas/canvas-state.js?v=",
            "canvas/canvas-storage.js?v=",
            "canvas/canvas-api.js?v=",
            "canvas/canvas-export.js?v=",
            "canvas-collab-sync.js?v=",
            "canvas/canvas-app.js?v=",
        ]
        positions = [html.index(asset) for asset in assets]
        self.assertEqual(sorted(positions), positions)
        for asset in assets:
            self.assertRegex(html, re.escape(asset) + r"[0-9a-f]{8}")

    def test_app_uses_modules_instead_of_legacy_payloads(self):
        app = APP_PATH.read_text(encoding="utf-8")
        exporter = (ROOT / "site" / "workbench" / "canvas" / "canvas-export.js").read_text(encoding="utf-8")
        for legacy in (
            "function exportRoundRect(",
            "function exportWrappedText(",
            "function loadExportImage(",
            "function exportNodeImage(",
            "function drawExportNode(",
        ):
            self.assertNotIn(legacy, app)
        self.assertNotRegex(app, r"\bfetch\(")
        for call in (
            "canvasExporter.serializeTemplate(",
            "canvasExporter.parseTemplate(",
            "canvasExporter.safeFilename(",
            "canvasExporter.exportJpeg(",
        ):
            self.assertIn(call, app)
        self.assertIn("function renderExportPanel(", app)
        self.assertIn("function updateState(", app)
        self.assertNotIn("portCenter:portCenter", app)
        self.assertRegex(app, r"exportEdges=edges\.map\(")
        self.assertNotRegex(exporter, r"\b(?:document|window)\b")
        self.assertNotIn("portCenter", exporter)


if __name__ == "__main__":
    unittest.main()
