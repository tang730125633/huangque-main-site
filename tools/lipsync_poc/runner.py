"""Provider-neutral PoC orchestration with recoverable, redacted state."""

import hashlib
import time

from .adapters.base import ProviderStatus, TERMINAL_STATUSES
from .metrics.media_probe import probe_media
from .metrics.media_output import ensure_silent_video
from .metrics.quality import empty_human_review, media_contract_metrics
from .paths import artifact_paths
from .redaction import redact
from .state import STATE_VERSION, atomic_json, exclusive_lock, load_json


REPORT_VERSION = "1.2"


class PocRunError(RuntimeError):
    def __init__(self, code, message, report=None):
        safe_message = str(redact(str(message)))
        super().__init__(safe_message)
        self.code = str(code)
        self.report = redact(report or {})


def _request_id(provider, input_hash):
    source = f"{provider}:{input_hash}".encode("utf-8")
    return hashlib.sha256(source).hexdigest()


def _status_value(status):
    return status.value if isinstance(status, ProviderStatus) else str(status)


def _cost_details(capabilities, duration_ms):
    estimated = capabilities.estimate_cost_usd(duration_ms)
    duration_cost = (
        round(
            capabilities.cost_per_second_usd
            * duration_ms
            / 1000,
            6,
        )
        if capabilities.cost_per_second_usd is not None
        else None
    )
    return estimated, {
        "billing_unit": capabilities.billing_unit,
        "cost_per_second_usd": capabilities.cost_per_second_usd,
        "minimum_charge_usd": capabilities.minimum_charge_usd,
        "duration_ms": duration_ms,
        "duration_cost_usd": duration_cost,
        "pricing_source": capabilities.pricing_source,
        "source": (
            "configured"
            if estimated is not None
            else "unconfigured"
        ),
    }


def recovery_capabilities(state, capabilities):
    state = state or {}
    cancel = state.get("cancel")
    cancel = cancel if isinstance(cancel, dict) else {}
    effective_status = (
        cancel.get("provider_status")
        or state.get("last_provider_status")
    )
    if not effective_status and state.get("status") == "succeeded":
        effective_status = ProviderStatus.SUCCEEDED.value
    effective_status = str(effective_status or "unknown").lower()
    if effective_status == "cancelled":
        effective_status = ProviderStatus.CANCELED.value

    has_job = bool(state.get("provider_job_id"))
    can_resume = has_job and effective_status in {
        ProviderStatus.QUEUED.value,
        ProviderStatus.RUNNING.value,
        "unknown",
    }
    can_refetch = bool(
        has_job
        and effective_status == ProviderStatus.SUCCEEDED.value
        and capabilities.supports_result_refetch
    )
    return {
        "effective_provider_status": effective_status,
        "can_resume": can_resume,
        "can_refetch": can_refetch,
    }


