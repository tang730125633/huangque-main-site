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
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import ThreadingHTTPServer
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import audio, core
from scripts import cleanup_legacy_voice_slot_data


class FakePointsError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class FakePoints:
    AuthPointsError = FakePointsError

    def __init__(self, balance=200):
        self.balance = balance
        self.deductions = []
        self.refunds = []
        self.fail_status = None

    def deduct_points(self, username, amount, reason=""):
        if self.fail_status:
            raise FakePointsError(self.fail_status, "点数不足" if self.fail_status == 402 else "点数服务失败")
        if self.balance < amount:
            raise FakePointsError(402, "点数不足")
        self.balance -= amount
        self.deductions.append((username, amount, reason))
        return self.balance

    def safe_refund_points(self, username, amount, reason=""):
        self.balance += amount
        self.refunds.append((username, amount, reason))
        return self.balance


class VoiceSlotPurchaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(pathlib.Path(self.tmp.name) / "audio.db")
        self.db_patch = patch.object(core, "AUDIO_DB", self.db)
        self.db_patch.start()
        core.init_audio_db()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def _slot_rows(self):
        with closing(core.adb()) as conn:
            return conn.execute(
                "SELECT username,user_id,slot_id,status FROM audio_voice_slots ORDER BY id"
            ).fetchall()

    def test_purchase_deducts_50_and_creates_uuid_slot(self):
        points = FakePoints(balance=125)
        with patch.object(audio, "points_domain", points), patch.object(audio, "get_user_id", return_value=7):
            result = audio.purchase_audio_voice_slot("alice")

        self.assertEqual(50, result["cost"])
        self.assertEqual(75, result["points_left"])
        self.assertRegex(result["slot_id"], r"^slot_[0-9a-f]{32}$")
        self.assertEqual([("alice", 50, "voice_slot")], points.deductions)
        rows = self._slot_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual(("alice", 7, result["slot_id"], "active"), tuple(rows[0]))

    def test_sixth_slot_is_rejected_before_deduct(self):
        with closing(core.adb()) as conn:
            now = 1
            for index, status in enumerate(("active", "training", "ready", "failed", "active")):
                conn.execute(
                    "INSERT INTO audio_voice_slots(username,user_id,slot_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    ("alice", 7, "slot_%d" % index, status, now, now),
                )
            conn.commit()
        points = FakePoints()
        with patch.object(audio, "points_domain", points), patch.object(audio, "get_user_id", return_value=7):
            with self.assertRaises(audio.VoiceSlotLimitError) as raised:
                audio.purchase_audio_voice_slot("alice")
        self.assertIn("最多 5 个音色槽位", str(raised.exception))
        self.assertEqual([], points.deductions)
        self.assertEqual(5, len(self._slot_rows()))

    def test_concurrent_fifth_and_sixth_purchase_only_deducts_once(self):
        with closing(core.adb()) as conn:
            for index in range(4):
                conn.execute(
                    "INSERT INTO audio_voice_slots(username,user_id,slot_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    ("alice", 7, "slot_existing_%d" % index, "active", 1, 1),
                )
            conn.commit()
        points = FakePoints(balance=200)

        def buy():
            try:
                return audio.purchase_audio_voice_slot("alice")["status"]
            except audio.VoiceSlotLimitError:
                return "limited"

        with patch.object(audio, "points_domain", points), patch.object(audio, "get_user_id", return_value=7):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: buy(), range(2)))

        self.assertEqual(["active", "limited"], sorted(outcomes))
        self.assertEqual(1, len(points.deductions))
        self.assertEqual(5, len(self._slot_rows()))

    def test_insufficient_points_does_not_create_slot(self):
        points = FakePoints(balance=10)
        points.fail_status = 402
        with patch.object(audio, "points_domain", points), patch.object(audio, "get_user_id", return_value=7):
            with self.assertRaises(FakePointsError) as raised:
                audio.purchase_audio_voice_slot("alice")
        self.assertEqual(402, raised.exception.status)
        self.assertEqual([], self._slot_rows())

    def test_insert_failure_refunds_deduction(self):
        broken_db = pathlib.Path(self.tmp.name) / "broken.db"
        with closing(sqlite3.connect(broken_db)) as conn:
            conn.execute("CREATE TABLE audio_voice_slots(username TEXT, slot_id TEXT, status TEXT)")
            conn.commit()

        def broken_adb():
            conn = sqlite3.connect(broken_db)
            conn.row_factory = sqlite3.Row
            return conn

        points = FakePoints(balance=125)
        with patch.object(audio, "adb", broken_adb), patch.object(audio, "points_domain", points), \
                patch.object(audio, "get_user_id", return_value=7):
            with self.assertRaises(audio.VoiceSlotPurchaseError) as raised:
                audio.purchase_audio_voice_slot("alice")
        self.assertIn("50 点已退回", str(raised.exception))
        self.assertEqual(125, points.balance)
        self.assertEqual([("alice", 50, "voice_slot:insert_failed")], points.refunds)

    def test_http_buy_route_and_deprecated_redeem_route(self):
        points = FakePoints(balance=125)
        server = None
        with patch.object(audio, "points_domain", points), patch.object(audio, "get_user_id", return_value=7), \
                patch.object(core, "_domains", return_value=(audio, points, None)), \
                patch.object(core, "verify", side_effect=lambda token: {"username": "alice", "points": points.balance, "must_change": False}), \
                patch.object(core.feature_flags, "require_enabled", return_value=None):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = "http://127.0.0.1:%d" % server.server_address[1]
                headers = {"Authorization": "Bearer test", "Content-Type": "application/json"}
                request = urllib.request.Request(
                    base + "/api/gen/audio/buy-slot", data=b"{}", method="POST", headers=headers
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    bought = json.loads(response.read())
                self.assertTrue(bought["ok"])
                self.assertEqual(50, bought["cost"])
                self.assertEqual(75, bought["points_left"])

                slots_request = urllib.request.Request(
                    base + "/api/gen/audio/slots", headers={"Authorization": "Bearer test"}
                )
                with urllib.request.urlopen(slots_request, timeout=5) as response:
                    slots = json.loads(response.read())
                self.assertEqual(1, slots["slot_count"])
                self.assertEqual(5, slots["slot_max"])
                self.assertEqual(50, slots["slot_cost"])
                self.assertEqual(20, slots["reclone_max"])
                self.assertEqual(75, slots["points"])

                deprecated = urllib.request.Request(
                    base + "/api/gen/audio/redeem-slot", data=b'{}', method="POST", headers=headers
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(deprecated, timeout=5)
                self.assertEqual(410, raised.exception.code)
                detail = json.loads(raised.exception.read())["detail"]
                self.assertIn("点数购买", detail)
            finally:
                if server:
                    server.shutdown()
                    server.server_close()


class VoiceSlotFrontendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "site" / "workbench" / "assets.html").read_text(encoding="utf-8")

    def test_purchase_ui_has_no_redemption_code_flow(self):
        self.assertIn("/api/gen/audio/buy-slot", self.html)
        self.assertIn("HQ.refreshPoints", self.html)
        self.assertIn("recharge.html", self.html)
        self.assertIn("slot_count", self.html)
        for stale in ("/api/gen/audio/redeem-slot", "slotCodeInput", "确认兑换", "请输入兑换码"):
            self.assertNotIn(stale, self.html)

    def test_inline_javascript_parses(self):
        scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", self.html)
        self.assertTrue(scripts)
        checked = subprocess.run(
            ["node", "--check", "-"], input=scripts[-1], text=True,
            encoding="utf-8", capture_output=True,
        )
        self.assertEqual(0, checked.returncode, checked.stderr)


class LegacyVoiceSlotCleanupTest(unittest.TestCase):
    def test_cleanup_is_dry_run_by_default_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            db = pathlib.Path(td) / "audio.db"
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("CREATE TABLE voice_slot_pool(slot_id TEXT, status TEXT)")
                conn.execute("CREATE TABLE voice_slot_codes(code TEXT, status TEXT)")
                conn.executemany("INSERT INTO voice_slot_pool VALUES(?,?)", [("old", "assigned"), ("spare", "available")])
                conn.executemany("INSERT INTO voice_slot_codes VALUES(?,?)", [("old", "used"), ("spare", "unused")])
                conn.commit()

            dry = cleanup_legacy_voice_slot_data.cleanup(db)
            self.assertEqual({"assigned_pool": 1, "used_codes": 1}, dry["before"])
            self.assertEqual(dry["before"], dry["after"])
            applied = cleanup_legacy_voice_slot_data.cleanup(db, apply=True)
            self.assertEqual({"assigned_pool": 0, "used_codes": 0}, applied["after"])
            again = cleanup_legacy_voice_slot_data.cleanup(db, apply=True)
            self.assertEqual({"assigned_pool": 0, "used_codes": 0}, again["before"])

            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(("spare", "available"), conn.execute("SELECT * FROM voice_slot_pool").fetchone())
                self.assertEqual(("spare", "unused"), conn.execute("SELECT * FROM voice_slot_codes").fetchone())


if __name__ == "__main__":
    unittest.main()
