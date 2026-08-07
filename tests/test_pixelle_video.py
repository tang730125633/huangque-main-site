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
        self.assertEqual(len(templates), 20)
        self.assertEqual(len({item["key"] for item in templates}), 20)
        self.assertTrue(all(item["key"].startswith("1080x1920/") for item in templates))

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

    def test_submit_uses_async_service_contract(self):
        payload = self.pixelle.prepare_payload({"text": "AI 培训", "mode": "generate"})
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
