"""Process-local registry for executable lipsync provider adapters.

Capability metadata and executable adapters deliberately stay separate.  A
deployment must register an adapter before paid HTTP submissions are accepted.
"""

import importlib
import os
import threading


_lock = threading.Lock()
_providers = {}


def register_provider(name, provider):
    name = str(name or "").strip()
    if not name or provider is None:
        raise ValueError("runtime provider name and adapter are required")
    with _lock:
        _providers[name] = provider


def unregister_provider(name):
    with _lock:
        _providers.pop(str(name or "").strip(), None)


def get_provider(name):
    with _lock:
        provider = _providers.get(str(name or "").strip())
    return provider() if callable(provider) else provider


def load_from_environment():
    """Register one deployment-owned adapter factory from an explicit import path."""
    name = str(os.environ.get(
        "HQ_SHORT_DRAMA_LIPSYNC_RUNTIME_PROVIDER", ""
    )).strip()
    reference = str(os.environ.get(
        "HQ_SHORT_DRAMA_LIPSYNC_RUNTIME_FACTORY", ""
    )).strip()
    if not name and not reference:
        return None
    if not name or ":" not in reference:
        raise RuntimeError(
            "lipsync runtime provider and module:factory must both be configured"
        )
    module_name, attribute = reference.rsplit(":", 1)
    factory = getattr(importlib.import_module(module_name), attribute)
    if not callable(factory):
        raise RuntimeError("lipsync runtime factory is not callable")
    provider = factory()
    if provider is None:
        raise RuntimeError("lipsync runtime factory returned no adapter")
    if str(getattr(provider, "name", "") or "") != name:
        raise RuntimeError("lipsync runtime adapter name does not match config")
    register_provider(name, provider)
    return name


def clear():
    with _lock:
        _providers.clear()
