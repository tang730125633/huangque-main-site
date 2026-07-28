import subprocess
import tempfile
import unittest
from pathlib import Path

from server.content_domains import short_drama_assembly_subtitles as subtitles


class ShortDramaAssemblySubtitleFontTests(unittest.TestCase):
    @staticmethod
    def _runner(font, family=subtitles.FONT_NAME, charset=None):
        charset = charset or "5b57 5e55 6d4b 8bd5 96c0 9ec4"

        def run(command, **_kwargs):
            if command[0] == "fc-match":
                output = "%s\n%s" % (family, font)
            else:
                output = charset
            return subprocess.CompletedProcess(command, 0, output, "")

        return run

    def test_exact_noto_file_family_and_cjk_coverage_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            font = Path(directory) / "NotoSansCJK-Regular.ttc"
            font.write_bytes(b"font-fixture")
            result = subtitles.inspect_font(
                font, runner=self._runner(font)
            )
        self.assertEqual(subtitles.FONT_NAME, result["family"])
        self.assertEqual(str(font.resolve()), result["file"])
        self.assertEqual(str(font.resolve().parent), result["font_dir"])

    def test_dejavu_fallback_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            font = Path(directory) / "NotoSansCJK-Regular.ttc"
            font.write_bytes(b"font-fixture")
            with self.assertRaises(subtitles.SubtitleError) as context:
                subtitles.inspect_font(
                    font, runner=self._runner(font, family="DejaVu Sans")
                )
        self.assertEqual("subtitle_font_unavailable", context.exception.code)

    def test_fontconfig_file_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            font = Path(directory) / "NotoSansCJK-Regular.ttc"
            fallback = Path(directory) / "fallback.ttf"
            font.write_bytes(b"font-fixture")
            fallback.write_bytes(b"fallback")
            with self.assertRaises(subtitles.SubtitleError):
                subtitles.inspect_font(
                    font, runner=self._runner(fallback)
                )

    def test_missing_required_cjk_glyph_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            font = Path(directory) / "NotoSansCJK-Regular.ttc"
            font.write_bytes(b"font-fixture")
            with self.assertRaises(subtitles.SubtitleError):
                subtitles.inspect_font(
                    font,
                    runner=self._runner(
                        font, charset="5b57 5e55 6d4b 8bd5 96c0"
                    ),
                )

    def test_missing_configured_file_fails_before_fontconfig(self):
        runner_called = False

        def runner(*_args, **_kwargs):
            nonlocal runner_called
            runner_called = True

        with self.assertRaises(subtitles.SubtitleError):
            subtitles.inspect_font("missing-noto-font.ttc", runner=runner)
        self.assertFalse(runner_called)


if __name__ == "__main__":
    unittest.main()
