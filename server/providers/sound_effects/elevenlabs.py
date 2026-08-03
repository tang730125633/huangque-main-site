"""ElevenLabs text-to-sound-effects adapter.

The provider currently returns the generated MP3 in the response body.  Keeping
the HTTP integration behind this adapter prevents billing, retries and project
state from depending on vendor-specific fields.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from .base import (
    GeneratedSoundEffect,
    ProviderConfigurationError,
    ProviderError,
    SoundEffectProvider,
)


class ElevenLabsSoundEffectProvider(SoundEffectProvider):
    name = "elevenlabs"

    def __init__(self, api_key, *, base_url, model, timeout=120):
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise ProviderConfigurationError("未配置 ELEVENLABS_API_KEY")
        self.base_url = str(base_url or "https://api.elevenlabs.io").rstrip("/")
        self.model = str(model or "eleven_text_to_sound_v2").strip()
        self.timeout = max(10, int(timeout or 120))

    @classmethod
    def from_env(cls, env):
        return cls(
            env.get("ELEVENLABS_API_KEY"),
            base_url=env.get("ELEVENLABS_API_BASE") or
            "https://api.elevenlabs.io",
            model=env.get("ELEVENLABS_SOUND_EFFECT_MODEL") or
            "eleven_text_to_sound_v2",
            timeout=env.get("ELEVENLABS_SOUND_EFFECT_TIMEOUT") or 120,
        )

    def generate(self, *, prompt, duration_seconds, loop=False):
        duration = float(duration_seconds)
        if duration < 0.5 or duration > 30:
            raise ValueError("音效时长必须在 0.5 到 30 秒之间")
        payload = json.dumps({
            "text": str(prompt or "").strip(),
            "duration_seconds": duration,
            "loop": bool(loop),
            "prompt_influence": 0.45,
            "model_id": self.model,
        }, ensure_ascii=False).encode("utf-8")
        query = urllib.parse.urlencode({"output_format": "mp3_44100_128"})
        request = urllib.request.Request(
            self.base_url + "/v1/sound-generation?" + query,
            data=payload,
            method="POST",
            headers={
                "xi-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read()
                if not data:
                    raise ProviderError("音效 Provider 返回了空文件")
                return GeneratedSoundEffect(
                    data=data,
                    content_type=(response.headers.get("Content-Type") or
                                  "audio/mpeg").split(";", 1)[0],
                    provider=self.name,
                    model=self.model,
                    request_id=response.headers.get("request-id") or "",
                    billing_units=int(
                        response.headers.get("character-cost") or 0
                    ),
                )
        except urllib.error.HTTPError as error:
            try:
                detail = error.read().decode("utf-8", "replace")[:400]
            except Exception:
                detail = str(error)
            raise ProviderError(
                "ElevenLabs 音效生成失败（HTTP %s）：%s" %
                (error.code, detail),
                retryable=error.code in {408, 409, 429, 500, 502, 503, 504},
                provider_status=error.code,
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ProviderError(
                "ElevenLabs 音效服务暂时不可达：%s" % str(error)[:180],
                retryable=True,
            ) from error
