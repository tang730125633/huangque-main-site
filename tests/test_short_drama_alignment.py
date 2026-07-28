import sqlite3
import sys
import threading
import time
import types
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
        "project_revision": revision,
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
        "project_revision": revision,
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

    def silent_current(self, conn, project):
        snapshot = {
            "project_id": project["id"],
            "revision": project["revision"],
            "shots": [],
        }
        return snapshot, silent_contract(project["id"], project["revision"])

    def test_real_word_timestamps_preserve_pause_and_variable_rate(self):
        current = contract(self.project["id"], self.project["revision"])
        line = current["shots"][0]["lines"][0]
        line["text"] = "前慢后快"
        line["audio_file"] = "audio/variable-rate.wav"

        def transcriber(audio_file):
            self.assertEqual("audio/variable-rate.wav", audio_file)
            return [
                {
                    "word": "前", "start_ms": 0, "end_ms": 700,
                    "confidence": 0.97,
                },
                {
                    "word": "慢", "start_ms": 700, "end_ms": 1400,
                    "confidence": 0.96,
                },
                {
                    "word": "后", "start_ms": 1600, "end_ms": 1750,
                    "confidence": 0.94,
                },
                {
                    "word": "快", "start_ms": 1750, "end_ms": 1900,
                    "confidence": 0.93,
                },
            ]

        timeline, quality = alignment._align(
            current, transcriber=transcriber
        )
        tokens = timeline[0]["tokens"]
        self.assertEqual(
            [700, 700, 150, 150],
            [item["end_ms"] - item["start_ms"] for item in tokens],
        )
        self.assertEqual(200, tokens[0]["start_ms"])
        self.assertEqual(1800, tokens[2]["start_ms"])
        self.assertEqual("asr_word_timestamps", quality["provider_mode"])
        self.assertEqual(1.0, quality["coverage"])
        self.assertEqual(0, quality["estimated_token_count"])
        self.assertTrue(all(
            token["match_type"] == "asr_word" for token in tokens
        ))

    def test_asr_word_interpolation_retains_real_pause_boundaries(self):
        current = contract(self.project["id"], self.project["revision"])
        line = current["shots"][0]["lines"][0]
        line["text"] = "你好世界"
        line["audio_file"] = "audio/pause.wav"
        timeline, quality = alignment._align(
            current,
            transcriber=lambda _path: [
                {
                    "word": "你好", "start_ms": 100, "end_ms": 500,
                    "confidence": 0.95,
                },
                {
                    "word": "世界", "start_ms": 1200, "end_ms": 1800,
                    "confidence": 0.91,
                },
            ],
        )
        tokens = timeline[0]["tokens"]
        self.assertEqual(300, tokens[0]["start_ms"])
        self.assertEqual(700, tokens[1]["end_ms"])
        self.assertEqual(1400, tokens[2]["start_ms"])
        self.assertEqual(2000, tokens[3]["end_ms"])
        self.assertTrue(all(
            token["match_type"] == "interpolated_within_asr_word"
            for token in tokens
        ))
        self.assertEqual("asr_word_timestamps", quality["provider_mode"])

    def test_unmatched_asr_tokens_are_explicit_estimates_without_confidence(self):
        current = contract(self.project["id"], self.project["revision"])
        line = current["shots"][0]["lines"][0]
        line["text"] = "你好世界"
        line["audio_file"] = "audio/incomplete.wav"
        timeline, quality = alignment._align(
            current,
            transcriber=lambda _path: [{
                "word": "你好", "start_ms": 100, "end_ms": 600,
                "confidence": 0.93,
            }],
        )
        estimated = [
            token for token in timeline[0]["tokens"]
            if token["match_type"] == "estimated"
        ]
        self.assertEqual(2, len(estimated))
        self.assertTrue(all(token["confidence"] is None for token in estimated))
        self.assertEqual("mixed", quality["provider_mode"])
        self.assertEqual(2, quality["estimated_token_count"])
        self.assertIn(
            "estimated_timing_present",
            {item["code"] for item in quality["blockers"]},
        )

    def test_provider_failure_is_downgraded_and_never_fakes_confidence(self):
        current = contract(self.project["id"], self.project["revision"])
        current["shots"][0]["lines"][0]["audio_file"] = "audio/missing.wav"

        def unavailable(_path):
            raise alignment.AlignmentProviderUnavailable("model unavailable")

        timeline, quality = alignment._align(
            current, transcriber=unavailable
        )
        self.assertEqual("estimated_fallback", quality["provider_mode"])
        self.assertIsNone(quality["mean_confidence"])
        self.assertTrue(all(
            token["confidence"] is None
            for token in timeline[0]["tokens"]
        ))
        self.assertIn(
            "forced_alignment_unavailable",
            {item["code"] for item in quality["blockers"]},
        )

    def test_faster_whisper_provider_requests_real_word_timestamps(self):
        calls = {}

        class FakeModel:
            def __init__(self, model_name, **kwargs):
                calls["init"] = (model_name, kwargs)

            def transcribe(self, path, **kwargs):
                calls["transcribe"] = (path, kwargs)
                word = types.SimpleNamespace(
                    word="你好", start=0.25, end=0.85, probability=0.91
                )
                return [types.SimpleNamespace(words=[word])], object()

        fake_module = types.SimpleNamespace(WhisperModel=FakeModel)
        source = Path(self.path).with_suffix(".wav")
        source.write_bytes(b"real-audio-fixture")
        alignment._WHISPER_MODEL = None
        alignment._WHISPER_MODEL_NAME = None
        try:
            with (
                mock.patch.dict(
                    sys.modules, {"faster_whisper": fake_module}
                ),
                mock.patch(
                    "content_domains.core._resolve_out_file",
                    return_value=source,
                ),
            ):
                words = alignment._transcribe_words("audio/line.wav")
        finally:
            alignment._WHISPER_MODEL = None
            alignment._WHISPER_MODEL_NAME = None
            source.unlink(missing_ok=True)
        self.assertEqual("small", calls["init"][0])
        self.assertEqual(str(source), calls["transcribe"][0])
        self.assertTrue(calls["transcribe"][1]["word_timestamps"])
        self.assertTrue(calls["transcribe"][1]["vad_filter"])
        self.assertEqual("zh", calls["transcribe"][1]["language"])
        self.assertEqual(250, words[0]["start_ms"])
        self.assertEqual(850, words[0]["end_ms"])

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


if __name__ == "__main__":
    unittest.main()
