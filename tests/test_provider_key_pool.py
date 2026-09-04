# -*- coding: utf-8 -*-
import base64
import os
import pathlib
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import Mock, patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
import sys
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import admin_api  # noqa: E402
from content_domains import provider_keys, video  # noqa: E402


class ProviderKeyPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_provider_db = provider_keys.DB_PATH
        self.old_admin_db = admin_api.ADMIN_DB
        self.db_path = pathlib.Path(self.tmp.name) / "admin.db"
        provider_keys.DB_PATH = self.db_path
        admin_api.ADMIN_DB = self.db_path
        admin_api._PROVIDER_KEY_PING_ATTEMPTS.clear()
        self.env = patch.dict(
            os.environ,
            {
                provider_keys.MASTER_KEY_ENV: base64.urlsafe_b64encode(
                    b"k" * 32
                ).decode(),
                "OPENAI_API_KEY": "",
                "ARK_API_KEY": "",
                "GEMINI_API_KEY": "",
                "XAI_API_KEY": "",
                "MINIMAX_API_KEY": "",
                "DEEPSEEK_API_KEY": "",
            },
            clear=False,
        )
        self.env.start()
        with patch.object(admin_api, "feature_flags", None):
            admin_api.init_db()

    def tearDown(self):
        self.env.stop()
        provider_keys._LEGACY_IMPORT_PATHS.discard(str(self.db_path))
        provider_keys._RUNTIME_UNHEALTHY_UNTIL.clear()
        admin_api._PROVIDER_KEY_PING_ATTEMPTS.clear()
        provider_keys.DB_PATH = self.old_provider_db
        admin_api.ADMIN_DB = self.old_admin_db
        self.tmp.cleanup()

    def add(self, provider="sora", secret="sk-provider-secret-1234"):
        return provider_keys.add_key(
            provider,
            "线路 1",
            secret,
            "tang1",
            {"ok": True, "latency_ms": 18},
        )

    def test_secret_is_encrypted_and_never_returned(self):
        item = self.add()
        self.assertEqual(item["last4"], "1234")
        self.assertNotIn("secret", item)
        self.assertNotIn(b"sk-provider-secret-1234", self.db_path.read_bytes())
        self.assertEqual(
            provider_keys.candidates("sora", item["id"])[0]["secret"],
            "sk-provider-secret-1234",
        )

    def test_invalid_master_key_is_not_reported_as_ready(self):
        with patch.dict(os.environ, {provider_keys.MASTER_KEY_ENV: "invalid"}):
            self.assertFalse(provider_keys.vault_ready())

    def test_deepseek_provider_is_supported_and_encrypted(self):
        self.assertIn("deepseek", provider_keys.PROVIDERS)
        self.assertEqual(
            "DEEPSEEK_API_KEY", provider_keys.ENV_KEYS["deepseek"]
        )
        item = self.add("deepseek", "deepseek-test-secret-1234")
        self.assertEqual("deepseek", item["provider"])
        self.assertNotIn(b"deepseek-test-secret-1234", self.db_path.read_bytes())
        self.assertEqual(
            "deepseek-test-secret-1234",
            provider_keys.candidates("deepseek", item["id"])[0]["secret"],
        )

    def test_retired_key_stops_new_jobs_but_can_finish_bound_job(self):
        item = self.add()
        provider_keys.retire_key(item["id"])
        self.assertEqual(provider_keys.candidates("sora"), [])
        self.assertEqual(
            provider_keys.candidates("sora", item["id"])[0]["id"],
            item["id"],
        )

    def test_readding_retired_secret_restores_the_same_key(self):
        item = self.add()
        provider_keys.retire_key(item["id"])
        restored = provider_keys.add_key(
            "sora",
            "恢复线路",
            "sk-provider-secret-1234",
            "tang1",
            {"ok": True},
        )
        self.assertEqual(restored["id"], item["id"])
        self.assertEqual(restored["state"], "active")

    def test_environment_key_is_snapshotted_before_tasks_bind_to_it(self):
        os.environ["OPENAI_API_KEY"] = "sk-environment-legacy"
        provider_keys._LEGACY_IMPORT_PATHS.discard(str(self.db_path))
        provider_keys.init_db()

        first = provider_keys.candidates("sora")[0]
        self.assertNotEqual(first["id"], "env")
        self.assertEqual(first["secret"], "sk-environment-legacy")

        os.environ["OPENAI_API_KEY"] = "sk-environment-rotated"
        current = provider_keys.candidates("sora")[0]
        legacy = provider_keys.candidates("sora", preferred_id="env")[0]
        self.assertEqual(current, first)
        self.assertEqual(legacy, first)

    def test_unsnapshotted_environment_key_stops_new_paid_tasks(self):
        os.environ["OPENAI_API_KEY"] = "sk-environment-late"
        with self.assertRaisesRegex(
            provider_keys.KeyStoreUnavailable, "停止新付费任务"
        ):
            provider_keys.candidates("sora")

    def test_old_env_task_without_snapshot_never_reads_rotated_env(self):
        os.environ["OPENAI_API_KEY"] = "sk-rotated-environment"
        with self.assertRaisesRegex(
            provider_keys.KeyStoreUnavailable, "没有加密快照"
        ):
            provider_keys.candidates("sora", preferred_id="env")

    def test_runtime_quarantine_survives_health_database_failure(self):
        item = self.add()
        with patch.object(
            provider_keys, "_connect", side_effect=sqlite3.OperationalError("locked")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                provider_keys.set_health(item["id"], False)
        self.assertEqual(provider_keys.candidates("sora"), [])

        provider_keys.set_health(item["id"], True)
        self.assertEqual(provider_keys.candidates("sora")[0]["id"], item["id"])

    def test_failed_candidate_probe_is_not_saved(self):
        with patch.object(
            admin_api,
            "probe_provider_secret",
            return_value={"ok": False, "http_status": 401, "latency_ms": 9},
        ):
            with self.assertRaisesRegex(ValueError, "检测未通过"):
                admin_api.add_provider_key(
                    "tang1",
                    {
                        "provider": "sora",
                        "label": "坏线路",
                        "secret": "sk-invalid-provider",
                    },
                )
        self.assertEqual(provider_keys.list_public(), [])

    def test_successful_candidate_probe_saves_only_public_metadata(self):
        with patch.object(
            admin_api,
            "probe_provider_secret",
            return_value={"ok": True, "http_status": 200, "latency_ms": 12},
        ):
            result = admin_api.add_provider_key(
                "tang1",
                {
                    "provider": "omni",
                    "label": "Omni 线路 2",
                    "secret": "gemini-provider-secret-7788",
                },
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["item"]["last4"], "7788")
        self.assertNotIn("secret", str(result))

    def test_background_probe_refreshes_stale_managed_key_without_generating(self):
        item = self.add("omni", "gemini-provider-secret-7788")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE provider_api_keys SET last_checked_at=1 WHERE id=?",
                (item["id"],),
            )
            conn.commit()
        with patch.object(
            admin_api, "probe_provider_secret",
            return_value={"ok": True, "http_status": 200, "latency_ms": 12},
        ) as probe:
            checked = admin_api.probe_provider_keys(now=1000)
            skipped = admin_api.probe_provider_keys(now=1010)
        self.assertEqual(checked, [{"id": item["id"], "provider": "omni", "ok": True}])
        self.assertEqual(skipped, [])
        probe.assert_called_once_with("omni", "gemini-provider-secret-7788")
        self.assertEqual(provider_keys.public_key(item["id"])["health_status"], "healthy")

    def test_xai_keys_rotate_by_least_use_and_can_be_revealed_with_audit(self):
        first = self.add("xai", "xai-provider-secret-1111")
        second = provider_keys.add_key(
            "xai", "线路 2", "xai-provider-secret-2222", "tang1",
            {"ok": True},
        )
        self.assertEqual(provider_keys.claim_candidate("xai")["id"], first["id"])
        self.assertEqual(provider_keys.claim_candidate("xai")["id"], second["id"])

        revealed = admin_api.reveal_provider_key("tang1", {"id": first["id"]})
        self.assertEqual(revealed["secret"], "xai-provider-secret-1111")
        self.assertEqual(revealed["expires_in"], 5)
        public = admin_api.provider_key_list()
        self.assertNotIn("secret", str(public))
        with closing(sqlite3.connect(self.db_path)) as conn:
            action = conn.execute(
                "SELECT action FROM admin_audit ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(action, "provider_key.reveal")

    def test_concurrent_claims_split_between_two_keys(self):
        first = self.add("xai", "xai-provider-secret-1111")
        second = provider_keys.add_key(
            "xai", "线路 2", "xai-provider-secret-2222", "tang1", {"ok": True}
        )
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait()
            return provider_keys.claim_candidate("xai")["id"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda _index: claim(), range(2)))
        self.assertEqual(set(claimed), {first["id"], second["id"]})

    def test_server_key_reveal_is_short_lived_and_audited(self):
        with patch.dict(os.environ, {"HEYGEN_API_KEY": "heygen-secret-7788"}):
            result = admin_api.reveal_server_key(
                "tang1", {"channel": "heygen"}
            )
            public_status = admin_api.key_status()
        self.assertEqual(result["expires_in"], 5)
        self.assertEqual(
            result["secrets"],
            [{"env": "HEYGEN_API_KEY", "secret": "heygen-secret-7788"}],
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            action, detail = conn.execute(
                "SELECT action,detail FROM admin_audit ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(action, "server_key.reveal")
        self.assertNotIn("heygen-secret-7788", detail)
        self.assertNotIn("heygen-secret-7788", str(public_status))

    def test_key_catalog_maps_api_providers_to_frontend_features(self):
        with patch.dict(
            os.environ,
            {"GEMINI_OMNI_BASE": "https://sg.huangquechuanmei.com/google"},
        ):
            rows = {item["key"]: item for item in admin_api.key_status()}
        self.assertEqual(rows["gemini"]["name"], "Google Gemini API")
        self.assertIn("图片生成 → 纳米香蕉", rows["gemini"]["features"])
        self.assertIn("视频模块 → Omni 视频", rows["gemini"]["features"])
        self.assertEqual(rows["gemini"]["env_features"], ["图片生成 → 纳米香蕉"])
        self.assertEqual(rows["gemini"]["pool_features"], ["视频模块 → Omni 视频"])
        self.assertEqual(rows["gemini"]["pool_base_host"], "sg.huangquechuanmei.com")
        self.assertIn("视频模块 → 电影化身", rows["heygen"]["features"])
        self.assertEqual(rows["seedance"]["pool_provider"], "seedance")

    def test_deepseek_catalog_exposes_agent_base_and_supported_models(self):
        rows = {item["key"]: item for item in admin_api.key_status()}
        deepseek = rows["deepseek"]
        self.assertEqual("DeepSeek API", deepseek["name"])
        self.assertEqual("deepseek", deepseek["pool_provider"])
        self.assertEqual("api.deepseek.com", deepseek["pool_base_host"])
        self.assertEqual("VIDEO_AGENT_MODEL", deepseek["model_env"])
        self.assertEqual("deepseek-v4-flash", deepseek["model"])
        self.assertEqual(
            [
                "deepseek-v4-flash",
                "deepseek-v4-pro",
                "deepseek-v4-flash-vision-exp",
            ],
            deepseek["model_options"],
        )

    def test_deepseek_probe_is_fixed_to_official_origin_without_redirects(self):
        with patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "deepseek-test-key",
            "DEEPSEEK_API_BASE": "https://api.deepseek.com/v1/responses",
        }), patch.object(
            admin_api, "_ping_upstream", return_value={"ok": True}
        ) as ping:
            result = admin_api._key_ping_deepseek()
        self.assertTrue(result["ok"])
        self.assertEqual(ping.call_args.args[:2], (
            "GET", "https://api.deepseek.com/v1/models",
        ))
        self.assertFalse(ping.call_args.kwargs["allow_redirects"])

    def test_deepseek_probe_rejects_untrusted_base_before_network(self):
        with patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "deepseek-test-key",
            "DEEPSEEK_API_BASE": "https://attacker.example/collect",
        }), patch.object(admin_api, "_ping_upstream") as ping:
            result = admin_api._key_ping_deepseek()
        self.assertFalse(result["ok"])
        self.assertIn("Base", result["error"])
        ping.assert_not_called()

    def test_transient_manual_probe_does_not_quarantine_key(self):
        item = self.add()
        with patch.object(
            admin_api,
            "probe_provider_secret",
            return_value={"ok": False, "http_status": 503, "latency_ms": 9},
        ), patch.object(provider_keys, "set_health") as health:
            result = admin_api.test_provider_key(
                "tang1", {"provider": "sora", "id": item["id"]}
            )
        self.assertFalse(result["ok"])
        health.assert_not_called()
        self.assertEqual(provider_keys.candidates("sora")[0]["id"], item["id"])

    def test_auth_rejection_quarantines_key(self):
        item = self.add()
        with patch.object(
            admin_api,
            "probe_provider_secret",
            return_value={"ok": False, "http_status": 401, "latency_ms": 9},
        ):
            result = admin_api.test_provider_key(
                "tang1", {"provider": "sora", "id": item["id"]}
            )
        self.assertFalse(result["ok"])
        self.assertEqual(provider_keys.candidates("sora"), [])

    def test_rotation_only_handles_definitive_credential_rejection(self):
        class CredentialRejected(RuntimeError):
            pass

        selected = [
            {"id": "key-1", "secret": "one"},
            {"id": "key-2", "secret": "two"},
        ]
        calls = []

        def create(candidate):
            calls.append(candidate["id"])
            if candidate["id"] == "key-1":
                raise CredentialRejected("401")
            return {"video_id": "video-ok"}

        with patch.object(provider_keys, "claim_candidate", side_effect=selected), \
                patch.object(provider_keys, "set_health") as health, \
                patch.object(video, "update_video_asset_phase") as persist:
            result, key = video._create_with_provider_key(
                "sora",
                7,
                "sora_submitting",
                CredentialRejected,
                create,
            )
        self.assertEqual(calls, ["key-1", "key-2"])
        self.assertEqual(key["id"], "key-2")
        self.assertEqual(result["provider_key_id"], "key-2")
        self.assertEqual(persist.call_count, 2)
        self.assertEqual(health.call_args_list[0].args[:2], ("key-1", False))

    def test_unknown_create_outcome_never_rotates(self):
        selected = [
            {"id": "key-1", "secret": "one"},
            {"id": "key-2", "secret": "two"},
        ]
        create = Mock(side_effect=TimeoutError("outcome unknown"))
        with patch.object(provider_keys, "claim_candidate", return_value=selected[0]), \
                patch.object(provider_keys, "set_health"), \
                patch.object(video, "update_video_asset_phase"):
            with self.assertRaisesRegex(TimeoutError, "outcome unknown"):
                video._create_with_provider_key(
                    "sora", 7, "sora_submitting", ValueError, create
                )
        create.assert_called_once_with(selected[0])

    def test_health_metadata_failure_never_changes_paid_result(self):
        selected = [{"id": "key-1", "secret": "one"}]
        with patch.object(provider_keys, "claim_candidate", return_value=selected[0]), \
                patch.object(
                    provider_keys, "set_health", side_effect=RuntimeError("db busy")
                ), \
                patch.object(video, "update_video_asset_phase"):
            result, key = video._create_with_provider_key(
                "sora", 7, "sora_submitting", ValueError,
                lambda _candidate: {"video_id": "video-ok"},
            )
        self.assertEqual(result["video_id"], "video-ok")
        self.assertEqual(key["id"], "key-1")

    def test_video_assets_schema_persists_provider_key_id(self):
        db_path = pathlib.Path(self.tmp.name) / "assets.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE video_assets(
                job_id INTEGER UNIQUE,username TEXT NOT NULL,mode TEXT NOT NULL,
                provider_video_id TEXT,provider_key_id TEXT,model TEXT,
                resolution TEXT,ratio TEXT,phase TEXT,status TEXT,
                error TEXT,created_at INTEGER,updated_at INTEGER
            )"""
        )
        conn.execute(
            "INSERT INTO video_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                9, "u", "sora", "video-9", "key-9", "sora-2",
                "720p", "9:16", "sora_queued", "running", "", 1, 1,
            ),
        )
        conn.commit()
        conn.close()

        def connect():
            value = sqlite3.connect(db_path)
            value.row_factory = sqlite3.Row
            return value

        with patch.object(video, "adb", side_effect=connect):
            item = video.get_resumable_sora_request(9)
        self.assertEqual(item["provider_key_id"], "key-9")

    def test_admin_console_exposes_real_pool_controls_and_non_disruptive_refresh(self):
        html = (ROOT / "site" / "admin" / "index.html").read_text(encoding="utf-8")
        for text in (
            "state.module==='dashboard'", "xAI API · 果肉视频", "查看 5 秒",
            "DeepSeek API · 视频创作助手", "model_options",
            "data-provider-key-replace", "data-server-key-reveal",
            "data-provider-key-delete",
            "前端功能对应关系", "navigator.clipboard",
        ):
            self.assertIn(text, html)
        self.assertNotIn("data-provider-key-rename", html)
        self.assertIn("if(document.hidden||state.poolActions)return", html)
        self.assertIn("if(state.module==='dashboard')loadDashboard(true)", html)
        self.assertIn("else if(state.module==='operations')load(true)", html)
        self.assertNotIn("/api/admin/provider-keys/rename", html)
        self.assertNotIn("其他上游", html)
        self.assertIn("Date.now()+ttl", html)
        self.assertIn("Math.min(5", html)
        self.assertIn("clearSecretWindows", html)
        self.assertIn("pagehide", html)
        self.assertIn("pageshow", html)
        self.assertIn("服务器环境变量（图片）", html)
        self.assertIn("加密号池（视频）", html)
        self.assertIn("env_base_host", html)
        self.assertIn("pool_base_host", html)
        self.assertNotIn("left-=1", html)
        self.assertIn("!state.poolActions", html)
        self.assertIn("requestPoolEpoch!==state.poolEpoch", html)
        self.assertIn("state.refreshPending", html)
        self.assertNotIn("渠道开关 / 配置", html)


if __name__ == "__main__":
    unittest.main()
