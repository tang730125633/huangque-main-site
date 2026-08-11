# -*- coding: utf-8 -*-
"""首页视频横幅的最小契约测试。"""
import hashlib
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class HomeVideoBannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "site/index.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "site/homepage.css").read_text(encoding="utf-8")
        cls.liquid_glass = (ROOT / "site/homepage-liquid-glass.js").read_text(encoding="utf-8")
        cls.particles = (ROOT / "site/homepage-particles.js").read_text(encoding="utf-8")
        cls.bird_points = ROOT / "site/assets/home/bird-points.bin"
        cls.three_core = ROOT / "site/vendor/three.core.min.js"
        cls.three_module = ROOT / "site/vendor/three.module.min.js"
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
        self.assertIn("data-nav-liquid-glass", self.html)
        self.assertIn("/homepage-liquid-glass.js", self.html)
        self.assertIn("prefers-reduced-motion:reduce", self.liquid_glass)
        self.assertIn("refractedPoint", self.liquid_glass)
        self.assertIn("hero-liquid-glass-ready", self.liquid_glass)
        self.assertIn("nav-liquid-glass-ready", self.liquid_glass)
        self.assertIn(".nav-shell>.nav-liquid-glass{position:absolute;z-index:0;inset:0;width:100%;height:100%;border-radius:inherit", self.css)
        self.assertIn("float lensDistance(vec2 p){float edgeInset=2.0", self.liquid_glass)
        self.assertIn("gl_FragColor=vec4(color*alpha,alpha)", self.liquid_glass)

    def test_nav_uses_function_drawers_instead_of_page_anchors(self):
        self.assertEqual(self.html.count('<button type="button" data-nav-trigger='), 5)
        self.assertEqual(self.html.count('<section class="nav-drawer"'), 5)
        self.assertIn("/workbench/video", self.html)
        self.assertIn("/workbench/ip12", self.html)
        self.assertIn("panel.inert = !active", self.html)
        self.assertIn("data-nav-slider", self.html)
        self.assertIn("nav-slider-trail", self.html)
        self.assertIn("navSliderBlob.animate", self.html)
        self.assertIn("scaleX(1.34) scaleY(.84)", self.html)
        self.assertIn("setTimeout(() => setOpenNav(''), 320)", self.html)
        self.assertIn('class="language-switcher"', self.html)
        self.assertIn('data-language="en"', self.html)
        self.assertIn('.nav-shell{--glass-x:50%;--glass-y:-30%;position:relative;isolation:isolate;overflow:visible', self.css)
        self.assertIn("localStorage.setItem('huangque-language', next)", self.html)
        self.assertIn("nav-current-backdrop", self.liquid_glass)
        self.assertIn("key==='nav'&&!navOverHero", self.liquid_glass)
        self.assertIn(".nav-drawer-layer{position:absolute;top:100%", self.css)
        nav = self.html.split('<header class="site-header">', 1)[1].split("</header>", 1)[0]
        for anchor in ('href="#flow"', 'href="#ip12"', 'href="#agent"', 'href="#video"', 'href="#cli"'):
            self.assertNotIn(anchor, nav)

    def test_background_particles_use_the_real_scroll_driven_point_cloud_bird(self):
        self.assertIn('data-particle-story', self.html)
        particle_stamp = hashlib.md5(
            self.particles.replace("\r\n", "\n").encode("utf-8")
        ).hexdigest()[:8]
        self.assertIn(
            f'type="module" src="/homepage-particles.js?v={particle_stamp}"',
            self.html,
        )
        self.assertIn('.page-particle-stage{position:fixed;z-index:2', self.css)
        self.assertIn("ShaderMaterial", self.particles)
        self.assertIn("uPointerStrength", self.particles)
        self.assertIn("* uPointerStrength;", self.particles)
        self.assertIn("data-particle-scene", self.particles)
        self.assertIn("const INITIAL_STORY_TIMELINE = 0.82", self.particles)
        self.assertIn(
            "scrollTarget = INITIAL_STORY_TIMELINE + progress * (5 - INITIAL_STORY_TIMELINE)",
            self.particles,
        )
        self.assertTrue(self.bird_points.is_file())
        self.assertEqual(self.bird_points.stat().st_size, 65536 * 3 * 4)

    def test_particle_renderer_vendors_its_mit_three_modules(self):
        for module in (self.three_core, self.three_module):
            self.assertTrue(module.is_file(), module)
            self.assertLess(module.stat().st_size, 500 * 1024)
            header = module.read_text(encoding="utf-8")[:160]
            self.assertIn("@license", header)
            self.assertIn("SPDX-License-Identifier: MIT", header)
        self.assertIn(
            'from"./three.core.min.js"',
            self.three_module.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
