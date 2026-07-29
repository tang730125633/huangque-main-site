"""JSON-only command line interface for the fixed HQ CLI V0.1 contract."""

import argparse
import json
import math
import sys
import urllib.error
import urllib.request
import webbrowser

from . import __version__
from .catalog import CAPABILITIES, ENVIRONMENTS, capability_list, resolve_url

EXIT_USAGE = 2
EXIT_UNKNOWN_CAPABILITY = 3
EXIT_INPUT = 4
EXIT_DOCTOR = 5
EXIT_UNAVAILABLE = 6
EXIT_BROWSER = 7
MAX_INPUT_BYTES = 65536


class CliError(Exception):
    def __init__(self, code, error, message):
        super().__init__(message)
        self.code = code
        self.error = error
        self.message = message


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError(EXIT_USAGE, "usage_error", message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _write(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _envelope(schema, **values):
    return {"schema": schema, "cli_version": __version__, **values}


def _error(error):
    _write(sys.stderr, _envelope("hq.error/v1", error=error.error, message=error.message, exit_code=error.code))
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
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_non_finite)
    except (UnicodeDecodeError, ValueError, RecursionError, json.JSONDecodeError) as exc:
        raise CliError(EXIT_INPUT, "input_error", "input must be finite UTF-8 JSON: %s" % exc)
    _validate_unicode(payload)
    if not isinstance(payload, dict):
        raise CliError(EXIT_INPUT, "input_error", "input must be a JSON object")
    return payload


def _validate(capability, payload):
    schema = capability["input_schema"]
    properties = schema["properties"]
    unknown = sorted(set(payload) - set(properties))
    if unknown:
        raise CliError(EXIT_INPUT, "input_error", "unknown input field: %s" % unknown[0])
    for key in schema["required"]:
        if key not in payload:
            raise CliError(EXIT_INPUT, "input_error", "missing required input field: %s" % key)
    for key, definition in properties.items():
        if key not in payload:
            continue
        value = payload[key]
        if definition["type"] == "string" and not isinstance(value, str):
            raise CliError(EXIT_INPUT, "input_error", "input field %s must be a string" % key)
        if definition["type"] == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise CliError(EXIT_INPUT, "input_error", "input field %s must be a finite number" % key)
        if definition["type"] == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise CliError(EXIT_INPUT, "input_error", "input field %s must be an integer" % key)
        if "enum" in definition and value not in definition["enum"]:
            raise CliError(EXIT_INPUT, "input_error", "input field %s must be one of: %s" % (key, ", ".join(definition["enum"])))
        if "minLength" in definition and len(value) < definition["minLength"]:
            raise CliError(EXIT_INPUT, "input_error", "input field %s is too short" % key)
        if "maxLength" in definition and len(value) > definition["maxLength"]:
            raise CliError(EXIT_INPUT, "input_error", "input field %s is too long" % key)
        if "minimum" in definition and value < definition["minimum"]:
            raise CliError(EXIT_INPUT, "input_error", "input field %s is below minimum" % key)
        if "maximum" in definition and value > definition["maximum"]:
            raise CliError(EXIT_INPUT, "input_error", "input field %s is above maximum" % key)


def _doctor(environment):
    checks = []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    for service, path in (("auth", "/api/auth/health"), ("generation", "/api/gen/health")):
        request = urllib.request.Request(ENVIRONMENTS[environment] + path, headers={"User-Agent": "hq-cli/%s" % __version__})
        try:
            with opener.open(request, timeout=5) as response:
                status = response.getcode()
        except (urllib.error.URLError, OSError) as exc:
            raise CliError(EXIT_DOCTOR, "doctor_error", "%s %s health check failed: %s" % (environment, service, exc))
        if status < 200 or status >= 300:
            raise CliError(EXIT_DOCTOR, "doctor_error", "%s %s health check returned HTTP %s" % (environment, service, status))
        checks.append({"service": service, "url": request.full_url, "http_status": status, "status": "ok"})
    return checks


def _add_common(parser, help_dest):
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-h", "--help", dest=help_dest, action="store_true", help=argparse.SUPPRESS)


def build_parser():
    parser = JsonArgumentParser(prog="hq", add_help=False, allow_abbrev=False)
    _add_common(parser, "show_help")
    subcommands = parser.add_subparsers(dest="command")
    for name in ("version", "capabilities", "help"):
        command = subcommands.add_parser(name, add_help=False, allow_abbrev=False)
        _add_common(command, "show_command_help")
    describe = subcommands.add_parser("describe", add_help=False, allow_abbrev=False)
    _add_common(describe, "show_command_help")
    describe.add_argument("id", nargs="?")
    run = subcommands.add_parser("run", add_help=False, allow_abbrev=False)
    _add_common(run, "show_command_help")
    run.add_argument("id", nargs="?")
    run.add_argument("--input")
    run.add_argument("--environment", choices=sorted(ENVIRONMENTS), default="main")
    run.add_argument("--open-browser", action="store_true")
    doctor = subcommands.add_parser("doctor", add_help=False, allow_abbrev=False)
    _add_common(doctor, "show_command_help")
    doctor.add_argument("--environment", choices=sorted(ENVIRONMENTS), default="main")
    return parser


def _help(command=None):
    return _envelope(
        "hq.help/v1",
        command=command,
        commands=["version", "capabilities", "describe ID", "run ID", "doctor"],
        next_actions=["Run `hq capabilities --json`, then `hq describe ID --json`, then `hq run ID --environment main --json`."],
    )


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        if args.command is None or args.command == "help" or args.show_help or getattr(args, "show_command_help", False):
            _write(sys.stdout, _help(args.command))
            return 0
        if args.command == "version":
            _write(sys.stdout, _envelope("hq.version/v1", next_actions=["Run `hq capabilities --json` to discover safe actions."]))
            return 0
        if args.command == "capabilities":
            _write(sys.stdout, _envelope("hq.capabilities/v1", capabilities=capability_list(), next_actions=["Choose a runnable capability, inspect its complete schema, then run hq run ID --json."]))
            return 0
        if args.command in ("describe", "run"):
            if not args.id:
                raise CliError(EXIT_USAGE, "usage_error", "%s requires an ID" % args.command)
            capability = CAPABILITIES.get(args.id)
            if capability is None:
                raise CliError(EXIT_UNKNOWN_CAPABILITY, "unknown_capability", "unknown capability: %s" % args.id)
            if args.command == "describe":
                next_action = (
                    "Use `hq run %s --environment main --json` with only this input_schema." % args.id
                    if capability["runnable"]
                    else "Do not run this capability in V0.1; it requires an authenticated product flow."
                )
                _write(sys.stdout, _envelope("hq.describe/v1", capability=capability, next_actions=[next_action]))
                return 0
            if not capability["runnable"]:
                raise CliError(EXIT_UNAVAILABLE, "unavailable_capability", "capability is not runnable in V0.1: %s" % args.id)
            payload = _load_json(args.input)
            _validate(capability, payload)
            url = resolve_url(capability, args.environment, payload)
            opened_browser = False
            if args.open_browser:
                try:
                    opened_browser = bool(webbrowser.open(url))
                except Exception as exc:
                    raise CliError(EXIT_BROWSER, "browser_error", "browser open failed: %s" % exc)
            _write(sys.stdout, _envelope("hq.run/v1", url=url, opened_browser=opened_browser, next_actions=capability["next_actions"]))
            return 0
        if args.command == "doctor":
            _write(sys.stdout, _envelope("hq.doctor/v1", environment=args.environment, checks=_doctor(args.environment), next_actions=["Use `hq capabilities --json` for safe deep links."]))
            return 0
        raise CliError(EXIT_USAGE, "usage_error", "unknown command")
    except CliError as exc:
        return _error(exc)


if __name__ == "__main__":
    raise SystemExit(main())
