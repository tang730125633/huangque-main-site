import unittest
from pathlib import Path


PAGE = (Path(__file__).resolve().parents[1] / "site/workbench/device.html").read_text(encoding="utf-8")


class HQCLIDevicePageTests(unittest.TestCase):
    def test_page_preserves_code_through_login_and_never_handles_tokens(self):
        self.assertIn('next="/workbench/device?user_code="+encodeURIComponent(userCode)', PAGE)
        self.assertIn('location.replace("/login?next="+encodeURIComponent(next))', PAGE)
        self.assertNotIn("access_token", PAGE)
        self.assertNotIn("localStorage", PAGE)

    def test_page_lists_scopes_before_explicit_approve_or_deny(self):
        self.assertIn('post("/api/auth/cli/device/info"', PAGE)
        self.assertIn("scope_details", PAGE)
        self.assertIn('post("/api/auth/cli/device/approve"', PAGE)
        self.assertIn("同意授权", PAGE)
        self.assertIn("拒绝", PAGE)


if __name__ == "__main__":
    unittest.main()
