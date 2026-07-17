# -*- coding: utf-8 -*-
import base64
import pathlib
import sys
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import video


VIDEO_HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")
CORE_SOURCE = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 24).decode("ascii")
SEGMENTS = [
    (0.0, 1.8, "姐妹们 今天开始体验科技焕肤"),
    (1.8, 3.6, "皮肤透亮 效果清晰可见"),
]


class SubtitleTemplateValidationTests(unittest.TestCase):
    def setUp(self):
        self.fonts = patch.object(video, "_subtitle_fonts_cache", (
            "Noto Sans SC", "Noto Sans CJK SC", "Noto Serif CJK SC",
        ))
        self.fonts.start()

    def tearDown(self):
        self.fonts.stop()

    def test_config_exposes_exact_six_structural_templates(self):
        config = video.subtitle_config()
        self.assertEqual(
            [item["key"] for item in config["templates"]],
            ["keyword_highlight", "word_highlight", "karaoke", "bounce", "glow", "bilingual"],
        )
        self.assertEqual("基础字幕", config["templates"][0]["label"])
        keyword_capable = {"keyword_highlight", "glow", "bilingual"}
        self.assertTrue(all(
            item["defaults"].get("keyword_highlight_enabled") is False
            for item in config["templates"] if item["key"] in keyword_capable
        ))
        self.assertTrue(all(item["defaults"].get("font_family") for item in config["templates"]))
        self.assertTrue(all(font["value"] in video._SUBTITLE_FONT_ALLOWLIST for font in config["fonts"]))
        self.assertEqual(
            [font["label"] for font in config["fonts"]],
            ["简体中文黑体（推荐）", "中日韩黑体", "中日韩宋体"],
        )
        self.assertTrue(all(not any("a" <= char.lower() <= "z" for char in font["label"]) for font in config["fonts"]))

    def test_rejects_unknown_style_font_color_and_option(self):
        with self.assertRaisesRegex(ValueError, "不支持的字幕模板"):
            video._normalize_subtitle_options("../../evil", {})
        with self.assertRaisesRegex(ValueError, "字体未安装"):
            video._normalize_subtitle_options("glow", {"font_family": "/tmp/evil.ttf"})
        with self.assertRaisesRegex(ValueError, "#RRGGBB"):
            video._normalize_subtitle_options("glow", {"glow_color": "red;movie=x"})
        with self.assertRaisesRegex(ValueError, "不支持的字幕参数"):
            video._normalize_subtitle_options("glow", {"ffmpeg_filter": "movie=x"})

    def test_template_specific_requirements_are_explicit(self):
        with self.assertRaisesRegex(ValueError, "至少填写一个关键词"):
            video._normalize_subtitle_options("keyword_highlight", {
                "keyword_highlight_enabled": True,
                "keyword_mode": "manual",
            })
        disabled = video._normalize_subtitle_options("keyword_highlight", {
            "keyword_highlight_enabled": False,
            "keyword_mode": "manual",
        })
        self.assertFalse(disabled["keyword_highlight_enabled"])
        with self.assertRaisesRegex(ValueError, "布尔值"):
            video._normalize_subtitle_options("glow", {"keyword_highlight_enabled": "true"})
        with self.assertRaisesRegex(ValueError, "英文副字幕"):
            video._normalize_subtitle_options("bilingual", {})
        opts = video._normalize_subtitle_options("bilingual", {
            "secondary_text": "Bright skin starts today",
            "secondary_font_family": "Noto Sans CJK SC",
        })
        self.assertEqual("Bright skin starts today", opts["secondary_text"])

    def test_request_validation_happens_before_job_creation(self):
        payload = {
            "mode": "text", "image_data": PNG, "text": "你好", "voice": "voice-1",
            "subtitle": True, "subtitle_style": "word_highlight",
            "subtitle_options": {"font_color": "#FFFFFF", "highlight_color": "#FFE45C"},
        }
        cleaned = video.validate_video_payload(payload)
        self.assertEqual("word_highlight", cleaned["subtitle_style"])
        self.assertEqual("#FFE45C", cleaned["subtitle_options"]["highlight_color"])
        self.assertIn("body = video_domain.validate_video_payload", CORE_SOURCE)
        self.assertLess(
            CORE_SOURCE.index("body = video_domain.validate_video_payload"),
            CORE_SOURCE.index("cost = points_domain.cost_of(kind, body)"),
        )


