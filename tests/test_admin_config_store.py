# 管理配置存储层测试（M3D）：SQLite 模式行为不变 + PostgreSQL 模式全链路。
#
# 默认（未设置 HQ_ADMIN_CONFIG_STORE）走原 SQLite 路径，本文件用真实临时
# admin_config.db 验证「写入仍落在同一个文件、口径不变」；postgres 模式在
# CI / 服务器 staging 上用 HQ_DATABASE_URL 跑（本机无 PG 时自动跳过）。
import base64
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import admin_api  # noqa: E402
from content_domains import admin_config_store, provider_keys  # noqa: E402
from content_domains import feature_flags, pricing  # noqa: E402

PG_URL = os.environ.get("HQ_DATABASE_URL")
MASTER_KEY = base64.urlsafe_b64encode(b"m" * 32).decode()
ACTOR = "m3d-test"
# 密钥池测试用一个**测试专用渠道**，绝不碰真实渠道（xai/deepseek…）的行：
# 共享 staging 库里可能有生产回填数据，用别的密钥加密，掺进来会干扰断言。
TEST_PROVIDER = "m3dtest"
TEST_PROVIDER_PATCH = {
    "PROVIDERS": provider_keys.PROVIDERS | {TEST_PROVIDER},
    "ENV_KEYS": dict(provider_keys.ENV_KEYS, m3dtest="M3D_TEST_API_KEY"),
    "BASE_URLS": dict(provider_keys.BASE_URLS, m3dtest="https://api.x.ai/v1"),
    "BASE_URL_ENVS": dict(provider_keys.BASE_URL_ENVS, m3dtest=()),
    "OFFICIAL_BASE_HOSTS": dict(
        provider_keys.OFFICIAL_BASE_HOSTS, m3dtest={"api.x.ai"}),
}


def _clean_env():
    values = {name: "" for name in provider_keys.ENV_KEYS.values()}
    values["M3D_TEST_API_KEY"] = ""
    values[provider_keys.MASTER_KEY_ENV] = MASTER_KEY
    return values


