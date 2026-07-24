import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = (ROOT / "site" / "workbench" / "assets.html").read_text(encoding="utf-8")
AUDIO = (ROOT / "site" / "workbench" / "audio.html").read_text(encoding="utf-8")
VIDEO = (ROOT / "site" / "workbench" / "video.html").read_text(encoding="utf-8")
VIDEO_BACKEND = (ROOT / "server" / "content_domains" / "video.py").read_text(encoding="utf-8")


class AssetRegenHandoffUiTests(unittest.TestCase):
    def test_image_regen_returns_to_editor_instead_of_submitting(self):
        self.assertIn("return {href:'banana.html?'+imageParams.toString()}", ASSETS)
        self.assertIn("imageParams.set('prompt',x.prompt||'')", ASSETS)
        self.assertIn("imageParams.set('engine',engine)", ASSETS)

    def test_audio_regen_preserves_editable_parameters(self):
        self.assertIn("return {href:'audio.html?'+audioParams.toString()}", ASSETS)
        for key in ("prompt", "voice", "speed", "pitch", "volume"):
            self.assertIn("audioParams.set('%s'" % key, ASSETS)

    def test_handoff_redirects_before_the_legacy_submit_path(self):
        redirect = "if(cfg.href){ location.href=cfg.href; return; }"
        submit = "fetch(cfg.url,{method:'POST'"
        self.assertIn(redirect, ASSETS)
        self.assertLess(ASSETS.index(redirect), ASSETS.index(submit))

    def test_audio_page_consumes_and_clamps_asset_parameters(self):
        self.assertIn("var pv=q.get('voice'); if(pv) selectedVoice=pv", AUDIO)
        self.assertIn("params.speed=Math.max(.5,Math.min(2", AUDIO)
        self.assertIn("params.pitch=Math.max(-12,Math.min(12", AUDIO)
        self.assertIn("params.volume=Math.max(-50,Math.min(100", AUDIO)

    def test_official_video_regen_preserves_billing_parameters(self):
        self.assertIn("duration:x.duration||x.requested_duration||''", ASSETS)
        self.assertIn("generate_audio:typeof x.generate_audio==='boolean'", ASSETS)
        self.assertIn("typeof d.generate_audio==='boolean'", VIDEO)
        self.assertIn("[data-omni-duration=", VIDEO)
        self.assertIn("[data-seedance-duration=", VIDEO)
        self.assertIn('item["generate_audio"] = audio_choice', VIDEO_BACKEND)


if __name__ == "__main__":
    unittest.main()
