# -*- coding: utf-8 -*-
"""provider_config（环境变量型线路的后台覆盖配置）回归测试。

覆盖改造方案第八节可在纯逻辑层验证的验收项：
1 只改 URL；2 只改 Key；3 URL/Key 原子生效；4 验证失败不改生产配置；
5 验证后改字段原证据失效；6 重复点击不重复发布；7 并发旧版本被拒；
10 回滚；11 刷新后仍是后端真实状态；12 输出不出现明文 Key。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.content_domains import provider_config as pc  # noqa: E402

TARGET = "image.xiaole"          # 与号池无关，影响范围干净
TARGET_POOL_SHARED = "image.banana.nb2"
ENV_KEY_NAME = "XIAOLEVIDEO_API_KEY"
ENV_URL_NAME = "XIAOLEVIDEO_API_BASE"
SECRET_OLD = "sk-xiaole-OLD-0001"
SECRET_NEW = "sk-xiaole-NEW-9999"


class ProviderConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="provider-config-test-"))
        self.env_backup = dict(os.environ)
        os.environ["ADMIN_DB"] = str(self.tmp / "admin_config.db")
        os.environ["HQ_PROVIDER_KEYS_MASTER_KEY"] = base64.urlsafe_b64encode(
            b"0123456789abcdef0123456789abcdef"
        ).decode("ascii")
        os.environ.pop("HQ_ADMIN_CONFIG_STORE", None)
        os.environ.pop("HQ_PROVIDER_BASE_HOST_ALLOWLIST", None)
        os.environ[ENV_KEY_NAME] = SECRET_OLD
        os.environ[ENV_URL_NAME] = "https://api.xiaolevideo.cn"
        pc.invalidate()
        pc.init_db()

    def tearDown(self):
        pc.invalidate()
        os.environ.clear()
        os.environ.update(self.env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 基线 ----------

    def test_status_falls_back_to_env_before_any_publish(self):
        st = pc.status(TARGET)
        self.assertEqual(st["source"], pc.SOURCE_ENV)
        self.assertIsNone(st["version"])
        self.assertTrue(st["key_present"])
        self.assertEqual(st["key_last4"], SECRET_OLD[-4:])
        res = pc.resolve(TARGET)
        self.assertEqual(res["source"], pc.SOURCE_ENV)
        self.assertEqual(res["credential"], SECRET_OLD)

    # ---------- 验收 1/2/3 ----------

    def _publish_first(self, url=None, secret=None):
        draft = pc.save_draft(TARGET, url=url, secret=secret, actor="alice")
        pc.record_evidence(TARGET, draft["seq"],
                          {"ok": True, "checks": {"connection": "passed"},
                           "free_verification": True}, actor="alice")
        return pc.publish(TARGET, draft["seq"], expected_seq=0,
                          op_id="op-%s" % draft["seq"], actor="alice")

    def test_1_only_url_keeps_existing_key(self):
        self._publish_first()
        draft = pc.save_draft(TARGET, url="https://api.xiaolevideo.cn", actor="alice")
        pc.record_evidence(TARGET, draft["seq"], {"ok": True}, actor="alice")
        pc.publish(TARGET, draft["seq"], expected_seq=1, op_id="op-url", actor="alice")
        res = pc.resolve(TARGET)
        self.assertEqual(res["source"], pc.SOURCE_BACKEND)
        self.assertEqual(res["credential"], SECRET_OLD)      # Key 未变
        self.assertEqual(res["version"], draft["seq"])

    def test_2_only_key_keeps_existing_url(self):
        self._publish_first(url="https://api.xiaolevideo.cn")
        draft = pc.save_draft(TARGET, secret=SECRET_NEW, actor="alice")
        pc.record_evidence(TARGET, draft["seq"], {"ok": True}, actor="alice")
        pc.publish(TARGET, draft["seq"], expected_seq=1, op_id="op-key", actor="alice")
        res = pc.resolve(TARGET)
        self.assertEqual(res["credential"], SECRET_NEW)
        self.assertEqual(res["url"], "https://api.xiaolevideo.cn")   # URL 未变

    def test_3_url_and_key_change_together(self):
        self._publish_first()
        os.environ["HQ_PROVIDER_BASE_HOST_ALLOWLIST"] = "relay.example.com"
        draft = pc.save_draft(TARGET, url="https://relay.example.com/v1",
                              secret=SECRET_NEW, actor="alice")
        self.assertEqual(draft["url"], "https://relay.example.com/v1")
        pc.record_evidence(TARGET, draft["seq"], {"ok": True}, actor="alice")
        pc.publish(TARGET, draft["seq"], expected_seq=1, op_id="op-both", actor="alice")
        res = pc.resolve(TARGET)
        # URL 与 Key 一定来自同一个版本
        self.assertEqual(res["credential"], SECRET_NEW)
        self.assertEqual(res["url"], "https://relay.example.com/v1")
        self.assertEqual(res["version"], draft["seq"])

    # ---------- 验收 4/5 ----------

    def test_4_failed_evidence_never_changes_production(self):
        self._publish_first()
        draft = pc.save_draft(TARGET, secret=SECRET_NEW, actor="alice")
        pc.record_evidence(TARGET, draft["seq"],
                          {"ok": False, "checks": {"auth": "failed"}}, actor="alice")
        with self.assertRaises(pc.NotVerified):
            pc.publish(TARGET, draft["seq"], expected_seq=1, op_id="op-bad", actor="alice")
        res = pc.resolve(TARGET)
        self.assertEqual(res["version"], 1)
        self.assertEqual(res["credential"], SECRET_OLD)      # 生产未变

    def test_4b_never_published_draft_stays_env(self):
        self._publish_first()
        draft = pc.save_draft(TARGET, secret=SECRET_NEW, actor="alice")
        with self.assertRaises(pc.NotVerified):
            pc.publish(TARGET, draft["seq"], expected_seq=1, op_id="op-nv", actor="alice")
        self.assertEqual(pc.status(TARGET)["version"], 1)

    def test_5_editing_after_verify_invalidates_evidence(self):
        draft = pc.save_draft(TARGET, secret=SECRET_OLD, actor="alice")
        pc.record_evidence(TARGET, draft["seq"], {"ok": True}, actor="alice")
        # 验证后又改了字段 → 产生新版本，旧证据不适用于新版本
        again = pc.save_draft(TARGET, secret=SECRET_NEW, actor="alice")
        self.assertNotEqual(again["seq"], draft["seq"])
        versions = {v["seq"]: v for v in pc.list_versions(TARGET)}
        self.assertIsNone(versions[again["seq"]]["evidence"])
        with self.assertRaises(pc.NotVerified):
            pc.publish(TARGET, again["seq"], expected_seq=0, op_id="op-e2", actor="alice")

    # ---------- 验收 6/7 ----------

    def test_6_repeat_publish_same_op_id_is_idempotent(self):
        draft = pc.save_draft(TARGET, secret=SECRET_OLD, actor="alice")
        pc.record_evidence(TARGET, draft["seq"], {"ok": True}, actor="alice")
        first = pc.publish(TARGET, draft["seq"], expected_seq=0, op_id="dup", actor="alice")
        second = pc.publish(TARGET, draft["seq"], expected_seq=0, op_id="dup", actor="alice")
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["seq"], second["seq"])
        published = [v for v in pc.list_versions(TARGET) if v["status"] == pc.STATUS_PUBLISHED]
        self.assertEqual(len(published), 1)

    def test_7_stale_expected_seq_is_rejected(self):
        self._publish_first()
        draft = pc.save_draft(TARGET, secret=SECRET_NEW, actor="bob")
        pc.record_evidence(TARGET, draft["seq"], {"ok": True}, actor="bob")
        # 另一个管理员已经把版本推到 1；bob 仍以为 0
        with self.assertRaises(pc.VersionConflict):
            pc.publish(TARGET, draft["seq"], expected_seq=0, op_id="op-bob", actor="bob")

    # ---------- 验收 10/11 ----------

    def test_10_rollback_restores_previous_version(self):
        self._publish_first()
        draft = pc.save_draft(TARGET, secret=SECRET_NEW, actor="alice")
        pc.record_evidence(TARGET, draft["seq"], {"ok": True}, actor="alice")
        pc.publish(TARGET, draft["seq"], expected_seq=1, op_id="op-v2", actor="alice")
        self.assertEqual(pc.resolve(TARGET)["credential"], SECRET_NEW)
        out = pc.rollback(TARGET, expected_seq=2, op_id="op-rb", actor="alice")
        res = pc.resolve(TARGET)
        self.assertEqual(res["credential"], SECRET_OLD)          # 恢复旧版本
        self.assertEqual(res["url"], out["url"])
        self.assertEqual(out["restored_seq"], 1)

    def test_11_state_persists_for_a_fresh_reader(self):
        self._publish_first()
        pc.invalidate()
        st = pc.status(TARGET)                                   # 模拟刷新后重新读后端
        self.assertEqual(st["source"], pc.SOURCE_BACKEND)
        self.assertEqual(st["version"], 1)
        self.assertTrue(st["key_present"])

    # ---------- 验收 12 + fail-closed ----------

    def test_12_no_plaintext_secret_in_any_read_output(self):
        self._publish_first(secret=SECRET_OLD)
        blobs = [
            json.dumps(pc.status(TARGET), ensure_ascii=False),
            json.dumps(pc.list_versions(TARGET), ensure_ascii=False),
            json.dumps(pc.targets(), ensure_ascii=False),
        ]
        for blob in blobs:
            self.assertNotIn(SECRET_OLD, blob)
        with sqlite3.connect(str(self.tmp / "admin_config.db")) as conn:
            raw = conn.execute(
                "SELECT url, key_last4, evidence FROM provider_config_versions"
            ).fetchall()
        self.assertNotIn(SECRET_OLD, json.dumps(raw, ensure_ascii=False))

    def test_no_silent_fallback_when_published_credential_is_broken(self):
        self._publish_first()
        with sqlite3.connect(str(self.tmp / "admin_config.db")) as conn:
            conn.execute("UPDATE provider_config_versions SET ciphertext=? WHERE seq=1",
                         (b"tampered-ciphertext",))
            conn.commit()
        pc.invalidate()
        with self.assertRaises(pc.ProviderConfigUnavailable):
            pc.resolve(TARGET)      # 绝不回退到环境变量

    def test_postgres_authority_is_refused(self):
        os.environ["HQ_ADMIN_CONFIG_STORE"] = "postgres"
        with self.assertRaises(pc.ProviderConfigUnavailable):
            pc.status(TARGET)

    # ---------- URL 校验 ----------

    def test_url_validation_rules(self):
        ok = pc.validate_url(TARGET, "https://api.xiaolevideo.cn")
        self.assertEqual(ok, "https://api.xiaolevideo.cn")
        for bad in ("http://api.xiaolevideo.cn",
                    "https://user:pw@api.xiaolevideo.cn",
                    "https://api.xiaolevideo.cn/?k=1",
                    "https://127.0.0.1",
                    "https://evil.example.com"):
            with self.assertRaises(pc.ProviderConfigError):
                pc.validate_url(TARGET, bad)
        os.environ["HQ_PROVIDER_BASE_HOST_ALLOWLIST"] = "relay.example.com"
        self.assertEqual(
            pc.validate_url(TARGET, "https://relay.example.com/v1"),
            "https://relay.example.com/v1")

    def test_pool_shared_target_is_flagged(self):
        shared = {t["target_id"]: t for t in pc.targets()}
        self.assertTrue(shared[TARGET_POOL_SHARED]["pool_shared"])
        self.assertFalse(shared[TARGET]["pool_shared"])


if __name__ == "__main__":
    unittest.main()
