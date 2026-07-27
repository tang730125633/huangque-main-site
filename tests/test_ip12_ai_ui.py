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

        self.assertIn("本次 AI 教练或分析", html)
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
        self.assertIn('confirmed===totalSteps?"完整档案 · 54 / 54"', html)
        self.assertIn("首轮已走完", html)
        self.assertIn('id="skippedItems"', html)
        self.assertIn('id="reportUnlock"', html)
        self.assertIn("progressed===totalSteps&&project?.id", html)
        self.assertIn("ip12-report.html?project=", html)
        self.assertIn('id="openReportBtn"', html)
        report_source = html[html.index('$("openReportBtn")'):html.index('$("skippedItems").querySelectorAll("[data-resume-module]")')]
        self.assertIn("await flushProjectSave()", report_source)
        self.assertLess(report_source.index("await flushProjectSave()"), report_source.index("location.href=`ip12-report.html"))
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
        self.assertIn("我不知道怎么填", html)
        self.assertIn("告诉我下一步", html)
        self.assertIn("不会监听输入", html)
        self.assertIn("AI 分析服务 · 结构化分析", html)
        self.assertIn("credentials:\"include\"", html)
        self.assertIn("AI 只给建议", html)
        self.assertNotIn("OPENAI_API_KEY", html)

    def test_brand_and_visible_ai_labels_are_neutral(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn('class="brand" href="/" aria-label="返回黄雀主站首页"', html)
        self.assertIn("发送给 AI 分析服务", html)
        self.assertIn("AI 分析服务进行结构化分析", html)
        self.assertNotIn("OpenAI", html)
        self.assertNotIn("STRUCTURED", html)

    def test_coach_welcome_rotation_pauses_and_yields_to_real_replies(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn("const COACH_WELCOME_MESSAGES = [", html)
        self.assertIn("function stopCoachWelcomeRotation()", html)
        self.assertIn("function startCoachWelcomeRotation()", html)
        self.assertIn("if(document.hidden||state.guideTurns.length)return;", html)
        self.assertIn('document.addEventListener("visibilitychange"', html)
        self.assertIn("if(turns.length)stopCoachWelcomeRotation();else startCoachWelcomeRotation();", html)
        self.assertIn("@keyframes coachWelcome", html)
        self.assertIn("@keyframes coachWelcomeFade", html)

    def test_coach_keeps_six_messages_and_only_applies_suggested_text_as_a_draft(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn("const priorTurns=state.guideTurns.slice(-6)", html)
        self.assertIn("].slice(-6);", html)
        self.assertIn('data-guide-use-draft="${index}"', html)
        self.assertIn("function applyGuideDraft(value)", html)
        source = html[html.index("function applyGuideDraft"):html.index("function runGuideAction")]
        self.assertIn("answer.value=draft", source)
        self.assertIn("saveDraft();", source)
        self.assertNotIn("fetch(", source)
        self.assertNotIn("confirmCurrent", source)

    def test_foundation_outcome_requires_all_module_one_to_four_answers_to_be_confirmed(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn('id="foundationOutcome" aria-live="polite" hidden', html)
        source = html[html.index("function renderFoundationOutcome"):html.index("function render()")]
        self.assertIn("item.answer?.confirmed||item.answer?.skipped", source)
        self.assertIn("items.filter(item=>!item.answer?.confirmed)", source)
        self.assertIn("state.profile[id]", source)
        self.assertIn("跳过项不会被 AI 当成事实", source)
        self.assertIn("你的 IP 底座 · 已确认", source)
        self.assertIn('$("skippedItems").querySelectorAll("[data-resume-module]")', html)
        listener = html[html.index('$("foundationOutcome").addEventListener'):html.index('$("coachFloat").addEventListener')]
        self.assertIn('event.target.closest("[data-resume-module]")', listener)
        for target in ("script_studio", "image_studio", "video_studio"):
            self.assertIn(f'data-foundation-target="{target}"', source)

    def test_foundation_handoff_is_explicit_and_does_not_generate_or_expose_context_in_url(self):
        html = PAGE.read_text(encoding="utf-8")
        source = html[html.index("function foundationHandoff"):html.index("function nextStepTitle")]
        self.assertIn("foundationItems().some(item=>!item.answer?.confirmed)", source)
        self.assertIn("sessionStorage.setItem(PRODUCT_HANDOFF_KEY", source)
        self.assertIn("created_at:Date.now()", source)
        self.assertIn("?prefill=ip12", source)
        navigation = source[source.index("location.href"):]
        self.assertIn('location.href=`${product.url}?prefill=ip12`', navigation)
        self.assertNotIn("context", navigation)
        self.assertNotIn("fetch(", source)
        self.assertNotIn("confirmCurrent", source)
        self.assertIn("不会自动生成、扣点或发布", html)

    def test_editing_a_confirmed_foundation_answer_relocks_the_outcome(self):
        html = PAGE.read_text(encoding="utf-8")
        source = html[html.index("function saveDraft"):html.index("async function confirmCurrent")]
        self.assertIn("if(changed)delete state.profile[module.id]", source)
        self.assertIn("if(changed)renderFoundationOutcome();", source)

    def test_project_recovery_consent_and_action_links_are_visible(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn("/api/gen/digital-ip/projects", html)
        self.assertIn("IP12 成长档案", html)
        self.assertIn("原始文件不会保存到项目档案", html)
        self.assertIn("PPT/Word 内嵌图表建议先导出为 PDF", html)
        self.assertIn("来源证据与定位", html)
        self.assertIn("current_module:module.name", html)
        self.assertIn("current_step:step.title", html)
        for extension, mime in {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "md": "text/markdown", "jpeg": "image/jpeg"}.items():
            self.assertIn(f'{extension}:"{mime}"', html)
        self.assertIn("type:mime", html)
        self.assertIn("data_url:`data:${mime};base64,${base64}`", html)
        self.assertIn("profile:state.profile", html)
        self.assertIn("state=restoreLocal?draft.state:remote&&typeof remote===\"object\"?{...initialState,...remote,analyses:{}", html)
        self.assertIn("async function flushProjectSave()", html)
        self.assertGreaterEqual(html.count("await flushProjectSave();"), 2)
        self.assertIn('`${STORAGE_KEY}:${project.id}`', html)
        self.assertIn("let state = structuredClone(initialState);", html)
        self.assertNotIn("localStorage.setItem(STORAGE_KEY,JSON.stringify(state))", html)
        self.assertIn("saveProject(true)", html)
        self.assertIn("项目已在另一端更新，请重新查看后再操作", html)
        self.assertIn("project.last_analysis?.input", html)
        self.assertIn('step.type==="review"?step.preview.join("\\n")', html)
        self.assertIn('textarea.addEventListener("input",()=>{', html)

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
