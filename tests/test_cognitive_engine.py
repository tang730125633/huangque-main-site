import copy
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


HERMES = Path(__file__).resolve().parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import cognitive_engine
import semantic_router


def decision(**changes):
    value = {
        "schema": semantic_router.SCHEMA,
        "intent": "direct_answer",
        "delegate_to": "none",
        "tool": "none",
        "reply": "好的",
        "awaiting": "none",
        "confidence": 0.9,
        "reason_codes": [],
        "memory_evidence": [],
        "memory_updates": [],
        "tool_policy": "none",
        "payment_policy": {
            "quote_required": False,
            "explicit_confirmation_required": False,
        },
        "references": {"production_id": "", "category_id": "", "topic_id": ""},
    }
    value.update(changes)
    return value


def memory():
    production = {
        "production_id": "prod_1", "action": "digital-ip-text-generate",
        "family": "video", "status": "draft", "job_present": False,
        "confirmation_present": False, "selected_fields": ["avatar_id", "voice"],
        "quote_token": "private-quote", "job_id": "private-job",
    }
    return {
        "project_id": "project_1",
        "workflow": {"current_module": 6, "completed_modules": [1, 2, 3, 4, 5, 6]},
        "facts": {"location": {"value": "成都", "evidence": "raw evidence"}},
        "preferences": {},
        "confirmed_outputs": [{"excerpt": "private confirmed output"}],
        "content_topics": [{
            "category_id": "category_1", "topic_id": "topic_1", "title": "第一篇",
            "version": 1, "status": "ready", "excerpt": "private full script",
        }],
        "active_content_target": {"category_id": "category_1", "topic_id": "topic_1"},
        "voice_clone": {"status": "complete", "voice_name": "我的声音", "slot_id": "private-slot"},
        "productions": [production], "active_production": production,
        "recent_messages": [{"role": "user", "content": "private raw history"}],
        "active_agent_run": {
            "agent_id": "talking_head_video_agent", "status": "planning",
            "awaiting": "", "next_action": "inspect", "job_id": "private-job",
        },
        "tool_catalog": [{
            "tool": "talking_head.prepare", "delegate_to": "talking_head_video_agent",
            "capability_id": "digital-ip-text-generate", "available": True,
        }],
    }


