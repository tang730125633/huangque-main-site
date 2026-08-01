import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama


def project_payload(**changes):
    payload = {
        "title": "独立短剧项目",
        "synopsis": "用于验证独立短剧中心与画布项目的数据边界。",
        "ratio": "9:16",
        "target_duration": 30,
        "shot_count": 6,
        "visual_style": "电影写实",
        "point_budget": 100,
    }
    payload.update(changes)
    return payload


class ShortDramaStandaloneScopeContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.database)
        short_drama.init_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_personal_and_canvas_project_lists_are_disjoint(self):
        alice_local = short_drama.create_project(
            self.db, "alice", project_payload(title="Alice 的个人短剧")
        )
        bob_local = short_drama.create_project(
            self.db, "bob", project_payload(title="Bob 的个人短剧")
        )
        shared = short_drama.create_project(
            self.db,
            "alice",
            project_payload(title="画布共享短剧", board_id="board-1"),
            access={"board_id": "board-1", "role": "owner"},
        )

        self.assertIsNone(alice_local["board_id"])
        self.assertIsNone(bob_local["board_id"])
        self.assertEqual("board-1", shared["board_id"])
        self.assertEqual(
            [alice_local["id"]],
            [item["id"] for item in short_drama.list_projects(
                self.db, "alice"
            )["items"]],
        )
        self.assertEqual(
            [bob_local["id"]],
            [item["id"] for item in short_drama.list_projects(
                self.db, "bob"
            )["items"]],
        )
        self.assertEqual(
            [shared["id"]],
            [item["id"] for item in short_drama.list_projects(
                self.db,
                "bob",
                access={"board_id": "board-1", "role": "viewer"},
            )["items"]],
        )

    def test_personal_projects_are_discoverable_only_by_the_creator(self):
        project = short_drama.create_project(
            self.db, "alice", project_payload()
        )

        self.assertEqual(
            project["id"],
            short_drama.get_project(self.db, "alice", project["id"])["id"],
        )
        with self.assertRaises(LookupError):
            short_drama.get_project(self.db, "bob", project["id"])

    def test_canvas_projects_require_matching_trusted_access(self):
        project = short_drama.create_project(
            self.db,
            "alice",
            project_payload(board_id="board-1"),
            access={"board_id": "board-1", "role": "owner"},
        )

        for access in (
            None,
            {"board_id": "board-2", "role": "owner"},
            {"board_id": "board-1", "role": ""},
        ):
            with self.subTest(access=access):
                with self.assertRaises(LookupError):
                    short_drama.get_project(
                        self.db, "alice", project["id"], access=access
                    )

        for role in ("owner", "editor", "viewer"):
            with self.subTest(role=role):
                fetched = short_drama.get_project(
                    self.db,
                    "bob",
                    project["id"],
                    access={"board_id": "board-1", "role": role},
                )
                self.assertEqual(project["id"], fetched["id"])

    def test_canvas_write_access_and_creation_are_role_scoped(self):
        with self.assertRaises(PermissionError):
            short_drama.create_project(
                self.db,
                "viewer",
                project_payload(board_id="board-1"),
                access={"board_id": "board-1", "role": "viewer"},
            )
        with self.assertRaises(PermissionError):
            short_drama.create_project(
                self.db,
                "editor",
                project_payload(board_id="board-1"),
                access={"board_id": "board-2", "role": "editor"},
            )

        project = short_drama.create_project(
            self.db,
            "owner",
            project_payload(board_id="board-1"),
            access={"board_id": "board-1", "role": "owner"},
        )
        self.assertEqual(
            "owner",
            short_drama._project_username_for_access(
                self.db,
                "editor",
                project["id"],
                access={"board_id": "board-1", "role": "editor"},
                write=True,
            ),
        )
        with self.assertRaises(PermissionError):
            short_drama._project_username_for_access(
                self.db,
                "viewer",
                project["id"],
                access={"board_id": "board-1", "role": "viewer"},
                write=True,
            )


if __name__ == "__main__":
    unittest.main()
