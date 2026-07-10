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
        # 软上限防 core 膨胀（真守卫是上面的 hasattr 域处理器检查）；1250→1300→1330→1400→1330：
        # 视频任务池按 mode 三分(口播/motion/慢池)属 core 队列基础设施、非域逻辑，合理留 core；
        # reclaim_orphaned_running(启动回收重启遗留孤儿→退点)属 core 任务生命周期、紧挨 reaper。
        self.assertLess(len(core_path.read_text(encoding="utf-8").splitlines()), 1400)

    def test_content_api_reclaims_orphans_on_startup(self):
        # 防回归：孤儿回收必须挂在真入口 content_api.main（服务走 content_api.py，
        # 不是 core.__main__），且排在 start_job_workers 之前——此刻 worker 未启动，
        # DB 里任何 running 必是上次重启遗留的孤儿。
        content_api = importlib.import_module("content_api")
        src = Path(content_api.__file__).read_text(encoding="utf-8")
        self.assertIn("reclaim_orphaned_running", src)
        self.assertLess(src.index("reclaim_orphaned_running"), src.index("start_job_workers"))

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
        self.assertGreaterEqual(lead["intent_score"], 80)
        self.assertIn("命中", lead["intent_reason"])
        self.assertEqual(lead["follow_status"], "待跟进")
        self.assertEqual(lead["follow_note"], "")
        self.assertRegex(lead["lead_id"], r"^[0-9a-f]{16}$")

    def test_leads_crm_persists_and_merges_by_user(self):
        leads = importlib.import_module("content_domains.leads")
        original_db = leads.LEADS_CRM_DB
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
                ]}

        with tempfile.TemporaryDirectory() as td:
            leads.LEADS_CRM_DB = str(Path(td) / "leads_crm.db")
            leads.tikhub = FakeTikHub()
            try:
                first = leads.gen_leads({"keyword": "美业获客", "platforms": ["douyin"], "count": 1, "_username": "fang"})
                lead_id = first["leads"][0]["lead_id"]
                saved = leads.upsert_crm("fang", {
                    "lead_id": lead_id,
                    "intent": "价格敏感",
                    "follow_status": "跟进中",
                    "follow_note": "已私信报价",
                })
                self.assertEqual(saved["follow_status"], "跟进中")
                self.assertEqual(leads.list_crm("fang", [lead_id])[lead_id]["follow_note"], "已私信报价")

                merged = leads.gen_leads({"keyword": "美业获客", "platforms": ["douyin"], "count": 1, "_username": "fang"})
                self.assertEqual(merged["leads"][0]["intent"], "价格敏感")
                self.assertEqual(merged["leads"][0]["follow_status"], "跟进中")
                self.assertEqual(merged["leads"][0]["follow_note"], "已私信报价")
                other_user = leads.gen_leads({"keyword": "美业获客", "platforms": ["douyin"], "count": 1, "_username": "other"})
                self.assertEqual(other_user["leads"][0]["follow_status"], "待跟进")
                with self.assertRaises(ValueError):
                    leads.upsert_crm("fang", {"lead_id": lead_id, "follow_status": "乱填"})
            finally:
                leads.LEADS_CRM_DB = original_db
                leads.tikhub = original_tikhub

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

            core._job_queue = queue.Queue(maxsize=2)
            core._queued_job_ids = set()
            self.assertTrue(core.enqueue_jobs([201, 202]))
            self.assertFalse(core.enqueue_jobs([203, 204]))
            self.assertEqual([201, 202], list(core._job_queue.queue))
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

            def safe_refund_points(self, username, cost, reason=""):
                self.refunds.append((username, cost, reason))
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
                self.assertEqual(fake_points.refunds, [("fang", 12, "job#1")])
                self.assertFalse(core._reject_pending_job(1, "fang", 12, "full again"))
                self.assertEqual(fake_points.refunds, [("fang", 12, "job#1")])
                with closing(core.jdb()) as c:
                    row = c.execute("SELECT status,error,refunded FROM jobs WHERE id=1").fetchone()
                self.assertEqual(row["status"], "error")
                self.assertEqual(row["error"], "full")
                self.assertEqual(row["refunded"], 1)
            finally:
                core.JOB_DB = original_job_db
                core._domains = original_domains

    def test_talking_running_gate_defers_over_limit(self):
        core = importlib.import_module("content_domains.core")
        original_job_db = core.JOB_DB
        original_handlers = core.HANDLERS
        with tempfile.TemporaryDirectory() as td:
            core.JOB_DB = str(Path(td) / "jobs.db")
            called = []
            core.HANDLERS = {"video": lambda p: called.append(p) or {"ok": 1}}
            try:
                with closing(core.jdb()) as c:
                    c.execute("""CREATE TABLE jobs(
                        id INTEGER PRIMARY KEY, kind TEXT, username TEXT, cost INTEGER,
                        status TEXT, payload TEXT, result TEXT, error TEXT,
                        created_at INTEGER, updated_at INTEGER, refunded INTEGER DEFAULT 0)""")
                    # fang: 2 条口播 running + 1 条 motion running(不该计入口播)
                    c.execute("INSERT INTO jobs(id,kind,username,cost,status,payload,created_at,updated_at) VALUES(1,'video','fang',20,'running','{\"mode\":\"text\"}',1,1)")
                    c.execute("INSERT INTO jobs(id,kind,username,cost,status,payload,created_at,updated_at) VALUES(2,'video','fang',20,'running','{\"mode\":\"audio\"}',1,1)")
                    c.execute("INSERT INTO jobs(id,kind,username,cost,status,payload,created_at,updated_at) VALUES(3,'video','fang',20,'running','{\"mode\":\"motion\"}',1,1)")
                    # 第4条 pending 口播 —— 应被运行闸 defer，留 pending、不调 handler
                    c.execute("INSERT INTO jobs(id,kind,username,cost,status,payload,created_at,updated_at) VALUES(4,'video','fang',20,'pending','{\"mode\":\"text\"}',1,1)")
                    c.commit()
                # count 只算口播(排除 motion)=2
                self.assertEqual(core._user_running_talking_count("fang"), 2)
                core.run_job(4)
                self.assertEqual(called, [])  # 超运行闸→handler 不该被调
                with closing(core.jdb()) as c:
                    self.assertEqual(c.execute("SELECT status FROM jobs WHERE id=4").fetchone()["status"], "pending")
            finally:
                core.JOB_DB = original_job_db
                core.HANDLERS = original_handlers

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
