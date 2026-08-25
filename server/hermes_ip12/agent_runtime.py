"""Durable AgentRun, ToolCall, Observation, and event contracts for IP12."""

import copy
import hashlib
import json
from datetime import datetime, timezone


RUN_SCHEMA = "ip12.agent-run/v1"
EVENT_SCHEMA = "ip12.agent-event/v1"
STATUSES = {
    "planning", "needs_input", "quote_ready", "awaiting_confirmation",
    "submitting", "running", "verifying", "completed", "failed",
}
TERMINAL = {"completed", "failed"}
TRANSITIONS = {
    "planning": {"needs_input", "quote_ready", "completed", "failed"},
    "needs_input": {"planning", "quote_ready", "failed"},
    "quote_ready": {"awaiting_confirmation", "needs_input", "failed"},
    "awaiting_confirmation": {"submitting", "needs_input", "quote_ready", "failed"},
    "submitting": {"running", "failed"},
    "running": {"verifying", "failed"},
    "verifying": {"completed", "failed"},
    "completed": set(),
    "failed": set(),
}
EVENT_TYPES = {
    "status", "tool_started", "tool_completed", "awaiting_input", "quote_ready",
    "job_started", "artifact_ready", "delta", "done", "error",
}
PRIVATE_KEYS = {
    "job_id", "quote_token", "confirmation_id", "idempotency_key",
    "tool_input", "tool_payload", "internal_detail",
}


def _tool(required, output, *, risk, billing="free", confirmation=False,
          retryable=False, idempotency="none", public=(), private=()):
    return {
        "input_schema": {"type": "object", "required": list(required)},
        "output_schema": {"type": "object", "required": list(output)},
        "risk": risk,
        "billing": billing,
        "confirmation_required": bool(confirmation),
        "retryable": bool(retryable),
        "idempotency": idempotency,
        "public_fields": tuple(public),
        "private_fields": tuple(private),
    }


TOOL_CONTRACTS = {
    "account": _tool(
        (), ("points",), risk="read", public=("points",),
        private=("username", "account_id", "scopes", "membership"),
    ),
    "project.read": _tool((), ("source",), risk="read", public=("source", "script_title"), private=("script",)),
    "capability.read": _tool(("action",), ("available",), risk="read", public=("available", "gate_status")),
    "assets.read": _tool((), (), risk="read", public=("avatar_ready", "voice_ready", "missing"), private=("avatar_id", "voice")),
    "production.quote": _tool(
        ("production_id", "input_digest"), ("cost",), risk="read", billing="quote",
        retryable=True, idempotency="production+input_digest",
        public=("cost", "points", "expires_at"), private=("quote_token",),
    ),
    "production.submit": _tool(
        ("production_id", "input_digest"), (), risk="paid_write", billing="paid",
        confirmation=True, retryable=True, idempotency="production.idempotency_key",
        public=("status",), private=("job_id", "quote_token"),
    ),
    "task.read": _tool(
        ("production_id",), ("status",), risk="read", retryable=True,
        idempotency="original_job", public=("status", "refund_status"), private=("job_id", "asset_refs"),
    ),
    "artifact.verify": _tool(
        ("production_id", "asset_digest"), ("decision",), risk="network_read",
        retryable=True, idempotency="asset_digest",
        public=("decision", "issues", "media"),
    ),
    "project.writeback": _tool(
        ("production_id", "artifact_digest"), ("artifact_id",), risk="project_write",
        retryable=True, idempotency="production+artifact_digest",
        public=("artifact_id", "version"),
    ),
}


