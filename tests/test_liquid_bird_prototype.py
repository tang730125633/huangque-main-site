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

        self.assertIn("data-light-field", html)
        self.assertIn("public/assets/glass-bird.webp", html)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("@media (max-width:560px)", css)
        self.assertIn("__liquidBirdCheck", script)
        self.assertLess(bird.stat().st_size, 300_000)


if __name__ == "__main__":
    unittest.main()
