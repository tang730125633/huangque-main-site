# -*- coding: utf-8 -*-
"""上游额度熔断器：上游没钱了，就别再让用户提交了。

## 线上：48 条任务死于「上游账户没钱」

    "视频接口失败: HTTP 400 {"code":400,"message":"积分余额不足，请先充值"}"       × 25
    "WaveSpeed接口失败: HTTP 400 {"code":400,"message":"Insufficient credits..."}"  × 23

余额哨兵（balance_sentinel.py）每 10 分钟查一次余额、低于阈值就往飞书报警 —— 它确实报了。
**但告警只叫醒了我们，拦不住用户。** 从「余额见底」到「有人充上钱」这段时间里，用户照样
点生成、照样被扣点、照样等几分钟，然后看到一句天书。

## 熔断的信号来自上游【自己的拒绝】，不是猜余额

很多渠道根本没有余额 API（果肉、泽龙）。所以不去查余额，而是看它们刚刚是不是在因为没钱
而拒绝我们：某个功能最近 30 分钟内有 ≥2 条「余额不足」的失败、且期间没有任何一条成功
→ 熔断。一旦有一条成功（充上钱了），自动解除。

## ⚠️ 三条红线

1. **必须在扣点之前拦** —— 否则用户还是先掉点、再被拒。
2. **必须 fail-open** —— 熔断器自己出问题，一律放行。绝不能因为一个监控组件把整站堵死。
3. **不能连坐** —— 果肉没钱，不该把同一个 kind 下的欧米/豆姐一起熔断。
"""
import importlib
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

os.environ.setdefault("CONTENT_BASE", tempfile.mkdtemp())
core = importlib.import_module("content_domains.core")
guard = importlib.import_module("content_domains.upstream_guard")
CORE_SRC = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")

# 线上真实的报错原文 —— 别用编的，各家措辞完全不同
XIAOLE_NO_MONEY = '视频接口失败: HTTP 400 {"code":400,"message":"积分余额不足，请先充值","data":null}'
WAVESPEED_NO_MONEY = ('WaveSpeed接口失败: HTTP 400 {"code":400,"message":"Insufficient credits. '
                      'Please top up your account to continue."}')
RUNNINGHUB_NO_MONEY = "[812] 企业版余额不足，请充值"
NOT_MONEY = "HeyGen视频生成超时"
# 上游是聚合中转，渠道动态上下线 —— 这是它「当前没有支持这个比例的渠道」的说法
NO_CHANNEL = "该视频渠道当前仅部分比例可用，请优先尝试 16:9（横屏）"
NO_CHANNEL2 = '视频接口失败: HTTP 404 {"code":404,"message":"当前模型暂无支持该视频参数的可用渠道"}'


