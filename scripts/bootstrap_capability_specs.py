#!/usr/bin/env python3
"""One-time legacy bootstrap for CapabilitySpec v1; not a build/check input."""

import argparse
import copy
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools" / "hq-cli" / "src"))

import hq_cli  # noqa: E402
from hq_cli.catalog import bootstrap_capability_list  # noqa: E402
import hq_cli_api  # noqa: E402


OUTPUT = ROOT / "server" / "capability_specs.v1.json"
REGISTRY_VERSION = "capability-registry-v1-20260825"


def _tool_spec(capability, action=None):
    side_effect = capability["side_effect"]
    paid = side_effect == "paid"
    confirmation = bool(capability["confirmation_required"])
    return {
        "schema": "huangque.capability-spec/v1",
        "id": capability["id"],
        "version": "1.0.0",
        "kind": "tool",
        "lifecycle": "stable" if capability["availability"] == "available" else "experimental",
        "name": capability["name"],
        "description": capability["description"],
        "family": (action or {}).get("family") or "",
        # Agent execution follows the server action contract; the public CLI
        # discovery schema stays preserved in projection.hq_cli below.
        "input_schema": copy.deepcopy((action or {}).get("input_schema") or capability["input_schema"]),
        "output_schema": copy.deepcopy(capability["output_schema"]),
        "execution": {
            "binding": "hq_cli",
            "capability_id": capability["id"],
            "api_action": capability.get("api_action"),
            "transport": copy.deepcopy((action or {}).get("transport") or {"kind": capability["kind"]}),
        },
        "permission": {
            "requires_auth": bool(capability["requires_auth"]),
            "scope": capability.get("required_scope"),
            "target_auth": capability.get("target_auth"),
        },
        "side_effect": side_effect,
        "billing": {
            "kind": capability["cost"].get("kind", "none"),
            "quote_required": paid,
            "unit": capability["cost"].get("unit"),
        },
        "confirmation": {
            "required": confirmation,
            "authority": "runtime_quote_card" if paid else ("explicit_user" if confirmation else "none"),
        },
        "idempotency": {
            "required": paid,
            "key_contract": "request_id+input_digest+quote_token" if paid else "none",
        },
        "async": {
            "enabled": paid,
            "poll_tool_ref": "task" if paid else None,
            "terminal_states": ["done", "error", "refunded"] if paid else [],
        },
        "failure": {
            "contract": "production_job_contract" if paid else "synchronous_error",
            "refund_policy": "production_refund_state" if paid else "not_applicable",
        },
        "availability": {"status": capability["availability"]},
        "agent_exposure": "model_read_only" if side_effect == "read" else "runtime_only",
        "projection": {
            "hq_cli": copy.deepcopy(capability),
            **({"action_catalog": copy.deepcopy(action)} if action else {}),
        },
    }


def _internal_tool_spec(action):
    paid = action["billing"] == "quote_then_confirm"
    side_effect = "paid" if paid else ("write" if action["external_effect"] else "read")
    return {
        "schema": "huangque.capability-spec/v1",
        "id": action["action"],
        "version": "1.0.0",
        "kind": "tool",
        "lifecycle": "stable",
        "name": action["purpose"],
        "description": action["purpose"],
        "family": action.get("family") or "",
        "input_schema": copy.deepcopy(action["input_schema"]),
        "output_schema": {"type": "object", "additionalProperties": True},
        "execution": {
            "binding": "internal_action",
            "capability_id": action["action"],
            "api_action": action["action"],
            "transport": copy.deepcopy(action["transport"]),
        },
        "permission": {"requires_auth": True, "scope": None, "target_auth": "first_party"},
        "side_effect": side_effect,
        "billing": {"kind": "server_quote" if paid else "none", "quote_required": paid, "unit": "points" if paid else None},
        "confirmation": {"required": bool(action["confirmation_required"]), "authority": "runtime_quote_card" if paid else ("explicit_user" if action["confirmation_required"] else "none")},
        "idempotency": {"required": paid, "key_contract": "production_id+input_digest" if paid else "none"},
        "async": {"enabled": paid, "poll_tool_ref": "task" if paid else None, "terminal_states": ["done", "error", "refunded"] if paid else []},
        "failure": {"contract": "production_job_contract" if paid else "synchronous_error", "refund_policy": "production_refund_state" if paid else "not_applicable"},
        "availability": copy.deepcopy(action["availability"]),
        "agent_exposure": "model_read_only" if side_effect == "read" else "runtime_only",
        "projection": {"action_catalog": copy.deepcopy(action)},
    }