class CognitiveEngineTests(unittest.TestCase):
    def test_sdk_canary_is_confined_to_one_project(self):
        self.assertEqual(
            cognitive_engine.canary_mode("agents_sdk", {"project_id": "target"}, "target"),
            "agents_sdk",
        )
        self.assertEqual(
            cognitive_engine.canary_mode("agents_sdk", {"project_id": "other"}, "target"),
            "custom",
        )
        self.assertEqual(
            cognitive_engine.canary_mode("agents_sdk", {"project_id": "target"}, ""),
            "custom",
        )
        self.assertEqual(cognitive_engine.canary_mode("custom", {}, "target"), "custom")

    def test_safe_context_is_minimal_and_private_business_fields_stay_out(self):
        context = cognitive_engine.safe_context(memory(), "制作口播视频")
        rendered = json.dumps(context, ensure_ascii=False)
        for private in (
            "private-quote", "private-job", "private-slot", "private raw history",
            "private full script", "private confirmed output", "raw evidence",
        ):
            self.assertNotIn(private, rendered)
        self.assertEqual(context["project"]["facts"]["location"], "成都")
        self.assertEqual(context["agent_run"]["status"], "planning")
        self.assertEqual(context["read_tools"], ["project.read", "capability.read", "assets.read"])

    def test_safe_context_preserves_exact_asset_readiness_without_private_ids(self):
        source = memory()
        source["available_assets"] = {"avatar_ready": False, "voice_ready": True}
        context = cognitive_engine.safe_context(source, "制作")
        self.assertEqual(context["project"]["available_assets"], {
            "avatar_ready": False, "voice_ready": True,
        })
        self.assertEqual(cognitive_engine.asset_readiness(context), {
            "avatar_ready": False, "voice_ready": True,
        })
        source["available_assets"] = {"avatar_ready": True, "voice_ready": False}
        second = cognitive_engine.safe_context(source, "制作")
        self.assertEqual(cognitive_engine.asset_readiness(second), {
            "avatar_ready": True, "voice_ready": False,
        })

    def test_custom_and_sdk_return_the_same_vendor_neutral_contract(self):
        expected = decision(
            intent="delegate", delegate_to="talking_head_video_agent",
            tool="talking_head.prepare", awaiting="confirmation",
            tool_policy="prepare_only",
            payment_policy={"quote_required": True, "explicit_confirmation_required": True},
            references={"production_id": "", "category_id": "category_1", "topic_id": "topic_1"},
        )
        custom = cognitive_engine.decide(memory(), "制作", lambda *_: copy.deepcopy(expected))
        sdk = cognitive_engine.decide(
            memory(), "制作", lambda *_: self.fail("custom fallback should not run"),
            mode="agents_sdk", sdk_enabled=True,
            sdk_decider=lambda *_: copy.deepcopy(expected),
        )
        self.assertEqual(custom, sdk)
        self.assertEqual(set(sdk), set(semantic_router.DECISION_SCHEMA["required"]))

    def test_sdk_invalid_output_timeout_and_exception_fall_back_without_mutation(self):
        source = memory()
        before = copy.deepcopy(source)
        fallback = decision(reply="安全回退")

        def invalid(context, _goal):
            context["project"]["facts"]["location"] = "被修改"
            return decision(
                intent="delegate", delegate_to="talking_head_video_agent",
                tool="talking_head.prepare", awaiting="confirmation", tool_policy="prepare_only",
                payment_policy={"quote_required": True, "explicit_confirmation_required": True},
                references={"production_id": "unknown", "category_id": "", "topic_id": ""},
            )

        before_metrics = cognitive_engine.metrics()
        got = cognitive_engine.decide(
            source, "制作", lambda *_: copy.deepcopy(fallback), mode="agents_sdk",
            sdk_enabled=True, sdk_decider=invalid,
        )
        self.assertEqual(got["reply"], "安全回退")
        self.assertEqual(source, before)
        after_metrics = cognitive_engine.metrics()
        self.assertEqual(after_metrics["sdk_fallbacks"], before_metrics["sdk_fallbacks"] + 1)
        self.assertNotIn("unknown", str(after_metrics["fallback_reasons"]))

        def timeout(*_):
            raise TimeoutError("sdk timeout")

        timed_out = cognitive_engine.decide(
            source, "制作", lambda *_: copy.deepcopy(fallback), mode="agents_sdk",
            sdk_enabled=True, sdk_decider=timeout, timeout_seconds=0.01,
        )
        self.assertEqual(timed_out["reply"], "安全回退")
        errored = cognitive_engine.decide(
            source, "制作", lambda *_: copy.deepcopy(fallback), mode="agents_sdk",
            sdk_enabled=True, sdk_decider=lambda *_: (_ for _ in ()).throw(RuntimeError("stream interrupted")),
        )
        self.assertEqual(errored["reply"], "安全回退")

    def test_flag_off_never_calls_sdk_and_diagnostics_are_allowlisted(self):
        sdk_calls = []
        got = cognitive_engine.decide(
            memory(), "制作", lambda *_: decision(), mode="agents_sdk",
            sdk_enabled=False, sdk_decider=lambda *_: sdk_calls.append(True),
        )
        self.assertEqual(got["intent"], "direct_answer")
        self.assertEqual(sdk_calls, [])
        diagnostics = cognitive_engine.public_diagnostics([{
            "type": "delta", "agent": "ip12_master_agent", "status": "running",
            "quote_token": "private-quote", "job_id": "private-job",
            "arguments": {"text": "SENSITIVE_MARKER"}, "history": "SENSITIVE_MARKER",
        }])
        rendered = json.dumps(diagnostics)
        self.assertNotIn("private", rendered)
        self.assertNotIn("SENSITIVE_MARKER", rendered)

    def test_invalid_custom_output_fails_closed(self):
        got = cognitive_engine.decide(memory(), "制作", lambda *_: {"intent": "delegate"})
        self.assertEqual(got["intent"], "clarify")
        self.assertEqual(got["tool"], "none")

    def test_unknown_provider_and_unproven_dashscope_fail_closed(self):
        context = cognitive_engine.safe_context(memory(), "你好")
        with patch.dict(os.environ, {
            "HERMES_AGENTS_SDK_PROVIDER": "zelong_proxy",
            "HERMES_AGENTS_SDK_MODEL": "wrong-model",
        }):
            with self.assertRaisesRegex(RuntimeError, "provider_unsupported"):
                cognitive_engine.agents_sdk_decider(context, "你好", 1)
        with patch.dict(os.environ, {
            "HERMES_AGENTS_SDK_PROVIDER": "dashscope",
            "HERMES_AGENTS_SDK_MODEL": "qwen-plus",
            "DASHSCOPE_API_KEY": "dummy",
            "HERMES_AGENTS_SDK_DASHSCOPE_CONFORMANT": "0",
        }):
            with self.assertRaisesRegex(RuntimeError, "conformance_not_proven"):
                cognitive_engine.agents_sdk_decider(context, "你好", 1)

    def test_sdk_enablement_requires_pinned_live_conformance_artifact(self):
        release = "release-fixture"
        with patch.dict(os.environ, {"HERMES_AGENTS_SDK_ENABLED": "1"}, clear=False):
            blocked = cognitive_engine.conformance_gate(release)
        self.assertFalse(blocked["valid"])
        self.assertEqual(blocked["reason"], "conformance_artifact_not_configured")

        report = {
            "schema": "ip12.cognitive-conformance/v1", "decision": "PASS",
            "evidence_source": "live_capture", "release_sha": release,
            "corpus_sha256": cognitive_engine.CORPUS_SHA256, "provider": "openai",
            "model": "model-fixture", "expires_at": int(time.time()) + 3600,
            "eval": {
                "schema_rate": 1.0, "safety_rate": 1.0, "route_rate": 0.95,
                "tool_hallucinations": 0, "reference_hallucinations": 0,
                "chat_tool_misfires": 0,
            },
            "custom_eval": {
                "passed": True, "schema_rate": 1.0, "safety_rate": 1.0,
                "route_rate": 0.95, "tool_hallucinations": 0,
                "reference_hallucinations": 0, "chat_tool_misfires": 0,
            },
            "budget": {
                "requests": 100, "max_requests": 120, "estimated_cny": 1.0,
                "worst_case_cny": 9.0, "usage_missing": 0, "max_cny": 10.0,
            },
            "provider_compat": {
                "schema": "ip12.provider-compat-report/v1", "decision": "PASS",
                "passed": True, "evidence_source": "live_capture",
                "evidence_correlated": True,
                "provider": "openai", "model": "model-fixture",
            },
        }
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True).encode()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "conformance.json"
            path.write_bytes(encoded)
            with patch.dict(os.environ, {
                "HERMES_AGENTS_SDK_ENABLED": "1",
                "HERMES_AGENTS_SDK_PROVIDER": "openai",
                "HERMES_AGENTS_SDK_MODEL": "model-fixture",
                "HERMES_AGENTS_SDK_CONFORMANCE_PATH": str(path),
                "HERMES_AGENTS_SDK_CONFORMANCE_SHA256": __import__("hashlib").sha256(encoded).hexdigest(),
            }, clear=False):
                def check(payload):
                    current = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
                    path.write_bytes(current)
                    os.environ["HERMES_AGENTS_SDK_CONFORMANCE_SHA256"] = (
                        __import__("hashlib").sha256(current).hexdigest()
                    )
                    return cognitive_engine.conformance_gate(release)

                allowed = check(report)
                self.assertTrue(allowed["valid"], allowed)
                report["custom_eval"]["passed"] = False
                self.assertFalse(check(report)["valid"])
                report["custom_eval"]["passed"] = True
                report["budget"]["requests"] = 121
                self.assertFalse(check(report)["valid"])
                report["budget"]["requests"] = 100
                report["budget"]["worst_case_cny"] = 10.01
                self.assertFalse(check(report)["valid"])
                report["budget"]["worst_case_cny"] = 9.0
                report["evidence_source"] = "fixture"
                rejected = check(report)
                self.assertFalse(rejected["valid"])

    def test_custom_keeps_valid_references_outside_the_sdk_safe_window(self):
        source = memory()
        source["content_topics"] = [
            {"category_id": "category_%s" % index, "topic_id": "topic_%s" % index,
             "title": "第%s篇" % index, "version": 1, "status": "ready"}
            for index in range(1, 14)
        ]
        expected = decision(
            intent="revise_content", delegate_to="content_revision_agent",
            tool="content.revise", awaiting="feedback", tool_policy="prepare_only",
            references={"production_id": "", "category_id": "category_13", "topic_id": "topic_13"},
        )
        got = cognitive_engine.decide(source, "修改第十三篇", lambda *_: expected)
        self.assertEqual(got["references"]["topic_id"], "topic_13")

    @unittest.skipUnless(importlib.util.find_spec("agents"), "optional Agents SDK is not installed")
    def test_optional_sdk_builds_master_and_specialist_without_session_or_trace_payloads(self):
        from agents import Runner
        from openai.types.responses.response_usage import InputTokensDetails

        self.assertEqual(InputTokensDetails(cached_tokens=0).cached_tokens, 0)

        expected = decision()
        context = cognitive_engine.safe_context(memory(), "你好")
        with patch.dict(os.environ, {
            "HERMES_AGENTS_SDK_OPENAI_API_KEY": "dummy",
            "HERMES_AGENTS_SDK_MODEL": "wrong-model",
        }), patch.object(
            Runner, "run", new=AsyncMock(return_value=SimpleNamespace(final_output=expected))
        ) as run:
            got = cognitive_engine.agents_sdk_decider(
                context, "你好", 1, max_output_tokens=700,
                provider_name="openai", model_name="fixture-model",
            )
        master = run.await_args.args[0]
        self.assertEqual(got, expected)
        self.assertEqual(master.name, "ip12_master_agent")
        self.assertEqual(master.model.model, "fixture-model")
        self.assertEqual([tool.name for tool in master.tools], ["talking_head_video_agent"])
        self.assertIs(master.model_settings.store, False)
        self.assertEqual(master.model_settings.max_tokens, 700)
        self.assertTrue(run.await_args.kwargs["run_config"].tracing_disabled)
        self.assertFalse(run.await_args.kwargs["run_config"].trace_include_sensitive_data)
        self.assertNotIn("session", run.await_args.kwargs)


if __name__ == "__main__":
    unittest.main()
