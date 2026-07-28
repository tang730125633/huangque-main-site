"""Stable, vendor-neutral contract for real lip-sync provider evaluation."""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple


class ProviderStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_STATUSES = {
    ProviderStatus.SUCCEEDED,
    ProviderStatus.FAILED,
    ProviderStatus.CANCELED,
}


def optional_cost(value):
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("provider cost must be a non-negative number") from exc
    if parsed < 0:
        raise ValueError("provider cost must be a non-negative number")
    return parsed


@dataclass(frozen=True)
class LipsyncCapabilities:
    provider: str
    max_duration_ms: int
    max_file_bytes: int
    video_formats: Tuple[str, ...]
    audio_formats: Tuple[str, ...]
    resolutions: Tuple[str, ...]
    supports_face_target: bool
    supports_segment_speakers: bool
    supports_cancel: bool
    supports_result_refetch: bool
    output_may_contain_audio: bool
    notes: Tuple[str, ...] = ()
    cost_per_second_usd: Optional[float] = None
    minimum_charge_usd: Optional[float] = None
    billing_unit: str = "output_second"
    pricing_source: str = "unconfigured"

    def as_dict(self):
        return asdict(self)

    def estimate_cost_usd(self, duration_ms):
        duration_ms = int(duration_ms)
        if duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        if (
            self.cost_per_second_usd is None
            and self.minimum_charge_usd is None
        ):
            return None
        duration_cost = (
            float(self.cost_per_second_usd or 0)
            * duration_ms
            / 1000
        )
        return round(
            max(
                duration_cost,
                float(self.minimum_charge_usd or 0),
            ),
            6,
        )


@dataclass(frozen=True)
class LipsyncRequest:
    sample_id: str
    video_path: Path
    audio_path: Path
    transcript: str
    speaking_mode: str
    character_key: Optional[str]
    face_target: Optional[Mapping[str, Any]]
    duration_ms: int
    ratio: str
    resolution: str
    fps: int
    input_hash: str


@dataclass(frozen=True)
class ProviderJob:
    job_id: str
    status: ProviderStatus
    provider: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResult:
    job_id: str
    output_path: Path
    provider: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


class LipsyncProvider(ABC):
    """Contract exercised by PoC adapters before production integration."""

    @abstractmethod
    def capabilities(self) -> LipsyncCapabilities:
        raise NotImplementedError

    @abstractmethod
    def validate_input(self, request: LipsyncRequest) -> None:
        raise NotImplementedError

    @abstractmethod
    def create_job(self, request: LipsyncRequest) -> ProviderJob:
        raise NotImplementedError

    @abstractmethod
    def get_job(self, job_id: str) -> ProviderJob:
        raise NotImplementedError

    @abstractmethod
    def cancel_job(self, job_id: str) -> ProviderJob:
        raise NotImplementedError

    @abstractmethod
    def fetch_result(self, job_id: str, destination: Path) -> ProviderResult:
        raise NotImplementedError

    @abstractmethod
    def normalize_error(self, error: Exception) -> Mapping[str, Any]:
        raise NotImplementedError
