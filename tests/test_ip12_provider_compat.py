import copy
import hashlib
import json
import sys
import time
import tempfile
import subprocess
import unittest
from unittest import mock
from pathlib import Path


HERMES = Path(__file__).resolve().parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import provider_compat
import provider_live_eval


MODEL = "provider-fixture-model"


def good_observations():
    return {
        "structured_output": {"status_code": 200, "response": {"output_text": '{"status":"ok"}'}},
        "tool_choice": {"status_code": 200, "response": {"output": [{
            "type": "function_call", "name": "inspect_project",
            "arguments": '{"project_ref":"project_fixture"}',
        }]}},
        "stream": {"status_code": 200, "events": [
            {"type": "response.output_text.delta"}, {"type": "response.completed"},
        ]},
        "continuation": {"status_code": 200, "response": {"output_text": "IP12-CONTINUITY-731"}},
        "reasoning": {"status_code": 200, "official_contract": True, "response": {}},
        "store_false": {"status_code": 200, "retrieval_status": 404, "response": {"id": "resp_fixture"}},
        "usage": {"status_code": 200, "response": {"usage": {"input_tokens": 3, "output_tokens": 2}}},
        "model_identity": {"status_code": 200, "response": {"model": MODEL}},
        "error_contract": {"status_code": 400, "response": {"error": {"code": "invalid_tool"}}},
        "timeout_cancel": {"terminal": "timeout"},
    }


