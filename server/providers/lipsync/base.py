"""Pure lipsync capability and quote descriptors."""

from dataclasses import asdict, dataclass
from abc import ABC, abstractmethod


class ProviderSubmissionUnknownError(RuntimeError):
    """The provider may have accepted a paid create request."""

    outcome_unknown = True
    submitted = True


@dataclass(frozen=True)
class LipsyncCapability:
    name: str
    capability_version: str
    profile: str
    min_duration_ms: int
    max_duration_ms: int
    max_width: int
    max_height: int
    formats: tuple
    price_per_second: float
    minimum_external_cost: float

    def to_dict(self):
        value = asdict(self)
        value["formats"] = list(self.formats)
        return value

    def supports(self, *, duration_ms, width, height, source_format):
        return (
            self.min_duration_ms <= duration_ms <= self.max_duration_ms
            and 0 < width <= self.max_width
            and 0 < height <= self.max_height
            and str(source_format or "").lower() in self.formats
        )

    def estimate(self, duration_ms):
        billable_duration_ms = max(self.min_duration_ms, int(duration_ms))
        external = max(
            self.minimum_external_cost,
            round((billable_duration_ms / 1000.0) * self.price_per_second, 4),
        )
        return {
            "actual_duration_ms": int(duration_ms),
            "billable_duration_ms": billable_duration_ms,
            "external_estimate": external,
        }


class LipsyncProvider(ABC):
    """Network adapter contract. Implementations must make create idempotent."""

    name = ""
    model_version = ""
    supports_cancel = False
    supports_result_refetch = True
    requires_local_media = False

    @abstractmethod
    def create_job(self, request, idempotency_key):
        raise NotImplementedError

    @abstractmethod
    def get_job(self, provider_job_id):
        raise NotImplementedError

    def cancel_job(self, provider_job_id):
        raise NotImplementedError("provider cancellation is not supported")

    @abstractmethod
    def fetch_result(self, provider_job_id, destination):
        raise NotImplementedError
