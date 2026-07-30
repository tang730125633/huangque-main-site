import hashlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "site" / "workbench" / "canvas.html"
CSS_PATH = ROOT / "site" / "workbench" / "canvas" / "canvas.css"
APP_PATH = ROOT / "site" / "workbench" / "canvas" / "canvas-app.js"

EXPECTED_CSS_SHA256 = "fd5b02f73234ae86fb93309b305bd6db7f55009a4ef14629b709bb9954b5e47a"


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

    def test_side_toolbar_has_pointer_keyboard_and_reduced_motion_feedback(self):
        css = CSS_PATH.read_text(encoding="utf-8")
        self.assertIn(".nc-side-tools:hover,.nc-side-tools:focus-within", css)
        self.assertIn(".nc-side-tool:hover,.nc-side-tool.on,.nc-side-tool:focus-visible", css)
        self.assertIn("content:attr(aria-label)", css)
        self.assertIn("@media (prefers-reduced-motion:reduce)", css)

    def test_grid_and_default_nodes_follow_the_centered_viewport(self):
        css = CSS_PATH.read_text(encoding="utf-8")
        app = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("--grid-minor:24px", css)
        self.assertIn("function syncCanvasGrid()", app)
        self.assertIn("fallback=x==null||y==null?viewportNodePoint():null", app)
        self.assertIn("function centerEmptyView()", app)
        self.assertIn("((minX+maxX)/2)*zoom-canvas.clientWidth/2", app)
        self.assertIn("CANVAS_VIEW_PAD=1200", app)
        self.assertIn("margin:1200px", css)
        self.assertIn("offset=(Object.keys(nodes).length%5)*36", app)

    def test_agent_workspace_stays_visible_without_covering_the_canvas(self):
        html = HTML_PATH.read_text(encoding="utf-8")
        css = CSS_PATH.read_text(encoding="utf-8")
        app = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('id="ncFsAgent"', html)
        self.assertIn('data-agent-start=', html)
        self.assertIn(".nc-canvas-shell.agent-open .nc-canvas", css)
        self.assertIn(".nc-canvas-shell.agent-open .nc-empty", css)
        self.assertIn("openSidePanel('agent',true)", app)

    def test_portable_infinite_canvas_interactions_are_wired(self):
        html = HTML_PATH.read_text(encoding="utf-8")
        css = CSS_PATH.read_text(encoding="utf-8")
        app = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('id="ncGuideX"', html)
        self.assertIn(".nc-resize-handle", css)
        self.assertIn("function connectedNodeMenuItems(", app)
        self.assertIn("graphApi.resizeNodeRect(", app)
        self.assertIn("graphApi.alignmentGuides(", app)

    def test_canvas_modules_are_versioned_and_loaded_in_exact_order(self):
        html = HTML_PATH.read_text(encoding="utf-8")
        css = re.search(r'href="canvas/canvas\.css\?v=([0-9a-f]{8})"', html)
        self.assertIsNotNone(css, "canvas stylesheet must have a content stamp")
        assets = [
            "canvas/canvas-graph.js?v=",
            "canvas/canvas-state.js?v=",
            "canvas/canvas-storage.js?v=",
            "canvas/canvas-api.js?v=",
            "canvas/canvas-agent.js?v=",
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
