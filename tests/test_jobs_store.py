# -*- coding: utf-8 -*-
"""content_domains/jobs_store.py —— 三个服务共用的 jobs 状态机与退点幂等。

这段逻辑此前在 core.py / leadgen_api.py / imggen_api.py 里各抄了一份，
同一个资金 bug 因此依次踩过三次（#187、jobs 1170、jobs 1356 那批）。

不变量：
1. 终态 CAS：谁先抢到谁定终态，败者不写状态、不做副作用
2. 认领 CAS：只有 pending 能被接管，防同一 job 跑两遍
3. 退点幂等：最多退一次
4. 退点失败保持 refunded=2 待确认，scanner 用同一个键继续确认
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from content_domains import jobs_store  # noqa: E402


class JobsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "jobs.db")
        with closing(self._conn()) as c:
            c.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
                created_at INTEGER, updated_at INTEGER, deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0,
                owner TEXT)""")
            c.commit()
        self.refunds = []

    def tearDown(self):
        self.tmp.cleanup()

    def _conn(self):
        c = sqlite3.connect(self.db, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _jdb(self):
        return self._conn()

    def _insert(self, cost=10, status="running"):
        now = int(time.time())
        with closing(self._conn()) as c:
            cur = c.execute("INSERT INTO jobs(kind,username,cost,status,created_at,updated_at) "
                            "VALUES('collect','u',?,?,?,?)", (cost, status, now, now))
            c.commit()
            return cur.lastrowid

    def _row(self, jid):
        with closing(self._conn()) as c:
            return c.execute("SELECT status, refunded, result, error FROM jobs WHERE id=?", (jid,)).fetchone()

    def _ok_refund(self, u, c):
        self.refunds.append((u, c))
        return True

    def _bad_refund(self, u, c):
        return False

    # --- 1. 终态 CAS ---
    def test_set_terminal_requires_running_by_default(self):
        jid = self._insert(status="pending")
        self.assertFalse(jobs_store.set_terminal(self._jdb, jid, "done", result={"x": 1}))
        self.assertEqual(self._row(jid)["status"], "pending")

    def test_set_terminal_from_pending_when_allowed(self):
        jid = self._insert(status="pending")
        self.assertTrue(jobs_store.set_terminal(self._jdb, jid, "error", error="db locked",
                                                from_states=("pending", "running")))
        self.assertEqual(self._row(jid)["status"], "error")
        self.assertEqual(self._row(jid)["refunded"], 2)

    def test_loser_does_not_overwrite_terminal(self):
        """reaper 先判 error，worker 随后成功 → 结果必须被丢弃。这就是 21 条僵尸记录的成因。"""
        jid = self._insert()
        self.assertTrue(jobs_store.set_terminal(self._jdb, jid, "error", error="超时"))
        self.assertFalse(jobs_store.set_terminal(self._jdb, jid, "done", result={"text": "拿到了"}))
        row = self._row(jid)
        self.assertEqual(row["status"], "error")
        self.assertIsNone(row["result"])

    def test_error_message_truncated_to_300(self):
        jid = self._insert()
        jobs_store.set_terminal(self._jdb, jid, "error", error="x" * 500)
        self.assertEqual(len(self._row(jid)["error"]), 300)

    # --- 2. 认领 CAS ---
    def test_claim_running_only_from_pending(self):
        jid = self._insert(status="pending")
        self.assertTrue(jobs_store.claim_running(self._jdb, jid))
        self.assertEqual(self._row(jid)["status"], "running")
        self.assertFalse(jobs_store.claim_running(self._jdb, jid), "同一 job 不该被认领两次")

    def test_claim_running_refuses_terminal_job(self):
        jid = self._insert(status="pending")
        jobs_store.claim_running(self._jdb, jid)
        jobs_store.set_terminal(self._jdb, jid, "error", error="boom")
        self.assertFalse(jobs_store.claim_running(self._jdb, jid))
        self.assertEqual(self._row(jid)["status"], "error")

    # --- 3. 退点幂等 ---
    def test_refund_once_only_once(self):
        jid = self._insert(10)
        jobs_store.set_terminal(self._jdb, jid, "error", error="boom")
        self.assertTrue(jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund))
        self.assertFalse(jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund))
        self.assertEqual(self.refunds, [("u", 10)])
        self.assertEqual(self._row(jid)["refunded"], 1)

    def test_refund_requires_error_terminal(self):
        jid = self._insert(10)
        jobs_store.set_terminal(self._jdb, jid, "done", result={"ok": 1})
        self.assertFalse(jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund))
        self.assertEqual(self.refunds, [], "done 的任务不该退点")

    def test_zero_or_bad_cost_never_refunds(self):
        jid = self._insert(0)
        jobs_store.set_terminal(self._jdb, jid, "error", error="boom")
        self.assertFalse(jobs_store.refund_once(self._jdb, jid, "u", 0, self._ok_refund))
        self.assertFalse(jobs_store.refund_once(self._jdb, jid, "u", None, self._ok_refund))
        self.assertFalse(jobs_store.refund_once(self._jdb, jid, "u", "abc", self._ok_refund))
        self.assertEqual(self.refunds, [])

    # --- 4. 退点失败保持待确认，恢复后安全重试 ---
    def test_refund_failure_stays_pending(self):
        jid = self._insert(10)
        jobs_store.set_terminal(self._jdb, jid, "error", error="boom")
        self.assertFalse(jobs_store.refund_once(self._jdb, jid, "u", 10, self._bad_refund))
        self.assertEqual(self._row(jid)["refunded"], 2)
        # 恢复后重试应能成功退一次
        self.assertTrue(jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund))
        self.assertEqual(self.refunds, [("u", 10)])
        self.assertEqual(self._row(jid)["refunded"], 1)

    def test_refund_exception_rolls_back_flag_for_retry(self):
        jid = self._insert(10)
        jobs_store.set_terminal(self._jdb, jid, "error", error="boom")

        self.assertFalse(jobs_store.refund_once(
            self._jdb, jid, "u", 10,
            lambda *_: (_ for _ in ()).throw(ConnectionError("response lost")),
        ))
        self.assertEqual(self._row(jid)["refunded"], 2)
        self.assertTrue(jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund))

    def test_scanner_ignores_ambiguous_historical_error(self):
        jid = self._insert(10, status="error")
        self.assertEqual(jobs_store.retry_failed_refunds(
            self._jdb, lambda *_: self.fail("historical row must not refund")), 0)

    def test_batch_insert_failure_compensates_total_once(self):
        with closing(self._conn()) as c:
            c.execute("""CREATE TRIGGER fail_pending BEFORE INSERT ON jobs
                         WHEN NEW.status='pending' BEGIN SELECT RAISE(FAIL, 'insert failed'); END""")
            c.commit()
        refund_calls = []

        def refund(username, amount, reason="", transaction_key=""):
            refund_calls.append((username, amount, transaction_key))
            return True

        with self.assertRaises(jobs_store.PaidJobInsertError) as ctx:
            jobs_store.create_paid_jobs(
                self._jdb, lambda *_args: 60, refund, "video", "u",
                [(20, {"n": 1}), (20, {"n": 2})], "content", "video_batch")
        self.assertEqual(ctx.exception.compensation, "refunded")
        self.assertEqual(1, len(refund_calls))
        self.assertEqual(("u", 40), refund_calls[0][:2])
        with closing(self._conn()) as c:
            row = c.execute("SELECT status,cost,refunded FROM jobs").fetchone()
        self.assertEqual(("error", 40, 1), tuple(row))

    def test_paid_job_before_commit_is_atomic_and_pending_is_invisible(self):
        with closing(self._conn()) as c:
            c.execute("CREATE TABLE job_links(job_id INTEGER PRIMARY KEY)")
            c.commit()
        visible_during_callback = []

        def associate(connection, job_id):
            with closing(self._conn()) as observer:
                visible_during_callback.append(bool(observer.execute(
                    "SELECT 1 FROM jobs WHERE id=?", (job_id,)
                ).fetchone()))
            connection.execute("INSERT INTO job_links(job_id) VALUES(?)", (job_id,))

        job_id, points_left = jobs_store.create_paid_job(
            self._jdb, lambda *_args: 90, lambda *_args, **_kwargs: True,
            "image", "u", 10, {"prompt": "still"}, "content",
            before_commit=associate,
        )

        self.assertEqual([False], visible_during_callback)
        self.assertEqual(90, points_left)
        with closing(self._conn()) as c:
            self.assertEqual("pending", c.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()[0])
            self.assertEqual(job_id, c.execute(
                "SELECT job_id FROM job_links"
            ).fetchone()[0])

    def test_paid_job_before_commit_failure_rolls_back_job_and_association(self):
        with closing(self._conn()) as c:
            c.execute("CREATE TABLE job_links(job_id INTEGER PRIMARY KEY)")
            c.commit()
        refunds = []

        def refund(username, amount, reason="", transaction_key=""):
            refunds.append((username, amount, transaction_key))
            return True

        def fail_association(connection, job_id):
            connection.execute("INSERT INTO job_links(job_id) VALUES(?)", (job_id,))
            raise RuntimeError("association failed")

        with self.assertRaises(jobs_store.PaidJobInsertError) as ctx:
            jobs_store.create_paid_job(
                self._jdb, lambda *_args: 90, refund,
                "image", "u", 10, {"prompt": "still"}, "content",
                before_commit=fail_association,
            )

        self.assertEqual("refunded", ctx.exception.compensation)
        self.assertEqual(1, len(refunds))
        self.assertEqual(("u", 10), refunds[0][:2])
        with closing(self._conn()) as c:
            self.assertEqual(0, c.execute("SELECT COUNT(*) FROM job_links").fetchone()[0])
            self.assertEqual(0, c.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='pending'"
            ).fetchone()[0])

    def test_insert_compensation_and_restart_recovery_share_refund_key(self):
        import hashlib

        with closing(self._conn()) as c:
            c.execute("""CREATE TRIGGER fail_pending BEFORE INSERT ON jobs
                         WHEN NEW.status='pending' BEGIN SELECT RAISE(FAIL, 'insert failed'); END""")
            c.commit()
        refund_keys = []

        def refund(username, amount, reason="", transaction_key=""):
            refund_keys.append(transaction_key)
            if len(refund_keys) == 1:
                raise ConnectionError("refund response lost")
            return True

        charge_key = "job-charge:u:/api/gen/xiaole_video:stable-key"
        expected = "job-charge-refund:" + hashlib.sha256(
            charge_key.encode("utf-8")).hexdigest()
        with self.assertRaises(jobs_store.PaidJobInsertError) as ctx:
            jobs_store.create_paid_job(
                self._jdb, lambda *_args: 90, refund,
                "xiaole_video", "u", 10, {"channel": "micro"}, "content",
                charge_transaction_key=charge_key,
            )
        self.assertEqual("queued", ctx.exception.compensation)

        def retry_job(job_id, username, cost, transaction_key=""):
            return jobs_store.refund_once(
                self._jdb, job_id, username, cost,
                lambda u, c: refund(
                    u, c, "retry", transaction_key=transaction_key),
            )

        self.assertEqual(1, jobs_store.retry_failed_refunds(
            self._jdb, retry_job))
        self.assertEqual([expected, expected], refund_keys)

    # --- 端到端：reaper 与 worker 交错，钱只退一次，结果不覆写 ---
    def test_reaper_wins_race_money_is_correct(self):
        jid = self._insert(10)
        if jobs_store.set_terminal(self._jdb, jid, "error", error="生成超时自动结束，已退点"):
            jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund)
        self.assertFalse(jobs_store.set_terminal(self._jdb, jid, "done", result={"text": "结果"}))
        self.assertEqual(len(self.refunds), 1)
        self.assertIsNone(self._row(jid)["result"])

    def test_worker_wins_race_no_refund(self):
        jid = self._insert(10)
        self.assertTrue(jobs_store.set_terminal(self._jdb, jid, "done", result={"text": "结果"}))
        if jobs_store.set_terminal(self._jdb, jid, "error", error="超时"):
            jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund)
        self.assertEqual(self.refunds, [], "任务成功了不该退点")
        self.assertEqual(self._row(jid)["status"], "done")


class WrappersDelegateTests(unittest.TestCase):
    """三个服务的 _set_terminal/_refund_once 必须是薄包装，签名一致。"""

    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        os.environ.setdefault("CONTENT_OUT", tempfile.mkdtemp(prefix="hq-jobsstore-"))

    def test_all_three_expose_from_states(self):
        import importlib, inspect
        for mod_name in ("content_domains.core", "leadgen_api", "imggen_api"):
            m = importlib.import_module(mod_name)
            sig = inspect.signature(m._set_terminal)
            self.assertIn("from_states", sig.parameters, mod_name)

    def test_no_duplicate_cas_sql_left_behind(self):
        """三处的裸 SQL 必须已经删干净，否则改一处漏两处的老问题会复发。"""
        for rel in ("server/content_domains/core.py", "server/leadgen_api.py", "server/imggen_api.py"):
            text = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
            self.assertNotIn("refunded=1 WHERE id=? AND refunded=0", text, rel)


if __name__ == "__main__":
    unittest.main()
