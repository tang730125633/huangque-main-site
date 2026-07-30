import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools.lipsync_poc.adapters import (
    MockLipsyncProvider,
    ProviderJob,
    ProviderStatus,
)
from tools.lipsync_poc.manifest import PocSample
from tools.lipsync_poc.paths import artifact_paths
from tools.lipsync_poc.runner import PocRunError, PocRunner
from tools.lipsync_poc import run_poc
from tests.test_lipsync_poc_runner import fake_probe


class FailOnceProvider(MockLipsyncProvider):
    name = "fail-once"

    def __init__(self):
        super().__init__()
        self.create_calls = 0
        self.get_calls = 0

    def create_job(self, request):
        self.create_calls += 1
        job = super().create_job(request)
        self._jobs[job.job_id]["status"] = ProviderStatus.RUNNING
        return ProviderJob(job.job_id, ProviderStatus.RUNNING, self.name)

    def get_job(self, job_id):
        self.get_calls += 1
        if self.get_calls == 1:
            raise RuntimeError(
                "Authorization: Basic dXNlcjpwYXNz\n"
                "Cookie: sessionid=topsecret\n"
                "https://provider.test:notaport/result?token=urlsecret"
            )
        self._jobs[job_id]["status"] = ProviderStatus.SUCCEEDED
        return super().get_job(job_id)


class TimeoutProvider(MockLipsyncProvider):
    name = "timeout-provider"

    def __init__(self):
        super().__init__()
        self.cancel_calls = 0
        self.get_calls = 0

    def create_job(self, request):
        job = super().create_job(request)
        self._jobs[job.job_id]["status"] = ProviderStatus.RUNNING
        return ProviderJob(job.job_id, ProviderStatus.RUNNING, self.name)

    def cancel_job(self, job_id):
        self.cancel_calls += 1
        return super().cancel_job(job_id)

    def get_job(self, job_id):
        self.get_calls += 1
        return super().get_job(job_id)


class CancelFailureProvider(TimeoutProvider):
    name = "cancel-failure"

    def cancel_job(self, job_id):
        self.cancel_calls += 1
        raise RuntimeError("cancel request failed")


class TerminalFailedProvider(MockLipsyncProvider):
    name = "terminal-failed"

    def __init__(self):
        super().__init__()
        self.get_calls = 0

    def create_job(self, request):
        job = super().create_job(request)
        self._jobs[job.job_id]["status"] = ProviderStatus.FAILED
        return ProviderJob(job.job_id, ProviderStatus.FAILED, self.name)

    def get_job(self, job_id):
        self.get_calls += 1
        return super().get_job(job_id)


class LostCreateResponseProvider(MockLipsyncProvider):
    name = "lost-create-response"

    def __init__(self):
        super().__init__()
        self.create_calls = 0

    def create_job(self, request):
        self.create_calls += 1
        super().create_job(request)
        raise TimeoutError("provider accepted request but response was lost")


class BlockingCreateProvider(MockLipsyncProvider):
    name = "blocking-create"

    def __init__(self):
        super().__init__()
        self.create_calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def create_job(self, request):
        self.create_calls += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test provider was not released")
        return super().create_job(request)


class LipsyncPocRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / "source.mp4"
        self.audio = self.root / "master.wav"
        self.video.write_bytes(b"video")
        self.audio.write_bytes(b"audio")
        self.sample = PocSample(
            sample_id="front-01",
            video_path=self.video,
            audio_path=self.audio,
            transcript="test line",
            speaking_mode="visible",
            character_key="host",
            face_target={"type": "character", "value": "host"},
            duration_ms=5000,
            ratio="9:16",
            resolution="720p",
            fps=25,
            tags=("front",),
            notes="",
            input_hash="a" * 64,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_job_id_and_redacted_failure_report_survive_poll_failure(self):
        provider = FailOnceProvider()
        output = self.root / "out"
        runner = PocRunner(provider, probe=fake_probe, sleep=lambda _: None)
        with self.assertRaises(PocRunError):
            runner.run(self.sample, output)

        paths = artifact_paths(output, provider.name, self.sample.sample_id)
        state = json.loads(paths.state.read_text(encoding="utf-8"))
        report = json.loads(paths.report.read_text(encoding="utf-8"))
        self.assertTrue(state["provider_job_id"].startswith("mock-"))
        self.assertEqual(state["provider_job_id"], report["provider_job_id"])
        encoded = json.dumps(report)
        self.assertNotIn("dXNlcjpwYXNz", encoded)
        self.assertNotIn("topsecret", encoded)
        self.assertNotIn("urlsecret", encoded)
        self.assertEqual("requires_reconciliation", state["billing_status"])
        self.assertEqual("running", report["effective_provider_status"])
        self.assertTrue(report["recovery"]["can_resume"])
        self.assertFalse(report["recovery"]["can_refetch"])

    def test_resume_reuses_persisted_job_without_creating_another(self):
        provider = FailOnceProvider()
        output = self.root / "out"
        runner = PocRunner(provider, probe=fake_probe, sleep=lambda _: None)
        with self.assertRaises(PocRunError):
            runner.run(self.sample, output)

        report = runner.run(self.sample, output, resume=True)
        self.assertEqual("succeeded", report["status"])
        self.assertEqual(1, provider.create_calls)

    def test_timeout_calls_cancel_and_keeps_job_id(self):
        provider = TimeoutProvider()
        output = self.root / "out"
        ticks = iter((0.0, 2.0, 3.0, 4.0))
        runner = PocRunner(
            provider,
            probe=fake_probe,
            clock=lambda: next(ticks),
            sleep=lambda _: None,
        )
        with self.assertRaises(PocRunError) as raised:
            runner.run(
                self.sample,
                output,
                timeout_seconds=1,
                poll_seconds=0.1,
            )
        self.assertEqual("provider_timeout", raised.exception.code)
        self.assertEqual(1, provider.cancel_calls)
        paths = artifact_paths(output, provider.name, self.sample.sample_id)
        state = json.loads(paths.state.read_text(encoding="utf-8"))
        report = json.loads(paths.report.read_text(encoding="utf-8"))
        self.assertTrue(state["provider_job_id"])
        self.assertTrue(state["cancel"]["attempted"])
        self.assertEqual("canceled", report["effective_provider_status"])
        self.assertFalse(report["recovery"]["can_resume"])
        self.assertFalse(report["recovery"]["can_refetch"])
        with self.assertRaises(PocRunError) as resume_error:
            runner.run(self.sample, output, resume=True)
        self.assertEqual("resume_not_allowed", resume_error.exception.code)
        with self.assertRaises(PocRunError) as refetch_error:
            runner.run(self.sample, output, refetch=True)
        self.assertEqual(
            "refetch_not_allowed",
            refetch_error.exception.code,
        )
        self.assertEqual(0, provider.get_calls)

    def test_failed_cancel_leaves_running_job_resumable(self):
        provider = CancelFailureProvider()
        output = self.root / "out"
        ticks = iter((0.0, 2.0, 3.0, 4.0))
        runner = PocRunner(
            provider,
            probe=fake_probe,
            clock=lambda: next(ticks),
            sleep=lambda _: None,
        )
        with self.assertRaises(PocRunError):
            runner.run(
                self.sample,
                output,
                timeout_seconds=1,
                poll_seconds=0.1,
            )
        paths = artifact_paths(output, provider.name, self.sample.sample_id)
        report = json.loads(paths.report.read_text(encoding="utf-8"))
        self.assertEqual(1, provider.cancel_calls)
        self.assertEqual("running", report["effective_provider_status"])
        self.assertTrue(report["recovery"]["can_resume"])
        self.assertFalse(report["recovery"]["can_refetch"])

    def test_terminal_failed_job_cannot_resume_or_refetch(self):
        provider = TerminalFailedProvider()
        output = self.root / "out"
        runner = PocRunner(provider, probe=fake_probe, clock=lambda: 1.0)
        with self.assertRaises(PocRunError):
            runner.run(self.sample, output)
        paths = artifact_paths(output, provider.name, self.sample.sample_id)
        report = json.loads(paths.report.read_text(encoding="utf-8"))
        self.assertEqual("failed", report["effective_provider_status"])
        self.assertFalse(report["recovery"]["can_resume"])
        self.assertFalse(report["recovery"]["can_refetch"])

        with self.assertRaises(PocRunError) as resume_error:
            runner.run(self.sample, output, resume=True)
        self.assertEqual("resume_not_allowed", resume_error.exception.code)
        with self.assertRaises(PocRunError) as refetch_error:
            runner.run(self.sample, output, refetch=True)
        self.assertEqual(
            "refetch_not_allowed",
            refetch_error.exception.code,
        )
        self.assertEqual(0, provider.get_calls)

    def test_refetch_preserves_existing_human_review(self):
        provider = MockLipsyncProvider()
        output = self.root / "out"
        runner = PocRunner(provider, probe=fake_probe, clock=lambda: 1.0)
        runner.run(self.sample, output)
        paths = artifact_paths(output, provider.name, self.sample.sample_id)
        report = json.loads(paths.report.read_text(encoding="utf-8"))
        report["human_review"]["review_status"] = "complete"
        report["human_review"]["lip_sync_score"] = 5
        paths.report.write_text(json.dumps(report), encoding="utf-8")

        refetched = runner.run(self.sample, output, refetch=True)
        self.assertEqual("complete", refetched["human_review"]["review_status"])
        self.assertEqual(5, refetched["human_review"]["lip_sync_score"])
        self.assertFalse(refetched["recovery"]["can_resume"])
        self.assertTrue(refetched["recovery"]["can_refetch"])

    def test_provider_namespaces_do_not_overwrite_each_other(self):
        output = self.root / "out"
        first = MockLipsyncProvider()
        first.name = "provider-a"
        second = MockLipsyncProvider()
        second.name = "provider-b"
        PocRunner(first, probe=fake_probe, clock=lambda: 1.0).run(
            self.sample, output
        )
        PocRunner(second, probe=fake_probe, clock=lambda: 1.0).run(
            self.sample, output
        )
        first_paths = artifact_paths(
            output, first.name, self.sample.sample_id
        )
        second_paths = artifact_paths(
            output, second.name, self.sample.sample_id
        )
        self.assertTrue(first_paths.media.is_file())
        self.assertTrue(second_paths.media.is_file())
        self.assertNotEqual(first_paths.media, second_paths.media)
        self.assertTrue(first_paths.report.is_file())
        self.assertTrue(second_paths.report.is_file())

    def test_lost_create_response_requires_reconciliation(self):
        provider = LostCreateResponseProvider()
        output = self.root / "out"
        runner = PocRunner(provider, probe=fake_probe)
        with self.assertRaises(PocRunError):
            runner.run(self.sample, output)

        paths = artifact_paths(output, provider.name, self.sample.sample_id)
        state = json.loads(paths.state.read_text(encoding="utf-8"))
        report = json.loads(paths.report.read_text(encoding="utf-8"))
        self.assertEqual("reconciliation_required", state["status"])
        self.assertEqual("reconcile_submission", state["stage"])
        self.assertEqual(
            "requires_reconciliation",
            report["billing_status"],
        )
        self.assertIsNone(report["provider_job_id"])

        with self.assertRaises(PocRunError) as repeated:
            runner.run(self.sample, output)
        self.assertEqual(
            "submission_reconciliation_required",
            repeated.exception.code,
        )
        self.assertEqual(1, provider.create_calls)

    def test_concurrent_runs_create_only_one_provider_job(self):
        provider = BlockingCreateProvider()
        output = self.root / "out"
        runner = PocRunner(provider, probe=fake_probe, clock=lambda: 1.0)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(runner.run, self.sample, output)
            self.assertTrue(provider.entered.wait(timeout=5))
            with self.assertRaises(PocRunError) as repeated:
                runner.run(self.sample, output)
            provider.release.set()
            report = first.result(timeout=5)

        self.assertEqual(
            "submission_reconciliation_required",
            repeated.exception.code,
        )
        self.assertEqual("succeeded", report["status"])
        self.assertEqual(1, provider.create_calls)

    def test_recovery_rejects_changed_input_hash(self):
        provider = FailOnceProvider()
        output = self.root / "out"
        runner = PocRunner(provider, probe=fake_probe, sleep=lambda _: None)
        with self.assertRaises(PocRunError):
            runner.run(self.sample, output)
        changed = PocSample(
            **{
                **self.sample.__dict__,
                "input_hash": "b" * 64,
            }
        )
        with self.assertRaises(PocRunError) as raised:
            runner.run(changed, output, resume=True)
        self.assertEqual(
            "recovery_input_mismatch",
            raised.exception.code,
        )

    def test_provider_name_cannot_escape_output_directory(self):
        with self.assertRaises(ValueError):
            artifact_paths(
                self.root / "out",
                "../provider",
                self.sample.sample_id,
            )
        with self.assertRaises(ValueError):
            artifact_paths(
                self.root / "out",
                "Provider-A",
                self.sample.sample_id,
            )

    def test_cli_failure_exposes_persisted_job_and_recovery_status(self):
        assets = self.root / "assets"
        (assets / "video").mkdir(parents=True)
        (assets / "audio").mkdir()
        (assets / "video/source.mp4").write_bytes(b"video")
        (assets / "audio/master.wav").write_bytes(b"audio")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({
            "manifest_version": "1.0",
            "dataset_name": "recovery",
            "samples": [{
                "sample_id": "front-01",
                "video_file": "video/source.mp4",
                "audio_file": "audio/master.wav",
                "transcript": "test line",
                "speaking_mode": "visible",
                "character_key": "host",
                "duration_ms": 5000,
                "ratio": "9:16",
                "output_spec": {"resolution": "720p", "fps": 25},
            }],
        }), encoding="utf-8")
        provider = FailOnceProvider()
        output = io.StringIO()
        with patch.dict(
            run_poc.PROVIDERS,
            {"fail-once": lambda: provider},
            clear=True,
        ), redirect_stdout(output):
            code = run_poc.main([
                "--manifest", str(manifest),
                "--assets-root", str(assets),
                "--output-dir", str(self.root / "out"),
                "--provider", "fail-once",
                "--poll-seconds", "0.01",
            ])
        payload = json.loads(output.getvalue())
        failure = payload["failed"][0]
        self.assertEqual(1, code)
        self.assertTrue(failure["provider_job_id"])
        self.assertEqual(
            "requires_reconciliation",
            failure["billing_status"],
        )
        self.assertTrue(failure["recovery"]["can_resume"])
        self.assertEqual(
            "fail-once/reports/front-01.json",
            failure["report_file"],
        )


if __name__ == "__main__":
    unittest.main()
