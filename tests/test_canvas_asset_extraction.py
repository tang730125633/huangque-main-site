import hashlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "site" / "workbench" / "canvas.html"
CSS_PATH = ROOT / "site" / "workbench" / "canvas" / "canvas.css"
APP_PATH = ROOT / "site" / "workbench" / "canvas" / "canvas-app.js"
SHORT_DRAMA_CSS = sorted((ROOT / "site" / "workbench" / "canvas").glob("canvas-short-drama*.css"))

EXPECTED_CSS_SHA256 = "56209e89ee4beb432419de076f1a91fac19ebc47babf24d4689f67b11f3b3fd0"


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
        self.assertIn("if(empty&&empty.parentNode!==inner) inner.appendChild(empty);", app)
        self.assertIn("empty.classList.toggle('on', count===0)", app)
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

    def test_storyboard_is_a_derived_view_of_the_existing_workflow(self):
        html = HTML_PATH.read_text(encoding="utf-8")
        css = CSS_PATH.read_text(encoding="utf-8")
        app = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('id="ncStoryboard"', html)
        self.assertIn('data-canvas-view="workflow"', html)
        self.assertIn('data-canvas-view="story"', html)
        self.assertIn("graphApi.topologicalOrder(", app)
        self.assertIn("function renderStoryboard()", app)
        self.assertIn("function setCanvasView(view)", app)
        self.assertIn(".nc-canvas-shell.story-view .nc-storyboard", css)

    def test_short_drama_workspaces_use_the_canvas_dark_palette(self):
        css = "\n".join(path.read_text(encoding="utf-8") for path in SHORT_DRAMA_CSS)
        self.assertIn("#070b13", css)
        self.assertIn("#e7b24c", css)
        self.assertIn("#94a4bb", css)
        self.assertNotRegex(
            css,
            r"background(?:-color)?\s*:[^;{}]*#(?:fff|ffffff|fff5dd|ffedc8|fffaf0)\b",
        )

    def test_portable_infinite_canvas_interactions_are_wired(self):
        html = HTML_PATH.read_text(encoding="utf-8")
        css = CSS_PATH.read_text(encoding="utf-8")
        app = APP_PATH.read_text(encoding="utf-8")
        self.assertIn('id="ncGuideX"', html)
        self.assertIn(".nc-resize-handle", css)
        self.assertIn("function connectedNodeMenuItems(", app)
        self.assertIn("graphApi.resizeNodeRect(", app)
        self.assertIn("graphApi.alignmentGuides(", app)

    def test_canvas_creation_and_context_menus_have_distinct_roles(self):
        html = HTML_PATH.read_text(encoding="utf-8")
        css = CSS_PATH.read_text(encoding="utf-8")
        app = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("双击画布空白处添加节点", html)
        self.assertIn("function showAddNodeMenu(", app)
        self.assertRegex(app, r"canvas\.addEventListener\('dblclick',[\s\S]*?showAddNodeMenu\(canvasPointFromClient\(e\)")
        self.assertRegex(app, r"function menuForCanvas\(e\)[\s\S]*?label:'上传图片'[\s\S]*?label:'添加节点'[\s\S]*?label:'撤销'[\s\S]*?label:'重做'[\s\S]*?label:'粘贴'")
        self.assertIn("disabled:!history.canUndo()", app)
        self.assertIn("disabled:!history.canRedo()", app)
        self.assertIn("disabled:!clipNode", app)
        self.assertIn(".nc-menu-shortcut", css)
        self.assertIn("var MENU_ICONS=", app)
        self.assertIn("function menuIcon(", app)
        self.assertIn("@keyframes nc-menu-icon-draw", css)
        self.assertIn("@keyframes nc-menu-icon-spark", css)
        self.assertIn("prefers-reduced-motion:reduce", css)

    def test_account_assets_drag_to_matching_canvas_nodes(self):
        app = APP_PATH.read_text(encoding="utf-8")
        css = CSS_PATH.read_text(encoding="utf-8")
        self.assertIn("draggable=\"'+(canEditCanvas()?'true':'false')+'\"", app)
        self.assertIn("btn.ondragstart=function(e)", app)
        self.assertIn("canvas.addEventListener('dragover'", app)
        self.assertIn("canvas.addEventListener('drop'", app)
        self.assertIn("!Object.keys(nodes).length&&(e.clientX<r.left||e.clientY<r.top)", app)
        self.assertIn("centerEmptyView();", app)
        self.assertRegex(app, r"type=asset\.type==='video'\?'videoAsset':'image'")
        self.assertIn("videoAsset:{name:'视频 · 素材'", app)
        self.assertIn("outputs.video=asset.url", app)
        self.assertIn("outputs.image=asset.url", app)
        self.assertIn(".nc-canvas.asset-drop-target", css)

    def test_selected_images_offer_animated_connected_draft_actions(self):
        app = APP_PATH.read_text(encoding="utf-8")
        css = CSS_PATH.read_text(encoding="utf-8")
        self.assertIn("function createImageActionDraft(", app)
        self.assertIn("function updateImageToolbar()", app)
        self.assertIn("data-image-action=\"portrait\"", app)
        self.assertIn("data-image-action=\"lighting\"", app)
        self.assertIn("data-image-action=\"angle\"", app)
        self.assertIn("data-image-action=\"grid\"", app)
        self.assertIn("data-image-action=\"video\"", app)
        self.assertIn("connectEdge({node:source.id,port:'image'}", app)
        self.assertIn("确认参数后再生成", app)
        draft = re.search(r"function createImageActionDraft\([\s\S]*?(?=\n  function createImageGenDraft)", app)
        self.assertIsNotNone(draft)
        self.assertNotIn("runNode(", draft.group(0))
        self.assertIn(".nc-image-toolbar button:hover .nc-icon-base", css)
        self.assertIn(".nc-image-toolbar button:focus-visible", css)
        self.assertRegex(css, r"prefers-reduced-motion:[^)]+\)[\s\S]*?\.nc-image-toolbar")

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
