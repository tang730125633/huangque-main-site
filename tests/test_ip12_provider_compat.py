import copy
import hashlib
import json
import sys
import time
import unittest
from pathlib import Path


HERMES = Path(__file__).resolve().parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import provider_compat


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
