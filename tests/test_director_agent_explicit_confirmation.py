import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import core, director_agent


def payload(prompt="帮我做一个东鹏特饮分镜脚本", **overrides):
    value = {
        "prompt": prompt,
        "session_id": "director_session_fixed_123",
        "page_revision": "a1b2c3d4",
        "page_context": {
            "page": "script", "path": "/workbench/script.html",
            "mode": "write", "topic": "东鹏特饮",
            "selling_points": "买三送一", "style": "口播",
            "duration": "30s", "platform": "抖音",
            "has_script": False, "scene_count": 0,
            "has_breakdown": False, "breakdown_scene_count": 0,
            "breakdown_url": "", "breakdown_tool": "scenes",
            "has_reverse_prompt": False, "active_job_status": "idle",
        },
        "history": [], "source_page": "script",
        "provider": "openai_responses", "quoted_cost": 0,
    }
    value.update(overrides)
    return value


def model_result(offer=True, actions=None):
    return json.dumps({
        "content": "方案已经整理好了。",
        "stage": "production",
        "actions": list(actions or []),
        "warnings": [],
        "offer_production": offer,
    }, ensure_ascii=False)


class ExplicitConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = pathlib.Path(self.temp.name) / "jobs.db"

    def db(self):
        connection = sqlite3.connect(str(self.database), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def request(self, **overrides):
        request = director_agent.validate_payload(payload(**overrides))
        request.update(_username="alice", _job_id=42)
        return request

    def plan(self, request=None, cost=3):
        request = request or self.request()
        with mock.patch.object(
            director_agent.director_cli, "production_is_available",
            return_value=True,
        ), mock.patch(
            "content_domains.points.cost_of", return_value=cost,
        ):
            return director_agent._script_production_offer(
                request, [], True, force_write=True,
                topic_fallback=request["page_context"]["topic"],
            )

    def save(self, request=None, cost=3, now=2_000_000_000):
        request = request or self.request()
        return director_agent._save_pending_plan(
            self.db, "alice", request["session_id"],
            self.plan(request, cost=cost), now=now,
        )

    def test_only_exact_current_turn_command_matches(self):
        accepted = ("确认生成", " 确认生成 ", "\n确认生成\t")
        rejected = (
            "确认生成。", "请确认生成", "确认生成吧", "我要确认生成",
            "确认 生成", "继续", "开始吧", "直接做", "可以开始了",
            "给我看看", "确认生产", "确认开始", "确认生成\n继续",
        )
        for prompt in accepted:
            with self.subTest(prompt=prompt):
                self.assertTrue(director_agent._explicit_script_production_request(
                    self.request(prompt=prompt)
                ))
        for prompt in rejected:
            with self.subTest(prompt=prompt):
                request = self.request(prompt=prompt)
                request["history"] = [{"role": "assistant", "content": "确认生成"}]
                request["page_context"]["topic"] = "确认生成"
                self.assertFalse(
                    director_agent._explicit_script_production_request(request)
                )
        self.assertFalse(director_agent._explicit_script_production_request({
            "prompt": "确认生成",
            "page_context": {"page": "digital_human_oneclick"},
        }))

    def test_natural_language_prepares_durable_plan_then_exact_command_opens_card(self):
        raw = model_result(offer=True)
        body = payload()
        body.update(_username="alice", _job_id=42)
        with mock.patch.object(core, "jdb", self.db), mock.patch.object(
            director_agent, "_responses_chat", return_value=raw,
        ), mock.patch.object(
            director_agent.director_cli, "production_is_available",
            return_value=True,
        ), mock.patch(
            "content_domains.points.cost_of", return_value=3,
        ):
            prepared = director_agent.gen_director_agent(body)
        self.assertNotIn("production_offer", prepared)
        self.assertIn("请回复：确认生成", prepared["content"])
        with closing(self.db()) as connection:
            row = connection.execute(
                "SELECT * FROM director_agent_pending_plans"
            ).fetchone()
        self.assertEqual("director_session_fixed_123", row["session_id"])
        self.assertEqual(3, row["expected_cost"])
        self.assertEqual("东鹏特饮", json.loads(row["input_json"])["topic"])

        confirm_body = payload(
            prompt="确认生成", page_revision=row["page_revision"],
        )
        confirm_body.update(_username="alice", _job_id=43)
        with mock.patch.object(core, "jdb", self.db), mock.patch.object(
            director_agent, "_responses_chat",
        ) as model_call, mock.patch.object(
            director_agent.director_cli, "production_is_available",
            return_value=True,
        ), mock.patch(
            "content_domains.points.cost_of", return_value=3,
        ):
            confirmed = director_agent.gen_director_agent(confirm_body)
            repeated = director_agent.gen_director_agent(confirm_body)
        model_call.assert_not_called()
        self.assertEqual("script", confirmed["production_offer"]["kind"])
        self.assertEqual(
            row["page_revision"],
            confirmed["production_offer"]["page_revision"],
        )
        self.assertEqual(
            confirmed["production_offer"]["offer_id"],
            repeated["production_offer"]["offer_id"],
        )

    def test_no_pending_plan_never_calls_model_or_opens_card(self):
        body = payload(prompt="确认生成")
        body.update(_username="alice", _job_id=44)
        with mock.patch.object(core, "jdb", self.db), mock.patch.object(
            director_agent, "_responses_chat",
        ) as model_call:
            result = director_agent.gen_director_agent(body)
        model_call.assert_not_called()
        self.assertNotIn("production_offer", result)
        self.assertIn("没有待确认方案", result["content"])

    def test_expired_plan_is_deleted_and_cannot_open_card(self):
        request = self.request(prompt="确认生成")
        self.save(request, now=2_000_000_000)
        result = director_agent._pending_plan_confirmation(
            self.db, "alice", request,
            now=2_000_000_000 + director_agent.OFFER_TTL_SECONDS + 1,
        )
        self.assertEqual("expired", result["state"])
        with closing(self.db()) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(1) FROM director_agent_pending_plans"
            ).fetchone()[0])

    def test_page_revision_or_parameters_change_invalidates_plan(self):
        request = self.request(prompt="确认生成")
        saved = self.save(request)
        changed_revision = dict(request, page_revision="deadbeef")
        self.assertEqual("changed", director_agent._pending_plan_confirmation(
            self.db, "alice", changed_revision, now=2_000_000_001,
        )["state"])

        saved = self.save(request)
        changed_parameters = dict(request)
        changed_parameters["page_revision"] = saved["page_revision"]
        changed_parameters["page_context"] = dict(
            request["page_context"], duration="60s",
        )
        self.assertEqual("changed", director_agent._pending_plan_confirmation(
            self.db, "alice", changed_parameters, now=2_000_000_001,
        )["state"])

    def test_price_change_requires_a_new_exact_confirmation_turn(self):
        request = self.request(prompt="确认生成")
        saved = self.save(request, cost=3)
        request["page_revision"] = saved["page_revision"]
        with mock.patch.object(
            director_agent.director_cli, "production_is_available",
            return_value=True,
        ), mock.patch.object(director_agent, "_copy_cost", return_value=4):
            changed = director_agent._pending_plan_confirmation(
                self.db, "alice", request, now=2_000_000_001,
            )
            ready = director_agent._pending_plan_confirmation(
                self.db, "alice", request, now=2_000_000_002,
            )
        self.assertEqual("price_changed", changed["state"])
        self.assertEqual(4, changed["expected_cost"])
        self.assertEqual("ready", ready["state"])
        self.assertEqual(4, ready["offer"]["expected_cost"])

    def test_model_can_prepare_but_can_never_open_a_card(self):
        request = self.request()
        with mock.patch.object(
            director_agent.director_cli, "production_is_available",
            return_value=True,
        ), mock.patch(
            "content_domains.points.cost_of", return_value=3,
        ):
            result = director_agent.normalize_model_result(
                model_result(offer=True), request,
            )
        self.assertNotIn("production_offer", result)
        self.assertIn("_pending_production_plan", result)

    def test_server_page_revision_matches_frontend_digest(self):
        self.assertEqual(
            "bc3325c1",
            director_agent._client_page_revision(self.request()["page_context"]),
        )

    def test_cli_reprice_round_trip_requires_text_reconfirmation(self):
        request = self.request(prompt="确认生成")
        saved = self.save(request, cost=3)
        request["page_revision"] = saved["page_revision"]
        with mock.patch.object(
            director_agent.director_cli, "production_is_available",
            return_value=True,
        ), mock.patch.object(director_agent, "_copy_cost", return_value=3):
            ready = director_agent._pending_plan_confirmation(
                self.db, "alice", request, now=2_000_000_001,
            )
        issued = director_agent._issue_production_offer(
            self.db, "alice", ready["offer"], request["page_revision"],
            now=2_000_000_001,
        )
        submitted_value = {
            "offer_id": issued["offer_id"], "input": issued["input"],
            "expected_cost": issued["expected_cost"],
            "plan_digest": issued["plan_digest"],
            "quote_token": issued["quote_token"],
        }
        with mock.patch.object(
            director_agent.director_cli, "quote_script", return_value={
                "quote_token": "q" * 24, "cost": 4,
                "points": 99, "expires_in": 60,
            },
        ), mock.patch.object(
            director_agent.director_cli, "confirm_script",
        ) as confirm_call:
            status, changed = director_agent.produce_script(
                self.db, "alice", submitted_value, now=2_000_000_002,
            )
        self.assertEqual(409, status)
        self.assertTrue(changed["pending_plan_updated"])
        self.assertTrue(changed["requires_new_text_confirmation"])
        self.assertNotIn("quote_token", changed)
        confirm_call.assert_not_called()

        with closing(self.db()) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY,kind TEXT,username TEXT,
                cost INTEGER,status TEXT
            )""")
            connection.execute(
                "INSERT INTO jobs VALUES(77,'copy','alice',4,'pending')"
            )
            connection.commit()
        with mock.patch.object(
            director_agent.director_cli, "production_is_available",
            return_value=True,
        ), mock.patch.object(director_agent, "_copy_cost", return_value=4):
            refreshed = director_agent._pending_plan_confirmation(
                self.db, "alice", request, now=2_000_000_003,
            )
        self.assertEqual("ready", refreshed["state"])
        self.assertEqual(4, refreshed["offer"]["expected_cost"])
        reissued = director_agent._issue_production_offer(
            self.db, "alice", refreshed["offer"], request["page_revision"],
            now=2_000_000_003,
        )
        refreshed_value = {
            "offer_id": reissued["offer_id"], "input": reissued["input"],
            "expected_cost": reissued["expected_cost"],
            "plan_digest": reissued["plan_digest"],
            "quote_token": reissued["quote_token"],
        }
        with mock.patch.object(
            director_agent.director_cli, "quote_script",
        ) as quote_call, mock.patch.object(
            director_agent.director_cli, "confirm_script",
            return_value={"job_id": 77, "points_left": 95},
        ) as confirm_call:
            status, submitted = director_agent.produce_script(
                self.db, "alice", refreshed_value, now=2_000_000_004,
            )
        self.assertEqual(200, status)
        self.assertEqual(77, submitted["job_id"])
        quote_call.assert_not_called()
        confirm_call.assert_called_once()

    def test_untrusted_modifier_is_not_accepted_as_topic(self):
        for prompt in (
            "做一个30秒的分镜脚本",
            "做一个抖音用的分镜脚本",
            "围绕之前那个话题生成分镜脚本",
        ):
            with self.subTest(prompt=prompt):
                value = payload(prompt=prompt)
                value["page_context"] = dict(value["page_context"], topic="")
                request = director_agent.validate_payload(value)
                request.update(_username="alice", _job_id=42)
                with mock.patch.object(
                    director_agent.director_cli, "production_is_available",
                    return_value=True,
                ), mock.patch(
                    "content_domains.points.cost_of", return_value=3,
                ):
                    result = director_agent.normalize_model_result(
                        model_result(offer=True), request,
                    )
                self.assertNotIn("_pending_production_plan", result)
                self.assertNotIn("production_offer", result)

    def test_pending_plan_is_scoped_to_account_and_session(self):
        request = self.request(prompt="确认生成")
        saved = self.save(request)
        request["page_revision"] = saved["page_revision"]
        with mock.patch.object(
            director_agent.director_cli, "production_is_available",
            return_value=True,
        ), mock.patch.object(director_agent, "_copy_cost", return_value=3):
            self.assertEqual("missing", director_agent._pending_plan_confirmation(
                self.db, "bob", request, now=2_000_000_001,
            )["state"])
            other_session = dict(request, session_id="director_session_other_456")
            self.assertEqual("missing", director_agent._pending_plan_confirmation(
                self.db, "alice", other_session, now=2_000_000_001,
            )["state"])


if __name__ == "__main__":
    unittest.main()
