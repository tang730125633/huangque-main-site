# -*- coding: utf-8 -*-
"""#187 P0·资金：job 状态机 CAS + 退点幂等 并发正确性单测。
断言：无论 reaper 超时 / worker 成功 / worker 异常如何交错，点数最多退一次，
且 error 终态不会被后到的 done 覆盖（不会既出片又退点）。"""
import importlib, os, sys, time, unittest
from contextlib import closing
from pathlib import Path


class JobRefundCasTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.core = importlib.import_module("content_domains.core")
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_jobdb = self.core.JOB_DB
        self.core.JOB_DB = os.path.join(self.tmp.name, "jobs.db")
        with closing(self.core.jdb()) as c:
            c.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
                created_at INTEGER, updated_at INTEGER, deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0,
                owner TEXT)""")   # owner：任务归属服务(#511)。本夹具手搓 schema，得跟着 init_db 走
            c.commit()
        # 统计真正退点次数（打桩 points 域）
        self.refunds = []
        self.refund_keys = []
        outer = self
        class _FakePoints:
            @staticmethod
            def refund_points(username, amount, reason="", transaction_key=""):
                outer.refunds.append((username, amount, reason))
                outer.refund_keys.append(transaction_key)
        self._orig_domains = self.core._domains
        self.core._domains = lambda: (None, _FakePoints, None)

    def tearDown(self):
        self.core.JOB_DB = self._orig_jobdb
        self.core._domains = self._orig_domains
        self.tmp.cleanup()

    def _insert(self, cost=20, kind="video"):
        now = int(time.time())
        with closing(self.core.jdb()) as c:
            cur = c.execute(
                "INSERT INTO jobs(kind,username,cost,status,created_at,updated_at) "
                "VALUES(?,?,?,'running',?,?)",
                (kind, "u", cost, now, now),
            )
            c.commit()
            return cur.lastrowid

    def _row(self, jid):
        with closing(self.core.jdb()) as c:
            return c.execute("SELECT status, refunded FROM jobs WHERE id=?", (jid,)).fetchone()

    def _reaper_step(self, jid, cost=20):
        """模拟 reaper 判超时：CAS 抢 error，抢到才幂等退点。"""
        if self.core._set_terminal(jid, "error", error="生成超时"):
            self.core._refund_once(jid, "u", cost)

    def _worker_success(self, jid, cost=20):
        """模拟 worker 成功：CAS 抢 done；抢不到则放弃(不入库/不覆盖)。"""
        return self.core._set_terminal(jid, "done", result={"ok": 1})

    def _worker_fail(self, jid, cost=20):
        """模拟 worker 异常：CAS 抢 error，幂等退点。"""
        self.core._set_terminal(jid, "error", error="boom")
        self.core._refund_once(jid, "u", cost)

    # --- 竞态 1：reaper 先判超时退点，worker 随后成功 → 不得覆盖、不得二次退点 ---
    def test_reaper_wins_then_worker_success_cannot_overwrite(self):
        jid = self._insert(20)
        self._reaper_step(jid)               # reaper 抢 error + 退 1 次
        won = self._worker_success(jid)      # worker 成功但 CAS 应失败
        self.assertFalse(won)
        row = self._row(jid)
        self.assertEqual(row["status"], "error")   # 终态未被 done 覆盖
        self.assertEqual(len(self.refunds), 1)     # 只退一次

    # --- 竞态 2：reaper 先退点，worker 随后异常也退点 → 仍只退一次 ---
    def test_reaper_and_worker_both_error_refund_once(self):
        jid = self._insert(20)
        self._reaper_step(jid)
        self._worker_fail(jid)               # worker 异常路径也调退点
        self.assertEqual(len(self.refunds), 1)
        self.assertEqual(self._row(jid)["status"], "error")

    # --- 正常成功：不退点，reaper 事后也无法 error/退点 ---
    def test_worker_success_never_refunds(self):
        jid = self._insert(20)
        self.assertTrue(self._worker_success(jid))
        self._reaper_step(jid)               # reaper 想判超时：CAS 失败 → 不退点
        self.assertEqual(self._row(jid)["status"], "done")
        self.assertEqual(len(self.refunds), 0)

    # --- 正常失败：退且仅退一次 ---
    def test_worker_fail_refunds_once(self):
        jid = self._insert(20)
        self._worker_fail(jid)
        self._worker_fail(jid)               # 重复调不应二次退
        self.assertEqual(len(self.refunds), 1)
        self.assertEqual(self._row(jid)["status"], "error")

    # --- 退点幂等键：直接连调 _refund_once 也只退一次 ---
    def test_refund_once_idempotent(self):
        jid = self._insert(20)
        self.core._set_terminal(jid, "error", error="x")
        self.core._refund_once(jid, "u", 20)
        self.core._refund_once(jid, "u", 20)
        self.core._refund_once(jid, "u", 20)
        self.assertEqual(len(self.refunds), 1)
        self.assertEqual(self._row(jid)["refunded"], 1)
        self.assertEqual(self.refund_keys, ["job-refund:u:%d" % jid])

    def test_failed_refund_scanner_retries_error_rows(self):
        old_jid = self._insert_status("error", 20)
        retry = lambda: self.core.jobs_store.retry_failed_refunds(
            self.core.jdb, self.core._refund_once)
        self.assertEqual(retry(), 0, "历史 refunded=0 不能在没有确切证据时自动退款")
        jid = self._insert(20)
        self.assertTrue(self.core._set_terminal(jid, "error", error="new failure"))
        failing_points = self.core._domains()[1]
        self.core._domains = lambda: (None, type("P", (), {
            "refund_points": staticmethod(lambda *args, **kwargs: (_ for _ in ()).throw(
                ConnectionError("response lost")))
        }), None)
        self.assertFalse(self.core._refund_once(jid, "u", 20))
        self.assertEqual(self._row(jid)["refunded"], 2)
        self.core._domains = lambda: (None, failing_points, None)
        self.assertEqual(retry(), 1)
        self.assertEqual(self._row(jid)["refunded"], 1)
        self.assertEqual(retry(), 0)
        self.assertEqual(self.refund_keys, ["job-refund:u:%d" % jid])
        self.assertEqual(self._row(old_jid)["refunded"], 0)

    def test_auth_commits_then_response_is_lost_without_double_refund(self):
        jid = self._insert(20)
        self.assertTrue(self.core._set_terminal(jid, "error", error="upstream failed"))
        applied, calls = set(), []

        class _LostFirstResponse:
            @staticmethod
            def refund_points(username, amount, reason="", transaction_key=""):
                calls.append(transaction_key)
                first = transaction_key not in applied
                applied.add(transaction_key)
                if first:
                    raise ConnectionError("Auth committed but response was lost")
                return 100

        self.core._domains = lambda: (None, _LostFirstResponse, None)
        self.assertFalse(self.core._refund_once(jid, "u", 20))
        self.assertEqual(self._row(jid)["refunded"], 2)
        self.assertEqual(self.core.jobs_store.retry_failed_refunds(
            self.core.jdb, self.core._refund_once), 1)
        self.assertEqual(len(applied), 1, "同一个退款键在 Auth 只能实际入账一次")
        self.assertEqual(calls, ["job-refund:u:%d" % jid] * 2)
        self.assertEqual(self._row(jid)["refunded"], 1)

    def test_client_payload_cannot_choose_refund_key(self):
        jid = self._insert(20)
        with closing(self.core.jdb()) as c:
            c.execute("UPDATE jobs SET payload=? WHERE id=?",
                      ('{"_refund_transaction_key":"attacker-controlled"}', jid))
            c.commit()
        self.assertTrue(self.core._set_terminal(jid, "error", error="failed"))
        self.assertEqual(self.core.jobs_store.retry_failed_refunds(
            self.core.jdb, self.core._refund_once), 1)
        self.assertEqual(self.refund_keys, ["job-refund:u:%d" % jid])

    def test_insert_failure_response_loss_queues_same_refund_key(self):
        seen = []
        working_points = self.core._domains()[1]

        class _LostResponsePoints:
            @staticmethod
            def refund_points(username, amount, reason="", transaction_key=""):
                seen.append(transaction_key)
                raise ConnectionError("response lost")

        with closing(self.core.jdb()) as c:
            c.execute("""CREATE TRIGGER fail_pending BEFORE INSERT ON jobs
                         WHEN NEW.status='pending' BEGIN SELECT RAISE(FAIL, 'db insert failed'); END""")
            c.commit()
        with self.assertRaises(self.core.jobs_store.PaidJobInsertError) as ctx:
            self.core.jobs_store.create_paid_job(
                self.core.jdb, lambda *_: 80, _LostResponsePoints.refund_points,
                "image", "u", 20, {}, "content")
        self.assertEqual(ctx.exception.compensation, "queued")

        self.core._domains = lambda: (None, working_points, None)
        self.assertEqual(self.core.jobs_store.retry_failed_refunds(
            self.core.jdb, self.core._refund_once), 1)
        self.assertEqual(seen, self.refund_keys)
        self.assertEqual(1, len(seen))
        self.assertTrue(seen[0].startswith("job-refund:u:"))

    # --- done 的 job 即便误调 _refund_once 也不退（status='error' 保险） ---
    def test_done_job_not_refunded(self):
        jid = self._insert(20)
        self.assertTrue(self._worker_success(jid))
        self.core._refund_once(jid, "u", 20)   # 误调
        self.assertEqual(len(self.refunds), 0)

    def _insert_status(self, status, cost=20):
        now = int(time.time())
        with closing(self.core.jdb()) as c:
            cur = c.execute(
                "INSERT INTO jobs(kind,username,cost,status,created_at,updated_at) VALUES('tryon','u',?,?,?,?)",
                (cost, status, now, now))
            c.commit(); return cur.lastrowid

    # --- 启动回收孤儿：只回收 running(重启遗留)，pending/done 不动，退且仅退一次、幂等 ---
    def test_reclaim_orphaned_running(self):
        run1 = self._insert(20)                    # running 孤儿
        run2 = self._insert_status("running", 30)  # running 孤儿
        pend = self._insert_status("pending", 20)  # 排队中→不该动(交给 pending 恢复机制)
        done = self._insert_status("done", 20)     # 已完成→不该动
        n = self.core.reclaim_orphaned_running()
        self.assertEqual(n, 2)
        self.assertEqual(self._row(run1)["status"], "error")
        self.assertEqual(self._row(run2)["status"], "error")
        self.assertEqual(self._row(pend)["status"], "pending")
        self.assertEqual(self._row(done)["status"], "done")
        self.assertEqual(len(self.refunds), 2)     # 两个孤儿各退一次
        # 幂等：再调一次不重复退(running 已清空)
        self.assertEqual(self.core.reclaim_orphaned_running(), 0)
        self.assertEqual(len(self.refunds), 2)

    def test_reclaim_requeues_resumable_xai_without_refund(self):
        jid = self._insert(300, kind="xiaole_video")

        class _FakeVideo:
            @staticmethod
            def get_resumable_xai_request(job_id):
                return {"request_id": "rid-existing"} if job_id == jid else None

        self.core._domains = lambda: (None, type("P", (), {
            "refund_points": staticmethod(lambda *args, **kwargs: None)
        }), _FakeVideo)
        n = self.core.reclaim_orphaned_running()
        self.assertEqual(n, 1)
        self.assertEqual(self._row(jid)["status"], "pending")
        self.assertEqual(self._row(jid)["refunded"], 0)
        self.assertEqual(self.refunds, [])

    def test_reclaim_lookup_exception_keeps_running_without_refund(self):
        jid = self._insert(300, kind="xiaole_video")
        points_domain = self.core._domains()[1]

        class _BrokenVideo:
            @staticmethod
            def get_resumable_xai_request(job_id):
                raise RuntimeError("lookup unavailable")

        self.core._domains = lambda: (None, points_domain, _BrokenVideo)
        self.assertEqual(self.core.reclaim_orphaned_running(), 0)
        self.assertEqual(self._row(jid)["status"], "running")
        self.assertEqual(self._row(jid)["refunded"], 0)
        self.assertEqual(len(self.refunds), 0)
        self.assertEqual(self.core.reclaim_orphaned_running(), 0)
        self.assertEqual(len(self.refunds), 0)

    def test_reclaim_lost_requeue_cas_does_not_refund_or_overwrite(self):
        jid = self._insert(300, kind="xiaole_video")

        class _FakeVideo:
            @staticmethod
            def get_resumable_xai_request(job_id):
                return {"request_id": "rid-racing"}

        self.core._domains = lambda: (None, type("P", (), {
            "refund_points": staticmethod(lambda *args, **kwargs: None)
        }), _FakeVideo)
        original_requeue = self.core._requeue_running_job
        self.core._requeue_running_job = lambda job_id: False
        try:
            self.assertEqual(self.core.reclaim_orphaned_running(), 0)
        finally:
            self.core._requeue_running_job = original_requeue
        self.assertEqual(self._row(jid)["status"], "running")
        self.assertEqual(self._row(jid)["refunded"], 0)
        self.assertEqual(self.refunds, [])

    def test_reclaim_malformed_request_id_falls_back_safely(self):
        jid = self._insert(300, kind="xiaole_video")
        points_domain = self.core._domains()[1]

        class _MalformedVideo:
            @staticmethod
            def get_resumable_xai_request(job_id):
                return {"request_id": "   "}

        self.core._domains = lambda: (None, points_domain, _MalformedVideo)
        self.assertEqual(self.core.reclaim_orphaned_running(), 1)
        self.assertEqual(self._row(jid)["status"], "error")
        self.assertEqual(self._row(jid)["refunded"], 1)
        self.assertEqual(len(self.refunds), 1)


if __name__ == "__main__":
    unittest.main()
