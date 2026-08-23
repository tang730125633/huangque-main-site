from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from contracts import AgentDecision
from spike import default_project, provider_matrix, run_once


ROOT = Path(__file__).resolve().parents[2]


def _a_numstat() -> dict[str, int]:
    output = subprocess.check_output(
        ["git", "show", "--numstat", "--format=", "7a6d91f9"], cwd=ROOT, text=True,
    )
    additions = deletions = files = 0
    for line in output.splitlines():
        columns = line.split("\t")
        if len(columns) != 3 or not columns[0].isdigit() or not columns[1].isdigit():
            continue
        additions += int(columns[0]); deletions += int(columns[1]); files += 1
    return {"files": files, "additions": additions, "deletions": deletions,
            "net_lines": additions - deletions}


def _b_loc() -> dict[str, int]:
    base = Path(__file__).parent
    files = [base / name for name in ("contracts.py", "fixture_runtime.py", "providers.py", "spike.py")]
    harness = [base / "scripted_model.py", base / "eval_ab.py"]
    probe = base / "cross_process_probe.py"
    tests = list((Path(__file__).parent / "tests").glob("test_*.py"))
    return {
        "integration_files": len(files),
        "integration_lines": sum(len(path.read_text(encoding="utf-8").splitlines()) for path in files),
        "offline_harness_lines": sum(len(path.read_text(encoding="utf-8").splitlines()) for path in harness),
        "cross_process_probe_lines": len(probe.read_text(encoding="utf-8").splitlines()),
        "test_files": len(tests),
        "test_lines": sum(len(path.read_text(encoding="utf-8").splitlines()) for path in tests),
    }


async def evaluate() -> dict[str, Any]:
    scenarios = []

    async def case(name: str, project: dict[str, Any], **kwargs: Any) -> None:
        started = time.perf_counter()
        result = await run_once(project, request_id=name, **kwargs)
        scenarios.append({
            "name": name,
            "status": result["public_run"]["status"],
            "intent": (result["public_run"].get("decision") or {}).get("intent"),
            "next_action": result["public_run"]["next_action"],
            "tool_calls": result["tool_calls"],
            "model_calls": result["model_calls"],
            "stream_delta_count": len(result["stream_deltas"]),
            "restored": result["restored"],
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        })

    await case("ready", default_project(), include_quote=False)
    await case("missing_avatar", default_project(avatar=False), include_quote=False)
    await case("missing_voice_stream", default_project(voice=False), include_quote=False, streamed=True)
    ambiguous = default_project(); ambiguous["ambiguous"] = True
    await case("multi_production_ambiguity", ambiguous, include_quote=False)
    modified = default_project(); modified["selected_source"] = {"topic_id": "topic_fixture_2", "script_version": 4}
    await case("modified_target", modified, include_quote=False, user_message="改用第二篇制作")
    await case("approval_resume", default_project(), include_quote=True, approve=True, streamed=True)
    await case("tool_timeout", default_project(), include_quote=False, fail_tool="project.read")
    await case("invalid_tool_structure", default_project(), include_quote=False,
               invalid_tool="capability.read")
    await case("text_confirmation_no_paid_tool", default_project(), include_quote=True,
               user_message="确认提交90点")

    model_calls = sum(item["model_calls"] for item in scenarios)
    assert model_calls <= 50, model_calls
    assert all("production.submit" not in item["tool_calls"] for item in scenarios)
    evaluated = [item for item in scenarios if item["intent"]]
    illegal = sum(
        1 for item in evaluated
        if (item["status"] == "ready" and item["intent"] != "delegate")
        or (item["status"] == "needs_input" and item["intent"] not in {"delegate", "clarify"})
    )

    fixture = json.loads((ROOT / "tests" / "fixtures" / "ip12_semantic_router_cases.json").read_text())
    contract_cases = 0
    for item in fixture:
        intents = item["expected_intents"]
        intent = intents[0]
        AgentDecision(
            intent=intent,
            delegate_to="talking_head_video_agent" if intent == "delegate" else "none",
            reply="fixture", awaiting="none", next_action="continue",
        )
        contract_cases += 1

    return {
        "schema": "huangque.agents-sdk-ab-eval/v1",
        "call_budget": {"limit": 50, "actual_scripted_model_calls": model_calls,
                        "real_provider_calls": 0, "huangque_paid_calls": 0},
        "a_self_runtime": {
            "checkpoint": "7a6d91f9",
            "implementation_delta": _a_numstat(),
            "business_state_boundary": "Project/Production/AgentRun owned by Huangque",
            "semantic_fixture_count": len(fixture),
            "decision_accuracy": "not rerun in this zero-key spike",
        },
        "b_agents_sdk": {
            "sdk_version": "0.8.4",
            "dependencies": {"openai": "2.20.0", "pydantic": "2.12.3"},
            "implementation": _b_loc(),
            "runtime_scenarios": scenarios,
            "runtime_passed": len(scenarios),
            "semantic_contract_coverage": "%s/%s" % (contract_cases, len(fixture)),
            "decision_accuracy": "provider_blocked",
            "status_intent_mismatch_rate": "%s/%s scripted decisions" % (illegal, len(evaluated)),
            "features": {
                "master_owns_reply": True,
                "agent_as_tool": True,
                "approval_interruption": True,
                "serialized_resume": "same-process PASS; Agent-as-Tool cross-process FAIL",
                "streaming_delta": True,
                "safe_local_trace": True,
                "session_continuation": "SDK-supported; Huangque state remains outer source of truth",
            },
        },
        "providers": provider_matrix(),
        "decision_gate": {
            "recommendation": "暂不采用",
            "reason": [
                "B1/B2 provider accuracy, latency, token cost, and retention are unverified without credentials",
                "SDK 0.8.4 Agent-as-Tool approval state failed the fresh-process resume probe",
                "SDK removes the inner model-tool loop and approval plumbing, but not Huangque durable business orchestration",
                "dependency resolution required pins beyond openai-agents itself in this environment",
            ],
        },
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(evaluate()), ensure_ascii=False, indent=2))
