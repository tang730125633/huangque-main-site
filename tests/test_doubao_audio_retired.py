from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DoubaoAudioRetiredTests(unittest.TestCase):
    def test_runtime_configuration_no_longer_exports_doubao_audio(self):
        core = (ROOT / "server" / "content_domains" / "core.py").read_text(
            encoding="utf-8"
        )
        audio = (ROOT / "server" / "content_domains" / "audio.py").read_text(
            encoding="utf-8"
        )
        for name in (
            "DOUBAO_APPID",
            "DOUBAO_CLONE_RESOURCE",
            "DOUBAO_CLONE_MODEL_TYPE",
            "DOUBAO_TTS_RESOURCE",
        ):
            self.assertNotIn(name + " =", core)
        self.assertNotIn("openspeech.bytedance.com", audio)
        self.assertNotIn("generate_doubao_preview", audio)
        self.assertNotIn("query_doubao_clone_status", audio)

    def test_operations_console_uses_cosyvoice_channel(self):
        admin = (ROOT / "server" / "admin_api.py").read_text(encoding="utf-8")
        self.assertIn('"key": "cosyvoice"', admin)
        self.assertNotIn('"key": "doubao"', admin)
        self.assertNotIn("openspeech.bytedance.com", admin)

if __name__ == "__main__":
    unittest.main()
