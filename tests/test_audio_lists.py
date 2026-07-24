import gc
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import audio, core


class AudioListTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "audio.db")
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript("""
                CREATE TABLE audio_voice_slots(
                    id INTEGER, username TEXT, user_id INTEGER, slot_id TEXT, status TEXT,
                    voice_id INTEGER, reclone_count INTEGER, created_at INTEGER, updated_at INTEGER,
                    clone_started_at INTEGER, clone_upload_at INTEGER, clone_error TEXT,
                    clone_upload_speaker_id TEXT, clone_upload_response TEXT,
                    clone_baseline_version TEXT, clone_baseline_icl_speaker_id TEXT,
                    clone_baseline_demo_audio TEXT);
                CREATE TABLE audio_voices(
                    id INTEGER, scope TEXT, username TEXT, voice_key TEXT, display_name TEXT,
                    provider_voice TEXT, preview_file TEXT, preview_url TEXT, slot_id TEXT,
                    created_at INTEGER, updated_at INTEGER);
                INSERT INTO audio_voice_slots VALUES(
                    1,'alice',1,'S_test','training',1,0,1,1,1,1,NULL,NULL,NULL,NULL,NULL,NULL);
                INSERT INTO audio_voices VALUES(
                    1,'personal','alice','vip','我的音色','S_test',NULL,NULL,'S_test',1,1);
                INSERT INTO audio_voices VALUES(
                    2,'public','','S_d21F8OR62','公共音色','S_d21F8OR62',NULL,NULL,NULL,1,1);
            """)
        finally:
            conn.close()
        self.db_patch = patch.object(core, "AUDIO_DB", self.db)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        gc.collect()
        self.tmp.cleanup()

    def test_slot_list_does_not_query_external_clone_status(self):
        with patch.object(audio, "check_clone_status", side_effect=AssertionError("external call")):
            items = audio.list_user_audio_voice_slots("alice")
        self.assertEqual(items[0]["slot_id"], "S_test")
        self.assertEqual(items[0]["status"], "training")

    def test_clone_status_repairs_training_slot_when_preview_exists(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("""UPDATE audio_voices
                SET provider_voice='cosyvoice-v3.5-plus-bailian-test',
                    preview_url='https://preview.example/test.mp3'
                WHERE id=1""")

        result = audio.check_clone_status("alice", "S_test")

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["preview_url"], "https://preview.example/test.mp3")
        with sqlite3.connect(self.db) as conn:
            status = conn.execute(
                "SELECT status FROM audio_voice_slots WHERE id=1"
            ).fetchone()[0]
        self.assertEqual(status, "ready")

    def test_clone_status_keeps_training_during_provider_handoff(self):
        with patch.object(audio.cosyvoice, "enabled", return_value=True), \
                patch.object(audio.cosyvoice, "voice_status",
                             side_effect=AssertionError("placeholder is not a provider voice")):
            result = audio.check_clone_status("alice", "S_test")

        self.assertEqual(result, {"status": "training"})
        with sqlite3.connect(self.db) as conn:
            status = conn.execute(
                "SELECT status FROM audio_voice_slots WHERE id=1"
            ).fetchone()[0]
        self.assertEqual(status, "training")

    def test_clone_status_ignores_retired_or_stale_preview_rows(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("""UPDATE audio_voices
                SET provider_voice='S_legacy', preview_url='https://preview.example/legacy.mp3'
                WHERE id=1""")
            conn.execute("""INSERT INTO audio_voices VALUES(
                3,'personal','alice','vip_old','旧音色',
                'cosyvoice-v3.5-plus-bailian-stale',NULL,
                'https://preview.example/stale.mp3','S_test',1,1)""")

        with patch.object(audio.cosyvoice, "enabled", return_value=False):
            result = audio.check_clone_status("alice", "S_test")

        self.assertEqual(result["status"], "failed")
        with sqlite3.connect(self.db) as conn:
            status = conn.execute(
                "SELECT status FROM audio_voice_slots WHERE id=1"
            ).fetchone()[0]
        self.assertEqual(status, "training")

    def test_clone_status_does_not_mark_new_reclone_ready_from_old_snapshot(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("""UPDATE audio_voices
                SET provider_voice='cosyvoice-v3.5-plus-bailian-old',
                    preview_url='https://preview.example/old.mp3'
                WHERE id=1""")

        calls = 0

        def racing_adb():
            nonlocal calls
            calls += 1
            if calls == 2:
                with sqlite3.connect(self.db) as conn:
                    conn.execute("""UPDATE audio_voices
                        SET provider_voice='S_test', preview_url=NULL WHERE id=1""")
                    conn.execute("""UPDATE audio_voice_slots
                        SET status='training' WHERE id=1""")
            conn = sqlite3.connect(self.db)
            conn.row_factory = sqlite3.Row
            return conn

        with patch.object(audio, "adb", side_effect=racing_adb):
            result = audio.check_clone_status("alice", "S_test")

        self.assertEqual(result["status"], "training")
        with sqlite3.connect(self.db) as conn:
            status = conn.execute(
                "SELECT status FROM audio_voice_slots WHERE id=1"
            ).fetchone()[0]
        self.assertEqual(status, "training")

    def test_voice_list_returns_db_before_background_warmup(self):
        audio._preview_warm_running = False
        audio._preview_warm_next_at = 0
        with patch.object(audio.threading, "Thread") as thread, \
                patch.object(audio, "_ensure_public_voice_preview", side_effect=AssertionError("external call")):
            items = audio.list_audio_voices("alice")
        audio._preview_warm_running = False
        self.assertEqual(len(items), 2)
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    def test_public_voice_migration_invalidates_old_preview_once(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("""UPDATE audio_voices
                SET preview_file='audio/old.mp3', preview_url='https://old.example/preview.mp3'
                WHERE id=2""")

        self.assertEqual(audio._migrate_public_voice_presets(), 1)
        self.assertEqual(audio._migrate_public_voice_presets(), 0)

        with sqlite3.connect(self.db) as conn:
            row = conn.execute("""SELECT provider_voice, preview_file, preview_url
                FROM audio_voices WHERE id=2""").fetchone()
        self.assertEqual(row, ("longwan", None, None))

    def test_ready_cosyvoice_slot_repair_is_strict_and_idempotent(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("""UPDATE audio_voices
                SET provider_voice='cosyvoice-v3.5-plus-bailian-test',
                    preview_url='https://preview.example/test.mp3'
                WHERE id=1""")

        self.assertEqual(audio._repair_ready_cosyvoice_slots(), 1)
        self.assertEqual(audio._repair_ready_cosyvoice_slots(), 0)

        with sqlite3.connect(self.db) as conn:
            status = conn.execute(
                "SELECT status FROM audio_voice_slots WHERE id=1"
            ).fetchone()[0]
        self.assertEqual(status, "ready")


if __name__ == "__main__":
    unittest.main()
