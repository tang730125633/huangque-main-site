from __future__ import annotations

import concurrent.futures
import http.server
import importlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
PRODUCTION_LEGACY_SEMANTIC_CONTRACTS = {
    "v02": {
        "version": 1,
        "max_width_px": 996,
        "layers": {
            "top1": {"font_size_px": 86, "max_lines": 2},
            "top2": {"font_size_px": 62, "max_lines": 4},
            "bottom2": {"font_size_px": 78, "max_lines": 2},
        },
    },
    "v05": {
        "version": 1,
        "max_width_px": 996,
        "layers": {
            "top1": {"font_size_px": 102, "max_lines": 2},
            "top2": {"font_size_px": 104, "max_lines": 2},
            "top3": {"font_size_px": 68, "max_lines": 3},
            "bottom2": {"font_size_px": 70, "max_lines": 2},
        },
    },
}


class MatrixTemplateVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(SERVER) not in sys.path:
            sys.path.insert(0, str(SERVER))
        cls.module = importlib.import_module("content_domains.matrix_template_video")

    def setUp(self):
        self.module._CACHE.update({
            "at": 0.0,
            "templates": [],
            "fonts": [],
            "max_batch_size": 1,
            "engine_concurrency": {"ffmpeg": 1, "hyperframes": 1},
        })

    def templates(self):
        templates = [{
            "id": "native-bold" if index == 0 else f"template-{index:02d}",
            "name": f"模板 {index}", "description": "说明", "tags": ["标签"],
        } for index in range(15)]
        templates[-2]["id"] = "full-overlay-bold"
        templates[-1]["id"] = "poster-split"
        return templates

    def reference_templates(self, semantic_variants=None):
        if semantic_variants is None:
            semantic_variants = tuple(sorted(self.module._ALL_REFERENCE_VARIANTS))
        legacy_contract = set(semantic_variants) in (
            {"v02"}, {"v02", "v05"},
        )
        values = [
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
        for item in values:
            variant = item.get("variant")
            if variant in semantic_variants:
                if legacy_contract:
                    item["semantic_layout"] = json.loads(json.dumps(
                        PRODUCTION_LEGACY_SEMANTIC_CONTRACTS[variant]
                    ))
                else:
                    item["semantic_layout"] = {
                        "version": 1,
                        "max_width_px": 996,
                        "layers": {
                            layer: {
                                "font_size_px": values[0],
                                "font_weight": values[1],
                                "max_width_px": values[2],
                                "max_lines": values[3],
                            }
                            for layer, values in self.module._SEMANTIC_CONTRACTS[
                                variant
                            ].items()
                        },
                    }
        return values

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
            "max_batch_size": 5,
            "hyperframes_concurrency": 2,
            "engine_concurrency": {"ffmpeg": 5, "hyperframes": 2},
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
        v10 = next(item for item in expanded if item.get("variant") == "v10")
        self.assertEqual(
            (85, 65),
            tuple(
                v10["semantic_layout"]["layers"][layer]["font_size_px"]
                for layer in ("top1", "top3")
            ),
        )
        self.assertEqual(
            {"font_size_px": 80, "font_weight": 400,
             "max_width_px": 970, "max_lines": 2},
            v10["semantic_layout"]["layers"]["bottom2"],
        )
        v05 = next(item for item in expanded if item.get("variant") == "v05")
        self.assertEqual(
            {"font_size_px": 68, "font_weight": 900,
             "max_width_px": 996, "max_lines": 2},
            v05["semantic_layout"]["layers"]["top3"],
        )
        v04 = next(item for item in expanded if item.get("variant") == "v04")
        self.assertEqual(
            {"font_size_px": 80, "font_weight": 900,
             "max_width_px": 996, "max_lines": 2},
            v04["semantic_layout"]["layers"]["bottom2"],
        )
        transitional_templates = self.reference_templates()
        next(
            item for item in transitional_templates
            if item.get("variant") == "v04"
        )["semantic_layout"]["layers"]["bottom2"]["font_size_px"] = 60
        with mock.patch.object(self.module, "_request", return_value={
            "templates": transitional_templates,
            "max_batch_size": 5,
            "engine_concurrency": {"ffmpeg": 5, "hyperframes": 2},
        }):
            transitional = self.module.public_templates(force=True)
        self.assertEqual(
            60,
            next(
                item for item in transitional
                if item.get("variant") == "v04"
            )["semantic_layout"]["layers"]["bottom2"]["font_size_px"],
        )
        v09 = next(item for item in expanded if item.get("variant") == "v09")
        self.assertEqual(
            {"font_size_px": 88, "font_weight": 400,
             "max_width_px": 996, "max_lines": 2},
            v09["semantic_layout"]["layers"]["top1"],
        )
        v12 = next(item for item in expanded if item.get("variant") == "v12")
        self.assertEqual(
            (80, 70),
            tuple(
                v12["semantic_layout"]["layers"][layer]["font_size_px"]
                for layer in ("top1", "top3")
            ),
        )
        v16 = next(item for item in expanded if item.get("variant") == "v16")
        self.assertEqual(
            80, v16["semantic_layout"]["layers"]["top1"]["font_size_px"],
        )
        self.assertEqual(
            {f"v{index:02d}" for index in range(1, 18)},
            {item["variant"] for item in expanded if item["engine"] == "hyperframes"},
        )
        self.assertEqual(
            [f"v{index:02d}" for index in range(1, 18)],
            [item["variant"] for item in expanded if item.get("semantic_layout")],
        )
        self.assertEqual({
            "max_batch_size": 5,
            "engine_concurrency": {"ffmpeg": 5, "hyperframes": 2},
        }, self.module.public_batch_capability())

        for partial in (("v02",), ("v02", "v05")):
            with self.subTest(partial=partial), mock.patch.object(
                self.module, "_request", return_value={
                    "templates": self.reference_templates(partial),
                    "max_batch_size": 5,
                    "engine_concurrency": {"ffmpeg": 5, "hyperframes": 2},
                },
            ), self.assertRaisesRegex(RuntimeError, "语义排版|不完整"):
                self.module.public_templates(force=True)

    def test_reference_catalog_rejects_missing_v02_unknown_variant_and_drift(self):
        invalid_cases = []
        invalid_cases.append(self.reference_templates(("v05",)))

        invalid_cases.append(self.reference_templates(("v02", "v06")))

        unknown = self.reference_templates()
        next(item for item in unknown if item.get("variant") == "v17")[
            "variant"
        ] = "v18"
        invalid_cases.append(unknown)

        drift = self.reference_templates()
        next(item for item in drift if item.get("variant") == "v05")[
            "semantic_layout"
        ]["layers"]["top3"]["font_size_px"] = 69
        invalid_cases.append(drift)

        weight_drift = self.reference_templates()
        next(item for item in weight_drift if item.get("variant") == "v05")[
            "semantic_layout"
        ]["layers"]["top2"]["font_weight"] = 800
        invalid_cases.append(weight_drift)

        width_drift = self.reference_templates()
        next(item for item in width_drift if item.get("variant") == "v10")[
            "semantic_layout"
        ]["layers"]["bottom2"]["max_width_px"] = 996
        invalid_cases.append(width_drift)

        v04_size_drift = self.reference_templates()
        next(
            item for item in v04_size_drift
            if item.get("variant") == "v04"
        )["semantic_layout"]["layers"]["bottom2"]["font_size_px"] = 59
        invalid_cases.append(v04_size_drift)

        mixed_contract = self.reference_templates()
        mixed_v02 = next(
            item for item in mixed_contract if item.get("variant") == "v02"
        )["semantic_layout"]["layers"]
        for layer in mixed_v02.values():
            layer.pop("font_weight")
            layer.pop("max_width_px")
        invalid_cases.append(mixed_contract)

        legacy_v05_drift = self.reference_templates(("v02", "v05"))
        next(
            item for item in legacy_v05_drift if item.get("variant") == "v05"
        )["semantic_layout"]["layers"]["top3"]["max_lines"] = 2
        invalid_cases.append(legacy_v05_drift)

        for templates in invalid_cases:
            with self.subTest(templates=templates), mock.patch.object(
                self.module, "_request", return_value={
                    "templates": templates,
                    "max_batch_size": 5,
                    "engine_concurrency": {"ffmpeg": 5, "hyperframes": 2},
                },
            ), self.assertRaisesRegex(RuntimeError, "语义排版|不完整"):
                self.module.public_templates(force=True)

    def test_v09_top1_transition_accepts_only_78_or_88_px(self):
        for font_size in (78, 88):
            templates = self.reference_templates()
            next(
                item for item in templates if item.get("variant") == "v09"
            )["semantic_layout"]["layers"]["top1"]["font_size_px"] = font_size
            with self.subTest(font_size=font_size), mock.patch.object(
                self.module, "_request", return_value={
                    "templates": templates,
                    "max_batch_size": 5,
                    "engine_concurrency": {"ffmpeg": 5, "hyperframes": 2},
                },
            ):
                values = self.module.public_templates(force=True)
                self.assertEqual(
                    font_size,
                    next(
                        item for item in values
                        if item.get("variant") == "v09"
                    )["semantic_layout"]["layers"]["top1"]["font_size_px"],
                )

        drift = self.reference_templates()
        next(
            item for item in drift if item.get("variant") == "v09"
        )["semantic_layout"]["layers"]["top1"]["font_size_px"] = 79
        with mock.patch.object(self.module, "_request", return_value={
            "templates": drift,
            "max_batch_size": 5,
            "engine_concurrency": {"ffmpeg": 5, "hyperframes": 2},
        }), self.assertRaisesRegex(RuntimeError, "语义排版能力无效"):
            self.module.public_templates(force=True)

    def test_v12_v16_typography_transition_accepts_only_old_or_new_sizes(self):
        transitions = {
            ("v12", "top1"): (72, 80),
            ("v12", "top3"): (50, 70),
            ("v16", "top1"): (48, 80),
        }
        legacy = self.reference_templates()
        for (variant, layer), (old_size, _new_size) in transitions.items():
            next(
                item for item in legacy if item.get("variant") == variant
            )["semantic_layout"]["layers"][layer]["font_size_px"] = old_size
        with mock.patch.object(self.module, "_request", return_value={
            "templates": legacy,
            "max_batch_size": 5,
            "engine_concurrency": {"ffmpeg": 5, "hyperframes": 2},
        }):
            values = self.module.public_templates(force=True)
        by_variant = {item.get("variant"): item for item in values}
        for (variant, layer), (old_size, _new_size) in transitions.items():
            self.assertEqual(
                old_size,
                by_variant[variant]["semantic_layout"]["layers"][layer][
                    "font_size_px"
                ],
            )

        for (variant, layer), (old_size, new_size) in transitions.items():
            drift = self.reference_templates()
            invalid_size = min(old_size, new_size) + 1
            next(
                item for item in drift if item.get("variant") == variant
            )["semantic_layout"]["layers"][layer][
                "font_size_px"
            ] = invalid_size
            with self.subTest(variant=variant, layer=layer), mock.patch.object(
                self.module, "_request", return_value={
                    "templates": drift,
                    "max_batch_size": 5,
                    "engine_concurrency": {"ffmpeg": 5, "hyperframes": 2},
                },
            ), self.assertRaisesRegex(RuntimeError, "语义排版能力无效"):
                self.module.public_templates(force=True)

    def test_v10_typography_transition_accepts_only_old_or_new_sizes(self):
        transitions = {"top1": (70, 85), "top3": (54, 65)}
        for layer, accepted_sizes in transitions.items():
            for font_size in accepted_sizes:
                templates = self.reference_templates()
                next(
                    item for item in templates if item.get("variant") == "v10"
                )["semantic_layout"]["layers"][layer][
                    "font_size_px"
                ] = font_size
                with self.subTest(
                    layer=layer, font_size=font_size,
                ), mock.patch.object(self.module, "_request", return_value={
                    "templates": templates,
                    "max_batch_size": 5,
                    "engine_concurrency": {"ffmpeg": 5, "hyperframes": 2},
                }):
                    values = self.module.public_templates(force=True)
                    current = next(
                        item for item in values if item.get("variant") == "v10"
                    )
                    self.assertEqual(
                        font_size,
                        current["semantic_layout"]["layers"][layer][
                            "font_size_px"
                        ],
                    )

            drift = self.reference_templates()
            next(
                item for item in drift if item.get("variant") == "v10"
            )["semantic_layout"]["layers"][layer]["font_size_px"] = (
                min(accepted_sizes) + 1
            )
            with self.subTest(layer=layer, drift=True), mock.patch.object(
                self.module, "_request", return_value={
                    "templates": drift,
                    "max_batch_size": 5,
                    "engine_concurrency": {"ffmpeg": 5, "hyperframes": 2},
                },
            ), self.assertRaisesRegex(RuntimeError, "语义排版能力无效"):
                self.module.public_templates(force=True)

    def test_legacy_partial_semantic_catalog_is_rejected(self):
        templates = self.reference_templates(("v02", "v05"))
        with mock.patch.object(self.module, "_request", return_value={
            "templates": templates,
            "max_batch_size": 5,
            "engine_concurrency": {"ffmpeg": 5, "hyperframes": 2},
        }), self.assertRaisesRegex(RuntimeError, "语义排版|不完整"):
            self.module.public_templates(force=True)

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
        semantic = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": self.module.matrix_template_semantics._source_sha256(
                "活动标题", "评论区回复关键词",
            ),
            "top1_end": 3, "top_break_after": [],
            "bottom_break_after": [],
        }
        expected = {
            "top_text": "活动标题", "bottom_text": "评论区回复关键词",
            "template_id": "ref-01-fixture-01", "bgm": True,
            "duration": 8.0, "semantic_layout": semantic,
        }

        def resolve(_top, _bottom, _template_id, _contract, validator):
            layout = dict(semantic)
            accepted, response = validator(layout)
            self.assertTrue(accepted)
            return layout, response

        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(
                 self.module, "public_templates",
                 return_value=self.reference_templates(),
             ), \
             mock.patch.object(
                 self.module.matrix_template_semantics, "resolve",
                 side_effect=resolve,
             ), mock.patch.object(
                 self.module, "_request", return_value={"payload": expected},
             ) as request:
            result = self.module.validate_payload({
                **expected,
                "font_family": "AaHouDiHei",
                "duration": None,
            }, "alice")
        self.assertNotIn("font_family", result)
        self.assertNotIn("font_family", request.call_args.args[2])
        batch_expected = {
            **expected,
            "batch_id": "a" * 32,
            "batch_index": 2,
            "batch_size": 5,
        }
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(
                 self.module, "public_templates",
                 return_value=self.reference_templates(),
             ), \
             mock.patch.object(
                 self.module.matrix_template_semantics, "resolve",
                 side_effect=resolve,
             ), mock.patch.object(
                 self.module, "_request",
                 return_value={"payload": batch_expected},
             ):
            batch = self.module.validate_payload(batch_expected, "alice")
        self.assertEqual(("a" * 32, 2, 5), (
            batch["batch_id"], batch["batch_index"], batch["batch_size"],
        ))

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

    def test_absolute_expiry_covers_pending_and_running_without_queue_change(self):
        from content_domains import core

        rows = [
            {"id": 1, "username": "alice", "cost": 5},
            {"id": 2, "username": "bob", "cost": 5},
        ]

        class Connection:
            def execute(self, sql, params):
                self.sql = sql
                self.params = params
                return self

            def fetchall(self):
                return rows

            def close(self):
                return None

        connection = Connection()
        with mock.patch.object(core, "jdb", return_value=connection), \
             mock.patch.object(
                 core, "_fail_job_and_schedule_refund",
                 side_effect=[True, False],
             ) as fail, mock.patch.object(
                 core, "_mark_video_asset_failed",
             ) as mark, mock.patch.object(self.module, "TOTAL_TIMEOUT", 1200):
            expired = core._expire_matrix_template_jobs(now=5000)
        self.assertEqual(1, expired)
        self.assertIn("status IN ('pending','running')", connection.sql)
        self.assertEqual(3800, connection.params[1])
        self.assertEqual(("pending", "running"), fail.call_args_list[0].kwargs["from_states"])
        self.assertEqual("matrix_template_video", fail.call_args_list[0].kwargs["kind"])
        mark.assert_called_once_with(1, "matrix_template_video", "模板成片超过总时限")

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

    def test_validate_payload_uses_owned_voice_and_forces_bgm_off(self):
        from content_domains import audio

        expected_provider = {
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
            "template_id": "native-bold", "bgm": False, "duration": 8.0,
        }
        expected_request = dict(expected_provider, duration=None)
        normalized_voiceover = {
            "text": "这是一段口播文案", "voice": "vip_alice",
            "speed": 1.0, "pitch": 0, "volume": 0,
            "delivery": "natural", "voice_scope": "personal",
            "provider": "cosyvoice",
        }
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(
                 self.module, "public_templates", return_value=self.templates(),
             ), mock.patch.object(
                 self.module.feature_flags, "require_enabled",
             ), mock.patch.object(
                 audio.cosyvoice, "enabled", return_value=True,
             ), mock.patch.object(
                 audio, "validate_audio_payload", return_value=normalized_voiceover,
             ) as validate_audio, mock.patch.object(
                 audio, "require_owned_ready_personal_voice",
             ) as require_personal, mock.patch.object(
                 audio, "resolve_audio_provider_voice",
                 return_value="cosyvoice-v3.5-plus-alice",
             ), mock.patch.object(
                 self.module, "_request", return_value={"payload": expected_provider},
             ) as request:
            payload = self.module.validate_payload({
                "top_text": "AI 工作流",
                "bottom_text": "评论区留下关键词",
                "template_id": "native-bold", "bgm": True,
                "voiceover": {
                    "text": "这是一段口播文案", "voice": "vip_alice",
                    "voice_scope": "personal",
                    "speed": 1.0, "pitch": 0, "volume": 0,
                    "delivery": "natural",
                },
            }, "alice")

        validate_audio.assert_called_once()
        self.assertEqual("alice", validate_audio.call_args.args[1])
        require_personal.assert_called_once_with("alice", "vip_alice")
        self.assertEqual(expected_request, request.call_args.args[2])
        self.assertFalse(payload["bgm"])
        self.assertEqual("vip_alice", payload["voiceover"]["voice"])
        self.assertEqual("personal", payload["voiceover"]["voice_scope"])
        self.assertEqual(64, len(payload["voiceover"]["voice_version"]))
        self.assertNotIn("provider", payload["voiceover"])

    def test_voiceover_requires_structured_owned_voice_settings(self):
        for value in (True, "vip_alice", []):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "配音设置无效",
            ):
                self.module._normalize_voiceover(value, "alice")
        with self.assertRaisesRegex(ValueError, "无效字段"):
            self.module._normalize_voiceover(
                {"text": "口播", "voice": "vip_alice", "provider_voice": "secret"},
                "alice",
            )
        with self.assertRaisesRegex(ValueError, "音色归属"):
            self.module._normalize_voiceover(
                {"text": "口播", "voice": "vip_alice"}, "",
            )
        from content_domains import audio
        with mock.patch.object(
            self.module.feature_flags, "require_enabled",
        ), mock.patch.object(
            audio.cosyvoice, "enabled", return_value=True,
        ), mock.patch.object(audio, "validate_audio_payload", return_value={
            "text": "口播", "voice": "vip_alice", "speed": 1.0,
            "pitch": 0, "volume": 0, "delivery": "natural",
            "voice_scope": "personal",
        }), mock.patch.object(
            audio, "require_owned_ready_personal_voice",
        ), mock.patch.object(
            audio, "resolve_audio_provider_voice",
            return_value="cosyvoice-v3.5-plus-alice",
        ), self.assertRaisesRegex(ValueError, "归属已变化"):
            self.module._normalize_voiceover({
                "text": "口播", "voice": "vip_alice",
                "voice_scope": "public",
            }, "alice")
        with mock.patch.object(
            self.module.feature_flags, "require_enabled",
        ), mock.patch.object(
            audio.cosyvoice, "enabled", return_value=True,
        ), mock.patch.object(audio, "validate_audio_payload", return_value={
            "text": "口播", "voice": "vip_alice", "speed": 1.0,
            "pitch": 0, "volume": 0, "delivery": "natural",
            "voice_scope": "personal",
        }), mock.patch.object(
            audio, "require_owned_ready_personal_voice",
        ), mock.patch.object(
            audio, "resolve_audio_provider_voice",
            return_value="cosyvoice-v3.5-plus-new",
        ), self.assertRaisesRegex(ValueError, "版本已变化"):
            self.module._normalize_voiceover({
                "text": "口播", "voice": "vip_alice",
                "voice_scope": "personal", "voice_version": "0" * 64,
            }, "alice")
        with mock.patch.object(
            self.module.feature_flags, "require_enabled",
            side_effect=self.module.feature_flags.FeatureDisabled("配音已关闭"),
        ), self.assertRaises(self.module.feature_flags.FeatureDisabled):
            self.module._normalize_voiceover(
                {"text": "口播", "voice": "vip_alice"}, "alice",
            )

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

    def test_v02_semantic_layout_repairs_against_generation_preflight(self):
        template = next(
            item for item in self.reference_templates()
            if item.get("variant") == "v02"
        )
        first = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": "a" * 64, "top1_end": 1,
            "top_break_after": [1], "bottom_break_after": [],
        }
        repaired = dict(first, top1_end=5, top_break_after=[5])
        requests = []

        def preflight(_method, _path, body, **_kwargs):
            requests.append(dict(body))
            if len(requests) == 1:
                raise self.module.MatrixTemplateHTTPError(
                    400, "HyperFrames 顶部语义断点拆开了完整词组",
                )
            return {"payload": dict(body, duration=11)}

        def resolve(_top, _bottom, _template_id, _contract, validator):
            accepted, feedback = validator(first)
            self.assertFalse(accepted)
            self.assertIn("语义断点", feedback)
            accepted, response = validator(repaired)
            self.assertTrue(accepted)
            return repaired, response

        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(self.module, "public_templates", return_value=[template]), \
             mock.patch.object(
                 self.module.matrix_template_semantics, "resolve",
                 side_effect=resolve,
             ) as resolve_call, \
             mock.patch.object(self.module, "_request", side_effect=preflight):
            result = self.module.validate_payload({
                "top_text": "团队8个人，每天产出100条短视频",
                "bottom_text": "评论区扣888",
                "template_id": template["id"],
                "bgm": False,
            })
        self.assertEqual(repaired, result["semantic_layout"])
        self.assertEqual((first, repaired), (
            requests[0]["semantic_layout"], requests[1]["semantic_layout"],
        ))
        resolve_call.assert_called_once()

    def test_v05_long_health_copy_uses_stronger_semantic_repair(self):
        template = next(
            item for item in self.reference_templates()
            if item.get("variant") == "v05"
        )
        top = (
            "我是大鹏 陕西西安人在广州有个健康赛道创业圈子"
            "资源共享|大健康|AI矩阵社交破圈|一人公司"
        )
        bottom = "PL区扣888"
        source_hash = self.module.matrix_template_semantics._source_sha256(
            top, bottom,
        )
        first = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": source_hash, "top1_end": 4,
            "top_break_after": [3, 4, 9, 22, 27, 31, 40],
            "bottom_break_after": [],
        }
        repaired = {
            **first, "model": "gpt-4.1", "top1_end": 9,
            "top_break_after": [3, 4, 9, 12, 22, 27, 31, 40],
        }
        models = []

        def generated(_top, _bottom, _contract, *, previous=None,
                      feedback="", model=None, repair=False):
            models.append(model)
            if previous is None:
                self.assertFalse(repair)
                return first
            self.assertIn("顶部最长块为索引 10-22", feedback)
            self.assertTrue(repair)
            return repaired

        def preflight(_method, _path, body, **_kwargs):
            if body["semantic_layout"] == first:
                raise self.module.MatrixTemplateHTTPError(
                    400, "HyperFrames 文案无法在完整语义边界内排入模板",
                )
            return {"payload": dict(body, duration=11.1)}

        self.module.matrix_template_semantics._CACHE.clear()
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(
                 self.module, "public_templates", return_value=[template],
             ), mock.patch.object(
                 self.module.matrix_template_semantics, "generate",
                 side_effect=generated,
             ), mock.patch.object(
                 self.module, "_request", side_effect=preflight,
             ):
            result = self.module.validate_payload({
                "top_text": top, "bottom_text": bottom,
                "template_id": template["id"], "bgm": True,
            })
        self.assertEqual(repaired, result["semantic_layout"])
        self.assertEqual([
            self.module.matrix_template_semantics.MODEL,
            self.module.matrix_template_semantics.REPAIR_MODEL,
        ], models)

    def test_worker_reuses_frozen_semantic_layout_without_second_ai_call(self):
        template = next(
            item for item in self.reference_templates()
            if item.get("variant") == "v02"
        )
        top = "团队8个人，每天产出100条短视频"
        bottom = "评论区扣888"
        semantic = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": self.module.matrix_template_semantics._source_sha256(
                top, bottom,
            ),
            "top1_end": top.index("，"),
            "top_break_after": [top.index("，")],
            "bottom_break_after": [],
        }
        expected = {
            "top_text": top, "bottom_text": bottom,
            "template_id": template["id"], "bgm": False,
            "duration": 11, "semantic_layout": semantic,
        }
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(
                 self.module, "public_templates", return_value=[template],
             ), mock.patch.object(
                 self.module.matrix_template_semantics, "resolve",
             ) as resolve, mock.patch.object(
                 self.module, "_request", return_value={"payload": expected},
             ) as request:
            result = self.module.validate_payload(
                expected, "alice", trusted_semantic_layout=semantic,
            )
        self.assertEqual(semantic, result["semantic_layout"])
        resolve.assert_not_called()
        self.assertEqual(1, request.call_count)

    def test_worker_rejects_invalid_frozen_layout_without_second_ai_call(self):
        template = next(
            item for item in self.reference_templates()
            if item.get("variant") == "v02"
        )
        semantic = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": "a" * 64, "top1_end": 1,
            "top_break_after": [1], "bottom_break_after": [],
        }
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(
                 self.module, "public_templates", return_value=[template],
             ), mock.patch.object(
                 self.module.matrix_template_semantics, "resolve",
             ) as resolve, mock.patch.object(
                 self.module, "_request",
                 side_effect=self.module.MatrixTemplateHTTPError(
                     400, "HyperFrames 文案无法在完整语义边界内排入模板",
                 ),
             ), self.assertRaisesRegex(ValueError, "任务失败.*退点"):
            self.module.validate_payload(
                {
                    "top_text": "团队8个人，每天产出100条短视频",
                    "bottom_text": "评论区扣888",
                    "template_id": template["id"], "bgm": False,
                },
                "alice", trusted_semantic_layout=semantic,
            )
        resolve.assert_not_called()

    def test_semantic_failure_aborts_without_generation_owned_fallback(self):
        template = next(
            item for item in self.reference_templates()
            if item.get("variant") == "v02"
        )
        rejected = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": "a" * 64, "top1_end": 1,
            "top_break_after": [1], "bottom_break_after": [],
        }
        requests = []

        def preflight(_method, _path, body, **_kwargs):
            requests.append(dict(body))
            raise self.module.MatrixTemplateHTTPError(
                400, "HyperFrames 顶部语义断点拆开了完整词组",
            )

        def resolve(_top, _bottom, _template_id, _contract, validator):
            accepted, feedback = validator(rejected)
            self.assertFalse(accepted)
            self.assertIn("语义断点", feedback)
            raise RuntimeError("AI 语义排版经两次修复后仍未通过真实字体校验")

        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(
                 self.module, "public_templates", return_value=[template],
             ), mock.patch.object(
                 self.module.matrix_template_semantics, "resolve",
                 side_effect=resolve,
             ), mock.patch.object(
                 self.module, "_request", side_effect=preflight,
             ), self.assertRaisesRegex(
                 ValueError, "AI 断句失败.*未扣点",
             ):
            self.module.validate_payload({
                "top_text": "团队8个人，每天产出100条短视频",
                "bottom_text": "评论区扣888",
                "template_id": template["id"],
                "bgm": False,
            })

        self.assertEqual(1, len(requests))
        self.assertIn("semantic_layout", requests[0])

    def test_semantic_connection_failure_aborts_before_preflight(self):
        template = next(
            item for item in self.reference_templates()
            if item.get("variant") == "v02"
        )
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(
                 self.module, "public_templates", return_value=[template],
             ), mock.patch.object(
                 self.module.matrix_template_semantics, "resolve",
                 side_effect=RuntimeError("AI 语义排版服务连接失败"),
             ), mock.patch.object(self.module, "_request") as request, \
             self.assertRaisesRegex(ValueError, "AI 断句失败.*未扣点"):
            self.module.validate_payload({
                "top_text": "团队8个人，每天产出100条短视频",
                "bottom_text": "评论区扣888",
                "template_id": template["id"],
                "bgm": False,
            })
        request.assert_not_called()

    def test_generation_preflight_connection_failure_remains_retryable_503(self):
        from content_domains import feature_flags

        template = next(
            item for item in self.reference_templates()
            if item.get("variant") == "v02"
        )
        semantic = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": "a" * 64, "top1_end": 1,
            "top_break_after": [1], "bottom_break_after": [],
        }

        def resolve(_top, _bottom, _template_id, _contract, validator):
            validator(semantic)
            self.fail("transport failure must escape the semantic resolver")

        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(
                 self.module, "public_templates", return_value=[template],
             ), mock.patch.object(
                 self.module.matrix_template_semantics, "resolve",
                 side_effect=resolve,
             ), mock.patch.object(
                 self.module, "_request",
                 side_effect=RuntimeError("生成端连接中断"),
             ), self.assertRaisesRegex(
                 feature_flags.FeatureDisabled, "模板成片服务暂不可用",
             ):
            self.module.validate_payload({
                "top_text": "团队8个人，每天产出100条短视频",
                "bottom_text": "评论区扣888",
                "template_id": template["id"], "bgm": False,
            })

    def test_reference_template_without_semantic_contract_fails_closed(self):
        template = next(
            item for item in self.reference_templates(("v02",))
            if item.get("variant") == "v05"
        )
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(
                 self.module, "public_templates", return_value=[template],
             ), \
             mock.patch.object(
                 self.module.matrix_template_semantics, "resolve",
             ) as resolve, \
             mock.patch.object(self.module, "_request") as request, \
             self.assertRaisesRegex(ValueError, "AI 断句能力不可用.*未扣点"):
            self.module.validate_payload({
                "top_text": "团队8个人，每天产出100条短视频",
                "bottom_text": "评论区扣111",
                "template_id": template["id"],
                "bgm": False,
            })
        resolve.assert_not_called()
        request.assert_not_called()

    def test_v02_http_200_normalization_is_accepted_once_for_concurrent_batch(self):
        template = next(
            item for item in self.reference_templates()
            if item.get("variant") == "v02"
        )
        top = "覆盖3.5万人，每天交流项目"
        bottom = "评论区扣111"
        first = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": "b" * 64, "top1_end": top.index("，"),
            "top_break_after": [3, top.index("，")],
            "bottom_break_after": [],
        }
        repaired = dict(first, top_break_after=[top.index("，")])

        def generated(_top, _bottom, _contract, *, previous=None,
                      feedback="", model=None, repair=False):
            self.assertIsNone(previous)
            self.assertFalse(feedback)
            self.assertFalse(repair)
            self.assertEqual(
                self.module.matrix_template_semantics.MODEL, model,
            )
            return first

        def preflight(_method, _path, body, **_kwargs):
            semantic = body["semantic_layout"]
            echoed = repaired if semantic == first else semantic
            return {
                "payload": dict(body, semantic_layout=echoed, duration=11),
            }

        for workers in (2, 5):
            with self.subTest(workers=workers):
                self.module.matrix_template_semantics._CACHE.clear()
                with mock.patch.object(self.module, "require_available"), \
                     mock.patch.object(
                         self.module, "public_templates", return_value=[template],
                     ), \
                     mock.patch.object(
                         self.module.matrix_template_semantics, "generate",
                         side_effect=generated,
                     ) as generate, \
                     mock.patch.object(
                         self.module, "_request", side_effect=preflight,
                     ), concurrent.futures.ThreadPoolExecutor(
                         max_workers=workers,
                     ) as pool:
                    futures = [
                        pool.submit(self.module.validate_payload, {
                            "top_text": top,
                            "bottom_text": bottom,
                            "template_id": template["id"],
                            "bgm": False,
                        }, "alice")
                        for _ in range(workers)
                    ]
                    results = [future.result() for future in futures]
                self.assertTrue(all(
                    item["semantic_layout"] == repaired for item in results
                ))
                self.assertEqual(1, generate.call_count)

    def test_semantic_normalization_rejects_critical_or_expanded_changes(self):
        template = next(
            item for item in self.reference_templates()
            if item.get("variant") == "v02"
        )
        top = "覆盖3.5万人，每天交流项目"
        bottom = "评论区扣111"
        first = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": "b" * 64, "top1_end": top.index("，"),
            "top_break_after": [3, top.index("，")],
            "bottom_break_after": [],
        }
        repaired = dict(first, top_break_after=[top.index("，")])
        tampered_values = {
            "top1_end": dict(repaired, top1_end=len(top) - 1),
            "expanded_breaks": dict(
                repaired,
                top_break_after=[top.index("，"), len(top) - 2],
            ),
        }

        for label, tampered in tampered_values.items():
            def generated(_top, _bottom, _contract, *, previous=None,
                          feedback="", model=None, repair=False):
                return repaired if previous is not None and feedback else first

            def preflight(_method, _path, body, **_kwargs):
                semantic = body["semantic_layout"]
                echoed = tampered if semantic == first else semantic
                return {
                    "payload": dict(
                        body, semantic_layout=echoed, duration=11,
                    ),
                }

            self.module.matrix_template_semantics._CACHE.clear()
            with self.subTest(label=label), \
                 mock.patch.object(self.module, "require_available"), \
                 mock.patch.object(
                     self.module, "public_templates", return_value=[template],
                 ), mock.patch.object(
                     self.module.matrix_template_semantics, "generate",
                     side_effect=generated,
                 ) as generate, mock.patch.object(
                     self.module, "_request", side_effect=preflight,
                 ):
                result = self.module.validate_payload({
                    "top_text": top, "bottom_text": bottom,
                    "template_id": template["id"], "bgm": False,
                }, "alice")

            self.assertEqual(repaired, result["semantic_layout"])
            self.assertEqual(2, generate.call_count)

    def test_v01_whitespace_boundary_cleanup_keeps_ai_layout(self):
        template = next(
            item for item in self.reference_templates()
            if item.get("variant") == "v01"
        )
        top = (
            "我是大鹏 陕西西安人 在广州有个创业圈子 "
            "资源共享|大健康|AI矩阵社交破圈|一人公司"
        )
        bottom = "PL区扣888"
        first = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": self.module.matrix_template_semantics._source_sha256(
                top, bottom,
            ), "top1_end": 10,
            "top_break_after": [4, 9, 10, 20, 25, 29, 38],
            "bottom_break_after": [],
        }
        normalized = dict(
            first, top_break_after=[4, 10, 20, 25, 29, 38],
        )

        def preflight(_method, _path, body, **_kwargs):
            return {
                "payload": dict(
                    body, semantic_layout=normalized, duration=14,
                ),
            }

        self.module.matrix_template_semantics._CACHE.clear()
        with mock.patch.object(self.module, "require_available"), \
             mock.patch.object(
                 self.module, "public_templates", return_value=[template],
             ), mock.patch.object(
                 self.module.matrix_template_semantics, "generate",
                 return_value=first,
             ) as generate, mock.patch.object(
                 self.module, "_request", side_effect=preflight,
             ):
            result = self.module.validate_payload({
                "top_text": top, "bottom_text": bottom,
                "template_id": template["id"], "bgm": True,
            }, "yuanzhi")

        self.assertEqual(normalized, result["semantic_layout"])
        self.assertNotIn(9, result["semantic_layout"]["top_break_after"])
        self.assertIn(38, result["semantic_layout"]["top_break_after"])
        self.assertEqual(1, generate.call_count)

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
             mock.patch.object(self.module, "_persist_runtime", return_value=True), \
             mock.patch.object(self.module, "public_url", return_value="/api/gen/file/token"), \
             mock.patch.object(self.module.time, "sleep"):
            result = self.module.generate(raw)
        self.assertEqual("video/matrix_template_77.mp4", result["video_file"])
        self.assertEqual("/api/gen/file/token", result["video_url"])
        self.assertEqual("a" * 32, result["provider_task_id"])
        self.assertEqual("matrix-template-77", request.call_args_list[0].kwargs["request_id"])
        self.assertEqual(
            ("/v1/files/%s.mp4" % ("a" * 32), "77"),
            download.call_args.args,
        )
        self.assertLessEqual(download.call_args.kwargs["timeout"], 240)
        self.assertGreater(download.call_args.kwargs["deadline_at"], 0)
        self.assertEqual("matrix_template", result["mode"])
        self.assertEqual(("done", "1080p", "9:16"), (
            result["phase"], result["resolution"], result["ratio"]
        ))

    def test_generate_voiceover_is_not_sent_to_renderer_and_replaces_bgm(self):
        raw = {
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
            "template_id": "native-bold", "bgm": False,
            "voiceover": {
                "text": "这是最终口播", "voice": "vip_alice", "speed": 1.0,
                "pitch": 0, "volume": 0, "delivery": "natural",
                "voice_scope": "personal",
            },
            "_username": "alice", "_job_id": 78,
        }
        provider_id = "d" * 32
        responses = [
            {"job_id": provider_id, "status": "pending"},
            {"status": "completed", "result": {
                "file_url": f"/v1/files/{provider_id}.mp4",
                "duration": 9.0, "width": 1080, "height": 1920,
                "template_id": "native-bold",
            }},
        ]
        prepared_audio = {
            "path": Path("voice.mp3"), "duration": 12.345,
            "fingerprint": "f" * 64,
        }
        with mock.patch.object(
            self.module, "validate_payload", return_value={
                key: value for key, value in raw.items() if not key.startswith("_")
            },
        ), mock.patch.object(
            self.module, "_request", side_effect=responses,
        ) as request, mock.patch.object(
            self.module, "_prepare_voiceover_audio", return_value=prepared_audio,
        ) as prepare_audio, mock.patch.object(
            self.module, "_download",
            return_value=("video/matrix_template_78.mp4", 4096),
        ), mock.patch.object(
            self.module, "_mux_voiceover", return_value=(12.345, 8192),
        ) as mux, mock.patch.object(
            self.module, "_persist_runtime", return_value=True,
        ) as persist, mock.patch.object(
            self.module, "public_url", return_value="/api/gen/file/voiced-video",
        ), mock.patch.object(self.module.time, "sleep"):
            result = self.module.generate(raw)

        provider_body = request.call_args_list[0].args[2]
        self.assertNotIn("voiceover", provider_body)
        self.assertFalse(provider_body["bgm"])
        prepare_audio.assert_called_once_with(
            "78", "alice", raw["voiceover"], None, mock.ANY,
        )
        mux.assert_called_once_with(
            "video/matrix_template_78.mp4", prepared_audio, mock.ANY,
        )
        self.assertTrue(any(
            call.kwargs.get("phase") == "muxing_voiceover"
            for call in persist.call_args_list
        ))
        self.assertEqual(12.345, result["duration"])
        self.assertEqual(8192, result["file_size"])
        self.assertEqual({
            "enabled": True, "voice": "vip_alice",
            "voice_scope": "personal", "duration_ms": 12345,
            "bgm": False,
        }, result["voiceover"])

    def test_voiceover_cache_is_shared_by_batch_but_isolated_by_owner(self):
        voiceover = {
            "text": "同一批次口播", "voice": "vip_alice", "speed": 1.0,
            "pitch": 0, "volume": 0, "delivery": "natural",
        }
        batch = "a" * 32
        first, _ = self.module._voiceover_cache_path(
            "101", "alice", dict(voiceover, _batch_id=batch),
        )
        second, _ = self.module._voiceover_cache_path(
            "102", "alice", dict(voiceover, _batch_id=batch),
        )
        other_owner, _ = self.module._voiceover_cache_path(
            "103", "bob", dict(voiceover, _batch_id=batch),
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, other_owner)
        self.assertNotIn("alice", first.name)

    def test_prepare_voiceover_audio_synthesizes_once_for_same_batch(self):
        from content_domains import audio

        voiceover = {
            "text": "同一批次口播", "voice": "vip_alice", "speed": 1.0,
            "pitch": 0, "volume": 0, "delivery": "natural",
            "voice_scope": "personal",
            "voice_version": self.module.hashlib.sha256(
                b"cosyvoice-v3.5-plus-alice"
            ).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def synthesize(_payload, publish=True):
                self.assertFalse(publish)
                source = root / "audio/generated.mp3"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"voice" * 1024)
                return {"file": "audio/generated.mp3"}

            with mock.patch.object(self.module, "OUT_DIR", root), \
                 mock.patch.object(
                     self.module, "_persist_runtime", return_value=True,
                 ), mock.patch.object(
                     self.module, "_media_probe",
                     return_value=([{"codec_type": "audio"}], 2.5),
                 ), mock.patch.object(
                     audio, "resolve_audio_provider_voice",
                     return_value="cosyvoice-v3.5-plus-alice",
                 ), mock.patch.object(
                     audio, "gen_audio", side_effect=synthesize,
                 ) as generate_audio:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    first, second = list(pool.map(
                        lambda job_id: self.module._prepare_voiceover_audio(
                            job_id, "alice", voiceover, "a" * 32,
                            time.time() + 30,
                        ),
                        ("101", "102"),
                    ))

        self.assertEqual(1, generate_audio.call_count)
        self.assertEqual("alice", generate_audio.call_args.args[0]["_username"])
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(2.5, second["duration"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "ffmpeg and ffprobe are required")
    def test_mux_voiceover_loops_video_and_keeps_only_voice_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "video/base.mp4"
            voice = root / "voice.wav"
            video.parent.mkdir(parents=True)
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=blue:s=1080x1920:r=30:d=0.6",
                "-f", "lavfi", "-i", "sine=frequency=880:duration=0.6",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-shortest", str(video),
            ], check=True, capture_output=True, timeout=30)
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1.7",
                "-c:a", "pcm_s16le", str(voice),
            ], check=True, capture_output=True, timeout=30)
            with mock.patch.object(self.module, "OUT_DIR", root):
                streams, voice_duration = self.module._media_probe(voice)
                self.assertEqual(1, len([
                    item for item in streams if item.get("codec_type") == "audio"
                ]))
                duration, size = self.module._mux_voiceover(
                    "video/base.mp4",
                    {"path": voice, "duration": voice_duration},
                    time.time() + 30,
                )
                output_streams, output_duration = self.module._media_probe(video)
            self.assertAlmostEqual(voice_duration, duration, delta=0.05)
            self.assertAlmostEqual(voice_duration, output_duration, delta=0.05)
            self.assertGreater(size, 1024)
            self.assertEqual(["h264"], [
                item.get("codec_name") for item in output_streams
                if item.get("codec_type") == "video"
            ])
            self.assertEqual(["aac"], [
                item.get("codec_name") for item in output_streams
                if item.get("codec_type") == "audio"
            ])

    def test_legacy_submission_unknown_replays_original_payload_exactly(self):
        legacy_payload = {
            "top_text": "有效标题", "bottom_text": "评论区扣888",
            "template_id": "ref-04-fixture-04", "bgm": False,
            "duration": 11,
            "_matrix_runtime": {"phase": "submission_unknown"},
        }
        remote_id = "e" * 32
        requests = []

        def request(method, path, body=None, **kwargs):
            requests.append((method, path, body, kwargs))
            if (method, path) == ("POST", "/v1/jobs"):
                return {"job_id": remote_id, "status": "pending"}
            if (method, path) == ("GET", "/v1/jobs/" + remote_id):
                return {"status": "failed", "error": "legacy request missing"}
            raise AssertionError((method, path))

        with mock.patch.object(
            self.module, "_runtime", return_value={
                "created_at": int(self.module.time.time()),
                "payload": legacy_payload,
            },
        ), mock.patch.object(
            self.module, "validate_payload",
            side_effect=AssertionError("legacy unknown replay must not revalidate"),
        ), mock.patch.object(
            self.module, "_request", side_effect=request,
        ), mock.patch.object(
            self.module, "_persist_runtime", return_value=True,
        ), mock.patch.object(self.module.time, "sleep"), \
             self.assertRaisesRegex(
                 self.module.MatrixTemplateProviderFailed,
                 "legacy request missing",
             ):
            self.module.generate({
                **legacy_payload, "_job_id": 77, "_username": "alice",
            })

        self.assertEqual(("POST", "/v1/jobs"), requests[0][:2])
        self.assertEqual({
            key: value for key, value in legacy_payload.items()
            if not key.startswith("_")
        }, requests[0][2])
        self.assertEqual("matrix-template-77", requests[0][3]["request_id"])
        self.assertTrue(all(path != "/v1/preflight" for _, path, _, _ in requests))

    def test_generate_uses_submission_time_as_absolute_deadline(self):
        with mock.patch.object(
            self.module, "_runtime",
            return_value={"created_at": 100, "payload": {}},
        ), mock.patch.object(self.module.time, "time", return_value=1400), \
             mock.patch.object(self.module, "validate_payload") as validate, \
             mock.patch.object(self.module, "_request") as request, \
             self.assertRaisesRegex(RuntimeError, "等待超时"):
            self.module.generate({"_job_id": 88, "_username": "alice"})
        validate.assert_not_called()
        request.assert_not_called()

    def test_generate_rechecks_deadline_after_preflight_before_post(self):
        clock = {"now": 100.0}

        def validate(*_args, **_kwargs):
            clock["now"] = 1301.0
            return {"template_id": "native-bold"}

        with mock.patch.object(
            self.module, "_runtime",
            return_value={"created_at": 100, "payload": {}},
        ), mock.patch.object(
            self.module.time, "time", side_effect=lambda: clock["now"],
        ), mock.patch.object(
            self.module, "validate_payload", side_effect=validate,
        ), mock.patch.object(self.module, "_request") as request, \
             mock.patch.object(self.module, "_persist_runtime") as persist, \
             self.assertRaisesRegex(RuntimeError, "生成超时"):
            self.module.generate({"_job_id": 90, "_username": "alice"})
        request.assert_not_called()
        persist.assert_not_called()

    def test_generate_resumes_persisted_provider_job_without_second_post(self):
        provider_id = "c" * 32
        stored = {
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
            "template_id": "native-bold", "bgm": True, "duration": 8,
            "_matrix_runtime": {
                "phase": "provider_queued", "provider_job_id": provider_id,
            },
        }
        with mock.patch.object(
            self.module, "_runtime",
            return_value={
                "created_at": int(self.module.time.time()), "payload": stored,
            },
        ), mock.patch.object(self.module, "validate_payload") as validate, \
             mock.patch.object(
                 self.module, "_request",
                 return_value={"status": "failed", "error": "renderer failed"},
             ) as request, mock.patch.object(self.module.time, "sleep"), \
             self.assertRaisesRegex(RuntimeError, "renderer failed"):
            self.module.generate({"_job_id": 91, "_username": "alice"})
        validate.assert_not_called()
        self.assertEqual(1, request.call_count)
        self.assertEqual(("GET", "/v1/jobs/" + provider_id), request.call_args.args)

    def test_generate_resumes_voiceover_job_without_resubmitting_renderer(self):
        provider_id = "c" * 32
        voiceover = {
            "text": "恢复配音", "voice": "vip_alice", "speed": 1.0,
            "pitch": 0, "volume": 0, "delivery": "natural",
            "voice_scope": "personal",
        }
        stored = {
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
            "template_id": "native-bold", "bgm": False, "duration": 8,
            "voiceover": voiceover,
            "_matrix_runtime": {
                "phase": "provider_queued", "provider_job_id": provider_id,
            },
        }
        prepared = {
            "path": Path("voice.mp3"), "duration": 8.0,
            "fingerprint": "f" * 64,
        }
        with mock.patch.object(
            self.module, "_runtime", return_value={
                "created_at": int(self.module.time.time()), "payload": stored,
            },
        ), mock.patch.object(
            self.module, "_prepare_voiceover_audio", return_value=prepared,
        ) as prepare, mock.patch.object(
            self.module, "_request",
            return_value={"status": "failed", "error": "renderer failed"},
        ) as request, mock.patch.object(self.module.time, "sleep"), \
             self.assertRaisesRegex(RuntimeError, "renderer failed"):
            self.module.generate({"_job_id": 93, "_username": "alice"})

        prepare.assert_called_once_with("93", "alice", voiceover, None, mock.ANY)
        self.assertEqual(1, request.call_count)
        self.assertEqual(("GET", "/v1/jobs/" + provider_id), request.call_args.args)

    def test_generate_does_not_post_without_durable_submitting_phase(self):
        with mock.patch.object(
            self.module, "_runtime",
            return_value={
                "created_at": int(self.module.time.time()), "payload": {},
            },
        ), mock.patch.object(
            self.module, "validate_payload", return_value={
                "template_id": "native-bold",
            },
        ), mock.patch.object(
            self.module, "_persist_runtime", return_value=False,
        ), mock.patch.object(self.module, "_request") as request, \
             self.assertRaisesRegex(RuntimeError, "状态保存失败"):
            self.module.generate({"_job_id": 92, "_username": "alice"})
        request.assert_not_called()

    def test_download_discards_partial_file_when_deadline_crosses_during_read(self):
        clock = {"now": 100.0}

        class SlowResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                clock["now"] = 101.0
                return b"\x00\x00\x00\x18ftyp" + (b"x" * 2048)

        opener = mock.Mock()
        opener.open.return_value = SlowResponse()
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(self.module, "OUT_DIR", Path(temp)), \
             mock.patch.object(self.module, "_safe_file_url", return_value="https://example.test/file.mp4"), \
             mock.patch.object(self.module, "_NO_PROXY", opener), \
             mock.patch.object(self.module.time, "time", side_effect=lambda: clock["now"]), \
             self.assertRaisesRegex(RuntimeError, "生成超时"):
            self.module._download(
                "/file.mp4", "slow-job", timeout=10, deadline_at=100.5,
            )
        self.assertFalse((Path(temp) / "video" / "matrix_template_slow-job.mp4").exists())
        self.assertFalse((Path(temp) / "video" / "matrix_template_slow-job.mp4.part").exists())

    def test_download_real_trickle_stream_obeys_wall_clock_deadline(self):
        class TrickleHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "20")
                self.end_headers()
                for _index in range(20):
                    try:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    except (
                        BrokenPipeError, ConnectionAbortedError,
                        ConnectionResetError,
                    ):
                        break
                    time.sleep(0.1)

            def log_message(self, _format, *_args):
                return

        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), TrickleHandler,
        )
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d/video.mp4" % server.server_port
        try:
            with tempfile.TemporaryDirectory() as temp, \
                 mock.patch.object(self.module, "OUT_DIR", Path(temp)), \
                 mock.patch.object(self.module, "_safe_file_url", return_value=url), \
                 mock.patch.object(
                     self.module, "_NO_PROXY",
                     urllib.request.build_opener(
                         urllib.request.ProxyHandler({})
                     ),
                 ):
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "生成超时"):
                    self.module._download(
                        url, "real-trickle", timeout=10,
                        deadline_at=time.time() + 0.25,
                    )
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 1.0)
                target = Path(temp) / "video/matrix_template_real-trickle.mp4"
                self.assertFalse(target.exists())
                self.assertFalse(target.with_suffix(".mp4.part").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_generate_persists_provider_identity_and_progress(self):
        responses = [
            {"job_id": "b" * 32},
            {"status": "running"},
            {"status": "failed", "error": "renderer failed"},
        ]
        with mock.patch.object(
            self.module, "_runtime",
            return_value={"created_at": int(self.module.time.time()), "payload": {}},
        ), mock.patch.object(self.module, "validate_payload", return_value={
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
            "template_id": "full-overlay-bold", "bgm": True, "duration": 8,
        }), mock.patch.object(
            self.module, "_request", side_effect=responses,
        ), mock.patch.object(
            self.module, "_persist_runtime", return_value=True,
        ) as persist, mock.patch.object(self.module.time, "sleep"), \
             self.assertRaisesRegex(RuntimeError, "renderer failed"):
            self.module.generate({"_job_id": 89, "_username": "alice"})
        provider = next(
            call for call in persist.call_args_list
            if call.kwargs.get("provider_job_id")
        )
        self.assertEqual("b" * 32, provider.kwargs["provider_job_id"])
        self.assertEqual("provider_queued", provider.kwargs["phase"])
        self.assertTrue(any(
            call.kwargs.get("phase") == "rendering"
            for call in persist.call_args_list
        ))

    def test_public_lifecycle_uses_server_time_and_hides_provider_id(self):
        row = {
            "status": "running", "created_at": 100,
            "payload": json.dumps({"_matrix_runtime": {
                "phase": "rendering", "provider_job_id": "secret-provider-id",
                "last_progress_at": 180,
            }}),
        }
        value = self.module.public_lifecycle(row, now=250)
        self.assertEqual("rendering", value["phase"])
        self.assertEqual(150, value["elapsed_seconds"])
        self.assertEqual(100 + self.module.TOTAL_TIMEOUT, value["deadline_at"])
        self.assertTrue(value["provider_submitted"])
        self.assertNotIn("provider_job_id", value)

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

    def test_http_retry_recovers_charged_voiceover_before_current_voice_validation(self):
        from content_domains import (
            core, matrix_template_submission, submission_idempotency,
        )

        with tempfile.TemporaryDirectory() as temp:
            database_path = Path(temp) / "jobs.db"

            def database():
                connection = sqlite3.connect(database_path, timeout=30)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(database()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,username TEXT NOT NULL,cost INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',payload TEXT NOT NULL,
                    result TEXT,error TEXT,refunded INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
                    owner TEXT
                )""")
                submission_idempotency.ensure_table(connection)
                connection.commit()

            request_body = {
                "top_text": "有效标题", "bottom_text": "评论区扣关键词",
                "template_id": "native-bold", "bgm": False,
                "voiceover": {
                    "text": "已冻结的口播", "voice": "vip_alice",
                    "voice_scope": "personal", "speed": 1,
                    "pitch": 0, "volume": 0, "delivery": "natural",
                },
            }
            frozen = json.loads(json.dumps(request_body, ensure_ascii=False))
            frozen["duration"] = 8.0
            frozen["voiceover"]["voice_version"] = "f" * 64
            key = "matrix-charged-voice-recovery"
            state, _ = submission_idempotency.begin(
                database, "alice", "/api/gen/matrix-template", key,
                request_body,
            )
            self.assertEqual("new", state)
            matrix_template_submission.prepare(
                database, "alice", "/api/gen/matrix-template", key,
                request_body, 5, execution_body=frozen,
            )
            with closing(database()) as connection:
                connection.execute("""UPDATE matrix_template_submission_attempts
                    SET state='charged',points_left=95,lease_token='',lease_until=0
                    WHERE username='alice' AND endpoint='/api/gen/matrix-template'
                      AND idem_key=?""", (key,))
                connection.commit()

            class Points:
                deductions = 1

                @classmethod
                def deduct_points(cls, *_args, **_kwargs):
                    cls.deductions += 1
                    raise AssertionError("charged attempt must not deduct again")

                @staticmethod
                def refund_points(*_args, **_kwargs):
                    raise AssertionError("charged recovery must not refund")

                @staticmethod
                def get_points_transaction(_key):
                    return None

            queued = []
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            original_job_db = core.JOB_DB
            core.JOB_DB = str(database_path)
            thread.start()
            try:
                with mock.patch.object(
                    core, "verify", return_value={
                        "username": "alice", "points": 95,
                        "must_change": False,
                    },
                ), mock.patch.object(
                    core, "_domains", return_value=(
                        SimpleNamespace(), Points, SimpleNamespace(),
                    ),
                ), mock.patch.object(
                    core, "enqueue_job",
                    side_effect=lambda job_id, kind, mode: (
                        queued.append((job_id, kind, mode)) or True
                    ),
                ), mock.patch.object(
                    self.module, "validate_payload",
                    side_effect=AssertionError(
                        "current voice state must not be revalidated"
                    ),
                ) as validate:
                    responses = []
                    for _attempt in range(2):
                        request = urllib.request.Request(
                            "http://127.0.0.1:%d/api/gen/matrix-template"
                            % server.server_port,
                            data=json.dumps(request_body, ensure_ascii=False).encode(),
                            headers={
                                "Authorization": "Bearer account-token",
                                "Content-Type": "application/json",
                                "Idempotency-Key": key,
                            },
                            method="POST",
                        )
                        with urllib.request.urlopen(request, timeout=10) as response:
                            self.assertEqual(200, response.status)
                            responses.append(json.load(response))
                validate.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                core.JOB_DB = original_job_db

            self.assertEqual(1, Points.deductions)
            self.assertEqual(responses[0]["job_id"], responses[1]["job_id"])
            self.assertTrue(responses[0]["reconciled"])
            with closing(database()) as connection:
                jobs = connection.execute(
                    "SELECT id,status,payload FROM jobs ORDER BY id"
                ).fetchall()
            self.assertEqual(1, len(jobs))
            self.assertEqual("pending", jobs[0]["status"])
            self.assertEqual(frozen, json.loads(jobs[0]["payload"]))
            self.assertEqual(
                [(jobs[0]["id"], "matrix_template_video", None)], queued,
            )

    def test_http_shutdown_preserves_prepared_attempt_then_recovers_once(self):
        from content_domains import (
            core, matrix_template_submission, submission_idempotency,
        )

        with tempfile.TemporaryDirectory() as temp:
            database_path = Path(temp) / "jobs.db"

            def database():
                connection = sqlite3.connect(database_path, timeout=30)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(database()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,username TEXT NOT NULL,cost INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',payload TEXT NOT NULL,
                    result TEXT,error TEXT,refunded INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
                    owner TEXT
                )""")
                submission_idempotency.ensure_table(connection)
                connection.commit()
            request_body = {
                "top_text": "停机标题", "bottom_text": "评论区扣关键词",
                "template_id": "native-bold", "bgm": False,
                "voiceover": {
                    "text": "停机期间不能扣点", "voice": "vip_alice",
                    "voice_scope": "personal", "speed": 1,
                    "pitch": 0, "volume": 0, "delivery": "natural",
                },
            }
            frozen = json.loads(json.dumps(request_body, ensure_ascii=False))
            frozen["duration"] = 8.0
            frozen["voiceover"]["voice_version"] = "a" * 64
            key = "matrix-prepared-shutdown-recovery"
            submission_idempotency.begin(
                database, "alice", "/api/gen/matrix-template", key,
                request_body,
            )
            matrix_template_submission.prepare(
                database, "alice", "/api/gen/matrix-template", key,
                request_body, 5, execution_body=frozen,
            )

            class Points:
                deductions = 0

                @classmethod
                def deduct_points(cls, *_args, **_kwargs):
                    cls.deductions += 1
                    return 95

                @staticmethod
                def refund_points(*_args, **_kwargs):
                    raise AssertionError("successful recovery must not refund")

                @staticmethod
                def get_points_transaction(_key):
                    return None

            shutting_down = {"value": True}
            queued = []
            original_job_db = core.JOB_DB
            core.JOB_DB = str(database_path)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def submit():
                request = urllib.request.Request(
                    "http://127.0.0.1:%d/api/gen/matrix-template"
                    % server.server_port,
                    data=json.dumps(request_body, ensure_ascii=False).encode(),
                    headers={
                        "Authorization": "Bearer account-token",
                        "Content-Type": "application/json",
                        "Idempotency-Key": key,
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.loads(error.read())

            try:
                with mock.patch.object(
                    core, "verify", return_value={
                        "username": "alice", "points": 100,
                        "must_change": False,
                    },
                ), mock.patch.object(
                    core, "_domains", return_value=(
                        SimpleNamespace(), Points, SimpleNamespace(),
                    ),
                ), mock.patch.object(
                    core, "is_shutting_down",
                    side_effect=lambda: shutting_down["value"],
                ), mock.patch.object(
                    core, "enqueue_job",
                    side_effect=lambda job_id, kind, mode: (
                        queued.append((job_id, kind, mode)) or True
                    ),
                ), mock.patch.object(
                    self.module, "validate_payload",
                    side_effect=AssertionError(
                        "frozen attempt must not revalidate current voice"
                    ),
                ) as validate:
                    blocked_status, blocked = submit()
                    self.assertEqual(503, blocked_status)
                    self.assertEqual("shutting_down", blocked["code"])
                    with closing(database()) as connection:
                        attempt = connection.execute(
                            "SELECT state FROM matrix_template_submission_attempts "
                            "WHERE idem_key=?", (key,),
                        ).fetchone()
                        self.assertEqual("prepared", attempt["state"])
                        self.assertEqual(
                            0, connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                        )
                    self.assertEqual(0, Points.deductions)
                    self.assertEqual([], queued)

                    shutting_down["value"] = False
                    resumed_status, resumed = submit()
                    self.assertEqual(200, resumed_status)
                    self.assertGreater(int(resumed["job_id"]), 0)
                    validate.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                core.JOB_DB = original_job_db

            self.assertEqual(1, Points.deductions)
            with closing(database()) as connection:
                self.assertEqual(
                    1, connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                )
                state = connection.execute(
                    "SELECT state FROM matrix_template_submission_attempts "
                    "WHERE idem_key=?", (key,),
                ).fetchone()["state"]
            self.assertEqual("linked", state)
            self.assertEqual(1, len(queued))

    def test_background_shutdown_skips_charge_and_job_until_resumed(self):
        from content_domains import (
            core, matrix_template_submission, submission_idempotency,
        )

        with tempfile.TemporaryDirectory() as temp:
            database_path = Path(temp) / "jobs.db"

            def database():
                connection = sqlite3.connect(database_path, timeout=30)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(database()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,username TEXT NOT NULL,cost INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',payload TEXT NOT NULL,
                    result TEXT,error TEXT,refunded INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
                    owner TEXT
                )""")
                submission_idempotency.ensure_table(connection)
                connection.commit()
            body = {
                "top_text": "后台恢复", "bottom_text": "评论区扣关键词",
                "template_id": "native-bold", "bgm": True, "duration": 8.0,
            }
            key = "matrix-background-shutdown"
            submission_idempotency.begin(
                database, "alice", "/api/gen/matrix-template", key, body,
            )
            matrix_template_submission.prepare(
                database, "alice", "/api/gen/matrix-template", key,
                body, 5, execution_body=body,
            )

            class Points:
                deductions = 0

                @classmethod
                def deduct_points(cls, *_args, **_kwargs):
                    cls.deductions += 1
                    return 95

                @staticmethod
                def refund_points(*_args, **_kwargs):
                    raise AssertionError("successful recovery must not refund")

                @staticmethod
                def get_points_transaction(_key):
                    return None

            queued = []
            original_job_db = core.JOB_DB
            core.JOB_DB = str(database_path)
            try:
                with mock.patch.object(
                    core, "_domains", return_value=(
                        SimpleNamespace(), Points, SimpleNamespace(),
                    ),
                ), mock.patch.object(
                    core, "enqueue_job",
                    side_effect=lambda job_id, kind, mode: (
                        queued.append((job_id, kind, mode)) or True
                    ),
                ), mock.patch.object(
                    core, "is_shutting_down", return_value=True,
                ):
                    self.assertEqual(0, core._retry_matrix_template_submissions())
                with closing(database()) as connection:
                    self.assertEqual(
                        0, connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                    )
                    state = connection.execute(
                        "SELECT state FROM matrix_template_submission_attempts "
                        "WHERE idem_key=?", (key,),
                    ).fetchone()["state"]
                self.assertEqual("prepared", state)
                self.assertEqual(0, Points.deductions)
                self.assertEqual([], queued)

                with mock.patch.object(
                    core, "_domains", return_value=(
                        SimpleNamespace(), Points, SimpleNamespace(),
                    ),
                ), mock.patch.object(
                    core, "enqueue_job",
                    side_effect=lambda job_id, kind, mode: (
                        queued.append((job_id, kind, mode)) or True
                    ),
                ), mock.patch.object(
                    core, "is_shutting_down", return_value=False,
                ):
                    self.assertEqual(1, core._retry_matrix_template_submissions())
            finally:
                core.JOB_DB = original_job_db

            self.assertEqual(1, Points.deductions)
            with closing(database()) as connection:
                self.assertEqual(
                    1, connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                )
                state = connection.execute(
                    "SELECT state FROM matrix_template_submission_attempts "
                    "WHERE idem_key=?", (key,),
                ).fetchone()["state"]
            self.assertEqual("linked", state)
            self.assertEqual(1, len(queued))

    def test_background_shutdown_still_reconciles_refund_pending(self):
        from content_domains import core, matrix_template_submission

        item = {
            "username": "alice", "endpoint": "/api/gen/matrix-template",
            "idem_key": "matrix-refund-during-shutdown",
        }
        refunded = {**item, "state": "refunded", "job_id": None}
        with mock.patch.object(
            core, "is_shutting_down", return_value=True,
        ), mock.patch.object(
            matrix_template_submission, "recoverable", return_value=[item],
        ), mock.patch.object(
            matrix_template_submission, "get",
            return_value={**item, "state": "refund_pending"},
        ), mock.patch.object(
            matrix_template_submission, "recover", return_value=refunded,
        ) as recover, mock.patch.object(
            core, "_domains", return_value=(
                SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
            ),
        ), mock.patch.object(core, "enqueue_job") as enqueue:
            self.assertEqual(1, core._retry_matrix_template_submissions())
        recover.assert_called_once()
        enqueue.assert_not_called()

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
        self.assertNotIn('id="fontFamily"', page)
        self.assertNotIn('id="fontSource"', page)
        self.assertIn('id="voiceoverEnabled"', page)
        self.assertIn('id="voiceoverText"', page)
        self.assertIn('id="voiceoverVoice"', page)
        self.assertIn('id="voiceoverSpeed"', page)
        self.assertIn("/api/gen/audio/voices", page)
        self.assertIn("bgm:!withVoice", page)
        self.assertIn("speed:voiceSpeed()", page)
        self.assertIn("function playableVoiceUrl(value)", page)
        self.assertIn("URL.revokeObjectURL", page)
        self.assertIn("addEventListener('pagehide',stopVoicePreview)", page)
        self.assertIn("item.scope===voiceScope&&item.ready===true", page)
        self.assertIn('id="batchCount"', page)
        self.assertIn("Math.min(batchLimit", page)
        self.assertNotIn("排队", page)
        self.assertNotIn("（并行）", page)
        self.assertNotIn("font_family", page)
        self.assertNotIn("素材来源", page)
        self.assertIn("template_id:activeTemplate,bgm:!withVoice", page)
        self.assertIn('hq-content[data-active="matrix-template"]{height:auto!important', page)
        self.assertIn("function fitLiveText(node,max,min)", page)
        self.assertIn("var referencePreviews=", page)
        self.assertIn("data-variant", page)
        self.assertIn("item.description", page)
        self.assertIn("node.scrollHeight>node.clientHeight", page)
        self.assertIn("fitLiveText(el('liveTop'),topSizes[activeTemplate]||34,12)", page)
        self.assertIn("fitLiveText(el('liveBottom'),20,12)", page)
        self.assertIn("var hiddenTemplateIds={'full-overlay-bold':true,'poster-split':true}", page)
        self.assertIn("filter(function(item){return item&&!hiddenTemplateIds[item.id]})", page)
        self.assertIn(".mt-action:disabled{opacity:.55;cursor:not-allowed}", page)
        self.assertNotIn(".mt-action:disabled{opacity:.55;cursor:wait}", page)
        self.assertIn("button.disabled=!busy&&!activeTemplate", page)
        self.assertIn("if(!checking&&warnCopy())return", page)
        self.assertIn("busy?'检查任务状态'", page)
        self.assertIn("if(!pending){busy=false;sync();return}", page)
        self.assertIn("checking=busy||!!existing", page)
        self.assertIn("pendingIdentity(current)!==expectedIdentity", page)
        self.assertLess(shell.index("k:'text-video'"), shell.index("k:'matrix-template'"))
        self.assertIn("/api/gen/matrix-template/capability", shell)

    def test_openapi_documents_owned_voiceover_contract(self):
        docs = (ROOT / "docs/api/openapi.json").read_text(encoding="utf-8")
        site = (ROOT / "site/api-docs/openapi.json").read_text(encoding="utf-8")
        self.assertEqual(docs, site)
        operation = json.loads(docs)["paths"]["/api/gen/matrix-template"]["post"]
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        voiceover = schema["properties"]["voiceover"]
        self.assertFalse(voiceover["additionalProperties"])
        self.assertEqual(["text", "voice"], voiceover["required"])
        self.assertEqual(
            ["public", "personal"],
            voiceover["properties"]["voice_scope"]["enum"],
        )
        self.assertIn("强制关闭 BGM", operation["description"])
        self.assertIn("时长跟随配音", operation["description"])
        voice_item = json.loads(docs)["paths"]["/api/gen/audio/voices"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]["properties"][
            "items"
        ]["items"]
        self.assertIn("status", voice_item["required"])
        self.assertIn("ready", voice_item["required"])

    def test_layout_browser_regression_covers_all_reference_templates(self):
        source = (ROOT / "tests/matrix_template_layout_browser.js").read_text(
            encoding="utf-8"
        )
        self.assertEqual(17, len(re.findall(r"'ref-[0-9]{2}-[a-z0-9-]+'", source)))
        self.assertIn("cardCount !== 17", source)
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
        self.assertTrue(all("voiceover" not in body for body in result["bodies"]))
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
        self.assertTrue(result["afterFailure"]["busy"])
        self.assertTrue(result["afterFailure"]["enabled"])
        self.assertEqual("检查任务状态", result["afterFailure"]["text"])
        self.assertEqual(2, result["polls"])
        self.assertTrue(result["cleared"])

    def test_poll_http_5xx_keeps_busy_and_recovers(self):
        result = self.runtime("pollHttpFailure")
        self.assertEqual(1, result["before"]["polls"])
        self.assertTrue(result["before"]["action"]["busy"])
        self.assertTrue(result["before"]["action"]["enabled"])
        self.assertEqual("检查任务状态", result["before"]["action"]["text"])
        self.assertFalse(result["before"]["cleared"])
        self.assertEqual(2, result["polls"])
        self.assertEqual("/http-poll-recovered-video", result["src"])
        self.assertTrue(result["cleared"])

    def test_repeated_poll_failures_keep_recovering_without_customer_click(self):
        result = self.runtime("pollRecoveryBeyondFive")
        self.assertEqual(1, result["before"]["polls"])
        self.assertFalse(result["before"]["cleared"])
        self.assertEqual(7, result["polls"])
        self.assertEqual("/poll-recovered-video", result["src"])
        self.assertNotIn("点击生成", result["status"])
        self.assertTrue(result["cleared"])

    def test_completed_single_result_loads_into_right_player_immediately(self):
        result = self.runtime("instantResult")
        self.assertEqual("/instant-video", result["src"])
        self.assertEqual("block", result["display"])
        self.assertEqual("none", result["live"])
        self.assertEqual("/instant-video", result["download"])
        self.assertEqual("auto", result["preload"])
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

    def test_slow_result_address_sync_does_not_require_refresh(self):
        result = self.runtime("longDelayedResultUrl")
        self.assertEqual(9, result["polls"])
        self.assertEqual("/slow-video", result["src"])
        self.assertEqual(1, result["loads"])
        self.assertIn("完成", result["status"])
        self.assertTrue(result["cleared"])

    def test_returning_to_foreground_polls_immediately_without_refresh(self):
        result = self.runtime("foregroundResume")
        self.assertEqual(1, result["before"]["polls"])
        self.assertEqual(0, result["before"]["loads"])
        self.assertFalse(result["before"]["cleared"])
        self.assertEqual(2, result["polls"])
        self.assertEqual("/focus-video", result["src"])
        self.assertEqual("block", result["display"])
        self.assertEqual(1, result["loads"])
        self.assertTrue(result["cleared"])

    def test_uncertain_submission_recovers_without_customer_click(self):
        result = self.runtime("uncertainAutoRecovery")
        self.assertEqual(1, result["afterLoad"]["posts"])
        self.assertIn("正在自动确认提交结果", result["afterLoad"]["status"])
        self.assertIn("不会重复扣点", result["afterLoad"]["status"])
        self.assertNotIn("867 秒", result["afterLoad"]["status"])
        self.assertTrue(result["afterLoad"]["action"]["busy"])
        self.assertTrue(result["afterLoad"]["action"]["enabled"])
        self.assertEqual("检查任务状态", result["afterLoad"]["action"]["text"])
        self.assertEqual(5, result["posts"])
        self.assertEqual(
            ["matrix-template-stable-retry-key"] * 5,
            result["keys"],
        )
        self.assertNotIn("点击生成确认重试", result["status"])
        self.assertEqual("/auto-recovered-video", result["src"])
        self.assertTrue(result["cleared"])

    def test_stale_unaccepted_submission_recovers_without_customer_click(self):
        result = self.runtime("staleSubmittingAutoRecovery")
        self.assertEqual(1, result["posts"])
        self.assertEqual(
            "matrix-template-stale-retry-key",
            result["key"],
        )
        self.assertEqual("/stale-recovered-video", result["src"])
        self.assertNotIn("点击生成", result["status"])
        self.assertTrue(result["cleared"])

    def test_pending_submission_is_never_replayed_for_another_account(self):
        result = self.runtime("crossAccountPending")
        self.assertEqual(0, result["posts"])
        self.assertEqual(0, result["polls"])
        self.assertTrue(result["aliceRetained"])
        self.assertTrue(result["ownerlessRemoved"])
        self.assertEqual("", result["top"])

    def test_dynamic_account_switch_stops_old_account_post_and_poll(self):
        result = self.runtime("dynamicAccountSwitch")
        self.assertEqual(["alice", "alice"], result["before"]["postAccounts"])
        self.assertEqual(["alice"], result["before"]["pollAccounts"])
        self.assertTrue(result["before"]["alicePending"])
        self.assertEqual(0, result["bobPosts"])
        self.assertEqual(0, result["bobPolls"])
        self.assertEqual(["alice", "alice"], result["postAccounts"])
        self.assertEqual(["alice"], result["pollAccounts"])
        self.assertTrue(result["alicePending"])
        self.assertFalse(result["bobPending"])
        self.assertEqual("", result["top"])
        self.assertEqual("", result["bottom"])
        self.assertNotIn("AI 工作流", result["status"])

    def test_auth_failure_before_retry_stops_paid_submission(self):
        result = self.runtime("retryAuthFailure")
        self.assertEqual(1, result["before"]["posts"])
        self.assertTrue(result["before"]["pending"])
        self.assertEqual(1, result["posts"])
        self.assertEqual(0, result["polls"])
        self.assertTrue(result["pending"])
        self.assertEqual("", result["top"])
        self.assertIn("auth unavailable", result["status"])

    def test_concurrent_stale_auth_restores_new_owner_pending_once(self):
        result = self.runtime("concurrentStaleAuth")
        self.assertEqual(["alice", "bob"], result["postAccounts"])
        self.assertEqual(1, result["bobPosts"])
        self.assertEqual(1, result["bobPolls"])
        self.assertEqual("bob-own-key", result["postKeys"][1])
        self.assertTrue(result["alicePending"])
        self.assertFalse(result["bobPending"])
        self.assertEqual("Bob 标题", result["top"])
        self.assertEqual("/bob-own-video", result["src"])

    def test_foreground_resume_does_not_duplicate_inflight_requests(self):
        result = self.runtime("foregroundSingleFlight")
        self.assertEqual(1, result["postsWhileInflight"])
        self.assertEqual(1, result["pollsWhileInflight"])
        self.assertEqual("/single-flight-video", result["src"])
        self.assertTrue(result["cleared"])

    def test_hung_submission_times_out_and_recovers_without_stale_callback(self):
        result = self.runtime("hungSubmissionTimeout")
        self.assertEqual(1, result["before"]["posts"])
        self.assertEqual(1, result["afterTimeout"]["posts"])
        self.assertIn("自动确认", result["afterTimeout"]["status"])
        self.assertFalse(result["afterTimeout"]["cleared"])
        self.assertEqual(2, result["afterRecovery"]["posts"])
        self.assertEqual(1, len(set(result["afterRecovery"]["keys"])))
        self.assertEqual("/timeout-recovered-video", result["afterRecovery"]["src"])
        self.assertTrue(result["afterRecovery"]["cleared"])
        self.assertEqual(2, result["afterLateResponse"]["posts"])
        self.assertEqual(1, result["afterLateResponse"]["polls"])
        self.assertEqual("/timeout-recovered-video", result["afterLateResponse"]["src"])
        self.assertTrue(result["afterLateResponse"]["cleared"])

    def test_hung_poll_times_out_and_recovers_without_stale_callback(self):
        result = self.runtime("hungPollTimeout")
        self.assertEqual(1, result["before"]["polls"])
        self.assertTrue(result["before"]["action"]["busy"])
        self.assertTrue(result["before"]["action"]["enabled"])
        self.assertFalse(result["before"]["cleared"])
        self.assertEqual(1, result["afterTimeout"]["polls"])
        self.assertTrue(result["afterTimeout"]["action"]["busy"])
        self.assertTrue(result["afterTimeout"]["action"]["enabled"])
        self.assertFalse(result["afterTimeout"]["cleared"])
        self.assertEqual(2, result["afterRecovery"]["polls"])
        self.assertEqual("/timeout-poll-recovered-video", result["afterRecovery"]["src"])
        self.assertTrue(result["afterRecovery"]["cleared"])
        self.assertEqual(2, result["afterLateResponse"]["polls"])
        self.assertEqual("/timeout-poll-recovered-video", result["afterLateResponse"]["src"])
        self.assertTrue(result["afterLateResponse"]["cleared"])

    def test_result_video_retries_media_load_without_page_refresh(self):
        result = self.runtime("mediaRetry")
        self.assertEqual("/retry-video", result["before"]["src"])
        self.assertEqual("auto", result["before"]["preload"])
        self.assertEqual(1, result["before"]["loads"])
        self.assertIn("hq_media_retry=1-", result["after"]["src"])
        self.assertEqual(2, result["after"]["loads"])
        self.assertEqual("/retry-video", result["download"])
        self.assertTrue(result["cleared"])

    def test_live_preview_tracks_copy_and_selected_template(self):
        result = self.runtime("livePreview")
        self.assertEqual("实时标题", result["top"])
        self.assertEqual("实时行动文案", result["bottom"])
        self.assertEqual("minimal-headline", result["template"])
        self.assertEqual("#f5f5f2", result["liveBg"])
        self.assertEqual("#111111", result["liveFg"])
        self.assertEqual("#df3f36", result["liveAccent"])
        self.assertEqual("none", result["videoDisplay"])

    def test_action_reminds_missing_copy_without_auth_or_submission(self):
        result = self.runtime("actionPrerequisites")
        self.assertTrue(result["empty"]["enabled"])
        self.assertEqual(
            "请先填写顶部文案和底部行动文案（每项至少 2 个字）",
            result["empty"]["title"],
        )
        self.assertEqual(
            "请先填写顶部文案和底部行动文案（每项至少 2 个字）",
            result["emptyReminder"]["status"],
        )
        self.assertEqual(
            result["emptyReminder"]["status"], result["emptyReminder"]["toast"],
        )
        self.assertEqual((0, 0, 0, 0), tuple(
            result["emptyReminder"][name]
            for name in ("auth", "post", "poll", "confirm")
        ))
        self.assertTrue(result["topOnly"]["enabled"])
        self.assertEqual(
            "请先填写至少 2 个字的底部行动文案",
            result["topOnly"]["title"],
        )
        self.assertEqual(
            "请先填写至少 2 个字的底部行动文案",
            result["topOnlyReminder"]["status"],
        )
        self.assertEqual(
            result["topOnlyReminder"]["status"],
            result["topOnlyReminder"]["toast"],
        )
        self.assertEqual((0, 0, 0, 0), tuple(
            result["topOnlyReminder"][name]
            for name in ("auth", "post", "poll", "confirm")
        ))
        self.assertTrue(result["bottomOnly"]["enabled"])
        self.assertEqual(
            "请先填写至少 2 个字的顶部文案",
            result["bottomOnly"]["title"],
        )
        self.assertEqual(
            "请先填写至少 2 个字的顶部文案",
            result["bottomOnlyReminder"]["status"],
        )
        self.assertEqual(
            result["bottomOnlyReminder"]["status"],
            result["bottomOnlyReminder"]["toast"],
        )
        self.assertEqual((0, 0, 0, 0), tuple(
            result["bottomOnlyReminder"][name]
            for name in ("auth", "post", "poll", "confirm")
        ))
        self.assertTrue(result["complete"]["enabled"])
        self.assertEqual("生成视频 · 5 点", result["complete"]["text"])
        self.assertEqual("", result["complete"]["title"])

    def test_hidden_templates_are_not_rendered_in_the_picker(self):
        result = self.runtime("templateVisibility")
        self.assertEqual(3, result["count"])
        self.assertNotIn("沉浸强标题", result["html"])
        self.assertNotIn("三段式活动海报", result["html"])
        self.assertIn("1. 默认原生大字", result["html"])
        self.assertIn("2. 极简标题", result["html"])
        self.assertIn("3. 参考模板", result["html"])
        self.assertEqual("1. 默认原生大字", result["selectedName"])
        self.assertEqual("native-bold", result["active"])

    def test_pending_hidden_template_still_recovers(self):
        result = self.runtime("hiddenTemplatePendingRecovery")
        self.assertEqual("full-overlay-bold", result["body"]["template_id"])
        self.assertEqual("/hidden-template-video", result["src"])
        self.assertEqual("native-bold", result["active"])
        self.assertTrue(result["cleared"])

    def test_voiceover_submission_uses_personal_voice_and_disables_bgm(self):
        result = self.runtime("voiceoverSubmission")
        self.assertFalse(result["panelHidden"])
        self.assertEqual("我的音色", result["scope"])
        self.assertIn("S_d21F8OR62", result["publicOptions"])
        self.assertIn("vip_alice", result["options"])
        self.assertNotIn("vip_training", result["options"])
        self.assertNotIn("vip_failed", result["options"])
        self.assertEqual("10 / 1000", result["count"])
        self.assertEqual("1.3", result["speed"])
        self.assertEqual("1.3x", result["speedLabel"])
        self.assertFalse(result["body"]["bgm"])
        self.assertEqual({
            "text": "这是一段完整口播文案", "voice": "vip_alice",
            "voice_scope": "personal", "speed": 1.3, "pitch": 0,
            "volume": 0, "delivery": "natural",
        }, result["body"]["voiceover"])

    def test_voiceover_requires_copy_before_auth_or_submission(self):
        result = self.runtime("voiceoverValidation")
        self.assertIn("口播文案", result["status"])
        self.assertEqual(result["status"], result["toast"])
        self.assertEqual((0, 0, 0), (
            result["auth"], result["posts"], result["confirms"],
        ))

    def test_voiceover_pending_state_restores_after_refresh(self):
        result = self.runtime("voiceoverRestore")
        self.assertTrue(result["enabled"])
        self.assertFalse(result["panelHidden"])
        self.assertEqual("恢复后的完整口播", result["text"])
        self.assertEqual("我的音色", result["scope"])
        self.assertEqual("vip_alice", result["voice"])
        self.assertEqual("1.6", result["speed"])
        self.assertEqual("1.6x", result["speedLabel"])
        self.assertEqual(0, result["posts"])
        self.assertEqual("/voiceover-restored-video", result["src"])
        self.assertIn("配音", result["meta"])
        self.assertTrue(result["cleared"])

    def test_frontend_omits_font_selector_and_uses_template_default(self):
        result = self.runtime("automaticFont")
        self.assertFalse(result["fontControl"])
        self.assertFalse(result["fontSource"])
        self.assertNotIn("font_family", result["body"])
        self.assertEqual("native-bold", result["body"]["template_id"])

    def test_locked_reference_template_omits_font_and_keeps_batch_control(self):
        result = self.runtime("lockedTemplateBatch")
        self.assertNotIn("font_family", result["body"])
        self.assertEqual("ref-01-fixture-01", result["body"]["template_id"])
        self.assertFalse(result["batchDisabled"])
        self.assertEqual("5", result["batchValue"])
        self.assertEqual("最多5条", result["batchHint"])
        self.assertEqual(5, result["posts"])
        self.assertTrue(all(body["batch_size"] == 5 for body in result["bodies"]))
        self.assertEqual([1, 2, 3, 4, 5], [
            body["batch_index"] for body in result["bodies"]
        ])

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
        self.assertEqual("最多5条", result["batchHint"])
        self.assertEqual(
            ["1条", "2条", "3条", "4条", "5条"],
            result["batchLabels"],
        )
        self.assertEqual(["metadata"] * 5, result["preloads"])
        self.assertEqual([1] * 5, result["loads"])
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
        self.assertFalse(result["action"]["busy"])
        self.assertTrue(result["action"]["enabled"])
        self.assertEqual("生成视频 · 5 点", result["action"]["text"])

    def test_refund_pending_keeps_polling_until_confirmed(self):
        result = self.runtime("refundPendingThenConfirmed")
        self.assertEqual(2, result["polls"])
        self.assertEqual("退款处理中", result["before"])
        self.assertTrue(result["beforeAction"]["busy"])
        self.assertTrue(result["beforeAction"]["enabled"])
        self.assertEqual("检查任务状态", result["beforeAction"]["text"])
        self.assertEqual("已退款", result["after"])
        self.assertFalse(result["afterAction"]["busy"])
        self.assertTrue(result["afterAction"]["enabled"])
        self.assertEqual("生成视频 · 5 点", result["afterAction"]["text"])
        self.assertEqual("第 1 条生成失败", result["title"])
        self.assertEqual(1, result["cards"])
        self.assertTrue(result["cleared"])

    def test_busy_action_checks_existing_job_without_duplicate_submission(self):
        result = self.runtime("busyActionCheck")
        self.assertTrue(result["before"]["busy"])
        self.assertTrue(result["before"]["enabled"])
        self.assertEqual("检查任务状态", result["before"]["text"])
        self.assertEqual((1, 1), (
            result["during"]["posts"], result["during"]["polls"],
        ))
        self.assertTrue(result["during"]["action"]["busy"])
        self.assertTrue(result["during"]["action"]["enabled"])
        self.assertEqual((1, 1), (result["posts"], result["polls"]))
        self.assertFalse(result["after"]["busy"])
        self.assertTrue(result["after"]["enabled"])
        self.assertEqual("生成视频 · 5 点", result["after"]["text"])
        self.assertTrue(result["cleared"])

    def test_delayed_outer_check_auth_cannot_create_a_new_job(self):
        result = self.runtime("delayedOuterCheckAuth")
        self.assertEqual((1, 1, 1), (
            result["beforeTerminal"]["posts"],
            result["beforeTerminal"]["polls"],
            result["beforeTerminal"]["confirms"],
        ))
        self.assertTrue(result["beforeTerminal"]["action"]["busy"])
        self.assertEqual("/first-video", result["terminal"]["src"])
        self.assertTrue(result["terminal"]["cleared"])
        self.assertFalse(result["terminal"]["action"]["busy"])
        self.assertEqual((1, 1, 1), (
            result["posts"], result["polls"], result["confirms"],
        ))
        self.assertEqual(["matrix-template-uuid-1"], result["keys"])
        self.assertEqual("/first-video", result["src"])
        self.assertFalse(result["action"]["busy"])
        self.assertTrue(result["action"]["enabled"])
        self.assertTrue(result["cleared"])

    def test_preclaimed_submit_flight_blocks_duplicate_during_delayed_auth(self):
        result = self.runtime("delayedPostAuth")
        self.assertEqual((0, 0, 4), (
            result["before"]["posts"], result["before"]["polls"],
            result["before"]["auth"],
        ))
        self.assertTrue(result["before"]["action"]["busy"])
        self.assertEqual((0, 0, 5), (
            result["afterClick"]["posts"], result["afterClick"]["polls"],
            result["afterClick"]["auth"],
        ))
        self.assertTrue(result["afterClick"]["action"]["busy"])
        self.assertTrue(result["afterClick"]["action"]["enabled"])
        self.assertEqual((1, 1), (result["posts"], result["polls"]))
        self.assertEqual(6, result["auth"])
        self.assertEqual("/delayed-auth-post-video", result["src"])
        self.assertFalse(result["after"]["busy"])
        self.assertTrue(result["after"]["enabled"])
        self.assertTrue(result["cleared"])

    def test_preclaimed_poll_flight_blocks_duplicate_during_delayed_auth(self):
        result = self.runtime("delayedPollAuth")
        self.assertEqual((1, 0, 5), (
            result["before"]["posts"], result["before"]["polls"],
            result["before"]["auth"],
        ))
        self.assertTrue(result["before"]["action"]["busy"])
        self.assertEqual((1, 0, 6), (
            result["afterClick"]["posts"], result["afterClick"]["polls"],
            result["afterClick"]["auth"],
        ))
        self.assertTrue(result["afterClick"]["action"]["busy"])
        self.assertEqual((1, 1, 6), (
            result["posts"], result["polls"], result["auth"],
        ))
        self.assertFalse(result["action"]["busy"])
        self.assertTrue(result["action"]["enabled"])
        self.assertEqual("/delayed-auth-poll-video", result["src"])
        self.assertTrue(result["cleared"])

    def test_delayed_submit_auth_honors_job_linked_in_current_storage(self):
        result = self.runtime("linkedJobDuringAuth")
        self.assertEqual((0, 0), (
            result["afterAuth"]["posts"], result["afterAuth"]["polls"],
        ))
        self.assertTrue(result["afterAuth"]["action"]["busy"])
        self.assertEqual((0, 1), (result["posts"], result["polls"]))
        self.assertFalse(result["action"]["busy"])
        self.assertTrue(result["action"]["enabled"])
        self.assertEqual("/linked-job-video", result["src"])
        self.assertTrue(result["cleared"])

    def test_delayed_poll_auth_honors_cleared_current_pending(self):
        result = self.runtime("clearedPendingDuringAuth")
        self.assertEqual(0, result["afterAuth"]["polls"])
        self.assertTrue(result["afterAuth"]["cleared"])
        self.assertEqual(0, result["polls"])
        self.assertFalse(result["action"]["busy"])
        self.assertTrue(result["action"]["enabled"])
        self.assertEqual("生成视频 · 5 点", result["action"]["text"])
        self.assertTrue(result["cleared"])


if __name__ == "__main__":
    unittest.main()
