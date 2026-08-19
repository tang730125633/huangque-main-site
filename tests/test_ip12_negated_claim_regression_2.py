import sys
import unittest
from pathlib import Path


HERMES = Path(__file__).parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import ip12_harness as harness


class NegatedClaimRegressionTest(unittest.TestCase):
    def test_negated_expertise_boundaries_are_not_treated_as_claims(self):
        for draft in (
            "这套定位不会把你包装成已经拥有多年经验的 AI Agent 专家",
            "定位边界：不把你包装成一位拥有多年经验和成熟客户案例的 AI Agent 专家",
            "不以多年专家自居",
        ):
            with self.subTest(draft=draft):
                harness._validate_confirmable_claims(
                    draft, "我进入这个领域只有三个月，不要把我包装成多年专家"
                )

    def test_positive_expertise_claims_remain_blocked(self):
        for draft in (
            "当前定位：AI Agent 专家",
            "我不是普通人，而是 AI Agent 专家",
            "我不仅努力，还是 AI Agent 专家",
            "我没有多年经验但已经是 AI Agent 专家",
        ):
            with self.subTest(draft=draft), self.assertRaisesRegex(harness.HarnessError, "未经证实"):
                harness._validate_confirmable_claims(draft, "我进入这个领域只有三个月")
