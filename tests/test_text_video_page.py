import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "site/workbench/text-video.html").read_text(encoding="utf-8")
SHELL = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
CORE = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")


class TextVideoPageTests(unittest.TestCase):
    def test_page_uses_authenticated_paid_job_pipeline(self):
        self.assertIn("/api/gen/script_to_video", PAGE)
        self.assertIn("pipeline:'pixelle'", PAGE)
        self.assertIn("Idempotency-Key", PAGE)
        self.assertIn("/api/gen/job/", PAGE)

    def test_template_catalog_is_authenticated_and_not_hardcoded_to_service(self):
        self.assertIn("/api/gen/text-video/templates", PAGE)
        self.assertIn('p == "/api/gen/text-video/templates"', CORE)
        self.assertNotIn("127.0.0.1:8103", PAGE)
        self.assertNotIn("/api/video/generate/async", PAGE)

    def test_topic_and_full_copy_are_explicit_modes(self):
        self.assertIn('data-mode="generate"', PAGE)
        self.assertIn('data-mode="fixed"', PAGE)
        self.assertIn("主题创作", PAGE)
        self.assertIn("完整文案", PAGE)

    def test_sidebar_exposes_text_video_workspace(self):
        self.assertIn("{k:'text-video',l:'文案成片',i:'clapper'}", SHELL)
        self.assertIn("active==='text-video'", SHELL)

    def test_page_does_not_expose_provider_branding_or_manual_upload(self):
        self.assertNotIn("Pixelle", PAGE)
        self.assertNotIn("RunningHub", PAGE)
        self.assertNotIn('type="file"', PAGE)


if __name__ == "__main__":
    unittest.main()
