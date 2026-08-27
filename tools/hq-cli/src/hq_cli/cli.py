"""JSON-first CLI for fixed Huangque main-site capabilities."""

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from . import __version__
from . import client
from . import mcp_server
from . import skill_install
from .catalog import (
    CAPABILITIES, ENVIRONMENTS, VIDEO_CHANNEL_RULES,
    capability_list, resolve_url,
)


EXIT_USAGE = 2
EXIT_UNKNOWN_CAPABILITY = 3
EXIT_INPUT = 4
EXIT_DOCTOR = 5
EXIT_UNAVAILABLE = 6
EXIT_BROWSER = 7
EXIT_NETWORK = 8
EXIT_AUTH = 9
EXIT_API = 10
EXIT_CONFIRMATION = 11
EXIT_INSTALL = 12
MAX_INPUT_BYTES = 65536
LOGIN_SCOPES = [
    "profile:read", "ip12:read", "ip12:write", "ip12:chat", "prompt:optimize", "canvas:read",
    "canvas:write", "canvas:agent", "canvas:edit", "tasks:read", "assets:read", "assets:write", "assets:upload",
    "generation:quote", "generation:submit",
    "video-compose:read", "video-compose:write", "digital-presenter:read", "digital-presenter:write",
    "inspiration:read", "inspiration:write", "leads:read", "leads:write", "short-drama:read", "short-drama:write",
]


