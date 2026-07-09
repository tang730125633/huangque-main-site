# -*- coding: utf-8 -*-
"""leadgen_api（collect/leads，:8100）的 job 状态机 CAS + 退点幂等。

背景：leadgen_api 与 content_api 共写同一张 jobs 表，但 reaper 只在 content_api 里跑。
在补 CAS 之前，leadgen 的 worker 无条件 `UPDATE jobs SET status='done'`，会把 reaper
已判超时并退过点的任务覆写回 done —— 用户既拿到结果又拿回点数（线上 id=1170：耗时
3686s、refunded=1、status=done、result 完整）。异常分支的 add_points 也没有幂等保护，
与 reaper 并发时会双重退点。

本测试与 tests/test_job_refund_cas.py 同构，断言同一条不变量：
无论 reaper / worker 如何交错，点数最多退一次，且 error 终态不被后到的 done 覆盖。
"""
import importlib, os, sys, tempfile, time, unittest
from contextlib import closing
from pathlib import Path


class LeadgenJobCasTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.lg = importlib.import_module("leadgen_api")
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_jobdb = self.lg.JOB_DB
        self.lg.JOB_DB = os.path.join(self.tmp.name, "jobs.db")
        with closing(self.lg.jdb()) as c:
            c.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
                created_at INTEGER, updated_at INTEGER, deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0)""")
            c.commit()
        # 打桩 add_points，统计真正退点次数（不碰 users.db）。
        # 必须返回 True：_refund_once 现在按返回值判断退点是否成功，失败会回滚 refunded 标记。
        self.refunds = []
        self._orig_add_points = self.lg.add_points
        self.lg.add_points = lambda username, delta, reason="": (self.refunds.append((username, delta, reason)), True)[1]

    def tearDown(self):
        self.lg.JOB_DB = self._orig_jobdb
        self.lg.add_points = self._orig_add_points
        self.tmp.cleanup()

    def _insert(self, cost=6, status="running"):
        now = int(time.time())
        with closing(self.lg.jdb()) as c:
            cur = c.execute(
                "INSERT INTO jobs(kind,username,cost,status,payload,created_at,updated_at) "
                "VALUES('collect','u',?,?,'{}',?,?)", (cost, status, now, now))
            c.commit()
            return cur.lastrowid

    def _row(self, jid):
        with closing(self.lg.jdb()) as c:
            return c.execute("SELECT status, refunded, result FROM jobs WHERE id=?", (jid,)).fetchone()

    def _reaper_step(self, jid, cost=6):
        """content_api 的 reaper：CAS 抢 error，抢到才幂等退点。"""
        if self.lg._set_terminal(jid, "error", error="生成超时自动结束，已退点"):
            self.lg._refund_once(jid, "u", cost)

    # --- 核心回归：reaper 判超时退点在先，worker 随后成功 → 不得覆写 done、不得二次退点 ---
    def test_reaper_wins_then_worker_success_cannot_overwrite(self):
        jid = self._insert(6)
        self._reaper_step(jid)
        won = self.lg._set_terminal(jid, "done", result={"transcript": {"text": "x"}})
        self.assertFalse(won, "worker 不该抢到终态")
        row = self._row(jid)
        self.assertEqual(row["status"], "error")   # 线上 id=1170 就是这里被覆写成 done 的
        self.assertIsNone(row["result"])           # 结果不入库
        self.assertEqual(len(self.refunds), 1)
        # 退点要带 job 上下文，否则 points_audit 里这笔退款无法与任务配对（#18）
        self.assertEqual(self.refunds[0][2], "job#%d" % jid)

    # --- reaper 退点后 worker 又异常 → 仍只退一次（原 add_points 无幂等，会退两次）---
    def test_reaper_and_worker_both_error_refund_once(self):
        jid = self._insert(6)
        self._reaper_step(jid)
        if self.lg._set_terminal(jid, "error", error="boom"):
            self.lg._refund_once(jid, "u", 6)
        self.assertEqual(len(self.refunds), 1)

    # --- worker 正常成功：不退点，reaper 事后也抢不到 ---
    def test_worker_success_never_refunds(self):
        jid = self._insert(6)
        self.assertTrue(self.lg._set_terminal(jid, "done", result={"ok": 1}))
        self._reaper_step(jid)
        self.assertEqual(self._row(jid)["status"], "done")
        self.assertEqual(len(self.refunds), 0)

    # --- worker 正常失败：退且仅退一次 ---
    def test_worker_fail_refunds_once(self):
        jid = self._insert(6)
        self.assertTrue(self.lg._set_terminal(jid, "error", error="boom"))
        self.lg._refund_once(jid, "u", 6)
        self.lg._refund_once(jid, "u", 6)
        self.assertEqual(len(self.refunds), 1)
        self.assertEqual(self._row(jid)["refunded"], 1)

    # --- done 的 job 误调 _refund_once 也不退（status='error' 保险）---
    def test_done_job_not_refunded(self):
        jid = self._insert(6)
        self.assertTrue(self.lg._set_terminal(jid, "done", result={"ok": 1}))
        self.lg._refund_once(jid, "u", 6)
        self.assertEqual(len(self.refunds), 0)

    # --- cost<=0 不退点 ---
    def test_zero_cost_not_refunded(self):
        jid = self._insert(0)
        self.assertTrue(self.lg._set_terminal(jid, "error", error="boom"))
        self.lg._refund_once(jid, "u", 0)
        self.assertEqual(len(self.refunds), 0)

    # --- 回归：异常发生在任务转成 running 之前，仍必须退点，不能把 job 永久留在 pending ---
    def test_exception_before_running_still_refunds(self):
        jid = self._insert(6, status="pending")
        # 任务还在 pending 时就报错（模拟认领那句 UPDATE 自己抛异常）
        claimed = self.lg._set_terminal(jid, "error", error="db locked", from_states=("pending", "running"))
        self.assertTrue(claimed, "pending 态必须能被抢成 error，否则 reaper 也扫不到它")
        self.lg._refund_once(jid, "u", 6)
        self.assertEqual(self._row(jid)["status"], "error")
        self.assertEqual(len(self.refunds), 1, "预扣的点必须退回")

    def test_set_terminal_default_still_requires_running(self):
        """默认 from_states 只认 running —— reaper 与 worker 竞争的语义不能被放宽。"""
        jid = self._insert(6, status="pending")
        self.assertFalse(self.lg._set_terminal(jid, "done", result={"x": 1}))

    # --- 回归：退点失败必须回滚 refunded 标记，否则用户的点永久拿不回来 ---
    def test_refund_failure_rolls_back_refunded_flag(self):
        jid = self._insert(6)
        self.assertTrue(self.lg._set_terminal(jid, "error", error="boom"))
        self.lg.add_points = lambda u, d, reason="": False          # auth 挂了 + 直写也失败
        self.lg._refund_once(jid, "u", 6)
        self.assertEqual(self._row(jid)["refunded"], 0, "退点失败却留下 refunded=1，点数永久丢失")
        # 恢复后重试应能成功退一次
        self.lg.add_points = lambda u, d, reason="": (self.refunds.append((u, d, reason)), True)[1]
        self.lg._refund_once(jid, "u", 6)
        self.assertEqual(len(self.refunds), 1)
        self.assertEqual(self._row(jid)["refunded"], 1)

    # --- 端到端复现线上 id=1170：worker 跑到一半 reaper 判超时，worker 随后完成 ---
    def test_run_job_slow_success_after_reaper_timeout(self):
        import threading
        jid = self._insert(6, status="pending")
        started, release = threading.Event(), threading.Event()

        def _slow_handler(payload):
            started.set()
            release.wait(5)                       # 模拟耗时 3686s 的采集
            return {"transcript": {"text": "拿到了文案"}}

        orig = self.lg.HANDLERS
        self.lg.HANDLERS = {"collect": _slow_handler}
        try:
            t = threading.Thread(target=self.lg.run_job, args=(jid,))
            t.start()
            self.assertTrue(started.wait(5), "handler 未启动")
            self._reaper_step(jid)                # 任务仍 running 时 reaper 判超时并退点
            release.set()
            t.join(10)
        finally:
            self.lg.HANDLERS = orig

        row = self._row(jid)
        self.assertEqual(row["status"], "error")  # 修复前这里会是 done
        self.assertIsNone(row["result"])          # 修复前这里会有完整结果
        self.assertEqual(len(self.refunds), 1)    # 退点恰好一次


if __name__ == "__main__":
    unittest.main()
