import pathlib
import re
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class VoicePreviewClickTests(unittest.TestCase):
    def test_public_voice_preview_starts_before_async_asset_resolution(self):
        pages = {
            "video": (ROOT / "site/workbench/video.html").read_text(encoding="utf-8"),
            "audio": (ROOT / "site/workbench/audio.html").read_text(encoding="utf-8"),
        }
        for page, html in pages.items():
            with self.subTest(page=page):
                block = html[html.index("function playPreview(url,btn){"):]
                block = block[:block.index("\n  }")]
                self.assertIn("start(fresh(url))", block)
                self.assertIn("activePreview.play()", block)
                self.assertIn("activePreview.onerror=fail", block)
                self.assertLess(block.index("start(fresh(url))"), block.index(".then(start)"))

    def test_public_voice_preview_uses_compact_native_audio_controls(self):
        for page in ("video", "audio"):
            with self.subTest(page=page):
                html = (ROOT / f"site/workbench/{page}.html").read_text(encoding="utf-8")
                self.assertIn('class="voice-preview-audio" controls', html)
                self.assertIn('class="voice-play voice-native-preview"', html)
                self.assertRegex(html, r'\.voice-preview-audio\{position:absolute;left:-11px;top:-[12]px;width:300px;height:30px;cursor:pointer')
                self.assertIn('.voice-preview-audio::-webkit-media-controls-enclosure', html)
                self.assertIn("nativeAudio.onplay=function()", html)
                self.assertIn("if(a!==nativeAudio)a.pause()", html)

    def test_inline_scripts_still_parse(self):
        for page in ("video", "audio"):
            with self.subTest(page=page):
                html = (ROOT / f"site/workbench/{page}.html").read_text(encoding="utf-8")
                scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", html)
                checked = subprocess.run(["node", "--check", "-"], input=scripts[-1], text=True,
                                         encoding="utf-8", capture_output=True)
                self.assertEqual(0, checked.returncode, checked.stderr)


if __name__ == "__main__":
    unittest.main()
