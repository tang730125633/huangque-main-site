from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentDecision(BaseModel):
    """Vendor-neutral output consumed by the Huangque business layer."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    intent: Literal[
        "direct_answer", "continue_ip12", "delegate", "revise_content",
        "clarify", "pause", "status",
    ]
    delegate_to: Literal[
        "none", "voice_clone_agent", "audio_preview_agent",
        "talking_head_video_agent", "content_revision_agent",
    ] = "none"
    reply: str = Field(min_length=1, max_length=1200)
    awaiting: Literal["none", "user_input", "approval"] = "none"
    next_action: str = Field(default="continue", max_length=120)
    missing: list[Literal["script", "avatar", "voice"]] = Field(default_factory=list)
    ready_to_quote: bool = False


class HuangqueAgentRun(BaseModel):
    """Outer durable business work order. SDK state remains private."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["huangque.agent-run/spike-v1"] = Field(
        default="huangque.agent-run/spike-v1", alias="schema",
    )
    run_id: str
    project_id: str
    agent_id: Literal["ip12_master_agent"] = "ip12_master_agent"
    status: Literal["planning", "needs_input", "awaiting_approval", "ready", "failed"]
    awaiting: Literal["none", "user_input", "approval"] = "none"
    next_action: str
    sdk_run_count: int = 0
    state_ref: str = ""
    decision: AgentDecision | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)


def public_run(run: HuangqueAgentRun) -> dict[str, Any]:
    value = run.model_dump(mode="json", by_alias=True)
    value.pop("state_ref", None)
    for event in value.get("events") or []:
        for private in ("quote_token", "job_id", "arguments", "tool_input", "raw_project"):
            event.pop(private, None)
    return value
