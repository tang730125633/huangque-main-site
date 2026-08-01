import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from server.content_domains import short_drama, short_drama_production, short_drama_voice


class ShortDramaProjectLedgerTests(unittest.TestCase):
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
                "status TEXT, payload TEXT, result TEXT, error TEXT, refunded INTEGER DEFAULT 0, "
                "created_at INTEGER, updated_at INTEGER)"
            )
            conn.commit()
        short_drama.init_db(self.db)
        self.owner_access = {"board_id": "board-ledger", "role": "owner"}
        self.editor_access = {"board_id": "board-ledger", "role": "editor"}

    def tearDown(self):
        self.temp.cleanup()

    def create_project(self, budget=100):
        project = short_drama.create_project(
            self.db,
            "alice",
            {
                "title": "统一项目账本",
                "synopsis": "一个用于验证多人协作项目账本的完整故事梗概",
                "ratio": "9:16",
                "target_duration": 30,
                "shot_count": 6,
                "point_budget": budget,
                "board_id": "board-ledger",
            },
            self.owner_access,
        )
        shot_id = "shot-" + project["id"]
        with closing(self.db()) as conn:
            now = int(time.time())
            conn.execute(
                "INSERT INTO short_drama_shots "
                "(id,project_id,script_version,shot_key,sort_order,duration,scene_description,"
                "camera_description,character_keys_json,dialogue_line_ids_json,image_prompt,video_prompt) "
                "VALUES(?,?,1,'S01',1,5,'scene','camera','[]','[]','image','video')",
                (shot_id, project["id"]),
            )
            conn.execute(
                "INSERT INTO short_drama_voice_shots "
                "(shot_id,project_id,locked,timeline_revision,created_at,updated_at) "
                "VALUES(?,?,0,1,?,?)",
                (shot_id, project["id"], now, now),
            )
            conn.commit()
        project["test_shot_id"] = shot_id
        return project

    def add_job(self, username, kind, cost, project_id, *, status="pending", refunded=0):
        payload = {"project_id": project_id}
        if kind == "copy":
            payload["format"] = "short_drama"
        now = int(time.time())
        with closing(self.db()) as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(kind,username,cost,status,payload,result,error,refunded,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'{}','',?,?,?)",
                (kind, username, cost, status, json.dumps(payload), refunded, now, now),
            )
            conn.commit()
            return cursor.lastrowid

    def add_production_link(self, username, project_id, job_id, cost, *, status="pending", refunded=0):
        now = int(time.time())
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_production_jobs "
                "(id,username,project_id,shot_id,kind,job_id,idempotency_key,quoted_cost,status,refunded,created_at,updated_at) "
                "VALUES(?,?,? ,?,'still',?,?,?, ?,?,?,?)",
                (
                    str(uuid.uuid4()), username, project_id, "shot-" + project_id, job_id,
                    "idem-" + str(job_id), cost, status, refunded, now, now,
                ),
            )
            conn.commit()

    def add_attempt(self, username, project_id, cost, state, suffix, job_id=None):
        now = int(time.time())
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_charge_attempts "
                "(charge_key,refund_key,username,endpoint,idempotency_key,request_hash,project_id,shot_id,"
                "quote_token,cost,image_payload_json,state,job_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?, '{}',?,?,?,?)",
                (
                    "charge-" + suffix, "refund-" + suffix, username, "/api/gen/image",
                    "idem-attempt-" + suffix, "hash-" + suffix, project_id, "shot-" + project_id,
                    "quote-" + suffix, cost, state, job_id, now, now,
                ),
            )
            conn.commit()

    def test_planning_charge_is_not_counted_again_as_outstanding(self):
        project = self.create_project(budget=100)
        self.add_job("alice", "copy", 40, project["id"], status="pending")

        rejected = False
        try:
            short_drama.check_planning_budget(
                self.db, "alice", project["id"], 30, self.owner_access
            )
        except short_drama.PointBudgetExceeded:
            rejected = True

        self.assertFalse(rejected, "an already charged planning job must be counted exactly once")

    def test_project_usage_unifies_collaborators_planning_production_and_attempts(self):
        project = self.create_project(budget=100)
        project_id = project["id"]
        self.add_job("alice", "copy", 20, project_id, status="done")
        self.add_job("bob", "copy", 15, project_id, status="pending")
        self.add_job("alice", "copy", 40, project_id, status="error", refunded=1)
        owner_image = self.add_job("alice", "image", 10, project_id, status="pending")
        editor_image = self.add_job("bob", "image", 12, project_id, status="done")
        refunded_image = self.add_job("bob", "image", 20, project_id, status="error", refunded=1)
        self.add_production_link("alice", project_id, owner_image, 10, status="pending")
        self.add_production_link("bob", project_id, editor_image, 12, status="done")
        self.add_production_link("bob", project_id, refunded_image, 20, status="failed", refunded=1)
        self.add_attempt("bob", project_id, 5, "accepted", "accepted")
        self.add_attempt("bob", project_id, 7, "charged", "charged")
        self.add_attempt("alice", project_id, 9, "refund_pending", "refund-pending")
        self.add_attempt("alice", project_id, 11, "refunded", "refunded")

        detail = short_drama.get_project(
            self.db, "bob", project_id, self.editor_access
        )
        self.assertEqual(73, detail["spent_points"])

        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET stage='stills_review' WHERE id=?",
                (project_id,),
            )
            conn.commit()
        rejected = False
        try:
            short_drama_production.check_production_budget(
                self.db, "bob", project_id, 23, self.editor_access
            )
        except short_drama.PointBudgetExceeded:
            rejected = True
        self.assertTrue(rejected, "73 actual + 5 reserved + 23 new exceeds the project budget")

    def test_legacy_spent_points_is_used_only_without_project_ledger_activity(self):
        project = self.create_project(budget=100)
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_projects SET spent_points=17 WHERE id=?",
                (project["id"],),
            )
            conn.commit()
        self.assertEqual(
            17,
            short_drama.get_project(
                self.db, "alice", project["id"], self.owner_access
            )["spent_points"],
        )
        self.add_job("bob", "copy", 8, project["id"], status="error", refunded=1)
        self.assertEqual(
            0,
            short_drama.get_project(
                self.db, "alice", project["id"], self.owner_access
            )["spent_points"],
            "refunded ledger activity supersedes the legacy aggregate without charging it",
        )

    def test_voice_snapshot_uses_the_same_cross_collaborator_ledger(self):
        project = self.create_project(budget=100)
        self.add_job("bob", "copy", 15, project["id"], status="done")
        with closing(self.db()) as conn:
            project_row = conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=?", (project["id"],)
            ).fetchone()
            snapshot = short_drama_voice.build_voice_snapshot(conn, project_row)

        self.assertEqual(15, snapshot["spent_points"])
        self.assertEqual(0, snapshot["reserved_points"])


if __name__ == "__main__":
    unittest.main()
