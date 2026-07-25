import json
import sys
import unittest
from pathlib import Path
from unittest import mock


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import digital_ip


def _analysis():
    return {
        "summary": "这是一家有专业基础、但获客和复购不稳定的皮肤管理店。",
        "confirmed_facts": ["经营 7 年", "有 2 家门店"],
        "inferred_signals": ["老板具备长期内容素材"],
        "business_pains": [
            {"label": "获客成本高", "evidence": "平台流量越来越贵", "impact": "到店不稳定"},
        ],
        "positioning_candidates": [
            {
                "title": "问题肌管理主理人",
                "one_liner": "不制造焦虑，讲清长期改善。",
                "reasons": ["有真实经营经验"],
                "risks": ["需要持续案例"],
                "content_angles": ["顾客误区"],
            },
            {
                "title": "美业复购教练",
                "one_liner": "把一次成交变成长期关系。",
                "reasons": ["擅长老客维护"],
                "risks": ["同行受众更窄"],
                "content_angles": ["复购流程"],
            },
            {
                "title": "七年美业老板复盘者",
                "one_liner": "公开讲门店经营的得与失。",
                "reasons": ["经营经历可验证"],
                "risks": ["需要披露真实失败"],
                "content_angles": ["经营复盘"],
            },
        ],
        "recommended_index": 0,
        "follow_up_question": "老客复购下降最明显的是哪个项目？",
        "ready_to_confirm": False,
        "uncertainty_note": "还缺少具体复购数据。",
    }


class DigitalIPTests(unittest.TestCase):
    def setUp(self):
        digital_ip._recent_requests.clear()

    def test_diagnose_uses_responses_structured_outputs(self):
        captured = {}

        def fake_post(path, body, content_type, timeout):
            captured.update(
                path=path,
                body=json.loads(body),
                content_type=content_type,
                timeout=timeout,
            )
            return {
                "model": "gpt-5.6-sol-2026-07-01",
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(_analysis(), ensure_ascii=False)}],
                }],
                "usage": {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300},
            }

        with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                mock.patch.object(digital_ip, "_post", side_effect=fake_post):
            result = digital_ip.diagnose({
                "module": "定位诊断",
                "step": "采集门店经营底图",
                "answer": "经营 7 年，平台流量越来越贵，老客复购下降。",
                "confirmed_context": [],
            }, "beauty-owner")

        self.assertEqual(captured["path"], "/v1/responses")
        self.assertEqual(captured["body"]["text"]["format"]["type"], "json_schema")
        self.assertTrue(captured["body"]["text"]["format"]["strict"])
        self.assertFalse(captured["body"]["store"])
        self.assertEqual(captured["body"]["model"], "gpt-5.6-sol")
        self.assertNotIn("beauty-owner", json.dumps(captured["body"], ensure_ascii=False))
        self.assertEqual(result["analysis"]["recommended_index"], 0)
        self.assertTrue(result["ai_recommendation"])
        self.assertFalse(result["user_confirmed"])

    def test_payload_validation_bounds_user_input(self):
        with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "当前回答不能为空"):
            digital_ip.validate_payload({"module": "定位诊断", "step": "第一步", "answer": ""})
        with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "6000"):
            digital_ip.validate_payload({
                "module": "定位诊断",
                "step": "第一步",
                "answer": "美" * 6001,
            })

    def test_refusal_is_not_treated_as_schema_output(self):
        with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "暂时无法分析"):
            digital_ip._extract_output({
                "output": [{
                    "type": "message",
                    "content": [{"type": "refusal", "refusal": "cannot comply"}],
                }],
            })

    def test_rate_limit_blocks_seventh_request(self):
        for _ in range(6):
            digital_ip._check_rate_limit("owner")
        with self.assertRaises(digital_ip.DigitalIPRateLimited):
            digital_ip._check_rate_limit("owner")


if __name__ == "__main__":
    unittest.main()

