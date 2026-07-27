import pathlib
import unittest


SCRIPT_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/script.html"
CORE_PY = pathlib.Path(__file__).resolve().parents[1] / "server/content_domains/core.py"


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

    def test_reverse_video_estimate_matches_seedance_price(self):
        self.assertIn("selectedAvatarId===null?30:10", self.html)
        self.assertIn("seconds*rate", self.html)
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
        self.assertIn("selectedAvatarId===null?30:10", self.html)

    def test_reverse_video_picker_load_failure_keeps_no_avatar_available(self):
        self.assertIn("function _showReverseVideoPicker(prompt,onConfirm)", self.html)
        self.assertIn("fetch('/api/gen/video/avatars?limit=60'", self.html)
        self.assertIn("形象加载失败，不影响无形象生成", self.html)
        self.assertIn("data-reverse-retry", self.html)
        self.assertIn("retry.onclick=loadAvatars", self.html)
        self.assertIn("还没有形象", self.html)
        self.assertIn("video.html", self.html)

    def test_reverse_video_picker_cancel_and_submit_are_explicit(self):
        self.assertIn('id="reverseVideoPickClose"', self.html)
        self.assertIn('id="reverseVideoConfirm"', self.html)
        self.assertIn("confirm.disabled=true", self.html)
        self.assertIn("if(submitted) return;", self.html)
        self.assertIn("dismiss();\n      onConfirm({avatarId:selectedAvatarId,duration:selectedDuration})", self.html)
        self.assertIn("onConfirm({avatarId:selectedAvatarId,duration:selectedDuration})", self.html)

    def test_reverse_video_picker_ignores_stale_avatar_responses(self):
        self.assertIn("var reverseVideoPickerRequest=0", self.html)
        self.assertIn("var requestId=++reverseVideoPickerRequest", self.html)
        self.assertGreaterEqual(self.html.count("if(requestId!==reverseVideoPickerRequest) return;"), 2)

    def test_breakdown_mode_ui_and_api_exist(self):
        self.assertIn('data-mode="breakdown"', self.html)
        self.assertIn('id="panelBreakdown"', self.html)
        self.assertIn('id="bdToolScenes"', self.html)
        self.assertIn('id="bdToolReverse"', self.html)
        self.assertIn("data-bd-tool=\"reverse_prompt\"", self.html)
        self.assertIn('id="bdGen"', self.html)
        self.assertIn("fetch('/api/gen/breakdown'", self.html)
        self.assertIn("var reqBody=isBatch?{urls:lines,mode:'scenes'}:{url:lines[0],mode:submitMode};", self.html)

    def test_breakdown_progress_and_history_restore_exist(self):
        self.assertIn('id="bdProgress"', self.html)
        self.assertIn('data-phase="downloading"', self.html)
        self.assertIn('data-phase="extracting_frames"', self.html)
        self.assertIn('data-phase="transcribing"', self.html)
        self.assertIn('data-phase="analyzing"', self.html)
        self.assertIn("BREAKDOWN_HISTORY_KEY='hq_script_breakdown_history'", self.html)
        self.assertIn("switchMode('breakdown')", self.html)
        self.assertIn("renderBreakdown({source_url:m.source_url", self.html)
        self.assertIn("renderBreakdownReverse({type:'breakdown_reverse'", self.html)
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

    def test_reverse_video_without_avatar_uses_seedance_with_references(self):
        self.assertIn("_showReverseVideoPicker(prompt,function(choice)", self.html)
        self.assertIn("{channel:'micro',prompt:seedancePrompt,reference_images:reverseRefs,duration:choice.duration}", self.html)
        self.assertIn("{endpoint:'/api/gen/xiaole_video',sceneCount:1}", self.html)
        self.assertIn("endpoint==='/api/gen/xiaole_video'", self.html)

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
        self.assertIn("return prompt && prompt!==sourceUrl ? prompt : '';", self.html)
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
        self.assertIn("prompt:(bd.prompt||'')", self.html)
        self.assertIn("return !!((Array.isArray(meta.scenes)&&meta.scenes.length)||String(meta.prompt||'').trim());", self.html)
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
        self.assertIn("'Idempotency-Key':breakdownKey", self.html)
        self.assertIn("'Idempotency-Key':imageKey", self.html)
        self.assertIn('"script_to_video", "breakdown"}', self.core)

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

if __name__ == "__main__":
    unittest.main()
