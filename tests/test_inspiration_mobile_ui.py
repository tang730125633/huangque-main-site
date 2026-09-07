import unittest
from pathlib import Path


class InspirationMobileUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1]
            / "site"
            / "workbench"
            / "inspiration.html"
        ).read_text(encoding="utf-8")

    def test_mobile_gallery_keeps_eight_items_in_two_columns(self):
        self.assertIn("PAGE_SIZE=8", self.html)
        mobile_css = self.html.split("@media (max-width:620px){", 1)[1].split("</style>", 1)[0]
        self.assertIn(".masonry{grid-template-columns:repeat(2,minmax(0,1fr))", mobile_css)

    def test_mobile_filter_uses_an_accessible_bottom_sheet(self):
        self.assertIn('id="mobileCategoryTrigger"', self.html)
        self.assertIn('aria-controls="categoryPanel"', self.html)
        self.assertIn('id="categoryPanel"', self.html)
        self.assertIn('id="categoryBackdrop"', self.html)
        self.assertIn("setCategorySheet(false)", self.html)

    def test_mobile_touch_targets_and_safe_area_are_explicit(self):
        self.assertIn("min-height:48px", self.html)
        self.assertIn("height:44px", self.html)
        self.assertIn("env(safe-area-inset-bottom)", self.html)

    def test_detail_dialog_has_mobile_layout_hooks(self):
        self.assertIn("inspiration-dialog-shell", self.html)
        self.assertIn("inspiration-dialog-media", self.html)
        self.assertIn("inspiration-dialog-body", self.html)


if __name__ == "__main__":
    unittest.main()
