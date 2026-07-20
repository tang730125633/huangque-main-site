# -*- coding: utf-8 -*-
import base64
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
                (username, scope, voice_key, display_name, provider_voice, slot_id, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (username, "personal", "vip_" + slot_id, "我的音色", "provider_test", slot_id, 1, 1))
            voice_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute("""INSERT INTO audio_voice_slots
                (username, slot_id, status, voice_id, has_cloned, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?)""", (username, slot_id, "ready", voice_id, 1, 1, 1))
            conn.execute("""INSERT INTO audio_assets
                (job_id, username, voice_id, voice_key, created_at)
                VALUES(?,?,?,?,?)""", (1, username, voice_id, "vip_" + slot_id, 1))
            conn.commit()
        return voice_id

    def _valid_clone_payload(self, slot_id="slot_a"):
        return {
            "slot_id": slot_id,
            "audio": base64.b64encode(b"sample").decode(),
            "audio_format": "mp3",
        }

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

    def test_limit_blocks_reclone_without_deleting_current_voice(self):
        voice_id = self._create_ready_voice()
        with closing(core.adb()) as conn:
            conn.execute("UPDATE audio_voice_slots SET reclone_count=? WHERE slot_id='slot_a'",
                         (audio.VOICE_RECLONE_MAX,))
            conn.commit()

        with self.assertRaises(audio.CloneVipValidationError) as raised:
            audio.validate_clone_vip_payload("alice", self._valid_clone_payload())
        self.assertEqual(409, raised.exception.status)
        with self.assertRaisesRegex(ValueError, "重新复刻上限"):
            audio.mark_clone_training("alice", "slot_a", "不能覆盖")

        with closing(core.adb()) as conn:
            slot = conn.execute("SELECT status, voice_id, reclone_count FROM audio_voice_slots WHERE slot_id='slot_a'").fetchone()
            voice = conn.execute("SELECT provider_voice FROM audio_voices WHERE id=?", (voice_id,)).fetchone()
        self.assertEqual("ready", slot["status"])
        self.assertEqual(voice_id, slot["voice_id"])
        self.assertEqual(audio.VOICE_RECLONE_MAX, slot["reclone_count"])
        self.assertEqual("provider_test", voice["provider_voice"])

    def test_delete_at_limit_does_not_reset_quota_or_allow_reclone(self):
        self._create_ready_voice()
        with closing(core.adb()) as conn:
            conn.execute("UPDATE audio_voice_slots SET reclone_count=? WHERE slot_id='slot_a'",
                         (audio.VOICE_RECLONE_MAX,))
            conn.commit()

        result = audio.delete_audio_voice("alice", "slot_a")

        self.assertEqual(0, result["reclone_remaining"])
        with closing(core.adb()) as conn:
            slot = conn.execute("SELECT status, voice_id, reclone_count, has_cloned FROM audio_voice_slots WHERE slot_id='slot_a'").fetchone()
        self.assertEqual("active", slot["status"])
        self.assertIsNone(slot["voice_id"])
        self.assertEqual(audio.VOICE_RECLONE_MAX, slot["reclone_count"])
        self.assertEqual(1, slot["has_cloned"])
        with self.assertRaises(audio.CloneVipValidationError) as raised:
            audio.validate_clone_vip_payload("alice", self._valid_clone_payload())
        self.assertEqual(409, raised.exception.status)
        with self.assertRaisesRegex(ValueError, "重新复刻上限"):
            audio.mark_clone_training("alice", "slot_a", "不能复刻")

    def test_reclone_after_delete_consumes_next_attempt(self):
        self._create_ready_voice()
        audio.delete_audio_voice("alice", "slot_a")

        result = audio.mark_clone_training("alice", "slot_a", "重新创建")

        self.assertEqual(1, result["reclone_count"])
        self.assertEqual(audio.VOICE_RECLONE_MAX - 1, result["reclone_remaining"])

    def test_initial_clone_does_not_consume_reclone_quota(self):
        with closing(core.adb()) as conn:
            conn.execute("""INSERT INTO audio_voice_slots
                (username, slot_id, status, has_cloned, created_at, updated_at)
                VALUES('alice','slot_new','active',0,1,1)""")
            conn.commit()

        result = audio.mark_clone_training("alice", "slot_new", "首次复刻")

        self.assertEqual(0, result["reclone_count"])
        self.assertEqual(audio.VOICE_RECLONE_MAX, result["reclone_remaining"])
        with closing(core.adb()) as conn:
            has_cloned = conn.execute("SELECT has_cloned FROM audio_voice_slots WHERE slot_id='slot_new'").fetchone()["has_cloned"]
        self.assertEqual(1, has_cloned)

    def test_init_backfills_clone_history_for_existing_voice(self):
        self._create_ready_voice()
        with closing(core.adb()) as conn:
            conn.execute("UPDATE audio_voice_slots SET has_cloned=0 WHERE slot_id='slot_a'")
            conn.commit()

        core.init_audio_db()

        with closing(core.adb()) as conn:
            has_cloned = conn.execute("SELECT has_cloned FROM audio_voice_slots WHERE slot_id='slot_a'").fetchone()["has_cloned"]
        self.assertEqual(1, has_cloned)

    def test_delete_cleans_local_cos_and_provider_voice(self):
        voice_id = self._create_ready_voice()
        preview = pathlib.Path(self.tmp.name) / "voice_preview_test.mp3"
        preview.write_bytes(b"preview")
        provider_voice = audio.cosyvoice.CLONE_MODEL + "-bailian-test"
        with closing(core.adb()) as conn:
            conn.execute("""UPDATE audio_voices
                SET provider_voice=?, preview_file=?, preview_url=? WHERE id=?""",
                (provider_voice, "audio/voice_preview_test.mp3", "https://cos.example/audio/voice_preview_test.mp3", voice_id))
            conn.commit()

        with patch.object(audio, "_resolve_out_file", return_value=preview), \
                patch.object(audio.cos, "enabled", return_value=True), \
                patch.object(audio.cos, "delete") as delete_cos, \
                patch.object(audio.cosyvoice, "enabled", return_value=True), \
                patch.object(audio.cosyvoice, "delete_voice") as delete_provider:
            result = audio.delete_audio_voice("alice", "slot_a")

        self.assertFalse(preview.exists())
        delete_cos.assert_called_once_with("audio/voice_preview_test.mp3")
        delete_provider.assert_called_once_with(provider_voice)
        self.assertTrue(result["preview_deleted"])
        self.assertTrue(result["remote_deleted"])

    def test_clone_reference_is_private_and_deleted_after_submission(self):
        self._create_ready_voice()
        temp_ref = pathlib.Path(self.tmp.name) / "clone-reference.mp3"
        with patch.object(audio, "_out_path", return_value=temp_ref), \
                patch.object(audio.cos, "enabled", return_value=True), \
                patch.object(audio.cos, "upload") as upload, \
                patch.object(audio.cos, "object_url", return_value="https://signed.example/reference") as object_url, \
                patch.object(audio.cos, "delete") as delete_cos, \
                patch.object(audio.cosyvoice, "create_voice", return_value="cosyvoice-v3.5-plus-bailian-new"), \
                patch.object(audio.cosyvoice, "voice_status", return_value=("OK", {})), \
                patch.object(audio, "_cosy_backfill_preview_async"):
            audio._clone_via_cosyvoice("alice", "slot_a", "新音色", base64.b64encode(b"sample").decode())

        key = upload.call_args.args[1]
        self.assertTrue(upload.call_args.kwargs["private"])
        object_url.assert_called_once_with(key, private=True)
        delete_cos.assert_called_once_with(key)
        self.assertFalse(temp_ref.exists())


class VoiceSlotQuotaFrontendTest(unittest.TestCase):
    def test_web_frontend_consumes_backend_quota_fields(self):
        html = (ROOT / "site" / "workbench" / "assets.html").read_text(encoding="utf-8")
        self.assertIn("slot.reclone_max", html)
        self.assertIn("slot.reclone_remaining", html)
        self.assertNotIn("Math.min(10, parseInt(slot.reclone_count", html)

    def test_web_frontend_requires_confirmation_and_calls_delete_endpoint(self):
        html = (ROOT / "site" / "workbench" / "assets.html").read_text(encoding="utf-8")
        self.assertIn("openDeleteVoiceModal", html)
        self.assertIn("/api/gen/audio/voice-delete", html)
        self.assertIn("确认删除", html)
        self.assertIn("次数不会重置", html)
        self.assertIn("删除后将不能再次复刻", html)
        self.assertIn("quotaExhausted", html)


if __name__ == "__main__":
    unittest.main()
