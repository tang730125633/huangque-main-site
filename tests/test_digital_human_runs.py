import sqlite3
import sys
import tempfile
import unittest
import importlib
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

class DigitalHumanRunLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.runs = importlib.import_module("content_domains.digital_human_runs")
        cls.legacy = importlib.import_module("content_domains.digital_human_oneclick")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger_path = Path(self.temp.name) / "ledger.sqlite3"
        self.jobs_path = Path(self.temp.name) / "jobs.sqlite3"

        def ledger_db():
            connection = sqlite3.connect(self.ledger_path)
            connection.row_factory = sqlite3.Row
            return connection

        def jobs_db():
            connection = sqlite3.connect(self.jobs_path)
            connection.row_factory = sqlite3.Row
            return connection

        self.ledger_db = ledger_db
        self.jobs_db = jobs_db
        with closing(jobs_db()) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY, username TEXT, kind TEXT, status TEXT,
                result TEXT, error TEXT, cost INTEGER, refunded INTEGER
            )""")
        self.runs.init_db(ledger_db)
        self.jdb_patch = patch.object(self.runs, "jdb", jobs_db)
        self.jdb_patch.start()

    def tearDown(self):
        self.jdb_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def request_data(request_id="request-0001"):
        normalized = {
            "request_id": request_id, "consent_token": "consent-token",
            "plan_digest": "a" * 64, "script": "这是一段用于测试的数字人口播文案。",
            "narration_mode": "text", "audio_upload_id": "",
            "allow_ai_materials": False, "customer_upload_ids": [],
            "portrait_upload_id": "img_" + "b" * 32, "voice_key": "voice-ready",
        }
        plan = {
            "plan_digest": "a" * 64, "copy": normalized["script"],
            "materials": [], "segments": [{"text": normalized["script"]}],
        }
        record = {"run_id": "dh-run-test-0001", "plan_digest": "a" * 64, "id": "consent-id"}
        return normalized, plan, record

    def test_start_replays_same_request_without_duplicate_child_submission(self):
        normalized, plan, record = self.request_data()
        submissions = []

        def submit(kind, body, key, cost):
            submissions.append((kind, key))
            with closing(self.jobs_db()) as connection:
                connection.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                    (1, "alice", kind, "pending", "{}", "", cost, 0),
                )
                connection.commit()
            return 200, {"job_id": 1}

        with patch.object(self.runs, "_normalized_request", return_value=(normalized, plan, record)), \
                patch.object(self.runs, "_cost_breakdown", return_value={
                    "materials_max": 0, "materials_each": [], "talking": 10,
                    "talking_each": [10], "compose": 0, "total": 10,
                }), patch.object(self.runs.points, "cost_of", return_value=10):
            first = self.runs.start_response({}, "alice", 10, submit, self.ledger_db)
            second = self.runs.start_response({}, "alice", 10, submit, self.ledger_db)

        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(1, len(submissions))
        self.assertEqual(first["run"]["run_id"], second["run"]["run_id"])

    def test_run_is_owner_scoped(self):
        normalized, plan, record = self.request_data()
        with patch.object(self.runs, "_normalized_request", return_value=(normalized, plan, record)), \
                patch.object(self.runs, "_cost_breakdown", return_value={
                    "materials_max": 0, "materials_each": [], "talking": 0,
                    "talking_each": [0], "compose": 0, "total": 0,
                }), patch.object(self.runs.points, "cost_of", return_value=0):
            self.runs.start_response({}, "alice", 0, lambda *args: (200, {"job_id": 1}), self.ledger_db)
        with self.assertRaises(self.legacy.DigitalHumanRequestError) as raised:
            self.runs.status_response(record["run_id"], "bob", self.ledger_db)
        self.assertEqual(404, raised.exception.status)

    def test_local_material_failure_remains_terminal_failed(self):
        run = {
            "status": "confirmed", "error": "",
            "steps": {
                "materials": [{"status": "failed", "job_id": 0, "error": "未授权 AI 补图"}],
                "talking": [],
                "compose": {"status": "waiting", "job_id": 0, "error": ""},
            },
        }
        synced = self.runs._sync(run)
        self.assertEqual("failed", synced["status"])
        self.assertEqual("未授权 AI 补图", synced["error"])

    def test_abandoned_run_stays_abandoned_while_children_finish(self):
        run = {
            "status": "abandoned", "error": "用户已放弃后续恢复", "result": {},
            "steps": {
                "materials": [],
                "talking": [{"status": "queued", "job_id": 0, "error": ""}],
                "compose": {"status": "waiting", "job_id": 0, "error": "", "result": {}},
            },
        }
        self.assertEqual("abandoned", self.runs._sync(run)["status"])


if __name__ == "__main__":
    unittest.main()
