import importlib.util
from pathlib import Path
import sys
import unittest


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER_DIR))
SPEC = importlib.util.spec_from_file_location("auth_server", SERVER_DIR / "auth_server.py")
auth_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auth_server)


class JsapiTestAmountTests(unittest.TestCase):
    def test_any_user_can_use_ten_cent_test_tier(self):
        self.assertEqual(auth_server.jsapi_recharge_quote(0.1), (0.1, 1))

    def test_regular_tiers_use_ten_points_per_yuan(self):
        self.assertEqual(auth_server.jsapi_recharge_quote(100), (100, 1000))
        self.assertEqual(auth_server.jsapi_recharge_quote(10), (10, 100))


if __name__ == "__main__":
    unittest.main()
