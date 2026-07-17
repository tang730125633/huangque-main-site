# -*- coding: utf-8 -*-
import base64
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import audio, core


class CosyVoiceOnlyAudioTest(unittest.TestCase):
    def test_tts_fails_closed_without_cosyvoice(self):
        with patch.object(audio, "resolve_audio_provider_voice", return_value="S_d21F8OR62"), \
                patch.object(audio.cosyvoice, "enabled", return_value=False), \
                patch.object(audio, "generate_doubao_preview", side_effect=AssertionError("豆包不得调用")), \
                patch.object(audio, "_post_bytes", side_effect=AssertionError("OpenAI TTS 不得调用")):
            with self.assertRaisesRegex(ValueError, "仅支持 CosyVoice"):
                audio.gen_audio({"text": "你好", "voice": "S_d21F8OR62", "_username": "alice"})

    def test_tts_uses_cosyvoice_without_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            output = pathlib.Path(td) / "result.mp3"
            with patch.object(audio, "resolve_audio_provider_voice", return_value="S_d21F8OR62"), \
                    patch.object(audio.cosyvoice, "enabled", return_value=True), \
                    patch.object(audio.cosyvoice, "synth", return_value=b"mp3") as synth, \
                    patch.object(audio, "_out_path", return_value=output), \
                    patch.object(audio, "public_url", return_value="https://cos/result.mp3"), \
                    patch.object(audio, "generate_doubao_preview", side_effect=AssertionError("豆包不得调用")), \
                    patch.object(audio, "_post_bytes", side_effect=AssertionError("OpenAI TTS 不得调用")):
                result = audio.gen_audio({"text": "你好", "voice": "S_d21F8OR62", "_username": "alice"})

        self.assertEqual("https://cos/result.mp3", result["url"])
        self.assertEqual("longwan", synth.call_args.args[0])

    def test_clone_fails_closed_without_cosyvoice(self):
        payload = {
            "slot_id": "slot_a",
            "audio": base64.b64encode(b"sample").decode(),
            "audio_format": "mp3",
        }
        with patch.object(audio.cosyvoice, "enabled", return_value=False), \
                patch.object(audio.urllib.request, "urlopen", side_effect=AssertionError("豆包不得调用")):
            with self.assertRaisesRegex(ValueError, "仅支持 CosyVoice"):
                audio.clone_vip_voice("alice", payload)

    def test_public_preview_is_generated_by_cosyvoice(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(pathlib.Path(td) / "audio.db")
            db_patch = patch.object(core, "AUDIO_DB", db)
            db_patch.start()
            try:
                core.init_audio_db()
                with core.closing(core.adb()) as conn:
                    conn.execute("""UPDATE audio_voices
                        SET preview_file=NULL, preview_url=NULL
                        WHERE scope='public' AND username='' AND voice_key='S_d21F8OR62'""")
                    voice_id = conn.execute("""SELECT id FROM audio_voices
                        WHERE scope='public' AND username='' AND voice_key='S_d21F8OR62'""").fetchone()["id"]
                    conn.commit()
                target = pathlib.Path(td) / "preview.mp3"
                row = {"id": voice_id, "scope": "public", "voice_key": "S_d21F8OR62",
                       "provider_voice": "S_d21F8OR62", "preview_url": ""}
                with patch.object(audio.cosyvoice, "enabled", return_value=True), \
                        patch.object(audio.cosyvoice, "synth", return_value=b"preview") as synth, \
                        patch.object(audio, "_out_path", return_value=target), \
                        patch.object(audio, "public_url", return_value="https://cos/preview.mp3"), \
                        patch.object(audio, "generate_doubao_preview", side_effect=AssertionError("豆包不得调用")):
                    result = audio._ensure_public_voice_preview(row)
            finally:
                db_patch.stop()

        self.assertEqual("https://cos/preview.mp3", result["preview_url"])
        self.assertEqual("longwan", synth.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
