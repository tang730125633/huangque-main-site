"""CapabilitySpec v1 registry and deterministic Agent tool resolver."""

import copy
import hashlib
import json
from pathlib import Path


REGISTRY_SCHEMA = "huangque.capability-registry/v1"
SPEC_SCHEMA = "huangque.capability-spec/v1"
REGISTRY_PATH = Path(__file__).with_name("capability_specs.v1.json")
LIFECYCLES = ("draft", "experimental", "stable", "deprecated", "retired")
LIFECYCLE_TRANSITIONS = {
    "draft": {"experimental", "retired"},
    "experimental": {"stable", "deprecated", "retired"},
    "stable": {"deprecated"},
    "deprecated": {"retired"},
    "retired": set(),
}


class CapabilitySpecError(ValueError):
    pass


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lifecycle_transition_allowed(current, target):
    return target == current or target in LIFECYCLE_TRANSITIONS.get(current, set())


def _require_object(value, label):
    if not isinstance(value, dict):
        raise CapabilitySpecError(label + " must be an object")
    return value


def validate_registry(value):
    registry = _require_object(value, "registry")
    if registry.get("schema") != REGISTRY_SCHEMA or not registry.get("registry_version"):
        raise CapabilitySpecError("invalid registry identity")
    specs = registry.get("capabilities")
    if not isinstance(specs, list) or not specs:
        raise CapabilitySpecError("registry capabilities must be a non-empty list")
    ids = [str(item.get("id") or "") for item in specs if isinstance(item, dict)]
    if len(ids) != len(specs) or not all(ids) or len(ids) != len(set(ids)):
        raise CapabilitySpecError("capability ids must be non-empty and unique")
    by_id = {item["id"]: item for item in specs}
    for spec in specs:
        if spec.get("schema") != SPEC_SCHEMA or spec.get("kind") not in {"tool", "outcome"}:
            raise CapabilitySpecError("invalid capability spec: " + spec.get("id", ""))
        if spec.get("lifecycle") not in LIFECYCLES or not spec.get("version"):
            raise CapabilitySpecError("invalid lifecycle or version: " + spec["id"])
        if spec["kind"] == "tool":
            for field in (
                "input_schema", "output_schema", "execution", "permission",
                "side_effect", "billing", "confirmation", "idempotency",
                "async", "failure", "availability",
            ):
                if field not in spec:
                    raise CapabilitySpecError("tool missing %s: %s" % (field, spec["id"]))
            paid = spec["side_effect"] == "paid" or spec["billing"].get("quote_required") is True
            if paid and not (
                spec["billing"].get("quote_required") is True
                and spec["confirmation"].get("required") is True
                and spec["confirmation"].get("authority") == "runtime_quote_card"
            ):
                raise CapabilitySpecError("paid tool must require quote-card confirmation: " + spec["id"])
            if spec["async"].get("enabled"):
                poll_ref = spec["async"].get("poll_tool_ref")
                if not poll_ref:
                    raise CapabilitySpecError("async tool missing poll tool: " + spec["id"])
                if poll_ref not in by_id or by_id[poll_ref].get("kind") != "tool":
                    raise CapabilitySpecError("async tool has dangling poll tool: " + spec["id"])
        else:
            harness = _require_object(spec.get("harness"), "outcome harness")
            refs = harness.get("allowed_tool_refs")
            if not isinstance(refs, list) or not 3 <= len(refs) <= 8 or len(refs) != len(set(refs)):
                raise CapabilitySpecError("outcome must allow 3-8 unique tools: " + spec["id"])
            dangling = [ref for ref in refs if ref not in by_id or by_id[ref].get("kind") != "tool"]
            if dangling:
                raise CapabilitySpecError("outcome has dangling tool ref: " + dangling[0])
            if not spec.get("success_contract"):
                raise CapabilitySpecError("outcome missing success contract: " + spec["id"])
            if not spec.get("failure_contract"):
                raise CapabilitySpecError("outcome missing failure contract: " + spec["id"])
    return registry


def load_registry(path=None):
    registry_path = Path(path or REGISTRY_PATH)
    with registry_path.open(encoding="utf-8") as handle:
        return validate_registry(json.load(handle))


def _index(registry=None):
    value = registry or load_registry()
    return value, {item["id"]: item for item in value["capabilities"]}


