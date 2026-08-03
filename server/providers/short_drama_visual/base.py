"""Stable contract for a billable short-drama visual provider."""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass


class VisualProviderError(RuntimeError):
    def __init__(self, code, message, submitted=False):
        super().__init__(message)
        self.code = str(code)
        self.submitted = bool(submitted)


@dataclass(frozen=True)
class ShotVisualCapability:
    provider: str
    ratios: tuple
    minimum_seconds: int
    maximum_seconds: int
    supports_cancel: bool
    supports_result_refetch: bool

    def to_dict(self):
        return asdict(self)


class ShotVisualProvider(ABC):
    @property
    @abstractmethod
    def capability(self):
        raise NotImplementedError

    @abstractmethod
    def validate_request(self, request):
        raise NotImplementedError

    @abstractmethod
    def create_job(self, request):
        raise NotImplementedError

    @abstractmethod
    def get_job(self, provider_job_id):
        raise NotImplementedError

    def cancel_job(self, provider_job_id):
        raise VisualProviderError(
            "provider_cancel_unsupported",
            "当前画面 Provider 不支持取消已经提交的任务",
            submitted=True,
        )

    @abstractmethod
    def fetch_result(self, provider_job_id, result_url):
        raise NotImplementedError
