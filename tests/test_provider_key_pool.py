# -*- coding: utf-8 -*-
import base64
import os
import pathlib
import sqlite3
import tempfile
import unittest
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
        self.env = patch.dict(
            os.environ,
            {
                provider_keys.MASTER_KEY_ENV: base64.urlsafe_b64encode(
                    b"k" * 32
                ).decode(),
                "OPENAI_API_KEY": "",
                "ARK_API_KEY": "",
                "GEMINI_API_KEY": "",
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

        with patch.object(provider_keys, "candidates", return_value=selected), \
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
        with patch.object(provider_keys, "candidates", return_value=selected), \
                patch.object(provider_keys, "set_health"), \
                patch.object(video, "update_video_asset_phase"):
            with self.assertRaisesRegex(TimeoutError, "outcome unknown"):
                video._create_with_provider_key(
                    "sora", 7, "sora_submitting", ValueError, create
                )
        create.assert_called_once_with(selected[0])

    def test_health_metadata_failure_never_changes_paid_result(self):
        selected = [{"id": "key-1", "secret": "one"}]
        with patch.object(provider_keys, "candidates", return_value=selected), \
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


if __name__ == "__main__":
    unittest.main()
