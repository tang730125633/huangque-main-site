#!/usr/bin/env python3
"""Read-only TCP listener exposure audit for the Huangque host.

The script parses `ss -lntp` output and reports listeners that should be
reviewed before any port-convergence or firewall change is attempted. It never
changes firewall rules, service files, or process state.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


PUBLIC_PORTS = {80, 443}
HUANGQUE_LOOPBACK_PORTS = {
    8090: "legacy leadgen",
    8091: "leadgen-A",
    8092: "leadgen-B",
    8095: "auth",
    8096: "content",
    8097: "download",
    8098: "admin",
    8100: "leadgen-api",
    8101: "imggen-api",
}
DECISION_REQUIRED_PORTS = {
    631: "cups",
    3001: "wechat decrypt",
    3002: "dify",
    5002: "dify",
    5003: "dify",
    8099: "zhipu nginx proxy",
    8102: "zhipu python proxy",
    8501: "xiaotan",
    18789: "openclaw",
}
DECISION_REQUIRED_RANGES = ((1890, 1893, "openclaw"),)


@dataclass(frozen=True)
class Listener:
    host: str
    port: int
    process: str
    raw: str


@dataclass(frozen=True)
class Finding:
    status: str
    port: int
    host: str
    service: str
    process: str
    message: str


def normalize_host(host: str) -> str:
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if "%" in host:
        host = host.split("%", 1)[0]
    return host or "*"


def parse_endpoint(endpoint: str) -> tuple[str, int] | None:
    endpoint = endpoint.strip()
    match = re.match(r"^\[(?P<host>.*)\]:(?P<port>\d+)$", endpoint)
    if not match:
        match = re.match(r"^(?P<host>.*):(?P<port>\d+)$", endpoint)
    if not match:
        return None
    return normalize_host(match.group("host")), int(match.group("port"))


def parse_ss(text: str) -> list[Listener]:
    listeners: list[Listener] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith(("netid ", "state ")):
            continue
        parts = stripped.split()
        try:
            listen_index = next(i for i, part in enumerate(parts) if part.upper() == "LISTEN")
        except StopIteration:
            continue
        fields_after_state = parts[listen_index + 1 :]
        if len(fields_after_state) < 3:
            continue
        endpoint = fields_after_state[2]
        parsed = parse_endpoint(endpoint)
        if not parsed:
            continue
        host, port = parsed
        process = " ".join(fields_after_state[4:]) if len(fields_after_state) > 4 else ""
        listeners.append(Listener(host=host, port=port, process=process, raw=stripped))
    return listeners


def is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def is_public_bind(host: str) -> bool:
    if host in {"0.0.0.0", "::", "*"}:
        return True
    return not is_loopback(host)


def decision_service(port: int) -> str | None:
    if port in DECISION_REQUIRED_PORTS:
        return DECISION_REQUIRED_PORTS[port]
    for start, end, service in DECISION_REQUIRED_RANGES:
        if start <= port <= end:
            return service
    return None


def classify(listener: Listener) -> Finding:
    public = is_public_bind(listener.host)
    service = (
        HUANGQUE_LOOPBACK_PORTS.get(listener.port)
        or decision_service(listener.port)
        or ("public web" if listener.port in PUBLIC_PORTS else "unknown")
    )
    if listener.port in PUBLIC_PORTS and public:
        return Finding("OK", listener.port, listener.host, service, listener.process, "expected public web listener")
    if listener.port in HUANGQUE_LOOPBACK_PORTS:
        if public:
            return Finding(
                "WARN",
                listener.port,
                listener.host,
                service,
                listener.process,
                "huangque internal service should bind loopback only",
            )
        return Finding("OK", listener.port, listener.host, service, listener.process, "huangque loopback listener")
    decision = decision_service(listener.port)
    if decision and public:
        return Finding(
            "REVIEW",
            listener.port,
            listener.host,
            decision,
            listener.process,
            "external/non-huangque port requires owner confirmation before convergence",
        )
    if public:
        return Finding(
            "REVIEW",
            listener.port,
            listener.host,
            service,
            listener.process,
            "public listener is not in the approved inventory",
        )
    return Finding("OK", listener.port, listener.host, service, listener.process, "loopback listener")


def read_ss_output(input_path: str | None) -> str:
    if input_path:
        return Path(input_path).read_text(encoding="utf-8")
    result = subprocess.run(["ss", "-lntp"], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "`ss -lntp` failed")
    return result.stdout


def print_table(findings: list[Finding]) -> None:
    print("STATUS  PORT   HOST          SERVICE                 MESSAGE")
    for item in findings:
        print(
            f"{item.status:<7} {item.port:<6} {item.host:<13} "
            f"{item.service:<23} {item.message}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit public TCP listeners from ss -lntp output.")
    parser.add_argument("--input", help="Read saved ss -lntp output instead of running ss.")
    parser.add_argument("--json", action="store_true", help="Emit JSON findings.")
    args = parser.parse_args(argv)

    try:
        listeners = parse_ss(read_ss_output(args.input))
    except Exception as exc:  # pragma: no cover - command failures depend on host
        print(f"port audit failed: {exc}", file=sys.stderr)
        return 1

    findings = sorted((classify(item) for item in listeners), key=lambda item: (item.status != "OK", item.port, item.host))
    if args.json:
        print(json.dumps([asdict(item) for item in findings], ensure_ascii=False, indent=2))
    else:
        print_table(findings)

    return 2 if any(item.status in {"WARN", "REVIEW"} for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
