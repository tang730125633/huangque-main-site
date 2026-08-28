import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import director_workflow_contract as contract  # noqa: E402
import hq_cli_api  # noqa: E402
from content_domains import function_registry  # noqa: E402
from content_domains import points as points_domain  # noqa: E402


class DirectorCLIContractTests(unittest.TestCase):
    def test_workflow_states_and_transitions_are_closed_and_terminal(self):
        states = set(contract.WORKFLOW_STATES)
        self.assertEqual(states, set(contract.STATE_TRANSITIONS))
        self.assertEqual(set(contract.TERMINAL_STATES), {
            "completed", "failed", "refunded", "abandoned",
        })
        for state, targets in contract.STATE_TRANSITIONS.items():
            with self.subTest(state=state):
                self.assertEqual(len(targets), len(set(targets)))
                self.assertTrue(set(targets) <= states)
                if state in contract.TERMINAL_STATES:
                    self.assertEqual((), targets)
        self.assertEqual(("refunded",), contract.STATE_TRANSITIONS["refund_pending"])

    def test_every_director_site_operation_is_owned_by_one_contract_action(self):
        registered = {
            mode["key"]
            for feature in function_registry.SCRIPT_FUNCTIONS
            for mode in feature.get("modes") or []
        }
        registered.update(
            item["key"] for item in function_registry.BROWSER_JOURNEYS["script"]
        )
        owners = {}
        for action in contract.DIRECTOR_ACTIONS:
            for operation in action["website_operations"]:
                owners.setdefault(operation, []).append(action["id"])
        self.assertEqual(registered, set(owners))
        self.assertEqual([], [
            (operation, action_ids)
            for operation, action_ids in owners.items()
            if len(action_ids) != 1
        ])

    def test_current_digital_human_page_and_loaded_scripts_are_registered(self):
        html = (ROOT / "site" / "workbench" / "digital-human-oneclick.html").read_text(
            encoding="utf-8"
        )
        script_sources = re.findall(r'<script[^>]+src="([^"?]+)', html)
        self.assertIn("digital-human-unified.js", script_sources)
        loaded_sources = [html]
        for source in script_sources:
            script_path = ROOT / "site" / "workbench" / source
            if script_path.is_file():
                loaded_sources.append(script_path.read_text(encoding="utf-8"))
        current = {
            value.split("?", 1)[0]
            for source in loaded_sources
            for value in re.findall(r"/api/gen/[A-Za-z0-9_/?=&.-]+", source)
            if value.startswith((
                "/api/gen/digital-human-v2/", "/api/gen/video/",
                "/api/gen/audio", "/api/gen/job/",
                "/api/gen/video-compose/", "/api/gen/file/",
            ))
        }
        covered_roots = {
            endpoint.split(":", 1)[1].split("?", 1)[0].split("{", 1)[0].rstrip("/")
            for action in contract.DIGITAL_HUMAN_ONECLICK_ACTIONS
            for endpoint in action["server_endpoints"]
        }
        self.assertTrue(current)
        uncovered = {
            endpoint for endpoint in current
            if not any(
                endpoint.rstrip("/") == root or endpoint.startswith(root + "/")
                for root in covered_roots
            )
        }
        self.assertEqual(set(), uncovered)

    def test_real_paid_director_actions_match_points_domain_costs(self):
        expected = {
            "director-script-generate": ("copy", 3),
            "director-breakdown": ("breakdown", 20),
            "director-breakdown-upload": ("breakdown", 20),
        }
        actions = {action["id"]: action for action in contract.DIRECTOR_ACTIONS}
        for identifier, (kind, cost) in expected.items():
            with self.subTest(action=identifier):
                action = actions[identifier]
                self.assertEqual("director:generate", action["required_scope"])
                self.assertEqual("quote_then_confirm", action["billing"])
                self.assertTrue(action["confirmation_required"])
                self.assertTrue(action["idempotency_required"])
                self.assertEqual(kind, action["points_kind"])
                self.assertEqual(cost, points_domain.cost_of(kind, {}))

    def test_contract_does_not_advertise_planned_actions_as_executable(self):
        identifiers = [action["id"] for action in contract.ALL_ACTIONS]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        available = {
            action["id"] for action in contract.ALL_ACTIONS
            if action["availability"] == "available"
        }
        planned = set(identifiers) - available
        self.assertEqual({"director-capability"}, available)
        self.assertNotIn("director-production-start", hq_cli_api.ACTION_CATALOG_MAP)
        self.assertEqual(available, set(hq_cli_api.ACTION_CATALOG_MAP) & set(identifiers))
        self.assertTrue(planned)

    def test_scope_billing_and_recovery_boundaries_are_explicit(self):
        self.assertEqual(set(contract.SCOPE_CONTRACT), set(contract.SCOPE_CONTRACT) & set(hq_cli_api.SCOPES))
        for action in contract.ALL_ACTIONS:
            with self.subTest(action=action["id"]):
                self.assertIn(action["required_scope"], contract.SCOPE_CONTRACT)
                self.assertIn(action["availability"], {"available", "planned"})
                self.assertIn(action["transport"], {"action", "dedicated_upload"})
                if action["billing"] == "quote_then_confirm":
                    self.assertTrue(action["confirmation_required"])
                    self.assertTrue(action["idempotency_required"])
        invariants = " ".join(contract.WORKFLOW_INVARIANTS)
        for marker in ("quote_token", "plan_digest", "request_id", "禁止重复提交", "退款"):
            self.assertIn(marker, invariants)

if __name__ == "__main__":
    unittest.main()