class SubtitleAssRenderingTests(unittest.TestCase):
    def setUp(self):
        self.fonts = patch.object(video, "_subtitle_fonts_cache", ("Noto Sans SC",))
        self.fonts.start()
        self.words = video._segments_to_timed_words(SEGMENTS)

    def tearDown(self):
        self.fonts.stop()

    def _render(self, key, extra=None, width=1080, height=1920):
        opts = video._normalize_subtitle_options(key, extra or {})
        return video._build_ass(SEGMENTS, key, width, height, options=opts, timed_words=self.words)

    def test_all_six_templates_generate_dialogues(self):
        for key in video._SUBTITLE_TEMPLATE_LABELS:
            extra = {"secondary_text": "Bright skin starts today"} if key == "bilingual" else {}
            with self.subTest(key=key):
                ass = self._render(key, extra)
                self.assertIn("[Events]", ass)
                self.assertIn("Dialogue:", ass)
                self.assertNotIn("{movie=", ass)

    def test_word_and_karaoke_use_single_timed_dialogue_per_card(self):
        word_ass = self._render("word_highlight")
        karaoke_ass = self._render("karaoke")
        word_cards = video._split_timed_word_cards(self.words, 16)
        self.assertEqual(len(word_cards), word_ass.count("Dialogue:"))
        self.assertEqual(len(word_cards), karaoke_ass.count("Dialogue:"))
        self.assertIn("{\\k", word_ass)
        self.assertIn("{\\kf", karaoke_ass)
        self.assertIn("\\fscx108", word_ass)
        self.assertIn("\\t(", word_ass)

    def test_glow_and_bilingual_have_distinct_layers(self):
        glow = self._render("glow", {"glow_color": "#35C8FF", "glow_radius": 9})
        bilingual = self._render("bilingual", {"secondary_text": "Bright skin starts today"})
        self.assertIn("Style: Glow", glow)
        self.assertIn("{\\blur", glow)
        self.assertIn("Style: Secondary", bilingual)
        self.assertIn("Bright skin", bilingual)

    def test_semantic_keyword_highlight_is_opt_in(self):
        for key in ("keyword_highlight", "glow", "bilingual"):
            base = {"secondary_text": "Bright skin starts today"} if key == "bilingual" else {}
            with self.subTest(key=key, enabled=False):
                ass = self._render(key, base)
                self.assertNotIn("{\\c&H", ass)
                self.assertNotIn("\\fscx108", ass)
            enabled = dict(base, keyword_highlight_enabled=True, keyword_mode="manual", keywords=["焕肤"])
            with self.subTest(key=key, enabled=True):
                ass = self._render(key, enabled)
                self.assertIn("{\\c&H", ass)
                self.assertIn("\\fscx108", ass)

    def test_bilingual_wraps_long_secondary_copy_without_overlapping_layers(self):
        bilingual = self._render("bilingual", {
            "secondary_text": "This deliberately long English subtitle must wrap inside the safe video area without overlapping the primary Chinese subtitle line",
        }, width=1080, height=1080)
        secondary_dialogue = next(line for line in bilingual.splitlines() if ",Secondary," in line)
        self.assertIn("\\N", secondary_dialogue)
        self.assertIn("{\\an2\\pos(", bilingual)

    def test_bounce_uses_per_word_timeline_transforms(self):
        bounce = self._render("bounce", {"bounce_height": 20, "animation_duration_ms": 240})
        self.assertGreater(bounce.count("\\t("), 2)
        self.assertNotIn("\\move(", bounce)

    def test_known_copy_keeps_whisper_boundaries(self):
        raw = [(0.0, 0.5, "错"), (0.5, 1.0, "别字")]
        corrected = video._retime_known_text("正确文案", raw, [(0.0, 1.0, "错别字")])
        self.assertEqual("正确文案", "".join(item[2] for item in corrected))
        self.assertEqual((0.0, 0.5), corrected[0][:2])

    def test_common_ratios_keep_card_length_bounded(self):
        for width, height in ((1080, 1920), (1920, 1080), (1080, 1080), (1080, 1350), (1350, 1080)):
            with self.subTest(size=(width, height)):
                ass = self._render("keyword_highlight", width=width, height=height)
                self.assertIn("PlayResX: %d" % width, ass)
                self.assertIn("PlayResY: %d" % height, ass)
                self.assertNotIn("\\N\\N", ass)