def _seed(jobs):
    """jobs: [(kind, payload_json, status, error, age_seconds)]"""
    with sqlite3.connect(core.JOB_DB) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY, kind TEXT, username TEXT, cost INT, payload TEXT,
            status TEXT, error TEXT, result TEXT, created_at INT, updated_at INT, owner TEXT)""")
        db.execute("DELETE FROM jobs")
        now = int(time.time())
        for i, (kind, payload, status, error, age) in enumerate(jobs):
            db.execute("INSERT INTO jobs(id,kind,payload,status,error,created_at,updated_at)"
                       " VALUES(?,?,?,?,?,?,?)",
                       (i + 1, kind, payload, status, error, now - age, now - age))


class ItTripsOnRealUpstreamRejectionsTests(unittest.TestCase):
    def test_xiaole_out_of_money(self):
        _seed([("xiaole_video", '{"channel":"grok"}', "error", XIAOLE_NO_MONEY, 300)] * 2)
        r = guard.exhausted_reason("xiaole_video", {"channel": "grok"})
        self.assertIsNotNone(r)
        self.assertIn("果肉视频生成", r)
        self.assertIn("未扣点", r, "得让用户知道这次没扣他的钱")

    def test_wavespeed_out_of_money(self):
        _seed([("video", '{"mode":"motion"}', "error", WAVESPEED_NO_MONEY, 300)] * 2)
        self.assertIsNotNone(guard.exhausted_reason("video", {"mode": "motion"}))

    def test_runninghub_out_of_money(self):
        _seed([("tryon", "{}", "error", RUNNINGHUB_NO_MONEY, 300)] * 2)
        self.assertIsNotNone(guard.exhausted_reason("tryon", {}))

    def test_one_hit_is_not_enough(self):
        """一次偶发的 400 不该把整个功能熔断。"""
        _seed([("xiaole_video", '{"channel":"grok"}', "error", XIAOLE_NO_MONEY, 300)])
        self.assertIsNone(guard.exhausted_reason("xiaole_video", {"channel": "grok"}))

    def test_other_failures_do_not_trip_it(self):
        """超时、审核拦截…… 这些不是没钱，熔断了只会让用户更用不了。"""
        _seed([("video", '{"mode":"motion"}', "error", NOT_MONEY, 300)] * 5)
        self.assertIsNone(guard.exhausted_reason("video", {"mode": "motion"}))


class ItClearsItselfTests(unittest.TestCase):
    def test_a_success_lifts_the_breaker(self):
        """充上钱之后自动解除 —— 不需要人工干预，也不需要重启。"""
        _seed([
            ("xiaole_video", '{"channel":"grok"}', "done", None, 60),      # 最新：成功了
            ("xiaole_video", '{"channel":"grok"}', "error", XIAOLE_NO_MONEY, 300),
            ("xiaole_video", '{"channel":"grok"}', "error", XIAOLE_NO_MONEY, 400),
        ])
        self.assertIsNone(guard.exhausted_reason("xiaole_video", {"channel": "grok"}))

    def test_old_incidents_are_ignored(self):
        """30 分钟前的事故，钱多半早充上了。再拿它熔断就是误伤。"""
        _seed([("xiaole_video", '{"channel":"grok"}', "error", XIAOLE_NO_MONEY, 3 * 3600)] * 3)
        self.assertIsNone(guard.exhausted_reason("xiaole_video", {"channel": "grok"}))


class ItDoesNotPunishTheInnocentTests(unittest.TestCase):
    def test_one_channel_running_dry_does_not_block_its_siblings(self):
        """⚠️ 果肉/豆姐/欧米的 kind 都是 xiaole_video，但它们是【三家不同的上游】。
        果肉没钱，不该把欧米一起熔断。"""
        _seed([
            ("xiaole_video", '{"channel":"grok"}', "error", XIAOLE_NO_MONEY, 300),
            ("xiaole_video", '{"channel":"grok"}', "error", XIAOLE_NO_MONEY, 400),
        ])
        self.assertIsNotNone(guard.exhausted_reason("xiaole_video", {"channel": "grok"}))
        self.assertIsNone(guard.exhausted_reason("xiaole_video", {"channel": "omni"}))
        self.assertIsNone(guard.exhausted_reason("xiaole_video", {"channel": "micro"}))

    def test_the_granularity_is_the_user_facing_function(self):
        """熔断粒度 = 用户看到的功能名，和运营后台的日志/统计用同一份映射（func_names）。"""
        self.assertEqual(guard._func_key("xiaole_video", {"channel": "micro"}), "豆姐视频生成")
        self.assertEqual(guard._func_key("image", {"provider": "zelong2"}), "作图 · 泽龙2生图")


class FailOpenTests(unittest.TestCase):
    def test_a_broken_guard_never_blocks_anyone(self):
        """⚠️ 这是最要紧的一条。熔断器是个监控性质的组件 —— 它自己挂了，
        绝不能把整站的生成一起堵死。那比它想防的问题严重得多。"""
        with patch.object(guard, "jdb", side_effect=RuntimeError("库挂了")):
            self.assertIsNone(guard.exhausted_reason("xiaole_video", {"channel": "grok"}))

    def test_a_missing_table_never_blocks_anyone(self):
        with patch.object(guard, "jdb", side_effect=sqlite3.OperationalError("no such table: jobs")):
            self.assertIsNone(guard.exhausted_reason("video", {"mode": "motion"}))


class ItRunsBeforeTheDeductionTests(unittest.TestCase):
    """⚠️ 拦在扣点【之后】等于没拦 —— 用户照样先看到点数掉了。"""

    def test_the_guard_is_wired_before_cost_of_and_deduct(self):
        block = CORE_SRC.split("if p.startswith(\"/api/gen/\") and p[9:] in HANDLERS:")[1][:8000]
        i_guard = block.index("upstream_guard.exhausted_reason")
        i_cost = block.index("points_domain.cost_of")
        i_deduct = block.index("deduct_points")
        self.assertLess(i_guard, i_cost, "熔断必须在算点数之前")
        self.assertLess(i_guard, i_deduct, "熔断必须在扣点之前")

    def test_it_returns_503_not_500(self):
        """503 = 暂时不可用（等会儿再来），不是 500（我们的代码炸了）。
        前端会把 detail 原样显示给用户，所以那句话必须是人话。"""
        self.assertIn('self._send(503, {"detail": blocked, "code": "upstream_exhausted"', CORE_SRC)


class RatioUnavailableTests(unittest.TestCase):
    """「这个比例当前没有可用渠道」—— 113 条失败。

    ⚠️ 这【不是】一个静态的支持矩阵。别去前端写死「果肉只支持 16:9」——
    线上证据：grok + 9:16 + 720p 有 5 成 0 败，而另一批 grok 9:16 却 27 条全挂。
    同一个比例，不同时间，结果不同。写死矩阵会把本来能用的组合也禁掉。
    """

    def test_a_dead_ratio_is_blocked(self):
        _seed([("xiaole_video", '{"channel":"grok","ratio":"9:16"}', "error", NO_CHANNEL, 300)] * 2)
        r = guard.exhausted_reason("xiaole_video", {"channel": "grok", "ratio": "9:16"})
        self.assertIsNotNone(r)
        self.assertIn("9:16", r)
        self.assertIn("未扣点", r)

    def test_the_other_ratios_of_the_same_channel_still_work(self):
        """果肉的 9:16 没渠道，不代表 16:9 也没有 —— 别把用户唯一能用的比例也禁了。"""
        _seed([("xiaole_video", '{"channel":"grok","ratio":"9:16"}', "error", NO_CHANNEL, 300)] * 2)
        self.assertIsNone(guard.exhausted_reason("xiaole_video", {"channel": "grok", "ratio": "16:9"}))

    def test_a_success_on_the_same_ratio_lifts_it(self):
        _seed([
            ("xiaole_video", '{"channel":"grok","ratio":"9:16"}', "done", None, 60),
            ("xiaole_video", '{"channel":"grok","ratio":"9:16"}', "error", NO_CHANNEL, 300),
            ("xiaole_video", '{"channel":"grok","ratio":"9:16"}', "error", NO_CHANNEL2, 400),
        ])
        self.assertIsNone(guard.exhausted_reason("xiaole_video", {"channel": "grok", "ratio": "9:16"}))

    def test_a_success_on_a_DIFFERENT_ratio_does_NOT_lift_it(self):
        """⚠️ 16:9 成功了，完全不代表 9:16 也有渠道了 —— 两件事。"""
        _seed([
            ("xiaole_video", '{"channel":"grok","ratio":"16:9"}', "done", None, 60),
            ("xiaole_video", '{"channel":"grok","ratio":"9:16"}', "error", NO_CHANNEL, 300),
            ("xiaole_video", '{"channel":"grok","ratio":"9:16"}', "error", NO_CHANNEL2, 400),
        ])
        self.assertIsNotNone(guard.exhausted_reason("xiaole_video", {"channel": "grok", "ratio": "9:16"}))


class TheTwoBreakersClearDifferentlyTests(unittest.TestCase):
    """⚠️ 两个熔断的【解除条件不一样】—— 混为一谈就会漏放或误拦。

    余额：任意一条成功就证明账户有钱了（不管它是什么比例）
    比例：只有【同比例】的成功才证明这个比例的渠道活了
    """

    def test_any_success_clears_the_money_breaker_even_on_another_ratio(self):
        _seed([
            ("xiaole_video", '{"channel":"grok","ratio":"16:9"}', "done", None, 60),   # 别的比例成功了
            ("xiaole_video", '{"channel":"grok","ratio":"9:16"}', "error", XIAOLE_NO_MONEY, 300),
            ("xiaole_video", '{"channel":"grok","ratio":"9:16"}', "error", XIAOLE_NO_MONEY, 400),
        ])
        # 账户显然有钱（16:9 刚跑成功）→ 不该再报「余额已用尽」
        r = guard.exhausted_reason("xiaole_video", {"channel": "grok", "ratio": "9:16"})
        if r is not None:
            self.assertNotIn("额度已用尽", r, "别的比例刚成功，说明账户有钱 —— 余额熔断必须解除")


if __name__ == "__main__":
    unittest.main()