def _matrix_outcome():
    return {
        "schema": "huangque.capability-spec/v1",
        "id": "matrix-video.text-media-text",
        "version": "1.0.0",
        "kind": "outcome",
        "lifecycle": "experimental",
        "business_result": "使用顶部标题、中央已授权素材和底部 CTA 生成可发布竖屏模板视频",
        "intents": ["模板成片", "上文字中素材下文字", "批量矩阵视频"],
        "availability": {
            "status": "blocked",
            "reason": "matrix_video_main_site_api_missing",
        },
        "harness": {
            "role": "matrix_video_text_media_text_agent",
            "skill_ref": "skill://script-to-matrix-video#text-media-text",
            "context_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["top_text", "bottom_text", "media_asset_ids"],
                "properties": {
                    "top_text": {"type": "string", "minLength": 1},
                    "bottom_text": {"type": "string", "minLength": 1},
                    "media_asset_ids": {"type": "array", "minItems": 2, "items": {"type": "integer", "minimum": 1}},
                    "template_id": {"type": "string"},
                    "bgm": {"type": "boolean"},
                },
            },
            "allowed_tool_refs": [
                "account", "channels", "assets", "pricing", "image-upload", "video-upload", "task"
            ],
            "policies": [
                "master_discovers_outcome_only",
                "specialist_receives_resolved_tools_only",
                "no_ai_generated_media",
                "runtime_owns_quote_confirmation_and_submit",
                "model_never_receives_confirm_or_shell",
            ],
            "success_criteria": [
                "1080x1920 H.264/AAC MP4 is playable",
                "top title and bottom CTA remain visible",
                "all media has supplied or approved-library provenance",
                "artifact is linked to the same Project and AgentRun",
            ],
        },
        "state_contract": {
            "states": ["planning", "needs_input", "blocked", "quote_ready", "awaiting_confirmation", "running", "verifying", "completed", "failed"],
            "initial": "planning",
            "terminal": ["completed", "failed"],
        },
        "success_contract": {
            "artifact_kind": "video",
            "required": ["asset_id", "duration", "codec", "width", "height", "material_manifest"],
            "media": {"codec": "h264", "audio_codec": "aac", "width": 1080, "height": 1920, "min_duration": 8},
        },
        "failure_contract": {
            "codes": ["material_missing", "validation_failed", "render_failed", "capability_unavailable"],
            "paid_retry": "never_automatic",
            "refund_state": "required_after_debit",
        },
    }


def build_registry():
    cli = {item["id"]: item for item in bootstrap_capability_list()}
    actions = {item["action"]: item for item in hq_cli_api.bootstrap_action_catalog()}
    specs = [_tool_spec(cli[tool_id], actions.get(tool_id)) for tool_id in sorted(cli)]
    specs.extend(_internal_tool_spec(actions[action_id]) for action_id in sorted(set(actions) - set(cli)))
    specs.append(_matrix_outcome())
    return {
        "schema": "huangque.capability-registry/v1",
        "registry_version": REGISTRY_VERSION,
        "source_contracts": {
            "hq_cli_version": hq_cli.__version__,
            "hq_cli_schema": "hq.capabilities/v1",
            "public_tool_count": len(cli),
            "action_catalog_version": hq_cli_api.ACTION_CATALOG_VERSION,
            "action_tool_count": len(actions),
        },
        "manual_edits": "canonical source; normal build uses compile_capability_specs.py",
        "capabilities": sorted(specs, key=lambda item: (item["kind"], item["id"])),
    }


def serialized():
    return json.dumps(
        build_registry(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if OUTPUT.exists() and not args.force:
        raise SystemExit("canonical CapabilitySpec already exists; pass --force only for one-time bootstrap")
    OUTPUT.write_text(serialized(), encoding="utf-8")


if __name__ == "__main__":
    main()
