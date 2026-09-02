import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "tools" / "hq-cli" / "src")]

import hq_cli_api  # noqa: E402
from hq_cli.catalog import CAPABILITIES  # noqa: E402


ALLOWED = {"direct_cli", "existing_composite", "navigation_handoff", "local_only", "display_only", "admin_only", "security_handoff", "payment_handoff", "remaining_backend_supported"}
MATRIX = {
    "creator_agent": ("direct_cli", set(hq_cli_api.WEB_PARITY_ACTIONS) & {"creator-agent-capability", "creator-agent-message", "creator-agent-batch-confirm"}),
    "creator_agent_pdf": ("direct_cli", {"creator-agent-background-pdf"}),
    "invite": ("direct_cli", {"invite-config", "invite-dashboard", "invite-users", "invite-rewards", "invite-code", "invite-referrer"}),
    "notifications": ("direct_cli", {"notifications", "notification-read", "notifications-read-all"}),
    "failed_task_delete": ("direct_cli", {"task-delete"}),
    "profile_friends_points": ("direct_cli", {"profile-update", "friends", "friend-requests", "friend-request", "friend-request-respond", "friend-delete", "points-transfer-recipient", "points-transfers"}),
    "recharge": ("direct_cli", {"recharge-packages", "recharge-orders"}),
    "assets": ("direct_cli", {"asset-marks", "asset-batch-delete", "avatar-rename", "avatar-delete", "voice-rename"}),
    "canvas_members": ("direct_cli", {"canvas-members", "canvas-member-add", "canvas-member-remove"}),
    "digital_ip": ("direct_cli", {"digital-ip-diagnose", "digital-ip-guide", "digital-ip-report-generate", "digital-ip-report-confirm"}),
    "digital_human_internal_steps": ("direct_cli", {"digital-human-oneclick-heygen-preflight", "digital-human-oneclick-material-resolve"}),
    "short_drama": ("direct_cli", {name for name in hq_cli_api.WEB_PARITY_ACTIONS if name.startswith("short-drama-")}),
    "points_transfer": ("security_handoff", {"/api/auth/points/transfer requires password"}),
    "password_change": ("security_handoff", {"/api/auth/change_password"}),
    "payment": ("payment_handoff", {"/api/auth/recharge/order", "/api/auth/wxpay/native", "/api/gen/audio/buy-slot"}),
    "asset_batch_download": ("direct_cli", {"asset-batch-download"}),
    "video_import": ("direct_cli", {"video-import"}),
    "avatar_upload": ("direct_cli", {"profile-avatar-upload"}),
    "browser_export": ("local_only", {"JPG export and clipboard/QR rendering"}),
    "dashboards": ("display_only", {"cost cards, bots and analytics beacons"}),
    "admin_controls": ("admin_only", {"admin-only controls"}),
}