class CliError(Exception):
    def __init__(self, code, error, message, details=None):
        super().__init__(message)
        self.code, self.error, self.message = int(code), str(error), str(message)
        self.details = details or {}


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        if str(message).startswith("unrecognized arguments:"):
            message = "unrecognized arguments"
        raise CliError(EXIT_USAGE, "usage_error", message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _write(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    stream.flush()


def _envelope(schema, **values):
    return {"schema": schema, "cli_version": __version__, **values}


def _error(error):
    payload = _envelope("hq.error/v1", error=error.error, message=error.message, exit_code=error.code)
    if error.details:
        payload["details"] = error.details
    _write(sys.stderr, payload)
    return error.code


def _reject_non_finite(value):
    raise ValueError("non-finite number is not valid JSON: %s" % value)


def _validate_unicode(value):
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            try:
                current.encode("utf-8")
            except UnicodeEncodeError:
                raise CliError(EXIT_INPUT, "input_error", "input contains invalid Unicode")
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


def _load_json(source):
    if source is None:
        return {}
    if not source.startswith("@"):
        raise CliError(EXIT_USAGE, "usage_error", "--input must be @file or @-")
    if source == "@-":
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        try:
            with open(source[1:], "rb") as handle:
                raw = handle.read(MAX_INPUT_BYTES + 1)
        except OSError as exc:
            raise CliError(EXIT_INPUT, "input_error", "cannot read input: %s" % exc)
    if len(raw) > MAX_INPUT_BYTES:
        raise CliError(EXIT_INPUT, "input_error", "input exceeds 65536 bytes")
    try:
        payload = json.loads(raw.decode("utf-8-sig"), parse_constant=_reject_non_finite)
    except (UnicodeDecodeError, ValueError, RecursionError, json.JSONDecodeError) as exc:
        raise CliError(EXIT_INPUT, "input_error", "input must be finite UTF-8 JSON: %s" % exc)
    _validate_unicode(payload)
    if not isinstance(payload, dict):
        raise CliError(EXIT_INPUT, "input_error", "input must be a JSON object")
    return payload


def _validate_video_channel(payload):
    channel = payload.get("channel", "grok")
    rule = VIDEO_CHANNEL_RULES[channel]
    ratio = payload.get("ratio", rule["default_ratio"])
    if ratio not in rule["ratios"]:
        raise CliError(EXIT_INPUT, "input_error", "video ratio is not supported by channel=%s" % channel)
    references = payload.get("reference_upload_ids")
    if references is not None and not 1 <= len(references) <= rule["reference_max"]:
        raise CliError(
            EXIT_INPUT, "input_error",
            "video reference_upload_ids exceeds channel=%s limit" % channel,
        )
    resolution = payload.get("resolution", rule["default_resolution"])
    if channel == "sora":
        if "duration" in payload or "generate_audio" in payload:
            raise CliError(EXIT_INPUT, "input_error", "sora uses seconds and rejects duration or generate_audio")
        seconds = payload.get("seconds", rule["default_seconds"])
        if seconds not in rule["seconds"]:
            raise CliError(EXIT_INPUT, "input_error", "sora seconds must be 4, 8, or 12")
        model = payload.get("model", rule["default_model"])
        if model not in rule["models"]:
            raise CliError(EXIT_INPUT, "input_error", "unsupported sora model")
        if resolution not in rule["model_resolutions"][model]:
            raise CliError(EXIT_INPUT, "input_error", "video resolution is not supported by sora model")
        return
    if "seconds" in payload:
        raise CliError(EXIT_INPUT, "input_error", "seconds is only supported by channel=sora")
    duration = payload.get("duration", rule["default_duration"])
    if not rule["duration"][0] <= duration <= rule["duration"][1]:
        raise CliError(EXIT_INPUT, "input_error", "video duration is not supported by channel=%s" % channel)
    if resolution not in rule["resolutions"]:
        raise CliError(EXIT_INPUT, "input_error", "video resolution is not supported by channel=%s" % channel)
    if "model" in payload and channel != "grok":
        raise CliError(EXIT_INPUT, "input_error", "model is only supported by grok or sora")
    if "generate_audio" in payload and not rule["generate_audio"]:
        raise CliError(EXIT_INPUT, "input_error", "generate_audio is only supported by channel=micro")
    if channel == "grok":
        model = payload.get("model", rule["default_model"])
        if model not in rule["models"]:
            raise CliError(EXIT_INPUT, "input_error", "unsupported grok model")
        if model in rule["reference_required_models"] and not references:
            raise CliError(EXIT_INPUT, "input_error", "grok video 1.5 requires reference_upload_ids")
        if references and resolution not in rule["reference_resolutions"]:
            raise CliError(EXIT_INPUT, "input_error", "grok reference video resolution must be 720p")


def _validate(capability, payload):
    schema = capability["input_schema"]
    properties = schema["properties"]
    unknown = sorted(set(payload) - set(properties))
    if unknown:
        raise CliError(EXIT_INPUT, "input_error", "unknown input field: %s" % unknown[0])
    for key in schema["required"]:
        if key not in payload:
            raise CliError(EXIT_INPUT, "input_error", "missing required input field: %s" % key)
    for keyword, expected in (("anyOf", "at least one"), ("oneOf", "exactly one")):
        options = schema.get(keyword) or []
        if options:
            matches = sum(all(key in payload for key in option.get("required", [])) for option in options)
            if (keyword == "anyOf" and matches < 1) or (keyword == "oneOf" and matches != 1):
                raise CliError(
                    EXIT_INPUT, "input_error",
                    "input must match %s of the documented field groups" % expected,
                )
    for key, definition in properties.items():
        if key not in payload:
            continue
        value, value_type = payload[key], definition["type"]
        if value_type == "string" and not isinstance(value, str):
            raise CliError(EXIT_INPUT, "input_error", "input field %s must be a string" % key)
        if value_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise CliError(EXIT_INPUT, "input_error", "input field %s must be a finite number" % key)
        if value_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            raise CliError(EXIT_INPUT, "input_error", "input field %s must be an integer" % key)
        if value_type == "boolean" and not isinstance(value, bool):
            raise CliError(EXIT_INPUT, "input_error", "input field %s must be a boolean" % key)
        if value_type == "object":
            if not isinstance(value, dict):
                raise CliError(EXIT_INPUT, "input_error", "input field %s must be an object" % key)
            if len(value) < definition.get("minProperties", 0) or len(value) > definition.get("maxProperties", len(value)):
                raise CliError(EXIT_INPUT, "input_error", "input field %s has an invalid number of properties" % key)
            child = definition.get("additionalProperties") or {}
            if child.get("enum") and any(entry not in child["enum"] for entry in value.values()):
                raise CliError(EXIT_INPUT, "input_error", "input field %s contains an invalid value" % key)
        if value_type == "array":
            if not isinstance(value, list):
                raise CliError(EXIT_INPUT, "input_error", "input field %s must be an array" % key)
            if len(value) < definition.get("minItems", 0):
                raise CliError(EXIT_INPUT, "input_error", "input field %s has too few items" % key)
            if len(value) > definition.get("maxItems", len(value)):
                raise CliError(EXIT_INPUT, "input_error", "input field %s has too many items" % key)
            if definition.get("uniqueItems") and len(value) != len(set(value)):
                raise CliError(EXIT_INPUT, "input_error", "input field %s contains duplicate items" % key)
            item = definition.get("items") or {}
            if item.get("type") == "string" and any(
                    not isinstance(entry, str) or len(entry) < item.get("minLength", 0)
                    or len(entry) > item.get("maxLength", len(entry)) for entry in value):
                raise CliError(EXIT_INPUT, "input_error", "input field %s contains an invalid item" % key)
            if item.get("type") == "integer" and any(
                    isinstance(entry, bool) or not isinstance(entry, int)
                    or entry < item.get("minimum", entry) or entry > item.get("maximum", entry)
                    for entry in value):
                raise CliError(EXIT_INPUT, "input_error", "input field %s contains an invalid item" % key)
            if item.get("enum") and any(entry not in item["enum"] for entry in value):
                raise CliError(EXIT_INPUT, "input_error", "input field %s contains an invalid item" % key)
        if "enum" in definition and value not in definition["enum"]:
            raise CliError(EXIT_INPUT, "input_error", "input field %s must be one of: %s" % (key, ", ".join(map(str, definition["enum"]))))
        if "minLength" in definition and len(value) < definition["minLength"]:
            raise CliError(EXIT_INPUT, "input_error", "input field %s is too short" % key)
        if "maxLength" in definition and len(value) > definition["maxLength"]:
            raise CliError(EXIT_INPUT, "input_error", "input field %s is too long" % key)
        if "pattern" in definition and not re.fullmatch(definition["pattern"], value):
            raise CliError(EXIT_INPUT, "input_error", "input field %s has an invalid format" % key)
        if "minimum" in definition and value < definition["minimum"]:
            raise CliError(EXIT_INPUT, "input_error", "input field %s is below minimum" % key)
        if "maximum" in definition and value > definition["maximum"]:
            raise CliError(EXIT_INPUT, "input_error", "input field %s is above maximum" % key)
    if capability.get("id") == "video-generate":
        _validate_video_channel(payload)
    if capability.get("id") == "text-video-generate":
        _validate_text_video_talking(payload)
    if capability.get("id") == "leads-generate":
        platforms = payload.get("platforms") or []
        if any(platform in {"douyin", "xhs"} for platform in platforms) and not payload.get("keyword"):
            raise CliError(EXIT_INPUT, "input_error", "douyin or xhs leads require keyword")
        if "channels" in platforms and not payload.get("channels_targets"):
            raise CliError(EXIT_INPUT, "input_error", "channels leads require channels_targets")


def _validate_text_video_talking(payload):
    talking = payload.get("talking_material")
    if talking is None:
        return
    required = {"enabled", "plan_id", "source_hash", "ratio", "default_avatar_asset_id", "scenes"}
    if set(talking) != required or talking.get("enabled") is not True:
        raise CliError(EXIT_INPUT, "input_error", "talking_material has invalid fields")
    if not re.fullmatch(r"talking_plan_[0-9a-f]{32}", str(talking.get("plan_id") or "")):
        raise CliError(EXIT_INPUT, "input_error", "talking_material plan_id is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(talking.get("source_hash") or "")):
        raise CliError(EXIT_INPUT, "input_error", "talking_material source_hash is invalid")
    if (isinstance(talking.get("ratio"), bool)
            or not isinstance(talking.get("ratio"), (int, float))
            or not 0.1 <= float(talking["ratio"]) <= 0.5):
        raise CliError(EXIT_INPUT, "input_error", "talking_material ratio is invalid")
    avatar_pattern = r"local_avatar_[0-9a-f]{32}"
    if not re.fullmatch(avatar_pattern, str(talking.get("default_avatar_asset_id") or "")):
        raise CliError(EXIT_INPUT, "input_error", "talking_material default avatar is invalid")
    scenes = talking.get("scenes")
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 20:
        raise CliError(EXIT_INPUT, "input_error", "talking_material scenes must contain 1-20 items")
    seen = set()
    enabled = False
    for scene in scenes:
        if not isinstance(scene, dict) or not {"scene_id", "enabled"} <= set(scene) or set(scene) - {
                "scene_id", "enabled", "avatar_asset_id"}:
            raise CliError(EXIT_INPUT, "input_error", "talking_material scene is invalid")
        scene_id = str(scene.get("scene_id") or "")
        if not re.fullmatch(r"scene_[0-9]{2}", scene_id) or scene_id in seen:
            raise CliError(EXIT_INPUT, "input_error", "talking_material scene_id is invalid or duplicated")
        seen.add(scene_id)
        if not isinstance(scene.get("enabled"), bool):
            raise CliError(EXIT_INPUT, "input_error", "talking_material scene enabled must be boolean")
        enabled = enabled or scene["enabled"]
        override = scene.get("avatar_asset_id")
        if override is not None and (not scene["enabled"] or not re.fullmatch(avatar_pattern, str(override))):
            raise CliError(EXIT_INPUT, "input_error", "talking_material scene avatar is invalid")
    if not enabled:
        raise CliError(EXIT_INPUT, "input_error", "talking_material must enable at least one scene")


def _doctor(environment):
    checks = []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    for service, path in (("auth", "/api/auth/health"), ("generation", "/api/gen/health")):
        request = urllib.request.Request(ENVIRONMENTS[environment] + path, headers={"User-Agent": "hq-cli/%s" % __version__})
        try:
            with opener.open(request, timeout=5) as response:
                status = response.getcode()
        except (urllib.error.URLError, OSError) as exc:
            raise CliError(EXIT_DOCTOR, "doctor_error", "%s health check failed: %s" % (service, exc))
        if status < 200 or status >= 300:
            raise CliError(EXIT_DOCTOR, "doctor_error", "%s health check returned HTTP %s" % (service, status))
        checks.append({"service": service, "url": request.full_url, "http_status": status, "status": "ok"})
    return checks


def _checked_response(status, payload, accepted=None):
    accepted = accepted or range(200, 300)
    if status not in accepted:
        detail = payload.get("detail") if isinstance(payload, dict) else "request failed"
        code = payload.get("code") if isinstance(payload, dict) else "api_error"
        if status == 401:
            raise CliError(EXIT_AUTH, "auth_error", str(detail) + "; run `hq login --json`", {"http_status": status, "code": code})
        exit_code = EXIT_CONFIRMATION if status == 409 else EXIT_API
        details = {"http_status": status}
        if isinstance(payload, dict):
            for key in (
                    "jobs", "job_ids", "failures", "next_index", "batch_id",
                    "refund_state", "points_left", "submitted_count", "failed_count"):
                if key in payload:
                    details[key] = payload[key]
        raise CliError(exit_code, str(code or "api_error"), str(detail), details)
    return payload


def _request(path, method="GET", body=None, token="", timeout=30, accepted=None):
    try:
        status, payload = client.request_json(path, method=method, body=body, token=token, timeout=timeout)
    except (client.NetworkError, ValueError) as exc:
        raise CliError(EXIT_NETWORK, "network_error", "cannot reach Huangque main site: %s" % exc)
    return _checked_response(status, payload, accepted)


def _credentials():
    credentials = client.load_credentials()
    if not credentials:
        raise CliError(EXIT_AUTH, "auth_required", "HQ CLI is not authorized; run `hq login --json`")
    return credentials


def _login(no_browser):
    start = _request(
        "/api/auth/cli/device/start", "POST",
        {"client_name": "HQ CLI %s" % __version__, "requested_scopes": LOGIN_SCOPES},
    )
    instruction = _envelope(
        "hq.login.instructions/v1", user_code=start["user_code"],
        verification_uri=start["verification_uri"], scopes=start["scopes"],
        expires_in=start["expires_in"],
    )
    _write(sys.stderr, instruction)
    opened_browser = False
    if not no_browser:
        try:
            opened_browser = bool(webbrowser.open(start["verification_uri"]))
        except Exception:
            opened_browser = False
    deadline = time.monotonic() + int(start["expires_in"])
    interval = max(1, int(start.get("interval") or 3))
    while time.monotonic() < deadline:
        try:
            status, payload = client.request_json(
                "/api/auth/cli/device/poll", method="POST",
                body={"device_code": start["device_code"]}, timeout=10,
            )
        except client.NetworkError as exc:
            raise CliError(EXIT_NETWORK, "network_error", "authorization polling failed: %s" % exc)
        if status == 200 and payload.get("access_token"):
            expires_at = int(time.time()) + int(payload.get("expires_in") or 0)
            client.save_credentials(payload["access_token"], expires_at, payload.get("scopes") or [])
            try:
                current = _request("/api/auth/cli/status", token=payload["access_token"])
            except CliError:
                client.delete_credentials()
                raise
            return {"user": current.get("user"), "scopes": current.get("scopes"),
                    "expires_at": current.get("expires_at"), "opened_browser": opened_browser}
        code = payload.get("code") if isinstance(payload, dict) else ""
        if status in {202, 429} and code in {"authorization_pending", "slow_down"}:
            time.sleep(interval)
            continue
        detail = payload.get("detail") if isinstance(payload, dict) else "authorization failed"
        raise CliError(EXIT_AUTH, str(code or "auth_error"), str(detail), {"http_status": status})
    raise CliError(EXIT_AUTH, "expired_token", "device authorization expired; run `hq login --json` again")


def _add_common(parser, help_dest):
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-h", "--help", dest=help_dest, action="store_true", help=argparse.SUPPRESS)


def build_parser():
    parser = JsonArgumentParser(prog="hq", add_help=False, allow_abbrev=False)
    _add_common(parser, "show_help")
    subcommands = parser.add_subparsers(dest="command")
    for name in ("version", "capabilities", "channels", "help", "status", "logout"):
        command = subcommands.add_parser(name, add_help=False, allow_abbrev=False)
        _add_common(command, "show_command_help")
    login = subcommands.add_parser("login", add_help=False, allow_abbrev=False)
    _add_common(login, "show_command_help")
    login.add_argument("--no-browser", action="store_true")
    describe = subcommands.add_parser("describe", add_help=False, allow_abbrev=False)
    _add_common(describe, "show_command_help")
    describe.add_argument("id", nargs="?")
    run = subcommands.add_parser("run", add_help=False, allow_abbrev=False)
    _add_common(run, "show_command_help")
    run.add_argument("id", nargs="?")
    run.add_argument("--input")
    run.add_argument("--environment", choices=sorted(ENVIRONMENTS), default="main")
    run.add_argument("--open-browser", action="store_true")
    run.add_argument("--confirm", action="store_true")
    run.add_argument("--quote-token")
    run.add_argument("--file")
    doctor = subcommands.add_parser("doctor", add_help=False, allow_abbrev=False)
    _add_common(doctor, "show_command_help")
    doctor.add_argument("--environment", choices=sorted(ENVIRONMENTS), default="main")
    mcp = subcommands.add_parser("mcp", add_help=False, allow_abbrev=False)
    _add_common(mcp, "show_command_help")
    skill = subcommands.add_parser("skill", add_help=False, allow_abbrev=False)
    _add_common(skill, "show_skill_help")
    skill_commands = skill.add_subparsers(dest="skill_command")
    install = skill_commands.add_parser("install", add_help=False, allow_abbrev=False)
    _add_common(install, "show_skill_command_help")
    install.add_argument("target", nargs="?", choices=("deepseek", "codex", "openclaw", "pi", "mcp"))
    install.add_argument("--replace", action="store_true")
    return parser


def _help(command=None):
    return _envelope(
        "hq.help/v1", command=command,
        commands=["login", "status", "logout", "capabilities", "channels", "describe ID", "run ID",
                  "skill install TARGET", "mcp", "doctor", "version"],
        next_actions=["Run `hq login --json`, then `hq capabilities --json`, then inspect and run one capability."],
    )


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        if args.command is None or args.command == "help" or args.show_help or getattr(args, "show_command_help", False):
            _write(sys.stdout, _help(args.command))
            return 0
        if args.command == "version":
            _write(sys.stdout, _envelope("hq.version/v1", product="Huangque main-site CLI",
                                         origin=ENVIRONMENTS["main"], next_actions=["Run `hq capabilities --json`. "]))
            return 0
        if args.command == "mcp":
            return mcp_server.serve()
        if args.command == "skill":
            if (args.skill_command is None or getattr(args, "show_skill_help", False)
                    or getattr(args, "show_skill_command_help", False)):
                _write(sys.stdout, _help("skill"))
                return 0
            if args.skill_command != "install" or not args.target:
                raise CliError(EXIT_USAGE, "usage_error", "skill install requires a target")
            try:
                result = skill_install.install_skill(args.target, replace=args.replace)
            except skill_install.SkillInstallError as exc:
                raise CliError(EXIT_INSTALL, exc.error, exc.message, exc.details)
            except OSError as exc:
                raise CliError(EXIT_INSTALL, "skill_install_error", "cannot install Huangque Agent Skill: %s" % exc)
            _write(sys.stdout, _envelope(
                "hq.skill.install/v1", result=result,
                next_actions=["Restart or reload the selected Agent, then ask it to use `use-huangque-cli`."],
            ))
            return 0
        if args.command == "capabilities":
            _write(sys.stdout, _envelope("hq.capabilities/v1", capabilities=capability_list(),
                                         next_actions=["Inspect one capability with `hq describe ID --json` before running it."]))
            return 0
        if args.command == "channels":
            credentials = _credentials()
            result = _request("/api/auth/cli/action", "POST", {
                "action": "channels", "input": {}, "confirm": False,
            }, credentials["access_token"])
            _write(sys.stdout, _envelope("hq.channels/v1", result=result,
                                         next_actions=["Use each channel's capabilities and selector with `hq run`. "]))
            return 0
        if args.command == "login":
            result = _login(args.no_browser)
            _write(sys.stdout, _envelope("hq.login/v1", result=result,
                                         next_actions=["Run `hq status --json`, then `hq capabilities --json`. "]))
            return 0
        if args.command == "status":
            credentials = _credentials()
            result = _request("/api/auth/cli/status", token=credentials["access_token"])
            _write(sys.stdout, _envelope("hq.status/v1", result=result,
                                         next_actions=["Run `hq capabilities --json` to discover account-bound actions."]))
            return 0
        if args.command == "logout":
            credentials = client.load_credentials()
            if credentials:
                _request("/api/auth/cli/logout", "POST", {}, credentials["access_token"])
                client.delete_credentials()
            _write(sys.stdout, _envelope("hq.logout/v1", revoked=bool(credentials),
                                         next_actions=["Run `hq login --json` to authorize again."]))
            return 0
        if args.command in ("describe", "run"):
            if not args.id:
                raise CliError(EXIT_USAGE, "usage_error", "%s requires an ID" % args.command)
            capability = CAPABILITIES.get(args.id)
            if capability is None:
                raise CliError(EXIT_UNKNOWN_CAPABILITY, "unknown_capability", "unknown capability: %s" % args.id)
            if args.command == "describe":
                next_action = ("Run `hq run %s --file /absolute/path --confirm --json`." % args.id
                               if capability["kind"] == "upload" else
                               "Use only this input_schema with `hq run %s --input @file --json`." % args.id)
                _write(sys.stdout, _envelope("hq.describe/v1", capability=capability,
                    next_actions=[next_action]))
                return 0
            if not capability["runnable"]:
                raise CliError(EXIT_UNAVAILABLE, "unavailable_capability", "capability is unavailable: %s" % args.id)
            is_upload = capability["kind"] == "upload"
            if is_upload:
                if args.input:
                    raise CliError(EXIT_USAGE, "usage_error", "upload capabilities do not accept --input")
                payload = {}
            else:
                if args.file:
                    raise CliError(EXIT_USAGE, "usage_error", "only upload capabilities accept --file")
                payload = _load_json(args.input)
                _validate(capability, payload)
            if is_upload:
                if args.open_browser or args.quote_token:
                    raise CliError(EXIT_USAGE, "usage_error", "upload capabilities do not accept browser or quote options")
                if not args.confirm:
                    raise CliError(EXIT_CONFIRMATION, "confirmation_required", "re-run this upload with --confirm")
                if not args.file:
                    raise CliError(EXIT_USAGE, "usage_error", "%s requires --file /absolute/path" % args.id)
                credentials = _credentials()
                if args.id == "video-upload":
                    upload_kind, uploader = "video", client.upload_video
                elif args.id == "audio-upload":
                    upload_kind, uploader = "audio", client.upload_audio
                else:
                    upload_kind, uploader = "image", client.upload_image
                try:
                    status, upload = uploader(args.file, credentials["access_token"])
                except ValueError as exc:
                    raise CliError(EXIT_INPUT, "invalid_upload_file", "%s upload failed: %s" % (upload_kind, exc))
                except client.NetworkError as exc:
                    raise CliError(EXIT_NETWORK, "upload_error", "%s upload failed: %s" % (upload_kind, exc))
                result = _checked_response(status, upload)
            elif capability["kind"] == "navigation":
                if args.confirm or args.quote_token:
                    raise CliError(EXIT_USAGE, "usage_error", "navigation does not accept --confirm or --quote-token")
                url = resolve_url(capability, args.environment, payload)
                opened_browser = False
                if args.open_browser:
                    try:
                        opened_browser = bool(webbrowser.open(url))
                    except Exception as exc:
                        raise CliError(EXIT_BROWSER, "browser_error", "browser open failed: %s" % exc)
                result = {"url": url, "opened_browser": opened_browser}
            else:
                if args.open_browser:
                    raise CliError(EXIT_USAGE, "usage_error", "API capabilities do not accept --open-browser")
                paid = capability["side_effect"] == "paid"
                if capability["confirmation_required"] and not paid and not args.confirm:
                    raise CliError(EXIT_CONFIRMATION, "confirmation_required", "re-run this action with --confirm")
                if args.quote_token and not args.confirm:
                    raise CliError(EXIT_USAGE, "usage_error", "--quote-token requires --confirm")
                if paid and args.confirm and not args.quote_token:
                    raise CliError(EXIT_CONFIRMATION, "quote_required", "run without --confirm first, then reuse the same input with the returned quote_token")
                credentials = _credentials()
                request_body = {"action": capability["api_action"], "input": payload, "confirm": bool(args.confirm)}
                if args.quote_token:
                    request_body["quote_token"] = args.quote_token
                result = _request("/api/auth/cli/action", "POST", request_body,
                                  credentials["access_token"],
                                  timeout=310 if capability["id"] == "ip12-message" else 120)
            next_actions = list(capability["next_actions"])
            if capability["side_effect"] == "paid" and not args.confirm:
                next_actions = ["Review cost and points, then re-run the identical input with `--confirm --quote-token <quote_token>`. "]
            _write(sys.stdout, _envelope("hq.run/v1", capability=args.id, result=result, next_actions=next_actions))
            return 0
        if args.command == "doctor":
            _write(sys.stdout, _envelope("hq.doctor/v1", environment=args.environment, checks=_doctor(args.environment),
                                         next_actions=["Run `hq login --json` for account-bound actions."]))
            return 0
        raise CliError(EXIT_USAGE, "usage_error", "unknown command")
    except CliError as exc:
        return _error(exc)


if __name__ == "__main__":
    raise SystemExit(main())
