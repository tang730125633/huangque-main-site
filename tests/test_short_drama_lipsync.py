import hashlib
import json
import os
import shutil
import sqlite3
import sys
import threading
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest import mock


SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from content_domains import short_drama
from content_domains import short_drama_alignment
from content_domains import short_drama_lipsync
from content_domains import short_drama_lipsync_faces
from content_domains import short_drama_lipsync_quotes
from content_domains import short_drama_lipsync_visuals
from content_domains import short_drama_timeline
from providers.faces import FakeFaceAnalysisProvider


class RouteHandler:
    def __init__(self, path, body=None):
        self.path = path
        self.body = body
        self.headers = {}
        self.response = None

    def _token(self):
        return "token"

    def _json_body_strict(self):
        return self.body

    def _send(self, status, payload):
        self.response = (status, payload)


class ShortDramaLipsyncTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".tmp-tests"
        root.mkdir(exist_ok=True)
        self.path = str(root / ("lipsync-%s.db" % uuid.uuid4().hex))
        self.media_root = root / ("lipsync-media-%s" % uuid.uuid4().hex)
        (self.media_root / "media").mkdir(parents=True)
        (self.media_root / "audio").mkdir(parents=True)
        self.video_bytes = b"locked-video-bytes-v1"
        self.voice_bytes = b"locked-voice-bytes-v1"
        (self.media_root / "media" / "shot-1.mp4").write_bytes(
            self.video_bytes
        )
        (self.media_root / "audio" / "voice-1.wav").write_bytes(
            self.voice_bytes
        )
        (self.media_root / "audio" / "voice-guest.wav").write_bytes(
            b"locked-guest-voice-bytes-v1"
        )
        content_patch = mock.patch.dict(
            os.environ, {"CONTENT_OUT": str(self.media_root)}
        )
        content_patch.start()
        self.addCleanup(content_patch.stop)
        self.db = lambda: sqlite3.connect(self.path)
        short_drama.init_db(self.db)
        self.project = short_drama.create_project(self.db, "alice", {
            "title": "口型快照",
            "synopsis": "验证依赖冻结和模拟报价",
            "ratio": "16:9",
            "target_duration": 30,
            "shot_count": 6,
        })
        self.project_id = self.project["id"]
        self.seed_ready()
        contract_patch = mock.patch.object(
            short_drama_alignment,
            "_current_contract",
            return_value=({}, {"input_hash": "x" * 64}),
        )
        contract_patch.start()
        self.addCleanup(contract_patch.stop)
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            project = conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=?",
                (self.project_id,),
            ).fetchone()
            authoritative = short_drama_timeline._authoritative_source(
                conn, project
            )
            conn.execute(
                "UPDATE short_drama_timeline_versions "
                "SET source_hashes_json=? WHERE id='timeline-1'",
                (json.dumps(authoritative["source_hashes"]),),
            )
            conn.commit()

    def tearDown(self):
        path = Path(self.path)
        if path.exists():
            path.unlink()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def seed_ready(self):
        now = 1000
        with closing(self.db()) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "UPDATE short_drama_projects SET stage='video_review' WHERE id=?",
                (self.project_id,),
            )
            conn.execute(
                "INSERT INTO short_drama_characters "
                "(id,project_id,character_key,name,source_type,"
                "reference_file,reference_url,reference_version,"
                "reference_locked) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "character-host", self.project_id, "host", "Host",
                    "ai_character", "characters/host-v1.png",
                    "/api/gen/file/characters/host-v1.png", 1, 1,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_shots "
                "(id,project_id,script_version,shot_key,sort_order,duration,"
                "scene_description,camera_description,character_keys_json,"
                "dialogue_line_ids_json,image_prompt,video_prompt) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "shot-1", self.project_id, 1, "shot1", 0, 5,
                    "室内", "中景", '["host"]', '["line-1"]', "image", "video",
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_timeline_versions "
                "(id,project_id,version,status,revision,contract_version,"
                "duration_ms,source_hashes_json,timeline_hash,input_hash,"
                "subtitle_cues_json,blockers_json,created_by,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "timeline-1", self.project_id, 1, "ready", 1,
                    "short_drama_master_timeline_v1", 5000,
                    json.dumps({
                        "master_audio_hash": "m" * 64,
                        "transcript_hash": "t" * 64,
                        "alignment_hash": "a" * 64,
                    }),
                    "h" * 64, "i" * 64, "[]", "[]", "alice", now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_voice_lines "
                "(id,project_id,shot_id,dialogue_line_id,line_type,sort_order,"
                "character_key,source_text,speech_text,subtitle_text,voice_key,"
                "current_version,start_ms,end_ms,input_hash,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "voice-line-1", self.project_id, "shot-1", "line-1",
                    "dialogue", 0, "host", "hello", "hello", "hello", "host",
                    1, 500, 2500, "q" * 64, now, now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_voice_jobs "
                "(id,username,project_id,shot_id,voice_line_id,job_id,"
                "idempotency_key,quoted_cost,status,error,refunded,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "voice-job-1", "alice", self.project_id, "shot-1",
                    "voice-line-1", 8001, "voice-idem-1", 0, "done", "", 0,
                    now, now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_voice_versions "
                "(id,voice_line_id,version,job_id,audio_file,audio_url,duration_ms,"
                "speech_text,voice_key,settings_json,input_hash,cost,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "voice-1", "voice-line-1", 1, 8001, "audio/voice-1.wav", "",
                    2000, "hello", "host", "{}", "u" * 64, 0, "done", now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_timeline_segments "
                "(id,version_id,project_id,shot_id,line_id,character_key,"
                "voice_asset_id,start_ms,end_ms,speaking_mode,face_target_json,"
                "sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "segment-1", "timeline-1", self.project_id, "shot-1",
                    "line-1", "host", "voice-1", 500, 2500, "visible",
                    '{"type":"character","value":"host"}', 0,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_timeline_current "
                "(project_id,version_id,revision,updated_at) VALUES (?,?,?,?)",
                (self.project_id, "timeline-1", 1, now),
            )
            conn.execute(
                "INSERT INTO short_drama_alignment_versions "
                "(id,project_id,version,status,revision,provider,model_version,"
                "contract_version,input_hash,master_audio_hash,transcript_hash,"
                "alignment_hash,timeline_json,quality_json,manual_reviewed,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "alignment-parent", self.project_id, 1, "ready", 1,
                    "deterministic-local", "v1", "forced-alignment-v1",
                    "x" * 64, "m" * 64, "t" * 64, "p" * 64, "[]", "{}",
                    0, now, now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_alignment_versions "
                "(id,project_id,version,parent_id,status,revision,provider,"
                "model_version,contract_version,input_hash,master_audio_hash,"
                "transcript_hash,alignment_hash,timeline_json,quality_json,"
                "manual_reviewed,review_action,reviewed_by,reviewed_at,"
                "reviewed_source_revision,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "alignment-1", self.project_id, 2, "alignment-parent",
                    "locked", 2, "deterministic-local", "v1",
                    "forced-alignment-v1", "x" * 64, "m" * 64, "t" * 64,
                    "a" * 64, "[]", "{}", 1, "confirm_unchanged", "alice",
                    now, 1, now, now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_alignment_current "
                "(project_id,version_id,updated_at) VALUES (?,?,?)",
                (self.project_id, "alignment-1", now),
            )
            conn.execute(
                "INSERT INTO short_drama_video_shots "
                "(id,project_id,shot_id,current_version,locked,video_revision,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "video-shot-1", self.project_id, "shot-1", 1, 1, 1,
                    now, now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_video_jobs "
                "(id,username,owner_username,project_id,shot_id,job_id,"
                "provider_video_id,idempotency_key,request_hash,status,error,"
                "refunded,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "video-job-1", "alice", "alice", self.project_id,
                    "shot-1", 9001, "provider-video-1", "video-idem-1",
                    "video-request-1", "done", "", 0, now, now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_video_versions "
                "(id,video_shot_id,version,job_id,url,file,cover_url,cover_file,"
                "duration_ms,ratio,prompt,enhance_prompt,input_hash,cost,provider,"
                "provider_video_id,prompt_template_version,compiled_prompt_hash,"
                "visual_spec_hash,semantic_status,semantic_report_json,status,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "video-version-1", "video-shot-1", 1, 9001,
                    "/api/gen/file/media/shot-1.mp4", "media/shot-1.mp4", "", "",
                    5000, "16:9", "video", 0, "video-input", 0, "test",
                    "provider-video-1", "v1", "compiled", "visual", "accepted",
                    "{}", "done", now,
                ),
            )
            synchronized = short_drama_lipsync_visuals.sync_project(
                conn,
                self.project_id,
                source_inspector=lambda _: {
                    "fingerprint": {
                        "sha256": hashlib.sha256(self.video_bytes).hexdigest(),
                        "size": len(self.video_bytes),
                    },
                    "probe": {
                        "duration_ms": 5000,
                        "video": {
                            "codec": "h264", "width": 1280, "height": 720,
                            "fps": 25.0, "rotation": 0,
                        },
                        "audio": None,
                    },
                },
                now=now,
            )
            self.assertTrue(synchronized["shot-1"])
            conn.commit()

    def snapshot(self, can_write=True):
        return short_drama_lipsync.get_snapshot(
            self.db, "alice", self.project_id, can_write=can_write
        )

    def quote_payload(
        self, snapshot=None, face_target=None, idempotency_key="quote-request-1"
    ):
        snapshot = snapshot or self.snapshot()
        return {
            "project_id": self.project_id,
            "shot_id": "shot-1",
            "expected_revision": snapshot["revision"],
            "expected_input_hash": snapshot["input_hash"],
            "provider": "fal-latentsync",
            "profile": "standard",
            "face_target": face_target or {
                "type": "character", "value": "host"
            },
            "idempotency_key": idempotency_key,
        }

    def test_migration_is_repeatable_and_cross_project_source_is_rejected(self):
        short_drama_lipsync.init_db(self.db)
        short_drama_lipsync.init_db(self.db)
        other = short_drama.create_project(self.db, "alice", {
            "title": "另一个项目", "synopsis": "用于验证跨项目隔离", "ratio": "16:9",
            "target_duration": 30, "shot_count": 6,
        })
        with closing(self.db()) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO short_drama_lipsync_visual_sources "
                    "(id,project_id,shot_id,source_kind,uri,source_hash,"
                    "is_current,locked_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        "bad", other["id"], "shot-1", "video", "bad.mp4",
                        "b" * 64, 1, 1, 1,
                    ),
                )

    def test_snapshot_is_deterministic_and_viewer_is_read_only(self):
        first = self.snapshot()
        second = self.snapshot()
        self.assertTrue(first["can_quote"])
        self.assertEqual(first["blockers"], [])
        self.assertEqual(first["input_hash"], second["input_hash"])
        viewer = self.snapshot(can_write=False)
        self.assertFalse(viewer["can_quote"])
        self.assertIn(
            "project_stage_readonly",
            [item["code"] for item in viewer["blockers"]],
        )

    def test_visual_dependency_is_synchronized_from_locked_video(self):
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            synchronized = short_drama_lipsync_visuals.sync_project(
                conn,
                self.project_id,
                source_inspector=lambda _: {
                    "fingerprint": {"sha256": "v" * 64, "size": 1000},
                    "probe": {
                        "duration_ms": 5000,
                        "video": {
                            "codec": "h264", "width": 1280, "height": 720,
                            "fps": 25.0, "rotation": 0,
                        },
                        "audio": None,
                    },
                },
                now=2000,
            )
            self.assertTrue(synchronized["shot-1"])
            source = conn.execute(
                "SELECT id,is_current,locked_at FROM "
                "short_drama_lipsync_visual_sources WHERE project_id=?",
                (self.project_id,),
            ).fetchone()
            self.assertEqual("video-version:video-version-1", source["id"])
            self.assertEqual((1, 1000), (
                int(source["is_current"]), int(source["locked_at"])
            ))
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_lipsync_media_reports"
                ).fetchone()[0],
            )
            conn.execute(
                "UPDATE short_drama_video_shots SET locked=0 "
                "WHERE project_id=? AND shot_id='shot-1'",
                (self.project_id,),
            )
            short_drama_lipsync_visuals.sync_project(
                conn, self.project_id, now=2001
            )
            self.assertEqual(
                (0, None),
                tuple(conn.execute(
                    "SELECT is_current,locked_at FROM "
                    "short_drama_lipsync_visual_sources WHERE id=?",
                    ("video-version:video-version-1",),
                ).fetchone()),
            )

    def test_effectively_stale_timeline_blocks_quote(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_timeline_versions "
                "SET source_hashes_json='{}' WHERE id='timeline-1'"
            )
            conn.commit()
        result = self.snapshot()
        self.assertFalse(result["can_quote"])
        self.assertIn(
            "timeline_stale", [item["code"] for item in result["blockers"]]
        )

    def test_stale_alignment_input_and_incomplete_review_block_quote(self):
        with mock.patch.object(
            short_drama_alignment,
            "_current_contract",
            return_value=({}, {"input_hash": "changed-input"}),
        ):
            stale = self.snapshot()
        self.assertIn(
            "alignment_stale", [item["code"] for item in stale["blockers"]]
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_alignment_versions SET reviewed_by=NULL "
                "WHERE id='alignment-1'"
            )
            conn.commit()
        incomplete = self.snapshot()
        self.assertIn(
            "alignment_review_incomplete",
            [item["code"] for item in incomplete["blockers"]],
        )

    def test_quotes_are_scoped_to_the_selected_face_target_duration(self):
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_voice_lines "
                "(id,project_id,shot_id,dialogue_line_id,line_type,sort_order,"
                "character_key,source_text,speech_text,subtitle_text,voice_key,"
                "current_version,start_ms,end_ms,input_hash,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "voice-line-guest", self.project_id, "shot-1", "line-guest",
                    "dialogue", 1, "guest", "guest", "guest", "guest", "guest",
                    1, 3000, 4500, "g" * 64, 100, 100,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_voice_jobs "
                "(id,username,project_id,shot_id,voice_line_id,job_id,"
                "idempotency_key,quoted_cost,status,error,refunded,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "voice-job-guest", "alice", self.project_id, "shot-1",
                    "voice-line-guest", 8002, "voice-idem-guest", 0, "done", "", 0,
                    100, 100,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_voice_versions "
                "(id,voice_line_id,version,job_id,audio_file,audio_url,duration_ms,"
                "speech_text,voice_key,settings_json,input_hash,cost,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "voice-guest", "voice-line-guest", 1, 8002,
                    "audio/voice-guest.wav", "", 1500, "guest", "guest", "{}",
                    "w" * 64, 0, "done", 100,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_timeline_segments "
                "(id,version_id,project_id,shot_id,line_id,character_key,"
                "voice_asset_id,start_ms,end_ms,speaking_mode,face_target_json,"
                "sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "segment-guest", "timeline-1", self.project_id, "shot-1",
                    "line-guest", "guest", "voice-guest", 3000, 4500,
                    "visible", '{"type":"character","value":"guest"}', 1,
                ),
            )
            conn.commit()
        snapshot = self.snapshot()
        host = short_drama_lipsync.create_quote(
            self.db, "alice", "alice",
            self.quote_payload(
                snapshot,
                {"type": "character", "value": "host"},
                "quote-host",
            ),
        )
        guest = short_drama_lipsync.create_quote(
            self.db, "alice", "alice",
            self.quote_payload(
                snapshot,
                {"type": "character", "value": "guest"},
                "quote-guest",
            ),
        )
        self.assertEqual(2000, host["duration_ms"])
        self.assertEqual(1500, guest["duration_ms"])
        self.assertNotEqual(host["business_key"], guest["business_key"])

    def test_musetalk_quote_uses_full_visual_duration(self):
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_LIPSYNC_PROVIDERS": "fal-latentsync,musetalk",
            "HQ_SHORT_DRAMA_LIPSYNC_DEFAULT_PROVIDER": "musetalk",
        }):
            snapshot = self.snapshot()
            payload = self.quote_payload(snapshot, idempotency_key="musetalk-duration")
            payload["provider"] = "musetalk"
            quote = short_drama_lipsync.create_quote(
                self.db, "alice", "alice", payload,
            )
        self.assertEqual(5000, quote["duration_ms"])
        self.assertEqual(5000, quote["actual_duration_ms"])
        self.assertEqual(5000, quote["billable_duration_ms"])

    def test_old_project_returns_blockers_instead_of_error(self):
        legacy = short_drama.create_project(self.db, "alice", {
            "title": "历史项目", "synopsis": "这个历史项目没有新依赖", "ratio": "16:9",
            "target_duration": 30, "shot_count": 6,
        })
        result = short_drama_lipsync.get_snapshot(
            self.db, "alice", legacy["id"], can_write=True
        )
        self.assertFalse(result["can_quote"])
        self.assertIn(
            "missing_master_timeline",
            [item["code"] for item in result["blockers"]],
        )

    def test_dependency_change_changes_hash(self):
        before = self.snapshot()["input_hash"]
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_media_reports "
                "SET report_hash=? "
                "WHERE source_id='video-version:video-version-1'",
                ("n" * 64,),
            )
            conn.commit()
        self.assertNotEqual(before, self.snapshot()["input_hash"])

    def test_quote_replays_without_money_provider_or_task_side_effects(self):
        snapshot = self.snapshot()
        first = short_drama_lipsync.create_quote(
            self.db, "alice", "alice", self.quote_payload(snapshot)
        )
        second = short_drama_lipsync.create_quote(
            self.db, "alice", "alice", self.quote_payload(snapshot)
        )
        self.assertFalse(first["chargeable"])
        self.assertEqual(first["quote_mode"], "simulation")
        self.assertEqual(first["cost"]["points"], 0)
        self.assertEqual(first["quote_id"], second["quote_id"])
        self.assertTrue(second["replayed"])
        with closing(self.db()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_lipsync_attempts"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_lipsync_jobs"
                ).fetchone()[0],
                0,
            )

    def test_stale_expected_hash_is_rejected(self):
        old = self.snapshot()
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_media_reports "
                "SET report_hash=? "
                "WHERE source_id='video-version:video-version-1'",
                ("n" * 64,),
            )
            conn.commit()
        with self.assertRaises(short_drama_lipsync_quotes.LipsyncQuoteError) as raised:
            short_drama_lipsync.create_quote(
                self.db, "alice", "alice", self.quote_payload(old)
            )
        self.assertEqual(raised.exception.code, "dependency_changed")
        self.assertEqual(raised.exception.status, 409)

    def test_expired_quote_creates_new_revision_without_mutating_history(self):
        snapshot = self.snapshot()
        payload = self.quote_payload(snapshot)
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            project = conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=?",
                (self.project_id,),
            ).fetchone()
            direct_snapshot = (
                short_drama_lipsync.short_drama_lipsync_snapshot.build_snapshot(
                    conn, project, can_write=True
                )
            )
            first = short_drama_lipsync_quotes.create_quote(
                conn, actor="alice", owner="alice", payload=payload,
                snapshot=direct_snapshot, now=1000,
            )
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            project = conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=?",
                (self.project_id,),
            ).fetchone()
            direct_snapshot = (
                short_drama_lipsync.short_drama_lipsync_snapshot.build_snapshot(
                    conn, project, can_write=True
                )
            )
            second = short_drama_lipsync_quotes.create_quote(
                conn, actor="alice", owner="alice", payload=payload,
                snapshot=direct_snapshot, now=1400,
            )
        self.assertNotEqual(first["quote_id"], second["quote_id"])
        self.assertEqual(first["quote_revision"], 1)
        self.assertEqual(second["quote_revision"], 2)
        with closing(self.db()) as conn:
            rows = conn.execute(
                "SELECT quote_revision,status,input_hash "
                "FROM short_drama_lipsync_quotes ORDER BY quote_revision"
            ).fetchall()
        self.assertEqual(rows, [
            (1, "expired", snapshot["input_hash"]),
            (2, "issued", snapshot["input_hash"]),
        ])

    def test_quote_price_and_identity_are_database_immutable(self):
        snapshot = self.snapshot()
        quote = short_drama_lipsync.create_quote(
            self.db, "alice", "alice", self.quote_payload(snapshot)
        )
        with closing(self.db()) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE short_drama_lipsync_quotes SET cost_json='{}' "
                    "WHERE id=?",
                    (quote["quote_id"],),
                )

    def test_provider_limits_are_enforced(self):
        snapshot = self.snapshot()
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_lipsync_media_reports "
                "SET width=4096 "
                "WHERE source_id='video-version:video-version-1'"
            )
            conn.commit()
        current = self.snapshot()
        payload = self.quote_payload(current)
        with self.assertRaises(short_drama_lipsync_quotes.LipsyncQuoteError) as raised:
            short_drama_lipsync.create_quote(
                self.db, "alice", "alice", payload
            )
        self.assertEqual(raised.exception.code, "provider_unsupported")

    def test_board_viewer_can_read_snapshot_but_cannot_quote(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET board_id='board-1' WHERE id=?",
                (self.project_id,),
            )
            conn.commit()
        access = {"board_id": "board-1", "role": "viewer"}
        reader = RouteHandler(
            "/api/gen/short-drama/lipsync/snapshot?project_id="
            + self.project_id
        )
        matched = short_drama.dispatch_http(
            reader, "GET", self.db, lambda token: {"username": "bob"},
            canvas_access_resolver=lambda handler: access,
        )
        self.assertTrue(matched)
        self.assertEqual(reader.response[0], 200)
        self.assertFalse(reader.response[1]["permissions"]["quote"])
        writer = RouteHandler(
            "/api/gen/short-drama/lipsync/quote",
            self.quote_payload(reader.response[1]),
        )
        short_drama.dispatch_http(
            writer, "POST", self.db, lambda token: {"username": "bob"},
            canvas_access_resolver=lambda handler: access,
        )
        self.assertEqual(writer.response[0], 403)
        self.assertEqual(writer.response[1]["code"], "forbidden")

    def test_pr_g_mutations_are_denied_when_feature_is_disabled(self):
        with mock.patch.dict(
            "os.environ",
            {"HQ_SHORT_DRAMA_LIPSYNC_MUTATIONS_ENABLED": "0"},
        ):
            with self.assertRaises(
                short_drama_lipsync.LipsyncVersionError
            ) as raised:
                short_drama_lipsync.update_speakers(
                    self.db, "alice", "alice", {}, "disabled-speakers"
                )
        self.assertEqual(
            "lipsync_mutations_disabled", raised.exception.code
        )
        self.assertEqual(503, raised.exception.status)

    def test_pr_g_speaker_update_creates_authoritative_timeline_version(self):
        before = self.snapshot()
        current = before["dependencies"]["timeline"]
        source = current["segments"][0]
        payload = {
            "project_id": self.project_id,
            "revision": before["revision"],
            "timeline_revision": current["timeline_revision"],
            "changes": [{
                "id": source["id"],
                "start_ms": source["start_ms"],
                "end_ms": source["end_ms"],
                "character_key": source["character_key"],
                "speaking_mode": "offscreen",
                "face_target": None,
            }],
        }
        with mock.patch.dict(
            "os.environ",
            {"HQ_SHORT_DRAMA_LIPSYNC_MUTATIONS_ENABLED": "1"},
        ):
            result = short_drama_lipsync.update_speakers(
                self.db, "alice", "alice", payload, "speaker-change-1"
            )
        updated = result["current_version"]
        self.assertEqual(
            "offscreen", updated["segments"][0]["speaking_mode"]
        )
        self.assertNotEqual(current["version_id"], updated["id"])
        self.assertEqual("ready", updated["effective_status"])

    def test_pr_g_draft_or_blocked_timeline_requires_full_readiness(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_timeline_segments "
                "SET face_target_json=NULL WHERE id='segment-1'"
            )
            conn.commit()
            before = conn.execute(
                "SELECT version_id,revision FROM short_drama_timeline_current "
                "WHERE project_id=?",
                (self.project_id,),
            ).fetchone()
            version_count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_timeline_versions "
                "WHERE project_id=?",
                (self.project_id,),
            ).fetchone()[0]
        for status in ("draft", "blocked"):
            with self.subTest(status=status):
                with closing(self.db()) as conn:
                    conn.execute(
                        "UPDATE short_drama_timeline_versions "
                        "SET status=?,blockers_json=? WHERE id='timeline-1'",
                        (status, json.dumps([{
                            "code": "timeline_missing_face_target",
                            "scope": "segment",
                            "entity_id": "segment-1",
                        }])),
                    )
                    conn.commit()
                snapshot = self.snapshot()
                current = snapshot["dependencies"]["timeline"]
                source = current["segments"][0]
                payload = {
                    "project_id": self.project_id,
                    "revision": snapshot["revision"],
                    "timeline_revision": current["timeline_revision"],
                    "changes": [{
                        "id": source["id"],
                        "start_ms": source["start_ms"] + 50,
                        "end_ms": source["end_ms"],
                        "character_key": source["character_key"],
                        "speaking_mode": source["speaking_mode"],
                        "face_target": None,
                    }],
                }
                with mock.patch.dict(
                    os.environ,
                    {"HQ_SHORT_DRAMA_LIPSYNC_MUTATIONS_ENABLED": "1"},
                ):
                    with self.assertRaises(
                        short_drama_timeline.TimelineError
                    ) as raised:
                        short_drama_lipsync.update_speakers(
                            self.db, "alice", "alice", payload,
                            status + "-speaker-change",
                        )
                self.assertEqual(
                    "timeline_blocked", raised.exception.code
                )
                self.assertEqual(422, raised.exception.status)
                with closing(self.db()) as conn:
                    after = conn.execute(
                        "SELECT version_id,revision FROM "
                        "short_drama_timeline_current WHERE project_id=?",
                        (self.project_id,),
                    ).fetchone()
                    self.assertEqual(before, after)
                    self.assertEqual(
                        version_count,
                        conn.execute(
                            "SELECT COUNT(*) FROM "
                            "short_drama_timeline_versions WHERE project_id=?",
                            (self.project_id,),
                        ).fetchone()[0],
                    )

    def test_pr_j_face_analysis_is_fail_closed_and_reused(self):
        payload = {"project_id": self.project_id, "shot_id": "shot-1"}
        with self.assertRaises(
            short_drama_lipsync_faces.FaceAnalysisError
        ) as disabled:
            short_drama_lipsync_faces.analyze(
                self.db, "alice", "alice", payload
            )
        self.assertEqual("face_analysis_disabled", disabled.exception.code)
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_FACE_ANALYSIS_ENABLED": "1"}
        ):
            first = short_drama_lipsync_faces.analyze(
                self.db, "alice", "alice", payload
            )
            second = short_drama_lipsync_faces.analyze(
                self.db, "alice", "alice", payload
            )
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            "short-drama-lipsync-face-analysis-v2",
            first["contract_version"],
        )
        self.assertTrue(first["manual_confirmation_required"])
        self.assertFalse(first["can_create_paid_job"])
        self.assertNotIn("embedding", json.dumps(first["result"]).lower())
        with closing(self.db()) as conn:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_lipsync_jobs"
                ).fetchone()[0],
            )

    def test_pr_j_face_analysis_http_requires_editor_and_never_bills(self):
        handler = RouteHandler(
            "/api/gen/short-drama/lipsync/faces/analyze",
            {"project_id": self.project_id, "shot_id": "shot-1"},
        )
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_FACE_ANALYSIS_ENABLED": "1"}
        ):
            matched = short_drama.dispatch_http(
                handler, "POST", self.db,
                lambda token: {"username": "alice"},
            )
        self.assertTrue(matched)
        self.assertEqual(200, handler.response[0])
        self.assertFalse(handler.response[1]["can_create_paid_job"])
        with closing(self.db()) as conn:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_lipsync_attempts"
                ).fetchone()[0],
            )

    def test_pr_j_manual_confirmation_creates_immutable_track_version(self):
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_FACE_ANALYSIS_ENABLED": "1"}
        ):
            analysis = short_drama_lipsync_faces.analyze(
                self.db, "alice", "alice",
                {"project_id": self.project_id, "shot_id": "shot-1"},
            )
            proposal = analysis["result"]["proposals"][0]
            selected = proposal["candidates"][0]
            confirmed = short_drama_lipsync_faces.confirm(
                self.db, "alice", "alice", {
                    "analysis_id": analysis["id"],
                    "expected_input_hash": analysis["input_hash"],
                    "expected_result_hash": analysis["result_hash"],
                    "expected_revision": 0,
                    "review_mode": "manual_confirmed",
                    "review_reason": "逐段试听并核对人物",
                    "mapping": [{
                        "segment_id": proposal["segment_id"],
                        "face_track_id": selected["face_track_id"],
                        "character_key": selected["character_key"],
                    }],
                },
            )
        self.assertTrue(confirmed["locked"])
        self.assertFalse(confirmed["creates_paid_job"])
        self.assertEqual(1, confirmed["revision"])
        current = short_drama_lipsync_faces.get_current(
            self.db, "alice", self.project_id, "shot-1"
        )
        self.assertEqual(confirmed["id"], current["id"])
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_FACE_ANALYSIS_ENABLED": "1"}
        ):
            with self.assertRaises(
                short_drama_lipsync_faces.FaceAnalysisError
            ) as stale:
                short_drama_lipsync_faces.confirm(
                    self.db, "alice", "alice", {
                        "analysis_id": analysis["id"],
                        "expected_input_hash": analysis["input_hash"],
                        "expected_result_hash": analysis["result_hash"],
                        "expected_revision": 0,
                        "review_mode": "manual_confirmed",
                        "review_reason": "重复提交旧版本",
                        "mapping": confirmed["mapping"],
                    },
                )
        self.assertEqual(
            "face_track_revision_changed", stale.exception.code
        )

    def test_pr_j_rejects_overlap_and_biometric_provider_payload(self):
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_timeline_segments "
                "(id,version_id,project_id,shot_id,line_id,character_key,"
                "voice_asset_id,start_ms,end_ms,speaking_mode,face_target_json,"
                "sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "segment-2", "timeline-1", self.project_id, "shot-1",
                    "line-2", "guest", "voice-2", 2000, 3000, "visible",
                    '{"type":"character","value":"guest"}', 1,
                ),
            )
            conn.commit()
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_FACE_ANALYSIS_ENABLED": "1"}
        ):
            with self.assertRaises(
                short_drama_lipsync_faces.FaceAnalysisError
            ) as overlap:
                short_drama_lipsync_faces.analyze(
                    self.db, "alice", "alice",
                    {"project_id": self.project_id, "shot_id": "shot-1"},
                )
        self.assertEqual(
            "overlapping_visible_speech", overlap.exception.code
        )
        with closing(self.db()) as conn:
            conn.execute(
                "DELETE FROM short_drama_timeline_segments "
                "WHERE id='segment-2'"
            )
            conn.commit()
        provider = FakeFaceAnalysisProvider({
            "tracks": [],
            "proposals": [],
            "embedding": [0.1, 0.2],
        })
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_FACE_ANALYSIS_ENABLED": "1"}
        ):
            with self.assertRaises(
                short_drama_lipsync_faces.FaceAnalysisError
            ) as biometric:
                short_drama_lipsync_faces.analyze(
                    self.db, "alice", "alice",
                    {"project_id": self.project_id, "shot_id": "shot-1"},
                    provider=provider,
                )
        self.assertEqual(
            "biometric_payload_rejected", biometric.exception.code
        )

    def test_pr_j_strict_provider_schema_rejects_biometric_and_malformed_results(self):
        class MutatingProvider(FakeFaceAnalysisProvider):
            def __init__(inner, mutate):
                super().__init__()
                inner.mutate = mutate

            def analyze(inner, request):
                result = super().analyze(request)
                inner.mutate(result)
                return result

        cases = {
            "face_embedding": lambda result: result.update({
                "face_embedding": [0.1, 0.2],
            }),
            "nested_embedding_vector": lambda result: result["tracks"][0].update({
                "quality": {"embedding_vector": [0.1, 0.2]},
            }),
            "unknown_field": lambda result: result.update({
                "provider_debug": "must not persist",
            }),
            "empty_tracks": lambda result: result.update({"tracks": []}),
            "empty_matches": lambda result: result.update({"matches": []}),
            "empty_proposals": lambda result: result.update({"proposals": []}),
            "oversized_result": lambda result: result.update({
                "limitations": ["x" * (600 * 1024)],
            }),
        }
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_FACE_ANALYSIS_ENABLED": "1"}
        ):
            for name, mutate in cases.items():
                with self.subTest(name=name):
                    provider = MutatingProvider(mutate)
                    with self.assertRaises(
                        short_drama_lipsync_faces.FaceAnalysisError
                    ) as rejected:
                        short_drama_lipsync_faces.analyze(
                            self.db, "alice", "alice", {
                                "project_id": self.project_id,
                                "shot_id": "shot-1",
                                "params": {"case": name},
                            }, provider=provider,
                        )
                    expected = (
                        "biometric_payload_rejected"
                        if "embedding" in name
                        else "invalid_provider_result"
                    )
                    self.assertEqual(expected, rejected.exception.code)
        with closing(self.db()) as conn:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_face_analyses"
                ).fetchone()[0],
            )

    def test_pr_j_references_are_server_scoped_locked_and_versioned(self):
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_FACE_ANALYSIS_ENABLED": "1"}
        ):
            with self.assertRaises(
                short_drama_lipsync_faces.FaceAnalysisError
            ) as client_asset:
                short_drama_lipsync_faces.analyze(
                    self.db, "alice", "alice", {
                        "project_id": self.project_id,
                        "shot_id": "shot-1",
                        "character_references": [{
                            "character_key": "host",
                            "reference_asset_ids": ["foreign-asset"],
                        }],
                    },
                )
            self.assertEqual(
                "client_reference_assets_forbidden",
                client_asset.exception.code,
            )
            with closing(self.db()) as conn:
                conn.execute(
                    "UPDATE short_drama_characters "
                    "SET reference_locked=0 WHERE project_id=? "
                    "AND character_key='host'",
                    (self.project_id,),
                )
                conn.commit()
            with self.assertRaises(
                short_drama_lipsync_faces.FaceAnalysisError
            ) as unlocked:
                short_drama_lipsync_faces.analyze(
                    self.db, "alice", "alice", {
                        "project_id": self.project_id,
                        "shot_id": "shot-1",
                    },
                )
            self.assertEqual(
                "character_reference_not_locked", unlocked.exception.code
            )
            with closing(self.db()) as conn:
                conn.execute(
                    "UPDATE short_drama_characters "
                    "SET reference_locked=1 WHERE project_id=? "
                    "AND character_key='host'",
                    (self.project_id,),
                )
                conn.execute(
                    "UPDATE short_drama_timeline_segments "
                    "SET character_key='not-a-project-character' "
                    "WHERE id='segment-1'",
                )
                conn.commit()
            with self.assertRaises(
                short_drama_lipsync_faces.FaceAnalysisError
            ) as foreign_character:
                short_drama_lipsync_faces.analyze(
                    self.db, "alice", "alice", {
                        "project_id": self.project_id,
                        "shot_id": "shot-1",
                    },
                )
            self.assertEqual(
                "project_character_not_found",
                foreign_character.exception.code,
            )
        with closing(self.db()) as conn:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_face_analyses"
                ).fetchone()[0],
            )

    def test_pr_j_reference_version_change_prevents_reuse_and_confirmation(self):
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_FACE_ANALYSIS_ENABLED": "1"}
        ):
            first = short_drama_lipsync_faces.analyze(
                self.db, "alice", "alice", {
                    "project_id": self.project_id,
                    "shot_id": "shot-1",
                },
            )
            self.assertEqual(
                {
                    "character_key",
                    "reference_version",
                    "reference_identity_hash",
                },
                set(first["character_references"][0]),
            )
            self.assertNotIn(
                "characters/host-v1.png",
                json.dumps(first["character_references"]),
            )
            with closing(self.db()) as conn:
                conn.execute(
                    "UPDATE short_drama_characters "
                    "SET reference_version=reference_version+1,"
                    "reference_file='characters/host-v2.png' "
                    "WHERE project_id=? AND character_key='host'",
                    (self.project_id,),
                )
                conn.commit()
            second = short_drama_lipsync_faces.analyze(
                self.db, "alice", "alice", {
                    "project_id": self.project_id,
                    "shot_id": "shot-1",
                },
            )
            self.assertFalse(second["reused"])
            self.assertNotEqual(first["input_hash"], second["input_hash"])
            proposal = first["result"]["proposals"][0]
            selected = proposal["candidates"][0]
            with self.assertRaises(
                short_drama_lipsync_faces.FaceAnalysisError
            ) as stale:
                short_drama_lipsync_faces.confirm(
                    self.db, "alice", "alice", {
                        "analysis_id": first["id"],
                        "expected_input_hash": first["input_hash"],
                        "expected_result_hash": first["result_hash"],
                        "expected_revision": 0,
                        "review_mode": "manual_confirmed",
                        "review_reason": "reference changed",
                        "mapping": [{
                            "segment_id": proposal["segment_id"],
                            "face_track_id": selected["face_track_id"],
                            "character_key": selected["character_key"],
                        }],
                    },
                )
            self.assertEqual("stale_face_analysis", stale.exception.code)

    def test_pr_j_confirmation_requires_exact_proposal_candidate_relation(self):
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_characters "
                "(id,project_id,character_key,name,source_type,"
                "reference_file,reference_version,reference_locked) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "character-guest", self.project_id, "guest", "Guest",
                    "ai_character", "characters/guest-v1.png", 1, 1,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_timeline_segments "
                "(id,version_id,project_id,shot_id,line_id,character_key,"
                "voice_asset_id,start_ms,end_ms,speaking_mode,face_target_json,"
                "sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "segment-2", "timeline-1", self.project_id, "shot-1",
                    "line-2", "guest", "voice-2", 2600, 4000, "visible",
                    '{"type":"character","value":"guest"}', 1,
                ),
            )
            conn.commit()

        class RestrictedCandidateProvider(FakeFaceAnalysisProvider):
            def analyze(inner, request):
                result = super().analyze(request)
                result["proposals"][0]["candidates"] = [
                    result["proposals"][0]["candidates"][0]
                ]
                return result

        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_FACE_ANALYSIS_ENABLED": "1"}
        ):
            analysis = short_drama_lipsync_faces.analyze(
                self.db, "alice", "alice", {
                    "project_id": self.project_id,
                    "shot_id": "shot-1",
                }, provider=RestrictedCandidateProvider(),
            )
            first_proposal = analysis["result"]["proposals"][0]
            allowed = first_proposal["candidates"][0]
            other_match = next(
                item for item in analysis["result"]["matches"]
                if (
                    item["track_id"], item["character_key"]
                ) != (
                    allowed["face_track_id"], allowed["character_key"]
                )
            )
            with self.assertRaises(
                short_drama_lipsync_faces.FaceAnalysisError
            ) as invalid:
                short_drama_lipsync_faces.confirm(
                    self.db, "alice", "alice", {
                        "analysis_id": analysis["id"],
                        "expected_input_hash": analysis["input_hash"],
                        "expected_result_hash": analysis["result_hash"],
                        "expected_revision": 0,
                        "review_mode": "manual_adjusted",
                        "review_reason": "invalid cross pair",
                        "mapping": [{
                            "segment_id": first_proposal["segment_id"],
                            "face_track_id": other_match["track_id"],
                            "character_key": other_match["character_key"],
                        }, {
                            "segment_id": analysis["result"]["proposals"][1][
                                "segment_id"
                            ],
                            "face_track_id": analysis["result"]["proposals"][1][
                                "candidates"
                            ][0]["face_track_id"],
                            "character_key": analysis["result"]["proposals"][1][
                                "candidates"
                            ][0]["character_key"],
                        }],
                    },
                )
            self.assertEqual("invalid_face_mapping", invalid.exception.code)

    def test_pr_j_slow_provider_does_not_hold_write_lock_and_stale_result_is_dropped(self):
        entered = threading.Event()
        release = threading.Event()

        class SlowProvider(FakeFaceAnalysisProvider):
            def analyze(inner, request):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test provider wait timed out")
                return super().analyze(request)

        outcome = {}

        def run_analysis():
            try:
                outcome["value"] = short_drama_lipsync_faces.analyze(
                    self.db, "alice", "alice", {
                        "project_id": self.project_id,
                        "shot_id": "shot-1",
                    }, provider=SlowProvider(),
                )
            except Exception as exc:
                outcome["error"] = exc

        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_FACE_ANALYSIS_ENABLED": "1"}
        ):
            worker = threading.Thread(target=run_analysis)
            worker.start()
            self.assertTrue(entered.wait(3))
            with closing(self.db()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE short_drama_characters "
                    "SET reference_version=reference_version+1 "
                    "WHERE project_id=? AND character_key='host'",
                    (self.project_id,),
                )
                conn.commit()
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(
            outcome.get("error"),
            short_drama_lipsync_faces.FaceAnalysisError,
        )
        self.assertEqual(
            "stale_face_analysis_input", outcome["error"].code
        )
        with closing(self.db()) as conn:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_face_analyses"
                ).fetchone()[0],
            )

    def test_pr_j_concurrent_identical_analysis_creates_one_version(self):
        entered = 0
        entered_lock = threading.Lock()
        both_entered = threading.Event()
        release = threading.Event()

        class ConcurrentProvider(FakeFaceAnalysisProvider):
            def analyze(inner, request):
                nonlocal entered
                with entered_lock:
                    entered += 1
                    if entered == 2:
                        both_entered.set()
                if not release.wait(5):
                    raise RuntimeError("concurrent provider wait timed out")
                return super().analyze(request)

        outcomes = []

        def run_analysis():
            try:
                outcomes.append(short_drama_lipsync_faces.analyze(
                    self.db, "alice", "alice", {
                        "project_id": self.project_id,
                        "shot_id": "shot-1",
                    }, provider=ConcurrentProvider(),
                ))
            except Exception as exc:
                outcomes.append(exc)

        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_FACE_ANALYSIS_ENABLED": "1"}
        ):
            workers = [
                threading.Thread(target=run_analysis)
                for _ in range(2)
            ]
            for worker in workers:
                worker.start()
            self.assertTrue(both_entered.wait(3))
            release.set()
            for worker in workers:
                worker.join(5)
                self.assertFalse(worker.is_alive())
        self.assertEqual(2, len(outcomes))
        self.assertTrue(all(isinstance(item, dict) for item in outcomes))
        self.assertEqual(1, len({item["id"] for item in outcomes}))
        self.assertEqual([False, True], sorted(
            item["reused"] for item in outcomes
        ))
        with closing(self.db()) as conn:
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_face_analyses"
                ).fetchone()[0],
            )


if __name__ == "__main__":
    unittest.main()
