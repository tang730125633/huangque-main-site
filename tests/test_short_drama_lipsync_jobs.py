import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from tests.test_short_drama_lipsync import ShortDramaLipsyncTests
from tests.test_short_drama_lipsync import RouteHandler

from content_domains import short_drama_lipsync
from content_domains import short_drama_assembly_plan
from content_domains import short_drama_lipsync_jobs
from content_domains import short_drama_lipsync_inputs
from content_domains import short_drama_lipsync_media
from content_domains import short_drama_lipsync_diagnostics
from content_domains import short_drama_lipsync_reconcile
from content_domains import short_drama_lipsync_rollout
from content_domains import short_drama_lipsync_worker
from providers.lipsync.fake import FakeLipsyncProvider
from providers.lipsync import runtime as lipsync_runtime


class FakeLedger:
    def __init__(self, points=1000):
        self.points = points
        self.transactions = {}
        self.fail_after_deduct = False
        self.fail_after_refund = False

    def deduct(self, username, amount, reason, key):
        if key not in self.transactions:
            if self.points < amount:
                raise RuntimeError("insufficient points")
            self.points -= amount
            self.transactions[key] = {
                "id": key, "points": self.points, "direction": "deduct"
            }
        if self.fail_after_deduct:
            self.fail_after_deduct = False
            raise TimeoutError("deduct response lost")
        return self.points

    def refund(self, username, amount, reason, key):
        if key not in self.transactions:
            self.points += amount
            self.transactions[key] = {
                "id": key, "points": self.points, "direction": "refund"
            }
        if self.fail_after_refund:
            self.fail_after_refund = False
            raise TimeoutError("refund response lost")
        return self.points

    def lookup(self, key):
        return self.transactions.get(key)


class InsufficientPointsError(RuntimeError):
    status = 402
    detail = "项目点数不足"


class InsufficientPointsLedger(FakeLedger):
    def deduct(self, username, amount, reason, key):
        raise InsufficientPointsError(InsufficientPointsError.detail)


