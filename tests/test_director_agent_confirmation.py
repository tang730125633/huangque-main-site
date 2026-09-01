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


def issued_script_value(db, offer_id, expected_cost=3, now=2_000_000_000):
    cli_input = {
        "request_id": offer_id,
        "topic": "东鹏特饮", "selling_points": "买三送一",
        "style": "口播", "duration": "30s", "platform": "抖音",
    }
    issued = director_agent._issue_production_offer(
        db, "alice", {
            "offer_id": offer_id, "kind": "script",
            "expected_cost": expected_cost, "requires_confirmation": True,
            "input": cli_input,
            "summary": {
                "topic": "东鹏特饮", "style": "口播",
                "duration": "30s", "platform": "抖音",
            },
        }, "a1b2c3d4", now=now,
    )
    return {
        "offer_id": offer_id,
        "expected_cost": expected_cost,
        "input": cli_input,
        "plan_digest": issued["plan_digest"],
        "quote_token": issued["quote_token"],
    }


class DirectorAgentConfirmationTests(unittest.TestCase):
    def test_production_confirmation_accepts_canonical_signed_quote_token_shape(self):
        offer_id = "director-production-1234567890abcdef"
        token = "A" * 80 + "." + "b" * 64
        normalized = director_agent._normalize_production_request({
            "offer_id": offer_id,
            "input": {
                "request_id": offer_id, "topic": "东鹏特饮",
                "selling_points": "买三送一", "style": "种草",
                "duration": "30s", "platform": "抖音",
            },
            "expected_cost": 3,
            "plan_digest": "a" * 64,
            "quote_token": token,
        })
        self.assertEqual(normalized[0], offer_id)
        self.assertEqual(normalized[4], token)

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
            value = issued_script_value(db, offer_id)
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
            value = issued_script_value(db, offer_id)
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
            self.assertNotEqual(value["quote_token"], result["quote_token"])
            self.assertEqual(value["plan_digest"], result["plan_digest"])
            confirm_call.assert_not_called()

            refreshed = dict(
                value, expected_cost=4, quote_token=result["quote_token"],
            )
            with mock.patch.object(
                director_agent.director_cli, "confirm_script",
            ) as confirm_call, self.assertRaises(
                director_agent.DirectorOfferError,
            ) as raised:
                director_agent.produce_script(
                    db, "alice", refreshed,
                    now=result["expires_at"] + 1,
                )
            self.assertEqual("director_offer_expired", raised.exception.code)
            confirm_call.assert_not_called()

    def test_offer_claim_and_attempt_insert_roll_back_together_on_crash(self):
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
            issued_at = 2_000_000_000
            value = issued_script_value(
                db, "director-production-atomic1234567890", now=issued_at,
            )

            def crash_between_claim_and_insert():
                raise RuntimeError("simulated crash")

            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                director_agent.produce_script(
                    db, "alice", value, now=issued_at + 1,
                    before_attempt_insert=crash_between_claim_and_insert,
                )
            with closing(db()) as connection:
                offer = connection.execute(
                    "SELECT confirmed_at FROM director_agent_offers WHERE username='alice'"
                ).fetchone()
                production_table = connection.execute(
                    "SELECT COUNT(1) FROM sqlite_master "
                    "WHERE type='table' AND name='director_cli_productions'"
                ).fetchone()[0]
                attempts = (connection.execute(
                    "SELECT COUNT(1) FROM director_cli_productions"
                ).fetchone()[0] if production_table else 0)
            self.assertIsNone(offer["confirmed_at"])
            self.assertEqual(0, attempts)

            with mock.patch.object(
                director_agent.director_cli, "quote_script",
            ) as quote_call, self.assertRaisesRegex(
                director_agent.DirectorOfferError, "已过期",
            ) as raised:
                director_agent.produce_script(
                    db, "alice", value,
                    now=issued_at + director_agent.OFFER_TTL_SECONDS + 1,
                )
            self.assertEqual("director_offer_expired", raised.exception.code)
            quote_call.assert_not_called()

    def test_price_refresh_uses_old_token_compare_and_swap(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            value = issued_script_value(
                db, "director-production-refresh123456789",
            )
            first = director_agent._rotate_production_offer(
                db, "alice", value["offer_id"], value["plan_digest"],
                value["input"], 4, value["quote_token"], now=2_000_000_001,
            )
            self.assertNotEqual(value["quote_token"], first["quote_token"])
            with self.assertRaises(
                director_agent.DirectorOfferError,
            ) as raised:
                director_agent._rotate_production_offer(
                    db, "alice", value["offer_id"], value["plan_digest"],
                    value["input"], 5, value["quote_token"], now=2_000_000_002,
                )
            self.assertEqual("director_offer_refreshed", raised.exception.code)
            with closing(db()) as connection:
                row = connection.execute(
                    "SELECT expected_cost,token_hash FROM director_agent_offers"
                ).fetchone()
            self.assertEqual(4, row["expected_cost"])
            self.assertEqual(
                director_agent._token_hash(first["quote_token"]),
                row["token_hash"],
            )

    def test_forged_or_expired_offer_is_rejected_before_cli_quote(self):
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
            offer_id = "director-production-forged1234567890"
            forged = {
                "offer_id": offer_id, "expected_cost": 3,
                "input": {
                    "request_id": offer_id, "topic": "绕过确认",
                    "selling_points": "", "style": "口播",
                    "duration": "30s", "platform": "抖音",
                },
                "plan_digest": "a" * 64,
                "quote_token": "forged_confirmation_token_1234",
            }
            with mock.patch.object(
                director_agent.director_cli, "quote_script",
            ) as quote_call, self.assertRaisesRegex(ValueError, "服务器签发"):
                director_agent.produce_script(
                    db, "alice", forged, now=2_000_000_000,
                )
            quote_call.assert_not_called()

            expired = issued_script_value(
                db, "director-production-expired123456789",
                now=2_000_000_000,
            )
            with mock.patch.object(
                director_agent.director_cli, "quote_script",
            ) as quote_call, self.assertRaisesRegex(ValueError, "已过期"):
                director_agent.produce_script(
                    db, "alice", expired,
                    now=2_000_000_000 + director_agent.OFFER_TTL_SECONDS + 1,
                )
            quote_call.assert_not_called()

    def test_director_agent_uses_dedicated_queue(self):
        self.assertIs(
            core._pick_job_queue("director_agent"),
            core._director_agent_job_queue,
        )
        self.assertIsNot(core._director_agent_job_queue, core._fast_job_queue)
        self.assertNotIn("private_domain_video", director_agent.NAV_TARGETS)

    def test_digital_human_guide_contract_is_required(self):
        context = {
            "page": "digital_human_oneclick",
            "path": "/workbench/digital-human-oneclick.html",
            "mode": "photo", "narration_mode": "text",
            "script_text": "测试口播", "script_length": 4,
            "has_portrait": False, "has_video_source": False,
            "has_voice_source": False, "has_drive_audio": False,
            "customer_material_count": 0, "consent_confirmed": False,
            "precision_template": "", "has_result": False,
            "active_job_status": "idle",
        }
        with self.assertRaisesRegex(ValueError, "引导契约无效"):
            director_agent._digital_human_page_context(context)
        context["guide_contract"] = director_agent.DIGITAL_HUMAN_GUIDE_CONTRACT
        self.assertEqual(
            director_agent.DIGITAL_HUMAN_GUIDE_CONTRACT,
            director_agent._digital_human_page_context(context)["guide_contract"],
        )

    def test_http_production_route_rejects_unissued_offer_before_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            offer_id = "director-production-httpforged123456"
            body = {
                "offer_id": offer_id, "expected_cost": 3,
                "input": {
                    "request_id": offer_id, "topic": "绕过确认",
                    "selling_points": "", "style": "口播",
                    "duration": "30s", "platform": "抖音",
                },
                "plan_digest": "a" * 64,
                "quote_token": "forged_confirmation_token_1234",
            }
            patches = [
                mock.patch.object(core, "jdb", db),
                mock.patch.object(core, "verify", lambda token: {
                    "username": "alice", "must_change": False, "points": 99,
                } if token else None),
                mock.patch.object(core.feature_flags, "require_enabled", lambda _key: None),
                mock.patch.object(
                    director_agent.director_cli, "production_is_available",
                    return_value=True,
                ),
                mock.patch.object(director_agent.director_cli, "quote_script"),
            ]
            started = []
            for item in patches:
                started.append(item.start())
                self.addCleanup(item.stop)
            quote_call = started[-1]
            server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            url = "http://127.0.0.1:%d/api/gen/director_agent/produce" % server.server_address[1]
            request = urllib.request.Request(
                url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                method="POST", headers={
                    "Authorization": "Bearer alice",
                    "Content-Type": "application/json",
                    "Idempotency-Key": offer_id,
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(400, raised.exception.code)
            response = json.loads(raised.exception.read())
            self.assertIn("服务器签发", response["detail"])
            self.assertEqual("director_offer_invalid", response["code"])
            quote_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
