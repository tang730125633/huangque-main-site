import json
import sqlite3
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from contextlib import closing
from pathlib import Path


import sys

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import short_drama, short_drama_assembly, short_drama_voice


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
        self.tempdir = tempfile.TemporaryDirectory(
            prefix=".tmp-short-drama-assembly-", dir=ROOT
        )
        self.db_path = Path(self.tempdir.name) / "content.db"

        def db_factory():
            return sqlite3.connect(self.db_path, timeout=5)

        self.db = db_factory
        short_drama.init_db(self.db)
        with closing(self.db()) as conn:
            conn.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, username TEXT,
                cost INTEGER, status TEXT DEFAULT 'pending', payload TEXT,
                result TEXT, error TEXT, created_at INTEGER, updated_at INTEGER,
                deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0,
                owner TEXT
            )""")
            conn.commit()
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
        self.assertEqual("formal_export", snapshot["implementation_status"])
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
        self.assertEqual({
            "can_save_config": False,
            "can_preview": False,
            "can_lock_preview": False,
            "can_export": False,
            "can_confirm": False,
        }, snapshot["actions"])

    def _lock_all_inputs(self):
        now = int(time.time())
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE short_drama_voice_shots SET locked=1,audio_mode='native' "
                "WHERE project_id=?",
                (self.project["id"],),
            )
            shots = conn.execute(
                "SELECT id,duration FROM short_drama_shots WHERE project_id=? "
                "ORDER BY sort_order",
                (self.project["id"],),
            ).fetchall()
            for index, shot in enumerate(shots):
                asset_id = "video-asset-%d" % index
                conn.execute(
                    "INSERT INTO short_drama_video_assets "
                    "(id,project_id,shot_id,current_version,locked,created_at,updated_at) "
                    "VALUES (?,?,?,1,1,?,?)",
                    (asset_id, self.project["id"], shot["id"], now, now),
                )
                conn.execute(
                    "INSERT INTO short_drama_video_versions "
                    "(id,asset_id,version,job_id,channel,model,prompt,duration,ratio,"
                    "resolution,file,url,cost,created_at) VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?)",
                    ("video-version-%d" % index, asset_id, 9000 + index,
                     "micro", "seedance", "natural motion", shot["duration"],
                     "9:16", "480p", "video/clip-%d.mp4" % index,
                     "/api/gen/file/video/clip-%d.mp4" % index, 0, now),
                )
            conn.commit()

    def test_locked_video_and_voice_enable_final_export(self):
        self._lock_all_inputs()
        snapshot = short_drama_assembly.get_assembly_workspace(
            self.db, "alice", self.project["id"]
        )
        self.assertTrue(snapshot["readiness"]["ready"])
        self.assertTrue(snapshot["actions"]["can_export"])
        self.assertTrue(all(shot["ready"] for shot in snapshot["shots"]))

    def test_final_render_submission_is_idempotent(self):
        self._lock_all_inputs()
        with mock.patch.object(short_drama_assembly.threading.Thread, "start"):
            first = short_drama_assembly.start_final_render(
                self.db, "alice", self.project["id"],
                self.project["revision"] + 1, "same-render-request",
            )
            second = short_drama_assembly.start_final_render(
                self.db, "alice", self.project["id"],
                self.project["revision"] + 1, "same-render-request",
            )
        self.assertEqual(first["active_job"]["job_id"], second["active_job"]["job_id"])
        with closing(self.db()) as conn:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM short_drama_composition_jobs"
            ).fetchone()[0])
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM short_drama_composition_versions"
            ).fetchone()[0])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_clip_normalization_creates_exact_media_tracks(self):
        source = Path(self.tempdir.name) / "source.mp4"
        target = Path(self.tempdir.name) / "target.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ], check=True, timeout=30)
        short_drama_assembly._normalize_clip(source, target, 1, 108, 192)
        media = short_drama_assembly._probe(target)
        self.assertEqual((108, 192), (media["width"], media["height"]))
        self.assertEqual("h264", media["video_codec"])
        self.assertEqual("aac", media["audio_codec"])
        self.assertLess(abs(media["duration_ms"] - 1000), 150)

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


if __name__ == "__main__":
    unittest.main()
