import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama, short_drama_conversation, short_drama_preflight


def project_payload(**changes):
    value = {
        "title": "雨夜来信",
        "synopsis": "记者在暴雨夜追查一段来自未来的录音，并重新理解自己的家人。",
        "ratio": "16:9",
        "target_duration": 30,
        "shot_count": 6,
        "visual_style": "电影感写实",
        "target_platform": "抖音",
        "point_budget": 0,
    }
    value.update(changes)
    return value


class Handler:
    def __init__(self, path, body=None, key="preflight-test-key", token="alice"):
        self.path = path
        self.body = body
        self.token = token
        self.headers = {"Idempotency-Key": key}
        self.response = None

    def _token(self):
        return self.token

    def _json_body_strict(self):
        return self.body

    def _send(self, status, payload):
        self.response = (status, payload)


class ShortDramaPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.database)
        short_drama.init_db(self.db)
        self.project = short_drama.create_project(
            self.db, "alice", project_payload()
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _lock_script(self, project=None):
        project = project or self.project
        selected = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": project["id"],
                "conversation_revision": 1,
                "message": "方案一 · 情感治愈",
            },
            "preflight-select-%s" % project["id"],
        )
        confirmed = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": project["id"],
                "conversation_revision": selected["conversation"]["revision"],
                "message": "确认这个方向",
            },
            "preflight-confirm-%s" % project["id"],
        )
        generated = short_drama_conversation.generate_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
                "instruction": "温暖反转",
            },
            "generate-script-key",
        )
        return short_drama_conversation.lock_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": project["id"],
                "conversation_revision": generated["conversation"]["revision"],
                "version_id": generated["current_script"]["id"],
            },
            "lock-script-key",
        )

    def test_locked_script_becomes_confirmed_production_plan_without_charge(self):
        locked = self._lock_script()
        prepared = short_drama_preflight.generate_plan(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": locked["conversation"]["revision"],
                "quality_route": "quick_draft",
            },
            "prepare-plan-key",
        )
        current = prepared["current_plan"]
        plan = current["plan"]
        self.assertEqual("ready_for_confirmation", prepared["state"])
        self.assertTrue(plan["ready"])
        self.assertEqual(30000, plan["duration"]["target_ms"])
        self.assertEqual(
            30000,
            sum(item["duration_ms"] for item in plan["duration"]["shots"]),
        )
        self.assertEqual("estimate_only", plan["estimate"]["billing"])
        self.assertFalse(prepared["billing"]["charged"])
        self.assertEqual("standalone-preflight-v2", plan["contract_version"])
        self.assertEqual(6, len(plan["material_plan"]))
        first_shot = plan["material_plan"][0]
        self.assertTrue(first_shot["input_hash"])
        self.assertIn("visual_prompt", first_shot)
        self.assertTrue(first_shot["provider_prompt"])
        self.assertIn("negative_prompt", first_shot)
        self.assertIn("dialogue", first_shot)

        conversation = short_drama_conversation.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        source_shot = conversation["current_script"]["script"]["shots"][0]
        self.assertEqual(
            source_shot["provider_prompt"],
            first_shot["provider_prompt"],
        )
        self.assertEqual(
            source_shot["negative_prompt"],
            first_shot["negative_prompt"],
        )

        confirmed = short_drama_preflight.confirm_plan(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "plan_id": current["id"],
                "plan_version": current["version"],
                "accepted_issue_keys": plan["required_acceptance"],
            },
            "confirm-plan-key",
        )
        self.assertEqual("confirmed", confirmed["state"])
        self.assertEqual("confirmed", confirmed["current_plan"]["status"])
        project = short_drama.get_project(self.db, "alice", self.project["id"])
        self.assertEqual(0, project["spent_points"])
        self.assertEqual("draft", project["stage"])

    def test_confirmation_requires_explicit_warning_acceptance(self):
        locked = self._lock_script()
        prepared = short_drama_preflight.generate_plan(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": locked["conversation"]["revision"],
                "quality_route": "quick_draft",
            },
            "prepare-warning-key",
        )
        current = prepared["current_plan"]
        self.assertTrue(current["plan"]["required_acceptance"])
        with self.assertRaises(short_drama_preflight.PreflightError) as raised:
            short_drama_preflight.confirm_plan(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "plan_id": current["id"],
                    "plan_version": current["version"],
                    "accepted_issue_keys": [],
                },
                "confirm-without-acceptance",
            )
        self.assertEqual("adjustments_not_accepted", raised.exception.code)

    def test_budget_blocker_prevents_confirmation(self):
        limited = short_drama.create_project(
            self.db, "alice", project_payload(title="低预算项目", point_budget=10)
        )
        locked = self._lock_script(limited)
        prepared = short_drama_preflight.generate_plan(
            self.db,
            "alice",
            "alice",
            {
                "project_id": limited["id"],
                "conversation_revision": locked["conversation"]["revision"],
                "quality_route": "quick_draft",
            },
            "prepare-budget-key",
        )
        current = prepared["current_plan"]
        self.assertFalse(current["plan"]["ready"])
        self.assertIn("budget", current["plan"]["blockers"])
        with self.assertRaises(short_drama_preflight.PreflightError) as raised:
            short_drama_preflight.confirm_plan(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": limited["id"],
                    "plan_id": current["id"],
                    "plan_version": current["version"],
                    "accepted_issue_keys": current["plan"]["required_acceptance"],
                },
                "confirm-blocked-key",
            )
        self.assertEqual("preflight_blocked", raised.exception.code)

    def test_plan_generation_is_idempotent_and_reuses_same_inputs(self):
        locked = self._lock_script()
        body = {
            "project_id": self.project["id"],
            "conversation_revision": locked["conversation"]["revision"],
            "quality_route": "quick_draft",
        }
        first = short_drama_preflight.generate_plan(
            self.db, "alice", "alice", body, "same-preflight-key"
        )
        replay = short_drama_preflight.generate_plan(
            self.db, "alice", "alice", body, "same-preflight-key"
        )
        reused = short_drama_preflight.generate_plan(
            self.db, "alice", "alice", body, "new-preflight-key"
        )
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["current_plan"]["id"], replay["current_plan"]["id"])
        self.assertEqual(first["current_plan"]["id"], reused["current_plan"]["id"])
        self.assertEqual(1, len(reused["versions"]))

    def test_switching_routes_reactivates_the_selected_immutable_plan(self):
        locked = self._lock_script()
        common = {
            "project_id": self.project["id"],
            "conversation_revision": locked["conversation"]["revision"],
        }
        quick = short_drama_preflight.generate_plan(
            self.db,
            "alice",
            "alice",
            dict(common, quality_route="quick_draft"),
            "prepare-quick-route",
        )
        formal = short_drama_preflight.generate_plan(
            self.db,
            "alice",
            "alice",
            dict(common, quality_route="formal"),
            "prepare-formal-route",
        )
        restored = short_drama_preflight.generate_plan(
            self.db,
            "alice",
            "alice",
            dict(common, quality_route="quick_draft"),
            "prepare-quick-again",
        )
        self.assertEqual(quick["current_plan"]["id"], restored["current_plan"]["id"])
        self.assertEqual("draft", restored["current_plan"]["status"])
        statuses = {item["id"]: item["status"] for item in restored["versions"]}
        self.assertEqual("superseded", statuses[formal["current_plan"]["id"]])

    def test_http_routes_apply_auth_and_error_contract(self):
        verify = lambda token: (
            {"username": token, "must_change": False} if token else None
        )
        anonymous = Handler(
            "/api/gen/short-drama/preflight?project_id=" + self.project["id"],
            token="",
        )
        self.assertTrue(short_drama.dispatch_http(anonymous, "GET", self.db, verify))
        self.assertEqual(401, anonymous.response[0])

        workspace = Handler(
            "/api/gen/short-drama/preflight?project_id=" + self.project["id"]
        )
        self.assertTrue(short_drama.dispatch_http(workspace, "GET", self.db, verify))
        self.assertEqual(200, workspace.response[0])
        self.assertEqual("script_required", workspace.response[1]["state"])

        short_drama_conversation.workspace(
            self.db, "alice", "alice", self.project["id"], True
        )
        rejected = Handler(
            "/api/gen/short-drama/preflight/generate",
            body={
                "project_id": self.project["id"],
                "conversation_revision": 1,
                "quality_route": "quick_draft",
            },
        )
        self.assertTrue(short_drama.dispatch_http(rejected, "POST", self.db, verify))
        self.assertEqual(409, rejected.response[0])
        self.assertEqual("locked_script_required", rejected.response[1]["code"])


if __name__ == "__main__":
    unittest.main()
