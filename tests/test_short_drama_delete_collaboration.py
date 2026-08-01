import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from server.content_domains import short_drama


class DeleteHandler:
    path = "/api/gen/short-drama/project/delete"

    def __init__(self, username, body):
        self.username = username
        self.body = body
        self.response = None

    def _token(self):
        return self.username

    def _json_body_strict(self):
        return dict(self.body)

    def _send(self, status, payload):
        self.response = (status, payload)


class ShortDramaCollaborativeDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "content.db"

        def db_factory():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

        self.db = db_factory
        with closing(self.db()) as conn:
            conn.execute(
                "CREATE TABLE jobs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, username TEXT, cost INTEGER, "
                "status TEXT, payload TEXT, refunded INTEGER DEFAULT 0)"
            )
            conn.commit()
        short_drama.init_db(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def create_project(self):
        return short_drama.create_project(
            self.db,
            "alice",
            {
                "title": "Collaborative delete",
                "synopsis": "A sufficiently detailed synopsis for collaborative deletion checks.",
                "ratio": "9:16",
                "target_duration": 30,
                "shot_count": 6,
                "point_budget": 100,
                "board_id": "board-delete",
            },
            {"board_id": "board-delete", "role": "owner"},
        )

    def dispatch_delete(self, username, role, project):
        handler = DeleteHandler(
            username, {"project_id": project["id"], "revision": project["revision"]}
        )
        short_drama.dispatch_http(
            handler,
            "POST",
            self.db,
            lambda token: {"username": token, "must_change": False},
            avatar_lookup=lambda _username, _avatar_id: None,
            canvas_access_resolver=lambda _handler: {
                "board_id": "board-delete",
                "role": role,
            },
        )
        return handler.response

    def deleted_value(self, project_id):
        with closing(self.db()) as conn:
            return conn.execute(
                "SELECT deleted FROM short_drama_projects WHERE id=?", (project_id,)
            ).fetchone()[0]

    def test_editor_delete_delegates_to_the_project_owner(self):
        project = self.create_project()

        status, payload = self.dispatch_delete("bob", "editor", project)

        self.assertEqual(200, status)
        self.assertTrue(payload["deleted"])
        self.assertEqual(1, self.deleted_value(project["id"]))

    def test_viewer_delete_is_forbidden_and_keeps_the_project(self):
        project = self.create_project()

        status, payload = self.dispatch_delete("carol", "viewer", project)

        self.assertEqual(403, status)
        self.assertEqual("forbidden", payload["code"])
        self.assertEqual(0, self.deleted_value(project["id"]))

    def test_owner_delete_remains_supported(self):
        project = self.create_project()

        status, payload = self.dispatch_delete("alice", "owner", project)

        self.assertEqual(200, status)
        self.assertTrue(payload["deleted"])
        self.assertEqual(1, self.deleted_value(project["id"]))

    def test_editor_delete_keeps_the_existing_paid_job_blocker(self):
        project = self.create_project()
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO jobs(kind,username,cost,status,payload,refunded) "
                "VALUES('copy','bob',10,'pending',?,0)",
                (json.dumps({"format": "short_drama", "project_id": project["id"]}),),
            )
            conn.commit()

        status, payload = self.dispatch_delete("bob", "editor", project)

        self.assertEqual(409, status)
        self.assertEqual("short_drama_unapplied_paid_job", payload["code"])
        self.assertEqual(0, self.deleted_value(project["id"]))


if __name__ == "__main__":
    unittest.main()
