"""Explicit, side-effect-free Provider selection for short-drama visuals."""

import os

from .heygen_cinematic import HeyGenCinematicShotProvider
from .grok_xai import GrokXaiShotProvider


PROVIDERS = {
    "heygen_cinematic": HeyGenCinematicShotProvider,
    "grok": GrokXaiShotProvider,
    "grok_xai": GrokXaiShotProvider,
}


def load_by_name(name):
    provider_type = PROVIDERS.get(str(name or "").strip().lower())
    return provider_type() if provider_type else None


def load_from_environment():
    selected = str(
        os.getenv("HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER") or ""
    ).strip().lower()
    return load_by_name(selected)


def capability_snapshot():
    selected = str(
        os.getenv("HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER") or ""
    ).strip().lower()
    provider = load_from_environment()
    if not selected:
        return {
            "selected": None,
            "configured": False,
            "code": "provider_not_selected",
            "message": "尚未选择真实画面 Provider",
            "capability": None,
        }
    if provider is None:
        return {
            "selected": selected,
            "configured": False,
            "code": "provider_unsupported",
            "message": "配置的真实画面 Provider 当前不受支持",
            "capability": None,
        }
    configured = bool(provider.configured)
    return {
        "selected": provider.name,
        "requested": selected,
        "configured": configured,
        "code": "provider_ready" if configured else "provider_not_configured",
        "message": (
            "Provider 已配置，可进行镜头请求预检"
            if configured
            else "已选择 Provider，但 API Key 尚未配置"
        ),
        "capability": provider.capability.to_dict(),
    }
