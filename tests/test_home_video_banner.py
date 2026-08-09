# -*- coding: utf-8 -*-
"""首页视频横幅的最小契约测试。"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class HomeVideoBannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "site/index.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "site/homepage.css").read_text(encoding="utf-8")
        cls.liquid_glass = (ROOT / "site/homepage-liquid-glass.js").read_text(encoding="utf-8")
        cls.videos = [
            ROOT / "site/assets/home/hero-banner-monochrome-eye.mp4",
            ROOT / "site/assets/home/hero-banner-ancient-courtyard.mp4",
        ]

    def test_video_replaces_moon(self):
        self.assertIn('<div class="hero-media" aria-hidden="true">', self.html)
        self.assertIn("hero-banner-monochrome-eye.mp4", self.html)
        self.assertIn("hero-banner-ancient-courtyard.mp4", self.html)
        self.assertEqual(self.html.count("<video class="), 2)
        self.assertIn("autoplay muted playsinline", self.html)
        self.assertNotIn("hero-moon", self.html)
        self.assertNotIn("moon3d.js", self.html)

    def test_video_assets_are_small_mp4_files(self):
        for video in self.videos:
            self.assertTrue(video.is_file())
            self.assertLess(video.stat().st_size, 5 * 1024 * 1024)
            self.assertIn(b"ftyp", video.read_bytes()[:32])

    def test_video_fills_hero_and_respects_reduced_motion(self):
        self.assertIn(".hero-media video{position:absolute;z-index:0;inset:0;width:100%;height:100%;object-fit:cover", self.css)
        self.assertIn("transition:opacity 1s ease", self.css)
        self.assertIn("video.addEventListener('ended', showNextHeroVideo)", self.html)
        self.assertIn("if (reducedMotion.matches)", self.html)
        self.assertIn("heroVideos.forEach(video => video.pause())", self.html)

    def test_liquid_glass_uses_pointer_driven_highlight(self):
        self.assertIn("Huang Que AI Hub", self.html)
        self.assertGreaterEqual(self.html.count("data-liquid-glass"), 5)
        self.assertIn("addEventListener('pointermove'", self.html)
        self.assertIn("at var(--glass-x) var(--glass-y)", self.css)

    def test_hero_cta_uses_optical_liquid_glass_with_fallback(self):
        self.assertIn("data-hero-liquid-glass", self.html)
        self.assertIn("/homepage-liquid-glass.js", self.html)
        self.assertIn("prefers-reduced-motion:reduce", self.liquid_glass)
        self.assertIn("refractedPoint", self.liquid_glass)
        self.assertIn("hero-liquid-glass-ready", self.liquid_glass)


if __name__ == "__main__":
    unittest.main()
