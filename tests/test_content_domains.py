import importlib
import base64
import queue
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


class ContentDomainTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)

    def test_entrypoint_uses_domain_registry(self):
        content_api = importlib.import_module("content_api")
        self.assertEqual(
            sorted(content_api.HANDLERS),
            ["audio", "collect", "copy", "image", "leads", "tryon", "video", "xiaole_video"],
        )
        self.assertIs(content_api.HANDLERS, content_api.registry.HANDLERS)

    def test_domains_export_expected_handlers(self):
        registry = importlib.import_module("content_domains.registry")
        for name in ("image", "copy", "collect", "leads", "audio", "video", "xiaole_video"):
            self.assertIn(name, registry.HANDLERS)
            self.assertTrue(callable(registry.HANDLERS[name]))

    def test_core_does_not_own_domain_handlers(self):
        core = importlib.import_module("content_domains.core")
        for name in ("gen_image", "gen_copy", "gen_collect", "gen_leads", "gen_audio", "gen_video"):
            self.assertFalse(hasattr(core, name), name)

        core_path = Path(core.__file__)
        self.assertLess(len(core_path.read_text(encoding="utf-8").splitlines()), 1200)

    def test_leads_returns_crm_fields_and_dedupe_count(self):
        leads = importlib.import_module("content_domains.leads")
        original_tikhub = leads.tikhub

        class FakeTikHub:
            class TikHubError(Exception):
                pass

            PLATFORMS = {"douyin"}

            def search(self, platform, keyword):
                return {"items": [{"id": "v1", "title": "门店拓客案例"}]}

            def comments(self, platform, vid_id, cursor=None, count=20):
                return {"has_more": False, "items": [
                    {"text": "想咨询一下价格", "user_id": "u1", "user": "小美", "ip": "广东", "likes": 3, "profile_url": "https://example.test/u1"},
                    {"text": "想咨询一下价格", "user_id": "u1", "user": "小美", "ip": "广东", "likes": 2, "profile_url": "https://example.test/u1"},
                    {"text": "路过看看", "user_id": "u2", "user": "阿青", "ip": "上海", "likes": 1, "profile_url": "https://example.test/u2"},
                ]}

        leads.tikhub = FakeTikHub()
        try:
            result = leads.gen_leads({"keyword": "美业获客", "platforms": ["douyin"], "count": 1})
        finally:
            leads.tikhub = original_tikhub

        self.assertEqual(result["leads_count"], 1)
        self.assertEqual(result["deduped"], 1)
        self.assertEqual(result["chat"], 1)
        lead = result["leads"][0]
        self.assertEqual(lead["intent"], "咨询")
        self.assertEqual(lead["follow_status"], "待跟进")
        self.assertEqual(lead["follow_note"], "")
        self.assertRegex(lead["lead_id"], r"^[0-9a-f]{16}$")

    def test_job_public_dict_hides_payload(self):
        core = importlib.import_module("content_domains.core")
        row = {
            "id": 1,
            "kind": "video",
            "username": "fang",
            "cost": 20,
            "status": "done",
            "payload": '{"text":"secret prompt","image_data":"data:image/png;base64,aaa"}',
            "result": '{"url":"/api/gen/file/video/demo.mp4"}',
            "error": None,
            "created_at": 1,
            "updated_at": 2,
        }
        public = core._job_public_dict(row, "done")
        self.assertNotIn("payload", public)
        self.assertEqual(public["result"]["url"], "/api/gen/file/video/demo.mp4")
        self.assertEqual(public["phase"], "done")

    def test_must_change_password_flag(self):
        core = importlib.import_module("content_domains.core")
        self.assertTrue(core._must_change_password({"must_change": True}))
        self.assertFalse(core._must_change_password({"must_change": False}))
        self.assertFalse(core._must_change_password(None))

    def test_job_queue_is_bounded_and_deduplicated(self):
        core = importlib.import_module("content_domains.core")
        original_queue = core._job_queue
        original_ids = core._queued_job_ids
        try:
            core._job_queue = queue.Queue(maxsize=1)
            core._queued_job_ids = set()
            self.assertTrue(core.enqueue_job(101))
            self.assertTrue(core.enqueue_job(101))
            self.assertFalse(core.enqueue_job(102))
            self.assertEqual(core._job_queue.qsize(), 1)
        finally:
            core._job_queue = original_queue
            core._queued_job_ids = original_ids

    def test_reject_pending_job_marks_error_and_refunds_once(self):
        core = importlib.import_module("content_domains.core")
        original_job_db = core.JOB_DB
        original_domains = core._domains

        class FakePoints:
            def __init__(self):
                self.refunds = []

            def safe_refund_points(self, username, cost):
                self.refunds.append((username, cost))
                return 0

        fake_points = FakePoints()
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "jobs.db"
            core.JOB_DB = str(db_path)
            core._domains = lambda: (None, fake_points, None)
            try:
                with closing(core.jdb()) as c:
                    c.execute("""CREATE TABLE jobs(
                        id INTEGER PRIMARY KEY, kind TEXT, username TEXT, cost INTEGER,
                        status TEXT, payload TEXT, result TEXT, error TEXT,
                        created_at INTEGER, updated_at INTEGER, refunded INTEGER DEFAULT 0
                    )""")
                    c.execute("""INSERT INTO jobs(id,kind,username,cost,status,payload,created_at,updated_at,refunded)
                                 VALUES(1,'image','fang',12,'pending','{}',1,1,0)""")
                    c.commit()

                self.assertTrue(core._reject_pending_job(1, "fang", 12, "full"))
                self.assertEqual(fake_points.refunds, [("fang", 12)])
                self.assertFalse(core._reject_pending_job(1, "fang", 12, "full again"))
                self.assertEqual(fake_points.refunds, [("fang", 12)])
                with closing(core.jdb()) as c:
                    row = c.execute("SELECT status,error,refunded FROM jobs WHERE id=1").fetchone()
                self.assertEqual(row["status"], "error")
                self.assertEqual(row["error"], "full")
                self.assertEqual(row["refunded"], 1)
            finally:
                core.JOB_DB = original_job_db
                core._domains = original_domains

    def test_clone_vip_validation_rejects_before_mutation(self):
        audio = importlib.import_module("content_domains.audio")
        original_adb = audio.adb
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "audio.db"

            def test_adb():
                c = sqlite3.connect(db_path)
                c.row_factory = sqlite3.Row
                return c

            audio.adb = test_adb
            try:
                with closing(test_adb()) as c:
                    c.execute("""CREATE TABLE audio_voice_slots(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT, slot_id TEXT, status TEXT, voice_id INTEGER,
                        reclone_count INTEGER, updated_at INTEGER, clone_upload_at INTEGER
                    )""")
                    c.execute("""INSERT INTO audio_voice_slots
                        (username, slot_id, status, voice_id, reclone_count, updated_at, clone_upload_at)
                        VALUES('fang','S_demo','ready',7,9,100,100)""")
                    c.commit()

                before = self._slot_snapshot(test_adb, "fang", "S_demo")
                cases = [
                    ({"slot_id": "S_demo", "audio_format": "wav"}, 400, "请先上传样音"),
                    ({"slot_id": "S_demo", "audio": "YQ==", "audio_format": "exe"}, 400, "audio_format 仅支持"),
                    ({"slot_id": "S_missing", "audio": "YQ==", "audio_format": "wav"}, 404, "音色槽位不存在"),
                ]
                for payload, status, msg in cases:
                    with self.subTest(payload=payload):
                        with self.assertRaises(audio.CloneVipValidationError) as cm:
                            audio.validate_clone_vip_payload("fang", payload)
                        self.assertEqual(cm.exception.status, status)
                        self.assertIn(msg, cm.exception.detail)
                        self.assertEqual(before, self._slot_snapshot(test_adb, "fang", "S_demo"))

                with closing(test_adb()) as c:
                    c.execute("UPDATE audio_voice_slots SET reclone_count=10 WHERE username='fang' AND slot_id='S_demo'")
                    c.commit()
                before_limit = self._slot_snapshot(test_adb, "fang", "S_demo")
                with self.assertRaises(audio.CloneVipValidationError) as cm:
                    audio.validate_clone_vip_payload("fang", {"slot_id": "S_demo", "audio": base64.b64encode(b'audio').decode(), "audio_format": "wav"})
                self.assertEqual(cm.exception.status, 409)
                self.assertIn("复刻上限", cm.exception.detail)
                self.assertEqual(before_limit, self._slot_snapshot(test_adb, "fang", "S_demo"))
            finally:
                audio.adb = original_adb

    def test_clone_vip_validation_normalizes_valid_payload(self):
        audio = importlib.import_module("content_domains.audio")
        original_adb = audio.adb
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "audio.db"

            def test_adb():
                c = sqlite3.connect(db_path)
                c.row_factory = sqlite3.Row
                return c

            audio.adb = test_adb
            try:
                with closing(test_adb()) as c:
                    c.execute("""CREATE TABLE audio_voice_slots(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT, slot_id TEXT, status TEXT, voice_id INTEGER,
                        reclone_count INTEGER, updated_at INTEGER, clone_upload_at INTEGER
                    )""")
                    c.execute("""INSERT INTO audio_voice_slots
                        (username, slot_id, status, voice_id, reclone_count, updated_at, clone_upload_at)
                        VALUES('fang','S_demo','active',NULL,0,100,100)""")
                    c.commit()
                payload = audio.validate_clone_vip_payload("fang", {
                    "slot_id": " S_demo ",
                    "audio": "data:audio/wav;base64," + base64.b64encode(b'audio').decode(),
                    "audio_format": ".WAV",
                })
                self.assertEqual(payload["slot_id"], "S_demo")
                self.assertEqual(payload["audio"], base64.b64encode(b'audio').decode())
                self.assertEqual(payload["audio_format"], "wav")
            finally:
                audio.adb = original_adb

    def _slot_snapshot(self, adb, username, slot_id):
        with closing(adb()) as c:
            row = c.execute("""SELECT status, voice_id, reclone_count, updated_at, clone_upload_at
                FROM audio_voice_slots WHERE username=? AND slot_id=?""", (username, slot_id)).fetchone()
        return tuple(row) if row else None


if __name__ == "__main__":
    unittest.main()
