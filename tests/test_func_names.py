# -*- coding: utf-8 -*-
"""任务/路径 → 功能名：唯一事实来源。

原来这份映射有【两份】拷贝，已经各自漂移：

    admin_api.call_func_name              （运营后台的日志/统计）
    content_domains.points._history_func_name  （用户的消费明细）

漂移的后果，拿线上近 14 天 1247 条真实任务跑了一遍当前映射：**749 条（60%）的功能名是错的
或没用的** ——

  * cinematic(49) / avatar(43)  → 后台原样吐英文 "cinematic" / "avatar"
  * video motion(198)           → 「视频 · 动作模仿 · 线路一(HeyGen)」。线路概念在去线路化
                                  (#594)时就删了，motion 现在只走 WaveSpeed —— 这不是过时，
                                  是【错的】：它根本不走 HeyGen
  * xiaole_video(394)           → 果肉/豆姐/欧米三个渠道混成一个「视频 · 小乐」
  * image seedream/xiaole(65)   → 分不出引擎，都叫「作图」

换装的线路一/线路二【是真的还在】（前端还给用户选），那个标签不动。
"""
import importlib
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

F = importlib.import_module("func_names")
ADMIN_SRC = (ROOT / "server/admin_api.py").read_text(encoding="utf-8")
POINTS_SRC = (ROOT / "server/content_domains/points.py").read_text(encoding="utf-8")
DEPLOY = (ROOT / "scripts/deploy_site.sh").read_text(encoding="utf-8")
SENTINEL = (ROOT / "scripts/drift_sentinel.py").read_text(encoding="utf-8")


class OnlyOneCopyTests(unittest.TestCase):
    def test_neither_caller_keeps_its_own_implementation(self):
        """两份拷贝就会漂移 —— 这次已经漂了 60%。改一处、留一处，下次照样分家。"""
        self.assertNotIn("def call_func_name(", ADMIN_SRC)
        self.assertNotIn("def _history_func_name(", POINTS_SRC)
        self.assertNotIn("def _path_func(", ADMIN_SRC)

    def test_both_callers_point_at_the_shared_module(self):
        self.assertIn("call_func_name = func_names.func_name", ADMIN_SRC)
        self.assertIn("_path_func = func_names.path_func", ADMIN_SRC)
        self.assertIn("_history_func_name = _func_names.func_name", POINTS_SRC)

    def test_the_shared_module_has_no_import_side_effects(self):
        """两个服务都要 import 它。它要是碰 DB/env，就会把别人的启动顺序绑死。

        只查【代码】—— 模块注释里为了讲清楚来龙去脉，一定会提到 content_domains。
        """
        src = (ROOT / "server/func_names.py").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if ln.strip() and not ln.lstrip().startswith("#"))
        code = re.sub(r'"""..*?"""', "", code, flags=re.S)   # 去掉 docstring
        for bad in ("import sqlite3", "import os", "from .core", "content_domains", "import urllib"):
            self.assertNotIn(bad, code, "func_names 不该依赖 %s" % bad)


class NewKindsAreNamedTests(unittest.TestCase):
    def test_cinematic_and_avatar_are_no_longer_raw_english(self):
        for kind in ("cinematic", "avatar"):
            got = F.func_name(kind, {})
            self.assertNotEqual(got, kind, "%s 原样吐 kind —— 运营后台里就是一串英文" % kind)
            self.assertRegex(got, r"[一-鿿]", "%s 的功能名里一个中文都没有" % kind)

    def test_cinematic_shows_which_of_the_three_modes(self):
        self.assertEqual(F.func_name("cinematic", {"cine_mode": "motion"}), "电影化身 · 动作模仿")
        self.assertEqual(F.func_name("cinematic", {"cine_mode": "duo"}), "电影化身 · 双人动作模仿")
        self.assertEqual(F.func_name("cinematic", {"cine_mode": "open"}), "电影化身 · 开放式生成")
        # 老任务的 payload 里没有 cine_mode（#601 之前入的库）—— 不能崩，回落到功能名本身
        self.assertEqual(F.func_name("cinematic", {}), "电影化身")

    def test_the_three_xiaole_channels_are_told_apart(self):
        """394 条任务混成一个「视频 · 小乐」，运营根本看不出谁在跑哪个渠道。"""
        self.assertEqual(F.func_name("xiaole_video", {"channel": "grok"}), "果肉视频生成")
        self.assertEqual(F.func_name("xiaole_video", {"channel": "micro"}), "豆姐视频生成")
        self.assertEqual(F.func_name("xiaole_video", {"channel": "omni"}), "欧米视频生成")

    def test_image_engines_are_told_apart(self):
        self.assertEqual(F.func_name("image", {"provider": "seedream"}), "作图 · 黄雀引擎 1 标准")
        self.assertEqual(F.func_name("image", {"provider": "xiaole"}), "作图 · 果肉生图")


class DeadLineLabelsTests(unittest.TestCase):
    def test_motion_no_longer_claims_to_run_on_heygen(self):
        """#594 去线路化后 motion 只走 WaveSpeed。贴「线路一(HeyGen)」不是过时，是错的。"""
        got = F.func_name("video", {"mode": "motion"})
        self.assertEqual(got, "动作模仿")
        self.assertNotIn("线路", got)
        self.assertNotIn("HeyGen", got)

    def test_tryon_keeps_its_lines_because_they_are_real(self):
        """换装的线路一/线路二还在（前端还给用户选）—— 别顺手一起删了。"""
        self.assertIn("线路一(RunningHub)", F.func_name("tryon", {}))
        self.assertIn("线路二(WaveSpeed)", F.func_name("tryon", {"line": "2"}))


