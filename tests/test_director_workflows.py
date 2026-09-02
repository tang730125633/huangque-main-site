import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys
from unittest import mock


SERVER = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
from content_domains import director_workflows  # noqa: E402


class DirectorWorkflowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "jobs.db"

        def database():
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            return connection

        self.db = database
        with self.db() as connection:
            connection.execute(
                "CREATE TABLE jobs(id INTEGER PRIMARY KEY,kind TEXT,username TEXT,status TEXT,cost INTEGER DEFAULT 0,result TEXT,error TEXT,refunded INTEGER DEFAULT 0,updated_at INTEGER DEFAULT 0,deleted INTEGER DEFAULT 0)"
            )
        director_workflows.init_db(self.db)
        self.storyboard = [{
            "id": "scene_01", "title": "开场", "scene": "雨夜街头",
            "line": "故事从这里开始", "dur": 3,
        }]

    def tearDown(self):
        self.temp.cleanup()

    def test_create_replay_update_export_and_owner_scope(self):
        value = {"title": "雨夜故事", "storyboard": self.storyboard}
        created = director_workflows.create(self.db, "alice", value, "workflow-create-0001")
        replay = director_workflows.create(self.db, "alice", value, "workflow-create-0001")
        self.assertFalse(created["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(created["workflow_id"], replay["workflow_id"])
        self.assertEqual(1, director_workflows.list_workflows(self.db, "alice")["total"])
        self.assertEqual(0, director_workflows.list_workflows(self.db, "bob")["total"])
        with self.assertRaises(director_workflows.WorkflowError) as hidden:
            director_workflows.get_workflow(self.db, "bob", created["workflow_id"])
        self.assertEqual(404, hidden.exception.status)

        updated = director_workflows.update_storyboard(self.db, "alice", created["workflow_id"], {
            "revision": 1,
            "storyboard": [{**self.storyboard[0], "line": "新的开场台词"}],
        })
        self.assertEqual(2, updated["revision"])
        with self.assertRaises(director_workflows.WorkflowError) as conflict:
            director_workflows.update_storyboard(self.db, "alice", created["workflow_id"], {
                "revision": 1, "storyboard": self.storyboard,
            })
        self.assertEqual("revision_conflict", conflict.exception.code)
        exported = director_workflows.export_storyboard(self.db, "alice", created["workflow_id"])
        self.assertIn("新的开场台词", exported["markdown"])

        with self.assertRaises(director_workflows.WorkflowError) as reused:
            director_workflows.create(
                self.db, "alice", {"title": "另一个", "storyboard": self.storyboard},
                "workflow-create-0001",
            )
        self.assertEqual("idempotency_conflict", reused.exception.code)

    def test_create_from_owned_completed_script_job_only(self):
        result = {"scenes": [{"scene": "办公室产品特写", "line": "新品来了", "dur": 4}]}
        with self.db() as connection:
            connection.execute(
                "INSERT INTO jobs(id,kind,username,status,result) VALUES(1,'copy','alice','done',?)",
                (json.dumps(result, ensure_ascii=False),),
            )
            connection.execute(
                "INSERT INTO jobs(id,kind,username,status,result) VALUES(2,'copy','bob','done',?)",
                (json.dumps(result, ensure_ascii=False),),
            )
        created = director_workflows.create(
            self.db, "alice", {"title": "任务导入", "source_job_id": 1},
            "workflow-source-0001",
        )
        self.assertEqual(1, created["source_job_id"])
        self.assertEqual("办公室产品特写", created["storyboard"][0]["scene"])
        with self.assertRaises(director_workflows.WorkflowError):
            director_workflows.create(
                self.db, "alice", {"title": "越权", "source_job_id": 2},
                "workflow-source-0002",
            )

    def test_production_and_remake_freeze_quote_submit_status_and_recover(self):
        workflow = director_workflows.create(
            self.db, "alice", {"title": "生产", "storyboard": self.storyboard},
            "workflow-production-0001",
        )
        production = director_workflows.create_plan(
            self.db, "alice", workflow["workflow_id"], "production",
            {"output_kind": "image", "options": {"ratio": "9:16", "quality": "standard"}},
        )
        quote = director_workflows.quote_plan(
            self.db, "alice", workflow["workflow_id"], "production",
            {"plan_digest": production["plan_digest"], "request_id": "production-start-0001"},
            lambda kind, payload: 12,
        )
        self.assertEqual(12, quote["cost"])
        with mock.patch.object(
                director_workflows, "_submit_child",
                return_value={"job_id": 9, "cost": 12, "points_left": 88}) as submit:
            started = director_workflows.start_run(
                self.db, "alice", workflow["workflow_id"], "production",
                {"plan_digest": production["plan_digest"], "request_id": "production-start-0001"},
                12, "web-token", "internal-token", lambda kind, payload: 12,
            )
            replay = director_workflows.start_run(
                self.db, "alice", workflow["workflow_id"], "production",
                {"plan_digest": production["plan_digest"], "request_id": "production-start-0001"},
                12, "web-token", "internal-token", lambda kind, payload: 12,
            )
        self.assertEqual(9, started["job_id"])
        self.assertEqual(started["run_id"], replay["run_id"])
        submit.assert_called_once()
        with self.db() as connection:
            connection.execute(
                "INSERT INTO jobs(id,kind,username,status,cost,result,updated_at) "
                "VALUES(9,'image','alice','done',12,'{}',1)"
            )
        status = director_workflows.run_status(
            self.db, "alice", workflow["workflow_id"], "production",
        )
        self.assertEqual("completed", status["state"])

        remake = director_workflows.create_plan(
            self.db, "alice", workflow["workflow_id"], "remake",
            {"mode": "grok", "instruction": "保持运镜，换成新品", "options": {"ratio": "9:16", "duration": 6}},
        )
        self.assertEqual("video-generate", remake["action"])

        director_workflows.update_storyboard(
            self.db, "alice", workflow["workflow_id"],
            {"revision": 1, "storyboard": [{**self.storyboard[0], "scene": "新画面"}]},
        )
        with self.assertRaises(director_workflows.WorkflowError) as stale:
            director_workflows.quote_plan(
                self.db, "alice", workflow["workflow_id"], "production",
                {"plan_digest": production["plan_digest"], "request_id": "production-start-0002"},
                lambda kind, payload: 12,
            )
        self.assertEqual("plan_stale", stale.exception.code)


if __name__ == "__main__":
    unittest.main()
