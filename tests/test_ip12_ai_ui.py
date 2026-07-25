import unittest
from pathlib import Path


PAGE = Path(__file__).resolve().parents[1] / "site" / "workbench" / "ip12.html"


class IP12AIUITests(unittest.TestCase):
    def test_ai_is_explicit_structured_and_keeps_confirmation_separate(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn("/api/gen/digital-ip/diagnose", html)
        self.assertIn("AI 分析本步", html)
        self.assertIn("OPENAI · STRUCTURED", html)
        self.assertIn("credentials:\"include\"", html)
        self.assertIn("AI 只给建议", html)
        self.assertNotIn("OPENAI_API_KEY", html)


if __name__ == "__main__":
    unittest.main()
