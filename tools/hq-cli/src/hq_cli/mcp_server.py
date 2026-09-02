"""Dependency-free stdio MCP adapter for the fixed Huangque CLI catalog."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys

from . import __version__
from .catalog import CAPABILITIES, ENVIRONMENTS


PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")
SUPPORTED_PROTOCOL_VERSIONS = (PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS)
PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"
CONTROL_TOOLS = {
    "hq_cli_help": {
        "description": "Show the fixed Huangque CLI command catalog.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "command": lambda arguments: ["help"],
        "read_only": True,
    },
    "hq_cli_version": {
        "description": "Read the installed Huangque CLI version.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "command": lambda arguments: ["version"],
        "read_only": True,
    },
    "hq_cli_capabilities": {
        "description": "Read the complete machine-readable Huangque capability catalog.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "command": lambda arguments: ["capabilities"],
        "read_only": True,
    },
    "hq_cli_channels": {
        "description": "Read the current account's available Huangque provider channels.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "command": lambda arguments: ["channels"],
        "read_only": True,
    },
    "hq_cli_describe": {
        "description": "Read one Huangque capability's exact input and side-effect contract.",
        "schema": {
            "type": "object",
            "properties": {"id": {"type": "string", "enum": sorted(CAPABILITIES)}},
            "required": ["id"],
            "additionalProperties": False,
        },
        "command": lambda arguments: ["describe", arguments["id"]],
        "read_only": True,
    },
    "hq_cli_doctor": {
        "description": "Check the fixed Huangque service endpoints without changing account data.",
        "schema": {
            "type": "object",
            "properties": {"environment": {"type": "string", "enum": sorted(ENVIRONMENTS)}},
            "additionalProperties": False,
        },
        "command": lambda arguments: ["doctor", "--environment", arguments.get("environment", "main")],
        "read_only": True,
    },
    "hq_cli_status": {
        "description": "Read the currently authorized Huangque account and scopes.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "command": lambda arguments: ["status"],
        "read_only": True,
    },
    "hq_cli_login": {
        "description": "Start Huangque device authorization. This may open a browser and waits for the user to approve.",
        "schema": {
            "type": "object",
            "properties": {"no_browser": {"type": "boolean", "default": False}},
            "additionalProperties": False,
        },
        "command": lambda arguments: ["login"] + (["--no-browser"] if arguments.get("no_browser") else []),
        "read_only": False,
    },
    "hq_cli_logout": {
        "description": "Revoke and remove the current Huangque CLI authorization. Requires explicit confirmation.",
        "schema": {
            "type": "object",
            "properties": {"confirm": {"type": "boolean", "const": True}},
            "required": ["confirm"],
            "additionalProperties": False,
        },
        "command": lambda arguments: ["logout"],
        "read_only": False,
        "destructive": True,
    },
}


def capability_tool_name(capability_id):
    return "hq_" + capability_id.replace("-", "_")


TOOL_TO_CAPABILITY = {capability_tool_name(identifier): identifier for identifier in CAPABILITIES}


def _capability_schema(capability):
    schema = copy.deepcopy(capability["input_schema"])
    properties = schema.setdefault("properties", {})
    required = list(schema.get("required") or [])
    if capability["kind"] == "upload":
        properties["file"] = {
            "type": "string",
            "description": "One explicit absolute local file path accepted by this upload capability.",
        }
        if capability["side_effect"] == "paid":
            properties["confirm"] = {
                "type": "boolean", "default": False,
                "description": "Leave false to obtain a file-bound quote; set true only after explicit user approval.",
            }
            properties["quote_token"] = {
                "type": "string", "minLength": 1,
                "description": "Server quote token returned for this exact file.",
            }
            properties["expected_cost"] = {
                "type": "integer", "minimum": 0,
                "description": "Quoted point cost explicitly approved by the user.",
            }
            required.append("file")
        else:
            properties["confirm"] = {"type": "boolean", "const": True}
            required.extend(["file", "confirm"])
        for flag, definition in (
                capability.get("file_input", {}).get("requiredMetadata") or {}).items():
            name = flag.lstrip("-").replace("-", "_")
            properties[name] = {
                "type": "string", "pattern": definition,
                "description": "Required upload metadata forwarded as %s." % flag,
            }
            required.append(name)
    elif capability["kind"] == "navigation":
        properties["open_browser"] = {
            "type": "boolean",
            "default": False,
            "description": "Open the returned fixed Huangque URL in the user's browser.",
        }
    elif capability["confirmation_required"]:
        if capability["side_effect"] == "paid":
            properties["confirm"] = {
                "type": "boolean",
                "default": False,
                "description": "Leave false to obtain a quote; set true only after explicit user approval.",
            }
            properties["quote_token"] = {
                "type": "string",
                "minLength": 1,
                "description": "Server quote token from an identical unconfirmed call.",
            }
        else:
            properties["confirm"] = {"type": "boolean", "const": True}
            required.append("confirm")
    schema["required"] = list(dict.fromkeys(required))
    schema["additionalProperties"] = False
    return schema


def list_tools():
    tools = []
    for name, definition in CONTROL_TOOLS.items():
        annotations = {
            "readOnlyHint": definition["read_only"],
            "destructiveHint": bool(definition.get("destructive")),
            "idempotentHint": definition["read_only"],
            "openWorldHint": name not in {"hq_cli_help", "hq_cli_version", "hq_cli_capabilities", "hq_cli_describe"},
        }
        tools.append({
            "name": name,
            "description": definition["description"],
            "inputSchema": definition["schema"],
            "annotations": annotations,
        })
    for capability in CAPABILITIES.values():
        side_effect = capability["side_effect"]
        destructive = capability.get("agent", {}).get("operation") == "delete"
        confirmation = " Explicit confirmation is required." if capability["confirmation_required"] else ""
        tools.append({
            "name": capability_tool_name(capability["id"]),
            "description": "%s Side effect: %s.%s" % (capability["description"], side_effect, confirmation),
            "inputSchema": _capability_schema(capability),
            "outputSchema": capability["output_schema"],
            "annotations": {
                "readOnlyHint": side_effect in {"read", "navigation"},
                "destructiveHint": destructive,
                "idempotentHint": side_effect in {"read", "navigation"},
                "openWorldHint": True,
            },
        })
    return tools


def _run_hq(arguments, stdin_text=""):
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "hq_cli", *arguments, "--json"],
            input=stdin_text,
            text=True,
            capture_output=True,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 8, {"schema": "hq.error/v1", "error": "mcp_execution_error", "message": str(exc)}
    raw = completed.stdout if completed.returncode == 0 else completed.stderr
    lines = [line for line in raw.splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1])
    except (IndexError, ValueError, json.JSONDecodeError):
        payload = {
            "schema": "hq.error/v1",
            "error": "mcp_execution_error",
            "message": "HQ CLI returned invalid JSON",
        }
        return completed.returncode or 10, payload
    return completed.returncode, payload


def _validate_control_arguments(name, arguments):
    schema = CONTROL_TOOLS[name]["schema"]
    properties = schema["properties"]
    unknown = set(arguments) - set(properties)
    if unknown:
        raise ValueError("unknown tool argument: %s" % sorted(unknown)[0])
    for required in schema.get("required", []):
        if required not in arguments:
            raise ValueError("missing required tool argument: %s" % required)
    if name == "hq_cli_logout" and arguments.get("confirm") is not True:
        raise ValueError("hq_cli_logout requires confirm=true")
    if name == "hq_cli_describe" and arguments.get("id") not in CAPABILITIES:
        raise ValueError("unknown Huangque capability")
    if name == "hq_cli_doctor" and arguments.get("environment", "main") not in ENVIRONMENTS:
        raise ValueError("unknown Huangque environment")
    if "no_browser" in arguments and not isinstance(arguments["no_browser"], bool):
        raise ValueError("no_browser must be a boolean")


def _capability_command(capability, arguments):
    values = dict(arguments)
    confirm = values.pop("confirm", False)
    quote_token = values.pop("quote_token", None)
    expected_cost = values.pop("expected_cost", None)
    file_path = values.pop("file", None)
    open_browser = values.pop("open_browser", False)
    if not isinstance(confirm, bool):
        raise ValueError("confirm must be a boolean")
    if not isinstance(open_browser, bool):
        raise ValueError("open_browser must be a boolean")
    if capability["confirmation_required"] and capability["side_effect"] != "paid" and confirm is not True:
        raise ValueError("this Huangque capability requires confirm=true")
    command = ["run", capability["id"]]
    stdin_text = ""
    if capability["kind"] == "upload":
        metadata = []
        for flag, pattern in (
                capability.get("file_input", {}).get("requiredMetadata") or {}).items():
            name = flag.lstrip("-").replace("-", "_")
            value = values.pop(name, None)
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise ValueError("upload tool requires valid %s metadata" % name)
            metadata.extend([flag, value])
        if values:
            raise ValueError("upload tool received unexpected capability input")
        if not isinstance(file_path, str) or not file_path:
            raise ValueError("upload tool requires one file path")
        command.extend(["--file", file_path])
        command.extend(metadata)
    else:
        command.extend(["--input", "@-"])
        stdin_text = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    if open_browser:
        command.append("--open-browser")
    if confirm:
        command.append("--confirm")
    if quote_token is not None:
        if not isinstance(quote_token, str) or not quote_token:
            raise ValueError("quote_token must be a non-empty string")
        command.extend(["--quote-token", quote_token])
    if expected_cost is not None:
        if isinstance(expected_cost, bool) or not isinstance(expected_cost, int) or expected_cost < 0:
            raise ValueError("expected_cost must be a non-negative integer")
        command.extend(["--expected-cost", str(expected_cost)])
    return command, stdin_text


def call_tool(name, arguments, runner=_run_hq):
    if not isinstance(arguments, dict):
        return _tool_result(2, {"error": "invalid_arguments", "message": "tool arguments must be an object"})
    try:
        if name in CONTROL_TOOLS:
            _validate_control_arguments(name, arguments)
            code, payload = runner(CONTROL_TOOLS[name]["command"](arguments), "")
        elif name in TOOL_TO_CAPABILITY:
            capability = CAPABILITIES[TOOL_TO_CAPABILITY[name]]
            command, stdin_text = _capability_command(capability, arguments)
            code, payload = runner(command, stdin_text)
        else:
            return _tool_result(3, {"error": "unknown_tool", "message": "unknown MCP tool: %s" % name})
    except (KeyError, TypeError, ValueError) as exc:
        return _tool_result(2, {"error": "invalid_arguments", "message": str(exc)})
    return _tool_result(code, payload)


def _tool_result(code, payload):
    if not isinstance(payload, dict):
        payload = {"result": payload}
    result = {
        "resultType": "complete",
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, sort_keys=True)}],
    }
    if code:
        result["isError"] = True
    else:
        result["structuredContent"] = payload
    return result


def _handle(request, runner, mode):
    method = request.get("method")
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "initialize":
        requested = (request.get("params") or {}).get("protocolVersion")
        return {
            "protocolVersion": requested,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "huangque", "version": __version__},
            "instructions": (
                "Use one typed Huangque tool per capability. Obtain a quote before paid work and set "
                "confirm only after explicit user approval. Never retry an uncertain create automatically."
            ),
        }
    if method == "server/discover":
        return {
            "resultType": "complete",
            "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": {"tools": {"listChanged": False}},
            "_meta": {
                "io.modelcontextprotocol/serverInfo": {"name": "huangque", "version": __version__},
            },
            "instructions": (
                "Use one typed Huangque tool per capability. Obtain a quote before paid work and set "
                "confirm only after explicit user approval. Never retry an uncertain create automatically."
            ),
            "ttlMs": 300000,
            "cacheScope": "public",
        }
    if method == "ping" and mode == "legacy":
        return {}
    if method == "tools/list":
        return {
            "resultType": "complete", "tools": list_tools(),
            "ttlMs": 300000, "cacheScope": "public",
        }
    if method == "tools/call":
        params = request.get("params") or {}
        return call_tool(params.get("name"), params.get("arguments") or {}, runner=runner)
    raise KeyError("method not found")


def _rpc_error(code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return error


def _request_mode(request, connection_mode, legacy_protocol):
    method = request.get("method")
    if method == "initialize":
        params = request.get("params") or {}
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        if requested not in LEGACY_PROTOCOL_VERSIONS:
            raise ValueError((
                -32022, "Unsupported protocol version",
                {"requested": str(requested or ""), "supported": list(SUPPORTED_PROTOCOL_VERSIONS)},
            ))
        if connection_mode == "modern":
            raise ValueError((
                -32022, "Connection already uses the modern protocol era",
                {"requested": requested, "supported": [PROTOCOL_VERSION]},
            ))
        return "legacy", "legacy", requested

    params = request.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if meta is None and connection_mode == "legacy" and legacy_protocol in LEGACY_PROTOCOL_VERSIONS:
        return "legacy", connection_mode, legacy_protocol
    if not isinstance(meta, dict):
        raise ValueError((-32602, "Invalid params: required _meta is missing", None))
    requested = meta.get(PROTOCOL_META)
    if requested != PROTOCOL_VERSION:
        raise ValueError((
            -32022, "Unsupported protocol version",
            {"requested": str(requested or ""), "supported": list(SUPPORTED_PROTOCOL_VERSIONS)},
        ))
    if not isinstance(meta.get(CLIENT_CAPABILITIES_META), dict):
        raise ValueError((-32602, "Invalid params: clientCapabilities is required", None))
    if connection_mode == "legacy":
        raise ValueError((
            -32022, "Connection already uses the legacy protocol era",
            {"requested": requested, "supported": [legacy_protocol]},
        ))
    return "modern", "modern", legacy_protocol


def _attach_server_info(result):
    if not isinstance(result, dict):
        return result
    meta = result.setdefault("_meta", {})
    meta.setdefault(SERVER_INFO_META, {"name": "huangque", "version": __version__})
    return result


def serve(input_stream=None, output_stream=None, runner=_run_hq):
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    connection_mode = None
    legacy_protocol = None
    for line in input_stream:
        request_id = None
        parsed_request = False
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError((-32600, "Invalid Request", None))
            parsed_request = True
            request_id = request.get("id")
            if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                raise ValueError((-32600, "Invalid Request", None))
            mode, connection_mode, legacy_protocol = _request_mode(
                request, connection_mode, legacy_protocol,
            )
            result = _handle(request, runner, mode)
            if request_id is None:
                continue
            if mode == "modern":
                result = _attach_server_info(result)
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except json.JSONDecodeError as exc:
            response = {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error: %s" % exc},
            }
        except ValueError as exc:
            if request_id is None and parsed_request:
                continue
            if exc.args and isinstance(exc.args[0], tuple):
                code, message, data = exc.args[0]
            else:
                code, message, data = -32602, str(exc), None
            response = {
                "jsonrpc": "2.0", "id": request_id,
                "error": _rpc_error(code, message, data),
            }
        except (KeyError, TypeError) as exc:
            if request_id is None:
                continue
            response = {
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601 if isinstance(exc, KeyError) else -32602, "message": str(exc)},
            }
        output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        output_stream.flush()
    return 0
