# -*- coding: utf-8 -*-
"""获客页历史记录 + 任务状态持久化。

线上两个 bug（2026-07-15）：
1. 平台获客没有历史记录 —— leads.html 从不加载服务端 assets 表里的往次获客，跑完切页就丢。
2. 切换分页后任务状态看不到 —— 完成的结果只在内存，切页回来一片空白。

修复：leads.html 从 /api/gen/assets?kind=leads 加载历史，做成下拉；没有进行中的任务时
自动载入最近一次，切页回来不空白；新任务跑完刷新历史。leads 数据本就入了统一 assets 表
（meta.leads 与实时结果同构，renderLeads 可直接渲染）。
"""
import pathlib
import unittest

LEADS_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/leads.html"


class LeadsHistoryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = LEADS_HTML.read_text(encoding="utf-8")

    def test_history_is_loaded_from_the_assets_endpoint(self):
        """历史来自服务端 assets 表（leads 本就入库），不是只存内存。"""
        self.assertIn("/api/gen/assets?kind=leads", self.html)
        self.assertIn("function loadHistory", self.html)

    def test_history_dropdown_exists(self):
        self.assertIn('id="histSel"', self.html)
        self.assertIn("histSel.onchange", self.html)

    def test_history_renders_from_meta_leads(self):
        """历史项的客户明细在 meta.leads，与实时结果同构，直接喂 renderLeads。"""
        block = self.html.split("function renderHistory", 1)[1].split("function loadHistory", 1)[0]
        self.assertIn("m.leads", block)
        self.assertIn("renderLeads(", block)
        self.assertIn("setKPIs(", block)

    def test_no_active_job_auto_loads_latest_so_the_page_is_not_blank(self):
        """⚠️ Bug2：切页回来没有进行中的任务时，自动载入最近一次，别一片空白。"""
        self.assertIn("loadHistory(true)", self.html)

    def test_a_finished_run_is_added_to_history(self):
        """新任务跑完要刷新历史下拉，否则刚跑完这次进不了历史。"""
        done = self.html.split("if(d.status==='done'){", 1)[1].split("}else if", 1)[0]
        self.assertIn("loadHistory(false)", done)

    def test_running_job_resume_is_kept(self):
        """进行中的任务仍要能接上（离开页面再回来不丢），不能被历史逻辑顶掉。"""
        self.assertIn("hq_active_leads_job", self.html)
        self.assertIn("watch(aj.id", self.html)


if __name__ == "__main__":
    unittest.main()
