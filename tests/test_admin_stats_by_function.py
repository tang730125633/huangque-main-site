# -*- coding: utf-8 -*-
"""调用统计：按【功能】分组，不是按 kind。

光把功能名修对是不够的 —— 「调用统计」那块原来是 `GROUP BY kind`，所以：

    果肉 / 豆姐 / 欧米（394 条）  kind 都是 xiaole_video  → 统计里是【一坨】，还显示成英文
    数字人口播 和 动作模仿        kind 都是 video          → 也混在一起
    五个作图引擎                kind 都是 image          → 也混在一起
    电影化身 / 创建数字人形象     没人认识                  → 直接显示英文 kind

功能是写在 payload 里的（channel / mode / provider / cine_mode），不把 payload 读出来就
永远分不开。

而且这份「kind → 名字」的表原来有【三份】：
    admin_api.call_func_name                   后台的请求日志
    content_domains.points._history_func_name  用户的消费明细
    site/admin/index.html 的 kindName          后台的调用统计（最旧的一份：video 一律叫
                                               「视频口播」，动作模仿也被算进去）
三份都删了，统一走 server/func_names.py。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import admin_api  # noqa: E402

ADMIN_SRC = (ROOT / "server/admin_api.py").read_text(encoding="utf-8")
CONSOLE = (ROOT / "site/admin/index.html").read_text(encoding="utf-8")
STATS_BLOCK = ADMIN_SRC.split("def job_stats")[1].split("\ndef ")[0]
# 只看【代码】—— docstring 里为了讲清楚来龙去脉，一定会提到旧的 GROUP BY kind
STATS_CODE = STATS_BLOCK.split('"""', 2)[-1]


class StatsGroupByFunctionTests(unittest.TestCase):
    def test_it_no_longer_groups_by_kind(self):
        self.assertNotIn("GROUP BY kind", STATS_CODE,
                         "按 kind 分组 —— 果肉/豆姐/欧米会被混成一个")
        self.assertIn("by_func", STATS_CODE)

    def test_the_group_key_is_the_shared_func_name(self):
        """和「请求日志」「用户消费明细」用【同一份】映射 —— 三处显示的名字必须一致。"""
        self.assertIn("call_func_name(kind, _job_payload(", STATS_BLOCK)

    def test_it_still_reads_only_the_payload_prefix(self):
        """统计要扫全表。payload 整条有几百 KB 的 base64 —— 整条读出来会把内存打爆。"""
        self.assertIn("substr(payload, 1, 4096)", STATS_BLOCK)

    def test_it_keeps_the_underlying_kind_for_triage(self):
        """功能名给运营看；kind 给排查看 —— 要知道这条任务走的是哪个 worker 池。"""
        self.assertIn('"kind": kind', STATS_BLOCK)
        self.assertIn("esc(x.func||x.kind)", CONSOLE)


class TruncatedPayloadTests(unittest.TestCase):
    """payload 只取前 4KB，JSON 会被截断，这时靠正则从前缀里捞字段。
    漏一个字段，那个维度就【永远】分不出来 —— channel / cine_mode / variant 原来都漏了。"""

    def test_channel_survives_truncation(self):
        truncated = '{"channel": "micro", "prompt": "' + "x" * 5000
        self.assertEqual(admin_api._job_payload(truncated).get("channel"), "micro")
        self.assertEqual(
            admin_api.call_func_name("xiaole_video", admin_api._job_payload(truncated)),
            "豆姐视频生成")

    def test_cine_mode_survives_truncation(self):
        truncated = '{"cine_mode": "duo", "prompt": "' + "x" * 5000
        self.assertEqual(
            admin_api.call_func_name("cinematic", admin_api._job_payload(truncated)),
            "电影化身 · 双人动作模仿")

    def test_seedream_variant_survives_truncation(self):
        truncated = '{"provider": "seedream", "variant": "pro", "prompt": "' + "x" * 5000
        self.assertEqual(
            admin_api.call_func_name("image", admin_api._job_payload(truncated)),
            "作图 · Seedream Pro")


class TheConsoleDoesNotGuessNamesTests(unittest.TestCase):
    def test_the_third_copy_of_the_mapping_is_gone(self):
        """前端那份 kindName 是第三份拷贝，而且最旧：
        video 一律叫「视频口播」（动作模仿也被算进去）、xiaole_video 叫「视频·小乐」、
        cinematic / avatar 根本不认识。"""
        self.assertNotIn("function kindName", CONSOLE)
        # 只查代码行 —— 注释里为了讲清楚「原来错在哪」，还会提到那些旧名字
        code = [ln for ln in CONSOLE.splitlines() if not ln.lstrip().startswith("//")]
        self.assertNotIn("视频·小乐", "\n".join(code))


class RealDataRegressionTests(unittest.TestCase):
    """线上近 14 天的真实分布 —— 这些以前全都挤在 3 个 kind 桶里。"""

    def test_the_three_channels_land_in_three_buckets(self):
        names = {
            admin_api.call_func_name("xiaole_video", {"channel": ch})
            for ch in ("grok", "micro", "omni")
        }
        self.assertEqual(len(names), 3, "394 条任务原来是一个桶：%s" % names)

    def test_talking_and_motion_are_no_longer_the_same_bucket(self):
        self.assertNotEqual(admin_api.call_func_name("video", {"mode": "text"}),
                            admin_api.call_func_name("video", {"mode": "motion"}))

    def test_the_five_image_engines_land_in_five_buckets(self):
        names = {
            admin_api.call_func_name("image", p)
            for p in ({}, {"provider": "seedream"}, {"provider": "xiaole"},
                      {"provider": "zelong2"}, {"model": "nb2"})
        }
        self.assertEqual(len(names), 5, "五个引擎原来都叫「作图」：%s" % names)


if __name__ == "__main__":
    unittest.main()
