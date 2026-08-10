import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


class HomeOrbitGalleryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (SITE / "index.html").read_text(encoding="utf-8")
        cls.css = (SITE / "homepage.css").read_text(encoding="utf-8")
        cls.js = (SITE / "assets" / "home" / "orbit-gallery.js").read_text(encoding="utf-8")
        cls.manifest = json.loads(
            (SITE / "assets" / "home" / "orbit-gallery" / "gallery.json").read_text(encoding="utf-8")
        )

    def test_replaces_the_three_card_grid_with_the_orbit_gallery(self):
        self.assertIn("data-orbit-gallery", self.html)
        self.assertIn("data-gallery-track", self.html)
        self.assertIn("data-gallery-preview", self.html)
        self.assertIn("data-gallery-fallback", self.html)
        self.assertNotIn('class="page-shell sample-grid"', self.html)
        self.assertIn("orbit-gallery.js?v=20260810-concave4", self.html)

    def test_manifest_contains_all_requested_media(self):
        items = self.manifest["items"]
        self.assertEqual(len(items), 21)
        self.assertEqual(len({item["id"] for item in items}), 21)
        self.assertEqual(sum(item["type"] == "image" for item in items), 8)
        self.assertEqual(sum(item["type"] == "video" for item in items), 13)

    def test_every_manifest_asset_is_local_and_present(self):
        for item in self.manifest["items"]:
            self.assertTrue(item["src"].startswith("/assets/home/orbit-gallery/"), item["src"])
            self.assertTrue((SITE / item["src"].lstrip("/")).is_file(), item["src"])
            if item["type"] == "video":
                self.assertTrue(item["poster"].startswith("/assets/home/orbit-gallery/"), item["poster"])
                self.assertTrue((SITE / item["poster"].lstrip("/")).is_file(), item["poster"])

    def test_only_the_center_video_is_loaded_and_played(self):
        self.assertIn("index === state.activeIndex", self.js)
        self.assertIn("video.removeAttribute('src')", self.js)
        self.assertIn("video.play().catch", self.js)
        self.assertIn("IntersectionObserver", self.js)
        self.assertIn("state.inViewport", self.js)

    def test_drag_keyboard_dialog_and_fallback_contracts(self):
        self.assertIn("pointermove", self.js)
        self.assertIn("ArrowLeft", self.js)
        self.assertIn("ArrowRight", self.js)
        self.assertIn("media.controls = true", self.js)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('data-gallery-state="loading"', self.html)
        self.assertIn('data-gallery-state="ready"', self.css)

    def test_orbit_uses_a_visible_concave_vertical_arc(self):
        self.assertIn("const arcLift = compact", self.js)
        self.assertIn("? 168", self.js)
        self.assertIn("Math.min(680, Math.max(470, innerWidth * 0.34))", self.js)
        self.assertIn("Math.pow(curve, compact ? 0.68 : 0.5)", self.js)
        self.assertIn("const y = -verticalCurve * arcLift", self.js)

    def test_orbit_uses_strong_depth_perspective_and_side_wrap(self):
        self.assertIn("Math.min(compact ? 420 : 820", self.js)
        self.assertIn("const depthExpansion = compact ? 1.65 : 1.85", self.js)
        self.assertIn("Math.min(82, Math.abs(angle) * (compact ? 94 : 112))", self.js)
        self.assertIn("perspective:880px", self.css)
        self.assertIn("perspective-origin:50% 60%", self.css)

    def test_low_resource_modes_keep_static_media_available(self):
        self.assertIn("connection?.saveData", self.js)
        self.assertIn("navigator.deviceMemory", self.js)
        self.assertIn("prefers-reduced-motion: reduce", self.js)
        self.assertIn("!conserveResources", self.js)
        self.assertIn("orbit-gallery-fallback", self.css)


if __name__ == "__main__":
    unittest.main()
