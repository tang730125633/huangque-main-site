import json
import pathlib
import shutil
import subprocess
import unittest


SCRIPT_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/script.html"
CORE_PY = pathlib.Path(__file__).resolve().parents[1] / "server/content_domains/core.py"


def _extract_js_function(source, name):
    marker = f"function {name}("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


class ScriptActionsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = SCRIPT_HTML.read_text(encoding="utf-8")
        cls.core = CORE_PY.read_text(encoding="utf-8")

    def test_scene_handoffs_keep_prompt_parameters(self):
        self.assertIn("handoffUrl('video.html',a.getAttribute('data-to-video')", self.html)
        self.assertIn("handoffUrl('audio.html',b.getAttribute('data-to-audio')", self.html)
        self.assertIn("'?prompt='+encodeURIComponent(prompt||'')", self.html)
        self.assertIn("escAttr(s.scene||'')", self.html)
        self.assertIn("escAttr(s.line||'')", self.html)

    def test_export_builds_utf8_text_download(self):
        self.assertIn('id="scExport"', self.html)
        self.assertIn("new Blob(['﻿'+txt]", self.html)
        self.assertIn("a.download=filename", self.html)

    def test_one_click_video_calls_script_to_video_api(self):
        self.assertIn('id="scGenVideo"', self.html)
        self.assertIn('id="scGenAudio"', self.html)
        self.assertIn("options.endpoint||'/api/gen/script_to_video'", self.html)
        self.assertIn("function _confirmDramaVideo(list)", self.html)
        self.assertIn("预计消耗 '+cost+' 点", self.html)
        self.assertIn("if(!_confirmDramaVideo(list)) return;", self.html)

    def test_reverse_video_estimate_uses_server_quote(self):
        self.assertIn("noAvatarOffer.duration_costs[String(selectedDuration)]", self.html)
        self.assertIn("noAvatarOffer.duration_costs[String(seconds)]", self.html)
        self.assertIn("selectedDuration*10", self.html)
        self.assertIn("5 秒 · 150 点", self.html)
        self.assertIn("10 秒 · 300 点", self.html)
        self.assertIn("15 秒 · 450 点", self.html)
        self.assertIn("_setGenerateBusy", self.html)
        self.assertIn("_doGenerate({scenes:list,style:'剧情',duration:_dramaDuration(list)},genVideoBtn)", self.html)

    def test_one_click_video_passes_style_and_selected_avatar(self):
        self.assertIn("lastStyle=style||'口播'", self.html)
        self.assertIn("var talkingStyle=lastStyle==='剧情'?'口播':(lastStyle||'口播');", self.html)
        self.assertIn("_doGenerate({scenes:list,style:talkingStyle,avatar_id:avatarId,voice:voice},genAudioBtn)", self.html)

    def test_one_click_video_loads_avatar_picker_for_talking_styles(self):
        self.assertIn("fetch('/api/gen/video/avatars?limit=60'", self.html)
        self.assertIn('id="avatarPickModal"', self.html)
        self.assertIn('id="avatarPickGrid"', self.html)

    def test_reverse_video_picker_has_optional_avatar_duration_and_cost(self):
        self.assertIn('id="reverseVideoPickModal"', self.html)
        self.assertIn('id="reverseVideoNoAvatar"', self.html)
        self.assertIn('id="reverseVideoAvatarGrid"', self.html)
        self.assertIn('data-reverse-duration="5"', self.html)
        self.assertIn('data-reverse-duration="10"', self.html)
        self.assertIn('data-reverse-duration="15"', self.html)
        self.assertIn('id="reverseVideoCost"', self.html)
        self.assertIn("selectedDuration=10", self.html)
        self.assertIn("selectedAvatarId=null", self.html)
        self.assertIn("var noAvatarOffer=null", self.html)

    def test_reverse_video_picker_avatar_failure_does_not_bypass_channel_gate(self):
        self.assertIn("function _showReverseVideoPicker(prompt,onConfirm)", self.html)
        self.assertIn("fetch('/api/gen/video/avatars?limit=60'", self.html)
        self.assertIn("形象加载失败，不影响无形象生成", self.html)
        self.assertIn("data-reverse-retry", self.html)
        self.assertIn("retry.onclick=loadAvatars", self.html)
        self.assertIn("还没有形象", self.html)
        self.assertIn("video.html", self.html)
        self.assertIn("fetch('/api/gen/health',{cache:'no-store'})", self.html)
        self.assertIn("reverse_remake_video_offer", self.html)
        self.assertIn("offer.duration_costs", self.html)
        self.assertIn("if(submitted||confirm.disabled) return;", self.html)

    def test_reverse_video_picker_cancel_and_submit_are_explicit(self):
        self.assertIn('id="reverseVideoPickClose"', self.html)
        self.assertIn('id="reverseVideoConfirm"', self.html)
        self.assertIn("confirm.disabled=true", self.html)
        self.assertIn("if(submitted||confirm.disabled) return;", self.html)
        self.assertIn("model:selectedAvatarId===null&&noAvatarOffer?noAvatarOffer.model:''", self.html)
        self.assertIn("resolution:selectedAvatarId===null&&noAvatarOffer?noAvatarOffer.resolution:''", self.html)

    def test_reverse_video_picker_ignores_stale_avatar_responses(self):
        self.assertIn("var reverseVideoPickerRequest=0", self.html)
        self.assertIn("var invocationId=++reverseVideoPickerRequest", self.html)
        self.assertIn("var avatarRequestId=++avatarLoadRequest", self.html)
        self.assertGreaterEqual(self.html.count("if(invocationId!==reverseVideoPickerRequest"), 3)

    def test_breakdown_mode_ui_and_api_exist(self):
        self.assertIn('data-mode="breakdown"', self.html)
        self.assertIn('id="panelBreakdown"', self.html)
        self.assertIn('id="bdToolScenes"', self.html)
        self.assertIn('id="bdToolReverse"', self.html)
        self.assertIn("data-bd-tool=\"reverse_prompt\"", self.html)
        self.assertIn('id="bdGen"', self.html)
        self.assertIn("fetch('/api/gen/breakdown'", self.html)
        self.assertIn("var reqBody=isBatch?{urls:lines,mode:'scenes'}:{url:lines[0],mode:submitMode};", self.html)
        self.assertIn("function normalizeBreakdownUrl(text)", self.html)
        self.assertIn("链接格式不正确", self.html)
        self.assertIn("链接视频最大 200MB", self.html)

    def test_breakdown_progress_and_history_restore_exist(self):
        self.assertIn('id="bdProgress"', self.html)
        self.assertIn('data-phase="downloading"', self.html)
        self.assertIn('data-phase="extracting_frames"', self.html)
        self.assertIn('data-phase="transcribing"', self.html)
        self.assertIn('data-phase="analyzing"', self.html)
        self.assertIn("BREAKDOWN_HISTORY_KEY='hq_script_breakdown_history'", self.html)
        self.assertIn("switchMode('breakdown')", self.html)
        self.assertIn("renderBreakdown({source_url:m.source_url", self.html)
        self.assertIn("loadBreakdownHistoryDetail(item).then(function(detail)", self.html)
        self.assertIn("renderBreakdownReverse(Object.assign({},detail", self.html)
        self.assertIn("Object.assign({},detail,{source_title:detail.source_title||heading})", self.html)
        self.assertIn("analysis:m.analysis||''", self.html)

    def test_breakdown_analysis_is_rendered_and_saved_to_history(self):
        self.assertIn('id="bdAnalysis"', self.html)
        self.assertIn('id="bdAnalysisText"', self.html)
        self.assertIn("function setBreakdownAnalysis(text)", self.html)
        self.assertIn("setBreakdownAnalysis(analysis)", self.html)
        self.assertIn("analysis:(bd.analysis||'')", self.html)

    def test_breakdown_remake_reuses_current_one_click_flow(self):
        self.assertIn('id="bdRemakeBtn"', self.html)
        self.assertIn("prepareBreakdownRemakePayload(bd, style)", self.html)
        self.assertIn("return {scenes:normalizeBreakdownScenes((bd&&bd.scenes)||[]),style:style||'剧情'};", self.html)
        self.assertIn("_pickRemakeStyle(function(style)", self.html)
        self.assertIn("_showAvatarPicker(function(avatarId)", self.html)
        self.assertIn("_showReverseVideoPicker(prompt,function(choice)", self.html)
        self.assertIn("_doGenerate({scenes:scenes,style:'剧情',duration:_dramaDuration(scenes)},bdRemakeBtn)", self.html)

    def test_reverse_video_without_avatar_uses_available_channel_with_ordered_references(self):
        self.assertIn("_showReverseVideoPicker(prompt,function(choice)", self.html)
        self.assertIn("var reverseRefs=reverseReferenceImages(lastBreakdownReverse)", self.html)
        self.assertIn("function reverseReferenceThumbnailIndices(bd)", self.html)
        self.assertIn("function reverseReferenceImages(bd)", self.html)
        self.assertIn("channel:choice.channel", self.html)
        self.assertIn("model:choice.model", self.html)
        self.assertIn("resolution:choice.resolution", self.html)
        self.assertIn("reference_mode:choice.channel==='grok'?'ordered_storyboard':undefined", self.html)
        self.assertIn("{endpoint:'/api/gen/xiaole_video',sceneCount:1}", self.html)
        self.assertIn("endpoint==='/api/gen/xiaole_video'", self.html)

    def test_reverse_video_picker_fails_closed_without_open_channel(self):
        self.assertIn('id="reverseVideoSeedanceStatus"', self.html)
        self.assertIn("var noAvatarChannel=''", self.html)
        self.assertIn("noAvatar.disabled=!noAvatarChannel", self.html)
        self.assertIn("var blocked=selectedAvatarId===null&&!noAvatarChannel", self.html)
        self.assertIn("开放式视频通道暂未开启", self.html)
        self.assertIn("开放式生成通道可用（果肉视频）", self.html)

    def test_reverse_video_with_avatar_uses_existing_cinematic_api(self):
        self.assertIn("endpoint:'/api/gen/cinematic'", self.html)
        self.assertIn("cine_mode:'open'", self.html)
        self.assertIn("avatar_ids:[choice.avatarId]", self.html)
        self.assertIn("prompt:avatarPrompt", self.html)
        self.assertIn("reference_images:reverseRefs", self.html)
        self.assertIn("duration:choice.duration", self.html)
        self.assertIn("ratio:'9:16'", self.html)
        self.assertIn("resolution:'720p'", self.html)
        self.assertIn("enhance_prompt:false", self.html)

    def test_shared_video_submitter_supports_both_endpoints_safely(self):
        self.assertIn("function _doGenerate(payload,btn,options)", self.html)
        self.assertIn("options=options||{}", self.html)
        self.assertIn("options.endpoint||'/api/gen/script_to_video'", self.html)
        self.assertIn("(payload.scenes||[]).length", self.html)
        self.assertIn("confirm.disabled=true", self.html)

    def test_breakdown_reverse_prompt_ui_and_actions_exist(self):
        self.assertIn('id="bdReverseCopyBtn"', self.html)
        self.assertIn('id="bdReverseDrawBtn"', self.html)
        self.assertIn("function renderBreakdownReverse(bd)", self.html)
        self.assertIn("} else if(result.type==='breakdown_reverse'){", self.html)
        self.assertIn("switchBreakdownTool('reverse_prompt')", self.html)
        self.assertIn("document.getElementById('bdReversePromptText')", self.html)
        self.assertIn("function validReversePromptText(value, sourceUrl)", self.html)
        self.assertIn("return card ? validReversePromptText(card.textContent,sourceUrl) : '';", self.html)
        self.assertIn("location.href=handoffUrl('banana.html',prompt)", self.html)
        self.assertIn("if(currentMode==='breakdown' && isBreakdownReverseTool()) txt=reversePromptText();", self.html)
        self.assertIn("提示词反推暂仅支持单条视频链接", self.html)

    def test_history_loads_copy_assets_and_restores_scenes(self):
        self.assertIn("'/api/gen/assets?limit=60&kind=copy'", self.html)
        self.assertIn("historyList.appendChild(historyCard(item))", self.html)
        self.assertIn("render({scenes:list},heading", self.html)
        self.assertIn("readBreakdownHistory()", self.html)
        self.assertIn("flattenBreakdownAsset(item)", self.html)
        self.assertIn("saveBreakdownHistory(item);", self.html)

    def test_reverse_history_is_saved_and_restored(self):
        self.assertIn("prompt:isReverse?reverseResultPrompt(bd):(bd.prompt||'')", self.html)
        self.assertIn("var prompt=meta.type==='breakdown_reverse'?reverseResultPrompt(meta):String(meta.prompt||'').trim();", self.html)
        self.assertIn("timeline_audit:isReverse?(bd.timeline_audit||null):null", self.html)
        self.assertIn("quality_score:isReverse?(bd.quality_score||null):null", self.html)
        self.assertIn("renderBreakdownReverse(result); saveBreakdownHistory(result); loadHistory();", self.html)
        self.assertIn("isReverse?'反推':'拆解'", self.html)

    def test_theme_toggle_ui_and_sync_exist(self):
        self.assertIn('id="scThemeLight"', self.html)
        self.assertIn('id="scThemeDark"', self.html)
        self.assertIn('data-theme-option="light"', self.html)
        self.assertIn('data-theme-option="dark"', self.html)
        self.assertIn("function renderThemeToggle(theme)", self.html)
        self.assertIn("window.HQTheme.set(theme); renderThemeToggle(theme);", self.html)
        self.assertIn("document.addEventListener('hq-theme-change'", self.html)

    def test_history_controls_are_accessible_buttons(self):
        self.assertIn('id="scHistoryBtn" class="sc-btn" type="button"', self.html)
        self.assertIn("btn.type='button'; btn.className='sc-history-item'", self.html)

    def test_breakdown_poll_handles_network_errors(self):
        self.assertIn("pollErrors=0", self.html)
        self.assertIn("MAX_POLL_ERRORS=10", self.html)
        self.assertIn("pollErrors++;", self.html)
        self.assertIn("网络不稳定，正在重试", self.html)
        self.assertIn("网络连接失败，请检查网络后重试", self.html)
        # 三处轮询（写脚本、拆解、成片）都已覆盖
        self.assertTrue(self.html.count("pollErrors=0") >= 6)
        self.assertTrue(self.html.count("网络不稳定，正在重试") >= 3)

    def test_breakdown_and_image_submissions_are_idempotent(self):
        self.assertIn("'Idempotency-Key':breakdownPending.key", self.html)
        self.assertIn("'Idempotency-Key':imagePending.key", self.html)
        self.assertIn("requestHeaders['Idempotency-Key']=videoPending.key", self.html)
        self.assertIn('"script_to_video", "breakdown"}', self.core)
        self.assertIn("sessionStorage.setItem(storageKey", self.html)
        self.assertIn("saved&&saved.body===body&&saved.key", self.html)
        self.assertIn("code==='idempotency_in_progress'", self.html)
        self.assertEqual(
            3,
            self.html.count(
                "if(x.s<500||(x.d&&x.d.operation_terminal===true)) "
                "_confirmSubmission"
            ),
        )

    def test_lost_submission_response_reuses_pending_key(self):
        self.assertIn("var _pendingSubmissionMemory={}", self.html)
        self.assertIn("var storageKey='hq_pending_submit_'+scope", self.html)
        self.assertIn("saved&&saved.body===body&&saved.key", self.html)
        self.assertIn("return {storageKey:storageKey,body:body,key:saved.key}", self.html)
        self.assertIn("fetch('/api/gen/breakdown'", self.html)
        self.assertIn("body:breakdownPending.body", self.html)
        self.assertIn("body:imagePending.body", self.html)
        self.assertIn("videoPending?videoPending.body:JSON.stringify(payload)", self.html)
        # Network catches intentionally do not confirm/clear the pending key.
        self.assertNotIn(
            ".catch(function(){ _confirmSubmission(breakdownPending)",
            self.html,
        )
        self.assertNotIn(
            ".catch(function(){ _confirmSubmission(imagePending)",
            self.html,
        )

    def test_terminal_500_discards_pending_key_but_uncertain_failures_keep_it(self):
        confirmation = (
            "if(x.s<500||(x.d&&x.d.operation_terminal===true)) "
            "_confirmSubmission"
        )
        self.assertEqual(3, self.html.count(confirmation))
        self.assertIn("code==='idempotency_in_progress'", self.html)
        self.assertNotIn("x.s>=500) _confirmSubmission", self.html)

    def test_video_job_survives_refresh_and_blocks_duplicate_submit(self):
        self.assertIn("ACTIVE_VIDEO_JOB_KEY='hq_script_active_video_job'", self.html)
        self.assertIn("localStorage.setItem(key,JSON.stringify(value))", self.html)
        self.assertIn("function _resumeActiveVideoJob()", self.html)
        self.assertIn("if(_readActiveVideoJob())", self.html)
        self.assertIn("已恢复任务 ", self.html)
        self.assertIn("_clearActiveVideoJob(x.d.job_id,activeJobOwner)", self.html)
        self.assertIn("_resumeActiveVideoJob();", self.html)

    def test_video_job_recovery_is_isolated_by_current_account(self):
        self.assertIn("function _activeVideoOwner()", self.html)
        self.assertIn("user.username||user.account_id||user.id", self.html)
        self.assertIn(
            "ACTIVE_VIDEO_JOB_KEY+':'+encodeURIComponent(owner)",
            self.html,
        )
        self.assertIn("value=Object.assign({},value,{owner:owner})", self.html)
        self.assertIn("String(value.owner||'')!==owner", self.html)
        self.assertIn("localStorage.removeItem(ACTIVE_VIDEO_JOB_KEY)", self.html)
        self.assertIn("_clearActiveVideoJob(active.jobId,active.owner)", self.html)

    def test_video_job_recovery_clears_404_and_other_terminal_4xx(self):
        self.assertIn("function _videoLookupDisposition(status)", self.html)
        self.assertIn("[400,403,404,410].indexOf(status)>=0", self.html)
        self.assertIn("if(status===401) return 'login'", self.html)
        self.assertIn("if(status>=400) return 'retry'", self.html)
        self.assertGreaterEqual(
            self.html.count("if(disposition==='clear')"),
            2,
        )
        self.assertIn(
            "_clearActiveVideoJob(active.jobId,active.owner); finishButtons();",
            self.html,
        )
        self.assertIn(
            "_clearActiveVideoJob(x.d.job_id,activeJobOwner)",
            self.html,
        )
        self.assertIn("该任务已失效或不属于当前账号，请重新提交", self.html)
        self.assertIn(
            "if(activeVideoResumeTimer) clearInterval(activeVideoResumeTimer)",
            self.html,
        )

    def test_initial_poll_401_preserves_job_and_requests_login(self):
        direct = self.html[self.html.index("function _doGenerate("):]
        login_start = direct.index("if(disposition==='login')")
        login_end = direct.index("if(disposition==='clear')", login_start)
        login_branch = direct[login_start:login_end]
        self.assertIn("clearInterval(iv)", login_branch)
        self.assertIn("HQ.login()", login_branch)
        self.assertNotIn("_clearActiveVideoJob", login_branch)
        self.assertNotIn("localStorage.removeItem", login_branch)

    @unittest.skipUnless(shutil.which("node"), "node is required for browser logic test")
    def test_video_job_account_switch_and_404_behavior(self):
        start = self.html.index("var ACTIVE_VIDEO_JOB_KEY=")
        end = self.html.index("function _doGenerate(", start)
        recovery = self.html[start:end]
        script = """
const storage = {};
var localStorage = {
  getItem: key => Object.prototype.hasOwnProperty.call(storage,key) ? storage[key] : null,
  setItem: (key,value) => { storage[key]=String(value); },
  removeItem: key => { delete storage[key]; }
};
var genVideoBtn={}, bdRemakeBtn=null, scenes={innerHTML:''};
var window={HQ:null}, HQ=null;
var cleared=[];
function clearInterval(id){ cleared.push(id); }
function setInterval(){ return 77; }
function _setGenerateBusy(btn,busy){ if(btn) btn.busy=busy; }
function _readApiResponse(response){ return Promise.resolve(response); }
function tok(){ return '__cookie__'; }
function esc(value){ return String(value); }
function loadHistory(){}
var fetch=()=>Promise.resolve({s:404,d:{detail:'not found'}});
eval(%s);
function user(name){ localStorage.setItem('hq_user',JSON.stringify({username:name})); }
user('account-a');
_saveActiveVideoJob({jobId:101,startedAt:1});
user('account-b');
const bBefore=_readActiveVideoJob();
_saveActiveVideoJob({jobId:202,startedAt:2});
user('account-a');
const aJob=_readActiveVideoJob();
user('account-b');
_resumeActiveVideoJob();
setImmediate(function(){
  const bKey=_activeVideoJobKey('account-b');
  const aKey=_activeVideoJobKey('account-a');
  process.stdout.write(JSON.stringify({
    bBefore:bBefore,
    aJob:aJob&&aJob.jobId,
    aStillStored:!!localStorage.getItem(aKey),
    bCleared:!localStorage.getItem(bKey),
    timerStopped:activeVideoResumeTimer===null&&cleared.indexOf(77)>=0,
    buttonRestored:genVideoBtn.busy===false
  }));
});
""" % json.dumps(recovery)
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        got = json.loads(result.stdout)
        self.assertIsNone(got["bBefore"])
        self.assertEqual(101, got["aJob"])
        self.assertTrue(got["aStillStored"])
        self.assertTrue(got["bCleared"])
        self.assertTrue(got["timerStopped"])
        self.assertTrue(got["buttonRestored"])

    @unittest.skipUnless(shutil.which("node"), "node is required for browser logic test")
    def test_video_job_429_retries_without_clearing_recovery_state(self):
        start = self.html.index("var ACTIVE_VIDEO_JOB_KEY=")
        end = self.html.index("function _doGenerate(", start)
        recovery = self.html[start:end]
        script = """
const storage = {};
var localStorage = {
  getItem: key => Object.prototype.hasOwnProperty.call(storage,key) ? storage[key] : null,
  setItem: (key,value) => { storage[key]=String(value); },
  removeItem: key => { delete storage[key]; }
};
var genVideoBtn={}, bdRemakeBtn=null, scenes={innerHTML:''};
var window={HQ:null}, HQ=null;
function clearInterval(){}
function setInterval(){ return 88; }
function _setGenerateBusy(btn,busy){ if(btn) btn.busy=busy; }
function _readApiResponse(response){ return Promise.resolve(response); }
function tok(){ return '__cookie__'; }
function esc(value){ return String(value); }
function loadHistory(){}
var fetch=()=>Promise.resolve({s:429,d:{detail:'busy'}});
eval(%s);
localStorage.setItem('hq_user',JSON.stringify({username:'account-b'}));
_saveActiveVideoJob({jobId:202,startedAt:2});
_resumeActiveVideoJob();
setImmediate(function(){
  const key=_activeVideoJobKey('account-b');
  process.stdout.write(JSON.stringify({
    disposition401:_videoLookupDisposition(401),
    disposition404:_videoLookupDisposition(404),
    disposition429:_videoLookupDisposition(429),
    stillStored:!!localStorage.getItem(key),
    timerActive:activeVideoResumeTimer===88
  }));
});
""" % json.dumps(recovery)
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        got = json.loads(result.stdout)
        self.assertEqual("login", got["disposition401"])
        self.assertEqual("clear", got["disposition404"])
        self.assertEqual("retry", got["disposition429"])
        self.assertTrue(got["stillStored"])
        self.assertTrue(got["timerActive"])

    def test_breakdown_handles_gateway_html_and_can_resume_polling(self):
        self.assertIn("function _readApiResponse(response)", self.html)
        self.assertIn("服务返回异常（HTTP ", self.html)
        self.assertIn("data-resume-breakdown", self.html)
        self.assertIn("继续查询", self.html)
        self.assertIn("任务编号：", self.html)
        self.assertIn("startPolling()", self.html)

    def test_breakdown_batch_phase_regex_matches_digits(self):
        self.assertIn(r"/^batch_(\d+)_(\d+)$/.exec", self.html)

    def test_breakdown_scenes_are_editable(self):
        self.assertIn('id="bdEditBtn"', self.html)
        self.assertIn("function _toggleBreakdownEdit()", self.html)
        self.assertIn("function _editableCardHTML(s,i)", self.html)
        self.assertIn("function _saveBreakdownEdit()", self.html)
        self.assertIn("function _renderBreakdownEditMode()", self.html)
        self.assertIn('data-scene-dur', self.html)
        self.assertIn('data-scene-text', self.html)
        self.assertIn('data-scene-line', self.html)
        self.assertIn("bdEditing=false", self.html)

    def test_breakdown_storyboard_ui_elements_exist(self):
        self.assertIn('id="bdStoryboard"', self.html)
        self.assertIn('id="bdStoryboardStrip"', self.html)
        self.assertIn("function setBreakdownStoryboard(frames)", self.html)
        self.assertIn("frame_thumbnails", self.html)
        self.assertIn("setBreakdownStoryboard((bd&&bd.frame_thumbnails)||[])", self.html)

    def test_esc_tolerates_non_string_values(self):
        """esc 必须容忍数字等非字符串（后端 duration 是毫秒整数）"""
        self.assertIn("String(s==null?'':s)", self.html)

    def test_breakdown_to_image_button_generates_in_page(self):
        self.assertIn('id="bdToImageBtn"', self.html)
        self.assertIn("function _doGenerateImage(prompt, btn)", self.html)
        self.assertIn("fetch('/api/gen/image'", self.html)

    def test_write_gen_401_resets_button(self):
        """写脚本 401 必须复位生成按钮，否则按钮卡死在生成中"""
        self.assertIn("if(x.s===401){ setBtn(orig,false); if(window.HQ) HQ.login(); return; }", self.html)

    def test_remake_validates_scenes_by_style(self):
        """生成同款视频按风格前置校验：剧情要画面、口播/种草要文案"""
        self.assertIn("无法生成剧情视频", self.html)
        self.assertIn("无法生成'+style+'视频", self.html)

    def test_history_dedup_skips_items_without_source_url(self):
        """普通脚本历史（无 source_url）不参与去重，同标题多版本都要保留"""
        self.assertIn("if(!meta.source_url) return true;", self.html)

    def test_unknown_phase_keeps_progress_bar(self):
        """未知 phase（如 batch_N_M）不得打空进度条，且显示批量进度"""
        self.assertIn("if(order.indexOf(phase)<0) return;", self.html)
        self.assertIn("批量拆解中（第'", self.html)

    def test_media_lightbox_for_generated_results(self):
        """生成的视频/图片、故事板缩略图必须可点击放大预览"""
        self.assertIn("function _openMediaLightbox(kind, src)", self.html)
        self.assertIn("hqMediaLightbox", self.html)
        self.assertIn("t.closest('#scScenes')||t.closest('#bdStoryboardStrip')", self.html)

    def test_scene_card_hides_empty_voiceover(self):
        """口播为空（纯音乐/歌舞视频）时不渲染口播行和转口播按钮"""
        self.assertIn("((s.line||'').trim()?'<div style=\"font-size:13px; color:#eaf1fa;", self.html)
        self.assertIn("((s.line||'').trim()?'<a data-to-audio=", self.html)

    def test_breakdown_labels_show_20_points_per_link(self):
        """分镜拆解、提示词反推及批量说明必须与后端每链接 20 点一致"""
        self.assertIn("开始拆解（20 点）", self.html)
        self.assertIn("开始反推（20 点）", self.html)
        self.assertIn("每个链接 20 点", self.html)
        for stale in ("开始拆解（8 点）", "开始反推（8 点）", "首条 8 点每多一条+4 点"):
            self.assertNotIn(stale, self.html)

    def test_default_selling_point_tags_removed(self):
        """核心卖点下的默认标签（清爽控油/补水保湿/抗老紧致/提亮肤色）已移除"""
        for tag in ("清爽控油", "补水保湿", "抗老紧致", "提亮肤色"):
            self.assertNotIn(tag, self.html)

    # === 8 UX fixes ===

    def test_placeholder_cards_have_data_attribute_and_are_skipped(self):
        """P0: 占位分镜带 data-placeholder，readScenesFromDom 跳过"""
        self.assertIn('data-placeholder="1"', self.html)
        self.assertIn("!card.hasAttribute('data-placeholder')", self.html)

    def test_scene_handoff_links_open_new_tab(self):
        """P1: 转视频/转口播以新标签页打开"""
        self.assertIn('target="_blank" rel="noopener">转视频<', self.html)
        self.assertIn("window.open(handoffUrl('video.html'", self.html)
        self.assertIn("window.open(handoffUrl('audio.html'", self.html)

    def test_edit_mode_has_cancel_button(self):
        """P2: 编辑模式有取消按钮"""
        self.assertIn('id="bdEditCancelBtn"', self.html)
        self.assertIn("function _cancelBreakdownEdit()", self.html)
        self.assertIn("bdEditCancelBtn.style.display=bdEditing?'':'none'", self.html)

    def test_duration_hint_shows_estimated_scenes(self):
        """P2: 时长选择下方显示预计分镜数"""
        self.assertIn('id="scDurHint"', self.html)
        self.assertIn("预计产出", self.html)

    def test_batch_url_textarea_rows_two(self):
        """P3: bdUrl textarea rows=2"""
        self.assertIn('id="bdUrl" rows="2"', self.html)

    def test_reverse_draw_button_label(self):
        """P3: 去作图按钮改为「去作图页精修」"""
        self.assertIn("去作图页精修", self.html)

    def test_export_includes_content_analysis(self):
        """P3: 导出补内容分析"""
        self.assertIn("lastBreakdown.analysis", self.html)

    def test_style_picker_has_description(self):
        """P3: 风格选择器有说明文字"""
        self.assertIn("口播/种草=数字人念稿", self.html)

    def test_video_wait_timer_is_realtime(self):
        """成片等待时间必须按真实时间实时刷新，不能在第三档冻住"""
        self.assertIn("startTs=Date.now()", self.html)
        self.assertIn("Math.floor((Date.now()-startTs)/1000)", self.html)
        self.assertIn("bucket!==lastProgress||bucket===2", self.html)

    def test_batch_poll_timeout_scales_with_url_count(self):
        """批量拆解轮询上限按条数放宽，避免后端还在跑前端先报超时"""
        self.assertIn("var maxPolls=isBatch?(100*lines.length):120;", self.html)
        self.assertIn("if(pollCount>maxPolls)", self.html)

    def test_generate_success_keeps_scenes_visible(self):
        """一键成片/做图成功后分镜列表保留，成功横幅插入顶部"""
        self.assertIn("scenes.insertAdjacentHTML('afterbegin',successHtml)", self.html)

    def test_legacy_millisecond_duration_display_fixed(self):
        """存量毫秒时长显示修正（fmtDur 18320→18）"""
        self.assertIn("function fmtDur(d)", self.html)
        self.assertIn("n>1000", self.html)

    def test_talking_flow_offers_personal_voice_picker(self):
        """一键生成口播/同款口播必须提供个人音色选择"""
        self.assertIn("function _pickVoice(onPick)", self.html)
        self.assertIn("/api/gen/audio/voices", self.html)
        self.assertIn("voice:voice", self.html)
        self.assertIn("scope==='personal'", self.html)

    def test_drama_duration_sums_scenes_and_clamps_to_15(self):
        """剧情时长求和所有分镜 dur 并 clamp 到 [1,15]"""
        self.assertIn("function _dramaDuration(list)", self.html)
        self.assertIn("Math.min(15, Math.max(1", self.html)

    def test_scene_cards_support_copy_drag_and_direct_edit(self):
        self.assertIn('data-scene-copy="', self.html)
        self.assertIn('draggable="true" data-scene-idx="', self.html)
        self.assertIn("function startDirectSceneEdit(index)", self.html)
        self.assertIn("点击卡片可直接编辑", self.html)
        self.assertIn("HQ.toast('分镜顺序已更新')", self.html)

    def test_live_scene_stats_render_and_refresh(self):
        self.assertIn('id="scLiveStats"', self.html)
        self.assertIn('id="scWordCount"', self.html)
        self.assertIn('id="scEstimateDuration"', self.html)
        self.assertIn("function sceneStats(list)", self.html)
        self.assertIn("function renderSceneStats(list)", self.html)
        self.assertIn("renderSceneStats(readEditingScenes())", self.html)
        self.assertIn("修改口播会实时刷新字数 / 时长", self.html)


class ReverseVideoPickerRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node is required for reverse picker contracts")
        html = SCRIPT_HTML.read_text(encoding="utf-8")
        cls.picker = _extract_js_function(html, "_showReverseVideoPicker")

    def _run_picker(self, channel="", avatars=None, pick_avatar=False, seedance_enabled=False,
                    costs=None, model=None, resolution="720p"):
        avatars = avatars or []
        if costs is None:
            costs = ({"5": 150, "10": 300, "15": 450} if channel == "micro"
                     else {"5": 60, "10": 120, "15": 180} if channel == "grok" else {})
        if model is None:
            model = ("seedance-1-5-pro-251215" if channel == "micro"
                     else "grok-imagine-video" if channel == "grok" else "")
        offer = {"channel": channel, "model": model, "resolution": resolution,
                 "duration_costs": costs}
        harness = f"""
class ClassList {{
  constructor(){{this.values={{}};}}
  toggle(name,on){{this.values[name]=Boolean(on);}}
}}
class Element {{
  constructor(id){{this.id=id;this.style={{}};this.disabled=false;this.textContent='';this.children=[];this.attributes={{}};this.classList=new ClassList();this.onclick=null;this._innerHTML='';}}
  set innerHTML(value){{this._innerHTML=String(value);this.children=[];}}
  get innerHTML(){{return this._innerHTML;}}
  setAttribute(name,value){{this.attributes[name]=String(value);}}
  getAttribute(name){{return this.attributes[name]||null;}}
  appendChild(child){{this.children.push(child);return child;}}
  querySelectorAll(selector){{
    var match=selector.match(/^\\[([^\\]]+)\\]$/);
    if(!match) return [];
    return this.children.filter(function(child){{return Object.prototype.hasOwnProperty.call(child.attributes,match[1]);}});
  }}
}}
var ids={{}};
['reverseVideoPickModal','reverseVideoNoAvatar','reverseVideoSeedanceStatus','reverseVideoAvatarGrid','reverseVideoDuration','reverseVideoCost','reverseVideoConfirm','reverseVideoPickClose'].forEach(function(id){{ids[id]=new Element(id);}});
[5,10,15].forEach(function(seconds){{var button=new Element('duration-'+seconds);button.setAttribute('data-reverse-duration',seconds);ids.reverseVideoDuration.appendChild(button);}});
var document={{
  getElementById:function(id){{return ids[id]||null;}},
  createElement:function(tag){{return new Element(tag);}}
}};
var reverseVideoPickerRequest=0;
function tok(){{return 'token';}}
function esc(value){{return String(value);}}
function response(data){{return {{ok:true,json:function(){{return Promise.resolve(data);}}}};}}
function fetch(url){{
  if(url.indexOf('/api/gen/video/avatars')===0) return Promise.resolve(response({{items:{json.dumps(avatars, ensure_ascii=False)}}}));
  if(url==='/api/gen/health') return Promise.resolve(response({{reverse_remake_video_offer:{json.dumps(offer)},seedance_video_enabled:{str(seedance_enabled).lower()}}}));
  return Promise.reject(new Error('unexpected '+url));
}}
{self.picker}
var choice=null;
_showReverseVideoPicker('prompt',function(value){{choice=value;}});
setImmediate(function(){{setImmediate(function(){{
  var cardCount=ids.reverseVideoAvatarGrid.children.length;
  if({str(pick_avatar).lower()}){{
    var cards=ids.reverseVideoAvatarGrid.children;
    if(cards.length) cards[0].onclick();
  }}
  ids.reverseVideoConfirm.onclick();
  process.stdout.write(JSON.stringify({{
    choice:choice,noAvatarDisabled:ids.reverseVideoNoAvatar.disabled,
    confirmDisabled:ids.reverseVideoConfirm.disabled,
    state:ids.reverseVideoSeedanceStatus.getAttribute('data-state'),
    statusText:ids.reverseVideoSeedanceStatus.textContent,cardCount:cardCount,
    costText:ids.reverseVideoCost.textContent,
    durationLabels:ids.reverseVideoDuration.children.map(function(item){{return item.textContent;}})
  }}));
}});}});
"""
        result = subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True,
            text=True, encoding="utf-8",
        )
        return json.loads(result.stdout)

    def test_no_open_channel_cannot_submit_paid_no_avatar_job(self):
        got = self._run_picker()
        self.assertIsNone(got["choice"])
        self.assertTrue(got["noAvatarDisabled"])
        self.assertTrue(got["confirmDisabled"])
        self.assertEqual("blocked", got["state"])

    def test_explicit_empty_channel_is_not_overridden_by_legacy_seedance_flag(self):
        got = self._run_picker(seedance_enabled=True)
        self.assertIsNone(got["choice"])
        self.assertTrue(got["noAvatarDisabled"])
        self.assertTrue(got["confirmDisabled"])

    def test_grok_fallback_submits_explicit_channel_once(self):
        got = self._run_picker(channel="grok")
        self.assertEqual(
            {"avatarId": None, "duration": 10, "channel": "grok",
             "model": "grok-imagine-video", "resolution": "720p"},
            got["choice"],
        )
        self.assertFalse(got["noAvatarDisabled"])
        self.assertEqual("ready", got["state"])
        self.assertEqual("预计消耗 120 点", got["costText"])
        self.assertEqual(["5 秒 · 60 点", "10 秒 · 120 点", "15 秒 · 180 点"],
                         got["durationLabels"])

    def test_seedance_offer_uses_server_quoted_prices(self):
        got = self._run_picker(channel="micro")
        self.assertEqual("预计消耗 300 点", got["costText"])
        self.assertEqual(["5 秒 · 150 点", "10 秒 · 300 点", "15 秒 · 450 点"],
                         got["durationLabels"])

    def test_missing_or_invalid_server_quote_fails_closed(self):
        for costs in ({}, {"5": 60, "10": 120}, {"5": 60, "10": 0, "15": 180}):
            with self.subTest(costs=costs):
                got = self._run_picker(channel="grok", costs=costs)
                self.assertIsNone(got["choice"])
                self.assertTrue(got["noAvatarDisabled"])
                self.assertTrue(got["confirmDisabled"])
                self.assertIn("报价不可用", got["statusText"])

    def test_avatar_path_remains_available_when_open_channels_are_closed(self):
        got = self._run_picker(
            avatars=[{"id": "avatar-7", "name": "avatar"}],
            pick_avatar=True,
        )
        self.assertEqual(1, got["cardCount"], got)
        self.assertEqual(
            {"avatarId": "avatar-7", "duration": 10, "channel": "",
             "model": "", "resolution": ""},
            got["choice"],
            got,
        )


if __name__ == "__main__":
    unittest.main()
