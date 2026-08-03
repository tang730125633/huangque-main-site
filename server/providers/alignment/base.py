"""Provider-neutral forced-alignment protocol."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class AlignmentCapabilities:
    provider: str
    model_version: str
    supports_word_timestamps: bool
    supports_cancel: bool
    supports_resume: bool
    supports_result_refetch: bool
    max_audio_seconds: int
    accepted_formats: tuple[str, ...] = ("wav",)
    language_models: tuple[str, ...] = ("zh-CN",)
    real_forced_alignment: bool = False

    def to_dict(self):
        value = asdict(self)
        value["accepted_formats"] = list(self.accepted_formats)
        value["language_models"] = list(self.language_models)
        return value


@dataclass(frozen=True)
class ProviderJob:
    provider_job_id: str
    status: str
    trace_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResult:
    provider_job_id: str
    status: str
    segments: tuple[dict[str, Any], ...]
    diagnostics: dict[str, Any] = field(default_factory=dict)


class AlignmentProviderError(RuntimeError):
    def __init__(self, code, message, *, provider_status="failed", retryable=False):
        super().__init__(str(message))
        self.code = str(code)
        self.provider_status = str(provider_status)
        self.retryable = bool(retryable)


class ForcedAlignmentProvider:
    """Adapters translate provider protocols; domain policy stays elsewhere."""

    name = "abstract"

    def capabilities(self) -> AlignmentCapabilities:
        raise NotImplementedError

    def create_job(self, request: dict[str, Any]) -> ProviderJob:
        raise NotImplementedError

    def get_job(self, provider_job_id: str) -> ProviderJob:
        raise NotImplementedError

    def cancel_job(self, provider_job_id: str) -> ProviderJob:
        raise NotImplementedError

    def fetch_result(self, provider_job_id: str) -> ProviderResult:
        raise NotImplementedError

    def normalize_error(self, error: Exception) -> AlignmentProviderError:
        if isinstance(error, AlignmentProviderError):
            return error
        return AlignmentProviderError(
            "alignment_provider_error",
            "forced-alignment provider request failed",
        )
