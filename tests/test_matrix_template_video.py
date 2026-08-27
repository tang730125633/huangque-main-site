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
from types import SimpleNamespace
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
        special = {0: "native-bold", 13: "full-overlay-bold", 14: "poster-split"}
        return [{
            "id": special.get(index, f"template-{index:02d}"),
            "name": f"模板 {index}", "description": "说明", "tags": ["标签"],
        } for index in range(15)]

    def test_public_catalog_is_sanitized_and_requires_fifteen_templates(self):
        response = {"templates": self.templates()}
        with mock.patch.object(self.module, "_request", return_value=response):
            values = self.module.public_templates(force=True)
        self.assertEqual(15, len(values))
        self.assertEqual("native-bold", values[0]["id"])
        self.assertEqual(
            {"full-overlay-bold", "poster-split"},
            {item["id"] for item in values[-2:]},
        )
        with mock.patch.object(self.module, "_request", return_value={"templates": values[:-1]}), \
             self.assertRaisesRegex(RuntimeError, "不完整"):
            self.module.public_templates(force=True)

    def test_public_catalog_rejects_missing_conversion_template(self):
        values = self.templates()
        values[-1] = {**values[-1], "id": "replacement-template"}
        with mock.patch.object(self.module, "_request", return_value={"templates": values}), \
             self.assertRaisesRegex(RuntimeError, "不完整"):
            self.module.public_templates(force=True)

    def test_availability_requires_fifteen_template_runtime(self):
        with mock.patch.object(self.module.feature_flags, "is_enabled", return_value=True), \
             mock.patch.object(self.module, "_request", return_value={"ok": True, "templates": 15}):
            self.assertTrue(self.module.availability()["ready"])
        with mock.patch.object(self.module.feature_flags, "is_enabled", return_value=True), \
             mock.patch.object(self.module, "_request", return_value={"ok": True, "templates": 13}):
            self.assertFalse(self.module.availability()["ready"])

    def test_validate_payload_is_library_only_and_catalog_bound(self):
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(self.module, "public_templates", return_value=self.templates()), \
             mock.patch.object(self.module, "_request", return_value={"payload": {
                 "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
                 "template_id": "native-bold", "bgm": True, "duration": 8.0,
             }}) as request:
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
            request.assert_called_once_with(
                "POST", "/v1/preflight", {
                    "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
                    "template_id": "native-bold", "bgm": True, "duration": None,
                }, timeout=10,
            )
        self.assertNotIn("provider", payload)
        self.assertNotIn("prompt", payload)

    def test_validate_payload_uses_authoritative_67_68_visible_character_boundary(self):
        accepted = {
            "top_text": "中" * 60, "bottom_text": "A" * 7 + "，。！？",
            "template_id": "native-bold", "bgm": True, "duration": 14.9,
        }
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(self.module, "public_templates", return_value=self.templates()), \
             mock.patch.object(self.module, "_request", return_value={"payload": accepted}):
            result = self.module.validate_payload({
                "top_text": "中" * 60,
                "bottom_text": "A" * 7 + "，。！？",
                "template_id": "native-bold",
            }, "alice")
        self.assertEqual(14.9, result["duration"])

        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(self.module, "public_templates", return_value=self.templates()), \
             mock.patch.object(
                 self.module, "_request",
                 side_effect=self.module.MatrixTemplateHTTPError(400, "文案过长，请缩短标题或行动文案"),
             ), self.assertRaisesRegex(ValueError, "文案过长"):
            self.module.validate_payload({
                "top_text": "中" * 60, "bottom_text": "A" * 8,
                "template_id": "native-bold",
            }, "alice")

    def test_preflight_unavailable_maps_404_5xx_and_network_to_feature_disabled(self):
        from content_domains import feature_flags

        body = {
            "top_text": "有效标题", "bottom_text": "有效行动文案",
            "template_id": "native-bold",
        }
        for error in (
            self.module.MatrixTemplateHTTPError(404, "not found"),
            self.module.MatrixTemplateHTTPError(503, "maintenance"),
            RuntimeError("network unavailable"),
        ):
            with self.subTest(error=error), \
                 mock.patch.object(self.module, "require_available"), \
                 mock.patch.object(self.module, "public_templates", return_value=self.templates()), \
                 mock.patch.object(self.module, "_request", side_effect=error), \
                 self.assertRaises(feature_flags.FeatureDisabled):
                self.module.validate_payload(body, "alice")

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

    def test_accepted_job_is_durably_reconciled_without_second_charge(self):
        from content_domains import jobs_store, submission_idempotency

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "jobs.db"

            def database():
                connection = sqlite3.connect(path)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(database()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,username TEXT NOT NULL,cost INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',payload TEXT NOT NULL,
                    result TEXT,error TEXT,refunded INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,owner TEXT
                )""")
                submission_idempotency.ensure_table(connection)
                connection.commit()

            body = {
                "top_text": "有效标题", "bottom_text": "关注查看更多",
                "template_id": "native-bold", "bgm": True,
            }
            key = "creator-accepted-reconcile"
            state, _ = submission_idempotency.begin(
                database, "alice", "/api/gen/matrix-template", key, body,
            )
            self.assertEqual(state, "new")
            deductions = []

            def deduct(username, amount, reason, transaction_key):
                deductions.append((username, amount, transaction_key))
                return 95

            job_id, _ = jobs_store.create_paid_job(
                database, deduct, lambda *_args, **_kwargs: True,
                "matrix_template_video", "alice", 5, body, "content",
                charge_transaction_key="job-charge:alice:/api/gen/matrix-template:" + key,
                before_commit=lambda connection, accepted_job_id: (
                    submission_idempotency.accept_in_transaction(
                        connection, "alice", "/api/gen/matrix-template", key, body,
                        {"job_id": accepted_job_id, "cost": 5, "accepted": True},
                    )
                ),
            )
            replay_state, response = submission_idempotency.replay_existing(
                database, "alice", "/api/gen/matrix-template", key, [body],
            )
            self.assertEqual(replay_state, "replay")
            self.assertEqual(response["job_id"], job_id)
            self.assertTrue(response["accepted"])
            with closing(database()) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1,
                )
            self.assertEqual(len(deductions), 1)

    def test_unified_function_names_cover_history_and_request_path(self):
        from server import func_names

        self.assertEqual("模板成片", func_names.func_name("matrix_template_video", {}))
        self.assertEqual("模板成片", func_names.path_func("/api/gen/matrix-template"))
        self.assertEqual("模板成片", func_names.path_func("/api/gen/matrix-template/templates"))

    def test_cli_quote_validates_matrix_payload_before_returning_cost(self):
        from content_domains import cli_gateway

        class Handler:
            headers = {"X-HQ-Internal-Token": "secret"}

            def _token(self): return "account-token"
            def _json_body_strict(self):
                return {"kind": "matrix_template_video", "payload": {
                    "top_text": "有效标题", "bottom_text": "有效行动文案",
                    "template_id": "native-bold", "bgm": True,
                }}
            def _send(self, status, body): self.result = (status, body)

        handler = Handler()
        normalized = {
            "top_text": "有效标题", "bottom_text": "有效行动文案",
            "template_id": "native-bold", "bgm": True, "duration": None,
        }
        feature_flags = SimpleNamespace(
            require_enabled=mock.Mock(), FeatureDisabled=RuntimeError,
        )
        points = SimpleNamespace(
            cost_of=mock.Mock(return_value=5), get_points=mock.Mock(return_value=100),
        )
        with mock.patch.object(self.module, "validate_payload", return_value=normalized) as validate:
            handled = cli_gateway.handle_quote(
                handler, "/api/gen/cli/quote", lambda _token: {"username": "alice"},
                lambda _user: False, lambda: False, feature_flags, points,
                SimpleNamespace(), SimpleNamespace(), "secret",
            )
        self.assertTrue(handled)
        self.assertEqual((200, "matrix_template_video", 5, 100), (
            handler.result[0], handler.result[1]["kind"],
            handler.result[1]["cost"], handler.result[1]["points"],
        ))
        validate.assert_called_once()
        points.cost_of.assert_called_once_with("matrix_template_video", normalized)

    def test_cli_quote_rejects_failed_preflight_without_returning_cost(self):
        from content_domains import cli_gateway

        class Handler:
            headers = {"X-HQ-Internal-Token": "secret"}
            def _token(self): return "account-token"
            def _json_body_strict(self):
                return {"kind": "matrix_template_video", "payload": {
                    "top_text": "中" * 60, "bottom_text": "A" * 8,
                    "template_id": "native-bold", "bgm": True,
                }}
            def _send(self, status, body): self.result = (status, body)

        handler = Handler()
        points = SimpleNamespace(
            cost_of=mock.Mock(return_value=5), get_points=mock.Mock(return_value=100),
        )
        with mock.patch.object(
            self.module, "validate_payload", side_effect=ValueError("文案过长")
        ):
            cli_gateway.handle_quote(
                handler, "/api/gen/cli/quote", lambda _token: {"username": "alice"},
                lambda _user: False, lambda: False,
                SimpleNamespace(require_enabled=mock.Mock(), FeatureDisabled=RuntimeError),
                points, SimpleNamespace(), SimpleNamespace(), "secret",
            )
        self.assertEqual(400, handler.result[0])
        self.assertIn("文案过长", handler.result[1]["detail"])
        points.cost_of.assert_not_called()

    def test_cli_quote_preflight_unavailable_returns_structured_503(self):
        from content_domains import cli_gateway, feature_flags

        class Handler:
            headers = {"X-HQ-Internal-Token": "secret"}
            def _token(self): return "account-token"
            def _json_body_strict(self):
                return {"kind": "matrix_template_video", "payload": {
                    "top_text": "有效标题", "bottom_text": "有效行动文案",
                    "template_id": "native-bold", "bgm": True,
                }}
            def _send(self, status, body): self.result = (status, body)

        handler = Handler()
        points = SimpleNamespace(
            cost_of=mock.Mock(return_value=5), get_points=mock.Mock(return_value=100),
        )
        flags = SimpleNamespace(
            require_enabled=mock.Mock(), FeatureDisabled=feature_flags.FeatureDisabled,
        )
        with mock.patch.object(
            self.module, "validate_payload",
            side_effect=feature_flags.FeatureDisabled("模板成片服务暂不可用"),
        ):
            cli_gateway.handle_quote(
                handler, "/api/gen/cli/quote", lambda _token: {"username": "alice"},
                lambda _user: False, lambda: False, flags, points,
                SimpleNamespace(), SimpleNamespace(), "secret",
            )
        self.assertEqual((503, "feature_disabled", 5000), (
            handler.result[0], handler.result[1]["code"],
            handler.result[1]["retry_after_ms"],
        ))
        points.cost_of.assert_not_called()


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
        self.assertIn('hq-content[data-active="matrix-template"]{height:auto!important', page)
        self.assertIn("function fitLiveText(node,max,min)", page)
        self.assertIn("node.scrollHeight>node.clientHeight", page)
        self.assertIn("fitLiveText(el('liveTop'),topSizes[activeTemplate]||34,12)", page)
        self.assertIn("fitLiveText(el('liveBottom'),bottomSizes[activeTemplate]||20,12)", page)
        self.assertIn("'full-overlay-bold':['#11151c'", page)
        self.assertIn("'poster-split':['#70577c'", page)
        self.assertIn('data-template="\'+esc(item.id)+\'"', page)
        self.assertIn('.mt-live[data-template="full-overlay-bold"]', page)
        self.assertIn('.mt-live[data-template="poster-split"]', page)
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

    def test_live_preview_tracks_copy_and_selected_template(self):
        result = self.runtime("livePreview")
        self.assertEqual("实时标题", result["top"])
        self.assertEqual("实时行动文案", result["bottom"])
        self.assertEqual("minimal-headline", result["template"])
        self.assertIn("--live-bg:#f5f5f2", result["style"])
        self.assertEqual("none", result["videoDisplay"])

    def test_conversion_layout_selection_reaches_generation_payload(self):
        result = self.runtime("conversionLayouts")
        self.assertEqual(
            ["full-overlay-bold", "poster-split"],
            [item["template"] for item in result],
        )
        for item in result:
            self.assertEqual(item["template"], item["cardTemplate"])
            self.assertEqual(item["template"], item["submitted"])


if __name__ == "__main__":
    unittest.main()
