# -*- coding: utf-8 -*-
"""imggen_api（Nano Banana 作图，:8101）的 job 状态机 CAS + 退点幂等。

与 leadgen_api 同一类缺陷：imggen_api 与 content_api 共写同一张 jobs 表，reaper 只在
content_api 里跑，而 imggen 的 worker 原本无条件 `UPDATE jobs SET status='done'`，
会把 reaper 判超时并退过点的任务覆写回 done —— 用户既拿到图又拿回点数。
线上 content_jobs.db 里 kind='image' 有 10 条 status='done' 且 refunded=1 的记录。

断言与 tests/test_job_refund_cas.py、tests/test_leadgen_job_cas.py 一致。
"""
import importlib, os, sys, tempfile, time, unittest
from contextlib import closing
from pathlib import Path

# imggen_api.py 在【模块导入时】就 OUT_DIR.mkdir()，而它的默认值硬编码成
# /home/ubuntu/content-api/content_out。CI runner 上建不了 → PermissionError: '/home/ubuntu'。
# 本地 Windows 反而"成功"（当成相对路径在当前盘建目录），所以本地全绿、CI 全红。
# 必须在 import imggen_api 之前把 CONTENT_OUT 指走。
os.environ.setdefault("CONTENT_OUT", tempfile.mkdtemp(prefix="hq-imggen-out-"))


class ImggenJobCasTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.m = importlib.import_module("imggen_api")
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_jobdb = self.m.JOB_DB
        self.m.JOB_DB = os.path.join(self.tmp.name, "jobs.db")
        with closing(self.m.jdb()) as c:
            c.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
                created_at INTEGER, updated_at INTEGER, deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0)""")
            c.commit()
        # 打桩 refund_points（不打 auth 服务），统计真正退点次数
        self.refunds = []
        self._orig_refund = self.m.refund_points
        self.m.refund_points = lambda username, amount, reason="": (self.refunds.append((username, amount, reason)), (200, {}))[1]

    def tearDown(self):
        self.m.JOB_DB = self._orig_jobdb
        self.m.refund_points = self._orig_refund
        self.tmp.cleanup()

    def _insert(self, cost=14, status="running"):
        now = int(time.time())
        with closing(self.m.jdb()) as c:
            cur = c.execute(
                "INSERT INTO jobs(kind,username,cost,status,payload,created_at,updated_at) "
                "VALUES('image','u',?,?,'{}',?,?)", (cost, status, now, now))
            c.commit()
            return cur.lastrowid

    def _row(self, jid):
        with closing(self.m.jdb()) as c:
            return c.execute("SELECT status, refunded, result FROM jobs WHERE id=?", (jid,)).fetchone()

    def _reaper_step(self, jid, cost=14):
        if self.m._set_terminal(jid, "error", error="生成超时自动结束，已退点"):
            self.m._refund_once(jid, "u", cost)

    def test_reaper_wins_then_worker_success_cannot_overwrite(self):
        jid = self._insert(14)
        self._reaper_step(jid)
        self.assertFalse(self.m._set_terminal(jid, "done", result={"url": "x.png"}))
        row = self._row(jid)
        self.assertEqual(row["status"], "error")
        self.assertIsNone(row["result"])
        self.assertEqual(len(self.refunds), 1)
        # 退点要带 job 上下文，否则 points_audit 里这笔退款无法与任务配对（#18）
        self.assertEqual(self.refunds[0][2], "job#%d" % jid)

    def test_reaper_and_worker_both_error_refund_once(self):
        jid = self._insert(14)
        self._reaper_step(jid)
        if self.m._set_terminal(jid, "error", error="boom"):
            self.m._refund_once(jid, "u", 14)
        self.assertEqual(len(self.refunds), 1)

    def test_worker_success_never_refunds(self):
        jid = self._insert(14)
        self.assertTrue(self.m._set_terminal(jid, "done", result={"ok": 1}))
        self._reaper_step(jid)
        self.assertEqual(self._row(jid)["status"], "done")
        self.assertEqual(len(self.refunds), 0)

    def test_refund_once_idempotent(self):
        jid = self._insert(14)
        self.assertTrue(self.m._set_terminal(jid, "error", error="boom"))
        self.m._refund_once(jid, "u", 14)
        self.m._refund_once(jid, "u", 14)
        self.assertEqual(len(self.refunds), 1)
        self.assertEqual(self._row(jid)["refunded"], 1)

    def test_done_job_not_refunded(self):
        jid = self._insert(14)
        self.assertTrue(self.m._set_terminal(jid, "done", result={"ok": 1}))
        self.m._refund_once(jid, "u", 14)
        self.assertEqual(len(self.refunds), 0)

    # --- 回归：异常发生在转成 running 之前，仍必须退点（否则 job 永久卡 pending，reaper 也扫不到）---
    def test_exception_before_running_still_refunds(self):
        jid = self._insert(14, status="pending")
        self.assertTrue(self.m._set_terminal(jid, "error", error="db locked",
                                             from_states=("pending", "running")))
        self.m._refund_once(jid, "u", 14)
        self.assertEqual(self._row(jid)["status"], "error")
        self.assertEqual(len(self.refunds), 1)

    def test_set_terminal_default_still_requires_running(self):
        jid = self._insert(14, status="pending")
        self.assertFalse(self.m._set_terminal(jid, "done", result={"x": 1}))

    # --- 回归：退点失败必须回滚 refunded，否则点数永久丢失（imggen 没有直写兜底）---
    def test_refund_failure_rolls_back_refunded_flag(self):
        jid = self._insert(14)
        self.assertTrue(self.m._set_terminal(jid, "error", error="boom"))
        self.m.refund_points = lambda u, a, reason="": (503, {"detail": "auth 重启中"})
        self.m._refund_once(jid, "u", 14)
        self.assertEqual(self._row(jid)["refunded"], 0, "退点失败却留下 refunded=1，点数永久丢失")
        self.m.refund_points = lambda u, a, reason="": (self.refunds.append((u, a, reason)), (200, {}))[1]
        self.m._refund_once(jid, "u", 14)
        self.assertEqual(len(self.refunds), 1)

    # --- 端到端：gen_banana 跑到一半 reaper 判超时，随后完成 → 不覆写、不二次退点 ---
    def test_run_job_slow_success_after_reaper_timeout(self):
        import threading
        jid = self._insert(14, status="pending")
        started, release = threading.Event(), threading.Event()

        def _slow_gen(payload):
            started.set()
            release.wait(5)
            return {"type": "image", "url": "generated.png"}

        orig = self.m.gen_banana
        self.m.gen_banana = _slow_gen
        try:
            t = threading.Thread(target=self.m.run_job, args=(jid,))
            t.start()
            self.assertTrue(started.wait(5), "gen_banana 未启动")
            self._reaper_step(jid)
            release.set()
            t.join(10)
        finally:
            self.m.gen_banana = orig

        row = self._row(jid)
        self.assertEqual(row["status"], "error")  # 修复前是 done
        self.assertIsNone(row["result"])
        self.assertEqual(len(self.refunds), 1)


if __name__ == "__main__":
    unittest.main()
