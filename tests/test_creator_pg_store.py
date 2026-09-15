# 创作 Agent 存储层测试：SQLite 模式行为不变 + PostgreSQL 模式全链路。
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from creator_agent import pg_store  # noqa: E402
from creator_agent.model_usage import ModelUsageError, ModelUsageGuard  # noqa: E402
from creator_agent.store import (  # noqa: E402
    CreatorAgentStore, IdempotencyConflict, StateConflict, StoreError,
)

PG_URL = os.environ.get("HQ_DATABASE_URL")
USER = "creator-pg-qa"
PROJECT = "abcdef012345"
STORE_TABLES = {
    "creator_account_state", "creator_workspaces", "creator_messages",
    "creator_batches", "creator_jobs",
}
ALL_TABLES = STORE_TABLES | {"creator_model_calls"}


def plans(*platforms):
    return [
        {"platform": platform, "input": {"action": "matrix-template-generate",
                                         "text": "第 %s 条" % platform}}
        for platform in platforms
    ]


class ModuleContractTest(unittest.TestCase):
    """硬性契约：新模块 PG-only（无 sqlite3），且不 import server.db.postgres。

    生产 creator-agent 进程的 sys.path 里没有 server/ 包，一旦 import 就是启动即炸，
    所以这里把「不许出现该 import」钉成测试（注释里提到模块名不算）。
    """

    def test_pg_store_is_postgres_only(self):
        source = (Path(SERVER) / "creator_agent" / "pg_store.py").read_text(
            encoding="utf-8")
        self.assertNotIn("import sqlite3", source)
        self.assertIsNone(re.search(
            r"(?m)^\s*(from\s+[\w.]*server[\w.]*\s+import|import\s+[\w.]*server\.db)",
            source))
        self.assertIsNone(re.search(
            r"(?m)^\s*from\s+\.{1,2}db\s+import\s+postgres", source))
        self.assertIsNone(re.search(
            r"(?m)^\s*from\s+server\.db\s+import\s+postgres", source))


