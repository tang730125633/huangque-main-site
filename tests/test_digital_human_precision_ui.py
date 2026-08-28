import pathlib
import shutil
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = (ROOT / "site/workbench/digital-human-oneclick.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "site/workbench/digital-human-unified.js").read_text(encoding="utf-8")
STATE_SCRIPT = ROOT / "site/workbench/digital-human-unified-state.js"
DIRECTOR = (ROOT / "site/workbench/script.html").read_text(encoding="utf-8")


class DigitalHumanPrecisionUiTests(unittest.TestCase):
    def test_precision_controls_are_available_inside_the_unified_page(self):
        for target in (
            "photoDrop", "voiceUploadDrop", "customerMaterialsPicker",
            "driveAudioDrop", "dhDrop", "dhConsent", "dhAnalyze", "dhStart",
        ):
            self.assertIn('id="%s"' % target, PAGE)

    def test_director_entry_opens_photo_mode_without_a_second_photo_tab(self):
        self.assertIn('data-active="script"', PAGE)
        self.assertIn('href="digital-human-oneclick.html">🎬 数字人一键生成</a>', DIRECTOR)
        self.assertIn('data-dh-mode="photo">数字人一键生成</button>', PAGE)
        self.assertIn('data-dh-mode="video">真人视频 Precision</button>', PAGE)
        self.assertNotIn("照片数字人", PAGE)
        self.assertIn("params.get('mode')==='video'?'video':'photo'", SCRIPT)
        self.assertNotIn('data-mode="script_to_video"', DIRECTOR)

    def test_video_mode_clones_the_uploaded_videos_voice_before_paid_generation(self):
        state_tag = 'src="digital-human-unified-state.js?'
        app_tag = 'src="digital-human-unified.js?'
        self.assertLess(PAGE.index(state_tag), PAGE.index(app_tag))
        for marker in (
            "/api/gen/video/lipsync-import",
            "/api/gen/video/lipsync-voice-sample",
            "/api/gen/audio/slots",
            "/api/gen/audio/clone-vip",
            "/api/gen/audio/clone-status",
            "/api/gen/audio",
            "/api/gen/video",
            "/api/gen/video-compose/projects",
            "/analyze-source",
            "/edit-decisions",
            "/render",
        ):
            self.assertIn(marker, SCRIPT)
        self.assertNotIn("/api/gen/audio/voices", SCRIPT)
        self.assertNotIn('id="dhVoice"', PAGE)
        self.assertIn("lipsync_mode:'precision'", SCRIPT)
        self.assertIn("dynamic_duration:true", SCRIPT)
        self.assertIn("consent_confirmed:true", SCRIPT)
        self.assertIn("'Idempotency-Key'", SCRIPT)
        analyze = SCRIPT[SCRIPT.index("function analyzeVoice("):SCRIPT.index("function previewVoice(")]
        self.assertNotIn("generateAudio(", analyze)
        self.assertNotIn("generateLipsync(", analyze)
        confirm = SCRIPT[SCRIPT.index("function confirmAndGenerate("):]
        self.assertIn("generateAudio(text).then(generateLipsync)", confirm)

    def test_three_templates_have_visible_ten_second_examples(self):
        expected = {
            "viral-talking-head-v1": "high-frequency-10s.mp4",
            "professional-explainer-v1": "professional-explainer-10s.mp4",
            "clean-talking-v1": "clean-talking-10s.mp4",
        }
        for template_id, filename in expected.items():
            self.assertIn('data-template="%s"' % template_id, PAGE)
            self.assertIn(filename, PAGE)
            preview = ROOT / "site/assets/one-click/previews" / filename
            self.assertGreater(preview.stat().st_size, 100000)
        self.assertEqual(3, PAGE.count('<video muted loop playsinline preload="metadata"'))

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_javascript_parses(self):
        for script in (STATE_SCRIPT, ROOT / "site/workbench/digital-human-unified.js"):
            completed = subprocess.run(
                ["node", "--check", str(script)], capture_output=True, text=True,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_slot_selection_and_clone_version_idempotency_behaviors(self):
        completed = subprocess.run(
            ["node", str(ROOT / "tests/test_digital_human_unified_state.js")],
            capture_output=True, text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("unified state tests passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
