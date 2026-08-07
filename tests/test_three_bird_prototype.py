import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = ROOT / "prototypes" / "three-bird-hero"


class ThreeBirdPrototypeTest(unittest.TestCase):
    def test_three_bird_contract(self):
        html = (PROTOTYPE / "index.html").read_text(encoding="utf-8")
        css = (PROTOTYPE / "style.css").read_text(encoding="utf-8")
        script = (PROTOTYPE / "experience.js").read_text(encoding="utf-8")

        self.assertIn("three@0.185.1", html)
        self.assertIn("animejs@4.5.0", html)
        self.assertIn("data-three-stage", html)
        self.assertIn('type="module"', html)
        self.assertIn("MeshPhysicalMaterial", script)
        self.assertIn("UnrealBloomPass", script)
        self.assertIn("animejs/adapters/three", script)
        self.assertIn("animate(glass", script)
        self.assertIn("GLTFLoader", script)
        self.assertIn("huangque-eagle.glb", script)
        self.assertIn("AnimationMixer", script)
        self.assertNotIn("wingGeometry", script)
        self.assertNotIn("const eye", script)
        self.assertTrue((PROTOTYPE / "public" / "assets" / "huangque-eagle.glb").is_file())
        self.assertIn("transmission: .18", script)
        self.assertIn("iridescence: 1", script)
        self.assertIn("__threeBirdCheck", script)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("bird-fallback", html + css)
        self.assertNotIn("data-light-field", html)


if __name__ == "__main__":
    unittest.main()
