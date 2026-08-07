import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "site/workbench/text-video.html").read_text(encoding="utf-8")
SHELL = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
CORE = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
FLAGS = (ROOT / "server/content_domains/feature_flags.py").read_text(encoding="utf-8")


class TextVideoPageTests(unittest.TestCase):
    def test_page_uses_authenticated_paid_job_pipeline(self):
        self.assertIn("/api/gen/script_to_video", PAGE)
        self.assertIn("pipeline:'pixelle'", PAGE)
        self.assertIn("Idempotency-Key", PAGE)
        self.assertIn("/api/gen/job/", PAGE)

    def test_uncertain_submission_reuses_persisted_body_and_idempotency_key(self):
        self.assertIn("hq-text-video-pending-v1", PAGE)
        self.assertIn("sessionStorage.setItem", PAGE)
        self.assertIn("saved.body===body&&saved.key", PAGE)
        self.assertIn("body:pending.body", PAGE)
        self.assertIn("headers['Idempotency-Key']=pending.key", PAGE)
        self.assertIn("response.status<500||response.data.operation_terminal===true", PAGE)
        self.assertIn("confirmSubmission(pending);", PAGE)

    def test_template_catalog_is_authenticated_and_not_hardcoded_to_service(self):
        self.assertIn("/api/gen/text-video/templates", PAGE)
        self.assertIn('"/api/gen/text-video/capability", "/api/gen/text-video/templates"', CORE)
        self.assertNotIn("127.0.0.1:8103", PAGE)
        self.assertNotIn("/api/video/generate/async", PAGE)

    def test_feature_is_default_off_and_all_entry_points_use_readiness_gate(self):
        self.assertIn('"key": "pixelle_text_video"', FLAGS)
        self.assertIn('"default_enabled": False', FLAGS)
        self.assertIn('/api/gen/text-video/capability', CORE)
        self.assertIn('pixelle_video.require_available()', CORE)
        self.assertIn("feature:'pixelle_text_video'", SHELL)
        self.assertIn("data-nav-feature", SHELL)
        self.assertIn("if(data&&data.available){item.hidden=false;item.style.display='flex';}", SHELL)

    def test_topic_and_full_copy_are_explicit_modes(self):
        self.assertIn('data-mode="generate"', PAGE)
        self.assertIn('data-mode="fixed"', PAGE)
        self.assertIn("主题创作", PAGE)
        self.assertIn("完整文案", PAGE)

    def test_sidebar_exposes_text_video_workspace(self):
        self.assertIn("{k:'text-video',l:'文案成片',i:'clapper',feature:'pixelle_text_video'}", SHELL)
        self.assertIn("active==='text-video'", SHELL)

    def test_page_does_not_expose_provider_branding_or_manual_upload(self):
        self.assertNotIn("Pixelle", PAGE)
        self.assertNotIn("RunningHub", PAGE)
        self.assertNotIn('type="file"', PAGE)


if __name__ == "__main__":
    unittest.main()
