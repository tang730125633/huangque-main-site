# -*- coding: utf-8 -*-
import pathlib
import sys
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import feature_flags  # noqa: E402


class DigitalPresenterFeatureFlagTests(unittest.TestCase):
    def test_missing_digital_presenter_flag_is_disabled(self):
        with patch.object(feature_flags, "_cached_rows", return_value={}):
            self.assertFalse(feature_flags.is_enabled("digital_presenter"))
            self.assertTrue(feature_flags.is_enabled("video"))

    def test_flag_read_failure_keeps_digital_presenter_disabled(self):
        cache = {"loaded_at": 0, "items": {"digital_presenter": {"enabled": True}}}
        with patch.object(feature_flags, "_CACHE", cache), \
                patch.object(feature_flags, "_load_rows", side_effect=OSError("db down")), \
                patch.object(feature_flags.time, "time", return_value=100):
            self.assertFalse(feature_flags.is_enabled("digital_presenter"))
            self.assertTrue(feature_flags.is_enabled("video"))

    def test_feature_reads_fail_closed_without_changing_legacy_defaults(self):
        with patch.object(feature_flags, "_load_rows", side_effect=OSError("db down")):
            presenter = feature_flags.get_feature("digital_presenter")
            listed = {item["key"]: item for item in feature_flags.list_features()}
        self.assertFalse(presenter["enabled"])
        self.assertFalse(listed["digital_presenter"]["enabled"])
        self.assertTrue(listed["video"]["enabled"])


if __name__ == "__main__":
    unittest.main()
