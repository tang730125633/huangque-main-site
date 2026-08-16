import sys
import unittest
from pathlib import Path
from unittest import mock


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains.short_drama_alignment_normalize import normalize_result
from content_domains import short_drama_alignment
from providers.alignment import (
    DeterministicLocalProvider,
    ProviderResult,
)
from tests.fixtures.short_drama_alignment_provider import FakeAlignmentProvider


def request():
    return {
        "shots": [{
            "shot_id": "shot-1",
            "lines": [{
                "shot_id": "shot-1",
                "line_id": "line-1",
                "text": "你好，世界！",
                "audio_start_ms": 100,
                "audio_end_ms": 2100,
            }],
        }],
    }


class AlignmentProviderContractTests(unittest.TestCase):
    def test_fake_provider_cannot_be_enabled_by_production_configuration(self):
        self.assertNotIn(
            "fake-zh-alignment",
            short_drama_alignment.PROVIDER_FACTORIES,
        )
        with mock.patch.dict(
            "os.environ",
            {
                "HQ_SHORT_DRAMA_ALIGNMENT_PROVIDER": "fake-zh-alignment",
                "HQ_SHORT_DRAMA_ALIGNMENT_REAL_ENABLED": "1",
            },
            clear=False,
        ):
            with self.assertRaises(
                short_drama_alignment.AlignmentError
            ) as context:
                short_drama_alignment._provider_selection()
        self.assertEqual(
            "alignment_provider_unavailable",
            context.exception.code,
        )

    def test_builtin_provider_implements_full_recovery_contract(self):
        provider = DeterministicLocalProvider()
        capabilities = provider.capabilities()
        job = provider.create_job(request())
        self.assertEqual("succeeded", provider.get_job(job.provider_job_id).status)
        result = provider.fetch_result(job.provider_job_id)
        self.assertEqual(job.provider_job_id, result.provider_job_id)
        self.assertTrue(capabilities.supports_result_refetch)
        self.assertFalse(capabilities.real_forced_alignment)
        self.assertEqual(
            "canceled", provider.cancel_job(job.provider_job_id).status
        )

    def test_provider_transcript_never_rewrites_locked_text(self):
        provider = FakeAlignmentProvider()
        result = ProviderResult(
            "job-1",
            "succeeded",
            ({
                "line_id": "line-1",
                "transcript": "泥号世界",
                "words": [
                    {"token": "你", "start_ms": 120, "end_ms": 320,
                     "confidence": 0.98},
                    {"token": "好", "start_ms": 320, "end_ms": 520,
                     "confidence": 0.97},
                    {"token": "世", "start_ms": 600, "end_ms": 800,
                     "confidence": 0.96},
                    {"token": "界", "start_ms": 800, "end_ms": 1000,
                     "confidence": 0.95},
                ],
            },),
        )
        timeline, quality = normalize_result(
            request(), result, provider.capabilities()
        )
        self.assertEqual("你好，世界！", timeline[0]["text"])
        self.assertEqual("泥号世界", timeline[0]["provider_transcript"])
        self.assertEqual(["你", "好", "世", "界"], [
            item["token"] for item in timeline[0]["words"]
        ])
        self.assertEqual([], quality["unmatched_tokens"])

    def test_unmatched_and_low_confidence_are_not_packaged_as_success(self):
        provider = FakeAlignmentProvider()
        result = ProviderResult(
            "job-2",
            "succeeded",
            ({
                "line_id": "line-1",
                "transcript": "你好世",
                "words": [
                    {"token": "你", "start_ms": 120, "end_ms": 320,
                     "confidence": 0.99},
                    {"token": "好", "start_ms": 320, "end_ms": 520,
                     "confidence": 0.40},
                    {"token": "世", "start_ms": 600, "end_ms": 800,
                     "confidence": 0.90},
                ],
            },),
        )
        timeline, quality = normalize_result(
            request(), result, provider.capabilities()
        )
        self.assertEqual("partial_match", timeline[0]["status"])
        self.assertEqual("界", quality["unmatched_tokens"][0]["token"])
        self.assertTrue(quality["low_confidence_ranges"])
        self.assertEqual(
            {"alignment_unmatched_transcript", "alignment_low_confidence"},
            {item["code"] for item in quality["blockers"]},
        )

    def test_fully_unmatched_line_has_no_fabricated_timestamps(self):
        provider = FakeAlignmentProvider(result={"segments": []})
        result = provider.fetch_result("job-3")
        timeline, quality = normalize_result(
            request(), result, provider.capabilities()
        )
        self.assertIsNone(timeline[0]["subtitle_start_ms"])
        self.assertIsNone(timeline[0]["subtitle_end_ms"])
        self.assertEqual("unmatched", timeline[0]["status"])
        self.assertEqual(0.0, quality["coverage"])
        artifacts = short_drama_alignment._artifact_payloads({
            "alignment_hash": "a" * 64,
            "master_audio_hash": "m" * 64,
            "transcript_hash": "t" * 64,
            "timeline": timeline,
            "quality": quality,
            "manual_reviewed": False,
        })
        self.assertIn('"status":"unmatched"', artifacts["alignment_json"])
        self.assertNotIn("Dialogue:", artifacts["ass"])
        self.assertNotIn("-->", artifacts["webvtt"])


if __name__ == "__main__":
    unittest.main()