class SqliteModeTest(unittest.TestCase):
    """默认 sqlite 模式：与迁移前行为逐项一致（公开 API 全走 SQLite 路径）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hq-creator-store-")
        self.db_path = os.path.join(self.tmp, "creator_agent.db")
        os.environ.pop("HQ_CREATOR_STORE", None)
        self.store = CreatorAgentStore(self.db_path)

    def tearDown(self):
        os.environ.pop("HQ_CREATOR_STORE", None)

    def test_default_mode_is_sqlite(self):
        self.assertEqual(pg_store.mode(), "sqlite")
        self.assertFalse(pg_store.enabled())

    def test_sqlite_schema_is_created_on_construction(self):
        conn = sqlite3.connect(self.db_path)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertTrue(STORE_TABLES <= tables)

    def test_db_returns_plain_sqlite_connection(self):
        with closing(self.store.db()) as connection:
            self.assertIsInstance(connection, sqlite3.Connection)
            self.assertIs(connection.row_factory, sqlite3.Row)

    def test_store_roundtrip_over_sqlite(self):
        self.assertIsNone(self.store.workspace(USER, PROJECT))
        workspace = self.store.ensure_workspace(USER, PROJECT, "我的项目")
        self.assertEqual(workspace["alias"], "我的项目")
        self.assertEqual(workspace["template_video_preferences"],
                         {"global": [], "platforms": {}})
        self.store.update_workspace(USER, PROJECT, platforms=["douyin"])
        self.assertEqual(self.store.workspace(USER, PROJECT)["platforms"], ["douyin"])
        with self.assertRaises(StoreError):
            self.store.update_workspace(USER, "no-such-project", alias="x")

    def test_message_idempotency_over_sqlite(self):
        self.store.ensure_workspace(USER, PROJECT)
        first, created = self.store.add_message(
            USER, PROJECT, "user", "给我做三条短视频",
            request_id="req-1", request_hash="hash-1",
        )
        self.assertTrue(created)
        again, created = self.store.add_message(
            USER, PROJECT, "user", "给我做三条短视频",
            request_id="req-1", request_hash="hash-1",
        )
        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        with self.assertRaises(IdempotencyConflict):
            self.store.add_message(
                USER, PROJECT, "user", "换个说法",
                request_id="req-1", request_hash="hash-2",
            )

    def test_batch_lifecycle_over_sqlite(self):
        self.store.ensure_workspace(USER, PROJECT)
        batch = self.store.create_batch(USER, PROJECT, "主题", "目标", plans("douyin"))
        self.assertEqual(batch["status"], "draft")
        claimed = self.store.claim_quote(USER, batch["id"], batch["revision"])
        self.assertFalse(claimed["quote_reused"])
        quotes = [
            {"id": job["id"], "cost": 30, "expires_at": int(time.time()) + 3600,
             "quote_token": "token-" + job["id"], "input_hash": job["input_hash"],
             "quote": {"points": 30}}
            for job in claimed["jobs"]
        ]
        quoted = self.store.finish_quote(
            USER, batch["id"], claimed["claim_id"], quotes, {"points": 30})
        self.assertEqual(quoted["status"], "quoted")
        self.assertEqual(len(self.store.batches(USER, PROJECT)), 1)
        self.assertEqual(self.store.latest_batch(USER, PROJECT)["id"], batch["id"])

    def test_invalid_mode_raises(self):
        os.environ["HQ_CREATOR_STORE"] = "mysql"
        try:
            with self.assertRaises(RuntimeError):
                self.store.workspace(USER, PROJECT)
        finally:
            os.environ.pop("HQ_CREATOR_STORE", None)


class _RecordingConnection:
    """记录 SQL 的 SQLite 连接包装：把用量守卫真正发出的语句原样记下来。"""

    def __init__(self, connection, log):
        self._connection = connection
        self._log = log

    def execute(self, statement, params=()):
        self._log.append((statement, params))
        return self._connection.execute(statement, params)

    def commit(self):
        return self._connection.commit()

    def rollback(self):
        return self._connection.rollback()

    def close(self):
        return self._connection.close()


class UsageTranslationTest(unittest.TestCase):
    """用量守卫适配器的翻译面：本机（无 PG）用 SQLite 驱动真实语句后逐条翻译。

    ``model_usage.py`` 不在本域文件边界内，它发什么语句我们只能被动适配，所以这里
    把守卫**真实发出**的每一条语句都过一遍 ``_translate``：只要有一条翻不了或还剩
    ``?`` 占位符，测试就红——这正是「绝不静默执行错语义」的守门测试。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hq-creator-usage-")
        os.environ.pop("HQ_CREATOR_STORE", None)
        self.store = CreatorAgentStore(os.path.join(self.tmp, "creator_agent.db"))
        self.statements = []
        real_db = self.store.db

        def factory():
            return _RecordingConnection(real_db(), self.statements)

        self.guard = ModelUsageGuard(
            factory, window_seconds=60, user_window_requests=5, ip_window_requests=5,
            user_concurrency=2, global_concurrency=8, user_daily_requests=10,
            global_daily_requests=100, user_daily_tokens=100000,
            global_daily_tokens=1000000, user_daily_cost_micro_usd=1000000,
            global_daily_cost_micro_usd=10000000,
        )

    def test_every_statement_the_guard_emits_is_translatable(self):
        lease = self.guard.acquire(USER, "10.0.0.1", "profile_question", 1000, 2400)
        lease.finish(True)
        lease = self.guard.acquire(USER, "10.0.0.2", "plan", 1000, 2400)
        lease.finish(False)
        self.assertTrue(self.guard.health())
        self.assertGreater(len(self.statements), 8)
        kinds = set()
        no_ops = 0
        for statement, _params in self.statements:
            translated = pg_store._UsageConnection._translate(statement)
            if translated is None:
                # 建表/建索引在 PG 模式是空操作（schema 归 Alembic，运行角色无 CREATE）
                no_ops += 1
            else:
                self.assertNotIn("?", translated)
                self.assertTrue(
                    "agent.creator_model_calls" in translated
                    or "information_schema" in translated,
                    translated,
                )
            kinds.add(statement.strip().split()[0].upper())
        self.assertGreater(no_ops, 0)
        # 守卫用到的语句类别都必须被覆盖到
        self.assertEqual(kinds, {"BEGIN", "CREATE", "PRAGMA", "UPDATE", "SELECT", "INSERT",
                                 "DELETE"})

    def test_begin_immediate_becomes_a_table_lock(self):
        self.assertEqual(
            pg_store._UsageConnection._translate("BEGIN IMMEDIATE"),
            "LOCK TABLE agent.creator_model_calls IN EXCLUSIVE MODE",
        )

    def test_pragma_table_info_uses_information_schema(self):
        translated = pg_store._UsageConnection._translate(
            "PRAGMA table_info(creator_model_calls)")
        self.assertIn("information_schema.columns", translated)
        self.assertIn("'creator_model_calls'", translated)

    def test_scalar_max_becomes_greatest_and_placeholders_flip(self):
        translated = pg_store._UsageConnection._translate(
            "UPDATE creator_model_calls SET estimated_cost_micro_usd=MAX("
            "estimated_cost_micro_usd, ((estimated_tokens + ?) * ? + ? - 1) / ?) "
            "WHERE price_version='legacy-unversioned'")
        self.assertIn("=GREATEST(", translated)
        self.assertNotIn("MAX(", translated)
        self.assertNotIn("?", translated)
        self.assertEqual(translated.count("%s"), 4)

    def test_ddl_is_a_no_op_and_alter_fails_loud(self):
        # 建表/建索引：空操作（Alembic 是唯一 schema 权威，运行角色不该有 CREATE）
        for statement in (
            "CREATE TABLE IF NOT EXISTS creator_model_calls(id TEXT PRIMARY KEY)",
            "CREATE INDEX IF NOT EXISTS idx_creator_model_calls_user_time "
            "ON creator_model_calls(username,created_at)",
        ):
            self.assertIsNone(pg_store._UsageConnection._translate(statement))
        # 缺列才会发出 ALTER：必须报错让人去跑迁移，绝不静默跳过
        with self.assertRaises(RuntimeError) as raised:
            pg_store._UsageConnection._translate(
                "ALTER TABLE creator_model_calls ADD COLUMN x TEXT NOT NULL DEFAULT ''")
        self.assertIn("alembic upgrade head", str(raised.exception))

    def test_table_name_is_schema_qualified(self):
        translated = pg_store._UsageConnection._translate(
            "SELECT COUNT(*) FROM creator_model_calls WHERE state='active'")
        self.assertIn("FROM agent.creator_model_calls", translated)
        translated = pg_store._UsageConnection._translate(
            "DELETE FROM creator_model_calls WHERE created_at<? AND state<>'active'")
        self.assertIn("DELETE FROM agent.creator_model_calls", translated)
        self.assertEqual(translated.count("%s"), 1)

    def test_unknown_statement_fails_loud(self):
        with self.assertRaises(RuntimeError):
            pg_store._UsageConnection._translate("PRAGMA journal_mode=WAL")
        with self.assertRaises(RuntimeError):
            pg_store._UsageConnection._translate("VACUUM")