class IP12ProviderCompatTests(unittest.TestCase):
    def test_probe_requests_are_identical_in_scope_and_never_contain_credentials(self):
        requests = provider_compat.build_requests(MODEL)
        self.assertEqual(set(requests), {
            "structured_output", "tool_choice", "stream", "continuation_first",
            "continuation_second", "reasoning", "store_false", "usage",
            "model_identity", "error_contract",
        })
        for request in requests.values():
            self.assertEqual(request["model"], MODEL)
            self.assertIs(request["store"], False)
            self.assertNotIn("api_key", str(request).lower())
            self.assertEqual(request["metadata"]["suite"], "ip12-provider-compat-v1")

    def test_live_runner_normalizes_endpoints_and_enforces_request_and_cny_budgets(self):
        self.assertEqual(
            provider_live_eval._base_url("https://proxy.example/v1/chat/completions", ""),
            "https://proxy.example/v1",
        )
        budget = provider_live_eval.Budget(max_requests=1, max_cny=100)
        budget.reserve({"input": "fixture", "max_output_tokens": 16})
        with self.assertRaises(provider_live_eval.BudgetExceeded):
            budget.reserve({"input": "fixture", "max_output_tokens": 16})
        cost = provider_live_eval.Budget(max_requests=10, max_cny=0.0001)
        with self.assertRaises(provider_live_eval.BudgetExceeded):
            cost.add_usage({"input_tokens": 1000, "output_tokens": 1000})
        bounded = provider_live_eval.Budget(max_requests=10, max_cny=1)
        bounded.reserve({"input": "fixture", "max_output_tokens": 512})
        bounded.add_usage({})

        terra = provider_live_eval.Budget(max_requests=15, max_cny=1, model="gpt-5.6-terra")
        terra.reserve({"input": "fixture", "max_output_tokens": 512})
        self.assertLess(terra.worst_case_cny, 0.05)
        self.assertEqual(terra.public()["model"], "gpt-5.6-terra")
        with self.assertRaisesRegex(provider_live_eval.BudgetExceeded, "pricing is unknown"):
            provider_live_eval.Budget(model="unknown-model")
        error_budget = provider_live_eval.Budget(max_requests=2, max_cny=1)
        error_transport = provider_live_eval.LiveResponsesTransport(
            "openai_official", {"base_url": "https://api.example", "key": "fixture"}, error_budget
        )
        response = type("Response", (), {"status_code": 400, "headers": {}})()
        error_transport._observation(response, "fingerprint", {"error": {"code": "invalid"}})
        error_budget.reserve({"input": "next", "max_output_tokens": 16})
        self.assertEqual(error_budget.usage_missing, 0)
        timeout_budget = provider_live_eval.Budget(max_requests=2, max_cny=1)
        timeout_transport = provider_live_eval.LiveResponsesTransport(
            "openai_official", {"base_url": "https://api.example", "key": "fixture"},
            timeout_budget,
        )
        with mock.patch.object(
            provider_live_eval.requests, "post",
            side_effect=provider_live_eval.requests.exceptions.ProxyError(
                "proxy handshake timed out"
            ),
        ):
            timeout = timeout_transport(
                "timeout_cancel", {"model": "gpt-5.6-terra", "max_output_tokens": 16}
            )
        self.assertEqual(timeout["terminal"], "timeout")
        self.assertGreater(bounded.worst_case_cny, 0)
        self.assertEqual(bounded.public()["cost_status"], "upper_bound_only")
        with tempfile.TemporaryDirectory() as root:
            ledger = str(Path(root) / "budget.json")
            first = provider_live_eval.Budget(5000, 500, ledger)
            self.assertEqual(first.max_requests, 1000)
            self.assertEqual(first.max_cny, 100.0)
            first.reserve({"input": "one", "max_output_tokens": 16})
            restored = provider_live_eval.Budget(1000, 100, ledger)
            self.assertEqual(restored.requests, 1)
            with self.assertRaisesRegex(provider_live_eval.BudgetExceeded, "usage missing"):
                restored.add_usage({}, required=True)
            blocked = provider_live_eval.Budget(1000, 100, ledger)
            with self.assertRaisesRegex(provider_live_eval.BudgetExceeded, "reconcile billing"):
                blocked.reserve({"input": "two", "max_output_tokens": 16})

    def test_budget_ledger_serializes_concurrent_process_reservations_and_seeds_once(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = str(Path(root) / "budget.json")
            code = (
                "import sys; sys.path.insert(0, %r); "
                "from provider_live_eval import Budget; "
                "Budget(1000,100,%r).reserve({'input':'fixture','max_output_tokens':16})"
            ) % (str(HERMES), ledger)
            processes = [subprocess.Popen([sys.executable, "-c", code]) for _ in range(8)]
            self.assertTrue(all(process.wait() == 0 for process in processes))
            self.assertEqual(provider_live_eval.Budget(1000, 100, ledger).requests, 8)

            seeded_ledger = str(Path(root) / "seeded.json")
            seeded = provider_live_eval.Budget(1000, 100, seeded_ledger)
            seeded.seed_existing(
                requests_count=25, usage_missing=20,
                reserved_input_tokens=100000, reserved_output_tokens=12800,
            )
            restored = provider_live_eval.Budget(1000, 100, seeded_ledger)
            self.assertEqual(restored.requests, 25)
            self.assertEqual(restored.usage_missing, 20)
            self.assertEqual(restored.reserved_input_tokens, 100000)
            self.assertEqual(restored.reserved_output_tokens, 12800)
            with self.assertRaisesRegex(provider_live_eval.BudgetExceeded, "already initialized"):
                restored.seed_existing(
                    requests_count=1, usage_missing=0,
                    reserved_input_tokens=1, reserved_output_tokens=1,
                )

    def test_official_transcript_with_behavioral_evidence_passes(self):
        report = provider_compat.evaluate("openai_official", MODEL, good_observations())
        self.assertFalse(report["passed"], report)
        self.assertTrue(report["offline_passed"], report)
        self.assertEqual(report["decision"], "OFFLINE_PASS")
        self.assertTrue(all(value == "pass" for value in report["critical"].values()))

    def test_http_200_without_effective_parameter_evidence_holds(self):
        observations = good_observations()
        observations["tool_choice"] = {"status_code": 200, "response": {"output_text": "ok"}}
        observations["reasoning"] = {"status_code": 200, "response": {}}
        observations["store_false"] = {"status_code": 200, "response": {"id": "still-retrievable"}}
        report = provider_compat.evaluate("zelong_proxy", MODEL, observations)
        self.assertFalse(report["passed"])
        self.assertEqual(report["results"]["tool_choice"]["status"], "fail")
        self.assertEqual(report["results"]["reasoning"]["status"], "unknown")
        self.assertEqual(report["results"]["store_false"]["status"], "unknown")

    def test_model_rewrite_or_missing_provider_is_hold(self):
        observations = good_observations()
        observations["model_identity"]["response"]["model"] = "fallback-model"
        report = provider_compat.evaluate("current_provider", MODEL, observations)
        self.assertFalse(report["passed"])
        self.assertEqual(report["results"]["model_identity"]["status"], "fail")

        blocked = provider_compat.evaluate("openai_official", MODEL, {})
        self.assertFalse(blocked["passed"])
        self.assertEqual(blocked["decision"], "HOLD")
        self.assertTrue(all(value == "blocked" for value in blocked["critical"].values()))

    def test_timeout_or_cancel_is_a_required_terminal_observation(self):
        observations = good_observations()
        observations["timeout_cancel"] = {"terminal": "running"}
        report = provider_compat.evaluate("openai_official", MODEL, observations)
        self.assertFalse(report["passed"])
        self.assertEqual(report["results"]["timeout_cancel"]["status"], "unknown")

    def test_runner_sends_the_same_canonical_requests_and_chains_original_response(self):
        captured = []
        fixtures = good_observations()

        def transport(name, request):
            captured.append((name, copy.deepcopy(request)))
            if name == "continuation_first":
                return {"status_code": 200, "response": {"id": "resp-first"}}
            if name == "continuation_second":
                return fixtures["continuation"]
            return fixtures[name]

        report = provider_compat.run_suite("openai_official", MODEL, transport)
        self.assertFalse(report["passed"], report)
        self.assertTrue(report["offline_passed"], report)
        self.assertEqual(report["evidence_source"], "fixture")
        chained = next(request for name, request in captured if name == "continuation_second")
        self.assertEqual(chained["previous_response_id"], "resp-first")
        self.assertEqual(len(captured), 11)
        self.assertTrue(all(request.get("store") is False for _, request in captured))

    def test_live_label_without_correlated_transport_cannot_pass(self):
        report = provider_compat.evaluate("openai_official", MODEL, good_observations())
        self.assertTrue(report["offline_passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["decision"], "OFFLINE_PASS")

    def test_only_correlated_live_transport_can_produce_provider_pass(self):
        fixtures = good_observations()

        class LiveTransport:
            evidence_source = "live_capture"

            def __call__(self, name, request):
                if name == "continuation_first":
                    observation = {"status_code": 200, "response": {"id": "resp-first"}}
                elif name == "continuation_second":
                    observation = copy.deepcopy(fixtures["continuation"])
                else:
                    observation = copy.deepcopy(fixtures[name])
                observation.update(
                    provider_request_id="request-" + name,
                    captured_at=int(time.time()),
                    request_fingerprint=hashlib.sha256(json.dumps(
                        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode()).hexdigest(),
                )
                return observation

        report = provider_compat.run_suite("openai_official", MODEL, LiveTransport())
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["decision"], "PASS")
        self.assertTrue(report["evidence_correlated"])


if __name__ == "__main__":
    unittest.main()
