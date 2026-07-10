# -*- coding: utf-8 -*-
"""灵感「做同款」从提示词识别比例（#547）。

守的不变量：
1. 识别出比例后调 selectRatio(ratio, false) —— **不联动模板**。
   #526 给 selectRatio 加了第二个参数：不传就会 syncTemplateForRatio()，
   自动选中并高亮一个匹配该比例的模板、带出模板说明。用户是从灵感页点「做同款」
   进来的，并没有在用模板，那样会误导（虽然不污染 prompt —— 提交读的是 pr.value）。
2. 正则的边界：不能把 "1080x1920" 里的 "0x1" 当成 1:1，也不能匹配 "19:16"。

行为用例通过 node 执行真实的 JS 函数；没有 node 时跳过（结构断言仍然生效）。
"""
import json
import pathlib
import re
import shutil
import subprocess
import unittest


BANANA = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/banana.html"
SRC = BANANA.read_text(encoding="utf-8")


class TemplateSyncTests(unittest.TestCase):
    def test_infer_does_not_sync_template(self):
        self.assertIn("selectRatio(inferredRatio, false)", SRC)

    def test_infer_never_calls_bare_selectRatio(self):
        """漏掉 false 就会连带高亮一个用户没选的模板。"""
        self.assertNotIn("selectRatio(inferredRatio);", SRC)

    def test_selectRatio_still_takes_sync_flag(self):
        """#526 的双向同步能力不能被删掉。"""
        self.assertIn("function selectRatio(next,syncTemplate)", SRC)
        self.assertIn("if(matched && syncTemplate!==false) syncTemplateForRatio(next);", SRC)


class DetectRatioBehaviourTests(unittest.TestCase):
    """用 node 跑真实的 detectPromptRatio。"""

    CASES = [
        ("9:16 竖版美业海报", "9:16"),
        ("16:9 横版banner", "16:9"),
        ("1:1 方图", "1:1"),
        ("3:4 人像", "3:4"),
        ("4:5 小红书", "4:5"),
        ("5:4 横构图", "5:4"),
        ("16：9 中文冒号", "16:9"),
        ("16x9 小写x", "16:9"),
        ("16X9 大写X", "16:9"),
        ("16 : 9 带空格", "16:9"),
        ("四宫格拼图", "1:1"),
        ("九宫格", "1:1"),
        ("方图设计", "1:1"),
        ("横版构图", "16:9"),
        ("竖版海报", "9:16"),
        # 边界：这些都不该匹配
        ("1080x1920 像素", ""),
        ("19:16 不该匹配", ""),
        ("9:169 不该匹配", ""),
        ("无比例信息的提示词", ""),
    ]

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("no node")
        m = re.search(r"function detectPromptRatio\(text\)\{[\s\S]*?\n  \}", SRC)
        assert m, "banana.html 里找不到 detectPromptRatio"
        cls.fn = m.group(0)

    def _run(self, inputs):
        script = "%s\nconsole.log(JSON.stringify(%s.map(detectPromptRatio)));" % (
            self.fn, json.dumps(inputs, ensure_ascii=False))
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             encoding="utf-8", check=True)
        return json.loads(out.stdout.strip())

    def test_all_cases(self):
        inputs = [c[0] for c in self.CASES]
        got = self._run(inputs)
        for (text, want), actual in zip(self.CASES, got):
            self.assertEqual(want, actual, "detectPromptRatio(%r)" % text)

    def test_pixel_dimensions_never_match(self):
        """'1080x1920' 里含 '0x1'，正则边界必须挡住。"""
        self.assertEqual([""], self._run(["1080x1920"]))


if __name__ == "__main__":
    unittest.main()
