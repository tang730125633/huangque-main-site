import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


class XiaoleVideoTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        from content_domains import video
        self.video = video

    def test_xiaole_request_retry_deadline_caps_internal_backoff(self):
        now = [0.0]
        calls = []

        def monotonic():
            return now[0]

        def sleep(seconds):
            now[0] += seconds

        def rate_limited(_request, timeout):
            calls.append(timeout)
            raise urllib.error.HTTPError(
                "https://example.test", 429, "busy", None, io.BytesIO(b"busy")
            )

        with patch.object(self.video, "XIAOLEVIDEO_API_KEY", "test-key"), \
             patch.object(self.video.time, "monotonic", side_effect=monotonic), \
             patch.object(self.video.time, "sleep", side_effect=sleep), \
             patch.object(self.video.urllib.request, "urlopen", side_effect=rate_limited):
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                self.video._xiaole_request("POST", "/api/v1/generations", {}, retry_deadline=10)

        self.assertEqual(len(calls), 2)
        self.assertAlmostEqual(now[0], 10)
        self.assertEqual(calls, [10, 2])

    def test_unstable_micro_and_omni_channels_are_rejected(self):
        for channel in ("micro", "omni"):
            with self.subTest(channel=channel):
                with self.assertRaisesRegex(ValueError, "渠道维护中"):
                    self.video.validate_xiaole_video_payload({"channel": channel, "prompt": "demo"})
                with self.assertRaisesRegex(ValueError, "渠道维护中"):
                    self.video.gen_xiaole_video({"channel": channel, "prompt": "demo"})

    def test_unstable_channel_tabs_are_hidden(self):
        html = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "video.html").read_text()
        self.assertIn('class="function-tab hidden" type="button" data-function="micro"', html)
        self.assertIn('class="function-tab hidden" type="button" data-function="omni"', html)
        self.assertIn("if(ch!=='grok') ch='grok';", html)

    def test_generate_xiaole_video_sends_size_without_aspect_ratio(self):
        calls = []

        def fake_request(method, path, body=None, timeout=90):
            calls.append((method, path, body, timeout))
            if method == "POST":
                return {"code": 200, "data": {"request_id": "rid-1", "status_url": "/status/rid-1"}}
            return {"data": {"status": "completed", "output": {"videos": [{"url": "https://cdn.example/video.mp4"}]}}}

        with patch.object(self.video, "_xiaole_request", side_effect=fake_request), \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_demo.mp4"), \
             patch.object(self.video, "GROK_VIDEO_PROVIDER", "xiaole"):
            result = self.video.generate_xiaole_video("Grok Image Video", "demo", size="1280x720", prefix="grok")

        self.assertEqual(result["video_file"], "video/grok_demo.mp4")
        self.assertEqual(calls[0][2]["input"]["size"], "1280x720")
        self.assertNotIn("aspect_ratio", calls[0][2]["input"])

    def test_xiaole_download_candidates_prefers_tunnel_over_relay(self):
        import os as _os
        url = "https://vidgen.x.ai/abc/video.mp4"
        with patch.dict(_os.environ, {"HEYGEN_RELAY_BASE": "https://heygen.zelong.vip"}, clear=False):
            cands = self.video._xiaole_download_candidates(url, "http://127.0.0.1:10809")
        # ① 快隧道优先：原始 URL + 隧道代理
        self.assertEqual(cands[0][0], url)
        self.assertEqual(cands[0][2], "http://127.0.0.1:10809")
        # ② heygen 中转兜底：走 relay /cdn/，不强制代理(None)
        self.assertIn("heygen.zelong.vip/cdn/vidgen.x.ai/", cands[1][0])
        self.assertIsNone(cands[1][2])
        # ③ 最后直连原始 URL
        self.assertEqual(cands[-1][0], url)
        self.assertIsNone(cands[-1][2])

    def test_xiaole_download_candidates_no_tunnel_is_legacy_order(self):
        import os as _os
        url = "https://vidgen.x.ai/abc/video.mp4"
        with patch.dict(_os.environ, {"HEYGEN_RELAY_BASE": "https://heygen.zelong.vip"}, clear=False):
            cands = self.video._xiaole_download_candidates(url, "")
        # 无隧道 → 退化为老行为：heygen 中转在前、直连兜底，无隧道档
        self.assertNotIn("10809", str(cands))
        self.assertIn("heygen.zelong.vip/cdn/", cands[0][0])
        self.assertIsNone(cands[0][2])
        self.assertEqual(cands[-1][0], url)
        self.assertIsNone(cands[-1][2])

    def test_authenticated_download_header_is_not_forwarded_to_relay(self):
        import os as _os
        url = "https://openrouter.ai/api/v1/videos/job/content?index=0"
        with patch.dict(_os.environ, {"HEYGEN_RELAY_BASE": "https://relay.example"}, clear=False):
            cands = self.video._xiaole_download_candidates(
                url, "http://127.0.0.1:10809",
                origin_headers={"Authorization": "Bearer secret"},
            )
        self.assertEqual(cands[0][1]["Authorization"], "Bearer secret")
        self.assertNotIn("Authorization", cands[1][1])
        self.assertEqual(cands[-1][1]["Authorization"], "Bearer secret")

    def test_gen_xiaole_video_maps_ratio_to_size_and_defaults_unknown_ratio(self):
        calls = []

        def fake_request(method, path, body=None, timeout=90):
            calls.append((method, path, body, timeout))
            if method == "POST":
                return {"code": 200, "data": {"request_id": "rid-1", "status_url": "/status/rid-1"}}
            return {"data": {"status": "completed", "output": {"videos": [{"url": "https://cdn.example/video.mp4"}]}}}

        with patch.object(self.video, "_xiaole_request", side_effect=fake_request), \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_demo.mp4"), \
             patch.object(self.video, "GROK_VIDEO_PROVIDER", "xiaole"):
            ok = self.video.gen_xiaole_video({"channel": "grok", "prompt": "demo", "ratio": "1:1"})
            fallback = self.video.gen_xiaole_video({"channel": "grok", "prompt": "demo", "ratio": "2:3"})

        self.assertEqual(ok["ratio"], "1:1")
        self.assertEqual(calls[0][2]["input"]["size"], "1024x1024")
        self.assertEqual(fallback["ratio"], "9:16")
        self.assertEqual(calls[2][2]["input"]["size"], "720x1280")
        self.assertNotIn("aspect_ratio", calls[0][2]["input"])
        self.assertNotIn("aspect_ratio", calls[2][2]["input"])

    def test_xiaole_ratio_channel_error_matches_supplier_size_message(self):
        self.assertTrue(self.video._is_xiaole_ratio_channel_error(
            '视频接口失败: HTTP 404 {"code":404,"message":"当前模型暂无支持该视频参数的可用渠道：渠道不支持当前视频尺寸"}'
        ))

    def test_generate_xiaole_video_normalizes_supplier_size_error(self):
        with patch.object(
            self.video,
            "_xiaole_request",
            side_effect=RuntimeError('视频接口失败: HTTP 404 {"code":404,"message":"当前模型暂无支持该视频参数的可用渠道：渠道不支持当前视频尺寸"}')
        ):
            with self.assertRaisesRegex(RuntimeError, "当前仅部分比例可用，请优先尝试 16:9（横屏）"):
                self.video.generate_xiaole_video("Grok Image Video", "demo", size="720x1280", prefix="grok")

    def test_validate_official_grok_parameters(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            body = self.video.validate_xiaole_video_payload({
                "channel": "grok", "prompt": "cinematic demo", "ratio": "2:3",
                "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
            })
        self.assertEqual(body["ratio"], "2:3")
        self.assertEqual(body["duration"], 10)

    def test_validate_official_grok_accepts_text_only_duration_15(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            body = self.video.validate_xiaole_video_payload({
                "channel": "grok", "prompt": "cinematic demo",
                "duration": 15, "model": "grok-imagine-video",
            })
        self.assertEqual(body["duration"], 15)

    def test_validate_official_grok_rejects_reference_duration_over_10(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            with self.assertRaisesRegex(ValueError, "1-10秒"):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "cinematic demo",
                    "duration": 15, "model": "grok-imagine-video",
                    "reference_images": ["https://a/ref.jpg"],
                })

    def test_validate_video_15_accepts_one_first_frame(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            body = self.video.validate_xiaole_video_payload({
                "channel": "grok", "prompt": "cinematic demo",
                "duration": 15, "model": "grok-imagine-video-1.5",
                "reference_images": ["https://a/first.jpg"],
            })
        self.assertEqual(body["duration"], 15)
        self.assertEqual(body["reference_images"], ["https://a/first.jpg"])

    def test_validate_official_edit_is_under_maintenance(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            with self.assertRaisesRegex(ValueError, "编辑维护中"):
                self.video.validate_xiaole_video_payload({"channel": "grok", "operation": "edit",
                                                          "prompt": "change person"})

    def test_validate_official_edit_rejects_before_media_processing(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video, "_probe_data_video_duration") as probe:
            with self.assertRaisesRegex(ValueError, "编辑维护中"):
                self.video.validate_xiaole_video_payload({"channel": "grok", "operation": "edit", "prompt": "demo"})
        probe.assert_not_called()

    def test_validate_official_grok_rejects_over_max_references(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            n = self.video.XIAOLE_MAX_REF + 1
            with self.assertRaisesRegex(ValueError, "最多支持%d张" % self.video.XIAOLE_MAX_REF):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "cinematic demo",
                    "reference_images": ["https://a/%d.jpg" % i for i in range(n)],
                })

    def test_validate_official_grok_accepts_multiple_references(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            cleaned = self.video.validate_xiaole_video_payload({
                "channel": "grok", "prompt": "cinematic demo",
                "reference_images": ["https://a/1.jpg", "https://a/2.jpg", "https://a/3.jpg"],
            })
            self.assertEqual(len(cleaned["reference_images"]), 3)

    def test_validate_video_15_rejects_multiple_first_frames(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            with self.assertRaisesRegex(ValueError, "仅支持1张首帧图"):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "cinematic demo",
                    "model": "grok-imagine-video-1.5",
                    "reference_images": ["https://a/1.jpg", "https://a/2.jpg"],
                })

    def test_validate_video_15_requires_reference(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            with self.assertRaisesRegex(ValueError, "仅支持1张首帧图"):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "cinematic demo",
                    "model": "grok-imagine-video-1.5",
                })

    def test_gen_grok_official_preserves_result_contract(self):
        fake = {
            "request_id": "xai-1", "model": "grok-imagine-video",
            "source_video_url": "https://vidgen.x.ai/demo.mp4", "duration": 10,
        }
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate", return_value=fake) as generate, \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_xai_demo.mp4"), \
             patch.object(self.video, "_extract_first_frame_cover", return_value="video/grok_xai_demo_cover.jpg"), \
             patch.object(self.video, "public_url", side_effect=[
                 "https://cos.example/cover.jpg",
                 "https://cos.example/video/grok_xai_demo.mp4",
             ]) as publish:
            result = self.video.gen_xiaole_video({
                "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
            })
        self.assertEqual(result["video_file"], "video/grok_xai_demo.mp4")
        self.assertEqual(result["video_url"], "https://cos.example/video/grok_xai_demo.mp4")
        self.assertEqual(result["provider_video_id"], "xai-1")
        self.assertEqual(result["model"], "grok-imagine-video")
        self.assertEqual(result["duration"], 10)
        publish.assert_any_call("video/grok_xai_demo.mp4", "video/mp4", private=True)
        generate.assert_called_once()

    def test_grok_does_not_fallback_after_xai_create_failure(self):
        from content_domains import video_xai

        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate",
                   side_effect=video_xai.XaiCreateUnavailableError("xAI quota")), \
             patch("content_domains.video_openrouter.generate") as generate:
            with self.assertRaises(video_xai.XaiCreateUnavailableError):
                self.video.gen_xiaole_video({
                    "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                    "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
                })
        generate.assert_not_called()

    def test_existing_xai_provider_id_resumes_without_generate(self):
        resumed = {
            "request_id": "rid-existing", "model": "grok-imagine-video",
            "source_video_url": "https://vidgen.x.ai/existing.mp4", "duration": 10,
        }
        payload = {
            "channel": "grok", "prompt": "demo", "model": "grok-imagine-video",
            "ratio": "9:16", "duration": 10, "resolution": "720p",
            "_job_id": 7, "_username": "qilin",
        }
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video.get_resumable_grok_request", return_value={
                 "request_id": "rid-existing", "model": "grok-imagine-video", "provider": "xai",
             }), \
             patch("content_domains.video_xai.resume", return_value=resumed) as resume, \
             patch("content_domains.video_xai.generate") as generate, \
             patch("content_domains.video._download_xiaole_video", return_value="video/out.mp4"), \
             patch("content_domains.video._extract_first_frame_cover", return_value=None), \
             patch("content_domains.video.update_video_asset_phase") as update:
            result = self.video.gen_xiaole_video(payload)
        generate.assert_not_called()
        resume.assert_called_once()
        self.assertNotIn("queued", [call.args[1] for call in update.call_args_list])
        self.assertEqual(result["provider_video_id"], "rid-existing")

    def test_gen_grok_official_edit_uploads_source_and_preserves_contract(self):
        fake = {"request_id": "edit-1", "model": "grok-imagine-video",
                "source_video_url": "https://vidgen.x.ai/edit.mp4", "duration": 6.2}
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video, "_save_data_file", return_value="video/source.mp4"), \
             patch.object(self.video, "public_url", side_effect=[
                 "https://cos.example/source.mp4",
                 "https://cos.example/cover.jpg",
                 "https://cos.example/edit.mp4",
             ]), \
             patch.object(self.video, "_file_url", return_value="/api/files/video/source.mp4"), \
             patch("content_domains.video_xai.edit", return_value=fake) as edit, \
             patch.object(self.video, "_download_xiaole_video", return_value="video/edit.mp4"), \
             patch.object(self.video, "_extract_first_frame_cover", return_value="video/edit_cover.jpg"):
            result = self.video.gen_xiaole_video({"channel": "grok", "operation": "edit", "prompt": "change person",
                                                  "reference_video_data": "data:video/mp4;base64,AAAA", "source_duration": 6.2})
        self.assertEqual(result["operation"], "edit")
        self.assertEqual(result["reference_video_file"], "video/source.mp4")
        self.assertIsNone(result["resolution"])
        edit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
