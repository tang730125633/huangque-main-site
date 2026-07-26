import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import core, short_drama, short_drama_video, short_drama_voice, upstream_guard, video


def project_payload():
    return {
        "title": "视频版本测试",
        "synopsis": "验证短剧视频报价、任务、版本选择与阶段确认。",
        "ratio": "9:16",
        "target_duration": 30,
        "shot_count": 6,
        "visual_style": "电影写实",
        "point_budget": 1000,
    }


def plan():
    return {
        "characters": [],
        "script": {"title": "视频测试", "dialogue_lines": []},
        "shots": [{
            "key": "shot-%d" % (index + 1),
            "duration": 5,
            "scene_description": "场景 %d" % (index + 1),
            "camera_description": "自然跟拍",
            "character_keys": [],
            "dialogue_line_ids": [],
            "image_prompt": "统一人物与场景",
            "video_prompt": "自然呼吸、行走和眼神变化",
        } for index in range(6)],
    }


class ShortDramaVideoTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="short-drama-video-")
        self.path = Path(self.tempdir.name) / "content.db"
        self.db = lambda: sqlite3.connect(self.path, timeout=5)
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
        project = short_drama.create_project(self.db, "alice", project_payload())
        applied = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"], plan(),
            planning_cost=0, planning_job_id=501,
        )
        now = int(time.time())
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE short_drama_projects SET stage='video_review',"
                "revision=revision+1 WHERE id=?", (applied["id"],),
            )
            short_drama_voice.ensure_voice_workspace(
                conn, applied["id"], allowed_stages={"video_review"}
            )
            conn.execute(
                "UPDATE short_drama_voice_shots SET locked=1,audio_mode='native' "
                "WHERE project_id=?", (applied["id"],),
            )
            shots = conn.execute(
                "SELECT id FROM short_drama_shots WHERE project_id=? ORDER BY sort_order",
                (applied["id"],),
            ).fetchall()
            for index, shot in enumerate(shots):
                asset_id = "still-%d" % index
                conn.execute(
                    "INSERT INTO short_drama_assets "
                    "(id,project_id,shot_id,type,current_version,locked,created_at,updated_at) "
                    "VALUES (?,?,?,'still',1,1,?,?)",
                    (asset_id, applied["id"], shot["id"], now, now),
                )
                conn.execute(
                    "INSERT INTO short_drama_asset_versions "
                    "(id,asset_id,version,job_id,url,file,prompt,ratio,cost,status,created_at) "
                    "VALUES (?,?,1,?,?,?,?,?,0,'done',?)",
                    ("still-version-%d" % index, asset_id, 7000 + index,
                     "https://example.invalid/still-%d.jpg" % index,
                     "still-%d.jpg" % index, "consistent frame", "9:16", now),
                )
            self.shots = [dict(row) for row in shots]
            self.revision = conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?",
                (applied["id"],),
            ).fetchone()[0]
            conn.commit()
        self.project_id = applied["id"]

    def tearDown(self):
        self.tempdir.cleanup()

    def request(self, shot_id=None):
        return {
            "project_id": self.project_id,
            "revision": self.revision,
            "shot_id": shot_id or self.shots[0]["id"],
            "channel": "micro",
            "model": "seedance-test",
            "prompt": "人物自然转身，衣服和背景保持一致",
            "resolution": "480p",
            "upscale": True,
            "generate_audio": True,
        }

    @staticmethod
    def validated(payload):
        cleaned = dict(payload)
        cleaned.setdefault("model", "seedance-test")
        return cleaned

    def test_quote_job_version_lock_and_stage_handoff(self):
        with mock.patch.object(video, "validate_xiaole_video_payload", self.validated):
            quote = short_drama_video.prepare_quote(
                self.db, "alice", self.request(), lambda _kind, _payload: 25
            )
            submitted = dict(self.request(), quote_token=quote["quote_token"])
            prepared = short_drama_video.prepare_submission(
                self.db, "alice", submitted
            )
        now = int(time.time())
        result = {
            "video_file": "video/generated-1.mp4",
            "video_url": "https://example.invalid/generated-1.mp4",
            "model": "seedance-test",
            "resolution": "1080p",
        }
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "INSERT INTO jobs "
                "(id,kind,username,cost,status,payload,result,error,created_at,updated_at) "
                "VALUES (101,'xiaole_video','alice',25,'done',?,?, '',?,?)",
                (json.dumps(prepared["video_payload"]), json.dumps(result), now, now),
            )
            short_drama_video.record_submitted_job(
                conn, username="alice", prepared=prepared,
                idempotency_key="video-request-1", job_id=101,
            )
            conn.commit()
        patches = (
            mock.patch.object(video, "_user_owns_output_file", return_value=True),
            mock.patch.object(video, "_resolve_out_file", return_value=Path("/tmp/generated-1.mp4")),
            mock.patch.object(video, "get_video_job_phase", return_value="completed"),
        )
        with patches[0], patches[1], patches[2]:
            first = short_drama_video.get_workspace(self.db, "alice", self.project_id)
            second = short_drama_video.get_workspace(self.db, "alice", self.project_id)
            self.assertEqual(25, first["spent_points"])
            self.assertEqual(25, second["spent_points"])
            shot = first["shots"][0]
            self.assertEqual(1, len(shot["video"]["versions"]))
            self.assertNotIn("file", shot["video"]["versions"][0])
            selected = short_drama_video.select_version(self.db, "alice", {
                "project_id": self.project_id,
                "revision": first["revision"],
                "asset_id": shot["video"]["asset_id"],
                "version": 1,
                "lock": True,
            })
        self.revision = selected["revision"]
        with closing(self.db()) as conn:
            now += 1
            for index, source_shot in enumerate(self.shots[1:], start=1):
                asset_id = "video-%d" % index
                conn.execute(
                    "INSERT INTO short_drama_video_assets "
                    "(id,project_id,shot_id,current_version,locked,created_at,updated_at) "
                    "VALUES (?,?,?,1,1,?,?)",
                    (asset_id, self.project_id, source_shot["id"], now, now),
                )
                conn.execute(
                    "INSERT INTO short_drama_video_versions "
                    "(id,asset_id,version,job_id,channel,model,prompt,duration,ratio,"
                    "resolution,file,url,cost,created_at) VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?)",
                    ("video-version-%d" % index, asset_id, 8000 + index,
                     "micro", "seedance-test", "motion", 5, "9:16", "1080p",
                     "video/generated-%d.mp4" % index,
                     "https://example.invalid/generated-%d.mp4" % index, 0, now),
                )
            conn.commit()
        with mock.patch.object(video, "get_video_job_phase", return_value="completed"):
            ready = short_drama_video.get_workspace(self.db, "alice", self.project_id)
            self.assertTrue(ready["ready"])
            handed_off = short_drama_video.confirm_stage(self.db, "alice", {
                "project_id": self.project_id,
                "revision": ready["revision"],
                "stage": "video_review",
            })
        self.assertEqual("assembly_review", handed_off["stage"])

    def test_expired_quote_is_rejected_before_charge(self):
        with mock.patch.object(video, "validate_xiaole_video_payload", self.validated):
            quote = short_drama_video.prepare_quote(
                self.db, "alice", self.request(), lambda _kind, _payload: 25
            )
            with closing(self.db()) as conn:
                conn.execute(
                    "UPDATE short_drama_video_quotes SET expires_at=? WHERE token=?",
                    (int(time.time()) - 1, quote["quote_token"]),
                )
                conn.commit()
            with self.assertRaisesRegex(ValueError, "已过期"):
                short_drama_video.prepare_submission(
                    self.db, "alice",
                    dict(self.request(), quote_token=quote["quote_token"]),
                )

    def test_zero_point_budget_means_no_project_cap(self):
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET point_budget=0 WHERE id=?",
                (self.project_id,),
            )
            conn.commit()
        with mock.patch.object(video, "validate_xiaole_video_payload", self.validated):
            quote = short_drama_video.prepare_quote(
                self.db, "alice", self.request(), lambda _kind, _payload: 25
            )
        self.assertEqual(25, quote["cost"])

    def test_request_boundary_rejects_cross_project_and_bad_upscale(self):
        bad = self.request()
        bad["resolution"] = "720p"
        with self.assertRaisesRegex(ValueError, "必须先生成"):
            short_drama_video.normalize_request(bad)
        with mock.patch.object(video, "validate_xiaole_video_payload", self.validated):
            with self.assertRaises(LookupError):
                short_drama_video.prepare_quote(
                    self.db, "mallory", self.request(), lambda _kind, _payload: 25
                )


