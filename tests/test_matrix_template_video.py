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
        self.module._CACHE.update({"at": 0.0, "templates": [], "fonts": []})

    def templates(self):
        templates = [{
            "id": "native-bold" if index == 0 else f"template-{index:02d}",
            "name": f"模板 {index}", "description": "说明", "tags": ["标签"],
        } for index in range(15)]
        templates[-2]["id"] = "full-overlay-bold"
        templates[-1]["id"] = "poster-split"
        return templates

    def reference_templates(self):
        return [
            {
                "id": "full-overlay-bold", "name": "沉浸强标题",
                "engine": "ffmpeg", "font_mode": "selectable",
                "font_selectable": True,
            },
            {
                "id": "poster-split", "name": "海报切分",
                "engine": "ffmpeg", "font_mode": "selectable",
                "font_selectable": True,
            },
        ] + [{
            "id": f"ref-{index:02d}-fixture-{index:02d}",
            "name": f"参考模板 {index}",
            "description": "固定字体模板",
            "tags": ["HyperFrames", "内置字体"],
            "engine": "hyperframes",
            "font_mode": "template_locked",
            "font_selectable": False,
            "variant": f"v{index:02d}",
        } for index in range(1, 18)]

    def test_public_catalog_accepts_transition_counts_but_exposes_only_approved_templates(self):
        response = {"templates": self.templates(), "fonts": [
            {"value": "", "label": "自动搭配", "source": "automatic"},
            {"value": "Noto Sans SC", "label": "思源黑体", "source": "bundled"},
            {"value": "AaHouDiHei", "label": "Aa厚底黑", "source": "private"},
            {"value": "../bad", "label": "非法", "source": "private"},
        ]}
        with mock.patch.object(self.module, "_request", return_value=response):
            values = self.module.public_templates(force=True)
        self.assertEqual(
            ["full-overlay-bold", "poster-split"],
            [item["id"] for item in values],
        )
        self.assertEqual(
            ["", "Noto Sans SC", "AaHouDiHei"],
            [item["value"] for item in self.module.public_fonts()],
        )
        with mock.patch.object(
            self.module, "_request", return_value={"templates": self.templates()[:-2]}
        ), \
             self.assertRaisesRegex(RuntimeError, "不完整"):
            self.module.public_templates(force=True)
        missing_required = self.templates()
        missing_required[-1] = {
            "id": "replacement-template", "name": "替代模板",
            "description": "说明", "tags": ["标签"],
        }
        with mock.patch.object(
            self.module, "_request", return_value={"templates": missing_required}
        ), self.assertRaisesRegex(RuntimeError, "不完整"):
            self.module.public_templates(force=True)

        with mock.patch.object(self.module, "_request", return_value={
            "templates": [
                {"id": "full-overlay-bold", "name": "沉浸强标题"},
                {"id": "poster-split", "name": "海报切分"},
            ],
        }):
            restricted = self.module.public_templates(force=True)
        self.assertEqual(
            ["full-overlay-bold", "poster-split"],
            [item["id"] for item in restricted],
        )

        with mock.patch.object(self.module, "_request", return_value={
            "templates": self.reference_templates(),
        }):
            expanded = self.module.public_templates(force=True)
        self.assertEqual(19, len(expanded))
        self.assertEqual(17, len([
            item for item in expanded if item["engine"] == "hyperframes"
        ]))
        self.assertTrue(all(
            item["font_selectable"] is False
            for item in expanded if item["engine"] == "hyperframes"
        ))
        self.assertEqual(
            {f"v{index:02d}" for index in range(1, 18)},
            {item["variant"] for item in expanded if item["engine"] == "hyperframes"},
        )

    def test_availability_accepts_two_fifteen_or_nineteen_healthy_templates(self):
        for count in (2, 15, 19):
            with self.subTest(count=count), \
                 mock.patch.object(self.module.feature_flags, "is_enabled", return_value=True), \
                 mock.patch.object(
                     self.module, "_request",
                     return_value={"ok": True, "templates": count},
                 ):
                self.assertEqual({
                    "enabled": True, "ready": True, "available": True,
                }, self.module.availability(force=True))
        for health in ({"ok": True, "templates": 13}, {"ok": False, "templates": 2}):
            with self.subTest(health=health), \
                 mock.patch.object(self.module.feature_flags, "is_enabled", return_value=True), \
                 mock.patch.object(self.module, "_request", return_value=health):
                self.assertFalse(self.module.availability(force=True)["ready"])

    def test_transition_catalog_rejects_unapproved_template_submission(self):
        with mock.patch.object(
            self.module, "_request", return_value={"templates": self.templates()}
        ):
            self.module.public_templates(force=True)
        with mock.patch.object(self.module, "require_available"), \
             self.assertRaisesRegex(ValueError, "请选择有效模板"):
            self.module.validate_payload({
                "top_text": "AI 工作流",
                "bottom_text": "评论区留下关键词",
                "template_id": "native-bold",
            })

    def test_reference_template_ignores_font_selection(self):
        expected = {
            "top_text": "活动标题", "bottom_text": "评论区回复关键词",
            "template_id": "ref-01-fixture-01", "bgm": True,
            "duration": 8.0,
        }
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(
                 self.module, "public_templates",
                 return_value=self.reference_templates(),
             ), \
             mock.patch.object(
                 self.module, "_request", return_value={"payload": expected}
             ) as request:
            result = self.module.validate_payload({
                **expected,
                "font_family": "AaHouDiHei",
                "duration": None,
            }, "alice")
        self.assertNotIn("font_family", result)
        self.assertNotIn("font_family", request.call_args.args[2])
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(
                 self.module, "public_templates",
                 return_value=self.reference_templates(),
             ), \
             self.assertRaisesRegex(ValueError, "暂仅支持单条"):
            self.module.validate_payload({
                **expected,
                "batch_id": "a" * 32,
                "batch_index": 1,
                "batch_size": 2,
            }, "alice")

    def test_missing_template_defaults_to_first_approved_layout(self):
        approved = [
            {"id": "full-overlay-bold", "name": "沉浸强标题"},
            {"id": "poster-split", "name": "海报切分"},
        ]
        expected = {
            "top_text": "AI 工作流",
            "bottom_text": "评论区留下关键词",
            "template_id": "full-overlay-bold",
            "bgm": True,
            "duration": 8.0,
        }
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(self.module, "public_templates", return_value=approved), \
             mock.patch.object(self.module, "_request", return_value={"payload": expected}):
            result = self.module.validate_payload({
                "top_text": "AI 工作流",
                "bottom_text": "评论区留下关键词",
            })
        self.assertEqual("full-overlay-bold", result["template_id"])

    def test_matrix_jobs_use_dedicated_five_worker_queue(self):
        from content_domains import core
        self.assertIs(
            core._pick_job_queue("matrix_template_video"),
            core._matrix_job_queue,
        )
        self.assertEqual(5, core.MATRIX_JOB_WORKERS)
        self.assertGreaterEqual(core.MAX_USER_ACTIVE_JOBS, 5)

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

    def test_validate_payload_accepts_only_current_catalog_font(self):
        fonts = [
            {"value": "", "label": "自动搭配", "source": "automatic"},
            {"value": "AaHouDiHei", "label": "Aa厚底黑", "source": "private"},
        ]
        expected = {
            "top_text": "指定字体标题", "bottom_text": "指定字体行动文案",
            "template_id": "native-bold", "font_family": "AaHouDiHei",
            "bgm": True, "duration": 8.0,
        }
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(self.module, "public_templates", return_value=self.templates()), \
             mock.patch.object(self.module, "public_fonts", return_value=fonts), \
             mock.patch.object(self.module, "_request", return_value={"payload": expected}):
            result = self.module.validate_payload(dict(expected, duration=None), "alice")
            self.assertEqual("AaHouDiHei", result["font_family"])
            with self.assertRaisesRegex(ValueError, "当前可用字体"):
                self.module.validate_payload(dict(expected, font_family="Missing Font"), "alice")

    def test_validate_payload_forwards_batch_identity(self):
        expected = {
            "top_text": "批量标题", "bottom_text": "批量行动文案",
            "template_id": "native-bold", "bgm": True, "duration": 8.0,
            "batch_id": "a" * 32, "batch_index": 2, "batch_size": 5,
        }
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(self.module, "public_templates", return_value=self.templates()), \
             mock.patch.object(self.module, "_request", return_value={"payload": expected}):
            result = self.module.validate_payload(dict(expected, duration=None), "alice")
        self.assertEqual(("a" * 32, 2, 5), (
            result["batch_id"], result["batch_index"], result["batch_size"],
        ))
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(self.module, "public_templates", return_value=self.templates()), \
             self.assertRaisesRegex(ValueError, "批量任务参数"):
            self.module.validate_payload(dict(expected, batch_index=6), "alice")

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
        self.assertIn('id="fontFamily"', page)
        self.assertIn('id="batchCount"', page)
        self.assertIn("Math.min(5", page)
        self.assertIn("body.font_family=selectedFont", page)
        self.assertNotIn("素材来源", page)
        self.assertIn("template_id:activeTemplate,bgm:true", page)
        self.assertIn('hq-content[data-active="matrix-template"]{height:auto!important', page)
        self.assertIn("function fitLiveText(node,max,min)", page)
        self.assertIn("var referencePreviews=", page)
        self.assertIn("data-variant", page)
        self.assertIn("item.description", page)
        self.assertIn("node.scrollHeight>node.clientHeight", page)
        self.assertIn("fitLiveText(el('liveTop'),topSizes[activeTemplate]||34,12)", page)
        self.assertIn("fitLiveText(el('liveBottom'),20,12)", page)
        self.assertLess(shell.index("k:'text-video'"), shell.index("k:'matrix-template'"))
        self.assertIn("/api/gen/matrix-template/capability", shell)

    def test_layout_browser_regression_covers_all_reference_templates(self):
        source = (ROOT / "tests/matrix_template_layout_browser.js").read_text(
            encoding="utf-8"
        )
        self.assertEqual(17, len(re.findall(r"'ref-[0-9]{2}-[a-z0-9-]+'", source)))
        self.assertIn("cardCount !== 19", source)
        self.assertIn("referenceCount !== 17", source)
        self.assertIn("distinctReferencePreviews !== 17", source)

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

    def test_completed_single_result_loads_into_right_player_immediately(self):
        result = self.runtime("instantResult")
        self.assertEqual("/instant-video", result["src"])
        self.assertEqual("block", result["display"])
        self.assertEqual("none", result["live"])
        self.assertEqual("/instant-video", result["download"])
        self.assertEqual("metadata", result["preload"])
        self.assertEqual(1, result["loads"])
        self.assertEqual(1, result["pauses"])
        self.assertTrue(result["cleared"])

    def test_done_without_video_url_keeps_polling_until_result_is_ready(self):
        result = self.runtime("delayedResultUrl")
        self.assertEqual(1, result["before"]["polls"])
        self.assertEqual(0, result["before"]["loads"])
        self.assertFalse(result["before"]["cleared"])
        self.assertIn("生成中", result["before"]["status"])
        self.assertEqual(2, result["polls"])
        self.assertEqual("/delayed-video", result["src"])
        self.assertEqual("block", result["display"])
        self.assertEqual(1, result["loads"])
        self.assertTrue(result["cleared"])

    def test_live_preview_tracks_copy_and_selected_template(self):
        result = self.runtime("livePreview")
        self.assertEqual("实时标题", result["top"])
        self.assertEqual("实时行动文案", result["bottom"])
        self.assertEqual("minimal-headline", result["template"])
        self.assertIn("--live-bg:#f5f5f2", result["style"])
        self.assertEqual("none", result["videoDisplay"])

    def test_font_selector_lists_available_fonts_and_submits_parameter(self):
        result = self.runtime("fontSelect")
        self.assertEqual("AaHouDiHei", result["body"]["font_family"])
        self.assertEqual("私有字体", result["source"])
        self.assertEqual(["", "Noto Sans SC", "AaHouDiHei"], result["options"])

    def test_locked_reference_template_disables_and_omits_font(self):
        result = self.runtime("lockedFont")
        self.assertTrue(result["disabled"])
        self.assertEqual("", result["value"])
        self.assertEqual("模板内置", result["source"])
        self.assertNotIn("font_family", result["body"])
        self.assertEqual("ref-01-fixture-01", result["body"]["template_id"])
        self.assertTrue(result["batchDisabled"])
        self.assertEqual("1", result["batchValue"])
        self.assertEqual("HyperFrames模板暂仅支持单条", result["batchHint"])

    def test_batch_five_submits_distinct_jobs_and_renders_all_results(self):
        result = self.runtime("batchFive")
        self.assertEqual(5, result["posts"])
        self.assertEqual(5, result["polls"])
        self.assertEqual(5, len(set(result["keys"])))
        self.assertTrue(all(body["bgm"] is True for body in result["bodies"]))
        self.assertEqual(1, len({body["batch_id"] for body in result["bodies"]}))
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{32}", body["batch_id"])
                            for body in result["bodies"]))
        self.assertEqual([1, 2, 3, 4, 5], [body["batch_index"] for body in result["bodies"]])
        self.assertTrue(all(body["batch_size"] == 5 for body in result["bodies"]))
        self.assertEqual(5, result["cards"])
        self.assertTrue(result["cleared"])

    def test_legacy_single_pending_state_is_recovered_after_upgrade(self):
        result = self.runtime("legacyPending")
        self.assertEqual(0, result["posts"])
        self.assertEqual(1, result["polls"])
        self.assertTrue(result["cleared"])

    def test_failed_batch_item_is_visible_and_never_reposted_after_reload(self):
        result = self.runtime("mixedFailureReload")
        self.assertEqual(5, result["beforePosts"])
        self.assertEqual(0, result["afterPosts"])
        self.assertEqual(0, result["afterPolls"])
        self.assertEqual((5, 5), (result["beforeCards"], result["afterCards"]))
        self.assertEqual(4, result["videos"])
        self.assertEqual("任务队列已满", result["error"])
        self.assertEqual("未受理/未扣点", result["refund"])
        self.assertEqual(1, result["failedKeyAttempts"])
        self.assertTrue(result["pendingCleared"])

    def test_failed_remote_job_shows_confirmed_refund(self):
        result = self.runtime("jobFailureRefund")
        self.assertEqual(1, result["cards"])
        self.assertEqual("渲染失败", result["error"])
        self.assertEqual("已退款", result["refund"])

    def test_refund_pending_keeps_polling_until_confirmed(self):
        result = self.runtime("refundPendingThenConfirmed")
        self.assertEqual(2, result["polls"])
        self.assertEqual("退款处理中", result["before"])
        self.assertEqual("已退款", result["after"])
        self.assertEqual("第 1 条生成失败", result["title"])
        self.assertEqual(1, result["cards"])
        self.assertTrue(result["cleared"])


if __name__ == "__main__":
    unittest.main()
