from __future__ import annotations

import importlib
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
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
        self.assertEqual("matrix_template", result["mode"])
        self.assertEqual(("done", "1080p", "9:16"), (
            result["phase"], result["resolution"], result["ratio"]
        ))

    def test_completed_result_archives_in_real_video_assets_schema(self):
        from content_domains import core, video

        with tempfile.TemporaryDirectory() as temp:
            old = core.AUDIO_DB
            core.AUDIO_DB = Path(temp) / "assets.db"
            try:
                with closing(sqlite3.connect(core.AUDIO_DB)) as db:
                    db.execute("""CREATE TABLE video_assets(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,job_id INTEGER UNIQUE,
                        username TEXT NOT NULL,mode TEXT NOT NULL,image_file TEXT,
                        audio_file TEXT,reference_video_file TEXT,video_file TEXT,
                        video_url TEXT,text TEXT,voice_key TEXT,resolution TEXT,
                        ratio TEXT,motion TEXT,phase TEXT,image_asset_id TEXT,
                        audio_asset_id TEXT,reference_asset_id TEXT,provider_video_id TEXT,
                        provider_key_id TEXT,provider_avatar_id TEXT,
                        provider_avatar_group_id TEXT,source_video_url TEXT,
                        background_file TEXT,tryon_mode TEXT,model TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',error TEXT,
                        created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL)""")
                    db.commit()
                result = {
                    "mode": "matrix_template", "video_file": "video/final.mp4",
                    "video_url": "/api/gen/file/token", "resolution": "1080p",
                    "ratio": "9:16", "phase": "done", "status": "done",
                    "provider_task_id": "remote-1",
                }
                video.record_video_asset(77, "alice", result)
                with closing(sqlite3.connect(core.AUDIO_DB)) as db:
                    row = db.execute(
                        "SELECT mode,video_file,resolution,ratio,phase,status "
                        "FROM video_assets WHERE job_id=77"
                    ).fetchone()
                self.assertEqual(
                    ("matrix_template", "video/final.mp4", "1080p", "9:16", "done", "done"),
                    row,
                )
            finally:
                core.AUDIO_DB = old

    def test_pricing_and_feature_are_registered(self):
        from content_domains import feature_flags, points, pricing

        self.assertIn("matrix_template_video", feature_flags.CATALOG_MAP)
        self.assertIn("video.matrix_template", pricing.CATALOG_MAP)
        self.assertEqual(
            pricing.get_price("video.matrix_template"),
            points.cost_of("matrix_template_video", {}),
        )
        registry_source = (ROOT / "server/content_domains/registry.py").read_text(encoding="utf-8")
        self.assertIn("matrix_template_video", registry_source)

    def test_unified_function_names_cover_history_and_request_path(self):
        from server import func_names

        self.assertEqual("模板成片", func_names.func_name("matrix_template_video", {}))
        self.assertEqual("模板成片", func_names.path_func("/api/gen/matrix-template"))
        self.assertEqual("模板成片", func_names.path_func("/api/gen/matrix-template/templates"))


class MatrixTemplatePageTests(unittest.TestCase):
    def runtime(self, scenario):
        result = subprocess.run(
            ["node", str(ROOT / "tests/matrix_template_page_runtime.js"), scenario],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        return json.loads(result.stdout)

    def test_page_and_sidebar_expose_feature_after_text_video(self):
        page = (ROOT / "site/workbench/matrix-template.html").read_text(encoding="utf-8")
        shell = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
        self.assertIn('data-active="matrix-template"', page)
        self.assertIn("/api/gen/matrix-template/templates", page)
        self.assertIn("/api/gen/matrix-template'", page)
        self.assertIn("Idempotency-Key", page)
        self.assertNotIn('id="duration"', page)
        self.assertNotIn('id="bgm"', page)
        self.assertNotIn("素材来源", page)
        self.assertIn("template_id:activeTemplate,bgm:true", page)
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

    def test_post_response_loss_reuses_the_same_idempotency_key(self):
        result = self.runtime("postLoss")
        self.assertEqual(2, result["posts"])
        self.assertEqual(1, len(set(result["keys"])))
        self.assertTrue(all(body["bgm"] is True for body in result["bodies"]))
        self.assertTrue(all("duration" not in body for body in result["bodies"]))
        self.assertTrue(result["cleared"])

    def test_idempotency_in_progress_retries_the_same_claim(self):
        result = self.runtime("inProgress")
        self.assertEqual(1, len(set(result["keys"])))
        self.assertTrue(result["cleared"])

    def test_refresh_recovers_polling_without_new_submission(self):
        result = self.runtime("refresh")
        self.assertEqual(0, result["secondPosts"])
        self.assertGreaterEqual(result["secondPolls"], 1)
        self.assertTrue(result["cleared"])

    def test_single_poll_failure_keeps_busy_and_recovers(self):
        result = self.runtime("pollFailure")
        self.assertTrue(result["busyAfterFailure"])
        self.assertEqual(2, result["polls"])
        self.assertTrue(result["cleared"])


if __name__ == "__main__":
    unittest.main()
