import importlib
import sqlite3
import sys
import tempfile
import types
from contextlib import closing
import unittest
from pathlib import Path


class InspirationLikeBackendTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        # This focused unit test only exercises the mark helpers.  Stub optional
        # domain modules that are not part of the small API fixture checkout.
        stubs = {
            "assets_store": {"KINDS": set()},
            "jobs_store": {"public_dict": lambda row: dict(row)},
            "startup_recovery": {},
            "submission_idempotency": {"clean_key": lambda value: value},
            "miniprogram_security": {},
            "asset_batch": {},
        }
        for name, attrs in stubs.items():
            qualified = "content_domains." + name
            if qualified not in sys.modules:
                module = types.ModuleType(qualified)
                for key, value in attrs.items():
                    setattr(module, key, value)
                sys.modules[qualified] = module
        self.core = importlib.import_module("content_domains.core")
        self.likes = importlib.import_module("content_domains.inspiration_likes")
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db = self.core.AUDIO_DB
        self.core.AUDIO_DB = Path(self.tempdir.name) / "audio.db"
        with closing(sqlite3.connect(self.core.AUDIO_DB)) as conn:
            conn.execute(
                """CREATE TABLE asset_marks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    asset_kind TEXT NOT NULL,
                    asset_key TEXT NOT NULL,
                    favorite INTEGER NOT NULL DEFAULT 0,
                    tags TEXT,
                    updated_at INTEGER,
                    UNIQUE(username, asset_kind, asset_key)
                )"""
            )
            conn.commit()

    def tearDown(self):
        self.core.AUDIO_DB = self.original_db
        self.tempdir.cleanup()

    def test_inspiration_id_must_be_a_positive_integer(self):
        for value in (None, "", 0, -1, "abc", 1.5, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.likes.clean_inspiration_id(value)
        self.assertEqual(self.likes.clean_inspiration_id("12"), "12")

    def test_like_is_idempotent_and_can_be_cancelled(self):
        first = self.likes.set_like(self.core.AUDIO_DB, "alice", 7, True)
        repeated = self.likes.set_like(self.core.AUDIO_DB, "alice", 7, True)
        cancelled = self.likes.set_like(self.core.AUDIO_DB, "alice", 7, False)

        self.assertEqual(first, {"id": 7, "favorite": True, "count": 1})
        self.assertEqual(repeated, first)
        self.assertEqual(cancelled, {"id": 7, "favorite": False, "count": 0})

    def test_summary_returns_global_counts_and_current_users_likes(self):
        self.likes.set_like(self.core.AUDIO_DB, "alice", 1, True)
        self.likes.set_like(self.core.AUDIO_DB, "bob", 1, True)
        self.likes.set_like(self.core.AUDIO_DB, "bob", 2, True)

        self.assertEqual(self.likes.summary(self.core.AUDIO_DB), {"counts": {"1": 2, "2": 1}})
        self.assertEqual(
            self.likes.summary(self.core.AUDIO_DB, "alice"),
            {"counts": {"1": 2, "2": 1}, "liked": [1]},
        )


class InspirationLikeFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1] / "site" / "workbench" / "inspiration.html"
        ).read_text(encoding="utf-8")

    def test_page_uses_accessible_like_buttons_in_list_and_detail(self):
        self.assertIn('data-like-id=', self.html)
        self.assertIn('aria-pressed=', self.html)
        self.assertIn('type="button"', self.html)
        self.assertIn('e.stopPropagation()', self.html)

    def test_page_loads_and_mutates_persistent_like_state(self):
        self.assertIn('/api/gen/inspiration/likes', self.html)
        self.assertIn('/api/gen/inspiration/like', self.html)
        self.assertIn('credentials:"same-origin"', self.html)
        self.assertIn('HQ.login()', self.html)


if __name__ == "__main__":
    unittest.main()
