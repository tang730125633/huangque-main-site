import tempfile
import unittest
from pathlib import Path

from tools.lipsync_poc.evaluation import build_summary
from tools.lipsync_poc.state import atomic_json


def report(sample_id, provider, *, status="succeeded", elapsed=1000):
    return {
        "sample_id": sample_id,
        "provider": provider,
        "status": status,
        "elapsed_ms": elapsed,
        "billing_status": "provider_succeeded",
        "estimated_cost_usd": 0.1,
        "media": {
            "provider_output": {
                "audio_stream_count": 0,
            },
        },
        "human_review": {
            "review_status": "complete",
            "lip_sync_score_1_to_5": 5,
            "identity_score_1_to_5": 4,
            "visual_quality_score_1_to_5": 4,
            "whole_sentence_offset": False,
            "av_offset_ms": 80,
        },
    }


class LipsyncPocEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, provider, payload):
        path = (
            self.root
            / provider
            / "reports"
            / f"{payload['sample_id']}.json"
        )
        atomic_json(path, payload)

    def test_go_requires_quality_cost_silence_and_billing_gates(self):
        for index in range(20):
            self._write(
                "sync-labs",
                report(f"sample-{index:02d}", "sync-labs"),
            )
        summary = build_summary(self.root, ["sync-labs"])
        provider = summary["providers"][0]
        self.assertEqual("go", provider["decision"])
        self.assertEqual("sync-labs", summary["default_provider"])
        self.assertEqual("go", summary["overall_decision"])
        self.assertEqual(80, provider["human_review"]["av_offset_ms_p95"])

    def test_unresolved_billing_is_hard_no_go(self):
        payload = report("sample-01", "fal-latentsync", status="failed")
        payload["billing_status"] = "requires_reconciliation"
        self._write("fal-latentsync", payload)
        summary = build_summary(self.root, ["fal-latentsync"])
        self.assertEqual("no_go", summary["providers"][0]["decision"])
        self.assertEqual(
            ["sample-01"],
            summary["providers"][0]["unresolved_billing_samples"],
        )

    def test_pending_human_review_is_conditional_not_fabricated(self):
        payload = report("sample-01", "sync-labs")
        payload["human_review"] = {
            "review_status": "pending",
            "lip_sync_score_1_to_5": None,
        }
        self._write("sync-labs", payload)
        summary = build_summary(self.root, ["sync-labs"])
        provider = summary["providers"][0]
        self.assertEqual("conditional_go", provider["decision"])
        self.assertIsNone(provider["human_review"]["usable_rate"])
        self.assertIsNone(summary["default_provider"])

    def test_one_perfect_sample_cannot_be_declared_go(self):
        self._write(
            "sync-labs",
            report("sample-01", "sync-labs"),
        )
        summary = build_summary(self.root, ["sync-labs"])
        provider = summary["providers"][0]
        self.assertEqual("conditional_go", provider["decision"])
        self.assertFalse(provider["gates"]["sample_count_gte_minimum"])
        self.assertEqual(20, provider["minimum_sample_count"])
        self.assertIsNone(summary["default_provider"])


if __name__ == "__main__":
    unittest.main()
