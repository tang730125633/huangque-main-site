import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class IP12IntakePlaygroundTests(unittest.TestCase):
    def test_launcher_is_local_v1_codex_only(self):
        source = (ROOT / "tests/ip12_intake_playground.py").read_text(encoding="utf-8")
        self.assertIn('"HERMES_AI_TRANSPORT"] = "codex-cli"', source)
        self.assertIn('"HERMES_IP12_SKILL_PIPELINE_DEFAULT"] = "v1"', source)
        self.assertIn('host="127.0.0.1"', source)
        self.assertIn('@server.app.route("/intake-lab")', source)

    def test_page_focuses_only_on_intake_tuning(self):
        page = (ROOT / "server/hermes_ip12/templates/intake_lab.html").read_text(encoding="utf-8")
        for text in ("只调问题、回复风格、采集结果", "当前正在问", "当前总结稿", "已经采集的内容", "答非所问", "总结不准"):
            self.assertIn(text, page)
        self.assertNotIn("模块 5", page)
        self.assertNotIn("生成 PDF", page)


if __name__ == "__main__":
    unittest.main()
