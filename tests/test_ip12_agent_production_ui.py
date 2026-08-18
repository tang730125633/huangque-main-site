import unittest
from pathlib import Path


PAGE = Path(__file__).resolve().parents[1] / "server" / "hermes_ip12" / "templates" / "index.html"


class IP12AgentProductionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")

    def test_resizable_panel_has_desktop_bounds_and_accessible_controls(self):
        self.assertIn("--production-panel-width:440px", self.html)
        self.assertIn("min:360,max:Math.min(720,Math.floor(window.innerWidth*.5))", self.html)
        self.assertIn('id="productionPanelResizer"', self.html)
        self.assertIn('role="separator"', self.html)
        self.assertIn("beginProductionResize(event)", self.html)
        self.assertIn("resizeProductionByKey(event)", self.html)
        self.assertIn("localStorage.setItem(productionStorageKey(),next)", self.html)
        self.assertIn("localStorage.getItem(productionStorageKey())", self.html)
        self.assertIn("@media(max-width:1100px){\n  .rpn.open{position:fixed", self.html)
        self.assertIn("@media(max-width:720px)", self.html)

    def test_one_contextual_panel_renders_all_four_result_shapes(self):
        start = self.html.index("function renderProductionPanel")
        panel = self.html[start:self.html.index("function restoreProductionPanel()", start)]
        self.assertIn("来源版本", panel)
        self.assertIn("为什么推荐", panel)
        self.assertIn("素材与参数", panel)
        self.assertIn("实时报价", panel)
        self.assertIn("任务与结果", panel)
        result = self.html[self.html.index("function productionResultHtml"):self.html.index("function productionOptionsHtml")]
        self.assertIn("<img", result)
        self.assertIn("<video controls", result)
        self.assertIn("<audio controls", result)
        self.assertIn("打开 Canvas", result)
        self.assertIn("下载", result)

    def test_only_the_quote_card_exposes_the_paid_confirmation(self):
        confirm = self.html[self.html.index("async function confirmProduction"):self.html.index("async function refreshProduction")]
        self.assertIn("/api/ip12/productions/confirm", confirm)
        self.assertIn("record.status==='quoted'", confirm)
        quote_card = self.html[self.html.index("if(quoted){"):self.html.index("html+='<div class=\"rpn-card\"><div class=\"rpn-card-header\">任务与结果")]
        self.assertIn('data-production-confirm="true"', quote_card)
        self.assertIn("确认并提交这次生产", quote_card)
        actions = self.html[self.html.index("function runStateAction"):self.html.index("function attachHarnessActions")]
        self.assertIn("if(item.type==='confirm_paid_job'){", actions)
        self.assertNotIn("confirmProduction()", actions)
        message = self.html[self.html.index("function sendMessage"):self.html.index("async function sendTurn")]
        self.assertIn("var turn={message:text}", message)
        self.assertNotIn("confirmProduction", message)

    def test_restore_and_direct_navigation_keep_conversation_context(self):
        select = self.html[self.html.index("async function selectConvo"):self.html.index("async function jumpModule")]
        self.assertIn("productions=c.productions||{}", select)
        self.assertIn("activeProductionId=restoreProductionId()", select)
        self.assertIn("restoreProductionPanel()", select)
        navigation = self.html[self.html.index("function navigateToProductionRoute"):self.html.index("function openProductionCanvas")]
        self.assertIn("sessionStorage.setItem('ip12-production-return'", navigation)
        self.assertIn("url.searchParams.set('conversation_id',cid||'')", navigation)
        self.assertIn("url.searchParams.set('project_id',cid||'')", navigation)
        self.assertIn("url.searchParams.set('return_to',location.pathname+location.search)", navigation)

    def test_production_errors_are_safe_user_messages_not_server_details(self):
        source = self.html[self.html.index("function productionError"):self.html.index("function productionRoute")]
        self.assertIn("暂时无法读取生产状态，请稍后再试。", source)
        self.assertNotIn("data.error", source)
        self.assertNotIn("e.message", source)
        self.assertNotIn("安全整理", self.html)


if __name__ == "__main__":
    unittest.main()