class AgentRuntimeError(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(value):
    raw = json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _required(schema, value, label):
    if not isinstance(value, dict):
        raise AgentRuntimeError(label + " must be an object")
    missing = [name for name in schema.get("required") or [] if value.get(name) in (None, "", [])]
    if missing:
        raise AgentRuntimeError("%s missing field: %s" % (label, missing[0]))


def _redact(value):
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items() if key not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return copy.deepcopy(value)


def tool_contract(tool_id):
    contract = TOOL_CONTRACTS.get(str(tool_id or ""))
    if not contract:
        raise AgentRuntimeError("tool is not registered: %s" % tool_id)
    return contract


class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, tool_id, handler, *, requires_confirmation=None, private_fields=()):
        contract = tool_contract(tool_id)
        if tool_id in self._tools or not callable(handler):
            raise AgentRuntimeError("invalid tool registration")
        private = set(contract["private_fields"]) | {str(field) for field in private_fields}
        self._tools[tool_id] = {
            "handler": handler,
            "requires_confirmation": (
                contract["confirmation_required"]
                if requires_confirmation is None else bool(requires_confirmation)
            ),
            "private_fields": tuple(sorted(private)),
        }

    def execute(self, tool_id, payload, *, confirmed=False):
        spec = self._tools.get(tool_id)
        if not spec:
            raise AgentRuntimeError("tool is not registered: %s" % tool_id)
        _required(tool_contract(tool_id)["input_schema"], payload or {}, "tool input")
        if spec["requires_confirmation"] and not confirmed:
            raise AgentRuntimeError("tool confirmation required: %s" % tool_id)
        result = spec["handler"](copy.deepcopy(payload or {}))
        if not isinstance(result, dict):
            raise AgentRuntimeError("tool result must be an object")
        _required(tool_contract(tool_id)["output_schema"], result, "tool result")
        return result

    def private_fields(self, tool_id):
        spec = self._tools.get(tool_id) or {}
        return spec.get("private_fields") or ()


def append_event(run, event_type, data=None, *, request_id=""):
    if event_type not in EVENT_TYPES:
        raise AgentRuntimeError("unsupported agent event: %s" % event_type)
    sequence = int(run.get("event_sequence") or 0) + 1
    run["event_sequence"] = sequence
    event = {
        "schema": EVENT_SCHEMA,
        "type": event_type,
        "request_id": str(request_id or ""),
        "run_id": str(run.get("run_id") or run.get("id") or ""),
        "sequence": sequence,
        "timestamp": _now(),
        "data": _redact(data or {}),
    }
    run.setdefault("events", []).append(event)
    run["events"] = run["events"][-200:]
    return event


def record_model_response(run, response, round_index):
    """Persist only correlation and usage, never raw Responses content."""
    if isinstance(response, dict):
        response_id = response.get("id") or response.get("response_id")
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        value = lambda key: usage.get(key)
    else:
        response_id = getattr(response, "response_id", "")
        usage = getattr(response, "usage", None)
        value = lambda key: getattr(usage, key, 0)
    safe_usage = {key: int(value(key) or 0) for key in (
        "requests", "input_tokens", "output_tokens", "total_tokens"
    )}
    entry = {
        "response_id": str(response_id or "")[:160],
        "round": int(round_index),
        "usage": safe_usage,
    }
    run.setdefault("model_responses", []).append(entry)
    run["model_responses"] = run["model_responses"][-4:]
    run["revision"] = int(run.get("revision") or 0) + 1
    run["updated_at"] = _now()
    return entry


def start(project, run_id, policy, goal, *, project_id="", production_id="",
          inputs=None, selected_source=None):
    if not isinstance(project, dict) or not run_id or not getattr(policy, "agent_id", ""):
        raise AgentRuntimeError("invalid agent run")
    runs = project.setdefault("agent_runs", {})
    if run_id in runs:
        run = runs[run_id]
        if run.get("agent_id") != policy.agent_id or run.get("schema") != RUN_SCHEMA:
            raise AgentRuntimeError("agent run contract mismatch")
        return run
    timestamp = _now()
    run = {
        "schema": RUN_SCHEMA,
        "id": run_id,
        "run_id": run_id,
        "project_id": str(project_id or project.get("id") or ""),
        "production_id": str(production_id or ""),
        "agent_id": policy.agent_id,
        "goal": str(goal or ""),
        "status": "planning",
        "awaiting": None,
        "next_action": "plan",
        "inputs": copy.deepcopy(inputs or {}),
        "selected_source": copy.deepcopy(selected_source or {}),
        "tool_calls": {},
        "artifacts": [],
        "result": None,
        "error": None,
        "refund_status": "none",
        "revision": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
        "event_sequence": 0,
        "events": [],
        "_private": {"job_id": None, "quote_token": "", "tool_inputs": {}, "tool_results": {}},
        # Compatibility for the old policy loop; durable production code does not rely on it.
        "step": 0,
        "observations": [],
    }
    runs[run_id] = run
    append_event(run, "status", {"status": "planning", "next_action": "plan"})
    return run