class HQCLIWebParityActionsTests(unittest.TestCase):
    def test_matrix_has_only_explicit_outcomes_and_all_direct_actions_are_mirrored(self):
        server = {item["action"] for item in hq_cli_api.ACTION_CATALOG}
        for _, (outcome, entries) in MATRIX.items():
            self.assertIn(outcome, ALLOWED)
            if outcome == "direct_cli":
                self.assertTrue(entries)
                self.assertTrue(entries <= server)
                self.assertTrue(entries <= set(CAPABILITIES))
                self.assertTrue(all(CAPABILITIES[name]["kind"] != "navigation" for name in entries))

    def test_every_fixed_web_action_has_a_fixed_route_strict_keys_and_matching_client_capability(self):
        server = {item["action"]: item for item in hq_cli_api.ACTION_CATALOG}
        for action, spec in hq_cli_api.WEB_PARITY_ACTIONS.items():
            _, method, route, fields, required, confirmed, idempotent = spec
            self.assertTrue(route.startswith("/"))
            self.assertTrue(set(required) <= set(fields))
            self.assertIn(action, server)
            self.assertIn(action, CAPABILITIES)
            self.assertEqual(bool(confirmed), action in hq_cli_api.CONFIRMATION_ACTIONS)
            self.assertEqual(hq_cli_api._WEB_ACTION_INPUTS[action]["required"], list(required))
            self.assertEqual(server[action]["input_schema"], CAPABILITIES[action]["input_schema"])
            self.assertEqual(server[action]["constraints"], CAPABILITIES[action]["constraints"])
            self.assertNotIn("input", server[action]["input_schema"]["properties"])
            self.assertFalse("password" in fields or "cookie" in fields or "api_key" in fields or "otp" in fields)
            if idempotent:
                self.assertIn("request_id", fields)
            with self.assertRaises(hq_cli_api.CLIAPIError):
                hq_cli_api.action_plan(action, {"unexpected": True})
            self.assertIn(method, {"GET", "POST", "PUT", "DELETE"})

    def test_security_handoffs_remain_non_executable(self):
        actions = set(hq_cli_api.WEB_PARITY_ACTIONS)
        self.assertFalse(any("password" in action or "payment-final" in action for action in actions))
        self.assertNotIn("recharge-payment-init", actions)
        self.assertNotIn("points-transfer", actions)
        self.assertNotIn("voice-slot-buy", actions)

    def test_creator_message_uses_the_real_top_level_web_body(self):
        plan = hq_cli_api.action_plan("creator-agent-message", {
            "message": "为秋季系列做三条视频方向",
            "request_id": "creator-message-0001",
            "project_id": "abc123def456",
        })
        self.assertEqual("/messages", plan["path"])
        self.assertEqual("creator-message-0001", plan["body"]["request_id"])
        self.assertEqual("creator-message-0001", plan["headers"]["Idempotency-Key"])

    def test_short_drama_routes_keep_exact_versions_and_idempotency(self):
        self.assertEqual({}, hq_cli_api.WEB_PARITY_REMAINING)
        cases = (
            ("short-drama-project-update", {
                "project_id": "drama-1", "revision": 3, "title": "新版标题",
            }, "/api/gen/short-drama/project?id=drama-1", {"revision": 3, "title": "新版标题"}),
            ("short-drama-shot-update", {
                "project_id": "drama-1", "revision": 4, "version_id": "version-1",
                "shot_key": "shot-1", "changes": {"visual": "雨夜街头"},
                "request_id": "shot-update-0001",
            }, "/api/gen/short-drama/conversation/script/shot/update", {
                "project_id": "drama-1", "revision": 4, "version_id": "version-1",
                "shot_key": "shot-1", "changes": {"visual": "雨夜街头"},
            }),
            ("short-drama-scene-create", {
                "project_id": "drama-1", "graph_revision": 2, "name": "客厅",
                "description": "夜晚的旧式客厅", "shot_keys": ["shot-1"],
            }, "/api/gen/short-drama/asset-graph/scenes", {
                "project_id": "drama-1", "graph_revision": 2, "name": "客厅",
                "description": "夜晚的旧式客厅", "shot_keys": ["shot-1"],
            }),
            ("short-drama-refinement-confirm", {
                "project_id": "drama-1", "version_id": "refine-1",
                "checklist": {"visual": True}, "source_hashes": {"video": "abc"},
            }, "/api/gen/short-drama/refinement/confirm", {
                "project_id": "drama-1", "version_id": "refine-1",
                "checklist": {"visual": True}, "source_hashes": {"video": "abc"},
            }),
        )
        for action, value, path, body in cases:
            with self.subTest(action=action):
                plan = hq_cli_api.action_plan(action, value)
                self.assertEqual(path, plan["path"])
                self.assertEqual(body, plan["body"])
        self.assertEqual(
            "shot-update-0001",
            hq_cli_api.action_plan("short-drama-shot-update", cases[1][1])["headers"]["Idempotency-Key"],
        )

    def test_short_drama_enums_are_capability_contracts(self):
        structure = CAPABILITIES["short-drama-shot-structure"]["input_schema"]
        self.assertEqual(
            ["delete", "copy", "insert_before", "insert_after", "smart_insert", "move_up", "move_down"],
            structure["properties"]["action"]["enum"],
        )
        media = CAPABILITIES["short-drama-refinement-media"]["input_schema"]
        self.assertEqual(
            ["voice_timeline", "provider_audio", "silent"],
            media["properties"]["mode"]["enum"],
        )


if __name__ == "__main__":
    unittest.main()
