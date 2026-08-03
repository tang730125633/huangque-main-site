"""Forced-alignment provider contracts and built-in adapters."""

from .base import (
    AlignmentCapabilities,
    AlignmentProviderError,
    ForcedAlignmentProvider,
    ProviderJob,
    ProviderResult,
)
from .deterministic_local import DeterministicLocalProvider
from .faster_whisper_local import FasterWhisperLocalProvider

__all__ = [
    "AlignmentCapabilities",
    "AlignmentProviderError",
    "DeterministicLocalProvider",
    "FasterWhisperLocalProvider",
    "ForcedAlignmentProvider",
    "ProviderJob",
    "ProviderResult",
]
