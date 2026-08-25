from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"


class MatrixTemplateVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(SERVER) not in sys.path:
            sys.path.insert(0, str(SERVER))
        cls.module = importlib.import_module("content_domains.matrix_template_video")

    def setUp(self):
        self.module._CACHE.update({"at": 0.0, "templates": []})

    def templates(self):
        return [{
            "id": "native-bold" if index == 0 else f"template-{index:02d}",
            "name": f"模板 {index}", "description": "说明", "tags": ["标签"],
        } for index in range(13)]

    def test_public_catalog_is_sanitized_and_requires_thirteen_templates(self):
        response = {"templates": self.templates()}
        with mock.patch.object(self.module, "_request", return_value=response):
            values = self.module.public_templates(force=True)
        self.assertEqual(13, len(values))
        self.assertEqual("native-bold", values[0]["id"])
        with mock.patch.object(self.module, "_request", return_value={"templates": values[:-1]}), \
             self.assertRaisesRegex(RuntimeError, "不完整"):
            self.module.public_templates(force=True)

    def test_validate_payload_is_library_only_and_catalog_bound(self):
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(self.module, "public_templates", return_value=self.templates()):
            payload = self.module.validate_payload({
                "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
                "template_id": "native-bold", "bgm": True,
            }, "alice")
            self.assertEqual("native-bold", payload["template_id"])
            with self.assertRaises(ValueError):
                self.module.validate_payload({
                    "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
                    "template_id": "unknown",
                }, "alice")
        self.assertNotIn("provider", payload)
        self.assertNotIn("prompt", payload)

    def test_generation_url_allows_https_or_loopback_only(self):
        for value in (
            "https://generation.example.com/internal/matrix-template",
            "http://127.0.0.1:8112",
        ):
            with self.subTest(value=value), mock.patch.object(self.module, "API_URL", value):
                self.assertTrue(self.module._validated_base().hostname)
        for value in (
            "http://generation.example.com/internal/matrix-template",
            "https://user:pass@generation.example.com/internal/matrix-template",
            "file:///tmp/service",
        ):
            with self.subTest(value=value), mock.patch.object(self.module, "API_URL", value), \
                 self.assertRaises(RuntimeError):
                self.module._validated_base()

    def test_generate_submits_polls_downloads_and_preserves_local_job_id(self):
        raw = {
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
            "template_id": "native-bold", "bgm": True,
            "_username": "alice", "_job_id": 77,
        }
        responses = [
            {"job_id": "a" * 32, "status": "pending"},
            {"job_id": "a" * 32, "status": "running"},
            {"job_id": "a" * 32, "status": "completed", "result": {
                "file_url": "/v1/files/%s.mp4" % ("a" * 32),
                "duration": 8.2, "width": 1080, "height": 1920,
                "template_id": "native-bold", "material_manifest": [{"record_id": "v1"}],
            }},
        ]
        with mock.patch.object(self.module, "validate_payload", return_value={
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
            "template_id": "native-bold", "bgm": True, "duration": None,
        }), mock.patch.object(self.module, "_request", side_effect=responses) as request, \
             mock.patch.object(self.module, "_download", return_value=("video/matrix_template_77.mp4", 4096)) as download, \
             mock.patch.object(self.module, "public_url", return_value="/api/gen/file/token"), \
             mock.patch.object(self.module.time, "sleep"):
            result = self.module.generate(raw)
        self.assertEqual("video/matrix_template_77.mp4", result["video_file"])
        self.assertEqual("/api/gen/file/token", result["video_url"])
        self.assertEqual("a" * 32, result["provider_task_id"])
        self.assertEqual("matrix-template-77", request.call_args_list[0].kwargs["request_id"])
        download.assert_called_once_with("/v1/files/%s.mp4" % ("a" * 32), "77")

    def test_pricing_and_feature_are_registered(self):
        from content_domains import feature_flags, points, pricing, registry

        self.assertIn("matrix_template_video", feature_flags.CATALOG_MAP)
        self.assertIn("video.matrix_template", pricing.CATALOG_MAP)
        self.assertEqual(
            pricing.get_price("video.matrix_template"),
            points.cost_of("matrix_template_video", {}),
        )
        self.assertIn("matrix_template_video", registry.HANDLERS)


class MatrixTemplatePageTests(unittest.TestCase):
    def test_page_and_sidebar_expose_feature_after_text_video(self):
        page = (ROOT / "site/workbench/matrix-template.html").read_text(encoding="utf-8")
        shell = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
        self.assertIn('data-active="matrix-template"', page)
        self.assertIn("/api/gen/matrix-template/templates", page)
        self.assertIn("/api/gen/matrix-template'", page)
        self.assertIn("Idempotency-Key", page)
        self.assertIn("平台素材库", page)
        self.assertLess(shell.index("k:'text-video'"), shell.index("k:'matrix-template'"))
        self.assertIn("/api/gen/matrix-template/capability", shell)

    def test_inline_javascript_parses(self):
        page = (ROOT / "site/workbench/matrix-template.html").read_text(encoding="utf-8")
        scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", page)
        source = next(value for value in reversed(scripts) if value.strip())
        result = subprocess.run(
            ["node", "--check", "-"], input=source,
            capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
