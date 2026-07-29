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
        self.assertIn("var openModuleSteps=[5,5,5,5,4,3,3,4], openStepKeys=[];", SHELL)
        self.assertIn("openStepKeys.push(moduleIndex+'-'+stepIndex)", SHELL)
        self.assertIn("var progressed=openStepKeys.filter", SHELL)
        self.assertIn("var skipped=openStepKeys.filter", SHELL)
        self.assertNotIn("Object.keys(answers)", SHELL)
        self.assertIn("answer&&(answer.confirmed||answer.skipped)", SHELL)
        self.assertIn("if(progressed>=34&&!skipped.length) return", SHELL)
        self.assertIn("+' · 首轮进度 '+progressed+'/34'", SHELL)
        self.assertIn("openStepKeys.find(function(key)", SHELL)
        self.assertNotIn("if(progressed>=54&&!skipped.length) return", SHELL)
        self.assertNotIn("+' · 首轮进度 '+progressed+'/54'", SHELL)
        self.assertIn("id:'ip12-progress-'+project.id+'-'+progressed+'-'+skipped.length", SHELL)
        self.assertIn("title:skipped.length?'IP12 有 '+skipped.length+' 项待补':'继续完善 IP12'", SHELL)
        self.assertIn("ip12.html?project='+encodeURIComponent(project.id)+'&module='+encodeURIComponent(moduleIndex+1)+'&step='+encodeURIComponent(stepIndex+1)", SHELL)
        self.assertIn("fetch('/api/gen/digital-ip/projects'", SHELL)
        self.assertIn("d.ip12_skips=ip12ProgressNotices(all[2])", SHELL)
        self.assertNotIn("/api/admin/users/notification", SHELL)
        self.assertNotIn("/api/auth/admin/notifications", SHELL)

    def test_notification_counts_only_the_34_open_steps(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        source = SHELL[SHELL.index("function ip12ProgressNotices"):SHELL.index("function buildNotices")]
        script = source + """
const stepCounts=[5,5,5,5,4,3,3,4], complete={};
stepCounts.forEach((count,moduleIndex)=>{for(let stepIndex=0;stepIndex<count;stepIndex++)complete[`${moduleIndex}-${stepIndex}`]={confirmed:true};});
complete['8-0']={skipped:true};
const partial={'0-0':{confirmed:true},'0-1':{confirmed:true},'8-0':{confirmed:true},'11-4':{skipped:true}};
const done=ip12ProgressNotices({items:[{id:'done',state:{questionnaire_state:{answers:complete}}}]});
const next=ip12ProgressNotices({items:[{id:'partial',title:'测试项目',state:{questionnaire_state:{answers:partial}}}]});
console.log(JSON.stringify({done,next}));
"""
        result = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(result["done"], [])
        self.assertEqual(len(result["next"]), 1)
        self.assertIn("2/34", result["next"][0]["detail"])
        self.assertIn("module=1&step=3", result["next"][0]["href"])


if __name__ == "__main__":
    unittest.main()
