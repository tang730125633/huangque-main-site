import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
        self.assertEqual(17, len(cli_ids))
        by_id = {item["id"]: item for item in hq_cli_api.CHANNEL_CATALOG}
        self.assertEqual([
            {"capability": "image-generate", "input": {"provider": "openai"}},
            {"capability": "video-generate", "input": {"channel": "sora"}},
        ], by_id["openai"]["selectors"])
        self.assertEqual([
            {"capability": "image-generate", "input": {"provider": "banana"}},
            {"capability": "video-generate", "input": {"channel": "omni"}},
        ], by_id["gemini"]["selectors"])

    def test_retired_apis_remain_visible_but_cannot_accept_new_jobs(self):
        retired = {item["key"]: item for item in admin_api.KEY_GROUPS
                   if item.get("accepts_new_jobs") is False}
        self.assertEqual(set(retired), {"xiaolevideo", "zelong", "zelong2"})
        self.assertTrue(all(item.get("replacement") for item in retired.values()))
        html = (ROOT / "site" / "admin" / "index.html").read_text(encoding="utf-8")
        self.assertIn("已下架", html)
        self.assertIn("停止接新单", html)

        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "admin.db"
            connection = sqlite3.connect(database)
            connection.executescript("""
                CREATE TABLE admin_channel_config(
                    channel TEXT PRIMARY KEY, enabled INTEGER, config TEXT,
                    updated_by TEXT, updated_at INTEGER
                );
                CREATE TABLE admin_audit(
                    id INTEGER PRIMARY KEY, actor TEXT, action TEXT,
                    target TEXT, detail TEXT, created_at INTEGER
                );
                INSERT INTO admin_channel_config VALUES('zelong',1,'{}','legacy',1);
            """)
            connection.commit()
            connection.close()

            def temporary_db():
                value = sqlite3.connect(database)
                value.row_factory = sqlite3.Row
                return value

            with patch.object(admin_api, "db", temporary_db):
                channels = {item["key"]: item for item in admin_api.load_channels()}
                self.assertFalse(channels["zelong"]["enabled"])
                with self.assertRaisesRegex(ValueError, "不能重新开启"):
                    admin_api.save_channel("tester", {"channel": "zelong", "enabled": True})


if __name__ == "__main__":
    unittest.main()
