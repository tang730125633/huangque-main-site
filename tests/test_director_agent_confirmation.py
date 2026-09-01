import concurrent.futures
import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import core, director_agent


def payload(**overrides):
    value = {
        "prompt": "帮我生成一份分镜脚本",
        "session_id": "director_session_123",
        "page_revision": "a1b2c3d4",
        "page_context": {
            "page": "script", "path": "/workbench/script.html", "mode": "write",
            "topic": "东鹏特饮", "selling_points": "买三送一", "style": "口播",
            "duration": "30s", "platform": "抖音", "has_script": False,
            "scene_count": 0, "has_breakdown": False, "breakdown_scene_count": 0,
            "breakdown_url": "", "breakdown_tool": "scenes",
            "has_reverse_prompt": False, "active_job_status": "idle",
        },
        "history": [], "source_page": "script", "provider": "openai_responses",
        "quoted_cost": 0,
    }
    value.update(overrides)
    return value


class DirectorAgentConfirmationTests(unittest.TestCase):
    def test_ready_request_returns_direct_production_question(self):
        request = director_agent.validate_payload(payload())
        request.update(_username="alice", _job_id=42)
        raw = json.dumps({
            "content": "我已经把参数填好了。",
            "stage": "production",
            "actions": [],
            "warnings": [],
            "offer_production": True,
        }, ensure_ascii=False)
        with mock.patch.object(
            director_agent.director_cli, "production_is_available", return_value=True,
        ), mock.patch(
            "content_domains.points.cost_of", return_value=3,
        ):
            result = director_agent.normalize_model_result(raw, request)
        self.assertEqual(
            "生产信息已经准备好，预计扣除 3 点。是否开始生产？",
            result["content"],
        )
        self.assertEqual(3, result["production_offer"]["expected_cost"])
        self.assertTrue(result["production_offer"]["requires_confirmation"])
        self.assertEqual("东鹏特饮", result["production_offer"]["summary"]["topic"])

    def test_chat_job_claim_is_atomic_and_replayable_without_schema_change(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(db()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT, username TEXT, cost INTEGER,
                    status TEXT DEFAULT 'pending', payload TEXT, result TEXT,
                    error TEXT, created_at INTEGER, updated_at INTEGER,
                    deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0,
                    owner TEXT
                )""")
                connection.commit()

            request = director_agent.validate_payload(payload())

            def submit(_index):
                return director_agent.accept_chat_job(
                    db, "alice", request, "content", "/api/gen/director_agent",
                    "director-agent-idem-0001", points_left=99,
                    max_active_jobs=20, now=2_000_000_000,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(submit, range(8)))
            self.assertEqual(1, sum(state == "new" for state, _ in results))
            self.assertEqual({"new", "replay"}, {state for state, _ in results})
            self.assertEqual(1, len({item["job_id"] for _, item in results}))
            with closing(db()) as connection:
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(1) FROM jobs WHERE kind='director_agent'"
                ).fetchone()[0])
                columns = [row[1] for row in connection.execute(
                    "PRAGMA table_info(submission_idempotency)"
                )]
            self.assertEqual(
                ["username", "endpoint", "idem_key", "request_hash",
                 "response_json", "created_at", "updated_at"],
                columns,
            )

            changed = dict(request, prompt="换一个请求")
            state, response = director_agent.accept_chat_job(
                db, "alice", changed, "content", "/api/gen/director_agent",
                "director-agent-idem-0001", points_left=99,
                max_active_jobs=20, now=2_000_000_001,
            )
            self.assertEqual("conflict", state)
            self.assertIsNone(response)

    def test_http_chat_route_accepts_once_and_replays_same_job(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(db()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT, username TEXT, cost INTEGER,
                    status TEXT DEFAULT 'pending', payload TEXT, result TEXT,
                    error TEXT, created_at INTEGER, updated_at INTEGER,
                    deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0,
                    owner TEXT
                )""")
                connection.commit()

            patches = [
                mock.patch.object(core, "jdb", db),
                mock.patch.object(core, "verify", lambda token: {
                    "username": "alice", "must_change": False, "points": 99,
                } if token else None),
                mock.patch.object(core, "_domains", return_value=(
                    SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
                )),
                mock.patch.object(core.feature_flags, "require_enabled", lambda _key: None),
                mock.patch.object(core, "_director_agent_available", return_value=True),
                mock.patch.object(core.miniprogram_security, "check_payload", lambda _body: None),
                mock.patch.object(core, "enqueue_job", return_value=True),
            ]
            for item in patches:
                item.start()
                self.addCleanup(item.stop)

            server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            url = "http://127.0.0.1:%d/api/gen/director_agent" % server.server_address[1]

            def post(body):
                request = urllib.request.Request(
                    url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    method="POST", headers={
                        "Authorization": "Bearer alice",
                        "Content-Type": "application/json",
                        "Idempotency-Key": "director-agent-http-0001",
                    },
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())

            first_status, first = post(payload())
            replay_status, replay = post(payload())
            self.assertEqual((200, 200), (first_status, replay_status))
            self.assertEqual(first["job_id"], replay["job_id"])
            self.assertEqual(0, first["cost"])
            with closing(db()) as connection:
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(1) FROM jobs WHERE kind='director_agent'"
                ).fetchone()[0])

    def test_confirmed_script_production_quotes_submits_once_and_replays(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(db()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY, kind TEXT, username TEXT,
                    cost INTEGER, status TEXT
                )""")
                connection.execute(
                    "INSERT INTO jobs(id,kind,username,cost,status) VALUES(77,'copy','alice',3,'pending')"
                )
                connection.commit()

            offer_id = "director-production-1234567890abcdef"
            value = {
                "offer_id": offer_id,
                "expected_cost": 3,
                "input": {
                    "request_id": offer_id,
                    "topic": "东鹏特饮", "selling_points": "买三送一",
                    "style": "口播", "duration": "30s", "platform": "抖音",
                },
            }
            quote = {
                "quote_token": "q" * 24, "cost": 3,
                "points": 99, "expires_in": 60,
            }
            submitted = {"job_id": 77, "points_left": 96}
            with mock.patch.object(
                director_agent.director_cli, "quote_script", return_value=quote,
            ) as quote_call, mock.patch.object(
                director_agent.director_cli, "confirm_script", return_value=submitted,
            ) as confirm_call:
                first_status, first = director_agent.produce_script(
                    db, "alice", value, now=2_000_000_000,
                )
                replay_status, replay = director_agent.produce_script(
                    db, "alice", value, now=2_000_000_001,
                )
            self.assertEqual((200, 200), (first_status, replay_status))
            self.assertEqual(77, first["job_id"])
            self.assertFalse(first["recovered"])
            self.assertTrue(replay["recovered"])
            quote_call.assert_called_once()
            confirm_call.assert_called_once()

    def test_confirmed_script_production_requotes_changed_price_without_submit(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(db()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY, kind TEXT, username TEXT,
                    cost INTEGER, status TEXT
                )""")
                connection.commit()
            offer_id = "director-production-fedcba0987654321"
            value = {
                "offer_id": offer_id,
                "expected_cost": 3,
                "input": {
                    "request_id": offer_id,
                    "topic": "东鹏特饮", "selling_points": "买三送一",
                    "style": "口播", "duration": "30s", "platform": "抖音",
                },
            }
            with mock.patch.object(
                director_agent.director_cli, "quote_script", return_value={
                    "quote_token": "q" * 24, "cost": 4,
                    "points": 99, "expires_in": 60,
                },
            ), mock.patch.object(
                director_agent.director_cli, "confirm_script",
            ) as confirm_call:
                status, result = director_agent.produce_script(
                    db, "alice", value, now=2_000_000_000,
                )
            self.assertEqual(409, status)
            self.assertEqual("production_price_changed", result["code"])
            self.assertEqual(4, result["current_cost"])
            confirm_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
