import base64
import hashlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch


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

    def test_official_micro_and_omni_parameters_are_validated_before_charge(self):
        from content_domains import feature_flags, video_gemini_omni, video_seedance
        with patch.object(feature_flags, "is_enabled", return_value=True), \
                patch.object(video_gemini_omni, "available", return_value=True), \
                patch.object(video_seedance, "available", return_value=True):
            omni = self.video.validate_xiaole_video_payload({
                "channel": "omni", "prompt": "product shot",
                "model": "gemini-omni-flash-preview", "ratio": "16:9",
                "duration": 3, "resolution": "720p",
            })
            seedance = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "paper bird",
                "model": "doubao-seedance-2-0-260128", "ratio": "adaptive",
                "duration": 4, "resolution": "480p", "generate_audio": True,
            })
        self.assertEqual(omni["duration"], 3)
        self.assertEqual(seedance["resolution"], "480p")

    def test_official_channels_default_closed_and_tabs_follow_health(self):
        from content_domains import feature_flags
        with patch.object(feature_flags, "_cached_rows", return_value={}):
            self.assertFalse(feature_flags.is_enabled("omni_video"))
            self.assertFalse(feature_flags.is_enabled("seedance_video"))
            self.assertFalse(feature_flags.is_enabled("minimax_h3_video"))
        html = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "video.html").read_text(encoding="utf-8")
        self.assertIn('class="function-tab hidden" type="button" data-function="omni"', html)
        self.assertIn('class="function-tab hidden" type="button" data-function="micro"', html)
        self.assertIn('class="function-tab hidden" type="button" data-function="minimax"', html)
        self.assertLess(
            html.index('data-function="sora"'),
            html.index('data-function="omni"'),
        )
        self.assertLess(
            html.index('data-function="omni"'),
            html.index('data-function="micro"'),
        )
        self.assertIn("omniAvailable=d.omni_video_enabled===true", html)
        self.assertIn("seedanceAvailable=d.seedance_video_enabled===true", html)
        self.assertIn("['grok','micro','omni','minimax'].indexOf(ch)<0", html)
        self.assertIn("gemini-omni-flash-preview", html)
        self.assertIn("doubao-seedance-2-0-260128", html)
        self.assertIn("doubao-seedance-2-0-fast-260128", html)
        self.assertIn('data-seedance-model="doubao-seedance-2-0-fast-260128" disabled', html)
        for seconds in range(3, 11):
            self.assertIn('data-omni-duration="%d"' % seconds, html)
        for seconds in range(4, 16):
            self.assertIn('data-seedance-duration="%d"' % seconds, html)
        self.assertIn("headers['Idempotency-Key']=requestKey", html)
        self.assertIn("OFFICIAL_VIDEO_BLOCK_STORAGE", html)
        self.assertIn("retry.blocked&&!retry.body", html)
        self.assertIn("if(!saveOfficialVideoRetry(channel))", html)
        self.assertIn("videoHealthReady.then(applyInspirationPrefill)", html)
        self.assertIn("targetMode==='omni'", html)
        self.assertIn("targetMode==='micro'", html)
        self.assertIn("setupXiaoleRefPanel('omni', omniRefData, 6)", html)
        self.assertIn("setupXiaoleRefPanel('micro', microRefData, 9)", html)
        self.assertIn("setupXiaoleRefPanel('minimax', minimaxRefData, 5)", html)

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

    def test_public_output_can_prefer_direct_download(self):
        import os as _os
        url = "https://cdn.example/output/video.mp4"
        with patch.dict(_os.environ, {"HEYGEN_RELAY_BASE": "https://heygen.zelong.vip"}, clear=False):
            cands = self.video._xiaole_download_candidates(
                url, "", direct_first=True)
        self.assertEqual(cands[0], (
            url, {"User-Agent": "huangque-content/1.0"}, None))
        self.assertIn("heygen.zelong.vip/cdn/", cands[1][0])

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

    def test_validate_official_grok_accepts_reference_duration_15(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            body = self.video.validate_xiaole_video_payload({
                "channel": "grok", "prompt": "cinematic demo",
                "duration": 15, "model": "grok-imagine-video",
                "reference_images": ["https://a/ref.jpg"],
            })
        self.assertEqual(body["duration"], 15)

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

    def test_reverse_grok_keeps_one_to_four_ordered_frames_and_stages_before_charge(self):
        from PIL import Image
        from content_domains import cos

        refs = []
        expected_hashes = []
        for shade in (24, 72, 120, 168):
            buffer = io.BytesIO()
            Image.new("RGB", (8, 8), (shade, 128, 255 - shade)).save(buffer, "PNG")
            raw = buffer.getvalue()
            expected_hashes.append(hashlib.sha256(raw).hexdigest())
            refs.append("data:image/png;base64," + base64.b64encode(raw).decode("ascii"))

        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            payload = self.video.validate_xiaole_video_payload({
                "channel": "grok", "prompt": "逐段还原", "duration": 10,
                "ratio": "9:16", "resolution": "720p",
                "reference_images": refs, "reference_mode": "ordered_storyboard",
            })
        self.assertEqual(refs, payload["reference_images"])
        self.assertEqual(4, payload["_reference_storyboard_count"])
        self.assertEqual(expected_hashes, payload["_reference_storyboard_source_hashes"])
        self.assertIn("按原视频时间顺序排列", payload["prompt"])

        uploads = []
        def publish(data, key, content_type=None, private=False):
            uploads.append((key, content_type, private))
            return "https://cos.example/" + key

        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video, "grok_reference_upload_is_open", return_value=True), \
             patch.object(self.video, "_persist_staging_cleanup_intent"), \
             patch.object(cos, "put_bytes", side_effect=publish):
            keys, error = self.video.stage_xiaole_video_references(
                "xiaole_video", payload, "fang", "a" * 32)
        self.assertIsNone(error)
        self.assertEqual(4, len(keys))
        self.assertEqual(4, len(set(keys)))
        self.assertEqual([False] * 4, [item[2] for item in uploads])
        self.assertEqual(
            ["https://cos.example/" + key for key in keys],
            payload["reference_images"],
        )
        self.assertEqual(keys, payload["_seedance_staged_keys"])

        generated = {
            "request_id": "xai-ordered", "model": "grok-imagine-video",
            "source_video_url": "https://vidgen.x.ai/ordered.mp4", "duration": 10,
        }
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video.provider_keys, "claim_candidate", return_value={
                 "id": "xai-key", "secret": "secret"
             }), \
             patch.object(self.video.provider_keys, "set_health"), \
             patch("content_domains.video_xai.generate", return_value=generated) as generate, \
             patch.object(self.video, "_download_xiaole_video", return_value="video/ordered.mp4"), \
             patch.object(self.video, "_extract_first_frame_cover", return_value=None), \
             patch.object(self.video, "public_url", return_value="https://cos.example/video/ordered.mp4"):
            result = self.video.gen_xiaole_video(payload)
        self.assertEqual(payload["reference_images"], generate.call_args.kwargs["reference_image_urls"])
        self.assertEqual(4, result["reference_storyboard_count"])
        self.assertEqual(expected_hashes, result["reference_storyboard_source_hashes"])

    def test_reverse_grok_rejects_remote_or_too_many_frames_before_charge(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            with self.assertRaisesRegex(ValueError, "本地关键帧"):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "逐段还原",
                    "reference_images": ["https://untrusted.example/frame.jpg"],
                    "reference_mode": "ordered_storyboard",
                })
            with self.assertRaisesRegex(ValueError, "1-4张"):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "逐段还原",
                    "reference_images": [], "reference_mode": "ordered_storyboard",
                })

    def test_legacy_xiaole_reverse_rejects_five_and_corrupt_data_urls_before_charge(self):
        corrupt = "data:image/png;base64,AA=="
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xiaole"):
            with self.assertRaisesRegex(ValueError, "1-4张"):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "逐段还原",
                    "reference_images": [corrupt] * 5,
                    "reference_mode": "ordered_storyboard",
                })
            with self.assertRaisesRegex(ValueError, "图片|参考图"):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "逐段还原",
                    "reference_images": [corrupt],
                    "reference_mode": "ordered_storyboard",
                })

    def test_legacy_xiaole_reverse_valid_frame_still_fails_before_charge(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (30, 60, 90)).save(buffer, "PNG")
        ref = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        body = {
            "channel": "grok", "prompt": "逐段还原",
            "reference_images": [ref], "reference_mode": "ordered_storyboard",
        }
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xiaole"):
            with self.assertRaisesRegex(ValueError, "不支持安全反推参考帧"):
                self.video.validate_xiaole_video_payload(body)
            self.assertTrue(self.video.xiaole_reference_needs_staging("xiaole_video", body))
            keys, error = self.video.stage_xiaole_video_references(
                "xiaole_video", body, "fang", "c" * 32)
        self.assertIsNone(keys)
        self.assertEqual(503, error[0])
        self.assertEqual("grok_reference_upload_unavailable", error[1]["code"])
        self.assertIn("未扣点", error[1]["detail"])

    def test_reverse_grok_staging_failure_is_precharge_503(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (40, 80, 120)).save(buffer, "PNG")
        ref = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            payload = self.video.validate_xiaole_video_payload({
                "channel": "grok", "prompt": "逐段还原",
                "reference_images": [ref], "reference_mode": "ordered_storyboard",
            })
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video, "grok_reference_upload_is_open", return_value=False):
            keys, error = self.video.stage_xiaole_video_references(
                "xiaole_video", payload, "fang", "b" * 32)
        self.assertIsNone(keys)
        self.assertEqual(503, error[0])
        self.assertEqual("grok_reference_upload_unavailable", error[1]["code"])
        self.assertIn("未扣点", error[1]["detail"])

    def test_validate_video_15_accepts_multiple_references(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            body = self.video.validate_xiaole_video_payload({
                "channel": "grok", "prompt": "让 @图片1 穿上 @图片2 的衣服",
                "model": "grok-imagine-video-1.5", "resolution": "720p",
                "reference_images": ["https://a/1.jpg", "https://a/2.jpg"],
            })
        self.assertEqual(2, len(body["reference_images"]))

    def test_validate_video_15_requires_reference(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            with self.assertRaisesRegex(ValueError, "至少需要1张参考图"):
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
             patch.object(self.video.provider_keys, "claim_candidate", return_value={
                 "id": "xai-key", "secret": "secret"
             }), \
             patch.object(self.video.provider_keys, "set_health"), \
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
             patch.object(self.video.provider_keys, "claim_candidate", side_effect=[
                 {"id": "xai-key", "secret": "secret"}, None
             ]), \
             patch.object(self.video.provider_keys, "set_health"), \
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
             patch.object(self.video.provider_keys, "candidates", return_value=[
                 {"id": "xai-key", "secret": "secret"}
             ]), \
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

    def test_seedance_official_never_calls_old_xiaole_supplier(self):
        fake = {
            "request_id": "cgt-1",
            "model": "doubao-seedance-2-0-260128",
            "source_video_url": "https://cdn.example/seedance.mp4",
            "duration": 4,
            "resolution": "480p",
            "ratio": "9:16",
            "generate_audio": True,
        }
        with patch("content_domains.video_seedance.generate", return_value=fake) as generate, \
             patch.object(self.video, "get_resumable_grok_request", return_value=None), \
             patch.object(self.video.provider_keys, "claim_candidate", return_value={
                 "id": "seedance-key", "secret": "secret"
             }), \
             patch.object(self.video.provider_keys, "set_health"), \
             patch.object(self.video, "update_video_asset_phase"), \
             patch.object(self.video, "_xiaole_request") as old_supplier, \
             patch.object(self.video, "_download_xiaole_video", return_value="video/seedance.mp4"), \
             patch.object(self.video, "_extract_first_frame_cover", return_value=None), \
             patch.object(self.video, "public_url", return_value="https://cos.example/seedance.mp4"):
            result = self.video.gen_xiaole_video({
                "_job_id": 7, "channel": "micro", "prompt": "paper bird",
                "model": "doubao-seedance-2-0-260128",
                "duration": 4, "ratio": "9:16", "resolution": "480p",
                "generate_audio": True,
            })
        old_supplier.assert_not_called()
        generate.assert_called_once()
        self.assertEqual(result["provider_video_id"], "cgt-1")
        self.assertEqual(result["provider"], "volcengine_seedance")

    def test_seedance_final_refs_are_validated_before_submitting_phase(self):
        bad_ref = "data:image/png;base64,cG5n"
        with patch.object(self.video, "_xiaole_ref_to_url", return_value=bad_ref), \
             patch.object(self.video, "get_resumable_grok_request", return_value=None), \
             patch.object(self.video, "update_video_asset_phase") as phase, \
             patch("content_domains.video_seedance.generate") as generate:
            with self.assertRaisesRegex(ValueError, "公网 URL"):
                self.video.gen_xiaole_video({
                    "_job_id": 7, "channel": "micro", "prompt": "paper bird",
                    "model": "doubao-seedance-2-0-260128",
                    "duration": 4, "ratio": "9:16", "resolution": "480p",
                    "generate_audio": True, "reference_images": [bad_ref],
                })
        phase.assert_not_called()
        generate.assert_not_called()

    def test_seedance_download_exhaustion_stays_resumable(self):
        from content_domains import video_seedance
        rendered = {
            "request_id": "cgt-download",
            "model": "doubao-seedance-2-0-260128",
            "source_video_url": "https://cdn.example/seedance.mp4",
            "duration": 4, "resolution": "480p", "ratio": "9:16",
            "generate_audio": True,
        }
        with patch.object(self.video, "get_resumable_grok_request", return_value={
            "request_id": "cgt-download", "provider": "seedance",
            "phase": "seedance_succeeded", "model": rendered["model"],
            "provider_key_id": "seedance-key",
        }), patch("content_domains.video_seedance.resume", return_value=rendered), \
             patch.object(self.video.provider_keys, "candidates", return_value=[
                 {"id": "seedance-key", "secret": "secret"}
             ]), \
             patch.object(self.video, "_download_xiaole_video",
                          side_effect=RuntimeError("视频下载失败: timeout")), \
             patch.object(self.video, "update_video_asset_phase"):
            with self.assertRaises(video_seedance.TransientSeedanceError):
                self.video.gen_xiaole_video({
                    "_job_id": 8, "channel": "micro", "prompt": "paper bird",
                    "model": rendered["model"], "duration": 4,
                    "ratio": "9:16", "resolution": "480p",
                    "generate_audio": True,
                })

    def test_omni_official_writes_bytes_without_old_supplier(self):
        fake = {
            "request_id": "v1-omni",
            "model": "gemini-omni-flash-preview",
            "source_video_url": "https://generativelanguage.googleapis.com/v1beta/files/f:download",
            "video_bytes": b"\x00\x00\x00\x18ftypmp42",
            "duration": 3,
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "provider": "google_gemini_omni",
        }
        with tempfile.TemporaryDirectory() as td, \
             patch("content_domains.video_gemini_omni.generate", return_value=fake) as generate, \
             patch.object(self.video, "get_resumable_grok_request", return_value=None), \
             patch.object(self.video.provider_keys, "claim_candidate", return_value={
                 "id": "omni-key", "secret": "secret"
             }), \
             patch.object(self.video.provider_keys, "set_health"), \
             patch.object(self.video, "update_video_asset_phase"), \
             patch.object(self.video, "_xiaole_request") as old_supplier, \
             patch.object(self.video, "_out_path",
                          side_effect=lambda rel: Path(td) / rel), \
             patch.object(self.video, "_faststart_video_file", side_effect=lambda rel: rel), \
             patch.object(self.video, "_extract_first_frame_cover", return_value=None), \
             patch.object(self.video, "public_url", return_value="https://cos.example/omni.mp4"):
            result = self.video.gen_xiaole_video({
                "_job_id": 8, "channel": "omni", "prompt": "product shot",
                "model": "gemini-omni-flash-preview",
                "duration": 3, "ratio": "16:9", "resolution": "720p",
            })
        old_supplier.assert_not_called()
        generate.assert_called_once()
        self.assertEqual(result["provider_video_id"], "v1-omni")
        self.assertEqual(result["provider"], "google_gemini_omni")

    def test_unknown_official_submission_is_held_without_refund_or_resubmit(self):
        for provider in ("xai", "seedance", "omni", "minimax"):
            with self.subTest(provider=provider), patch.object(
                self.video, "get_resumable_grok_request", return_value={
                    "request_id": None, "provider": provider,
                    "submission_unknown": True,
                    "phase": provider + "_submitting",
                },
            ), patch.object(self.video, "update_video_asset_phase") as update:
                self.assertTrue(
                    self.video.recover_official_video_paid_job(7, "response lost")
                )
                update.assert_called_once_with(
                    7, provider + "_recovery_required", error="response lost"
                )

    def test_unknown_official_submission_hold_never_expires_to_refund(self):
        for provider in ("xai", "seedance", "omni", "minimax"):
            with self.subTest(provider=provider), patch.object(
                self.video,
                "get_resumable_grok_request",
                return_value={
                    "request_id": None,
                    "provider": provider,
                    "submission_unknown": True,
                    "phase": provider + "_recovery_required",
                },
            ):
                self.assertFalse(
                    self.video.recovery_hold_expired(7, "xiaole_video", 99999, 1)
                )

    def test_omni_file_phase_remains_resumable(self):
        class Connection:
            def execute(self, *_args):
                return self

            def fetchone(self):
                return {
                    "provider_video_id": "v1-file", "model": "gemini-omni-flash-preview",
                    "phase": "omni_file_processing", "status": "running",
                    "resolution": "720p", "ratio": "16:9",
                }

            def close(self):
                pass

        with patch.object(self.video, "adb", return_value=Connection()):
            resumed = self.video.get_resumable_grok_request(9)
        self.assertEqual(resumed["provider"], "omni")
        self.assertEqual(resumed["request_id"], "v1-file")

    def test_core_unknown_official_create_never_refunds(self):
        from content_domains import core, video_gemini_omni

        for channel, error in (
            ("omni", video_gemini_omni.GeminiOmniCreateOutcomeUnknown("lost")),
            ("grok", json.JSONDecodeError("bad response", "x", 0)),
        ):
            class Connection:
                def execute(self, *_args):
                    return self

                def fetchone(self):
                    return {
                        "id": 7, "kind": "xiaole_video", "username": "u",
                        "cost": 90,
                        "payload": json.dumps({"channel": channel}),
                        "status": "pending",
                    }

                def close(self):
                    pass

            recover = Mock(return_value=True)
            terminal = Mock(return_value=True)
            refund = Mock()
            with self.subTest(channel=channel), \
                 patch.object(self.video, "recover_official_video_paid_job", recover), \
                 patch.object(core, "jdb", return_value=Connection()), \
                 patch.object(core.jobs_store, "claim_running", return_value=True), \
                 patch.object(core, "_start_job_heartbeat", return_value=Mock()), \
                 patch.object(core, "HANDLERS", {
                     "xiaole_video": Mock(side_effect=error),
                 }), \
                 patch.object(core, "_domains", return_value=(None, None, self.video)), \
                 patch.object(core, "_set_terminal", terminal), \
                 patch.object(core, "_refund_once", refund), \
                 patch.object(core, "_mark_video_asset_failed"):
                core.run_job(7)
            recover.assert_called_once()
            terminal.assert_not_called()
            refund.assert_not_called()

    def test_definite_xai_create_rejection_is_not_held(self):
        from content_domains import video_xai

        with patch.object(self.video, "recover_official_video_paid_job") as recover:
            held = self.video.recover_paid_video_error(
                7, "xiaole_video", {"channel": "grok"},
                video_xai.XaiCreateRejected("HTTP 400"),
            )
        self.assertFalse(held)
        recover.assert_not_called()

    def test_gen_grok_official_edit_uploads_source_and_preserves_contract(self):
        fake = {"request_id": "edit-1", "model": "grok-imagine-video",
                "source_video_url": "https://vidgen.x.ai/edit.mp4", "duration": 6.2}
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video.provider_keys, "claim_candidate", return_value={
                 "id": "xai-key", "secret": "secret"
             }), \
             patch.object(self.video.provider_keys, "set_health"), \
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
