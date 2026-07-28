import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


PAGE = Path(__file__).resolve().parents[1] / "site" / "workbench" / "ip12.html"
CORE = Path(__file__).resolve().parents[1] / "server" / "content_domains" / "core.py"


class IP12AIUITests(unittest.TestCase):
    def test_drafts_survive_quick_exit_without_overwriting_newer_remote_state(self):
        html = PAGE.read_text(encoding="utf-8")

        self.assertIn("function localDraft()", html)
        self.assertIn("JSON.stringify({state,title:$(\"projectTitle\").value,revision:project?.revision,savedAt:Date.now()})", html)
        self.assertIn("function shouldRestoreLocal(draft)", html)
        self.assertIn("draft.revision===remoteRevision", html)
        self.assertIn("window.addEventListener(\"pagehide\"", html)
        self.assertIn("keepalive:true", html)
        self.assertNotIn('window.addEventListener("beforeunload",saveDraft)', html)

    def test_recovered_local_draft_is_synced_without_waiting_for_another_edit(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn("const remote=project.state?.questionnaire_state, draft=localDraft(), restoreLocal=shouldRestoreLocal(draft);", html)
        self.assertIn("if(restoreLocal)queueProjectSave();", html)
        load_project = html[html.index("async function loadProject"):html.index("function keyFor")]
        self.assertLess(load_project.index("render();"), load_project.index("if(restoreLocal)queueProjectSave();"))

    def test_editing_stale_analysis_returns_the_stale_state_for_the_notice(self):
        html = PAGE.read_text(encoding="utf-8")
        save_draft = html[html.index("function saveDraft()"):html.index("async function confirmCurrent")]
        self.assertIn("return stale;", save_draft)
        self.assertIn('if(stale){ showToast("回答已修改，请重新分析并确认"); }', html)

    def test_editing_a_confirmed_answer_requires_reconfirmation(self):
        html = PAGE.read_text(encoding="utf-8")

        self.assertIn("confirmedValue:answerText(step,answer)", html)
        self.assertIn("const changed=confirmedAnswerChanged(step,current,next);", html)
        self.assertIn("confirmed:false", html)
        self.assertIn("if(changed)delete state.profile[module.id];", html)

    def test_confirmed_choice_survives_navigation_but_real_choice_change_relocks(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        html = PAGE.read_text(encoding="utf-8")
        functions = "\n".join(
            re.search(rf"function {name}\(.*?\n    \}}", html, re.S).group(0)
            for name in ("answerText", "confirmedAnswerChanged")
        )
        script = functions + """
const select={type:'select'}, multi={type:'multi'};
console.log(JSON.stringify([
  confirmedAnswerChanged(select,{confirmed:true,confirmedValue:'获客'},{choice:'获客'}),
  confirmedAnswerChanged(select,{confirmed:true,confirmedValue:'获客'},{choice:'复购'}),
  confirmedAnswerChanged(multi,{confirmed:true,confirmedValue:'获客、复购'},{choice:['获客','复购']}),
  confirmedAnswerChanged(multi,{confirmed:true,confirmedValue:'获客、复购'},{choice:['复购']})
]));
"""
        got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got, [False, True, False, True])

    def test_all_ai_requests_require_the_existing_explicit_consent(self):
        html = PAGE.read_text(encoding="utf-8")

        self.assertIn("我同意在主动发送时", html)
        guide_source = html[html.index("async function askGuide"):html.index("function runGuideAction")]
        self.assertIn('if(!$("aiConsent").checked)', guide_source)
        self.assertIn("consent:true", guide_source)
        self.assertIn("consent:true", html[html.index("async function analyzeCurrent"):html.index("async function confirmCandidate")])

    def test_scroll_respects_reduced_motion(self):
        html = PAGE.read_text(encoding="utf-8")

        self.assertIn("function scrollBehavior()", html)
        self.assertIn("prefers-reduced-motion: reduce", html)
        self.assertNotIn('behavior:"smooth"', html)

    def test_skipped_steps_are_persisted_without_ai_or_profile_and_can_be_resumed(self):
        html = PAGE.read_text(encoding="utf-8")

        self.assertIn('id="skipBtn"', html)
        self.assertIn("function skipCurrent()", html)
        self.assertIn("confirmed:false,skipped:true", html)
        self.assertIn("confirmed:true,confirmedValue:answerText(step,answer),skipped:false", html)
        self.assertIn("delete state.analyses[keyFor()];", html)
        self.assertIn("delete state.profile[module.id];", html)
        self.assertIn("function progressedStepCount(){ return confirmedStepCount()+skippedSteps().length; }", html)
        self.assertIn('confirmed===totalSteps?`当前开放 · ${totalSteps} / ${totalSteps}`', html)
        self.assertIn("首轮已走完", html)
        self.assertIn('id="skippedItems"', html)
        self.assertIn('id="reportUnlock"', html)
        self.assertIn("完成模块 1–4 后生成阶段报告", html)
        self.assertIn("ip12-report.html?project=", html)
        self.assertIn("function openFoundationReport", html)
        self.assertIn("data-resume-module=", html)
        self.assertIn("data-resume-step=", html)

        skip_source = html[html.index("function skipCurrent()"):html.index("function advanceCurrent(")]
        self.assertNotIn("analyzeCurrent", skip_source)
        self.assertNotIn("confirmCandidate", skip_source)
        self.assertNotIn("fetch(", skip_source)

    def test_project_module_step_query_can_open_a_skipped_step(self):
        html = PAGE.read_text(encoding="utf-8")

        self.assertIn("new URLSearchParams(location.search)", html)
        self.assertIn('get("project")', html)
        self.assertIn("function entryStep()", html)
        self.assertIn("return {moduleIndex:module-1,stepIndex:step-1};", html)
        self.assertIn("const target=entryStep();", html)

    def test_ai_is_explicit_structured_and_keeps_confirmation_separate(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn("/api/gen/digital-ip/diagnose", html)
        self.assertIn("/api/gen/digital-ip/guide", html)
        self.assertIn("AI 分析本步", html)
        self.assertIn("小黄雀 · IP 成长教练", html)
        self.assertIn("我不知道怎么开始", html)
        self.assertIn("一次只问一个问题", html)
        self.assertIn("不会监听输入", html)
        self.assertIn("AI 分析服务 · 结构化分析", html)
        self.assertIn("credentials:\"include\"", html)
        self.assertIn("AI 的提问和整理只形成建议草稿", html)
        self.assertNotIn("OPENAI_API_KEY", html)

    def test_coach_ignores_duplicate_send_while_reply_is_pending(self):
        html = PAGE.read_text(encoding="utf-8")
        guide = html[html.index("async function askGuide("):html.index("function applyGuideDraft(")]

        self.assertIn("if(guideBusy)return;", guide)
        self.assertIn("guideBusy=true;", guide)
        self.assertIn("guideBusy=false;", guide)

    def test_brand_and_visible_ai_labels_are_neutral(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn('class="brand" href="/" aria-label="返回黄雀主站首页"', html)
        self.assertIn("交给 AI 做采访整理", html)
        self.assertNotIn("OpenAI", html)
        self.assertNotIn("STRUCTURED", html)

    def test_coach_keeps_continuous_history_and_records_one_answer_before_advancing(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn("function visibleGuideTurns()", html)
        self.assertIn("function requestGuideTurns()", html)
        render_source = html[html.index("function renderCoach()"):html.index("async function askGuide")]
        self.assertIn("const turns=visibleGuideTurns()", render_source)
        ask_source = html[html.index("async function askGuide"):html.index("function applyGuideDraft")]
        self.assertIn("const priorTurns=requestGuideTurns()", ask_source)
        latest_source = html[html.index("function latestGuideReply"):html.index("function renderCoach")]
        self.assertIn("currentGuideTurns()", latest_source)
        self.assertIn("const GUIDE_TURN_LIMIT = 240;", html)
        self.assertIn("].slice(-GUIDE_TURN_LIMIT);", html)
        self.assertIn("activeGuideTurns().slice(-GUIDE_TURN_LIMIT)", html)
        self.assertIn("keywords:guide.suggested_answer||answer.keywords", ask_source)
        self.assertIn("confirmed:!followUp.length", ask_source)
        self.assertIn("if(followUp.length)", ask_source)
        self.assertIn("advanceCurrent(false)", ask_source)

    def test_coach_followups_are_one_question_at_a_time(self):
        source = CORE.with_name("digital_ip.py").read_text(encoding="utf-8")
        self.assertRegex(source, r'(?s)"follow_up_questions":\s*\{.*?"maxItems":\s*1')

    def test_advancing_a_step_never_calls_ai(self):
        html = PAGE.read_text(encoding="utf-8")
        source = html[html.index("function advanceCurrent("):html.index("function goBack()")]
        for forbidden in ("analyzeCurrent(", "askGuide(", "fetch("):
            self.assertNotIn(forbidden, source)

    def test_module_four_requires_a_confirmed_foundation_report_before_content_modules(self):
        html = PAGE.read_text(encoding="utf-8")
        outcome = html[html.index("function renderFoundationOutcome"):html.index("function render()")]
        self.assertIn('data-open-foundation-report', outcome)
        self.assertIn("function openFoundationReport", html)
        self.assertIn("ip12-report.html?project=", html)
        self.assertIn("function reportPdfUrl()", html)
        self.assertIn('download>下载 PDF</a>', outcome)
        self.assertIn("stage.report_id", outcome)
        source = CORE.with_name("digital_ip.py").read_text(encoding="utf-8")
        self.assertIn("FOUNDATION_MODULES = 4", source)
        self.assertIn("请先生成并确认模块 1–4 阶段报告，再进入模块 5–6", source)

    def test_navigation_words_do_not_pollute_answers_and_modules_resume_unfinished_steps(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        html = PAGE.read_text(encoding="utf-8")
        function = re.search(r"function navigationCommand\(.*?\n    \}", html, re.S).group(0)
        script = function + """
console.log(JSON.stringify([
  navigationCommand('继续'),
  navigationCommand('上一题'),
  navigationCommand('暂时跳过'),
  navigationCommand('你跳一下模块2'),
  navigationCommand('广州')
]));
"""
        got = json.loads(subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
        ).stdout)
        self.assertEqual(got[:3], [{"type": "next"}, {"type": "previous"}, {"type": "skip"}])
        self.assertEqual(got[3], {"type": "module", "moduleIndex": 1})
        self.assertIsNone(got[4])
        ask = html[html.index("async function askGuide"):html.index("function applyGuideDraft")]
        self.assertLess(ask.index("handleNavigationMessage(message)"), ask.index('if(!$("aiConsent").checked)'))
        self.assertLess(ask.index("handleNavigationMessage(message)"), ask.index("fetch(GUIDE_API_URL"))
        modules = html[html.index("function renderModules"):html.index("function renderInteraction")]
        self.assertIn("firstUnresolvedStep(state.moduleIndex)", modules)

    def test_editing_a_confirmed_foundation_answer_relocks_the_outcome(self):
        html = PAGE.read_text(encoding="utf-8")
        source = html[html.index("function saveDraft"):html.index("async function confirmCurrent")]
        self.assertIn("if(changed)delete state.profile[module.id]", source)
        self.assertIn("if(changed)renderFoundationOutcome();", source)

    def test_project_recovery_and_consent_remain_but_extra_workbench_controls_are_hidden(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn("/api/gen/digital-ip/projects", html)
        self.assertIn("profile:state.profile", html)
        self.assertIn("interviewVersion:2", html)
        self.assertIn("remote?.interviewVersion===2", html)
        self.assertIn("async function flushProjectSave()", html)
        self.assertGreaterEqual(html.count("await flushProjectSave();"), 2)
        self.assertIn('`${prefix}:${project.id}`', html)
        self.assertIn("LEGACY_STORAGE_KEY", html)
        self.assertIn("let state = structuredClone(initialState);", html)
        self.assertNotIn("localStorage.setItem(STORAGE_KEY,JSON.stringify(state))", html)
        self.assertIn("saveProject(true)", html)
        self.assertIn("项目已在另一端更新，请重新查看后再操作", html)
        self.assertIn(".project-card{display:none}", html)
        self.assertIn(".memory{display:none}", html)
        self.assertIn(".privacy,.side-actions{display:none!important}", html)
        self.assertIn(".memory-drawer-btn,.coach-quick,.coach-actions,#interaction,.support-details,#nextBtn{display:none!important}", html)

    def test_intake_is_conversation_first_and_growth_agents_are_coming_soon(self):
        html = PAGE.read_text(encoding="utf-8")

        self.assertIn("一次聚焦一个主题", html)
        self.assertIn("复杂问题也只拆成 2–3 个短确认点", html)
        self.assertIn("你可以直接选一个、改写，或补充自己的答案", html)
        self.assertIn("你大概在哪个年龄段？比如 25–30、31–35、36–40", html)
        self.assertNotIn("你希望报告里如何呈现你的性别和年龄段", html)
        self.assertIn("<b>我先这样理解</b>", html)
        self.assertNotIn("<b>采访记录</b>", html)
        self.assertIn("const answers=confirmedContext()", html)
        self.assertIn("return [...answers,...profiles]", html)
        self.assertIn("return source||", html)
        self.assertNotIn("const firstQuestion=", html)
        self.assertEqual(html.count('id="coachCard"'), 1)
        self.assertNotIn('id="coachFloat"', html)
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        source = re.search(r"const interviewStep=.*?\n    \];", html, re.S).group(0)
        script = source + """
console.log(JSON.stringify({
  open: MODULES.map(module=>module.availability!=="coming_soon"),
  foundationSteps: MODULES.slice(0,4).reduce((total,module)=>total+module.steps.length,0),
  activeSteps: MODULES.slice(0,6).reduce((total,module)=>total+module.steps.length,0)
}));
"""
        got = json.loads(subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
        ).stdout)
        self.assertEqual(got["open"], [True] * 6 + [False] * 6)
        self.assertEqual(got["foundationSteps"], 30)
        self.assertEqual(got["activeSteps"], 38)
        self.assertIn('availability:"coming_soon"', html)
        self.assertIn('coming?"正在开发中，敬请期待"', html)
        mobile = html[html.index("@media(max-width:760px)"):]
        self.assertIn(".app{display:block;min-height:100dvh}", mobile)
        self.assertIn(".module-scroll{display:block;overflow-y:auto", mobile)
        self.assertIn(".module-state{display:block", mobile)
        self.assertNotIn("请写下门店类型与规模", html)
        for field in ("姓名或昵称", "最强能力", "三个性格词", "记忆金句", "绝境翻身", "一年目标", "首条口播主题"):
            self.assertIn(field, html)

    def test_guide_summary_prioritizes_recent_confirmed_answers(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        html = PAGE.read_text(encoding="utf-8")
        source = re.search(r"const interviewStep=.*?\n    \];", html, re.S).group(0)
        source += "\n" + "\n".join(
            re.search(rf"function {name}\(.*?\n    \}}", html, re.S).group(0)
            for name in ("answerText", "confirmedContext", "currentIPSummary")
        )
        script = source + """
let state={
  profile:{1:{title:'旧模块摘要',summary:'旧'.repeat(900)}},
  answers:{'0-0':{confirmed:true,text:'LATEST-CONFIRMED-ANSWER'}}
};
console.log(currentIPSummary());
"""
        summary = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertTrue(summary.startswith("定位诊断 / 姓名或昵称：LATEST-CONFIRMED-ANSWER"))
        self.assertLessEqual(len(summary), 800)

    def test_invalid_legacy_answer_keys_do_not_count_as_open_steps(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        html = PAGE.read_text(encoding="utf-8")
        source = re.search(r"const interviewStep=.*?\n    \];", html, re.S).group(0)
        source += "\n" + "\n".join(
            re.search(rf"function {name}\(.*?\n    \}}", html, re.S).group(0)
            for name in ("isOpenModuleIndex", "isOpenStepKey")
        )
        script = source + """
console.log(JSON.stringify([
  isOpenStepKey('0-0'),
  isOpenStepKey('6-0'),
  isOpenStepKey('99-0'),
  isOpenStepKey('0-99'),
  isOpenStepKey('invalid')
]));
"""
        got = json.loads(subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
        ).stdout)
        self.assertEqual(got, [True, False, False, False, False])

    def test_paid_ip12_ai_routes_follow_membership_enforcement(self):
        source = CORE.read_text(encoding="utf-8") + CORE.with_name("digital_ip.py").read_text(encoding="utf-8")
        self.assertIn("_membership_enforcement_enabled", source)
        self.assertIn("_digital_ip_membership_required(user)", source)
        self.assertIn('"code": "membership_required"', source)

    def test_upload_mime_extension_fallbacks(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        html = PAGE.read_text(encoding="utf-8")
        source = re.search(r"const UPLOAD_MIME = .*?;", html).group(0) + "\n" + re.search(r"function uploadMime\(file\)\{.*?\}", html).group(0)
        names = ["a.pdf", "a.docx", "a.pptx", "a.xlsx", "a.md", "a.jpeg"]
        script = source + "\nconsole.log(JSON.stringify(%s.map(name=>uploadMime({name,type:'application/octet-stream'}))));" % json.dumps(names)
        got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got, ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/markdown", "image/jpeg"])
        self.assertIn("去既有图片工具", html)
        self.assertIn("去既有视频工具", html)
        self.assertIn('project?.status==="confirmed"', html)

    def test_inspiration_card_opens_ip12_not_video(self):
        inspiration = PAGE.parent / "inspiration.html"
        html = inspiration.read_text(encoding="utf-8")
        self.assertIn('href="ip12.html"', html)
        self.assertIn("开始制作", html)


if __name__ == "__main__":
    unittest.main()
