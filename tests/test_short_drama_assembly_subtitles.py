import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


import sys

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import short_drama_assembly_subtitles as subtitles


class ShortDramaAssemblySubtitleTests(unittest.TestCase):
    def test_ass_time_uses_centiseconds_without_shortening_end(self):
        self.assertEqual("0:00:00.00", subtitles.ass_time(0, end=False))
        self.assertEqual("0:00:01.23", subtitles.ass_time(1239, end=False))
        self.assertEqual("0:00:01.24", subtitles.ass_time(1239, end=True))
        self.assertEqual("1:01:01.01", subtitles.ass_time(3661010, end=True))

    def test_escape_text_blocks_override_injection_and_normalizes_lines(self):
        escaped = subtitles.escape_ass_text(
            "正常{\\pos(0,0)}\\N\r\n第二行\x00"
        )
        self.assertEqual("正常｛＼pos(0,0)｝＼N\\N第二行", escaped)
        self.assertNotIn("{", escaped)
        self.assertNotIn("}", escaped)
        self.assertNotIn("\x00", escaped)

    def test_generate_ass_offsets_shot_lines_and_sorts_by_project_time(self):
        plan = {
            "project_duration_ms": 10000,
            "shots": [
                {
                    "id": "shot-2", "start_ms": 5000, "end_ms": 10000,
                    "audio": {"lines": [{
                        "id": "line-2", "start_ms": 200, "end_ms": 900,
                        "subtitle_visible": True, "subtitle_text": "后一句",
                    }]},
                },
                {
                    "id": "shot-1", "start_ms": 0, "end_ms": 5000,
                    "audio": {"lines": [{
                        "id": "line-1", "start_ms": 1000, "end_ms": 1800,
                        "subtitle_visible": True, "subtitle_text": "前一句",
                    }]},
                },
            ],
        }
        result = subtitles.generate_ass("9:16", "bottom", plan)
        self.assertIn("PlayResX: 1080", result)
        self.assertIn("PlayResY: 1920", result)
        self.assertIn("Style: Default,Noto Sans CJK SC", result)
        first = result.index("前一句")
        second = result.index("后一句")
        self.assertLess(first, second)
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:01.80,Default", result)
        self.assertIn("Dialogue: 0,0:00:05.20,0:00:05.90,Default", result)

    def test_top_style_and_hidden_subtitles(self):
        plan = {
            "project_duration_ms": 5000,
            "shots": [{
                "id": "shot-1", "start_ms": 0, "end_ms": 5000,
                "audio": {"lines": [{
                    "id": "hidden", "start_ms": 0, "end_ms": 1000,
                    "subtitle_visible": False, "subtitle_text": "不能出现",
                }]},
            }],
        }
        result = subtitles.generate_ass("16:9", "top", plan)
        self.assertIn("PlayResX: 1920", result)
        self.assertRegex(result, r"Style: Default,[^\n]+,8,")
        self.assertNotIn("不能出现", result)

    def test_invalid_ratio_position_text_and_bounds_are_rejected(self):
        base = {
            "project_duration_ms": 5000,
            "shots": [{
                "id": "shot-1", "start_ms": 0, "end_ms": 5000,
                "audio": {"lines": [{
                    "id": "line-1", "start_ms": 0, "end_ms": 1000,
                    "subtitle_visible": True, "subtitle_text": "字幕",
                }]},
            }],
        }
        for ratio, position, code in (
            ("1:1", "bottom", "subtitle_timeline_invalid"),
            ("9:16", "middle", "subtitle_timeline_invalid"),
        ):
            with self.assertRaises(subtitles.SubtitleError) as raised:
                subtitles.generate_ass(ratio, position, base)
            self.assertEqual(code, raised.exception.code)

        empty = {
            **base,
            "shots": [{
                **base["shots"][0],
                "audio": {"lines": [{
                    **base["shots"][0]["audio"]["lines"][0],
                    "subtitle_text": "  ",
                }]},
            }],
        }
        with self.assertRaises(subtitles.SubtitleError) as raised:
            subtitles.generate_ass("9:16", "bottom", empty)
        self.assertEqual("subtitle_text_invalid", raised.exception.code)

        overflow = {
            **base,
            "shots": [{
                **base["shots"][0],
                "audio": {"lines": [{
                    **base["shots"][0]["audio"]["lines"][0],
                    "end_ms": 5001,
                }]},
            }],
        }
        with self.assertRaises(subtitles.SubtitleError) as raised:
            subtitles.generate_ass("9:16", "bottom", overflow)
        self.assertEqual("subtitle_timeline_invalid", raised.exception.code)

    def test_output_is_deterministic(self):
        plan = {
            "project_duration_ms": 5000,
            "shots": [{
                "id": "s", "start_ms": 0, "end_ms": 5000,
                "audio": {"lines": [{
                    "id": "l", "start_ms": 1, "end_ms": 999,
                    "subtitle_visible": True, "subtitle_text": "确定性",
                }]},
            }],
        }
        self.assertEqual(
            subtitles.generate_ass("9:16", "bottom", plan),
            subtitles.generate_ass("9:16", "bottom", plan),
        )

    def test_font_environment_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            font = Path(directory) / "NotoSansCJK-Regular.ttc"
            font.write_bytes(b"font")
            charset = " ".join(
                "%04x" % ord(character)
                for character in subtitles.REQUIRED_CJK_GLYPHS
            )

            def found(args, **_kwargs):
                if args[0] == "fc-match":
                    return SimpleNamespace(
                        returncode=0,
                        stdout="Noto Sans CJK SC\n%s" % font,
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0, stdout=charset, stderr=""
                )

            self.assertEqual(
                str(font.resolve()),
                subtitles.inspect_font(font, runner=found)["file"],
            )

            def fallback(args, **_kwargs):
                if args[0] == "fc-match":
                    return SimpleNamespace(
                        returncode=0,
                        stdout="DejaVu Sans\n%s" % font,
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0, stdout=charset, stderr=""
                )

            with self.assertRaises(subtitles.SubtitleError) as raised:
                subtitles.inspect_font(font, runner=fallback)
            self.assertEqual(
                "subtitle_font_unavailable", raised.exception.code
            )

    def test_font_gate_rejects_missing_cjk_glyphs(self):
        with tempfile.TemporaryDirectory() as directory:
            font = Path(directory) / "NotoSansCJK-Regular.ttc"
            font.write_bytes(b"font")

            def runner(args, **_kwargs):
                if args[0] == "fc-match":
                    return SimpleNamespace(
                        returncode=0,
                        stdout="Noto Sans CJK SC\n%s" % font,
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0, stdout="0041-007a", stderr=""
                )

            with self.assertRaises(subtitles.SubtitleError) as raised:
                subtitles.inspect_font(font, runner=runner)
            self.assertEqual(
                "subtitle_font_unavailable", raised.exception.code
            )


if __name__ == "__main__":
    unittest.main()
