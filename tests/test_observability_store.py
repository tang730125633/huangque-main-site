# 运行证据与告警发件箱存储层测试：SQLite 模式行为不变 + PostgreSQL 模式全链路。
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest import mock

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from content_domains import observability_store, runtime_observability as telemetry  # noqa: E402

PG_URL = os.environ.get("HQ_DATABASE_URL")


@contextlib.contextmanager
def channel_settings_stub(enabled=False):
    """把 ``dispatch()`` 里的「通知地址读取」换成桩。

    那一步属于渠道域（``channel_manager``），本域用例只验证证据与发件箱行为；
    桩掉它既让用例不依赖并发域的落地状态，也让「postgres 模式不碰旧 SQLite 文件」
    这条断言成立（真实 ``channel_manager.notification_settings`` 会直连旧库，
    已在 Runbook「已知风险」记录，改由收编方处理）。
    """
    stub = types.ModuleType("content_domains.channel_manager")
    stub.notification_settings = lambda private=False: {"enabled": enabled}
    import content_domains
    with mock.patch.dict(sys.modules, {"content_domains.channel_manager": stub}), \
            mock.patch.object(content_domains, "channel_manager", stub, create=True):
        yield


class MiniMaxCredentialRejected(Exception):
    """与适配器同名：提交阶段的确定拒绝必须记 failed 而不是 unknown。"""


