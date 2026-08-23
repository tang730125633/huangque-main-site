from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from agents import RunState, Runner

from fixture_runtime import SpikeContext
from spike import build_agents, default_project, safe_context


async def create(path: Path) -> None:
    master, _, _ = build_agents(include_quote=True)
    context = SpikeContext(default_project(), "cross-process")
    result = await Runner.run(master, "制作一条数字人口播视频", context=context, max_turns=20)
    assert len(result.interruptions) == 1
    state = result.to_state()
    path.write_text(json.dumps(
        state.to_json(context_serializer=safe_context, strict_context=True),
        ensure_ascii=False,
    ), encoding="utf-8")
    print(json.dumps({"created": True, "interruptions": 1, "tool_calls": context.tool_calls}))


async def resume(path: Path) -> None:
    master, _, _ = build_agents(include_quote=True)
    context = SpikeContext(default_project(), "cross-process")
    state = await RunState.from_json(
        master, json.loads(path.read_text(encoding="utf-8")),
        context_override=context, strict_context=True,
    )
    interruptions = state.get_interruptions()
    state.approve(interruptions[0])
    result = await Runner.run(master, state, max_turns=20)
    print(json.dumps({
        "created": False,
        "interruptions": len(result.interruptions),
        "has_final_output": result.final_output is not None,
        "tool_calls": context.tool_calls,
    }))


if __name__ == "__main__":
    mode, target = sys.argv[1], Path(sys.argv[2])
    asyncio.run(create(target) if mode == "create" else resume(target))
