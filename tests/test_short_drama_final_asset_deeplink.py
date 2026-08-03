from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = (ROOT / "site/workbench/assets.html").read_text(encoding="utf-8")
CANVAS = (
    ROOT
    / "site/workbench/canvas/canvas-short-drama-workspace.js"
).read_text(encoding="utf-8")


class ShortDramaFinalAssetDeepLinkTests(unittest.TestCase):
    def test_canvas_uses_canonical_asset_route(self):
        self.assertIn("'/workbench/assets?'+query.join('&')", CANVAS)
        self.assertNotIn("../assets.html?asset_id=", CANVAS)
        self.assertIn("'project_id='+encodeURIComponent", CANVAS)
        self.assertIn("'board_id='+encodeURIComponent", CANVAS)

    def test_asset_page_loads_and_focuses_short_drama_asset(self):
        self.assertIn("params.get('asset_id')", ASSETS)
        self.assertIn("params.get('board_id')", ASSETS)
        self.assertIn(
            "'/api/gen/short-drama/final-assets/'"
            "+encodeURIComponent(focusAssetId)",
            ASSETS,
        )
        self.assertIn("'X-Canvas-Board-Id'", ASSETS)
        self.assertIn("data-asset-id", ASSETS)
        self.assertIn("source_type==='short_drama_final'", ASSETS)

    def test_short_drama_asset_avoids_generic_mutations(self):
        self.assertIn("if(!isShortDramaFinal)", ASSETS)
        self.assertIn("short-drama-final-asset", ASSETS)
        self.assertIn(
            "x.source_type==='short_drama_final'",
            ASSETS,
        )
        self.assertIn(
            "visibleItems=list.map(function(x){ return bulkAssetMeta(x,kind); })"
            ".filter(Boolean)",
            ASSETS,
        )
        self.assertIn(
            "isBulkEligible(x,x.kind)",
            ASSETS,
        )


if __name__ == "__main__":
    unittest.main()
