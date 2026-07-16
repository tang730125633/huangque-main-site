import sys
import unittest
from pathlib import Path
from unittest.mock import patch


class XiaoleVideoTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        from content_domains import video
        self.video = video

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
                "duration": 15, "resolution": "720p", "model": "grok-imagine-video",
            })
        self.assertEqual(body["ratio"], "2:3")
        self.assertEqual(body["duration"], 15)

    def test_validate_official_edit_verifies_server_side_duration(self):
        source = "data:video/mp4;base64,AAAA"
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video, "_probe_data_video_duration", return_value=8.6):
            body = self.video.validate_xiaole_video_payload({"channel": "grok", "operation": "edit",
                                                              "prompt": "change person", "reference_video_data": source})
        self.assertEqual(body["source_duration"], 8.6)
        self.assertEqual(body["model"], "grok-imagine-video")

    def test_validate_official_edit_rejects_over_8_7_seconds(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video, "_probe_data_video_duration", return_value=8.71):
            with self.assertRaisesRegex(ValueError, "8.7"):
                self.video.validate_xiaole_video_payload({"channel": "grok", "operation": "edit", "prompt": "demo",
                                                          "reference_video_data": "data:video/mp4;base64,AAAA"})

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

    def test_validate_video_15_requires_reference(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            with self.assertRaisesRegex(ValueError, "仅支持图生视频"):
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
             patch.object(self.video, "public_url", return_value="https://cos.example/cover.jpg"):
            result = self.video.gen_xiaole_video({
                "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
            })
        self.assertEqual(result["video_file"], "video/grok_xai_demo.mp4")
        self.assertEqual(result["provider_video_id"], "xai-1")
        self.assertEqual(result["model"], "grok-imagine-video")
        self.assertEqual(result["duration"], 10)
        generate.assert_called_once()

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
             patch("content_domains.video.get_resumable_xai_request", return_value={
                 "request_id": "rid-existing", "model": "grok-imagine-video",
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
             patch.object(self.video, "public_url", side_effect=["https://cos.example/source.mp4", "https://cos.example/cover.jpg"]), \
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
