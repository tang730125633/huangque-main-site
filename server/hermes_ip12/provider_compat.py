"""Provider-neutral request probes and evidence grading for IP12 cognitive engines."""

import copy
import hashlib
import json


SCHEMA = "ip12.provider-compat-report/v1"
CRITICAL_PROBES = {
    "structured_output", "tool_choice", "stream", "continuation",
    "reasoning", "store_false", "usage", "model_identity", "error_contract",
    "timeout_cancel",
}
_LIVE_ATTESTATION = object()


def build_requests(model):
    model = str(model or "").strip()
    if not model:
        raise ValueError("provider model is required")
    common = {
        "model": model,
        "input": "Return only the requested fixture result.",
        "store": False,
        "max_output_tokens": 512,
        "metadata": {"suite": "ip12-provider-compat-v1"},
    }
    strict_schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"status": {"type": "string", "enum": ["ok"]}},
        "required": ["status"],
    }
    tool = {
        "type": "function", "name": "inspect_project",
        "description": "Return the requested fixture project reference.",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {"project_ref": {"type": "string", "enum": ["project_fixture"]}},
            "required": ["project_ref"],
        },
        "strict": True,
    }
    return {
        "structured_output": {
            **copy.deepcopy(common),
            "text": {"format": {"type": "json_schema", "name": "compat_result",
                                "strict": True, "schema": strict_schema}},
        },
        "tool_choice": {
            **copy.deepcopy(common), "tools": [tool],
            "tool_choice": {"type": "function", "name": "inspect_project"},
        },
        "stream": {**copy.deepcopy(common), "stream": True},
        "continuation_first": {
            **copy.deepcopy(common), "input": "Return exactly IP12-CONTINUITY-731."
        },
        "continuation_second": {
            **copy.deepcopy(common), "input": "Return the marker from the previous assistant output.",
        },
        "reasoning": {**copy.deepcopy(common), "reasoning": {"effort": "low"}},
        "store_false": copy.deepcopy(common),
        "usage": copy.deepcopy(common),
        "model_identity": copy.deepcopy(common),
        "error_contract": {
            **copy.deepcopy(common), "tools": [tool],
            "tool_choice": {"type": "function", "name": "missing_tool"},
        },
    }


def _output_text(response):
    if not isinstance(response, dict):
        return ""
    if response.get("output_text"):
        return str(response["output_text"])
    parts = []
    for item in response.get("output") or []:
        for content in item.get("content") or [] if isinstance(item, dict) else []:
            if isinstance(content, dict) and content.get("text"):
                parts.append(str(content["text"]))
    return "".join(parts)


def _function_calls(response):
    return [
        item for item in (response or {}).get("output") or []
        if isinstance(item, dict) and item.get("type") in {"function_call", "tool_call"}
    ] if isinstance(response, dict) else []


def _result(status, reason, evidence=None):
    return {"status": status, "reason": reason, "evidence": copy.deepcopy(evidence or {})}


