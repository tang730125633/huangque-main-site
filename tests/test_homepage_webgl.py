import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HomepageWebglTest(unittest.TestCase):
    def test_story_scene_contract(self):
        html = (ROOT / "site/index.html").read_text()
        css = (ROOT / "site/homepage.css").read_text()
        script = (ROOT / "site/homepage-webgl.js").read_text()

        self.assertIn('data-webgl-stage', html)
        self.assertIn('data-scene="flight"', html)
        self.assertIn('data-scene="release"', html)
        self.assertNotIn('hero-moon', html)
        self.assertIn('scenes.length === 7', html)
        self.assertIn('prefers-reduced-motion', css + script)
        self.assertIn('webglcontextlost', script)
        self.assertNotIn('Math.random', script)


if __name__ == "__main__":
    unittest.main()
