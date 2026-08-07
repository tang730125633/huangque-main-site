import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = ROOT / "prototypes" / "liquid-bird-hero"


class LiquidBirdPrototypeTest(unittest.TestCase):
    def test_prototype_contract(self):
        html = (PROTOTYPE / "index.html").read_text(encoding="utf-8")
        css = (PROTOTYPE / "style.css").read_text(encoding="utf-8")
        script = (PROTOTYPE / "experience.js").read_text(encoding="utf-8")
        bird = PROTOTYPE / "public" / "assets" / "glass-bird.webp"
        preview = ROOT / "site" / "previews" / "liquid-bird-hero"

        self.assertIn("data-light-field", html)
        self.assertNotIn("data-cursor-wake", html)
        self.assertIn("public/assets/glass-bird.webp", html)
        self.assertIn("style.css?v=cursor10", html)
        self.assertIn("experience.js?v=cursor10", html)
        self.assertNotIn("<h1>", html)
        self.assertNotIn('class="actions"', html)
        self.assertEqual(html.count("<li><span>0"), 4)
        self.assertEqual(html.count('class="chapter"'), 4)
        self.assertIn('class="closing"', html)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("@media (max-width:560px)", css)
        self.assertNotIn("prism-wake", css)
        self.assertIn("__liquidBirdCheck", script)
        self.assertIn("uPointerActive", script)
        self.assertIn("uPointerTarget", script)
        self.assertIn("segmentDistance", script)
        self.assertIn("starField", script)
        self.assertIn("starField(vec2 point,vec2 cursor,float velocity)", script)
        self.assertIn("vec2 tangent", script)
        self.assertIn("mix(cursorNow,cursorTarget", script)
        self.assertIn("document.documentElement.scrollHeight - innerHeight", script)
        self.assertIn("IntersectionObserver", script)
        self.assertIn("sceneIndex", script)
        self.assertIn("float halo=", script)
        self.assertIn("radius=mix(.0045,.011", script)
        self.assertIn("targetY = 1 - event.clientY", script)
        self.assertIn("mix-blend-mode:screen", css)
        self.assertIn("position:fixed", css)
        self.assertIn("--orbit-x", css)
        self.assertIn("motion-ready", css)
        self.assertNotIn("bird-glint", html + css)
        self.assertNotIn("wakeDamping", script)
        self.assertNotIn("pointer:fine", script)
        self.assertLess(bird.stat().st_size, 300_000)
        self.assertEqual((preview / "index.html").read_text(encoding="utf-8"), html)
        self.assertEqual((preview / "experience.js").read_text(encoding="utf-8"), script)
        self.assertEqual((preview / "public/assets/glass-bird.webp").read_bytes(), bird.read_bytes())
        self.assertEqual(
            (preview / "style.css").read_text(encoding="utf-8").replace(
                "../../assets/fonts/", "../../site/assets/fonts/"
            ),
            css,
        )


if __name__ == "__main__":
    unittest.main()
