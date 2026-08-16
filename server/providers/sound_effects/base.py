"""Provider contract for short-drama AI sound effects."""

from dataclasses import dataclass


class ProviderError(RuntimeError):
    def __init__(self, message, *, retryable=False, provider_status=None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.provider_status = provider_status


class ProviderConfigurationError(ProviderError):
    pass


@dataclass(frozen=True)
class GeneratedSoundEffect:
    data: bytes
    content_type: str
    provider: str
    model: str
    request_id: str = ""
    billing_units: int = 0


class SoundEffectProvider:
    name = ""
    model = ""

    def generate(self, *, prompt, duration_seconds, loop=False):
        raise NotImplementedError
