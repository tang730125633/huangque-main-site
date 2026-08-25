#!/usr/bin/env python3
"""Compile canonical CapabilitySpec into compatibility projections."""

import argparse
import copy
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import capability_specs  # noqa: E402


ACTION_PATH = Path("server/action_catalog.generated.json")
CLI_PATH = Path("tools/hq-cli/src/hq_cli/capabilities.generated.json")


def _tools(registry):
    return {
        item["id"]: item for item in registry["capabilities"]
        if item["kind"] == "tool"
    }


def compile_registry(registry):
    capability_specs.validate_registry(registry)
    tools = _tools(registry)
    action_items = []
    cli_items = []
    for tool_id in sorted(tools):
        spec = tools[tool_id]
        projections = spec.get("projection") or {}
        action = projections.get("action_catalog")
        if action is not None:
            item = copy.deepcopy(action)
            item["input_schema"] = copy.deepcopy(spec["input_schema"])
            item["confirmation_required"] = bool(spec["confirmation"]["required"])
            item["billing"] = "quote_then_confirm" if spec["billing"]["quote_required"] else "free"
            item["external_effect"] = spec["side_effect"] not in {"read", "navigation"}
            item["risk"] = (
                "production" if spec["side_effect"] == "paid"
                else "write" if item["external_effect"] else "read"
            )
            action_items.append(item)
        cli = projections.get("hq_cli")
        if cli is not None:
            item = copy.deepcopy(cli)
            item["confirmation_required"] = bool(spec["confirmation"]["required"])
            item["side_effect"] = spec["side_effect"]
            item["requires_auth"] = bool(spec["permission"]["requires_auth"])
            item["required_scope"] = spec["permission"].get("scope")
            item["target_auth"] = spec["permission"].get("target_auth")
            item["availability"] = spec["availability"]["status"]
            item["cost"]["kind"] = spec["billing"]["kind"]
            if spec["billing"].get("unit") is not None:
                item["cost"]["unit"] = spec["billing"]["unit"]
            cli_items.append(item)
    video = next(item for item in cli_items if item["id"] == "video-generate")
    return {
        ACTION_PATH: {
            "schema": "huangque.action-catalog-projection/v1",
            "registry_version": registry["registry_version"],
            "version": registry["source_contracts"]["action_catalog_version"],
            "actions": action_items,
        },
        CLI_PATH: {
            "schema": registry["source_contracts"]["hq_cli_schema"],
            "registry_version": registry["registry_version"],
            "cli_version": registry["source_contracts"]["hq_cli_version"],
            "video_channel_rules": video["input_schema"]["x-hq-channel-rules"],
            "capabilities": cli_items,
        },
    }


def _serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def write_outputs(registry, output_root=ROOT):
    output_root = Path(output_root)
    for relative, value in compile_registry(registry).items():
        target = output_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_serialized(value), encoding="utf-8")


def check_outputs(registry, output_root=ROOT):
    output_root = Path(output_root)
    stale = []
    for relative, value in compile_registry(registry).items():
        target = output_root / relative
        if not target.is_file() or target.read_text(encoding="utf-8") != _serialized(value):
            stale.append(str(relative))
    return stale


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--source", type=Path, default=capability_specs.REGISTRY_PATH)
    parser.add_argument("--output-root", type=Path, default=ROOT)
    args = parser.parse_args()
    registry = capability_specs.load_registry(args.source)
    if args.check:
        stale = check_outputs(registry, args.output_root)
        if stale:
            raise SystemExit("stale CapabilitySpec projection: " + ", ".join(stale))
        return
    write_outputs(registry, args.output_root)


if __name__ == "__main__":
    main()
