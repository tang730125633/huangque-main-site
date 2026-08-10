import json
import pathlib
import re
import subprocess
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
        self.assertIn('homepage.css?v=20260810-orbitfix2', self.html)
        self.assertIn("orbit-gallery.js?v=20260810-orbitfix2", self.html)
        self.assertIn("gallery.json?v=20260810-orbitfix2", self.html)

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
        self.assertIn("const playAttempt = video.play()", self.js)
        self.assertIn("entry.card.classList.add('is-playing')", self.js)
        self.assertIn("IntersectionObserver", self.js)
        self.assertIn("state.inViewport", self.js)

    def test_drag_keyboard_dialog_and_fallback_contracts(self):
        self.assertIn("pointermove", self.js)
        self.assertIn("ArrowLeft", self.js)
        self.assertIn("ArrowRight", self.js)
        self.assertIn("media.controls = true", self.js)
        root_tag = re.search(r'<div\s+class="orbit-gallery"(?P<attrs>.*?)>', self.html, re.S)
        self.assertIsNotNone(root_tag)
        self.assertNotIn('aria-live="polite"', root_tag.group("attrs"))
        self.assertNotIn('tabindex="0"', root_tag.group("attrs"))
        self.assertIn('<p data-gallery-status>黄雀图片与视频创作样片</p>', self.html)
        self.assertIn('data-gallery-state="fallback"', self.html)
        self.assertNotIn('data-gallery-state="loading"', self.html)
        self.assertIn('data-gallery-state="ready"', self.css)

    def test_runtime_pointer_focus_loading_and_motion_behaviour(self):
        result = subprocess.run(
            ["node", str(ROOT / "tests" / "test_home_orbit_gallery_behavior.js")],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
        self.assertIn("MEDIA_LOAD_RANGE", self.js)
        self.assertIn("initObserver", self.js)
        self.assertIn("previewAutoplayAllowed", self.js)
        self.assertIn("!conserveResources && !reducedMotion.matches", self.js)
        self.assertNotIn("AUTO_SPEED", self.js)

    def test_idle_animation_is_demand_driven_and_media_failures_are_explicit(self):
        self.assertIn("state.rafId = requestAnimationFrame(animate)", self.js)
        self.assertNotIn("requestAnimationFrame(animate);\n  }\n\n  async function init", self.js)
        self.assertIn("handleMediaFailure", self.js)
        self.assertIn("probe.onerror", self.js)
        self.assertIn("showPreviewMediaError", self.js)
        self.assertIn("setFallbackState", self.js)


if __name__ == "__main__":
    unittest.main()
