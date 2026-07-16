import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "xiaotan.service"


class XiaotanServiceTests(unittest.TestCase):
    def test_unit_uses_production_entrypoint_and_restart_policy(self):
        text = UNIT.read_text(encoding="utf-8")
        self.assertIn(
            "ExecStart=/home/ubuntu/douyin-scraper/.venv/bin/python start.py",
            text,
        )
        self.assertIn("Restart=always", text)
        self.assertNotIn("--host 0.0.0.0 --port 8501", text)

    def test_unit_keeps_existing_hardening(self):
        text = UNIT.read_text(encoding="utf-8")
        self.assertIn("Wants=network-online.target", text)
        self.assertIn("After=network-online.target", text)
        self.assertIn("StartLimitIntervalSec=300", text)
        self.assertIn("Environment=PYTHONUNBUFFERED=1", text)
        self.assertIn("SyslogIdentifier=xiaotan", text)


if __name__ == "__main__":
    unittest.main()
