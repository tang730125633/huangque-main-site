import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama, short_drama_advisor


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class ShortDramaAdvisorTests(unittest.TestCase):
    def test_question_does_not_extract_business_fields(self):
        captured = {}

        def opener(request, timeout=0):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            content = {
                "intent": "ask_recommendation",
                "reply": "可以，我给你三个冲突方向。",
                "extracted_fields": {},
                "missing_fields": ["conflict"],
                "confidence": 0.98,
                "quick_replies": ["关系即将破裂", "时间只剩一天"],
            }
            return Response({"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "https://advisor.example/v1",
            "SHORT_DRAMA_ADVISOR_API_KEY": "test-key",
        }, clear=False):
            result = short_drama_advisor.advise({
                "messages": ["青春期学生", "你觉得呢"],
                "understanding": {"protagonist": "青春期学生"},
                "expected_field": "conflict",
                "user_message": "你觉得呢",
            }, opener=opener)

        self.assertEqual(result["intent"], "ask_recommendation")
        self.assertEqual(result["extracted_fields"], {})
        self.assertEqual(result["missing_fields"], ["conflict"])
        self.assertEqual(captured["timeout"], 45)
        self.assertEqual(captured["payload"]["model"], "grok-3-mini")

    def test_response_is_normalized_to_public_contract(self):
        result = short_drama_advisor._normalize({
            "intent": "ANSWER",
            "reply": "收到",
            "extracted_fields": {"conflict": "时间只剩一天", "admin": "secret"},
            "missing_fields": ["ending", "admin"],
            "confidence": 8,
            "quick_replies": ["一", "二", "三", "四", "五"],
        })
        self.assertEqual(result["intent"], "answer")
        self.assertEqual(result["extracted_fields"], {"conflict": "时间只剩一天"})
        self.assertEqual(result["missing_fields"], ["ending"])
        self.assertEqual(result["confidence"], 1.0)
        self.assertEqual(len(result["quick_replies"]), 4)
        self.assertEqual(result["field_updates"][0]["operation"], "set")
        self.assertEqual(result["field_updates"][0]["status"], "confirmed")
        self.assertEqual(result["next_action"], "continue")
        self.assertEqual(result["mode"], "ai")
        self.assertFalse(result["degraded"])

    def test_negation_and_clear_operation_are_preserved(self):
        result = short_drama_advisor._normalize({
            "intent": "negate",
            "reply": "已取消悬疑风格",
            "recap": "风格已清空",
            "field_updates": [{
                "field": "style", "operation": "clear", "value": "",
                "confidence": 0.92, "evidence": "不要悬疑",
            }],
            "confidence": 0.92,
        })
        self.assertEqual(result["intent"], "negate")
        self.assertEqual(result["field_updates"], [{
            "field": "style", "operation": "clear", "value": "",
            "confidence": 0.92, "evidence": "不要悬疑",
            "status": "removed",
        }])
        self.assertEqual(result["recap"], "风格已清空")

    def test_multiple_fields_evidence_and_low_confidence_status_are_preserved(self):
        result = short_drama_advisor._normalize({
            "intent": "answer",
            "reply": "我理解了大部分设定。",
            "field_updates": [
                {"field": "topic", "operation": "set", "value": "雨夜便利店", "confidence": .96, "evidence": "雨夜便利店的故事"},
                {"field": "protagonist", "operation": "set", "value": "刚失业的女性", "confidence": .93, "evidence": "女主刚失业"},
                {"field": "ending", "operation": "set", "value": "温暖", "confidence": .72, "evidence": "最后想温暖一点"},
            ],
            "focus_field": "conflict",
            "next_action": "ask",
            "confidence": .9,
        })
        self.assertEqual(len(result["field_updates"]), 3)
        self.assertEqual(result["field_updates"][0]["status"], "confirmed")
        self.assertEqual(result["field_updates"][2]["status"], "inferred")
        self.assertEqual(result["field_updates"][1]["evidence"], "女主刚失业")
        self.assertEqual(result["focus_field"], "conflict")

    def test_ambiguous_conflict_is_normalized_without_overwriting_reasoning(self):
        result = short_drama_advisor._normalize({
            "intent": "modify",
            "reply": "你希望保留哪一种情绪？",
            "field_updates": [{"field": "emotion", "operation": "set", "value": "温暖", "confidence": .7, "evidence": "也可以温暖"}],
            "conflicts": [{"field": "emotion", "proposed_value": "温暖", "reason": "没有明确表示替换", "requires_confirmation": True}],
            "next_action": "clarify",
        }, {"emotion": "紧张悬疑"})
        self.assertEqual(result["field_updates"][0]["status"], "conflicted")
        self.assertEqual(result["conflicts"][0]["existing_value"], "紧张悬疑")
        self.assertEqual(result["next_action"], "clarify")

    def test_missing_provider_is_explicit_and_route_is_allowlisted(self):
        with mock.patch.dict(os.environ, {
            "SHORT_DRAMA_ADVISOR_API_BASE": "",
            "SHORT_DRAMA_ADVISOR_API_KEY": "",
            "XAI_API_BASE": "",
            "XAI_API_KEY": "",
        }, clear=False):
            with self.assertRaises(short_drama_advisor.AdvisorError) as raised:
                short_drama_advisor.advise({"user_message": "你觉得呢"})
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.code, "advisor_provider_not_configured")
        self.assertIn("/api/gen/short-drama/advisor", short_drama._HTTP_ROUTES["POST"])


if __name__ == "__main__":
    unittest.main()
