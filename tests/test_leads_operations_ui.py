# -*- coding: utf-8 -*-
import pathlib
import unittest

HTML = (pathlib.Path(__file__).resolve().parents[1] / "site/workbench/leads.html").read_text(encoding="utf-8")


class LeadsOperationsUiTests(unittest.TestCase):
    def test_filters_sorting_and_details_are_available(self):
        for marker in ('id="leadSearch"', 'id="platformFilter"', 'id="intentFilter"',
                       'id="regionFilter"', 'id="statusFilter"', 'id="leadSort"'):
            self.assertIn(marker, HTML)
        self.assertIn("function applyLeadView", HTML)
        self.assertIn("function openLeadDetail", HTML)
        self.assertIn("b.like_count", HTML)
        self.assertIn("L.legacy_lead_id", HTML)

    def test_price_is_derived_from_the_submitted_parameters(self):
        self.assertIn('id="leadCost"', HTML)
        self.assertIn("function leadCost", HTML)
        self.assertNotIn(">30 点</span>", HTML)

    def test_accounting_and_reply_rate_are_visible(self):
        self.assertIn('id="kDeduped"', HTML)
        self.assertIn('id="kEmpty"', HTML)
        self.assertIn('id="kReplyRate"', HTML)
        self.assertIn("function replyRate", HTML)

    def test_compliance_confirmation_is_required_before_submit(self):
        self.assertIn('id="complianceConfirm"', HTML)
        self.assertIn("平台规则", HTML)
        self.assertIn("个人信息", HTML)
        start = HTML.split("function start(){", 1)[1].split("function watch", 1)[0]
        self.assertIn("document.getElementById('complianceConfirm').checked", start)
        self.assertIn("compliance_version", start)
        core = (pathlib.Path(__file__).resolve().parents[1] / "server/content_domains/core.py").read_text(encoding="utf-8")
        self.assertIn("validate_compliance(body)", core)
        self.assertIn("_leads_domain().reserve_lead_submit(user[\"username\"])", core)
        self.assertNotIn("def reserve_lead_submit", core)


if __name__ == "__main__":
    unittest.main()
