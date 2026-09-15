# 渠道执行器与参数模块的存储分发测试：SQLite 模式行为不变 + PostgreSQL 模式（routing schema）。
import base64
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from content_domains import channel_manager as cm  # noqa: E402
from content_domains import channel_parameters as params  # noqa: E402
from content_domains import channel_runtime as runtime  # noqa: E402
from content_domains import channel_store  # noqa: E402

PG_URL = os.environ.get("HQ_DATABASE_URL")


class _RuntimeFixture(unittest.TestCase):
    """两条路径共用夹具：临时渠道库、保险箱主密钥、一个已保存的测试渠道。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "channels.db"
        self.job_db_path = Path(self.tmp.name) / "jobs.db"
        self.env = patch.dict(os.environ, {
            "HQ_CHANNEL_DB": str(self.db_path),
            "HQ_OBSERVABILITY_DB": str(Path(self.tmp.name) / "trace.db"),
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
        self.body = dict(name="测试渠道", adapter="openai_image", model="test-model",
                         base_url="http://127.0.0.1:9999/v1", secret="private-secret", enabled=True,
                         fixture={"prompt": "test"}, daily_limit=2, test_cost=1, daily_budget=2)
        self.ch = cm.save("admin", self.body)
        self.active_cid = self.ch["id"]

    def tearDown(self):
        channel_store.close_pool()

    def sqlite_state(self):
        """SQLite 文件的字节快照：用于证明 PG 模式下旧库不再被触碰。"""
        if not self.db_path.exists():
            return None
        return (self.db_path.stat().st_mtime_ns, self.db_path.read_bytes())

    def sqlite_rows(self, sql, args=()):
        with closing(cm.db()) as c:
            return [dict(row) for row in c.execute(sql, args)]

    def spec(self):
        """一份可发布的最小参数协议（与后台参数页同口径）。"""
        cfg = cm.version(self.active_cid)
        cap = params.capabilities(cfg)
        values = {k: v[0] for k, v in cap["fields"].items()}
        return dict(profile=cap["profile"],
                    fields=[dict(key=k, label=params.LABELS[k], visible=True) for k in cap["fields"]],
                    combinations=[dict(id="first", values=values, points=20)],
                    default="first", reference_min=cap["reference_min"],
                    reference_max=cap["reference_max"])

    def draft(self):
        state = params.admin_state(self.active_cid)
        return params.change("admin", dict(
            id=self.active_cid, version=state["version"],
            draft_revision=(state["draft"] or {}).get("revision", 0),
            action="draft", parameters=self.spec()))

    def publish(self):
        state = self.draft()
        return params.change("admin", dict(
            id=self.active_cid, version=state["version"],
            draft_revision=state["draft"]["revision"], action="publish", confirmed=True))

    def public_payload(self, front):
        """前台按公开参数目录提交的载荷（报价与捕获都按它的 revision 校验）。"""
        item = next(x for x in params.public_catalog()["items"] if x["front"] == front)
        return dict(prompt="test", model=front,
                    parameter_selection={"revision": item["revision"],
                                         "combination": item["default"]})


class SqliteModeTest(_RuntimeFixture):
    """默认 sqlite 模式：执行器、调度器、参数与报价与迁移前逐项一致。"""

    def test_executor_reads_runs_and_stops_for_claimed_run(self):
        """execute 的入口读与「已被认领」判定走 SQLite runs 表。"""
        rid = cm.reserve(self.ch["id"], "full")
        cm.finish(rid, "running", "provider executing")
        with patch.object(runtime, "generate") as generate:
            self.assertIsNone(runtime.execute(rid))
        self.assertFalse(generate.called)
        states = self.sqlite_rows("SELECT state FROM runs WHERE id=?", (rid,))
        self.assertEqual(states[0]["state"], "running")

    def test_terminated_state_is_written_to_sqlite(self):
        rid = cm.reserve(self.ch["id"], "full")
        self.assertTrue(runtime._mark_terminated(rid))
        self.assertFalse(runtime._mark_terminated(rid))  # 终态不覆盖
        rows = self.sqlite_rows("SELECT state,detail FROM runs WHERE id=?", (rid,))
        self.assertEqual(rows[0]["state"], "terminated")
        self.assertEqual(rows[0]["detail"], "管理员终止任务")

    def test_notify_writes_one_deduplicated_incident(self):
        rid = cm.reserve(self.ch["id"], "task", "m3c-job-1")
        row = {"id": rid, "channel": self.ch["id"], "kind": "task", "started": 1}
        runtime._notify(row, "failed")
        runtime._notify(row, "failed")  # 状态未变：不重复入账、不重复投递
        incidents = self.sqlite_rows(
            "SELECT state,action FROM channel_incidents WHERE channel=? AND kind=?",
            (self.ch["id"], "task"))
        self.assertEqual(len(incidents), 1)
        self.assertEqual((incidents[0]["state"], incidents[0]["action"]),
                         ("failed", "channel.failed"))
        runtime._notify(row, "passed")
        recovered = self.sqlite_rows(
            "SELECT state,action FROM channel_incidents WHERE channel=? AND kind=?",
            (self.ch["id"], "task"))
        self.assertEqual((recovered[0]["state"], recovered[0]["action"]),
                         ("passed", "channel.recovered"))
        self.assertEqual(runtime._notify(row, "passed"), None)  # 非告警状态直接返回

    def test_scheduler_claims_due_checks_once(self):
        cm.save("admin", dict(self.body, **self.ch, monitor=True, daily_test=True))
        with closing(cm.db()) as c:
            c.execute("UPDATE schedule SET light_due=0,full_due=0")
            c.commit()
        with patch.object(runtime, "start_test") as start:
            runtime.monitor_cycle()
            self.assertEqual(start.call_count, 2)
            runtime.monitor_cycle()  # 已推进到下次，不再重复派发
            self.assertEqual(start.call_count, 2)
        schedule = self.sqlite_rows("SELECT light_due,full_due FROM schedule WHERE channel=?",
                                    (self.ch["id"],))[0]
        self.assertGreater(schedule["light_due"], time.time() - 60)
        self.assertGreater(schedule["full_due"], time.time() - 60)

    def test_layout_and_parameter_draft_use_sqlite_settings(self):
        self.assertEqual(params.layout_state()["image"]["default"], "gpt")
        saved = params.layout_save("admin", {"layout": params.layout_state()})
        self.assertEqual(saved["layout"]["video"]["default"], "grok")
        layout_row = self.sqlite_rows("SELECT value FROM settings WHERE id=4")
        self.assertEqual(json.loads(layout_row[0]["value"])["video"]["default"], "grok")

        self.assertIsNone(params.admin_state(self.active_cid)["draft"])
        state = self.draft()
        self.assertEqual(state["draft"]["revision"], 1)
        drafts = json.loads(self.sqlite_rows("SELECT value FROM settings WHERE id=3")[0]["value"])
        self.assertEqual(drafts[self.active_cid]["parameters"]["default"], "first")

    def test_publish_and_quote_read_sqlite_versions_and_mappings(self):
        cm.save_mapping("admin", dict(kind="image", front="front-model",
                                      channel=self.ch["id"], enabled=True))
        self.assertIsNone(params.quote("image", dict(prompt="test", model="front-model")))
        published = self.publish()
        self.assertEqual(published["version"], 2)
        self.assertEqual(published["published"]["combinations"][0]["points"], 20)
        self.assertEqual(len(published["history"]), 1)
        self.assertEqual(params.quote("image", self.public_payload("front-model")), 20)
        self.assertEqual(params.public_catalog()["items"][0]["front"], "front-model")

    def test_switched_process_fails_closed_without_touching_sqlite(self):
        before = self.sqlite_state()
        with patch.dict(os.environ, {"HQ_CHANNEL_STORE": "postgres"}):
            os.environ.pop("HQ_DATABASE_URL", None)
            channel_store.close_pool()
            with self.assertRaises(RuntimeError):
                runtime._mark_terminated("m3c-missing")
            with self.assertRaises(RuntimeError):
                runtime.monitor_cycle()
            with self.assertRaises(RuntimeError):
                params.layout_state()
            with self.assertRaises(RuntimeError):
                params.admin_state(self.ch["id"])
            with self.assertRaises(RuntimeError):
                params.public_catalog()
            with self.assertRaises(RuntimeError):
                cm.db()
        self.assertEqual(self.sqlite_state(), before)


@unittest.skipUnless(PG_URL, "HQ_DATABASE_URL 未配置：跳过 PostgreSQL 测试")
class PostgresModeTest(_RuntimeFixture):
    """postgres 模式：执行器、调度器与参数发布走 routing schema，且不再碰旧库。"""

    def setUp(self):
        super().setUp()
        self.pg_env = patch.dict(os.environ, {
            "HQ_CHANNEL_DB": str(self.db_path),
            "HQ_PROVIDER_KEYS_MASTER_KEY": base64.urlsafe_b64encode(b"a" * 32).decode(),
            "HQ_CHANNEL_STORE": "postgres",
            "HQ_DATABASE_URL": PG_URL,
        })
        self.pg_env.start()
        self.addCleanup(self.pg_env.stop)
        channel_store.close_pool()
        self.cid = "m3c-rt-" + uuid.uuid4().hex[:8]
        self.settings_before = self._read_settings()
        # 切换后的权威是 PostgreSQL：在 PG 侧重新存一份测试渠道
        self.pg_channel = cm.save("admin", dict(self.body, id=self.cid))
        self.active_cid = self.cid

    def tearDown(self):
        self._clean()
        channel_store.close_pool()
        super().tearDown()

    def _conn(self):
        return channel_store._pool_instance().connection()

    def _read_settings(self):
        with self._conn() as conn:
            return {row["id"]: row["value"] for row in conn.execute(
                "SELECT id,value FROM routing.settings WHERE id IN (3,4)").fetchall()}

    def _clean(self):
        like = "m3c-%"
        with self._conn() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM routing.run_snapshots WHERE run_id IN "
                             "(SELECT id FROM routing.runs WHERE job_id LIKE %s OR channel LIKE %s)",
                             (like, like))
                conn.execute("DELETE FROM routing.runs WHERE job_id LIKE %s OR channel LIKE %s",
                             (like, like))
                conn.execute("DELETE FROM routing.events WHERE target LIKE %s", (like,))
                conn.execute("DELETE FROM routing.schedule WHERE channel LIKE %s", (like,))
                conn.execute("DELETE FROM routing.versions WHERE channel LIKE %s", (like,))
                conn.execute("DELETE FROM routing.channels WHERE id LIKE %s", (like,))
                # 选择器形如 kind:front，测试前置用 m3c- 前缀，因此按 '%m3c-%' 收尾
                conn.execute("DELETE FROM routing.mappings WHERE selector LIKE %s", ("%" + like,))
                conn.execute("DELETE FROM routing.channel_incidents WHERE channel LIKE %s", (like,))
                conn.execute("DELETE FROM routing.settings WHERE id IN (3,4)")
                for setting_id, value in self.settings_before.items():
                    conn.execute("INSERT INTO routing.settings(id,value) VALUES(%s,%s)",
                                 (setting_id, value))

    def _other_schedules(self):
        """别人的排期行快照：poll_schedule 会推进所有已到期渠道，用完照原样放回。"""
        with self._conn() as conn:
            return [(row["channel"], row["light_due"], row["full_due"]) for row in conn.execute(
                "SELECT channel,light_due,full_due FROM routing.schedule WHERE channel NOT LIKE %s",
                ("m3c-%",)).fetchall()]

    def _restore_schedules(self, snapshot):
        with self._conn() as conn:
            with conn.transaction():
                for channel, light_due, full_due in snapshot:
                    conn.execute("UPDATE routing.schedule SET light_due=%s,full_due=%s "
                                 "WHERE channel=%s", (light_due, full_due, channel))

    # --- 用例 -------------------------------------------------------------

    def test_executor_gate_and_termination_use_postgres(self):
        rid = cm.reserve(self.cid, "full")
        cfg = cm.version(self.cid)
        self.assertEqual(channel_store.try_start_run(rid, cfg, time.time() - 2400), True)
        with self._conn() as conn:
            state = conn.execute("SELECT state FROM routing.runs WHERE id=%s", (rid,)).fetchone()["state"]
            rate = conn.execute("SELECT COUNT(*) AS n FROM routing.events WHERE target=%s "
                                "AND action='runtime.dispatch'", (self.cid,)).fetchone()["n"]
        self.assertEqual(state, "running")
        self.assertEqual(rate, 1)
        self.assertEqual(channel_store.try_start_run(rid, cfg, time.time() - 2400), False)
        self.assertEqual(channel_store.execution_phase(rid), "等待执行")
        self.assertTrue(channel_store.mark_terminated(rid, "管理员终止任务"))
        self.assertFalse(channel_store.mark_terminated(rid, "管理员终止任务"))
        with self._conn() as conn:
            row = conn.execute("SELECT state,detail FROM routing.runs WHERE id=%s", (rid,)).fetchone()
        self.assertEqual((row["state"], row["detail"]), ("terminated", "管理员终止任务"))
        self.assertIsNone(channel_store.run_record("m3c-missing-run"))

    def test_notify_schedule_and_audit_use_postgres(self):
        rid = cm.reserve(self.cid, "connection")
        action, occurred = channel_store.note_incident(self.cid, "connection", "failed")
        self.assertEqual(action, "channel.failed")
        again, occurred_again = channel_store.note_incident(self.cid, "connection", "failed")
        self.assertEqual((again, occurred_again), (action, occurred))
        recovered, _ = channel_store.note_incident(self.cid, "connection", "passed")
        self.assertEqual(recovered, "channel.recovered")
        mine = [(item["channel"], item["action"])
                for item in channel_store.pending_incidents() if item["channel"] == self.cid]
        self.assertEqual(mine, [(self.cid, "channel.recovered")])
        self.assertIn(rid, channel_store.queued_test_run_ids(1000))

        cm.save("admin", dict(self.body, **self.pg_channel, monitor=True, daily_test=True))
        with self._conn() as conn:
            with conn.transaction():
                conn.execute("UPDATE routing.schedule SET light_due=0,full_due=0 WHERE channel=%s",
                             (self.cid,))
        others = self._other_schedules()
        try:
            due = channel_store.poll_schedule(time.time())
            self.assertEqual(sorted(kind for channel, kind in due if channel == self.cid),
                             ["connection", "full"])
            self.assertEqual([kind for channel, kind in channel_store.poll_schedule(time.time())
                              if channel == self.cid], [])
        finally:
            self._restore_schedules(others)
        with self._conn() as conn:
            advanced = conn.execute("SELECT light_due,full_due FROM routing.schedule WHERE channel=%s",
                                    (self.cid,)).fetchone()
        self.assertGreater(advanced["light_due"], 0)
        self.assertGreater(advanced["full_due"], 0)
        channel_store.record_audit("scheduler.blocked.full", self.cid, "scheduler")
        with self._conn() as conn:
            audited = conn.execute("SELECT COUNT(*) AS n FROM routing.events WHERE target=%s "
                                   "AND action='scheduler.blocked.full'", (self.cid,)).fetchone()["n"]
        self.assertEqual(audited, 1)

    def test_parameters_layout_draft_publish_and_quote_use_postgres(self):
        effective = params.admin_layout_state()["effective_layout"]
        params.layout_save("admin", {"layout": effective})
        self.assertEqual(channel_store.layout_setting()["video"]["order"],
                         effective["video"]["order"])
        self.assertNotIn(self.cid, channel_store.draft_setting())

        state = self.draft()
        self.assertEqual(state["draft"]["revision"], 1)
        self.assertEqual(channel_store.draft_setting()[self.cid]["parameters"]["default"], "first")
        published = self.publish()
        self.assertEqual(published["version"], 2)
        self.assertEqual(published["published"]["combinations"][0]["points"], 20)
        # 历史只列「带参数协议」的版本：v1 没有参数，v2 刚发布
        self.assertEqual([item["version"] for item in published["history"]], [2])
        with self._conn() as conn:
            secret = conn.execute("SELECT secret FROM routing.versions WHERE channel=%s AND version=2",
                                  (self.cid,)).fetchone()["secret"]
        self.assertNotIn("private-secret", secret)

        cm.save_mapping("admin", dict(kind="image", front="m3c-front",
                                      channel=self.cid, enabled=True))
        self.assertEqual(params.quote("image", self.public_payload("m3c-front")), 20)
        published_configs = channel_store.published_channel_configs()
        self.assertIn(("m3c-front", 2),
                      [(mapping["front"], version) for mapping, version, _ in published_configs])
        self.assertEqual(channel_store.mapping_by_selector("image:m3c-front")["channel"], self.cid)
        self.assertEqual(channel_store.mapping_by_selector("image:m3c-missing"), {})
        self.assertIn("m3c-front", [item["front"] for item in params.public_catalog()["items"]])
        self.assertEqual(channel_store.channel_versions(self.cid)[0]["version"], 2)

    def test_switched_process_never_touches_sqlite(self):
        before = self.sqlite_state()
        rid = cm.reserve(self.cid, "full")
        channel_store.try_start_run(rid, cm.version(self.cid), time.time() - 2400)
        channel_store.note_incident(self.cid, "full", "failed")
        channel_store.record_audit("test.connection", self.cid, "admin")
        self.draft()
        params.layout_save("admin", {"layout": params.admin_layout_state()["effective_layout"]})
        self.assertEqual(self.sqlite_state(), before)
        with self.assertRaises(RuntimeError):
            cm.db()


if __name__ == "__main__":
    unittest.main()
