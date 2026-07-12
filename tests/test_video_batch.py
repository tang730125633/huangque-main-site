import base64
import json
import pathlib
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import video


def _data_url(seed):
    raw = b"\x89PNG\r\n\x1a\n" + seed.encode("ascii")
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


class VideoBatchValidationTests(unittest.TestCase):
    def _payload(self, avatars=None):
        return {
            "mode": "text",
            "text": "同一份口播文案",
            "voice": "voice-demo",
            "resolution": "1080p",
            "ratio": "9:16",
            "motion": "medium",
            "avatars": avatars or [
                {"image_data": _data_url("one"), "label": "形象一"},
                {"image_data": _data_url("two"), "label": "形象二"},
            ],
        }

    def test_batch_expands_common_settings_into_individual_video_jobs(self):
        items = video.validate_video_batch_payload(self._payload(), username="fang")
        self.assertEqual(2, len(items))
        self.assertEqual([1, 2], [item["batch_index"] for item in items])
        self.assertEqual([2, 2], [item["batch_size"] for item in items])
        self.assertEqual(["形象一", "形象二"], [item["batch_label"] for item in items])
        self.assertTrue(all(item["text"] == "同一份口播文案" for item in items))
        self.assertTrue(all(item["voice"] == "voice-demo" for item in items))

    def test_batch_accepts_owned_saved_avatars_without_embedding_images(self):
        avatars = [{"avatar_id": 11, "label": "门店主理人"}, {"avatar_id": 12, "label": "护理师"}]
        with patch.object(video, "get_video_avatar", return_value={"id": 11, "image_file": "image/avatar.jpg"}) as get_avatar:
            items = video.validate_video_batch_payload(self._payload(avatars), username="fang")
        self.assertEqual(["11", "12"], [item["avatar_id"] for item in items])
        self.assertTrue(all(not item["image_data"] for item in items))
        self.assertEqual([("fang", "11"), ("fang", "12")], [call.args for call in get_avatar.call_args_list])

    def test_batch_rejects_wrong_count_duplicate_or_non_text_mode(self):
        with self.assertRaisesRegex(ValueError, "至少选择 2"):
            video.validate_video_batch_payload(self._payload([{"image_data": _data_url("one")}]))
        with self.assertRaisesRegex(ValueError, "最多选择 3"):
            video.validate_video_batch_payload(self._payload([
                {"image_data": _data_url(str(i))} for i in range(4)
            ]), max_items=3)
        with self.assertRaisesRegex(ValueError, "不能重复"):
            image = _data_url("same")
            video.validate_video_batch_payload(self._payload([{"image_data": image}, {"image_data": image}]))
        payload = self._payload()
        payload["mode"] = "audio"
        with self.assertRaisesRegex(ValueError, "仅支持文案配音"):
            video.validate_video_batch_payload(payload)

    def test_text_and_avatar_ownership_are_checked_before_generation(self):
        payload = {"mode": "text", "avatar_id": "9", "voice": "v", "text": ""}
        with self.assertRaisesRegex(ValueError, "text 必填"):
            video.validate_video_payload(payload, username="fang")
        payload["text"] = "hello"
        with patch.object(video, "get_video_avatar", side_effect=ValueError("形象不存在")):
            with self.assertRaisesRegex(ValueError, "形象不存在"):
                video.validate_video_payload(payload, username="fang")

    def test_talking_job_can_reuse_owned_avatar_image(self):
        payload = {"_username": "fang", "_job_id": 8, "mode": "text", "avatar_id": "9",
                   "text": "hello", "voice": "v", "resolution": "1080p", "ratio": "9:16", "motion": "medium"}
        with patch.object(video, "HEYGEN_API_KEY", "configured"), \
                patch.object(video, "get_video_avatar", return_value={"id": 9, "image_file": "image/avatar.jpg"}), \
                patch.object(video, "gen_audio", return_value={"file": "audio/voice.mp3", "url": "/audio.mp3"}), \
                patch.object(video, "generate_heygen_video", return_value={"video_file": "video/out.mp4", "duration": 12}) as generate, \
                patch.object(video, "public_url", return_value="https://cdn.example/out.mp4"), \
                patch.object(video, "_file_url", side_effect=lambda value: "/api/gen/file/" + str(value or "")):
            result = video.gen_video(payload)
        generate.assert_called_once_with("image/avatar.jpg", "audio/voice.mp3", "1080p", "9:16", "medium")
        self.assertEqual(9, result["avatar_id"])


class VideoBatchIntegrationGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
        cls.html = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")

    def test_batch_route_checks_slots_before_deduct_and_enqueues_atomically(self):
        start = self.core.index('if p == "/api/gen/video/batch":')
        end = self.core.index('if p.startswith("/api/gen/")', start)
        route = self.core[start:end]
        self.assertLess(route.index("validate_video_batch_payload"), route.index("costs ="))
        self.assertLess(route.index("active_jobs + len(payloads)"), route.index("deduct_points"))
        self.assertIn('enqueue_jobs(job_ids, "video", "text")', route)
        self.assertIn('"available_slots"', route)

    def test_ui_submits_every_avatar_and_tracks_every_returned_job(self):
        for needle in ('data-talking-shape="batch"', 'id="batchImageFile"', "multiple hidden",
                       "body.avatars=talkingBatchItems.map", "fetch('/api/gen/video/batch'",
                       "jobs.forEach(function(job)", "batchId:res.data.batch_id", "resumeNextTrackedVideoTask"):
            self.assertIn(needle, self.html)

    def test_inline_javascript_parses_as_utf8(self):
        scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", self.html)
        self.assertTrue(scripts)
        checked = subprocess.run(["node", "--check", "-"], input=scripts[-1], text=True,
                                 encoding="utf-8", capture_output=True)
        self.assertEqual(0, checked.returncode, checked.stderr)

    def test_batch_http_route_accepts_all_jobs_and_rejects_slot_overflow_before_deduct(self):
        from content_domains import core

        class FakePointsError(Exception):
            def __init__(self, status, detail):
                self.status, self.detail = status, detail

        class FakePoints:
            AuthPointsError = FakePointsError

            def __init__(self):
                self.deductions = []

            def cost_of(self, kind, body):
                return 20

            def deduct_points(self, username, cost, reason):
                self.deductions.append((username, cost, reason))
                return 100 - cost

            def safe_refund_points(self, username, cost, reason):
                return 100

        originals = {
            "JOB_DB": core.JOB_DB, "AUDIO_DB": core.AUDIO_DB, "_domains": core._domains,
            "verify": core.verify, "require_enabled": core.feature_flags.require_enabled,
            "queue": core._talking_job_queue, "ids": core._queued_job_ids,
            "max_active": core.MAX_USER_ACTIVE_JOBS,
        }
        fake = FakePoints()
        server = None
        with tempfile.TemporaryDirectory() as td:
            core.JOB_DB = str(pathlib.Path(td) / "jobs.db")
            core.AUDIO_DB = str(pathlib.Path(td) / "assets.db")
            core.verify = lambda token: {"username": "fang", "must_change": False}
            core.feature_flags.require_enabled = lambda kind: None
            core._talking_job_queue = queue.Queue(maxsize=8)
            core._queued_job_ids = set()
            core.MAX_USER_ACTIVE_JOBS = 3
            try:
                with closing(sqlite3.connect(core.JOB_DB)) as db:
                    db.execute("""CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
                        status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,created_at INTEGER,updated_at INTEGER,
                        deleted INTEGER DEFAULT 0,refunded INTEGER DEFAULT 0,owner TEXT)""")
                    db.commit()
                core.init_audio_db()
                core._domains = lambda: (None, fake, video)
                server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                url = "http://127.0.0.1:%d/api/gen/video/batch" % server.server_address[1]
                data = json.dumps({
                    "mode": "text", "text": "batch", "voice": "voice-demo",
                    "avatars": [{"image_data": _data_url("http-one")}, {"image_data": _data_url("http-two")}],
                }).encode("utf-8")
                request = urllib.request.Request(url, data=data, method="POST", headers={
                    "Authorization": "Bearer test", "Content-Type": "application/json",
                })
                with urllib.request.urlopen(request, timeout=5) as response:
                    accepted = json.loads(response.read())
                self.assertEqual(2, accepted["count"])
                self.assertEqual(40, accepted["cost"])
                self.assertEqual([("fang", 40, "job:video_batch")], fake.deductions)
                with closing(core.jdb()) as db:
                    rows = db.execute("SELECT status,cost,payload FROM jobs ORDER BY id").fetchall()
                self.assertEqual(["pending", "pending"], [row["status"] for row in rows])
                self.assertEqual([20, 20], [row["cost"] for row in rows])
                self.assertEqual(2, core._talking_job_queue.qsize())

                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(429, rejected.exception.code)
                self.assertEqual([("fang", 40, "job:video_batch")], fake.deductions)
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                core.JOB_DB, core.AUDIO_DB = originals["JOB_DB"], originals["AUDIO_DB"]
                core._domains, core.verify = originals["_domains"], originals["verify"]
                core.feature_flags.require_enabled = originals["require_enabled"]
                core._talking_job_queue, core._queued_job_ids = originals["queue"], originals["ids"]
                core.MAX_USER_ACTIVE_JOBS = originals["max_active"]


