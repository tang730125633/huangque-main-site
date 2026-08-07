import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = ROOT / "prototypes" / "organic-bird-hero"


class OrganicBirdPrototypeTest(unittest.TestCase):
    def test_organic_variant_contract(self):
        html = (PROTOTYPE / "index.html").read_text(encoding="utf-8")
        css = (PROTOTYPE / "style.css").read_text(encoding="utf-8")
        script = (PROTOTYPE / "experience.js").read_text(encoding="utf-8")

        self.assertIn("纸上生长", html)
        self.assertIn('class="organic-scene"', html)
        self.assertEqual(html.count('class="root-list"'), 1)
        self.assertEqual(html.count("<article><span>0"), 4)
        self.assertNotIn("glass-bird.webp", html + css)
        self.assertNotIn("bird-body", html + css)
        self.assertIn('class="growth-mark"', html)
        self.assertNotIn("WebGL", html + css + script)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("IntersectionObserver", script)
        self.assertIn("__organicBirdStatus", script)
        self.assertIn("@media (max-width:560px)", css)

    def test_deployable_preview_matches_prototype(self):
        preview = ROOT / "site" / "previews" / "organic-bird-hero"
        self.assertEqual(
            (preview / "index.html").read_text(encoding="utf-8"),
            (PROTOTYPE / "index.html").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (preview / "experience.js").read_text(encoding="utf-8"),
            (PROTOTYPE / "experience.js").read_text(encoding="utf-8"),
        )
        deployed_css = (preview / "style.css").read_text(encoding="utf-8").replace(
            "../../assets/fonts/", "../../site/assets/fonts/"
        )
        self.assertEqual(
            deployed_css,
            (PROTOTYPE / "style.css").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
