import sys
from pathlib import Path

import pytest


HERMES = Path(__file__).parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import ip12_harness as harness


@pytest.mark.parametrize("draft", (
    "这套定位不会把你包装成已经拥有多年经验的 AI Agent 专家",
    "定位边界：不把你包装成一位拥有多年经验和成熟客户案例的 AI Agent 专家",
    "不以多年专家自居",
))
def test_negated_expertise_boundaries_are_not_treated_as_claims(draft):
    harness._validate_confirmable_claims(draft, "我进入这个领域只有三个月，不要把我包装成多年专家")


@pytest.mark.parametrize("draft", (
    "当前定位：AI Agent 专家",
    "我不是普通人，而是 AI Agent 专家",
    "我不仅努力，还是 AI Agent 专家",
    "我没有多年经验但已经是 AI Agent 专家",
))
def test_positive_expertise_claims_remain_blocked(draft):
    with pytest.raises(harness.HarnessError, match="未经证实"):
        harness._validate_confirmable_claims(draft, "我进入这个领域只有三个月")
