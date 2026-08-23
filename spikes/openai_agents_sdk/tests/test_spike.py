from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contracts import AgentDecision
from spike import default_project, provider_matrix, run_once


class AgentsSDKSpikeTests(unittest.TestCase):
    def test_master_owns_final_reply_and_specialist_selects_three_reads(self):
        result = asyncio.run(run_once(
            default_project(), request_id="ready-no-quote", include_quote=False,
        ))
        decision = result["public_run"]["decision"]
        self.assertEqual(decision["delegate_to"], "talking_head_video_agent")
        self.assertTrue(decision["ready_to_quote"])
        self.assertEqual(result["tool_calls"], {
            "project.read": 1, "capability.read": 1, "assets.read": 1,
        })
        self.assertNotIn("handoff", json.dumps(result, ensure_ascii=False).lower())

    def test_missing_avatar_and_voice_waits_naturally(self):
        result = asyncio.run(run_once(
            default_project(avatar=False, voice=False), request_id="missing-assets",
            include_quote=False,
        ))
        decision = result["public_run"]["decision"]
        self.assertEqual(result["public_run"]["status"], "needs_input")
        self.assertEqual(decision["missing"], ["avatar", "voice"])
        self.assertEqual(decision["next_action"], "provide:avatar")
        self.assertNotIn("production.quote.prepare", result["tool_calls"])

    def test_approval_state_serializes_and_resumes_same_outer_run(self):
        result = asyncio.run(run_once(
            default_project(), request_id="approval-resume", include_quote=True, approve=True,
            include_private_state=True,
        ))
        self.assertTrue(result["restored"])
        self.assertIsInstance(result["_private_state_json"], dict)
        self.assertEqual(result["tool_calls"]["production.quote.prepare"], 1)
        self.assertEqual(result["public_run"]["run_id"], "hq_run_approval-resume")
        rendered_state = json.dumps(result["_private_state_json"], ensure_ascii=False)
        self.assertNotIn(default_project()["script"], rendered_state)
        self.assertNotIn("quote_token", rendered_state)
        self.assertNotIn("job_id", rendered_state)

    def test_streaming_emits_delta_without_internal_reasoning(self):
        result = asyncio.run(run_once(
            default_project(voice=False), request_id="stream-missing",
            include_quote=False, streamed=True,
        ))
        self.assertTrue(result["stream_deltas"])
        streamed = "".join(result["stream_deltas"])
        self.assertIn("voice", streamed)
        self.assertNotIn("reasoning", streamed.lower())
        self.assertTrue(any(event["type"] == "trace_started" for event in result["trace_events"]))
        self.assertFalse(any("quote_token" in event or "job_id" in event
                             for event in result["public_run"]["events"]))

    def test_concurrent_traces_remain_grouped_by_outer_run(self):
        async def concurrent():
            return await asyncio.gather(*(
                run_once(default_project(voice=False), request_id="trace-%s" % index,
                         include_quote=False, streamed=True)
                for index in range(10)
            ))

        results = asyncio.run(concurrent())
        for result in results:
            types = [event["type"] for event in result["trace_events"]]
            self.assertEqual(types.count("trace_started"), 1, types)
            self.assertEqual(types.count("trace_completed"), 1, types)

    def test_pause_skips_specialist_and_tool_failure_is_safe(self):
        # RuleModel recognizes pause from the user turn; no specialist tool is invoked.
        from agents import Runner
        from fixture_runtime import SpikeContext
        from spike import build_agents

        master, master_model, specialist_model = build_agents(include_quote=False)
        context = SpikeContext(default_project(), "pause")
        paused = asyncio.run(Runner.run(master, "先不用，暂停", context=context))
        self.assertEqual(paused.final_output.intent, "pause")
        self.assertEqual(context.tool_calls, {})
        self.assertEqual(specialist_model.calls, 0)

        failed = asyncio.run(run_once(
            default_project(), request_id="tool-timeout", include_quote=False,
            fail_tool="project.read",
        ))
        self.assertEqual(failed["public_run"]["decision"]["intent"], "clarify")
        self.assertFalse(failed["public_run"]["decision"]["ready_to_quote"])
        self.assertNotIn("production.quote.prepare", failed["tool_calls"])

    def test_sqlite_session_continues_after_pause_without_owning_business_state(self):
        from agents import Runner, SQLiteSession
        from fixture_runtime import SpikeContext
        from spike import build_agents

        master, _, _ = build_agents(include_quote=False)
        context = SpikeContext(default_project(), "session")
        session = SQLiteSession("sdk_session_fixture")
        first = asyncio.run(Runner.run(master, "先不用，暂停", context=context, session=session))
        second = asyncio.run(Runner.run(master, "继续制作", context=context, session=session))
        self.assertEqual(first.final_output.intent, "pause")
        self.assertEqual(second.final_output.intent, "delegate")
        self.assertTrue(second.final_output.ready_to_quote)
        self.assertEqual(context.tool_calls, {
            "project.read": 1, "capability.read": 1, "assets.read": 1,
        })

    def test_agent_as_tool_approval_cross_process_resume_is_known_failed(self):
        probe = Path(__file__).resolve().parents[1] / "cross_process_probe.py"
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state.json"
            created = subprocess.run(
                [sys.executable, str(probe), "create", str(state)],
                check=True, capture_output=True, text=True,
            )
            resumed = subprocess.run(
                [sys.executable, str(probe), "resume", str(state)],
                check=True, capture_output=True, text=True,
            )
        create_result = json.loads(created.stdout.strip().splitlines()[-1])
        resume_result = json.loads(resumed.stdout.strip().splitlines()[-1])
        self.assertEqual(create_result["interruptions"], 1)
        # SDK 0.8.4 loses nested Agent-as-Tool resume registry across a fresh process.
        self.assertEqual(resume_result["interruptions"], 1)
        self.assertFalse(resume_result["has_final_output"])
        self.assertEqual(resume_result["tool_calls"], {
            "project.read": 1, "capability.read": 1,
            "assets.read": 1,
        })

    def test_provider_paths_fail_closed_without_credentials(self):
        providers = provider_matrix()
        self.assertEqual(providers["b1_openai_responses"]["status"], "provider_blocked")
        self.assertEqual(providers["b2_dashscope_qwen_plus"]["status"], "provider_blocked")

    def test_serialized_state_is_private_and_raw_user_input_never_enters_public_run(self):
        marker = "SENSITIVE_FIXTURE_MARKER_123"
        private = asyncio.run(run_once(
            default_project(), request_id="private-state", include_quote=True,
            user_message=marker, include_private_state=True,
        ))
        state_text = json.dumps(private["_private_state_json"], ensure_ascii=False)
        public_text = json.dumps(private["public_run"], ensure_ascii=False)
        self.assertIn(marker, state_text)
        self.assertNotIn(marker, public_text)
        public_only = asyncio.run(run_once(
            default_project(voice=False), request_id="public-only", include_quote=False,
            user_message=marker,
        ))
        self.assertNotIn("_private_state_json", public_only)

    def test_provider_adapters_construct_without_network_calls(self):
        from providers import build_b1_openai_responses, build_b2_dashscope_chat_completions

        with patch.dict(os.environ, {"OPENAI_API_KEY": "dummy", "DASHSCOPE_API_KEY": "dummy"}):
            b1, b1_meta = build_b1_openai_responses()
            b2, b2_meta = build_b2_dashscope_chat_completions()
        self.assertEqual(b1.__class__.__name__, "OpenAIResponsesModel")
        self.assertEqual(b2.__class__.__name__, "OpenAIChatCompletionsModel")
        self.assertEqual(b1_meta["status"], "ready")
        self.assertEqual(b2_meta["status"], "ready")

    def test_vendor_neutral_contract_covers_semantic_fixture_shape(self):
        for intent in ("direct_answer", "delegate", "clarify", "pause", "status"):
            decision = AgentDecision(
                intent=intent,
                delegate_to="talking_head_video_agent" if intent == "delegate" else "none",
                reply="fixture", awaiting="none", next_action="continue",
            )
            self.assertEqual(decision.intent, intent)


if __name__ == "__main__":
    unittest.main()
