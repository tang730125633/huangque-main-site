# -*- coding: utf-8 -*-
"""#511 jobs.owner 归属列 + 每用户并行 3 个生图。

背景：content(8096)/imggen(8101)/leadgen(8100) 三个进程共写同一张 jobs 表，但表里
没有「这条归谁」的列。于是 content 的两处全表扫描会把别家的任务当成自己的：

  1. _recover_pending_jobs  捞走 imggen 的 pending banana 任务 → 用 content 的 gpt
     处理器跑掉：用户付了 banana 的点数，拿到一张 gpt 的图。
  2. reclaim_orphaned_running  在 content 重启时，把 imggen/leadgen 正在飞的任务
     全判失败退点，而对面 worker 还在跑 → 用户既没图又白等。

以前 (1) 只有几毫秒的竞态窗口（imggen 插完立刻起线程抢 running），所以没爆。
但本次给 imggen 加了「单用户 3 个」运行闸后，超闸的 banana 任务会合法地在 pending
里躺几分钟 —— 窗口从毫秒变成分钟，content 会稳定地偷走它们。owner 列是加闸的前置条件。

并行闸的关键设计：闸计数数的是全表 kind='image'，content 和 imggen 都写这张表，
所以「每人 3 个生图」是跨两个服务统一的，不是各算各的 3 个（否则实际是 6 个）。
"""
import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

jobs_store = importlib.import_module("content_domains.jobs_store")

SCHEMA_WITHOUT_OWNER = """CREATE TABLE jobs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, username TEXT, cost INTEGER,
    status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
    created_at INTEGER, updated_at INTEGER, deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0)"""