class VideoSingleRouteSubLimitTests(unittest.TestCase):
    def test_single_video_routes_use_kind_specific_caps_before_deduct(self):
        from content_domains import core

        class FakePointsError(Exception):
            def __init__(self, status, detail):
                self.status, self.detail = status, detail

        class FakePoints:
            AuthPointsError = FakePointsError

            def __init__(self):
                self.deductions = []

            def cost_of(self, kind, body):
                return 20

            def deduct_points(self, username, cost, reason):
                self.deductions.append((username, cost, reason))
                return 100 - cost

            def safe_refund_points(self, username, cost, reason):
                return 100

        originals = {
            "JOB_DB": core.JOB_DB,
            "AUDIO_DB": core.AUDIO_DB,
            "_domains": core._domains,
            "verify": core.verify,
            "require_enabled": core.feature_flags.require_enabled,
            "max_active": core.MAX_USER_ACTIVE_JOBS,
            "max_xiaole": core.MAX_USER_ACTIVE_XIAOLE_VIDEO,
            "max_motion": core.MAX_USER_ACTIVE_MOTION,
            "max_tryon": core.MAX_USER_ACTIVE_TRYON,
            "handlers": core.HANDLERS,
            "validate_video": video.validate_video_payload,
            "validate_tryon": video.validate_tryon_payload,
            "validate_xiaole": video.validate_xiaole_video_payload,
        }
        fake = FakePoints()
        server = None
        with tempfile.TemporaryDirectory() as td:
            core.JOB_DB = str(pathlib.Path(td) / "jobs.db")
            core.AUDIO_DB = str(pathlib.Path(td) / "assets.db")
            core.verify = lambda token: {"username": "fang", "must_change": False}
            core.feature_flags.require_enabled = lambda kind: None
            core.MAX_USER_ACTIVE_JOBS = 5
            core.MAX_USER_ACTIVE_XIAOLE_VIDEO = 3
            core.MAX_USER_ACTIVE_MOTION = 2
            core.MAX_USER_ACTIVE_TRYON = 1
            core.HANDLERS = {"video": lambda body: body, "tryon": lambda body: body, "xiaole_video": lambda body: body}
            video.validate_video_payload = lambda body, username: body
            video.validate_tryon_payload = lambda body: body
            video.validate_xiaole_video_payload = lambda body: body
            try:
                with closing(sqlite3.connect(core.JOB_DB)) as db:
                    db.execute("""CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
                        status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,created_at INTEGER,updated_at INTEGER,
                        deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0)""")
                    db.commit()
                core.init_audio_db()
                core._domains = lambda: (None, fake, video)
                server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = "http://127.0.0.1:%d" % server.server_address[1]

                cases = [
                    {
                        "seed": [
                            ("xiaole_video", "pending", '{"channel":"omni"}'),
                            ("xiaole_video", "running", '{"channel":"grok"}'),
                            ("xiaole_video", "pending", '{"channel":"micro"}'),
                        ],
                        "path": "/api/gen/xiaole_video",
                        "body": {"channel": "omni", "prompt": "商品展示"},
                        "detail": "当前果肉/豆姐/欧米视频最多同时排队或生成 3 个任务，请等待部分完成后再继续",
                        "code": "xiaole_active_cap",
                    },
                    {
                        "seed": [
                            ("video", "pending", '{"mode":"motion"}'),
                            ("video", "running", '{"mode":"motion"}'),
                        ],
                        "path": "/api/gen/video",
                        # 「影视级模仿」已更名为「动作模仿」（去线路化：原线路一 HeyGen 的能力
                        # 拆成了独立的「AI 剧情视频」，这边只留 WaveSpeed）
                        "body": {"mode": "motion", "text": "动作模仿"},
                        "detail": "当前动作模仿最多同时排队或生成 2 个任务，请等待部分完成后再继续",
                        "code": "motion_active_cap",
                    },
                    {
                        "seed": [
                            ("tryon", "running", '{"line":"2"}'),
                        ],
                        "path": "/api/gen/tryon",
                        "body": {"line": "2", "text": "换装"},
                        "detail": "当前换装视频最多同时排队或生成 1 个任务，请等待任务完成后再继续",
                        "code": "tryon_active_cap",
                    },
                ]

                for case in cases:
                    with self.subTest(path=case["path"]):
                        with closing(core.jdb()) as db:
                            db.execute("DELETE FROM jobs")
                            for idx, (kind, status, payload) in enumerate(case["seed"], start=1):
                                db.execute("INSERT INTO jobs(id,kind,username,cost,status,payload,created_at,updated_at,deleted,refunded) VALUES(?,?,?,?,?,?,?,?,0,0)",
                                           (idx, kind, "fang", 20, status, payload, 1, 1))
                            db.commit()
                        before = list(fake.deductions)
                        req = urllib.request.Request(base + case["path"], data=json.dumps(case["body"]).encode("utf-8"), method="POST", headers={
                            "Authorization": "Bearer test", "Content-Type": "application/json",
                        })
                        with self.assertRaises(urllib.error.HTTPError) as rejected:
                            urllib.request.urlopen(req, timeout=5)
                        self.assertEqual(429, rejected.exception.code)
                        payload = json.loads(rejected.exception.read().decode("utf-8"))
                        self.assertEqual(case["detail"], payload["detail"])
                        self.assertEqual(case["code"], payload["code"])
                        self.assertEqual(before, fake.deductions)

                with urllib.request.urlopen(base + "/api/gen/health", timeout=5) as response:
                    health = json.loads(response.read())
                self.assertEqual(3, health["max_user_active_xiaole_video"])
                self.assertEqual(2, health["max_user_active_motion"])
                self.assertEqual(1, health["max_user_active_tryon"])
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                core.JOB_DB = originals["JOB_DB"]
                core.AUDIO_DB = originals["AUDIO_DB"]
                core._domains = originals["_domains"]
                core.verify = originals["verify"]
                core.feature_flags.require_enabled = originals["require_enabled"]
                core.MAX_USER_ACTIVE_JOBS = originals["max_active"]
                core.MAX_USER_ACTIVE_XIAOLE_VIDEO = originals["max_xiaole"]
                core.MAX_USER_ACTIVE_MOTION = originals["max_motion"]
                core.MAX_USER_ACTIVE_TRYON = originals["max_tryon"]
                core.HANDLERS = originals["handlers"]
                video.validate_video_payload = originals["validate_video"]
                video.validate_tryon_payload = originals["validate_tryon"]
                video.validate_xiaole_video_payload = originals["validate_xiaole"]


if __name__ == "__main__":
    unittest.main()
