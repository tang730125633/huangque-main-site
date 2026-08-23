from __future__ import annotations

import ast
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import ModelResponse, TResponseInputItem, TResponseOutputItem, TResponseStreamEvent
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelTracing
from agents.tool import Tool
from agents.usage import Usage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseContentPartAddedEvent,
    ResponseContentPartDoneEvent,
    ResponseCreatedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseInProgressEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
    ResponseTextDoneEvent,
    ResponseUsage,
)
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails


def text_message(value: dict[str, Any] | str, item_id: str = "msg_final") -> ResponseOutputMessage:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ResponseOutputMessage(
        id=item_id,
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
        role="assistant", status="completed", type="message",
    )


def tool_call(name: str, arguments: dict[str, Any], call_id: str) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call", name=name, call_id=call_id,
        arguments=json.dumps(arguments, ensure_ascii=False), status="completed",
    )


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _output(items: Any, call_id: str) -> dict[str, Any] | None:
    for item in _plain(items if isinstance(items, list) else [items]):
        if isinstance(item, dict) and item.get("type") == "function_call_output" and item.get("call_id") == call_id:
            value = item.get("output")
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except ValueError:
                    try:
                        parsed = ast.literal_eval(value)
                        return parsed if isinstance(parsed, dict) else {"value": parsed}
                    except (SyntaxError, ValueError):
                        return {"error": value}
            return value if isinstance(value, dict) else {"value": value}
    return None


def _response(items: list[TResponseOutputItem], response_id: str) -> Response:
    return Response(
        id=response_id, created_at=123, model="scripted-spike", object="response",
        output=items, tool_choice="none", tools=[], top_p=None,
        parallel_tool_calls=False,
        usage=ResponseUsage(
            input_tokens=12, output_tokens=8, total_tokens=20,
            input_tokens_details=InputTokensDetails(cached_tokens=0),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        ),
    )