@unittest.skipUnless(PG_URL, "HQ_DATABASE_URL 未配置：跳过 PostgreSQL 测试")
class PostgresModeTest(unittest.TestCase):
    """postgres 模式：六张表全链路 + 用量账本适配器。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hq-creator-pg-")
        self.db_path = os.path.join(self.tmp, "creator_agent.db")
        os.environ["HQ_CREATOR_STORE"] = "postgres"
        os.environ["HQ_DATABASE_URL"] = PG_URL
        pg_store.close_pool()
        self._clean()
        self.store = CreatorAgentStore(self.db_path)

    def tearDown(self):
        self._clean()
        os.environ.pop("HQ_CREATOR_STORE", None)
        pg_store.close_pool()

    def _clean(self):
        with pg_store._connection() as conn:
            with conn.transaction():
                for table in ALL_TABLES:
                    conn.execute("DELETE FROM agent.%s WHERE username = %%s" % table,
                                 (USER,))
                conn.execute("DELETE FROM agent.creator_account_state WHERE username=%s",
                             ("__creator_health__",))

    def test_mode_and_missing_database_url(self):
        self.assertEqual(pg_store.mode(), "postgres")
        self.assertTrue(pg_store.enabled())
        old = os.environ.pop("HQ_DATABASE_URL", None)
        pg_store.close_pool()
        try:
            with self.assertRaises(RuntimeError):
                pg_store.workspace(USER, PROJECT)
        finally:
            if old is not None:
                os.environ["HQ_DATABASE_URL"] = old
            pg_store.close_pool()

    def test_postgres_mode_does_not_touch_the_sqlite_file(self):
        self.assertFalse(os.path.exists(self.db_path))

    def test_workspace_roundtrip(self):
        self.assertIsNone(self.store.workspace(USER, PROJECT))
        workspace = self.store.ensure_workspace(USER, PROJECT, "我的项目")
        self.assertEqual(workspace["alias"], "我的项目")
        self.store.set_active_project(USER, PROJECT)
        self.assertEqual(self.store.active_project(USER), PROJECT)
        self.store.update_workspace(USER, PROJECT, platforms=["douyin"],
                                    profile={"nickname": "小雀"})
        again = self.store.workspace(USER, PROJECT)
        self.assertEqual(again["platforms"], ["douyin"])
        self.assertEqual(again["profile"], {"nickname": "小雀"})
        self.assertEqual([item["project_id"] for item in self.store.workspaces(USER)],
                         [PROJECT])
        with self.assertRaises(StoreError):
            self.store.update_workspace(USER, "no-such-project", alias="x")

    def test_profile_revision_conflict(self):
        self.store.ensure_workspace(USER, PROJECT)
        state = {"revision": 2, "current_module": 1}
        updated = self.store.update_profile_state(USER, PROJECT, state, 1)
        self.assertEqual(updated["profile_state"], state)
        with self.assertRaises(StateConflict):
            self.store.update_profile_state(USER, PROJECT, {"revision": 3}, 1)

    def test_message_idempotency_and_order(self):
        self.store.ensure_workspace(USER, PROJECT)
        first, created = self.store.add_message(
            USER, PROJECT, "user", "第一条", request_id="pg-req-1",
            request_hash="hash-1")
        self.assertTrue(created)
        again, created = self.store.add_message(
            USER, PROJECT, "user", "第一条", request_id="pg-req-1",
            request_hash="hash-1")
        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        with self.assertRaises(IdempotencyConflict):
            self.store.add_message(USER, PROJECT, "user", "换一个", request_id="pg-req-1",
                                   request_hash="hash-2")
        second, _ = self.store.add_message(USER, PROJECT, "assistant", "第一条回复")
        self.assertEqual([item["id"] for item in self.store.messages(USER, PROJECT)],
                         [first["id"], second["id"]])
        with self.assertRaises(StoreError):
            self.store.add_message(USER, "no-such-project", "user", "越界")

    def test_message_turn_commit(self):
        self.store.ensure_workspace(USER, PROJECT)
        request, _ = self.store.add_message(USER, PROJECT, "user", "问题")
        turn = self.store.commit_message_turn(
            USER, PROJECT, request["id"], "回答", {"kind": "assistant_reply"})
        self.assertIn("assistant_message_id", turn)
        messages = self.store.messages(USER, PROJECT)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["public"]["turn"]["reply"], "回答")

    def test_batch_lifecycle_and_ordering(self):
        self.store.ensure_workspace(USER, PROJECT)
        first = self.store.create_batch(USER, PROJECT, "第一批", "目标", plans("douyin"))
        second = self.store.create_batch(
            USER, PROJECT, "第二批", "目标", plans("douyin", "xiaohongshu"),
            source_message_id=7)
        # 同一消息重复建批次：返回同一批次（与 SQLite 的部分唯一索引同语义）
        repeated = self.store.create_batch(
            USER, PROJECT, "第二批", "目标", plans("douyin"), source_message_id=7)
        self.assertEqual(repeated["id"], second["id"])
        self.assertEqual(self.store.batch_for_source_message(USER, PROJECT, 7)["id"],
                         second["id"])
        self.assertEqual(len(second["jobs"]), 2)
        self.assertEqual(self.store.latest_batch(USER, PROJECT)["id"], second["id"])
        self.assertEqual([item["id"] for item in self.store.batches(USER, PROJECT)],
                         [second["id"], first["id"]])

        claimed = self.store.claim_quote(USER, first["id"], first["revision"])
        self.assertFalse(claimed["quote_reused"])
        with self.assertRaises(StateConflict):
            self.store.claim_quote(USER, first["id"], first["revision"])
        quotes = [
            {"id": job["id"], "cost": 30, "expires_at": int(time.time()) + 3600,
             "quote_token": "token-" + job["id"], "input_hash": job["input_hash"],
             "quote": {"points": 30}}
            for job in claimed["jobs"]
        ]
        quoted = self.store.finish_quote(
            USER, first["id"], claimed["claim_id"], quotes, {"points": 30})
        self.assertEqual(quoted["status"], "quoted")
        self.assertTrue(quoted["quote_expires_at"] > int(time.time()))
        reusable = self.store.claim_quote(USER, first["id"], quoted["revision"])
        self.assertTrue(reusable["quote_reused"])
        # 报价中途放弃：认领第二个批次后 abort，回到可报价态（与 SQLite 同语义）
        second_claim = self.store.claim_quote(USER, second["id"], second["revision"])
        self.store.abort_quote(USER, second["id"], second_claim["claim_id"])
        self.assertEqual(self.store.batch(USER, second["id"])["status"], "ready")

    def test_confirmation_and_job_lifecycle(self):
        self.store.ensure_workspace(USER, PROJECT)
        batch = self.store.create_batch(USER, PROJECT, "主题", "目标", plans("douyin"))
        claimed = self.store.claim_quote(USER, batch["id"], batch["revision"])
        quotes = [
            {"id": job["id"], "cost": 30, "expires_at": int(time.time()) + 3600,
             "quote_token": "token-" + job["id"], "input_hash": job["input_hash"],
             "quote": {"points": 30}}
            for job in claimed["jobs"]
        ]
        quoted = self.store.finish_quote(
            USER, batch["id"], claimed["claim_id"], quotes, {"points": 30})
        confirmed = self.store.claim_confirmation(
            USER, batch["id"], "confirm-1", quoted["revision"],
            quoted["quote_expires_at"])
        self.assertEqual(confirmed["status"], "submitting")
        self.assertEqual(len(confirmed["claimed_jobs"]), 1)
        job = confirmed["claimed_jobs"][0]
        self.assertTrue(self.store.finish_submit_claim(
            USER, job["id"], job["revision"], status="submitted", job_id="task-1"))
        polled = self.store.batch(USER, batch["id"], include_private=True)
        job = polled["jobs"][0]
        self.assertEqual(job["job_id"], "task-1")
        self.assertTrue(self.store.finish_task_poll(
            USER, job["id"], job["revision"], status="done",
            result={"url": "https://example.invalid/video.mp4"}))
        final = self.store.batch(USER, batch["id"], include_private=True)
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["jobs"][0]["result"]["url"],
                         "https://example.invalid/video.mp4")
        self.assertFalse(self.store.finish_task_poll(
            USER, job["id"], job["revision"], status="done"))

    def test_replace_batch_plans_and_revision(self):
        self.store.ensure_workspace(USER, PROJECT)
        batch = self.store.create_batch(USER, PROJECT, "主题", "目标", plans("douyin"))
        revised = self.store.replace_batch_plans(
            USER, batch["id"], plans("douyin"), batch["revision"],
            mutation_message_id=11)
        self.assertEqual(revised["status"], "ready")
        self.assertEqual(revised["revision"], batch["revision"] + 1)
        # 同一条修改消息重放：直接返回，不再改动
        replay = self.store.replace_batch_plans(
            USER, batch["id"], plans("douyin"), batch["revision"],
            mutation_message_id=11)
        self.assertEqual(replay["revision"], revised["revision"])
        self.assertEqual(self.store.batch_for_mutation_message(
            USER, PROJECT, 11)["id"], batch["id"])
        with self.assertRaises(StateConflict):
            self.store.replace_batch_plans(USER, batch["id"], plans("douyin"),
                                           batch["revision"], mutation_message_id=12)

    def test_profile_opening_and_turn(self):
        self.store.ensure_workspace(USER, PROJECT)
        self.store.update_workspace(USER, PROJECT, profile_state={"revision": 1})
        request, created = self.store.add_message(
            USER, PROJECT, "user", "我是企业AI顾问", request_id="pg-profile-1",
            request_hash="h1")
        self.assertTrue(created)
        turn = self.store.commit_profile_turn(
            USER, PROJECT, request["id"], {"revision": 2, "phase": "ready"}, 1,
            "你的目标客户是谁？", {"kind": "profile_question"},
            profile={"nickname": "小雀"})
        self.assertEqual(turn["reply"], "你的目标客户是谁？")
        workspace = self.store.workspace(USER, PROJECT)
        self.assertEqual(workspace["profile_state"], {"revision": 2, "phase": "ready"})
        self.assertEqual(workspace["profile"], {"nickname": "小雀"})
        asked = self.store.messages(USER, PROJECT)[0]
        self.assertEqual(asked["public"]["turn"]["assistant_message_id"],
                         turn["assistant_message_id"])
        with self.assertRaises(StateConflict):
            self.store.commit_profile_turn(
                USER, PROJECT, request["id"], {"revision": 3, "phase": "ready"}, 1,
                "重复提交", {})

        opening = self.store.commit_profile_opening(
            USER, PROJECT, {"revision": 3}, "补充问题", {"kind": "profile_question"},
            "profile-open-1", flow={"mode": "profile_interview"})
        self.assertEqual(opening["flow"], {"mode": "profile_interview"})
        self.assertEqual(opening["profile_state"], {"revision": 3})
        before = len(self.store.messages(USER, PROJECT))
        self.store.commit_profile_opening(
            USER, PROJECT, {"revision": 4}, "又一遍", {"kind": "profile_question"},
            "profile-open-1")
        self.assertEqual(len(self.store.messages(USER, PROJECT)), before)

    def test_message_maintenance(self):
        self.store.ensure_workspace(USER, PROJECT)
        answered, _ = self.store.add_message(USER, PROJECT, "user", "第一个问题")
        # 「已应答」的判据是 public 里带 turn/response（与 SQLite 同口径）
        self.store.update_message_public(
            USER, answered["id"], {"turn": {"reply": "回答"}})
        self.assertEqual(self.store.messages(USER, PROJECT)[0]["public"]["turn"],
                         {"reply": "回答"})
        self.assertFalse(self.store.delete_message_if_unanswered(USER, answered["id"]))
        pending, _ = self.store.add_message(USER, PROJECT, "user", "第二个问题")
        self.store.update_message_public(USER, pending["id"], {"kind": "pending"})
        self.assertTrue(self.store.delete_message_if_unanswered(USER, pending["id"]))
        self.assertEqual([item["id"] for item in self.store.messages(USER, PROJECT)],
                         [answered["id"]])
        with self.assertRaises(StoreError):
            self.store.update_message_public(USER, 10 ** 9, {"kind": "x"})

    def test_recovery_and_manual_updates(self):
        self.store.ensure_workspace(USER, PROJECT)
        batch = self.store.create_batch(USER, PROJECT, "主题", "目标", plans("douyin"))
        claimed = self.store.claim_quote(USER, batch["id"], batch["revision"])
        quotes = [
            {"id": job["id"], "cost": 30, "expires_at": int(time.time()) + 3600,
             "quote_token": "token-" + job["id"], "input_hash": job["input_hash"],
             "quote": {"points": 30}}
            for job in claimed["jobs"]
        ]
        quoted = self.store.finish_quote(
            USER, batch["id"], claimed["claim_id"], quotes, {"points": 30})
        confirmed = self.store.claim_confirmation(
            USER, batch["id"], "confirm-9", quoted["revision"],
            quoted["quote_expires_at"])
        job = confirmed["claimed_jobs"][0]
        # 提交结果不可判定：重启后按确认单抢回，继续轮询
        self.store.finish_submit_claim(
            USER, job["id"], job["revision"], status="submission_unknown")
        recovered = self.store.claim_recovery(USER, batch["id"])
        self.assertEqual([item["id"] for item in recovered], [job["id"]])
        self.assertEqual(recovered[0]["status"], "submit_claimed")

        updated = self.store.update_job(USER, job["id"], error="上游超时")
        self.assertEqual(updated["error"], "上游超时")
        with self.assertRaises(StoreError):
            self.store.update_job(USER, job["id"], no_such_field="x")
        revised = self.store.update_batch(USER, batch["id"], topic="改过的主题",
                                          plans=plans("douyin"))
        self.assertEqual(revised["topic"], "改过的主题")
        with self.assertRaises(StoreError):
            self.store.update_batch(USER, batch["id"], no_such_field="x")
        recomputed = self.store.recompute_batch(USER, batch["id"])
        self.assertEqual(recomputed["status"], "running")

        plain = self.store.create_batch(USER, PROJECT, "另一批", "目标",
                                        plans("xiaohongshu"))
        self.assertEqual(self.store.claim_recovery(USER, plain["id"]), [])

    def test_health_and_usage_ledger_over_postgres(self):
        self.assertTrue(self.store.health())
        guard = ModelUsageGuard(
            self.store.db, window_seconds=60, user_window_requests=5,
            ip_window_requests=5, user_concurrency=2, global_concurrency=8,
            user_daily_requests=1, global_daily_requests=10,
            user_daily_tokens=100000, global_daily_tokens=1000000,
            user_daily_cost_micro_usd=1000000, global_daily_cost_micro_usd=10000000,
        )
        self.assertTrue(guard.health())
        lease = guard.acquire(USER, "127.0.0.1", "profile_question", 1000, 2400)
        lease.finish(True)
        with pg_store._connection() as conn:
            stored = conn.execute(
                "SELECT COUNT(*) AS n FROM agent.creator_model_calls WHERE username=%s",
                (USER,)).fetchone()["n"]
        self.assertEqual(stored, 1)
        # 日额度已用 1 次，第二次必须被账本挡住（证明限流读的是 PostgreSQL）
        with self.assertRaises(ModelUsageError):
            guard.acquire(USER, "127.0.0.1", "profile_question", 1000, 2400)

    def test_usage_adapter_fails_loud_on_unknown_statement(self):
        with closing(self.store.db()) as connection:
            connection.execute("BEGIN IMMEDIATE")  # 翻译成表级排他锁
            connection.rollback()
            with self.assertRaises(RuntimeError):
                connection.execute("PRAGMA journal_mode=WAL")


if __name__ == "__main__":
    unittest.main()
