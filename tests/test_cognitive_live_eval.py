import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


HERMES = Path(__file__).resolve().parents[1] / "server" / "hermes_ip12"
import sys
sys.path.insert(0, str(HERMES))

import cognitive_engine
import cognitive_live_eval
import ip12_harness as coach_harness
import provider_live_eval


def passing_engine_report():
    return {
        "passed": True,
        "rates": {"schema_rate": 1.0, "safety_rate": 1.0, "route_rate": 0.95},
        "totals": {
            "tool_hallucinations": 0, "reference_hallucinations": 0,
            "chat_tool_misfires": 0,
        },
        "engine": {"calls": 44, "errors": {}, "latency_ms": {"average": 1, "p95": 2}},
        "results": [{"id": "case-ok", "schema": True, "route": True, "safety": True,
                     "reference_hallucinations": 0}],
    }


def passing_provider_report(model="gpt-5.6-terra"):
    return {
        "schema": "ip12.provider-compat-report/v1", "decision": "PASS",
        "passed": True, "evidence_source": "live_capture",
        "evidence_correlated": True, "provider": "openai_official", "model": model,
    }


class CognitiveLiveEvalTests(unittest.TestCase):
    def test_eval_summary_lists_only_failed_case_ids_without_raw_output(self):
        report = passing_engine_report()
        report["results"].append({
            "id": "case-bad", "schema": True, "route": False, "safety": True,
            "reference_hallucinations": 1,
        })
        self.assertEqual(cognitive_live_eval._eval_summary(report)["failed_case_ids"], ["case-bad"])

    def test_over_authorized_budget_stops_before_provider_lookup(self):
        args = SimpleNamespace(max_requests=121, max_cny=10)
        with patch.object(
            provider_live_eval, "provider_configs",
            side_effect=AssertionError("provider must not be read"),
        ):
            with self.assertRaisesRegex(RuntimeError, "request_budget"):
                cognitive_live_eval.run_t3(args)
        args.max_requests = 120
        args.max_cny = 12.01
        with self.assertRaisesRegex(RuntimeError, "cost_budget"):
            cognitive_live_eval.run_canary(args)

    def test_t3_writes_a_gate_compatible_private_artifact(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "conformance.json"
            args = SimpleNamespace(
                corpus=str(Path(__file__).parent / "fixtures/ip12_semantic_router_cases.json"),
                max_requests=120, max_cny=12, budget_ledger=str(Path(root) / "budget.json"),
                model="gpt-5.6-terra", max_output_tokens=700, timeout=10,
                release_sha="release-under-test", valid_seconds=3600, output=str(output),
            )
            with patch.object(
                provider_live_eval, "provider_configs",
                return_value={"openai_official": {"base_url": "https://api.openai.com/v1", "key": "dummy"}},
            ), patch.object(
                provider_live_eval, "run_compat", return_value=passing_provider_report(),
            ), patch.object(
                cognitive_live_eval.eval_contract, "run_engine",
                side_effect=[passing_engine_report(), passing_engine_report()],
            ):
                artifact, digest = cognitive_live_eval.run_t3(args)
            self.assertEqual(artifact["decision"], "PASS")
            self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")
            with patch.dict(os.environ, {
                "HERMES_AGENTS_SDK_ENABLED": "1",
                "HERMES_AGENTS_SDK_PROVIDER": "openai",
                "HERMES_AGENTS_SDK_MODEL": "gpt-5.6-terra",
                "HERMES_AGENTS_SDK_CONFORMANCE_PATH": str(output),
                "HERMES_AGENTS_SDK_CONFORMANCE_SHA256": digest,
            }, clear=False):
                self.assertTrue(cognitive_engine.conformance_gate("release-under-test")["valid"])

    def test_canary_reads_one_project_without_writing_or_preparing(self):
        with tempfile.TemporaryDirectory() as root:
            project_path = Path(root) / "project.json"
            project = {
                "id": "project-canary", "title": "Canary", "messages": [],
                "coach_state": coach_harness.initial_state(), "productions": {},
            }
            project_path.write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
            before = project_path.read_bytes()
            safe = {
                "schema": "ip12.semantic-master-decision/v1", "intent": "status",
                "delegate_to": "none", "tool": "project.status", "reply": "已完成六步",
                "awaiting": "none", "confidence": 0.9, "reason_codes": [],
                "memory_evidence": [], "memory_updates": [], "tool_policy": "read_only",
                "payment_policy": {"quote_required": False, "explicit_confirmation_required": False},
                "references": {"production_id": "", "category_id": "", "topic_id": ""},
            }
            args = SimpleNamespace(
                release_sha="release-under-test", project=str(project_path),
                budget_ledger=str(Path(root) / "budget.json"), max_requests=120, max_cny=10,
                model="gpt-5.6-terra", max_output_tokens=700, timeout=10, message="到哪了",
            )
            with patch.dict(os.environ, {
                "HERMES_AGENTS_SDK_CANARY_PROJECT_ID": "project-canary",
            }, clear=False), patch.object(
                cognitive_engine, "conformance_gate", return_value={"valid": True},
            ), patch.object(
                cognitive_live_eval, "_sdk_decider", return_value=lambda *_: safe,
            ):
                result = cognitive_live_eval.run_canary(args)
            self.assertEqual(result["decision"], "PASS")
            self.assertEqual(before, project_path.read_bytes())

    def test_async_budget_hooks_count_request_and_require_usage(self):
        class Request:
            content = b'{"max_output_tokens":10,"input":"hello"}'

        class Response:
            status_code = 200
            async def aread(self):
                return b'{"usage":{"input_tokens":5,"output_tokens":3}}'

        with tempfile.TemporaryDirectory() as root:
            budget = provider_live_eval.Budget(
                2, 1, str(Path(root) / "budget.json"), "gpt-5.6-terra",
            )
            hooks = cognitive_live_eval.AsyncBudgetHooks(budget)
            asyncio.run(hooks.request(Request()))
            asyncio.run(hooks.response(Response()))
            self.assertEqual(budget.requests, 1)
            self.assertEqual(budget.usage_reports, 1)
            self.assertEqual(budget.usage_missing, 0)


if __name__ == "__main__":
    unittest.main()
