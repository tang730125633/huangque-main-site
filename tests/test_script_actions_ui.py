import pathlib
import unittest


SCRIPT_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/script.html"


class ScriptActionsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = SCRIPT_HTML.read_text(encoding="utf-8")

    def test_scene_handoffs_keep_prompt_parameters(self):
        self.assertIn("handoffUrl('video.html',a.getAttribute('data-to-video')", self.html)
        self.assertIn("handoffUrl('audio.html',b.getAttribute('data-to-audio')", self.html)
        self.assertIn("'?prompt='+encodeURIComponent(prompt||'')", self.html)
        self.assertIn("escAttr(s.scene||'')", self.html)
        self.assertIn("escAttr(s.line||'')", self.html)

    def test_export_builds_utf8_text_download(self):
        self.assertIn('id="scExport"', self.html)
        self.assertIn("new Blob(['﻿'+scriptText(exportScenes)]", self.html)
        self.assertIn("a.download=filename", self.html)

    def test_one_click_video_calls_script_to_video_api(self):
        self.assertIn('id="scGenVideo"', self.html)
        self.assertIn('id="scGenAudio"', self.html)
        self.assertIn("fetch('/api/gen/script_to_video'", self.html)
        self.assertIn("_setGenerateBusy", self.html)
        self.assertIn("_doGenerate({scenes:list,style:'剧情'},genVideoBtn)", self.html)

    def test_one_click_video_passes_style_and_selected_avatar(self):
        self.assertIn("lastStyle=style||'口播'", self.html)
        self.assertIn("var talkingStyle=lastStyle==='剧情'?'口播':(lastStyle||'口播');", self.html)
        self.assertIn("_doGenerate({scenes:list,style:talkingStyle,avatar_id:avatarId},genAudioBtn)", self.html)

    def test_one_click_video_loads_avatar_picker_for_talking_styles(self):
        self.assertIn("fetch('/api/gen/video/avatars?limit=60'", self.html)
        self.assertIn('id="avatarPickModal"', self.html)
        self.assertIn('id="avatarPickGrid"', self.html)

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
        self.assertIn("_doGenerate({scenes:scenes,style:'剧情'},bdRemakeBtn)", self.html)

    def test_breakdown_reverse_prompt_ui_and_actions_exist(self):
        self.assertIn('id="bdReverseCopyBtn"', self.html)
        self.assertIn('id="bdReverseDrawBtn"', self.html)
        self.assertIn("function renderBreakdownReverse(bd)", self.html)
        self.assertIn("} else if(result.type==='breakdown_reverse'){", self.html)
        self.assertIn("switchBreakdownTool('reverse_prompt')", self.html)
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

if __name__ == "__main__":
    unittest.main()
