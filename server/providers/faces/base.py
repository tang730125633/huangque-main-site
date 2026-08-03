"""Pure provider protocol for project-scoped face detection and tracking."""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class FaceProviderCapabilities:
    name: str
    contract_version: str
    detector_version: str
    tracker_version: str
    matcher_version: str
    supports_occlusion: bool = True
    supports_reid: bool = False

    def to_dict(self):
        return asdict(self)


class FaceAnalysisProvider(ABC):
    """Provider output contains geometry and scores, never raw embeddings."""

    capabilities = None

    @abstractmethod
    def analyze(self, request):
        """Return detections, tracks, matches and per-segment proposals."""
        raise NotImplementedError
