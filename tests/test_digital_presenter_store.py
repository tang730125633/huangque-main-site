# -*- coding: utf-8 -*-
import importlib
import pathlib
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import closing


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains.canvas_access import CanvasAccess  # noqa: E402


class DigitalPresenterStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = importlib.import_module("content_domains.digital_presenter_store")
        self.presenter = importlib.import_module("content_domains.digital_presenter")
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(pathlib.Path(self.tmp.name) / "content.db")

        def connect():
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            return connection

        self.db = connect
        self.presenter.init_db(self.db)
        self.owner_access = CanvasAccess("board-a", "owner", "owner", "owner")
        self.editor_access = CanvasAccess("board-a", "editor", "owner", "editor")
        self.viewer_access = CanvasAccess("board-a", "viewer", "owner", "viewer")
        self.other_board_access = CanvasAccess("board-b", "other", "other", "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def _project(self, access=None, idempotency_key=None, **changes):
        payload = {
            "title": "门店资讯",
            "script_text": "今天介绍夏季护理方案。",
            "ratio": "9:16",
            "target_duration": 45,
        }
        payload.update(changes)
        return self.store.create_project(
            self.db,
            access or self.owner_access,
            payload,
            idempotency_key or ("test-create-" + uuid.uuid4().hex),
        )

    def test_schema_and_create_persist_trusted_board_ownership(self):
        project = self._project(self.editor_access)

        self.assertTrue(project["id"].startswith("dp_"))
        self.assertEqual("board-a", project["board_id"])
        self.assertEqual("owner", project["owner_username"])
        self.assertEqual("editor", project["created_by"])
        self.assertEqual("draft", project["stage"])
        self.assertEqual("1080p", project["resolution"])
        self.assertEqual(1, project["revision"])
        self.assertEqual(0, project["plan_revision"])
        self.assertEqual(0, project["timeline_revision"])
        self.assertEqual(0, project["spent_points"])

        with closing(self.db()) as connection:
            columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(digital_presenter_projects)"
                )
            }
        self.assertTrue({
            "id", "owner_username", "created_by", "board_id", "script_text",
            "revision", "plan_revision", "timeline_revision", "deleted",
        }.issubset(columns))

    def test_create_replay_returns_one_project_and_original_response(self):
        key = "test-create-replay"
        first = self._project(idempotency_key=key)
        self.store.update_project(
            self.db, self.owner_access, first["id"], 1, {"title": "updated later"}
        )
        replay = self._project(idempotency_key=key)

        self.assertEqual(first, replay)
        with closing(self.db()) as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM digital_presenter_projects"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM digital_presenter_idempotency"
                ).fetchone()[0],
            )

    def test_create_rejects_key_reuse_with_different_request(self):
        key = "test-create-conflict"
        self._project(idempotency_key=key)
        with self.assertRaises(self.store.IdempotencyConflict):
            self._project(idempotency_key=key, title="different request")

    def test_create_key_is_scoped_by_actor_and_board(self):
        key = "test-create-scoped"
        owner = self._project(self.owner_access, idempotency_key=key)
        editor = self._project(self.editor_access, idempotency_key=key)
        other = self._project(self.other_board_access, idempotency_key=key)
        self.assertEqual(3, len({owner["id"], editor["id"], other["id"]}))

    def test_create_requires_valid_idempotency_key_without_writes(self):
        payload = {"title": "test", "ratio": "9:16", "target_duration": 30}
        for key in (None, "short", "invalid key"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.store.create_project(self.db, self.owner_access, payload, key)
        with closing(self.db()) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM digital_presenter_projects"
                ).fetchone()[0],
            )

    def test_editor_can_update_board_project(self):
        project = self._project(self.owner_access)
        updated = self.store.update_project(
            self.db, self.editor_access, project["id"], 1, {"title": "新版"}
        )
        self.assertEqual("新版", updated["title"])
        self.assertEqual(2, updated["revision"])

    def test_viewer_can_read_but_cannot_create_update_or_delete(self):
        project = self._project(self.owner_access)
        self.assertEqual(
            project["id"],
            self.store.get_project(self.db, self.viewer_access, project["id"])["id"],
        )
        with self.assertRaises(self.store.PermissionDenied):
            self._project(self.viewer_access)
        with self.assertRaises(self.store.PermissionDenied):
            self.store.update_project(
                self.db, self.viewer_access, project["id"], 1, {"title": "越权"}
            )
        with self.assertRaises(self.store.PermissionDenied):
            self.store.delete_project(self.db, self.viewer_access, project["id"], 1)

    def test_project_from_other_board_is_hidden(self):
        project = self._project()
        for operation in (
            lambda: self.store.get_project(self.db, self.other_board_access, project["id"]),
            lambda: self.store.update_project(
                self.db, self.other_board_access, project["id"], 1, {"title": "越界"}
            ),
            lambda: self.store.delete_project(
                self.db, self.other_board_access, project["id"], 1
            ),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(LookupError):
                    operation()

    def test_stale_editor_update_is_rejected(self):
        project = self._project()
        self.store.update_project(
            self.db, self.editor_access, project["id"], 1, {"title": "新版"}
        )
        with self.assertRaises(self.store.RevisionConflict):
            self.store.update_project(
                self.db, self.editor_access, project["id"], 1, {"title": "旧页覆盖"}
            )

    def test_asset_binding_fields_are_rejected_by_generic_create(self):
        restricted = {
            "avatar_asset_id": "avatar-owned",
            "background_asset_id": "background-owned",
            "background_mode": "separate",
        }
        for field, value in restricted.items():
            with self.subTest(operation="create", field=field):
                with self.assertRaises(ValueError):
                    self._project(**{field: value})
        with closing(self.db()) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM digital_presenter_projects"
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_asset_binding_fields_are_rejected_by_generic_update(self):
        restricted = {
            "avatar_asset_id": "avatar-owned",
            "background_asset_id": "background-owned",
            "background_mode": "separate",
        }
        project = self._project()
        for field, value in restricted.items():
            with self.subTest(operation="update", field=field):
                with self.assertRaises(ValueError):
                    self.store.update_project(
                        self.db, self.owner_access, project["id"], 1, {field: value}
                    )
        unchanged = self.store.get_project(self.db, self.owner_access, project["id"])
        self.assertEqual(1, unchanged["revision"])
        self.assertIsNone(unchanged["avatar_asset_id"])
        self.assertIsNone(unchanged["background_asset_id"])
        self.assertEqual("source", unchanged["background_mode"])

    def test_invalid_fields_and_values_do_not_change_revision(self):
        project = self._project()
        invalid_patches = (
            {"owner_username": "attacker"},
            {"board_id": "board-b"},
            {"ratio": "1:1"},
            {"resolution": "720p"},
            {"target_duration": 29},
            {"target_duration": True},
            {"unknown": "field"},
        )
        for patch in invalid_patches:
            with self.subTest(patch=patch):
                with self.assertRaises(ValueError):
                    self.store.update_project(
                        self.db, self.owner_access, project["id"], 1, patch
                    )
        self.assertEqual(
            1, self.store.get_project(self.db, self.owner_access, project["id"])["revision"]
        )

    def test_delete_is_owner_only_revisioned_and_soft(self):
        project = self._project()
        with self.assertRaises(self.store.PermissionDenied):
            self.store.delete_project(
                self.db, self.editor_access, project["id"], project["revision"]
            )
        with self.assertRaises(self.store.RevisionConflict):
            self.store.delete_project(self.db, self.owner_access, project["id"], 9)

        deleted = self.store.delete_project(
            self.db, self.owner_access, project["id"], project["revision"]
        )
        self.assertTrue(deleted["deleted"])
        self.assertEqual(2, deleted["revision"])
        with self.assertRaises(LookupError):
            self.store.get_project(self.db, self.owner_access, project["id"])
        with closing(self.db()) as connection:
            row = connection.execute(
                "SELECT deleted FROM digital_presenter_projects WHERE id=?", (project["id"],)
            ).fetchone()
        self.assertEqual(1, row["deleted"])


if __name__ == "__main__":
    unittest.main()
