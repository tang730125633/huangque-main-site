"""Explicit deterministic fallback adapter for subtitle alignment."""

from __future__ import annotations

import uuid

from .base import (
    AlignmentCapabilities,
    ForcedAlignmentProvider,
    ProviderJob,
    ProviderResult,
)


class DeterministicLocalProvider(ForcedAlignmentProvider):
    name = "deterministic-local"
    model_version = "estimated-char-v1"

    def __init__(self):
        self._results = {}

    def capabilities(self):
        return AlignmentCapabilities(
            provider=self.name,
            model_version=self.model_version,
            supports_word_timestamps=True,
            supports_cancel=True,
            supports_resume=False,
            supports_result_refetch=True,
            max_audio_seconds=3600,
            accepted_formats=("wav", "mp3", "m4a"),
            language_models=("zh-CN",),
            real_forced_alignment=False,
        )

    def create_job(self, request):
        job_id = str(uuid.uuid4())
        segments = []
        for shot in request.get("shots") or []:
            for line in shot.get("lines") or []:
                text = str(line.get("text") or "")
                tokens = [item for item in text if not item.isspace()]
                start_ms = int(line["audio_start_ms"])
                end_ms = int(line["audio_end_ms"])
                duration = max(1, end_ms - start_ms)
                words = []
                for index, token in enumerate(tokens):
                    start = start_ms + round(duration * index / len(tokens))
                    end = start_ms + round(duration * (index + 1) / len(tokens))
                    words.append({
                        "token": token,
                        "start_ms": start,
                        "end_ms": max(start + 1, end),
                        "confidence": 0.0,
                    })
                segments.append({
                    "line_id": line.get("line_id"),
                    "transcript": text,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "confidence": 0.0,
                    "words": words,
                    "degradation": "deterministic_estimate",
                })
        self._results[job_id] = ProviderResult(
            provider_job_id=job_id,
            status="succeeded",
            segments=tuple(segments),
            diagnostics={"estimated": True},
        )
        return ProviderJob(job_id, "succeeded")

    def get_job(self, provider_job_id):
        status = "succeeded" if provider_job_id in self._results else "failed"
        return ProviderJob(provider_job_id, status)

    def cancel_job(self, provider_job_id):
        return ProviderJob(provider_job_id, "canceled")

    def fetch_result(self, provider_job_id):
        return self._results[provider_job_id]
