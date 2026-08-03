from pathlib import Path
import unittest


class AdminPointsLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1] / "site" / "admin" / "index.html"
        ).read_text(encoding="utf-8")

    def test_has_dedicated_points_module_and_filters(self):
        self.assertIn('data-module-tab="points"', self.html)
        self.assertIn('data-module="points"', self.html)
        self.assertIn('id="pointsUser"', self.html)
        self.assertIn('id="pointsActor"', self.html)
        self.assertIn('id="pointsDirection"', self.html)
        self.assertNotIn('id="auditActor"', self.html)

    def test_user_detail_opens_filtered_points_module(self):
        self.assertIn("el('detailPoints').onclick=function(){openPoints(u.username)};", self.html)
        self.assertIn("openUserDetail('',btn.getAttribute('data-user-id'));", self.html)
        self.assertIn("el('pointsUser').value=username||'';", self.html)
        self.assertIn("switchModule('points');", self.html)

    def test_query_includes_source_direction_and_username(self):
        self.assertIn(
            "'?limit=120&actor='+encodeURIComponent(el('pointsActor').value||'')",
            self.html,
        )
        self.assertIn(
            "'&direction='+encodeURIComponent(el('pointsDirection').value||'')",
            self.html,
        )
        self.assertIn(
            "'&username='+encodeURIComponent((el('pointsUser').value||'').trim())",
            self.html,
        )

    def test_renders_summary_and_transaction_key(self):
        for element_id in (
            "pointsTotal",
            "pointsCredits",
            "pointsDebits",
            "pointsNet",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("x.who_admin==='system'?'任务':x.who_admin", self.html)
        self.assertIn("x.transaction_key||'-'", self.html)

    def test_internal_reason_codes_are_rendered_as_chinese_actions(self):
        self.assertIn("function pointReasonText(item)", self.html)
        self.assertIn("raw.match(/^job:([a-z0-9_]+)(?:\\s|$)/)", self.html)
        self.assertIn("if(/^job#\\d+(?:\\s|$)/.test(raw))", self.html)
        self.assertIn("if(raw==='reverse')", self.html)
        self.assertIn("if(raw==='reverse:refund')", self.html)
        self.assertIn("任务失败退款", self.html)
        self.assertIn("任务扣点", self.html)
        self.assertIn("购买音色复刻槽位", self.html)
        self.assertIn("音色槽位购买退款", self.html)
        self.assertIn("抖音内容搜索", self.html)
        self.assertIn("抖音内容搜索退款", self.html)
        self.assertIn("esc(pointReasonText(x))", self.html)

    def test_users_and_logs_use_twenty_item_pages(self):
        self.assertIn("userPage:1, userPageSize:20", self.html)
        self.assertIn("reqPage:1, reqPageSize:20", self.html)
        self.assertIn("<th>ID</th><th>账号</th>", self.html)
        self.assertIn("data-user-page", self.html)
        self.assertIn("data-req-page", self.html)
        self.assertIn("'&limit='+state.userPageSize+'&offset='", self.html)
        self.assertIn("'?limit='+state.reqPageSize+'&offset='", self.html)


if __name__ == "__main__":
    unittest.main()
