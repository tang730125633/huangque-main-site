import json
import sqlite3
import sys
import threading
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest import mock


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import (
    short_drama,
    short_drama_alignment as alignment,
    short_drama_voice,
)
from tests.fixtures.short_drama_alignment_provider import FakeAlignmentProvider


def contract(project_id, revision):
    shots = [{
        "shot_id": "shot-1",
        "start_ms": 0,
        "end_ms": 5000,
        "lines": [{
            "shot_id": "shot-1",
            "line_id": "line-1",
            "text": "你好，世界",
            "audio_start_ms": 200,
            "audio_end_ms": 2200,
            "source_version": 1,
            "source_hash": "a" * 64,
        }],
    }]
    identity = {
        "contract_version": alignment.CONTRACT_VERSION,
        "project_id": project_id,
        "master_audio_hash": "m" * 64,
        "transcript_hash": "t" * 64,
        "language": "zh-CN",
        "provider": alignment.PROVIDER_NAME,
        "model_version": alignment.MODEL_VERSION,
        "shots": shots,
    }
    return {
        "input_hash": alignment._hash(identity),
        "master_audio_hash": "m" * 64,
        "transcript_hash": "t" * 64,
        "identity": identity,
        "shots": shots,
        "blockers": [],
    }


def silent_contract(project_id, revision):
    identity = {
        "contract_version": alignment.CONTRACT_VERSION,
        "project_id": project_id,
        "master_audio_hash": "s" * 64,
        "transcript_hash": "e" * 64,
        "language": "zh-CN",
        "provider": alignment.PROVIDER_NAME,
        "model_version": alignment.MODEL_VERSION,
        "shots": [],
    }
    return {
        "input_hash": alignment._hash(identity),
        "master_audio_hash": "s" * 64,
        "transcript_hash": "e" * 64,
        "identity": identity,
        "shots": [],
        "blockers": [],
    }


class ShortDramaAlignmentTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests"
        temp_root.mkdir(exist_ok=True)
        self.path = str(temp_root / ("alignment-%s.db" % uuid.uuid4().hex))
        self.db = lambda: sqlite3.connect(self.path)
        short_drama.init_db(self.db)
        self.project = short_drama.create_project(self.db, "alice", {
            "title": "对齐测试",
            "synopsis": "验证字幕强制对齐",
            "ratio": "9:16",
            "target_duration": 30,
            "shot_count": 6,
        })
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='voice_review' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()

    def tearDown(self):
        path = Path(self.path)
        if path.exists():
            path.unlink()

    def current(self, conn, project):
        snapshot = {
            "project_id": project["id"],
            "revision": project["revision"],
            "shots": [],
        }
        return snapshot, contract(project["id"], project["revision"])

    def test_contract_hash_ignores_unrelated_project_revision_changes(self):
        def snapshot(revision):
            return {
                "project_id": self.project["id"],
                "revision": revision,
                "shots": [{
                    "id": "shot-1",
                    "duration": 5,
                    "locked": True,
                    "lines": [{
                        "id": "line-1",
                        "start_ms": 200,
                        "subtitle_text": "hello",
                        "input_hash": "a" * 64,
                        "current_version": 1,
                        "versions": [{
                            "version": 1,
                            "status": "done",
                            "input_hash": "a" * 64,
                            "duration_ms": 2000,
                        }],
                    }],
                }],
            }

        first = alignment._input_contract(snapshot(1))
        second = alignment._input_contract(snapshot(99))
        self.assertEqual(first["input_hash"], second["input_hash"])
        self.assertEqual(first["identity"], second["identity"])

    def test_locked_v1_contract_remains_valid_when_content_is_unchanged(self):
        current = contract(self.project["id"], self.project["revision"])
        legacy = {
            "contract_version": alignment.LEGACY_CONTRACT_VERSION,
            "input_hash": "legacy-project-revision-hash",
            "master_audio_hash": current["master_audio_hash"],
            "transcript_hash": current["transcript_hash"],
            "provider": current["identity"]["provider"],
            "model_version": current["identity"]["model_version"],
        }
        self.assertTrue(alignment.version_matches_contract(legacy, current))
        changed = dict(current)
        changed["transcript_hash"] = "changed"
        self.assertFalse(alignment.version_matches_contract(legacy, changed))

    def silent_current(self, conn, project):
        snapshot = {
            "project_id": project["id"],
            "revision": project["revision"],
            "shots": [],
        }
        return snapshot, silent_contract(project["id"], project["revision"])

    def test_generate_review_lock_and_replay_without_points(self):
        payload = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
        }
        with mock.patch.object(alignment, "_current_contract", self.current):
            created = alignment.create_job(
                self.db, "alice", payload, "alignment-key-1"
            )
            replayed = alignment.create_job(
                self.db, "alice", payload, "alignment-key-1"
            )
            reused = alignment.create_job(
                self.db, "alice", payload, "alignment-key-1-reused"
            )
            self.assertFalse(created["replayed"])
            self.assertTrue(replayed["replayed"])
            self.assertFalse(replayed["reused"])
            self.assertFalse(reused["replayed"])
            self.assertTrue(reused["reused"])
            self.assertEqual(
                created["workspace"]["current_version"]["id"],
                reused["workspace"]["current_version"]["id"],
            )
            with closing(self.db()) as duplicate_check:
                self.assertEqual(1, duplicate_check.execute(
                    "SELECT COUNT(*) FROM short_drama_alignment_versions"
                ).fetchone()[0])
            self.assertFalse(created["workspace"]["handoff"]["ready"])
            self.assertTrue(created["workspace"]["handoff"]["required"])
            version = reused["workspace"]["current_version"]
            self.assertEqual("needs_review", version["status"])
            self.assertTrue(all(
                token["match_type"] == "estimated"
                for token in version["timeline"][0]["tokens"]
            ))
            reviewed = alignment.save_timeline(self.db, "alice", {
                "project_id": self.project["id"],
                "version_id": version["id"],
                "revision": version["revision"],
                "review_action": "save_adjustments",
                "lines": [{
                    "line_id": "line-1",
                    "subtitle_start_ms": 250,
                    "subtitle_end_ms": 2100,
                }],
            })
            reviewed_version = reviewed["current_version"]
            self.assertEqual("ready", reviewed_version["status"])
            locked = alignment.lock_version(self.db, "alice", {
                "project_id": self.project["id"],
                "version_id": reviewed_version["id"],
                "revision": reviewed_version["revision"],
            })
            self.assertEqual("locked", locked["current_version"]["status"])
            self.assertTrue(locked["handoff"]["ready"])
            alignment.require_current_locked(
                self.db, "alice", self.project["id"]
            )
        with closing(self.db()) as conn:
            self.assertEqual(2, conn.execute(
                "SELECT COUNT(*) FROM short_drama_alignment_versions"
            ).fetchone()[0])
            self.assertEqual(2, conn.execute(
                "SELECT COUNT(*) FROM short_drama_alignment_jobs"
            ).fetchone()[0])
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(DISTINCT version_id) "
                "FROM short_drama_alignment_jobs"
            ).fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_voice_charge_attempts"
            ).fetchone()[0])
            self.assertEqual(
                {"alignment_json", "webvtt", "ass"},
                {
                    row[0] for row in conn.execute(
                        "SELECT kind FROM short_drama_alignment_artifacts"
                    )
                },
            )

    def test_legacy_workspace_does_not_require_alignment_for_handoff(self):
        with mock.patch.object(alignment, "_current_contract", self.current):
            workspace = alignment.get_workspace(
                self.db, "alice", self.project["id"]
            )
        self.assertFalse(workspace["handoff"]["required"])
        self.assertTrue(workspace["handoff"]["ready"])
        self.assertEqual([], workspace["handoff"]["blockers"])

    def test_silent_project_can_review_lock_and_handoff_empty_timeline(self):
        payload = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
        }
        with mock.patch.object(
            alignment, "_current_contract", self.silent_current
        ):
            generated = alignment.create_job(
                self.db, "alice", payload, "alignment-silent"
            )
            source = generated["workspace"]["current_version"]
            self.assertEqual([], source["timeline"])
            with self.assertRaises(alignment.AlignmentError) as context:
                alignment.save_timeline(self.db, "alice", {
                    "project_id": self.project["id"],
                    "version_id": source["id"],
                    "revision": source["revision"],
                    "review_action": "save_adjustments",
                    "lines": [],
                })
            self.assertEqual("review_action_mismatch", context.exception.code)

            reviewed = alignment.save_timeline(
                self.db, "alice", {
                    "project_id": self.project["id"],
                    "version_id": source["id"],
                    "revision": source["revision"],
                    "review_action": "confirm_unchanged",
                    "lines": [],
                },
                actor_username="silent-reviewer",
            )["current_version"]
            self.assertEqual("ready", reviewed["status"])
            self.assertEqual([], reviewed["timeline"])
            self.assertEqual(
                "silent-reviewer", reviewed["review"]["reviewed_by"]
            )
            locked = alignment.lock_version(self.db, "alice", {
                "project_id": self.project["id"],
                "version_id": reviewed["id"],
                "revision": reviewed["revision"],
            })
            self.assertEqual("locked", locked["current_version"]["status"])
            self.assertTrue(locked["handoff"]["ready"])
            self.assertEqual(
                reviewed["id"],
                alignment.require_locked_if_started(
                    self.db, "alice", self.project["id"]
                )["id"],
            )

    def test_nonempty_alignment_rejects_an_empty_review_timeline(self):
        payload = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
        }
        with mock.patch.object(alignment, "_current_contract", self.current):
            generated = alignment.create_job(
                self.db, "alice", payload, "alignment-empty-invalid"
            )
            source = generated["workspace"]["current_version"]
            with self.assertRaises(alignment.AlignmentError) as context:
                alignment.save_timeline(self.db, "alice", {
                    "project_id": self.project["id"],
                    "version_id": source["id"],
                    "revision": source["revision"],
                    "review_action": "confirm_unchanged",
                    "lines": [],
                })
        self.assertEqual("timeline_invalid", context.exception.code)

    def test_running_job_blocks_handoff_before_a_version_exists(self):
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            now = int(time.time())
            conn.execute(
                "INSERT INTO short_drama_alignment_jobs "
                "(id,username,project_id,idempotency_key,request_hash,input_hash,"
                "provider,provider_job_id,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "running-job", "alice", self.project["id"], "running-key",
                    "r" * 64, "i" * 64, alignment.PROVIDER_NAME,
                    "provider-running", "running", now, now,
                ),
            )
            conn.commit()
            project = conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()
            with self.assertRaises(alignment.AlignmentError) as context:
                alignment.require_locked_if_started_in_transaction(conn, project)
        self.assertEqual("active_alignment_job", context.exception.code)
        with mock.patch.object(alignment, "_current_contract", self.current):
            workspace = alignment.get_workspace(
                self.db, "alice", self.project["id"]
            )
        self.assertTrue(workspace["handoff"]["required"])
        self.assertFalse(workspace["handoff"]["ready"])
        self.assertEqual(
            "active_alignment_job",
            workspace["handoff"]["blockers"][0]["code"],
        )

    def test_stage_confirmation_cannot_race_provider_materialization(self):
        payload = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
        }
        provider_started = threading.Event()
        provider_release = threading.Event()
        worker_errors = []
        original_align = alignment._align

        def delayed_align(current_contract):
            provider_started.set()
            if not provider_release.wait(5):
                raise RuntimeError("provider test timed out")
            return original_align(current_contract)

        def create_alignment():
            try:
                alignment.create_job(
                    self.db, "alice", payload, "alignment-race"
                )
            except Exception as error:
                worker_errors.append(error)

        def voice_project(conn, owner, project_id, revision):
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM short_drama_projects "
                "WHERE id=? AND username=? AND revision=?",
                (project_id, owner, revision),
            ).fetchone()

        with mock.patch.object(alignment, "_current_contract", self.current):
            with mock.patch.object(alignment, "_align", side_effect=delayed_align):
                thread = threading.Thread(target=create_alignment)
                thread.start()
                try:
                    self.assertTrue(provider_started.wait(5))
                    with mock.patch.object(
                        short_drama_voice,
                        "_voice_project_for_write",
                        side_effect=voice_project,
                    ):
                        with mock.patch.object(
                            short_drama_voice,
                            "build_voice_snapshot",
                            return_value={
                                "handoff_blocked": False,
                                "handoff_blockers": [],
                            },
                        ):
                            with self.assertRaises(
                                alignment.AlignmentError
                            ) as context:
                                short_drama.confirm_stage(
                                    self.db,
                                    "alice",
                                    self.project["id"],
                                    self.project["revision"],
                                    "voice_review",
                                )
                    self.assertEqual(
                        "active_alignment_job", context.exception.code
                    )
                finally:
                    provider_release.set()
                    thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], worker_errors)
        with closing(self.db()) as conn:
            stage = conn.execute(
                "SELECT stage FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0]
        self.assertEqual("voice_review", stage)

    def test_estimated_timeline_can_be_explicitly_confirmed_without_changes(self):
        payload = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
        }
        with mock.patch.object(alignment, "_current_contract", self.current):
            generated = alignment.create_job(
                self.db, "alice", payload, "alignment-noop-review"
            )
            version = generated["workspace"]["current_version"]
            source = version["timeline"][0]
            review_payload = {
                "project_id": self.project["id"],
                "version_id": version["id"],
                "revision": version["revision"],
                "review_action": "confirm_unchanged",
                "lines": [{
                    "line_id": source["line_id"],
                    "subtitle_start_ms": source["subtitle_start_ms"],
                    "subtitle_end_ms": source["subtitle_end_ms"],
                }],
            }
            reviewed = alignment.save_timeline(
                self.db, "alice", review_payload,
                actor_username="reviewer",
            )["current_version"]
            replayed = alignment.save_timeline(
                self.db, "alice", review_payload,
                actor_username="reviewer",
            )["current_version"]
        self.assertEqual("ready", reviewed["status"])
        self.assertEqual(reviewed["id"], replayed["id"])
        self.assertEqual(version["timeline"], reviewed["timeline"])
        self.assertEqual("confirm_unchanged", reviewed["review"]["action"])
        self.assertEqual("reviewer", reviewed["review"]["reviewed_by"])
        self.assertEqual(version["id"], reviewed["review"]["source_version_id"])
        self.assertEqual(
            version["revision"], reviewed["review"]["source_revision"]
        )
        self.assertIsInstance(reviewed["review"]["reviewed_at"], int)
        with closing(self.db()) as conn:
            self.assertEqual(2, conn.execute(
                "SELECT COUNT(*) FROM short_drama_alignment_versions"
            ).fetchone()[0])

    def test_review_action_must_match_whether_boundaries_changed(self):
        payload = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
        }
        with mock.patch.object(alignment, "_current_contract", self.current):
            generated = alignment.create_job(
                self.db, "alice", payload, "alignment-review-mode"
            )
            version = generated["workspace"]["current_version"]
            source = version["timeline"][0]
            cases = [
                (
                    "save_adjustments",
                    source["subtitle_start_ms"],
                    source["subtitle_end_ms"],
                ),
                (
                    "confirm_unchanged",
                    source["subtitle_start_ms"] + 50,
                    source["subtitle_end_ms"] - 50,
                ),
            ]
            for action, start, end in cases:
                with self.subTest(action=action):
                    with self.assertRaises(
                        alignment.AlignmentError
                    ) as context:
                        alignment.save_timeline(self.db, "alice", {
                            "project_id": self.project["id"],
                            "version_id": version["id"],
                            "revision": version["revision"],
                            "review_action": action,
                            "lines": [{
                                "line_id": source["line_id"],
                                "subtitle_start_ms": start,
                                "subtitle_end_ms": end,
                            }],
                        })
                    self.assertEqual(
                        "review_action_mismatch", context.exception.code
                    )

    def test_idempotency_conflict_and_boundary_validation(self):
        payload = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
        }
        with mock.patch.object(alignment, "_current_contract", self.current):
            created = alignment.create_job(
                self.db, "alice", payload, "alignment-key-2"
            )
            def changed_current(conn, project):
                snapshot, current_contract = self.current(conn, project)
                current_contract = dict(current_contract)
                current_contract["input_hash"] = "b" * 64
                return snapshot, current_contract
            with mock.patch.object(
                alignment, "_current_contract", changed_current
            ):
                with self.assertRaises(alignment.AlignmentIdempotencyConflict):
                    alignment.create_job(
                        self.db, "alice", payload, "alignment-key-2"
                    )
            version = created["workspace"]["current_version"]
            with self.assertRaises(alignment.AlignmentError) as context:
                alignment.save_timeline(self.db, "alice", {
                    "project_id": self.project["id"],
                    "version_id": version["id"],
                    "revision": version["revision"],
                    "review_action": "save_adjustments",
                    "lines": [{
                        "line_id": "line-1",
                        "subtitle_start_ms": 0,
                        "subtitle_end_ms": 2400,
                    }],
                })
            self.assertEqual("timeline_boundary_invalid", context.exception.code)

    def test_locked_alignment_only_changes_subtitle_fields(self):
        plan = {
            "shots": [{
                "id": "shot-1",
                "start_ms": 0,
                "audio": {"lines": [{
                    "id": "line-1",
                    "start_ms": 200,
                    "end_ms": 2200,
                }]},
            }]
        }
        payload = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
        }
        with mock.patch.object(alignment, "_current_contract", self.current):
            generated = alignment.create_job(
                self.db, "alice", payload, "alignment-key-3"
            )
            source = generated["workspace"]["current_version"]
            reviewed = alignment.save_timeline(self.db, "alice", {
                "project_id": self.project["id"],
                "version_id": source["id"],
                "revision": source["revision"],
                "review_action": "save_adjustments",
                "lines": [{
                    "line_id": "line-1",
                    "subtitle_start_ms": 300,
                    "subtitle_end_ms": 2000,
                }],
            })["current_version"]
            alignment.lock_version(self.db, "alice", {
                "project_id": self.project["id"],
                "version_id": reviewed["id"],
                "revision": reviewed["revision"],
            })
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            updated = alignment.apply_locked_timeline(
                conn, self.project["id"], plan
            )
        line = updated["shots"][0]["audio"]["lines"][0]
        self.assertEqual(200, line["start_ms"])
        self.assertEqual(2200, line["end_ms"])
        self.assertEqual(300, line["subtitle_start_ms"])
        self.assertEqual(2000, line["subtitle_end_ms"])

    def test_lock_rejects_reviewed_version_without_audit_identity(self):
        payload = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
        }
        with mock.patch.object(alignment, "_current_contract", self.current):
            generated = alignment.create_job(
                self.db, "alice", payload, "alignment-audit-gate"
            )
            source = generated["workspace"]["current_version"]
            reviewed = alignment.save_timeline(self.db, "alice", {
                "project_id": self.project["id"],
                "version_id": source["id"],
                "revision": source["revision"],
                "review_action": "confirm_unchanged",
                "lines": [{
                    "line_id": item["line_id"],
                    "subtitle_start_ms": item["subtitle_start_ms"],
                    "subtitle_end_ms": item["subtitle_end_ms"],
                } for item in source["timeline"]],
            })["current_version"]
            with closing(self.db()) as conn:
                conn.execute(
                    "UPDATE short_drama_alignment_versions "
                    "SET reviewed_by=NULL WHERE id=?",
                    (reviewed["id"],),
                )
                conn.commit()
            with self.assertRaises(alignment.AlignmentError) as context:
                alignment.lock_version(self.db, "alice", {
                    "project_id": self.project["id"],
                    "version_id": reviewed["id"],
                    "revision": reviewed["revision"],
                })
        self.assertEqual("quality_gate_blocked", context.exception.code)

    def test_provider_identity_is_durable_before_materialization_failure(self):
        payload = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
        }
        with mock.patch.object(alignment, "_current_contract", self.current):
            with mock.patch.object(
                alignment, "_align", side_effect=RuntimeError("provider secret")
            ):
                with self.assertRaises(RuntimeError):
                    alignment.create_job(
                        self.db, "alice", payload, "alignment-key-failure"
                    )
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT provider_job_id,status,error_json "
                "FROM short_drama_alignment_jobs WHERE idempotency_key=?",
                ("alignment-key-failure",),
            ).fetchone()
        self.assertTrue(row["provider_job_id"])
        self.assertEqual("failed", row["status"])
        self.assertNotIn("provider secret", row["error_json"])

    def test_real_provider_contract_persists_quality_and_recovery_identity(self):
        fake = FakeAlignmentProvider(result={"segments": [{
            "line_id": "line-1",
            "transcript": "你好世界",
            "words": [
                {"token": token, "start_ms": 250 + index * 300,
                 "end_ms": 500 + index * 300, "confidence": 0.96}
                for index, token in enumerate("你好世界")
            ],
        }]})
        payload = {
            "project_id": self.project["id"],
            "revision": self.project["revision"],
        }
        with mock.patch.dict(
            "os.environ",
            {
                "HQ_SHORT_DRAMA_ALIGNMENT_PROVIDER": "fake-zh-alignment",
                "HQ_SHORT_DRAMA_ALIGNMENT_REAL_ENABLED": "1",
                "HQ_SHORT_DRAMA_ALIGNMENT_WORD_TIMING_ENABLED": "1",
            },
            clear=False,
        ), mock.patch.dict(
            alignment.PROVIDER_FACTORIES,
            {"fake-zh-alignment": lambda: fake},
            clear=False,
        ), mock.patch.object(alignment, "_current_contract", self.current):
            created = alignment.create_job(
                self.db, "alice", payload, "alignment-real-provider"
            )
        version = created["workspace"]["current_version"]
        self.assertEqual("fake-zh-alignment", version["provider"])
        self.assertEqual("你好，世界", version["timeline"][0]["text"])
        self.assertEqual(1.0, version["quality"]["coverage"])
        self.assertEqual([], version["quality"]["unmatched_tokens"])
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT provider_job_id,provider_status,capabilities_json,"
                "trace_id,heartbeat_at,poll_count,terminal_json "
                "FROM short_drama_alignment_jobs WHERE idempotency_key=?",
                ("alignment-real-provider",),
            ).fetchone()
        self.assertEqual("fake-job-1", row["provider_job_id"])
        self.assertEqual("succeeded", row["provider_status"])
        self.assertEqual("fake-trace", row["trace_id"])
        self.assertGreater(row["heartbeat_at"], 0)
        self.assertEqual(1, row["poll_count"])
        self.assertTrue(json.loads(row["capabilities_json"])["supports_resume"])
        self.assertEqual("succeeded", json.loads(row["terminal_json"])["status"])

    def test_real_provider_is_default_off_and_exposes_feature_state(self):
        with mock.patch.dict(
            "os.environ",
            {"HQ_SHORT_DRAMA_ALIGNMENT_PROVIDER": "fake-zh-alignment"},
            clear=False,
        ), mock.patch.dict(
            alignment.PROVIDER_FACTORIES,
            {"fake-zh-alignment": FakeAlignmentProvider},
            clear=False,
        ), mock.patch.object(alignment, "_current_contract", self.current):
            workspace = alignment.get_workspace(
                self.db, "alice", self.project["id"]
            )
            self.assertFalse(workspace["provider"]["feature_enabled"])
            with self.assertRaises(alignment.AlignmentError) as context:
                alignment.create_job(
                    self.db,
                    "alice",
                    {
                        "project_id": self.project["id"],
                        "revision": self.project["revision"],
                    },
                    "alignment-disabled-provider",
                )
        self.assertEqual(
            "alignment_provider_unavailable", context.exception.code
        )

    def test_interrupted_real_job_preserves_recovery_identity(self):
        capabilities = {
            "supports_resume": True,
            "supports_cancel": True,
            "supports_result_refetch": True,
        }
        stale_at = int(alignment.time.time()) - (
            alignment.INTERRUPTED_JOB_SECONDS + 10
        )
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_alignment_jobs "
                "(id,username,project_id,idempotency_key,request_hash,"
                "input_hash,provider,model_version,provider_job_id,"
                "provider_status,status,capabilities_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "recoverable-real-job",
                    "alice",
                    self.project["id"],
                    "recoverable-real-key",
                    "request-hash",
                    "input-hash",
                    "fake-zh-alignment",
                    "fake-v1",
                    "provider-job-7",
                    "running",
                    "running",
                    json.dumps(capabilities),
                    stale_at,
                    stale_at,
                ),
            )
            conn.commit()
        with mock.patch.object(alignment, "_current_contract", self.current):
            workspace = alignment.get_workspace(
                self.db, "alice", self.project["id"]
            )
        self.assertEqual("running", workspace["job"]["status"])
        self.assertEqual(
            "alignment_refetch_required",
            workspace["job"]["last_error_code"],
        )
        self.assertTrue(workspace["job"]["recovery"]["required"])
        self.assertTrue(workspace["job"]["recovery"]["can_resume"])
        self.assertTrue(workspace["job"]["recovery"]["can_cancel"])
        self.assertFalse(workspace["job"]["recovery"]["can_refetch"])


if __name__ == "__main__":
    unittest.main()
