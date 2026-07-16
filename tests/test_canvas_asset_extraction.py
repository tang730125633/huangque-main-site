import hashlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "site" / "workbench" / "canvas.html"
CSS_PATH = ROOT / "site" / "workbench" / "canvas" / "canvas.css"
APP_PATH = ROOT / "site" / "workbench" / "canvas" / "canvas-app.js"

EXPECTED_CSS_SHA256 = "96c2cf4a29c2fcd04113c920f198783f07a2794d3a6959582986b46a95353396"
EXPECTED_APP_SHA256 = "4e864ecf3e7045d0fa9f64d212b35f55e41838bd2d0fb03e2f4b5e71883c2238"


def normalized_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CanvasAssetExtractionTests(unittest.TestCase):
    def test_inline_payloads_are_external_and_unchanged(self):
        self.assertTrue(CSS_PATH.is_file(), CSS_PATH)
        self.assertTrue(APP_PATH.is_file(), APP_PATH)
        self.assertEqual(EXPECTED_CSS_SHA256, normalized_sha256(CSS_PATH))
        self.assertEqual(EXPECTED_APP_SHA256, normalized_sha256(APP_PATH))

        html = HTML_PATH.read_text(encoding="utf-8")
        self.assertNotRegex(html, r"(?s)<style>.*?</style>")
        self.assertNotRegex(html, r"(?s)<script>\s*/\* 节点生产画布")

    def test_canvas_assets_are_versioned_and_loaded_in_order(self):
        html = HTML_PATH.read_text(encoding="utf-8")
        css = re.search(r'href="canvas/canvas\.css\?v=([0-9a-f]{8})"', html)
        app = re.search(r'src="canvas/canvas-app\.js\?v=([0-9a-f]{8})"', html)
        self.assertIsNotNone(css, "canvas stylesheet must have a content stamp")
        self.assertIsNotNone(app, "canvas application script must have a content stamp")
        self.assertLess(html.index("cloud-shell.js?v="), html.index("canvas-collab-sync.js?v="))
        self.assertLess(html.index("canvas-collab-sync.js?v="), html.index("canvas/canvas-app.js?v="))


if __name__ == "__main__":
    unittest.main()
