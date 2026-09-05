import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("director_regression_gate", Path(__file__).resolve().parents[1] / "scripts/check_director_regression.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class DirectorRegressionGateTests(unittest.TestCase):
    def report(self, **changes):
        result = {"tests": ["passing", "existing_failure"], "count": 2,
                  "skips": [], "failures": {"existing_failure": "same assertion"}}
        result.update(changes)
        return result

    def test_same_failure_is_recorded_without_claiming_all_green(self):
        self.assertEqual([], gate.compare(self.report(), self.report()))

    def test_new_or_changed_failure_blocks(self):
        for failures in ({"new": "bad"}, {"existing_failure": "different assertion"}):
            self.assertTrue(gate.compare(self.report(), self.report(failures=failures)))

    def test_removed_tests_and_new_skips_block(self):
        for changes in ({"tests": ["passing"]}, {"count": 1}, {"skips": ["passing"]}):
            self.assertTrue(gate.compare(self.report(), self.report(**changes)))

    def test_added_passing_tests_are_accepted(self):
        self.assertEqual([], gate.compare(self.report(), self.report(tests=["passing", "existing_failure", "new"], count=3)))

    def test_same_import_error_is_not_a_valid_baseline(self):
        broken = self.report(tests=["unittest.loader._FailedTest.example"])
        self.assertTrue(gate.compare(broken, broken))
