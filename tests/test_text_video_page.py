import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "site/workbench/text-video.html").read_text(encoding="utf-8")
SHELL = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
CORE = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
FLAGS = (ROOT / "server/content_domains/feature_flags.py").read_text(encoding="utf-8")


class TextVideoPageTests(unittest.TestCase):
    def _run_page_runtime(self, scenario):
        result = subprocess.run(
            ["node", str(ROOT / "tests/text_video_page_runtime.js"), scenario],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(result.stdout)

    def _run_talking_state(self, statements):
        start_marker = "/* talking-state:start */"
        end_marker = "/* talking-state:end */"
        self.assertIn(start_marker, PAGE)
        self.assertIn(end_marker, PAGE)
        source = PAGE.split(start_marker, 1)[1].split(end_marker, 1)[0]
        script = source + "\n" + statements
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(result.stdout)

    def test_talking_switch_is_default_off_and_panel_is_hidden(self):
        self.assertIn('id="talkingMaterialEnabled"', PAGE)
        self.assertIn('type="checkbox"', PAGE)
        self.assertNotIn('id="talkingMaterialEnabled" checked', PAGE)
        self.assertIn('id="talkingMaterialPanel" hidden', PAGE)
        self.assertIn('启用口播视频素材', PAGE)

    def test_talking_controls_are_accessible_and_have_stable_defaults(self):
        self.assertIn('id="talkingDefaultAvatar"', PAGE)
        self.assertIn('accept="image/jpeg,image/png,image/webp"', PAGE)
        self.assertIn('id="talkingAvatarPreview"', PAGE)
        self.assertIn('id="replaceTalkingAvatar"', PAGE)
        self.assertIn('id="removeTalkingAvatar"', PAGE)
        self.assertIn('id="talkingRatio"', PAGE)
        self.assertIn('min="10"', PAGE)
        self.assertIn('max="50"', PAGE)
        self.assertIn('value="30"', PAGE)
        self.assertIn('id="talkingScenes"', PAGE)
        self.assertIn('aria-live="polite"', PAGE)

    def test_talking_state_preserves_scene_edits_without_replanning(self):
        state = self._run_talking_state("""
const state=createTalkingState();
state.setEnabled(true);
state.setDefaultAvatar({asset_id:'avatar_default',preview_url:'/default'});
state.setPlan({plan_id:'plan_1',source_hash:'hash_1',ratio:0.3,scenes:[
  {scene_id:'scene_01',text:'第一幕',estimated_duration:5.8,talking_recommended:true},
  {scene_id:'scene_02',text:'第二幕',estimated_duration:6.4,talking_recommended:false}
]});
state.setSceneEnabled('scene_01',false);
state.setSceneEnabled('scene_02',true);
state.setSceneAvatar('scene_02',{asset_id:'avatar_other',preview_url:'/other'});
const before=state.snapshot();
const payload=state.buildTalkingMaterial(30);
process.stdout.write(JSON.stringify({before:before,payload:payload}));
""")
        self.assertEqual(state["before"]["plan"]["plan_id"], "plan_1")
        self.assertFalse(state["before"]["sceneSelections"]["scene_01"])
        self.assertTrue(state["before"]["sceneSelections"]["scene_02"])
        self.assertEqual(state["before"]["sceneAvatarOverrides"]["scene_02"]["asset_id"], "avatar_other")
        self.assertEqual(state["payload"], {
            "enabled": True,
            "plan_id": "plan_1",
            "source_hash": "hash_1",
            "ratio": 0.3,
            "default_avatar_asset_id": "avatar_default",
            "scenes": [
                {"scene_id": "scene_01", "enabled": False},
                {"scene_id": "scene_02", "enabled": True, "avatar_asset_id": "avatar_other"},
            ],
        })

    def test_nine_scene_plan_serializes_only_requested_talking_selections(self):
        state = self._run_talking_state("""
const state=createTalkingState();
state.setEnabled(true);
state.setDefaultAvatar({asset_id:'local_avatar_A',preview_url:'/avatar-a'});
state.setPlan({plan_id:'plan_9',source_hash:'hash_9',ratio:1/3,scenes:[
  {scene_id:'scene_01',text:'scene 1',estimated_duration:6,talking_recommended:true},
  {scene_id:'scene_02',text:'scene 2',estimated_duration:6,talking_recommended:false},
  {scene_id:'scene_03',text:'scene 3',estimated_duration:6,talking_recommended:false},
  {scene_id:'scene_04',text:'scene 4',estimated_duration:6,talking_recommended:false},
  {scene_id:'scene_05',text:'scene 5',estimated_duration:6,talking_recommended:true},
  {scene_id:'scene_06',text:'scene 6',estimated_duration:6,talking_recommended:false},
  {scene_id:'scene_07',text:'scene 7',estimated_duration:6,talking_recommended:false},
  {scene_id:'scene_08',text:'scene 8',estimated_duration:6,talking_recommended:false},
  {scene_id:'scene_09',text:'scene 9',estimated_duration:6,talking_recommended:true}
]});
state.setSceneAvatar('scene_05',{asset_id:'local_avatar_B',preview_url:'/avatar-b'});
process.stdout.write(JSON.stringify(state.buildTalkingMaterial(33.333333)));
""")
        self.assertEqual(state["default_avatar_asset_id"], "local_avatar_A")
        self.assertEqual(
            [item["scene_id"] for item in state["scenes"] if item["enabled"]],
            ["scene_01", "scene_05", "scene_09"],
        )
        self.assertNotIn("avatar_asset_id", state["scenes"][0])
        self.assertEqual(state["scenes"][4]["avatar_asset_id"], "local_avatar_B")
        self.assertNotIn("avatar_asset_id", state["scenes"][8])

    def test_plan_input_changes_invalidate_but_scene_changes_do_not(self):
        state = self._run_talking_state("""
const state=createTalkingState();
state.setEnabled(true);
state.setDefaultAvatar({asset_id:'avatar_default',preview_url:'/default'});
state.setPlan({plan_id:'plan_1',source_hash:'hash_1',ratio:0.3,scenes:[
  {scene_id:'scene_01',text:'第一幕',estimated_duration:6,talking_recommended:true}
]});
state.setSceneEnabled('scene_01',false);
const afterSceneEdit=state.snapshot();
state.invalidatePlan();
const afterPlanInput=state.snapshot();
process.stdout.write(JSON.stringify({afterSceneEdit:afterSceneEdit,afterPlanInput:afterPlanInput}));
""")
        self.assertEqual(state["afterSceneEdit"]["plan"]["plan_id"], "plan_1")
        self.assertIsNone(state["afterPlanInput"]["plan"])
        self.assertEqual(state["afterPlanInput"]["sceneSelections"], {})
        self.assertEqual(state["afterPlanInput"]["sceneAvatarOverrides"], {})

    def test_page_implements_two_stage_plan_then_paid_confirmation(self):
        self.assertIn("fetch('/api/gen/text-video/plan'", PAGE)
        self.assertIn("生成分镜方案", PAGE)
        self.assertIn("确认并生成视频", PAGE)
        self.assertIn("talking_material:talkingState.buildTalkingMaterial", PAGE)
        self.assertIn("default_avatar_asset_id", PAGE)
        self.assertIn("source_hash", PAGE)
        self.assertIn("plan_id", PAGE)

    def test_plan_invalidation_is_bound_to_all_plan_inputs(self):
        self.assertIn("bindPlanInvalidation(el('videoText'),'input')", PAGE)
        self.assertIn("bindPlanInvalidation(el('materialStyle'),'change')", PAGE)
        self.assertIn("bindPlanInvalidation(el('videoVoice'),'change')", PAGE)
        self.assertIn("bindPlanInvalidation(el('speechRate'),'input')", PAGE)
        self.assertIn("bindPlanInvalidation(el('talkingRatio'),'input')", PAGE)
        self.assertIn("invalidateTalkingPlan();selectMode(button)", PAGE)
        self.assertIn("invalidateTalkingPlan();selectTemplate(button,template)", PAGE)
        self.assertIn("function selectKind(button){\n    invalidateTalkingPlan();", PAGE)
        self.assertIn("function selectOrientation(button){\n    invalidateTalkingPlan();", PAGE)
        self.assertNotIn("setSceneEnabled(sceneId,enabled);invalidateTalkingPlan", PAGE)
        self.assertNotIn("setSceneAvatar(sceneId,avatar);invalidateTalkingPlan", PAGE)

    def test_upload_planning_and_final_errors_are_separate(self):
        for field in ("talkingUploadError", "talkingPlanError", "talkingFinalError"):
            self.assertIn('id="%s"' % field, PAGE)
        self.assertIn("validateAvatarDataUrl", PAGE)
        self.assertIn("HQ-ASSET-001", PAGE)
        self.assertIn("上传人物图片失败", PAGE)
        self.assertIn("生成分镜方案失败", PAGE)
        self.assertIn("视频任务提交失败", PAGE)

    def test_talking_progress_and_non_blocking_warnings_are_rendered(self):
        self.assertIn("正在生成口播素材", PAGE)
        self.assertIn("talking_warnings", PAGE)
        self.assertIn('id="talkingWarnings"', PAGE)
        self.assertIn("renderTalkingWarnings", PAGE)

    def test_late_plan_response_cannot_restore_invalidated_plan(self):
        result = self._run_page_runtime("latePlan")
        self.assertEqual(result["button"], "生成分镜方案")
        self.assertNotIn("旧方案", result["status"])
        self.assertLessEqual(result["scenes"], 1)

    def test_every_plan_input_invalidates_an_inflight_response(self):
        result = self._run_page_runtime("planMutations")
        self.assertEqual(
            set(result),
            {"text", "mode", "voice", "speechRate", "style", "ratio", "template", "kind", "orientation", "enabled", "defaultAvatar"},
        )
        self.assertTrue(all(result.values()), result)

    def test_avatar_uploads_are_last_write_wins_and_block_paid_submit(self):
        result = self._run_page_runtime("avatarRace")
        self.assertTrue(result["blockedWhilePending"])
        self.assertEqual(result["paidWhilePending"], 0)
        self.assertEqual(result["payload"]["talking_material"]["default_avatar_asset_id"], "avatar-new")
        self.assertGreaterEqual(len(result["revoked"]), 2)

    def test_scene_avatar_uploads_are_last_write_wins(self):
        result = self._run_page_runtime("sceneAvatarRace")
        self.assertTrue(result["blockedWhilePending"])
        scenes = result["payload"]["talking_material"]["scenes"]
        self.assertEqual(scenes[0]["avatar_asset_id"], "scene-new")

    def test_plan_invalidation_cleans_all_pending_scene_avatar_uploads(self):
        result = self._run_page_runtime("sceneAvatarInvalidation")
        self.assertTrue(result["blockedWhilePending"])
        self.assertEqual(
            result["afterInvalidation"],
            {
                "disabled": False,
                "button": "生成分镜方案",
                "aborted": [True, True],
                "revoked": ["blob:avatar-1", "blob:avatar-2", "blob:avatar-3"],
            },
        )
        self.assertEqual(
            result["afterStaleCallbacks"],
            {
                "disabled": True,
                "button": "生成分镜方案",
                "error": "",
                "status": "正在上传人物图片",
                "revoked": ["blob:avatar-1", "blob:avatar-2", "blob:avatar-3"],
            },
        )
        self.assertTrue(result["defaultBlockedWhilePending"])
        material = result["payload"]["talking_material"]
        self.assertEqual(material["default_avatar_asset_id"], "avatar-replacement")
        self.assertNotIn("stale-scene", json.dumps(material))
        self.assertEqual(result["revoked"], [
            "blob:avatar-1", "blob:avatar-2", "blob:avatar-3", "blob:avatar-4",
        ])

    def test_poll_prefers_real_phase_over_legacy_stage(self):
        result = self._run_page_runtime("phase")
        self.assertIn("正在生成口播素材", result["status"])
        self.assertNotIn("普通素材阶段", result["status"])

    def test_default_off_runtime_path_submits_exact_legacy_payload(self):
        result = self._run_page_runtime("disabledPath")
        self.assertEqual(result["planRequests"], 0)
        self.assertNotIn("talking_material", result["payload"])
        self.assertEqual(result["payload"]["pipeline"], "pixelle")

    def test_declined_quote_never_reaches_paid_submission(self):
        result = self._run_page_runtime("quoteCancel")
        self.assertEqual(1, result["quoteRequests"])
        self.assertEqual(0, result["paidRequests"])
        self.assertIn("未扣点", result["status"])

    def test_stale_quote_is_aborted_without_confirmation_or_paid_submission(self):
        result = self._run_page_runtime("staleQuote")
        self.assertEqual("修改后的新文案", result["visibleText"])
        self.assertTrue(result["quoteAborted"])
        self.assertEqual(0, result["confirms"])
        self.assertEqual(0, result["paidRequests"])

    def test_talking_planning_and_avatar_routes_are_wired(self):
        self.assertIn('/api/gen/text-video/plan', CORE)
        self.assertIn('/api/gen/text-video/avatar', CORE)
        self.assertIn('private, max-age=300', CORE)

    def test_page_uses_authenticated_paid_job_pipeline(self):
        self.assertIn("/api/gen/script_to_video", PAGE)
        self.assertIn("pipeline:'pixelle'", PAGE)
        self.assertIn("Idempotency-Key", PAGE)
        self.assertIn("/api/gen/job/", PAGE)

    def test_uncertain_submission_reuses_persisted_body_and_idempotency_key(self):
        self.assertIn("hq-text-video-pending-v1", PAGE)
        self.assertIn("sessionStorage.setItem", PAGE)
        self.assertIn("saved.body===body&&saved.key", PAGE)
        self.assertIn("body:pending.body", PAGE)
        self.assertIn("headers['Idempotency-Key']=pending.key", PAGE)
        self.assertIn("response.status===409&&response.data.code==='idempotency_in_progress'", PAGE)
        self.assertIn("if(shouldConfirmSubmission(response))confirmSubmission(pending)", PAGE)
        self.assertIn("confirmSubmission(pending);", PAGE)

    def test_idempotency_in_progress_keeps_original_retry_key(self):
        helpers = PAGE.split("function requestKey()", 1)[1].split("function authHeaders", 1)[0]
        script = "var pendingStorageKey='hq-text-video-pending-v1';\nfunction requestKey()" + helpers + """
const values = new Map();
global.sessionStorage = {
  getItem: (key) => values.has(key) ? values.get(key) : null,
  setItem: (key, value) => values.set(key, value),
  removeItem: (key) => values.delete(key),
};
global.window = {crypto: {randomUUID: () => 'stable-key'}};
global.crypto = global.window.crypto;
const payload = {pipeline: 'pixelle', text: 'test', style: 'future_tech'};
const first = pendingSubmission(payload);
const response = {status: 409, data: {code: 'idempotency_in_progress'}};
if (shouldConfirmSubmission(response)) confirmSubmission(first);
const retry = pendingSubmission(payload);
process.stdout.write(JSON.stringify({first: first.key, retry: retry.key}));
"""
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        keys = json.loads(result.stdout)
        self.assertTrue(keys["first"].startswith("text-video-"))
        self.assertEqual(keys["first"], keys["retry"])

    def test_template_catalog_is_authenticated_and_not_hardcoded_to_service(self):
        self.assertIn("/api/gen/text-video/templates", PAGE)
        for path in (
            "/api/gen/text-video/capability",
            "/api/gen/text-video/templates",
        ):
            self.assertIn(path, CORE)
        self.assertNotIn("127.0.0.1:8103", PAGE)
        self.assertNotIn("/api/video/generate/async", PAGE)

    def test_style_catalog_is_authenticated_sanitized_and_readiness_gated(self):
        for path in (
            "/api/gen/text-video/capability",
            "/api/gen/text-video/templates",
            "/api/gen/text-video/styles",
        ):
            self.assertIn(path, CORE)
        self.assertIn('"styles": pixelle_video.public_styles()', CORE)
        self.assertIn('"default_style": pixelle_video.DEFAULT_STYLE', CORE)

    def test_material_style_dropdown_loads_public_catalog_without_thumbnails(self):
        self.assertIn('id="materialStyle"', PAGE)
        self.assertIn('aria-label="素材风格"', PAGE)
        self.assertIn("/api/gen/text-video/styles", PAGE)
        self.assertIn("response.data.default_style", PAGE)
        self.assertIn("option.value=style.key", PAGE)
        self.assertIn("option.textContent=style.name", PAGE)
        self.assertNotIn("style.preview_url", PAGE)

    def test_generation_requires_loaded_style_and_submits_it_idempotently(self):
        self.assertIn("if(!stylesReady)", PAGE)
        self.assertIn("素材风格暂不可用", PAGE)
        self.assertIn("style:el('materialStyle').value", PAGE)
        self.assertIn("var pending=pendingSubmission(payload)", PAGE)

    def test_style_load_failure_keeps_generation_disabled(self):
        self.assertIn("stylesReady=false", PAGE)
        self.assertIn("syncGenerateButton()", PAGE)
        self.assertIn("select.disabled=true", PAGE)

    def test_voice_catalog_is_authenticated_and_generation_is_readiness_gated(self):
        self.assertIn("/api/gen/text-video/voices", CORE)
        self.assertIn('"voices": pixelle_video.public_voices(user["username"])', CORE)
        self.assertIn('id="videoVoice"', PAGE)
        self.assertIn('aria-label="配音音色"', PAGE)
        self.assertIn("/api/gen/text-video/voices", PAGE)
        self.assertIn("voicesReady=false", PAGE)
        self.assertIn("isBusy||!stylesReady||!voicesReady", PAGE)

    def test_speech_rate_control_is_accessible_and_defaults_to_one_x(self):
        self.assertIn('id="speechRate"', PAGE)
        self.assertIn('type="range"', PAGE)
        self.assertIn('aria-label="语速调节"', PAGE)
        self.assertIn('min="0.5"', PAGE)
        self.assertIn('max="2"', PAGE)
        self.assertIn('step="0.1"', PAGE)
        self.assertIn('value="1"', PAGE)
        self.assertIn('id="speechRateValue"', PAGE)
        self.assertIn(">1.0x<", PAGE)

    def test_generation_submits_namespaced_voice_selection(self):
        self.assertIn("voice:el('videoVoice').value", PAGE)
        self.assertIn("if(!voicesReady)", PAGE)
        self.assertIn("音色暂不可用", PAGE)
        self.assertIn("option.value=voice.id", PAGE)
        self.assertIn("个人音色", PAGE)

    def test_generation_submits_and_displays_current_speech_rate(self):
        self.assertIn("speech_rate:Number(el('speechRate').value)", PAGE)
        self.assertIn("el('speechRate').addEventListener('input',updateSpeechRateValue)", PAGE)
        self.assertIn("el('speechRateValue').textContent=Number(el('speechRate').value).toFixed(1)+'x'", PAGE)

    def test_template_gallery_uses_preview_images_and_filters(self):
        self.assertIn('data-kind="illustration"', PAGE)
        self.assertIn('data-kind="video"', PAGE)
        self.assertIn('data-orientation="portrait"', PAGE)
        self.assertIn('data-orientation="landscape"', PAGE)
        self.assertIn("template.preview_url", PAGE)
        self.assertIn("image.loading='lazy'", PAGE)
        self.assertIn("image.onerror=function()", PAGE)
        self.assertIn("tv-template-selected", PAGE)

    def test_selected_template_updates_result_aspect_ratio(self):
        self.assertIn("template.orientation==='landscape'", PAGE)
        self.assertIn("stage.classList.toggle('landscape',isLandscape)", PAGE)
        self.assertIn(".tv-stage.landscape{aspect-ratio:16/9", PAGE)

    def test_feature_is_default_off_and_all_entry_points_use_readiness_gate(self):
        self.assertIn('"key": "pixelle_text_video"', FLAGS)
        self.assertIn('"default_enabled": False', FLAGS)
        self.assertIn("/api/gen/text-video/capability", CORE)
        self.assertIn("pixelle_video.require_available()", CORE)
        self.assertIn("feature:'pixelle_text_video'", SHELL)
        self.assertIn("data-nav-feature", SHELL)
        self.assertIn("if(data&&data.available){item.hidden=false;item.style.display='flex';}", SHELL)

    def test_topic_and_full_copy_are_explicit_modes(self):
        self.assertIn('data-mode="generate"', PAGE)
        self.assertIn('data-mode="fixed"', PAGE)
        self.assertIn("主题创作", PAGE)
        self.assertIn("完整文案", PAGE)

    def test_sidebar_exposes_text_video_workspace(self):
        self.assertIn("{k:'text-video',l:'文案成片',i:'clapper',feature:'pixelle_text_video'}", SHELL)
        self.assertIn("active==='text-video'", SHELL)

    def test_page_does_not_expose_provider_branding_and_only_uploads_talking_avatars(self):
        self.assertNotIn("Pixelle", PAGE)
        self.assertNotIn("RunningHub", PAGE)
        self.assertEqual(PAGE.count('type="file"'), 2)
        self.assertIn('id="talkingDefaultAvatar"', PAGE)
        self.assertIn('class="tv-scene-avatar-input"', PAGE)


if __name__ == "__main__":
    unittest.main()
