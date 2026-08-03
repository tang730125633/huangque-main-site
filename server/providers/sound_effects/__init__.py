"""Replaceable text-to-sound-effects providers."""

from .base import (
    ProviderConfigurationError,
    ProviderError,
    SoundEffectProvider,
)
from .elevenlabs import ElevenLabsSoundEffectProvider
from .mock import MockSoundEffectProvider


def configured_provider(env):
    name = str(env.get("SOUND_EFFECT_PROVIDER") or "elevenlabs").strip().lower()
    if name == "mock":
        if str(env.get("SOUND_EFFECT_ALLOW_MOCK") or "").strip().lower() not in {
            "1", "true", "yes",
        }:
            raise ProviderConfigurationError(
                "mock 音效 Provider 仅允许在测试环境显式开启"
            )
        return MockSoundEffectProvider()
    if name != "elevenlabs":
        raise ProviderConfigurationError("不支持的音效 Provider：%s" % name)
    return ElevenLabsSoundEffectProvider.from_env(env)


def capability(env):
    name = str(env.get("SOUND_EFFECT_PROVIDER") or "elevenlabs").strip().lower()
    configured = False
    detail = ""
    try:
        provider = configured_provider(env)
        configured = True
        model = provider.model
    except ProviderConfigurationError as error:
        model = str(env.get("ELEVENLABS_SOUND_EFFECT_MODEL") or
                    "eleven_text_to_sound_v2")
        detail = str(error)
    return {
        "provider": name,
        "configured": configured,
        "model": model,
        "max_duration_seconds": 30,
        "supports_loop": True,
        "detail": detail,
    }


__all__ = [
    "ProviderConfigurationError",
    "ProviderError",
    "SoundEffectProvider",
    "configured_provider",
    "capability",
]
