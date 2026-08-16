import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama, short_drama_voice


def voice_plan():
    dialogue = [
        {"id": "line-1", "character_key": "detective", "text": "谁在那里？"},
        {"id": "line-2", "character_key": "narrator", "text": "门外没有回答。"},
    ]
    characters = [
        {
            "character_key": "detective", "name": "林探长",
            "identity_text": "detective", "personality": "calm",
            "source_type": "ai_character", "avatar_id": None,
            "appearance_prompt": "coat", "wardrobe_prompt": "dark coat",
            "voice_key": "longwan",
            "voice_settings": {"speed": 1.2, "pitch": 1, "volume": 4},
            "sort_order": 0,
        },
        {
            "character_key": "narrator", "name": "旁白",
            "identity_text": "narrator", "personality": "steady",
            "source_type": "ai_character", "avatar_id": None,
            "appearance_prompt": "voice only", "wardrobe_prompt": "none",
            "voice_key": "longcheng", "voice_settings": {},
            "sort_order": 1,
        },
    ]
    shots = []
    for index in range(6):
        shots.append({
            "shot_key": "shot-%d" % (index + 1), "sort_order": index,
            "duration": 5, "scene_description": "scene",
            "camera_description": "camera",
            "character_keys": ["detective", "narrator"] if index == 0 else [],
            "dialogue_line_ids": ["line-1", "line-2"] if index == 0 else [],
            "image_prompt": "image", "video_prompt": "video",
        })
    return {
        "characters": characters,
        "script": {
            "title": "Night", "logline": "visitor", "hook": "knock",
            "conflict_text": "silence", "turn_text": "empty",
            "ending": "door opens", "dialogue_lines": dialogue,
        },
        "shots": shots,
    }


class GetHandler:
    def __init__(self, path, token="alice", body=None):
        self.path = path
        self.token = token
        self.body = body
        self.response = None

    def _token(self):
        return self.token

    def _send(self, status, payload):
        self.response = (status, payload)

    def _json_body_strict(self):
        return self.body


class ShortDramaVoiceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.path)
        short_drama.init_db(self.db)
        payload = {
            "title": "Night", "synopsis": "A detective hears a midnight knock.",
            "ratio": "9:16", "target_duration": 30, "shot_count": 6,
        }
        project = short_drama.create_project(self.db, "alice", payload)
        self.project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            voice_plan(), planning_cost=0, planning_job_id=501,
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='voice_review' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def test_lazy_snapshot_maps_dialogue_narration_defaults_and_silent_shots(self):
        snapshot = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual("voice_review", snapshot["stage"])
        self.assertEqual(6, len(snapshot["shots"]))
        first = snapshot["shots"][0]
        self.assertEqual(["dialogue", "narration"], [
            line["line_type"] for line in first["lines"]
        ])
        self.assertEqual(["谁在那里？", "门外没有回答。"], [
            line["source_text"] for line in first["lines"]
        ])
        self.assertEqual("longwan", first["lines"][0]["voice_key"])
        self.assertEqual(1.2, first["lines"][0]["speed"])
        self.assertEqual("pending", first["status"])
        self.assertTrue(all(shot["status"] == "silent" for shot in snapshot["shots"][1:]))

    def test_snapshot_is_idempotent_and_does_not_resync_source_changes(self):
        first = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        line_id = first["shots"][0]["lines"][0]["id"]
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_voice_lines SET speech_text='custom' WHERE id=?",
                (line_id,),
            )
            script = conn.execute(
                "SELECT id,dialogue_lines_json FROM short_drama_scripts "
                "WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (self.project["id"],),
            ).fetchone()
            lines = json.loads(script[1])
            lines[0]["text"] = "changed upstream"
            conn.execute(
                "UPDATE short_drama_scripts SET dialogue_lines_json=? WHERE id=?",
                (json.dumps(lines, ensure_ascii=False), script[0]),
            )
            conn.commit()
        second = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual(line_id, second["shots"][0]["lines"][0]["id"])
        self.assertEqual("谁在那里？", second["shots"][0]["lines"][0]["source_text"])
        self.assertEqual("custom", second["shots"][0]["lines"][0]["speech_text"])

    def test_voice_get_route_requires_auth_and_returns_owned_snapshot(self):
        handler = GetHandler(
            "/api/gen/short-drama/voice?project_id=" + self.project["id"]
        )
        handled = short_drama.dispatch_http(
            handler, "GET", self.db,
            lambda token: {"username": token, "must_change": False} if token else None,
        )
        self.assertTrue(handled)
        self.assertEqual(200, handler.response[0])
        self.assertEqual(self.project["id"], handler.response[1]["project_id"])

        anonymous = GetHandler(handler.path, token="")
        short_drama.dispatch_http(anonymous, "GET", self.db, lambda _token: None)
        self.assertEqual(401, anonymous.response[0])

        other = GetHandler(handler.path, token="mallory")
        short_drama.dispatch_http(
            other, "GET", self.db,
            lambda token: {"username": token, "must_change": False},
        )
        self.assertEqual(404, other.response[0])

        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET board_id='board-a' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        viewer = GetHandler(handler.path, token="viewer")
        short_drama.dispatch_http(
            viewer, "GET", self.db,
            lambda token: {"username": token, "must_change": False},
            canvas_access_resolver=lambda _handler: {
                "board_id": "board-a", "role": "viewer",
            },
        )
        self.assertEqual(200, viewer.response[0])

    def _voice_quote(self):
        snapshot = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        line = snapshot["shots"][0]["lines"][0]
        quote = short_drama_voice.prepare_voice_quote(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "revision": snapshot["revision"],
                "items": [{
                    "line_id": line["id"], "voice_key": "longwan",
                    "speed": 1.1, "pitch": 2, "volume": 3,
                }],
            },
            lambda kind, _payload: 10 if kind == "audio" else 0,
            lambda username, voice_key: self.assertEqual(
                ("alice", "longwan"), (username, voice_key)
            ),
        )
        return snapshot, line, quote

    def test_voice_quote_is_free_and_binds_normalized_input(self):
        snapshot, line, quote = self._voice_quote()
        self.assertEqual(10, quote["total_cost"])
        self.assertEqual(line["id"], quote["items"][0]["line_id"])
        self.assertEqual(1.1, quote["items"][0]["input"]["speed"])
        with closing(self.db()) as conn:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM short_drama_voice_quotes"
            ).fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_voice_jobs"
            ).fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_voice_charge_attempts"
            ).fetchone()[0])
        self.assertEqual(snapshot["revision"], quote["revision"])

    def test_voice_quote_route_allows_editor_and_rejects_viewer(self):
        snapshot = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        line = snapshot["shots"][0]["lines"][0]
        body = {
            "project_id": self.project["id"], "revision": snapshot["revision"],
            "items": [{
                "line_id": line["id"], "voice_key": "longwan",
                "speed": 1.0, "pitch": 0, "volume": 0,
            }],
        }
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET board_id='board-a' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        roles = {"editor": "editor", "viewer": "viewer"}

        def access(handler):
            return {"board_id": "board-a", "role": roles[handler.token]}

        editor = GetHandler(
            "/api/gen/short-drama/voice-quote", token="editor", body=body
        )
        short_drama.dispatch_http(
            editor, "POST", self.db,
            lambda token: {"username": token, "must_change": False},
            cost_of=lambda _kind, _payload: 10,
            canvas_access_resolver=access,
            voice_validator=lambda _username, _voice_key: True,
        )
        self.assertEqual(200, editor.response[0])
        viewer = GetHandler(
            "/api/gen/short-drama/voice-quote", token="viewer", body=body
        )
        short_drama.dispatch_http(
            viewer, "POST", self.db,
            lambda token: {"username": token, "must_change": False},
            cost_of=lambda _kind, _payload: 10,
            canvas_access_resolver=access,
        )
        self.assertEqual(403, viewer.response[0])

    def test_submission_is_idempotent_and_rejects_request_rebinding(self):
        snapshot, line, quote = self._voice_quote()
        request = {
            "project_id": self.project["id"],
            "revision": snapshot["revision"],
            "line_id": line["id"], "voice_key": "longwan",
            "speed": 1.1, "pitch": 2, "volume": 3,
            "quote_token": quote["items"][0]["quote_token"],
        }
        first, replay = short_drama_voice.prepare_voice_submission(
            self.db, "alice", "alice", request, "voice-submit-001"
        )
        self.assertFalse(replay)
        self.assertEqual("accepted", first["state"])
        second, replay = short_drama_voice.prepare_voice_submission(
            self.db, "alice", "alice", request, "voice-submit-001"
        )
        self.assertTrue(replay)
        self.assertEqual(first["charge_key"], second["charge_key"])
        changed = dict(request, volume=4)
        with self.assertRaises(short_drama_voice.VoiceQuoteConsumed):
            short_drama_voice.prepare_voice_submission(
                self.db, "alice", "alice", changed, "voice-submit-001"
            )

    def test_expired_quote_is_rejected_before_charge_attempt(self):
        snapshot, line, quote = self._voice_quote()
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_voice_quotes SET expires_at=0 WHERE token=?",
                (quote["items"][0]["quote_token"],),
            )
            conn.commit()
        with self.assertRaisesRegex(ValueError, "已过期"):
            short_drama_voice.prepare_voice_submission(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": snapshot["revision"],
                    "line_id": line["id"], "voice_key": "longwan",
                    "speed": 1.1, "pitch": 2, "volume": 3,
                    "quote_token": quote["items"][0]["quote_token"],
                }, "voice-expired-001",
            )
        with closing(self.db()) as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_voice_charge_attempts"
            ).fetchone()[0])

    def test_direct_refund_pending_is_recovered_once(self):
        snapshot, line, quote = self._voice_quote()
        request = {
            "project_id": self.project["id"],
            "revision": snapshot["revision"],
            "line_id": line["id"], "voice_key": "longwan",
            "speed": 1.1, "pitch": 2, "volume": 3,
            "quote_token": quote["items"][0]["quote_token"],
        }
        short_drama_voice.prepare_voice_submission(
            self.db, "alice", "alice", request, "voice-refund-001"
        )
        short_drama_voice.mark_voice_attempt_charged(
            self.db, "alice", "voice-refund-001", 90
        )
        short_drama_voice.mark_voice_attempt_refund_pending(
            self.db, "alice", "voice-refund-001", {"detail": "insert failed"}
        )
        calls = []

        class Points:
            @staticmethod
            def refund_points(username, cost, reason, transaction_key=""):
                calls.append((username, cost, reason, transaction_key))

        self.assertEqual(1, short_drama_voice.retry_voice_attempt_refunds(
            self.db, Points, 10
        ))
        self.assertEqual(0, short_drama_voice.retry_voice_attempt_refunds(
            self.db, Points, 10
        ))
        self.assertEqual(1, len(calls))
        self.assertEqual("refunded", short_drama_voice.get_voice_attempt(
            self.db, "alice", "voice-refund-001"
        )["state"])

    def test_done_job_creates_version_and_select_is_free_revisioned_write(self):
        snapshot, line, quote = self._voice_quote()
        request = {
            "project_id": self.project["id"],
            "revision": snapshot["revision"],
            "line_id": line["id"], "voice_key": "longwan",
            "speed": 1.1, "pitch": 2, "volume": 3,
            "quote_token": quote["items"][0]["quote_token"],
        }
        short_drama_voice.prepare_voice_submission(
            self.db, "alice", "alice", request, "voice-submit-002"
        )
        short_drama_voice.mark_voice_attempt_charged(
            self.db, "alice", "voice-submit-002", 90
        )
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE jobs(id INTEGER PRIMARY KEY,kind TEXT,username TEXT,"
                "cost INTEGER,status TEXT,payload TEXT,result TEXT,error TEXT,"
                "refunded INTEGER DEFAULT 0)"
            )
            conn.execute(
                "INSERT INTO jobs VALUES "
                "(101,'audio','alice',10,'done','{}',?,'',0)",
                (json.dumps({
                    "file": "audio/one.mp3", "url": "/api/gen/file/audio/one.mp3",
                    "duration_ms": 1234,
                }),),
            )
            short_drama_voice.bind_voice_job(
                self.db, "alice", "voice-submit-002", conn, 101
            )
            conn.commit()
            short_drama_voice.reconcile_voice_jobs(conn, self.project["id"])
            conn.commit()
        current = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        completed = current["shots"][0]["lines"][0]
        self.assertEqual(1, completed["current_version"])
        self.assertEqual("done", completed["versions"][0]["status"])
        self.assertEqual(1234, completed["versions"][0]["duration_ms"])
        selected = short_drama_voice.select_voice_version(
            self.db, "alice", {
                "project_id": self.project["id"],
                "revision": current["revision"],
                "line_id": line["id"], "version": 1,
            },
        )
        self.assertEqual(current["revision"] + 1, selected["revision"])

    def test_failed_job_keeps_failed_version_and_refund_state(self):
        snapshot, line, quote = self._voice_quote()
        request = {
            "project_id": self.project["id"],
            "revision": snapshot["revision"],
            "line_id": line["id"], "voice_key": "longwan",
            "speed": 1.1, "pitch": 2, "volume": 3,
            "quote_token": quote["items"][0]["quote_token"],
        }
        short_drama_voice.prepare_voice_submission(
            self.db, "alice", "alice", request, "voice-submit-003"
        )
        short_drama_voice.mark_voice_attempt_charged(
            self.db, "alice", "voice-submit-003", 90
        )
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE jobs(id INTEGER PRIMARY KEY,kind TEXT,username TEXT,"
                "cost INTEGER,status TEXT,payload TEXT,result TEXT,error TEXT,"
                "refunded INTEGER DEFAULT 0)"
            )
            conn.execute(
                "INSERT INTO jobs VALUES "
                "(102,'audio','alice',10,'error','{}','{}','provider failed',1)"
            )
            short_drama_voice.bind_voice_job(
                self.db, "alice", "voice-submit-003", conn, 102
            )
            conn.commit()
            short_drama_voice.reconcile_voice_jobs(conn, self.project["id"])
            conn.commit()
        failed = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )["shots"][0]["lines"][0]
        self.assertIsNone(failed["current_version"])
        self.assertEqual("failed", failed["job"]["status"])
        self.assertEqual(1, failed["job"]["refunded"])
        self.assertEqual("failed", failed["versions"][0]["status"])
        self.assertEqual("provider failed", failed["versions"][0]["error"])
        self.assertEqual("refunded", short_drama_voice.get_voice_attempt(
            self.db, "alice", "voice-submit-003"
        )["state"])

    def _complete_first_voice_shot(self, durations=(1000, 1200)):
        snapshot = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        lines = snapshot["shots"][0]["lines"]
        with closing(self.db()) as conn:
            for index, (line, duration) in enumerate(zip(lines, durations), 1):
                job_id = 700 + index
                conn.execute(
                    "INSERT INTO short_drama_voice_jobs "
                    "(id,username,project_id,shot_id,voice_line_id,job_id,"
                    "idempotency_key,quoted_cost,status,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?, 'done',1,1)",
                    (
                        "timeline-job-%d" % index, "alice", self.project["id"],
                        snapshot["shots"][0]["id"], line["id"], job_id,
                        "timeline-idem-%d" % index, 0,
                    ),
                )
                conn.execute(
                    "INSERT INTO short_drama_voice_versions "
                    "(id,voice_line_id,version,job_id,audio_file,audio_url,"
                    "duration_ms,speech_text,voice_key,settings_json,input_hash,"
                    "cost,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,'done',1)",
                    (
                        "timeline-version-%d" % index, line["id"], 1, job_id,
                        "audio/%d.mp3" % index, "/api/gen/file/audio/%d.mp3" % index,
                        duration, line["speech_text"], line["voice_key"], "{}",
                        line["input_hash"],
                    ),
                )
                conn.execute(
                    "UPDATE short_drama_voice_lines SET current_version=1 "
                    "WHERE id=?",
                    (line["id"],),
                )
            conn.commit()
        return short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )

    @staticmethod
    def _timeline_body(snapshot, starts=(0, 1150), ends=(1000, 2350)):
        shot = snapshot["shots"][0]
        return {
            "project_id": snapshot["project_id"],
            "revision": snapshot["revision"],
            "shot_id": shot["id"],
            "timeline_revision": shot["timeline_revision"],
            "items": [{
                "line_id": line["id"],
                "subtitle_text": line["subtitle_text"],
                "subtitle_visible": line["subtitle_visible"],
                "start_ms": starts[index],
                "end_ms": ends[index],
            } for index, line in enumerate(shot["lines"])],
        }

    def test_snapshot_exposes_authoritative_timeline_suggestion_and_blockers(self):
        snapshot = self._complete_first_voice_shot()
        shot = snapshot["shots"][0]
        self.assertEqual([
            (0, 1000), (1150, 2350),
        ], [
            (line["suggested_start_ms"], line["suggested_end_ms"])
            for line in shot["lines"]
        ])
        self.assertFalse(shot["lockable"])
        self.assertEqual(
            ["timeline_missing"],
            [item["code"] for item in shot["lock_blockers"]],
        )
        self.assertTrue(snapshot["handoff_blocked"])
        self.assertEqual(6, snapshot["unlocked_shot_count"])

    def test_save_voice_timeline_updates_both_revisions_atomically(self):
        snapshot = self._complete_first_voice_shot()
        saved = short_drama_voice.save_voice_timeline(
            self.db, "alice", self._timeline_body(snapshot),
        )
        shot = saved["shots"][0]
        self.assertEqual(snapshot["revision"] + 1, saved["revision"])
        self.assertEqual(
            snapshot["shots"][0]["timeline_revision"] + 1,
            shot["timeline_revision"],
        )
        self.assertTrue(shot["lockable"])
        self.assertEqual([], shot["lock_blockers"])
        self.assertEqual(
            [(0, 1000), (1150, 2350)],
            [(line["start_ms"], line["end_ms"]) for line in shot["lines"]],
        )

    def test_save_voice_timeline_accepts_non_overlapping_reverse_time_order(self):
        snapshot = self._complete_first_voice_shot()
        saved = short_drama_voice.save_voice_timeline(
            self.db,
            "alice",
            self._timeline_body(
                snapshot,
                starts=(2000, 0),
                ends=(3000, 1200),
            ),
        )
        shot = saved["shots"][0]
        self.assertTrue(shot["lockable"])
        self.assertNotIn(
            "audio_overlap",
            [item["code"] for item in shot["lock_blockers"]],
        )
        self.assertNotIn(
            "subtitle_overlap",
            [item["code"] for item in shot["lock_blockers"]],
        )

    def test_save_voice_timeline_rejects_incomplete_overlap_and_overflow(self):
        snapshot = self._complete_first_voice_shot()
        valid = self._timeline_body(snapshot)
        invalid = {
            "missing": dict(valid, items=valid["items"][:1]),
            "duplicate": dict(valid, items=[valid["items"][0], valid["items"][0]]),
            "audio_overlap": self._timeline_body(
                snapshot, starts=(0, 900), ends=(800, 2100),
            ),
            "reverse_audio_overlap": self._timeline_body(
                snapshot, starts=(1000, 0), ends=(1800, 900),
            ),
            "subtitle_overlap": self._timeline_body(
                snapshot, starts=(0, 1050), ends=(1100, 2250),
            ),
            "reverse_subtitle_overlap": self._timeline_body(
                snapshot, starts=(1500, 0), ends=(2500, 1600),
            ),
            "duration_overflow": self._timeline_body(
                snapshot, starts=(0, 4000), ends=(1000, 4900),
            ),
        }
        for name, payload in invalid.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    short_drama_voice.save_voice_timeline(
                        self.db, "alice", payload,
                    )
        current = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual(snapshot["revision"], current["revision"])
        self.assertTrue(all(
            line["start_ms"] is None for line in current["shots"][0]["lines"]
        ))

    def test_duration_overflow_exposes_timing_details_and_recommended_speed(self):
        snapshot = self._complete_first_voice_shot()
        payload = self._timeline_body(
            snapshot, starts=(0, 4000), ends=(1000, 4900),
        )
        with self.assertRaises(
                short_drama_voice.VoiceTimelineValidationError) as raised:
            short_drama_voice.save_voice_timeline(
                self.db, "alice", payload,
            )
        blocker = raised.exception.blocker
        self.assertEqual("duration_overflow", blocker["code"])
        self.assertEqual(5000, blocker["shot_duration_ms"])
        self.assertEqual(1200, blocker["audio_duration_ms"])
        self.assertEqual(5200, blocker["audio_end_ms"])
        self.assertEqual(200, blocker["audio_overflow_ms"])
        self.assertEqual(0, blocker["subtitle_overflow_ms"])
        self.assertEqual(200, blocker["overflow_ms"])
        self.assertEqual(1.25, blocker["recommended_speed"])

    def test_subtitle_only_overflow_does_not_recommend_voice_speed(self):
        snapshot = self._complete_first_voice_shot()
        payload = self._timeline_body(
            snapshot, starts=(0, 1000), ends=(5200, 2200),
        )
        with self.assertRaises(
                short_drama_voice.VoiceTimelineValidationError) as raised:
            short_drama_voice.save_voice_timeline(
                self.db, "alice", payload,
            )
        blocker = raised.exception.blocker
        self.assertEqual("duration_overflow", blocker["code"])
        self.assertEqual(1000, blocker["audio_end_ms"])
        self.assertEqual(0, blocker["audio_overflow_ms"])
        self.assertEqual(200, blocker["subtitle_overflow_ms"])
        self.assertEqual(200, blocker["overflow_ms"])
        self.assertIsNone(blocker.get("recommended_speed"))

    def test_recommended_voice_speed_stays_within_supported_range(self):
        self.assertIsNone(short_drama_voice._recommended_voice_speed(
            0.5, 1000, 5000,
        ))
        self.assertIsNone(short_drama_voice._recommended_voice_speed(
            1, 5000, 1000,
        ))

    def test_lock_unlock_and_voice_handoff_are_server_authoritative(self):
        snapshot = self._complete_first_voice_shot()
        snapshot = short_drama_voice.save_voice_timeline(
            self.db, "alice", self._timeline_body(snapshot),
        )
        first = snapshot["shots"][0]
        snapshot = short_drama_voice.set_voice_shot_lock(
            self.db, "alice", {
                "project_id": snapshot["project_id"],
                "revision": snapshot["revision"],
                "shot_id": first["id"],
                "timeline_revision": first["timeline_revision"],
                "lock": True,
            },
        )
        for shot in snapshot["shots"][1:]:
            self.assertEqual("silent", shot["status"])
            snapshot = short_drama_voice.set_voice_shot_lock(
                self.db, "alice", {
                    "project_id": snapshot["project_id"],
                    "revision": snapshot["revision"],
                    "shot_id": shot["id"],
                    "timeline_revision": shot["timeline_revision"],
                    "lock": True,
                },
            )
        self.assertFalse(snapshot["handoff_blocked"])
        confirmed = short_drama.confirm_stage(
            self.db, "alice", snapshot["project_id"],
            snapshot["revision"], "voice_review",
        )
        self.assertEqual("video_review", confirmed["stage"])
        self.assertEqual(snapshot["revision"] + 1, confirmed["revision"])
        with self.assertRaises(ValueError):
            short_drama_voice.set_voice_shot_lock(
                self.db, "alice", {
                    "project_id": confirmed["project_id"],
                    "revision": confirmed["revision"],
                    "shot_id": confirmed["shots"][0]["id"],
                    "timeline_revision": confirmed["shots"][0]["timeline_revision"],
                    "lock": False,
                },
            )

    def test_lock_rejects_stale_timeline_revision_and_unsettled_charge(self):
        snapshot = self._complete_first_voice_shot()
        snapshot = short_drama_voice.save_voice_timeline(
            self.db, "alice", self._timeline_body(snapshot),
        )
        shot = snapshot["shots"][0]
        with self.assertRaises(short_drama.RevisionConflict):
            short_drama_voice.set_voice_shot_lock(
                self.db, "alice", {
                    "project_id": snapshot["project_id"],
                    "revision": snapshot["revision"],
                    "shot_id": shot["id"],
                    "timeline_revision": shot["timeline_revision"] - 1,
                    "lock": True,
                },
            )
        line = shot["lines"][0]
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_voice_quotes "
                "(token,username,project_id,voice_line_id,request_hash,cost,"
                "expires_at,created_at) VALUES "
                "('timeline-quote','alice',?,?, 'hash',0,9999999999,1)",
                (snapshot["project_id"], line["id"]),
            )
            conn.execute(
                "INSERT INTO short_drama_voice_charge_attempts "
                "(charge_key,refund_key,username,endpoint,idempotency_key,"
                "request_hash,project_id,shot_id,voice_line_id,quote_token,cost,"
                "audio_payload_json,state,created_at,updated_at) VALUES "
                "('timeline-charge','timeline-refund','alice',?,'timeline-pending',"
                "'hash',?,?,?,'timeline-quote',0,'{}','accepted',1,1)",
                (
                    short_drama_voice.VOICE_ENDPOINT, snapshot["project_id"],
                    shot["id"], line["id"],
                ),
            )
            conn.commit()
        blocked = short_drama_voice.get_voice_workspace(
            self.db, "alice", self.project["id"]
        )
        self.assertIn(
            "charge_attempt_pending",
            [item["code"] for item in blocked["shots"][0]["lock_blockers"]],
        )
        with self.assertRaises(ValueError):
            short_drama_voice.set_voice_shot_lock(
                self.db, "alice", {
                    "project_id": blocked["project_id"],
                    "revision": blocked["revision"],
                    "shot_id": blocked["shots"][0]["id"],
                    "timeline_revision": blocked["shots"][0]["timeline_revision"],
                    "lock": True,
                },
            )

    def test_timeline_routes_allow_editor_and_reject_viewer(self):
        snapshot = self._complete_first_voice_shot()
        body = self._timeline_body(snapshot)
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET board_id='board-c2' WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        roles = {"editor": "editor", "viewer": "viewer"}

        def access(handler):
            return {"board_id": "board-c2", "role": roles[handler.token]}

        viewer = GetHandler(
            "/api/gen/short-drama/save-voice-timeline",
            token="viewer", body=body,
        )
        short_drama.dispatch_http(
            viewer, "POST", self.db,
            lambda token: {"username": token, "must_change": False},
            canvas_access_resolver=access,
        )
        self.assertEqual(403, viewer.response[0])

        editor = GetHandler(
            "/api/gen/short-drama/save-voice-timeline",
            token="editor", body=body,
        )
        short_drama.dispatch_http(
            editor, "POST", self.db,
            lambda token: {"username": token, "must_change": False},
            canvas_access_resolver=access,
        )
        self.assertEqual(200, editor.response[0])
        self.assertEqual(snapshot["revision"] + 1, editor.response[1]["revision"])

    def test_locked_shot_rejects_new_quote_and_version_selection(self):
        snapshot = self._complete_first_voice_shot()
        snapshot = short_drama_voice.save_voice_timeline(
            self.db, "alice", self._timeline_body(snapshot),
        )
        shot = snapshot["shots"][0]
        snapshot = short_drama_voice.set_voice_shot_lock(
            self.db, "alice", {
                "project_id": snapshot["project_id"],
                "revision": snapshot["revision"],
                "shot_id": shot["id"],
                "timeline_revision": shot["timeline_revision"],
                "lock": True,
            },
        )
        line = snapshot["shots"][0]["lines"][0]
        with self.assertRaises(ValueError):
            short_drama_voice.prepare_voice_quote(
                self.db, "alice", "alice", {
                    "project_id": snapshot["project_id"],
                    "revision": snapshot["revision"],
                    "items": [{
                        "line_id": line["id"], "voice_key": line["voice_key"],
                        "speed": line["speed"], "pitch": line["pitch"],
                        "volume": line["volume"],
                    }],
                }, lambda _kind, _payload: 10,
            )
        with self.assertRaises(ValueError):
            short_drama_voice.select_voice_version(
                self.db, "alice", {
                    "project_id": snapshot["project_id"],
                    "revision": snapshot["revision"],
                    "line_id": line["id"], "version": 1,
                },
            )


class ShortDramaVoiceSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _insert_project(self, conn, project_id, username):
        conn.execute(
            "INSERT INTO short_drama_projects "
            "(id,username,title,synopsis,ratio,target_duration,shot_count,"
            "visual_style,target_platform,stage,revision,created_at,updated_at) "
            "VALUES (?,?,?,'long enough','9:16',30,6,'film','douyin',"
            "'voice_review',1,1,1)",
            (project_id, username, project_id),
        )

    def _insert_shot(self, conn, shot_id, project_id):
        conn.execute(
            "INSERT INTO short_drama_shots "
            "(id,project_id,script_version,shot_key,sort_order,duration,"
            "scene_description,camera_description,character_keys_json,"
            "dialogue_line_ids_json,image_prompt,video_prompt) "
            "VALUES (?,?,1,?,0,5,'scene','camera','[]','[]','image','video')",
            (shot_id, project_id, shot_id),
        )

    def _insert_voice_line(self, conn, line_id, project_id, shot_id, sort_order=0,
                           **changes):
        line = {
            "id": line_id,
            "project_id": project_id,
            "shot_id": shot_id,
            "line_type": "dialogue",
            "sort_order": sort_order,
            "source_text": "source",
            "speech_text": "speech",
            "subtitle_text": "subtitle",
            "input_hash": "hash",
            "created_at": 1,
            "updated_at": 1,
        }
        line.update(changes)
        columns = ",".join(line)
        placeholders = ",".join("?" for _ in line)
        conn.execute(
            "INSERT INTO short_drama_voice_lines (%s) VALUES (%s)"
            % (columns, placeholders),
            tuple(line.values()),
        )

    def _insert_quote(self, conn, token, username, project_id, voice_line_id,
                      consumed_job_id=None):
        conn.execute(
            "INSERT INTO short_drama_voice_quotes "
            "(token,username,project_id,voice_line_id,request_hash,cost,expires_at,"
            "consumed_job_id,created_at) VALUES (?,?,?,?, 'hash',0,10,?,1)",
            (token, username, project_id, voice_line_id, consumed_job_id),
        )

    def _insert_job(self, conn, job_id, username, project_id, shot_id, voice_line_id,
                    job_number=100):
        conn.execute(
            "INSERT INTO short_drama_voice_jobs "
            "(id,username,project_id,shot_id,voice_line_id,job_id,idempotency_key,"
            "quoted_cost,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,0,'pending',1,1)",
            (job_id, username, project_id, shot_id, voice_line_id, job_number, job_id),
        )

    def _insert_charge(self, conn, charge_key, username, project_id, shot_id,
                       voice_line_id, quote_token, job_id=None):
        conn.execute(
            "INSERT INTO short_drama_voice_charge_attempts "
            "(charge_key,refund_key,username,endpoint,idempotency_key,request_hash,"
            "project_id,shot_id,voice_line_id,quote_token,cost,audio_payload_json,state,"
            "job_id,created_at,updated_at) "
            "VALUES (?,?,?,'voice',?,'hash',?,?,?,?,0,'{}','accepted',?,1,1)",
            (charge_key, charge_key + "-refund", username, charge_key, project_id,
             shot_id, voice_line_id, quote_token, job_id),
        )

    def _replace_charge(self, conn, charge_key, refund_key, idempotency_key,
                        project_id, shot_id, voice_line_id, quote_token, job_id):
        conn.execute(
            "INSERT OR REPLACE INTO short_drama_voice_charge_attempts "
            "(charge_key,refund_key,username,endpoint,idempotency_key,request_hash,"
            "project_id,shot_id,voice_line_id,quote_token,cost,audio_payload_json,state,"
            "job_id,created_at,updated_at) "
            "VALUES (?,?,'editor','voice',?,'hash',?,?,?,?,0,'{}','accepted',?,1,1)",
            (charge_key, refund_key, idempotency_key, project_id, shot_id,
             voice_line_id, quote_token, job_id),
        )

    def _install_legacy_owner_actor_triggers(self, conn):
        definitions = (
            ("short_drama_voice_jobs_project_guard", "short_drama_voice_jobs", "INSERT"),
            ("short_drama_voice_jobs_project_update_guard", "short_drama_voice_jobs",
             "UPDATE OF username, project_id, shot_id, voice_line_id"),
            ("short_drama_voice_quotes_project_guard", "short_drama_voice_quotes", "INSERT"),
            ("short_drama_voice_quotes_project_update_guard", "short_drama_voice_quotes",
             "UPDATE OF username, project_id, voice_line_id, consumed_job_id"),
            ("short_drama_voice_charge_attempts_project_guard",
             "short_drama_voice_charge_attempts", "INSERT"),
            ("short_drama_voice_charge_attempts_project_update_guard",
             "short_drama_voice_charge_attempts",
             "UPDATE OF username, project_id, shot_id, voice_line_id, quote_token, job_id"),
        )
        for name, table, event in definitions:
            conn.execute("DROP TRIGGER IF EXISTS %s" % name)
            conn.execute(
                "CREATE TRIGGER %s BEFORE %s ON %s FOR EACH ROW "
                "WHEN NOT EXISTS (SELECT 1 FROM short_drama_projects "
                "WHERE id=NEW.project_id AND username=NEW.username) "
                "BEGIN SELECT RAISE(ABORT, 'legacy owner restriction'); END"
                % (name, event, table)
            )

    def _voice_trigger_definitions(self, conn):
        return dict(conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'short_drama_voice_%'"
        ).fetchall())

    def _insert_editor_voice_ledger(self, conn):
        self._insert_project(conn, "p1", "alice")
        self._insert_project(conn, "p2", "bob")
        self._insert_shot(conn, "s1", "p1")
        self._insert_shot(conn, "s2", "p2")
        self._insert_voice_line(
            conn, "line-1", "p1", "s1", character_key="hero",
        )
        self._insert_quote(conn, "quote-editor", "editor", "p1", "line-1")
        self._insert_job(
            conn, "voice-job-editor", "editor", "p1", "s1", "line-1",
            job_number=101,
        )
        self._insert_charge(
            conn, "charge-editor", "editor", "p1", "s1", "line-1",
            "quote-editor", job_id=101,
        )
        conn.execute(
            "UPDATE short_drama_voice_quotes SET consumed_job_id=101 "
            "WHERE token='quote-editor'"
        )

    def _insert_second_voice_line_and_job(self, conn):
        self._insert_voice_line(conn, "line-2", "p1", "s1", sort_order=1)
        self._insert_job(
            conn, "voice-job-second", "editor", "p1", "s1", "line-2",
            job_number=202,
        )

    def test_init_replaces_all_legacy_voice_identity_triggers(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_project(conn, "p1", "alice")
            self._insert_shot(conn, "s1", "p1")
            self._insert_voice_line(conn, "line-1", "p1", "s1")
            self._install_legacy_owner_actor_triggers(conn)
            conn.commit()

        short_drama_voice.init_db(self.db)
        short_drama_voice.init_db(self.db)

        with closing(self.db()) as conn:
            definitions = self._voice_trigger_definitions(conn)
        self.assertIn("short_drama_voice_versions_line_job_guard", definitions)
        self.assertIn("short_drama_voice_versions_line_job_update_guard", definitions)
        self.assertNotIn(
            "project.username=NEW.username",
            "\n".join(definitions.values()).replace(" ", ""),
        )

    def test_referenced_quote_identity_cannot_be_updated(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_editor_voice_ledger(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_quotes SET token='quote-renamed' "
                    "WHERE token='quote-editor'"
                )

    def test_linked_job_identity_cannot_orphan_old_references(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_editor_voice_ledger(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_jobs SET job_id=202 "
                    "WHERE id='voice-job-editor'"
                )

    def test_charge_attempt_job_can_bind_once_but_cannot_rebind(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_project(conn, "p1", "alice")
            self._insert_shot(conn, "s1", "p1")
            self._insert_voice_line(conn, "line-1", "p1", "s1")
            self._insert_quote(conn, "quote-1", "editor", "p1", "line-1")
            self._insert_job(
                conn, "job-101", "editor", "p1", "s1", "line-1",
                job_number=101,
            )
            self._insert_job(
                conn, "job-202", "editor", "p1", "s1", "line-1",
                job_number=202,
            )
            self._insert_charge(
                conn, "charge-1", "editor", "p1", "s1", "line-1",
                "quote-1",
            )

            conn.execute(
                "UPDATE short_drama_voice_charge_attempts SET job_id=101 "
                "WHERE charge_key='charge-1'"
            )
            conn.execute(
                "UPDATE short_drama_voice_quotes SET consumed_job_id=101 "
                "WHERE token='quote-1'"
            )
            conn.execute(
                "UPDATE short_drama_voice_charge_attempts SET job_id=101 "
                "WHERE charge_key='charge-1'"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_charge_attempts SET job_id=NULL "
                    "WHERE charge_key='charge-1'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_charge_attempts SET job_id=202 "
                    "WHERE charge_key='charge-1'"
                )
            self.assertEqual(101, conn.execute(
                "SELECT job_id FROM short_drama_voice_charge_attempts "
                "WHERE charge_key='charge-1'"
            ).fetchone()[0])

    def test_charge_attempt_job_must_match_consumed_quote(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_project(conn, "p1", "alice")
            self._insert_shot(conn, "s1", "p1")
            self._insert_voice_line(conn, "line-1", "p1", "s1")
            self._insert_quote(conn, "quote-1", "editor", "p1", "line-1")
            self._insert_job(
                conn, "job-101", "editor", "p1", "s1", "line-1",
                job_number=101,
            )
            self._insert_job(
                conn, "job-202", "editor", "p1", "s1", "line-1",
                job_number=202,
            )
            conn.execute(
                "UPDATE short_drama_voice_quotes SET consumed_job_id=101 "
                "WHERE token='quote-1'"
            )

            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_charge(
                    conn, "charge-wrong-insert", "editor", "p1", "s1",
                    "line-1", "quote-1", job_id=202,
                )
            self._insert_charge(
                conn, "charge-1", "editor", "p1", "s1", "line-1",
                "quote-1",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_charge_attempts SET job_id=202 "
                    "WHERE charge_key='charge-1'"
                )
            conn.execute(
                "UPDATE short_drama_voice_charge_attempts SET job_id=101 "
                "WHERE charge_key='charge-1'"
            )

    def test_charge_attempt_replace_cannot_change_bound_job(self):
        short_drama.init_db(self.db)
        conflict_values = {
            "primary": lambda charge: (
                charge, charge + "-replacement-refund", charge + "-replacement-idem",
            ),
            "refund": lambda charge: (
                charge + "-replacement", charge + "-refund", charge + "-replacement-idem",
            ),
            "idempotency": lambda charge: (
                charge + "-replacement", charge + "-replacement-refund", charge,
            ),
        }
        with closing(self.db()) as conn:
            for index, (suffix, replacement) in enumerate(conflict_values.items()):
                with self.subTest(conflict=suffix):
                    project_id = "p-" + suffix
                    shot_id = "s-" + suffix
                    line_id = "line-" + suffix
                    quote_token = "quote-" + suffix
                    charge_key = "charge-" + suffix
                    first_job_id = 101 + index * 1000
                    second_job_id = 202 + index * 1000
                    self._insert_project(conn, project_id, "alice")
                    self._insert_shot(conn, shot_id, project_id)
                    self._insert_voice_line(conn, line_id, project_id, shot_id)
                    self._insert_quote(
                        conn, quote_token, "editor", project_id, line_id,
                    )
                    self._insert_job(
                        conn, "job-101-" + suffix, "editor", project_id,
                        shot_id, line_id, job_number=first_job_id,
                    )
                    self._insert_job(
                        conn, "job-202-" + suffix, "editor", project_id,
                        shot_id, line_id, job_number=second_job_id,
                    )
                    self._insert_charge(
                        conn, charge_key, "editor", project_id, shot_id,
                        line_id, quote_token, job_id=first_job_id,
                    )
                    new_charge, new_refund, new_idempotency = replacement(charge_key)
                    with self.assertRaises(sqlite3.IntegrityError):
                        self._replace_charge(
                            conn, new_charge, new_refund, new_idempotency,
                            project_id, shot_id, line_id, quote_token, second_job_id,
                        )
                    self.assertEqual(first_job_id, conn.execute(
                        "SELECT job_id FROM short_drama_voice_charge_attempts "
                        "WHERE charge_key=?",
                        (charge_key,),
                    ).fetchone()[0])

    def test_voice_snapshot_source_identity_is_immutable(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_editor_voice_ledger(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_lines SET character_key='other' "
                    "WHERE id='line-1'"
                )

    def test_voice_version_job_must_belong_to_the_same_line(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_editor_voice_ledger(conn)
            self._insert_second_voice_line_and_job(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO short_drama_voice_versions "
                    "(id,voice_line_id,version,job_id,speech_text,voice_key,"
                    "settings_json,input_hash,cost,status,created_at) "
                    "VALUES ('bad-version','line-1',1,202,'text','voice','{}',"
                    "'hash',0,'done',1)"
                )

    def test_init_creates_all_voice_tables_and_is_idempotent(self):
        short_drama.init_db(self.db)
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue({
            "short_drama_voice_shots",
            "short_drama_voice_lines",
            "short_drama_voice_versions",
            "short_drama_voice_jobs",
            "short_drama_voice_quotes",
            "short_drama_voice_charge_attempts",
        }.issubset(tables))

    def test_voice_shots_reject_cross_project_links_on_insert_and_update(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_project(conn, "p1", "alice")
            self._insert_project(conn, "p2", "bob")
            self._insert_shot(conn, "s1", "p1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO short_drama_voice_shots "
                    "(shot_id,project_id,locked,timeline_revision,created_at,updated_at) "
                    "VALUES ('s1','p2',0,1,1,1)"
                )
            conn.execute(
                "INSERT INTO short_drama_voice_shots "
                "(shot_id,project_id,locked,timeline_revision,created_at,updated_at) "
                "VALUES ('s1','p1',0,1,1,1)"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_shots SET project_id='p2' WHERE shot_id='s1'"
                )

    def test_voice_line_source_text_is_immutable(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_project(conn, "p1", "alice")
            self._insert_shot(conn, "s1", "p1")
            self._insert_voice_line(conn, "line-1", "p1", "s1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_lines SET source_text='changed' "
                    "WHERE id='line-1'"
                )
            self.assertEqual(
                "source",
                conn.execute(
                    "SELECT source_text FROM short_drama_voice_lines WHERE id='line-1'"
                ).fetchone()[0],
            )

    def test_time_columns_reject_fractional_values(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_project(conn, "p1", "alice")
            self._insert_shot(conn, "s1", "p1")
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_voice_line(conn, "line-start", "p1", "s1", start_ms=1.5)
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_voice_line(conn, "line-end", "p1", "s1", end_ms=1.5)
            self._insert_voice_line(conn, "line-1", "p1", "s1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO short_drama_voice_versions "
                    "(id,voice_line_id,version,job_id,duration_ms,speech_text,voice_key,"
                    "settings_json,input_hash,status,created_at) "
                    "VALUES ('version-1','line-1',1,1,1.5,'speech','voice','{}','hash',"
                    "'done',1)"
                )

    def test_jobs_quotes_and_charge_attempts_allow_editor_actor_and_reject_mismatches(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_project(conn, "p1", "alice")
            self._insert_project(conn, "p2", "bob")
            self._insert_shot(conn, "s1", "p1")
            self._insert_shot(conn, "s2", "p2")
            self._insert_voice_line(conn, "line-1", "p1", "s1")
            self._insert_voice_line(conn, "line-2", "p2", "s2")
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_job(conn, "job-cross-project", "alice", "p2", "s1", "line-1")
            self._insert_job(conn, "job-1", "alice", "p1", "s1", "line-1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE short_drama_voice_jobs SET project_id='p2' WHERE id='job-1'")

            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_quote(conn, "quote-cross-project", "alice", "p2", "line-1")
            self._insert_quote(conn, "quote-1", "alice", "p1", "line-1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE short_drama_voice_quotes SET project_id='p2' WHERE token='quote-1'")

            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_charge(
                    conn, "charge-cross-quote", "alice", "p2", "s2", "line-2", "quote-1"
                )
            self._insert_charge(conn, "charge-1", "alice", "p1", "s1", "line-1", "quote-1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_charge_attempts SET voice_line_id='line-2' "
                    "WHERE charge_key='charge-1'"
                )

            self._insert_quote(conn, "quote-editor", "editor", "p1", "line-1")
            self._insert_job(
                conn, "job-editor", "editor", "p1", "s1", "line-1", job_number=101,
            )
            self._insert_charge(
                conn, "charge-editor", "editor", "p1", "s1", "line-1",
                "quote-editor", job_id=101,
            )
            conn.execute(
                "UPDATE short_drama_voice_quotes SET consumed_job_id=101 "
                "WHERE token='quote-editor'"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_charge(
                    conn, "charge-actor-mismatch", "alice", "p1", "s1", "line-1",
                    "quote-editor", job_id=101,
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_jobs SET username='alice' "
                    "WHERE id='job-editor'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_voice_quotes SET project_id='p2' "
                    "WHERE token='quote-editor'"
                )

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE short_drama_voice_lines SET project_id='p2' WHERE id='line-1'")

    def test_init_migrates_legacy_owner_actor_triggers_idempotently(self):
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            self._insert_project(conn, "p1", "alice")
            self._insert_shot(conn, "s1", "p1")
            self._insert_voice_line(conn, "line-1", "p1", "s1")
            self._install_legacy_owner_actor_triggers(conn)
            conn.commit()

        short_drama_voice.init_db(self.db)
        short_drama_voice.init_db(self.db)

        trigger_names = {
            "short_drama_voice_jobs_project_guard",
            "short_drama_voice_jobs_project_update_guard",
            "short_drama_voice_quotes_project_guard",
            "short_drama_voice_quotes_project_update_guard",
            "short_drama_voice_charge_attempts_project_guard",
            "short_drama_voice_charge_attempts_project_update_guard",
        }
        with closing(self.db()) as conn:
            definitions = dict(conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name IN (%s)" % ",".join("?" for _ in trigger_names),
                tuple(sorted(trigger_names)),
            ).fetchall())
            self.assertEqual(trigger_names, set(definitions))
            self.assertTrue(all(
                "legacy owner restriction" not in (sql or "")
                and "project.username=NEW.username" not in (sql or "").replace(" ", "")
                for sql in definitions.values()
            ))
            self._insert_quote(conn, "quote-editor", "editor", "p1", "line-1")
            self._insert_job(
                conn, "job-editor", "editor", "p1", "s1", "line-1", job_number=101,
            )
            self._insert_charge(
                conn, "charge-editor", "editor", "p1", "s1", "line-1",
                "quote-editor", job_id=101,
            )
            conn.execute(
                "UPDATE short_drama_voice_quotes SET consumed_job_id=101 "
                "WHERE token='quote-editor'"
            )
