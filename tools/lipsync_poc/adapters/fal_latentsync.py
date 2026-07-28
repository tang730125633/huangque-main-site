"""fal.ai LatentSync queue adapter for the stage 0-B lip-sync PoC."""

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
    file_data_uri,
    request_json,
)


_STATUS_MAP = {
    "IN_QUEUE": ProviderStatus.QUEUED,
    "IN_PROGRESS": ProviderStatus.RUNNING,
    "COMPLETED": ProviderStatus.SUCCEEDED,
}
_OFFICIAL_COST_PER_SECOND_USD = 0.005
_OFFICIAL_MINIMUM_CHARGE_USD = 0.20


class FalLatentSyncProvider(LipsyncProvider):
    """Run LatentSync through fal's durable asynchronous queue."""

    name = "fal-latentsync"

    def __init__(
        self,
        *,
        api_key=None,
        queue_base=None,
        model=None,
        http=request_json,
        downloader=download_file,
    ):
        self.api_key = api_key or os.environ.get("FAL_KEY", "")
        self.queue_base = (
            queue_base
            or os.environ.get("FAL_QUEUE_BASE")
            or "https://queue.fal.run"
        ).rstrip("/")
        self.model = (
            model
            or os.environ.get("FAL_LIPSYNC_MODEL")
            or "fal-ai/latentsync"
        ).strip("/")
        self.http = http
        self.downloader = downloader

    def capabilities(self):
        configured_rate = os.environ.get(
            "FAL_LIPSYNC_COST_PER_SECOND_USD"
        )
        configured_minimum = os.environ.get(
            "FAL_LIPSYNC_MINIMUM_CHARGE_USD"
        )
        has_override = any(
            value not in (None, "")
            for value in (configured_rate, configured_minimum)
        )
        return LipsyncCapabilities(
            provider=self.name,
            max_duration_ms=15_000,
            max_file_bytes=20 * 1024 * 1024,
            video_formats=("mp4", "mov"),
            audio_formats=("wav", "mp3", "m4a"),
            resolutions=("720p",),
            supports_face_target=False,
            supports_segment_speakers=False,
            supports_cancel=True,
            supports_result_refetch=True,
            output_may_contain_audio=True,
            notes=(
                "inline data URI submission",
                "fal durable queue",
                f"model={self.model}",
            ),
            cost_per_second_usd=optional_cost(
                configured_rate
                if configured_rate not in (None, "")
                else _OFFICIAL_COST_PER_SECOND_USD
            ),
            minimum_charge_usd=optional_cost(
                configured_minimum
                if configured_minimum not in (None, "")
                else _OFFICIAL_MINIMUM_CHARGE_USD
            ),
            pricing_source=(
                "environment_override"
                if has_override
                else "fal_model_page_2026-07-28"
            ),
        )

    def _headers(self):
        if not self.api_key:
            raise ValueError("FAL_KEY is required")
        return {
            "Accept": "application/json",
            "Authorization": f"Key {self.api_key}",
        }

    def _job_url(self, job_id, action):
        safe_id = quote(str(job_id), safe="")
        return (
            f"{self.queue_base}/{self.model}/requests/"
            f"{safe_id}/{action}"
        )

    def validate_input(self, request):
        capabilities = self.capabilities()
        if request.duration_ms > capabilities.max_duration_ms:
            raise ValueError("duration exceeds fal LatentSync capability")
        for path, formats in (
            (request.video_path, capabilities.video_formats),
            (request.audio_path, capabilities.audio_formats),
        ):
            path = Path(path)
            if not path.is_file():
                raise ValueError("provider input file is missing")
            if path.stat().st_size > capabilities.max_file_bytes:
                raise ValueError("fal inline input exceeds 20 MB")
            if path.suffix.lower().lstrip(".") not in formats:
                raise ValueError("provider input format is unsupported")
        if request.resolution not in capabilities.resolutions:
            raise ValueError("provider output resolution is unsupported")

    def _status_payload(self, job_id):
        response = self.http(
            "GET",
            self._job_url(job_id, "status"),
            headers=self._headers(),
            timeout=30,
        )
        return response.payload

    def _result_payload(self, job_id):
        response = self.http(
            "GET",
            self._job_url(job_id, "response"),
            headers=self._headers(),
            timeout=60,
        )
        return response.payload

    def _job(self, job_id, payload):
        raw_status = str(payload.get("status") or "IN_QUEUE").upper()
        status = _STATUS_MAP.get(raw_status)
        if status is None:
            status = ProviderStatus.FAILED
        if raw_status == "COMPLETED" and (
            payload.get("error") or payload.get("error_type")
        ):
            status = ProviderStatus.FAILED
        return ProviderJob(
            job_id=str(job_id),
            status=status,
            provider=self.name,
            metadata={
                "model": self.model,
                "provider_status": raw_status,
                "queue_position": payload.get("queue_position"),
                "error_type": payload.get("error_type"),
                "metrics": payload.get("metrics")
                if isinstance(payload.get("metrics"), dict)
                else {},
            },
        )

    def create_job(self, request):
        self.validate_input(request)
        response = self.http(
            "POST",
            f"{self.queue_base}/{self.model}",
            headers={
                **self._headers(),
                "X-Fal-No-Retry": "1",
            },
            json_body={
                "video_url": file_data_uri(
                    request.video_path,
                    self.capabilities().max_file_bytes,
                ),
                "audio_url": file_data_uri(
                    request.audio_path,
                    self.capabilities().max_file_bytes,
                ),
                "loop_mode": "loop",
                "seed": int(request.input_hash[:8], 16),
            },
            timeout=120,
        )
        job_id = str(response.payload.get("request_id") or "")
        if not job_id:
            raise ProviderHttpError(
                0,
                "fal_job_id_missing",
                "fal response did not include a request id",
            )
        return ProviderJob(
            job_id=job_id,
            status=ProviderStatus.QUEUED,
            provider=self.name,
            metadata={
                "model": self.model,
                "provider_status": "IN_QUEUE",
                "queue_position": response.payload.get("queue_position"),
            },
        )

    def get_job(self, job_id):
        payload = self._status_payload(job_id)
        job = self._job(job_id, payload)
        if job.status == ProviderStatus.SUCCEEDED:
            result = self._result_payload(job_id)
            if result.get("error") or result.get("error_type"):
                return ProviderJob(
                    job_id=str(job_id),
                    status=ProviderStatus.FAILED,
                    provider=self.name,
                    metadata={
                        **job.metadata,
                        "error_type": result.get("error_type"),
                    },
                )
        return job

    def cancel_job(self, job_id):
        current = self.get_job(job_id)
        if current.status == ProviderStatus.SUCCEEDED:
            return current
        response = self.http(
            "PUT",
            self._job_url(job_id, "cancel"),
            headers=self._headers(),
            json_body={},
            timeout=30,
        )
        cancel_status = str(response.payload.get("status") or "").upper()
        if cancel_status == "ALREADY_COMPLETED":
            return ProviderJob(
                str(job_id),
                ProviderStatus.SUCCEEDED,
                self.name,
                {"cancel_status": cancel_status},
            )
        # CANCELLATION_REQUESTED is not a terminal guarantee. Keeping the
        # provider status running preserves recovery and billing visibility.
        return ProviderJob(
            str(job_id),
            ProviderStatus.RUNNING,
            self.name,
            {"cancel_status": cancel_status or "CANCELLATION_REQUESTED"},
        )

    def fetch_result(self, job_id, destination):
        payload = self._result_payload(job_id)
        if payload.get("error") or payload.get("error_type"):
            raise ProviderHttpError(
                0,
                str(payload.get("error_type") or "fal_generation_failed"),
                str(payload.get("error") or "fal generation failed"),
            )
        video = payload.get("video")
        video = video if isinstance(video, dict) else {}
        output_url = video.get("url")
        if not output_url:
            raise ProviderHttpError(
                0,
                "fal_output_missing",
                "fal completed without a video URL",
            )
        downloaded = self.downloader(
            output_url,
            destination,
            timeout=180,
        )
        return ProviderResult(
            job_id=str(job_id),
            output_path=Path(destination),
            provider=self.name,
            metadata={
                "model": self.model,
                "download": downloaded,
                "content_type": video.get("content_type"),
                "file_size": video.get("file_size"),
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
            "code": "fal_provider_error",
            "message": str(redact(str(error))),
            "retryable": isinstance(error, (TimeoutError, ConnectionError)),
        }
