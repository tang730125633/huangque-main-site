from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SHELL = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
RECHARGE = (ROOT / "site/workbench/recharge.html").read_text(encoding="utf-8")
SETTINGS = (ROOT / "site/workbench/settings.html").read_text(encoding="utf-8")


class AccountMenuUiTest(unittest.TestCase):
    def test_both_avatar_entries_share_the_account_menu_and_card_avatar(self):
        self.assertEqual(2, SHELL.count('data-account-menu-trigger="1"'))
        self.assertIn("_accountAvatar=d.user.avatar||''", SHELL)
        self.assertNotIn("fetch('/api/auth/card/me'", SHELL)
        self.assertIn('href="settings.html" role="menuitem"', SHELL)
        self.assertIn('data-logout="1" role="menuitem"', SHELL)

    def test_payment_methods_use_local_brand_icons(self):
        for name in ("wechat", "alipay"):
            self.assertIn("../assets/brands/%s.svg" % name, RECHARGE)
            self.assertTrue((ROOT / "site/assets/brands" / (name + ".svg")).is_file())

    def test_settings_avatar_renders_and_updates_the_shared_card_avatar(self):
        self.assertIn('id="profileAvatarImage"', SETTINGS)
        self.assertIn("renderProfileAvatar(user.avatar,name)", SETTINGS)
        self.assertIn("apiFetch('/api/auth/card/media'", SETTINGS)
        self.assertIn("JSON.stringify({field:'avatar',data:reader.result})", SETTINGS)
        self.assertIn("window.HQ.refreshPoints()", SETTINGS)
        self.assertIn("头像已同步到网页和小程序", SETTINGS)


if __name__ == "__main__":
    unittest.main()