def transition(run, status, *, awaiting=None, next_action=None, error=None,
               refund_status=None, request_id=""):
    status = str(status or "")
    if status not in STATUSES:
        raise AgentRuntimeError("invalid agent status: %s" % status)
    current = str(run.get("status") or "")
    if current not in STATUSES:
        raise AgentRuntimeError("invalid current agent status: %s" % current)
    if status != current and status not in TRANSITIONS[current]:
        raise AgentRuntimeError("invalid agent transition: %s -> %s" % (current, status))
    run["status"] = status
    run["awaiting"] = str(awaiting) if awaiting else None
    if next_action is not None:
        run["next_action"] = str(next_action)
    if error is not None:
        run["error"] = _redact(error)
    if refund_status is not None:
        run["refund_status"] = str(refund_status)
    run["revision"] = int(run.get("revision") or 0) + 1
    run["updated_at"] = _now()
    event_type = (
        "done" if status == "completed" else
        "error" if status == "failed" else
        "awaiting_input" if status == "needs_input" else
        "quote_ready" if status in {"quote_ready", "awaiting_confirmation"} else
        "job_started" if status in {"submitting", "running"} else
        "status"
    )
    append_event(run, event_type, {
        "status": status, "awaiting": run.get("awaiting"),
        "next_action": run.get("next_action"), "refund_status": run.get("refund_status"),
    }, request_id=request_id)
    return run


def record_tool(run, tool_id, *, phase, input_value=None, output=None, error=None,
                call_id="", idempotency_key="", request_id=""):
    contract = tool_contract(tool_id)
    phase = str(phase or "")
    if phase not in {"started", "completed", "failed"}:
        raise AgentRuntimeError("invalid tool phase")
    if input_value is not None:
        _required(contract["input_schema"], input_value, "tool input")
    if phase == "completed":
        _required(contract["output_schema"], output or {}, "tool result")
    call_id = str(call_id or "%s:%s" % (run.get("run_id") or run.get("id"), tool_id))
    call = run.setdefault("tool_calls", {}).get(call_id) or {
        "call_id": call_id,
        "tool": tool_id,
        "risk": contract["risk"],
        "billing": contract["billing"],
        "confirmation_required": contract["confirmation_required"],
        "retryable": contract["retryable"],
        "idempotency": contract["idempotency"],
        "attempts": 0,
        "started_at": _now(),
    }
    if phase == "started":
        call["attempts"] = int(call.get("attempts") or 0) + 1
    call.update(status=phase, updated_at=_now())
    if input_value is not None:
        call["input_digest"] = _digest(input_value)
        run["_private"]["tool_inputs"][call_id] = copy.deepcopy(input_value)
    if idempotency_key:
        run["_private"].setdefault("idempotency_keys", {})[call_id] = str(idempotency_key)
    if phase == "completed":
        private_fields = set(contract["private_fields"])
        public = {key: copy.deepcopy(value) for key, value in (output or {}).items()
                  if key in contract["public_fields"] and key not in private_fields}
        private = {key: copy.deepcopy(value) for key, value in (output or {}).items()
                   if key in private_fields}
        call["observation"] = _redact(public)
        if private:
            run["_private"]["tool_results"][call_id] = private
        if tool_id == "production.submit" and (output or {}).get("job_id") not in (None, ""):
            run["_private"]["job_id"] = str(output["job_id"])
        if tool_id == "production.quote" and (output or {}).get("quote_token"):
            run["_private"]["quote_token"] = str(output["quote_token"])
    elif phase == "failed":
        call["error"] = _redact(error or {"code": "tool_failed"})
    run["tool_calls"][call_id] = call
    run["revision"] = int(run.get("revision") or 0) + 1
    run["updated_at"] = _now()
    append_event(run, "error" if phase == "failed" else "tool_%s" % phase, {
        "tool": tool_id, "call_id": call_id, "status": phase,
        **({"observation": call.get("observation") or {}} if phase == "completed" else {}),
        **({"error": call.get("error") or {}} if phase == "failed" else {}),
    }, request_id=request_id)
    return call


