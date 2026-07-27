# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSPIRATION = (ROOT / "site" / "workbench" / "inspiration.html").read_text(encoding="utf-8")
SHELL = (ROOT / "site" / "workbench" / "cloud-shell.js").read_text(encoding="utf-8")


class IP12EntryAndNotificationTests(unittest.TestCase):
    def test_landing_has_a_full_ip12_custom_plan_entry(self):
        self.assertIn('class="ip12-hero hv-lift"', INSPIRATION)
        self.assertIn('href="ip12.html"', INSPIRATION)
        for copy in ("数字化 IP", "行业痛点", "12", "54", "图片、视频行动计划", "开始制作"):
            self.assertIn(copy, INSPIRATION)
        self.assertNotIn("IP12 成长档案</div></div>\n      <div style=\"font-size:12px", INSPIRATION)

    def test_skipped_answers_become_local_ip12_reminders_only(self):
        self.assertIn("function ip12ProgressNotices(payload)", SHELL)
        self.assertIn("questionnaire_state;", SHELL)
        self.assertIn("questionnaire&&questionnaire.answers", SHELL)
        self.assertIn("answer&&(answer.confirmed||answer.skipped)", SHELL)
        self.assertIn("if(progressed>=54&&!skipped.length) return", SHELL)
        self.assertIn("id:'ip12-progress-'+project.id+'-'+progressed+'-'+skipped.length", SHELL)
        self.assertIn("title:skipped.length?'IP12 有 '+skipped.length+' 项待补':'继续完善 IP12'", SHELL)
        self.assertIn("ip12.html?project='+encodeURIComponent(project.id)+'&module='+encodeURIComponent(moduleIndex+1)+'&step='+encodeURIComponent(stepIndex+1)", SHELL)
        self.assertIn("fetch('/api/gen/digital-ip/projects'", SHELL)
        self.assertIn("d.ip12_skips=ip12ProgressNotices(all[2])", SHELL)
        self.assertNotIn("/api/admin/users/notification", SHELL)
        self.assertNotIn("/api/auth/admin/notifications", SHELL)


if __name__ == "__main__":
    unittest.main()
