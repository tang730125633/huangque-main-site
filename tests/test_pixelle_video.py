import importlib
import io
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
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

    def test_all_material_styles_default_people_to_chinese_or_east_asian(self):
        for style in self.pixelle.STYLE_PRESETS:
            prompt_prefix = style["prompt_prefix"]
            self.assertIn("Chinese or East Asian people", prompt_prefix, style["key"])
            self.assertIn(
                "unless the user text explicitly specifies another ethnicity, nationality, or region",
                prompt_prefix,
                style["key"],
            )

    def test_voice_catalog_sanitizes_public_and_ready_owned_personal_voices(self):
        upstream = {
            "items": [
                {"id": "zh-CN-XiaoxiaoNeural", "name": "raw", "locale": "zh-CN", "gender": "female"},
                {"id": "en-US-AriaNeural", "name": "English", "locale": "en-US", "gender": "female"},
                {"id": "../../bad", "name": "Bad", "locale": "zh-CN", "gender": "female"},
            ]
        }
        voices = [
            {"scope": "personal", "username": "alice", "voice_key": "vip_ready", "display_name": "我的音色", "slot_id": "slot-1", "provider_voice": "secret-provider", "preview_url": "/preview.mp3"},
            {"scope": "personal", "username": "alice", "voice_key": "vip_training", "display_name": "训练中", "slot_id": "slot-2", "provider_voice": "secret-provider-2"},
        ]
        def resolve(_username, voice_key):
            if voice_key == "vip_ready":
                return "cosyvoice-secret-provider"
            raise ValueError("not ready")
        with mock.patch.object(self.pixelle, "_json_request", return_value=upstream), \
             mock.patch("content_domains.audio.list_audio_voices", return_value=voices), \
             mock.patch("content_domains.audio.require_owned_ready_personal_voice", side_effect=resolve):
            catalog = self.pixelle.public_voices("alice")

        self.assertEqual([item["id"] for item in catalog], [
            "public:zh-CN-XiaoxiaoNeural", "personal:vip_ready",
        ])
        self.assertEqual(catalog[0]["name"], "女声-温柔（晓晓）")
        self.assertNotIn("provider_voice", str(catalog))
        self.assertNotIn("username", str(catalog))

    def test_prepare_freezes_namespaced_public_and_personal_voice_before_charge(self):
        public_items = [{
            "id": "public:zh-CN-YunjianNeural", "name": "男声-专业（云健）",
            "scope": "public", "gender": "male", "locale": "zh-CN",
        }]
        with mock.patch.object(self.pixelle, "public_voices", return_value=public_items):
            public = self.pixelle.prepare_payload({
                "text": "AI 培训", "voice": "public:zh-CN-YunjianNeural",
            }, "alice")
        self.assertEqual(public["voice_scope"], "public")
        self.assertEqual(public["voice_id"], "zh-CN-YunjianNeural")
        self.assertNotIn("voice_key", public)

        with mock.patch.object(
            self.pixelle.audio_domain, "require_owned_ready_personal_voice",
            return_value="cosyvoice-secret-provider",
        ) as resolve:
            personal = self.pixelle.prepare_payload({
                "text": "第一段\n\n第二段", "mode": "fixed",
                "voice": "personal:vip_ready",
            }, "alice")
        resolve.assert_called_once_with("alice", "vip_ready")
        self.assertEqual(personal["voice_scope"], "personal")
        self.assertEqual(personal["voice_key"], "vip_ready")
        self.assertNotIn("provider_voice", personal)

    def test_prepare_rejects_unknown_or_unowned_voice_before_charge(self):
        with mock.patch.object(self.pixelle, "public_voices", return_value=[]):
            with self.assertRaisesRegex(ValueError, "音色"):
                self.pixelle.prepare_payload({"text": "AI 培训", "voice": "public:bad"}, "alice")
        with mock.patch.object(
            self.pixelle.audio_domain, "require_owned_ready_personal_voice",
            side_effect=ValueError("个人音色不存在或不可用"),
        ):
            with self.assertRaisesRegex(ValueError, "个人音色不存在"):
                self.pixelle.prepare_payload({"text": "AI 培训", "voice": "personal:vip_bob"}, "alice")

    def _talking_plan(self):
        return {
            "plan_id": "talking_plan_" + "a" * 32,
            "source_hash": "b" * 64,
            "source": {
                "text": "原始主题",
                "mode": "generate",
                "ratio": 0.3,
                "template": "1080x1920/image_default.html",
                "style": "realistic_commercial",
                "speech_rate": 1.0,
                "source_page": "text-video",
                "voice_scope": "public",
                "voice_id": "zh-CN-YunjianNeural",
            },
            "scenes": [
                {"scene_id": "scene_01", "text": "确认后的第一段", "role": "hook"},
                {"scene_id": "scene_02", "text": "确认后的第二段", "role": "body"},
                {"scene_id": "scene_03", "text": "确认后的第三段", "role": "cta"},
            ],
            "status": "active",
            "job_id": None,
        }

    def test_prepare_talking_disabled_is_exact_and_backward_compatible(self):
        with mock.patch.object(self.pixelle, "public_voices", return_value=[{
            "id": "public:zh-CN-YunjianNeural", "scope": "public",
        }]), mock.patch.object(
            self.pixelle.pixelle_talking_assets, "get_plan"
        ) as get_plan:
            missing = self.pixelle.prepare_payload({"text": "AI 培训"}, "alice")
            disabled = self.pixelle.prepare_payload({
                "text": "AI 培训",
                "talking_material": {"enabled": False, "plan_id": "ignored"},
            }, "alice")

        self.assertEqual(missing["talking_material"], {"enabled": False})
        self.assertEqual(disabled["talking_material"], {"enabled": False})
        get_plan.assert_not_called()

    def test_prepare_talking_freezes_confirmed_plan_and_only_opaque_local_ids(self):
        plan = self._talking_plan()
        default_id = "local_avatar_" + "1" * 32
        override_id = "local_avatar_" + "2" * 32
        request = {
            "text": "原始主题",
            "mode": "generate",
            "source_page": "text-video",
            "voice": "public:zh-CN-YunjianNeural",
            "talking_material": {
                "enabled": True,
                "plan_id": plan["plan_id"],
                "source_hash": plan["source_hash"],
                "ratio": 0.3,
                "default_avatar_asset_id": default_id,
                "scenes": [
                    {"scene_id": "scene_03", "enabled": True},
                    {"scene_id": "scene_01", "enabled": True,
                     "avatar_asset_id": override_id},
                    {"scene_id": "scene_02", "enabled": False},
                ],
            },
        }
        avatars = {
            default_id: {"asset_id": default_id, "mime": "image/png",
                         "sha256": "c" * 64, "data": b"default"},
            override_id: {"asset_id": override_id, "mime": "image/jpeg",
                          "sha256": "d" * 64, "data": b"override"},
        }
        with mock.patch.object(self.pixelle, "public_voices", return_value=[{
            "id": "public:zh-CN-YunjianNeural", "scope": "public",
        }]), mock.patch.object(
            self.pixelle.pixelle_talking_assets, "get_plan", return_value=plan,
        ), mock.patch.object(
            self.pixelle.pixelle_talking_assets, "read_avatar",
            side_effect=lambda owner, asset_id: avatars[asset_id] if owner == "alice" else None,
        ) as read_avatar, mock.patch.object(
            self.pixelle.pixelle_talking_assets, "bind_plan_avatars",
        ) as bind_avatars:
            prepared = self.pixelle.prepare_payload(request, "alice")

        self.assertEqual(prepared["mode"], "fixed")
        self.assertEqual(prepared["text"],
                         "确认后的第一段\n\n确认后的第二段\n\n确认后的第三段")
        self.assertEqual(prepared["scenes"], [
            {"line": "确认后的第一段", "scene_id": "scene_01"},
            {"line": "确认后的第二段", "scene_id": "scene_02"},
            {"line": "确认后的第三段", "scene_id": "scene_03"},
        ])
        self.assertEqual(prepared["talking_material"], {
            "enabled": True,
            "plan_id": plan["plan_id"],
            "source_hash": plan["source_hash"],
            "ratio": 0.3,
            "default_avatar_asset_id": default_id,
            "scenes": [
                {"scene_id": "scene_01", "enabled": True,
                 "avatar_asset_id": override_id},
                {"scene_id": "scene_03", "enabled": True},
            ],
        })
        self.assertNotIn("username", json.dumps(prepared, ensure_ascii=False))
        self.assertNotIn("file_path", json.dumps(prepared, ensure_ascii=False))
        self.assertEqual(read_avatar.call_count, 2)
        bind_avatars.assert_not_called()

    def test_prepare_talking_requires_the_frozen_ratio_before_charge(self):
        plan = self._talking_plan()
        avatar_id = "local_avatar_" + "1" * 32
        request = {
            "text": "\u539f\u59cb\u4e3b\u9898",
            "mode": "generate",
            "voice": "public:zh-CN-YunjianNeural",
            "talking_material": {
                "enabled": True,
                "plan_id": plan["plan_id"],
                "source_hash": plan["source_hash"],
                "ratio": 0.5,
                "default_avatar_asset_id": avatar_id,
                "scenes": [{"scene_id": "scene_01", "enabled": True}],
            },
        }
        voices = [{"id": "public:zh-CN-YunjianNeural", "scope": "public"}]
        avatar = {
            "asset_id": avatar_id, "mime": "image/png",
            "sha256": "c" * 64, "data": b"avatar",
        }
        with mock.patch.object(self.pixelle, "public_voices", return_value=voices), \
             mock.patch.object(self.pixelle.pixelle_talking_assets, "get_plan",
                               return_value=plan), \
             mock.patch.object(self.pixelle.pixelle_talking_assets, "read_avatar",
                               return_value=avatar) as read_avatar:
            with self.assertRaises(ValueError):
                self.pixelle.prepare_payload(request, "alice")
        read_avatar.assert_not_called()

        request["talking_material"]["ratio"] = 0.30000000000000004
        with mock.patch.object(self.pixelle, "public_voices", return_value=voices), \
             mock.patch.object(self.pixelle.pixelle_talking_assets, "get_plan",
                               return_value=plan), \
             mock.patch.object(self.pixelle.pixelle_talking_assets, "read_avatar",
                               return_value=avatar):
            prepared = self.pixelle.prepare_payload(request, "alice")
        self.assertEqual(prepared["talking_material"]["ratio"], 0.3)

    def test_prepare_talking_rejects_a_consumed_plan_without_mutating_assets(self):
        plan = self._talking_plan()
        plan.update({"status": "consumed", "job_id": 41})
        avatar_id = "local_avatar_" + "1" * 32
        request = {
            "text": "\u539f\u59cb\u4e3b\u9898",
            "mode": "generate",
            "voice": "public:zh-CN-YunjianNeural",
            "talking_material": {
                "enabled": True,
                "plan_id": plan["plan_id"],
                "source_hash": plan["source_hash"],
                "ratio": 0.3,
                "default_avatar_asset_id": avatar_id,
                "scenes": [{"scene_id": "scene_01", "enabled": True}],
            },
        }
        voices = [{"id": "public:zh-CN-YunjianNeural", "scope": "public"}]
        with mock.patch.object(self.pixelle, "public_voices", return_value=voices), \
             mock.patch.object(self.pixelle.pixelle_talking_assets, "get_plan",
                               return_value=plan), \
             mock.patch.object(self.pixelle.pixelle_talking_assets,
                               "read_avatar") as read_avatar, \
             mock.patch.object(self.pixelle.pixelle_talking_assets,
                               "bind_plan_avatars") as bind_avatars:
            with self.assertRaises(ValueError):
                self.pixelle.prepare_payload(request, "alice")
        read_avatar.assert_not_called()
        bind_avatars.assert_not_called()

    def test_prepare_talking_rejects_cross_owner_hash_scene_and_avatar_drift(self):
        plan = self._talking_plan()
        base = {
            "text": "原始主题", "mode": "generate",
            "voice": "public:zh-CN-YunjianNeural",
            "talking_material": {
                "enabled": True, "plan_id": plan["plan_id"],
                "source_hash": plan["source_hash"], "ratio": 0.3,
                "default_avatar_asset_id": "local_avatar_" + "1" * 32,
                "scenes": [{"scene_id": "scene_01", "enabled": True}],
            },
        }
        voices = [{"id": "public:zh-CN-YunjianNeural", "scope": "public"}]
        with mock.patch.object(self.pixelle, "public_voices", return_value=voices), \
             mock.patch.object(self.pixelle.pixelle_talking_assets, "get_plan",
                               side_effect=LookupError("not owned")):
            with self.assertRaises(LookupError):
                self.pixelle.prepare_payload(base, "bob")

        wrong_hash = json.loads(json.dumps(base))
        wrong_hash["talking_material"]["source_hash"] = "f" * 64
        unknown_scene = json.loads(json.dumps(base))
        unknown_scene["talking_material"]["scenes"][0]["scene_id"] = "scene_99"
        no_explicit_scene = json.loads(json.dumps(base))
        no_explicit_scene["talking_material"]["scenes"] = []
        with mock.patch.object(self.pixelle, "public_voices", return_value=voices), \
             mock.patch.object(self.pixelle.pixelle_talking_assets, "get_plan",
                               return_value=plan), \
             mock.patch.object(self.pixelle.pixelle_talking_assets, "read_avatar",
                               return_value={"mime": "image/png", "sha256": "c" * 64,
                                             "data": b"avatar"}):
            for invalid in (wrong_hash, unknown_scene, no_explicit_scene):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    self.pixelle.prepare_payload(invalid, "alice")

        with mock.patch.object(self.pixelle, "public_voices", return_value=voices), \
             mock.patch.object(self.pixelle.pixelle_talking_assets, "get_plan",
                               return_value=plan), \
             mock.patch.object(self.pixelle.pixelle_talking_assets, "read_avatar",
                               side_effect=LookupError("cross owner")):
            with self.assertRaises(LookupError):
                self.pixelle.prepare_payload(base, "alice")

        changed_text = json.loads(json.dumps(base))
        changed_text["text"] = "客户端改写后的主题"
        with mock.patch.object(self.pixelle, "public_voices", return_value=voices), \
             mock.patch.object(self.pixelle.pixelle_talking_assets, "get_plan",
                               return_value=plan):
            with self.assertRaisesRegex(ValueError, "已确认"):
                self.pixelle.prepare_payload(changed_text, "alice")

    def test_remote_talking_material_deduplicates_by_sha_and_reuses_persisted_mapping(self):
        first_id = "local_avatar_" + "1" * 32
        second_id = "local_avatar_" + "2" * 32
        payload = {
            "_username": "alice", "_job_id": 71,
            "talking_material": {
                "enabled": True, "ratio": 0.3,
                "default_avatar_asset_id": first_id,
                "scenes": [
                    {"scene_id": "scene_01", "enabled": True,
                     "avatar_asset_id": second_id},
                ],
            },
        }
        same = {"mime": "image/png", "sha256": "e" * 64, "data": b"same"}
        with mock.patch.object(
            self.pixelle.pixelle_talking_assets, "read_avatar", return_value=same,
        ) as read_avatar, mock.patch.object(
            self.pixelle, "_load_remote_avatar_map", return_value={},
        ), mock.patch.object(
            self.pixelle, "_persist_remote_avatar_map",
        ) as persist, mock.patch.object(
            self.pixelle, "_upload_avatar_asset", return_value="avatar_" + "f" * 32,
        ) as upload:
            remote = self.pixelle._remote_talking_material(payload)

        self.assertEqual(read_avatar.call_count, 2)
        upload.assert_called_once()
        persist.assert_called_once_with(71, {"e" * 64: "avatar_" + "f" * 32})
        self.assertEqual(remote, {
            "enabled": True, "ratio": 0.3,
            "default_avatar_asset_id": "avatar_" + "f" * 32,
            "scenes": [{"scene_id": "scene_01", "enabled": True,
                        "avatar_asset_id": "avatar_" + "f" * 32}],
        })

        with mock.patch.object(
            self.pixelle.pixelle_talking_assets, "read_avatar", return_value=same,
        ), mock.patch.object(
            self.pixelle, "_load_remote_avatar_map",
            return_value={"e" * 64: "avatar_" + "f" * 32},
        ), mock.patch.object(self.pixelle, "_upload_avatar_asset") as retry_upload:
            replay = self.pixelle._remote_talking_material(payload)
        retry_upload.assert_not_called()
        self.assertEqual(replay, remote)

    def test_upload_avatar_retries_only_transient_errors(self):
        avatar = {"mime": "image/png", "sha256": "a" * 64, "data": b"png"}
        with mock.patch.object(
            self.pixelle, "_asset_request",
            side_effect=[self.pixelle.PixelleTransientError("temporary"),
                         {"asset_id": "avatar_" + "a" * 32}],
        ) as request, mock.patch.object(self.pixelle.time, "sleep"):
            result = self.pixelle._upload_avatar_asset(avatar, "text-video-avatar-71-a")
        self.assertEqual(result, "avatar_" + "a" * 32)
        self.assertEqual(request.call_count, 2)

        with mock.patch.object(
            self.pixelle, "_asset_request", side_effect=ValueError("rejected"),
        ) as request:
            with self.assertRaisesRegex(ValueError, "rejected"):
                self.pixelle._upload_avatar_asset(avatar, "text-video-avatar-71-b")
        request.assert_called_once()

    def test_avatar_request_classifies_4xx_and_5xx_without_leaking_response(self):
        def http_error(code, detail):
            return urllib.error.HTTPError(
                "http://pixelle/api/avatar-assets", code, "failed", {},
                io.BytesIO(json.dumps({"detail": detail}).encode("utf-8")),
            )

        for code in (500, 503, 429):
            with self.subTest(code=code), mock.patch.object(
                self.pixelle._NO_PROXY, "open",
                side_effect=http_error(code, "secret upstream detail"),
            ), self.assertRaises(self.pixelle.PixelleTransientError) as raised:
                self.pixelle._asset_request(
                    "/api/avatar-assets", b"image", "image/png", "req-1")
            self.assertNotIn("secret", str(raised.exception))

        with mock.patch.object(
            self.pixelle._NO_PROXY, "open",
            side_effect=http_error(413, "too large secret"),
        ), self.assertRaisesRegex(ValueError, "超过"):
            self.pixelle._asset_request(
                "/api/avatar-assets", b"image", "image/png", "req-2")
        with mock.patch.object(
            self.pixelle._NO_PROXY, "open",
            side_effect=http_error(400, "invalid image secret"),
        ), self.assertRaisesRegex(ValueError, "HTTP 400"):
            self.pixelle._asset_request(
                "/api/avatar-assets", b"image", "image/png", "req-3")

    def test_remote_mapping_persists_each_success_before_later_upload_failure(self):
        first_id = "local_avatar_" + "1" * 32
        second_id = "local_avatar_" + "2" * 32
        payload = {
            "_username": "alice", "_job_id": 74,
            "talking_material": {
                "enabled": True, "ratio": 0.5,
                "default_avatar_asset_id": first_id,
                "scenes": [{"scene_id": "scene_01", "enabled": True,
                            "avatar_asset_id": second_id}],
            },
        }
        avatars = {
            first_id: {"mime": "image/png", "sha256": "1" * 64,
                       "data": b"first"},
            second_id: {"mime": "image/jpeg", "sha256": "2" * 64,
                        "data": b"second"},
        }
        persisted = {}

        def persist(_job_id, mapping):
            persisted.update(mapping)

        with mock.patch.object(
            self.pixelle.pixelle_talking_assets, "read_avatar",
            side_effect=lambda _owner, asset_id: avatars[asset_id],
        ), mock.patch.object(
            self.pixelle, "_load_remote_avatar_map", side_effect=lambda _job: dict(persisted),
        ), mock.patch.object(
            self.pixelle, "_persist_remote_avatar_map", side_effect=persist,
        ), mock.patch.object(
            self.pixelle, "_upload_avatar_asset",
            side_effect=["avatar_" + "a" * 32,
                         self.pixelle.PixelleTransientError("second failed")],
        ) as upload:
            with self.assertRaises(self.pixelle.PixelleTransientError):
                self.pixelle._remote_talking_material(payload)

        self.assertEqual(persisted, {"1" * 64: "avatar_" + "a" * 32})
        self.assertEqual(upload.call_count, 2)

        with mock.patch.object(
            self.pixelle.pixelle_talking_assets, "read_avatar",
            side_effect=lambda _owner, asset_id: avatars[asset_id],
        ), mock.patch.object(
            self.pixelle, "_load_remote_avatar_map", return_value=dict(persisted),
        ), mock.patch.object(
            self.pixelle, "_persist_remote_avatar_map", side_effect=persist,
        ), mock.patch.object(
            self.pixelle, "_upload_avatar_asset", return_value="avatar_" + "b" * 32,
        ) as retry_upload:
            remote = self.pixelle._remote_talking_material(payload)
        retry_upload.assert_called_once()
        self.assertEqual(remote["default_avatar_asset_id"], "avatar_" + "a" * 32)
        self.assertEqual(remote["scenes"][0]["avatar_asset_id"],
                         "avatar_" + "b" * 32)

    def test_remote_avatar_mapping_storage_closes_sqlite_connections(self):
        class FakeConnection:
            def __init__(self, row):
                self.row = row
                self.closed = False

            def execute(self, *_args):
                return self

            def fetchone(self):
                return self.row

            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                self.closed = True

        remote_id = "avatar_" + "a" * 32
        digest = "b" * 64
        reader = FakeConnection((json.dumps({
            "_pixelle_remote_avatar_assets": {digest: remote_id},
        }),))
        writer = FakeConnection((json.dumps({}),))
        with mock.patch.object(self.pixelle.sqlite3, "connect",
                               side_effect=[reader, writer]):
            self.assertEqual(
                self.pixelle._load_remote_avatar_map(17), {digest: remote_id})
            self.pixelle._persist_remote_avatar_map(17, {digest: remote_id})
        self.assertTrue(reader.closed)
        self.assertTrue(writer.closed)

    def test_paid_talking_plan_association_is_deferred_to_job_transaction(self):
        payload = {"talking_material": {
            "enabled": True,
            "plan_id": "talking_plan_" + "a" * 32,
            "source_hash": "b" * 64,
        }}
        callback = self.pixelle.paid_plan_association(payload, "alice")
        connection = object()
        with mock.patch.object(
            self.pixelle.pixelle_talking_assets, "consume_and_bind_paid_plan",
        ) as associate:
            callback(connection, 72)
        associate.assert_called_once_with(
            connection, "alice", payload["talking_material"]["plan_id"],
            payload["talking_material"]["source_hash"], 72)

        self.assertIsNone(self.pixelle.paid_plan_association(
            {"talking_material": {"enabled": False}}, "alice"))

    def test_paid_plan_association_commits_atomically_and_rolls_back_on_mismatch(self):
        from content_domains import jobs_store

        with tempfile.TemporaryDirectory() as temp:
            db_path = str(Path(temp) / "jobs.db")
            out_dir = Path(temp) / "out"

            def database():
                connection = sqlite3.connect(db_path, timeout=30)
                connection.row_factory = sqlite3.Row
                return connection

            with mock.patch.object(self.pixelle.pixelle_talking_assets,
                                   "DB_PATH", db_path), mock.patch.object(
                self.pixelle.pixelle_talking_assets, "OUT_DIR", out_dir,
            ):
                self.pixelle.pixelle_talking_assets.init_db(db_path, out_dir)
                with closing(database()) as connection:
                    connection.execute("""CREATE TABLE jobs(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL, username TEXT NOT NULL,
                        cost INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                        payload TEXT, created_at INTEGER, updated_at INTEGER,
                        owner TEXT, refunded INTEGER NOT NULL DEFAULT 0
                    )""")
                plan = self.pixelle.pixelle_talking_assets.create_plan(
                    "alice", {"text": "主题"},
                    [{"scene_id": "ignored", "text": "确认段落"}],
                )
                payload = {
                    "pipeline": "pixelle",
                    "talking_material": {
                        "enabled": True, "plan_id": plan["plan_id"],
                        "source_hash": plan["source_hash"],
                    },
                }
                refunds = []
                job_id, _points = jobs_store.create_paid_job(
                    database, lambda *_args: 99,
                    lambda *args, **_kwargs: refunds.append(args) or True,
                    "script_to_video", "alice", 10, payload, "content",
                    before_commit=self.pixelle.paid_plan_association(payload, "alice"),
                )
                stored = self.pixelle.pixelle_talking_assets.get_plan(
                    "alice", plan["plan_id"])
                self.assertEqual(stored["status"], "consumed")
                self.assertEqual(stored["job_id"], job_id)
                self.assertEqual(refunds, [])

                second = self.pixelle.pixelle_talking_assets.create_plan(
                    "alice", {"text": "主题二"},
                    [{"scene_id": "ignored", "text": "第二段"}],
                )
                bad_payload = {
                    "pipeline": "pixelle",
                    "talking_material": {
                        "enabled": True, "plan_id": second["plan_id"],
                        "source_hash": "f" * 64,
                    },
                }
                try:
                    jobs_store.create_paid_job(
                        database, lambda *_args: 88,
                        lambda *args, **_kwargs: refunds.append(args) or True,
                        "script_to_video", "alice", 10, bad_payload, "content",
                        before_commit=self.pixelle.paid_plan_association(
                            bad_payload, "alice"),
                    )
                except jobs_store.PaidJobInsertError:
                    pass
                else:
                    self.fail("mismatched plan hash must abort the paid job")
                unchanged = self.pixelle.pixelle_talking_assets.get_plan(
                    "alice", second["plan_id"])
                self.assertEqual(unchanged["status"], "active")
                self.assertIsNone(unchanged["job_id"])
                self.assertEqual(len(refunds), 1)

    def test_submit_public_talking_uses_exact_confirmed_fixed_narration(self):
        payload = {
            "text": "确认第一段\n\n确认第二段", "mode": "fixed",
            "template": "1080x1920/image_default.html",
            "style": "realistic_commercial", "n_scenes": 2,
            "scenes": [
                {"line": "确认第一段", "scene_id": "scene_01"},
                {"line": "确认第二段", "scene_id": "scene_02"},
            ],
            "speech_rate": 1.0, "voice_scope": "public",
            "voice_id": "zh-CN-YunjianNeural",
            "talking_material": {"enabled": True},
        }
        remote = {
            "enabled": True, "ratio": 0.5,
            "default_avatar_asset_id": "avatar_" + "a" * 32,
            "scenes": [{"scene_id": "scene_01", "enabled": True,
                        "avatar_asset_id": ""}],
        }
        with mock.patch.object(
            self.pixelle, "_remote_talking_material", return_value=remote,
        ), mock.patch.object(
            self.pixelle, "_json_request", return_value={"task_id": "task-public-fixed"},
        ) as request, mock.patch.object(
            self.pixelle, "_personal_narrations",
            side_effect=AssertionError("public fixed narration must not re-plan"),
        ):
            self.assertEqual(self.pixelle._submit(payload), "task-public-fixed")
        body = request.call_args.args[2]
        self.assertEqual(body["text"], "确认第一段\n\n确认第二段")
        self.assertEqual(body["mode"], "fixed")
        self.assertEqual(body["n_scenes"], 2)
        self.assertEqual(body["talking_material"], remote)

    def test_nine_scene_public_adapter_uploads_two_avatars_without_extra_tts(self):
        avatar_a = "local_avatar_" + "a" * 32
        avatar_b = "local_avatar_" + "b" * 32
        remote_a = "avatar_" + "1" * 32
        remote_b = "avatar_" + "2" * 32
        scene_texts = [
            "第01段确认文案", "第02段确认文案", "第03段确认文案",
            "第04段确认文案", "第05段确认文案", "第06段确认文案",
            "第07段确认文案", "第08段确认文案", "第09段确认文案",
        ]
        plan = {
            "plan_id": "talking_plan_" + "c" * 32,
            "source_hash": "d" * 64,
            "status": "active",
            "job_id": None,
            "source": {
                "text": "九分镜口播方案", "mode": "generate", "ratio": 1 / 3,
                "template": "1080x1920/image_default.html",
                "style": "realistic_commercial", "speech_rate": 1.0,
                "source_page": "text-video", "voice_scope": "public",
                "voice_id": "zh-CN-YunjianNeural",
            },
            "scenes": [
                {"scene_id": "scene_%02d" % index, "text": text}
                for index, text in enumerate(scene_texts, 1)
            ],
        }
        request = {
            "text": "九分镜口播方案", "mode": "generate",
            "template": "1080x1920/image_default.html",
            "style": "realistic_commercial", "speech_rate": 1.0,
            "voice": "public:zh-CN-YunjianNeural",
            "talking_material": {
                "enabled": True, "plan_id": plan["plan_id"],
                "source_hash": plan["source_hash"], "ratio": 1 / 3,
                "default_avatar_asset_id": avatar_a,
                "scenes": [
                    {
                        "scene_id": "scene_%02d" % index,
                        "enabled": index in {1, 5, 9},
                        **({"avatar_asset_id": avatar_b} if index == 5 else {}),
                    }
                    for index in range(1, 10)
                ],
            },
        }
        avatars = {
            avatar_a: {"asset_id": avatar_a, "mime": "image/png",
                       "sha256": "a" * 64, "data": b"avatar-a"},
            avatar_b: {"asset_id": avatar_b, "mime": "image/jpeg",
                       "sha256": "b" * 64, "data": b"avatar-b"},
        }
        voices = [{"id": "public:zh-CN-YunjianNeural", "scope": "public"}]
        with mock.patch.object(
            self.pixelle, "public_voices", return_value=voices,
        ), mock.patch.object(
            self.pixelle.pixelle_talking_assets, "get_plan", return_value=plan,
        ), mock.patch.object(
            self.pixelle.pixelle_talking_assets, "read_avatar",
            side_effect=lambda _owner, asset_id: avatars[asset_id],
        ), mock.patch.object(
            self.pixelle, "_load_remote_avatar_map", return_value={},
        ), mock.patch.object(
            self.pixelle, "_persist_remote_avatar_map",
        ), mock.patch.object(
            self.pixelle, "_upload_avatar_asset", side_effect=[remote_a, remote_b],
        ) as upload, mock.patch.object(
            self.pixelle.audio_domain, "synthesize_owned_voice_segment",
        ) as synth, mock.patch.object(
            self.pixelle, "_json_request", return_value={"task_id": "task-public-9"},
        ) as submit:
            prepared = self.pixelle.prepare_payload(request, "alice")
            prepared["_job_id"] = 79
            self.assertEqual(self.pixelle._submit(prepared), "task-public-9")

        video_body = submit.call_args.args[2]
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(synth.call_count, 0)
        self.assertEqual(video_body["text"], "\n\n".join(scene_texts))
        self.assertEqual(video_body["mode"], "fixed")
        self.assertEqual(video_body["n_scenes"], 9)
        self.assertEqual(video_body["talking_material"], {
            "enabled": True, "ratio": 0.333333,
            "default_avatar_asset_id": remote_a,
            "scenes": [
                {"scene_id": "scene_01", "enabled": True,
                 "avatar_asset_id": ""},
                {"scene_id": "scene_05", "enabled": True,
                 "avatar_asset_id": remote_b},
                {"scene_id": "scene_09", "enabled": True,
                 "avatar_asset_id": ""},
            ],
        })

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

    def test_talking_plan_fixed_freezes_stable_scenes_and_recommendations(self):
        payload = {
            "text": "开场钩子\n\n核心观点一\n\n核心观点二\n\n行动引导",
            "mode": "fixed", "ratio": 0.5,
            "template": "1080x1920/image_default.html",
            "style": "realistic_commercial",
            "voice": "public:zh-CN-YunjianNeural", "speech_rate": 1.0,
        }
        stored = {"plan_id": "talking_plan_" + "a" * 32,
                  "source_hash": "b" * 64, "scenes": []}

        def create_plan(_username, _source, scenes):
            stored["scenes"] = [dict(scene, scene_id="scene_%02d" % index)
                                for index, scene in enumerate(scenes, 1)]
            return stored

        with mock.patch.object(self.pixelle, "public_voices", return_value=[{
            "id": "public:zh-CN-YunjianNeural", "scope": "public",
        }]), mock.patch(
            "content_domains.pixelle_talking_assets.create_plan",
            side_effect=create_plan,
        ) as create, mock.patch.object(self.pixelle, "_json_request") as request:
            result = self.pixelle.plan_talking_scenes(payload, "alice")

        request.assert_not_called()
        create.assert_called_once()
        self.assertEqual([scene["scene_id"] for scene in result["scenes"]], [
            "scene_01", "scene_02", "scene_03", "scene_04",
        ])
        self.assertEqual([scene["role"] for scene in result["scenes"]], [
            "hook", "body", "body", "cta",
        ])
        self.assertEqual(
            [scene["scene_id"] for scene in result["scenes"]
             if scene["talking_recommended"]],
            ["scene_01", "scene_04"],
        )
        self.assertTrue(all(
            isinstance(scene["estimated_duration"], float)
            and scene["estimated_duration"] > 0
            for scene in result["scenes"]
        ))

    def test_talking_plan_generate_calls_narration_planner_once_and_never_render(self):
        payload = {
            "text": "AI培训如何提升团队效率", "mode": "generate", "ratio": 0.3,
            "template": "1080x1920/image_default.html", "style": "future_tech",
            "voice": "public:zh-CN-YunjianNeural", "speech_rate": 1.2,
        }
        planner_result = {"narrations": [
            "先看一个真实问题", "把重复工作交给AI", "现在开始行动",
        ]}

        def create_plan(_username, _source, scenes):
            return {
                "plan_id": "talking_plan_" + "c" * 32,
                "source_hash": "d" * 64,
                "scenes": [dict(scene, scene_id="scene_%02d" % index)
                           for index, scene in enumerate(scenes, 1)],
            }

        with mock.patch.object(self.pixelle, "public_voices", return_value=[{
            "id": "public:zh-CN-YunjianNeural", "scope": "public",
        }]), mock.patch.object(
            self.pixelle, "_json_request", return_value=planner_result,
        ) as request, mock.patch(
            "content_domains.pixelle_talking_assets.create_plan",
            side_effect=create_plan,
        ), mock.patch.object(self.pixelle, "_submit") as submit:
            result = self.pixelle.plan_talking_scenes(payload, "alice")

        request.assert_called_once()
        self.assertEqual(request.call_args.args[:2], ("POST", "/api/content/narration"))
        submit.assert_not_called()
        self.assertEqual(len(result["scenes"]), 3)

    def test_talking_plan_duration_tracks_selected_speech_rate(self):
        base = {
            "text": "这是一段包含二十四个左右汉字用于测试语速时长估算的文案",
            "mode": "fixed", "ratio": 0.3,
            "voice": "public:zh-CN-YunjianNeural",
        }
        durations = []

        def create_plan(_username, _source, scenes):
            durations.append(scenes[0]["estimated_duration"])
            return {"plan_id": "talking_plan_" + "e" * 32,
                    "source_hash": "f" * 64,
                    "scenes": [dict(scenes[0], scene_id="scene_01")]}

        with mock.patch.object(self.pixelle, "public_voices", return_value=[{
            "id": "public:zh-CN-YunjianNeural", "scope": "public",
        }]), mock.patch(
            "content_domains.pixelle_talking_assets.create_plan",
            side_effect=create_plan,
        ):
            self.pixelle.plan_talking_scenes(dict(base, speech_rate=0.8), "alice")
            self.pixelle.plan_talking_scenes(dict(base, speech_rate=1.6), "alice")
        self.assertGreater(durations[0], durations[1])
        self.assertEqual(durations, [round(value, 1) for value in durations])

    def test_talking_plan_rejects_ratio_outside_contract(self):
        with self.assertRaisesRegex(ValueError, "10%-50%"):
            self.pixelle.plan_talking_scenes({
                "text": "有效文案", "mode": "fixed", "ratio": 0.6,
                "voice": "public:zh-CN-YunjianNeural",
            }, "alice")

    def test_talking_recommendations_match_generation_service_contract(self):
        scenes = [{"scene_id": "scene_%02d" % index}
                  for index in range(1, 8)]
        self.assertEqual(
            self.pixelle._recommended_scene_ids(scenes, 0.5),
            ["scene_01", "scene_07", "scene_04", "scene_02"],
        )

    def test_talking_duration_targets_six_seconds_without_hard_clamp(self):
        self.assertEqual(
            self.pixelle._estimated_scene_duration("中" * 24, 1.0), 6.0)
        self.assertEqual(
            self.pixelle._estimated_scene_duration("长" * 40, 1.0), 10.0)

    def test_talking_plan_rate_guard_is_owner_scoped(self):
        self.pixelle._PLAN_RATE_REQUESTS.clear()
        for index in range(self.pixelle._PLAN_RATE_MAX_REQUESTS):
            self.pixelle.check_plan_rate_limit("alice", now=float(index))
        with self.assertRaisesRegex(RuntimeError, "planning_rate_limited"):
            self.pixelle.check_plan_rate_limit("alice", now=10.0)
        self.pixelle.check_plan_rate_limit("bob", now=10.0)
        self.pixelle._PLAN_RATE_REQUESTS.clear()

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

    def test_prepare_defaults_normalizes_and_rejects_invalid_speech_rate(self):
        default = self.pixelle.prepare_payload({"text": "AI 培训"})
        normalized = self.pixelle.prepare_payload({
            "text": "AI 培训",
            "speech_rate": 1.26,
        })
        half_up = self.pixelle.prepare_payload({
            "text": "AI 培训",
            "speech_rate": 1.25,
        })
        self.assertEqual(default["speech_rate"], 1.0)
        self.assertEqual(normalized["speech_rate"], 1.3)
        self.assertEqual(half_up["speech_rate"], 1.3)

        invalid_values = [True, "1.1", float("nan"), float("inf"), float("-inf"), 0.4, 2.1]
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.pixelle.prepare_payload({
                    "text": "AI 培训",
                    "speech_rate": value,
                })

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
        self.assertEqual(body["tts_speed"], 1.0)

    def test_submit_normalizes_speech_rate_at_execution_boundary(self):
        legacy_payload = {
            "text": "AI training",
            "mode": "generate",
            "template": "1080x1920/image_default.html",
            "n_scenes": 5,
            "speech_rate": 1.26,
            "voice_scope": "public",
            "voice_id": "zh-CN-YunjianNeural",
        }
        with mock.patch.object(
            self.pixelle, "_json_request", return_value={"task_id": "task-normalized"}
        ) as request:
            self.assertEqual(self.pixelle._submit(legacy_payload), "task-normalized")

        body = request.call_args.args[2]
        self.assertEqual(body["tts_speed"], 1.3)
        self.assertEqual(body["voice_id"], "zh-CN-YunjianNeural")

    def test_submit_rejects_invalid_speech_rate_at_execution_boundary(self):
        invalid_values = [True, "1.1", float("nan"), float("inf"), float("-inf"), 0.4, 2.1]
        for value in invalid_values:
            legacy_payload = {
                "text": "AI training",
                "mode": "generate",
                "template": "1080x1920/image_default.html",
                "n_scenes": 5,
                "speech_rate": value,
            }
            with self.subTest(value=value), \
                 mock.patch.object(self.pixelle, "_json_request") as request, \
                 self.assertRaisesRegex(ValueError, "语速值"):
                self.pixelle._submit(legacy_payload)
            request.assert_not_called()

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

    def test_submit_public_voice_uses_resolved_upstream_voice_id(self):
        payload = self.pixelle.prepare_payload({"text": "AI 培训"})
        payload.update({"voice_scope": "public", "voice_id": "zh-CN-YunjianNeural"})
        with mock.patch.object(
            self.pixelle, "_json_request", return_value={"task_id": "task-public"}
        ) as request:
            self.assertEqual(self.pixelle._submit(payload), "task-public")
        body = request.call_args.args[2]
        self.assertEqual(body["voice_id"], "zh-CN-YunjianNeural")
        self.assertEqual(body["tts_workflow"], self.pixelle.PIXELLE_TTS_WORKFLOW)
        self.assertEqual(body["tts_speed"], 1.0)
        self.assertNotIn("narration_segments", body)

    def test_submit_personal_voice_plans_synthesizes_and_uploads_each_segment(self):
        payload = {
            "text": "AI 培训",
            "mode": "generate",
            "template": "1080x1920/image_default.html",
            "style": "realistic_commercial",
            "n_scenes": 5,
            "speech_rate": 1.44,
            "voice_scope": "personal", "voice_key": "vip_ready", "_username": "alice",
            "_job_id": 52,
        }
        responses = {
            "/api/content/narration": {"narrations": ["第一句", "第二句"]},
            "/api/video/generate/async": {"task_id": "task-personal"},
        }
        def fake_json(method, path, body=None, timeout=30):
            return responses[path]
        synth_results = [
            {"content": b"ID3-one", "content_type": "audio/mpeg"},
            {"content": b"ID3-two", "content_type": "audio/mpeg"},
        ]
        upload_results = [
            {"asset_id": "audio_" + "a" * 32},
            {"asset_id": "audio_" + "b" * 32},
        ]
        with mock.patch.object(self.pixelle, "_json_request", side_effect=fake_json) as request, \
             mock.patch.object(self.pixelle.audio_domain, "synthesize_owned_voice_segment", side_effect=synth_results) as synth, \
             mock.patch.object(self.pixelle, "_binary_request", side_effect=upload_results) as upload:
            self.assertEqual(self.pixelle._submit(payload), "task-personal")

        self.assertEqual(synth.call_count, 2)
        self.assertEqual(
            synth.call_args_list,
            [
                mock.call("alice", "vip_ready", "第一句", speed=1.4),
                mock.call("alice", "vip_ready", "第二句", speed=1.4),
            ],
        )
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(upload.call_args_list[0].args[3], "text-video-52-0")
        video_body = request.call_args_list[-1].args[2]
        self.assertEqual(video_body["mode"], "fixed")
        self.assertEqual(video_body["text"], "第一句\n\n第二句")
        self.assertEqual(video_body["narration_segments"], [
            {
                "text": "第一句",
                "audio_asset_id": "audio_" + "a" * 32,
                "caption_cues": [{"text": "第一句"}],
            },
            {
                "text": "第二句",
                "audio_asset_id": "audio_" + "b" * 32,
                "caption_cues": [{"text": "第二句"}],
            },
        ])
        self.assertNotIn("voice_id", video_body)
        self.assertNotIn("tts_speed", video_body)
        self.assertNotIn("tts_workflow", video_body)

    def test_personal_voice_splits_long_scene_without_adding_scenes(self):
        scene_text = "这是第一段需要按语音拆分轮播显示的字幕内容，而且素材只能生成一次。"
        payload = {
            "text": scene_text,
            "mode": "fixed",
            "template": "1080x1920/image_default.html",
            "style": "realistic_commercial",
            "n_scenes": 1,
            "scenes": [{"line": scene_text}],
            "speech_rate": 1.0,
            "voice_scope": "personal",
            "voice_key": "vip_ready",
            "_username": "alice",
            "_job_id": 53,
        }
        asset_id = "audio_" + "1" * 32

        with mock.patch.object(
            self.pixelle.audio_domain,
            "synthesize_owned_voice_segment",
            return_value={"content": b"ID3", "content_type": "audio/mpeg"},
        ) as synth, mock.patch.object(
            self.pixelle,
            "_binary_request",
            return_value={"asset_id": asset_id},
        ), mock.patch.object(
            self.pixelle,
            "_json_request",
            return_value={"task_id": "task-personal-long"},
        ) as request:
            self.assertEqual(self.pixelle._submit(payload), "task-personal-long")

        body = request.call_args.args[2]
        self.assertEqual(body["n_scenes"], 1)
        self.assertEqual(body["text"], scene_text)
        self.assertEqual(len(body["narration_segments"]), 1)
        self.assertEqual(
            "".join(
                cue["text"]
                for cue in body["narration_segments"][0]["caption_cues"]
            ),
            scene_text,
        )
        self.assertEqual(
            body["narration_segments"][0]["audio_asset_id"], asset_id
        )
        self.assertGreater(len(body["narration_segments"][0]["caption_cues"]), 1)
        self.assertTrue(all(
            self.pixelle._display_units(cue["text"]) <= 28
            for cue in body["narration_segments"][0]["caption_cues"]
        ))
        synth.assert_called_once_with(
            "alice", "vip_ready", scene_text, speed=1.0
        )

    def test_caption_splitter_packs_short_fragments_before_cue_limit(self):
        for text in (
            "a " * 21,
            "一，" * 21,
            "一。" * 21,
            " ".join(["word"] * 50),
        ):
            with self.subTest(text=text):
                cues = self.pixelle._split_caption_text(text)
                self.assertEqual("".join(cues), text)
                self.assertLessEqual(len(cues), 20)
                self.assertTrue(all(
                    self.pixelle._display_units(cue) <= 28 for cue in cues
                ))

    def test_caption_splitter_balances_unpunctuated_chinese_without_orphans(self):
        for text in (
            "人工智能正在改变我们生活的方方面面",
            "这也引发了关于隐私和伦理的讨论",
        ):
            with self.subTest(text=text):
                cues = self.pixelle._split_caption_text(text)
                self.assertEqual(text, "".join(cues))
                self.assertGreater(len(cues), 1)
                self.assertGreaterEqual(min(len(cue) for cue in cues), 6)
                self.assertTrue(all(
                    self.pixelle._display_units(cue) <= 28 for cue in cues
                ))

    def test_caption_cues_follow_real_word_timestamps_instead_of_character_ratio(self):
        text = "人工智能可以改变我们的生活方式"
        cue_texts = ["人工智能可以", "改变我们的生活方", "式"]
        words = [
            {"text": "人工智能", "start": 0.2, "end": 0.8},
            {"text": "可以", "start": 0.85, "end": 1.1},
            {"text": "改变", "start": 1.8, "end": 2.1},
            {"text": "我们的", "start": 2.1, "end": 2.6},
            {"text": "生活方式", "start": 2.7, "end": 3.5},
        ]

        cues = self.pixelle._caption_cues_from_word_timestamps(
            text, cue_texts, words, 3.8
        )

        self.assertEqual(0.0, cues[0]["start_time"])
        self.assertEqual(3.8, cues[-1]["end_time"])
        self.assertGreater(cues[0]["end_time"], 1.1)
        self.assertEqual(
            [cue["end_time"] for cue in cues[:-1]],
            [cue["start_time"] for cue in cues[1:]],
        )

    def test_caption_alignment_rejects_low_coverage_recognition(self):
        text = "人工智能正在改变我们生活的方方面面"
        cue_texts = self.pixelle._split_caption_text(text)
        with self.assertRaisesRegex(ValueError, "识别内容与原文不匹配"):
            self.pixelle._caption_cues_from_word_timestamps(
                text,
                cue_texts,
                [{"text": "天气", "start": 0.1, "end": 0.35}],
                8.0,
            )

    def test_caption_alignment_rejects_unrelated_recognition_of_similar_length(self):
        text = "人工智能正在改变我们生活的方方面面"
        cue_texts = self.pixelle._split_caption_text(text)
        with self.assertRaisesRegex(ValueError, "识别内容与原文不匹配"):
            self.pixelle._caption_cues_from_word_timestamps(
                text,
                cue_texts,
                [{
                    "text": "春夏秋冬东西南北日月星辰山川河流",
                    "start": 0.1,
                    "end": 7.5,
                }],
                8.0,
            )

    def test_caption_alignment_mismatch_falls_back_to_display_only_cues(self):
        class Word:
            word = "春夏秋冬东西南北日月星辰山川河流"
            start = 0.1
            end = 7.5

        class Segment:
            words = [Word()]

        class Model:
            @staticmethod
            def transcribe(*_args, **_kwargs):
                return [Segment()], None

        video_domain = importlib.import_module("content_domains.video")
        text = "人工智能正在改变我们生活的方方面面"
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.pixelle,
            "_CAPTION_ALIGN_TMP_ROOT",
            Path(directory) / "caption-private",
        ), mock.patch.object(
            self.pixelle.subprocess,
            "run",
            return_value=mock.Mock(stdout="8.0\n"),
        ), mock.patch.object(
            video_domain,
            "_get_whisper_model",
            return_value=Model(),
        ):
            cues = self.pixelle._aligned_caption_cues(text, b"ID3-test")

        self.assertEqual(text, "".join(cue["text"] for cue in cues))
        self.assertTrue(all(set(cue) == {"text"} for cue in cues))

    def test_caption_alignment_audio_uses_private_atomic_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "caption-private"
            with mock.patch.object(self.pixelle, "_CAPTION_ALIGN_TMP_ROOT", root):
                path = self.pixelle._write_private_caption_alignment_audio(b"ID3-test")
            try:
                self.assertEqual(path.parent, root)
                self.assertEqual(path.read_bytes(), b"ID3-test")
                self.assertFalse(path.resolve().is_relative_to(self.pixelle.OUT_DIR.resolve()))
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                path.unlink(missing_ok=True)

    def test_caption_alignment_temp_cleanup_only_removes_stale_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "caption-private"
            root.mkdir()
            stale = root / "caption-align-stale.mp3"
            fresh = root / "caption-align-fresh.mp3"
            unrelated = root / "keep.txt"
            for path in (stale, fresh, unrelated):
                path.write_bytes(b"test")
            os.utime(stale, (100.0, 100.0))
            os.utime(fresh, (3900.0, 3900.0))
            with mock.patch.object(self.pixelle, "_CAPTION_ALIGN_TMP_ROOT", root):
                self.pixelle._cleanup_stale_caption_alignment_files(now=4000.0)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())

    def test_caption_alignment_failure_keeps_legacy_display_cues(self):
        text = "这是第一段需要按语音拆分轮播显示的字幕内容。"
        with mock.patch.object(
            self.pixelle.subprocess,
            "run",
            side_effect=OSError("ffprobe unavailable"),
        ):
            cues = self.pixelle._aligned_caption_cues(text, b"ID3-test")

        self.assertEqual(text, "".join(cue["text"] for cue in cues))
        self.assertTrue(all(set(cue) == {"text"} for cue in cues))

    def test_caption_splitter_supports_long_scenes_up_to_one_hundred_cues(self):
        self.assertEqual(len(self.pixelle._split_caption_text("一，" * 141)), 21)
        self.assertEqual(len(self.pixelle._split_caption_text("一，" * 700)), 100)
        with self.assertRaisesRegex(ValueError, "字幕片段过多"):
            self.pixelle._split_caption_text("一，" * 701)

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

        with mock.patch.object(self.pixelle, "_json_request", return_value={
            "status": "failed", "error": "RunningHub API error: TASK_QUEUE_MAXED",
        }):
            with self.assertRaisesRegex(RuntimeError, "TASK_QUEUE_MAXED"):
                self.pixelle._wait("task-capacity")
        with mock.patch.object(
            self.pixelle.feature_flags, "is_enabled", return_value=True
        ), mock.patch.object(self.pixelle, "_json_request") as request:
            self.assertEqual(self.pixelle.availability(force=True), {
                "enabled": True, "ready": False, "available": False,
            })
        request.assert_not_called()
        self.pixelle._CAPACITY_BLOCKED_UNTIL = 0.0

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

    def test_wait_retries_remote_disconnect_until_task_completes(self):
        class JsonResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"status":"completed","result":{"video_url":"/api/files/result.mp4"}}'

        with mock.patch.object(
            self.pixelle._NO_PROXY, "open", side_effect=[
                http.client.RemoteDisconnected("server disconnected"), JsonResponse(),
            ],
        ) as request, mock.patch.object(self.pixelle.time, "sleep") as sleep:
            result = self.pixelle._wait("task-disconnected")

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
        payload = self.pixelle.prepare_payload({
            "text": "AI 培训", "mode": "generate", "source_page": "text-video",
        })
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
        self.assertEqual(payload["source_page"], "text-video")
        self.assertEqual(payload["provider"], "pixelle")
        self.assertEqual(result["provider_task_id"], "task-42")
        self.assertEqual(result["provider_video_id"], "task-42")
        self.assertEqual((result["status"], result["mode"]), ("done", "generate"))
        self.assertNotIn("prompt_prefix", result)
        self.assertNotIn("upstream_task_id", result)
        self.assertNotIn("voice_id", result)
        self.assertNotIn("voice_key", result)
        self.assertNotIn("narration_segments", result)

    def test_generate_returns_sanitized_unique_talking_warnings(self):
        payload = self.pixelle.prepare_payload({
            "text": "AI 培训", "mode": "generate", "source_page": "text-video",
        })
        payload["_job_id"] = 44
        upstream = {
            "video_url": "/api/files/result.mp4",
            "duration": 12,
            "talking_warnings": [
                {"scene_id": "scene_01", "message": "provider_unavailable after 1 attempt(s)"},
                {"scene_id": "scene_02", "message": "provider_unavailable after 1 attempt(s)"},
                {"scene_id": "scene_02", "message": "provider_unavailable after 1 attempt(s)"},
                {"scene_id": "../secret", "message": "line one\nline two\x00"},
            ],
        }
        with mock.patch.object(self.pixelle, "_submit", return_value="task-44"), \
             mock.patch.object(self.pixelle, "_wait", return_value=upstream), \
             mock.patch.object(self.pixelle, "_download_video", return_value=(
                 "video/pixelle_44.mp4", 4096,
             )), \
             mock.patch.object(self.pixelle, "public_url", return_value="/api/gen/file/token"):
            result = self.pixelle.generate(payload)

        self.assertEqual(result["talking_warnings"], [
            {"scene_id": "scene_01", "message": "provider_unavailable after 1 attempt(s)"},
            {"scene_id": "scene_02", "message": "provider_unavailable after 1 attempt(s)"},
            {"scene_id": "scene", "message": "line one line two"},
        ])

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


class TextVideoPlanningApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.core = importlib.import_module("content_domains.core")
        cls.pixelle = importlib.import_module("content_domains.pixelle_video")
        cls.assets = importlib.import_module("content_domains.pixelle_talking_assets")

    def setUp(self):
        self.originals = {
            "verify": self.core.verify,
            "domains": self.core._domains,
        }
        self.core.verify = lambda token: (
            {"username": token, "must_change": False} if token else None
        )
        self.core._domains = lambda: (mock.Mock(), mock.Mock(), mock.Mock())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.core.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.core.verify = self.originals["verify"]
        self.core._domains = self.originals["domains"]

    def request(self, method, path, body=None, username="alice"):
        headers = {}
        data = None
        if username is not None:
            headers["Authorization"] = "Bearer " + username
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.server.server_address[1], path),
            data=data, headers=headers, method=method,
        ), timeout=5)

    def test_plan_route_is_authenticated_guarded_and_does_not_enqueue_paid_job(self):
        expected = {
            "plan_id": "talking_plan_" + "a" * 32,
            "source_hash": "b" * 64,
            "scenes": [{"scene_id": "scene_01", "text": "第一段"}],
        }
        body = {"text": "第一段", "mode": "fixed", "ratio": 0.3,
                "template": "1080x1920/image_default.html",
                "style": "realistic_commercial",
                "voice": "public:zh-CN-YunjianNeural", "speech_rate": 1.0}
        with mock.patch.object(self.pixelle, "require_available"), \
             mock.patch.object(self.pixelle, "check_plan_rate_limit") as rate, \
             mock.patch.object(self.pixelle, "plan_talking_scenes", return_value=expected) as plan, \
             mock.patch.object(self.core.miniprogram_security, "check_payload") as guard, \
             mock.patch.object(self.core, "_user_active_job_count", return_value=0) as active, \
             mock.patch.object(self.core, "enqueue_job") as enqueue:
            with self.request("POST", "/api/gen/text-video/plan", body) as response:
                result = json.loads(response.read())

        self.assertEqual(result, expected)
        rate.assert_called_once_with("alice")
        active.assert_called_once_with("alice")
        guard.assert_called_once_with(body)
        plan.assert_called_once_with(body, "alice")
        enqueue.assert_not_called()

        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("POST", "/api/gen/text-video/plan", body, username=None)
        self.assertEqual(denied.exception.code, 401)

    def test_plan_route_reuses_existing_active_job_guard(self):
        with mock.patch.object(self.pixelle, "require_available"), \
             mock.patch.object(
                 self.core, "_user_active_job_count",
                 return_value=self.core.MAX_USER_ACTIVE_JOBS,
             ), mock.patch.object(self.pixelle, "plan_talking_scenes") as plan:
            with self.assertRaises(urllib.error.HTTPError) as limited:
                self.request("POST", "/api/gen/text-video/plan", {
                    "text": "第一段", "mode": "fixed", "ratio": 0.3,
                })
        self.assertEqual(limited.exception.code, 429)
        plan.assert_not_called()

    def test_avatar_upload_and_owner_scoped_private_preview(self):
        avatar = {"asset_id": "local_avatar_" + "c" * 32,
                  "mime": "image/png", "data": b"\x89PNG\r\n\x1a\nbody"}
        with mock.patch.object(self.pixelle, "require_available"), \
             mock.patch.object(self.core.miniprogram_security, "check_payload"), \
             mock.patch.object(self.assets, "store_avatar", return_value=avatar) as store:
            with self.request("POST", "/api/gen/text-video/avatar", {
                "image_data": "data:image/png;base64,aGVsbG8=",
            }) as response:
                result = json.loads(response.read())
        self.assertEqual(result, {
            "asset_id": avatar["asset_id"],
            "preview_url": "/api/gen/text-video/avatar/" + avatar["asset_id"],
        })
        store.assert_called_once_with("alice", "data:image/png;base64,aGVsbG8=")

        def read(owner, asset_id):
            if owner != "alice" or asset_id != avatar["asset_id"]:
                raise LookupError("not found")
            return avatar

        path = "/api/gen/text-video/avatar/" + avatar["asset_id"]
        with mock.patch.object(self.assets, "read_avatar", side_effect=read):
            with self.request("GET", path) as response:
                self.assertEqual(response.read(), avatar["data"])
                self.assertEqual(response.headers["Content-Type"], "image/png")
                self.assertEqual(response.headers["Cache-Control"], "private, max-age=300")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            with self.assertRaises(urllib.error.HTTPError) as denied:
                self.request("GET", path, username="bob")
        self.assertEqual(denied.exception.code, 404)

    def test_avatar_upload_rejects_invalid_image(self):
        with mock.patch.object(self.pixelle, "require_available"), \
             mock.patch.object(self.core.miniprogram_security, "check_payload"), \
             mock.patch.object(
                 self.assets, "store_avatar", side_effect=ValueError("invalid image")):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request("POST", "/api/gen/text-video/avatar", {
                    "image_data": "data:text/plain;base64,eA==",
                })
        self.assertEqual(rejected.exception.code, 400)
        self.assertIn("invalid image", rejected.exception.read().decode("utf-8"))

    def test_avatar_upload_rate_limit_rejects_before_storage(self):
        self.assets._AVATAR_UPLOAD_RATE_REQUESTS.clear()
        try:
            with mock.patch.object(self.pixelle, "require_available"), \
                    mock.patch.object(self.core.miniprogram_security, "check_payload"), \
                    mock.patch.object(
                        self.assets, "store_avatar",
                        return_value={"asset_id": "local_avatar_" + "d" * 32},
                    ) as store:
                for _ in range(self.assets.AVATAR_UPLOAD_RATE_MAX_REQUESTS):
                    with self.request("POST", "/api/gen/text-video/avatar", {
                        "image_data": "data:image/png;base64,aGVsbG8=",
                    }):
                        pass
                with self.assertRaises(urllib.error.HTTPError) as limited:
                    self.request("POST", "/api/gen/text-video/avatar", {
                        "image_data": "data:image/png;base64,aGVsbG8=",
                    })
                with self.request("POST", "/api/gen/text-video/avatar", {
                    "image_data": "data:image/png;base64,aGVsbG8=",
                }, username="bob"):
                    pass
            self.assertEqual(limited.exception.code, 429)
            payload = json.loads(limited.exception.read())
            self.assertEqual(payload["hq_code"], "HQ-RATE-001")
            self.assertEqual(store.call_count,
                             self.assets.AVATAR_UPLOAD_RATE_MAX_REQUESTS + 1)
        finally:
            self.assets._AVATAR_UPLOAD_RATE_REQUESTS.clear()

    def test_avatar_quota_error_uses_existing_rate_limit_contract(self):
        with mock.patch.object(self.pixelle, "require_available"), \
             mock.patch.object(self.core.miniprogram_security, "check_payload"), \
             mock.patch.object(
                 self.assets, "check_avatar_upload_rate_limit"), \
             mock.patch.object(
                 self.assets, "store_avatar",
                 side_effect=self.assets.AvatarQuotaExceeded("avatar quota exceeded")):
            with self.assertRaises(urllib.error.HTTPError) as limited:
                self.request("POST", "/api/gen/text-video/avatar", {
                    "image_data": "data:image/png;base64,aGVsbG8=",
                })
        self.assertEqual(limited.exception.code, 429)
        payload = json.loads(limited.exception.read())
        self.assertEqual(payload["hq_code"], "HQ-RATE-001")

    def test_oversized_plan_and_avatar_requests_use_asset_413_contract(self):
        with mock.patch.object(self.pixelle, "require_available"), \
                mock.patch.object(self.assets, "check_avatar_upload_rate_limit"):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request("POST", "/api/gen/text-video/plan", {
                    "text": "x" * (70 * 1024),
                })
            self.assertEqual(rejected.exception.code, 413)
            payload = json.loads(rejected.exception.read())
            self.assertEqual(payload["hq_code"], "HQ-ASSET-001")

            connection = http.client.HTTPConnection(
                "127.0.0.1", self.server.server_address[1], timeout=5)
            connection.putrequest("POST", "/api/gen/text-video/avatar")
            connection.putheader("Authorization", "Bearer alice")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(17 * 1024 * 1024 + 1))
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            self.assertEqual(json.loads(response.read())["hq_code"], "HQ-ASSET-001")
            connection.close()

    def test_decoded_avatar_over_limit_uses_asset_413_contract(self):
        from content_domains import error_contract
        with mock.patch.object(self.pixelle, "require_available"), \
             mock.patch.object(self.core.miniprogram_security, "check_payload"), \
             mock.patch.object(
                 self.assets, "check_avatar_upload_rate_limit"), \
             mock.patch.object(
                 self.assets, "store_avatar",
                 side_effect=error_contract.RequestBodyTooLarge(
                     "avatar decoded bytes exceed 12 MiB")):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request("POST", "/api/gen/text-video/avatar", {
                    "image_data": "data:image/png;base64,aGVsbG8=",
                })
        self.assertEqual(rejected.exception.code, 413)
        payload = json.loads(rejected.exception.read())
        self.assertEqual(payload["hq_code"], "HQ-ASSET-001")


if __name__ == "__main__":
    unittest.main()
