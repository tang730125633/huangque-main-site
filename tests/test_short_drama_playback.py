import json
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

from content_domains import short_drama
from content_domains import short_drama_assembly
from content_domains import short_drama_playback as playback
from content_domains import short_drama_playback_hashes as hashes
from content_domains import short_drama_playback_render as render
from content_domains import short_drama_timeline


class ShortDramaPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(
            prefix=".tmp-short-drama-playback-", dir=ROOT
        )
        self.db_path = Path(self.tempdir.name) / "content.db"

        def db_factory():
            connection = sqlite3.connect(self.db_path, timeout=5)
            connection.row_factory = sqlite3.Row
            return connection

        self.db = db_factory
        short_drama.init_db(self.db)
        project = short_drama.create_project(
            self.db, "alice", {
                "title": "播放包",
                "synopsis": "用于验证单文件播放器与外置字幕。",
                "ratio": "9:16", "target_duration": 30,
                "shot_count": 6, "visual_style": "电影写实",
                "point_budget": 100,
            }
        )
        self.project_id = project["id"]
        with closing(self.db()) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,"
                "cost INTEGER,status TEXT,payload TEXT,result TEXT,error TEXT,"
                "created_at INTEGER,updated_at INTEGER,owner TEXT)"
            )
            now = int(time.time())
            conn.execute(
                "INSERT INTO short_drama_compositions "
                "(project_id,assembly_revision,config_json,"
                "current_preview_version,created_at,updated_at) "
                "VALUES (?,1,?,1,?,?)",
                (
                    self.project_id,
                    json.dumps({"subtitle": {"delivery": "external_vtt"}}),
                    now, now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_composition_versions "
                "(id,project_id,kind,version,job_id,input_hash,config_json,"
                "file,url,duration_ms,width,height,fps,video_codec,audio_codec,"
                "status,created_at) VALUES "
                "('preview-1',?,'preview',1,'91','input-1',?,"
                "'short_drama_preview/p/preview.mp4',"
                "'/api/gen/file/short_drama_preview/p/preview.mp4',"
                "30000,720,1280,30,'h264','aac','succeeded',?)",
                (
                    self.project_id,
                    json.dumps({"subtitle": {"delivery": "external_vtt"}}),
                    now,
                ),
            )
            cues = [{
                "shot_id": "shot-1", "line_id": "line-1", "text": "你好",
                "start_ms": 100, "end_ms": 900,
            }]
            conn.execute(
                "INSERT INTO short_drama_timeline_versions "
                "(id,project_id,version,status,revision,contract_version,"
                "duration_ms,source_hashes_json,timeline_hash,input_hash,"
                "subtitle_cues_json,blockers_json,created_by,created_at) "
                "VALUES ('timeline-1',?,1,'ready',1,'v1',30000,'{}',"
                "'timeline-hash','timeline-input',?,'[]','alice',?)",
                (self.project_id, json.dumps(cues, ensure_ascii=False), now),
            )
            conn.execute(
                "INSERT INTO short_drama_timeline_current "
                "(project_id,version_id,revision,updated_at) "
                "VALUES (?,'timeline-1',1,?)",
                (self.project_id, now),
            )
            project_row = conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=?",
                (self.project_id,),
            ).fetchone()
            authoritative = short_drama_timeline._authoritative_source(
                conn, project_row
            )
            conn.execute(
                "UPDATE short_drama_timeline_versions "
                "SET source_hashes_json=? WHERE id='timeline-1'",
                (json.dumps(authoritative["source_hashes"]),),
            )
            conn.commit()
        snapshot_patch = mock.patch.object(
            short_drama_assembly,
            "build_assembly_snapshot",
            return_value={"input_hash": "input-1"},
        )
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_hashes_are_canonical_and_bundle_changes_with_subtitles(self):
        media = {"b": 2, "a": 1}
        first = hashes.canonical_hash(media)
        self.assertEqual(first, hashes.canonical_hash({"a": 1, "b": 2}))
        self.assertNotEqual(
            hashes.bundle_hash(media, {"cue": 1}),
            hashes.bundle_hash(media, {"cue": 2}),
        )

    def test_webvtt_uses_master_timeline_boundaries(self):
        value = render._webvtt([{
            "text": "你好", "start_ms": 100, "end_ms": 900,
        }])
        self.assertIn("WEBVTT", value)
        self.assertIn("00:00:00.100 --> 00:00:00.900", value)
        self.assertIn("你好", value)

    def test_zero_cost_remux_is_idempotent(self):
        queued = []
        first = playback.create_remux_job(
            self.db, "alice", "alice",
            {"project_id": self.project_id, "source_version_id": "preview-1"},
            "stable-remux", enqueue=lambda job_id, kind: queued.append(
                (job_id, kind)
            ),
        )
        second = playback.create_remux_job(
            self.db, "alice", "alice",
            {"project_id": self.project_id, "source_version_id": "preview-1"},
            "stable-remux",
        )
        self.assertEqual(0, first["cost"])
        self.assertEqual("short_drama_remux", queued[0][1])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["job_id"], second["job_id"])

    def test_legacy_burned_preview_cannot_claim_subtitle_toggle(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_composition_versions "
                "SET config_json='{}' WHERE id='preview-1'"
            )
            conn.commit()
        with self.assertRaises(playback.PlaybackError) as captured:
            playback.create_remux_job(
                self.db, "alice", "alice",
                {"project_id": self.project_id, "source_version_id": "preview-1"},
                "legacy-remux",
            )
        self.assertEqual("legacy_burned_subtitle", captured.exception.code)

    def test_explicit_source_version_cannot_cross_project_boundary(self):
        other = short_drama.create_project(
            self.db, "alice", {
                "title": "other", "synopsis": "other project",
                "ratio": "16:9", "target_duration": 30,
                "shot_count": 6, "visual_style": "cinematic",
                "point_budget": 100,
            },
        )
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_composition_versions "
                "(id,project_id,kind,version,job_id,status,input_hash,"
                "config_json,file,"
                "cover_file,duration_ms,width,height,fps,video_codec,audio_codec,"
                "created_at) VALUES "
                "(?,?, 'preview',1,'other-job','succeeded','other-hash',"
                "'{\"subtitle\":{\"delivery\":\"external_vtt\"}}',"
                "'short_drama_preview/other.mp4','',5000,1280,720,30,"
                "'h264','aac',1)",
                ("other-preview", other["id"]),
            )
            conn.commit()
        with self.assertRaises(playback.PlaybackError) as captured:
            playback.create_remux_job(
                self.db, "alice", "alice",
                {"project_id": self.project_id,
                 "source_version_id": "other-preview"},
                "cross-project",
            )
        self.assertEqual("playback_source_missing", captured.exception.code)

    def test_historical_preview_with_stale_input_is_rejected(self):
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_composition_versions "
                "(id,project_id,kind,version,job_id,input_hash,config_json,"
                "file,url,duration_ms,width,height,fps,video_codec,audio_codec,"
                "status,created_at) VALUES "
                "('preview-old',?,'preview',2,'93','input-old',?,"
                "'short_drama_preview/p/old.mp4',"
                "'/api/gen/file/short_drama_preview/p/old.mp4',"
                "30000,720,1280,30,'h264','aac','succeeded',?)",
                (
                    self.project_id,
                    json.dumps({"subtitle": {"delivery": "external_vtt"}}),
                    int(time.time()),
                ),
            )
            conn.commit()
        with self.assertRaises(playback.PlaybackError) as captured:
            playback.create_remux_job(
                self.db, "alice", "alice",
                {
                    "project_id": self.project_id,
                    "source_version_id": "preview-old",
                },
                "stale-preview",
            )
        self.assertEqual("playback_source_stale", captured.exception.code)

    def test_historical_preview_with_current_input_is_allowed(self):
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_composition_versions "
                "(id,project_id,kind,version,job_id,input_hash,config_json,"
                "file,url,duration_ms,width,height,fps,video_codec,audio_codec,"
                "status,created_at) VALUES "
                "('preview-compatible',?,'preview',2,'94','input-1',?,"
                "'short_drama_preview/p/compatible.mp4',"
                "'/api/gen/file/short_drama_preview/p/compatible.mp4',"
                "30000,720,1280,30,'h264','aac','succeeded',?)",
                (
                    self.project_id,
                    json.dumps({"subtitle": {"delivery": "external_vtt"}}),
                    int(time.time()),
                ),
            )
            conn.commit()
        created = playback.create_remux_job(
            self.db, "alice", "alice",
            {
                "project_id": self.project_id,
                "source_version_id": "preview-compatible",
            },
            "compatible-preview",
        )
        self.assertEqual("queued", created["status"])

    def test_persisted_ready_timeline_is_rejected_when_effectively_stale(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_timeline_versions "
                "SET source_hashes_json='{}' WHERE id='timeline-1'"
            )
            conn.commit()
        with self.assertRaises(playback.PlaybackError) as captured:
            playback.create_remux_job(
                self.db, "alice", "alice",
                {"project_id": self.project_id},
                "stale-timeline",
            )
        self.assertEqual("timeline_stale", captured.exception.code)

    def test_source_change_during_preflight_blocks_job_creation(self):
        with mock.patch.object(
            short_drama_assembly,
            "_source_identity",
            side_effect=[{"revision": 1}, {"revision": 2}],
        ):
            with self.assertRaises(playback.PlaybackError) as captured:
                playback.create_remux_job(
                    self.db, "alice", "alice",
                    {"project_id": self.project_id},
                    "changed-during-preflight",
                )
        self.assertEqual("playback_source_stale", captured.exception.code)
        with closing(self.db()) as conn:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_playback_jobs"
                ).fetchone()[0],
            )

    def test_incomplete_worker_result_becomes_terminal_failure(self):
        created = playback.create_remux_job(
            self.db, "alice", "alice",
            {"project_id": self.project_id, "source_version_id": "preview-1"},
            "invalid-result",
        )
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE jobs SET status='done',result='{}' WHERE id=?",
                (created["job_id"],),
            )
            conn.commit()
        self.assertTrue(playback.reconcile_job(self.db, created["job_id"]))
        job = playback.get_job(
            self.db, "alice", self.project_id, created["job_id"]
        )
        self.assertEqual("failed", job["status"])
        self.assertEqual("remux_result_invalid", job["error_code"])


if __name__ == "__main__":
    unittest.main()
