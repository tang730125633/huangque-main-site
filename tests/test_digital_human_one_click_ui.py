import pathlib
import shutil
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = (ROOT / "site/workbench/digital-human-one-click.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "site/workbench/digital-human-one-click.js").read_text(encoding="utf-8")
SHELL = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")


class DigitalHumanOneClickUiTests(unittest.TestCase):
    def test_module_lives_under_director_and_replaces_top_level_copy_to_video(self):
        self.assertIn('data-active="script"', PAGE)
        self.assertIn('href="script.html"', PAGE)
        self.assertIn('aria-current="page"', PAGE)
        self.assertNotIn("{k:'text-video',l:'文案成片'", SHELL)

    def test_real_precision_pipeline_is_wired_end_to_end(self):
        for marker in (
            "/api/gen/video/lipsync-import", "/api/gen/audio", "/api/gen/video",
            "/api/gen/video-compose/projects", "/analyze-source",
            "/edit-decisions", "/render",
        ):
            self.assertIn(marker, SCRIPT)
        self.assertIn("lipsync_mode:'precision'", SCRIPT)
        self.assertIn("dynamic_duration:true", SCRIPT)
        self.assertIn("'Idempotency-Key'", SCRIPT)

    def test_three_templates_have_visible_ten_second_examples(self):
        expected = {
            "viral-talking-head-v1": "high-frequency-10s.mp4",
            "professional-explainer-v1": "professional-explainer-10s.mp4",
            "clean-talking-v1": "clean-talking-10s.mp4",
        }
        for template_id, filename in expected.items():
            self.assertIn('data-template="%s"' % template_id, PAGE)
            self.assertIn(filename, PAGE)
            self.assertGreater((ROOT / "site/assets/one-click/previews" / filename).stat().st_size, 100000)
        self.assertEqual(3, PAGE.count("<video muted loop playsinline"))

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_javascript_parses(self):
        completed = subprocess.run(
            ["node", "--check", str(ROOT / "site/workbench/digital-human-one-click.js")],
            capture_output=True, text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
