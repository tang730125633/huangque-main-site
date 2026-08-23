from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from agents import Agent, RunState, Runner, trace
from agents.tracing import TracingProcessor, set_trace_processors
from pydantic import BaseModel

from contracts import AgentDecision, HuangqueAgentRun, public_run
from fixture_runtime import READ_TOOLS, SpikeContext, production_quote_prepare
from providers import build_b1_openai_responses, build_b2_dashscope_chat_completions
from scripted_model import RuleModel


class SpecialistResult(BaseModel):
    status: str
    missing: list[str]
    ready_to_quote: bool
    next_action: str


class SafeTraceProcessor(TracingProcessor):
    """Collect names/IDs only; never stores tool arguments, Project text, or secrets."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.trace_groups: dict[str, str] = {}
        self.events: dict[str, list[dict[str, str]]] = {}

    def _add(self, group_id: str, event: dict[str, str]) -> None:
        with self.lock:
            self.events.setdefault(group_id, []).append(event)

    def reset(self, group_id: str) -> None:
        with self.lock:
            self.events[group_id] = []

    def for_group(self, group_id: str) -> list[dict[str, str]]:
        with self.lock:
            return list(self.events.get(group_id) or [])

    def on_trace_start(self, value: Any) -> None:
        group_id = str(value.group_id or value.trace_id)
        with self.lock:
            self.trace_groups[str(value.trace_id)] = group_id
        self._add(group_id, {"type": "trace_started", "trace_id": str(value.trace_id)})

    def on_trace_end(self, value: Any) -> None:
        with self.lock:
            group_id = self.trace_groups.pop(str(value.trace_id), str(value.group_id or value.trace_id))
        self._add(group_id, {"type": "trace_completed", "trace_id": str(value.trace_id)})

    def on_span_start(self, value: Any) -> None:
        with self.lock:
            group_id = self.trace_groups.get(str(value.trace_id), str(value.trace_id))
        self._add(group_id, {"type": "span_started", "span_id": str(value.span_id),
                             "span": value.span_data.__class__.__name__})

    def on_span_end(self, value: Any) -> None:
        with self.lock:
            group_id = self.trace_groups.get(str(value.trace_id), str(value.trace_id))
        self._add(group_id, {"type": "span_completed", "span_id": str(value.span_id),
                             "span": value.span_data.__class__.__name__})

    def shutdown(self) -> None:
        with self.lock:
            self.trace_groups.clear()

    def force_flush(self) -> None:
        return None


SAFE_TRACE_PROCESSOR = SafeTraceProcessor()
set_trace_processors([SAFE_TRACE_PROCESSOR])


async def _specialist_output(result: Any) -> str:
    value = result.final_output
    if hasattr(value, "model_dump_json"):
        return value.model_dump_json()
    return json.dumps(value, ensure_ascii=False)


def build_agents(*, include_quote: bool = True) -> tuple[Agent[SpikeContext], RuleModel, RuleModel]:
    specialist_model = RuleModel("specialist", include_quote=include_quote)
    specialist = Agent[SpikeContext](
        name="talking_head_video_agent",
        instructions=(
            "SPECIALIST. Read the Project, capability, and assets. Return only missing or "
            "ready_to_quote. Never submit, generate, poll jobs, or own the final user reply."
        ),
        model=specialist_model,
        tools=[*READ_TOOLS, *([production_quote_prepare] if include_quote else [])],
        output_type=SpecialistResult,
    )
    master_model = RuleModel("master", include_quote=include_quote)
    master = Agent[SpikeContext](
        name="ip12_master_agent",
        instructions=(
            "MASTER. You always own the final user reply. Use talking_head_video_agent as a tool; "
            "never hand off conversation ownership. Never call paid Huangque tools."
        ),
        model=master_model,
        tools=[specialist.as_tool(
            tool_name="talking_head_video_agent",
            tool_description="Inspect one digital talking-head goal and return missing/ready state.",
            custom_output_extractor=_specialist_output,
            include_input_schema=True,
        )],
        output_type=AgentDecision,
    )
    return master, master_model, specialist_model


def safe_context(value: SpikeContext) -> dict[str, Any]:
    return {
        "project_ref": str(value.project.get("id") or ""),
        "request_id": value.request_id,
        "tool_calls": dict(value.tool_calls),
        "idempotent_results": dict(value.idempotent_results),
    }


def default_project(*, avatar: bool = True, voice: bool = True, script: bool = True) -> dict[str, Any]:
    return {
        "id": "project_fixture_1",
        "selected_source": {"topic_id": "topic_fixture_1", "script_version": 3},
        "script": "这是脱敏后的已确认口播文案。" if script else "",
        "capability_available": True,
        "gate_status": "unlocked",
        "assets": {
            "avatar": "avatar_fixture_1" if avatar else None,
            "voice": "voice_fixture_1" if voice else None,
        },
    }


async def run_once(project: dict[str, Any], *, request_id: str, include_quote: bool = True,
                   approve: bool = False, streamed: bool = False,
                   fail_tool: str = "", invalid_tool: str = "",
                   user_message: str = "制作一条数字人口播视频",
                   include_private_state: bool = False) -> dict[str, Any]:
    master, master_model, specialist_model = build_agents(include_quote=include_quote)
    context = SpikeContext(
        project=project, request_id=request_id, fail_tool=fail_tool, invalid_tool=invalid_tool,
    )
    outer = HuangqueAgentRun(
        run_id="hq_run_" + request_id, project_id=project["id"],
        status="planning", next_action="run_sdk",
    )
    SAFE_TRACE_PROCESSOR.reset(outer.run_id)
    deltas: list[str] = []
    with trace("ip12-sdk-spike", group_id=outer.run_id):
        if streamed:
            result = Runner.run_streamed(master, user_message, context=context, max_turns=20)
            async for event in result.stream_events():
                if event.type == "raw_response_event" and getattr(event.data, "type", "") == "response.output_text.delta":
                    deltas.append(event.data.delta)
        else:
            result = await Runner.run(master, user_message, context=context, max_turns=20)

    state_json = None
    restored = False
    if result.interruptions:
        outer.status = "awaiting_approval"
        outer.awaiting = "approval"
        outer.next_action = "approve_test_quote"
        state = result.to_state()
        state_json = state.to_json(context_serializer=safe_context, strict_context=True)
        serialized = json.dumps(state_json, ensure_ascii=False)
        assert project.get("script", "") not in serialized
        assert "quote_token" not in serialized and "job_id" not in serialized
        outer.state_ref = "sdk_state_fixture_1"
        if approve:
            restored_state = await RunState.from_json(
                master, state_json, context_override=context, strict_context=True,
            )
            interruptions = restored_state.get_interruptions()
            restored_state.approve(interruptions[0])
            with trace("ip12-sdk-spike-resume", group_id=outer.run_id):
                result = await Runner.run(master, restored_state)
            restored = True

    if not result.interruptions:
        decision = result.final_output
        if not isinstance(decision, AgentDecision):
            decision = AgentDecision.model_validate(decision)
        outer.decision = decision
        outer.status = "needs_input" if decision.awaiting == "user_input" else "ready"
        outer.awaiting = decision.awaiting
        outer.next_action = decision.next_action
    outer.sdk_run_count = master_model.calls + specialist_model.calls
    trace_events = SAFE_TRACE_PROCESSOR.for_group(outer.run_id)[-80:]
    outer.events = trace_events
    response = {
        "public_run": public_run(outer),
        "state_serialized": state_json is not None,
        "restored": restored,
        "tool_calls": dict(context.tool_calls),
        "model_calls": outer.sdk_run_count,
        "stream_deltas": deltas,
        "trace_events": trace_events,
    }
    if include_private_state:
        response["_private_state_json"] = state_json
    return response


def provider_matrix() -> dict[str, Any]:
    _, b1 = build_b1_openai_responses()
    _, b2 = build_b2_dashscope_chat_completions()
    return {
        "b1_openai_responses": b1,
        "b2_dashscope_qwen_plus": b2,
    }


if __name__ == "__main__":
    result = asyncio.run(run_once(default_project(), request_id="demo", approve=True, streamed=True))
    print(json.dumps({"result": result, "providers": provider_matrix()}, ensure_ascii=False, indent=2))
