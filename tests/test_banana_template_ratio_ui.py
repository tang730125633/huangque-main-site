import pathlib
import unittest


BANANA_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/banana.html"


class BananaTemplateRatioUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = BANANA_HTML.read_text(encoding="utf-8")

    def test_template_selection_updates_ratio_immediately(self):
        template_click = self.html.split(
            "grid.querySelectorAll('[data-tpl]').forEach", 1
        )[1].split("};});", 1)[0]
        self.assertIn("selectRatio(activeTemplate.ratio,false);", template_click)
        self.assertLess(
            template_click.index("activeTemplate=TEMPLATES.filter"),
            template_click.index("selectRatio(activeTemplate.ratio,false);"),
        )

    def test_ratio_selection_highlights_matching_template(self):
        sync_block = self.html.split("function syncTemplateForRatio(next){", 1)[1].split(
            "function selectRatio", 1
        )[0]
        self.assertIn("activeTemplate&&activeTemplate.ratio===next", sync_block)
        self.assertIn("return t.ratio===next", sync_block)
        self.assertIn("if(!match || match===activeTemplate) return;", sync_block)
        self.assertIn("activeTemplate=match;", sync_block)
        self.assertIn("renderTemplates();", sync_block)

        ratio_block = self.html.split("function selectRatio(next,syncTemplate){", 1)[
            1
        ].split(
            "document.querySelectorAll('#ratioRow > div').forEach(function(c)", 1
        )[0]
        self.assertIn(
            "if(matched && syncTemplate!==false) syncTemplateForRatio(next);",
            ratio_block,
        )

    def test_template_can_apply_or_generate_through_existing_submit_path(self):
        self.assertIn('id="tplApply"', self.html)
        self.assertIn('id="tplGenerate"', self.html)
        generate_block = self.html.split(
            "var tplGenerate=document.getElementById('tplGenerate');", 1
        )[1].split("};", 1)[0]
        self.assertIn("if(gen.disabled) return;", generate_block)
        self.assertIn("applyTemplate();", generate_block)
        self.assertIn("gen.click();", generate_block)

    def test_template_actions_share_generation_busy_state(self):
        # 生成按钮与模板按钮必须共享同一个「能不能点」的状态。原来这个状态是 busy(on)，
        # 支持每人并行 3 个生图后改成 syncGen()：lock = 已占满 3 个 或 正在提交。
        block = self.html.split("function syncGen(){", 1)[1].split("\n  }", 1)[0]
        self.assertIn("lock=(n>=MAX_PARALLEL)||submitting", block)
        self.assertIn("gen.disabled=lock", block)
        self.assertIn("['tplApply','tplGenerate']", block)
        self.assertIn("b.disabled=lock", block)


if __name__ == "__main__":
    unittest.main()
