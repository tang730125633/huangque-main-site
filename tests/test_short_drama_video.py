import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from server.content_domains import short_drama, short_drama_video
from tests.test_short_drama_voice import GetHandler, voice_plan


class ShortDramaVideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "content.db"

        def db_factory():
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            return conn

        self.db = db_factory
        short_drama.init_db(self.db)
        project = short_drama.create_project(self.db, "alice", {
            "title": "C3", "synopsis": "A detective receives a mysterious call.",
            "ratio": "9:16", "target_duration": 30, "shot_count": 6,
            "point_budget": 100,
        })
        self.project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            voice_plan(), planning_cost=0, planning_job_id=901,
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='video_review' WHERE id=?",
                (self.project["id"],),
            )
            shot = conn.execute(
                "SELECT id,video_prompt FROM short_drama_shots "
                "WHERE project_id=? ORDER BY sort_order LIMIT 1",
                (self.project["id"],),
            ).fetchone()
            self.shot_id, self.prompt = shot["id"], shot["video_prompt"]
            conn.execute(
                "UPDATE short_drama_characters SET source_type='cinematic_avatar',"
                "avatar_id='12' WHERE project_id=? AND character_key='detective'",
                (self.project["id"],),
            )
            conn.execute(
                "INSERT INTO short_drama_assets "
                "(id,project_id,shot_id,type,current_version,locked,created_at,updated_at) "
                "VALUES ('asset-c3',?,?,'still',1,1,1,1)",
                (self.project["id"], self.shot_id),
            )
            conn.execute(
                "INSERT INTO short_drama_asset_versions "
                "(id,asset_id,version,job_id,url,file,prompt,ratio,status,created_at) "
                "VALUES ('asset-version-c3','asset-c3',1,801,'/still','still.png',"
                "'still','9:16','done',1)"
            )
            conn.commit()
        # The C-2 snapshot is immutable once the project enters video_review.
        with closing(self.db()) as conn:
            from server.content_domains import short_drama_voice
            short_drama_video.ensure_video_workspace(conn, self.project["id"])
            short_drama_voice.ensure_voice_workspace(conn, self.project["id"])
            conn.execute(
                "UPDATE short_drama_voice_shots SET locked=1 WHERE project_id=?",
                (self.project["id"],),
            )
            conn.commit()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def avatar_lookup(username, avatar_id):
        if username == "alice" and str(avatar_id) == "12":
            return {
                "id": 12, "username": "alice", "status": "ready",
                "provider_avatar_id": "provider-12",
            }
        raise ValueError("avatar missing")

    def _accepted_attempt(self, key):
        workspace = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"]
        )
        shot = next(
            item for item in workspace["shots"] if item["id"] == self.shot_id
        )
        quote = short_drama_video.prepare_video_quote(
            self.db, "alice", "alice", {
                "project_id": workspace["project_id"],
                "revision": workspace["revision"],
                "shot_id": shot["id"],
                "video_revision": shot["video_revision"],
                "prompt": self.prompt,
                "enhance_prompt": True,
            },
            lambda kind, payload: 20,
            self.avatar_lookup,
        )
        attempt, replay = short_drama_video.prepare_video_submission(
            self.db, "alice", "alice", {
                "project_id": workspace["project_id"],
                "revision": workspace["revision"],
                "shot_id": shot["id"],
                "video_revision": shot["video_revision"],
                "quote_token": quote["quote_token"],
            },
            key,
            self.avatar_lookup,
        )
        self.assertFalse(replay)
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_video_charge_attempts SET updated_at=1 "
                "WHERE charge_key=?",
                (attempt["charge_key"],),
            )
            conn.commit()
        return short_drama_video.get_video_attempt(self.db, "alice", key)

    def test_quote_submission_binding_and_lock_are_server_authoritative(self):
        workspace = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"]
        )
        shot = next(item for item in workspace["shots"] if item["id"] == self.shot_id)
        quote = short_drama_video.prepare_video_quote(
            self.db, "alice", "alice", {
                "project_id": workspace["project_id"],
                "revision": workspace["revision"],
                "shot_id": shot["id"],
                "video_revision": shot["video_revision"],
                "prompt": self.prompt,
                "enhance_prompt": True,
            },
            lambda kind, payload: 20,
            self.avatar_lookup,
        )
        self.assertEqual(20, quote["total_cost"])
        self.assertTrue(quote["can_submit"])
        body = {
            "project_id": workspace["project_id"],
            "revision": workspace["revision"],
            "shot_id": shot["id"],
            "video_revision": shot["video_revision"],
            "quote_token": quote["quote_token"],
        }
        attempt, replay = short_drama_video.prepare_video_submission(
            self.db, "alice", "alice", body, "video-idem-1", self.avatar_lookup
        )
        self.assertFalse(replay)
        self.assertEqual("accepted", attempt["state"])
        recovered, replay = short_drama_video.prepare_video_submission(
            self.db, "alice", "alice", body, "video-idem-1", self.avatar_lookup
        )
        self.assertTrue(replay)
        self.assertEqual(attempt["charge_key"], recovered["charge_key"])
        claimed = short_drama_video.claim_video_attempt_charge(
            self.db, "alice", "video-idem-1"
        )
        short_drama_video.mark_video_attempt_charged(
            self.db, "alice", "video-idem-1", 80,
            claimed["recovery_token"],
        )
        with closing(self.db()) as conn:
            conn.execute("BEGIN")
            short_drama_video.bind_video_job(
                self.db, "alice", "video-idem-1", conn, 1001
            )
            conn.commit()
        with closing(self.db()) as conn:
            slot = conn.execute(
                "SELECT id FROM short_drama_video_shots WHERE shot_id=?",
                (self.shot_id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO short_drama_video_versions "
                "(id,video_shot_id,version,job_id,url,file,duration_ms,ratio,prompt,"
                "enhance_prompt,input_hash,cost,status,created_at) "
                "VALUES ('video-version-c3',?,1,1001,'/movie','movie.mp4',5000,"
                "'9:16',?,1,?,20,'done',1)",
                (slot["id"], self.prompt, quote["input_hash"]),
            )
            conn.execute(
                "UPDATE short_drama_video_shots SET current_version=1 WHERE id=?",
                (slot["id"],),
            )
            conn.execute(
                "UPDATE short_drama_video_charge_attempts SET state='done' "
                "WHERE idempotency_key='video-idem-1'"
            )
            conn.execute(
                "UPDATE short_drama_video_jobs SET status='done' WHERE job_id=1001"
            )
            conn.commit()
        current = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"]
        )
        current_shot = next(item for item in current["shots"] if item["id"] == self.shot_id)
        locked = short_drama_video.set_video_shot_lock(
            self.db, "alice", {
                "project_id": current["project_id"], "revision": current["revision"],
                "shot_id": current_shot["id"],
                "video_revision": current_shot["video_revision"], "lock": True,
            },
        )
        self.assertTrue(next(
            item for item in locked["shots"] if item["id"] == self.shot_id
        )["locked"])

    def test_unlimited_budget_and_idempotency_conflict(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET point_budget=0 WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        workspace = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"]
        )
        shot = next(item for item in workspace["shots"] if item["id"] == self.shot_id)
        quote = short_drama_video.prepare_video_quote(
            self.db, "alice", "alice", {
                "project_id": workspace["project_id"], "revision": workspace["revision"],
                "shot_id": shot["id"], "video_revision": shot["video_revision"],
                "prompt": self.prompt, "enhance_prompt": False,
            }, lambda kind, payload: 500, self.avatar_lookup,
        )
        self.assertIsNone(quote["budget_left"])
        self.assertTrue(quote["can_submit"])

    def test_quote_compiles_visual_only_prompt_and_disables_provider_audio(self):
        workspace = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"]
        )
        shot = next(
            item for item in workspace["shots"] if item["id"] == self.shot_id
        )
        captured = {}

        def cost_of(kind, payload):
            captured.update(payload)
            return 20

        short_drama_video.prepare_video_quote(
            self.db, "alice", "alice", {
                "project_id": workspace["project_id"],
                "revision": workspace["revision"],
                "shot_id": shot["id"],
                "video_revision": shot["video_revision"],
                "prompt": self.prompt,
                "enhance_prompt": True,
            },
            cost_of,
            self.avatar_lookup,
        )
        self.assertFalse(captured["generate_audio"])
        self.assertFalse(captured["enhance_prompt"])
        self.assertEqual("720p", captured["resolution"])
        self.assertTrue(captured["_short_drama_video"]["visual_only"])
        self.assertEqual(
            self.prompt,
            captured["_short_drama_video"]["user_prompt"],
        )
        self.assertIn("do not generate dialogue", captured["prompt"])
        self.assertEqual(
            64,
            len(captured["_short_drama_video"]["compiled_prompt_hash"]),
        )
        self.assertEqual(
            "scene", captured["_short_drama_video"]["visual_spec"]["scene"]
        )
        self.assertEqual(
            "video", captured["_short_drama_video"]["visual_spec"]["action"]
        )

    def test_edited_prompt_replaces_unsafe_planning_prompt_everywhere(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_shots SET video_prompt=? WHERE id=?",
                ("make the character speak forbidden dialogue", self.shot_id),
            )
            conn.commit()
        workspace = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"]
        )
        shot = next(
            item for item in workspace["shots"] if item["id"] == self.shot_id
        )
        captured = {}
        safe_prompt = "The detective silently opens the warehouse door"

        short_drama_video.prepare_video_quote(
            self.db, "alice", "alice", {
                "project_id": workspace["project_id"],
                "revision": workspace["revision"],
                "shot_id": shot["id"],
                "video_revision": shot["video_revision"],
                "prompt": safe_prompt,
                "enhance_prompt": False,
            },
            lambda kind, payload: captured.update(payload) or 20,
            self.avatar_lookup,
        )

        metadata = captured["_short_drama_video"]
        self.assertEqual(safe_prompt, metadata["user_prompt"])
        self.assertEqual(safe_prompt, metadata["visual_spec"]["action"])
        self.assertNotIn("forbidden dialogue", captured["prompt"])
        self.assertEqual(1, captured["prompt"].count(safe_prompt))

    def test_character_reference_matching_is_explicit_and_boundary_safe(self):
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_characters "
                "(id,project_id,character_key,name,source_type,sort_order) "
                "VALUES ('rain-character',?,'rain_character','小雨',"
                "'ai_character',8)",
                (self.project["id"],),
            )
            conn.execute(
                "INSERT INTO short_drama_characters "
                "(id,project_id,character_key,name,source_type,sort_order) "
                "VALUES ('bob-character',?,'bob_character','Bob',"
                "'ai_character',9)",
                (self.project["id"],),
            )
            project = conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()

            def blocker_codes(prompt):
                dependencies = short_drama_video._shot_dependencies(
                    conn, project, self.shot_id, prompt=prompt,
                    avatar_lookup=self.avatar_lookup,
                )
                return {item["code"] for item in dependencies["blockers"]}

            self.assertNotIn(
                "unknown_character_requested",
                blocker_codes("窗外下着小雨，侦探安静地关上窗户"),
            )
            self.assertIn(
                "unknown_character_requested",
                blocker_codes("@小雨 走进房间"),
            )
            self.assertNotIn(
                "unknown_character_requested",
                blocker_codes("@小雨伞放在门边"),
            )
            self.assertIn(
                "unknown_character_requested",
                blocker_codes("BOB enters the room"),
            )
            self.assertNotIn(
                "unknown_character_requested",
                blocker_codes("A bobcat enters the room"),
            )

    def test_historical_manual_review_report_does_not_block_locking(self):
        cases = (
            (
                "shadow rejection is diagnostic only",
                {
                    "semantic_status": "rejected_visual",
                    "semantic_report": {
                        "mode": "shadow", "decision": "rejected_visual",
                        "blocking": False,
                    },
                },
                None,
            ),
            (
                "enforce rejection blocks even with stale blocking flag",
                {
                    "semantic_status": "accepted",
                    "semantic_report": {
                        "mode": "enforce", "decision": "rejected_visual",
                        "blocking": False,
                    },
                },
                "semantic_visual_rejected",
            ),
            (
                "enforce manual review is diagnostic only",
                {
                    "semantic_status": "manual_review",
                    "semantic_report": {
                        "mode": "enforce", "decision": "manual_review",
                        "blocking": True,
                    },
                },
                None,
            ),
            (
                "status without an authoritative report does not block",
                {
                    "semantic_status": "rejected_visual",
                    "semantic_report": {},
                },
                None,
            ),
        )
        for label, version, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(
                    expected,
                    short_drama_video._semantic_blocker_code(version),
                )

    def test_rejected_visual_version_is_persisted_but_cannot_be_locked(self):
        with closing(self.db()) as conn:
            project = conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()
            dependencies = short_drama_video._shot_dependencies(
                conn, project, self.shot_id, prompt=self.prompt,
                avatar_lookup=self.avatar_lookup,
            )
            slot = conn.execute(
                "SELECT id FROM short_drama_video_shots WHERE shot_id=?",
                (self.shot_id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO short_drama_video_jobs "
                "(id,username,owner_username,project_id,shot_id,job_id,"
                "idempotency_key,request_hash,status,created_at,updated_at) "
                "VALUES ('semantic-job','alice','alice',?,?,2002,"
                "'semantic-idem','request-hash','done',1,1)",
                (self.project["id"], self.shot_id),
            )
            conn.execute(
                "INSERT INTO short_drama_video_versions "
                "(id,video_shot_id,version,job_id,url,file,duration_ms,ratio,"
                "prompt,enhance_prompt,input_hash,cost,semantic_status,"
                "semantic_report_json,status,created_at) "
                "VALUES ('semantic-version',?,1,2002,'/movie','movie.mp4',"
                "5000,'9:16',?,0,?,20,'rejected_visual',?,'done',1)",
                (
                    slot["id"], self.prompt, dependencies["input_hash"],
                    '{"mode":"enforce","decision":"rejected_visual",'
                    '"blocking":true,"codes":["visible_speech_detected"]}',
                ),
            )
            conn.execute(
                "UPDATE short_drama_video_shots SET current_version=1 "
                "WHERE id=?",
                (slot["id"],),
            )
            conn.commit()

        workspace = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"], self.avatar_lookup,
        )
        shot = next(
            item for item in workspace["shots"] if item["id"] == self.shot_id
        )
        self.assertEqual("rejected_visual", shot["versions"][0]["semantic_status"])
        self.assertIn(
            "semantic_visual_rejected",
            {item["code"] for item in shot["lock_blockers"]},
        )
        with self.assertRaises(short_drama_video.VideoBlocked) as error:
            short_drama_video.set_video_shot_lock(
                self.db, "alice", {
                    "project_id": workspace["project_id"],
                    "revision": workspace["revision"],
                    "shot_id": shot["id"],
                    "video_revision": shot["video_revision"],
                    "lock": True,
                },
                self.avatar_lookup,
            )
        self.assertEqual("semantic_visual_rejected", error.exception.code)

    def test_stale_accepted_attempt_with_committed_charge_is_refunded(self):
        attempt = self._accepted_attempt("video-charge-lost")

        class Points:
            def __init__(self):
                self.refunds = []

            def get_points_transaction(self, transaction_key):
                self.transaction_key = transaction_key
                return {
                    "username": "alice",
                    "delta": -20,
                    "before_points": 100,
                    "after_points": 80,
                }

            def refund_points(self, username, amount, reason,
                              transaction_key=""):
                self.refunds.append(
                    (username, amount, reason, transaction_key)
                )
                return 100

        points = Points()
        self.assertEqual(
            1,
            short_drama_video.retry_video_attempt_refunds(
                self.db, points
            ),
        )
        recovered = short_drama_video.get_video_attempt(
            self.db, "alice", "video-charge-lost"
        )
        self.assertEqual(attempt["charge_key"], points.transaction_key)
        self.assertEqual("refunded", recovered["state"])
        self.assertEqual(80, recovered["points_left"])
        self.assertEqual(
            [(
                "alice", 20, "short-drama video:recovery",
                attempt["refund_key"],
            )],
            points.refunds,
        )
        self.assertEqual(
            0,
            short_drama_video.retry_video_attempt_refunds(
                self.db, points
            ),
        )
        self.assertEqual(1, len(points.refunds))

    def test_stale_accepted_attempt_without_charge_can_fail_safely(self):
        self._accepted_attempt("video-never-charged")

        class Points:
            refunds = []

            @staticmethod
            def get_points_transaction(transaction_key):
                return None

            @classmethod
            def refund_points(cls, *args, **kwargs):
                cls.refunds.append((args, kwargs))

        self.assertEqual(
            0,
            short_drama_video.retry_video_attempt_refunds(
                self.db, Points()
            ),
        )
        recovered = short_drama_video.get_video_attempt(
            self.db, "alice", "video-never-charged"
        )
        self.assertEqual("accepted", recovered["state"])
        self.assertEqual(
            "video_charge_ledger_unconfirmed",
            recovered["terminal_response"]["code"],
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_video_charge_attempts SET updated_at=1 "
                "WHERE charge_key=?",
                (recovered["charge_key"],),
            )
            conn.commit()
        self.assertEqual(
            0,
            short_drama_video.retry_video_attempt_refunds(
                self.db, Points()
            ),
        )
        recovered = short_drama_video.get_video_attempt(
            self.db, "alice", "video-never-charged"
        )
        self.assertEqual("failed", recovered["state"])
        self.assertEqual(
            "video_operation_terminal",
            recovered["terminal_response"]["code"],
        )
        self.assertEqual([], Points.refunds)

    def test_unavailable_charge_ledger_keeps_attempt_recoverable(self):
        self._accepted_attempt("video-ledger-down")

        class Points:
            @staticmethod
            def get_points_transaction(transaction_key):
                raise RuntimeError("auth unavailable")

            @staticmethod
            def refund_points(*args, **kwargs):
                raise AssertionError("must not refund without reconciliation")

        self.assertEqual(
            0,
            short_drama_video.retry_video_attempt_refunds(
                self.db, Points()
            ),
        )
        recovered = short_drama_video.get_video_attempt(
            self.db, "alice", "video-ledger-down"
        )
        self.assertEqual("accepted", recovered["state"])
        self.assertIsNone(recovered["recovery_token"])

    def test_mismatched_charge_ledger_is_not_refunded_or_failed(self):
        self._accepted_attempt("video-ledger-mismatch")

        class Points:
            @staticmethod
            def get_points_transaction(transaction_key):
                return {
                    "username": "alice",
                    "delta": -19,
                    "before_points": 100,
                    "after_points": 81,
                }

            @staticmethod
            def refund_points(*args, **kwargs):
                raise AssertionError("must not refund a mismatched ledger")

        short_drama_video.retry_video_attempt_refunds(self.db, Points())
        recovered = short_drama_video.get_video_attempt(
            self.db, "alice", "video-ledger-mismatch"
        )
        self.assertEqual("accepted", recovered["state"])
        self.assertEqual(
            "video_ledger_inconsistent",
            recovered["terminal_response"]["code"],
        )
        self.assertIsNone(recovered["recovery_token"])

    def test_recovery_claim_prevents_late_local_charge_transition(self):
        attempt = self._accepted_attempt("video-recovery-owned")
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_video_charge_attempts "
                "SET recovery_token='recovery-owner',recovery_started_at=2 "
                "WHERE charge_key=?",
                (attempt["charge_key"],),
            )
            conn.commit()

        current = short_drama_video.mark_video_attempt_charged(
            self.db, "alice", "video-recovery-owned", 80,
            "submission-other",
        )

        self.assertEqual("accepted", current["state"])
        self.assertEqual("recovery-owner", current["recovery_token"])

    def test_submission_charge_lease_blocks_recovery_scanner(self):
        attempt = self._accepted_attempt("video-active-charge")
        claimed = short_drama_video.claim_video_attempt_charge(
            self.db, "alice", "video-active-charge"
        )
        self.assertTrue(claimed["recovery_token"].startswith("submission:"))
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_video_charge_attempts SET updated_at=1 "
                "WHERE charge_key=?",
                (attempt["charge_key"],),
            )
            conn.commit()

        class Points:
            @staticmethod
            def get_points_transaction(transaction_key):
                raise AssertionError("active submission lease must not be queried")

            @staticmethod
            def refund_points(*args, **kwargs):
                raise AssertionError("active submission lease must not be refunded")

        self.assertEqual(
            0,
            short_drama_video.retry_video_attempt_refunds(self.db, Points()),
        )
        charged = short_drama_video.mark_video_attempt_charged(
            self.db, "alice", "video-active-charge", 80,
            claimed["recovery_token"],
        )
        self.assertEqual("charged", charged["state"])
        self.assertIsNone(charged["recovery_token"])

    def test_charge_visible_after_first_empty_query_is_refunded(self):
        attempt = self._accepted_attempt("video-ledger-delayed")

        class Points:
            def __init__(self):
                self.lookups = 0
                self.refunds = []

            def get_points_transaction(self, transaction_key):
                self.lookups += 1
                if self.lookups == 1:
                    return None
                return {
                    "username": "alice",
                    "delta": -20,
                    "before_points": 100,
                    "after_points": 80,
                }

            def refund_points(self, username, amount, reason,
                              transaction_key=""):
                self.refunds.append(transaction_key)
                return 100

        points = Points()
        self.assertEqual(
            0,
            short_drama_video.retry_video_attempt_refunds(self.db, points),
        )
        first = short_drama_video.get_video_attempt(
            self.db, "alice", "video-ledger-delayed"
        )
        self.assertEqual("accepted", first["state"])
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_video_charge_attempts SET updated_at=1 "
                "WHERE charge_key=?",
                (attempt["charge_key"],),
            )
            conn.commit()
        self.assertEqual(
            1,
            short_drama_video.retry_video_attempt_refunds(self.db, points),
        )
        recovered = short_drama_video.get_video_attempt(
            self.db, "alice", "video-ledger-delayed"
        )
        self.assertEqual("refunded", recovered["state"])
        self.assertEqual([attempt["refund_key"]], points.refunds)

    def test_ai_character_can_be_bound_in_video_stage(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_characters SET source_type='ai_character',"
                "avatar_id=NULL WHERE project_id=? AND character_key='detective'",
                (self.project["id"],),
            )
            conn.commit()
        before = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"], self.avatar_lookup
        )
        target = next(
            item for item in before["cast_characters"]
            if item["character_key"] == "detective"
        )
        self.assertFalse(target["valid"])
        self.assertEqual("missing_cinematic_avatar", target["blocker"]["code"])

        saved = short_drama_video.save_video_cast(
            self.db, "alice", {
                "project_id": before["project_id"],
                "revision": before["revision"],
                "bindings": [{"character_key": "detective", "avatar_id": 12}],
            },
            self.avatar_lookup,
        )

        target = next(
            item for item in saved["cast_characters"]
            if item["character_key"] == "detective"
        )
        self.assertTrue(target["valid"])
        self.assertEqual("video_cast", target["binding_source"])
        self.assertEqual(12, target["avatar_id"])
        self.assertFalse(any(
            item["code"] == "missing_cinematic_avatar"
            for item in saved["shots"][0]["lock_blockers"]
        ))
        with closing(self.db()) as conn:
            row = conn.execute(
                "SELECT avatar_id FROM short_drama_video_cast "
                "WHERE project_id=? AND character_key='detective'",
                (self.project["id"],),
            ).fetchone()
        self.assertEqual(12, row["avatar_id"])

    def test_native_avatar_remains_compatible_without_override(self):
        workspace = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"], self.avatar_lookup
        )
        target = next(
            item for item in workspace["cast_characters"]
            if item["character_key"] == "detective"
        )
        self.assertTrue(target["valid"])
        self.assertEqual("character", target["binding_source"])
        self.assertEqual(12, target["avatar_id"])
        saved = short_drama_video.save_video_cast(
            self.db, "alice", {
                "project_id": workspace["project_id"],
                "revision": workspace["revision"],
                "bindings": [{"character_key": "detective", "avatar_id": 12}],
            },
            self.avatar_lookup,
        )
        self.assertEqual(workspace["revision"], saved["revision"])
        with closing(self.db()) as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_video_cast WHERE project_id=?",
                (self.project["id"],),
            ).fetchone()[0])

    def test_cast_reference_file_is_exposed_as_browser_url(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_characters SET reference_file=?,reference_url='' "
                "WHERE project_id=? AND character_key='detective'",
                ("avatar_src/alice-detective.jpg", self.project["id"]),
            )
            conn.commit()

        workspace = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"], self.avatar_lookup
        )
        target = next(
            item for item in workspace["cast_characters"]
            if item["character_key"] == "detective"
        )
        self.assertEqual(
            "/api/gen/file/avatar_src/alice-detective.jpg",
            target["reference_url"],
        )
        self.assertEqual(
            "avatar_src/alice-detective.jpg", target["reference_file"]
        )

    def test_cast_change_invalidates_only_affected_unlocked_shots(self):
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_video_cast "
                "(project_id,character_key,avatar_id,created_at,updated_at) "
                "VALUES (?,?,12,1,1)",
                (self.project["id"], "detective"),
            )
            slot = conn.execute(
                "SELECT id,video_revision FROM short_drama_video_shots "
                "WHERE project_id=? AND shot_id=?",
                (self.project["id"], self.shot_id),
            ).fetchone()
            conn.execute(
                "UPDATE short_drama_video_shots SET current_version=3 WHERE id=?",
                (slot["id"],),
            )
            conn.execute(
                "UPDATE short_drama_characters SET source_type='ai_character',"
                "avatar_id=NULL WHERE project_id=? AND character_key='detective'",
                (self.project["id"],),
            )
            conn.commit()

        def lookup(username, avatar_id):
            if username == "alice" and int(avatar_id) in {12, 13}:
                return {
                    "id": int(avatar_id), "username": "alice",
                    "name": "avatar-%s" % avatar_id, "status": "ready",
                    "provider_avatar_id": "provider-%s" % avatar_id,
                }
            raise ValueError("avatar missing")

        before = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"], lookup
        )
        before_hash = next(
            item for item in before["shots"] if item["id"] == self.shot_id
        )["input_hash"]
        changed = short_drama_video.save_video_cast(
            self.db, "alice", {
                "project_id": before["project_id"],
                "revision": before["revision"],
                "bindings": [{"character_key": "detective", "avatar_id": 13}],
            },
            lookup,
        )
        shot = next(item for item in changed["shots"] if item["id"] == self.shot_id)
        self.assertIsNone(shot["current_version"])
        self.assertGreater(shot["video_revision"], slot["video_revision"])
        self.assertNotEqual(before_hash, shot["input_hash"])
        with closing(self.db()) as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_video_versions "
                "WHERE video_shot_id=?",
                (slot["id"],),
            ).fetchone()[0])

    def test_cast_rejects_locked_active_duplicate_and_stale_requests(self):
        workspace = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"], self.avatar_lookup
        )
        body = {
            "project_id": workspace["project_id"],
            "revision": workspace["revision"],
            "bindings": [{"character_key": "detective", "avatar_id": 12}],
        }
        with self.assertRaises(short_drama.RevisionConflict):
            short_drama_video.save_video_cast(
                self.db, "alice", {**body, "revision": body["revision"] - 1},
                self.avatar_lookup,
            )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_characters SET source_type='ai_character',"
                "avatar_id=NULL WHERE project_id=? AND character_key='detective'",
                (self.project["id"],),
            )
            conn.execute(
                "UPDATE short_drama_video_shots SET locked=1 "
                "WHERE project_id=? AND shot_id=?",
                (self.project["id"], self.shot_id),
            )
            conn.commit()
        with self.assertRaises(short_drama_video.VideoCastConflict) as locked:
            short_drama_video.save_video_cast(
                self.db, "alice", body, self.avatar_lookup
            )
        self.assertEqual("locked_video_shot", locked.exception.code)

        with self.assertRaises(ValueError):
            short_drama_video.normalize_video_cast_request({
                "project_id": workspace["project_id"],
                "revision": workspace["revision"],
                "bindings": [
                    {"character_key": "detective", "avatar_id": 12},
                    {"character_key": "suspect", "avatar_id": 12},
                ],
            })

    def test_cast_rejects_unavailable_avatar_and_active_job(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_characters SET source_type='ai_character',"
                "avatar_id=NULL WHERE project_id=? AND character_key='detective'",
                (self.project["id"],),
            )
            conn.commit()
        workspace = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"], self.avatar_lookup
        )
        body = {
            "project_id": workspace["project_id"],
            "revision": workspace["revision"],
            "bindings": [{"character_key": "detective", "avatar_id": 99}],
        }
        with self.assertRaises(short_drama_video.VideoCastConflict) as invalid:
            short_drama_video.save_video_cast(
                self.db, "alice", body, self.avatar_lookup
            )
        self.assertEqual("invalid_cast_avatar", invalid.exception.code)

        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_video_jobs "
                "(id,username,owner_username,project_id,shot_id,job_id,"
                "provider_video_id,idempotency_key,request_hash,status,error,"
                "refunded,created_at,updated_at) "
                "VALUES ('active-cast','alice','alice',?,?,9901,'','cast-job',"
                "'cast-hash','running','',0,1,1)",
                (self.project["id"], self.shot_id),
            )
            conn.commit()
        active_body = {
            **body,
            "bindings": [{"character_key": "detective", "avatar_id": 12}],
        }
        with self.assertRaises(short_drama_video.VideoCastConflict) as active:
            short_drama_video.save_video_cast(
                self.db, "alice", active_body, self.avatar_lookup
            )
        self.assertEqual("active_job", active.exception.code)

    def test_video_cast_route_enforces_auth_and_write_access(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_characters SET source_type='ai_character',"
                "avatar_id=NULL WHERE project_id=? AND character_key='detective'",
                (self.project["id"],),
            )
            conn.commit()
        workspace = short_drama_video.get_video_workspace(
            self.db, "alice", self.project["id"], self.avatar_lookup
        )
        body = {
            "project_id": workspace["project_id"],
            "revision": workspace["revision"],
            "bindings": [{"character_key": "detective", "avatar_id": 12}],
        }
        handler = GetHandler(
            "/api/gen/short-drama/video-cast", body=body
        )
        self.assertTrue(short_drama.dispatch_http(
            handler, "POST", self.db,
            lambda token: {"username": token, "must_change": False},
            avatar_lookup=self.avatar_lookup,
        ))
        self.assertEqual(200, handler.response[0])

        anonymous = GetHandler(
            "/api/gen/short-drama/video-cast", token="", body=body
        )
        short_drama.dispatch_http(
            anonymous, "POST", self.db, lambda _token: None,
            avatar_lookup=self.avatar_lookup,
        )
        self.assertEqual(401, anonymous.response[0])

        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET board_id='board-c3' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        viewer = GetHandler(
            "/api/gen/short-drama/video-cast", token="viewer", body={
                **body, "revision": handler.response[1]["revision"],
            }
        )
        short_drama.dispatch_http(
            viewer, "POST", self.db,
            lambda token: {"username": token, "must_change": False},
            avatar_lookup=self.avatar_lookup,
            canvas_access_resolver=lambda _handler: {
                "board_id": "board-c3", "role": "viewer",
            },
        )
        self.assertEqual(403, viewer.response[0])

    def test_video_cast_avatar_candidates_use_project_owner_for_editor(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET board_id='board-c3' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        listed_for = []

        def avatar_list(username, limit):
            listed_for.append((username, limit))
            return [
                {
                    "id": 12, "username": username, "name": "Owner avatar",
                    "image_file": "owner.png", "image_url": "/owner.png",
                    "status": "ready", "provider_avatar_id": "provider-12",
                    "provider_avatar_group_id": "private-group",
                    "created_at": 1, "updated_at": 2,
                },
                {
                    "id": 13, "username": username, "name": "Unavailable",
                    "status": "pending", "provider_avatar_id": "",
                },
            ]

        editor = GetHandler(
            "/api/gen/short-drama/avatar-candidates?project_id=%s"
            % self.project["id"],
            token="editor",
        )
        self.assertTrue(short_drama.dispatch_http(
            editor, "GET", self.db,
            lambda token: {"username": token, "must_change": False},
            avatar_lookup=self.avatar_lookup,
            avatar_list=avatar_list,
            canvas_access_resolver=lambda _handler: {
                "board_id": "board-c3", "role": "editor",
            },
        ))
        self.assertEqual(200, editor.response[0])
        self.assertEqual([("alice", 120)], listed_for)
        self.assertEqual([12], [item["id"] for item in editor.response[1]["items"]])
        self.assertNotIn("username", editor.response[1]["items"][0])
        self.assertNotIn("provider_avatar_group_id", editor.response[1]["items"][0])
        self.assertNotIn("provider_avatar_id", editor.response[1]["items"][0])
        self.assertFalse(editor.response[1]["can_create_avatar"])

        viewer = GetHandler(
            "/api/gen/short-drama/video-cast/avatars?project_id=%s"
            % self.project["id"],
            token="viewer",
        )
        short_drama.dispatch_http(
            viewer, "GET", self.db,
            lambda token: {"username": token, "must_change": False},
            avatar_lookup=self.avatar_lookup,
            avatar_list=avatar_list,
            canvas_access_resolver=lambda _handler: {
                "board_id": "board-c3", "role": "viewer",
            },
        )
        self.assertEqual(403, viewer.response[0])
        self.assertEqual([("alice", 120)], listed_for)

        outsider = GetHandler(
            "/api/gen/short-drama/video-cast/avatars?project_id=%s"
            % self.project["id"],
            token="mallory",
        )
        short_drama.dispatch_http(
            outsider, "GET", self.db,
            lambda token: {"username": token, "must_change": False},
            avatar_lookup=self.avatar_lookup,
            avatar_list=avatar_list,
            canvas_access_resolver=lambda _handler: {
                "board_id": "another-board", "role": "editor",
            },
        )
        self.assertEqual(404, outsider.response[0])
        self.assertEqual([("alice", 120)], listed_for)

    def test_video_cast_avatar_candidates_allow_project_owner_to_create(self):
        handler = GetHandler(
            "/api/gen/short-drama/video-cast/avatars?project_id=%s"
            % self.project["id"],
            token="alice",
        )
        short_drama.dispatch_http(
            handler, "GET", self.db,
            lambda token: {"username": token, "must_change": False},
            avatar_lookup=self.avatar_lookup,
            avatar_list=lambda username, limit: [{
                "id": 12, "name": "Owner avatar", "status": "ready",
                "provider_avatar_id": "provider-12",
            }],
        )
        self.assertEqual(200, handler.response[0])
        self.assertTrue(handler.response[1]["can_create_avatar"])


if __name__ == "__main__":
    unittest.main()
