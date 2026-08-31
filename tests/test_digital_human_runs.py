import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import importlib
from concurrent.futures import ThreadPoolExecutor
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
            self.runs.status_response(
                record["run_id"], "bob", lambda *args: None, self.ledger_db,
            )
        self.assertEqual(404, raised.exception.status)

    def test_completed_children_advance_compose_once_under_concurrent_status_and_recover(self):
        normalized, plan, record = self.request_data()
        submissions = []
        submission_lock = threading.Lock()

        def submit(kind, body, key, cost):
            with submission_lock:
                submissions.append((kind, key, cost))
                job_id = 1 if kind == "video" else 2
            if kind == "script_to_video":
                time.sleep(0.1)
            with closing(self.jobs_db()) as connection:
                connection.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                    (
                        job_id, "alice", kind,
                        "pending" if kind == "video" else "done",
                        "{}" if kind == "video" else '{"file":"final.mp4"}',
                        "", cost, 0,
                    ),
                )
                connection.commit()
            return 200, {"job_id": job_id}

        with patch.object(self.runs, "_normalized_request", return_value=(normalized, plan, record)), \
                patch.object(self.runs, "_cost_breakdown", return_value={
                    "materials_max": 0, "materials_each": [], "talking": 10,
                    "talking_each": [10], "compose": 0, "total": 10,
                }), patch.object(self.runs.points, "cost_of", return_value=10):
            started = self.runs.start_response(
                {}, "alice", 10, submit, self.ledger_db,
            )

        self.assertEqual("running", started["run"]["status"])
        self.assertEqual("waiting", started["run"]["steps"]["compose"]["status"])
        with closing(self.jobs_db()) as connection:
            connection.execute(
                "UPDATE jobs SET status='done',result=? WHERE id=1",
                ('{"file":"talking.mp4"}',),
            )
            connection.commit()

        barrier = threading.Barrier(3)

        def query_status():
            barrier.wait()
            return self.runs.status_response(
                record["run_id"], "alice", submit, self.ledger_db,
            )

        def recover():
            barrier.wait()
            return self.runs.recover_response(
                record["run_id"], {"request_id": normalized["request_id"]},
                "alice", submit, self.ledger_db,
            )

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(query_status),
                executor.submit(query_status),
                executor.submit(recover),
            ]
            responses = [future.result(timeout=10) for future in futures]

        self.assertTrue(all(item["run"]["status"] == "completed" for item in responses))
        self.assertEqual(1, [item[0] for item in submissions].count("video"))
        self.assertEqual(1, [item[0] for item in submissions].count("script_to_video"))
        self.assertEqual(
            "final.mp4",
            self.runs.status_response(
                record["run_id"], "alice", submit, self.ledger_db,
            )["run"]["result"]["file"],
        )

    def test_unknown_refunded_retry_reuses_persisted_key_after_restart(self):
        normalized, plan, record = self.request_data()
        video_keys = []
        compose_keys = []
        recovery_job_id = {"value": 0}

        def submit(kind, body, key, cost):
            if kind == "script_to_video":
                compose_keys.append(key)
                with closing(self.jobs_db()) as connection:
                    connection.execute(
                        "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                        (43, "alice", kind, "done", '{"file":"final.mp4"}', "", 0, 0),
                    )
                    connection.commit()
                return 200, {"job_id": 43}

            video_keys.append(key)
            if len(video_keys) == 1:
                with closing(self.jobs_db()) as connection:
                    connection.execute(
                        "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                        (41, "alice", kind, "pending", "{}", "", cost, 0),
                    )
                    connection.commit()
                return 200, {"job_id": 41}

            if not recovery_job_id["value"]:
                recovery_job_id["value"] = 42
                with closing(self.jobs_db()) as connection:
                    connection.execute(
                        "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                        (42, "alice", kind, "pending", "{}", "", cost, 0),
                    )
                    connection.commit()
                return 502, {
                    "code": "child_submit_unknown",
                    "detail": "上游已受理，但响应丢失",
                }
            return 200, {"job_id": recovery_job_id["value"]}

        with patch.object(self.runs, "_normalized_request", return_value=(normalized, plan, record)), \
                patch.object(self.runs, "_cost_breakdown", return_value={
                    "materials_max": 0, "materials_each": [], "talking": 10,
                    "talking_each": [10], "compose": 0, "total": 10,
                }), patch.object(self.runs.points, "cost_of", return_value=10):
            self.runs.start_response({}, "alice", 10, submit, self.ledger_db)

        with closing(self.jobs_db()) as connection:
            connection.execute(
                "UPDATE jobs SET status='failed',error='upstream failed',refunded=1 "
                "WHERE id=41",
            )
            connection.commit()

        status = self.runs.status_response(
            record["run_id"], "alice", submit, self.ledger_db,
        )
        self.assertEqual("recoverable", status["run"]["status"])
        unknown = self.runs.recover_response(
            record["run_id"], {"request_id": normalized["request_id"]},
            "alice", submit, self.ledger_db,
        )
        uncertain_step = unknown["run"]["steps"]["talking"][0]
        self.assertEqual("needs_attention", uncertain_step["status"])
        self.assertEqual(0, uncertain_step["job_id"])
        self.assertEqual(2, uncertain_step["attempt"])
        self.assertTrue(uncertain_step["submission_uncertain"])
        self.assertNotIn("idempotency_key", uncertain_step)

        with closing(self.ledger_db()) as connection:
            persisted = json.loads(connection.execute(
                "SELECT steps_json FROM digital_human_runs WHERE run_id=?",
                (record["run_id"],),
            ).fetchone()["steps_json"])["talking"][0]
        self.assertEqual(video_keys[-1], persisted["idempotency_key"])
        self.assertTrue(persisted["submission_uncertain"])

        observed = self.runs.status_response(
            record["run_id"], "alice", submit, self.ledger_db,
        )
        self.assertEqual("needs_attention", observed["run"]["status"])
        self.assertEqual(2, len(video_keys))

        fresh_locks = tuple(threading.RLock() for _ in range(64))
        with patch.object(self.runs, "_RUN_LOCKS", fresh_locks):
            replayed = self.runs.recover_response(
                record["run_id"], {"request_id": normalized["request_id"]},
                "alice", submit, self.ledger_db,
            )
        self.assertEqual("running", replayed["run"]["status"])
        self.assertEqual(video_keys[1], video_keys[2])
        self.assertEqual(2, replayed["run"]["steps"]["talking"][0]["attempt"])

        with closing(self.jobs_db()) as connection:
            connection.execute(
                "UPDATE jobs SET status='done',result=? WHERE id=42",
                ('{"file":"talking.mp4"}',),
            )
            connection.commit()
        completed = self.runs.status_response(
            record["run_id"], "alice", submit, self.ledger_db,
        )
        self.assertEqual("completed", completed["run"]["status"])
        self.assertEqual(1, len(compose_keys))

        with closing(self.jobs_db()) as connection:
            paid_rows = connection.execute(
                "SELECT id,cost,refunded FROM jobs WHERE kind='video' ORDER BY id",
            ).fetchall()
        self.assertEqual([41, 42], [row["id"] for row in paid_rows])
        self.assertEqual(1, paid_rows[0]["refunded"])
        self.assertEqual(10, sum(
            row["cost"] for row in paid_rows if row["refunded"] == 0
        ))

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