def compiled_projection(target, registry=None):
    """Return one exact compatibility projection; generated files stay read-only."""
    _, by_id = _index(registry)
    values = []
    for spec in by_id.values():
        projection = (spec.get("projection") or {}).get(target)
        if projection is not None:
            values.append(copy.deepcopy(projection))
    order_key = "id" if target == "hq_cli" else "action"
    return sorted(values, key=lambda item: item[order_key])


def discover_outcomes(registry=None):
    """Master sees business outcomes only, never specialist tool references."""
    value, _ = _index(registry)
    return [
        {
            "id": spec["id"],
            "version": spec["version"],
            "lifecycle": spec["lifecycle"],
            "business_result": spec["business_result"],
            "intents": copy.deepcopy(spec.get("intents") or []),
            "availability": copy.deepcopy(spec.get("availability") or {}),
        }
        for spec in value["capabilities"] if spec["kind"] == "outcome"
    ]


def _context_issues(schema, context):
    context = context if isinstance(context, dict) else {}
    missing = []
    invalid = []
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        invalid.extend("unknown:" + field for field in sorted(set(context) - set(properties)))
    for field in schema.get("required") or []:
        value = context.get(field)
        if value in (None, "", []):
            missing.append(field)
    for field, value in context.items():
        definition = properties.get(field)
        if not definition or field in missing:
            continue
        kind = definition.get("type")
        if kind == "string" and (
            not isinstance(value, str) or len(value) < int(definition.get("minLength") or 0)
            or len(value) > int(definition.get("maxLength") or len(value))
            or definition.get("enum") and value not in definition["enum"]
        ):
            invalid.append(field)
        elif kind == "boolean" and not isinstance(value, bool):
            invalid.append(field)
        elif kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            invalid.append(field)
        elif kind == "array":
            item = definition.get("items") or {}
            item_type = item.get("type")
            if (not isinstance(value, list)
                    or len(value) < int(definition.get("minItems") or 0)
                    or len(value) > int(definition.get("maxItems") or len(value))):
                invalid.append(field)
            elif item_type == "integer" and any(
                isinstance(entry, bool) or not isinstance(entry, int)
                or entry < int(item.get("minimum") or entry)
                for entry in value
            ):
                invalid.append(field)
    return missing, invalid


def resolve_outcome(outcome_id, *, account_tool_ids, context=None, gate_status="unlocked", registry=None):
    """Resolve one Outcome into a frozen specialist binding without executing tools."""
    value, by_id = _index(registry)
    outcome = by_id.get(str(outcome_id or ""))
    if not outcome or outcome.get("kind") != "outcome":
        raise CapabilitySpecError("unknown outcome: " + str(outcome_id or ""))
    allowed = list(outcome["harness"]["allowed_tool_refs"])
    account_tools = {str(item) for item in account_tool_ids or []}
    available = [tool_id for tool_id in allowed if tool_id in account_tools]
    unavailable = [tool_id for tool_id in allowed if tool_id not in account_tools]
    model_tools = [
        tool_id for tool_id in available
        if by_id[tool_id]["side_effect"] == "read"
        and by_id[tool_id].get("agent_exposure") == "model_read_only"
    ]
    missing, invalid = _context_issues(outcome["harness"]["context_schema"], context)
    blockers = []
    if gate_status != "unlocked":
        blockers.append("capability_gate_locked")
    blockers.extend("tool_unavailable:" + tool_id for tool_id in unavailable)
    availability = outcome.get("availability") or {}
    if not missing and not invalid and availability.get("status") != "available":
        blockers.append(str(availability.get("reason") or "outcome_unavailable"))
    status = "needs_input" if missing or invalid else ("blocked" if blockers else "ready")
    binding = {
        "schema": "huangque.outcome-binding/v1",
        "registry_version": value["registry_version"],
        "outcome_id": outcome["id"],
        "outcome_version": outcome["version"],
        "specialist_id": outcome["harness"]["role"],
        "skill_ref": outcome["harness"]["skill_ref"],
        "runtime_tool_ids": available,
        "model_tool_ids": model_tools,
        "capability_versions": {tool_id: by_id[tool_id]["version"] for tool_id in available},
        "status": status,
        "missing": missing,
        "invalid": invalid,
        "blockers": blockers,
        "success_contract": copy.deepcopy(outcome["success_contract"]),
        "failure_contract": copy.deepcopy(outcome["failure_contract"]),
    }
    binding["binding_digest"] = _digest(binding)
    return binding