class PathPrefixOrderTests(unittest.TestCase):
    def test_reads_are_not_labelled_as_submits(self):
        """/api/gen/video 是前缀匹配 —— 读历史(/api/gen/video/assets)会被标成「视频 · 提交」，
        统计里凭空多出一堆根本没发生的提交。长前缀必须排在短前缀【前面】。"""
        self.assertEqual(F.path_func("/api/gen/video/assets"), "视频 · 读历史")
        self.assertEqual(F.path_func("/api/gen/video/avatars"), "数字人形象 · 读列表")
        self.assertEqual(F.path_func("/api/gen/video"), "视频 · 提交")

    def test_the_new_endpoints_are_registered(self):
        self.assertEqual(F.path_func("/api/gen/cinematic"), "电影化身 · 提交")
        self.assertEqual(F.path_func("/api/gen/avatar"), "创建数字人形象 · 提交")
        self.assertNotEqual(F.path_func("/api/gen/xiaole_video"), "")

    def test_no_prefix_is_shadowed_by_a_shorter_one_before_it(self):
        """自动查全表：任何一条的前缀，如果被排在它【前面】的某条前缀覆盖，它就永远匹配不到。"""
        seen = []
        for prefix, name in F.PATH_FUNCS:
            for earlier, ename in seen:
                self.assertFalse(prefix.startswith(earlier),
                                 "「%s」(%s) 永远匹配不到 —— 被前面的「%s」(%s) 吃掉了"
                                 % (prefix, name, earlier, ename))
            seen.append((prefix, name))


class NewModuleMustBeDeployedTests(unittest.TestCase):
    """func_names.py 是【新文件】，而部署是白名单式的。漏传它，content 和 admin
    两个服务【一起】ImportError 起不来 —— 这正是 jobs_store.py 踩过的坑(#23)。"""

    def test_it_is_in_the_deploy_whitelist(self):
        self.assertIn("server/func_names.py", DEPLOY)

    def test_the_deploy_restarts_admin_too(self):
        """admin 也 import 了它。只重启 content，admin 还跑着旧代码（甚至旧的没这文件）。"""
        self.assertRegex(DEPLOY, r"systemctl restart[^\n]*huangque-admin")

    def test_the_drift_sentinel_watches_it(self):
        self.assertIn("'server/func_names.py': '/home/ubuntu/content-api/func_names.py'", SENTINEL)


class NamesMatchTheProductTests(unittest.TestCase):
    """功能名要和产品里的叫法【逐字一致】—— 运营看到的名字，得能和用户点的那个按钮对上号。

    每一条都对着前端的实际标签校验；改了前端的标签，这些测试就会红。
    """

    VIDEO_HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")
    BANANA_HTML = (ROOT / "site/workbench/banana.html").read_text(encoding="utf-8")

    def test_the_three_third_party_channels_use_the_ui_labels(self):
        for ch, name in F.XIAOLE_CHANNELS.items():
            self.assertIn('data-function="%s">%s<' % (ch, name), self.VIDEO_HTML,
                          "「%s」不是前端 data-function=\"%s\" 的标签" % (name, ch))

    def test_the_video_function_names_use_the_ui_labels(self):
        for tab in ("动作模仿", "电影化身", "换装换背景", "数字化 IP"):
            self.assertIn('>%s<' % tab, self.VIDEO_HTML)
        self.assertEqual(F.func_name("video", {"mode": "motion"}), "动作模仿")
        self.assertTrue(F.func_name("cinematic", {}).startswith("电影化身"))
        self.assertTrue(F.func_name("video", {"mode": "text"}).startswith("数字化 IP"))

    def test_the_image_engine_names_use_the_ui_labels(self):
        """UI 品牌名与日志必须一致，不能泄露上游原模型名。"""
        for name in ("黄雀引擎 2", "黄雀引擎 1", "果肉生图", "泽龙2生图", "纳米香蕉"):
            self.assertIn(name, self.BANANA_HTML, "「%s」不是 banana.html 里的引擎标签" % name)
        self.assertEqual(F.func_name("image", {}), "作图 · 黄雀引擎 2")          # gpt 不发 provider
        self.assertEqual(F.func_name("image", {"provider": "zelong2"}), "作图 · 泽龙2生图")
        self.assertEqual(F.func_name("image", {"model": "nb2"}), "作图 · 纳米香蕉 2")
        self.assertEqual(F.func_name("image", {"provider": "seedream", "variant": "pro"}),
                         "作图 · 黄雀引擎 1 Pro")

    def test_the_legacy_zelong_pool_is_not_merged_into_zelong2(self):
        """zelong（不带 2）是老号池，界面上已经没入口了，但库里还有 66 条存量任务。
        混成一个名字，运营就看不出那些任务其实跑在一个已经下线的号池上。"""
        self.assertEqual(F.func_name("image", {"provider": "zelong"}), "作图 · 泽龙")
        self.assertNotEqual(F.func_name("image", {"provider": "zelong"}),
                            F.func_name("image", {"provider": "zelong2"}))


if __name__ == "__main__":
    unittest.main()
