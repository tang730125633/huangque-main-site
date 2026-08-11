import unittest
from pathlib import Path


TEMPLATES = Path(__file__).parents[1] / "server" / "hermes_ip12" / "templates"


class IP12HarnessActionsUITests(unittest.TestCase):
    def test_only_latest_assistant_reply_keeps_confirmation_actions(self):
        # Regression: ISSUE-001 — old assistant replies repeated the current action buttons.
        # Found by /qa on 2026-08-12.
        # Report: local visible QA for PR 1056.
        for filename in ("index.html", "index_clean.html"):
            with self.subTest(filename=filename):
                source = (TEMPLATES / filename).read_text(encoding="utf-8")
                attach = source[source.index("function attachHarnessActions"):source.index("function renderChat")]
                self.assertIn("document.querySelectorAll('#chatArea .harness-actions')", attach)


if __name__ == "__main__":
    unittest.main()
