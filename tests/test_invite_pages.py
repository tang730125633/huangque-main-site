import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTER = (ROOT / "site" / "register.html").read_text(encoding="utf-8")
INVITE = (ROOT / "site" / "workbench" / "invite.html").read_text(encoding="utf-8")
SETTINGS = (ROOT / "site" / "workbench" / "settings.html").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "site" / "workbench" / "dashboard.html").read_text(encoding="utf-8")
SHELL = (ROOT / "site" / "workbench" / "cloud-shell.js").read_text(encoding="utf-8")


def inline_scripts(html):
    return [body for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S | re.I)
            if "src=" not in attrs.lower()]


class InvitePagesTests(unittest.TestCase):
    def test_register_page_reads_invite_and_uses_shared_registration_api(self):
        self.assertIn("new URLSearchParams(location.search).get('invite')", REGISTER)
        self.assertIn("/api/invite/config", REGISTER)
        self.assertIn("/api/invite/validate?code=", REGISTER)
        self.assertIn("/api/auth/register", REGISTER)
        self.assertIn("invite_source", REGISTER)
        self.assertIn("device_id", REGISTER)

    def test_invite_center_has_qr_poster_stats_and_users(self):
        self.assertIn('src="qrcode.min.js?v=1"', INVITE)
        self.assertIn('id="posterCanvas"', INVITE)
        self.assertIn("/api/invite/code", INVITE)
        self.assertIn("/api/invite/dashboard", INVITE)
        self.assertIn("/api/invite/users?limit=100", INVITE)
        for label in ("累计绑定", "今日新增", "有效邀请", "一级直邀", "二级间邀"):
            self.assertIn(label, INVITE)

    def test_navigation_and_settings_link_to_invite_center(self):
        self.assertRegex(SHELL, r"\{k:'invite',l:'邀请中心',i:'users'\}")
        self.assertIn('href="invite.html"', SETTINGS)
        self.assertIn("/api/invite/referrer", SETTINGS)

    def test_old_reward_claim_is_removed(self):
        self.assertNotIn("邀请好友得点数", DASHBOARD)
        self.assertNotIn("双方各得 100 点数", DASHBOARD)
        self.assertIn("不包含点数、余额、会员、优惠券、分佣或充值奖励", INVITE)

    @unittest.skipUnless(shutil.which("node"), "node is required for inline JavaScript parsing")
    def test_inline_javascript_parses(self):
        for path, html in (("register.html", REGISTER), ("invite.html", INVITE), ("settings.html", SETTINGS)):
            scripts = inline_scripts(html)
            self.assertTrue(scripts, path)
            for script in scripts:
                checked = subprocess.run(
                    ["node", "--check", "-"], input=script, text=True, capture_output=True,
                )
                self.assertEqual(checked.returncode, 0, path + "\n" + checked.stderr)


if __name__ == "__main__":
    unittest.main()
