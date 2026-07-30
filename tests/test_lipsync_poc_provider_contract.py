import tempfile
import unittest
from pathlib import Path

from tools.lipsync_poc.adapters import (
    LipsyncProvider,
    LipsyncRequest,
    MockLipsyncProvider,
    ProviderStatus,
)


class LipsyncPocProviderContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / "source.mp4"
        self.audio = self.root / "master.wav"
        self.video.write_bytes(b"video")
        self.audio.write_bytes(b"audio")
        self.request = LipsyncRequest(
            sample_id="sample-01",
            video_path=self.video,
            audio_path=self.audio,
            transcript="测试对白",
            speaking_mode="visible",
            character_key="host",
            face_target={"type": "character", "value": "host"},
            duration_ms=5000,
            ratio="9:16",
            resolution="720p",
            fps=25,
            input_hash="a" * 64,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_contract_contains_all_poc_operations(self):
        expected = {
            "capabilities",
            "validate_input",
            "create_job",
            "get_job",
            "cancel_job",
            "fetch_result",
            "normalize_error",
        }
        self.assertTrue(expected.issubset(LipsyncProvider.__abstractmethods__))

    def test_mock_is_offline_and_never_billable(self):
        capabilities = MockLipsyncProvider().capabilities()
        self.assertEqual("mock", capabilities.provider)
        self.assertIn("never billable", capabilities.notes)

    def test_mock_job_is_deterministic_for_same_input_hash(self):
        provider = MockLipsyncProvider()
        first = provider.create_job(self.request)
        second = provider.create_job(self.request)
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(ProviderStatus.SUCCEEDED, first.status)

    def test_mock_result_can_be_refetched(self):
        provider = MockLipsyncProvider()
        job = provider.create_job(self.request)
        first = self.root / "first.mp4"
        second = self.root / "second.mp4"
        provider.fetch_result(job.job_id, first)
        provider.fetch_result(job.job_id, second)
        self.assertEqual(b"video", first.read_bytes())
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_mock_rejects_unsupported_resolution(self):
        request = LipsyncRequest(
            **{
                **self.request.__dict__,
                "resolution": "1080p",
            }
        )
        with self.assertRaises(ValueError):
            MockLipsyncProvider().validate_input(request)

    def test_unknown_job_is_normalized_without_credentials(self):
        normalized = MockLipsyncProvider().normalize_error(KeyError("missing"))
        self.assertEqual("mock_provider_error", normalized["code"])
        self.assertFalse(normalized["retryable"])


if __name__ == "__main__":
    unittest.main()
