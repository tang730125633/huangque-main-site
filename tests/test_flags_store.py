# 功能开关与价格存储层测试：SQLite 模式行为不变 + PostgreSQL 模式全链路。
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from content_domains import feature_flags, flags_store, pricing  # noqa: E402

PG_URL = os.environ.get("HQ_DATABASE_URL")


class SqliteModeTest(unittest.TestCase):
    """默认 sqlite 模式：与迁移前行为逐项一致（公开 API 全走 SQLite 路径）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hq-flags-store-")
        self.db_path = os.path.join(self.tmp, "feature_flags.db")
        os.environ["FEATURE_FLAGS_DB"] = self.db_path
        os.environ.pop("HQ_FLAGS_STORE", None)
        feature_flags.DB_PATH = Path(self.db_path)
        pricing.DB_PATH = Path(self.db_path)
        feature_flags.invalidate_cache()
        pricing.invalidate_cache()

    def tearDown(self):
        os.environ.pop("FEATURE_FLAGS_DB", None)

    def test_read_empty_creates_tables(self):
        self.assertEqual(feature_flags._load_rows(), {})
        self.assertEqual(pricing._load_rows(), {})
        conn = sqlite3.connect(self.db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertEqual(tables, {"feature_flags", "pricing_rules"})

    def test_flag_write_read_and_public_api(self):
        self.assertTrue(feature_flags.is_enabled("image"))  # 先读一次（生产行为：读即建表）
        feature_flags.set_enabled("video", True, "tang1")
        self.assertTrue(feature_flags.is_enabled("video"))
        got = feature_flags.get_feature("video")
        self.assertTrue(got["enabled"])
        self.assertEqual(got["updated_by"], "tang1")
        feature_flags.set_enabled("video", False, "boss")
        self.assertFalse(feature_flags.is_enabled("video"))
        got = feature_flags.get_feature("video")
        self.assertFalse(got["enabled"])
        self.assertEqual(got["updated_by"], "boss")

    def test_unknown_flag_uses_catalog_default(self):
        self.assertTrue(feature_flags.is_enabled("image"))          # 默认放行
        self.assertFalse(feature_flags.is_enabled("omni_video"))    # 默认关闭
        with self.assertRaises(ValueError):
            feature_flags.set_enabled("no_such_flag", True, "x")

    def test_pricing_write_read_and_validation(self):
        pricing.set_price("image.banana.nb2.std", 25, "admin")
        self.assertEqual(pricing.get_price("image.banana.nb2.std"), 25)
        rule = pricing.get_rule("image.banana.nb2.std")
        self.assertTrue(rule["custom"])
        self.assertEqual(rule["updated_by"], "admin")
        with self.assertRaises(ValueError):
            pricing.set_price("image.banana.nb2.std", 0, "admin")
        with self.assertRaises(ValueError):
            pricing.set_price("image.banana.nb2.std", "abc", "admin")
        with self.assertRaises(ValueError):
            pricing.set_price("no.such.rule", 10, "admin")

    def test_pricing_loads_only_catalog_rules(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE pricing_rules(rule TEXT PRIMARY KEY, points INTEGER NOT NULL, "
            "updated_by TEXT, updated_at INTEGER NOT NULL)")
        conn.execute("INSERT INTO pricing_rules VALUES('legacy.unknown_rule',999,'old',1)")
        conn.commit()
        conn.close()
        pricing.invalidate_cache()
        self.assertEqual(pricing.get_price("image.banana.nb2.std"),
                         pricing.CATALOG_MAP["image.banana.nb2.std"]["default_points"])
        self.assertNotIn("legacy.unknown_rule",
                         [item["key"] for item in pricing.list_prices()])

    def test_fail_closed_unknown_key(self):
        self.assertFalse(feature_flags.is_enabled_fail_closed("no_such_flag"))


@unittest.skipUnless(PG_URL, "HQ_DATABASE_URL 未配置：跳过 PostgreSQL 测试")
class PostgresModeTest(unittest.TestCase):
    """postgres 模式：连接、读写往返、布尔口径与 fail-closed。"""

    def setUp(self):
        os.environ["HQ_FLAGS_STORE"] = "postgres"
        os.environ["HQ_DATABASE_URL"] = PG_URL
        flags_store.close_pool()
        feature_flags.invalidate_cache()
        pricing.invalidate_cache()

    def tearDown(self):
        os.environ.pop("HQ_FLAGS_STORE", None)
        flags_store.close_pool()

    def _clean(self):
        with flags_store._pool_instance().connection() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM ops.pricing_rules WHERE rule = %s",
                             ("test.rule",))
                conn.execute("DELETE FROM ops.feature_flags WHERE feature = %s",
                             ("test_flag",))

    def test_flag_roundtrip_boolean(self):
        self._clean()
        try:
            flags_store.write_flag("test_flag", True, "ci", int(time.time()))
            rows = flags_store.read_flags()
            self.assertIsInstance(rows["test_flag"]["enabled"], bool)
            self.assertTrue(rows["test_flag"]["enabled"])
            flags_store.write_flag("test_flag", False, "ci2", int(time.time()))
            rows = flags_store.read_flags()
            self.assertFalse(rows["test_flag"]["enabled"])
            self.assertEqual(rows["test_flag"]["updated_by"], "ci2")
        finally:
            self._clean()

    def test_price_roundtrip(self):
        self._clean()
        try:
            flags_store.write_price("test.rule", 7, "ci", int(time.time()))
            rows = flags_store.read_prices()
            self.assertEqual(rows["test.rule"]["points"], 7)
        finally:
            self._clean()

    def test_public_api_over_postgres(self):
        key = "dl"  # CATALOG 真实键：公共 API 只接受已注册开关
        with flags_store._pool_instance().connection() as conn:
            original = conn.execute(
                "SELECT * FROM ops.feature_flags WHERE feature = %s", (key,)
            ).fetchone()
        try:
            feature_flags.set_enabled(key, True, "ci")
            feature_flags.invalidate_cache()
            self.assertTrue(feature_flags.is_enabled(key))
            self.assertTrue(feature_flags.get_feature(key)["enabled"])
        finally:
            with flags_store._pool_instance().connection() as conn:
                with conn.transaction():
                    conn.execute("DELETE FROM ops.feature_flags WHERE feature = %s", (key,))
                    if original is not None:
                        conn.execute(
                            "INSERT INTO ops.feature_flags(feature, enabled, updated_by, updated_at) "
                            "VALUES (%s, %s, %s, %s)",
                            (key, original["enabled"],
                             original["updated_by"], original["updated_at"]),
                        )

    def test_missing_database_url_raises(self):
        old = os.environ.pop("HQ_DATABASE_URL", None)
        flags_store.close_pool()
        try:
            with self.assertRaises(RuntimeError):
                flags_store.read_flags()
        finally:
            if old is not None:
                os.environ["HQ_DATABASE_URL"] = old
            flags_store.close_pool()


if __name__ == "__main__":
    unittest.main()
