"""Test-only forced-alignment provider."""

from __future__ import annotations

from providers.alignment.base import (
    AlignmentCapabilities,
    ForcedAlignmentProvider,
    ProviderJob,
    ProviderResult,
)


class FakeAlignmentProvider(ForcedAlignmentProvider):
    name = "fake-zh-alignment"

    def __init__(self, result=None, *, status="succeeded"):
        self.result = result
        self.status = status
        self.created = []
        self.canceled = []

    def capabilities(self):
        return AlignmentCapabilities(
            provider=self.name,
            model_version="fake-zh-v1",
            supports_word_timestamps=True,
            supports_cancel=True,
            supports_resume=True,
            supports_result_refetch=True,
            max_audio_seconds=600,
            real_forced_alignment=True,
        )

    def create_job(self, request):
        job_id = "fake-job-%d" % (len(self.created) + 1)
        self.created.append((job_id, request))
        return ProviderJob(job_id, self.status, trace_id="fake-trace")

    def get_job(self, provider_job_id):
        return ProviderJob(provider_job_id, self.status, trace_id="fake-trace")

    def cancel_job(self, provider_job_id):
        self.canceled.append(provider_job_id)
        self.status = "canceled"
        return ProviderJob(provider_job_id, "canceled", trace_id="fake-trace")

    def fetch_result(self, provider_job_id):
        if isinstance(self.result, ProviderResult):
            return self.result
        return ProviderResult(
            provider_job_id=provider_job_id,
            status=self.status,
            segments=tuple((self.result or {}).get("segments") or ()),
            diagnostics=dict((self.result or {}).get("diagnostics") or {}),
        )
