"""Durable background consumer for paid lipsync jobs."""

import os
import subprocess
import threading
import time
from contextlib import closing
from pathlib import Path

from providers.lipsync import get_runtime_provider, load_from_environment

from . import (
    short_drama_assembly_plan,
    short_drama_lipsync_inputs,
    short_drama_lipsync_jobs,
    short_drama_lipsync_media,
    short_drama_lipsync_reconcile,
)


def _enabled(name, default="0"):
    return str(os.environ.get(name, default)).strip().lower() in {
        "1", "true", "yes", "on",
    }


def reconcile_enabled():
    return _enabled("HQ_SHORT_DRAMA_LIPSYNC_RECONCILE_ENABLED", "1")


def _remux_without_audio(source, destination):
    command = [
        os.environ.get("FFMPEG_BIN", "ffmpeg"),
        "-y", "-v", "error", "-i", str(source),
        "-map", "0:v:0", "-an", "-c:v", "copy",
        "-movflags", "+faststart", str(destination),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("lipsync media remux could not run") from error
    if result.returncode:
        raise RuntimeError(
            "lipsync media remux failed: " + str(result.stderr or "")[-300:]
        )


def run_once(
    db_factory, job_id, provider, worker_id, *,
    max_result_retries=8, request_builder=None, **finalize_options
):
    token = short_drama_lipsync_jobs.acquire_lease(
        db_factory, job_id, worker_id
    )
    if not token:
        return None
    current = None
    try:
        current = short_drama_lipsync_jobs.process_once(
            db_factory, job_id, provider, token,
            request_builder=request_builder,
        )
        if (
            current["state"] == "running"
            and current.get("result", {}).get("result_ready")
            and finalize_options
        ):
            try:
                current = short_drama_lipsync_jobs.finalize_result(
                    db_factory, job_id, provider, token, **finalize_options
                )
            except short_drama_lipsync_media.LipsyncMediaValidationError as error:
                current = short_drama_lipsync_jobs.fail_job(
                    db_factory, job_id, token,
                    "media_acceptance_failed", error,
                )
            except short_drama_lipsync_jobs.LipsyncResultManualReviewError as error:
                current = short_drama_lipsync_jobs.manual_review_job(
                    db_factory, job_id, token,
                    "provider_result_refetch_unsupported", error,
                )
            except (
                short_drama_lipsync_jobs.LipsyncResultRetryableError,
                short_drama_lipsync_media.LipsyncMediaInfrastructureError,
            ) as error:
                current = short_drama_lipsync_jobs.defer_result(
                    db_factory, job_id, token,
                    "result_stage_retryable", error,
                    max_attempts=max_result_retries,
                )
        return current
    finally:
        if current and current.get("state") in {
            "queued", "running", "cancel_pending",
        }:
            short_drama_lipsync_jobs.release_lease(
                db_factory, job_id, token
            )


class WorkerService:
    def __init__(
        self, db_factory, points_domain, *, output_root, work_dir=None,
        provider_resolver=get_runtime_provider,
        probe=short_drama_assembly_plan.probe_media,
        remux=_remux_without_audio,
        worker_count=1,
        max_result_retries=8,
    ):
        self.db_factory = db_factory
        self.ledger = short_drama_lipsync_jobs.PointsLedger(points_domain)
        self.output_root = Path(output_root)
        self.work_dir = Path(work_dir or self.output_root / ".lipsync-work")
        self.provider_resolver = provider_resolver
        self.probe = probe
        self.remux = remux
        self.worker_count = max(1, int(worker_count))
        self.max_result_retries = max(1, int(max_result_retries))
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.threads = []
        self.last_reconcile = 0
        self.last_success_at = 0
        self.consecutive_failures = 0
        self.last_error = ""
        self._health_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._thread_sequence = 0

    def provider_ready(self, name):
        try:
            return self.provider_resolver(str(name or "")) is not None
        except Exception:
            return False

    def live_worker_count(self):
        with self._thread_lock:
            return sum(thread.is_alive() for thread in self.threads)

    def is_healthy(self):
        return self.live_worker_count() > 0 and not self.stop_event.is_set()

    def health(self):
        with self._health_lock:
            return {
                "live_workers": self.live_worker_count(),
                "last_success_at": self.last_success_at,
                "consecutive_failures": self.consecutive_failures,
                "last_error": self.last_error,
            }

    def _due_jobs(self, limit=32, now=None):
        now = int(time.time()) if now is None else int(now)
        with closing(self.db_factory()) as conn:
            rows = conn.execute(
                "SELECT job.id,job.provider FROM short_drama_lipsync_jobs job "
                "JOIN short_drama_lipsync_attempts attempt "
                "ON attempt.id=job.attempt_id "
                "WHERE job.state IN ('queued','running','cancel_pending') "
                "AND attempt.state IN ('charged','linked') "
                "AND (job.next_poll_at IS NULL OR job.next_poll_at<=?) "
                "AND (job.lease_expires_at IS NULL "
                "OR job.lease_expires_at<=?) "
                "ORDER BY COALESCE(job.next_poll_at,0),job.created_at LIMIT ?",
                (now, now, max(1, int(limit))),
            ).fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]

    def run_cycle(self, worker_id="lipsync-worker", *, now=None):
        now = int(time.time()) if now is None else int(now)
        if reconcile_enabled() and now - self.last_reconcile >= 30:
            try:
                short_drama_lipsync_reconcile.run(
                    self.db_factory, self.ledger, now=now,
                )
                self.last_reconcile = now
            except Exception as error:
                print(
                    "[lipsync] reconcile failed: %s" % error,
                    flush=True,
                )
        processed = []
        for job_id, provider_name in self._due_jobs(now=now):
            try:
                provider = self.provider_resolver(provider_name)
                if provider is None:
                    continue
                request_builder = None
                if getattr(provider, "requires_local_media", False):
                    request_builder = lambda current_job_id, _identity: (
                        short_drama_lipsync_inputs.prepare_provider_request(
                            self.db_factory, current_job_id, self.work_dir
                        )
                    )
                result = run_once(
                    self.db_factory, job_id, provider, worker_id,
                    request_builder=request_builder,
                    work_dir=self.work_dir,
                    output_root=self.output_root,
                    probe=self.probe,
                    remux=self.remux,
                    max_result_retries=self.max_result_retries,
                )
            except Exception as error:
                print(
                    "[lipsync] worker failed job=%s: %s" % (job_id, error),
                    flush=True,
                )
                continue
            if result is not None:
                processed.append(job_id)
        return processed

    def _loop(self, index):
        worker_id = "lipsync-worker-%d" % index
        failures = 0
        while not self.stop_event.is_set():
            delay = 1.0
            try:
                self.run_cycle(worker_id)
                failures = 0
                with self._health_lock:
                    self.last_success_at = int(time.time())
                    self.consecutive_failures = 0
                    self.last_error = ""
            except Exception as error:
                failures += 1
                delay = min(30.0, float(2 ** min(failures, 5)))
                with self._health_lock:
                    self.consecutive_failures = failures
                    self.last_error = str(error)[:220]
                print(
                    "[lipsync] worker cycle failed worker=%s retry=%.0fs: %s"
                    % (worker_id, delay, error),
                    flush=True,
                )
            self.wake_event.wait(delay)
            self.wake_event.clear()

    def start(self):
        with self._thread_lock:
            self.threads = [
                thread for thread in self.threads if thread.is_alive()
            ]
            missing = self.worker_count - len(self.threads)
            for _unused in range(max(0, missing)):
                self._thread_sequence += 1
                index = self._thread_sequence
                thread = threading.Thread(
                    target=self._loop, args=(index,),
                    name="content-lipsync-worker-%d" % index, daemon=True,
                )
                thread.start()
                self.threads.append(thread)

    def wake(self):
        self.wake_event.set()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        with self._thread_lock:
            threads = list(self.threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1)


_service = None
_service_lock = threading.Lock()


def start_service(db_factory, points_domain, *, output_root):
    global _service
    if (
        not short_drama_lipsync_jobs.jobs_enabled()
        and not reconcile_enabled()
    ):
        return None
    with _service_lock:
        if _service is None:
            try:
                load_from_environment()
            except Exception as error:
                print(
                    "[lipsync] runtime adapter unavailable: %s" % error,
                    flush=True,
                )
            count = int(os.environ.get(
                "HQ_SHORT_DRAMA_LIPSYNC_WORKERS", "1"
            ) or 1)
            max_result_retries = int(os.environ.get(
                "HQ_SHORT_DRAMA_LIPSYNC_RESULT_RETRIES", "8"
            ) or 8)
            _service = WorkerService(
                db_factory, points_domain, output_root=output_root,
                worker_count=count,
                max_result_retries=max_result_retries,
            )
        _service.start()
    return _service


def runtime_ready(provider_name):
    return bool(
        _service
        and _service.is_healthy()
        and _service.provider_ready(provider_name)
    )


def wake():
    if _service:
        _service.wake()
