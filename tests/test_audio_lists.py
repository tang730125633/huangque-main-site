import gc
import concurrent.futures
import base64
import sqlite3
import sys
import tempfile
import threading
import time
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
                    id INTEGER PRIMARY KEY, username TEXT, user_id INTEGER, slot_id TEXT, status TEXT,
                    voice_id INTEGER, reclone_count INTEGER, created_at INTEGER, updated_at INTEGER,
                    clone_started_at INTEGER, clone_upload_at INTEGER, clone_error TEXT,
                    clone_upload_speaker_id TEXT, clone_upload_response TEXT,
                    clone_baseline_version TEXT, clone_baseline_icl_speaker_id TEXT,
                    clone_baseline_demo_audio TEXT);
                CREATE TABLE audio_voices(
                    id INTEGER PRIMARY KEY, scope TEXT, username TEXT, voice_key TEXT, display_name TEXT,
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

    def test_clone_status_waits_for_playable_preview(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE audio_voice_slots SET status='ready' WHERE id=1")
            conn.execute("""UPDATE audio_voices
                SET provider_voice='cosyvoice-v3.5-plus-bailian-test', preview_url=NULL
                WHERE id=1""")

        with patch.object(audio.cosyvoice, "enabled", return_value=True), \
                patch.object(audio.cosyvoice, "voice_status", return_value=("OK", {})), \
                patch.object(audio, "_cosy_backfill_preview_async", return_value=True) as backfill:
            result = audio.check_clone_status("alice", "S_test")

        self.assertEqual(result["status"], "training")
        self.assertTrue(result["preview_pending"])
        backfill.assert_called_once_with(
            "cosyvoice-v3.5-plus-bailian-test", "alice", "vip"
        )
        with sqlite3.connect(self.db) as conn:
            status = conn.execute(
                "SELECT status FROM audio_voice_slots WHERE id=1"
            ).fetchone()[0]
        self.assertEqual(status, "training")

    def test_clone_status_keeps_training_during_provider_handoff(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE audio_voice_slots SET clone_started_at=? WHERE id=1",
                (int(time.time()),),
            )
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

    def test_stale_placeholder_training_is_recoverable_after_restart(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("""UPDATE audio_voice_slots SET status='training',clone_started_at=1,
                clone_upload_at=1,updated_at=1 WHERE id=1""")
        with patch.object(audio.cosyvoice, "enabled", return_value=True):
            result = audio.check_clone_status("alice", "S_test")
        self.assertEqual("failed", result["status"])
        self.assertIn("重新上传", result["clone_error"])
        with sqlite3.connect(self.db) as conn:
            status = conn.execute(
                "SELECT status FROM audio_voice_slots WHERE id=1"
            ).fetchone()[0]
        self.assertEqual("failed", status)

    def test_voice_clone_request_replays_without_starting_a_second_clone(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE audio_voice_slots SET status='ready' WHERE id=1")
            conn.execute("""INSERT INTO audio_voice_slots(
                id,username,user_id,slot_id,status,voice_id,reclone_count,created_at,updated_at
            ) VALUES(2,'alice',1,'S_other','active',NULL,0,1,1)""")
        created = audio.mark_clone_training(
            "alice", "S_test", "重新录制", "clone-request-0001", "digest-a",
        )
        replay = audio.clone_request_replay(
            "alice", "S_test", "clone-request-0001", "digest-a",
        )
        self.assertEqual(created["voice_key"], replay["voice_key"])
        self.assertEqual("training", replay["status"])
        self.assertTrue(replay["replayed"])
        atomic_replay = audio.mark_clone_training(
            "alice", "S_test", "重新录制", "clone-request-0001", "digest-a",
        )
        self.assertTrue(atomic_replay["replayed"])
        self.assertIsNone(audio.clone_request_replay("alice", "S_test", "other-request"))
        with self.assertRaisesRegex(audio.CloneVipValidationError, "不同的声音样音") as mismatch:
            audio.clone_request_replay(
                "alice", "S_test", "clone-request-0001", "digest-b",
            )
        self.assertEqual("idempotency_conflict", mismatch.exception.code)
        with self.assertRaisesRegex(audio.CloneVipValidationError, "不同的声音样音"):
            audio.mark_clone_training(
                "alice", "S_other", "另一个声音", "clone-request-0001", "digest-a",
            )

    def test_concurrent_same_voice_clone_request_has_one_atomic_claim(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE audio_voice_slots SET status='ready' WHERE id=1")
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=3)
            return audio.mark_clone_training(
                "alice", "S_test", "并发录制", "clone-concurrent-001", "digest-concurrent",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(1, sum(bool(item.get("replayed")) for item in results))
        self.assertEqual(1, sum(not item.get("replayed") for item in results))

    def test_superseded_clone_worker_cannot_overwrite_or_fail_new_request(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE audio_voice_slots SET status='ready' WHERE id=1")
        audio.mark_clone_training(
            "alice", "S_test", "旧请求", "clone-old-request", "digest-old",
        )
        with sqlite3.connect(self.db) as conn:
            conn.execute("""UPDATE audio_voice_slots
                SET status='training',updated_at=1,clone_upload_at=1 WHERE id=1""")
        audio.mark_clone_training(
            "alice", "S_test", "新请求", "clone-new-request", "digest-new",
        )

        with patch.object(audio, "clone_vip_voice", side_effect=RuntimeError("old failed")):
            audio.clone_vip_voice_background(
                "alice", {"slot_id": "S_test", "_request_id": "clone-old-request"},
            )
        with sqlite3.connect(self.db) as conn:
            self.assertEqual("training", conn.execute(
                "SELECT status FROM audio_voice_slots WHERE id=1"
            ).fetchone()[0])

        def out_path(relative):
            path = Path(self.tmp.name) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            return path

        with patch.object(audio, "_out_path", side_effect=out_path), \
                patch.object(audio.cos, "enabled", return_value=True), \
                patch.object(audio.cos, "upload"), \
                patch.object(audio.cos, "object_url", return_value="https://example.test/ref.mp3"), \
                patch.object(audio.cosyvoice, "create_voice", return_value="old-provider"), \
                patch.object(audio.cosyvoice, "voice_status", return_value=("OK", {})), \
                patch.object(audio, "_cosy_backfill_preview_async") as preview:
            result = audio._clone_via_cosyvoice(
                "alice", "S_test", "旧请求", base64.b64encode(b"audio").decode(),
                "clone-old-request",
            )
        self.assertEqual("superseded", result["status"])
        preview.assert_not_called()
        with sqlite3.connect(self.db) as conn:
            provider = conn.execute(
                "SELECT provider_voice FROM audio_voices WHERE voice_key='vip_S_test'"
            ).fetchone()[0]
        self.assertEqual("S_test", provider)

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

    def test_provider_poll_cannot_finish_a_newer_clone_request(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("""UPDATE audio_voices SET
                provider_voice='cosyvoice-v3.5-plus-bailian-old',preview_url=NULL WHERE id=1""")
            conn.execute("""UPDATE audio_voice_slots SET status='training',
                clone_upload_response='{"idempotency_key":"old"}' WHERE id=1""")

        def provider_status(_voice):
            with sqlite3.connect(self.db) as conn:
                conn.execute("UPDATE audio_voices SET provider_voice='S_test' WHERE id=1")
                conn.execute("""UPDATE audio_voice_slots SET status='training',
                    clone_upload_response='{"idempotency_key":"new"}' WHERE id=1""")
            return "OK", {}

        with patch.object(audio.cosyvoice, "enabled", return_value=True), \
                patch.object(audio.cosyvoice, "voice_status", side_effect=provider_status):
            result = audio.check_clone_status("alice", "S_test")
        self.assertEqual("training", result["status"])
        with sqlite3.connect(self.db) as conn:
            status = conn.execute(
                "SELECT status FROM audio_voice_slots WHERE id=1"
            ).fetchone()[0]
        self.assertEqual("training", status)

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
