import base64
import concurrent.futures
import importlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from PIL import Image


class PixelleTalkingAssetsTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "jobs.db"
        self.out = self.root / "content_out"
        self.store = importlib.import_module("content_domains.pixelle_talking_assets")
        self.old_db = self.store.DB_PATH
        self.old_out = self.store.OUT_DIR
        self.store.DB_PATH = str(self.db)
        self.store.OUT_DIR = self.out
        self.store.init_db()

    def tearDown(self):
        self.store.DB_PATH = self.old_db
        self.store.OUT_DIR = self.old_out
        self.temp.cleanup()

    @staticmethod
    def image_data_url(fmt="PNG", declared_mime=None):
        output = io.BytesIO()
        Image.new("RGB", (2, 2), (32, 96, 160)).save(output, fmt)
        mime = declared_mime or {
            "PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp",
        }[fmt]
        return "data:%s;base64,%s" % (
            mime, base64.b64encode(output.getvalue()).decode("ascii"))

    def test_plan_snapshot_has_stable_ids_hash_and_is_immutable(self):
        source = {"mode": "fixed", "text": "原文"}
        scenes = [{"text": "第一段", "role": "hook"}, {"text": "第二段"}]
        plan = self.store.create_plan("alice", source, scenes)
        same = self.store.create_plan("alice", source, scenes)

        self.assertRegex(plan["plan_id"], r"^talking_plan_[0-9a-f]{32}$")
        self.assertEqual([s["scene_id"] for s in plan["scenes"]],
                         ["scene_01", "scene_02"])
        self.assertEqual(plan["source_hash"], same["source_hash"])
        plan["source"]["text"] = "tampered"
        plan["scenes"][0]["text"] = "tampered"

        restored = self.store.get_plan("alice", plan["plan_id"])
        self.assertEqual(restored["source"]["text"], "原文")
        self.assertEqual(restored["scenes"][0]["text"], "第一段")

    def test_plan_rejects_cross_owner_and_invalid_scene_input(self):
        plan = self.store.create_plan("alice", {}, [{"text": "第一段"}])
        with self.assertRaises(LookupError):
            self.store.get_plan("bob", plan["plan_id"])
        with self.assertRaises(ValueError):
            self.store.create_plan("alice", {}, [])
        with self.assertRaises(ValueError):
            self.store.create_plan("alice", {}, [{"text": ""}])

    def test_avatar_is_owner_scoped_validated_and_private(self):
        for fmt in ("PNG", "JPEG", "WEBP"):
            with self.subTest(fmt=fmt):
                item = self.store.store_avatar("alice", self.image_data_url(fmt))
                loaded = self.store.get_avatar("alice", item["asset_id"])
                self.assertEqual(loaded["sha256"], item["sha256"])
                self.assertEqual(loaded["mime"], item["mime"])
                self.assertNotIn("path", item)
                self.assertNotIn("file_path", item)
                self.assertNotIn("path", loaded)
                self.assertRegex(item["asset_id"], r"^local_avatar_[0-9a-f]{32}$")
                if os.name != "nt":
                    mode = (self.out / "pixelle_avatar" /
                            (item["asset_id"].split("local_avatar_", 1)[1] + item["extension"])).stat().st_mode
                    self.assertEqual(mode & 0o777, 0o600)
                with self.assertRaises(LookupError):
                    self.store.get_avatar("bob", item["asset_id"])

    def test_avatar_rejects_mime_mismatch_corrupt_and_oversize_data(self):
        with self.assertRaisesRegex(ValueError, "格式"):
            self.store.store_avatar(
                "alice", self.image_data_url("PNG", declared_mime="image/jpeg"))
        corrupt = "data:image/png;base64," + base64.b64encode(
            b"\x89PNG\r\n\x1a\nnot-an-image").decode("ascii")
        with self.assertRaisesRegex(ValueError, "解码"):
            self.store.store_avatar("alice", corrupt)
        too_large = "data:image/png;base64," + base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"x" * (12 * 1024 * 1024)).decode("ascii")
        with self.assertRaisesRegex(ValueError, "12"):
            self.store.store_avatar("alice", too_large)

    def test_consume_requires_owner_and_expected_hash_and_is_idempotent(self):
        plan = self.store.create_plan("alice", {}, [{"text": "第一段"}])
        with self.assertRaises(ValueError):
            self.store.consume_plan("alice", plan["plan_id"], "0" * 64)
        with self.assertRaises(LookupError):
            self.store.consume_plan("bob", plan["plan_id"], plan["source_hash"])

        consumed = self.store.consume_plan(
            "alice", plan["plan_id"], plan["source_hash"])
        replay = self.store.consume_plan(
            "alice", plan["plan_id"], plan["source_hash"])
        self.assertEqual(consumed["status"], "consumed")
        self.assertEqual(replay["consumed_at"], consumed["consumed_at"])

    def test_concurrent_consume_is_safe_and_returns_one_snapshot(self):
        plan = self.store.create_plan("alice", {}, [{"text": "第一段"}])

        def consume(_):
            return self.store.consume_plan(
                "alice", plan["plan_id"], plan["source_hash"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(consume, range(16)))
        self.assertEqual({item["status"] for item in results}, {"consumed"})
        self.assertEqual(len({item["consumed_at"] for item in results}), 1)

    def test_cleanup_retains_consumed_paid_plan_until_terminal_then_releases(self):
        avatar = self.store.store_avatar("alice", self.image_data_url())
        plan = self.store.create_plan("alice", {}, [{"text": "第一段"}])
        self.store.bind_plan_avatars(
            "alice", plan["plan_id"], [avatar["asset_id"]])
        self.store.consume_plan("alice", plan["plan_id"], plan["source_hash"])
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY, username TEXT NOT NULL, status TEXT NOT NULL)""")
            connection.execute(
                "INSERT INTO jobs(id,username,status) VALUES(7,'alice','running')")
            connection.commit()
        self.store.bind_plan_job("alice", plan["plan_id"], 7)
        future = plan["expires_at"] + 1

        self.store.cleanup_expired(now=future)
        self.assertEqual(self.store.get_plan("alice", plan["plan_id"])["job_id"], 7)
        self.assertEqual(self.store.get_avatar("alice", avatar["asset_id"])["asset_id"],
                         avatar["asset_id"])

        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE jobs SET status='done' WHERE id=7")
            connection.commit()
        self.store.cleanup_expired(now=future)
        with self.assertRaises(LookupError):
            self.store.get_plan("alice", plan["plan_id"])
        with self.assertRaises(LookupError):
            self.store.get_avatar("alice", avatar["asset_id"])

    def test_running_paid_plan_keeps_expired_avatar_resolvable(self):
        avatar = self.store.store_avatar("alice", self.image_data_url())
        plan = self.store.create_plan("alice", {}, [{"text": "第一段"}])
        self.store.bind_plan_avatars(
            "alice", plan["plan_id"], [avatar["asset_id"]])
        self.store.consume_plan("alice", plan["plan_id"], plan["source_hash"])
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY, username TEXT NOT NULL, status TEXT NOT NULL)""")
            connection.execute(
                "INSERT INTO jobs(id,username,status) VALUES(8,'alice','running')")
            connection.execute(
                "UPDATE pixelle_avatar_assets SET expires_at=1 WHERE id=?",
                (avatar["asset_id"],))
            connection.commit()
        self.store.bind_plan_job("alice", plan["plan_id"], 8)

        loaded = self.store.get_avatar("alice", avatar["asset_id"])
        self.assertEqual(loaded["asset_id"], avatar["asset_id"])
        self.assertTrue(self.store.resolve_avatar_path(
            "alice", avatar["asset_id"]).is_file())

    def test_bind_plan_job_requires_owned_paid_job(self):
        plan = self.store.create_plan("alice", {}, [{"text": "第一段"}])
        self.store.consume_plan("alice", plan["plan_id"], plan["source_hash"])
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY, username TEXT NOT NULL, status TEXT NOT NULL)""")
            connection.execute(
                "INSERT INTO jobs(id,username,status) VALUES(9,'bob','pending')")
            connection.commit()
        with self.assertRaises(LookupError):
            self.store.bind_plan_job("alice", plan["plan_id"], 9)
        with self.assertRaises(LookupError):
            self.store.bind_plan_job("alice", plan["plan_id"], 10)

    def test_expired_avatar_retained_for_recovery_cannot_join_a_new_plan(self):
        avatar = self.store.store_avatar("alice", self.image_data_url())
        first = self.store.create_plan("alice", {}, [{"text": "第一段"}])
        self.store.bind_plan_avatars(
            "alice", first["plan_id"], [avatar["asset_id"]])
        self.store.consume_plan("alice", first["plan_id"], first["source_hash"])
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY, username TEXT NOT NULL, status TEXT NOT NULL)""")
            connection.execute(
                "INSERT INTO jobs(id,username,status) VALUES(11,'alice','running')")
            connection.execute(
                "UPDATE pixelle_avatar_assets SET expires_at=1 WHERE id=?",
                (avatar["asset_id"],))
            connection.commit()
        self.store.bind_plan_job("alice", first["plan_id"], 11)
        second = self.store.create_plan("alice", {}, [{"text": "第二段"}])

        with self.assertRaises(LookupError):
            self.store.bind_plan_avatars(
                "alice", second["plan_id"], [avatar["asset_id"]])

    def test_cleanup_removes_expired_unconsumed_plan_and_unreferenced_avatar(self):
        avatar = self.store.store_avatar("alice", self.image_data_url())
        plan = self.store.create_plan("alice", {}, [{"text": "第一段"}])
        self.store.cleanup_expired(now=max(plan["expires_at"], avatar["expires_at"]) + 1)
        with self.assertRaises(LookupError):
            self.store.get_plan("alice", plan["plan_id"])
        with self.assertRaises(LookupError):
            self.store.get_avatar("alice", avatar["asset_id"])

    def test_core_initializes_and_reaper_invokes_talking_store(self):
        core = importlib.import_module("content_domains.core")
        with mock.patch.object(core.pixelle_talking_assets, "init_db") as init_store, \
                mock.patch.object(core, "init_audio_db"), \
                mock.patch.object(core, "feature_flags"), \
                mock.patch.object(core, "pricing"), \
                mock.patch.object(core, "_short_drama_domain") as short_drama, \
                mock.patch.object(core.jobs_store, "ensure_video_notification_outbox"), \
                mock.patch.object(core, "JOB_DB", str(self.root / "core.db")):
            core.init_db()
        init_store.assert_called_once_with(str(self.root / "core.db"), core.OUT_DIR)

        domains = (mock.Mock(), mock.Mock(), mock.Mock())
        domains[1].retry_breakdown_refunds = None
        reaper_db = str(self.root / "reaper.db")
        with closing(sqlite3.connect(reaper_db)) as connection:
            connection.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, status TEXT)")
            connection.commit()
        with mock.patch.object(core, "_domains", return_value=domains), \
                mock.patch.object(core.pixelle_talking_assets, "cleanup_expired") as cleanup, \
                mock.patch.object(core, "JOB_DB", reaper_db), \
                mock.patch.object(core.time, "sleep", side_effect=RuntimeError("stop")):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                core.reaper()
        cleanup.assert_called_once_with(reaper_db, core.OUT_DIR)


if __name__ == "__main__":
    unittest.main()
