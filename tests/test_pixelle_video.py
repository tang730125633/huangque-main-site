import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class PixelleVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.pixelle = importlib.import_module("content_domains.pixelle_video")

    def test_public_template_catalog_matches_deployed_allowlist(self):
        templates = self.pixelle.public_templates()
        self.assertEqual(len(templates), 27)
        self.assertEqual(len({item["key"] for item in templates}), 27)
        self.assertEqual(
            sum(
                item["kind"] == "illustration"
                and item["orientation"] == "portrait"
                for item in templates
            ),
            20,
        )
        self.assertEqual(
            sum(
                item["kind"] == "illustration"
                and item["orientation"] == "landscape"
                for item in templates
            ),
            5,
        )
        self.assertEqual(
            sum(item["kind"] == "video" for item in templates),
            2,
        )
        self.assertTrue(all(item["orientation"] in {"portrait", "landscape"} for item in templates))
        self.assertTrue(all(item["preview_url"].startswith("../assets/pixelle-templates/") for item in templates))
        self.assertIn("1080x1920/image_default.html", self.pixelle.TEMPLATE_KEYS)

    def test_public_template_previews_exist(self):
        site_dir = Path(__file__).resolve().parents[1] / "site/workbench"
        for template in self.pixelle.public_templates():
            with self.subTest(template=template["key"]):
                self.assertIn("preview_url", template)
                preview = (site_dir / template["preview_url"]).resolve()
                self.assertTrue(preview.is_file())
                self.assertGreater(preview.stat().st_size, 0)

    def test_public_style_catalog_matches_private_allowlist(self):
        styles = self.pixelle.public_styles()
        self.assertEqual(len(styles), 10)
        self.assertEqual(len({item["key"] for item in styles}), 10)
        self.assertEqual(
            [item["key"] for item in styles],
            [
                "realistic_commercial",
                "cinematic",
                "future_tech",
                "healing_fresh",
                "chinese_illustration",
                "cartoon_3d",
                "retro_film",
                "minimal_line",
                "medical_beauty",
                "ecommerce_product",
            ],
        )
        self.assertEqual(self.pixelle.DEFAULT_STYLE, "realistic_commercial")
        self.assertTrue(all(set(item) == {"key", "name"} for item in styles))
        self.assertTrue(all("prompt_prefix" not in item for item in styles))
        self.assertTrue(all(
            self.pixelle.STYLE_PRESETS_BY_KEY[item["key"]]["prompt_prefix"]
            for item in styles
        ))

    def test_feature_catalog_is_fail_closed_by_default(self):
        meta = self.pixelle.feature_flags.CATALOG_MAP[self.pixelle.FEATURE_KEY]
        self.assertIs(meta["default_enabled"], False)

    def test_production_dropin_uses_private_generation_bridge(self):
        root = Path(__file__).resolve().parents[1]
        dropin = (
            root
            / "deploy/systemd/huangque-content.service.d/pixelle.conf"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "PIXELLE_API_URL=https://fang.huangquechuanmei.com/internal/pixelle",
            dropin,
        )
        self.assertNotIn("127.0.0.1:8103", dropin)
        self.assertIn(
            "PIXELLE_VIDEO_WORKFLOW=runninghub/video_wan2.1_fusionx.json",
            dropin,
        )

    def test_prepare_topic_and_fixed_copy(self):
        topic = self.pixelle.prepare_payload({
            "text": " AI 培训如何提升团队效率 ",
            "mode": "generate",
            "template": "1080x1920/image_modern.html",
        })
        self.assertEqual(topic["pipeline"], "pixelle")
        self.assertEqual(topic["n_scenes"], 5)
        self.assertEqual(topic["text"], "AI 培训如何提升团队效率")

        fixed = self.pixelle.prepare_payload({
            "text": "第一段讲清问题。\r\n\r\n  第二段给出方案。\n \n第三段总结价值。  ",
            "mode": "fixed",
        })
        self.assertEqual(fixed["n_scenes"], 3)
        self.assertEqual(fixed["text"], "第一段讲清问题。\n\n第二段给出方案。\n\n第三段总结价值。")
        self.assertEqual(
            [scene["line"] for scene in fixed["scenes"]],
            ["第一段讲清问题。", "第二段给出方案。", "第三段总结价值。"],
        )

    def test_fixed_copy_uses_upstream_paragraph_count_not_character_estimate(self):
        long_paragraph = "这是一段很长但没有空行的完整文案。" * 20
        prepared = self.pixelle.prepare_payload({"text": long_paragraph, "mode": "fixed"})
        self.assertEqual(prepared["n_scenes"], 1)

    def test_fixed_copy_rejects_more_than_twenty_upstream_paragraphs(self):
        text = "\n\n".join("第%d段" % index for index in range(21))
        with self.assertRaisesRegex(ValueError, "最多支持 20 个段落"):
            self.pixelle.prepare_payload({"text": text, "mode": "fixed"})

    def test_prepare_rejects_invalid_template_before_charge(self):
        with self.assertRaisesRegex(ValueError, "有效的视频模板"):
            self.pixelle.prepare_payload({"text": "测试主题", "template": "../../bad.html"})

    def test_prepare_uses_default_and_preserves_selected_style(self):
        default = self.pixelle.prepare_payload({"text": "AI 培训"})
        selected = self.pixelle.prepare_payload({
            "text": "AI 培训",
            "style": "future_tech",
        })
        self.assertEqual(default["style"], "realistic_commercial")
        self.assertEqual(selected["style"], "future_tech")

    def test_prepare_rejects_invalid_style_before_charge(self):
        with self.assertRaisesRegex(ValueError, "请选择有效的素材风格"):
            self.pixelle.prepare_payload({
                "text": "AI 培训",
                "style": "custom prompt injection",
            })

    def test_submit_legacy_payload_uses_default_style(self):
        legacy_payload = {
            "text": "AI training",
            "mode": "generate",
            "template": "1080x1920/image_default.html",
            "n_scenes": 5,
        }
        with mock.patch.object(
            self.pixelle, "_json_request", return_value={"task_id": "task-legacy"}
        ) as request:
            self.assertEqual(self.pixelle._submit(legacy_payload), "task-legacy")

        body = request.call_args.args[2]
        self.assertEqual(
            body["prompt_prefix"],
            self.pixelle.STYLE_PRESETS_BY_KEY[
                self.pixelle.DEFAULT_STYLE
            ]["prompt_prefix"],
        )

    def test_submit_rejects_invalid_style_at_execution_boundary(self):
        legacy_payload = {
            "text": "AI training",
            "mode": "generate",
            "template": "1080x1920/image_default.html",
            "n_scenes": 5,
            "style": "untrusted-style",
        }
        with self.assertRaisesRegex(ValueError, "请选择有效的素材风格"):
            self.pixelle._submit(legacy_payload)

    def test_submit_uses_async_service_contract(self):
        payload = self.pixelle.prepare_payload({
            "text": "AI 培训",
            "mode": "generate",
            "style": "medical_beauty",
        })
        with mock.patch.object(
            self.pixelle, "_json_request", return_value={"task_id": "task-1"}
        ) as request:
            task_id = self.pixelle._submit(payload)
        self.assertEqual(task_id, "task-1")
        method, path, body = request.call_args.args
        self.assertEqual((method, path), ("POST", "/api/video/generate/async"))
        self.assertEqual(body["frame_template"], payload["template"])
        self.assertEqual(body["n_scenes"], 5)
        self.assertIn("简体中文", body["text"])
        self.assertEqual(
            body["prompt_prefix"],
            self.pixelle.STYLE_PRESETS_BY_KEY["medical_beauty"]["prompt_prefix"],
        )
        self.assertEqual(body["media_workflow"], self.pixelle.PIXELLE_MEDIA_WORKFLOW)

    def test_submit_video_template_uses_video_workflow(self):
        payload = self.pixelle.prepare_payload({
            "text": "AI 培训",
            "mode": "generate",
            "template": "1080x1920/video_default.html",
            "style": "medical_beauty",
        })
        with mock.patch.object(
            self.pixelle, "_json_request", return_value={"task_id": "task-video"}
        ) as request:
            self.assertEqual(self.pixelle._submit(payload), "task-video")

        body = request.call_args.args[2]
        self.assertEqual(
            body["prompt_prefix"],
            self.pixelle.STYLE_PRESETS_BY_KEY["medical_beauty"]["prompt_prefix"],
        )
        self.assertEqual(body["media_workflow"], self.pixelle.PIXELLE_VIDEO_WORKFLOW)
        self.assertNotEqual(body["media_workflow"], self.pixelle.PIXELLE_MEDIA_WORKFLOW)

    def test_availability_is_fail_closed_and_checks_upstream_health(self):
        self.pixelle._HEALTH_CACHE.update({"checked_at": 0.0, "ready": False})
        with mock.patch.object(
            self.pixelle.feature_flags, "is_enabled", return_value=False
        ), mock.patch.object(self.pixelle, "_json_request") as request:
            self.assertEqual(self.pixelle.availability(), {
                "enabled": False, "ready": False, "available": False,
            })
        request.assert_not_called()

        self.pixelle._HEALTH_CACHE.update({"checked_at": 0.0, "ready": False})
        with mock.patch.object(
            self.pixelle.feature_flags, "is_enabled", return_value=True
        ), mock.patch.object(
            self.pixelle, "_json_request", return_value={"status": "healthy"}
        ) as request:
            self.assertTrue(self.pixelle.availability(force=True)["available"])
        request.assert_called_once_with("GET", "/health", timeout=3)

    def test_require_available_rejects_enabled_but_unhealthy_service(self):
        self.pixelle._HEALTH_CACHE.update({"checked_at": 0.0, "ready": False})
        with mock.patch.object(
            self.pixelle.feature_flags, "require_enabled"
        ), mock.patch.object(
            self.pixelle.feature_flags, "is_enabled", return_value=True
        ), mock.patch.object(
            self.pixelle, "_json_request", side_effect=RuntimeError("offline")
        ):
            with self.assertRaisesRegex(
                self.pixelle.feature_flags.FeatureDisabled, "暂不可用"
            ):
                self.pixelle.require_available()

    def test_wait_returns_result_and_surfaces_failure(self):
        with mock.patch.object(self.pixelle, "_json_request", return_value={
            "status": "completed", "result": {"video_url": "/api/files/result.mp4"},
        }):
            self.assertEqual(
                self.pixelle._wait("task-1")["video_url"], "/api/files/result.mp4"
            )
        with mock.patch.object(self.pixelle, "_json_request", return_value={
            "status": "failed", "error": "render failed",
        }):
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                self.pixelle._wait("task-2")

    def test_wait_retries_transient_poll_timeout_until_task_completes(self):
        class JsonResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"status":"completed","result":{"video_url":"/api/files/result.mp4"}}'

        with mock.patch.object(
            self.pixelle._NO_PROXY,
            "open",
            side_effect=[TimeoutError("read timed out"), JsonResponse()],
        ) as request, mock.patch.object(self.pixelle.time, "sleep") as sleep:
            result = self.pixelle._wait("task-transient")

        self.assertEqual(result["video_url"], "/api/files/result.mp4")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(self.pixelle.PIXELLE_POLL_INTERVAL)

    def test_wait_reports_job_timeout_after_repeated_transient_poll_errors(self):
        with mock.patch.object(self.pixelle, "PIXELLE_JOB_TIMEOUT", 1), \
             mock.patch.object(
                 self.pixelle.time, "monotonic", side_effect=[0, 0, 1]
             ), \
             mock.patch.object(
                 self.pixelle._NO_PROXY, "open", side_effect=TimeoutError("read timed out")
             ), \
             mock.patch.object(self.pixelle.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                self.pixelle._wait("task-timeout")

    def test_upstream_video_url_is_confined_to_service_files(self):
        accepted = self.pixelle._safe_upstream_video_url("/api/files/result.mp4")
        self.assertTrue(accepted.endswith("/api/files/result.mp4"))
        with self.assertRaisesRegex(RuntimeError, "无效的文件地址"):
            self.pixelle._safe_upstream_video_url("https://example.com/api/files/result.mp4")
        with self.assertRaisesRegex(RuntimeError, "无效的文件路径"):
            self.pixelle._safe_upstream_video_url("/admin/secrets")

    def test_prefixed_service_url_rewrites_rooted_file_url_through_bridge(self):
        api_url = "https://fang.huangquechuanmei.com/internal/pixelle"
        with mock.patch.object(self.pixelle, "PIXELLE_API_URL", api_url):
            rooted = self.pixelle._safe_upstream_video_url(
                "https://fang.huangquechuanmei.com/api/files/result.mp4"
            )
            relative = self.pixelle._safe_upstream_video_url("/api/files/result.mp4")
            prefixed = self.pixelle._safe_upstream_video_url(
                "/internal/pixelle/api/files/result.mp4"
            )
        expected = api_url + "/api/files/result.mp4"
        self.assertEqual(rooted, expected)
        self.assertEqual(relative, expected)
        self.assertEqual(prefixed, expected)

    def test_generate_persists_service_result_in_authenticated_asset_path(self):
        payload = self.pixelle.prepare_payload({"text": "AI 培训", "mode": "generate"})
        payload["_job_id"] = 42
        with mock.patch.object(self.pixelle, "_submit", return_value="task-42"), \
             mock.patch.object(self.pixelle, "_wait", return_value={
                 "video_url": "/api/files/result.mp4", "duration": 31.25,
             }), \
             mock.patch.object(self.pixelle, "_download_video", return_value=(
                 "video/pixelle_42.mp4", 4096,
             )), \
             mock.patch.object(self.pixelle, "public_url", return_value="/api/gen/file/token"):
            result = self.pixelle.generate(payload)
        self.assertEqual(result["video_url"], "/api/gen/file/token")
        self.assertEqual(result["duration"], 31.25)
        self.assertEqual(result["scene_count"], 5)
        self.assertEqual(result["style"], payload["style"])
        self.assertNotIn("prompt_prefix", result)

    def test_generate_legacy_payload_records_default_style(self):
        legacy_payload = {
            "text": "AI training",
            "mode": "generate",
            "template": "1080x1920/image_default.html",
            "n_scenes": 5,
            "_job_id": 43,
        }
        with mock.patch.object(self.pixelle, "_submit", return_value="task-43"), \
             mock.patch.object(self.pixelle, "_wait", return_value={
                 "video_url": "/api/files/result.mp4", "duration": 30,
             }), \
             mock.patch.object(self.pixelle, "_download_video", return_value=(
                 "video/pixelle_43.mp4", 4096,
             )), \
             mock.patch.object(self.pixelle, "public_url", return_value="/api/gen/file/token"):
            result = self.pixelle.generate(legacy_payload)

        self.assertEqual(result["style"], self.pixelle.DEFAULT_STYLE)
        self.assertNotIn("style", legacy_payload)

    def test_download_checks_only_header_without_reading_whole_file(self):
        source = b"\x00\x00\x00\x18ftypisom" + b"x" * 2048

        class Response:
            headers = {"Content-Length": str(len(source))}

            def __init__(self):
                self.offset = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, size):
                chunk = source[self.offset:self.offset + size]
                self.offset += len(chunk)
                return chunk

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(self.pixelle, "OUT_DIR", Path(directory)), \
             mock.patch.object(self.pixelle._NO_PROXY, "open", return_value=Response()):
            relative, size = self.pixelle._download_video(
                "http://127.0.0.1:8103/api/files/result.mp4", 7
            )
        self.assertEqual(relative, "video/pixelle_7.mp4")
        self.assertEqual(size, len(source))


if __name__ == "__main__":
    unittest.main()
