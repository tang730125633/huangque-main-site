"""Deterministic test-only provider."""

import io
import math
import struct
import wave

from .base import GeneratedSoundEffect, SoundEffectProvider


class MockSoundEffectProvider(SoundEffectProvider):
    name = "mock"
    model = "deterministic-tone-v1"

    def generate(self, *, prompt, duration_seconds, loop=False):
        sample_rate = 16000
        frames = max(1, int(float(duration_seconds) * sample_rate))
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            for index in range(frames):
                envelope = min(1.0, index / 320.0, (frames - index) / 320.0)
                value = int(
                    2200 * envelope *
                    math.sin(2 * math.pi * 440 * index / sample_rate)
                )
                wav.writeframesraw(struct.pack("<h", value))
        return GeneratedSoundEffect(
            data=output.getvalue(),
            content_type="audio/wav",
            provider=self.name,
            model=self.model,
        )
