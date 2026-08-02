import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")


class SoraVideoUiTests(unittest.TestCase):
    def test_user_can_open_a_visible_sora_panel(self):
        self.assertIn('class="function-tab" type="button" data-function="sora"', HTML)
        self.assertIn('id="soraPanel"', HTML)
        self.assertIn('id="soraPrompt"', HTML)
        self.assertIn('id="soraGenerateBtn"', HTML)
        self.assertIn("videoFunction==='sora'", HTML)

    def test_ui_matches_live_upstream_duration_and_resolution_contract(self):
        for seconds in (4, 8, 12):
            self.assertIn(f'data-sora-seconds="{seconds}"', HTML)
        self.assertNotIn('data-sora-seconds="10"', HTML)
        self.assertIn('data-sora-model="sora-2"', HTML)
        self.assertIn('data-sora-model="sora-2-pro"', HTML)
        for resolution in ("720p", "1024p", "1080p"):
            self.assertIn(f'data-sora-resolution="{resolution}"', HTML)

    def test_submit_is_health_gated_and_idempotent(self):
        self.assertIn("soraAvailable=d.sora_video_enabled===true", HTML)
        self.assertIn("max_user_active_sora_video", HTML)
        self.assertIn("fetch('/api/gen/sora_video'", HTML)
        self.assertIn("'Idempotency-Key':requestKey", HTML)
        self.assertIn("sessionStorage.setItem(SORA_RETRY_STORAGE", HTML)
        self.assertIn("restoreSoraRetry();", HTML)
        self.assertIn("if(soraRetryKey && soraRetryBody!==body)", HTML)
        self.assertIn("err.uncertain=res.status>=200 && res.status<300", HTML)
        self.assertIn("var uncertain=e.uncertain || !e.httpStatus || e.httpStatus>=500", HTML)
        self.assertIn("trackVideoJob(res.data.job_id", HTML)
        self.assertIn("pollJob(res.data.job_id,0)", HTML)

    def test_refresh_and_history_keep_sora_model_details(self):
        self.assertIn("mode:task.mode||''", HTML)
        self.assertIn("model:task.model||''", HTML)
        self.assertIn("duration:task.duration||''", HTML)
        self.assertIn("?'Sora 2 Pro':'Sora 2'", HTML)

    def test_points_preview_matches_backend_rates(self):
        for rate in (
            "'sora-2:720p':30",
            "'sora-2-pro:720p':90",
            "'sora-2-pro:1024p':150",
            "'sora-2-pro:1080p':210",
        ):
            self.assertIn(rate, HTML)

    def test_policy_and_sunset_are_visible_before_submit(self):
        self.assertIn("支持文生视频或 1 张非真人参考图", HTML)
        self.assertIn('id="soraRefGrid"', HTML)
        self.assertIn("reference_images:soraRefData", HTML)
        self.assertIn("2026-09-24", HTML)

    def test_prompt_reference_interactions_are_wired(self):
        self.assertIn('src="image-mentions.js?v=20260802f"', HTML)
        self.assertIn("HQImageMentions.bind(promptEl", HTML)
        self.assertIn("promptEl.addEventListener('drop'", HTML)
        self.assertIn("setupXiaoleRefPanel('sora', soraRefData, 1)", HTML)

    def test_banana_exact_route_accepts_the_backend_reference_budget(self):
        nginx = (ROOT / "deploy/nginx-huangquechuanmei.conf").read_text(encoding="utf-8")
        block = nginx.split("location = /api/gen/banana {", 1)[1].split("}", 1)[0]
        self.assertIn("client_max_body_size 80m;", block)


if __name__ == "__main__":
    unittest.main()