class PocRunner:
    def __init__(
        self,
        provider,
        probe=probe_media,
        clock=time.monotonic,
        sleep=time.sleep,
        wall_clock=time.time,
    ):
        self.provider = provider
        self.probe = probe
        self.clock = clock
        self.sleep = sleep
        self.wall_clock = wall_clock

    def _write_state(self, path, state, **updates):
        current = dict(state or {})
        current.update(updates)
        current["state_version"] = STATE_VERSION
        current["updated_at"] = int(self.wall_clock())
        atomic_json(path, current)
        return current

    def _claim_new_run(self, paths, sample):
        """Atomically reserve one provider submission for this sample."""
        try:
            with exclusive_lock(paths.state):
                existing = load_json(paths.state)
                if existing is not None:
                    code = (
                        "submission_reconciliation_required"
                        if existing.get("billing_status")
                        == "requires_reconciliation"
                        else "existing_run_state"
                    )
                    raise PocRunError(
                        code,
                        "persisted run state exists; reconcile, resume, "
                        "refetch, or use a new output directory",
                    )
                now = int(self.wall_clock())
                return self._write_state(
                    paths.state,
                    {
                        "provider": paths.provider,
                        "sample_id": sample.sample_id,
                        "input_hash": sample.input_hash,
                        "request_id": _request_id(
                            paths.provider,
                            sample.input_hash,
                        ),
                        "provider_job_id": None,
                        "status": "submitting",
                        "stage": "create_job",
                        "billing_status": "requires_reconciliation",
                        "created_at": now,
                    },
                )
        except TimeoutError as error:
            raise PocRunError(
                "submission_claim_busy",
                "another process is claiming this provider submission",
            ) from error

    def _wait(self, job, timeout_seconds, poll_seconds, on_status):
        deadline = self.clock() + timeout_seconds
        current = job
        on_status(current)
        while current.status not in TERMINAL_STATUSES:
            if self.clock() >= deadline:
                raise PocRunError("provider_timeout", "provider job timed out")
            self.sleep(poll_seconds)
            current = self.provider.get_job(current.job_id)
            on_status(current)
        return current

    def _human_review(self, report_path):
        try:
            existing = load_json(report_path)
        except (OSError, ValueError):
            existing = None
        if isinstance(existing, dict) and isinstance(
            existing.get("human_review"), dict
        ):
            return existing["human_review"]
        return empty_human_review()

    def _validate_recovery_state(
        self,
        state,
        sample,
        provider,
        capabilities,
        mode,
    ):
        if not state:
            raise PocRunError(
                "recovery_state_missing",
                "no persisted provider job is available",
            )
        if state.get("provider") != provider:
            raise PocRunError(
                "recovery_provider_mismatch",
                "persisted job belongs to another provider",
            )
        if state.get("sample_id") != sample.sample_id:
            raise PocRunError(
                "recovery_sample_mismatch",
                "persisted job belongs to another sample",
            )
        if state.get("input_hash") != sample.input_hash:
            raise PocRunError(
                "recovery_input_mismatch",
                "persisted job input hash does not match current sample",
            )
        if not state.get("provider_job_id"):
            raise PocRunError(
                "ambiguous_submission",
                "submission state has no provider job id; resolve billing "
                "before creating another job",
            )
        recovery = recovery_capabilities(state, capabilities)
        if mode == "resume" and not recovery["can_resume"]:
            raise PocRunError(
                "resume_not_allowed",
                "provider job is not in a resumable state",
                {"recovery": recovery},
            )
        if mode == "refetch" and not recovery["can_refetch"]:
            raise PocRunError(
                "refetch_not_allowed",
                "provider job is not in a refetchable state",
                {"recovery": recovery},
            )

    def _normalize_error(self, error):
        if isinstance(error, PocRunError):
            return redact({
                "code": error.code,
                "message": str(error),
                "retryable": error.code in {
                    "provider_timeout",
                    "provider_poll_failed",
                },
            })
        try:
            normalized = self.provider.normalize_error(error)
        except Exception:
            normalized = {
                "code": "provider_error",
                "message": "provider error normalization failed",
                "retryable": False,
            }
        if not isinstance(normalized, dict):
            normalized = {
                "code": "provider_error",
                "message": "provider operation failed",
                "retryable": False,
            }
        return redact(dict(normalized))

    def _cancel_timeout(self, job_id, capabilities):
        if not job_id:
            return {"attempted": False, "reason": "job_id_missing"}
        if not capabilities.supports_cancel:
            return {"attempted": False, "reason": "cancel_unsupported"}
        try:
            canceled = self.provider.cancel_job(job_id)
            return {
                "attempted": True,
                "succeeded": True,
                "provider_status": _status_value(canceled.status),
            }
        except Exception as error:
            return {
                "attempted": True,
                "succeeded": False,
                "error": self._normalize_error(error),
            }

    def _failure_report(
        self,
        sample,
        paths,
        capabilities,
        state,
        error,
        started,
        cancel=None,
    ):
        normalized = self._normalize_error(error)
        recovery = recovery_capabilities(state, capabilities)
        estimated_cost, cost_basis = _cost_details(
            capabilities,
            sample.duration_ms,
        )
        report = {
            "report_version": REPORT_VERSION,
            "sample_id": sample.sample_id,
            "input_hash": sample.input_hash,
            "provider": paths.provider,
            "artifact_namespace": paths.provider,
            "report_file": (
                f"{paths.provider}/reports/{sample.sample_id}.json"
            ),
            "provider_job_id": state.get("provider_job_id"),
            "request_id": state.get("request_id"),
            "status": "failed",
            "failure_stage": state.get("stage"),
            "billing_status": state.get("billing_status", "unknown"),
            "last_provider_status": state.get("last_provider_status"),
            "effective_provider_status": recovery[
                "effective_provider_status"
            ],
            "elapsed_ms": round((self.clock() - started) * 1000),
            "estimated_cost_usd": estimated_cost,
            "cost_basis": cost_basis,
            "capabilities": capabilities.as_dict(),
            "provider_error": normalized,
            "cancel": cancel,
            "recovery": {
                "can_resume": recovery["can_resume"],
                "can_refetch": recovery["can_refetch"],
            },
            "human_review": self._human_review(paths.report),
        }
        atomic_json(paths.report, report)
        return report

    def _finish(
        self,
        sample,
        paths,
        capabilities,
        state,
        job,
        started,
    ):
        if job.status != ProviderStatus.SUCCEEDED:
            raise PocRunError(
                "provider_terminal",
                f"provider ended in {_status_value(job.status)}",
            )
        state = self._write_state(
            paths.state,
            state,
            status="fetching",
            stage="fetch_result",
            last_provider_status=_status_value(job.status),
        )
        result = self.provider.fetch_result(job.job_id, paths.media)
        source_video = self.probe(sample.video_path)
        source_audio = self.probe(sample.audio_path)
        output_sanitization = ensure_silent_video(
            result.output_path,
            self.probe,
        )
        provider_output = self.probe(result.output_path)
        if int(provider_output.get("audio_stream_count") or 0) != 0:
            raise PocRunError(
                "provider_audio_not_removed",
                "provider output still contains an audio stream",
            )
        expected_dimensions = (
            {"width": 720, "height": 1280}
            if sample.ratio == "9:16"
            else {"width": 1280, "height": 720}
            if sample.ratio == "16:9"
            else {"width": 720, "height": 720}
        )
        succeeded_state = {
            **state,
            "status": "succeeded",
            "last_provider_status": ProviderStatus.SUCCEEDED.value,
        }
        recovery = recovery_capabilities(
            succeeded_state,
            capabilities,
        )
        estimated_cost, cost_basis = _cost_details(
            capabilities,
            sample.duration_ms,
        )
        report = {
            "report_version": REPORT_VERSION,
            "sample_id": sample.sample_id,
            "input_hash": sample.input_hash,
            "provider": paths.provider,
            "artifact_namespace": paths.provider,
            "report_file": (
                f"{paths.provider}/reports/{sample.sample_id}.json"
            ),
            "provider_job_id": job.job_id,
            "request_id": state["request_id"],
            "status": "succeeded",
            "billing_status": "provider_succeeded",
            "effective_provider_status": recovery[
                "effective_provider_status"
            ],
            "recovery": {
                "can_resume": recovery["can_resume"],
                "can_refetch": recovery["can_refetch"],
            },
            "duration_ms": sample.duration_ms,
            "ratio": sample.ratio,
            "speaking_mode": sample.speaking_mode,
            "elapsed_ms": round((self.clock() - started) * 1000),
            "estimated_cost_usd": estimated_cost,
            "cost_basis": cost_basis,
            "capabilities": capabilities.as_dict(),
            "media_file": (
                f"{paths.provider}/media/{sample.sample_id}.mp4"
            ),
            "media": {
                "source_video": source_video,
                "source_audio": source_audio,
                "provider_output": provider_output,
            },
            "output_sanitization": output_sanitization,
            "automated_metrics": media_contract_metrics(
                source_video,
                provider_output,
                {
                    **expected_dimensions,
                    "fps": sample.fps,
                },
            ),
            "human_review": self._human_review(paths.report),
            "provider_metadata": redact(dict(result.metadata)),
        }
        atomic_json(paths.report, report)
        self._write_state(
            paths.state,
            state,
            status="succeeded",
            stage="done",
            billing_status="provider_succeeded",
            last_provider_status=_status_value(job.status),
            media_file=report["media_file"],
        )
        return report

    def run(
        self,
        sample,
        output_dir,
        timeout_seconds=300,
        poll_seconds=2,
        resume=False,
        refetch=False,
    ):
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise PocRunError(
                "invalid_polling",
                "timeout_seconds and poll_seconds must be positive",
            )
        if resume and refetch:
            raise PocRunError(
                "invalid_recovery_mode",
                "resume and refetch are mutually exclusive",
            )
        request = sample.to_request()
        capabilities = self.provider.capabilities()
        paths = artifact_paths(
            output_dir,
            capabilities.provider,
            sample.sample_id,
        )
        state = load_json(paths.state)
        if resume or refetch:
            self._validate_recovery_state(
                state,
                sample,
                paths.provider,
                capabilities,
                "resume" if resume else "refetch",
            )
        else:
            self.provider.validate_input(request)
            state = self._claim_new_run(paths, sample)
        started = self.clock()
        job = None
        try:
            if resume or refetch:
                job = self.provider.get_job(state["provider_job_id"])
                state = self._write_state(
                    paths.state,
                    state,
                    status="running",
                    stage="polling",
                    last_provider_status=_status_value(job.status),
                )
            else:
                job = self.provider.create_job(request)
                state = self._write_state(
                    paths.state,
                    state,
                    provider_job_id=job.job_id,
                    status="running",
                    stage="polling",
                    billing_status="possibly_billable",
                    last_provider_status=_status_value(job.status),
                )

            if refetch:
                if not capabilities.supports_result_refetch:
                    raise PocRunError(
                        "refetch_unsupported",
                        "provider does not support result refetch",
                    )
                if job.status != ProviderStatus.SUCCEEDED:
                    raise PocRunError(
                        "provider_not_complete",
                        "provider job is not ready for result refetch",
                    )
            else:
                job = self._wait(
                    job,
                    timeout_seconds,
                    poll_seconds,
                    lambda current: self._write_state(
                        paths.state,
                        state,
                        status="running",
                        stage="polling",
                        last_provider_status=_status_value(current.status),
                    ),
                )
            return self._finish(
                sample,
                paths,
                capabilities,
                state,
                job,
                started,
            )
        except Exception as error:
            try:
                persisted = load_json(paths.state)
            except (OSError, ValueError):
                persisted = None
            if persisted:
                state = persisted
            if job is not None and not (state or {}).get("provider_job_id"):
                state = {
                    **(state or {}),
                    "provider_job_id": job.job_id,
                    "last_provider_status": _status_value(job.status),
                    "billing_status": "possibly_billable",
                }
            normalized = self._normalize_error(error)
            cancel = None
            if normalized.get("code") == "provider_timeout":
                cancel = self._cancel_timeout(
                    (state or {}).get("provider_job_id"),
                    capabilities,
                )
            provider_job_id = (state or {}).get("provider_job_id")
            ambiguous_submission = bool(
                not provider_job_id
                and (state or {}).get("status") == "submitting"
            )
            billing_status = (
                "requires_reconciliation"
                if provider_job_id or ambiguous_submission
                else (state or {}).get("billing_status")
                or "not_submitted"
            )
            state = self._write_state(
                paths.state,
                state or {
                    "provider": paths.provider,
                    "sample_id": sample.sample_id,
                    "input_hash": sample.input_hash,
                    "request_id": _request_id(
                        paths.provider,
                        sample.input_hash,
                    ),
                    "created_at": int(self.wall_clock()),
                },
                status=(
                    "reconciliation_required"
                    if ambiguous_submission
                    else "failed"
                ),
                stage=(
                    "reconcile_submission"
                    if ambiguous_submission
                    else (state or {}).get("stage")
                ),
                billing_status=billing_status,
                error=normalized,
                cancel=cancel,
            )
            report = self._failure_report(
                sample,
                paths,
                capabilities,
                state,
                error,
                started,
                cancel,
            )
            raise PocRunError(
                str(normalized.get("code") or "provider_error"),
                str(
                    normalized.get("message")
                    or "provider operation failed"
                ),
                report,
            ) from error
