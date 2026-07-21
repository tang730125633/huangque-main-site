import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ImageSubmitIdempotencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / "site" / "workbench" / "banana.html").read_text(encoding="utf-8")
        cls.core = (ROOT / "server" / "content_domains" / "core.py").read_text(encoding="utf-8")

    def test_generate_button_stays_locked_while_any_job_is_active(self):
        self.assertIn("lock=(n>0)||submitting", self.page)
        self.assertIn("if(activeCount())", self.page)
        self.assertIn("已有任务生成中，请等待完成", self.page)

    def test_image_submit_sends_an_idempotency_key(self):
        self.assertIn("function newIdempotencyKey()", self.page)
        self.assertIn("'Idempotency-Key':idemKey", self.page)

    def test_backend_enables_idempotency_for_both_image_endpoints(self):
        self.assertRegex(
            self.core,
            r'kind in \{[^\n}]*"image"[^\n}]*"banana"[^\n}]*\}',
        )


if __name__ == "__main__":
    unittest.main()
