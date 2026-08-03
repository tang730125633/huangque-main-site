import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


import sys

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import (
    short_drama,
    short_drama_assembly,
    short_drama_completion,
    short_drama_video,
    short_drama_voice,
)


def _project_payload():
    return {
        "title": "D-0 合成测试",
        "synopsis": "一段用于验证短剧合成契约的完整故事梗概。",
        "ratio": "9:16",
        "target_duration": 30,
        "shot_count": 6,
        "visual_style": "电影写实",
        "point_budget": 100,
    }


def _plan():
    shots = []
    for index in range(6):
        shots.append({
            "key": f"shot-{index + 1}",
            "duration": 5,
            "scene_description": f"场景 {index + 1}",
            "camera_description": "固定镜头",
            "character_keys": [],
            "dialogue_line_ids": [],
            "image_prompt": "电影感静帧",
            "video_prompt": "自然运动",
        })
    return {
        "characters": [],
        "script": {
            "title": "D-0 测试剧本",
            "dialogue_lines": [],
        },
        "shots": shots,
    }


class _Handler:
    def __init__(self, path, token="token"):
        self.path = path
        self._auth_token = token
        self.status = None
        self.payload = None

    def _token(self):
        return self._auth_token

    def _send(self, status, payload):
        self.status = status
        self.payload = payload


class ShortDramaAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.render_environment = mock.patch.object(
            short_drama_assembly,
            "_require_render_environment",
            return_value={
                "family": "Noto Sans CJK SC",
                "file": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            },
        )
        self.render_environment.start()
        self.addCleanup(self.render_environment.stop)
        self.tempdir = tempfile.TemporaryDirectory(
            prefix=".tmp-short-drama-assembly-", dir=ROOT
        )
        self.db_path = Path(self.tempdir.name) / "content.db"

        def db_factory():
            return sqlite3.connect(self.db_path, timeout=5)

        self.db = db_factory
        short_drama.init_db(self.db)
        project = short_drama.create_project(
            self.db, "alice", _project_payload()
        )
        self.project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            _plan(), planning_cost=0, planning_job_id=7001,
        )
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE short_drama_projects "
                "SET stage='assembly_review',revision=revision+1 "
                "WHERE id=?",
                (self.project["id"],),
            )
            short_drama_voice.ensure_voice_workspace(
                conn, self.project["id"],
                allowed_stages={"assembly_review"},
            )
            conn.commit()

    def tearDown(self):
        self.tempdir.cleanup()

    def _lock_silent_voice_and_video_sources(self):
        now = int(time.time())
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE short_drama_voice_shots SET locked=1 "
                "WHERE project_id=?",
                (self.project["id"],),
            )
            short_drama_video.ensure_video_workspace(
                conn, self.project["id"],
                allowed_stages={"assembly_review"},
            )
            slots = list(conn.execute(
                "SELECT id,shot_id FROM short_drama_video_shots "
                "WHERE project_id=? ORDER BY shot_id",
                (self.project["id"],),
            ))
            for index, slot in enumerate(slots, 1):
                job_id = 8100 + index
                conn.execute(
                    "INSERT INTO short_drama_video_jobs "
                    "(id,username,owner_username,project_id,shot_id,job_id,"
                    "idempotency_key,request_hash,status,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"video-job-{index}", "alice", "alice",
                        self.project["id"], slot["shot_id"], job_id,
                        f"idem-{index}", f"request-{index}", "done", now, now,
                    ),
                )
                conn.execute(
                    "INSERT INTO short_drama_video_versions "
                    "(id,video_shot_id,version,job_id,file,duration_ms,ratio,"
                    "prompt,input_hash,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"video-version-{index}", slot["id"], 1, job_id,
                        f"video/shot-{index}.mp4", 5000, "9:16",
                        "电影化视频", f"video-input-{index}", "done", now,
                    ),
                )
                conn.execute(
                    "UPDATE short_drama_video_shots "
                    "SET current_version=1,locked=1,updated_at=? WHERE id=?",
                    (now, slot["id"]),
                )
            conn.commit()

    @staticmethod
    def _video_inspection(file_key):
        return {
            "probe": {
                "duration_ms": 5000,
                "video": {
                    "codec": "h264", "width": 1080, "height": 1920,
                    "fps": 24.0, "pix_fmt": "yuv420p", "sar": "1:1",
                    "rotation": 0,
                },
                "audio": None,
            },
            "fingerprint": {
                "sha256": ("a" * 63) + file_key[-5],
                "size": 1024,
            },
        }

    def test_schema_is_created_with_future_safe_constraints(self):
        with closing(self.db()) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'short_drama_composition%'"
                )
            }
            self.assertEqual({
                "short_drama_compositions",
                "short_drama_composition_versions",
                "short_drama_composition_jobs",
            }, tables)
            attempt_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(short_drama_final_attempts)"
                )
            }
            self.assertTrue({
                "recovery_token", "recovery_started_at",
            }.issubset(attempt_columns))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO short_drama_composition_jobs "
                    "(id,username,project_id,job_id,kind,idempotency_key,"
                    "request_hash,status,progress,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "job-row", "alice", self.project["id"], "job-1",
                        "preview", "idem-1", "hash-1", "queued", 101, 1, 1,
                    ),
                )

    def test_snapshot_is_read_only_and_exposes_c3_blockers(self):
        before = None
        with closing(self.db()) as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM short_drama_compositions"
            ).fetchone()[0]
        snapshot = short_drama_assembly.get_assembly_workspace(
            self.db, "alice", self.project["id"]
        )
        with closing(self.db()) as conn:
            after = conn.execute(
                "SELECT COUNT(*) FROM short_drama_compositions"
            ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(
            "formal_export", snapshot["implementation_status"]
        )
        self.assertTrue(snapshot["rendering_enabled"])
        self.assertEqual(6, len(snapshot["shots"]))
        self.assertEqual(
            {"missing_locked_voice_shot", "missing_locked_video_shot"},
            {item["code"] for item in snapshot["readiness"]["blockers"]},
        )
        self.assertTrue(all(
            shot["video"]["status"] == "blocked"
            for shot in snapshot["shots"]
        ))
        self.assertIsNone(snapshot["media_plan"])
        self.assertIsNone(snapshot["input_hash"])
        self.assertEqual("blocked", snapshot["audio_subtitle"]["status"])
        self.assertEqual(
            {"missing_d1_media_plan"},
            {
                item["code"]
                for item in snapshot["audio_subtitle"]["blockers"]
            },
        )
        self.assertEqual({
            "can_save_config": True,
            "can_preview": False,
            "can_lock_preview": False,
            "can_export": False,
            "can_confirm": False,
        }, snapshot["actions"])

    def test_locked_voice_removes_only_voice_readiness_blocker(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_voice_shots SET locked=1 "
                "WHERE project_id=?",
                (self.project["id"],),
            )
            conn.commit()
        snapshot = short_drama_assembly.get_assembly_workspace(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual(
            {"missing_locked_video_shot"},
            {item["code"] for item in snapshot["readiness"]["blockers"]},
        )
        self.assertTrue(all(shot["voice"]["locked"] for shot in snapshot["shots"]))
        self.assertFalse(snapshot["readiness"]["ready"])

    def test_locked_c2_c3_sources_produce_deterministic_media_plan(self):
        self._lock_silent_voice_and_video_sources()
        first = short_drama_assembly.get_assembly_workspace(
            self.db, "alice", self.project["id"],
            source_inspector=self._video_inspection,
        )
        second = short_drama_assembly.get_assembly_workspace(
            self.db, "alice", self.project["id"],
            source_inspector=self._video_inspection,
        )
        self.assertTrue(first["readiness"]["ready"])
        self.assertTrue(all(shot["ready"] for shot in first["shots"]))
        self.assertEqual(
            "formal_export", first["implementation_status"]
        )
        self.assertEqual("short_drama_media_plan_v1", first["planner_version"])
        self.assertEqual(first["input_hash"], second["input_hash"])
        self.assertEqual(
            first["audio_subtitle"]["input_hash"],
            second["audio_subtitle"]["input_hash"],
        )
        self.assertEqual("not_built", first["audio_subtitle"]["status"])
        self.assertEqual([], first["audio_subtitle"]["blockers"])
        self.assertEqual(30000, first["media_plan"]["project_duration_ms"])
        self.assertEqual(6, len(first["media_plan"]["shots"]))
        self.assertTrue(first["rendering_enabled"])
        self.assertTrue(first["actions"]["can_preview"])
        self.assertTrue(all(
            not value for key, value in first["actions"].items()
            if key not in {"can_preview", "can_save_config"}
        ))

    def test_save_sound_config_versions_contract_and_rejects_stale_write(self):
        with closing(self.db()) as conn:
            shot = conn.execute(
                "SELECT id FROM short_drama_shots WHERE project_id=? "
                "ORDER BY sort_order LIMIT 1", (self.project["id"],),
            ).fetchone()
            revision = conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0]
        body = {
            "project_id": self.project["id"],
            "revision": revision,
            "assembly_revision": 1,
            "config": {
                "subtitle": {
                    "enabled": True,
                    "preset": "white_outline",
                    "position": "bottom",
                },
                "bgm": {
                    "asset_id": None,
                    "volume": 0.18,
                    "fade_in_ms": 500,
                    "fade_out_ms": 800,
                },
                "sound_cues": [{
                    "id": "cue-1",
                    "shot_id": shot[0],
                    "kind": "foley",
                    "asset_id": 91,
                    "start_ms": 200,
                    "end_ms": 1200,
                    "loop": False,
                    "volume": 0.6,
                    "fade_in_ms": 0,
                    "fade_out_ms": 100,
                    "enabled": True,
                }],
            },
        }
        lookup = lambda username, asset_id: {
            "username": username, "id": asset_id, "file": "audio/cue.wav"
        }
        saved = short_drama_assembly.save_assembly_config(
            self.db, "alice", body, lookup
        )
        self.assertTrue(saved["changed"])
        self.assertEqual(2, saved["assembly_revision"])
        self.assertEqual("cue-1", saved["config"]["sound_cues"][0]["id"])
        with self.assertRaises(short_drama_assembly.PreviewBlocked) as stale:
            short_drama_assembly.save_assembly_config(
                self.db, "alice", body, lookup
            )
        self.assertEqual("revision_conflict", stale.exception.code)

    def test_source_revision_change_during_probe_blocks_plan(self):
        self._lock_silent_voice_and_video_sources()
        changed = False

        def inspect(file_key):
            nonlocal changed
            if not changed:
                changed = True
                with closing(self.db()) as conn:
                    conn.execute(
                        "UPDATE short_drama_video_shots "
                        "SET video_revision=video_revision+1 "
                        "WHERE project_id=? AND shot_id=("
                        "SELECT id FROM short_drama_shots WHERE project_id=? "
                        "ORDER BY sort_order,id LIMIT 1)",
                        (self.project["id"], self.project["id"]),
                    )
                    conn.commit()
            return self._video_inspection(file_key)

        snapshot = short_drama_assembly.get_assembly_workspace(
            self.db, "alice", self.project["id"], source_inspector=inspect
        )
        self.assertIsNone(snapshot["media_plan"])
        self.assertIsNone(snapshot["input_hash"])
        self.assertIn(
            "source_changed_during_probe",
            {item["code"] for item in snapshot["readiness"]["blockers"]},
        )

    def test_lipsync_source_hash_mismatch_blocks_assembly_snapshot(self):
        self._lock_silent_voice_and_video_sources()
        with closing(self.db()) as conn:
            shot_id = conn.execute(
                "SELECT id FROM short_drama_shots WHERE project_id=? "
                "ORDER BY sort_order,id LIMIT 1",
                (self.project["id"],),
            ).fetchone()[0]
        plan = {
            "plan_hash": "plan-hash",
            "selected_sources": [{
                "shot_id": shot_id,
                "version_id": "lipsync-version-1",
                "version": 1,
                "job_id": "lipsync-job-1",
                "attempt_id": "lipsync-attempt-1",
                "provider": "provider",
                "model_version": "model",
                "input_hash": "lipsync-input",
                "file": "video/lipsync.mp4",
                "file_hash": "declared-hash-does-not-match",
                "dependency_hashes": {},
                "media_spec": {
                    "duration_ms": 5000,
                    "ratio": "9:16",
                },
                "cost": {},
                "locked_at": 1,
                "locked_by": "alice",
            }],
        }
        with mock.patch.object(
            short_drama_assembly.lipsync_assembly,
            "load_plan",
            return_value=plan,
        ):
            snapshot = short_drama_assembly.get_assembly_workspace(
                self.db,
                "alice",
                self.project["id"],
                source_inspector=self._video_inspection,
            )
        self.assertFalse(snapshot["readiness"]["ready"])
        self.assertIn(
            "lipsync_source_hash_mismatch",
            {item["code"] for item in snapshot["readiness"]["blockers"]},
        )
        affected = next(
            item for item in snapshot["shots"] if item["id"] == shot_id
        )
        self.assertEqual("blocked", affected["video"]["status"])

    def test_preview_submission_is_atomic_idempotent_and_reconciled(self):
        self._lock_silent_voice_and_video_sources()
        with closing(self.db()) as conn:
            conn.execute(
                "CREATE TABLE jobs("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,"
                "cost INTEGER,status TEXT,payload TEXT,result TEXT,error TEXT,"
                "created_at INTEGER,updated_at INTEGER,owner TEXT)"
            )
            revision = conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0]
            conn.commit()
        request = {
            "project_id": self.project["id"],
            "revision": revision,
            "assembly_revision": 1,
        }
        with mock.patch.object(
            short_drama_assembly, "_default_source_inspector",
            side_effect=self._video_inspection,
        ):
            created = short_drama_assembly.create_preview_job(
                self.db, "alice", "alice", request, "preview-key"
            )
            font_failure = short_drama_assembly.PreviewBlocked(
                "subtitle_font_unavailable", "subtitle font unavailable"
            )
            with mock.patch.object(
                short_drama_assembly, "_require_render_environment",
                side_effect=font_failure,
            ) as font_check:
                replayed = short_drama_assembly.create_preview_job(
                    self.db, "alice", "alice", request, "preview-key"
                )
                with self.assertRaises(
                    short_drama_assembly.PreviewIdempotencyConflict
                ):
                    short_drama_assembly.create_preview_job(
                        self.db, "alice", "alice",
                        {**request, "assembly_revision": 2},
                        "preview-key",
                    )
            font_check.assert_not_called()
        self.assertFalse(created["replayed"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(created["job_id"], replayed["job_id"])

        with closing(self.db()) as conn:
            preview_payload = json.loads(conn.execute(
                "SELECT payload FROM jobs WHERE id=?", (created["job_id"],),
            ).fetchone()[0])
        with mock.patch.object(
            short_drama_assembly, "build_assembly_snapshot",
            return_value={
                "input_hash": preview_payload["input_hash"],
                "audio_subtitle": {"input_hash": "f" * 64},
            },
        ):
            with self.assertRaises(short_drama_assembly.PreviewBlocked) as drift:
                short_drama_assembly.preview_render_context(
                    self.db, created["job_id"]
                )
        self.assertEqual("audio_input_changed", drift.exception.code)
        with closing(self.db()) as conn:
            self.assertEqual(
                (1, 1, 1),
                (
                    conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE kind='short_drama_preview'"
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_composition_jobs"
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_composition_versions"
                    ).fetchone()[0],
                ),
            )
            result = {
                "file": "short_drama_preview/p/1/preview.mp4",
                "url": "/api/gen/file/short_drama_preview/p/1/preview.mp4",
                "cover_file": "short_drama_preview/p/1/cover.jpg",
                "duration_ms": 30000, "width": 720, "height": 1280,
                "fps": 30.0, "video_codec": "h264", "audio_codec": "aac",
            }
            conn.execute(
                "UPDATE jobs SET status='done',result=? WHERE id=?",
                (json.dumps(result), created["job_id"]),
            )
            conn.commit()
        self.assertTrue(short_drama_assembly.reconcile_preview_job(
            self.db, created["job_id"]
        ))
        self.assertFalse(short_drama_assembly.reconcile_preview_job(
            self.db, created["job_id"]
        ))
        with closing(self.db()) as conn:
            version = conn.execute(
                "SELECT status,width,height FROM "
                "short_drama_composition_versions WHERE job_id=?",
                (str(created["job_id"]),),
            ).fetchone()
            self.assertEqual(("succeeded", 720, 1280), tuple(version))

    def test_preview_rejects_missing_subtitle_font_before_job_creation(self):
        self._lock_silent_voice_and_video_sources()
        with closing(self.db()) as conn:
            conn.execute(
                "CREATE TABLE jobs("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,"
                "cost INTEGER,status TEXT,payload TEXT,result TEXT,error TEXT,"
                "created_at INTEGER,updated_at INTEGER,owner TEXT)"
            )
            revision = conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0]
            conn.commit()
        request = {
            "project_id": self.project["id"],
            "revision": revision,
            "assembly_revision": 1,
        }
        with mock.patch.object(
            short_drama_assembly, "_default_source_inspector",
            side_effect=self._video_inspection,
        ), mock.patch.object(
            short_drama_assembly, "_require_render_environment",
            side_effect=short_drama_assembly.PreviewBlocked(
                "subtitle_font_unavailable",
                "服务器缺少可用的 Noto Sans CJK 中文字幕字体",
            ),
        ):
            with self.assertRaises(short_drama_assembly.PreviewBlocked) as caught:
                short_drama_assembly.create_preview_job(
                    self.db, "alice", "alice", request, "missing-font"
                )
        self.assertEqual("subtitle_font_unavailable", caught.exception.code)
        with closing(self.db()) as conn:
            self.assertEqual(
                (0, 0, 0),
                (
                    conn.execute(
                        "SELECT COUNT(*) FROM jobs "
                        "WHERE kind='short_drama_preview'"
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_composition_jobs"
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM "
                        "short_drama_composition_versions"
                    ).fetchone()[0],
                ),
            )

    def test_bgm_asset_is_owned_probed_and_bound_into_d2_hash(self):
        self._lock_silent_voice_and_video_sources()
        now = int(time.time())
        config = {
            "bgm": {
                "asset_id": 10,
                "volume": 0.18,
                "fade_in_ms": 500,
                "fade_out_ms": 800,
            },
        }
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_compositions "
                "(project_id,config_json,created_at,updated_at) "
                "VALUES (?,?,?,?)",
                (self.project["id"], json.dumps(config), now, now),
            )
            conn.commit()

        missing = short_drama_assembly.get_assembly_workspace(
            self.db, "alice", self.project["id"],
            source_inspector=self._video_inspection,
        )
        self.assertIn(
            "bgm_asset_missing",
            {item["code"] for item in missing["readiness"]["blockers"]},
        )
        self.assertEqual("blocked", missing["audio_subtitle"]["status"])

        def inspect(file_key):
            if file_key == "audio/bgm.mp3":
                return {
                    "probe": {
                        "duration_ms": 12000,
                        "video": None,
                        "audio": {
                            "codec": "mp3", "sample_rate": 44100,
                            "channels": 2,
                        },
                    },
                    "fingerprint": {"sha256": "b" * 64, "size": 2048},
                }
            return self._video_inspection(file_key)

        ready = short_drama_assembly.get_assembly_workspace(
            self.db, "alice", self.project["id"],
            source_inspector=inspect,
            bgm_lookup=lambda username, asset_id: {
                "id": asset_id,
                "username": username,
                "file": "audio/bgm.mp3",
            },
        )
        self.assertTrue(ready["readiness"]["ready"])
        self.assertEqual("not_built", ready["audio_subtitle"]["status"])
        self.assertEqual("b" * 64,
                         ready["audio_subtitle"]["bgm_source"]["sha256"])
        self.assertEqual(64, len(ready["audio_subtitle"]["input_hash"]))

    def test_persisted_contract_rows_are_returned_without_enabling_actions(self):
        now = int(time.time())
        config = {
            "subtitle": {"enabled": False, "position": "top"},
            "bgm": {"asset_id": "audio-1", "volume": 0.1},
        }
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_compositions "
                "(project_id,assembly_revision,config_json,"
                "current_preview_version,current_final_version,preview_locked,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    self.project["id"], 3, json.dumps(config), 1, None, 1,
                    now, now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_composition_versions "
                "(id,project_id,kind,version,job_id,input_hash,config_json,"
                "file,url,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "version-1", self.project["id"], "preview", 1,
                    "job-preview-1", "input-1", json.dumps(config),
                    "preview.mp4", "/preview.mp4", "succeeded", now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_composition_jobs "
                "(id,username,project_id,job_id,kind,idempotency_key,"
                "request_hash,status,phase,progress,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "job-row-1", "alice", self.project["id"], "job-active",
                    "final", "idem-final", "request-final", "running",
                    "encoding", 55, now, now,
                ),
            )
            conn.commit()
        snapshot = short_drama_assembly.get_assembly_workspace(
            self.db, "alice", self.project["id"]
        )
        self.assertEqual(3, snapshot["assembly_revision"])
        self.assertFalse(snapshot["config"]["subtitle"]["enabled"])
        self.assertEqual("white_outline", snapshot["config"]["subtitle"]["preset"])
        self.assertEqual("audio-1", snapshot["config"]["bgm"]["asset_id"])
        self.assertEqual("job-active", snapshot["active_job"]["job_id"])
        self.assertNotIn("idempotency_key", snapshot["active_job"])
        self.assertNotIn("request_hash", snapshot["active_job"])
        self.assertEqual(1, len(snapshot["versions"]))
        self.assertNotIn("file", snapshot["versions"][0])
        self.assertFalse(snapshot["actions"]["can_export"])

    def test_d4_quote_export_archive_replay_and_confirm(self):
        self._lock_silent_voice_and_video_sources()
        source_inspector = mock.patch.object(
            short_drama_assembly, "_default_source_inspector",
            side_effect=self._video_inspection,
        )
        source_inspector.start()
        self.addCleanup(source_inspector.stop)
        with closing(self.db()) as conn:
            conn.execute(
                "CREATE TABLE jobs("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,"
                "cost INTEGER,status TEXT,payload TEXT,result TEXT,error TEXT,"
                "created_at INTEGER,updated_at INTEGER,owner TEXT)"
            )
            revision = conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0]
            conn.commit()
        preview_request = {
            "project_id": self.project["id"], "revision": revision,
            "assembly_revision": 1,
        }
        with mock.patch.object(
            short_drama_assembly, "_default_source_inspector",
            side_effect=self._video_inspection,
        ):
            preview = short_drama_assembly.create_preview_job(
                self.db, "alice", "alice", preview_request, "preview-d4"
            )
        preview_result = {
            "file": "short_drama_preview/p/1/preview.mp4",
            "url": "/api/gen/file/short_drama_preview/p/1/preview.mp4",
            "cover_file": "short_drama_preview/p/1/cover.jpg",
            "duration_ms": 30000, "width": 720, "height": 1280,
            "fps": 30.0, "video_codec": "h264", "audio_codec": "aac",
        }
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE jobs SET status='done',result=? WHERE id=?",
                (json.dumps(preview_result), preview["job_id"]),
            )
            conn.commit()
        self.assertTrue(short_drama_assembly.reconcile_preview_job(
            self.db, preview["job_id"]
        ))
        quote_body = {
            **preview_request, "preview_version": 1, "cover_time_ms": 1200,
        }
        with closing(self.db()) as conn:
            preview_payload = json.loads(conn.execute(
                "SELECT payload FROM jobs WHERE id=?", (preview["job_id"],),
            ).fetchone()[0])
        with mock.patch.object(
            short_drama_assembly, "build_assembly_snapshot",
            return_value={
                "input_hash": preview_payload["input_hash"],
                "audio_subtitle": {"input_hash": "e" * 64},
            },
        ):
            with self.assertRaises(short_drama_assembly.PreviewBlocked) as drift:
                short_drama_assembly.create_final_quote(
                    self.db, "alice", "alice", quote_body, cost=0,
                    storage_available=True,
                )
        self.assertEqual("preview_stale", drift.exception.code)
        with mock.patch.object(
            short_drama_assembly, "_require_render_environment",
            side_effect=short_drama_assembly.PreviewBlocked(
                "subtitle_font_unavailable",
                "服务器缺少可用的 Noto Sans CJK 中文字幕字体",
            ),
        ):
            with self.assertRaises(short_drama_assembly.PreviewBlocked) as font:
                short_drama_assembly.create_final_quote(
                    self.db, "alice", "alice", quote_body, cost=5,
                    storage_available=True,
                )
        self.assertEqual("subtitle_font_unavailable", font.exception.code)
        with closing(self.db()) as conn:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_final_quotes"
                ).fetchone()[0],
            )
        with mock.patch.object(
            short_drama_assembly, "_default_source_inspector",
            side_effect=self._video_inspection,
        ):
            quote = short_drama_assembly.create_final_quote(
                self.db, "alice", "alice", quote_body, cost=0,
                storage_available=True,
            )
            paid_quote = short_drama_assembly.create_final_quote(
                self.db, "alice", "alice", quote_body, cost=5,
                storage_available=True,
            )
            rejected_quote = short_drama_assembly.create_final_quote(
                self.db, "alice", "alice", quote_body, cost=5,
                storage_available=True,
            )
            build_failed_quote = short_drama_assembly.create_final_quote(
                self.db, "alice", "alice", quote_body, cost=5,
                storage_available=True,
            )
        with closing(self.db()) as conn:
            now = int(time.time())
            conn.execute(
                "INSERT INTO short_drama_composition_jobs "
                "(id,username,project_id,job_id,kind,idempotency_key,"
                "request_hash,status,phase,progress,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "race-active", "alice", self.project["id"], "race-job",
                    "preview", "race-preview", "race-hash", "running",
                    "rendering", 20, now, now,
                ),
            )
            conn.commit()
        charged, refunded = [], []
        with self.assertRaises(short_drama_assembly.ActiveCompositionJob):
            short_drama_assembly.create_final_job(
                self.db, "alice", "alice", {
                    **quote_body, "quote_token": paid_quote["quote_token"],
                }, "export-race",
                deduct_points=lambda *args: charged.append(args),
                refund_points=lambda *args: refunded.append(args),
            )
        self.assertEqual([], charged)
        self.assertEqual([], refunded)
        with closing(self.db()) as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT state FROM short_drama_final_attempts "
                    "WHERE idempotency_key='export-race'"
                ).fetchone()
            )
            conn.execute(
                "DELETE FROM short_drama_composition_jobs WHERE id='race-active'"
            )
            now = int(time.time())
            conn.execute(
                "INSERT INTO short_drama_final_attempts "
                "(id,actor_username,owner_username,project_id,idempotency_key,"
                "request_hash,quote_token,cost,charge_key,refund_key,state,"
                "created_at,updated_at) VALUES "
                "('budget-hold','alice','alice',?,'budget-hold','hold-hash',"
                "'hold-quote',96,'hold-charge','hold-refund','accepted',?,?)",
                (self.project["id"], now, now),
            )
            conn.commit()
        export_body = {
            **quote_body, "quote_token": paid_quote["quote_token"],
        }
        blocked_charges = []
        with self.assertRaises(short_drama_assembly.PreviewBlocked) as budget:
            short_drama_assembly.create_final_job(
                self.db, "alice", "alice", export_body, "export-budget",
                deduct_points=lambda *args: blocked_charges.append(args),
            )
        self.assertEqual("point_budget_exceeded", budget.exception.code)
        self.assertEqual([], blocked_charges)
        with closing(self.db()) as conn:
            self.assertIsNone(conn.execute(
                "SELECT id FROM short_drama_final_attempts "
                "WHERE idempotency_key='export-budget'"
            ).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT consumed_job_id FROM short_drama_final_quotes "
                "WHERE token=?", (paid_quote["quote_token"],)
            ).fetchone()[0])
            conn.execute(
                "DELETE FROM short_drama_final_attempts WHERE id='budget-hold'"
            )
            conn.commit()
        rejected_body = {
            **quote_body, "quote_token": rejected_quote["quote_token"],
        }

        class ChargeRejected(Exception):
            status = 402

        def reject_charge(*_args):
            raise ChargeRejected("余额不足")

        with self.assertRaises(ChargeRejected):
            short_drama_assembly.create_final_job(
                self.db, "alice", "alice", rejected_body,
                "export-rejected",
                deduct_points=reject_charge,
                charge_lookup=lambda _key: None,
            )
        with closing(self.db()) as conn:
            rejected = conn.execute(
                "SELECT state FROM short_drama_final_attempts "
                "WHERE idempotency_key='export-rejected'"
            ).fetchone()
            self.assertEqual("failed", rejected[0])
            usage = short_drama._project_point_usage(
                conn, self.project["id"]
            )
        self.assertEqual(0, usage["reserved_points"])
        self.assertEqual(0, usage["spent_points"])
        build_failed_body = {
            **quote_body,
            "quote_token": build_failed_quote["quote_token"],
        }
        failed_refunds = []
        with closing(self.db()) as conn:
            conn.execute(
                "CREATE TRIGGER fail_final_job_insert "
                "BEFORE INSERT ON jobs "
                "WHEN NEW.kind='short_drama_final' "
                "BEGIN SELECT RAISE(ABORT,'injected final job failure'); END"
            )
            conn.commit()

        def unavailable_refund(*args):
            failed_refunds.append(args)
            raise RuntimeError("退款服务暂时不可用")

        with self.assertRaises(sqlite3.IntegrityError):
            short_drama_assembly.create_final_job(
                self.db, "alice", "alice", build_failed_body,
                "export-build-failed",
                deduct_points=lambda *_args: 95,
                refund_points=unavailable_refund,
            )
        with closing(self.db()) as conn:
            conn.execute("DROP TRIGGER fail_final_job_insert")
            pending = conn.execute(
                "SELECT state,job_id,recovery_token "
                "FROM short_drama_final_attempts "
                "WHERE idempotency_key='export-build-failed'"
            ).fetchone()
            conn.commit()
        self.assertEqual(("refund_pending", None, None), tuple(pending))
        self.assertEqual(1, len(failed_refunds))

        class RecoveryPoints:
            def __init__(self):
                self.refunds = []

            @staticmethod
            def get_points_transaction(_key):
                raise AssertionError(
                    "refund-pending attempt must not query charge ledger"
                )

            def refund_points(
                self, username, amount, reason, transaction_key=""
            ):
                self.refunds.append(transaction_key)
                return 100

        recovery_points = RecoveryPoints()
        recovered = short_drama_assembly.retry_final_charge_attempts(
            self.db, recovery_points
        )
        self.assertEqual(1, recovered["refunded"])
        with closing(self.db()) as conn:
            state = conn.execute(
                "SELECT state FROM short_drama_final_attempts "
                "WHERE idempotency_key='export-build-failed'"
            ).fetchone()[0]
            usage = short_drama._project_point_usage(
                conn, self.project["id"]
            )
        self.assertEqual("refunded", state)
        self.assertEqual(0, usage["reserved_points"])
        self.assertEqual(0, usage["spent_points"])
        self.assertEqual(
            failed_refunds[0][3], recovery_points.refunds[0]
        )
        export_charges = []

        def lost_charge_response(*args):
            export_charges.append(args)
            raise RuntimeError("扣点响应丢失")

        created = short_drama_assembly.create_final_job(
            self.db, "alice", "alice", export_body, "export-d4",
            deduct_points=lost_charge_response,
            charge_lookup=lambda _key: {
                "username": "alice", "delta": -5, "after_points": 95,
            },
        )
        with closing(self.db()) as conn:
            final_payload = json.loads(conn.execute(
                "SELECT payload FROM jobs WHERE id=?", (created["job_id"],),
            ).fetchone()[0])
        self.assertEqual(
            preview_payload["d2_input_hash"],
            final_payload["d2_input_hash"],
        )
        self.assertEqual(
            preview_payload.get("bgm_file", ""),
            final_payload["bgm_file"],
        )
        self.assertEqual(
            preview_payload.get("sound_cues", []),
            final_payload["sound_cues"],
        )
        replayed = short_drama_assembly.create_final_job(
            self.db, "alice", "alice", export_body, "export-d4",
            deduct_points=lambda *args: export_charges.append(args),
        )
        self.assertFalse(created["replayed"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(created["job_id"], replayed["job_id"])
        self.assertEqual(1, len(export_charges))
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_final_quotes SET consumed_job_id=NULL "
                "WHERE token=?", (paid_quote["quote_token"],),
            )
            conn.commit()
        legacy_replayed = short_drama_assembly.create_final_job(
            self.db, "alice", "alice", export_body, "export-d4"
        )
        self.assertTrue(legacy_replayed["replayed"])
        with closing(self.db()) as conn:
            self.assertEqual(
                str(created["job_id"]),
                conn.execute(
                    "SELECT consumed_job_id FROM short_drama_final_quotes "
                    "WHERE token=?", (paid_quote["quote_token"],),
                ).fetchone()[0],
            )
        with self.assertRaises(short_drama_assembly.PreviewBlocked) as consumed:
            short_drama_assembly.create_final_job(
                self.db, "alice", "alice", export_body, "export-new-key"
            )
        self.assertEqual("quote_consumed", consumed.exception.code)
        with closing(self.db()) as conn:
            usage = short_drama._project_point_usage(
                conn, self.project["id"]
            )
        self.assertEqual(5, usage["spent_points"])
        self.assertEqual(0, usage["reserved_points"])
        result = {
            "file": "short_drama_final/p/2/final.mp4",
            "url": "https://signed.example/final.mp4",
            "cover_file": "short_drama_final/p/2/cover.jpg",
            "cover_url": "https://signed.example/cover.jpg",
            "object_key": "short-drama/a/p/final/v1/hash.mp4",
            "cover_key": "short-drama/a/p/final/v1/hash-cover.jpg",
            "duration_ms": 30000, "width": 1080, "height": 1920,
            "fps": 30.0, "video_codec": "h264", "audio_codec": "aac",
            "size": 123456, "sha256": "f" * 64,
        }
        asset = short_drama_assembly.archive_final_asset(
            self.db, created["job_id"], result
        )
        result["asset_id"] = asset["id"]
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE jobs SET status='done',result=? WHERE id=?",
                (json.dumps(result), created["job_id"]),
            )
            conn.commit()
        self.assertTrue(short_drama_assembly.reconcile_final_job(
            self.db, created["job_id"]
        ))
        workspace = short_drama_assembly.get_assembly_workspace(
            self.db, "alice", self.project["id"],
            source_inspector=self._video_inspection,
        )
        self.assertTrue(workspace["actions"]["can_confirm"])
        completion_body = {
            "project_id": self.project["id"],
            "revision": revision, "final_version": 1,
        }
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_COMPLETION_ENABLED": "1"},
        ):
            with self.assertRaises(
                short_drama_completion.CompletionError
            ) as required:
                short_drama_assembly.confirm_final(
                    self.db, "alice", completion_body,
                )
        self.assertEqual("completion_required", required.exception.code)
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_COMPLETION_ENABLED": "0"},
        ):
            completed = short_drama_assembly.confirm_final(
                self.db, "alice", completion_body,
            )
        self.assertEqual("completed", completed["stage"])
        self.assertFalse(completed["replayed"])
        with closing(self.db()) as conn:
            stage = conn.execute(
                "SELECT stage FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()[0]
        self.assertEqual("completed", stage)

    def test_d4_attempt_states_are_included_in_project_point_ledger(self):
        now = int(time.time())
        with closing(self.db()) as conn:
            conn.execute(
                "CREATE TABLE jobs("
                "id INTEGER PRIMARY KEY,kind TEXT,username TEXT,cost INTEGER,"
                "status TEXT,payload TEXT,result TEXT,error TEXT,"
                "created_at INTEGER,updated_at INTEGER,owner TEXT,"
                "refunded INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "INSERT INTO jobs VALUES "
                "(801,'short_drama_final','alice',20,'running','{}',NULL,NULL,"
                "?,?, 'content',0)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO jobs VALUES "
                "(802,'short_drama_final','alice',30,'error','{}',NULL,'x',"
                "?,?, 'content',1)",
                (now, now),
            )
            rows = [
                ("reserve", "accepted", 10, None),
                ("charged", "charged", 20, "801"),
                ("refund", "refund_pending", 30, "802"),
                ("archive", "archived", 5, None),
                ("refunded", "refunded", 99, None),
            ]
            for suffix, state, cost, job_id in rows:
                conn.execute(
                    "INSERT INTO short_drama_final_attempts "
                    "(id,actor_username,owner_username,project_id,"
                    "idempotency_key,request_hash,quote_token,cost,charge_key,"
                    "refund_key,state,job_id,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "attempt-" + suffix, "alice", "alice",
                        self.project["id"], "idem-" + suffix,
                        "hash-" + suffix, "quote-" + suffix, cost,
                        "charge-" + suffix, "refund-" + suffix, state,
                        job_id, now, now,
                    ),
                )
            conn.commit()
            usage = short_drama._project_point_usage(
                conn, self.project["id"]
            )
        self.assertEqual(25, usage["spent_points"])
        self.assertEqual(10, usage["reserved_points"])

    def test_workspace_reconciles_timed_out_final_by_kind(self):
        now = int(time.time())
        with closing(self.db()) as conn:
            conn.execute(
                "CREATE TABLE jobs("
                "id INTEGER PRIMARY KEY,kind TEXT,username TEXT,cost INTEGER,"
                "status TEXT,payload TEXT,result TEXT,error TEXT,"
                "created_at INTEGER,updated_at INTEGER,owner TEXT,"
                "refunded INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "INSERT INTO jobs VALUES "
                "(901,'short_drama_final','alice',5,'error','{}',NULL,"
                "'timeout',?,?, 'content',1)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO short_drama_composition_versions "
                "(id,project_id,kind,version,job_id,input_hash,config_json,"
                "status,created_at) VALUES "
                "('final-timeout',?,'final',1,'901','hash','{}','rendering',?)",
                (self.project["id"], now),
            )
            conn.execute(
                "INSERT INTO short_drama_composition_jobs "
                "(id,username,project_id,job_id,kind,idempotency_key,"
                "request_hash,status,phase,progress,created_at,updated_at) "
                "VALUES ('linked-timeout','alice',?,'901','final',"
                "'timeout-key','timeout-hash','running','rendering',50,?,?)",
                (self.project["id"], now, now),
            )
            conn.execute(
                "INSERT INTO short_drama_final_attempts "
                "(id,actor_username,owner_username,project_id,"
                "idempotency_key,request_hash,quote_token,cost,charge_key,"
                "refund_key,state,job_id,created_at,updated_at) "
                "VALUES ('attempt-timeout','alice','alice',?,"
                "'timeout-key','timeout-hash','timeout-quote',5,"
                "'timeout-charge','timeout-refund','charged','901',?,?)",
                (self.project["id"], now, now),
            )
            conn.commit()
        short_drama_assembly._reconcile_project_composition_jobs(
            self.db, self.project["id"]
        )
        with closing(self.db()) as conn:
            self.assertEqual(
                "failed",
                conn.execute(
                    "SELECT status FROM short_drama_composition_jobs "
                    "WHERE job_id='901'"
                ).fetchone()[0],
            )
            self.assertEqual(
                "failed",
                conn.execute(
                    "SELECT status FROM short_drama_composition_versions "
                    "WHERE job_id='901'"
                ).fetchone()[0],
            )
            self.assertEqual(
                "refunded",
                conn.execute(
                    "SELECT state FROM short_drama_final_attempts "
                    "WHERE job_id='901'"
                ).fetchone()[0],
            )

    def test_d4_orphaned_accepted_charge_is_observed_then_recovered(self):
        old = int(time.time()) - 180
        with closing(self.db()) as conn:
            for suffix in ("missing", "charged"):
                conn.execute(
                    "INSERT INTO short_drama_final_attempts "
                    "(id,actor_username,owner_username,project_id,"
                    "idempotency_key,request_hash,quote_token,cost,charge_key,"
                    "refund_key,state,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "orphan-" + suffix, "alice", "alice",
                        self.project["id"], "orphan-idem-" + suffix,
                        "orphan-hash-" + suffix,
                        "orphan-quote-" + suffix, 5,
                        "orphan-charge-" + suffix,
                        "orphan-refund-" + suffix,
                        "accepted", old, old,
                    ),
                )
            conn.execute(
                "INSERT INTO short_drama_final_attempts "
                "(id,actor_username,owner_username,project_id,"
                "idempotency_key,request_hash,quote_token,cost,charge_key,"
                "refund_key,state,recovery_token,recovery_started_at,"
                "created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "orphan-active", "alice", "alice",
                    self.project["id"], "orphan-idem-active",
                    "orphan-hash-active", "orphan-quote-active", 5,
                    "orphan-charge-active", "orphan-refund-active",
                    "accepted", "submission:active", int(time.time()),
                    old, old,
                ),
            )
            conn.commit()

        class Points:
            def __init__(self):
                self.refunds = []

            def get_points_transaction(self, key):
                if key == "orphan-charge-active":
                    raise AssertionError(
                        "active charge lease must not be queried"
                    )
                if key == "orphan-charge-charged":
                    return {
                        "username": "alice", "delta": -5,
                        "after_points": 95,
                    }
                return None

            def refund_points(
                self, username, amount, reason, transaction_key=""
            ):
                self.refunds.append(
                    (username, amount, reason, transaction_key)
                )
                return 100

        points = Points()
        first = short_drama_assembly.retry_final_charge_attempts(
            self.db, points
        )
        self.assertEqual(1, first["refunded"])
        with closing(self.db()) as conn:
            missing = conn.execute(
                "SELECT state,error FROM short_drama_final_attempts "
                "WHERE id='orphan-missing'"
            ).fetchone()
            charged = conn.execute(
                "SELECT state FROM short_drama_final_attempts "
                "WHERE id='orphan-charged'"
            ).fetchone()
            active = conn.execute(
                "SELECT state,recovery_token "
                "FROM short_drama_final_attempts WHERE id='orphan-active'"
            ).fetchone()
            self.assertEqual("accepted", missing[0])
            self.assertEqual(
                short_drama_assembly.FINAL_CHARGE_LEDGER_ABSENT_ONCE,
                missing[1],
            )
            self.assertEqual("refunded", charged[0])
            self.assertEqual(
                ("accepted", "submission:active"), tuple(active)
            )
            conn.execute(
                "DELETE FROM short_drama_final_attempts "
                "WHERE id='orphan-active'"
            )
            conn.execute(
                "UPDATE short_drama_final_attempts SET updated_at=? "
                "WHERE id='orphan-missing'", (old,),
            )
            conn.commit()
        second = short_drama_assembly.retry_final_charge_attempts(
            self.db, points
        )
        self.assertEqual(1, second["failed"])
        with closing(self.db()) as conn:
            self.assertEqual(
                "failed",
                conn.execute(
                    "SELECT state FROM short_drama_final_attempts "
                    "WHERE id='orphan-missing'"
                ).fetchone()[0],
            )
            usage = short_drama._project_point_usage(
                conn, self.project["id"]
            )
        self.assertEqual(0, usage["reserved_points"])
        self.assertEqual(0, usage["spent_points"])
        self.assertEqual(
            "orphan-refund-charged", points.refunds[0][3]
        )

    def test_owner_stage_and_http_contract(self):
        with self.assertRaises(LookupError):
            short_drama_assembly.get_assembly_workspace(
                self.db, "mallory", self.project["id"]
            )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='video_review' "
                "WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        with self.assertRaises(ValueError):
            short_drama_assembly.get_assembly_workspace(
                self.db, "alice", self.project["id"]
            )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='assembly_review' "
                "WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()

        handler = _Handler(
            "/api/gen/short-drama/assembly?project_id=" + self.project["id"]
        )
        matched = short_drama.dispatch_http(
            handler, "GET", self.db,
            lambda token: {"username": "alice"} if token == "token" else None,
        )
        self.assertTrue(matched)
        self.assertEqual(200, handler.status)
        self.assertEqual(self.project["id"], handler.payload["project_id"])

        anonymous = _Handler(handler.path, token="")
        self.assertTrue(short_drama.dispatch_http(
            anonymous, "GET", self.db, lambda _token: None
        ))
        self.assertEqual(401, anonymous.status)

    def test_final_asset_detail_route_is_owner_scoped(self):
        now = int(time.time())
        asset_id = "f" * 32
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_final_assets "
                "(id,owner_username,created_by,project_id,"
                "composition_version_id,job_id,title,object_key,cover_key,"
                "video_url,cover_url,size,sha256,width,height,fps,"
                "duration_ms,video_codec,audio_codec,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    asset_id, "alice", "alice", self.project["id"],
                    "final-version-detail", "final-job-detail",
                    "测试正式成片", "short-drama/final.mp4",
                    "short-drama/final-cover.jpg",
                    "https://signed.example/final.mp4",
                    "https://signed.example/final-cover.jpg",
                    123456, "a" * 64, 1080, 1920, 30.0, 30000,
                    "h264", "aac", now,
                ),
            )
            conn.commit()

        item = short_drama_assembly.get_final_asset(
            self.db, "alice", asset_id
        )
        self.assertEqual(asset_id, item["asset_id"])
        self.assertEqual("short_drama_final", item["source_type"])
        self.assertEqual("1080x1920", item["resolution"])
        self.assertEqual("9:16", item["ratio"])

        handler = _Handler(
            "/api/gen/short-drama/final-assets/" + asset_id
        )
        self.assertTrue(short_drama.dispatch_http(
            handler, "GET", self.db,
            lambda token: {"username": "alice"} if token == "token" else None,
        ))
        self.assertEqual(200, handler.status)
        self.assertEqual(asset_id, handler.payload["id"])
        self.assertNotIn("object_key", handler.payload)

        denied = _Handler(handler.path)
        self.assertTrue(short_drama.dispatch_http(
            denied, "GET", self.db,
            lambda token: {"username": "mallory"} if token == "token" else None,
        ))
        self.assertEqual(404, denied.status)

        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET board_id='shared-board' "
                "WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        editor = _Handler(handler.path)
        self.assertTrue(short_drama.dispatch_http(
            editor, "GET", self.db,
            lambda token: {"username": "bob"} if token == "token" else None,
            canvas_access_resolver=lambda _handler: {
                "board_id": "shared-board", "role": "editor",
            },
        ))
        self.assertEqual(200, editor.status)
        self.assertEqual(asset_id, editor.payload["asset_id"])


if __name__ == "__main__":
    unittest.main()
