# 获客 CRM 存储层测试：SQLite 模式行为不变 + PostgreSQL 模式全链路。
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

from content_domains import leads, leads_store  # noqa: E402

PG_URL = os.environ.get("HQ_DATABASE_URL")
LEAD_A = "0123456789abcdef"
LEAD_B = "fedcba9876543210"


def _insert_lead(path, username, lead_id, intent="高意向", status="待跟进",
                 note="", updated_at=0):
    """直接写 SQLite 造数据（绕过被测代码）。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO lead_crm"
            "(username, lead_id, intent, follow_status, follow_note, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (username, lead_id, intent, status, note, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


class SqliteModeTest(unittest.TestCase):
    """默认 sqlite 模式：与迁移前行为逐项一致（公开 API 全走 SQLite 路径）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hq-leads-store-")
        self.db_path = os.path.join(self.tmp, "leads_crm.db")
        self.original_db = leads.LEADS_CRM_DB
        leads.LEADS_CRM_DB = self.db_path
        os.environ.pop("HQ_LEADS_STORE", None)

    def tearDown(self):
        leads.LEADS_CRM_DB = self.original_db
        os.environ.pop("HQ_LEADS_STORE", None)

    def test_sqlite_schema_and_pk_unchanged(self):
        self.assertEqual(leads.list_crm("nobody"), {})
        conn = sqlite3.connect(self.db_path)
        try:
            info = conn.execute("PRAGMA table_info(lead_crm)").fetchall()
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='lead_crm'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual([r[1] for r in info],
                         ["username", "lead_id", "intent", "follow_status",
                          "follow_note", "updated_at"])
        self.assertEqual([r[1] for r in info if r[5]], ["username", "lead_id"])
        self.assertIn("PRIMARY KEY(username, lead_id)", sql)
        self.assertIn("updated_at INTEGER NOT NULL", sql)

    def test_upsert_list_delete_roundtrip(self):
        saved = leads.upsert_crm("fang", {"lead_id": LEAD_A, "intent": "价格敏感",
                                          "follow_status": "跟进中", "follow_note": "已私信报价"})
        self.assertEqual(saved["intent"], "价格敏感")
        self.assertEqual(saved["follow_status"], "跟进中")
        self.assertIsInstance(saved["updated_at"], int)
        row = leads.list_crm("fang", [LEAD_A])[LEAD_A]
        self.assertEqual(row["follow_note"], "已私信报价")
        # 公开契约：返回体只有这 5 个键（不含 username）
        self.assertEqual(sorted(row), ["follow_note", "follow_status", "intent",
                                       "lead_id", "updated_at"])
        self.assertEqual(leads.delete_crm("fang", [LEAD_A]), {"deleted": 1})
        self.assertEqual(leads.list_crm("fang", [LEAD_A]), {})

    def test_upsert_merges_existing_row_and_keeps_new_defaults(self):
        leads.upsert_crm("fang", {"lead_id": LEAD_A, "intent": "咨询",
                                  "follow_status": "已加微", "follow_note": "已加微"})
        leads.upsert_crm("fang", {"lead_id": LEAD_A, "follow_status": "已成交"})
        row = leads.list_crm("fang", [LEAD_A])[LEAD_A]
        self.assertEqual(row["intent"], "咨询")          # 未提交的字段保留原值
        self.assertEqual(row["follow_status"], "已成交")
        self.assertEqual(row["follow_note"], "已加微")
        fresh = leads.upsert_crm("other", {"lead_id": LEAD_B})
        self.assertEqual((fresh["intent"], fresh["follow_status"], fresh["follow_note"]),
                         ("高意向", "待跟进", ""))

    def test_upsert_validation_errors_unchanged(self):
        with self.assertRaises(ValueError) as ctx:
            leads.upsert_crm("fang", {"lead_id": "not-a-hash"})
        self.assertEqual(str(ctx.exception), "线索ID无效")
        with self.assertRaises(ValueError) as ctx:
            leads.upsert_crm("fang", {"lead_id": LEAD_A, "intent": "非常想"})
        self.assertEqual(str(ctx.exception), "意向标签无效")
        with self.assertRaises(ValueError) as ctx:
            leads.upsert_crm("fang", {"lead_id": LEAD_A, "follow_status": "乱填"})
        self.assertEqual(str(ctx.exception), "跟进状态无效")

    def test_follow_note_truncated_to_300(self):
        leads.upsert_crm("fang", {"lead_id": LEAD_A, "follow_note": "备注" * 400})
        self.assertEqual(len(leads.list_crm("fang", [LEAD_A])[LEAD_A]["follow_note"]), 300)

    def test_list_ignores_invalid_ids_and_filters_by_account(self):
        leads.list_crm("fang")                                    # 触发建表
        _insert_lead(self.db_path, "fang", LEAD_A, updated_at=10)
        _insert_lead(self.db_path, "other", LEAD_B, updated_at=20)
        got = leads.list_crm("fang", ["不是指纹", LEAD_A, LEAD_B])
        self.assertEqual(list(got), [LEAD_A])
        self.assertEqual(leads.list_crm("fang", [LEAD_A])[LEAD_A]["updated_at"], 10)

    def test_list_without_ids_orders_desc_and_limits_500(self):
        leads.list_crm("fang")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                "INSERT INTO lead_crm(username, lead_id, intent, follow_status, "
                "follow_note, updated_at) VALUES(?,?,?,?,?,?)",
                [("fang", "%016x" % i, "高意向", "待跟进", "", i) for i in range(520)])
            conn.commit()
        finally:
            conn.close()
        rows = leads.list_crm("fang")
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows["%016x" % 519]["updated_at"], 519)   # 最近修改的在前

    def test_delete_error_messages_unchanged(self):
        with self.assertRaises(ValueError) as ctx:
            leads.delete_crm("fang", [])
        self.assertEqual(str(ctx.exception), "请选择要删除的线索")
        with self.assertRaises(ValueError) as ctx:
            leads.delete_crm("fang", ["不是指纹"])
        self.assertEqual(str(ctx.exception), "请选择要删除的线索")
        with self.assertRaises(ValueError) as ctx:
            leads.delete_crm("fang", [LEAD_A])
        self.assertEqual(str(ctx.exception), "所选线索不存在或不属于当前账号")

    def test_delete_is_scoped_to_account_and_dedupes(self):
        leads.list_crm("fang")
        _insert_lead(self.db_path, "fang", LEAD_A, updated_at=10)
        _insert_lead(self.db_path, "other", LEAD_B, updated_at=20)
        self.assertEqual(leads.delete_crm("fang", [LEAD_A, LEAD_A]), {"deleted": 1})
        self.assertEqual(leads.list_crm("other", [LEAD_B])[LEAD_B]["lead_id"], LEAD_B)

    def test_merge_saved_crm_overlays_follow_state(self):
        leads.upsert_crm("fang", {"lead_id": LEAD_A, "intent": "咨询",
                                  "follow_status": "已加微", "follow_note": "已加微"})
        merged = leads._merge_saved_crm("fang", [
            {"lead_id": LEAD_A, "follow_status": "待跟进", "follow_note": ""},
            {"lead_id": LEAD_B, "follow_status": "待跟进", "follow_note": ""},
        ])
        self.assertEqual(merged[0]["follow_status"], "已加微")
        self.assertEqual(merged[0]["follow_note"], "已加微")
        self.assertEqual(merged[1]["follow_status"], "待跟进")

    def test_mode_defaults_to_sqlite_and_rejects_unknown_value(self):
        self.assertEqual(leads_store.mode(), "sqlite")
        self.assertFalse(leads_store.enabled())
        os.environ["HQ_LEADS_STORE"] = "  SQLite  "
        self.assertEqual(leads_store.mode(), "sqlite")
        os.environ["HQ_LEADS_STORE"] = "mysql"
        with self.assertRaises(RuntimeError) as ctx:
            leads_store.mode()
        self.assertEqual(str(ctx.exception), "HQ_LEADS_STORE must be sqlite or postgres")


