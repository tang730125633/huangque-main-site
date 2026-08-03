"""Configurable lipsync capability and simulation-pricing catalog."""

import os

from .base import LipsyncCapability


CATALOG_VERSION = "lipsync-catalog-v1"
PRICING_VERSION = "lipsync-simulation-2026-07"


def _price(name, default):
    try:
        return max(0.0, float(os.environ.get(name, default) or default))
    except (TypeError, ValueError):
        return float(default)

_CATALOG = {
    "fal-latentsync": LipsyncCapability(
        name="fal-latentsync",
        capability_version="fal-latentsync-contract-v1",
        profile="standard",
        min_duration_ms=1000,
        max_duration_ms=300000,
        max_width=1920,
        max_height=1920,
        formats=("mp4", "mov"),
        price_per_second=0.005,
        minimum_external_cost=0.20,
    ),
    "musetalk": LipsyncCapability(
        name="musetalk",
        capability_version="musetalk-http-contract-v1",
        profile="standard",
        min_duration_ms=1000,
        max_duration_ms=300000,
        max_width=1920,
        max_height=1920,
        formats=("mp4", "mov"),
        # MuseTalk is self-hosted. These operator-controlled values account for
        # GPU time; they are not presented as an upstream software licence fee.
        price_per_second=_price("MUSETALK_COST_PER_SECOND_USD", 0.001),
        minimum_external_cost=_price("MUSETALK_MINIMUM_CHARGE_USD", 0.01),
    ),
}


def _enabled_names():
    raw = str(os.environ.get("HQ_SHORT_DRAMA_LIPSYNC_PROVIDERS") or "").strip()
    if not raw:
        # Preserve the pre-MuseTalk default. A real self-hosted endpoint must
        # always be enabled explicitly by deployment configuration.
        return ["fal-latentsync"]
    return [
        name for name in (part.strip() for part in raw.split(","))
        if name in _CATALOG
    ]


def default_provider():
    configured = str(
        os.environ.get("HQ_SHORT_DRAMA_LIPSYNC_DEFAULT_PROVIDER") or ""
    ).strip()
    enabled = _enabled_names()
    if configured:
        return configured if configured in enabled else ""
    return enabled[0] if len(enabled) == 1 else ""


def get_provider(name, profile="standard"):
    capability = _CATALOG.get(str(name or "").strip())
    if capability is None or capability.name not in _enabled_names():
        return None
    if str(profile or "standard") != capability.profile:
        return None
    return capability


def catalog_snapshot():
    names = _enabled_names()
    return {
        "catalog_version": CATALOG_VERSION,
        "pricing_version": PRICING_VERSION,
        "default_provider": default_provider() or None,
        "providers": [_CATALOG[name].to_dict() for name in names],
    }