def _evaluate(provider, model, observations, *, evidence_source, correlated, attestation=None):
    """Grade captured transcripts. Missing behavioral proof remains unknown, never PASS."""
    provider = str(provider or "unknown")
    model = str(model or "")
    observations = observations if isinstance(observations, dict) else {}
    results = {}

    structured = observations.get("structured_output") or {}
    try:
        parsed = json.loads(_output_text(structured.get("response")))
    except (TypeError, ValueError):
        parsed = None
    results["structured_output"] = (
        _result("pass", "strict fixture decoded") if parsed == {"status": "ok"}
        else _result("fail" if structured else "blocked", "strict JSON Schema was not observed")
    )

    tool_obs = observations.get("tool_choice") or {}
    calls = _function_calls(tool_obs.get("response"))
    tool_ok = len(calls) == 1 and calls[0].get("name") == "inspect_project"
    if tool_ok:
        try:
            args = calls[0].get("arguments")
            args = json.loads(args) if isinstance(args, str) else args
            tool_ok = args == {"project_ref": "project_fixture"}
        except (TypeError, ValueError):
            tool_ok = False
    results["tool_choice"] = _result(
        "pass" if tool_ok else ("fail" if tool_obs else "blocked"),
        "forced tool and strict arguments observed" if tool_ok else "forced tool behavior missing",
    )

    stream = observations.get("stream") or {}
    event_types = [
        str(item.get("type") or "") for item in stream.get("events") or []
        if isinstance(item, dict)
    ]
    stream_ok = any(value.endswith(".delta") for value in event_types) and any(
        value in {"response.completed", "done"} for value in event_types
    )
    results["stream"] = _result(
        "pass" if stream_ok else ("fail" if stream else "blocked"),
        "delta and terminal event observed" if stream_ok else "complete SSE evidence missing",
        {"event_types": event_types[:20]},
    )

    continuation = observations.get("continuation") or {}
    continued = "IP12-CONTINUITY-731" in _output_text(continuation.get("response"))
    results["continuation"] = _result(
        "pass" if continued else ("fail" if continuation else "blocked"),
        "state marker continued" if continued else "previous response continuity not proven",
    )

    reasoning = observations.get("reasoning") or {}
    effective = reasoning.get("effective") if isinstance(reasoning.get("effective"), dict) else {}
    documented = bool(reasoning.get("official_contract")) and provider == "openai_official"
    reasoning_ok = effective.get("effort") == "low" or documented
    results["reasoning"] = _result(
        "pass" if reasoning_ok else ("unknown" if reasoning else "blocked"),
        "effective reasoning level proven" if reasoning_ok else "HTTP success does not prove reasoning effort",
    )

    store = observations.get("store_false") or {}
    retrieval_status = store.get("retrieval_status")
    store_ok = retrieval_status in {404, 410}
    results["store_false"] = _result(
        "pass" if store_ok else ("fail" if retrieval_status == 200 else ("unknown" if store else "blocked")),
        "response was not retrievable" if store_ok else "store=false effectiveness not proven",
    )

    usage = observations.get("usage") or {}
    usage_body = (usage.get("response") or {}).get("usage") if isinstance(usage.get("response"), dict) else None
    usage_ok = isinstance(usage_body, dict) and any(
        key in usage_body for key in ("input_tokens", "output_tokens", "total_tokens")
    )
    results["usage"] = _result(
        "pass" if usage_ok else ("fail" if usage else "blocked"),
        "usage object observed" if usage_ok else "usage evidence missing",
    )

    identity = observations.get("model_identity") or {}
    actual_model = str((identity.get("response") or {}).get("model") or "")
    identity_ok = actual_model == model
    results["model_identity"] = _result(
        "pass" if identity_ok else ("fail" if identity else "blocked"),
        "model identity matched" if identity_ok else "requested and returned model differ",
        {"requested": model, "returned": actual_model},
    )

    error_obs = observations.get("error_contract") or {}
    status_code = int(error_obs.get("status_code") or 0)
    error_ok = 400 <= status_code < 500 and isinstance(error_obs.get("response"), dict)
    results["error_contract"] = _result(
        "pass" if error_ok else ("fail" if error_obs else "blocked"),
        "invalid request failed closed" if error_ok else "invalid request did not produce a structured 4xx",
    )

    timeout_obs = observations.get("timeout_cancel") or {}
    timeout_ok = str(timeout_obs.get("terminal") or "") in {"timeout", "cancelled"}
    results["timeout_cancel"] = _result(
        "pass" if timeout_ok else ("unknown" if timeout_obs else "blocked"),
        "timeout/cancel terminal observed" if timeout_ok else "timeout and cancellation not proven",
    )

    critical_statuses = {name: results[name]["status"] for name in CRITICAL_PROBES}
    offline_passed = all(value == "pass" for value in critical_statuses.values())
    live_attested = attestation is _LIVE_ATTESTATION
    passed = (
        offline_passed and evidence_source == "live_capture"
        and correlated is True and live_attested
    )
    return {
        "schema": SCHEMA, "provider": provider, "model": model,
        "evidence_source": evidence_source,
        "evidence_correlated": bool(correlated and live_attested),
        "offline_passed": offline_passed, "passed": passed,
        "decision": "PASS" if passed else ("OFFLINE_PASS" if offline_passed else "HOLD"),
        "critical": critical_statuses, "results": results,
    }


def evaluate(provider, model, observations):
    """Grade fixture or imported observations; direct calls can never claim live PASS."""
    return _evaluate(
        provider, model, observations,
        evidence_source="fixture", correlated=False, attestation=None,
    )


def run_suite(provider, model, transport):
    """Execute the canonical wire requests through an injected, auditable transport."""
    if not callable(transport):
        raise ValueError("provider transport is required")
    requests = build_requests(model)
    observations, correlated = {}, True

    def send(name, request):
        nonlocal correlated
        request.setdefault("metadata", {})["probe_id"] = name
        fingerprint = hashlib.sha256(json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        observation = transport(name, copy.deepcopy(request))
        observation = observation if isinstance(observation, dict) else {}
        if getattr(transport, "evidence_source", "fixture") == "live_capture":
            if name != "timeout_cancel":
                correlated = correlated and bool(observation.get("provider_request_id"))
            correlated = correlated and observation.get("request_fingerprint") == fingerprint
            correlated = correlated and bool(observation.get("captured_at"))
        return observation

    for name in (
        "structured_output", "tool_choice", "stream", "reasoning", "store_false",
        "usage", "model_identity", "error_contract",
    ):
        observations[name] = send(name, copy.deepcopy(requests[name]))
    first = send("continuation_first", copy.deepcopy(requests["continuation_first"]))
    first_output = copy.deepcopy((first.get("response") or {}).get("output") or [])
    second_request = copy.deepcopy(requests["continuation_second"])
    second_request["input"] = first_output + [{
        "role": "user", "content": "Return the marker from the previous assistant output.",
    }]
    observations["continuation"] = send("continuation_second", second_request)
    observations["timeout_cancel"] = send("timeout_cancel", {
        "model": model, "input": "Hold until the client cancels.", "store": False,
        "max_output_tokens": 512,
        "metadata": {"suite": "ip12-provider-compat-v1"},
    })
    source = str(getattr(transport, "evidence_source", "fixture") or "fixture")
    return _evaluate(
        provider, model, observations, evidence_source=source,
        correlated=correlated if source == "live_capture" else False,
        attestation=_LIVE_ATTESTATION if source == "live_capture" and correlated else None,
    )
