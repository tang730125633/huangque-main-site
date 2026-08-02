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

    def test_audio_mode_accepts_owned_audio_file_and_normalizes_relative_path(self):
        payload = {"mode": "audio", "image_data": _data_url("hero"), "audio_file": "voice.mp3"}
        audio_fp = video.OUT_DIR / "audio" / "voice.mp3"
        with patch.object(video, "_resolve_out_file", return_value=audio_fp), \
                patch.object(video, "_user_owns_output_file", return_value=True):
            cleaned = video.validate_video_payload(payload, username="fang")
        self.assertEqual("audio/voice.mp3", cleaned["audio_file"])

    def test_audio_mode_rejects_unowned_or_unsupported_audio_file(self):
        payload = {"mode": "audio", "image_data": _data_url("hero"), "audio_file": "audio/voice.mp3"}
        audio_fp = video.OUT_DIR / "audio" / "voice.mp3"
        with patch.object(video, "_resolve_out_file", return_value=audio_fp), \
                patch.object(video, "_user_owns_output_file", return_value=False):
            with self.assertRaisesRegex(ValueError, "不属于当前账号"):
                video.validate_video_payload(payload, username="fang")
        bad_fp = video.OUT_DIR / "video" / "voice.txt"
        with patch.object(video, "_resolve_out_file", return_value=bad_fp):
            with self.assertRaisesRegex(ValueError, "仅支持 mp3、wav、m4a"):
                video.validate_video_payload(payload, username=None)

    def test_audio_job_can_reuse_owned_audio_file_without_resaving(self):
        payload = {"_username": "fang", "_job_id": 8, "mode": "audio", "image_data": _data_url("hero"),
                   "audio_file": "audio/voice.mp3", "resolution": "1080p", "ratio": "9:16", "motion": "medium"}
        save_calls = []
        def fake_save(data_url, prefix, allowed_ext):
            save_calls.append(prefix)
            if prefix == "vid_img":
                return "image/avatar.jpg"
            raise AssertionError("audio_file 已复用时不应再次落盘音频")
        with patch.object(video, "HEYGEN_API_KEY", "configured"), \
                patch.object(video, "_save_data_file", side_effect=fake_save), \
                patch.object(video, "_resolve_out_file", return_value=video.OUT_DIR / "audio" / "voice.mp3"), \
                patch.object(video, "_user_owns_output_file", return_value=True), \
                patch.object(video, "generate_heygen_video", return_value={"video_file": "video/out.mp4", "duration": 12}) as generate, \
                patch.object(video, "public_url", return_value="https://cdn.example/out.mp4"), \
                patch.object(video, "_file_url", side_effect=lambda value: "/api/gen/file/" + str(value or "")):
            result = video.gen_video(payload)
        self.assertEqual(["vid_img"], save_calls)
        generate.assert_called_once_with("image/avatar.jpg", "audio/voice.mp3", "1080p", "9:16", "medium")
        self.assertEqual("audio/voice.mp3", result["audio_file"])
        self.assertEqual("/api/gen/file/audio/voice.mp3", result["audio_url"])

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
                self.refunds = []

            def cost_of(self, kind, body):
                return 20

            def deduct_points(self, username, cost, reason):
                self.deductions.append((username, cost, reason))
                return 100 - cost

            def safe_refund_points(self, username, cost, reason):
                return 100

            def refund_points(self, username, cost, reason, transaction_key=""):
                self.refunds.append((username, cost, reason, transaction_key))
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
                    "Idempotency-Key": "batch-submit-001",
                })
                with urllib.request.urlopen(request, timeout=5) as response:
                    accepted = json.loads(response.read())
                self.assertEqual(2, accepted["count"])
                self.assertEqual(40, accepted["cost"])
                self.assertEqual(("fang", 40), fake.deductions[0][:2])
                self.assertTrue(fake.deductions[0][2].startswith("job:video_batch submit:"))
                with closing(core.jdb()) as db:
                    rows = db.execute("SELECT status,cost,payload FROM jobs ORDER BY id").fetchall()
                self.assertEqual(["pending", "pending"], [row["status"] for row in rows])
                self.assertEqual([20, 20], [row["cost"] for row in rows])
                self.assertEqual(2, core._talking_job_queue.qsize())

                with urllib.request.urlopen(request, timeout=5) as response:
                    replayed = json.loads(response.read())
                self.assertEqual(accepted, replayed)
                self.assertEqual(1, len(fake.deductions))
                with closing(core.jdb()) as db:
                    self.assertEqual(2, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

                changed = urllib.request.Request(url, data=data.replace(b'"batch"', b'"changed"'), method="POST", headers={
                    "Authorization": "Bearer test", "Content-Type": "application/json",
                    "Idempotency-Key": "batch-submit-001",
                })
                with self.assertRaises(urllib.error.HTTPError) as conflict:
                    urllib.request.urlopen(changed, timeout=5)
                self.assertEqual(409, conflict.exception.code)
                self.assertEqual("idempotency_conflict", json.loads(conflict.exception.read())["code"])
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

            def refund_points(self, username, cost, reason, transaction_key=""):
                return 100

        originals = {
            "JOB_DB": core.JOB_DB,
            "AUDIO_DB": core.AUDIO_DB,
            "_domains": core._domains,
            "verify": core.verify,
            "require_enabled": core.feature_flags.require_enabled,
            "max_active": core.MAX_USER_ACTIVE_JOBS,
            "max_xiaole": core.MAX_USER_ACTIVE_XIAOLE_VIDEO,
            "max_tryon": core.MAX_USER_ACTIVE_TRYON,
            "handlers": core.HANDLERS,
            "enqueue": core.enqueue_job,
            "validate_video": video.validate_video_payload,
            "validate_tryon": video.validate_tryon_payload,
            "validate_xiaole": video.validate_xiaole_video_payload,
            "record_pending": video.record_video_pending_asset,
        }
        fake = FakePoints()
        server = None
        with tempfile.TemporaryDirectory() as td:
            core.JOB_DB = str(pathlib.Path(td) / "jobs.db")
            core.AUDIO_DB = str(pathlib.Path(td) / "assets.db")
            core.verify = lambda token: {"username": "fang", "must_change": False}
            core.feature_flags.require_enabled = lambda kind: None
            core.MAX_USER_ACTIVE_JOBS = 5
            core.MAX_USER_ACTIVE_XIAOLE_VIDEO = 2
            core.MAX_USER_ACTIVE_TRYON = 1
            core.HANDLERS = {"video": lambda body: body, "tryon": lambda body: body, "xiaole_video": lambda body: body}
            video.validate_video_payload = lambda body, username: body
            video.validate_tryon_payload = lambda body: body
            video.validate_xiaole_video_payload = lambda body, username=None: body
            try:
                with closing(sqlite3.connect(core.JOB_DB)) as db:
                    db.execute("""CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
                        status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,created_at INTEGER,updated_at INTEGER,
                        deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0, owner TEXT)""")
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
                        ],
                        "path": "/api/gen/xiaole_video",
                        "body": {"channel": "micro", "prompt": "商品展示"},
                        "detail": "当前果肉/Seedance/Omni 视频最多同时排队或生成 2 个任务，请等待部分完成后再继续",
                        "code": "xiaole_active_cap",
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
                            "Idempotency-Key": "video-cap-" + case["path"].rsplit("/", 1)[-1],
                        })
                        with self.assertRaises(urllib.error.HTTPError) as rejected:
                            urllib.request.urlopen(req, timeout=5)
                        self.assertEqual(429, rejected.exception.code)
                        payload = json.loads(rejected.exception.read().decode("utf-8"))
                        self.assertEqual(case["detail"], payload["detail"])
                        self.assertEqual(case["code"], payload["code"])
                        self.assertEqual(before, fake.deductions)

                enqueued = []
                core.enqueue_job = lambda *args: (enqueued.append(args), True)[1]
                video.record_video_pending_asset = lambda *args: (_ for _ in ()).throw(RuntimeError("asset db locked"))
                request = urllib.request.Request(base + "/api/gen/video", data=json.dumps({
                    "mode": "text", "text": "商品口播", "voice": "demo",
                }).encode("utf-8"), method="POST", headers={
                    "Authorization": "Bearer test", "Content-Type": "application/json",
                })
                with self.assertRaises(urllib.error.HTTPError) as failed:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(500, failed.exception.code)
                with closing(core.jdb()) as db:
                    row = db.execute("SELECT status,refunded FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(("error", 1), (row["status"], row["refunded"]))
                self.assertEqual([], enqueued, "资产登记失败的付费任务绝不能继续入队")

                with urllib.request.urlopen(base + "/api/gen/health", timeout=5) as response:
                    health = json.loads(response.read())
                self.assertEqual(core.JOB_QUEUE_MAX, health["job_queue_max"])
                self.assertEqual(core.TALKING_JOB_QUEUE_MAX, health["talking_job_queue_max"])
                self.assertEqual(2, health["max_user_active_xiaole_video"])
                self.assertEqual(1, health["max_user_active_tryon"])
                self.assertIn("reverse_remake_video_channel", health)
                self.assertIn("reverse_remake_video_offer", health)
                self.assertEqual(
                    health["reverse_remake_video_channel"],
                    health["reverse_remake_video_offer"]["channel"],
                )
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
                core.MAX_USER_ACTIVE_TRYON = originals["max_tryon"]
                core.HANDLERS = originals["handlers"]
                core.enqueue_job = originals["enqueue"]
                video.validate_video_payload = originals["validate_video"]
                video.validate_tryon_payload = originals["validate_tryon"]
                video.validate_xiaole_video_payload = originals["validate_xiaole"]
                video.record_video_pending_asset = originals["record_pending"]



def _real_png_data_url(seed=0):
    import io as _io
    from PIL import Image
    buf = _io.BytesIO()
    Image.new("RGB", (8, 8), (seed % 256, 128, 64)).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


class SeedanceReferenceOrderingTests(unittest.TestCase):
    """评审探针：COS 转存必须发生在幂等/任务上限/余额资格检查之后、扣点之前，
    且所有后续失败都要清理本批已上传对象。COS SDK 全程 mock，不联网。"""

    def test_reference_upload_runs_after_eligibility_and_before_deduct(self):
        from content_domains import core, cos

        class FakePointsError(Exception):
            def __init__(self, status, detail):
                self.status, self.detail = status, detail

        class FakePoints:
            AuthPointsError = FakePointsError

            def __init__(self):
                self.deductions = []
                self.refunds = []
                self.balance = 100
                self.deduct_error = None
                self.transaction = None
                self.get_point_calls = 0

            def cost_of(self, kind, body):
                return 20

            def get_points(self, username):
                self.get_point_calls += 1
                return self.balance

            def deduct_points(self, username, cost, reason, transaction_key=""):
                if self.deduct_error:
                    raise self.deduct_error
                self.deductions.append((username, cost, reason, transaction_key))
                return self.balance - cost

            def safe_refund_points(self, username, cost, reason):
                self.refunds.append((username, cost, reason))
                return self.balance

            def refund_points(self, username, cost, reason, transaction_key=""):
                self.refunds.append((username, cost, reason, transaction_key))
                return self.balance

            def get_points_transaction(self, transaction_key):
                return self.transaction

            def public_error_body(self, error, need):
                return {"detail": error.detail, "need": need}

        signed_url = "https://bucket-1250000000.cos.ap-guangzhou.myqcloud.com/seedance/reference/x?q-sign-algorithm=sha1"
        flags = {"upload_open": True, "enqueue_ok": True}
        fake = FakePoints()
        originals = {
            "JOB_DB": core.JOB_DB, "AUDIO_DB": core.AUDIO_DB, "_domains": core._domains,
            "verify": core.verify, "require_enabled": core.feature_flags.require_enabled,
            "is_enabled": core.feature_flags.is_enabled,
            "max_active": core.MAX_USER_ACTIVE_JOBS,
            "max_xiaole": core.MAX_USER_ACTIVE_XIAOLE_VIDEO,
            "handlers": core.HANDLERS, "enqueue_job": core.enqueue_job,
            "seedance_probe": video.seedance_video_is_open,
            "upload_probe": video.seedance_reference_upload_is_open,
            "presign": video._seedance_cos_presign,
            "cos_delete": video._seedance_cos_delete,
            "cos_enabled": cos.enabled, "cos_put": cos.put_bytes,
            "cleanup_ready": video._cleanup_table_ready,
        }
        server = None
        from content_domains import video_seedance
        originals["seedance_available"] = video_seedance.available
        with tempfile.TemporaryDirectory() as td:
            core.JOB_DB = str(pathlib.Path(td) / "jobs.db")
            core.AUDIO_DB = str(pathlib.Path(td) / "assets.db")
            video._cleanup_table_ready = False
            core.verify = lambda token: {
                "username": "fang", "must_change": False, "points": fake.balance
            }
            core.feature_flags.require_enabled = lambda kind: None
            core.feature_flags.is_enabled = lambda kind: True
            core.MAX_USER_ACTIVE_JOBS = 5
            core.MAX_USER_ACTIVE_XIAOLE_VIDEO = 3
            core.HANDLERS = {"xiaole_video": lambda body: body}
            core.enqueue_job = lambda jid, kind=None, mode=None: flags["enqueue_ok"]
            video.seedance_video_is_open = lambda: True
            video_seedance.available = lambda: True
            video.seedance_reference_upload_is_open = lambda: flags["upload_open"]
            video._seedance_cos_presign = lambda key, expire=video.SEEDANCE_REFERENCE_SIGN_EXPIRE: signed_url
            cos.enabled = lambda: True
            put_calls = []
            delete_calls = []
            def fake_put(data, key, content_type=None, private=False):
                put_calls.append({"key": key, "private": private, "content_type": content_type,
                                  "lock_held": core._submission_lock.locked()})
                return signed_url
            def fake_delete(key):
                delete_calls.append(key)
            cos.put_bytes = fake_put
            video._seedance_cos_delete = fake_delete
            try:
                with closing(sqlite3.connect(core.JOB_DB)) as db:
                    db.execute("""CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
                        status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,created_at INTEGER,updated_at INTEGER,
                        deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0, owner TEXT)""")
                    db.commit()
                core.init_audio_db()
                core._domains = lambda: (None, fake, video)
                server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = "http://127.0.0.1:%d" % server.server_address[1]

                request_seq = [0]

                def post(body, idem=None):
                    headers = {"Authorization": "Bearer test", "Content-Type": "application/json"}
                    if idem is None and body.get("channel") == "micro":
                        request_seq[0] += 1
                        idem = "seedance-probe-%04d" % request_seq[0]
                    if idem:
                        headers["Idempotency-Key"] = idem
                    req = urllib.request.Request(base + "/api/gen/xiaole_video",
                                                 data=json.dumps(body).encode("utf-8"),
                                                 method="POST", headers=headers)
                    try:
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            return resp.status, json.loads(resp.read())
                    except urllib.error.HTTPError as e:
                        return e.code, json.loads(e.read() or b"{}")

                def reset(seed_rows=()):
                    from content_domains import submission_idempotency
                    with closing(core.jdb()) as db:
                        db.execute("DELETE FROM jobs")
                        submission_idempotency.ensure_table(db)
                        db.execute("DELETE FROM submission_idempotency")
                        for idx, (kind, status, payload) in enumerate(seed_rows, start=1):
                            db.execute("INSERT INTO jobs(id,kind,username,cost,status,payload,created_at,updated_at,deleted,refunded) VALUES(?,?,?,?,?,?,?,1,0,0)",
                                       (idx, kind, "fang", 20, status, payload, 1))
                        db.commit()
                    put_calls.clear()
                    delete_calls.clear()
                    fake.deductions.clear()
                    fake.refunds.clear()
                    fake.balance = 100
                    fake.deduct_error = None
                    fake.transaction = None
                    fake.get_point_calls = 0
                    core._shutting_down.clear()
                    flags["upload_open"] = True
                    flags["enqueue_ok"] = True
                    core.MAX_USER_ACTIVE_JOBS = 5
                    core.MAX_USER_ACTIVE_XIAOLE_VIDEO = 3

                micro_body = {"channel": "micro", "prompt": "demo", "duration": 5,
                              "reference_images": [_real_png_data_url(1)]}

                # 资格全过：上传在扣点前完成，私有 ACL，payload 只存 cos-key:// 键不存签名 URL
                reset()
                status, resp = post(micro_body)
                self.assertEqual(200, status)
                self.assertEqual(1, len(put_calls))
                self.assertIs(put_calls[0]["private"], True)
                self.assertIs(put_calls[0]["lock_held"], False)   # COS 网络上传不得持有全局提交锁
                self.assertEqual(1, len(fake.deductions))
                self.assertEqual(0, fake.get_point_calls)
                with closing(core.jdb()) as db:
                    row = db.execute("SELECT payload FROM jobs").fetchone()
                    lifecycle = db.execute(
                        "SELECT state,job_id FROM seedance_pending_cleanup WHERE key=?",
                        (put_calls[0]["key"],),
                    ).fetchone()
                self.assertIn("cos-key://" + put_calls[0]["key"], row["payload"])
                self.assertEqual(("linked", resp["job_id"]), tuple(lifecycle))
                self.assertNotIn("data:image", row["payload"])
                self.assertNotIn("q-sign-algorithm", row["payload"])   # 提交时不签名，签名推迟到 worker
                self.assertEqual(0, video.retry_pending_seedance_cleanups())

                # 终态清理：job 进 done 后删除本次暂存对象；重复 CAS 不重复清理
                self.assertEqual([], delete_calls)
                self.assertTrue(core._set_terminal(resp["job_id"], "done", result={"url": "x"}, from_states=("pending", "running")))
                self.assertEqual([put_calls[0]["key"]], delete_calls)
                self.assertFalse(core._set_terminal(resp["job_id"], "done", result={"url": "y"}, from_states=("pending", "running")))
                self.assertEqual(1, len(delete_calls))

                # P0 入口：客户端注入的 _seedance_staged_keys 不得随 payload 入库
                reset()
                status, resp = post(dict(micro_body, _seedance_staged_keys=["seedance/reference/0000000000000000/evil.png"]))
                self.assertEqual(200, status)
                with closing(core.jdb()) as db:
                    row = db.execute("SELECT payload FROM jobs").fetchone()
                self.assertNotIn("evil.png", row["payload"])
                self.assertIn(put_calls[0]["key"], row["payload"])   # 只剩服务端自己生成的键

                # P0 出口：恶意构造的 payload 行（他人前缀/任意键）→ 终态清理删除调用数为 0
                reset()
                with closing(core.jdb()) as db:
                    db.execute("INSERT INTO jobs(id,kind,username,cost,status,payload,created_at,updated_at,deleted,refunded) VALUES(1,'xiaole_video','fang',20,'done',?,1,1,0,0)",
                               (json.dumps({"_seedance_staged_keys": ["seedance/reference/0000000000000000/evil.png", "anything/at/all.png"]}),))
                    db.commit()
                video.cleanup_job_staged_seedance_references(1)
                self.assertEqual([], delete_calls)

                # 余额不足：资格预检 402，COS 上传调用数为 0，不扣点不建任务
                reset()
                fake.balance = 0
                status, resp = post(micro_body)
                self.assertEqual(402, status)
                self.assertEqual([], put_calls)
                self.assertEqual([], fake.deductions)
                with closing(core.jdb()) as db:
                    self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

                # 全局任务上限已满：429，COS 上传调用数为 0
                reset(seed_rows=[("video", "running", "{}")])
                core.MAX_USER_ACTIVE_JOBS = 1
                status, resp = post(micro_body)
                self.assertEqual(429, status)
                self.assertEqual("active_job_cap", resp["code"])
                self.assertEqual([], put_calls)

                # 果肉/豆姐/欧米渠道上限已满：429，COS 上传调用数为 0
                reset(seed_rows=[("xiaole_video", "pending", '{"channel":"micro"}')])
                core.MAX_USER_ACTIVE_XIAOLE_VIDEO = 1
                status, resp = post(micro_body)
                self.assertEqual(429, status)
                self.assertEqual("xiaole_active_cap", resp["code"])
                self.assertEqual([], put_calls)

                # 幂等冲突：同 Key 不同请求体 409，COS 上传调用数为 0
                reset()
                status, resp = post({"channel": "micro", "prompt": "first", "duration": 5}, idem="probe-key-0001")
                self.assertEqual(200, status)
                self.assertEqual([], put_calls)   # 首个请求无参考图，本就不上传
                status, resp = post(micro_body, idem="probe-key-0001")
                self.assertEqual(409, status)
                self.assertEqual("idempotency_conflict", resp["code"])
                self.assertEqual([], put_calls)
                self.assertEqual(1, len(fake.deductions))   # 冲突请求没有二次扣点

                # 上传服务未配置：503 seedance_reference_upload_unavailable，扣点/建任务为 0
                reset()
                flags["upload_open"] = False
                status, resp = post(micro_body)
                self.assertEqual(503, status)
                self.assertEqual("seedance_reference_upload_unavailable", resp["code"])
                self.assertEqual([], put_calls)
                self.assertEqual([], fake.deductions)
                with closing(core.jdb()) as db:
                    self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

                # 扣点结果未知（上传后）：先保留对象，流水确认未扣点后再清理。
                reset()
                fake.deduct_error = FakePointsError(502, "点数接口不可用")
                status, resp = post(micro_body)
                self.assertEqual(502, status)
                self.assertEqual(1, len(put_calls))
                self.assertEqual([], delete_calls)
                with closing(core.jdb()) as db:
                    self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
                    self.assertEqual("charging", db.execute(
                        "SELECT state FROM seedance_staging_attempts").fetchone()[0])
                    db.execute("UPDATE seedance_staging_attempts SET updated_at=0")
                    db.commit()
                video.retry_pending_seedance_cleanups(points_domain=fake)
                self.assertEqual([put_calls[0]["key"]], delete_calls)

                # 入队失败（扣点后）：已上传对象必须被清理，点数退回
                reset()
                flags["enqueue_ok"] = False
                status, resp = post(micro_body)
                self.assertEqual(429, status)
                self.assertEqual("queue_full", resp["code"])
                self.assertEqual(1, len(put_calls))
                self.assertEqual([put_calls[0]["key"]], delete_calls)
                self.assertEqual(1, len(fake.refunds))

                # SIGTERM during COS upload: clean the object and stop before deduct/job creation.
                reset()
                def shutdown_during_put(data, key, content_type=None, private=False):
                    put_calls.append({"key": key, "lock_held": core._submission_lock.locked()})
                    core._shutting_down.set()
                    return signed_url
                cos.put_bytes = shutdown_during_put
                status, resp = post(micro_body)
                cos.put_bytes = fake_put
                core._shutting_down.clear()
                self.assertEqual(503, status)
                self.assertEqual("shutting_down", resp["code"])
                self.assertEqual([put_calls[0]["key"]], delete_calls)
                self.assertEqual([], fake.deductions)
                with closing(core.jdb()) as db:
                    self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

                # 并行提交：两个上传必须并发发生（证明上传不在全局提交锁内），且互不阻塞、各自成功
                reset()
                barrier = threading.Barrier(2)
                def blocking_put(data, key, content_type=None, private=False):
                    put_calls.append({"key": key, "lock_held": core._submission_lock.locked()})
                    barrier.wait(timeout=20)   # 上传若持锁，另一方到不了这里 → BrokenBarrierError → 503
                    return signed_url
                cos.put_bytes = blocking_put
                results = {}
                def worker(tag):
                    try:
                        results[tag] = post({"channel": "micro", "prompt": "demo", "duration": 5,
                                             "reference_images": [_real_png_data_url(tag)]})[0]
                    except Exception as exc:
                        results[tag] = repr(exc)
                threads = [threading.Thread(target=worker, args=(tag,)) for tag in (1, 2)]
                for t in threads: t.start()
                for t in threads: t.join(timeout=30)
                cos.put_bytes = fake_put
                self.assertEqual({1: 200, 2: 200}, results)
                self.assertEqual(2, len(put_calls))
                self.assertEqual(2, len({call["key"] for call in put_calls}))   # 跨提交对象键不共享
                self.assertEqual(2, len(fake.deductions))
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                core.JOB_DB = originals["JOB_DB"]
                core.AUDIO_DB = originals["AUDIO_DB"]
                core._domains = originals["_domains"]
                core.verify = originals["verify"]
                core.feature_flags.require_enabled = originals["require_enabled"]
                core.feature_flags.is_enabled = originals["is_enabled"]
                core.MAX_USER_ACTIVE_JOBS = originals["max_active"]
                core.MAX_USER_ACTIVE_XIAOLE_VIDEO = originals["max_xiaole"]
                core.HANDLERS = originals["handlers"]
                core.enqueue_job = originals["enqueue_job"]
                video.seedance_video_is_open = originals["seedance_probe"]
                video_seedance.available = originals["seedance_available"]
                video.seedance_reference_upload_is_open = originals["upload_probe"]
                video._seedance_cos_presign = originals["presign"]
                video._seedance_cos_delete = originals["cos_delete"]
                cos.enabled = originals["cos_enabled"]
                cos.put_bytes = originals["cos_put"]
                video._cleanup_table_ready = originals["cleanup_ready"]


class ReverseRemakeChannelTests(unittest.TestCase):
    def test_reverse_remake_prefers_seedance_then_grok_and_fails_closed(self):
        from content_domains import core

        with patch.object(video, "seedance_video_health_enabled", return_value=True), \
                patch.object(video, "seedance_reference_upload_is_open", return_value=True), \
                patch.object(video, "grok_video_is_open", return_value=True), \
                patch.object(video, "grok_reference_upload_is_open", return_value=True):
            self.assertEqual("micro", video.reverse_remake_video_channel(core.feature_flags))
        with patch.object(video, "seedance_video_health_enabled", return_value=False), \
                patch.object(video, "seedance_reference_upload_is_open", return_value=True), \
                patch.object(video, "grok_video_is_open", return_value=True), \
                patch.object(video, "grok_reference_upload_is_open", return_value=True):
            self.assertEqual("grok", video.reverse_remake_video_channel(core.feature_flags))
        with patch.object(video, "seedance_video_health_enabled", return_value=True), \
                patch.object(video, "seedance_reference_upload_is_open", return_value=False), \
                patch.object(video, "grok_video_is_open", return_value=True), \
                patch.object(video, "grok_reference_upload_is_open", return_value=False):
            self.assertEqual("", video.reverse_remake_video_channel(core.feature_flags))

    def test_legacy_xiaole_provider_is_never_advertised_for_reverse_frames(self):
        from content_domains import core

        with patch.object(video, "GROK_VIDEO_PROVIDER", "xiaole"), \
                patch.object(video, "XIAOLEVIDEO_API_KEY", "configured"), \
                patch.object(video, "seedance_video_health_enabled", return_value=False):
            self.assertFalse(video.grok_reference_upload_is_open())
            self.assertEqual("", video.reverse_remake_video_channel(core.feature_flags))

    def test_reverse_remake_offer_uses_authoritative_points_matrix(self):
        from content_domains import core, points

        with patch.object(video, "reverse_remake_video_channel", return_value="micro"):
            micro = video.reverse_remake_video_offer(core.feature_flags, points.cost_of)
        self.assertEqual("micro", micro["channel"])
        self.assertEqual("720p", micro["resolution"])
        self.assertEqual({"5": 150, "10": 300, "15": 450}, micro["duration_costs"])

        with patch.object(video, "reverse_remake_video_channel", return_value="grok"):
            grok = video.reverse_remake_video_offer(core.feature_flags, points.cost_of)
        self.assertEqual("grok-imagine-video", grok["model"])
        self.assertEqual({"5": 60, "10": 120, "15": 180}, grok["duration_costs"])

    def test_reverse_remake_offer_fails_closed_on_invalid_quote(self):
        from content_domains import core

        with patch.object(video, "reverse_remake_video_channel", return_value="grok"):
            offer = video.reverse_remake_video_offer(
                core.feature_flags, lambda _kind, _body: 0)
        self.assertEqual("", offer["channel"])
        self.assertEqual({}, offer["duration_costs"])

    def test_grok_reference_staging_is_before_deduct_in_submit_path(self):
        from content_domains import core

        source = pathlib.Path(core.__file__).read_text(encoding="utf-8")
        staging = source.index("prepare_xiaole_reference_submission(")
        deduct = source.index("points_domain.deduct_points", staging)
        self.assertLess(staging, deduct)



if __name__ == "__main__":
    unittest.main()
