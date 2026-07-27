import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import text


class ScriptSubmissionGuardTests(unittest.TestCase):
    def test_copy_payload_rejects_blank_prompt_before_queueing(self):
        for prompt in ("", "   ", "\n\t"):
            with self.subTest(prompt=repr(prompt)):
                with self.assertRaisesRegex(ValueError, "请输入文案需求"):
                    text.validate_copy_payload({"prompt": prompt, "format": "script"})

    def test_copy_payload_normalizes_prompt_without_mutating_request(self):
        original = {"prompt": "  夏季通勤防晒  ", "format": "script"}
        cleaned = text.validate_copy_payload(original)

        self.assertEqual(cleaned["prompt"], "夏季通勤防晒")
        self.assertEqual(original["prompt"], "  夏季通勤防晒  ")

    def test_script_prompt_forbids_invented_claims_and_brand_details(self):
        captured = {}

        def fake_chat(system_message, user_message, temperature):
            captured.update(system=system_message, user=user_message, temperature=temperature)
            return '{"scenes":[{"dur":"30s","scene":"通勤补涂防晒霜","line":"日常通勤注意防晒。"}]}'

        with mock.patch.object(text, "_chat", side_effect=fake_chat):
            result = text.gen_copy({
                "prompt": "夏季通勤防晒",
                "format": "script",
                "style": "种草",
                "dur": "30s",
                "platform": "小红书",
            })

        self.assertEqual(result["mode"], "script")
        for expected in ("未提供品牌名时不得虚构品牌", "不得自行补造数据", "绝对化"):
            self.assertIn(expected, captured["system"])
            self.assertIn(expected, captured["user"])

    def test_script_result_removes_unsupported_claims_and_offer_details(self):
        scenes = [{
            "dur": "30s",
            "scene": "镜头给到品牌名称，展示超强防晒效果。",
            "line": (
                "防晒必不可少，涂抹毫无负担，不必害怕阳光直射，全天候守护。"
                "完全不厚重，烈日下不怕晒黑。趁活动入手超划算！"
            ),
        }]

        cleaned = text.sanitize_script_scenes(scenes, "轻薄，日常通勤便于补涂")
        rendered = cleaned[0]["scene"] + cleaned[0]["line"]

        for forbidden in (
            "品牌名称", "超强", "完全不", "不怕晒黑", "活动", "超划算",
            "必不可少", "毫无负担", "不必害怕阳光直射", "全天候守护",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertIn("产品包装", cleaned[0]["scene"])
        self.assertIn("请以产品实际信息为准", cleaned[0]["line"])

    def test_script_result_keeps_offer_details_when_user_supplied_them(self):
        scenes = [{"dur": "30s", "scene": "展示活动海报", "line": "限时立减 100 元。"}]
        cleaned = text.sanitize_script_scenes(scenes, "品牌活动：限时立减 100 元")

        self.assertEqual(cleaned, scenes)

    def test_http_submission_validates_copy_before_pricing_and_paid_job_creation(self):
        source = (SERVER / "content_domains" / "core.py").read_text(encoding="utf-8")
        validate_at = source.index("body = text_domain.validate_copy_payload(body)")
        price_at = source.index("cost = points_domain.cost_of(kind, body)", validate_at)
        paid_job_at = source.index("jobs_store.create_paid_job(", validate_at)

        self.assertLess(validate_at, price_at)
        self.assertLess(validate_at, paid_job_at)

    def test_breakdown_partial_refund_intent_precedes_done_and_is_recoverable(self):
        source = (SERVER / "content_domains" / "core.py").read_text(encoding="utf-8")
        prepare_at = source.index("prepare_breakdown_batch_refund(")
        done_at = source.index('_set_terminal(job_id, "done"', prepare_at)
        reconcile_at = source.index("reconcile_breakdown_refund(job_id)", done_at)

        self.assertLess(prepare_at, done_at)
        self.assertLess(done_at, reconcile_at)
        self.assertIn("retry_breakdown_refunds(JOB_QUEUE_MAX)", source)


if __name__ == "__main__":
    unittest.main()
