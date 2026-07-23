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

    def test_current_operations_docs_use_cosyvoice(self):
        paths = (
            "README.md",
            "deploy/生产环境清单与还原手册.md",
            "docs/密钥分服务隔离与轮换流程-20260706.md",
            "docs/后端架构与API.md",
            "site/api-admin/index.html",
        )
        for path in paths:
            text = (ROOT / path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertNotIn("providers-doubao.env", text)
                self.assertNotIn("配音豆包", text)
                self.assertNotIn("走豆包 TTS", text)
        self.assertNotIn(
            "COS/豆包",
            (ROOT / "README.md").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            "DOUBAO_TOKEN",
            (ROOT / "site/api-admin/index.html").read_text(encoding="utf-8"),
        )

if __name__ == "__main__":
    unittest.main()
