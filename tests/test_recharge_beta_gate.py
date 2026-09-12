"""内测期间「点数、充值和会员购买」必须关干净。

背景：后端 `points_billing` 开关早就关了（`auth_server.POINTS_BILLING_CREATE_PATHS`
一律返回 503「内测期间点数、充值和会员购买暂不开放」），但**充值页完全没读这个开关** ——
会员卡和 4 个充值档位照常渲染，用户点下去才吃 503。

这组用例把这条边界钉住：开关关闭时，页面上**不得残留任何可点的购买入口**。
"""
import io
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "site" / "workbench" / "recharge.html"


class RechargeBetaGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = io.open(PAGE, encoding="utf-8").read()

    def _gated_regions(self):
        """返回被开关关掉的容器片段：#membershipOffer 与 #pointsRechargeArea。"""
        regions = []
        for anchor in ('<section id="membershipOffer"', '<div id="pointsRechargeArea"'):
            start = self.src.index(anchor)
            depth = 0
            cursor = start
            while cursor < len(self.src):
                m = re.compile(r"<(div|section)\b|</(div|section)>").search(self.src, cursor)
                if not m:
                    break
                depth += 1 if m.group(0).startswith("<") and not m.group(0).startswith("</") else -1
                cursor = m.end()
                if depth == 0:
                    break
            regions.append((start, cursor))
        return regions

    def test_page_reads_the_billing_flag(self):
        self.assertIn("points_billing_enabled", self.src)

    def test_flag_off_hides_every_purchase_surface(self):
        for container in ("membershipOffer", "pointsRechargeArea",
                          "payMethodArea", "rechargeNoteArea"):
            with self.subTest(container=container):
                self.assertIn(
                    "$('%s').hidden=true" % container, self.src,
                    "%s 在计费关闭时必须被隐藏" % container,
                )

    def test_short_circuits_before_rendering_the_offer(self):
        """必须先于 renderMembershipOffer 短路，否则会员卡照样画出来。"""
        gate = self.src.index("if(user.points_billing_enabled===false){")
        render = self.src.index("renderMembershipOffer(user);", self.src.index("function loadMe"))
        self.assertLess(gate, render, "开关判断必须在渲染会员卡之前")

    def test_no_purchase_button_survives_outside_the_gated_containers(self):
        """结构护栏：以后谁再加充值按钮，加在门外面就会被这条抓住。"""
        regions = self._gated_regions()
        for match in re.finditer(r"<button[^>]*data-recharge[^>]*>", self.src):
            pos = match.start()
            inside = any(start <= pos < end for start, end in regions)
            self.assertTrue(
                inside,
                "充值页上出现了一个没被开关罩住的购买按钮：%s" % match.group(0)[:80],
            )

    def test_beta_notice_exists_and_is_hidden_by_default(self):
        marker = '<section id="betaBillingClosed"'
        self.assertIn(marker, self.src)
        start = self.src.index(marker)
        open_tag = self.src[start: self.src.index(">", start)]
        # 默认必须藏起来：只有确认开关关闭才由 JS 显示
        self.assertIn("hidden", open_tag)
        self.assertIn("内测期间暂不开放", self.src[start: self.src.index("</section>", start)])

    def test_balance_and_history_stay_visible(self):
        """关的是购买，不是查账：余额和历史记录不能被一起藏掉。"""
        self.assertIn("pointsBalance", self.src)
        self.assertIn("orderBox", self.src)
        self.assertNotIn("$('orderBox').hidden=true", self.src)


class BackendGateStillInPlaceTests(unittest.TestCase):
    """后端那道门不能被绕过 —— 前端只是不露入口，服务端才是真拦。"""

    def test_create_paths_are_blocked_when_flag_off(self):
        src = io.open(ROOT / "server" / "auth_server.py", encoding="utf-8").read()
        self.assertIn("POINTS_BILLING_CREATE_PATHS", src)
        for path in ("/api/auth/recharge/order", "/api/auth/wxpay/native",
                     "/api/auth/wxpay/jsapi", "/api/auth/virtual-pay/order",
                     "/api/auth/points/transfer"):
            with self.subTest(path=path):
                self.assertIn('"%s"' % path, src)
        self.assertIn("points_billing_disabled", src)


if __name__ == "__main__":
    unittest.main()
