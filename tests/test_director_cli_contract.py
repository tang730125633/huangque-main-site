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


PRECISION_SCRIPT_EVIDENCE = {
    "GET:/api/gen/video/assets": ("request(fresh('/api/gen/video/assets?limit=40')",),
    "POST:/api/gen/video/lipsync-import": ("fetch('/api/gen/video/lipsync-import',{method:'POST'",),
    "GET:/api/gen/audio/slots": ("request(fresh('/api/gen/audio/slots')",),
    "POST:/api/gen/video/lipsync-voice-sample": ("request('/api/gen/video/lipsync-voice-sample',{method:'POST'",),
    "POST:/api/gen/audio/clone-vip": ("request('/api/gen/audio/clone-vip',{method:'POST'",),
    "GET:/api/gen/audio/clone-status": ("request(fresh('/api/gen/audio/clone-status?slot_id='",),
    "POST:/api/gen/audio": ("request('/api/gen/audio',{method:'POST'",),
    "GET:/api/gen/job/{job_id}": ("request(fresh('/api/gen/job/'+jobId)",),
    "GET:/api/gen/audio/assets": ("'/api/gen/audio/assets?limit=120'",),
    "POST:/api/gen/video": ("request('/api/gen/video',{method:'POST'",),
    "POST:/api/gen/video-compose/projects": ("request('/api/gen/video-compose/projects',{method:'POST'",),
    "POST:/api/gen/video-compose/projects/{project_id}/analyze-source": (
        "request('/api/gen/video-compose/projects/'+state.project.id+'/analyze-source',{method:'POST'",),
    "POST:/api/gen/video-compose/projects/{project_id}/edit-decisions": (
        "request('/api/gen/video-compose/projects/'+state.project.id+'/edit-decisions',{method:'POST'",),
    "POST:/api/gen/video-compose/projects/{project_id}/render": (
        "request('/api/gen/video-compose/projects/'+state.project.id+'/render',{method:'POST'",),
    "GET:/api/gen/video-compose/projects/{project_id}": (
        "request(fresh('/api/gen/video-compose/projects/'+state.project.id)",),
    "GET:/api/gen/video-compose/projects/{project_id}/output": (
        "'/api/gen/video-compose/projects/'+state.project.id+'/output'",),
    "GET:/api/gen/file/{path}": ("'/api/gen/file/'+item.video_file", "fetch(fresh(url)"),
}


def _precision_script_signatures(source):
    return {
        signature for signature, evidence in PRECISION_SCRIPT_EVIDENCE.items()
        if all(marker in source for marker in evidence)
    }


def _uncovered_precision_signatures(signatures, templates):
    uncovered = set()
    for signature in signatures:
        method, path = signature.split(":", 1)
        sample = path.replace("{job_id}", "job-1").replace(
            "{project_id}", "project-1"
        ).replace("{path}", "exports/result.mp4")
        if not any(
            contract.endpoint_template_matches(template, method, sample)
            for template in templates
        ):
            uncovered.add(signature)
    return uncovered


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
        loaded_sources = {}
        for source in script_sources:
            script_path = ROOT / "site" / "workbench" / source
            if script_path.is_file():
                loaded_sources[source] = script_path.read_text(encoding="utf-8")
        current = _precision_script_signatures(
            loaded_sources["digital-human-unified.js"]
        )
        expected = set(contract.PRECISION_INTERNAL_STEP_ENDPOINTS)
        self.assertEqual(set(PRECISION_SCRIPT_EVIDENCE), current)
        self.assertEqual(expected, current)
        self.assertEqual(set(), _uncovered_precision_signatures(current, expected))

    def test_precision_endpoint_gate_is_method_and_exact_template_sensitive(self):
        current = set(PRECISION_SCRIPT_EVIDENCE)
        templates = set(contract.PRECISION_INTERNAL_STEP_ENDPOINTS)
        for removed in sorted(templates):
            with self.subTest(removed=removed):
                self.assertIn(
                    removed,
                    _uncovered_precision_signatures(current, templates - {removed}),
                )
        for signature in sorted(templates):
            method, path = signature.split(":", 1)
            wrong_method = "POST" if method == "GET" else "GET"
            mutated = templates - {signature} | {wrong_method + ":" + path}
            with self.subTest(wrong_method=signature):
                self.assertIn(
                    signature, _uncovered_precision_signatures(current, mutated)
                )
        self.assertFalse(contract.endpoint_template_matches(
            "POST:/api/gen/video", "POST", "/api/gen/video/lipsync-import"
        ))
        self.assertTrue(contract.endpoint_template_matches(
            "GET:/api/gen/job/{job_id}", "GET", "/api/gen/job/job-7"
        ))

    def test_precision_public_contract_is_one_server_owned_recoverable_run(self):
        actions = {
            action["id"]: action
            for action in contract.DIGITAL_HUMAN_ONECLICK_ACTIONS
            if action["group"] == "precision"
        }
        self.assertEqual({
            "digital-human-precision-plan",
            "digital-human-precision-consent",
            "digital-human-precision-start",
            "digital-human-precision-status",
            "digital-human-precision-recover",
            "digital-human-precision-abandon",
        }, set(actions))
        public_endpoints = {
            endpoint for action in actions.values() for endpoint in action["server_endpoints"]
        }
        self.assertTrue(all(
            "/digital-human-v2/precision/" in item for item in public_endpoints
        ))
        self.assertTrue(
            public_endpoints.isdisjoint(contract.PRECISION_INTERNAL_STEP_ENDPOINTS)
        )
        start = actions["digital-human-precision-start"]
        self.assertEqual("quote_then_confirm", start["billing"])
        self.assertEqual("digital-human-oneclick:generate", start["required_scope"])

    def test_precision_failure_and_recovery_contract_is_idempotent(self):
        run = contract.PRECISION_RUN_CONTRACT
        self.assertEqual("server", run["authority"])
        self.assertEqual(
            ("run_id", "plan_digest", "quote_token", "request_id"),
            run["identity_fields"],
        )
        self.assertEqual({
            "full_audio_charged_response_unknown": "resume_same_run_without_recharge",
            "precision_lipsync_failed": "resume_same_run_from_precision_lipsync",
            "compose_failed_or_restarted": "resume_same_run_from_compose_ledger",
            "duplicate_request_id": "return_original_run_without_recharge",
        }, run["recovery_rules"])
        self.assertIn("full_audio", run["persistent_stages"])
        self.assertIn("precision_lipsync", run["persistent_stages"])
        self.assertIn("compose", run["persistent_stages"])

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
