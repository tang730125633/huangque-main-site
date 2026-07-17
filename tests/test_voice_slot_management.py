# -*- coding: utf-8 -*-
import pathlib
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import audio, core


class VoiceSlotManagementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(pathlib.Path(self.tmp.name) / "audio.db")
        self.db_patch = patch.object(core, "AUDIO_DB", self.db)
        self.db_patch.start()
        core.init_audio_db()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def _create_ready_voice(self, username="alice", slot_id="slot_a"):
        with closing(core.adb()) as conn:
            conn.execute("""INSERT INTO audio_voices
                (username, scope, voice_key, display_name, provider_voice, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?)""",
                (username, "personal", "vip_" + slot_id, "我的音色", "provider_test", 1, 1))
            voice_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute("""INSERT INTO audio_voice_slots
                (username, slot_id, status, voice_id, created_at, updated_at)
                VALUES(?,?,?,?,?,?)""", (username, slot_id, "ready", voice_id, 1, 1))
            conn.execute("""INSERT INTO audio_assets
                (job_id, username, voice_id, voice_key, created_at)
                VALUES(?,?,?,?,?)""", (1, username, voice_id, "vip_" + slot_id, 1))
            conn.commit()
        return voice_id

    def test_delete_voice_keeps_paid_slot_available_for_reclone(self):
        voice_id = self._create_ready_voice()

        result = audio.delete_audio_voice("alice", "slot_a")

        self.assertTrue(result["voice_deleted"])
        self.assertEqual("active", result["status"])
        with closing(core.adb()) as conn:
            slot = conn.execute(
                "SELECT status, voice_id FROM audio_voice_slots WHERE username=? AND slot_id=?",
                ("alice", "slot_a"),
            ).fetchone()
            voice = conn.execute("SELECT id FROM audio_voices WHERE id=?", (voice_id,)).fetchone()
            asset = conn.execute("SELECT voice_id FROM audio_assets WHERE job_id=1").fetchone()
        self.assertEqual("active", slot["status"])
        self.assertIsNone(slot["voice_id"])
        self.assertIsNone(voice)
        self.assertIsNone(asset["voice_id"])

    def test_delete_rejects_other_users_slot(self):
        self._create_ready_voice()
        with self.assertRaises(audio.VoiceSlotNotFoundError):
            audio.delete_audio_voice("bob", "slot_a")

    def test_delete_rejects_training_voice(self):
        self._create_ready_voice()
        with closing(core.adb()) as conn:
            conn.execute("UPDATE audio_voice_slots SET status='training' WHERE slot_id='slot_a'")
            conn.commit()
        with self.assertRaises(audio.VoiceSlotBusyError):
            audio.delete_audio_voice("alice", "slot_a")

    def test_slot_list_exposes_shared_reclone_limit_and_remaining_count(self):
        self._create_ready_voice()
        with closing(core.adb()) as conn:
            conn.execute("UPDATE audio_voice_slots SET reclone_count=19 WHERE slot_id='slot_a'")
            conn.commit()

        slot = audio.list_user_audio_voice_slots("alice")[0]

        self.assertEqual(20, audio.VOICE_RECLONE_MAX)
        self.assertEqual(audio.VOICE_RECLONE_MAX, slot["reclone_max"])
        self.assertEqual(1, slot["reclone_remaining"])

    def test_nineteenth_reclone_reaches_limit_without_exceeding_it(self):
        self._create_ready_voice()
        with closing(core.adb()) as conn:
            conn.execute("UPDATE audio_voice_slots SET reclone_count=19 WHERE slot_id='slot_a'")
            conn.commit()

        result = audio.mark_clone_training("alice", "slot_a", "第二十次")

        self.assertEqual(20, result["reclone_count"])
        self.assertEqual(20, result["reclone_max"])
        self.assertEqual(0, result["reclone_remaining"])


class VoiceSlotQuotaFrontendTest(unittest.TestCase):
    def test_web_frontend_consumes_backend_quota_fields(self):
        html = (ROOT / "site" / "workbench" / "assets.html").read_text(encoding="utf-8")
        self.assertIn("slot.reclone_max", html)
        self.assertIn("slot.reclone_remaining", html)
        self.assertNotIn("Math.min(10, parseInt(slot.reclone_count", html)


if __name__ == "__main__":
    unittest.main()