class RuleModel(Model):
    """Deterministic offline Model that exercises the real SDK Runner and tool loop."""

    def __init__(self, role: str, *, include_quote: bool = True):
        self.role = role
        self.include_quote = include_quote
        self.calls = 0
        self.inputs: list[Any] = []
        self.prefix = uuid.uuid4().hex[:10]

    def _call_id(self, name: str) -> str:
        return "%s_%s" % (self.prefix, name)

    def _specialist(self, items: Any) -> list[TResponseOutputItem]:
        project = _output(items, self._call_id("project"))
        if project is None:
            return [tool_call("project_read", {}, self._call_id("project"))]
        if project.get("error"):
            return [text_message({"status": "needs_input", "missing": [],
                                  "ready_to_quote": False, "next_action": "project_read_failed"})]
        if project.get("ambiguous"):
            return [text_message({"status": "needs_input", "missing": ["script"],
                                  "ready_to_quote": False, "next_action": "choose_source"})]
        capability = _output(items, self._call_id("capability"))
        if capability is None:
            return [tool_call("capability_read", {}, self._call_id("capability"))]
        if capability.get("available") is not True or capability.get("gate_status") != "unlocked":
            return [text_message({"status": "needs_input", "missing": ["script"],
                                  "ready_to_quote": False, "next_action": "capability_unavailable"})]
        assets = _output(items, self._call_id("assets"))
        if assets is None:
            return [tool_call("assets_read", {}, self._call_id("assets"))]
        missing = []
        if not project.get("script"):
            missing.append("script")
        missing.extend(name for name in assets.get("missing") or [] if name in {"avatar", "voice"})
        if missing:
            return [text_message({"status": "needs_input", "missing": missing,
                                  "ready_to_quote": False, "next_action": "provide:" + missing[0]})]
        if self.include_quote:
            quote = _output(items, self._call_id("quote"))
            if quote is None:
                return [tool_call("production_quote_prepare", {
                    "source_version": int((project.get("source") or {}).get("script_version") or 1),
                }, self._call_id("quote"))]
        return [text_message({"status": "ready_to_quote", "missing": [],
                              "ready_to_quote": True, "next_action": "show_quote"})]

    def _master(self, items: Any) -> list[TResponseOutputItem]:
        latest_user = ""
        for item in reversed(_plain(items if isinstance(items, list) else [items])):
            if isinstance(item, dict) and item.get("role") == "user":
                latest_user = str(item.get("content") or "")
                break
        if "暂停" in latest_user or "先不用" in latest_user:
            return [text_message({
                "intent": "pause", "delegate_to": "none", "reply": "好，我们先暂停。",
                "awaiting": "none", "next_action": "await_user", "missing": [],
                "ready_to_quote": False,
            })]
        specialist = _output(items, self._call_id("specialist"))
        if specialist is None:
            return [tool_call("talking_head_video_agent", {
                "input": "读取当前 Project 副本，判断数字人口播还缺什么。",
            }, self._call_id("specialist"))]
        if specialist.get("error"):
            return [text_message({
                "intent": "clarify", "delegate_to": "talking_head_video_agent",
                "reply": "这次读取暂时失败，没有执行任何付费动作。", "awaiting": "user_input",
                "next_action": "retry_read", "missing": [], "ready_to_quote": False,
            })]
        if str(specialist.get("next_action") or "").endswith("_failed"):
            return [text_message({
                "intent": "clarify", "delegate_to": "talking_head_video_agent",
                "reply": "读取当前 Project 的工具失败了；没有生成、报价或扣点。",
                "awaiting": "user_input", "next_action": specialist["next_action"],
                "missing": [], "ready_to_quote": False,
            })]
        missing = specialist.get("missing") or []
        if missing:
            if specialist.get("next_action") == "choose_source":
                return [text_message({
                    "intent": "clarify", "delegate_to": "talking_head_video_agent",
                    "reply": "当前有多篇文案可以制作，你想用哪一篇？",
                    "awaiting": "user_input", "next_action": "choose_source",
                    "missing": ["script"], "ready_to_quote": False,
                })]
            label = {"script": "文案", "avatar": "形象", "voice": "音色"}.get(missing[0], missing[0])
            return [text_message({
                "intent": "delegate", "delegate_to": "talking_head_video_agent",
                "reply": "口播短视频 Agent 已检查当前 Project；现在只缺%s。" % label,
                "awaiting": "user_input", "next_action": "provide:" + missing[0],
                "missing": missing, "ready_to_quote": False,
            })]
        return [text_message({
            "intent": "delegate", "delegate_to": "talking_head_video_agent",
            "reply": "文案、形象和音色已经齐全，可以展示测试报价；尚未生成或扣点。",
            "awaiting": "approval" if self.include_quote else "none",
            "next_action": "show_test_quote", "missing": [], "ready_to_quote": True,
        })]

    def _next(self, items: Any) -> list[TResponseOutputItem]:
        self.calls += 1
        self.inputs.append(_plain(items))
        return self._master(items) if self.role == "master" else self._specialist(items)

    async def get_response(
        self, system_instructions: str | None, input: str | list[TResponseInputItem],
        model_settings: ModelSettings, tools: list[Tool], output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff], tracing: ModelTracing, *, previous_response_id: str | None,
        conversation_id: str | None, prompt: Any | None,
    ) -> ModelResponse:
        items = self._next(input)
        return ModelResponse(output=items, usage=Usage(requests=1, input_tokens=12, output_tokens=8,
                                                        total_tokens=20), response_id="resp_%s_%s" % (self.role, self.calls))

    async def stream_response(
        self, system_instructions: str | None, input: str | list[TResponseInputItem],
        model_settings: ModelSettings, tools: list[Tool], output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff], tracing: ModelTracing, *, previous_response_id: str | None = None,
        conversation_id: str | None = None, prompt: Any | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        items = self._next(input)
        response = _response(items, "resp_%s_%s" % (self.role, self.calls))
        sequence = 0
        yield ResponseCreatedEvent(type="response.created", response=response, sequence_number=sequence); sequence += 1
        yield ResponseInProgressEvent(type="response.in_progress", response=response, sequence_number=sequence); sequence += 1
        for output_index, item in enumerate(items):
            yield ResponseOutputItemAddedEvent(type="response.output_item.added", item=item,
                                               output_index=output_index, sequence_number=sequence); sequence += 1
            if isinstance(item, ResponseFunctionToolCall):
                yield ResponseFunctionCallArgumentsDeltaEvent(
                    type="response.function_call_arguments.delta", item_id=item.call_id,
                    output_index=output_index, delta=item.arguments, sequence_number=sequence,
                ); sequence += 1
                yield ResponseFunctionCallArgumentsDoneEvent(
                    type="response.function_call_arguments.done", item_id=item.call_id,
                    output_index=output_index, arguments=item.arguments, name=item.name,
                    sequence_number=sequence,
                ); sequence += 1
            elif isinstance(item, ResponseOutputMessage):
                part = item.content[0]
                yield ResponseContentPartAddedEvent(
                    type="response.content_part.added", item_id=item.id, output_index=output_index,
                    content_index=0, part=part, sequence_number=sequence,
                ); sequence += 1
                yield ResponseTextDeltaEvent(
                    type="response.output_text.delta", item_id=item.id, output_index=output_index,
                    content_index=0, delta=part.text, logprobs=[], sequence_number=sequence,
                ); sequence += 1
                yield ResponseTextDoneEvent(
                    type="response.output_text.done", item_id=item.id, output_index=output_index,
                    content_index=0, text=part.text, logprobs=[], sequence_number=sequence,
                ); sequence += 1
                yield ResponseContentPartDoneEvent(
                    type="response.content_part.done", item_id=item.id, output_index=output_index,
                    content_index=0, part=part, sequence_number=sequence,
                ); sequence += 1
            yield ResponseOutputItemDoneEvent(type="response.output_item.done", item=item,
                                              output_index=output_index, sequence_number=sequence); sequence += 1
        yield ResponseCompletedEvent(type="response.completed", response=response, sequence_number=sequence)
