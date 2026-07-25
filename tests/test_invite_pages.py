import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOGIN = (ROOT / "site" / "login.html").read_text(encoding="utf-8")
REGISTER = (ROOT / "site" / "register.html").read_text(encoding="utf-8")
INVITE = (ROOT / "site" / "workbench" / "invite.html").read_text(encoding="utf-8")
SETTINGS = (ROOT / "site" / "workbench" / "settings.html").read_text(encoding="utf-8")
OPENAPI = json.loads((ROOT / "site" / "api-docs" / "openapi.json").read_text(encoding="utf-8"))


def inline_scripts(html):
    return [
        body
        for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S | re.I)
        if "src=" not in attrs.lower()
    ]


class InvitePagesTests(unittest.TestCase):
    def test_invite_link_opens_registration_and_is_sent_to_auth_api(self):
        self.assertIn('new URLSearchParams(location.search).get("invite")', LOGIN)
        self.assertIn(".trim().toUpperCase()", LOGIN)
        self.assertRegex(LOGIN, r"inviteValue\.length\s*<=\s*32")
        self.assertIn("if(inviteCode) setRegisterMode(true)", LOGIN)
        self.assertIn('fetch("/api/auth/register"', LOGIN)
        self.assertIn("payload.invite_code=inviteCode", LOGIN)
        self.assertIn('payload.invite_source="web_link"', LOGIN)
        self.assertIn("body:JSON.stringify(payload)", LOGIN)

    def test_settings_only_loads_the_minimum_invite_share_payload(self):
        card = re.search(
            r'<section id="inviteShareCard".*?</section>',
            SETTINGS,
            re.S,
        )
        self.assertIsNotNone(card)
        start = SETTINGS.index("function loadInviteShare")
        flow = SETTINGS[start:SETTINGS.index("function friendPanel", start)]
        invite_ui = (card.group(0) + flow).lower()

        self.assertIn("/api/auth/invite/code", flow)
        self.assertIn("res.data.code", flow)
        self.assertIn("res.data.invite_link", flow)
        self.assertIn("copyText(link)", flow)
        for forbidden in ("username", "account_id", "recharge", "membership"):
            self.assertNotIn(forbidden, invite_ui)

    def test_invite_center_uses_paginated_direct_users_and_hides_second_level(self):
        self.assertIn("/api/invite/users?level=", INVITE)
        self.assertIn("pageSize=10", INVITE)
        self.assertIn('id="levelTwo" type="button" hidden', INVITE)
        self.assertIn("二级邀请暂不展示", INVITE)

    def test_public_api_documents_optional_registration_invite(self):
        register = OPENAPI["paths"]["/api/auth/register"]["post"]
        properties = register["requestBody"]["content"]["application/json"]["schema"]["properties"]
        self.assertIn("invite_code", properties)
        self.assertNotIn("invite_code", register["requestBody"]["content"]["application/json"]["schema"]["required"])
        self.assertIn("/api/auth/invite/code", OPENAPI["paths"])

    @unittest.skipUnless(shutil.which("node"), "node is required for inline JavaScript parsing")
    def test_inline_javascript_parses(self):
        for path, html in (
            ("login.html", LOGIN),
            ("register.html", REGISTER),
            ("invite.html", INVITE),
            ("settings.html", SETTINGS),
        ):
            scripts = inline_scripts(html)
            self.assertTrue(scripts, path)
            for script in scripts:
                checked = subprocess.run(
                    ["node", "--check", "-"],
                    input=script,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(checked.returncode, 0, path + "\n" + checked.stderr)


if __name__ == "__main__":
    unittest.main()
