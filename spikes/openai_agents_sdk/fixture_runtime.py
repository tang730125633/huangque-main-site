from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from agents import RunContextWrapper, function_tool


@dataclass
class SpikeContext:
    project: dict[str, Any]
    request_id: str
    tool_calls: dict[str, int] = field(default_factory=dict)
    idempotent_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail_tool: str = ""
    invalid_tool: str = ""

    def called(self, tool: str) -> None:
        self.tool_calls[tool] = self.tool_calls.get(tool, 0) + 1
        if self.fail_tool == tool:
            raise TimeoutError(tool + " timed out")


@function_tool(name_override="project_read")
def project_read(ctx: RunContextWrapper[SpikeContext]) -> dict[str, Any]:
    """Read the selected script reference and safe Project summary."""
    ctx.context.called("project.read")
    source = ctx.context.project["selected_source"]
    result = {
        "project_id": ctx.context.project["id"],
        "source": copy.deepcopy(source),
        "script": ctx.context.project.get("script", ""),
        "ambiguous": bool(ctx.context.project.get("ambiguous")),
    }
    return {"invalid": True} if ctx.context.invalid_tool == "project.read" else result


@function_tool(name_override="capability_read")
def capability_read(ctx: RunContextWrapper[SpikeContext]) -> dict[str, Any]:
    """Read the fixture capability gate without touching Huangque."""
    ctx.context.called("capability.read")
    result = {
        "action": "digital-ip-text-generate",
        "available": bool(ctx.context.project.get("capability_available", True)),
        "gate_status": ctx.context.project.get("gate_status", "unlocked"),
    }
    return {"available": "invalid"} if ctx.context.invalid_tool == "capability.read" else result


@function_tool(name_override="assets_read")
def assets_read(ctx: RunContextWrapper[SpikeContext]) -> dict[str, Any]:
    """Read fixture avatar and voice readiness."""
    ctx.context.called("assets.read")
    assets = ctx.context.project.get("assets") or {}
    missing = [name for name in ("avatar", "voice") if not assets.get(name)]
    result = {"avatar": assets.get("avatar"), "voice": assets.get("voice"), "missing": missing}
    return {"missing": "invalid"} if ctx.context.invalid_tool == "assets.read" else result


@function_tool(name_override="production_quote_prepare", needs_approval=True)
def production_quote_prepare(
    ctx: RunContextWrapper[SpikeContext], source_version: int,
) -> dict[str, Any]:
    """Return a fake quote object. Never calls the Huangque Bridge."""
    ctx.context.called("production.quote.prepare")
    key = "%s:%s" % (ctx.context.request_id, source_version)
    if key not in ctx.context.idempotent_results:
        ctx.context.idempotent_results[key] = {
            "quote_id": "quote_fixture_1", "cost": 90,
            "currency": "test_points", "confirmation_required": True,
            "source_version": source_version,
        }
    return copy.deepcopy(ctx.context.idempotent_results[key])


READ_TOOLS = [project_read, capability_read, assets_read]
