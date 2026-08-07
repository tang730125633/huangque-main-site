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
            "text": "第一句讲清问题。第二句给出方案。第三句总结价值。",
            "mode": "fixed",
        })
        self.assertGreaterEqual(fixed["n_scenes"], 1)
        self.assertLessEqual(fixed["n_scenes"], 20)

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