class SqliteModeTest(unittest.TestCase):
    """默认 sqlite 模式：与迁移前行为逐项一致，绝不触碰 PostgreSQL。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hq-obs-store-")
        self.db_path = os.path.join(self.tmp, "runtime_observability.db")
        os.environ["HQ_OBSERVABILITY_DB"] = self.db_path
        os.environ["HQ_ALERT_ENABLED"] = "0"
        os.environ.pop("HQ_ALERT_WEBHOOK_URL", None)
        os.environ.pop("HQ_OBS_STORE", None)

    def tearDown(self):
        os.environ.pop("HQ_OBSERVABILITY_DB", None)
        os.environ.pop("HQ_OBS_STORE", None)
        os.environ.pop("HQ_ALERT_ENABLED", None)

    def _mutate(self):
        with closing(telemetry.database()) as connection:
            connection.execute("UPDATE alert_outbox SET next_try=0")
            connection.commit()

    def test_default_mode_is_sqlite(self):
        self.assertEqual(observability_store.mode(), "sqlite")
        self.assertFalse(observability_store.enabled())

    def test_invalid_mode_raises(self):
        with mock.patch.dict(os.environ, {"HQ_OBS_STORE": "mysql"}):
            with self.assertRaises(RuntimeError):
                observability_store.mode()
            # 非法取值在读写入口立刻暴露，绝不静默降级去写另一个库
            with self.assertRaises(RuntimeError):
                telemetry.traces(1)

    def test_record_upserts_and_keeps_started(self):
        telemetry.record(101, "route", "running", provider="Actual Provider")
        with closing(telemetry.database()) as connection:
            first = connection.execute(
                "SELECT started FROM task_trace WHERE job_id=? AND stage=?",
                ("101", "route")).fetchone()["started"]
        telemetry.record(101, "route", "recorded", duration=1.5, provider="Actual Provider")
        rows = telemetry.traces(101)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "recorded")
        self.assertEqual(rows[0]["duration_sec"], 1.5)
        self.assertEqual(rows[0]["started_at"], first)
        self.assertEqual(rows[0]["provider"], "Actual Provider")
        self.assertEqual(telemetry.traces(102), [])

    def test_metadata_is_filtered_and_truncated(self):
        telemetry.record(103, "download", "recorded", provider="x" * 200,
                         api_key="secret-value", transport=123)
        row = telemetry.traces(103)[0]
        self.assertEqual(len(row["provider"]), 160)
        self.assertEqual(row["transport"], "123")
        self.assertNotIn("secret-value", json.dumps(row))

    def test_call_states_match_engine_rules(self):
        with self.assertRaises(TimeoutError):
            telemetry.call(104, "provider_query",
                           lambda: (_ for _ in ()).throw(TimeoutError("secret")))
        self.assertEqual(telemetry.traces(104)[0]["state"], "failed")
        with self.assertRaises(TimeoutError):
            telemetry.call(105, "provider_submit",
                           lambda: (_ for _ in ()).throw(TimeoutError("secret")))
        self.assertEqual(telemetry.traces(105)[0]["state"], "unknown")
        with self.assertRaises(MiniMaxCredentialRejected):
            telemetry.call(106, "provider_submit",
                           lambda: (_ for _ in ()).throw(MiniMaxCredentialRejected("rejected")))
        self.assertEqual(telemetry.traces(106)[0]["state"], "failed")

    def test_search_task_ids_reads_metadata(self):
        telemetry.record(107, "route", "recorded", model="m3b-actual-model")
        self.assertEqual({"107"}, telemetry.search_task_ids("m3b-actual-model"))
        self.assertEqual(set(), telemetry.search_task_ids(""))

    def test_enqueue_dedup_and_alert_status(self):
        telemetry.enqueue("service.incident.open", "content", 1)
        telemetry.enqueue("service.incident.open", "content", 1)
        self.assertEqual(telemetry.alert_status()["counts"], {"pending": 1})
        # 未配置投递地址时 dispatch 直接返回，绝不外发
        with mock.patch.object(telemetry.urllib.request, "build_opener") as opener, \
                channel_settings_stub():
            telemetry.dispatch()
        opener.assert_not_called()
        self.assertFalse(telemetry.alert_status()["enabled"])

    def test_sqlite_mode_never_calls_postgres_store(self):
        with mock.patch.object(observability_store, "write_trace",
                               side_effect=AssertionError("sqlite 模式不得写 PostgreSQL")), \
                mock.patch.object(observability_store, "enqueue_alert",
                                  side_effect=AssertionError("sqlite 模式不得写 PostgreSQL")):
            telemetry.record(108, "route", "recorded")
            telemetry.enqueue("service.incident.recovered", "content", 2)
        self.assertEqual(len(telemetry.traces(108)), 1)

    def test_unreadable_database_degrades_without_raising(self):
        os.environ["HQ_OBSERVABILITY_DB"] = self.tmp  # 目录不可当库文件
        telemetry.record(109, "route", "recorded")   # 证据缺失不影响任务结论
        self.assertEqual(telemetry.traces(109), [])
        self.assertEqual(telemetry.search_task_ids("x"), set())
        self.assertEqual(telemetry.alert_status(),
                         {"enabled": False, "error": "通知记录不可读"})

    def test_postgres_mode_without_url_degrades_and_entry_point_raises(self):
        os.environ["HQ_OBS_STORE"] = "postgres"
        old = os.environ.pop("HQ_DATABASE_URL", None)
        observability_store.close_pool()
        try:
            self.assertTrue(observability_store.enabled())
            with self.assertRaises(RuntimeError):
                observability_store.read_traces("110")
            with self.assertRaises(RuntimeError):
                telemetry.enqueue("service.incident.open", "content", 3)
            telemetry.record(110, "route", "recorded")  # 存储失败沿用降级语义
            self.assertEqual(telemetry.traces(110), [])
            self.assertEqual(telemetry.alert_status(),
                             {"enabled": False, "error": "通知记录不可读"})
        finally:
            if old is not None:
                os.environ["HQ_DATABASE_URL"] = old
            os.environ.pop("HQ_OBS_STORE", None)
            observability_store.close_pool()

    def test_dispatch_retries_and_recovers_through_sqlite(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        received = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(503 if len(received) == 1 else 204)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.dict(os.environ, {
                "HQ_ALERT_ENABLED": "1",
                "HQ_ALERT_WEBHOOK_URL": "http://127.0.0.1:%d/events" % server.server_port,
            }), channel_settings_stub():
                telemetry.enqueue("service.incident.open", "content", 4)
                telemetry.dispatch()
                self.assertEqual(telemetry.alert_status()["counts"]["pending"], 1)
                self._mutate()
                telemetry.dispatch()
                telemetry.enqueue("service.incident.recovered", "content", 5)
                telemetry.dispatch()
                self.assertEqual(telemetry.alert_status()["counts"]["sent"], 2)
                self.assertEqual([item["event"] for item in received],
                                 ["service.incident.open", "service.incident.open",
                                  "service.incident.recovered"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_sqlite_schema_stays_identical(self):
        with closing(telemetry.database()):
            pass
        conn = sqlite3.connect(self.db_path)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        columns = [row[1] for row in conn.execute("PRAGMA table_info(task_trace)")]
        conn.close()
        self.assertEqual(tables, {"task_trace", "alert_outbox"})
        self.assertEqual(columns, ["job_id", "stage", "state", "started", "updated",
                                   "duration", "metadata"])


class PostgresBranchWiringTest(unittest.TestCase):
    """不连 PostgreSQL：用替身 store 验证分发分支（本机无 PG 也能跑这些路径）。

    `channel_manager` 只是「通知地址是否启用」的读取方，属于另一个域；这里替换成桩，
    既让用例不依赖并发域的落地状态，也能断言 PG 模式下不再触碰旧 SQLite 文件。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hq-obs-branch-")
        self.db_path = os.path.join(self.tmp, "runtime_observability.db")
        os.environ["HQ_OBS_STORE"] = "postgres"
        os.environ["HQ_OBSERVABILITY_DB"] = self.db_path
        os.environ["HQ_ALERT_ENABLED"] = "0"
        os.environ.pop("HQ_ALERT_WEBHOOK_URL", None)
        patcher = mock.patch.multiple(
            observability_store,
            write_trace=mock.DEFAULT, read_traces=mock.DEFAULT,
            enqueue_alert=mock.DEFAULT, pending_alerts=mock.DEFAULT,
            update_alert=mock.DEFAULT, alert_counts=mock.DEFAULT,
            search_trace_job_ids=mock.DEFAULT,
        )
        self.mocks = patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("HQ_OBS_STORE", "HQ_OBSERVABILITY_DB", "HQ_ALERT_ENABLED"):
            self.addCleanup(os.environ.pop, name, None)

    def test_record_traces_and_search_go_to_store(self):
        self.mocks["read_traces"].return_value = [
            {"stage": "route", "state": "recorded", "started": 1.0, "updated": 2.0,
             "duration": 0.5, "metadata": '{"provider": "minimax"}'}]
        self.mocks["search_trace_job_ids"].return_value = ["7750"]
        telemetry.record(7750, "route", "recorded", duration=0.5, provider="minimax",
                         api_key="secret-value")
        args = self.mocks["write_trace"].call_args.args
        self.assertEqual(args[:3], ("7750", "route", "recorded"))
        self.assertEqual(args[5], 0.5)
        self.assertEqual(json.loads(args[6]), {"provider": "minimax"})
        self.assertEqual(telemetry.traces(7750), [
            {"stage": "route", "state": "recorded", "started_at": 1.0, "updated_at": 2.0,
             "duration_sec": 0.5, "provider": "minimax"}])
        self.assertEqual({"7750"}, telemetry.search_task_ids("MiniMax"))
        self.assertFalse(os.path.exists(self.db_path))  # postgres 模式不碰旧 SQLite 文件

    def test_enqueue_and_alert_status_go_to_store(self):
        self.mocks["alert_counts"].return_value = {"pending": 1}
        telemetry.enqueue("service.incident.open", "content", 1234)
        event_id, payload, _ = self.mocks["enqueue_alert"].call_args.args
        self.assertEqual(event_id, "service.incident.open:content:1234")
        self.assertEqual(json.loads(payload), {"event": "service.incident.open",
                                              "service": "content", "occurred_at": 1234})
        self.assertEqual(telemetry.alert_status(),
                         {"enabled": False, "counts": {"pending": 1}, "error": ""})

    def test_dispatch_delivers_and_writes_back_through_store(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        received = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        payload = json.dumps({"event": "service.incident.open", "service": "content",
                              "occurred_at": 1})
        self.mocks["pending_alerts"].return_value = [
            {"event_id": "service.incident.open:content:1", "payload": payload, "state": "pending",
             "attempts": 0, "next_try": 0, "updated": 1.0, "error": ""}]
        with channel_settings_stub(), mock.patch.dict(os.environ, {
            "HQ_ALERT_ENABLED": "1",
            "HQ_ALERT_WEBHOOK_URL": "http://127.0.0.1:%d/notify" % server.server_port,
        }):
            telemetry.dispatch()
        self.assertEqual([item["event"] for item in received], ["service.incident.open"])
        event_id, state, attempts, _, _, error = self.mocks["update_alert"].call_args.args
        self.assertEqual((event_id, state, attempts, error),
                         ("service.incident.open:content:1", "sent", 1, ""))
        self.assertFalse(os.path.exists(self.db_path))  # 投递不留旧库痕迹


@unittest.skipUnless(PG_URL, "HQ_DATABASE_URL 未配置：跳过 PostgreSQL 测试")
class PostgresModeTest(unittest.TestCase):
    """postgres 模式：读写往返、覆盖语义、发件箱状态机与公共 API 一致。"""

    def setUp(self):
        os.environ["HQ_OBS_STORE"] = "postgres"
        os.environ["HQ_DATABASE_URL"] = PG_URL
        observability_store.close_pool()
        self.job = "m3b-" + uuid.uuid4().hex
        self.event = "%s:%s:%s" % ("m3b.check", self.job, int(time.time()))

    def tearDown(self):
        self._clean()
        os.environ.pop("HQ_OBS_STORE", None)
        observability_store.close_pool()

    def _clean(self):
        # 本模块自建连接池（与 flags_store 同做法），测试直接借用它清理。
        with observability_store._pool_instance().connection() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM ops.traces WHERE job_id = %s", (self.job,))
                conn.execute("DELETE FROM ops.alert_outbox WHERE event_id = %s", (self.event,))

    def test_trace_roundtrip_overwrite_and_order(self):
        try:
            observability_store.write_trace(self.job, "route", "running",
                                            time.time(), time.time(), None,
                                            json.dumps({"provider": "minimax"}))
            rows = observability_store.read_traces(self.job)
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["duration"])
            started = rows[0]["started"]
            observability_store.write_trace(self.job, "route", "recorded",
                                            time.time(), time.time(), 2.5,
                                            json.dumps({"provider": "minimax"}))
            rows = observability_store.read_traces(self.job)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], "recorded")
            self.assertEqual(rows[0]["duration"], 2.5)
            self.assertEqual(rows[0]["started"], started)
            observability_store.write_trace(self.job, "download", "failed",
                                            time.time() + 1, time.time() + 1, 0.5, "{}")
            rows = observability_store.read_traces(self.job)
            self.assertEqual([row["stage"] for row in rows], ["route", "download"])
            self.assertIn(self.job, observability_store.search_trace_job_ids("%minimax%"))
        finally:
            self._clean()

    def test_alert_state_machine(self):
        try:
            now = time.time()
            before = observability_store.alert_counts().get("pending", 0)
            observability_store.enqueue_alert(self.event, json.dumps(
                {"event": "m3b.check", "service": self.job, "occurred_at": now}), now)
            observability_store.enqueue_alert(self.event, json.dumps(
                {"event": "m3b.check", "service": self.job, "occurred_at": now}), now)
            self.assertEqual(observability_store.alert_counts().get("pending", 0), before + 1)
            pending = {row["event_id"]: row
                       for row in observability_store.pending_alerts(now + 1)}
            self.assertIn(self.event, pending)
            # 记一次失败：退避 60 秒后再到点，状态仍是 pending、原因只留异常类名
            observability_store.update_alert(self.event, "pending", 1, now + 120, now,
                                             "MiniMaxProviderFailed")
            self.assertNotIn(self.event, [row["event_id"]
                                          for row in observability_store.pending_alerts(now + 1)])
            later = {row["event_id"]: row
                     for row in observability_store.pending_alerts(now + 121)}
            self.assertEqual(later[self.event]["attempts"], 1)
            self.assertEqual(later[self.event]["error"], "MiniMaxProviderFailed")
            observability_store.update_alert(self.event, "sent", 2, now, now, "")
            self.assertNotIn(self.event, [row["event_id"]
                                          for row in observability_store.pending_alerts(now + 121)])
        finally:
            self._clean()

    def test_public_api_over_postgres(self):
        try:
            with self.assertRaises(TimeoutError):
                telemetry.call(self.job, "provider_query",
                               lambda: (_ for _ in ()).throw(TimeoutError("secret")),
                               model=self.job)
            rows = telemetry.traces(self.job)
            self.assertEqual([row["stage"] for row in rows], ["provider_query"])
            self.assertEqual(rows[0]["state"], "failed")
            self.assertEqual(rows[0]["error_type"], "TimeoutError")
            self.assertEqual(rows[0]["model"], self.job)
            self.assertNotIn("secret-value", json.dumps(rows))
            self.assertEqual({self.job}, telemetry.search_task_ids(self.job))
            occurred = int(time.time())
            self.event = "%s:%s:%d" % ("m3b.check", self.job, occurred)
            before = telemetry.alert_status()["counts"].get("pending", 0)
            telemetry.enqueue("m3b.check", self.job, occurred)
            self.assertEqual(telemetry.alert_status()["counts"].get("pending", 0), before + 1)
        finally:
            self._clean()


if __name__ == "__main__":
    unittest.main()
