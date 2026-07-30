import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from tools.lipsync_poc.adapters import (
    FalLatentSyncProvider,
    LipsyncRequest,
    ProviderStatus,
    SyncLabsProvider,
)
from tools.lipsync_poc.adapters.http import (
    HttpJsonResponse,
    ProviderHttpError,
)


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        payload = self.responses.pop(0)
        return HttpJsonResponse(200, {}, payload)


class RealProviderContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / "source.mp4"
        self.audio = self.root / "master.wav"
        self.video.write_bytes(b"video")
        self.audio.write_bytes(b"audio")
        self.request = LipsyncRequest(
            sample_id="front-01",
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

    def test_missing_credentials_fail_before_any_http_request(self):
        for provider in (
            SyncLabsProvider(api_key="", http=FakeHttp([])),
            FalLatentSyncProvider(api_key="", http=FakeHttp([])),
        ):
            with self.subTest(provider=provider.name):
                with self.assertRaises(ValueError):
                    provider.create_job(self.request)
                self.assertEqual([], provider.http.calls)

    def test_sync_labs_create_poll_and_refetch_contract(self):
        http = FakeHttp([
            {"id": "sync-job-1", "status": "PENDING", "model": "lipsync-2"},
            {"id": "sync-job-1", "status": "PROCESSING", "model": "lipsync-2"},
            {
                "id": "sync-job-1",
                "status": "COMPLETED",
                "model": "lipsync-2",
                "outputUrl": "https://provider.test/result.mp4?token=secret",
                "outputDuration": 5.0,
            },
        ])
        downloads = []

        def download(url, destination, **kwargs):
            downloads.append((url, Path(destination), kwargs))
            Path(destination).write_bytes(b"result")
            return {"size_bytes": 6, "sha256": "b" * 64}

        provider = SyncLabsProvider(
            api_key="sync-secret",
            http=http,
            downloader=download,
        )
        created = provider.create_job(self.request)
        self.assertEqual(ProviderStatus.QUEUED, created.status)
        self.assertEqual("sync-job-1", created.job_id)
        create_kwargs = http.calls[0][2]
        self.assertIn("multipart/form-data", create_kwargs["content_type"])
        self.assertIn(b'filename="source.mp4"', create_kwargs["body"])
        self.assertIn(b'filename="master.wav"', create_kwargs["body"])
        self.assertNotIn(b"sync-secret", create_kwargs["body"])

        polled = provider.get_job(created.job_id)
        self.assertEqual(ProviderStatus.RUNNING, polled.status)
        destination = self.root / "out.mp4"
        result = provider.fetch_result(created.job_id, destination)
        self.assertEqual(destination, result.output_path)
        self.assertEqual(1, len(downloads))
        self.assertTrue(destination.is_file())
        self.assertFalse(provider.capabilities().supports_cancel)

    def test_fal_create_poll_cancel_and_fetch_contract(self):
        http = FakeHttp([
            {"request_id": "fal-job-1", "queue_position": 1},
            {"status": "IN_PROGRESS"},
            {"status": "IN_PROGRESS"},
            {"status": "CANCELLATION_REQUESTED"},
            {
                "video": {
                    "url": "https://provider.test/result.mp4?token=secret",
                    "content_type": "video/mp4",
                    "file_size": 6,
                }
            },
        ])
        downloads = []

        def download(url, destination, **kwargs):
            downloads.append((url, Path(destination), kwargs))
            Path(destination).write_bytes(b"result")
            return {"size_bytes": 6, "sha256": "c" * 64}

        provider = FalLatentSyncProvider(
            api_key="fal-secret",
            http=http,
            downloader=download,
        )
        created = provider.create_job(self.request)
        self.assertEqual("fal-job-1", created.job_id)
        self.assertEqual(ProviderStatus.QUEUED, created.status)
        payload = http.calls[0][2]["json_body"]
        self.assertTrue(payload["video_url"].startswith("data:video/mp4;base64,"))
        self.assertTrue(payload["audio_url"].startswith("data:audio/"))
        self.assertEqual(int(self.request.input_hash[:8], 16), payload["seed"])
        self.assertNotIn("fal-secret", str(payload))

        polled = provider.get_job(created.job_id)
        self.assertEqual(ProviderStatus.RUNNING, polled.status)
        canceled = provider.cancel_job(created.job_id)
        self.assertEqual(ProviderStatus.RUNNING, canceled.status)
        self.assertEqual("PUT", http.calls[3][0])
        self.assertTrue(http.calls[3][1].endswith("/cancel"))
        self.assertEqual(
            "CANCELLATION_REQUESTED",
            canceled.metadata["cancel_status"],
        )

        destination = self.root / "fal.mp4"
        result = provider.fetch_result(created.job_id, destination)
        self.assertEqual(destination, result.output_path)
        self.assertEqual(1, len(downloads))

    def test_provider_error_normalization_redacts_credentials(self):
        error = ProviderHttpError(
            401,
            "unauthorized",
            "Authorization: Basic c2VjcmV0\n"
            "https://provider.test/result?token=secret",
        )
        for provider in (
            SyncLabsProvider(api_key="key"),
            FalLatentSyncProvider(api_key="key"),
        ):
            with self.subTest(provider=provider.name):
                normalized = provider.normalize_error(error)
                encoded = str(normalized)
                self.assertNotIn("c2VjcmV0", encoded)
                self.assertNotIn("token=secret", encoded)
                self.assertFalse(normalized["retryable"])

    def test_fal_official_minimum_charge_is_applied(self):
        with patch.dict(
            "os.environ",
            {
                "FAL_LIPSYNC_COST_PER_SECOND_USD": "",
                "FAL_LIPSYNC_MINIMUM_CHARGE_USD": "",
            },
        ):
            capabilities = FalLatentSyncProvider(
                api_key="fal-secret"
            ).capabilities()
        self.assertEqual(0.2, capabilities.estimate_cost_usd(1_000))
        self.assertEqual(0.2, capabilities.estimate_cost_usd(40_000))
        self.assertEqual(0.205, capabilities.estimate_cost_usd(41_000))
        self.assertEqual(0.2, capabilities.minimum_charge_usd)
        self.assertEqual(0.005, capabilities.cost_per_second_usd)

    def test_fal_pricing_environment_overrides_are_explicit(self):
        with patch.dict(
            "os.environ",
            {
                "FAL_LIPSYNC_COST_PER_SECOND_USD": "0.01",
                "FAL_LIPSYNC_MINIMUM_CHARGE_USD": "0.30",
            },
        ):
            capabilities = FalLatentSyncProvider(
                api_key="fal-secret"
            ).capabilities()
        self.assertEqual(0.3, capabilities.estimate_cost_usd(15_000))
        self.assertEqual("environment_override", capabilities.pricing_source)


if __name__ == "__main__":
    unittest.main()