def _apply_event(run, event):
    event = event if isinstance(event, dict) else {}
    event_type = str(event.get("type") or "")
    if event_type == "provide_input":
        values = event.get("values")
        if not isinstance(values, dict):
            raise AgentRuntimeError("provided input must be an object")
        run["inputs"].update(copy.deepcopy(values))
        if run.get("status") == "needs_input":
            transition(run, "planning", next_action="plan")
    elif event_type == "confirm":
        run["approval"] = str(event.get("approval") or "")
    elif event_type == "tick":
        run["external_resume_step"] = run.get("step")
    elif event_type:
        raise AgentRuntimeError("unsupported agent event")


def _last_result(run, tool_id):
    for item in reversed(run.get("observations") or []):
        if item.get("tool") == tool_id:
            private = ((run.get("_private") or {}).get("tool_results") or {}).get(
                item.get("call_id"), {}
            )
            return {**(item.get("result") or {}), **private}
    return {}


def resume(project, run_id, policy, tools, event=None, max_steps=16):
    """Compatibility loop used by tests; production HTTP paths journal the same contract directly."""
    run = (project.get("agent_runs") or {}).get(run_id) if isinstance(project, dict) else None
    if not isinstance(run, dict) or run.get("agent_id") != policy.agent_id:
        raise AgentRuntimeError("agent run not found")
    if run.get("status") in TERMINAL:
        return run
    _apply_event(run, event)
    for _ in range(max_steps):
        instruction = policy.next_action(copy.deepcopy(run))
        if not isinstance(instruction, dict):
            raise AgentRuntimeError("agent instruction must be an object")
        kind = instruction.get("type")
        if kind == "wait":
            awaiting = str(instruction.get("awaiting") or "input")
            target = (
                "awaiting_confirmation" if awaiting == "confirmation" else
                "running" if awaiting == "external" else "needs_input"
            )
            if run.get("status") == "quote_ready" and target == "awaiting_confirmation":
                transition(run, target, awaiting=awaiting,
                           next_action=str(instruction.get("next_action") or "wait"))
            elif run.get("status") != target:
                transition(run, target, awaiting=awaiting,
                           next_action=str(instruction.get("next_action") or "wait"))
            return run
        if kind == "complete":
            run["result"] = copy.deepcopy(instruction.get("result") or {})
            transition(run, "completed", next_action=str(instruction.get("next_action") or "request_feedback"))
            return run
        if kind == "fail":
            transition(run, "failed", error=copy.deepcopy(instruction.get("error") or {"code": "agent_failed"}),
                       next_action=str(instruction.get("next_action") or "explain_failure"))
            return run
        if kind != "tool":
            raise AgentRuntimeError("unsupported agent instruction")
        tool_id = str(instruction.get("tool") or "")
        call_id = "%s:%s:%s" % (run_id, run["step"], tool_id)
        call = run["tool_calls"].get(call_id)
        if call is None:
            confirmed = run.get("approval") == str(instruction.get("approval") or "")
            input_value = instruction.get("input") or {}
            record_tool(run, tool_id, phase="started", input_value=input_value, call_id=call_id)
            result = tools.execute(tool_id, input_value, confirmed=confirmed)
            record_tool(run, tool_id, phase="completed", output=result, call_id=call_id)
            private = ((run.get("_private") or {}).get("tool_results") or {}).get(call_id, {})
            public_result = (run["tool_calls"][call_id].get("observation") or {})
            call = {"call_id": call_id, "tool": tool_id, "result": copy.deepcopy(public_result)}
            run["tool_calls"][call_id]["result"] = copy.deepcopy(public_result)
            if private:
                run["_private"]["tool_results"][call_id] = private
            if confirmed:
                run.pop("approval", None)
            if tool_id == "production.quote" and run.get("status") == "planning":
                transition(run, "quote_ready", next_action="await_confirmation")
            elif tool_id == "production.submit" and run.get("status") == "awaiting_confirmation":
                transition(run, "submitting", next_action="persist_job")
                transition(run, "running", awaiting="external", next_action="poll_original_job")
            elif tool_id == "task.read" and result.get("status") == "done" and run.get("status") == "running":
                transition(run, "verifying", next_action="verify_artifact")
        run["observations"].append({
            "call_id": call_id, "tool": tool_id,
            "result": copy.deepcopy(call.get("result") or call.get("observation") or {}),
        })
        run["step"] += 1
        run["next_action"] = "observe"
    raise AgentRuntimeError("agent exceeded step limit")


