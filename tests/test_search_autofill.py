from html.parser import HTMLParser
from pathlib import Path
from unittest import TestCase, main


ROOT = Path(__file__).resolve().parents[1]
SEARCH_INPUTS = {
    "site/workbench/leads.html": ("id", "kw"),
    "site/workbench/collect.html": ("id", "kwInput"),
    "site/workbench/inspiration.html": ("id", "caseSearch"),
    "site/workbench/assets.html": ("name", "hq_asset_search"),
    "site/workbench/canvas.html": ("id", "ncBoardSearch"),
    "site/workbench/settings.html": ("id", "friendSearchInput"),
    "site/admin/index.html": ("id", "userSearch"),
    "site/admin/index.html#points": ("id", "pointsUser"),
    "site/admin/index.html#recharge": ("id", "rechargeUser"),
    "site/admin/index.html#requests": ("id", "reqSearch"),
}


class InputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = []

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.inputs.append(dict(attrs))


class SearchAutofillTests(TestCase):
    def test_search_inputs_have_non_login_semantics(self):
        for label, (key, value) in SEARCH_INPUTS.items():
            path = ROOT / label.split("#", 1)[0]
            parser = InputParser()
            parser.feed(path.read_text(encoding="utf-8"))
            field = next(item for item in parser.inputs if item.get(key) == value)

            with self.subTest(field=label):
                self.assertEqual(field.get("type"), "search")
                self.assertEqual(field.get("autocomplete"), "off")
                self.assertEqual(field.get("autocorrect"), "off")
                self.assertEqual(field.get("autocapitalize"), "off")
                self.assertEqual(field.get("spellcheck"), "false")
                self.assertEqual(field.get("enterkeyhint"), "search")
                self.assertTrue(field.get("name", "").startswith("hq_"))
                self.assertTrue(field.get("aria-label"))
                self.assertNotIn("readonly", field)
                self.assertNotIn("onfocus", field)

    def test_shared_auth_fields_are_isolated_in_a_real_form(self):
        shell = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
        form_start = shell.index("'<form id=\"hqLoginForm\">'+")
        form_end = shell.index("'</form>'+")

        for field_id in ("hqU", "hqC", "hqP", "hqP2", "hqD"):
            with self.subTest(field=field_id):
                self.assertLess(form_start, shell.index(f'id="{field_id}"'))
                self.assertLess(shell.index(f'id="{field_id}"'), form_end)

        self.assertIn('id="hqU" name="username" type="text" autocomplete="username"', shell)
        self.assertIn('id="hqC" name="one-time-code" type="text" inputmode="numeric" autocomplete="one-time-code"', shell)
        self.assertIn('id="hqP" name="password" type="password" autocomplete="current-password"', shell)
        self.assertIn('id="hqP2" name="password_confirm" type="password" autocomplete="new-password"', shell)
        self.assertIn("document.getElementById('hqLoginForm').onsubmit=function(e){ e.preventDefault();", shell)
        self.assertIn("p.setAttribute('autocomplete','new-password')", shell)
        self.assertIn("p.setAttribute('autocomplete','current-password')", shell)
        self.assertIn("if(c) c.disabled=!_hqPhone", shell)
        self.assertIn("if(p) p.disabled=_hqPhone", shell)
        self.assertIn("if(p2) p2.disabled=true", shell)


if __name__ == "__main__":
    main()
