import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import admin_api  # noqa: E402
import hq_cli_api  # noqa: E402


class HqCliChannelCatalogTests(unittest.TestCase):
    def test_admin_and_cli_keep_the_same_private_channel_ids(self):
        admin_ids = {item["key"] for item in admin_api.KEY_GROUPS}
        cli_ids = {item["id"] for item in hq_cli_api.CHANNEL_CATALOG}
        html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(admin_ids, cli_ids)
        self.assertEqual(
            {item["key"]: item["name"] for item in admin_api.KEY_GROUPS},
            {item["id"]: item["provider"] for item in hq_cli_api.CHANNEL_CATALOG},
        )
        self.assertNotIn('data-channel=', html)
        self.assertNotIn('data-access=', html)
        self.assertEqual(15, len(cli_ids))


if __name__ == "__main__":
    unittest.main()
