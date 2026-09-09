# -*- coding: utf-8 -*-
import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSPIRATION = (ROOT / "site" / "workbench" / "inspiration.html").read_text(encoding="utf-8")
SHELL = (ROOT / "site" / "workbench" / "cloud-shell.js").read_text(encoding="utf-8")


class IP12EntryAndNotificationTests(unittest.TestCase):
    def test_landing_has_an_interactive_ip12_agent_entry(self):
        self.assertIn('class="agent-entry"', INSPIRATION)
        self.assertIn('id="ip12Brief"', INSPIRATION)
        self.assertIn('id="ip12Start"', INSPIRATION)
        for copy in ("让 <em>Agent</em>", "实现你的操作", "定位诊断", "内容体系", "增长方案", "启动 Agent"):
            self.assertIn(copy, INSPIRATION)
        self.assertIn("location.href='/workbench/ip12/'+(brief?'?brief='", INSPIRATION)
        self.assertNotIn('class="capRow"', INSPIRATION)
        self.assertNotIn('class="catalog-note"', INSPIRATION)
        self.assertNotIn('class="ip12-metrics"', INSPIRATION)
        self.assertNotIn('class="ip12-entry"', INSPIRATION)
        self.assertNotIn("IP12 成长档案</div></div>\n      <div style=\"font-size:12px", INSPIRATION)


if __name__ == "__main__":
    unittest.main()
