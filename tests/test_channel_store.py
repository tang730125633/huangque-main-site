# 渠道存储层测试：SQLite 模式行为不变 + PostgreSQL 模式（routing schema）全链路。
import base64
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from content_domains import channel_lifecycle, channel_manager, channel_store  # noqa: E402

PG_URL = os.environ.get("HQ_DATABASE_URL")

_backfill_spec = importlib.util.spec_from_file_location(
    "migrate_routing_channels", str(ROOT / "scripts" / "migrate_routing_channels.py"))
backfill = importlib.util.module_from_spec(_backfill_spec)
_backfill_spec.loader.exec_module(backfill)


class _ChannelFixture(unittest.TestCase):
    """两条路径共用的夹具：临时 SQLite 文件、保险箱主密钥、测试渠道定义。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "channels.db"
        self.obs_path = Path(self.tmp.name) / "observability.db"
        self.job_db_path = Path(self.tmp.name) / "jobs.db"
        self.env = patch.dict(os.environ, {
            "HQ_CHANNEL_DB": str(self.db_path),
            "HQ_OBSERVABILITY_DB": str(self.obs_path),
            "CONTENT_JOB_DB": str(self.job_db_path),
            "HQ_PROVIDER_KEYS_MASTER_KEY": base64.urlsafe_b64encode(b"a" * 32).decode(),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        # 删除渠道前会核对任务库（active_jobs 读 jobs 表），夹具必须提供它
        with closing(sqlite3.connect(str(self.job_db_path))) as connection:
            connection.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, payload TEXT, "
                               "status TEXT DEFAULT 'pending')")
            connection.commit()
        os.environ.pop("HQ_CHANNEL_STORE", None)
        self.cid = "m3c-" + uuid.uuid4().hex[:8]
        self.job_id = "m3c-job-" + uuid.uuid4().hex[:8]
        self.body = dict(id=self.cid, name="测试渠道", adapter="openai_image", model="test-model",
                         base_url="http://127.0.0.1:9999/v1", secret="private-secret", enabled=True,
                         fixture={"prompt": "test"}, daily_limit=2, test_cost=1, daily_budget=2)

    def tearDown(self):
        channel_store.close_pool()

    def sqlite_state(self):
        """SQLite 文件的字节快照：用于证明 PG 模式下旧库不再被触碰。"""
        if not self.db_path.exists():
            return None
        return (self.db_path.stat().st_mtime_ns, self.db_path.read_bytes())


class SqliteModeTest(_ChannelFixture):
    """默认 sqlite 模式：与迁移前行为逐项一致，分发不生效。"""

    def test_default_mode_is_sqlite(self):
        self.assertEqual(channel_store.mode(), "sqlite")
        self.assertFalse(channel_store.enabled())
        saved = channel_manager.save("admin", self.body)
        self.assertEqual(saved["id"], self.cid)
        self.assertEqual(channel_manager.version(self.cid)["model"], "test-model")

    def test_invalid_store_value_raises(self):
        with patch.dict(os.environ, {"HQ_CHANNEL_STORE": "mysql"}):
            with self.assertRaises(RuntimeError):
                channel_store.mode()
            with self.assertRaises(RuntimeError):
                channel_manager.version(self.cid)

    def test_switched_process_never_touches_sqlite(self):
        """切到 postgres 后：db() 停用、公开 API 不再读写旧库、缺 URL 立即报错。"""
        channel_manager.save("admin", self.body)
        before = self.sqlite_state()
        with patch.dict(os.environ, {"HQ_CHANNEL_STORE": "postgres"}):
            os.environ.pop("HQ_DATABASE_URL", None)
            channel_store.close_pool()
            with self.assertRaises(RuntimeError):
                channel_manager.db()
            with self.assertRaises(RuntimeError):
                channel_manager.version(self.cid)
            with self.assertRaises(RuntimeError):
                channel_manager.overview()
            with self.assertRaises(RuntimeError):
                channel_lifecycle.mutate("admin", {"id": self.cid, "action": "disable",
                                                   "reason": "测试", "version": 1})
        self.assertEqual(self.sqlite_state(), before)
        self.assertEqual(channel_manager.version(self.cid)["version"], 1)

    def test_save_version_overview_and_secret(self):
        saved = channel_manager.save("admin", self.body)
        self.assertEqual(saved, {"id": self.cid, "version": 1})
        self.assertNotIn(b"private-secret", self.db_path.read_bytes())
        self.assertEqual(channel_manager.version(self.cid, 1, True)["secret"], "private-secret")
        changed = channel_manager.save("admin", dict(self.body, version=1, model="test-model-2"))
        self.assertEqual(changed["version"], 2)
        with self.assertRaises(ValueError):
            channel_manager.save("admin", dict(self.body, version=1, model="test-model-3"))
        overview = channel_manager.overview()
        item = next(ch for ch in overview["items"] if ch["id"] == self.cid)
        self.assertTrue(item["configured"])
        self.assertNotIn("private-secret", json.dumps(overview))

    def test_lifecycle_mapping_and_legacy_controls(self):
        channel_manager.save("admin", self.body)
        mapping = channel_manager.save_mapping("admin", dict(
            kind="image", front="front-model", channel=self.cid, enabled=True))
        self.assertTrue(mapping["enabled"])
        disabled = channel_lifecycle.mutate("admin", {"id": self.cid, "action": "disable",
                                                      "reason": "例行停用", "version": 1})
        self.assertFalse(disabled["enabled"])
        with self.assertRaises(ValueError):
            channel_lifecycle.mutate("admin", {"id": self.cid, "action": "delete",
                                               "reason": "仍需被映射引用", "version": 2})
        self.assertEqual(channel_lifecycle.unmap("admin", dict(
            selector="image:front-model", expected=mapping)), {"ok": True})
        deleted = channel_lifecycle.mutate("admin", {"id": self.cid, "action": "delete",
                                                     "reason": "下线旧渠道", "version": 2})
        self.assertTrue(deleted["deleted"])
        restored = channel_lifecycle.mutate("admin", {"id": self.cid, "action": "restore",
                                                      "reason": "恢复继续使用", "version": 3})
        self.assertFalse(restored["deleted"])
        state = channel_lifecycle.mutate_legacy("admin", {"id": "xai", "action": "disable",
                                                          "reason": "上游波动暂停", "version": 0})
        self.assertFalse(state["enabled"])
        self.assertFalse(channel_lifecycle.legacy_states()["xai"]["enabled"])
        with self.assertRaises(ValueError):
            channel_lifecycle.require_legacy("xiaole_video", {"channel": "grok"})

    def test_reserve_finish_and_task_evidence(self):
        channel_manager.save("admin", self.body)
        rid = channel_manager.reserve(self.cid, "task", self.job_id)
        channel_manager.finish(rid, "passed", "成品核验通过", provider_id="prov-1")
        evidence = channel_manager.task_evidence(self.job_id)
        self.assertEqual(evidence["state"], "passed")
        self.assertEqual(evidence["channel"], self.cid)
        self.assertEqual(channel_manager.task_recovery_state(self.job_id), "passed")
        self.assertEqual(channel_manager.mark_interrupted_task_unknown("m3c-missing", "x"), "absent")
        self.assertEqual(channel_manager.search_task_ids("prov-1"), {self.job_id})
        with self.assertRaises(ValueError):
            channel_manager.reserve(self.cid, "task", self.job_id)


class BackfillReaderTest(_ChannelFixture):
    """回填器读取与比对口径（不需要 PostgreSQL）。"""

    def test_reads_all_eleven_tables_and_is_deterministic(self):
        channel_manager.save("admin", self.body)
        channel_manager.save_mapping("admin", dict(kind="image", front="front-model",
                                                   channel=self.cid, enabled=True))
        rid = channel_manager.reserve(self.cid, "task", self.job_id)
        channel_manager.finish(rid, "passed", "成品核验通过")
        channel_lifecycle.mutate_legacy("admin", {"id": "xai", "action": "disable",
                                                 "reason": "上游波动暂停", "version": 0})
        data = backfill.read_source(self.db_path)
        self.assertEqual(set(data), {spec["table"] for spec in backfill.TABLES})
        self.assertEqual(len(data["channels"]), 1)
        self.assertEqual(len(data["versions"]), 1)
        self.assertEqual(len(data["mappings"]), 1)
        self.assertEqual(len(data["runs"]), 1)
        self.assertEqual(len(data["settings"]), 1)
        self.assertGreaterEqual(len(data["events"]), 3)
        # 密钥密文随行回填（明文只在保险箱里），报告里只出现计数与校验和
        self.assertTrue(data["versions"][self.cid + ":1"]["secret"])
        first = backfill.summary(data)
        second = backfill.summary(backfill.read_source(self.db_path))
        self.assertEqual(first, second)
        self.assertEqual(first["tables"]["schedule"], 1)
        self.assertEqual(first["rows"], sum(first["tables"].values()))
        self.assertEqual(len(first["source_checksum"]), 64)
        self.assertNotIn("private-secret", json.dumps(first))

    def test_missing_column_is_rejected(self):
        broken = Path(self.tmp.name) / "broken.db"
        connection = sqlite3.connect(str(broken))
        connection.execute("CREATE TABLE channels(id TEXT PRIMARY KEY, version INTEGER)")
        connection.commit()
        connection.close()
        with self.assertRaises(RuntimeError):
            backfill.read_source(broken)

    def test_apply_guards_empty_source_and_code_sha(self):
        with closing(channel_manager.db()):  # 建出与源一致的 11 张空表
            pass
        data = backfill.read_source(self.db_path)
        self.assertEqual(backfill.summary(data)["rows"], 0)
        with self.assertRaises(RuntimeError):
            backfill.apply(data, self.db_path, "m3c1234")  # 空源拒绝导入
        channel_manager.save("admin", self.body)
        data = backfill.read_source(self.db_path)
        with self.assertRaises(RuntimeError):
            backfill.apply(data, self.db_path, "")  # --apply 必须带 --code-sha


# 本机没有 PostgreSQL 时，用这个最小替身驱动回填器：它只认回填器用到的语句形态，
# 逐条校验 %s 占位符与参数个数，并保存行数据，从而在本地验证绑定口径、
# 冲突护栏与「读回核对」逻辑（真实 PG 由 PostgresModeTest 在 CI/staging 覆盖）。
_UPSERT = re.compile(r"^INSERT INTO routing\.(\w+)\(([^)]+)\) VALUES\([^)]+\) "
                     r"ON CONFLICT \(([^)]+)\) DO UPDATE SET (.+)$", re.S)
_SELECT = re.compile(r"^SELECT (.+?) FROM routing\.(\w+) WHERE (.+)$", re.S)


class _FakePgResult:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [] if self._row is None else [self._row]

    @property
    def rowcount(self):
        return 1


class _FakePgConnection:
    def __init__(self):
        self.rows = {}          # table -> {key tuple: row dict}
        self.audit = []         # ops.* 语句记录
        self.statements = []    # 执行过的全部语句（离线方言校验用）
        self.column_loss = ()   # (table, column) 模拟写入丢字段

    def execute(self, sql, params=()):
        flat = " ".join(str(sql).split())
        self.statements.append(flat)
        if flat.count("%s") != len(params):
            raise AssertionError("占位符与参数个数不符：%d vs %d: %s"
                                 % (flat.count("%s"), len(params), flat[:120]))
        select = _SELECT.match(flat)
        if select:
            columns, table, where = select.groups()
            keys = tuple(part.split("=")[0].strip() for part in where.split(" AND "))
            wanted = tuple(column.strip() for column in columns.split(","))
            row = self.rows.get(table, {}).get(tuple(params))
            if row is None:
                return _FakePgResult(None)
            return _FakePgResult({column: row.get(column) for column in wanted})
        upsert = _UPSERT.match(flat)
        if upsert:
            table, columns, keys, _ = upsert.groups()
            column_names = [column.strip() for column in columns.split(",")]
            key_names = [key.strip() for key in keys.split(",")]
            row = dict(zip(column_names, params))
            for column in self.column_loss:
                if column[0] == table and column[1] in row:
                    row[column[1]] = None
            key = tuple(row[name] for name in key_names)
            self.rows.setdefault(table, {})[key] = row
            return _FakePgResult(row)
        if flat.startswith("INSERT INTO ops.") or flat.startswith("UPDATE ops."):
            self.audit.append((flat[:40], params))
            return _FakePgResult(None)
        raise AssertionError("替身未覆盖的语句：%s" % flat[:120])


class _FakePgModule:
    def __init__(self):
        self.connection = _FakePgConnection()

    @contextmanager
    def transaction(self):
        yield self.connection


class BackfillApplyTest(_ChannelFixture):
    """回填器写入路径：绑定口径、冲突护栏、读回核对与幂等（替身驱动，无需 PG）。"""

    def setUp(self):
        super().setUp()
        channel_manager.save("admin", self.body)
        channel_manager.save_mapping("admin", dict(kind="image", front="front-model",
                                                   channel=self.cid, enabled=True))
        rid = channel_manager.reserve(self.cid, "task", self.job_id)
        channel_manager.finish(rid, "passed", "成品核验通过")
        self.data = backfill.read_source(self.db_path)
        self.fake = _FakePgModule()
        self.patcher = patch.object(backfill, "postgres", self.fake)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _table_keys(self, table):
        return {":".join(str(row[column]) for column in
                         next(spec["keys"] for spec in backfill.TABLES if spec["table"] == table))
                for row in self.fake.connection.rows.get(table, {}).values()}

    def test_apply_writes_every_table_and_is_idempotent(self):
        expected = backfill.summary(self.data)
        first = backfill.apply(self.data, self.db_path, "m3ctest1")
        self.assertEqual(first["inserted"], expected["rows"])
        self.assertEqual((first["updated"], first["unchanged"]), (0, 0))
        for table, count in expected["tables"].items():
            self.assertEqual(len(self.fake.connection.rows.get(table, {})), count, table)
        second = backfill.apply(backfill.read_source(self.db_path), self.db_path, "m3ctest1")
        self.assertEqual(second["unchanged"], expected["rows"])
        self.assertEqual((second["inserted"], second["updated"]), (0, 0))
        # 审计：domain='routing' 的运行记录 + 逐行 items
        audit = " ".join(sql for sql, _ in self.fake.connection.audit)
        self.assertIn("ops.data_migration_runs", audit)
        self.assertIn("ops.data_migration_items", audit)
        run_insert = next(params for sql, params in self.fake.connection.audit
                          if sql.startswith("INSERT INTO ops.data_migration_runs"))
        self.assertIn("routing", run_insert)

    def test_newer_target_timestamp_aborts_batch(self):
        backfill.apply(self.data, self.db_path, "m3ctest1")
        rid = next(iter(self.fake.connection.rows["runs"]))
        self.fake.connection.rows["runs"][rid]["updated"] += 3600  # PostgreSQL 侧有更新的写入
        with self.assertRaises(RuntimeError) as caught:
            backfill.apply(self.data, self.db_path, "m3ctest1")
        self.assertIn("冲突", str(caught.exception))

    def test_changed_target_row_without_timestamp_aborts_batch(self):
        backfill.apply(self.data, self.db_path, "m3ctest1")
        key = next(iter(self.fake.connection.rows["channels"]))
        self.fake.connection.rows["channels"][key]["enabled"] = False  # 切写后的新状态
        with self.assertRaises(RuntimeError) as caught:
            backfill.apply(self.data, self.db_path, "m3ctest1")
        self.assertIn("目标内容与源不一致", str(caught.exception))

    def test_readback_mismatch_raises(self):
        self.fake.connection.column_loss = (("channels", "version"),)
        with self.assertRaises(RuntimeError) as caught:
            backfill.apply(self.data, self.db_path, "m3ctest1")
        self.assertIn("行不一致", str(caught.exception))

    def test_boolean_and_float_columns_round_trip(self):
        """SQLite 的 0/1 与 REAL 必须按真假/数值比对，不因类型差异误报不一致。"""
        self.fake.connection.column_loss = ()
        backfill.apply(self.data, self.db_path, "m3ctest1")
        channel = next(iter(self.fake.connection.rows["channels"].values()))
        self.assertIs(channel["enabled"], True)
        run = next(iter(self.fake.connection.rows["runs"].values()))
        self.assertIsInstance(run["started"], float)


@unittest.skipUnless(PG_URL, "HQ_DATABASE_URL 未配置：跳过 PostgreSQL 测试")
class PostgresModeTest(_ChannelFixture):
    """postgres 模式：11 张表读写往返、生命周期、回填幂等与 db() fail-closed。"""

    def setUp(self):
        super().setUp()
        # 嵌套 patch：在外层同一批 env 之上再加开关，cleanup 逆序恢复。
        self.pg_env = patch.dict(os.environ, {
            "HQ_CHANNEL_DB": str(self.db_path),
            "HQ_OBSERVABILITY_DB": str(self.obs_path),
            "HQ_PROVIDER_KEYS_MASTER_KEY": base64.urlsafe_b64encode(b"a" * 32).decode(),
            "HQ_CHANNEL_STORE": "postgres",
            "HQ_DATABASE_URL": PG_URL,
        })
        self.pg_env.start()
        self.addCleanup(self.pg_env.stop)
        channel_store.close_pool()
        self.settings_before = self._read_settings()
        self.operation_id, self.operation_before, self.revision_max = self._pick_operation()

    # --- 夹具工具 ---------------------------------------------------------
    def _conn(self):
        return channel_store._pool_instance().connection()

    def _read_settings(self):
        with self._conn() as conn:
            return {row["id"]: row["value"] for row in conn.execute(
                "SELECT id,value FROM routing.settings WHERE id IN (1,2)").fetchall()}

    def _pick_operation(self):
        from content_domains import function_registry
        operation_id = function_registry.operation_catalog(channel_eligible=True)[0]["operation_id"]
        with self._conn() as conn:
            row = conn.execute(
                "SELECT operation_id,revision,state,config,actor,updated "
                "FROM routing.operation_mappings WHERE operation_id=%s", (operation_id,)).fetchone()
            revisions = conn.execute(
                "SELECT revision FROM routing.operation_mapping_versions WHERE operation_id=%s",
                (operation_id,)).fetchall()
        return (operation_id, dict(row) if row else None,
                max((r["revision"] for r in revisions), default=0))

    def _clean(self):
        """删除测试造的行，并把 settings 与功能映射恢复原状。"""
        like = "m3c-%"
        with self._conn() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM routing.run_snapshots WHERE run_id IN "
                             "(SELECT id FROM routing.runs WHERE job_id LIKE %s OR channel LIKE %s)",
                             (like, like))
                conn.execute("DELETE FROM routing.runs WHERE job_id LIKE %s OR channel LIKE %s",
                             (like, like))
                conn.execute("DELETE FROM routing.events WHERE actor LIKE %s OR target LIKE %s",
                             (like, like))
                conn.execute("DELETE FROM routing.schedule WHERE channel LIKE %s", (like,))
                conn.execute("DELETE FROM routing.versions WHERE channel LIKE %s", (like,))
                conn.execute("DELETE FROM routing.channels WHERE id LIKE %s", (like,))
                conn.execute("DELETE FROM routing.mappings WHERE selector LIKE %s", ("m3c-%",))
                conn.execute("DELETE FROM routing.channel_incidents WHERE channel LIKE %s", (like,))
                conn.execute("DELETE FROM routing.operation_mapping_versions "
                             "WHERE operation_id=%s AND revision > %s",
                             (self.operation_id, self.revision_max))
                conn.execute("DELETE FROM routing.operation_mappings WHERE operation_id=%s",
                             (self.operation_id,))
                if self.operation_before is not None:
                    conn.execute(
                        "INSERT INTO routing.operation_mappings"
                        "(operation_id,revision,state,config,actor,updated) VALUES(%s,%s,%s,%s,%s,%s)",
                        (self.operation_before["operation_id"], self.operation_before["revision"],
                         self.operation_before["state"], self.operation_before["config"],
                         self.operation_before["actor"], self.operation_before["updated"]))
                conn.execute("DELETE FROM routing.settings WHERE id IN (1,2)")
                for setting_id, value in self.settings_before.items():
                    conn.execute("INSERT INTO routing.settings(id,value) VALUES(%s,%s)",
                                 (setting_id, value))

    def tearDown(self):
        self._clean()
        channel_store.close_pool()
        super().tearDown()

    # --- 用例 -------------------------------------------------------------
    def test_eleven_tables_exist_in_routing(self):
        expected = {"channels", "versions", "mappings", "operation_mappings",
                    "operation_mapping_versions", "runs", "run_snapshots", "events",
                    "schedule", "settings", "channel_incidents"}
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='routing'"
            ).fetchall()
        self.assertTrue(expected.issubset({row["table_name"] for row in rows}))

    def test_save_version_and_overview_roundtrip(self):
        saved = channel_manager.save("admin", self.body)
        self.assertEqual(saved, {"id": self.cid, "version": 1})
        self.assertEqual(channel_manager.version(self.cid, 1)["model"], "test-model")
        self.assertEqual(channel_manager.version(self.cid, 1, True)["secret"], "private-secret")
        with self._conn() as conn:
            row = conn.execute("SELECT secret FROM routing.versions WHERE channel=%s AND version=1",
                               (self.cid,)).fetchone()
            enabled = conn.execute("SELECT enabled FROM routing.channels WHERE id=%s",
                                   (self.cid,)).fetchone()["enabled"]
        self.assertIsInstance(enabled, bool)
        self.assertTrue(enabled)
        self.assertNotIn("private-secret", row["secret"])
        changed = channel_manager.save("admin", dict(self.body, version=1, model="test-model-2"))
        self.assertEqual(changed["version"], 2)
        with self.assertRaises(ValueError):
            channel_manager.save("admin", dict(self.body, version=1, model="test-model-3"))
        overview = channel_manager.overview()
        item = next(ch for ch in overview["items"] if ch["id"] == self.cid)
        self.assertEqual(item["version"], 2)
        self.assertTrue(item["configured"])
        self.assertEqual(item["stats"]["total"], 0)
        self.assertNotIn("private-secret", json.dumps(overview))

    def test_lifecycle_mapping_and_legacy_controls(self):
        before_legacy = channel_lifecycle.legacy_states()
        channel_manager.save("admin", self.body)
        mapping = channel_manager.save_mapping("admin", dict(
            kind="image", front="m3c-front", channel=self.cid, enabled=True))
        self.assertTrue(mapping["enabled"])
        disabled = channel_lifecycle.mutate("admin", {"id": self.cid, "action": "disable",
                                                      "reason": "例行停用", "version": 1})
        self.assertFalse(disabled["enabled"])
        state = channel_lifecycle.mutate_legacy("m3c-test", {"id": "xai", "action": "disable",
                                                            "reason": "上游波动暂停", "version": 0})
        self.assertFalse(state["enabled"])
        after_legacy = channel_lifecycle.legacy_states()
        self.assertEqual(after_legacy["xai"]["revision"], 1)
        self.assertEqual({k for k in after_legacy if k != "xai"}, {k for k in before_legacy if k != "xai"})
        with self.assertRaises(ValueError):
            channel_lifecycle.require_legacy("xiaole_video", {"channel": "grok"})
        self.assertEqual(channel_lifecycle.unmap("admin", dict(
            selector="image:m3c-front", expected=mapping)), {"ok": True})
        with self.assertRaises(ValueError):
            channel_lifecycle.unmap("admin", dict(selector="image:m3c-front", expected=mapping))
        deleted = channel_lifecycle.mutate("admin", {"id": self.cid, "action": "delete",
                                                     "reason": "下线旧渠道", "version": 2})
        self.assertTrue(deleted["deleted"])

    def test_reserve_finish_and_recovery_states(self):
        channel_manager.save("admin", self.body)
        rid = channel_manager.reserve(self.cid, "task", self.job_id)
        channel_manager.finish(rid, "running", "提交供应商")
        channel_manager.finish(rid, "passed", "成品核验通过", provider_id="prov-m3c")
        evidence = channel_manager.task_evidence(self.job_id)
        self.assertEqual(evidence["state"], "passed")
        self.assertEqual(evidence["channel"], self.cid)
        self.assertEqual(channel_manager.task_recovery_state(self.job_id), "passed")
        self.assertEqual(channel_manager.search_task_ids("prov-m3c"), {self.job_id})
        other = "m3c-running-" + uuid.uuid4().hex[:8]
        rid2 = channel_manager.reserve(self.cid, "task", other)
        channel_manager.finish(rid2, "running", "提交供应商")
        self.assertEqual(channel_manager.mark_interrupted_task_unknown(other, "worker 中断"), "unknown")
        with self._conn() as conn:
            state = conn.execute("SELECT state FROM routing.runs WHERE id=%s", (rid2,)).fetchone()["state"]
        self.assertEqual(state, "unknown")
        with self.assertRaises(ValueError):
            channel_manager.reserve(self.cid, "task", self.job_id)

    def test_operation_mapping_publish_and_rollback(self):
        published = channel_manager.save_operation_mapping("m3c-test", dict(
            operation_id=self.operation_id, state="legacy"))
        self.assertEqual(published["state"], "legacy")
        self.assertEqual(published["operation_id"], self.operation_id)
        revision = published["revision"]
        self.assertEqual(channel_manager.operation_mapping(self.operation_id, revision)["revision"],
                         revision)
        with self.assertRaises(ValueError):
            channel_manager.save_operation_mapping("m3c-test", dict(
                operation_id=self.operation_id, state="legacy", expected_revision=revision - 1))
        rolled = channel_manager.rollback_operation_mapping("m3c-test", dict(
            operation_id=self.operation_id, target_revision=revision,
            expected_revision=revision))
        self.assertEqual(rolled["revision"], revision + 1)
        self.assertEqual(revision, self.revision_max + 1)

    def test_notification_settings_roundtrip(self):
        settings = channel_manager.notification_settings()
        self.assertIn("enabled", settings)
        self.assertIsInstance(settings["delivery"], dict)
        saved = channel_manager.save_notifications("m3c-test", dict(
            enabled=True, endpoint="http://127.0.0.1:7777/hook"))
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["endpoint"], "已配置（隐藏）")
        self.assertEqual(channel_manager.notification_settings(True)["endpoint"],
                         "http://127.0.0.1:7777/hook")
        with self._conn() as conn:
            stored = conn.execute("SELECT value FROM routing.settings WHERE id=1").fetchone()["value"]
        self.assertNotIn("http://127.0.0.1:7777/hook", stored)
        with self.assertRaises(ValueError):
            channel_manager.save_notifications("m3c-test", dict(enabled=True, endpoint="http://x"))

    def test_backfill_is_idempotent_against_postgres(self):
        with patch.dict(os.environ, {"HQ_CHANNEL_STORE": "sqlite"}):
            channel_manager.save("admin", self.body)  # 源侧数据（SQLite 路径）
            data = backfill.read_source(self.db_path)
        os.environ["HQ_CHANNEL_STORE"] = "postgres"
        first = backfill.apply(data, self.db_path, "m3ctest1")
        self.assertEqual(first["inserted"], first["rows"])
        self.assertEqual(first["updated"], 0)
        second = backfill.apply(backfill.read_source(self.db_path), self.db_path, "m3ctest1")
        self.assertEqual(second["unchanged"], second["rows"])
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(channel_manager.version(self.cid, 1)["model"], "test-model")
        backfill.postgres.close_pool()  # 回填器用的池，测试结束即关

    def test_task_failover_bookkeeping_matches_sqlite_semantics(self):
        """PG 路径：未提交（排队中）的任务也能安全切换，且一个任务只保留一条运行记录。"""
        channel_manager.save("admin", self.body)
        backup_id = "m3c-bk-" + uuid.uuid4().hex[:8]
        channel_manager.save("admin", dict(self.body, id=backup_id, name="备用图片渠道"))
        candidates = [
            {"id": self.cid, "version": 1, "adapter": "openai_image", "model": "test-model"},
            {"id": backup_id, "version": 1, "adapter": "openai_image", "model": "test-model"},
        ]
        binding = dict(
            operation_id="image.xiaole.text", mapping_revision=1, **candidates[0],
            front="", invocation_source="web", route_order=[self.cid, backup_id],
            route_attempt=1, route_candidates=candidates)
        rid = channel_manager.reserve(self.cid, "task", self.job_id,
                                      channel_manager.version(self.cid),
                                      execution_snapshot=binding)
        # 并发/限流排队超时的任务从未提交供应商：queued 也必须允许记账，否则任务卡死在排队。
        channel_manager.finish_task_failover_safe(
            rid, "等待执行：渠道并发或限流等待超时，尚未提交供应商")
        snapshot = channel_manager.prepare_task_failover(
            rid, candidates[1], "渠道并发或限流等待超时，尚未提交供应商")
        self.assertEqual(backup_id, snapshot["id"])
        self.assertEqual(2, snapshot["route_attempt"])
        self.assertEqual(self.cid, snapshot["attempts"][0]["channel"])
        self.assertEqual("failed", snapshot["attempts"][0]["state"])
        evidence = channel_manager.task_evidence(self.job_id)
        self.assertEqual(backup_id, evidence["channel"])
        self.assertEqual("queued", evidence["state"])
        with self._conn() as conn:
            rows = conn.execute("SELECT id,state,detail FROM routing.runs WHERE job_id=%s",
                                (self.job_id,)).fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual("queued", rows[0]["state"])
        self.assertIn("安全切换到下一渠道", rows[0]["detail"])
        # 没有新的失败就不允许再次切换（防止同一个失败被重复消费）
        with self.assertRaises(ValueError):
            channel_manager.prepare_task_failover(rid, candidates[0], "重复切换")

    def test_db_is_fail_closed_when_switched(self):
        with self.assertRaises(RuntimeError):
            channel_manager.db()


if __name__ == "__main__":
    unittest.main()