@unittest.skipUnless(PG_URL, "HQ_DATABASE_URL 未配置：跳过 PostgreSQL 测试")
class PostgresModeTest(unittest.TestCase):
    """postgres 模式：连接、读写往返、账号隔离与报错传播。"""

    def setUp(self):
        os.environ["HQ_LEADS_STORE"] = "postgres"
        os.environ["HQ_DATABASE_URL"] = PG_URL
        leads_store.close_pool()
        self.usernames = ["m3e_test_a", "m3e_test_b"]

    def tearDown(self):
        self._clean()
        os.environ.pop("HQ_LEADS_STORE", None)
        leads_store.close_pool()

    def _clean(self):
        with leads_store._pool_instance().connection() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM crm.leads WHERE username = ANY(%s)",
                             (self.usernames,))

    def test_enabled_and_mode(self):
        self.assertEqual(leads_store.mode(), "postgres")
        self.assertTrue(leads_store.enabled())

    def test_roundtrip_and_delete(self):
        self._clean()
        leads.upsert_crm("m3e_test_a", {"lead_id": LEAD_A, "intent": "咨询",
                                        "follow_status": "跟进中", "follow_note": "第一条"})
        row = leads.list_crm("m3e_test_a", [LEAD_A])[LEAD_A]
        self.assertEqual((row["intent"], row["follow_status"], row["follow_note"]),
                         ("咨询", "跟进中", "第一条"))
        self.assertIsInstance(row["updated_at"], int)
        self.assertIsNone(leads_store.read_one("m3e_test_a", LEAD_B))
        self.assertEqual(leads_store.read_one("m3e_test_a", LEAD_A)["lead_id"], LEAD_A)
        # 账号隔离：别人的线索读不到、也删不掉
        self.assertEqual(leads.list_crm("m3e_test_b", [LEAD_A]), {})
        with self.assertRaises(ValueError):
            leads.delete_crm("m3e_test_b", [LEAD_A])
        self.assertEqual(leads.delete_crm("m3e_test_a", [LEAD_A, LEAD_B]), {"deleted": 1})

    def test_upsert_merges_existing_row_over_postgres(self):
        self._clean()
        leads.upsert_crm("m3e_test_a", {"lead_id": LEAD_A, "follow_status": "已成交"})
        row = leads.list_crm("m3e_test_a", [LEAD_A])[LEAD_A]
        self.assertEqual(row["follow_status"], "已成交")
        self.assertEqual(row["intent"], "高意向")      # 默认值同样落在 PG 行上

    def test_validation_still_raises_over_postgres(self):
        self._clean()
        with self.assertRaises(ValueError) as ctx:
            leads.upsert_crm("m3e_test_a", {"lead_id": "not-a-hash"})
        self.assertEqual(str(ctx.exception), "线索ID无效")
        with self.assertRaises(ValueError) as ctx:
            leads.upsert_crm("m3e_test_a", {"lead_id": LEAD_A, "intent": "非常想"})
        self.assertEqual(str(ctx.exception), "意向标签无效")
        with self.assertRaises(ValueError) as ctx:
            leads.delete_crm("m3e_test_a", [])
        self.assertEqual(str(ctx.exception), "请选择要删除的线索")

    def test_postgres_mode_never_touches_sqlite(self):
        self._clean()
        tmp = tempfile.mkdtemp(prefix="hq-leads-store-")
        path = os.path.join(tmp, "leads_crm.db")
        original = leads.LEADS_CRM_DB
        leads.LEADS_CRM_DB = path
        try:
            leads.upsert_crm("m3e_test_a", {"lead_id": LEAD_A, "follow_note": "只进 PG"})
            self.assertEqual(leads.list_crm("m3e_test_a", [LEAD_A])[LEAD_A]["follow_note"], "只进 PG")
            leads.delete_crm("m3e_test_a", [LEAD_A])
            self.assertFalse(os.path.exists(path), "postgres 模式不得创建 SQLite 文件")
        finally:
            leads.LEADS_CRM_DB = original

    def test_list_without_ids_orders_desc_and_limits_500(self):
        self._clean()
        with leads_store._pool_instance().connection() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM crm.leads WHERE username = %s", ("m3e_test_500",))
                conn.executemany(
                    "INSERT INTO crm.leads"
                    "(username, lead_id, intent, follow_status, follow_note, updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    [("m3e_test_500", "%016x" % i, "高意向", "待跟进", "", i)
                     for i in range(520)])
        try:
            rows = leads.list_crm("m3e_test_500")
            self.assertEqual(len(rows), 500)
            self.assertEqual(rows["%016x" % 519]["updated_at"], 519)
        finally:
            with leads_store._pool_instance().connection() as conn:
                with conn.transaction():
                    conn.execute("DELETE FROM crm.leads WHERE username = %s", ("m3e_test_500",))

    def test_missing_database_url_raises(self):
        old = os.environ.pop("HQ_DATABASE_URL", None)
        leads_store.close_pool()
        try:
            with self.assertRaises(RuntimeError):
                leads_store.read_crm("m3e_test_a", [])
            with self.assertRaises(RuntimeError):
                leads.list_crm("m3e_test_a")      # 绝不吞错：异常一路抛到上层
        finally:
            if old is not None:
                os.environ["HQ_DATABASE_URL"] = old
            leads_store.close_pool()


if __name__ == "__main__":
    unittest.main()
