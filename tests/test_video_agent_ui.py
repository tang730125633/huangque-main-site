# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "site" / "workbench" / "video.html").read_text(encoding="utf-8")
CLOUD_SHELL = (ROOT / "site" / "workbench" / "cloud-shell.js").read_text(encoding="utf-8")


class VideoAgentUiTests(unittest.TestCase):
    def test_video_home_is_the_only_default_entry(self):
        self.assertNotIn('id="studioModeSwitch"', HTML)
        self.assertNotIn('data-studio-mode=', HTML)
        self.assertIn('id="agentPanel"', HTML)
        self.assertIn('id="expertStudio" class="hidden"', HTML)
        self.assertIn('id="videoWorkspace" class="gVid agent-layout"', HTML)
        self.assertIn('id="videoPrimary"', HTML)
        self.assertNotIn('id="historySidebar"', HTML)
        self.assertIn("function showVideoHome()", HTML)
        self.assertIn("function openVideoWorkbench(route)", HTML)

    def test_home_has_material_canvas_agent_dock_and_six_toolbox_modules(self):
        for element_id in (
            "materialCanvas", "materialCanvasStage", "materialCanvasEmpty",
            "canvasSelectionStatus", "canvasUploadInput", "canvasAddText",
            "agentDock", "videoToolbox", "videoToolboxToggle",
        ):
            self.assertIn('id="%s"' % element_id, HTML)
        self.assertIn('class="agent-thread agent-thread-large"', HTML)
        self.assertIn('aria-label="视频创作助手"', HTML)
        modules = ("talking", "motion", "story", "create", "tryon", "compose")
        toolbox_markup = HTML.split('id="videoToolbox"', 1)[1].split('</section>', 1)[0]
        for module in modules:
            self.assertIn('data-video-module="%s"' % module, toolbox_markup)
        for title in ("数字人口播", "动作模仿", "剧情故事", "自由生成", "换装换背景", "一键成片"):
            self.assertIn(title, toolbox_markup)
        agent_markup = HTML.split('id="agentPanel"', 1)[1].split('id="expertStudio"', 1)[0]
        for provider_name in ("Grok", "Sora", "MiniMax", "Omni", "Seedance"):
            self.assertNotIn(provider_name, agent_markup)
        self.assertIn(".video-home-shell{position:relative;display:block;height:100%;min-height:0}", HTML)
        self.assertIn(".material-canvas{position:absolute;z-index:1;inset:0", HTML)
        self.assertIn(".agent-dock{position:absolute;z-index:20;top:16px;right:16px;bottom:16px;width:410px", HTML)
        self.assertIn("box-shadow:0 22px 52px rgba(0,0,0,.34)", HTML)
        self.assertIn(".agent-panel.agent-dock-collapsed .agent-dock{width:58px;bottom:auto;height:58px}", HTML)

    def test_home_uses_one_surface_color_instead_of_stacked_panels(self):
        self.assertIn("--video-surface:#0b1018", HTML)
        for selector in (
            '.hq-content[data-active="video"],#videoWorkspace,#videoPrimary.canvas-home',
            ".video-home-shell", ".material-canvas", ".agent-dock",
            ".agent-dock-body", ".canvas-card", ".canvas-toolbar,.canvas-toolbox",
        ):
            self.assertIn(selector, HTML)
        self.assertIn(".agent-message.assistant{background:rgba(148,164,187,.045);border-color:transparent", HTML)
        self.assertIn(".agent-message.user{background:rgba(231,178,76,.07);border-color:transparent", HTML)
        self.assertIn(".material-canvas-title{border:0;background:transparent", HTML)

    def test_home_removes_the_redundant_module_heading_row(self):
        self.assertIn(".vid-main.canvas-home>.row{display:none}", HTML)
        self.assertIn(".agent-panel{height:100%;padding-top:0", HTML)
        self.assertIn('id="agentDockHistory"', HTML)
        self.assertIn("$('agentDockHistory').onclick=openVideoHistory", HTML)

    def test_material_canvas_supports_cards_selection_drag_and_safe_restore(self):
        for function_name in (
            "renderMaterialCanvas", "addCanvasTextCard", "selectCanvasMaterial",
            "removeCanvasMaterial", "beginCanvasDrag", "clampCanvasPosition",
            "safeCanvasLayoutForSession", "restoreCanvasLayout",
        ):
            self.assertIn("function %s" % function_name, HTML)
        self.assertIn("canvasSelectedIds", HTML)
        self.assertIn("canvasLayout", HTML)
        self.assertIn("canvas_layout:safeCanvasLayoutForSession(canvasLayout)", HTML)
        self.assertIn("restoreCanvasLayout(state.canvas_layout)", HTML)
        safe_layout = HTML.split("function safeCanvasLayoutForSession", 1)[1].split("function ", 1)[0]
        for unsafe_field in ("preview", "data_url", "blob", "file"):
            self.assertNotIn(unsafe_field, safe_layout.lower())

    def test_material_canvas_has_select_pan_zoom_and_safe_viewport_restore(self):
        for element_id in (
            "canvasSelectMode", "canvasPanMode", "canvasZoomOut",
            "canvasZoomValue", "canvasZoomIn", "canvasFitView",
        ):
            self.assertIn('id="%s"' % element_id, HTML)
        for function_name in (
            "setCanvasToolMode", "applyCanvasViewport", "setCanvasZoom",
            "beginCanvasPan", "fitCanvasView", "safeCanvasViewportForSession",
            "restoreCanvasViewport", "canvasViewportCenterPosition",
        ):
            self.assertIn("function %s" % function_name, HTML)
        self.assertIn("canvasToolMode='select'", HTML)
        self.assertIn("canvas_viewport:safeCanvasViewportForSession(canvasViewport)", HTML)
        self.assertIn("restoreCanvasViewport(state.canvas_viewport)", HTML)
        self.assertIn("if(e.code==='Space'", HTML)
        self.assertIn("if((e.ctrlKey||e.metaKey)", HTML)
        safe_viewport = HTML.split("function safeCanvasViewportForSession", 1)[1].split("function ", 1)[0]
        self.assertIn("scale", safe_viewport)
        for unsafe_field in ("preview", "data_url", "blob", "file"):
            self.assertNotIn(unsafe_field, safe_viewport.lower())

    def test_material_image_cards_are_large_resizable_and_previewable(self):
        self.assertIn(".canvas-card.media-card{width:320px;height:380px", HTML)
        self.assertIn(".canvas-card-preview img{object-fit:contain}", HTML)
        self.assertIn("data-canvas-resize=", HTML)
        self.assertIn("data-canvas-preview=", HTML)
        for function_name in (
            "safeCanvasCardSize", "canvasDefaultCardSize", "beginCanvasResize",
            "openCanvasImagePreview", "closeCanvasImagePreview",
        ):
            self.assertIn("function %s" % function_name, HTML)
        self.assertIn("/canvasResizeState.scale", HTML)
        self.assertIn("e.key==='Escape'", HTML)
        safe_layout = HTML.split("function safeCanvasLayoutForSession", 1)[1].split("function ", 1)[0]
        self.assertIn("width", safe_layout)
        self.assertIn("height", safe_layout)
        for unsafe_field in ("preview", "data_url", "blob", "file", "src"):
            self.assertNotIn(unsafe_field, safe_layout.lower())
        arrange = HTML.split("function autoArrangeCanvas", 1)[1].split("function ", 1)[0]
        self.assertIn("canvasDefaultCardSize", arrange)

    def test_restored_material_previews_use_authenticated_runtime_blob_urls(self):
        self.assertIn("function restoreAgentMaterialPreviews", HTML)
        preview_logic = HTML.split("function restoreAgentRemoteMaterialPreview", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("/preview", preview_logic)
        self.assertIn("Authorization:'Bearer '+token", preview_logic)
        self.assertIn("response.blob()", preview_logic)
        self.assertIn("URL.createObjectURL(file)", preview_logic)
        self.assertIn("persistAgentMediaFile(canvasId,file)", preview_logic)
        self.assertIn("renderMaterialCanvas()", preview_logic)
        restore_logic = HTML.split("function restoreAgentSession", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("restoreAgentMaterialPreviews()", restore_logic)
        safe_material = HTML.split("function safeAgentMaterialForSession", 1)[1].split(
            "function ", 1
        )[0]
        for unsafe_value in ("preview_url", "blob:", "data_url", "local_path"):
            self.assertNotIn(unsafe_value, safe_material)

    def test_material_files_use_account_scoped_indexeddb_and_restore_before_remote_preview(self):
        self.assertIn("AGENT_MEDIA_DB_NAME='hq_video_agent_media_v1'", HTML)
        self.assertIn("function openAgentMediaDb", HTML)
        self.assertIn("db.createObjectStore(AGENT_MEDIA_STORE,{keyPath:'key'})", HTML)
        self.assertIn("store.createIndex('owner','owner'", HTML)
        key_logic = HTML.split("function agentMediaStorageKey", 1)[1].split("function ", 1)[0]
        self.assertIn("owner+'\\u0000'+canvasId", key_logic)
        persist = HTML.split("function persistAgentMediaFile", 1)[1].split("function ", 1)[0]
        self.assertIn("blob:file", persist)
        self.assertIn("owner:owner", persist)
        cached_restore = HTML.split("function restoreAgentCachedMaterial", 1)[1].split(
            "function restoreAgentRemoteMaterialPreview", 1
        )[0]
        self.assertIn("loadAgentMediaFile(owner,canvasId)", cached_restore)
        self.assertIn("agentFiles[index]=file", cached_restore)
        self.assertIn("URL.createObjectURL(file)", cached_restore)
        self.assertIn("uploadAgentMaterial(file,index)", cached_restore)
        restore = HTML.split("function restoreAgentMaterialPreviews", 1)[1].split(
            "function restoreAgentSession", 1
        )[0]
        self.assertIn("restoreAgentCachedMaterial(meta,index,epoch)", restore)
        self.assertIn("restoreAgentRemoteMaterialPreview(meta,epoch)", restore)

    def test_material_cache_is_written_replaced_and_deleted_with_workspace_items(self):
        add_files = HTML.split("function addAgentFiles", 1)[1].split(
            "function recommendAgentRoute", 1
        )[0]
        self.assertIn("persistAgentMediaFile(id,item)", add_files)
        self.assertIn("current.media_cached=true", add_files)
        remove = HTML.split("function removeCanvasMaterial", 1)[1].split(
            "function chooseCanvasMaterialReplacement", 1
        )[0]
        self.assertIn("deleteAgentMediaFile(currentAgentSessionOwner(),id)", remove)
        reset = HTML.split("function resetAgentSession", 1)[1].split(
            "function renderAgentAttachments", 1
        )[0]
        self.assertIn("deleteAgentLocalDataForOwner(owner)", reset)

    def test_agent_receives_only_safe_selected_material_metadata(self):
        request_logic = HTML.split("function requestVideoAgent", 1)[1].split("function ", 1)[0]
        self.assertIn("selected:canvasSelectedIds.indexOf(item.canvas_id)>=0", request_logic)
        self.assertNotIn("preview", request_logic)
        self.assertNotIn("data_url", request_logic)

    def test_agent_accepts_text_and_mixed_media(self):
        self.assertIn('id="agentPrompt"', HTML)
        self.assertIn('id="agentFileInput"', HTML)
        self.assertIn('accept="image/*,video/*,audio/*"', HTML)
        self.assertIn('id="agentSendBtn"', HTML)
        self.assertIn("function analyzeAgentRequest()", HTML)
        self.assertIn("renderAgentAttachments", HTML)
        self.assertIn("/api/gen/video/agent/chat", HTML)
        self.assertIn("function requestVideoAgent", HTML)
        self.assertIn("agentConversation", HTML)
        self.assertIn("agentBrief", HTML)

    def test_chat_materials_share_the_canvas_with_safe_source_labels(self):
        for function_name in (
            "safeCanvasMaterialSource", "canvasMaterialSourceLabel",
            "addCanvasTextMaterial", "classifyChatTextMaterial",
            "renderChatMaterialOffer", "renderAgentDraftMaterialAction",
        ):
            self.assertIn("function %s" % function_name, HTML)
        safe_layout = HTML.split("function safeCanvasLayoutForSession", 1)[1].split(
            "function ", 1
        )[0]
        safe_material = HTML.split("function safeAgentMaterialForSession", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("source:", safe_layout)
        self.assertIn("safeCanvasMaterialSource", safe_layout)
        self.assertIn("source:", safe_material)
        self.assertIn("safeCanvasMaterialSource", safe_material)
        for unsafe_field in ("preview", "data_url", "blob", "local_path"):
            self.assertNotIn(unsafe_field, safe_layout.lower())
            self.assertNotIn(unsafe_field, safe_material.lower())

    def test_explicit_chat_copy_is_added_and_ambiguous_copy_requires_a_click(self):
        classify = HTML.split("function classifyChatTextMaterial", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("extractExplicitChatTextMaterial", classify)
        self.assertIn("mode:'auto'", classify)
        self.assertIn("mode:'offer'", classify)
        self.assertIn("mode:'none'", classify)
        analyze = HTML.split("function analyzeAgentRequest", 1)[1].split(
            "function collectAgentPreflight", 1
        )[0]
        self.assertIn("renderChatMaterialOffer(userMessage,shownText)", analyze)
        offer = HTML.split("function renderChatMaterialOffer", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("addCanvasTextMaterial", offer)
        self.assertIn("放入工作区", offer)
        self.assertIn("已加入素材工作区", offer)

    def test_chat_text_materials_are_deduplicated_and_restore_their_actions(self):
        add_text = HTML.split("function addCanvasTextMaterial", 1)[1].split(
            "function ", 1
        )[0]
        dedupe = HTML.split("function canvasTextMaterialByContent", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("canvasTextMaterialByContent", add_text)
        self.assertIn("normalizeCanvasTextMaterial", dedupe)
        self.assertIn("canvasLayout.find", dedupe)
        restore = HTML.split("function restoreAgentSession", 1)[1].split(
            "function clearAgentMemoryForOwner", 1
        )[0]
        self.assertIn("renderChatMaterialOffer", restore)

    def test_agent_draft_can_be_adopted_without_overwriting_user_copy(self):
        draft_action = HTML.split(
            "function renderAgentDraftMaterialAction", 1
        )[1].split("function ", 1)[0]
        self.assertIn("采用为文案素材", draft_action)
        self.assertIn("addCanvasTextMaterial", draft_action)
        render_result = HTML.split("function renderAgentResult", 1)[1].split(
            "function analyzeAgentRequest", 1
        )[0]
        self.assertIn("renderAgentDraftMaterialAction(assistant,agentLastResult.draft)", render_result)

    def test_agent_draft_canvas_text_decodes_escaped_line_breaks(self):
        normalize = HTML.split(
            "function normalizeCanvasTextContent", 1
        )[1].split("function ", 1)[0]
        self.assertIn("safeCanvasMaterialSource(source)==='agent'", normalize)
        self.assertIn(r"/\\r\\n|\\n|\\r/g", normalize)
        add_text = HTML.split("function addCanvasTextMaterial", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("normalizeCanvasTextContent(content,source)", add_text)
        restore = HTML.split("function safeCanvasLayoutForSession", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("normalizeCanvasTextContent(item.text,item.source)", restore)
        render = HTML.split("function renderMaterialCanvas", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("normalizeCanvasTextContent(item.text,item.source)", render)

    def test_chat_and_canvas_uploads_record_their_distinct_origins(self):
        self.assertIn("function handleCanvasUpload(source)", HTML)
        self.assertIn("handleCanvasUpload.call(this,'chat')", HTML)
        self.assertIn("handleCanvasUpload.call(this,'canvas')", HTML)
        self.assertIn("addAgentFiles(e.dataTransfer.files,'canvas')", HTML)

    def test_composer_attachments_are_a_one_turn_queue_not_the_workspace_store(self):
        self.assertIn("agentPendingAttachmentIds=[]", HTML)
        render = HTML.split("function renderAgentAttachments", 1)[1].split(
            "function agentUploadDigest", 1
        )[0]
        self.assertIn("agentPendingAttachmentIds", render)
        self.assertIn("canvas_id===id", render)
        self.assertNotIn("agentFiles.map", render)
        request = HTML.split("function requestVideoAgent", 1)[1].split(
            "function appendAgentMessage", 1
        )[0]
        self.assertIn("agentMaterialMeta.map", request)

    def test_home_chat_clears_only_pending_attachments_after_send(self):
        self.assertIn("function clearAgentPendingAttachments", HTML)
        analyze = HTML.split("function analyzeAgentRequest", 1)[1].split(
            "function collectAgentPreflight", 1
        )[0]
        self.assertIn("pendingCount=agentPendingAttachmentIds.length", analyze)
        self.assertIn("clearAgentPendingAttachments()", analyze)
        self.assertIn("pendingCount?(' · 已添加 '+pendingCount+' 个素材')", analyze)
        self.assertNotIn("已添加 '+agentFiles.length", analyze)
        clear = HTML.split("function clearAgentPendingAttachments", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("agentPendingAttachmentIds=[]", clear)
        self.assertNotIn("agentFiles=[]", clear)
        self.assertNotIn("agentMaterialMeta=[]", clear)

    def test_only_chat_uploads_enter_the_pending_attachment_queue(self):
        add_files = HTML.split("function addAgentFiles", 1)[1].split(
            "function recommendAgentRoute", 1
        )[0]
        self.assertIn("source==='chat'", add_files)
        self.assertIn("agentPendingAttachmentIds.push(id)", add_files)
        self.assertIn("agentPendingAttachmentIds.indexOf(id)<0", add_files)
        restore = HTML.split("function restoreAgentSession", 1)[1].split(
            "function clearAgentMemoryForOwner", 1
        )[0]
        self.assertIn("agentPendingAttachmentIds=[]", restore)

    def test_agent_assistant_messages_render_safe_basic_markdown(self):
        self.assertIn("function agentSafeMarkdown", HTML)
        markdown = HTML.split("function agentSafeMarkdown", 1)[1].split("function ", 1)[0]
        self.assertIn("esc(source)", markdown)
        self.assertIn("<strong>", markdown)
        self.assertIn("replace(/\\*\\*/g,'')", markdown)
        self.assertIn("agent-message-list-item", markdown)
        self.assertIn("agent-message-heading", markdown)
        self.assertIn("agent-message-numbered", markdown)
        self.assertIn("agent-message-gap", markdown)
        normalizer = HTML.split("function agentNormalizeReplyLayout", 1)[1].split("function ", 1)[0]
        self.assertIn("replace(/\\\\n/g,'\\n')", normalizer)
        self.assertIn("方案结论", normalizer)
        self.assertIn("agentWrapDenseReplyLine", normalizer)
        append = HTML.split("function appendAgentMessage", 1)[1].split("function ", 1)[0]
        self.assertIn("role==='assistant'?agentSafeMarkdown(content):''", append)
        self.assertIn("node.textContent=content", append)
        self.assertIn("node.innerHTML=formatted", append)
        render = HTML.split("function renderAgentResult", 1)[1].split("function ", 1)[0]
        self.assertIn("content:reply", render)
        self.assertNotIn("content:assistant.textContent", render)

    def test_agent_chat_reopens_at_latest_without_stealing_manual_history_reading(self):
        for function_name in (
            "agentThreadNearLatest", "scrollAgentToLatest", "initializeAgentChatScroll",
        ):
            self.assertIn("function %s" % function_name, HTML)
        scroll_logic = HTML.split("function scrollAgentToLatest", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("agentChatAutoFollow", scroll_logic)
        self.assertIn("options.force===true", scroll_logic)
        self.assertIn("requestAnimationFrame", scroll_logic)
        self.assertIn("thread.scrollHeight", scroll_logic)
        init_logic = HTML.split("function initializeAgentChatScroll", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("thread.addEventListener('scroll'", init_logic)
        self.assertIn("MutationObserver", init_logic)
        append_logic = HTML.split("function appendAgentMessage", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("scrollAgentToLatest({force:role==='user'", append_logic)
        self.assertNotIn("thread.scrollTop=thread.scrollHeight", append_logic)
        restore_logic = HTML.split("function restoreAgentSession", 1)[1].split(
            "function clearAgentMemoryForOwner", 1
        )[0]
        self.assertIn("scrollAgentToLatest({force:true", restore_logic)
        home_logic = HTML.split("function showVideoHome", 1)[1].split(
            "function openVideoWorkbench", 1
        )[0]
        self.assertIn("scrollAgentToLatest({force:true", home_logic)
        self.assertIn("window.addEventListener('pageshow'", HTML)
        self.assertIn("document.addEventListener('visibilitychange'", HTML)

    def test_agent_recommendation_hands_off_to_existing_workflows(self):
        self.assertIn("var AGENT_TASKS=", HTML)
        self.assertIn("function recommendAgentRoute", HTML)
        self.assertIn("function openAgentRecommendation", HTML)
        self.assertIn("openVideoWorkbench(route)", HTML)
        for function_name in ("talking", "grok", "minimax", "cinematic", "tryon"):
            self.assertIn("function:'%s'" % function_name, HTML)
        self.assertIn("ready_to_handoff", HTML)
        self.assertIn("material_requests", HTML)

    def test_agent_sends_metadata_only_and_has_a_degraded_fallback(self):
        self.assertIn("name:file.name", HTML)
        self.assertIn("size:file.size", HTML)
        self.assertNotIn("data:file.data", HTML)
        self.assertIn("function fallbackAgentResult", HTML)
        self.assertIn("智能分析暂时不可用", HTML)

    def test_agent_chat_errors_preserve_the_public_error_contract(self):
        request_logic = HTML.split("function makeVideoAgentRequestError", 1)[1].split(
            "function appendAgentMessage", 1
        )[0]
        for field in (
            "error.status=status", "error.code=code",
            "error.hq_code=hqCode", "error.request_id=requestId",
            "error.retryable=payload.retryable===true",
        ):
            self.assertIn(field, request_logic)
        self.assertIn("X-HQ-Error-Code", request_logic)
        self.assertIn("X-HQ-Request-ID", request_logic)
        self.assertIn("X-Request-ID", request_logic)
        self.assertNotIn("error.payload", request_logic)
        self.assertNotIn("payload.detail", request_logic)

    def test_agent_chat_treats_a_2xx_non_json_response_as_failure(self):
        request_logic = HTML.split("function requestVideoAgent", 1)[1].split(
            "function appendAgentMessage", 1
        )[0]
        self.assertIn("response.text()", request_logic)
        self.assertIn("JSON.parse(raw)", request_logic)
        self.assertIn("response.ok?'advisor_response_invalid'", request_logic)
        self.assertIn("throw makeVideoAgentRequestError", request_logic)
        self.assertNotIn("response.json().catch(function(){return {};})", request_logic)

    def test_agent_chat_failures_are_safe_categorized_and_degraded(self):
        message_logic = HTML.split("function agentRequestFailureMessage", 1)[1].split(
            "function fallbackAgentResult", 1
        )[0]
        for condition in (
            "status===401", "status===403", "status===400||status===413",
            "status===429", "status===502", "status===503", "status===504",
            "code==='advisor_response_invalid'",
        ):
            self.assertIn(condition, message_logic)
        self.assertIn("请求编号：", message_logic)
        self.assertNotIn("error.message", message_logic)

        home_logic = HTML.split("function analyzeAgentRequest", 1)[1].split(
            "function collectAgentPreflight", 1
        )[0]
        workbench_logic = HTML.split("function analyzeWorkbenchAgent", 1)[1].split(
            "function handoffAgentFiles", 1
        )[0]
        self.assertIn("catch(function(error)", home_logic)
        self.assertIn("fallbackAgentResult(shownText,error)", home_logic)
        self.assertIn("catch(function(error)", workbench_logic)
        self.assertIn("fallbackAgentResult(text,error)", workbench_logic)
        self.assertIn("degraded_message", HTML)
        self.assertIn("已切换为基础匹配，本次没有执行付费命令", HTML)

        fallback_logic = HTML.split("function fallbackAgentResult", 1)[1].split(
            "function requestVideoAgent", 1
        )[0]
        session_logic = HTML.split("function safeAgentResultForSession", 1)[1].split(
            "function pendingActionExpiresAt", 1
        )[0]
        self.assertNotIn("reply:'智能分析暂时不可用。'+failure", fallback_logic)
        self.assertNotIn("degraded_message", session_logic)
        for sensitive_field in ("request_id", "hq_code", "payload"):
            self.assertNotIn(sensitive_field, session_logic)

    def test_degraded_fallback_still_routes_explicit_story_intent(self):
        route_logic = HTML.split("function recommendAgentRoute", 1)[1].split("function fallbackAgentResult", 1)[0]
        for keyword in ("剧情", "电影感", "电影质感"):
            self.assertIn(keyword, route_logic)
        self.assertIn("return AGENT_TASKS.story", route_logic)

    def test_degraded_fallback_never_marks_the_plan_ready_for_handoff(self):
        fallback_logic = HTML.split("function fallbackAgentResult", 1)[1].split(
            "function requestVideoAgent", 1
        )[0]
        self.assertIn("stage:'clarify'", fallback_logic)
        self.assertIn("ready_to_handoff:false", fallback_logic)
        self.assertNotIn("stage:'plan_ready'", fallback_logic)
        self.assertNotIn("ready_to_handoff:true", fallback_logic)

    def test_v3_keeps_session_controls_without_redundant_detail_accordions(self):
        for element_id in ("agentNewSession", "agentResumeNotice"):
            self.assertIn('id="%s"' % element_id, HTML)
        for element_id in (
            "agentBriefPanel", "agentPlanList", "agentMaterialChecks", "agentDraftPanel",
        ):
            self.assertNotIn('id="%s"' % element_id, HTML)
        for label in ("当前视频简报", "创作步骤", "素材检查"):
            self.assertNotIn("<summary>%s</summary>" % label, HTML)

    def test_agent_dock_uses_one_continuous_chat_surface(self):
        self.assertNotIn('class="agent-dock-intro"', HTML)
        self.assertNotIn('class="agent-footnote"', HTML)
        self.assertIn(".agent-dock .agent-thread-large{flex:1;min-height:120px;max-height:none;margin:0;padding:12px 5px;border:0;border-radius:0}", HTML)
        self.assertIn(".agent-dock-head{height:54px;flex:none;display:flex;align-items:center;gap:9px;padding:0 13px;border-bottom:0}", HTML)
        for function_name in (
            "renderAgentWorkspace", "inspectAgentFile", "saveAgentSession",
            "restoreAgentSession", "resetAgentSession",
        ):
            self.assertIn("function %s" % function_name, HTML)
        self.assertIn("AGENT_SESSION_KEY_PREFIX='hq_video_agent_session_v3:'", HTML)
        self.assertIn(
            "AGENT_LEGACY_SESSION_KEYS=['hq_video_agent_session_v3','hq_video_agent_session_v2']",
            HTML,
        )

    def test_agent_plan_status_lives_on_canvas_instead_of_chat(self):
        agent_markup = HTML.split('id="agentDock"', 1)[1].split('</aside>', 1)[0]
        self.assertIn('id="agentPlanStatus"', agent_markup)
        self.assertNotIn('id="agentRecommendation"', agent_markup)
        self.assertIn("AGENT_PLAN_CANVAS_ID='cvm_agent_plan'", HTML)
        for function_name in (
            "ensureCanvasPlanCard", "renderCanvasPlanCard", "renderAgentPlanStatus",
            "focusAgentPlanCard", "agentMissingFieldLabel",
        ):
            self.assertIn("function %s" % function_name, HTML)
        render_canvas = HTML.split("function renderMaterialCanvas", 1)[1].split(
            "function selectCanvasMaterial", 1
        )[0]
        self.assertIn("renderCanvasPlanCard", render_canvas)
        self.assertIn("canvas-plan-card", HTML)
        render_result = HTML.split("function renderAgentResult", 1)[1].split(
            "function analyzeAgentRequest", 1
        )[0]
        self.assertIn("ensureCanvasPlanCard", render_result)
        self.assertIn("renderAgentPlanStatus", render_result)
        self.assertNotIn("rec.innerHTML", render_result)
        safe_layout = HTML.split("function safeCanvasLayoutForSession", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("item.kind==='plan'", safe_layout)
        for unsafe_field in ("reply", "reason", "missing_fields", "preview", "blob"):
            self.assertNotIn(unsafe_field, safe_layout.lower())

    def test_v3_continues_inside_workbench_with_safe_form_updates(self):
        for element_id in (
            "workbenchAgent", "workbenchAgentThread", "workbenchAgentPrompt",
            "workbenchAgentSend", "agentPreflight", "agentApplyUpdates",
        ):
            self.assertIn('id="%s"' % element_id, HTML)
        self.assertIn("function renderWorkbenchAgent", HTML)
        self.assertIn("function applyAgentFormUpdates", HTML)
        self.assertIn("function collectAgentPreflight", HTML)
        safe_apply = HTML.split("function applyAgentFormUpdates", 1)[1].split("function ", 1)[0]
        self.assertNotIn("GenerateBtn').click", safe_apply)
        self.assertNotIn("submitVideo", safe_apply)

    def test_agent_parameter_updates_use_fixed_dataset_allowlists(self):
        parameter_logic = HTML.split("var AGENT_PARAMETER_CONTROLS=", 1)[1].split(
            "function applyAgentFormUpdates", 1
        )[0]
        for field in ("ratio", "duration", "subtitles"):
            self.assertIn(field, parameter_logic)
        for dataset in (
            "ratio", "subtitle", "cineRatio", "cineDuration", "tryonSeconds",
            "grokRatio", "grokDuration", "soraRatio", "soraSeconds",
            "minimaxRatio", "minimaxDuration", "omniRatio", "omniDuration",
            "seedanceRatio", "seedanceDuration",
        ):
            self.assertIn("dataset:'%s'" % dataset, parameter_logic)
        self.assertIn("panel.querySelectorAll('.seg button')", parameter_logic)
        self.assertIn("['ratio','duration','subtitles'].indexOf(field)<0", parameter_logic)
        self.assertIn("Object.prototype.hasOwnProperty.call", parameter_logic)
        self.assertIn("spec.values.indexOf(wanted)<0", parameter_logic)
        self.assertIn("button.dataset[spec.dataset]!==wanted", parameter_logic)
        self.assertIn("/GenerateBtn$/", parameter_logic)
        self.assertNotIn("document.querySelector(", parameter_logic)
        self.assertNotIn("var selector=", parameter_logic)

    def test_agent_parameter_updates_reject_css_injection_and_unknown_values(self):
        finder_logic = HTML.split("function findAgentParameterButton", 1)[1].split(
            "function applyAgentParameterUpdate", 1
        )[0]
        apply_logic = HTML.split("function applyAgentParameterUpdate", 1)[1].split(
            "function applyAgentFormUpdates", 1
        )[0]
        form_logic = HTML.split("function applyAgentFormUpdates", 1)[1].split(
            "function analyzeWorkbenchAgent", 1
        )[0]
        self.assertIn("if(!spec||!wanted||spec.values.indexOf(wanted)<0)return null", finder_logic)
        self.assertIn("button.dataset[spec.dataset]!==wanted", finder_logic)
        self.assertIn("if(!button)return false", apply_logic)
        self.assertIn("&&applyAgentParameterUpdate(update)", form_logic)
        for unsafe_fragment in (
            "[data-grok-ratio=\"'+update.value",
            "[data-cine-duration=\"'+parseInt(update.value",
            "document.querySelector(selector)",
        ):
            self.assertNotIn(unsafe_fragment, form_logic)
        self.assertNotIn("GenerateBtn", form_logic)

    def test_paid_agent_action_is_visible_but_only_runs_after_explicit_click(self):
        for element_id in (
            "agentToolActivity", "agentPendingActions",
            "workbenchAgentToolActivity", "workbenchPendingActions",
        ):
            self.assertIn('id="%s"' % element_id, HTML)
        self.assertGreaterEqual(HTML.count('aria-live="polite"'), 6)
        for function_name in (
            "normalizeAgentPendingAction", "renderAgentPendingActions",
            "confirmAgentPendingAction", "setAgentToolActivity",
        ):
            self.assertIn("function %s" % function_name, HTML)
        self.assertIn("确认生成并支付", HTML)
        self.assertIn("data-agent-plan-open", HTML)
        self.assertIn("openAgentRecommendation", HTML)
        self.assertIn("/api/gen/video/agent/actions/", HTML)
        self.assertIn("JSON.stringify({idempotency_key:idempotencyKey})", HTML)
        confirm_logic = HTML.split("function confirmAgentPendingAction", 1)[1].split("function ", 1)[0]
        self.assertNotIn("quote_token", confirm_logic)
        self.assertNotIn("command", confirm_logic)
        self.assertNotIn("GenerateBtn", confirm_logic)
        self.assertNotIn(".click()", confirm_logic)

    def test_paid_confirmation_errors_keep_only_safe_state_and_fixed_messages(self):
        error_logic = HTML.split("function makeAgentConfirmRequestError", 1)[1].split(
            "function agentConfirmFailureMessage", 1
        )[0]
        for field in (
            "error.status=status", "error.code=code", "error.hq_code=hqCode",
            "error.request_id=requestId", "error.retryable=data.retryable===true",
            "error.result_unknown=data.result_unknown===true",
        ):
            self.assertIn(field, error_logic)
        self.assertIn("normalizeAgentPendingAction(data.pending_action)", error_logic)
        self.assertIn("new Error('agent_confirmation_failed')", error_logic)
        self.assertNotIn("error.payload", error_logic)
        self.assertNotIn("data.detail", error_logic)

        message_logic = HTML.split("function agentConfirmFailureMessage", 1)[1].split(
            "function confirmAgentPendingAction", 1
        )[0]
        for condition in (
            "current.status==='result_unknown'", "current.status==='expired'",
            "status===401", "status===402", "status===403", "status===404",
            "status===409", "status===429", "status===502||status===503",
            "status===504",
        ):
            self.assertIn(condition, message_logic)
        self.assertIn("请求编号：", message_logic)
        self.assertNotIn("error.message", message_logic)

        confirm_logic = HTML.split("function confirmAgentPendingAction", 1)[1].split(
            "function saveAgentSession", 1
        )[0]
        self.assertIn("makeAgentConfirmRequestError(response,data)", confirm_logic)
        self.assertIn("updated=error&&error.pending_action", confirm_logic)
        self.assertIn("error&&error.result_unknown===true", confirm_logic)
        self.assertIn("agentConfirmFailureMessage(error,current)", confirm_logic)
        for unsafe_fragment in ("error.payload", "data.detail", "error.message"):
            self.assertNotIn(unsafe_fragment, confirm_logic)

    def test_agent_chat_accepts_one_or_many_safe_pending_actions(self):
        render_logic = HTML.split("function renderAgentResult", 1)[1].split("function analyzeAgentRequest", 1)[0]
        self.assertIn("result.pending_action", render_logic)
        self.assertIn("result.pending_actions", render_logic)
        self.assertIn("pendingValues.forEach(upsertAgentPendingAction)", render_logic)
        self.assertNotIn("replaceAgentPendingActions(pendingValues)", render_logic)

    def test_talking_confirmation_stays_on_page_and_starts_safe_job_tracking(self):
        self.assertIn('id="agentInlineTasks"', HTML)
        for function_name in (
            "normalizeAgentVideoTask", "startAgentVideoTaskFromPending",
            "pollAgentVideoTask", "renderAgentInlineTasks",
            "renderAgentCanvasTasks",
        ):
            self.assertIn("function %s" % function_name, HTML)
        normalize_pending = HTML.split(
            "function normalizeAgentPendingAction", 1
        )[1].split("function safeAgentPendingActionForSession", 1)[0]
        self.assertIn("job_id", normalize_pending)
        self.assertIn("agentCapabilityInfo(capability)", normalize_pending)
        for capability in (
            "digital-ip-text-generate", "video-generate",
            "cinematic-open-generate", "cinematic-motion-generate",
            "tryon-fast-generate", "tryon-classic-generate",
        ):
            self.assertIn(capability, HTML)
        self.assertNotIn("video_url", normalize_pending)
        confirm_logic = HTML.split(
            "function confirmAgentPendingAction", 1
        )[1].split("function saveAgentSession", 1)[0]
        self.assertIn("startAgentVideoTaskFromPending", confirm_logic)
        self.assertIn("/api/gen/job/", HTML)
        self.assertIn("/api/gen/video/assets?limit=30", HTML)
        self.assertIn("renderAgentCanvasTasks", HTML)

    def test_all_ready_recommendations_can_enter_the_existing_workbench(self):
        plan_logic = HTML.split(
            "function renderCanvasPlanCard", 1
        )[1].split("function renderAgentPlanStatus", 1)[0]
        view_logic = HTML.split(
            "function currentAgentPlanView", 1
        )[1].split("function ensureCanvasPlanCard", 1)[0]
        self.assertIn("can_open:!!route", view_logic)
        self.assertIn("view.ready&&view.can_open", plan_logic)
        self.assertIn("data-agent-plan-open", plan_logic)
        render_logic = HTML.split(
            "function renderAgentResult", 1
        )[1].split("function analyzeAgentRequest", 1)[0]
        self.assertNotIn("openAgentRecommendation()", render_logic)

    def test_agent_material_upload_is_explicit_and_owner_bound_route_is_used(self):
        self.assertIn("function uploadAgentMaterial", HTML)
        self.assertIn("function agentDetectedUploadMime", HTML)
        self.assertIn("header[0]===255&&header[1]===216&&header[2]===255", HTML)
        self.assertIn("return 'image/jpeg'", HTML)
        self.assertIn("/api/gen/video/agent/uploads/", HTML)
        self.assertIn("X-HQ-Image-SHA256", HTML)
        self.assertIn("X-HQ-Video-SHA256", HTML)
        request_logic = HTML.split("function requestVideoAgent", 1)[1].split(
            "function appendAgentMessage", 1
        )[0]
        self.assertIn("upload_id:item.upload_id", request_logic)
        self.assertNotIn("uploadAgentMaterial", request_logic)
        upload_logic = HTML.split("function uploadAgentMaterial", 1)[1].split(
            "function addAgentFiles", 1
        )[0]
        self.assertIn("agentUploadFailureMessage", upload_logic)
        self.assertIn("upload_storage_unavailable", HTML)
        self.assertNotIn("data.detail", upload_logic)

    def test_uploaded_materials_enter_local_identify_and_confirm_flow(self):
        self.assertIn("function agentMaterialPurposeOptions", HTML)
        self.assertIn("function queueAgentMaterialConfirmation", HTML)
        self.assertIn("function openAgentMaterialConfirmation", HTML)
        self.assertIn("function confirmAgentMaterialPurpose", HTML)
        add_files = HTML.split("function addAgentFiles", 1)[1].split(
            "function recommendAgentRoute", 1
        )[0]
        self.assertIn("purpose_state:'analyzing'", add_files)
        self.assertIn("queueAgentMaterialConfirmation(confirmationIds)", add_files)
        confirm_flow = HTML.split(
            "function agentMaterialPurposeOptions", 1
        )[1].split("function recommendAgentRoute", 1)[0]
        self.assertNotIn("requestVideoAgent(", confirm_flow)
        self.assertNotIn("confirmAgentPendingAction(", confirm_flow)

    def test_material_purpose_confirmation_is_visible_and_persisted(self):
        render = HTML.split("function renderMaterialCanvas", 1)[1].split(
            "function selectCanvasMaterial", 1
        )[0]
        self.assertIn("data-material-confirm-open", render)
        self.assertIn("agentMaterialPurposeStatusLabel(meta)", render)
        safe_material = HTML.split(
            "function safeAgentMaterialForSession", 1
        )[1].split("var AGENT_CURRENT_PAGE_CAPABILITIES", 1)[0]
        self.assertIn("purpose_state", safe_material)
        self.assertIn("purpose_options", safe_material)
        save_logic = HTML.split("function saveAgentSession", 1)[1].split(
            "function restoreAgentSession", 1
        )[0]
        self.assertIn("material_confirmation_id:agentMaterialConfirmationId", save_logic)
        request_logic = HTML.split("function requestVideoAgent", 1)[1].split(
            "function agentThreadNearLatest", 1
        )[0]
        self.assertIn("purpose:item.purpose", request_logic)
        self.assertIn("purpose_state:item.purpose_state", request_logic)

    def test_confirmed_person_image_can_create_and_bind_avatar_in_current_page(self):
        render = HTML.split("function renderMaterialCanvas", 1)[1].split(
            "function selectCanvasMaterial", 1
        )[0]
        self.assertIn("data-agent-avatar-create", render)
        self.assertIn("agentAvatarCreationAvailable(meta)", render)
        self.assertIn("人物图片已提供", HTML)
        self.assertIn("数字人已创建", HTML)
        available = HTML.split("function agentAvatarCreationAvailable", 1)[1].split(
            "function agentAvatarCreationCandidate", 1
        )[0]
        self.assertIn("agentAvatarLocalFile(meta)", available)
        self.assertIn("meta.media_cached===true", available)
        self.assertIn("/^img_[0-9a-f]{32}$/", available)
        payload = HTML.split("function agentAvatarCreationPayload", 1)[1].split(
            "function requestAgentAvatarCreation", 1
        )[0]
        self.assertIn("image_upload_id:uploadId", payload)
        self.assertIn("loadAgentMediaFile(owner,meta.canvas_id)", payload)
        self.assertIn("readFileData(localFile)", payload)
        self.assertIn("image_data:data", payload)
        create = HTML.split("function requestAgentAvatarCreation", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("window.confirm", create)
        self.assertIn("agentAvatarCreationPayload(meta)", create)
        self.assertIn("fetch('/api/gen/avatar'", create)
        self.assertLess(create.index("window.confirm"), create.index("agentAvatarCreationPayload(meta)"))
        self.assertIn("pollAgentAvatarCreation", HTML)
        safe_material = HTML.split(
            "function safeAgentMaterialForSession", 1
        )[1].split("var AGENT_CURRENT_PAGE_CAPABILITIES", 1)[0]
        self.assertIn("avatar_id", safe_material)
        self.assertIn("avatar_state", safe_material)
        request_logic = HTML.split("function requestVideoAgent", 1)[1].split(
            "function agentThreadNearLatest", 1
        )[0]
        self.assertIn("avatar_id:item.avatar_id", request_logic)
        confirm_purpose = HTML.split(
            "function confirmAgentMaterialPurpose", 1
        )[1].split("function queueAgentMaterialConfirmation", 1)[0]
        self.assertNotIn("requestAgentAvatarCreation", confirm_purpose)

    def test_material_confirmation_uses_compact_chat_choices_and_advances_queue(self):
        render = HTML.split(
            "function renderAgentMaterialConfirmation", 1
        )[1].split("function openAgentMaterialConfirmation", 1)[0]
        self.assertIn("data-material-purpose", render)
        self.assertIn("暂不确定", render)
        self.assertIn("其他用途", render)
        self.assertIn("输入这份素材的用途", render)
        confirm = HTML.split(
            "function confirmAgentMaterialPurpose", 1
        )[1].split("function queueAgentMaterialConfirmation", 1)[0]
        self.assertIn("meta.purpose_state='confirmed'", confirm)
        self.assertIn("openNextAgentMaterialConfirmation", confirm)

    def test_restored_canvas_material_has_explicit_reselect_control(self):
        render = HTML.split("function renderMaterialCanvas", 1)[1].split(
            "function selectCanvasMaterial", 1
        )[0]
        self.assertIn('data-canvas-reselect="', render)
        self.assertIn("chooseCanvasMaterialReplacement", render)
        chooser = HTML.split("function chooseCanvasMaterialReplacement", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("input.type='file'", chooser)
        self.assertIn("input.accept=type+'/*'", chooser)
        self.assertIn("addAgentFiles([file],'canvas',id)", chooser)
        add_files = HTML.split("function addAgentFiles", 1)[1].split(
            "function recommendAgentRoute", 1
        )[0]
        self.assertIn("replacementIndex", add_files)
        self.assertIn("actualType!==expectedType", add_files)

    def test_agent_task_session_does_not_persist_generated_media_urls(self):
        safe_task = HTML.split(
            "function safeAgentVideoTaskForSession", 1
        )[1].split("function renderAgentInlineTasks", 1)[0]
        self.assertIn("job_id", safe_task)
        self.assertNotIn("video_url", safe_task)
        save_logic = HTML.split(
            "function saveAgentSession", 1
        )[1].split("function restoreAgentSession", 1)[0]
        self.assertIn("agent_video_tasks:agentVideoTasks.map(safeAgentVideoTaskForSession)", save_logic)

    def test_v3_session_persists_only_allowlisted_agent_state(self):
        save_logic = HTML.split("function saveAgentSession", 1)[1].split("function restoreAgentSession", 1)[0]
        self.assertIn("version:3", save_logic)
        self.assertIn("safeAgentResultForSession(agentLastResult)", save_logic)
        self.assertIn("safeAgentPendingActionForSession", save_logic)
        self.assertNotIn("last_result:agentLastResult", save_logic)
        self.assertNotIn("quote_token", save_logic)
        self.assertNotIn("command", save_logic)
        self.assertIn("function safeAgentResultForSession", HTML)
        self.assertIn("function safeAgentPendingActionForSession", HTML)

    def test_v3_session_is_scoped_to_current_account_owner(self):
        owner_logic = HTML.split("function currentAgentSessionOwner", 1)[1].split(
            "function safeAgentBriefForSession", 1
        )[0]
        save_logic = HTML.split("function saveAgentSession", 1)[1].split(
            "function restoreAgentSession", 1
        )[0]
        restore_logic = HTML.split("function restoreAgentSession", 1)[1].split(
            "function resetAgentSession", 1
        )[0]
        self.assertIn("localStorage.getItem('hq_user')", owner_logic)
        self.assertIn("user&&user.username", owner_logic)
        self.assertIn("AGENT_SESSION_KEY_PREFIX+encodeURIComponent(owner)", owner_logic)
        self.assertIn("if(!owner||!storageKey", save_logic)
        self.assertIn("agentSessionOwner&&agentSessionOwner!==owner", save_logic)
        self.assertIn("owner:owner", save_logic)
        self.assertIn("localStorage.setItem(storageKey", save_logic)
        self.assertIn("if(!owner||!storageKey)return false", restore_logic)
        self.assertIn("state.owner!==owner", restore_logic)
        self.assertIn("localStorage.getItem(storageKey)", restore_logic)

    def test_agent_session_switches_owner_on_same_page_and_cross_tab_auth_changes(self):
        switch_logic = HTML.split("function clearAgentMemoryForOwner", 1)[1].split(
            "function resetAgentSession", 1
        )[0]
        for state_reset in (
            "agentSessionEpoch++", "agentConversation=[]", "agentPendingActions=[]",
            "agentConfirmKeys={}", "agentBusy=false", "$('agentPrompt').value=''",
            "$('workbenchAgentPrompt').value=''",
        ):
            self.assertIn(state_reset, switch_logic)
        self.assertIn("if(nextOwner)restoreAgentSession()", switch_logic)
        self.assertIn("function ensureAgentSessionOwner", switch_logic)
        self.assertIn("currentOwner!==agentSessionOwner", switch_logic)
        self.assertIn("window.addEventListener('hq:auth-changed',handleAgentAuthChanged)", HTML)
        self.assertIn("window.addEventListener('storage'", HTML)
        self.assertIn("e.key==='hq_user'", HTML)

        self.assertIn("function notifyAuthChanged(user)", CLOUD_SHELL)
        self.assertIn("new CustomEvent('hq:auth-changed'", CLOUD_SHELL)
        auth_success = CLOUD_SHELL.split("function authSuccess", 1)[1].split(
            "function hqDoLogin", 1
        )[0]
        logout = CLOUD_SHELL.split("function _logout", 1)[1].split(
            "function avatarHTML", 1
        )[0]
        self.assertIn("notifyAuthChanged(res.d&&res.d.user)", auth_success)
        self.assertIn("notifyAuthChanged(null)", logout)
        self.assertIn("if(previousUsername!==nextUsername)notifyAuthChanged(d.user)", CLOUD_SHELL)
        self.assertRegex(HTML, r'cloud-shell\.js\?v=[0-9a-f]{8}')

    def test_expired_cookie_session_opens_login_and_clears_stale_user_mirror(self):
        self.assertIn("function requireLogin()", CLOUD_SHELL)
        require_login = CLOUD_SHELL.split("function requireLogin()", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("localStorage.removeItem('hq_user')", require_login)
        self.assertIn("notifyAuthChanged(null)", require_login)
        self.assertIn("renderUser();openLogin()", require_login)
        self.assertIn("requireLogin:requireLogin", CLOUD_SHELL)
        self.assertIn("if(r.status===401){ if(currentUser()) requireLogin()", CLOUD_SHELL)
        unauthorized = HTML.split("function handleAgentUnauthorized", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("window.HQ.requireLogin", unauthorized)
        self.assertIn("Number(error&&error.status)!==401", unauthorized)
        self.assertGreaterEqual(HTML.count("handleAgentUnauthorized(error)"), 2)

    def test_sensitive_agent_actions_fail_closed_on_owner_change(self):
        confirm_logic = HTML.split("function confirmAgentPendingAction", 1)[1].split(
            "function saveAgentSession", 1
        )[0]
        home_analyze = HTML.split("function analyzeAgentRequest", 1)[1].split(
            "function collectAgentPreflight", 1
        )[0]
        apply_logic = HTML.split("function applyAgentFormUpdates", 1)[1].split(
            "function analyzeWorkbenchAgent", 1
        )[0]
        workbench_analyze = HTML.split("function analyzeWorkbenchAgent", 1)[1].split(
            "function handoffAgentFiles", 1
        )[0]
        self.assertIn("if(!ensureAgentSessionOwner())return Promise.resolve(false)", confirm_logic)
        self.assertIn("if(!ensureAgentSessionOwner()||agentBusy)return", home_analyze)
        self.assertIn("epoch!==agentSessionEpoch", home_analyze)
        self.assertIn("if(!ensureAgentSessionOwner())return false", apply_logic)
        self.assertIn("if(!ensureAgentSessionOwner()||agentBusy)return", workbench_analyze)
        self.assertIn("epoch!==agentSessionEpoch", workbench_analyze)

    def test_result_unknown_is_not_pay_retryable_and_has_read_only_reconcile(self):
        confirm_logic = HTML.split("function confirmAgentPendingAction", 1)[1].split(
            "function saveAgentSession", 1
        )[0]
        self.assertIn("error&&error.result_unknown===true", confirm_logic)
        self.assertIn("status:'result_unknown'", confirm_logic)
        self.assertIn("terminal=['failed','expired','cancelled','result_unknown']", confirm_logic)
        confirmable_logic = HTML.split("function isAgentPendingActionConfirmable", 1)[1].split(
            "function replaceAgentPendingActions", 1
        )[0]
        self.assertNotIn("result_unknown", confirmable_logic)
        reconcile_logic = HTML.split("function reconcileAgentPendingAction", 1)[1].split(
            "function saveAgentSession", 1
        )[0]
        self.assertIn("/reconcile", reconcile_logic)
        self.assertIn("body:'{}'", reconcile_logic)
        self.assertIn("agentReconcileBusyId", reconcile_logic)
        self.assertIn("pending_reconcile_in_flight", reconcile_logic)
        self.assertIn("startAgentVideoTaskFromPending(updated)", reconcile_logic)
        render = HTML.split("function renderAgentPendingActions", 1)[1].split(
            "function setAgentToolActivity", 1
        )[0]
        self.assertIn("data-agent-reconcile", render)

    def test_v3_session_removes_unscoped_legacy_keys(self):
        cleanup_logic = HTML.split("function removeLegacyAgentSessions", 1)[1].split(
            "function safeAgentBriefForSession", 1
        )[0]
        self.assertIn("AGENT_LEGACY_SESSION_KEYS.forEach", cleanup_logic)
        self.assertIn("localStorage.removeItem(key)", cleanup_logic)
        self.assertNotIn("localStorage.setItem(AGENT_SESSION_KEY", HTML)
        self.assertNotIn("localStorage.getItem(AGENT_SESSION_KEY", HTML)

    def test_agent_confirmation_is_single_flight_and_expiry_aware(self):
        self.assertIn("agentConfirmBusyId", HTML)
        self.assertIn("if(agentConfirmBusyId)return", HTML)
        self.assertIn("isAgentPendingActionExpired", HTML)
        self.assertIn("disabled", HTML.split("function renderAgentPendingActions", 1)[1].split("function ", 1)[0])
        self.assertIn("idempotency_key", HTML)

    def test_module_cards_and_back_button_share_workbench_navigation(self):
        self.assertIn('id="backToVideoHome"', HTML)
        self.assertIn("function openVideoModule(moduleKey)", HTML)
        self.assertIn("document.querySelectorAll('[data-video-module]')", HTML)
        self.assertIn("$('backToVideoHome').onclick=showVideoHome", HTML)

    def test_history_is_a_separate_view_reached_from_one_marker(self):
        self.assertIn('id="openVideoHistory"', HTML)
        self.assertIn('id="historyView" class="vid-card vid-main video-history-view hidden"', HTML)
        self.assertIn('id="backFromVideoHistory"', HTML)
        self.assertIn("function openVideoHistory()", HTML)
        self.assertIn("function closeVideoHistory()", HTML)
        self.assertIn("$('openVideoHistory').onclick=openVideoHistory", HTML)
        self.assertIn("$('backFromVideoHistory').onclick=closeVideoHistory", HTML)
        history_markup = HTML.split('id="historyView"', 1)[1].split('</section>', 1)[0]
        self.assertIn('id="historyChannelFilter"', history_markup)
        self.assertIn('id="h3ImportFile"', history_markup)

    def test_existing_prefill_and_direct_links_can_open_workbench(self):
        self.assertIn("function hasVideoDeepLink()", HTML)
        self.assertIn("if(hasVideoDeepLink()){", HTML)
        self.assertIn("openVideoWorkbench({function:deepFunction", HTML)

    def test_avatar_provider_errors_are_friendly_and_refund_aware(self):
        failure = HTML.split("function agentAvatarFailureMessage", 1)[1].split(
            "function pollAgentAvatarCreation", 1
        )[0]
        self.assertIn("insufficient", failure)
        self.assertIn("data&&data.refunded", failure)
        self.assertIn("数字人服务额度不足，本次未生成", failure)
        self.assertIn("点已退回", failure)
        self.assertIn("退款状态尚未确认，请到任务记录核对扣点与退款结果", failure)
        self.assertNotIn("将按系统规则自动退回", failure)
        self.assertIn("数字人服务当前繁忙", failure)
        self.assertIn("数字人服务响应超时", failure)
        self.assertIn("未检测到清晰人脸", failure)
        poll = HTML.split("function pollAgentAvatarCreation", 1)[1].split(
            "function restoreAgentAvatarCreations", 1
        )[0]
        self.assertIn("meta.avatar_error=agentAvatarFailureMessage(data)", poll)
        self.assertIn("if(data.refunded)delete meta.avatar_idempotency_key", poll)
        self.assertNotIn("meta.avatar_error=agentSafeText(data.error", poll)
        self.assertIn("tries<90?3000:10000", poll)
        self.assertNotIn("if(tries<90)setTimeout", poll)

    def test_canvas_text_editor_fills_resized_card_without_covering_handle(self):
        self.assertIn(".canvas-text-card{width:260px;min-width:260px;min-height:220px", HTML)
        self.assertIn("display:flex;flex-direction:column", HTML)
        text_style = HTML.split(".canvas-text-card textarea{", 1)[1].split("}", 1)[0]
        self.assertIn("flex:1", text_style)
        self.assertIn("min-height:0", text_style)
        self.assertIn("height:auto", text_style)
        self.assertIn("padding:12px 28px 28px 12px", text_style)
        self.assertIn("overflow:auto", text_style)
        self.assertNotIn("height:138px", text_style)

    def test_canvas_plan_card_shows_progress_confirmed_and_missing_fields(self):
        view = HTML.split("function currentAgentPlanView", 1)[1].split(
            "function ensureCanvasPlanCard", 1
        )[0]
        self.assertIn("confirmed=Object.keys(agentBrief)", view)
        self.assertIn("progress=ready?100", view)
        self.assertIn("已确认 ", view)
        render = HTML.split("function renderCanvasPlanCard", 1)[1].split(
            "function renderAgentPlanStatus", 1
        )[0]
        self.assertIn("canvas-plan-progress", render)
        self.assertIn("canvas-plan-tags confirmed", render)
        self.assertIn("canvas-plan-tags missing", render)
        self.assertIn("还需确认 ", render)
        self.assertIn("继续确认", render)
        self.assertIn('<button type="button" data-agent-plan-chat>', render)
        focus = HTML.split("function focusAgentPlanMissingField", 1)[1].split(
            "function renderAgentPlanStatus", 1
        )[0]
        self.assertIn("view.missing[0]", focus)
        self.assertIn("prompt.placeholder", focus)
        self.assertIn("focusAgentPlanMissingField()", HTML)
        self.assertIn("addEventListener('click',function(e){var planChat=e.target.closest('[data-agent-plan-chat]')", HTML)

    def test_session_and_media_expiry_or_account_switch_delete_local_user_data(self):
        restore = HTML.split("function restoreAgentSession", 1)[1].split(
            "function clearAgentMemoryForOwner", 1
        )[0]
        self.assertIn("age>AGENT_SESSION_TTL_MS", restore)
        self.assertIn("deleteAgentLocalDataForOwner(owner)", restore)
        load = HTML.split("function loadAgentMediaFile", 1)[1].split(
            "function deleteAgentMediaFile", 1
        )[0]
        self.assertIn("age>AGENT_SESSION_TTL_MS", load)
        self.assertIn("deleteAgentMediaFile(owner,canvasId)", load)
        switch = HTML.split("function switchAgentSessionOwner", 1)[1].split(
            "function ensureAgentSessionOwner", 1
        )[0]
        self.assertIn("deleteAgentLocalDataForOwner(previousOwner)", switch)

    def test_canvas_cards_have_keyboard_move_resize_and_image_preview(self):
        render = HTML.split("function renderMaterialCanvas", 1)[1].split(
            "function selectCanvasMaterial", 1
        )[0]
        self.assertIn('tabindex="0" role="group"', render)
        self.assertIn("moveCanvasCardByKeyboard", render)
        self.assertIn("resizeCanvasCardByKeyboard", render)
        self.assertIn('class="canvas-preview-button" type="button"', render)
        self.assertIn("button.onclick=function", render)

    def test_form_updates_preserve_all_backend_fields_and_unapplied_advice(self):
        safe = HTML.split("function safeAgentResultForSession", 1)[1].split(
            "function pendingActionExpiresAt", 1
        )[0]
        for field in ("style", "voice", "music"):
            self.assertIn("'" + field + "'", safe)
        apply_logic = HTML.split("function applyAgentFormUpdates", 1)[1].split(
            "function analyzeWorkbenchAgent", 1
        )[0]
        self.assertIn("remaining.push(update)", apply_logic)
        self.assertIn("agentPendingUpdates=remaining", apply_logic)
        self.assertIn("applyAgentNamedUpdate(update)", apply_logic)

    def test_player_does_not_revoke_shared_cached_blob_on_modal_close(self):
        player = HTML.split("function openVideoPlayer", 1)[1].split(
            "function toast", 1
        )[0]
        self.assertNotIn("URL.revokeObjectURL", player)
        release = HTML.split("function releaseAssetCache", 1)[1].split(
            "function blobToDataUrl", 1
        )[0]
        self.assertIn("URL.revokeObjectURL(src)", release)
        self.assertIn("window.addEventListener('pagehide',releaseAssetCache)", HTML)


if __name__ == "__main__":
    unittest.main()
