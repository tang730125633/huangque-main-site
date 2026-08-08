# -*- coding: utf-8 -*-
"""首页动态月球与质感层的契约测试。

覆盖 feature/home-dynamic-moon 的新行为:
- WebGL 3D 月球(canvas + moon3d.js + 4K/2K 纹理 + 静态图兜底)
- 全页星空背景(starfield fixed)
- 动效 reduced-motion 兜底
"""
import os
import struct
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _webp_size(rel):
    """读取本项目无损/有损 WebP 的画布尺寸，不引入 Pillow 测试依赖。"""
    with open(os.path.join(ROOT, rel), "rb") as fh:
        data = fh.read(32)
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise AssertionError(f"not a WebP file: {rel}")
    kind = data[12:16]
    if kind == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
    elif kind == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            raise AssertionError(f"invalid VP8 frame header: {rel}")
        width, height = struct.unpack_from("<HH", data, 26)
        width &= 0x3FFF
        height &= 0x3FFF
    else:
        raise AssertionError(f"unsupported WebP encoding {kind!r}: {rel}")
    return width, height


class DynamicMoonMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _read("site/index.html")

    def test_canvas_and_fallback_img_coexist(self):
        self.assertIn('<canvas class="moon-3d">', self.html)
        self.assertIn('<img src="/assets/home/moon.webp" alt="">', self.html)

    def test_moon3d_script_loaded(self):
        self.assertIn('/assets/home/moon3d.js', self.html)


class DynamicMoonStyleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = _read("site/homepage.css")

    def test_starfield_is_fixed_fullpage_background(self):
        self.assertIn(".starfield{position:fixed", self.css)

    def test_gl_fallback_rules(self):
        self.assertIn(".hero-moon.gl-on img{visibility:hidden}", self.css)
        self.assertIn(".hero-moon.gl-on::after{display:none}", self.css)

    def test_canvas_fills_moon_box(self):
        self.assertIn(".hero-moon canvas.moon-3d{position:absolute;inset:0", self.css)

    def test_motion_gated_by_no_preference(self):
        block = self.css.split("@media (prefers-reduced-motion:no-preference){", 1)[1]
        self.assertIn("moon-drift", block)
        self.assertIn("moon-glow", block)
        self.assertIn("moon-shade", block)
        self.assertIn("twinkle", block)
        self.assertIn("heroLineIn", block)
        self.assertIn("heroSheen", block)


class Moon3dScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read("site/assets/home/moon3d.js")

    def test_webgl_setup_and_texture(self):
        self.assertIn("getContext('webgl'", self.js)
        self.assertIn("/assets/home/moon_map.webp", self.js)
        self.assertIn("/assets/home/moon_map_2k.webp", self.js)

    def test_4k_texture_and_2k_fallback_dimensions(self):
        self.assertEqual(_webp_size("site/assets/home/moon_map.webp"), (4096, 2048))
        self.assertEqual(_webp_size("site/assets/home/moon_map_2k.webp"), (2048, 1024))

    def test_texture_quality_guards(self):
        self.assertIn("gl.MAX_TEXTURE_SIZE", self.js)
        self.assertIn("navigator.connection", self.js)
        self.assertIn("EXT_texture_filter_anisotropic", self.js)
        self.assertIn("const LAT = 128, LON = 128", self.js)

    def test_static_fallback_when_no_webgl(self):
        self.assertIn("if (!gl) { canvas.remove(); return; }", self.js)

    def test_reduced_motion_and_visibility_guard(self):
        self.assertIn("prefers-reduced-motion: reduce", self.js)
        self.assertIn("IntersectionObserver", self.js)

    def test_rotation_present(self):
        self.assertIn("rotY(angle)", self.js)


if __name__ == "__main__":
    unittest.main()
