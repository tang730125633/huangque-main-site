"""Production-facing lipsync capability contracts.

Capability metadata stays separate from runtime adapters. Deployments opt in
to a network adapter through the runtime factory environment variables.
"""

from .catalog import catalog_snapshot, get_provider
from .base import LipsyncProvider, ProviderSubmissionUnknownError
from .runtime import (
    get_provider as get_runtime_provider,
    load_from_environment,
    register_provider,
)

__all__ = [
    "LipsyncProvider", "ProviderSubmissionUnknownError",
    "catalog_snapshot", "get_provider",
    "get_runtime_provider", "register_provider",
    "load_from_environment",
]