class SqliteModeTest(unittest.TestCase):
    """默认 sqlite 模式：公开 API 全走 SQLite，行为与迁移前一致。"""

    def setUp(self):
        os.environ.pop("HQ_ADMIN_CONFIG_STORE", None)
        self.tmp = tempfile.mkdtemp(prefix="hq-admin-config-")
        self.db_path = Path(self.tmp) / "admin_config.db"
        self.old_provider_db = provider_keys.DB_PATH
        self.old_admin_db = admin_api.ADMIN_DB
        # feature_flags/pricing 的 DB_PATH 是 import 时固定的模块常量，不随 env 变；
        # SQLite 模式的审计用例会经 admin_api.save_feature 写它们，必须一并指向
        # 本临时库并建表，否则在干净环境（服务器/CI）里会 no such table。
        self.old_flags_path = feature_flags.DB_PATH
        self.old_pricing_path = pricing.DB_PATH
        provider_keys.DB_PATH = self.db_path
        admin_api.ADMIN_DB = self.db_path
        feature_flags.DB_PATH = self.db_path
        pricing.DB_PATH = self.db_path
        feature_flags.init_db()
        pricing.init_db()
        self.env = patch.dict(
            os.environ,
            dict(_clean_env(), FEATURE_FLAGS_DB=str(self.db_path),
                 PRICING_DB=str(self.db_path)),
            clear=False,
        )
        self.env.start()
        self.provider_patch = [
            patch.object(provider_keys, name, value)
            for name, value in TEST_PROVIDER_PATCH.items()
        ]
        for item in self.provider_patch:
            item.start()
        provider_keys._LEGACY_IMPORT_PATHS.clear()

    def tearDown(self):
        provider_keys.DB_PATH = self.old_provider_db
        admin_api.ADMIN_DB = self.old_admin_db
        feature_flags.DB_PATH = self.old_flags_path
        pricing.DB_PATH = self.old_pricing_path
        feature_flags.invalidate_cache()
        pricing.invalidate_cache()
        provider_keys._LEGACY_IMPORT_PATHS.clear()
        for item in self.provider_patch:
            item.stop()
        self.env.stop()

    def _rows(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def test_mode_defaults_to_sqlite(self):
        self.assertEqual(admin_config_store.mode(), "sqlite")
        self.assertFalse(admin_config_store.enabled())

    def test_invalid_mode_raises(self):
        os.environ["HQ_ADMIN_CONFIG_STORE"] = "mysql"
        try:
            with self.assertRaises(RuntimeError):
                admin_config_store.mode()
        finally:
            os.environ.pop("HQ_ADMIN_CONFIG_STORE", None)

    def test_provider_key_pool_still_writes_sqlite(self):
        item = provider_keys.add_key(TEST_PROVIDER, "线路A", "sk-live-1234567890", "tang1")
        self.assertEqual(item["last4"], "7890")
        rows = self._rows("SELECT provider,last4,state,base_url FROM provider_api_keys")
        self.assertEqual([(r["provider"], r["last4"], r["state"]) for r in rows],
                         [(TEST_PROVIDER, "7890", "active")])
        self.assertEqual(rows[0]["base_url"], provider_keys.BASE_URLS[TEST_PROVIDER])
        # 明文绝不出现在库里，只有 AES-GCM 密文
        self.assertNotIn(b"sk-live-1234567890",
                         self._rows("SELECT ciphertext FROM provider_api_keys")[0]["ciphertext"])

    def test_provider_key_roundtrip_sqlite(self):
        first = provider_keys.add_key(TEST_PROVIDER, "线路A", "sk-aaaaaaaaaa", "tang1")
        provider_keys.add_key(TEST_PROVIDER, "线路B", "sk-bbbbbbbbbb", "tang1")
        with self.assertRaises(ValueError):
            provider_keys.add_key(TEST_PROVIDER, "重复", "sk-aaaaaaaaaa", "tang1")
        self.assertEqual(len(provider_keys.candidates(TEST_PROVIDER)), 2)
        self.assertEqual(provider_keys.reveal_key(first["id"]), "sk-aaaaaaaaaa")
        claimed = provider_keys.claim_candidate(TEST_PROVIDER)
        self.assertIn(claimed["id"], {item["id"] for item in provider_keys.list_public()})
        self.assertEqual(
            self._rows("SELECT use_count FROM provider_api_keys WHERE id=?",
                       (claimed["id"],))[0]["use_count"], 1)
        provider_keys.set_health(claimed["id"], False, 12, "boom")
        self.assertEqual(
            self._rows("SELECT health_status FROM provider_api_keys WHERE id=?",
                       (claimed["id"],))[0]["health_status"], "unhealthy")
        provider_keys.retire_key(first["id"])
        self.assertEqual(
            self._rows("SELECT state FROM provider_api_keys WHERE id=?",
                       (first["id"],))[0]["state"], "retired")
        with self.assertRaises(ValueError):
            provider_keys.reveal_key(first["id"])
        self.assertNotIn(first["id"], [item["id"] for item in provider_keys.list_public()])

    def test_admin_audit_still_writes_sqlite(self):
        with patch.object(admin_api, "feature_flags", None):
            admin_api.init_db()
        admin_api._admin_audit(ACTOR, "provider_key.add", "k1", {"provider": "xai"})
        admin_api.save_feature(ACTOR, {"feature": "dl", "enabled": True, "reason": "test"})
        rows = self._rows("SELECT actor,action,target FROM admin_audit ORDER BY id")
        self.assertEqual([(r["actor"], r["action"]) for r in rows],
                         [(ACTOR, "provider_key.add"), (ACTOR, "feature.toggle")])

    def test_channel_config_still_writes_sqlite(self):
        with patch.object(admin_api, "feature_flags", None):
            admin_api.init_db()
        item = admin_api.save_channel(
            ACTOR, {"channel": "deepseek", "enabled": True, "reason": "test"})
        self.assertTrue(item["enabled"])
        rows = self._rows("SELECT channel,enabled FROM admin_channel_config")
        self.assertEqual([(r["channel"], r["enabled"]) for r in rows], [("deepseek", 1)])
        self.assertEqual(len(admin_api.load_channels()), len(admin_api.CHANNELS))


@unittest.skipUnless(PG_URL, "HQ_DATABASE_URL 未配置：跳过 PostgreSQL 测试")
class PostgresModeTest(unittest.TestCase):
    """postgres 模式：密钥池、渠道开关、管理审计三段真实读写往返。"""

    def setUp(self):
        os.environ["HQ_ADMIN_CONFIG_STORE"] = "postgres"
        os.environ["HQ_DATABASE_URL"] = PG_URL
        admin_config_store.close_pool()
        provider_keys._LEGACY_IMPORT_PATHS.clear()
        self.env = patch.dict(os.environ, _clean_env(), clear=False)
        self.env.start()
        self.provider_patch = [
            patch.object(provider_keys, name, value)
            for name, value in TEST_PROVIDER_PATCH.items()
        ]
        for item in self.provider_patch:
            item.start()
        self._clean()

    def tearDown(self):
        self._clean()
        for item in self.provider_patch:
            item.stop()
        os.environ.pop("HQ_ADMIN_CONFIG_STORE", None)
        admin_config_store.close_pool()
        provider_keys._LEGACY_IMPORT_PATHS.clear()
        self.env.stop()

    def _clean(self):
        """只清本测试自己造的行：测试专用渠道 + 本测试的 actor。

        真实渠道（xai/deepseek…）的行一律不动，共享 staging 库可安全重跑。
        """
        with admin_config_store._pool_instance().connection() as conn:
            with conn.transaction():
                conn.execute(
                    "DELETE FROM ops.admin_provider_api_keys WHERE provider = %s",
                    (TEST_PROVIDER,))
                conn.execute("DELETE FROM ops.admin_channel_config WHERE updated_by = %s",
                             (ACTOR,))
                conn.execute("DELETE FROM ops.admin_audit WHERE actor = %s", (ACTOR,))

    # ---- 存储层原语 ----

    def test_key_insert_read_roundtrip_keeps_bytes(self):
        now = int(time.time())
        admin_config_store.insert_key(
            "m3d-key-1", TEST_PROVIDER, "线路A", "7890", b"\x00\x01cipher", b"\x00nonce",
            "https://api.x.ai/v1", 1, "healthy", now, 12, "", ACTOR, now)
        row = admin_config_store.fetch_key("m3d-key-1")
        self.assertEqual(bytes(row["ciphertext"]), b"\x00\x01cipher")
        self.assertEqual(bytes(row["nonce"]), b"\x00nonce")
        self.assertEqual(row["use_count"], 0)
        self.assertEqual(admin_config_store.count_by_provider()[TEST_PROVIDER], 1)
        self.assertIn("m3d-key-1", admin_config_store.fetch_keys())

    def test_claim_key_picks_least_used_and_increments(self):
        now = int(time.time())
        for key_id, uses in (("m3d-key-a", 3), ("m3d-key-b", 0)):
            admin_config_store.insert_key(
                key_id, TEST_PROVIDER, key_id, "0000", b"c", b"n", "https://api.x.ai/v1",
                1, "healthy", now, 1, "", ACTOR, now)
            for _ in range(uses):
                admin_config_store.claim_key(TEST_PROVIDER, (), now)
        claimed = admin_config_store.claim_key(TEST_PROVIDER, (), now)
        self.assertEqual(claimed["id"], "m3d-key-b")
        self.assertEqual(claimed["use_count"], 1)
        # 显式隔离的密钥不再被认领
        blocked = admin_config_store.claim_key(TEST_PROVIDER, ("m3d-key-b",), now)
        self.assertEqual(blocked["id"], "m3d-key-a")

    def test_health_retire_and_env_snapshot_once(self):
        now = int(time.time())
        admin_config_store.insert_key(
            "m3d-key-h", TEST_PROVIDER, "线路H", "0000", b"c", b"n", "https://api.x.ai/v1",
            1, "unknown", None, None, "", ACTOR, now)
        self.assertTrue(admin_config_store.write_health("m3d-key-h", False, 9, "boom", now))
        self.assertEqual(admin_config_store.fetch_key("m3d-key-h")["health_status"],
                         "unhealthy")
        self.assertTrue(admin_config_store.retire_key("m3d-key-h", now))
        self.assertFalse(admin_config_store.retire_key("m3d-key-h", now))
        self.assertFalse(admin_config_store.write_health("m3d-no-such", True, None, "", now))
        # 环境变量一次性托管：同渠道第二次不再插入
        first = admin_config_store.insert_env_key_once(
            "m3d-key-env", TEST_PROVIDER, "服务器环境变量（已加密托管）", "0000", b"c",
            b"n", "https://api.x.ai/v1", 0, "unknown", None, None, "",
            "system-env-migration", now)
        second = admin_config_store.insert_env_key_once(
            "m3d-key-env-2", TEST_PROVIDER, "服务器环境变量（已加密托管）", "0000", b"c",
            b"n", "https://api.x.ai/v1", 0, "unknown", None, None, "",
            "system-env-migration", now)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertIsNone(admin_config_store.fetch_key("m3d-key-env-2"))
        with admin_config_store._pool_instance().connection() as conn:
            with conn.transaction():
                conn.execute(
                    "DELETE FROM ops.admin_provider_api_keys WHERE provider = %s",
                    (TEST_PROVIDER,))

    def test_missing_database_url_raises(self):
        old = os.environ.pop("HQ_DATABASE_URL", None)
        admin_config_store.close_pool()
        try:
            with self.assertRaises(RuntimeError):
                admin_config_store.fetch_keys()
        finally:
            if old is not None:
                os.environ["HQ_DATABASE_URL"] = old
            admin_config_store.close_pool()

    # ---- 原模块公开 API ----

    def test_provider_key_public_api_over_postgres(self):
        item = provider_keys.add_key(TEST_PROVIDER, "线路A", "sk-aaaaaaaaaa", ACTOR)
        self.assertEqual(item["last4"], "aaaa")
        self.assertEqual([row["id"] for row in provider_keys.candidates(TEST_PROVIDER)],
                         [item["id"]])
        self.assertEqual(provider_keys.reveal_key(item["id"]), "sk-aaaaaaaaaa")
        with self.assertRaises(ValueError):
            provider_keys.add_key(TEST_PROVIDER, "重复", "sk-aaaaaaaaaa", ACTOR)
        claimed = provider_keys.claim_candidate(TEST_PROVIDER)
        self.assertEqual(claimed["id"], item["id"])
        self.assertEqual(admin_config_store.fetch_key(item["id"])["use_count"], 1)
        provider_keys.set_health(item["id"], False, 5, "boom")
        self.assertEqual(provider_keys.candidates(TEST_PROVIDER), [])
        provider_keys.retire_key(item["id"])
        with self.assertRaises(ValueError):
            provider_keys.reveal_key(item["id"])
        # 同明文重新添加恢复原编号（而不是新建一行）
        restored = provider_keys.add_key(TEST_PROVIDER, "线路A", "sk-aaaaaaaaaa", ACTOR)
        self.assertEqual(restored["id"], item["id"])
        self.assertEqual(restored["state"], "active")
        self.assertEqual(
            [row["id"] for row in admin_config_store.fetch_keys().values()
             if row["provider"] == TEST_PROVIDER], [item["id"]])

    def test_provider_key_rotation_across_two_keys(self):
        first = provider_keys.add_key(TEST_PROVIDER, "线路A", "sk-aaaaaaaaaa", ACTOR)
        second = provider_keys.add_key(TEST_PROVIDER, "线路B", "sk-bbbbbbbbbb", ACTOR)
        claimed = [provider_keys.claim_candidate(TEST_PROVIDER)["id"] for _ in range(2)]
        self.assertEqual(sorted(claimed), sorted([first["id"], second["id"]]))

    def test_audit_and_channel_over_postgres(self):
        with patch.object(admin_api, "feature_flags", None):
            admin_api.init_db()
        admin_api._admin_audit(ACTOR, "provider_key.add", "k1", {"provider": "xai"})
        rows = admin_config_store.read_audit_by_actions(["provider_key.add"])
        self.assertEqual(
            [(r["actor"], r["target"]) for r in rows if r["actor"] == ACTOR],
            [(ACTOR, "k1")])
        item = admin_api.save_channel(
            ACTOR, {"channel": "deepseek", "enabled": True,
                    "config": {"cost": "1"}, "reason": "test"})
        self.assertTrue(item["enabled"])
        self.assertEqual(item["config"], {"cost": "1"})
        # 渠道配置与它的审计行必须同时存在（同一事务）
        saved = admin_config_store.fetch_keys()  # 触发一次真实连接
        self.assertIsInstance(saved, dict)
        self.assertEqual(
            [r["channel"] for r in admin_config_store.read_channels().values()
             if r["updated_by"] == ACTOR], ["deepseek"])
        audit = admin_config_store.read_audit_by_actions(["channel.save"])
        self.assertEqual([r["target"] for r in audit if r["actor"] == ACTOR], ["deepseek"])
        loaded = {row["key"]: row for row in admin_api.load_channels()}
        self.assertTrue(loaded["deepseek"]["enabled"])
        # 三个审计读方都要能从 PG 读回（运行历史页 / 服务事件页 / 事件恢复判定）
        legacy = admin_config_store.read_legacy_audit(("provider_key.",), 50)
        self.assertTrue(any(row["target"] == "k1" for row in legacy))
        self.assertIn("created", legacy[0])
        admin_api._admin_audit(ACTOR, "service.incident.open", "content",
                               {"service": "内容服务"})
        events = admin_api.service_monitor_events(5)
        self.assertTrue(any(item["service_key"] == "content" for item in events))
        self.assertIn("content", admin_api._persisted_open_service_incidents())


if __name__ == "__main__":
    unittest.main()
