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


if __name__ == "__main__":
    main()
