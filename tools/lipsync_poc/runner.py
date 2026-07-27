"""Provider-neutral PoC orchestration with atomic, redacted reports."""

import json
import os
import time
from pathlib import Path

from .adapters.base import ProviderStatus, TERMINAL_STATUSES
from .metrics.media_probe import probe_media
from .metrics.quality import empty_human_review, media_contract_metrics
from .redaction import redact


REPORT_VERSION = "1.0"


class PocRunError(RuntimeError):
    def __init__(self, code, message, report=None):
        super().__init__(message)
        self.code = code
        self.report = report or {}


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class PocRunner:
    def __init__(
        self,
        provider,
        probe=probe_media,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.provider = provider
        self.probe = probe
        self.clock = clock
        self.sleep = sleep

    def _wait(self, job, timeout_seconds, poll_seconds):
        deadline = self.clock() + timeout_seconds
        current = job
        while current.status not in TERMINAL_STATUSES:
            if self.clock() >= deadline:
                raise PocRunError("provider_timeout", "provider job timed out")
            self.sleep(poll_seconds)
            current = self.provider.get_job(current.job_id)
        return current

    def run(self, sample, output_dir, timeout_seconds=300, poll_seconds=2):
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise PocRunError(
                "invalid_polling",
                "timeout_seconds and poll_seconds must be positive",
            )
        request = sample.to_request()
        capabilities = self.provider.capabilities()
        started = self.clock()
        try:
            self.provider.validate_input(request)
            job = self.provider.create_job(request)
            job = self._wait(job, timeout_seconds, poll_seconds)
            if job.status != ProviderStatus.SUCCEEDED:
                raise PocRunError(
                    "provider_terminal",
                    f"provider ended in {job.status.value}",
                )
            output_dir = Path(output_dir)
            output_path = output_dir / "media" / f"{sample.sample_id}.mp4"
            result = self.provider.fetch_result(job.job_id, output_path)
            source_video = self.probe(sample.video_path)
            source_audio = self.probe(sample.audio_path)
            provider_output = self.probe(result.output_path)
            expected_dimensions = (
                {"width": 720, "height": 1280}
                if sample.ratio == "9:16"
                else {"width": 1280, "height": 720}
                if sample.ratio == "16:9"
                else {"width": 720, "height": 720}
            )
            report = {
                "report_version": REPORT_VERSION,
                "sample_id": sample.sample_id,
                "input_hash": sample.input_hash,
                "provider": capabilities.provider,
                "provider_job_id": job.job_id,
                "status": "succeeded",
                "duration_ms": sample.duration_ms,
                "ratio": sample.ratio,
                "speaking_mode": sample.speaking_mode,
                "elapsed_ms": round((self.clock() - started) * 1000),
                "capabilities": capabilities.as_dict(),
                "media": {
                    "source_video": source_video,
                    "source_audio": source_audio,
                    "provider_output": provider_output,
                },
                "automated_metrics": media_contract_metrics(
                    source_video,
                    provider_output,
                    {
                        **expected_dimensions,
                        "fps": sample.fps,
                    },
                ),
                "human_review": empty_human_review(),
                "provider_metadata": redact(dict(result.metadata)),
            }
            _atomic_json(
                output_dir / "reports" / f"{sample.sample_id}.json",
                report,
            )
            return report
        except PocRunError:
            raise
        except Exception as error:
            normalized = redact(dict(self.provider.normalize_error(error)))
            raise PocRunError(
                str(normalized.get("code") or "provider_error"),
                str(normalized.get("message") or "provider operation failed"),
                {"provider_error": normalized},
            ) from error
