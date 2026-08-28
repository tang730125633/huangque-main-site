"""Stable contract for a billable short-drama visual provider."""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass


HEYGEN_PROMPT_MAX_CHARACTERS = 2000
MINIMAX_PROMPT_MAX_CHARACTERS = 7000
# xAI does not currently publish a smaller character limit for video prompts.
# Keep the product-side ceiling aligned with the largest prompt contract that
# the short-drama editor can safely preflight before a billable submission.
GROK_PROMPT_MAX_CHARACTERS = 7000


class VisualProviderError(RuntimeError):
    def __init__(self, code, message, submitted=False):
        super().__init__(message)
        self.code = str(code)
        self.submitted = bool(submitted)


def validate_prompt(prompt, maximum):
    value = str(prompt or "").strip()
    if not value:
        raise VisualProviderError(
            "visual_prompt_required", "镜头缺少可执行的画面提示词"
        )
    if len(value) > int(maximum):
        raise VisualProviderError(
            "visual_prompt_too_long",
            "镜头最终提示词不能超过 %d 个字符" % int(maximum),
        )
    return value


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

    def bind_reconciled_job_id(self, provider_job_id, request):
        """Normalize an operator-confirmed upstream ID before persistence."""
        return str(provider_job_id or "").strip()

    @abstractmethod
    def fetch_result(self, provider_job_id, result_url):
        raise NotImplementedError
