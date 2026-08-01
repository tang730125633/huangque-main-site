import json
import sqlite3
import sys
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest import mock


SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from content_domains import short_drama
from content_domains import short_drama_timeline as timeline


class RouteHandler:
    def __init__(self, path, token, body=None, idempotency_key=""):
        self.path = path
        self.token = token
        self.body = body
        self.headers = {"Idempotency-Key": idempotency_key}
        self.response = None

    def _token(self):
        return self.token

    def _json_body_strict(self):
        return self.body

    def _send(self, status, payload):
        self.response = (status, payload)


class ShortDramaTimelineTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".tmp-tests"
        root.mkdir(exist_ok=True)
        self.path = str(root / ("timeline-%s.db" % uuid.uuid4().hex))
        self.db = lambda: sqlite3.connect(self.path)
        short_drama.init_db(self.db)
        self.project = short_drama.create_project(self.db, "alice", {
            "title": "主时间轴",
            "synopsis": "验证说话人与时间区间的权威版本",
            "ratio": "16:9",
            "target_duration": 30,
            "shot_count": 6,
        })
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='voice_review' WHERE id=?",
                (self.project["id"],),
            )
            conn.execute(
                "INSERT INTO short_drama_characters "
                "(id,project_id,character_key,name,source_type,sort_order) "
                "VALUES (?,?,?,?,?,?)",
                ("character-1", self.project["id"], "host", "主持人",
                 "ai_character", 0),
            )
            conn.execute(
                "INSERT INTO short_drama_shots "
                "(id,project_id,script_version,shot_key,sort_order,duration,"
                "scene_description,camera_description,character_keys_json,"
                "dialogue_line_ids_json,image_prompt,video_prompt) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "shot-1", self.project["id"], 1, "shot1", 0, 5,
                    "室内", "中景", '["host"]', '["dialogue-1"]', "image", "video",
                ),
            )
            conn.commit()

    def tearDown(self):
        path = Path(self.path)
        if path.exists():
            path.unlink()

    def source(self, conn, project):
        source_hashes = {
            "speaker_hash_version": timeline.SPEAKER_HASH_VERSION,
            "transcript_hash": "t" * 64,
            "master_audio_hash": "m" * 64,
            "alignment_hash": "a" * 64,
            "speaker_hash": "s" * 64,
            "visual_hash": "v" * 64,
        }
        return {
            "project_id": self.project["id"],
            "project_revision": int(project["revision"]),
            "stage": project["stage"],
            "duration_ms": 5000,
            "characters": [{
                "character_key": "host", "name": "主持人",
                "reference_version": 1, "reference_locked": True,
            }],
            "voice": {
                "shots": [{
                    "id": "shot-1", "shot_key": "shot1", "sort_order": 0,
                    "duration": 5, "locked": True,
                    "character_keys": ["host"],
                    "lines": [{
                        "id": "voice-line-1", "dialogue_line_id": "dialogue-1",
                        "line_type": "dialogue", "character_key": "host",
                        "current_version": 1,
                        "versions": [{
                            "id": "voice-version-1", "version": 1,
                            "status": "done",
                        }],
                    }],
                }],
                "handoff_blocked": False,
            },
            "alignment": {
                "alignment_hash": "a" * 64,
                "timeline": [{
                    "shot_id": "shot-1", "line_id": "voice-line-1",
                    "text": "你好", "audio_start_ms": 100,
                    "audio_end_ms": 1000, "subtitle_start_ms": 100,
                    "subtitle_end_ms": 1000,
                }],
            },
            "alignment_ready": True,
            "voice_ready": True,
            "dependencies_ready": True,
            "source_hashes": source_hashes,
            "legacy_speaker_hash": "l" * 64,
            "shot_bounds": [{
                "shot_id": "shot-1", "shot_key": "shot1",
                "start_ms": 0, "end_ms": 5000,
            }],
        }

    def revision(self):
        with closing(self.db()) as conn:
            return conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0]

    def voice_project_for_write(self, conn, owner, project_id, revision):
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM short_drama_projects "
            "WHERE id=? AND username=? AND revision=? AND deleted=0",
            (project_id, owner, revision),
        ).fetchone()

    def confirm_voice_stage(self, revision, source=None):
        patches = [
            mock.patch.object(
                short_drama.short_drama_voice,
                "_voice_project_for_write",
                side_effect=self.voice_project_for_write,
            ),
            mock.patch.object(
                short_drama.short_drama_voice,
                "build_voice_snapshot",
                return_value={
                    "handoff_blocked": False,
                    "handoff_blockers": [],
                },
            ),
        ]
        if source is not None:
            patches.append(mock.patch.object(
                timeline, "_authoritative_source", source
            ))
        with patches[0], patches[1]:
            if len(patches) == 3:
                with patches[2]:
                    return short_drama.confirm_stage(
                        self.db, "alice", self.project["id"],
                        revision, "voice_review",
                    )
            return short_drama.confirm_stage(
                self.db, "alice", self.project["id"],
                revision, "voice_review",
            )

    def create_ready_timeline(self):
        with mock.patch.object(timeline, "_authoritative_source", self.source):
            rebuilt = timeline.rebuild(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": self.revision(), "timeline_revision": 0,
                }, "ready-rebuild",
            )
            return timeline.confirm(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": rebuilt["project_revision"],
                    "timeline_revision": rebuilt["timeline_revision"],
                }, "ready-confirm",
            )

    def downgrade_current_speaker_hash_to_v1(self):
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT version.* FROM short_drama_timeline_current current "
                "JOIN short_drama_timeline_versions version "
                "ON version.id=current.version_id WHERE current.project_id=?",
                (self.project["id"],),
            ).fetchone()
            source_hashes = json.loads(row["source_hashes_json"])
            source_hashes.pop("speaker_hash_version")
            source_hashes["speaker_hash"] = "l" * 64
            conn.execute(
                "UPDATE short_drama_timeline_versions "
                "SET source_hashes_json=?,input_hash=? WHERE id=?",
                (
                    timeline.canonical_json(source_hashes),
                    timeline.downstream_input_hash(
                        self.project["id"], source_hashes, row["timeline_hash"]
                    ),
                    row["id"],
                ),
            )
            conn.commit()
            return row["id"]

    def test_rebuild_edit_confirm_and_idempotent_replay(self):
        with mock.patch.object(timeline, "_authoritative_source", self.source):
            rebuilt = timeline.rebuild(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": self.revision(), "timeline_revision": 0,
                }, "rebuild-1",
            )
            self.assertEqual("draft", rebuilt["status"])
            current = rebuilt["current_version"]
            self.assertEqual([], current["blockers"])
            segment = current["segments"][0]
            self.assertEqual("visible", segment["speaking_mode"])
            self.assertEqual(
                {"type": "character", "value": "host"},
                segment["face_target"],
            )
            saved = timeline.save_changes(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": rebuilt["project_revision"],
                    "timeline_revision": rebuilt["timeline_revision"],
                    "changes": [{
                        "id": segment["id"], "start_ms": segment["start_ms"],
                        "end_ms": segment["end_ms"],
                        "character_key": "host",
                        "speaking_mode": "offscreen",
                        "face_target": None,
                    }],
                }, "save-1",
            )
            self.assertEqual("draft", saved["status"])
            self.assertEqual([], saved["current_version"]["blockers"])
            confirmed = timeline.confirm(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": saved["project_revision"],
                    "timeline_revision": saved["timeline_revision"],
                }, "confirm-1",
            )
            self.assertEqual("ready", confirmed["status"])
            replayed = timeline.confirm(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": saved["project_revision"],
                    "timeline_revision": saved["timeline_revision"],
                }, "confirm-1",
            )
            self.assertTrue(replayed["replayed"])
        with closing(self.db()) as conn:
            self.assertEqual(3, conn.execute(
                "SELECT COUNT(*) FROM short_drama_timeline_versions"
            ).fetchone()[0])
            self.assertEqual(3, conn.execute(
                "SELECT COUNT(*) FROM short_drama_timeline_audit"
            ).fetchone()[0])

    def test_rebuild_defaults_non_visible_speaker_to_offscreen(self):
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            project = conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()
            source = self.source(conn, project)
        source["voice"]["shots"][0]["character_keys"] = []
        segments, _ = timeline._suggested_timeline(source)
        self.assertEqual("offscreen", segments[0]["speaking_mode"])
        self.assertIsNone(segments[0]["face_target"])

    def test_stale_project_or_timeline_revision_is_rejected(self):
        with mock.patch.object(timeline, "_authoritative_source", self.source):
            rebuilt = timeline.rebuild(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": self.revision(), "timeline_revision": 0,
                }, "rebuild-1",
            )
            with self.assertRaises(timeline.TimelineRevisionConflict):
                timeline.rebuild(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "revision": 1, "timeline_revision": 0,
                    }, "rebuild-stale",
                )
            with self.assertRaises(timeline.TimelineError) as caught:
                timeline.rebuild(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "revision": rebuilt["project_revision"],
                        "timeline_revision": rebuilt["timeline_revision"],
                    }, "rebuild-1",
                )
            self.assertEqual("idempotency_conflict", caught.exception.code)

    def test_source_change_marks_current_version_stale(self):
        with mock.patch.object(timeline, "_authoritative_source", self.source):
            timeline.rebuild(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": self.revision(), "timeline_revision": 0,
                }, "rebuild-1",
            )

        def changed_source(conn, project):
            result = self.source(conn, project)
            result["source_hashes"]["speaker_hash"] = "x" * 64
            return result

        with mock.patch.object(
                timeline, "_authoritative_source", changed_source):
            snapshot = timeline.get_snapshot(
                self.db, "alice", self.project["id"]
            )
        self.assertEqual("stale", snapshot["status"])
        self.assertIn(
            "speaker_hash",
            snapshot["current_version"]["stale_impact"]["changed_sources"],
        )

    def test_legacy_speaker_hash_requires_audited_readonly_confirmation(self):
        ready = self.create_ready_timeline()
        legacy_version_id = self.downgrade_current_speaker_hash_to_v1()
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='assembly_review' "
                "WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()

        with mock.patch.object(timeline, "_authoritative_source", self.source):
            first = timeline.get_snapshot(self.db, "alice", self.project["id"])
            second = timeline.get_snapshot(self.db, "alice", self.project["id"])

        self.assertEqual("blocked", first["status"])
        self.assertTrue(first["capabilities"]["confirm_speaker_migration"])
        self.assertTrue(first["capabilities"]["confirm"])
        self.assertEqual(
            ["timeline_speaker_identity_unverified"],
            [
                item["code"]
                for item in first["current_version"]["blockers"]
            ],
        )
        self.assertEqual(
            timeline.SPEAKER_HASH_VERSION,
            first["current_version"]["source_hashes"]["speaker_hash_version"],
        )
        self.assertEqual(
            legacy_version_id, first["current_version"]["parent_id"]
        )
        self.assertEqual(
            ready["project_revision"], first["project_revision"]
        )
        self.assertEqual(
            first["current_version"]["id"], second["current_version"]["id"]
        )
        with closing(self.db()) as conn:
            self.assertEqual(3, conn.execute(
                "SELECT COUNT(*) FROM short_drama_timeline_versions"
            ).fetchone()[0])
            audit = conn.execute(
                "SELECT actor,action,details_json "
                "FROM short_drama_timeline_audit "
                "WHERE action='speaker_hash_v2_migration_pending'"
            ).fetchall()
        self.assertEqual(1, len(audit))
        self.assertEqual("system", audit[0][0])
        self.assertEqual(1, json.loads(audit[0][2])[
            "from_speaker_hash_version"
        ])
        with mock.patch.object(timeline, "_authoritative_source", self.source):
            confirmed = timeline.confirm(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": first["project_revision"],
                    "timeline_revision": first["timeline_revision"],
                }, "confirm-speaker-migration",
            )
            replayed = timeline.confirm(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": first["project_revision"],
                    "timeline_revision": first["timeline_revision"],
                }, "confirm-speaker-migration",
            )
        self.assertEqual("ready", confirmed["status"])
        self.assertTrue(replayed["replayed"])
        self.assertFalse(
            confirmed["capabilities"]["confirm_speaker_migration"]
        )
        self.assertEqual(
            ready["project_revision"] + 1,
            confirmed["project_revision"],
        )
        with closing(self.db()) as conn:
            self.assertEqual(4, conn.execute(
                "SELECT COUNT(*) FROM short_drama_timeline_versions"
            ).fetchone()[0])
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM short_drama_timeline_audit "
                "WHERE action='speaker_hash_v2_migration_confirm'"
            ).fetchone()[0])

    def test_pre_migration_shot_character_change_never_becomes_ready(self):
        self.create_ready_timeline()
        self.downgrade_current_speaker_hash_to_v1()

        def changed_source(conn, project):
            result = self.source(conn, project)
            result["source_hashes"]["speaker_hash"] = "x" * 64
            result["voice"]["shots"][0]["character_keys"] = []
            return result

        with mock.patch.object(
                timeline, "_authoritative_source", changed_source):
            first = timeline.get_snapshot(
                self.db, "alice", self.project["id"]
            )
            second = timeline.get_snapshot(
                self.db, "alice", self.project["id"]
            )
        self.assertEqual("blocked", first["status"])
        self.assertNotEqual("ready", first["status"])
        self.assertEqual(
            "offscreen",
            first["current_version"]["segments"][0]["speaking_mode"],
        )
        self.assertIsNone(
            first["current_version"]["segments"][0]["face_target"]
        )
        self.assertEqual(
            first["current_version"]["id"], second["current_version"]["id"]
        )

    def test_non_equivalent_legacy_hash_is_not_migrated(self):
        self.create_ready_timeline()
        legacy_version_id = self.downgrade_current_speaker_hash_to_v1()

        def changed_source(conn, project):
            result = self.source(conn, project)
            result["source_hashes"]["transcript_hash"] = "x" * 64
            return result

        with mock.patch.object(
                timeline, "_authoritative_source", changed_source):
            snapshot = timeline.get_snapshot(
                self.db, "alice", self.project["id"]
            )
        self.assertEqual("stale", snapshot["status"])
        self.assertEqual(
            legacy_version_id, snapshot["current_version"]["id"]
        )
        with closing(self.db()) as conn:
            self.assertEqual(2, conn.execute(
                "SELECT COUNT(*) FROM short_drama_timeline_versions"
            ).fetchone()[0])

    def test_readonly_confirm_exception_is_limited_to_speaker_migration(self):
        with mock.patch.object(timeline, "_authoritative_source", self.source):
            rebuilt = timeline.rebuild(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": self.revision(),
                    "timeline_revision": 0,
                }, "readonly-rebuild",
            )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='assembly_review' "
                "WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        with mock.patch.object(timeline, "_authoritative_source", self.source):
            with self.assertRaises(timeline.TimelineError) as caught:
                timeline.confirm(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "revision": rebuilt["project_revision"],
                        "timeline_revision": rebuilt["timeline_revision"],
                    }, "readonly-normal-confirm",
                )
        self.assertEqual("timeline_stage_readonly", caught.exception.code)
        with closing(self.db()) as conn:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM short_drama_timeline_versions"
            ).fetchone()[0])

    def test_speaker_migration_confirmation_rejects_new_source_change(self):
        self.create_ready_timeline()
        self.downgrade_current_speaker_hash_to_v1()
        with mock.patch.object(timeline, "_authoritative_source", self.source):
            pending = timeline.get_snapshot(
                self.db, "alice", self.project["id"]
            )

        def changed_source(conn, project):
            result = self.source(conn, project)
            result["source_hashes"]["speaker_hash"] = "x" * 64
            return result

        with mock.patch.object(
                timeline, "_authoritative_source", changed_source):
            with self.assertRaises(timeline.TimelineError) as caught:
                timeline.confirm(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "revision": pending["project_revision"],
                        "timeline_revision": pending["timeline_revision"],
                    }, "changed-migration-confirm",
                )
        self.assertEqual(
            "timeline_version_not_confirmable", caught.exception.code
        )
        with closing(self.db()) as conn:
            self.assertEqual(3, conn.execute(
                "SELECT COUNT(*) FROM short_drama_timeline_versions"
            ).fetchone()[0])

    def test_speaker_hash_v2_tracks_shot_character_keys(self):
        characters = [{"character_key": "host"}]
        cast = [{"character_key": "host", "avatar_id": 1}]
        legacy_a, current_a = timeline._speaker_hashes(
            characters, cast, [{"id": "shot-1", "character_keys": ["host"]}]
        )
        legacy_b, current_b = timeline._speaker_hashes(
            characters, cast, [{"id": "shot-1", "character_keys": []}]
        )
        self.assertEqual(legacy_a, legacy_b)
        self.assertNotEqual(current_a, current_b)

    def test_voice_stage_handoff_preserves_legacy_projects(self):
        self.confirm_voice_stage(self.revision())
        with closing(self.db()) as conn:
            stage = conn.execute(
                "SELECT stage FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0]
        self.assertEqual("video_review", stage)

    def test_voice_stage_http_handoff_rejects_unconfirmed_timeline(self):
        with mock.patch.object(timeline, "_authoritative_source", self.source):
            rebuilt = timeline.rebuild(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": self.revision(), "timeline_revision": 0,
                }, "blocked-rebuild",
            )

        def verify(_token):
            return {"username": "alice", "must_change": False}

        handler = RouteHandler(
            "/api/gen/short-drama/confirm",
            "alice",
            {
                "project_id": self.project["id"],
                "revision": rebuilt["project_revision"],
                "stage": "voice_review",
            },
        )
        with mock.patch.object(
                short_drama.short_drama_voice,
                "_voice_project_for_write",
                side_effect=self.voice_project_for_write,
        ), mock.patch.object(
                short_drama.short_drama_voice,
                "build_voice_snapshot",
                return_value={
                    "handoff_blocked": False,
                    "handoff_blockers": [],
                },
        ), mock.patch.object(
                timeline, "_authoritative_source", self.source,
        ):
            short_drama.dispatch_http(handler, "POST", self.db, verify)

        self.assertEqual(409, handler.response[0])
        self.assertEqual(
            "timeline_handoff_not_ready",
            handler.response[1]["code"],
        )
        self.assertEqual(
            "draft",
            handler.response[1]["blockers"][0]["effective_status"],
        )
        with closing(self.db()) as conn:
            project = conn.execute(
                "SELECT stage,revision FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()
        self.assertEqual(
            ("voice_review", rebuilt["project_revision"]),
            tuple(project),
        )

    def test_voice_stage_handoff_accepts_ready_timeline(self):
        ready = self.create_ready_timeline()
        self.confirm_voice_stage(ready["project_revision"], self.source)
        with closing(self.db()) as conn:
            stage = conn.execute(
                "SELECT stage FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0]
        self.assertEqual("video_review", stage)

    def test_voice_handoff_requires_legacy_hash_confirmation(self):
        ready = self.create_ready_timeline()
        self.downgrade_current_speaker_hash_to_v1()
        with self.assertRaises(timeline.TimelineError) as caught:
            self.confirm_voice_stage(ready["project_revision"], self.source)
        self.assertEqual(
            "timeline_handoff_not_ready", caught.exception.code
        )
        self.assertEqual(
            "blocked", caught.exception.blockers[0]["effective_status"]
        )
        with closing(self.db()) as conn:
            project = conn.execute(
                "SELECT stage,revision FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()
            version_count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_timeline_versions"
            ).fetchone()[0]
        self.assertEqual(
            ("voice_review", ready["project_revision"]),
            tuple(project),
        )
        self.assertEqual(2, version_count)

    def test_voice_stage_handoff_rejects_stale_ready_timeline(self):
        ready = self.create_ready_timeline()

        def changed_source(conn, project):
            result = self.source(conn, project)
            result["source_hashes"]["speaker_hash"] = "x" * 64
            return result

        with self.assertRaises(timeline.TimelineError) as caught:
            self.confirm_voice_stage(
                ready["project_revision"], changed_source
            )
        self.assertEqual(
            "timeline_handoff_not_ready",
            caught.exception.code,
        )
        self.assertEqual(
            "stale",
            caught.exception.blockers[0]["effective_status"],
        )
        with closing(self.db()) as conn:
            project = conn.execute(
                "SELECT stage,revision FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()
        self.assertEqual(
            ("voice_review", ready["project_revision"]),
            tuple(project),
        )

    def test_routes_allow_shared_viewer_read_and_only_editor_write(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET board_id='board-pr-c' "
                "WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        roles = {"viewer": "viewer", "editor": "editor"}

        def verify(token):
            return {"username": token, "must_change": False}

        def access(handler):
            return {
                "board_id": "board-pr-c",
                "role": roles[handler.token],
            }

        read = RouteHandler(
            "/api/gen/short-drama/master-timeline?project_id="
            + self.project["id"],
            "viewer",
        )
        short_drama.dispatch_http(
            read, "GET", self.db, verify, canvas_access_resolver=access
        )
        self.assertEqual(200, read.response[0], read.response)
        self.assertEqual("legacy", read.response[1]["status"])

        body = {
            "project_id": self.project["id"],
            "revision": self.revision(),
            "timeline_revision": 0,
        }
        denied = RouteHandler(
            "/api/gen/short-drama/master-timeline/rebuild",
            "viewer", body, "viewer-key",
        )
        short_drama.dispatch_http(
            denied, "POST", self.db, verify, canvas_access_resolver=access
        )
        self.assertEqual(403, denied.response[0])

        allowed = RouteHandler(
            "/api/gen/short-drama/master-timeline/rebuild",
            "editor", body, "editor-key",
        )
        with mock.patch.object(
                timeline, "rebuild", return_value={"ok": True}) as rebuild:
            short_drama.dispatch_http(
                allowed, "POST", self.db, verify,
                canvas_access_resolver=access,
            )
        self.assertEqual((200, {"ok": True}), allowed.response)
        rebuild.assert_called_once_with(
            self.db, "alice", "editor", body, "editor-key"
        )

    def test_schema_is_idempotent_and_guards_cross_project_shots(self):
        timeline.init_db(self.db)
        timeline.init_db(self.db)
        with closing(self.db()) as conn:
            definitions = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'short_drama_timeline_%'"
                )
            }
        self.assertIn("short_drama_timeline_versions", definitions)
        self.assertIn("short_drama_timeline_segments", definitions)

        with mock.patch.object(timeline, "_authoritative_source", self.source):
            rebuilt = timeline.rebuild(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "revision": self.revision(), "timeline_revision": 0,
                }, "rebuild-guard",
            )
        other = short_drama.create_project(self.db, "alice", {
            "title": "other project",
            "synopsis": "another project used to verify cross-project guards",
            "ratio": "16:9",
            "target_duration": 30,
            "shot_count": 6,
        })
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_shots "
                "(id,project_id,script_version,shot_key,sort_order,duration,"
                "scene_description,camera_description,character_keys_json,"
                "dialogue_line_ids_json,image_prompt,video_prompt) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "other-shot", other["id"], 1, "shot1", 0, 5,
                    "other", "wide", "[]", "[]", "image", "video",
                ),
            )
            version_id = rebuilt["current_version"]["id"]
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO short_drama_timeline_segments "
                    "(id,version_id,project_id,shot_id,line_id,character_key,"
                    "voice_asset_id,start_ms,end_ms,speaking_mode,sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "cross-project", version_id, self.project["id"],
                        "other-shot", "line", "host", "voice", 0, 100,
                        "visible", 0,
                    ),
                )
