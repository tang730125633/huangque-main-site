"""Deterministic no-network adapter used to verify the PoC contract."""

import shutil
from pathlib import Path

from .base import (
    LipsyncCapabilities,
    LipsyncProvider,
    LipsyncRequest,
    ProviderJob,
    ProviderResult,
    ProviderStatus,
)


class MockLipsyncProvider(LipsyncProvider):
    """Copies the input video and never performs a billable operation."""

    name = "mock"

    def __init__(self):
        self._jobs = {}

    def capabilities(self):
        return LipsyncCapabilities(
            provider=self.name,
            max_duration_ms=15_000,
            max_file_bytes=100 * 1024 * 1024,
            video_formats=("mp4", "mov"),
            audio_formats=("wav", "mp3", "m4a"),
            resolutions=("720p",),
            supports_face_target=True,
            supports_segment_speakers=False,
            supports_cancel=True,
            supports_result_refetch=True,
            output_may_contain_audio=False,
            notes=("offline contract test only", "never billable"),
            cost_per_second_usd=0.0,
            minimum_charge_usd=0.0,
            pricing_source="offline_mock",
        )

    def validate_input(self, request):
        capabilities = self.capabilities()
        if request.duration_ms > capabilities.max_duration_ms:
            raise ValueError("duration exceeds mock provider capability")
        if (
            request.video_path.stat().st_size > capabilities.max_file_bytes
            or request.audio_path.stat().st_size > capabilities.max_file_bytes
        ):
            raise ValueError("input exceeds mock provider file-size capability")
        if request.video_path.suffix.lower().lstrip(".") not in capabilities.video_formats:
            raise ValueError("unsupported video format")
        if request.audio_path.suffix.lower().lstrip(".") not in capabilities.audio_formats:
            raise ValueError("unsupported audio format")
        if request.resolution not in capabilities.resolutions:
            raise ValueError("unsupported resolution")

    def create_job(self, request):
        self.validate_input(request)
        job_id = "mock-" + request.input_hash[:24]
        self._jobs[job_id] = {
            "request": request,
            "status": ProviderStatus.SUCCEEDED,
        }
        return ProviderJob(job_id, ProviderStatus.SUCCEEDED, self.name)

    def get_job(self, job_id):
        row = self._jobs.get(job_id)
        if row is None:
            raise KeyError("unknown mock job")
        return ProviderJob(job_id, row["status"], self.name)

    def cancel_job(self, job_id):
        row = self._jobs.get(job_id)
        if row is None:
            raise KeyError("unknown mock job")
        if row["status"] != ProviderStatus.SUCCEEDED:
            row["status"] = ProviderStatus.CANCELED
        return ProviderJob(job_id, row["status"], self.name)

    def fetch_result(self, job_id, destination):
        row = self._jobs.get(job_id)
        if row is None:
            raise KeyError("unknown mock job")
        if row["status"] != ProviderStatus.SUCCEEDED:
            raise RuntimeError("mock job is not complete")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(row["request"].video_path, destination)
        return ProviderResult(
            job_id,
            destination,
            self.name,
            {"mode": "offline-copy", "billable": False},
        )

    def normalize_error(self, error):
        return {
            "code": "mock_provider_error",
            "message": str(error),
            "retryable": isinstance(error, TimeoutError),
        }