class _DbFixture(unittest.TestCase):
    """一个临时 jobs 库，含 owner 列，直接插行来摆出跨服务场景。"""

    def setUp(self):
        self.core = importlib.import_module("content_domains.core")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "jobs.db")

        self._orig_core_db = self.core.JOB_DB
        self.core.JOB_DB = self.db
        self.addCleanup(lambda: setattr(self.core, "JOB_DB", self._orig_core_db))

        with closing(self.core.jdb()) as c:
            c.execute(SCHEMA_WITHOUT_OWNER)
            c.commit()
        jobs_store.ensure_owner_column(self.core.jdb)

    def insert(self, kind="image", owner="content", status="pending", username="u", cost=10, payload=None):
        now = int(time.time())
        with closing(self.core.jdb()) as c:
            cur = c.execute(
                """INSERT INTO jobs(kind,username,cost,status,payload,created_at,updated_at,owner)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (kind, username, cost, status, json.dumps(payload or {}), now, now, owner))
            c.commit()
            return cur.lastrowid

    def insert_legacy(self, kind="image", status="pending", username="u", cost=10):
        """owner 为 NULL 的历史行（建列之前就存在的任务）。"""
        now = int(time.time())
        with closing(self.core.jdb()) as c:
            cur = c.execute(
                """INSERT INTO jobs(kind,username,cost,status,payload,created_at,updated_at)
                   VALUES(?,?,?,?,'{}',?,?)""", (kind, username, cost, status, now, now))
            c.commit()
            return cur.lastrowid

    def status_of(self, job_id):
        with closing(self.core.jdb()) as c:
            return c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"]


class EnsureOwnerColumnTests(_DbFixture):
    def test_column_added_and_idempotent(self):
        with closing(self.core.jdb()) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        self.assertIn("owner", cols)
        jobs_store.ensure_owner_column(self.core.jdb)   # 再调一次不应抛（三个服务各调一次，谁先起谁建）
        jobs_store.ensure_owner_column(self.core.jdb)


class RecoverPendingOwnershipTests(_DbFixture):
    """content 的 pending 重排扫描只能捞自己的活。"""

    def setUp(self):
        super().setUp()
        self.enqueued = []
        self._orig = self.core.enqueue_job
        self.core.enqueue_job = lambda jid, kind, mode="": (self.enqueued.append(jid), True)[1]
        self.addCleanup(lambda: setattr(self.core, "enqueue_job", self._orig))

    def test_does_not_steal_imggen_pending_job(self):
        mine = self.insert(owner="content")
        theirs = self.insert(owner="imggen")     # 超闸留 pending 的 banana 任务
        self.core._recover_pending_jobs()
        self.assertIn(mine, self.enqueued)
        self.assertNotIn(theirs, self.enqueued,
                         "content 捞走了 imggen 的 pending 任务，会用 gpt 处理器跑掉 banana 的活")

    def test_does_not_steal_leadgen_pending_job(self):
        theirs = self.insert(kind="collect", owner="leadgen")
        self.core._recover_pending_jobs()
        self.assertNotIn(theirs, self.enqueued)

    def test_legacy_null_owner_still_treated_as_content(self):
        """建列之前的历史行 owner=NULL：那时只有 content 会留 pending，仍归 content，行为不变。"""
        legacy = self.insert_legacy()
        self.core._recover_pending_jobs()
        self.assertIn(legacy, self.enqueued)


class ReclaimOrphanedOwnershipTests(_DbFixture):
    """content 重启时的孤儿回收只能杀自己的在飞任务（#511）。"""

    def setUp(self):
        super().setUp()
        self.refunds = []
        outer = self

        class _FakePoints:
            @staticmethod
            def refund_points(username, amount, reason="", transaction_key=""):
                outer.refunds.append((username, amount, reason))

        self._orig_domains = self.core._domains
        self.core._domains = lambda: (None, _FakePoints, None)
        self.addCleanup(lambda: setattr(self.core, "_domains", self._orig_domains))

    def test_does_not_kill_other_services_running_jobs(self):
        mine = self.insert(owner="content", status="running")
        banana = self.insert(owner="imggen", status="running")
        collect = self.insert(kind="collect", owner="leadgen", status="running")

        n = self.core.reclaim_orphaned_running()

        self.assertEqual(n, 1)
        self.assertEqual(self.status_of(mine), "error")
        self.assertEqual(self.status_of(banana), "running",
                         "content 重启把 imggen 正在跑的任务判死了：对面 worker 还在跑，用户既没图又白等")
        self.assertEqual(self.status_of(collect), "running")
        self.assertEqual(len(self.refunds), 1, "只该退自己那条的点")

    def test_legacy_null_owner_still_reclaimed(self):
        legacy = self.insert_legacy(status="running")
        self.assertEqual(self.core.reclaim_orphaned_running(), 1)
        self.assertEqual(self.status_of(legacy), "error")


class UnifiedImageGateTests(_DbFixture):
    """「每人并行 3 个生图」必须跨 content + imggen 统一计数，不是各 3 个。"""

    def setUp(self):
        super().setUp()
        self.imggen = importlib.import_module("imggen_api")
        self._orig_imggen_db = self.imggen.JOB_DB
        self.imggen.JOB_DB = self.db
        self.addCleanup(lambda: setattr(self.imggen, "JOB_DB", self._orig_imggen_db))

    def test_both_services_count_the_same_running_image_jobs(self):
        self.insert(kind="image", owner="content", status="running", username="kongli")   # gpt
        self.insert(kind="image", owner="imggen", status="running", username="kongli")    # banana
        self.insert(kind="image", owner="imggen", status="running", username="someone")   # 别人的，不算
        self.insert(kind="video", owner="content", status="running", username="kongli")   # 非生图，不算
        self.insert(kind="image", owner="content", status="pending", username="kongli")   # 未运行，不算

        # 两个服务各自数，必须得出同一个数字 —— 这正是「3 个」跨服务统一的根据
        self.assertEqual(self.core._user_running_image_count("kongli"), 2)
        self.assertEqual(self.imggen._user_running_image_count("kongli"), 2)

    def test_gate_default_is_three(self):
        self.assertEqual(self.core.MAX_USER_RUNNING_IMAGE, 3)
        self.assertEqual(self.imggen.MAX_USER_RUNNING_IMAGE, 3)


class ImggenRunJobGateTests(_DbFixture):
    """超闸的 banana 任务必须原地留 pending（等重排），绝不能被跑掉、也不能被判死退点。"""

    def setUp(self):
        super().setUp()
        self.imggen = importlib.import_module("imggen_api")
        self._orig_db = self.imggen.JOB_DB
        self.imggen.JOB_DB = self.db
        self.addCleanup(lambda: setattr(self.imggen, "JOB_DB", self._orig_db))

        self.generated = []
        self._orig_gen = self.imggen.gen_banana
        self.imggen.gen_banana = lambda payload: (self.generated.append(payload), {"images": ["x.png"]})[1]
        self.addCleanup(lambda: setattr(self.imggen, "gen_banana", self._orig_gen))

        self._orig_recover = self.imggen._recover_pending_jobs
        self.imggen._recover_pending_jobs = lambda limit=None: 0   # 别在单测里真去入队
        self.addCleanup(lambda: setattr(self.imggen, "_recover_pending_jobs", self._orig_recover))

    def test_over_gate_job_stays_pending_and_is_not_run(self):
        for _ in range(self.imggen.MAX_USER_RUNNING_IMAGE):        # 已占满 3 个运行位
            self.insert(kind="image", owner="imggen", status="running", username="kongli")
        jid = self.insert(kind="image", owner="imggen", status="pending", username="kongli")

        self.imggen.run_job(jid)

        self.assertEqual(self.generated, [], "超闸的任务被跑掉了，单用户并发上限形同虚设")
        self.assertEqual(self.status_of(jid), "pending",
                         "超闸的任务必须留 pending 等重排；判死会白扣点，判 running 会超发")

    def test_under_gate_job_runs(self):
        self.insert(kind="image", owner="imggen", status="running", username="kongli")
        jid = self.insert(kind="image", owner="imggen", status="pending", username="kongli")

        self.imggen.run_job(jid)

        self.assertEqual(len(self.generated), 1)
        self.assertEqual(self.status_of(jid), "done")

    def test_other_users_running_jobs_do_not_block_me(self):
        for _ in range(5):
            self.insert(kind="image", owner="imggen", status="running", username="someone")
        jid = self.insert(kind="image", owner="imggen", status="pending", username="kongli")

        self.imggen.run_job(jid)

        self.assertEqual(self.status_of(jid), "done", "闸必须是「单用户」的，别人的在飞任务不该挡住我")


if __name__ == "__main__":
    unittest.main()
