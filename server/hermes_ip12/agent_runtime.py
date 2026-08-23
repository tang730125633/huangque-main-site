"""Small durable plan → tool → observation loop for IP12 agents."""

import copy


RUN_SCHEMA = "ip12.agent-run/v1"
TERMINAL = {"completed", "failed"}


class AgentRuntimeError(RuntimeError):
    pass


class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, tool_id, handler, *, requires_confirmation=False, private_fields=()):
        if not tool_id or tool_id in self._tools or not callable(handler):
            raise AgentRuntimeError("invalid tool registration")
        self._tools[tool_id] = {
            "handler": handler,
            "requires_confirmation": bool(requires_confirmation),
            "private_fields": tuple(str(field) for field in private_fields),
        }

    def execute(self, tool_id, payload, *, confirmed=False):
        spec = self._tools.get(tool_id)
        if not spec:
            raise AgentRuntimeError("tool is not registered: %s" % tool_id)
        if spec["requires_confirmation"] and not confirmed:
            raise AgentRuntimeError("tool confirmation required: %s" % tool_id)
        result = spec["handler"](copy.deepcopy(payload or {}))
        if not isinstance(result, dict):
            raise AgentRuntimeError("tool result must be an object")
        return result

    def private_fields(self, tool_id):
        spec = self._tools.get(tool_id) or {}
        return spec.get("private_fields") or ()


def start(project, run_id, policy, goal):
    if not isinstance(project, dict) or not run_id or not getattr(policy, "agent_id", ""):
        raise AgentRuntimeError("invalid agent run")
    runs = project.setdefault("agent_runs", {})
    if run_id in runs:
        return runs[run_id]
    run = {
        "schema": RUN_SCHEMA,
        "id": run_id,
        "agent_id": policy.agent_id,
        "goal": str(goal or ""),
        "status": "running",
        "step": 0,
        "awaiting": None,
        "next_action": "plan",
        "inputs": {},
        "observations": [],
        "tool_calls": {},
        "result": None,
        "error": None,
        "_private": {"tool_results": {}},
    }
    runs[run_id] = run
    return run


def _apply_event(run, event):
    event = event if isinstance(event, dict) else {}
    event_type = str(event.get("type") or "")
    if event_type == "provide_input":
        values = event.get("values")
        if not isinstance(values, dict):
            raise AgentRuntimeError("provided input must be an object")
        run["inputs"].update(copy.deepcopy(values))
    elif event_type == "confirm":
        run["approval"] = str(event.get("approval") or "")
    elif event_type == "tick":
        run["external_resume_step"] = run.get("step")
    elif event_type:
        raise AgentRuntimeError("unsupported agent event")


def resume(project, run_id, policy, tools, event=None, max_steps=12):
    run = (project.get("agent_runs") or {}).get(run_id) if isinstance(project, dict) else None
    if not isinstance(run, dict) or run.get("agent_id") != policy.agent_id:
        raise AgentRuntimeError("agent run not found")
    if run.get("status") in TERMINAL:
        return run
    _apply_event(run, event)
    run.update(status="running", awaiting=None)
    for _ in range(max_steps):
        instruction = policy.next_action(copy.deepcopy(run))
        if not isinstance(instruction, dict):
            raise AgentRuntimeError("agent instruction must be an object")
        kind = instruction.get("type")
        if kind == "wait":
            run.update(
                status="waiting",
                awaiting=str(instruction.get("awaiting") or "input"),
                next_action=str(instruction.get("next_action") or "wait"),
            )
            return run
        if kind == "complete":
            run.update(
                status="completed",
                result=copy.deepcopy(instruction.get("result") or {}),
                next_action=str(instruction.get("next_action") or "request_feedback"),
            )
            return run
        if kind == "fail":
            run.update(
                status="failed",
                error=copy.deepcopy(instruction.get("error") or {"code": "agent_failed"}),
                next_action=str(instruction.get("next_action") or "explain_failure"),
            )
            return run
        if kind != "tool":
            raise AgentRuntimeError("unsupported agent instruction")
        tool_id = str(instruction.get("tool") or "")
        call_id = "%s:%s:%s" % (run_id, run["step"], tool_id)
        call = run["tool_calls"].get(call_id)
        if call is None:
            confirmed = run.get("approval") == str(instruction.get("approval") or "")
            result = tools.execute(
                tool_id,
                instruction.get("input") or {},
                confirmed=confirmed,
            )
            private = {
                key: result[key] for key in tools.private_fields(tool_id) if key in result
            }
            public_result = {
                key: value for key, value in result.items() if key not in private
            }
            call = {
                "call_id": call_id,
                "tool": tool_id,
                "input": copy.deepcopy(instruction.get("input") or {}),
                "result": copy.deepcopy(public_result),
            }
            run["tool_calls"][call_id] = call
            if private:
                run["_private"]["tool_results"][call_id] = copy.deepcopy(private)
            if confirmed:
                run.pop("approval", None)
        run["observations"].append({
            "call_id": call_id,
            "tool": tool_id,
            "result": copy.deepcopy(call["result"]),
        })
        run["step"] += 1
        run["next_action"] = "observe"
    raise AgentRuntimeError("agent exceeded step limit")