class SubtitleTemplateUiTests(unittest.TestCase):
    def test_page_has_six_templates_and_live_preview(self):
        for key in ("keyword_highlight", "word_highlight", "karaoke", "bounce", "glow", "bilingual"):
            self.assertIn('data-substyle="%s"' % key, VIDEO_HTML)
        for key in ("word_highlight", "karaoke", "bounce", "glow", "bilingual"):
            self.assertIn('data-subtitle-panel="%s"' % key, VIDEO_HTML)
        self.assertIn('id="subtitlePreview"', VIDEO_HTML)
        self.assertIn('id="subtitleResetBtn"', VIDEO_HTML)
        self.assertIn('id="subtitleKeywordEnabled" type="checkbox"', VIDEO_HTML)
        self.assertIn('<b>基础字幕</b>', VIDEO_HTML)
        self.assertIn("hq_video_subtitle_templates_v2", VIDEO_HTML)
        self.assertIn('<option value="Noto Sans SC">简体中文黑体（推荐）</option>', VIDEO_HTML)

    def test_page_sends_options_and_labels_history(self):
        self.assertIn("subtitle_options:subtitleOpts", VIDEO_HTML)
        self.assertIn("SUBTITLE_TEMPLATE_NAMES[x.subtitle_style]", VIDEO_HTML)
        self.assertIn("/api/gen/video/subtitle-config", VIDEO_HTML)
        self.assertIn('id="subtitleSecondaryText"', VIDEO_HTML)

    def test_live_preview_wires_every_template_option(self):
        preview = VIDEO_HTML.split("function updateSubtitlePreview(){", 1)[1].split("function refreshSubtitleStateFromForm", 1)[0]
        for option in (
            "font_family", "font_size", "font_weight", "font_color", "highlight_color",
            "outline_color", "outline_width", "position", "vertical_offset",
            "background_color", "background_opacity", "keyword_highlight_enabled", "keyword_scale",
            "word_highlight_speed", "active_word_scale", "pending_color", "progress_mode",
            "bounce_height", "animation_duration_ms", "glow_color", "glow_strength",
            "glow_radius", "secondary_font_family", "secondary_font_size",
            "secondary_color", "line_gap", "secondary_text",
        ):
            with self.subTest(option=option):
                self.assertIn(option, preview)
        self.assertIn("subtitlePreviewOutline", preview)
        self.assertIn("sub-karaoke-track", preview)
        self.assertIn("--bounce-height", preview)

    def test_keyword_controls_are_independent_and_default_off(self):
        self.assertIn("var SUBTITLE_KEYWORD_STYLES={keyword_highlight:true,glow:true,bilingual:true}", VIDEO_HTML)
        self.assertGreaterEqual(VIDEO_HTML.count("keyword_highlight_enabled:false"), 3)
        collector = VIDEO_HTML.split("function collectSubtitleOptions(){", 1)[1].split("function setSubtitleStyle", 1)[0]
        self.assertIn("subtitleSupportsKeywords(selectedSubtitleStyle)", collector)
        self.assertIn("keyword_highlight_enabled", collector)
        renderer = VIDEO_HTML.split("function updateSubtitlePreview(){", 1)[1].split("function refreshSubtitleStateFromForm", 1)[0]
        self.assertIn("o.keyword_highlight_enabled?subtitlePreviewHighlight", renderer)

    def test_range_values_stay_in_sync_with_slider_inputs(self):
        sync = VIDEO_HTML.split("function subtitleSyncRangeOutputs(){", 1)[1].split("function subtitleSetColor", 1)[0]
        refresh = VIDEO_HTML.split("function refreshSubtitleStateFromForm(){", 1)[1].split("function bindSubtitleBuilder", 1)[0]
        self.assertIn("input[type=\"range\"]", sync)
        self.assertIn("input.id+'Out'", sync)
        self.assertIn("output.textContent=input.value", sync)
        self.assertIn("subtitleSyncRangeOutputs()", refresh)

    def test_missing_template_fields_do_not_render_as_undefined(self):
        setter = VIDEO_HTML.split("function subtitleSetOutput", 1)[1].split("function subtitleSyncRangeOutputs", 1)[0]
        self.assertIn("value!=null", setter)

    def test_font_size_uses_standard_numeric_select(self):
        field = VIDEO_HTML.split('<label for="subtitleFontSize">字号</label>', 1)[1].split('</div>', 1)[0]
        self.assertIn('<select id="subtitleFontSize">', field)
        self.assertNotIn('type="range"', field)
        for size in (24, 32, 40, 48, 56, 64, 72, 80, 96, 112, 120):
            self.assertIn('<option value="{0}">{0}</option>'.format(size), field)

    def test_legacy_font_sizes_snap_to_standard_options(self):
        self.assertIn("var SUBTITLE_FONT_SIZES=[24,32,40,48,56,64,72,80,96,112,120]", VIDEO_HTML)
        normalizer = VIDEO_HTML.split("function subtitleNormalizeFontSize", 1)[1].split("function subtitleDefaults", 1)[0]
        self.assertIn("Math.abs(size-target)", normalizer)
        self.assertIn("gap===bestGap&&size>best", normalizer)

    def test_core_exposes_authenticated_font_config_route(self):
        route = CORE_SOURCE.index('if p == "/api/gen/video/subtitle-config":')
        verify = CORE_SOURCE.index("verify(self._token())", route)
        response = CORE_SOURCE.index("video_domain.subtitle_config()", verify)
        self.assertLess(route, verify)
        self.assertLess(verify, response)


if __name__ == "__main__":
    unittest.main()