class TalkingHeadPolicy:
    agent_id = "talking_head_video_agent"

    def next_action(self, run):
        project = _last_result(run, "project.read")
        if not project:
            return {"type": "tool", "tool": "project.read", "input": {}}
        capability = _last_result(run, "capability.read")
        if not capability:
            return {"type": "tool", "tool": "capability.read", "input": {"action": "digital-ip-text-generate"}}
        if capability.get("available") is not True:
            return {"type": "fail", "error": {"code": "capability_unavailable"}}
        assets = _last_result(run, "assets.read")
        if not assets:
            return {"type": "tool", "tool": "assets.read", "input": {}}
        inputs = {**project, **assets, **(run.get("inputs") or {})}
        missing = [name for name in ("script", "avatar_id", "voice") if not inputs.get(name)]
        if missing:
            return {"type": "wait", "awaiting": "material", "next_action": "provide:" + ",".join(missing)}
        quote = _last_result(run, "production.quote")
        if not quote:
            return {"type": "tool", "tool": "production.quote", "input": {
                "production_id": run.get("production_id"),
                "input_digest": _digest({key: inputs[key] for key in ("script", "avatar_id", "voice")}),
            }}
        submitted = _last_result(run, "production.submit")
        if not submitted:
            if run.get("approval") != "current_quote":
                return {"type": "wait", "awaiting": "confirmation", "next_action": "confirm_quote:%s" % quote.get("cost")}
            return {"type": "tool", "tool": "production.submit", "approval": "current_quote", "input": {
                "production_id": run.get("production_id"), "input_digest": run.get("inputs", {}).get("input_digest", "pending"),
            }}
        task = _last_result(run, "task.read")
        if not task or task.get("status") in {"queued", "running"}:
            if task and run.get("external_resume_step") != run.get("step"):
                return {"type": "wait", "awaiting": "external", "next_action": "poll_original_job"}
            return {"type": "tool", "tool": "task.read", "input": {"production_id": run.get("production_id")}}
        if task.get("status") != "done":
            return {"type": "fail", "error": {"code": task.get("code") or "production_failed"}}
        verified = _last_result(run, "artifact.verify")
        if not verified:
            return {"type": "tool", "tool": "artifact.verify", "input": {
                "production_id": run.get("production_id"), "asset_digest": _digest(task.get("asset_refs") or []),
            }}
        if verified.get("decision") != "pass":
            return {"type": "fail", "error": {"code": "artifact_verification_failed", "issues": verified.get("issues") or []}}
        writeback = _last_result(run, "project.writeback")
        if not writeback:
            return {"type": "tool", "tool": "project.writeback", "input": {
                "production_id": run.get("production_id"), "artifact_digest": _digest(task.get("asset_refs") or []),
            }}
        return {"type": "complete", "result": {"artifacts": task.get("asset_refs") or [], "verification": verified, "writeback": writeback},
                "next_action": "request_feedback"}


def public_event(event):
    return _redact(event if isinstance(event, dict) else {})


def public_run(run):
    result = _redact(run if isinstance(run, dict) else {})
    result.pop("_private", None)
    result.pop("observations", None)
    result.pop("inputs", None)
    result.pop("model_responses", None)
    return result
