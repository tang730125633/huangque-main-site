"""Face analysis provider contracts and deterministic local implementation."""

from .base import FaceAnalysisProvider, FaceProviderCapabilities
from .fake import FakeFaceAnalysisProvider


__all__ = [
    "FaceAnalysisProvider",
    "FaceProviderCapabilities",
    "FakeFaceAnalysisProvider",
]