class ShortDramaLipsyncJobTests(ShortDramaLipsyncTests):
    def setUp(self):
        super().setUp()
        self.flags = mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_LIPSYNC_JOBS_ENABLED": "1",
            "HQ_SHORT_DRAMA_LIPSYNC_BILLING_ENABLED": "1",
            "HQ_SHORT_DRAMA_LIPSYNC_POINTS_PER_USD": "100",
        })
        self.flags.start()
        self.addCleanup(self.flags.stop)

    def paid_quote(self, key="paid-quote"):
        snapshot = self.snapshot()
        return short_drama_lipsync.create_quote(
            self.db, "alice", "alice",
            self.quote_payload(snapshot, idempotency_key=key),
        )

    def prepared(self, key="paid-job"):
        quote = self.paid_quote("quote-" + key)
        payload = {
            "project_id": self.project_id,
            "shot_id": "shot-1",
            "quote_id": quote["quote_id"],
            "expected_input_hash": quote["input_hash"],
        }
        job = short_drama_lipsync_jobs.prepare(
            self.db, "alice", "alice", payload, key
        )
        return job, payload

    def pr_g_version(self, key="pr-g-version", version_id="lipsync-version-g"):
        job, payload = self.prepared(key)
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_attempts SET state='settled' "
                "WHERE id=?",
                (job["attempt_id"],),
            )
            conn.execute(
                "UPDATE short_drama_lipsync_jobs SET state='succeeded',"
                "progress=100 WHERE id=?",
                (job["id"],),
            )
            conn.execute(
                "INSERT INTO short_drama_lipsync_versions "
                "(id,project_id,shot_id,version,job_id,input_hash,provider,"
                "model_version,dependency_hashes_json,media_spec_json,file,"
                "file_hash,cost_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, self.project_id, "shot-1", 1,
                    job["id"], payload["expected_input_hash"],
                    "fal-latentsync", "v1", "{}",
                    '{"width":1280,"height":720,"duration_ms":5000}',
                    "lipsync/shot-1/v1.mp4", "f" * 64, '{"points":20}', 1000,
                ),
            )
            conn.commit()
        return payload

    def current_lipsync_pointer(self):
        with closing(self.db()) as conn:
            return conn.execute(
                "SELECT version_id,revision,locked_at,locked_by FROM "
                "short_drama_lipsync_current WHERE project_id=? AND shot_id=?",
                (self.project_id, "shot-1"),
            ).fetchone()

    def test_pr_g_selects_and_locks_immutable_version_with_pointer_cas(self):
        payload = self.pr_g_version()
        body = {
            "project_id": self.project_id,
            "expected_revision": self.snapshot()["revision"],
            "expected_input_hash": payload["expected_input_hash"],
            "expected_pointer_revision": 0,
        }
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_LIPSYNC_MUTATIONS_ENABLED": "1",
        }):
            selected = short_drama_lipsync.select_version(
                self.db, "alice", body, "lipsync-version-g"
            )
            self.assertEqual(1, selected["revision"])
            body["expected_pointer_revision"] = selected["revision"]
            locked = short_drama_lipsync.lock_version(
                self.db, "alice", "alice", body, "lipsync-version-g"
            )
        self.assertTrue(locked["locked"])
        self.assertEqual(2, locked["revision"])
        with closing(self.db()) as conn:
            current = conn.execute(
                "SELECT version_id,revision,locked_by FROM "
                "short_drama_lipsync_current WHERE project_id=? AND shot_id=?",
                (self.project_id, "shot-1"),
            ).fetchone()
        self.assertEqual(
            ("lipsync-version-g", 2, "alice"), current
        )

    def test_pr_g_readonly_stages_cannot_select_or_lock_versions(self):
        payload = self.pr_g_version("readonly-stage")
        body = {
            "project_id": self.project_id,
            "expected_revision": self.snapshot()["revision"],
            "expected_input_hash": payload["expected_input_hash"],
            "expected_pointer_revision": 0,
        }
        stages = ("voice_review", "assembly_review", "completed")
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_LIPSYNC_MUTATIONS_ENABLED": "1",
        }):
            for stage in stages:
                with closing(self.db()) as conn:
                    conn.execute(
                        "UPDATE short_drama_projects SET stage=? WHERE id=?",
                        (stage, self.project_id),
                    )
                    conn.commit()
                with self.subTest(operation="select", stage=stage):
                    with self.assertRaises(
                        short_drama_lipsync.LipsyncVersionError
                    ) as raised:
                        short_drama_lipsync.select_version(
                            self.db, "alice", body, "lipsync-version-g"
                        )
                    self.assertEqual(
                        "project_stage_readonly", raised.exception.code
                    )
                    self.assertEqual(409, raised.exception.status)
                    self.assertIsNone(self.current_lipsync_pointer())

            with closing(self.db()) as conn:
                conn.execute(
                    "UPDATE short_drama_projects SET stage='video_review' "
                    "WHERE id=?",
                    (self.project_id,),
                )
                conn.commit()
            selected = short_drama_lipsync.select_version(
                self.db, "alice", body, "lipsync-version-g"
            )
            body["expected_pointer_revision"] = selected["revision"]
            pointer = self.current_lipsync_pointer()
            for stage in stages:
                with closing(self.db()) as conn:
                    conn.execute(
                        "UPDATE short_drama_projects SET stage=? WHERE id=?",
                        (stage, self.project_id),
                    )
                    conn.commit()
                with self.subTest(operation="lock", stage=stage):
                    with self.assertRaises(
                        short_drama_lipsync.LipsyncVersionError
                    ) as raised:
                        short_drama_lipsync.lock_version(
                            self.db, "alice", "alice", body,
                            "lipsync-version-g",
                        )
                    self.assertEqual(
                        "project_stage_readonly", raised.exception.code
                    )
                    self.assertEqual(409, raised.exception.status)
                    self.assertEqual(pointer, self.current_lipsync_pointer())

    def test_pr_g_stale_version_cannot_advance_current_pointer(self):
        payload = self.pr_g_version("stale-select")
        body = {
            "project_id": self.project_id,
            "expected_revision": self.snapshot()["revision"],
            "expected_input_hash": payload["expected_input_hash"],
            "expected_pointer_revision": 0,
        }
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_LIPSYNC_MUTATIONS_ENABLED": "1",
        }):
            selected = short_drama_lipsync.select_version(
                self.db, "alice", body, "lipsync-version-g"
            )
            pointer = self.current_lipsync_pointer()
            with closing(self.db()) as conn:
                conn.execute(
                    "UPDATE short_drama_projects SET revision=revision+1 "
                    "WHERE id=?",
                    (self.project_id,),
                )
                conn.commit()
            current_snapshot = self.snapshot()
            self.assertNotEqual(
                payload["expected_input_hash"],
                current_snapshot["input_hash"],
            )
            body.update({
                "expected_revision": current_snapshot["revision"],
                "expected_pointer_revision": selected["revision"],
            })
            with self.assertRaises(
                short_drama_lipsync.LipsyncVersionError
            ) as raised:
                short_drama_lipsync.select_version(
                    self.db, "alice", body, "lipsync-version-g"
                )
            self.assertEqual("stale_snapshot", raised.exception.code)
            self.assertEqual(409, raised.exception.status)
            self.assertEqual(pointer, self.current_lipsync_pointer())

            body["expected_input_hash"] = current_snapshot["input_hash"]
            with self.assertRaises(
                short_drama_lipsync.LipsyncVersionError
            ) as raised:
                short_drama_lipsync.select_version(
                    self.db, "alice", body, "lipsync-version-g"
                )
            self.assertEqual("stale_version", raised.exception.code)
            self.assertEqual(409, raised.exception.status)
            self.assertEqual(pointer, self.current_lipsync_pointer())

    def test_prepare_consumes_quote_and_is_atomic_and_idempotent(self):
        job, payload = self.prepared()
        replay = short_drama_lipsync_jobs.prepare(
            self.db, "alice", "alice", payload, "paid-job"
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(job["id"], replay["id"])
        with closing(self.db()) as conn:
            self.assertEqual(
                "consumed",
                conn.execute(
                    "SELECT status FROM short_drama_lipsync_quotes "
                    "WHERE id=(SELECT quote_id FROM short_drama_lipsync_attempts "
                    "WHERE id=?)",
                    (job["attempt_id"],),
                ).fetchone()[0],
            )
            self.assertEqual(
                (1, 1),
                (
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_lipsync_attempts"
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_lipsync_jobs"
                    ).fetchone()[0],
                ),
            )

    def test_pr_e_placeholder_tables_are_upgraded_without_losing_jobs(self):
        quote = self.paid_quote("migration")
        with closing(self.db()) as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript("""
            DROP TRIGGER IF EXISTS short_drama_lipsync_version_immutable;
            DROP TRIGGER IF EXISTS short_drama_lipsync_version_project_guard;
            DROP TRIGGER IF EXISTS short_drama_lipsync_current_guard_insert;
            DROP TRIGGER IF EXISTS short_drama_lipsync_current_guard_update;
            DROP TABLE short_drama_lipsync_current;
            DROP TABLE short_drama_lipsync_versions;
            DROP TABLE short_drama_lipsync_jobs;
            DROP TABLE short_drama_lipsync_attempts;
            CREATE TABLE short_drama_lipsync_attempts (
              id TEXT PRIMARY KEY, actor_username TEXT NOT NULL,
              quote_id TEXT NOT NULL UNIQUE, idempotency_key TEXT NOT NULL,
              request_hash TEXT NOT NULL,
              state TEXT NOT NULL CHECK (
                state IN ('prepared','charged','job_created','settled',
                          'refund_pending','refunded','manual_review')),
              charge_ref TEXT, refund_ref TEXT,
              created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
              UNIQUE(actor_username,idempotency_key)
            );
            CREATE TABLE short_drama_lipsync_jobs (
              id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
              provider TEXT NOT NULL, provider_job_id TEXT,
              state TEXT NOT NULL CHECK (
                state IN ('queued','running','succeeded','failed','cancelled')),
              heartbeat_at INTEGER, error_json TEXT NOT NULL DEFAULT '{}',
              created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE short_drama_lipsync_versions (
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL, shot_id TEXT NOT NULL,
              version INTEGER NOT NULL, job_id TEXT NOT NULL UNIQUE,
              input_hash TEXT NOT NULL, provider TEXT NOT NULL,
              model_version TEXT NOT NULL, dependency_hashes_json TEXT NOT NULL,
              media_spec_json TEXT NOT NULL, file TEXT NOT NULL,
              file_hash TEXT NOT NULL, cost_json TEXT NOT NULL,
              created_at INTEGER NOT NULL, UNIQUE(project_id,shot_id,version)
            );
            CREATE TABLE short_drama_lipsync_current (
              project_id TEXT NOT NULL, shot_id TEXT NOT NULL,
              version_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
              updated_at INTEGER NOT NULL, PRIMARY KEY(project_id,shot_id)
            );
            """)
            conn.execute(
                "INSERT INTO short_drama_lipsync_attempts VALUES "
                "(?,?,?,?,?,'prepared',NULL,NULL,?,?)",
                ("legacy-attempt", "alice", quote["quote_id"], "legacy-key",
                 "r" * 64, 100, 100),
            )
            conn.execute(
                "INSERT INTO short_drama_lipsync_jobs VALUES "
                "(?,?,?,NULL,'queued',NULL,'{}',?,?)",
                ("legacy-job", "legacy-attempt", "fal-latentsync", 100, 100),
            )
            conn.commit()
        short_drama_lipsync.init_db(self.db)
        with closing(self.db()) as conn:
            attempt = conn.execute(
                "SELECT owner_username,state,charge_key FROM "
                "short_drama_lipsync_attempts WHERE id='legacy-attempt'"
            ).fetchone()
            job = conn.execute(
                "SELECT project_id,shot_id,state FROM "
                "short_drama_lipsync_jobs WHERE id='legacy-job'"
            ).fetchone()
        self.assertEqual(("alice", "accepted"), attempt[:2])
        self.assertTrue(attempt[2].startswith("short-drama-lipsync:alice:"))
        self.assertEqual((self.project_id, "shot-1", "queued"), job)

    def test_quote_replays_without_money_provider_or_task_side_effects(self):
        snapshot = self.snapshot()
        payload = self.quote_payload(snapshot, idempotency_key="paid-replay")
        first = short_drama_lipsync.create_quote(
            self.db, "alice", "alice", payload
        )
        second = short_drama_lipsync.create_quote(
            self.db, "alice", "alice", payload
        )
        self.assertTrue(first["chargeable"])
        self.assertTrue(second["replayed"])
        with closing(self.db()) as conn:
            self.assertEqual(
                (0, 0),
                (
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_lipsync_attempts"
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_lipsync_jobs"
                    ).fetchone()[0],
                ),
            )

    def test_lost_deduct_response_is_recovered_from_stable_ledger_key(self):
        job, _ = self.prepared("lost-debit")
        ledger = FakeLedger()
        ledger.fail_after_deduct = True
        charged = short_drama_lipsync_jobs.charge(
            self.db, job["id"], ledger
        )
        self.assertEqual("charged", charged["attempt_state"])
        self.assertEqual("queued", charged["state"])
        replay = short_drama_lipsync_jobs.charge(
            self.db, job["id"], ledger
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(1, len([
            key for key in ledger.transactions if not key.endswith(":refund")
        ]))

    def test_insufficient_points_returns_402_without_consuming_attempt(self):
        job, _ = self.prepared("insufficient-points")
        with self.assertRaises(short_drama_lipsync_jobs.LipsyncJobError) as caught:
            short_drama_lipsync_jobs.charge(
                self.db, job["id"], InsufficientPointsLedger()
            )
        self.assertEqual(402, caught.exception.status)
        self.assertEqual("insufficient_points", caught.exception.code)
        current = short_drama_lipsync_jobs.get(self.db, "alice", job["id"])
        self.assertEqual("accepted", current["attempt_state"])
        self.assertEqual("prepared", current["state"])

    def test_submission_fails_before_charge_when_worker_provider_is_unavailable(self):
        _, payload = self.prepared("runtime-gate")
        deduct = mock.Mock()
        with self.assertRaises(short_drama_lipsync.LipsyncJobError) as caught:
            short_drama_lipsync.create_job(
                self.db, "alice", "alice", payload, "runtime-gate",
                deduct_points=deduct,
                provider_ready=lambda _name: False,
            )
        self.assertEqual(503, caught.exception.status)
        self.assertEqual(
            "lipsync_worker_unavailable", caught.exception.code
        )
        deduct.assert_not_called()

    def test_runtime_provider_factory_is_loaded_once_and_name_checked(self):
        provider = FakeLipsyncProvider()
        factory = mock.Mock(return_value=provider)
        module = mock.Mock(build=factory)
        self.addCleanup(lipsync_runtime.clear)
        lipsync_runtime.clear()
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_LIPSYNC_RUNTIME_PROVIDER": provider.name,
            "HQ_SHORT_DRAMA_LIPSYNC_RUNTIME_FACTORY": "deployment:build",
        }), mock.patch.object(
            lipsync_runtime.importlib, "import_module", return_value=module
        ):
            loaded = lipsync_runtime.load_from_environment()
        self.assertEqual(provider.name, loaded)
        self.assertIs(provider, lipsync_runtime.get_provider(provider.name))
        factory.assert_called_once_with()

    def test_worker_links_provider_once_and_terminal_cas_settles(self):
        job, _ = self.prepared("worker")
        ledger = FakeLedger()
        short_drama_lipsync_jobs.charge(self.db, job["id"], ledger)
        provider = FakeLipsyncProvider()
        token = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-1", now=100
        )
        linked = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, token, now=100
        )
        self.assertEqual("running", linked["state"])
        self.assertEqual(1, provider.create_calls)
        provider.jobs[linked["provider_job_id"]]["status"] = "succeeded"
        ready = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, token, now=101
        )
        self.assertEqual("running", ready["state"])
        self.assertTrue(ready["result"]["result_ready"])
        temporary = Path(self.path).parent / ("final-" + Path(self.path).stem)
        temporary.mkdir()
        self.addCleanup(lambda: shutil.rmtree(temporary, ignore_errors=True))
        probes = [
            {
                "duration_ms": 5000,
                "video": {"width": 1280, "height": 720, "fps": 25, "codec": "h264"},
                "audio": {"codec": "aac"},
            },
            {
                "duration_ms": 5000,
                "video": {"width": 1280, "height": 720, "fps": 25, "codec": "h264"},
                "audio": None,
            },
        ]
        settled = short_drama_lipsync_jobs.finalize_result(
            self.db, job["id"], provider, token,
            work_dir=temporary / "work", output_root=temporary / "out",
            probe=lambda _: probes.pop(0),
            remux=lambda src, dst: Path(dst).write_bytes(Path(src).read_bytes()),
            now=102,
        )
        self.assertEqual("succeeded", settled["state"])
        self.assertEqual("settled", settled["attempt_state"])
        self.assertEqual(1, provider.create_calls)
        self.assertTrue(settled["result"]["version_id"])

    def test_worker_passes_immutable_provider_request_from_builder(self):
        job, _ = self.prepared("provider-request-builder")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())
        provider = FakeLipsyncProvider()
        token = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-builder", now=100
        )
        expected = {
            "video_path": "/controlled/locked.mp4",
            "audio_path": "/controlled/speech.wav",
            "face_target": {"character_key": "hero"},
            "metadata": {"input_hash": job["input_hash"]},
        }
        builder = mock.Mock(return_value=expected)
        linked = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, token, now=100,
            request_builder=builder,
        )
        builder.assert_called_once()
        submitted = provider.jobs[linked["provider_job_id"]]["request"]
        self.assertEqual(expected, submitted)

    def test_provider_contract_ignores_new_current_timeline_and_visual(self):
        job, _ = self.prepared("immutable-provider-contract")
        with closing(self.db()) as conn:
            original = short_drama_lipsync_inputs._load_contract(conn, job["id"])
            current_source = conn.execute(
                "SELECT * FROM short_drama_lipsync_visual_sources "
                "WHERE project_id=? AND shot_id='shot-1' AND is_current=1",
                (self.project_id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO short_drama_timeline_versions "
                "(id,project_id,version,parent_id,status,revision,contract_version,"
                "duration_ms,source_hashes_json,timeline_hash,input_hash,"
                "subtitle_cues_json,blockers_json,created_by,created_at) "
                "SELECT 'timeline-2',project_id,2,id,status,revision+1,"
                "contract_version,duration_ms,source_hashes_json,?, ?,"
                "subtitle_cues_json,blockers_json,created_by,created_at "
                "FROM short_drama_timeline_versions WHERE id='timeline-1'",
                ("n" * 64, "o" * 64),
            )
            conn.execute(
                "INSERT INTO short_drama_timeline_segments "
                "(id,version_id,project_id,shot_id,line_id,character_key,"
                "voice_asset_id,start_ms,end_ms,speaking_mode,face_target_json,"
                "sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "segment-2", "timeline-2", self.project_id, "shot-1",
                    "line-1", "host", "voice-1", 600, 2500, "visible",
                    '{"type":"character","value":"host"}', 0,
                ),
            )
            conn.execute(
                "UPDATE short_drama_timeline_current SET version_id='timeline-2',"
                "revision=revision+1 WHERE project_id=?", (self.project_id,),
            )
            conn.execute(
                "UPDATE short_drama_lipsync_visual_sources SET is_current=0 "
                "WHERE id=?", (current_source[0],),
            )
            conn.execute(
                "INSERT INTO short_drama_lipsync_visual_sources "
                "(id,project_id,shot_id,source_kind,uri,source_hash,is_current,"
                "locked_at,created_at) VALUES (?,?,?,?,?,?,1,?,?)",
                (
                    "visual-new", self.project_id, "shot-1", "video",
                    "media/new-shot.mp4", "z" * 64, 200, 200,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_lipsync_media_reports "
                "(id,source_id,probe_version,width,height,fps,duration_ms,codec,"
                "source_format,report_hash,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "report-new", "visual-new", "v1", 1280, 720, 25.0,
                    5000, "h264", "mp4", "y" * 64, 200,
                ),
            )
            conn.commit()
            immutable = short_drama_lipsync_inputs._load_contract(conn, job["id"])
        self.assertEqual(original, immutable)
        self.assertEqual(500, immutable["speech"][0]["start_ms"])
        self.assertNotEqual("media/new-shot.mp4", immutable["video_file"])

    def test_provider_contract_detects_mutated_historical_dependency(self):
        job, _ = self.prepared("mutated-provider-contract")
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_timeline_segments SET start_ms=600 "
                "WHERE id='segment-1' AND version_id='timeline-1'"
            )
            conn.commit()
            with self.assertRaises(short_drama_lipsync_inputs.LipsyncInputError):
                short_drama_lipsync_inputs._load_contract(conn, job["id"])

    def test_quote_freezes_actual_video_and_voice_file_fingerprints(self):
        quote = self.paid_quote("file-fingerprints")
        with closing(self.db()) as conn:
            contract = json.loads(conn.execute(
                "SELECT provider_contract_json FROM short_drama_lipsync_quotes "
                "WHERE id=?", (quote["quote_id"],),
            ).fetchone()[0])
        self.assertEqual(
            short_drama_lipsync_inputs.PROVIDER_CONTRACT_VERSION,
            contract["contract_version"],
        )
        self.assertEqual({
            "sha256": hashlib.sha256(self.video_bytes).hexdigest(),
            "size": len(self.video_bytes),
        }, contract["visual"]["file_fingerprint"])
        self.assertEqual({
            "sha256": hashlib.sha256(self.voice_bytes).hexdigest(),
            "size": len(self.voice_bytes),
        }, contract["segments"][0]["file_fingerprint"])

    def test_quote_rejects_video_that_already_differs_from_media_report(self):
        snapshot = self.snapshot()
        (self.media_root / "media" / "shot-1.mp4").write_bytes(
            b"Z" * len(self.video_bytes)
        )
        with self.assertRaises(
            short_drama_lipsync.LipsyncQuoteError
        ) as raised:
            short_drama_lipsync.create_quote(
                self.db, "alice", "alice",
                self.quote_payload(snapshot, idempotency_key="stale-video-file"),
            )
        self.assertEqual("dependency_blocked", raised.exception.code)
        self.assertEqual(422, raised.exception.status)
        with closing(self.db()) as conn:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_lipsync_quotes "
                    "WHERE business_key IS NOT NULL"
                ).fetchone()[0],
            )

    @staticmethod
    def _successful_audio_runner(command, **_options):
        Path(command[-1]).write_bytes(b"derived-provider-wav")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def _provider_request_builder(self, work_root):
        return lambda job_id, _identity: (
            short_drama_lipsync_inputs.prepare_provider_request(
                self.db, job_id, work_root,
                runner=self._successful_audio_runner,
            )
        )

    def _assert_mutated_input_fails_before_provider(self, key, relative, replacement):
        job, _ = self.prepared(key)
        ledger = FakeLedger()
        short_drama_lipsync_jobs.charge(self.db, job["id"], ledger)
        (self.media_root / relative).write_bytes(replacement)
        provider = FakeLipsyncProvider()
        work_root = self.media_root / ("work-" + key)
        token = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-mutated", now=100,
        )
        failed = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, token, now=100,
            request_builder=self._provider_request_builder(work_root),
        )
        self.assertEqual("failed", failed["state"])
        self.assertEqual("refund_pending", failed["attempt_state"])
        self.assertEqual("provider_create_failed", failed["error"]["code"])
        self.assertEqual(0, provider.create_calls)
        self.assertEqual(
            [job["attempt_id"]],
            short_drama_lipsync_jobs.reconcile_refunds(
                self.db, ledger, now=101,
            ),
        )
        self.assertEqual(
            [], short_drama_lipsync_jobs.reconcile_refunds(
                self.db, ledger, now=102,
            ),
        )
        self.assertEqual(1000, ledger.points)
        self.assertIsNone(short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-retry", now=103,
        ))
        self.assertEqual(0, provider.create_calls)

    def test_same_size_video_replacement_fails_before_provider_and_refunds_once(self):
        self._assert_mutated_input_fails_before_provider(
            "mutated-video-bytes", "media/shot-1.mp4",
            b"X" * len(self.video_bytes),
        )

    def test_same_size_voice_replacement_fails_before_provider_and_refunds_once(self):
        self._assert_mutated_input_fails_before_provider(
            "mutated-voice-bytes", "audio/voice-1.wav",
            b"Y" * len(self.voice_bytes),
        )

    def test_unchanged_media_is_staged_and_submitted_from_verified_copies(self):
        job, _ = self.prepared("verified-staging")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())
        provider = FakeLipsyncProvider()
        work_root = self.media_root / "work-verified"
        token = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-verified", now=100,
        )
        linked = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, token, now=100,
            request_builder=self._provider_request_builder(work_root),
        )
        self.assertEqual("running", linked["state"])
        self.assertEqual(1, provider.create_calls)
        submitted = provider.jobs[linked["provider_job_id"]]["request"]
        staged_video = Path(submitted["video_path"])
        self.assertNotEqual(
            (self.media_root / "media" / "shot-1.mp4").resolve(),
            staged_video.resolve(),
        )
        self.assertEqual(self.video_bytes, staged_video.read_bytes())
        self.assertEqual(
            hashlib.sha256(self.video_bytes).hexdigest(),
            submitted["metadata"]["video_fingerprint"]["sha256"],
        )

    def test_http_5xx_provider_create_is_recovered_by_idempotency_key(self):
        job, _ = self.prepared("http-5xx-provider-create")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())
        provider = FakeLipsyncProvider(faults={"create_http_5xx": 503})
        token = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-1", now=100
        )
        result = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, token, now=100
        )
        self.assertEqual("running", result["state"])
        self.assertEqual("charged", result["attempt_state"])
        self.assertIsNone(result["provider_job_id"])
        self.assertEqual(
            "provider_create_unknown", result["error"]["code"]
        )
        short_drama_lipsync_jobs.release_lease(
            self.db, job["id"], token, now=101
        )
        recovered_token = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-2", now=105
        )
        recovered = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, recovered_token, now=105
        )
        self.assertEqual("running", recovered["state"])
        self.assertTrue(recovered["provider_job_id"])
        self.assertEqual(2, provider.create_calls)
        self.assertEqual(1, len(provider.jobs))
        self.assertEqual(1, len(provider.created_by_key))

    def test_http_422_provider_create_failure_schedules_one_refund(self):
        job, _ = self.prepared("provider-create-failed")
        ledger = FakeLedger()
        short_drama_lipsync_jobs.charge(self.db, job["id"], ledger)
        provider = FakeLipsyncProvider(faults={"create_http_422": True})
        token = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-1", now=100
        )
        failed = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, token, now=100
        )
        self.assertEqual("failed", failed["state"])
        self.assertEqual("refund_pending", failed["attempt_state"])
        self.assertEqual("provider_create_failed", failed["error"]["code"])
        self.assertEqual(
            [job["attempt_id"]],
            short_drama_lipsync_jobs.reconcile_refunds(
                self.db, ledger, now=101
            ),
        )
        self.assertEqual(
            [],
            short_drama_lipsync_jobs.reconcile_refunds(
                self.db, ledger, now=102
            ),
        )
        self.assertEqual(1000, ledger.points)

    def test_cancel_during_provider_create_keeps_lease_and_cancels_external_job(self):
        job, _ = self.prepared("cancel-create-race")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())

        class CancelDuringCreate(FakeLipsyncProvider):
            def create_job(inner, request, idempotency_key):
                created = super(CancelDuringCreate, inner).create_job(
                    request, idempotency_key
                )
                pending = short_drama_lipsync_jobs.request_cancel(
                    self.db, "alice", job["id"], now=101
                )
                self.assertEqual("cancel_pending", pending["state"])
                return created

        provider = CancelDuringCreate()
        token = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-1", now=100
        )
        result = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, token, now=100
        )
        self.assertEqual("cancelled", result["state"])
        self.assertTrue(result["provider_job_id"])
        self.assertEqual(
            "cancelled",
            provider.jobs[result["provider_job_id"]]["status"],
        )
        self.assertEqual("refund_pending", result["attempt_state"])

    def test_cancel_after_http_5xx_recovers_provider_before_refund(self):
        job, _ = self.prepared("cancel-http-5xx")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())
        provider = FakeLipsyncProvider(
            faults={"create_http_5xx": 503}
        )
        token = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-1", now=100
        )
        first = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, token, now=100
        )
        self.assertEqual("running", first["state"])
        pending = short_drama_lipsync_jobs.request_cancel(
            self.db, "alice", job["id"], now=101
        )
        self.assertEqual("cancel_pending", pending["state"])
        recovered = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, token, now=102
        )
        self.assertEqual("cancelled", recovered["state"])
        self.assertTrue(recovered["provider_job_id"])
        self.assertEqual("refund_pending", recovered["attempt_state"])

    def test_late_create_response_persists_provider_id_after_lease_expiry(self):
        job, _ = self.prepared("late-create-response")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())

        class ExpireLeaseDuringCreate(FakeLipsyncProvider):
            def create_job(inner, request, idempotency_key):
                created = super(ExpireLeaseDuringCreate, inner).create_job(
                    request, idempotency_key
                )
                short_drama_lipsync_jobs.request_cancel(
                    self.db, "alice", job["id"], now=101
                )
                short_drama_lipsync_reconcile.release_expired_leases(
                    self.db, now=200
                )
                return created

        provider = ExpireLeaseDuringCreate()
        token = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-old", now=100
        )
        pending = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, token, now=100
        )
        self.assertEqual("cancel_pending", pending["state"])
        self.assertTrue(pending["provider_job_id"])
        replacement = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-new", now=201
        )
        cancelled = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, replacement, now=201
        )
        self.assertEqual("cancelled", cancelled["state"])
        self.assertEqual("refund_pending", cancelled["attempt_state"])

    def test_failure_refund_response_loss_is_reconciled_once(self):
        job, _ = self.prepared("refund")
        ledger = FakeLedger()
        short_drama_lipsync_jobs.charge(self.db, job["id"], ledger)
        provider = FakeLipsyncProvider()
        token = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-1", now=100
        )
        linked = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, token, now=100
        )
        provider.jobs[linked["provider_job_id"]]["status"] = "failed"
        failed = short_drama_lipsync_jobs.process_once(
            self.db, job["id"], provider, token, now=101
        )
        self.assertEqual("refund_pending", failed["attempt_state"])
        ledger.fail_after_refund = True
        recovered = short_drama_lipsync_jobs.reconcile_refunds(
            self.db, ledger, now=102
        )
        self.assertEqual([job["attempt_id"]], recovered)
        self.assertEqual([], short_drama_lipsync_jobs.reconcile_refunds(
            self.db, ledger, now=103
        ))
        self.assertEqual(1000, ledger.points)

    def test_active_lease_cannot_be_stolen(self):
        job, _ = self.prepared("lease")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())
        first = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-1", now=100
        )
        second = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-2", now=101
        )
        self.assertTrue(first)
        self.assertIsNone(second)
        self.assertTrue(short_drama_lipsync_jobs.heartbeat(
            self.db, job["id"], first, now=102
        ))

    def test_manual_reconcile_only_releases_expired_target_lease(self):
        job, _ = self.prepared("manual-reconcile-lease")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())
        token = short_drama_lipsync_jobs.acquire_lease(
            self.db, job["id"], "worker-1", now=100
        )
        self.assertEqual(
            [],
            short_drama_lipsync_reconcile.release_expired_leases(
                self.db, now=120, limit=1, job_id=job["id"]
            ),
        )
        with closing(self.db()) as conn:
            active = conn.execute(
                "SELECT lease_token,lease_expires_at FROM "
                "short_drama_lipsync_jobs WHERE id=?",
                (job["id"],),
            ).fetchone()
        self.assertEqual(token, active[0])
        self.assertGreater(active[1], 120)

        class HeartbeatBeforeReleaseCursor:
            def __init__(inner, cursor):
                inner.cursor = cursor

            def fetchall(inner):
                rows = inner.cursor.fetchall()
                short_drama_lipsync_jobs.heartbeat(
                    self.db, job["id"], token, now=190
                )
                return rows

        class HeartbeatBeforeReleaseConnection:
            def __init__(inner):
                inner.conn = self.db()

            @property
            def row_factory(inner):
                return inner.conn.row_factory

            @row_factory.setter
            def row_factory(inner, value):
                inner.conn.row_factory = value

            def execute(inner, sql, args=()):
                cursor = inner.conn.execute(sql, args)
                if sql.startswith(
                    "SELECT id,state,lease_token,lease_expires_at"
                ):
                    return HeartbeatBeforeReleaseCursor(cursor)
                return cursor

            def commit(inner):
                return inner.conn.commit()

            def close(inner):
                return inner.conn.close()

        self.assertEqual(
            [],
            short_drama_lipsync_reconcile.release_expired_leases(
                HeartbeatBeforeReleaseConnection,
                now=200,
                limit=1,
                job_id=job["id"],
            ),
        )
        with closing(self.db()) as conn:
            renewed = conn.execute(
                "SELECT lease_token,lease_expires_at FROM "
                "short_drama_lipsync_jobs WHERE id=?",
                (job["id"],),
            ).fetchone()
        self.assertEqual(token, renewed[0])
        self.assertEqual(235, renewed[1])
        self.assertEqual(
            [job["id"]],
            short_drama_lipsync_reconcile.release_expired_leases(
                self.db, now=236, limit=1, job_id=job["id"]
            ),
        )
        with closing(self.db()) as conn:
            released = conn.execute(
                "SELECT lease_token,lease_expires_at FROM "
                "short_drama_lipsync_jobs WHERE id=?",
                (job["id"],),
            ).fetchone()
        self.assertEqual((None, None), released)

    def test_manual_refund_blocks_active_job_and_targets_one_attempt(self):
        active, _ = self.prepared("manual-refund-active")
        ledger = FakeLedger()
        for job_state in (
            "prepared", "queued", "running", "cancel_pending"
        ):
            with self.subTest(job_state=job_state):
                if job_state == "queued":
                    short_drama_lipsync_jobs.charge(
                        self.db, active["id"], ledger
                    )
                with closing(self.db()) as conn:
                    conn.execute(
                        "UPDATE short_drama_lipsync_jobs SET state=? "
                        "WHERE id=?",
                        (job_state, active["id"]),
                    )
                    conn.commit()
                with self.assertRaises(
                    short_drama_lipsync_rollout.RolloutError
                ) as blocked:
                    short_drama_lipsync_rollout.request_manual_refund(
                        self.db, "admin", active["attempt_id"],
                        "incident refund", incident_id="INC-ACTIVE",
                        now=100,
                    )
                self.assertEqual(
                    "refund_job_not_terminal", blocked.exception.code
                )

        first = active
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_jobs SET state='failed' "
                "WHERE id=?",
                (first["id"],),
            )
            conn.commit()
        second, _ = self.prepared("manual-refund-second")
        short_drama_lipsync_jobs.charge(self.db, second["id"], ledger)
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_jobs SET state='failed' "
                "WHERE id IN (?,?)",
                (first["id"], second["id"]),
            )
            conn.commit()
        claimed_first = short_drama_lipsync_rollout.request_manual_refund(
            self.db, "admin", first["attempt_id"], "provider failure",
            incident_id="INC-REFUND", now=101,
        )
        short_drama_lipsync_rollout.request_manual_refund(
            self.db, "admin", second["attempt_id"], "provider failure",
            incident_id="INC-REFUND", now=101,
        )
        self.assertFalse(claimed_first["replayed"])
        self.assertTrue(
            short_drama_lipsync_jobs.reconcile_refund_attempt(
                self.db, ledger, first["attempt_id"], now=102
            )
        )
        with closing(self.db()) as conn:
            states = dict(conn.execute(
                "SELECT id,state FROM short_drama_lipsync_attempts "
                "WHERE id IN (?,?)",
                (first["attempt_id"], second["attempt_id"]),
            ).fetchall())
            audit = conn.execute(
                "SELECT actor,reason,incident_id,before_json,after_json "
                "FROM short_drama_lipsync_rollout_audit "
                "WHERE action='attempt.refund_requested' AND target=?",
                (first["attempt_id"],),
            ).fetchone()
        self.assertEqual("refunded", states[first["attempt_id"]])
        self.assertEqual("refund_pending", states[second["attempt_id"]])
        self.assertEqual(("admin", "provider failure", "INC-REFUND"),
                         audit[:3])
        self.assertIn('"attempt_state":"charged"', audit[3])
        self.assertIn('"attempt_state":"refund_pending"', audit[4])
        replay = short_drama_lipsync_rollout.request_manual_refund(
            self.db, "admin", first["attempt_id"], "provider failure",
            incident_id="INC-REFUND", now=103,
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual("refunded", replay["state"])

    def test_retry_and_refund_transitions_are_mutually_exclusive(self):
        ledger = FakeLedger()

        refund_first, _ = self.prepared("refund-before-retry")
        short_drama_lipsync_jobs.charge(
            self.db, refund_first["id"], ledger
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_jobs SET state='failed' "
                "WHERE id=?",
                (refund_first["id"],),
            )
            conn.commit()
        short_drama_lipsync_rollout.request_manual_refund(
            self.db, "admin", refund_first["attempt_id"],
            "provider failure", incident_id="INC-REFUND-FIRST", now=200,
        )
        transactions_before_retry = dict(ledger.transactions)
        with self.assertRaises(
            short_drama_lipsync_jobs.LipsyncJobError
        ) as pending_retry:
            short_drama_lipsync_jobs.retry(
                self.db, "alice", refund_first["id"], now=201
            )
        self.assertEqual(
            "retry_billing_not_active", pending_retry.exception.code
        )
        with closing(self.db()) as conn:
            pending = conn.execute(
                "SELECT job.state,job.lease_token,job.provider_job_id,"
                "attempt.state FROM short_drama_lipsync_jobs job "
                "JOIN short_drama_lipsync_attempts attempt "
                "ON attempt.id=job.attempt_id WHERE job.id=?",
                (refund_first["id"],),
            ).fetchone()
        self.assertEqual(
            ("failed", None, None, "refund_pending"), pending
        )
        self.assertEqual(transactions_before_retry, ledger.transactions)
        self.assertTrue(
            short_drama_lipsync_jobs.reconcile_refund_attempt(
                self.db, ledger, refund_first["attempt_id"], now=202
            )
        )
        with self.assertRaises(
            short_drama_lipsync_jobs.LipsyncJobError
        ) as refunded_retry:
            short_drama_lipsync_jobs.retry(
                self.db, "alice", refund_first["id"], now=203
            )
        self.assertEqual(
            "retry_billing_not_active", refunded_retry.exception.code
        )

        retry_first, _ = self.prepared("retry-before-refund")
        short_drama_lipsync_jobs.charge(
            self.db, retry_first["id"], ledger
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_jobs SET state='failed' "
                "WHERE id=?",
                (retry_first["id"],),
            )
            conn.commit()
        retried = short_drama_lipsync_jobs.retry(
            self.db, "alice", retry_first["id"], now=204
        )
        self.assertEqual("queued", retried["state"])
        self.assertEqual("charged", retried["attempt_state"])
        with self.assertRaises(
            short_drama_lipsync_rollout.RolloutError
        ) as refund_after_retry:
            short_drama_lipsync_rollout.request_manual_refund(
                self.db, "admin", retry_first["attempt_id"],
                "provider failure", incident_id="INC-RETRY-FIRST",
                now=205,
            )
        self.assertEqual(
            "refund_job_not_terminal", refund_after_retry.exception.code
        )
        with closing(self.db()) as conn:
            retry_first_state = conn.execute(
                "SELECT job.state,attempt.state "
                "FROM short_drama_lipsync_jobs job "
                "JOIN short_drama_lipsync_attempts attempt "
                "ON attempt.id=job.attempt_id WHERE job.id=?",
                (retry_first["id"],),
            ).fetchone()
        self.assertEqual(("queued", "charged"), retry_first_state)

    def test_refund_reconciliation_blocks_unsafe_job_and_emits_alert(self):
        ledger = FakeLedger()
        job, _ = self.prepared("unsafe-refund-reconcile")
        short_drama_lipsync_jobs.charge(self.db, job["id"], ledger)
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_attempts "
                "SET state='refund_pending' WHERE id=?",
                (job["attempt_id"],),
            )
            conn.execute(
                "UPDATE short_drama_lipsync_jobs SET state='queued' "
                "WHERE id=?",
                (job["id"],),
            )
            conn.commit()
        transactions_before = dict(ledger.transactions)
        self.assertFalse(
            short_drama_lipsync_jobs.reconcile_refund_attempt(
                self.db, ledger, job["attempt_id"], now=300
            )
        )
        self.assertEqual(
            [],
            short_drama_lipsync_jobs.reconcile_refunds(
                self.db, ledger, now=301
            ),
        )
        self.assertEqual(transactions_before, ledger.transactions)
        with closing(self.db()) as conn:
            state = conn.execute(
                "SELECT job.state,attempt.state "
                "FROM short_drama_lipsync_jobs job "
                "JOIN short_drama_lipsync_attempts attempt "
                "ON attempt.id=job.attempt_id WHERE job.id=?",
                (job["id"],),
            ).fetchone()
            alerts = conn.execute(
                "SELECT COUNT(*) FROM short_drama_lipsync_events "
                "WHERE event_name='lipsync.refund.blocked_unsafe_job' "
                "AND job_id=? AND attempt_id=?",
                (job["id"], job["attempt_id"]),
            ).fetchone()[0]
        self.assertEqual(("queued", "refund_pending"), state)
        self.assertEqual(2, alerts)

    def test_retry_cas_rejects_attempt_change_after_validation(self):
        job, _ = self.prepared("retry-attempt-cas")
        short_drama_lipsync_jobs.charge(
            self.db, job["id"], FakeLedger()
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_jobs SET state='failed' "
                "WHERE id=?",
                (job["id"],),
            )
            conn.commit()

        class AttemptChangesBeforeJobUpdate:
            def __init__(inner):
                inner.conn = self.db()
                inner.changed = False

            @property
            def row_factory(inner):
                return inner.conn.row_factory

            @row_factory.setter
            def row_factory(inner, value):
                inner.conn.row_factory = value

            def execute(inner, sql, args=()):
                if (
                    not inner.changed
                    and sql.startswith(
                        "UPDATE short_drama_lipsync_jobs "
                        "SET state='queued'"
                    )
                ):
                    inner.changed = True
                    inner.conn.execute(
                        "UPDATE short_drama_lipsync_attempts "
                        "SET state='refund_pending' WHERE id=?",
                        (job["attempt_id"],),
                    )
                return inner.conn.execute(sql, args)

            def close(inner):
                return inner.conn.close()

        with self.assertRaises(
            short_drama_lipsync_jobs.LipsyncJobError
        ) as conflict:
            short_drama_lipsync_jobs.retry(
                AttemptChangesBeforeJobUpdate,
                "alice", job["id"], now=400,
            )
        self.assertEqual("retry_state_conflict", conflict.exception.code)
        with closing(self.db()) as conn:
            state = conn.execute(
                "SELECT job.state,attempt.state "
                "FROM short_drama_lipsync_jobs job "
                "JOIN short_drama_lipsync_attempts attempt "
                "ON attempt.id=job.attempt_id WHERE job.id=?",
                (job["id"],),
            ).fetchone()
        self.assertEqual(("failed", "charged"), state)

    def test_diagnostics_redacts_real_rows_and_nested_sensitive_values(self):
        self.pr_g_version(
            "diagnostics-real-row", version_id="diagnostics-version"
        )
        with closing(self.db()) as conn:
            job_id = conn.execute(
                "SELECT job_id FROM short_drama_lipsync_versions "
                "WHERE id='diagnostics-version'"
            ).fetchone()[0]
            conn.execute(
                "UPDATE short_drama_lipsync_jobs "
                "SET result_json=?,error_json=? WHERE id=?",
                (
                    '{"url":"https://private.invalid/result",'
                    '"token":"provider-token"}',
                    '{"detail":"private provider error"}',
                    job_id,
                ),
            )
            conn.execute(
                "UPDATE short_drama_lipsync_attempts SET terminal_json=? "
                "WHERE id=(SELECT attempt_id FROM short_drama_lipsync_jobs "
                "WHERE id=?)",
                ('{"payload":"private billing payload"}', job_id),
            )
            conn.commit()
        result = short_drama_lipsync_diagnostics.query(
            self.db, {"job_id": job_id}, actor="admin"
        )
        encoded = __import__("json").dumps(
            result, ensure_ascii=False, sort_keys=True
        )
        self.assertNotIn("alice", encoded)
        self.assertNotIn("lipsync/shot-1/v1.mp4", encoded)
        for secret in (
            "https://private.invalid/result", "provider-token",
            "private provider error", "private billing payload",
        ):
            self.assertNotIn(secret, encoded)
        nested = short_drama_lipsync_diagnostics._redact({
            "owner_username": "bob",
            "file": "users/bob/private.mp4",
            "detail_json": '{"error":"nested database secret"}',
            "items": [{
                "actor_username": "alice",
                "url": "https://private.invalid/video",
                "token": "unsafe-token",
                "payload": {"private": "content"},
            }],
        })
        nested_encoded = __import__("json").dumps(nested, sort_keys=True)
        for secret in (
            "alice", "bob", "users/bob/private.mp4",
            "https://private.invalid/video", "unsafe-token", "content",
            "nested database secret",
        ):
            self.assertNotIn(secret, nested_encoded)

    def test_background_service_claims_polls_validates_and_settles(self):
        job, _ = self.prepared("background-service")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())
        provider = FakeLipsyncProvider()
        temporary = Path(self.path).parent / (
            "worker-" + Path(self.path).stem
        )
        temporary.mkdir()
        self.addCleanup(lambda: shutil.rmtree(temporary, ignore_errors=True))
        probes = [
            {
                "duration_ms": 5000,
                "video": {
                    "width": 1280, "height": 720, "fps": 25,
                    "codec": "h264",
                },
                "audio": {"codec": "aac"},
            },
            {
                "duration_ms": 5000,
                "video": {
                    "width": 1280, "height": 720, "fps": 25,
                    "codec": "h264",
                },
                "audio": None,
            },
        ]
        service = short_drama_lipsync_worker.WorkerService(
            self.db, object(), output_root=temporary / "out",
            work_dir=temporary / "work",
            provider_resolver=lambda _name: provider,
            probe=lambda _path: probes.pop(0),
            remux=lambda src, dst: Path(dst).write_bytes(
                Path(src).read_bytes()
            ),
        )
        self.assertEqual([job["id"]], service.run_cycle())
        linked = short_drama_lipsync_jobs.get(
            self.db, "alice", job["id"]
        )
        provider.jobs[linked["provider_job_id"]]["status"] = "succeeded"
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_jobs SET next_poll_at=0 "
                "WHERE id=?", (job["id"],)
            )
            conn.commit()
        self.assertEqual([job["id"]], service.run_cycle())
        settled = short_drama_lipsync_jobs.get(
            self.db, "alice", job["id"]
        )
        self.assertEqual("succeeded", settled["state"])
        self.assertEqual("settled", settled["attempt_state"])
        self.assertTrue(
            (temporary / "out" / settled["result"]["file"]).is_file()
        )

    def test_background_media_rejection_fails_and_schedules_refund(self):
        job, _ = self.prepared("background-media-rejection")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())
        provider = FakeLipsyncProvider()
        first = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-1"
        )
        provider.jobs[first["provider_job_id"]]["status"] = "succeeded"
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_jobs SET next_poll_at=0 "
                "WHERE id=?", (job["id"],)
            )
            conn.commit()
        temporary = Path(self.path).parent / (
            "rejected-" + Path(self.path).stem
        )
        temporary.mkdir()
        self.addCleanup(lambda: shutil.rmtree(temporary, ignore_errors=True))
        rejected = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-2",
            work_dir=temporary / "work",
            output_root=temporary / "out",
            probe=lambda _path: {},
        )
        self.assertEqual("failed", rejected["state"])
        self.assertEqual("refund_pending", rejected["attempt_state"])
        self.assertEqual(
            "media_acceptance_failed", rejected["error"]["code"]
        )

    def test_real_ffprobe_timeout_is_retryable_but_rejection_is_not(self):
        def timeout_runner(command, **options):
            raise subprocess.TimeoutExpired(command, options["timeout"])

        timeout_probe = lambda path: short_drama_assembly_plan.probe_media(
            path, runner=timeout_runner
        )
        with self.assertRaises(
            short_drama_lipsync_media.LipsyncMediaInfrastructureError
        ):
            short_drama_lipsync_media._probe_media(timeout_probe, "result.mp4")

        def rejected_runner(command, **_options):
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="invalid media"
            )

        rejected_probe = lambda path: short_drama_assembly_plan.probe_media(
            path, runner=rejected_runner
        )
        with self.assertRaises(
            short_drama_lipsync_media.LipsyncMediaValidationError
        ):
            short_drama_lipsync_media._probe_media(
                rejected_probe, "result.mp4"
            )

    def test_ffprobe_timeout_defers_without_refund_then_settles(self):
        job, _ = self.prepared("probe-timeout")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())
        provider = FakeLipsyncProvider()
        linked = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-1"
        )
        provider.jobs[linked["provider_job_id"]]["status"] = "succeeded"
        temporary = Path(self.path).parent / (
            "probe-timeout-" + Path(self.path).stem
        )
        temporary.mkdir()
        self.addCleanup(lambda: shutil.rmtree(temporary, ignore_errors=True))
        probe_calls = {"count": 0}

        def timeout_then_succeed(path):
            probe_calls["count"] += 1
            if probe_calls["count"] == 1:
                def timeout_runner(command, **options):
                    raise subprocess.TimeoutExpired(
                        command, options["timeout"]
                    )

                return short_drama_assembly_plan.probe_media(
                    path, runner=timeout_runner
                )
            return {
                "duration_ms": 5000,
                "video": {
                    "width": 1280, "height": 720, "fps": 25,
                    "codec": "h264",
                },
                "audio": None,
            }

        options = {
            "work_dir": temporary / "work",
            "output_root": temporary / "out",
            "probe": timeout_then_succeed,
            "remux": lambda src, dst: Path(dst).write_bytes(
                Path(src).read_bytes()
            ),
        }
        deferred = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-2", **options
        )
        self.assertEqual("running", deferred["state"])
        self.assertEqual("linked", deferred["attempt_state"])
        self.assertEqual(1, deferred["result_retry_count"])
        self.assertIsNone(deferred["refund_ref"])

        settled = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-3", **options
        )
        self.assertEqual("succeeded", settled["state"])
        self.assertEqual("settled", settled["attempt_state"])

    def test_transient_result_fetch_retries_without_refund_and_settles(self):
        job, _ = self.prepared("result-refetch")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())

        class CountingProvider(FakeLipsyncProvider):
            def __init__(inner):
                super().__init__(faults={"download_error": True})
                inner.fetch_calls = 0

            def fetch_result(inner, provider_job_id, destination):
                inner.fetch_calls += 1
                return super(CountingProvider, inner).fetch_result(
                    provider_job_id, destination
                )

        provider = CountingProvider()
        linked = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-1"
        )
        provider.jobs[linked["provider_job_id"]]["status"] = "succeeded"
        temporary = Path(self.path).parent / (
            "refetch-" + Path(self.path).stem
        )
        temporary.mkdir()
        self.addCleanup(lambda: shutil.rmtree(temporary, ignore_errors=True))
        deferred = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-2",
            work_dir=temporary / "work",
            output_root=temporary / "out",
            probe=lambda _path: {
                "duration_ms": 5000,
                "video": {
                    "width": 1280, "height": 720, "fps": 25,
                    "codec": "h264",
                },
                "audio": None,
            },
            remux=lambda src, dst: Path(dst).write_bytes(
                Path(src).read_bytes()
            ),
        )
        self.assertEqual("running", deferred["state"])
        self.assertEqual("linked", deferred["attempt_state"])
        self.assertEqual(1, deferred["result_retry_count"])
        self.assertTrue(deferred["error"]["retryable"])
        settled = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-3",
            work_dir=temporary / "work",
            output_root=temporary / "out",
            probe=lambda _path: {
                "duration_ms": 5000,
                "video": {
                    "width": 1280, "height": 720, "fps": 25,
                    "codec": "h264",
                },
                "audio": None,
            },
            remux=lambda src, dst: Path(dst).write_bytes(
                Path(src).read_bytes()
            ),
        )
        self.assertEqual("succeeded", settled["state"])
        self.assertEqual("settled", settled["attempt_state"])
        self.assertEqual(2, provider.fetch_calls)

    def test_transient_remux_failure_reuses_cached_provider_result(self):
        job, _ = self.prepared("result-cache")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())

        class CountingProvider(FakeLipsyncProvider):
            def __init__(inner):
                super().__init__()
                inner.fetch_calls = 0

            def fetch_result(inner, provider_job_id, destination):
                inner.fetch_calls += 1
                return super(CountingProvider, inner).fetch_result(
                    provider_job_id, destination
                )

        provider = CountingProvider()
        linked = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-1"
        )
        provider.jobs[linked["provider_job_id"]]["status"] = "succeeded"
        temporary = Path(self.path).parent / (
            "cached-result-" + Path(self.path).stem
        )
        temporary.mkdir()
        self.addCleanup(lambda: shutil.rmtree(temporary, ignore_errors=True))
        remux_calls = []

        def flaky_remux(source, destination):
            remux_calls.append(source)
            if len(remux_calls) == 1:
                raise OSError("temporary ffmpeg failure")
            Path(destination).write_bytes(Path(source).read_bytes())

        options = {
            "work_dir": temporary / "work",
            "output_root": temporary / "out",
            "probe": lambda _path: {
                "duration_ms": 5000,
                "video": {
                    "width": 1280, "height": 720, "fps": 25,
                    "codec": "h264",
                },
                "audio": None,
            },
            "remux": flaky_remux,
        }
        deferred = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-2", **options
        )
        self.assertEqual("running", deferred["state"])
        self.assertEqual("linked", deferred["attempt_state"])
        settled = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-3", **options
        )
        self.assertEqual("succeeded", settled["state"])
        self.assertEqual(1, provider.fetch_calls)
        self.assertEqual(2, len(remux_calls))

    def test_fetch_failure_without_refetch_enters_manual_review(self):
        job, _ = self.prepared("no-result-refetch")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())
        provider = FakeLipsyncProvider(faults={"download_error": True})
        provider.supports_result_refetch = False
        linked = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-1"
        )
        provider.jobs[linked["provider_job_id"]]["status"] = "succeeded"
        temporary = Path(self.path).parent / (
            "no-refetch-" + Path(self.path).stem
        )
        temporary.mkdir()
        self.addCleanup(lambda: shutil.rmtree(temporary, ignore_errors=True))
        result = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-2",
            work_dir=temporary / "work",
            output_root=temporary / "out",
            probe=lambda _path: {},
        )
        self.assertEqual("manual_review", result["state"])
        self.assertEqual("manual_review", result["attempt_state"])
        self.assertIsNone(result["refund_ref"])

    def test_result_retry_exhaustion_enters_manual_review_without_refund(self):
        job, _ = self.prepared("result-retry-exhausted")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())
        provider = FakeLipsyncProvider()
        linked = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-1"
        )
        provider.jobs[linked["provider_job_id"]]["status"] = "succeeded"
        temporary = Path(self.path).parent / (
            "retry-exhausted-" + Path(self.path).stem
        )
        temporary.mkdir()
        self.addCleanup(lambda: shutil.rmtree(temporary, ignore_errors=True))
        options = {
            "work_dir": temporary / "work",
            "output_root": temporary / "out",
            "probe": lambda _path: {
                "duration_ms": 5000,
                "video": {
                    "width": 1280, "height": 720, "fps": 25,
                    "codec": "h264",
                },
                "audio": None,
            },
            "remux": lambda _src, _dst: (
                (_ for _ in ()).throw(OSError("disk temporarily unavailable"))
            ),
            "max_result_retries": 2,
        }
        first = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-2", **options
        )
        self.assertEqual("running", first["state"])
        exhausted = short_drama_lipsync_worker.run_once(
            self.db, job["id"], provider, "worker-3", **options
        )
        self.assertEqual("manual_review", exhausted["state"])
        self.assertEqual("manual_review", exhausted["attempt_state"])
        self.assertIsNone(exhausted["refund_ref"])

    def test_worker_loop_survives_one_database_locked_cycle(self):
        job, _ = self.prepared("worker-loop-liveness")
        short_drama_lipsync_jobs.charge(self.db, job["id"], FakeLedger())
        provider = FakeLipsyncProvider()
        locked = threading.Event()
        calls = {"count": 0}

        def flaky_db():
            calls["count"] += 1
            if calls["count"] == 1:
                locked.set()
                raise sqlite3.OperationalError("database is locked")
            return self.db()

        temporary = Path(self.path).parent / (
            "worker-live-" + Path(self.path).stem
        )
        temporary.mkdir()
        self.addCleanup(lambda: shutil.rmtree(temporary, ignore_errors=True))
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_LIPSYNC_RECONCILE_ENABLED": "0",
        }):
            service = short_drama_lipsync_worker.WorkerService(
                flaky_db, object(), output_root=temporary / "out",
                provider_resolver=lambda _name: provider,
            )
            service.start()
            self.addCleanup(service.stop)
            self.assertTrue(locked.wait(2))
            service.wake()
            deadline = time.time() + 4
            while provider.create_calls == 0 and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(1, provider.create_calls)
            self.assertTrue(service.is_healthy())
            self.assertEqual(1, service.live_worker_count())

    def test_runtime_readiness_requires_a_live_worker(self):
        temporary = Path(self.path).parent / (
            "worker-health-" + Path(self.path).stem
        )
        temporary.mkdir()
        self.addCleanup(lambda: shutil.rmtree(temporary, ignore_errors=True))
        provider = FakeLipsyncProvider()
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_LIPSYNC_RECONCILE_ENABLED": "0",
        }):
            service = short_drama_lipsync_worker.WorkerService(
                self.db, object(), output_root=temporary / "out",
                provider_resolver=lambda _name: provider,
            )
            with mock.patch.object(
                short_drama_lipsync_worker, "_service", service
            ):
                self.assertFalse(
                    short_drama_lipsync_worker.runtime_ready(provider.name)
                )
                service.start()
                self.assertTrue(
                    short_drama_lipsync_worker.runtime_ready(provider.name)
                )
                service.stop()
                self.assertFalse(
                    short_drama_lipsync_worker.runtime_ready(provider.name)
                )

    def test_http_create_get_cancel_and_viewer_permissions(self):
        quote = self.paid_quote("http")
        body = {
            "project_id": self.project_id,
            "shot_id": "shot-1",
            "quote_id": quote["quote_id"],
            "expected_input_hash": quote["input_hash"],
        }
        transactions = {}

        def deduct(username, amount, reason, transaction_key=""):
            transactions[transaction_key] = {
                "id": transaction_key, "points": 900
            }
            return 900

        create = RouteHandler(
            "/api/gen/short-drama/lipsync/jobs", body
        )
        create.headers["Idempotency-Key"] = "http-job"
        self.assertTrue(short_drama_lipsync.short_drama_lipsync_jobs)
        from content_domains import short_drama, short_drama_lipsync_rollout
        short_drama_lipsync_rollout.set_config(
            self.db, "admin", {
                "enabled": True,
                "kill_switch": False,
                "percentage": 0,
                "allow_projects": [self.project_id],
                "provider_policy": {},
                "reason": "HTTP integration test",
            },
        )
        with mock.patch.object(
            short_drama_lipsync_rollout.feature_flags,
            "is_enabled", return_value=True,
        ):
            short_drama.dispatch_http(
                create, "POST", self.db, lambda _: {"username": "alice"},
                deduct_points=deduct,
                refund_points=lambda *args, **kwargs: 1000,
                charge_lookup=transactions.get,
            )
        self.assertEqual(202, create.response[0])
        job_id = create.response[1]["id"]
        read = RouteHandler(
            "/api/gen/short-drama/lipsync/jobs/" + job_id
        )
        short_drama.dispatch_http(
            read, "GET", self.db, lambda _: {"username": "alice"}
        )
        self.assertEqual(200, read.response[0])
        cancel = RouteHandler(
            "/api/gen/short-drama/lipsync/jobs/" + job_id + "/cancel", {}
        )
        short_drama.dispatch_http(
            cancel, "POST", self.db, lambda _: {"username": "alice"}
        )
        self.assertEqual("cancel_pending", cancel.response[1]["state"])

        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET board_id='board-1' WHERE id=?",
                (self.project_id,),
            )
            conn.commit()
        denied = RouteHandler(
            "/api/gen/short-drama/lipsync/jobs", body
        )
        denied.headers["Idempotency-Key"] = "viewer-job"
        short_drama.dispatch_http(
            denied, "POST", self.db, lambda _: {"username": "bob"},
            canvas_access_resolver=lambda _: {
                "board_id": "board-1", "role": "viewer"
            },
        )
        self.assertEqual(403, denied.response[0])

    def test_media_security_and_atomic_publish(self):
        with self.assertRaises(short_drama_lipsync_media.LipsyncMediaError):
            short_drama_lipsync_media.validate_result_url(
                "https://127.0.0.1/result.mp4"
            )
        temporary = (
            Path(self.path).parent / ("media-" + Path(self.path).stem)
        )
        temporary.mkdir()
        self.addCleanup(lambda: shutil.rmtree(temporary, ignore_errors=True))
        with mock.patch.object(tempfile, "tempdir", str(temporary)):
            source = Path(temporary) / "source.mp4"
            source.write_bytes(b"video")
            probes = [
                {
                    "duration_ms": 2000,
                    "video": {"width": 1280, "height": 720, "fps": 25, "codec": "h264"},
                    "audio": {"codec": "aac"},
                },
                {
                    "duration_ms": 2000,
                    "video": {"width": 1280, "height": 720, "fps": 25, "codec": "h264"},
                    "audio": None,
                },
            ]
            result = short_drama_lipsync_media.accept_and_publish(
                job_id="job-1", project_id="project-1", shot_id="shot-1",
                provider="fake", source_file=source, output_root=temporary,
                expected_spec={"width": 1280, "height": 720, "duration_ms": 2000},
                probe=lambda _: probes.pop(0),
                remux=lambda src, dst: Path(dst).write_bytes(Path(src).read_bytes()),
            )
            self.assertTrue((Path(temporary) / result["file"]).is_file())
            self.assertEqual(64, len(result["file_hash"]))


if __name__ == "__main__":
    unittest.main()
