import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools/hq-cli/src"))

import hq_cli_api  # noqa: E402
from hq_cli.catalog import CAPABILITIES  # noqa: E402


NEW_ACTIONS = {
    "director-chat", "director-produce", "director-scene-video-generate",
    "director-scene-talking-generate", "dl", "short-drama-advisor",
    "director-workflows", "director-workflow-create", "director-workflow",
    "director-storyboard-update", "director-storyboard-export",
    "director-production-plan", "director-production-start",
    "director-production-status", "director-production-recover",
    "director-remake-plan", "director-remake-start",
    "director-remake-status", "director-remake-recover",
    "short-drama-character-reference-generate",
    "short-drama-character-reference-confirm",
    "short-drama-preflight-plan", "short-drama-preflight-confirm",
    "short-drama-autodraft-preflight", "short-drama-autodraft-quote",
    "short-drama-autodraft-start", "short-drama-autodraft-status",
    "short-drama-delivery-quote", "short-drama-delivery-start",
    "short-drama-delivery-status", "short-drama-completion-readiness",
    "short-drama-completion", "short-drama-completion-confirm",
}


class HQCLIBActionsTests(unittest.TestCase):
    def test_server_and_client_register_the_same_real_actions(self):
        server = {item["action"] for item in hq_cli_api.ACTION_CATALOG}
        client = {
            identifier for identifier, capability in CAPABILITIES.items()
            if capability["kind"] != "navigation"
        }
        self.assertEqual(server, client)
        self.assertEqual(136, len(server))
        self.assertEqual("download", CAPABILITIES["dl"]["kind"])
        self.assertTrue(NEW_ACTIONS <= server)
        self.assertTrue(all(CAPABILITIES[item]["runnable"] for item in NEW_ACTIONS))

    def test_director_chat_and_produce_keep_confirmation_and_idempotency(self):
        chat = hq_cli_api.action_plan("director-chat", {
            "prompt": "帮我规划一条新品口播",
            "session_id": "director-session-0001",
            "page_revision": 1,
            "page_context": {"page": "script", "title": "编导"},
            "history": [],
            "request_id": "director-chat-0001",
        })
        self.assertEqual(("director:write", "/api/gen/director_agent"),
                         (chat["scope"], chat["path"]))
        self.assertEqual("director-chat-0001", chat["headers"]["Idempotency-Key"])
        self.assertNotIn("request_id", chat["body"])
        self.assertEqual(0, chat["body"]["quoted_cost"])

        offer_id = "director-production-1234567890abcdef"
        production = hq_cli_api.action_plan("director-produce", {
            "offer_id": offer_id,
            "input": {"request_id": offer_id, "topic": "新品"},
            "expected_cost": 3,
            "plan_digest": "a" * 64,
            "quote_token": "q" * 24,
        })
        self.assertEqual("director:generate", production["scope"])
        self.assertEqual(("generation:submit",), production["extra_scopes"])
        self.assertEqual(offer_id, production["headers"]["Idempotency-Key"])
        self.assertTrue(CAPABILITIES["director-chat"]["confirmation_required"])
        self.assertTrue(CAPABILITIES["director-produce"]["confirmation_required"])

    def test_character_reference_uses_server_quote_then_exact_confirmation(self):
        value = {"project_id": "project-1", "revision": 7, "character_key": "lead"}
        plan = hq_cli_api.action_plan(
            "short-drama-character-reference-generate", value,
        )
        self.assertEqual("generation", plan["kind"])
        self.assertEqual("short_drama_character_reference", plan["generation_kind"])
        self.assertEqual(
            "/api/gen/short-drama/character-reference-quote",
            plan["quote_endpoint"],
        )
        self.assertEqual(
            "/api/gen/short-drama/generate-character-reference",
            plan["endpoint"],
        )
        self.assertEqual(value, plan["payload"])
        capability = CAPABILITIES["short-drama-character-reference-generate"]
        self.assertEqual("paid", capability["side_effect"])
        self.assertEqual("server_quote", capability["cost"]["kind"])

    def test_director_scene_video_and_talking_reuse_existing_paid_pipelines(self):
        video = hq_cli_api.action_plan("director-scene-video-generate", {
            "scenes": [{"scene": "雨夜街头，女主转身离开"}],
            "channel": "grok", "ratio": "9:16", "duration": 6,
        })
        self.assertEqual(("director:generate", "xiaole_video", "/api/gen/xiaole_video"), (
            video["scope"], video["generation_kind"], video["endpoint"],
        ))
        self.assertEqual("雨夜街头，女主转身离开", video["payload"]["prompt"])

        talking = hq_cli_api.action_plan("director-scene-talking-generate", {
            "text": "今天讲三个核心卖点。",
            "template": "1080x1920/image_default.html",
            "style": "realistic_commercial",
            "voice": "public:voice-1",
        })
        self.assertEqual(("director:generate", "script_to_video"), (
            talking["scope"], talking["generation_kind"],
        ))
        self.assertEqual("/api/gen/text-video/quote", talking["quote_endpoint"])

    def test_director_workflow_crud_routes_are_owner_scoped_and_revision_guarded(self):
        create = hq_cli_api.action_plan("director-workflow-create", {
            "title": "新品分镜", "storyboard": [{"scene": "产品特写", "line": "新品来了"}],
            "request_id": "workflow-create-0001",
        })
        self.assertEqual(("director:write", "/api/gen/director/workflows"),
                         (create["scope"], create["path"]))
        self.assertEqual("workflow-create-0001", create["headers"]["Idempotency-Key"])
        workflow_id = "dw_" + "a" * 32
        update = hq_cli_api.action_plan("director-storyboard-update", {
            "workflow_id": workflow_id, "revision": 2,
            "storyboard": [{"scene": "更新画面", "line": "更新台词"}],
        })
        self.assertEqual("PUT", update["method"])
        self.assertEqual(
            "/api/gen/director/workflows/%s/storyboard" % workflow_id,
            update["path"],
        )
        exported = hq_cli_api.action_plan(
            "director-storyboard-export", {"workflow_id": workflow_id},
        )
        self.assertTrue(exported["path"].endswith("/storyboard/export"))
        production = hq_cli_api.action_plan("director-production-start", {
            "workflow_id": workflow_id, "plan_digest": "b" * 64,
            "request_id": "production-start-0001",
        })
        self.assertEqual("generation", production["kind"])
        self.assertEqual("director_production", production["generation_kind"])
        self.assertTrue(production["quote_endpoint"].endswith("/production/quote"))
        recover = hq_cli_api.action_plan("director-remake-recover", {
            "workflow_id": workflow_id, "plan_digest": "c" * 64,
            "request_id": "remake-recover-0001",
        })
        self.assertEqual(("director:recover", ("generation:submit",)),
                         (recover["scope"], recover["extra_scopes"]))

    def test_short_drama_native_quote_start_status_and_completion_routes(self):
        provider = {
            "project_id": "project-1", "plan_id": "plan-1",
            "shot_key": "shot-1",
        }
        quote = hq_cli_api.action_plan("short-drama-autodraft-quote", provider)
        self.assertEqual("/api/gen/short-drama/autodraft/provider-quote", quote["path"])
        start = hq_cli_api.action_plan("short-drama-autodraft-start", {
            "project_id": "project-1", "quote_token": "quote-1",
            "request_id": "autodraft-start-0001",
        })
        self.assertEqual(("generation:submit",), start["extra_scopes"])
        self.assertEqual("autodraft-start-0001", start["headers"]["Idempotency-Key"])
        status = hq_cli_api.action_plan("short-drama-autodraft-status", {
            "project_id": "project-1", "job_id": "job-1",
        })
        self.assertEqual(
            "/api/gen/short-drama/autodraft/provider-jobs/job-1?project_id=project-1",
            status["path"],
        )

        delivery = hq_cli_api.action_plan("short-drama-delivery-start", {
            "project_id": "project-1", "quote_token": "delivery-quote",
            "request_id": "delivery-start-0001",
        })
        self.assertEqual(("generation:submit",), delivery["extra_scopes"])
        readiness = hq_cli_api.action_plan(
            "short-drama-completion-readiness", {"project_id": "project-1"},
        )
        self.assertEqual(
            "/api/gen/short-drama/completion/readiness?project_id=project-1",
            readiness["path"],
        )
        completed = hq_cli_api.action_plan("short-drama-completion-confirm", {
            "project_id": "project-1", "revision": 8,
            "final_version_id": "final-v1", "asset_id": "asset-1",
            "delivery_hash": "b" * 64, "acknowledged": True,
            "request_id": "completion-confirm-0001",
        })
        self.assertEqual("completion-confirm-0001", completed["headers"]["Idempotency-Key"])

    def test_invalid_or_unconfirmed_inputs_fail_closed(self):
        with self.assertRaises(hq_cli_api.CLIAPIError):
            hq_cli_api.action_plan("director-chat", {
                "prompt": "test", "session_id": "s", "page_revision": 1,
                "page_context": {}, "request_id": "bad key",
            })
        with self.assertRaises(hq_cli_api.CLIAPIError):
            hq_cli_api.action_plan("short-drama-completion-confirm", {
                "project_id": "p", "revision": 1, "final_version_id": "v",
                "asset_id": "a", "delivery_hash": "bad",
                "acknowledged": True, "request_id": "completion-0001",
            })
        for action in (
            "director-chat", "director-produce", "short-drama-advisor",
            "short-drama-autodraft-start", "short-drama-delivery-start",
            "short-drama-completion-confirm",
        ):
            self.assertIn(action, hq_cli_api.CONFIRMATION_ACTIONS)


if __name__ == "__main__":
    unittest.main()
