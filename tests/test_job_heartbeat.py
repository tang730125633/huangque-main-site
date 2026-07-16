# -*- coding: utf-8 -*-
"""任务心跳：让 reaper 的信号是【真的】。

## 线上：110 条任务被 reaper 误判「生成超时」，用户白等 2655 分钟（44 小时）

    被 reaper 判超时（近30天）：video 45、xiaole_video 25、image 17、collect 7、tryon 3

## 根因：reaper 判的是「多久没心跳」，而我们在长操作期间【根本不发心跳】

    * HeyGen 轮询       最长 900s，整个循环里一次 UPDATE 都没有
    * 烧字幕            whisper 跑 CPU，几分钟
    * 生图的 HTTP 调用、成片下载

于是 reaper 看到「这么久没动静」，就当 worker 死了，把【还在正常干活】的任务杀掉退点。
用户等了 20 多分钟，看到一句「生成超时自动结束，已退点」。

## 修法不是把 grace 调宽

调宽只是让误杀【晚一点】发生 —— 信号本身还是假的。正确的做法是让 worker 真的发心跳：
任务跑着的时候每 30 秒刷一次 jobs.updated_at。这样「没心跳」才真的等于「worker 死了」。

## ⚠️ 心跳只证明 worker 活着，不证明任务会成功

任务的时间上限仍然由各引擎自己的死线兜住（VIDEO_GEN_DEADLINE / WS_DEADLINE / ...）——
那些到点会抛一个说得清的错。**别因为有了心跳就把死线删了**，否则一个真的卡死的上游会让
任务永远挂着，reaper 也不会来救（因为心跳还在跳）。
"""
import importlib
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

os.environ.setdefault("CONTENT_BASE", tempfile.mkdtemp())
os.environ["CONTENT_JOB_HEARTBEAT"] = "1"          # 1 秒一跳，测试里等得起
core = importlib.import_module("content_domains.core")
importlib.reload(core)
CORE_SRC = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")


def _new_job(kind="video"):
    core.init_db()
    now = int(time.time())
    with sqlite3.connect(core.JOB_DB) as db:
        db.execute("DELETE FROM jobs")
        db.execute("INSERT INTO jobs(kind,username,cost,payload,status,created_at,updated_at,owner)"
                   " VALUES(?,'u',20,'{}','pending',?,?,?)", (kind, now, now, core.SERVICE_OWNER))
        return db.execute("SELECT id FROM jobs").fetchone()[0]


def _peek(jid):
    with sqlite3.connect(core.JOB_DB) as db:
        return db.execute("SELECT updated_at, status FROM jobs WHERE id=?", (jid,)).fetchone()


class TheHeartbeatActuallyBeatsTests(unittest.TestCase):
    def test_a_silent_long_job_keeps_updated_at_fresh(self):
        """handler 里跑一个长操作、全程【零阶段更新】—— 正是 HeyGen 轮询/烧字幕的样子。
        没有心跳的话，jobs.updated_at 会一直停在开跑那一刻，reaper 就来杀它了。"""
        jid = _new_job()
        core.HANDLERS = {"video": lambda p: (time.sleep(3), {"ok": True})[1]}
        threading.Thread(target=core.run_job, args=(jid,), daemon=True).start()

        time.sleep(2.5)
        updated, status = _peek(jid)
        self.assertEqual(status, "running")
        stale = int(time.time()) - updated
        self.assertLessEqual(stale, 2, "updated_at 已经 %ds 没刷了 —— reaper 会把它当死的杀掉" % stale)

    def test_it_stops_when_the_job_ends(self):
        """⚠️ 不停掉的话：每跑一个任务泄漏一个线程，而且它会一直把【已终态】的任务刷成「活着」。"""
        jid = _new_job()
        core.HANDLERS = {"video": lambda p: {"ok": True}}
        core.run_job(jid)
        time.sleep(2.5)
        self.assertFalse(any("job-heartbeat" in t.name for t in threading.enumerate()),
                         "心跳线程泄漏了")

    def test_it_only_starts_after_the_job_is_claimed(self):
        """认领（CAS 抢 running）之前的几个 return 都还没占住任务 —— 不该有心跳。"""
        block = CORE_SRC.split("def run_job")[1].split("\ndef ")[0]
        i_claim = block.index("jobs_store.claim_running")
        i_beat = block.index("stop_heartbeat = _start_job_heartbeat")
        self.assertLess(i_claim, i_beat, "心跳必须在 CAS 认领【之后】才开")

    def test_it_is_stopped_in_a_finally(self):
        """任务抛异常时也必须停 —— 否则失败一次就泄漏一个线程。"""
        block = CORE_SRC.split("def run_job")[1].split("\ndef ")[0]
        tail = block[block.index("    finally:"):]
        self.assertIn("stop_heartbeat()", tail)

    def test_a_failing_heartbeat_never_breaks_the_job(self):
        """心跳是【辅助】—— 它自己写库失败，不该把任务弄挂。"""
        block = CORE_SRC.split("def _start_job_heartbeat")[1].split("\ndef ")[0]
        self.assertIn("except Exception:", block)
        self.assertIn("pass", block)


class TheReaperDefaultIsNotZeroTests(unittest.TestCase):
    """⚠️ 一个潜伏雷：`KIND_GRACE.get(kind, 0)` —— 默认值 0 在 reaper 里的语义是【立刻杀】。

        grace = KIND_GRACE.get(r["kind"], 0)
        if grace and r["updated_at"] >= now - grace:   # grace=0 是假值 → 不 continue
            continue
        # → 直接按 360s 的 cutoff 判失败

    也就是说：一个新 kind 忘了在 KIND_GRACE 里登记，它就只有 6 分钟寿命。
    audio / copy / leads / dl 至今都不在表里 —— 只是它们跑得快、够不着 6 分钟才没出事。
    """

    def test_an_unregistered_kind_is_not_insta_killed(self):
        self.assertNotIn("KIND_GRACE.get(r[\"kind\"], 0)", CORE_SRC,
                         "默认 grace 还是 0 —— 新 kind 只有 6 分钟寿命")
        self.assertIn("KIND_GRACE.get(r[\"kind\"], KIND_GRACE_DEFAULT)", CORE_SRC)

    def test_the_default_is_generous(self):
        self.assertGreaterEqual(core.KIND_GRACE_DEFAULT, 600)

    def test_the_kinds_that_are_still_missing_would_survive(self):
        """audio/copy/leads/dl 至今没登记 —— 现在至少不会 6 分钟就被杀。"""
        for kind in ("audio", "copy", "leads", "dl"):
            grace = core.KIND_GRACE.get(kind, core.KIND_GRACE_DEFAULT)
            self.assertGreaterEqual(grace, 600, "%s 的 grace 只有 %ds" % (kind, grace))


class TheEngineDeadlinesMustSurviveTests(unittest.TestCase):
    """⚠️ 心跳只证明【worker 活着】，不证明【任务会成功】。

    别因为有了心跳就把引擎死线删了 —— 否则一个真的卡死的上游会让任务永远挂着，
    而 reaper 也不会来救（因为心跳还在跳）。
    """

    def test_the_video_deadline_is_still_there(self):
        self.assertGreater(core.VIDEO_GEN_DEADLINE, 0)

    def test_the_reaper_still_exists_as_a_backstop(self):
        self.assertIn("def reaper():", CORE_SRC)


if __name__ == "__main__":
    unittest.main()