class ShortDramaVideoRouteTests(unittest.TestCase):
    class Points:
        class AuthPointsError(RuntimeError):
            def __init__(self, status, detail):
                self.status, self.detail, self.body = status, detail, {}
                super().__init__(detail)

        def __init__(self):
            self.charges = []

        def cost_of(self, kind, _payload):
            return 25 if kind == "xiaole_video" else 0

        def deduct_points(self, username, cost, _reason, transaction_key=""):
            self.charges.append((username, cost, transaction_key))
            return 1000 - sum(item[1] for item in self.charges)

        def refund_points(self, *_args, **_kwargs):
            return 1000

        def get_points(self, _username):
            return 1000 - sum(item[1] for item in self.charges)

        @staticmethod
        def public_error_body(error, cost):
            return {"detail": error.detail, "need": cost}

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="short-drama-video-route-")
        self.originals = {
            "JOB_DB": core.JOB_DB, "AUDIO_DB": core.AUDIO_DB,
            "verify": core.verify, "domains": core._domains,
            "feature_init": core.feature_flags.init_db,
            "feature_require": core.feature_flags.require_enabled,
            "security": core.miniprogram_security.check_payload,
            "upstream": upstream_guard.exhausted_reason,
            "enqueue": core.enqueue_job,
            "init_audio": core.init_audio_db,
            "validate": video.validate_xiaole_video_payload,
            "record": video.record_video_pending_asset,
        }
        core.JOB_DB = str(Path(self.tempdir.name) / "content.db")
        core.AUDIO_DB = str(Path(self.tempdir.name) / "audio.db")
        core.verify = lambda token: {"username": token, "must_change": False} if token else None
        self.points = self.Points()
        core._domains = lambda: (None, self.points, video)
        core.feature_flags.init_db = lambda: None
        core.feature_flags.require_enabled = lambda _kind: None
        core.miniprogram_security.check_payload = lambda _payload: None
        upstream_guard.exhausted_reason = lambda _kind, _payload: None
        core.enqueue_job = lambda *_args, **_kwargs: True
        core.init_audio_db = lambda: None
        video.validate_xiaole_video_payload = ShortDramaVideoTests.validated
        video.record_video_pending_asset = lambda *_args, **_kwargs: None
        core.init_db()
        project = short_drama.create_project(core.jdb, "alice", project_payload())
        project = short_drama.apply_plan(
            core.jdb, "alice", project["id"], project["revision"], plan(),
            planning_cost=0, planning_job_id=601,
        )
        now = int(time.time())
        with closing(core.jdb()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE short_drama_projects SET stage='video_review',revision=revision+1 "
                "WHERE id=?", (project["id"],),
            )
            shot = conn.execute(
                "SELECT id FROM short_drama_shots WHERE project_id=? ORDER BY sort_order LIMIT 1",
                (project["id"],),
            ).fetchone()
            conn.execute(
                "INSERT INTO short_drama_assets "
                "(id,project_id,shot_id,type,current_version,locked,created_at,updated_at) "
                "VALUES ('still-route',?,?,'still',1,1,?,?)",
                (project["id"], shot["id"], now, now),
            )
            conn.execute(
                "INSERT INTO short_drama_asset_versions "
                "(id,asset_id,version,job_id,url,file,prompt,ratio,cost,status,created_at) "
                "VALUES ('still-route-v1','still-route',1,7001,'https://example.invalid/still.jpg',"
                "'still.jpg','consistent','9:16',0,'done',?)", (now,),
            )
            self.revision = conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?", (project["id"],)
            ).fetchone()[0]
            conn.commit()
        self.project_id, self.shot_id = project["id"], shot["id"]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        core.JOB_DB, core.AUDIO_DB = self.originals["JOB_DB"], self.originals["AUDIO_DB"]
        core.verify, core._domains = self.originals["verify"], self.originals["domains"]
        core.feature_flags.init_db = self.originals["feature_init"]
        core.feature_flags.require_enabled = self.originals["feature_require"]
        core.miniprogram_security.check_payload = self.originals["security"]
        upstream_guard.exhausted_reason = self.originals["upstream"]
        core.enqueue_job = self.originals["enqueue"]
        core.init_audio_db = self.originals["init_audio"]
        video.validate_xiaole_video_payload = self.originals["validate"]
        video.record_video_pending_asset = self.originals["record"]
        self.tempdir.cleanup()

    def post(self, path, body, key=None):
        headers = {"Content-Type": "application/json", "Authorization": "Bearer alice"}
        if key: headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def video_request(self):
        return {
            "project_id": self.project_id, "revision": self.revision,
            "shot_id": self.shot_id, "channel": "micro", "model": "seedance-test",
            "prompt": "natural movement", "resolution": "480p",
            "upscale": True, "generate_audio": True,
        }

    def test_paid_route_replays_consumed_quote_without_double_charge(self):
        body = self.video_request()
        status, quote = self.post("/api/gen/short-drama/video-quote", body)
        self.assertEqual(200, status)
        submitted = dict(body, quote_token=quote["quote_token"])
        first_status, first = self.post(
            "/api/gen/short-drama/generate-video", submitted, "video-route-idem-1"
        )
        with closing(core.jdb()) as conn:
            conn.execute(
                "UPDATE submission_idempotency SET response_json=NULL "
                "WHERE username='alice' AND endpoint=? AND idem_key=?",
                ("/api/gen/short-drama/generate-video", "video-route-idem-1"),
            )
            conn.commit()
        replay_status, replay = self.post(
            "/api/gen/short-drama/generate-video", submitted, "video-route-idem-1"
        )
        self.assertEqual((200, 200), (first_status, replay_status))
        self.assertEqual(first["job_id"], replay["job_id"])
        self.assertEqual(1, len(self.points.charges))
        with closing(core.jdb()) as conn:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM short_drama_video_jobs"
            ).fetchone()[0])
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='xiaole_video'"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
