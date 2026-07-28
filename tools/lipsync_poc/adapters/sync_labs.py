"""Sync Labs direct API adapter for the stage 0-B lip-sync PoC."""

import os
from pathlib import Path
from urllib.parse import quote

from ..redaction import redact
from .base import (
    LipsyncCapabilities,
    LipsyncProvider,
    ProviderJob,
    ProviderResult,
    ProviderStatus,
    optional_cost,
)
from .http import (
    ProviderHttpError,
    download_file,
    encode_multipart,
    request_json,
)


_STATUS_MAP = {
    "PENDING": ProviderStatus.QUEUED,
    "PROCESSING": ProviderStatus.RUNNING,
    "COMPLETED": ProviderStatus.SUCCEEDED,
    "FAILED": ProviderStatus.FAILED,
    "REJECTED": ProviderStatus.FAILED,
}


class SyncLabsProvider(LipsyncProvider):
    """Upload local video/audio pairs to Sync's asynchronous generation API."""

    name = "sync-labs"

    def __init__(
        self,
        *,
        api_key=None,
        base_url=None,
        model=None,
        http=request_json,
        downloader=download_file,
    ):
        self.api_key = api_key or os.environ.get("SYNC_API_KEY", "")
        self.base_url = (
            base_url
            or os.environ.get("SYNC_API_BASE")
            or "https://api.sync.so"
        ).rstrip("/")
        self.model = (
            model
            or os.environ.get("SYNC_LIPSYNC_MODEL")
            or "lipsync-2"
        )
        self.http = http
        self.downloader = downloader

    def capabilities(self):
        configured_cost = os.environ.get(
            "SYNC_LIPSYNC_COST_PER_SECOND_USD"
        )
        return LipsyncCapabilities(
            provider=self.name,
            max_duration_ms=60_000,
            max_file_bytes=20 * 1024 * 1024,
            video_formats=("mp4", "mov"),
            audio_formats=("wav", "mp3", "m4a"),
            resolutions=("720p", "1080p"),
            supports_face_target=False,
            supports_segment_speakers=False,
            supports_cancel=False,
            supports_result_refetch=True,
            output_may_contain_audio=True,
            notes=(
                "direct multipart upload",
                "in-progress cancellation is not supported by the provider",
                f"model={self.model}",
            ),
            cost_per_second_usd=optional_cost(
                configured_cost
            ),
            pricing_source=(
                "environment_override"
                if configured_cost not in (None, "")
                else "unconfigured"
            ),
        )

    def _headers(self):
        if not self.api_key:
            raise ValueError("SYNC_API_KEY is required")
        return {
            "Accept": "application/json",
            "x-api-key": self.api_key,
        }

    def validate_input(self, request):
        capabilities = self.capabilities()
        if request.duration_ms > capabilities.max_duration_ms:
            raise ValueError("duration exceeds Sync Labs capability")
        for path, formats in (
            (request.video_path, capabilities.video_formats),
            (request.audio_path, capabilities.audio_formats),
        ):
            path = Path(path)
            if not path.is_file():
                raise ValueError("provider input file is missing")
            if path.stat().st_size > capabilities.max_file_bytes:
                raise ValueError("Sync direct-upload input exceeds 20 MB")
            if path.suffix.lower().lstrip(".") not in formats:
                raise ValueError("provider input format is unsupported")
        if request.resolution not in capabilities.resolutions:
            raise ValueError("provider output resolution is unsupported")

    def _generation(self, job_id):
        response = self.http(
            "GET",
            f"{self.base_url}/v2/generate/{quote(str(job_id), safe='')}",
            headers=self._headers(),
            timeout=30,
        )
        return response.payload

    def _job(self, payload):
        job_id = str(payload.get("id") or "")
        if not job_id:
            raise ProviderHttpError(
                0,
                "sync_job_id_missing",
                "Sync Labs response did not include a generation id",
            )
        raw_status = str(payload.get("status") or "PENDING").upper()
        status = _STATUS_MAP.get(raw_status)
        if status is None:
            raise ProviderHttpError(
                0,
                "sync_status_unknown",
                "Sync Labs returned an unknown generation status",
            )
        return ProviderJob(
            job_id=job_id,
            status=status,
            provider=self.name,
            metadata={
                "model": str(payload.get("model") or self.model),
                "provider_status": raw_status,
                "output_duration_seconds": payload.get("outputDuration"),
                "error_code": payload.get("errorCode"),
            },
        )

    def create_job(self, request):
        self.validate_input(request)
        body, content_type = encode_multipart(
            {
                "model": self.model,
                "options": '{"sync_mode":"cut_off"}',
                "outputFileName": request.sample_id,
            },
            {
                "video": request.video_path,
                "audio": request.audio_path,
            },
        )
        response = self.http(
            "POST",
            f"{self.base_url}/v2/generate",
            headers=self._headers(),
            body=body,
            content_type=content_type,
            timeout=120,
        )
        return self._job(response.payload)

    def get_job(self, job_id):
        return self._job(self._generation(job_id))

    def cancel_job(self, job_id):
        raise NotImplementedError(
            "Sync Labs does not support canceling an in-progress generation"
        )

    def fetch_result(self, job_id, destination):
        payload = self._generation(job_id)
        job = self._job(payload)
        if job.status != ProviderStatus.SUCCEEDED:
            raise RuntimeError("Sync Labs generation is not complete")
        output_url = payload.get("outputUrl")
        if not output_url:
            raise ProviderHttpError(
                0,
                "sync_output_missing",
                "Sync Labs completed without an output URL",
            )
        downloaded = self.downloader(
            output_url,
            destination,
            timeout=180,
        )
        return ProviderResult(
            job_id=job.job_id,
            output_path=Path(destination),
            provider=self.name,
            metadata={
                "model": job.metadata["model"],
                "provider_status": job.metadata["provider_status"],
                "output_duration_seconds": job.metadata[
                    "output_duration_seconds"
                ],
                "download": downloaded,
            },
        )

    def normalize_error(self, error):
        if isinstance(error, ProviderHttpError):
            return {
                "code": error.code,
                "message": str(redact(str(error))),
                "retryable": (
                    error.status == 0
                    or error.status == 429
                    or error.status >= 500
                ),
                "http_status": error.status or None,
            }
        return {
            "code": "sync_provider_error",
            "message": str(redact(str(error))),
            "retryable": isinstance(error, (TimeoutError, ConnectionError)),
        }
