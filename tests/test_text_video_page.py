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

    def test_page_does_not_expose_provider_branding_or_manual_upload(self):
        self.assertNotIn("Pixelle", PAGE)
        self.assertNotIn("RunningHub", PAGE)
        self.assertNotIn('type="file"', PAGE)


if __name__ == "__main__":
    unittest.main()
