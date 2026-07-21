import unittest
from pathlib import Path


class BananaResultRatioBadgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1] / "site" / "workbench" / "banana.html"
        ).read_text(encoding="utf-8")

    def test_result_badge_has_a_stable_dom_target(self):
        self.assertIn('id="resultRatio"', self.html)
        self.assertIn("document.getElementById('resultRatio')", self.html)

    def test_completed_job_updates_badge_from_result_ratio(self):
        self.assertIn("setResultRatio(result&&result.ratio)", self.html)
        self.assertIn("localStorage.setItem('hq_last_result_ratio'", self.html)

    def test_loaded_image_dimensions_are_the_final_source_of_truth(self):
        self.assertIn("function ratioFromDimensions", self.html)
        self.assertIn("res.naturalWidth", self.html)
        self.assertIn("res.naturalHeight", self.html)
        self.assertIn("res.addEventListener('load'", self.html)


if __name__ == "__main__":
    unittest.main()
