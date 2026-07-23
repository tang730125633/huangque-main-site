# -*- coding: utf-8 -*-
"""优雅停机：部署别再杀掉在飞的任务。

## 线上：53 条任务死于「服务重启中断，已退点，请重新提交」（近 14 天，涉及 8 个功能）

每次上线，正在生成的任务全部被判失败。用户等了几分钟，什么都没拿到。

## 根因：注释写着「优雅停机」，实际一个 signal handler 都没有

    hardening.conf:  KillMode=mixed
                     TimeoutStopSec=15      ← 只给 15 秒

而视频任务要跑 5~15 分钟。systemd 发 SIGTERM，进程【根本不理】，15 秒后 SIGKILL，
在飞任务全部猝死；下次启动 reclaim_orphaned_running() 把它们判失败退点。

## 优雅停机 = 两件事，缺一不可

    1. 代码：收到 SIGTERM → 停止收新提交 → 等在飞任务跑完 → 退出
    2. systemd：TimeoutStopSec 给够 —— 代码写得再好，systemd 15 秒后照样 SIGKILL

**两者必须配套。** 只改一个，另一个就成了新的天花板 —— 这是这次最容易漏的一环。
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

os.environ.setdefault("CONTENT_BASE", tempfile.mkdtemp())
import importlib  # noqa: E402
core = importlib.import_module("content_domains.core")

CORE_SRC = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
ENTRY_SRC = (ROOT / "server/content_api.py").read_text(encoding="utf-8")
HARDENING = (ROOT / "deploy/systemd/huangque-content.service.d/hardening.conf").read_text(encoding="utf-8")


class SystemdMustGiveItTimeTests(unittest.TestCase):
    """⚠️ 最容易漏的一环。代码里的 drain 写得再好，systemd 15 秒后照样 SIGKILL。"""

    def _systemd_timeout(self):
        m = re.search(r"^TimeoutStopSec=(\d+)", HARDENING, re.M)
        self.assertIsNotNone(m, "hardening.conf 里没有 TimeoutStopSec")
        return int(m.group(1))

    def test_the_15_second_timeout_is_gone(self):
        self.assertNotEqual(self._systemd_timeout(), 15,
                            "还是 15 秒 —— 视频要跑 5~15 分钟，在飞任务照样被 SIGKILL")

    def test_systemd_waits_longer_than_the_code_drains(self):
        """两者必须配套：systemd 的上限必须【大于】代码的排空窗口，
        否则 systemd 会在排空到一半时把进程砍了。"""
        self.assertGreater(self._systemd_timeout(), core.DRAIN_TIMEOUT,
                           "systemd 的 TimeoutStopSec(%ds) 不够代码排空(%ds)用"
                           % (self._systemd_timeout(), core.DRAIN_TIMEOUT))

    def test_the_drain_window_covers_the_longest_job(self):
        """最长的一档是视频：VIDEO_GEN_DEADLINE=900s，加上传下载。"""
        self.assertGreaterEqual(core.DRAIN_TIMEOUT, core.VIDEO_GEN_DEADLINE)


class TheCodeActuallyHandlesSigtermTests(unittest.TestCase):
    def test_signal_handlers_are_installed_at_startup(self):
        """原来【一个都没装】—— 注释写着「优雅停机」，代码里根本没有。"""
        self.assertIn("core.install_signal_handlers()", ENTRY_SRC)
        block = CORE_SRC.split("def install_signal_handlers")[1].split("\ndef ")[0]
        self.assertIn("signal.SIGTERM", block)
        self.assertIn("signal.SIGINT", block)

    def test_the_drain_wait_is_off_the_main_thread(self):
        """⚠️ 信号处理器跑在【主线程】。等待循环若在这里跑，serve_forever 的 accept 就停摆，
        排空那几分钟里【形象/资产等读接口全部拒连】（2026-07-15 事故：卡住的任务把排空拖满
        ~19 分钟，整个 content API 下线）。等待必须丢到后台线程，且排空期间【绝不 shutdown
        HTTP 服务】—— 读接口照常，do_POST 靠 is_shutting_down() 拒新提交即可。"""
        handler = CORE_SRC.split("def drain_and_exit")[1].split("\ndef ")[0]
        self.assertIn("threading.Thread(target=_drain_then_exit", handler,
                      "等待没丢到后台线程 —— 会阻塞主线程 accept，读接口下线")
        self.assertNotIn("while ", handler, "等待循环不能在信号处理器(主线程)里跑")
        self.assertNotIn(".shutdown(", handler, "排空期间不能关 HTTP 服务，否则读接口全挂")
        drainer = CORE_SRC.split("def _drain_then_exit")[1].split("\ndef ")[0]
        self.assertNotIn(".shutdown(", drainer, "后台排空也不该关 HTTP —— 进程退出时端口自然释放")

    def test_a_second_signal_exits_immediately(self):
        """急着回滚的时候，得有办法不等 —— 再发一次 SIGTERM 就立刻退。"""
        block = CORE_SRC.split("def drain_and_exit")[1].split("\ndef ")[0]
        self.assertIn("if _shutting_down.is_set():", block)
        self.assertIn("os._exit(1)", block)


class NoNewJobsWhileDrainingTests(unittest.TestCase):
    def test_submits_are_rejected_before_the_deduction(self):
        """⚠️ 拦在扣点之后等于没拦：用户被扣了点、任务入了队，进程下一秒就退了 ——
        又是一条「服务重启中断」。"""
        block = CORE_SRC.split('if p.startswith("/api/gen/") and p[9:] in HANDLERS:')[1]
        block = block.split("\n    def do_GET", 1)[0]
        i_guard = block.index("if is_shutting_down():")
        i_cost = block.index("points_domain.cost_of")
        i_deduct = block.index("points_domain.deduct_points")
        self.assertLess(i_guard, i_cost, "停机拦截必须在算点数之前")
        self.assertLess(i_guard, i_deduct, "停机拦截必须在扣点之前")

    def test_it_says_so_in_plain_words(self):
        """用户看到的是人话，而且得知道【没扣他的点】。"""
        self.assertIn('"code": "shutting_down"', CORE_SRC)
        self.assertIn("服务正在更新，请稍等几秒后重试（未扣点）", CORE_SRC)


class WorkersDrainInsteadOfBlockingTests(unittest.TestCase):
    def test_the_worker_loop_can_notice_the_shutdown(self):
        """原来是 q.get()【无限阻塞】—— 停机时 worker 永远卡在那里，排空检测不到队列已空。
        改成带超时地取。"""
        block = CORE_SRC.split("def _job_worker_loop")[1].split("\ndef ")[0]
        self.assertIn("q.get(timeout=", block)
        self.assertIn("if _shutting_down.is_set():", block)
        self.assertNotIn("job_id = q.get()", block)

    def test_inflight_is_counted(self):
        """排空要等的是【正在跑的】，不只是队列里排队的。"""
        block = CORE_SRC.split("def _job_worker_loop")[1].split("\ndef ")[0]
        self.assertIn("_inflight += 1", block)
        self.assertIn("_inflight -= 1", block)
        drain = CORE_SRC.split("def _drain_then_exit")[1].split("\ndef ")[0]
        self.assertIn("_inflight", drain)
        self.assertIn("qsize()", drain)

    def test_every_queue_is_drained(self):
        """漏掉一个队列，那个队列里的任务就会被丢掉 —— 而它是 6 个池之一。"""
        names = ast.literal_eval(
            "(" + CORE_SRC.split("_ALL_JOB_QUEUES = (")[1].split(")")[0].replace(",\n", ",") + ",)"
        ) if False else None  # 直接数：源码里那个元组必须包含全部 6 个队列
        block = CORE_SRC.split("_ALL_JOB_QUEUES = (")[1].split(")")[0]
        for q in ("_job_queue", "_fast_job_queue", "_talking_job_queue",
                  "_image_job_queue", "_cinematic_job_queue", "_avatar_job_queue"):
            self.assertIn(q, block, "_ALL_JOB_QUEUES 漏了 %s —— 那个池的任务会被丢掉" % q)


class ReclaimBecomesABackstopTests(unittest.TestCase):
    def test_the_orphan_reclaim_is_still_there(self):
        """drain 之后它应该【一条都收不到】—— 收到就说明上次是崩溃/被 SIGKILL 了。
        它现在是【兜底】，不再是常态。别因为有了 drain 就把它删了。"""
        self.assertIn("core.reclaim_orphaned_running()", ENTRY_SRC)
        self.assertIn("兜底", ENTRY_SRC)


if __name__ == "__main__":
    unittest.main()
