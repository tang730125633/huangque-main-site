import unittest
from pathlib import Path


class InspirationWatermarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1] / "site" / "workbench" / "inspiration.html"
        ).read_text(encoding="utf-8")

    def test_list_and_detail_render_brand_watermark(self):
        self.assertIn("function watermarkHtml()", self.html)
        self.assertGreaterEqual(self.html.count("+watermarkHtml()"), 2)
        self.assertIn("黄雀AI · 灵感样片", self.html)

    def test_watermark_does_not_intercept_user_interactions(self):
        self.assertIn("pointer-events:none", self.html)
        self.assertIn('aria-hidden="true"', self.html)

    def test_detail_prompt_does_not_claim_the_sample_is_unwatermarked(self):
        self.assertIn("function cleanPrompt", self.html)
        self.assertIn("replace(/无水印/g", self.html)
        self.assertIn("ta.value=cleanPrompt(x.prompt||'')", self.html)


if __name__ == "__main__":
    unittest.main()
