"""Deterministic, non-billable provider used by PR-F tests and local drills."""

import shutil
import uuid
from pathlib import Path

from .base import LipsyncProvider, ProviderSubmissionUnknownError


class FakeLipsyncProvider(LipsyncProvider):
    name = "fake-lipsync"
    model_version = "fake-v1"
    supports_cancel = True
    supports_result_refetch = True

    def __init__(self, result_file=None, faults=None):
        self.result_file = str(result_file or "")
        self.faults = dict(faults or {})
        self.jobs = {}
        self.created_by_key = {}
        self.create_calls = 0

    def create_job(self, request, idempotency_key):
        self.create_calls += 1
        if (
            self.faults.pop("create_error", False)
            or self.faults.pop("create_http_422", False)
        ):
            raise RuntimeError("fake create failure")
        key = str(idempotency_key)
        if key in self.created_by_key:
            return {"job_id": self.created_by_key[key], "status": "queued"}
        job_id = "fake-" + uuid.uuid4().hex
        self.created_by_key[key] = job_id
        self.jobs[job_id] = {
            "status": "queued",
            "progress": 0,
            "request": dict(request),
        }
        if self.faults.pop("create_response_lost", False):
            raise ProviderSubmissionUnknownError(
                "fake create response lost"
            )
        create_http_5xx = self.faults.pop("create_http_5xx", None)
        if create_http_5xx:
            raise ProviderSubmissionUnknownError(
                "fake create HTTP %s" % create_http_5xx
            )
        return {"job_id": job_id, "status": "queued"}

    def get_job(self, provider_job_id):
        if self.faults.pop("poll_empty", False):
            return {"status": "unknown", "progress": 0}
        job = self.jobs[str(provider_job_id)]
        sequence = self.faults.get("status_sequence")
        if isinstance(sequence, list) and sequence:
            job["status"] = sequence.pop(0)
        return {
            "status": job["status"],
            "progress": int(job.get("progress") or 0),
        }

    def cancel_job(self, provider_job_id):
        self.jobs[str(provider_job_id)]["status"] = "cancelled"
        return {"status": "cancelled"}

    def fetch_result(self, provider_job_id, destination):
        if self.faults.pop("download_error", False):
            raise RuntimeError("fake download failure")
        if self.jobs[str(provider_job_id)]["status"] != "succeeded":
            raise RuntimeError("fake result is not ready")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.result_file:
            shutil.copyfile(self.result_file, destination)
        else:
            destination.write_bytes(b"fake-lipsync-result")
        return str(destination)
