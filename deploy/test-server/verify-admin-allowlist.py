#!/usr/bin/env python3
"""Validate the active Nginx admin allowlist without printing configured sources."""

import ipaddress
import pathlib
import re
import sys


ALLOW = re.compile(r"allow\s+([^\s;]+)\s*;", re.IGNORECASE)


def canonical_network(value):
    try:
        network = ipaddress.ip_network(value.strip(), strict=False)
    except (AttributeError, ValueError) as error:
        raise ValueError("invalid IP or CIDR") from error
    if network.prefixlen == 0:
        raise ValueError("universal networks are forbidden")
    return network


def active_directives(path):
    directives = []
    for raw_line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            directives.append(line)
    return directives


def verify(path, expected_source):
    expected = canonical_network(expected_source)
    directives = active_directives(path)
    if len(directives) < 2 or directives[-1].lower() != "deny all;":
        raise ValueError("allowlist must end with exactly one deny all")

    allowed = []
    for directive in directives[:-1]:
        match = ALLOW.fullmatch(directive)
        if not match:
            raise ValueError("unexpected active directive")
        allowed.append(canonical_network(match.group(1)))
    if not allowed:
        raise ValueError("at least one allow directive is required")
    if expected not in allowed:
        raise ValueError("expected source is not represented")
    return True


def main(argv):
    if len(argv) != 3:
        print("invalid allowlist verifier invocation", file=sys.stderr)
        return 2
    try:
        verify(argv[1], argv[2])
    except (OSError, UnicodeError, ValueError):
        print("admin allowlist verification failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
