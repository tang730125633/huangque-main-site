import unittest
from pathlib import Path


class GrokOfficialUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parents[1] / "site/workbench/video.html").read_text(encoding="utf-8")

    def test_official_ratios_replace_supplier_only_ratios(self):
        for ratio in ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"):
            self.assertIn('data-grok-ratio="%s"' % ratio, self.html)
        self.assertNotIn('data-grok-ratio="4:5"', self.html)
        self.assertNotIn('data-grok-ratio="5:4"', self.html)

    def test_payload_contains_official_parameters(self):
        self.assertIn("xlPayload.duration=selectedGrokDuration", self.html)
        self.assertIn("xlPayload.resolution=selectedGrokResolution", self.html)
        self.assertIn("xlPayload.model=selectedGrokModel", self.html)
        self.assertIn("var GROK_REF_MAX=7", self.html)

    def test_edit_mode_is_marked_as_under_maintenance(self):
        self.assertIn('<button disabled title="功能维护中">编辑维护中</button>', self.html)
        self.assertNotIn('data-grok-operation="edit"', self.html)


if __name__ == "__main__":
    unittest.main()
