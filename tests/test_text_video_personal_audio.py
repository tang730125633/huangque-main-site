import gc
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import audio, core


class TextVideoPersonalAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "audio.db")
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TABLE audio_voices(
                    id INTEGER PRIMARY KEY,
                    scope TEXT NOT NULL,
                    username TEXT NOT NULL,
                    voice_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    provider_voice TEXT NOT NULL,
                    preview_file TEXT,
                    preview_url TEXT,
                    slot_id TEXT,
                    created_at INTEGER,
                    updated_at INTEGER
                );
                CREATE TABLE audio_voice_slots(
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    voice_id INTEGER,
                    created_at INTEGER,
                    updated_at INTEGER
                );
                INSERT INTO audio_voices VALUES(
                    1,'personal','alice','vip_alice','Alice voice',
                    'cosyvoice-v3.5-plus-bailian-alice',NULL,NULL,'slot_alice',1,1
                );
                INSERT INTO audio_voice_slots VALUES(
                    1,'alice','slot_alice','ready',1,1,1
                );
                INSERT INTO audio_voices VALUES(
                    2,'personal','bob','vip_bob','Bob voice',
                    'cosyvoice-v3.5-plus-bailian-bob',NULL,NULL,'slot_bob',1,1
                );
                INSERT INTO audio_voice_slots VALUES(
                    2,'bob','slot_bob','ready',2,1,1
                );
                INSERT INTO audio_voices VALUES(
                    3,'personal','alice','vip_training','Training voice',
                    'S_training',NULL,NULL,'slot_training',1,1
                );
                INSERT INTO audio_voice_slots VALUES(
                    3,'alice','slot_training','training',3,1,1
                );
                INSERT INTO audio_voices VALUES(
                    4,'public','','S_public','Public voice',
                    'longwan',NULL,NULL,NULL,1,1
                );
            """)
        self.db_patch = patch.object(core, "AUDIO_DB", self.db)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        gc.collect()
        self.tmp.cleanup()

    def test_synthesizes_owned_ready_personal_voice_without_persistence(self):
        with patch.object(audio.cosyvoice, "enabled", return_value=True), \
             patch.object(audio.cosyvoice, "synth", return_value=b"mp3-bytes") as synth, \
             patch.object(audio, "_out_path", side_effect=AssertionError("must not write")), \
             patch.object(audio, "record_audio_asset", side_effect=AssertionError("must not persist")), \
             patch.object(audio, "public_url", side_effect=AssertionError("must not publish")):
            result = audio.synthesize_owned_voice_segment(
                "alice", "vip_alice", "第一段", speed=1.1, pitch=2, volume=4
            )

        self.assertEqual(result, {
            "content": b"mp3-bytes",
            "content_type": "audio/mpeg",
            "voice_key": "vip_alice",
            "voice_scope": "personal",
            "provider": "cosyvoice",
        })
        synth.assert_called_once_with(
            "cosyvoice-v3.5-plus-bailian-alice",
            "第一段",
            rate=1.1,
            pitch=1.0 + 2 / 24.0,
            volume=52,
        )

    def test_rejects_cross_user_voice_before_synthesis(self):
        with patch.object(audio.cosyvoice, "synth") as synth:
            with self.assertRaisesRegex(ValueError, "个人音色不存在或不可用"):
                audio.synthesize_owned_voice_segment("alice", "vip_bob", "测试")
        synth.assert_not_called()

    def test_rejects_training_voice_before_synthesis(self):
        with patch.object(audio.cosyvoice, "synth") as synth:
            with self.assertRaisesRegex(ValueError, "个人音色不存在或不可用"):
                audio.synthesize_owned_voice_segment("alice", "vip_training", "测试")
        synth.assert_not_called()

    def test_rejects_public_voice_before_synthesis(self):
        with patch.object(audio.cosyvoice, "synth") as synth:
            with self.assertRaisesRegex(ValueError, "个人音色不存在或不可用"):
                audio.synthesize_owned_voice_segment("alice", "S_public", "测试")
        synth.assert_not_called()

    def test_rejects_invalid_text_and_controls_before_synthesis(self):
        invalid = [
            {"text": ""},
            {"text": "字" * 1001},
            {"text": "测试", "speed": 0.4},
            {"text": "测试", "pitch": 13},
            {"text": "测试", "volume": 101},
        ]
        with patch.object(audio.cosyvoice, "synth") as synth:
            for case in invalid:
                with self.subTest(case=case), self.assertRaises(ValueError):
                    audio.synthesize_owned_voice_segment(
                        "alice", "vip_alice", **case
                    )
        synth.assert_not_called()

    def test_disabled_cosyvoice_never_falls_back(self):
        with patch.object(audio.cosyvoice, "enabled", return_value=False), \
             patch.object(audio.cosyvoice, "synth") as synth, \
             patch.object(audio, "_post_bytes", side_effect=AssertionError("no fallback")):
            with self.assertRaisesRegex(ValueError, "声音服务暂时不可用"):
                audio.synthesize_owned_voice_segment("alice", "vip_alice", "测试")
        synth.assert_not_called()

    def test_rejects_empty_audio_response(self):
        with patch.object(audio.cosyvoice, "enabled", return_value=True), \
             patch.object(audio.cosyvoice, "synth", return_value=b""):
            with self.assertRaisesRegex(RuntimeError, "返回为空"):
                audio.synthesize_owned_voice_segment("alice", "vip_alice", "测试")


if __name__ == "__main__":
    unittest.main()