def _last_result(run, tool_id):
    for item in reversed(run.get("observations") or []):
        if item.get("tool") == tool_id:
            private = ((run.get("_private") or {}).get("tool_results") or {}).get(
                item.get("call_id"), {}
            )
            return {**(item.get("result") or {}), **private}
    return {}


class TalkingHeadPolicy:
    agent_id = "talking_head_video_agent"

    def next_action(self, run):
        project = _last_result(run, "project.read")
        if not project:
            return {"type": "tool", "tool": "project.read", "input": {}}
        assets = _last_result(run, "assets.read")
        if not assets:
            return {"type": "tool", "tool": "assets.read", "input": {}}
        inputs = {**project, **assets, **(run.get("inputs") or {})}
        missing = [name for name in ("script", "avatar_id", "voice") if not inputs.get(name)]
        if missing:
            return {
                "type": "wait",
                "awaiting": "material",
                "next_action": "provide:" + ",".join(missing),
            }
        quote = _last_result(run, "production.quote")
        if not quote:
            return {
                "type": "tool",
                "tool": "production.quote",
                "input": {key: inputs[key] for key in ("script", "avatar_id", "voice")},
            }
        submitted = _last_result(run, "production.submit")
        if not submitted:
            if run.get("approval") != "current_quote":
                return {
                    "type": "wait",
                    "awaiting": "confirmation",
                    "next_action": "confirm_quote:%s" % quote.get("cost"),
                }
            return {
                "type": "tool",
                "tool": "production.submit",
                "approval": "current_quote",
                "input": {"quote_token": quote.get("quote_token"), **{
                    key: inputs[key] for key in ("script", "avatar_id", "voice")
                }, "idempotency_key": "agent-" + str(run.get("id") or "")},
            }
        task = _last_result(run, "task.read")
        if not task or task.get("status") in {"queued", "running"}:
            if task and run.get("external_resume_step") != run.get("step"):
                return {"type": "wait", "awaiting": "external", "next_action": "poll_original_job"}
            return {
                "type": "tool", "tool": "task.read",
                "input": {"job_id": submitted.get("job_id")},
            }
        if task.get("status") != "done":
            return {
                "type": "fail",
                "error": {"code": task.get("code") or "production_failed"},
            }
        quality = _last_result(run, "quality.verify")
        if not quality:
            return {
                "type": "tool", "tool": "quality.verify",
                "input": {"asset_refs": task.get("asset_refs") or []},
            }
        if quality.get("decision") != "pass":
            return {"type": "fail", "error": {"code": "quality_failed", "issues": quality.get("issues") or []}}
        return {
            "type": "complete",
            "result": {"job_id": submitted.get("job_id"), "asset_refs": task.get("asset_refs") or [], "quality": quality},
            "next_action": "request_feedback",
        }


def public_run(run):
    result = copy.deepcopy(run if isinstance(run, dict) else {})
    result.pop("_private", None)
    return result
