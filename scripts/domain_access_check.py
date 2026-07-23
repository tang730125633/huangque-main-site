#!/usr/bin/env python3
"""Run one read-only domain access check; do not repair or monitor."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from urllib.parse import urlparse


REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class Observation:
    status: int
    url: str
    location: str | None
    elapsed_ms: int


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    code: str
    message: str
    http: Observation | None = None
    https: Observation | None = None


def classify(
    domain: str,
    http: Observation,
    https: Observation | None,
) -> CheckResult:
    if _is_webblock(http.location) or (https and _is_webblock(https.url)):
        return CheckResult(
            False,
            "DNSPOD_WEBBLOCK",
            "request intercepted by DNSPod webblock",
            http,
            https,
        )

    redirect = urlparse(http.location or "")
    if http.status not in REDIRECT_STATUSES:
        return CheckResult(
            False,
            "HTTP_STATUS",
            f"expected HTTP redirect, got {http.status}",
            http,
            https,
        )
    if redirect.scheme != "https" or redirect.hostname != domain:
        return CheckResult(
            False,
            "UNEXPECTED_REDIRECT",
            "HTTP redirect did not target site-owned HTTPS",
            http,
            https,
        )
    if https is None:
        return CheckResult(
            False,
            "HTTPS_MISSING",
            "HTTPS observation is missing",
            http,
            https,
        )

    final = urlparse(https.url)
    if final.hostname != domain:
        return CheckResult(
            False,
            "HTTPS_CROSS_DOMAIN",
            "HTTPS finished on another domain",
            http,
            https,
        )
    if not 200 <= https.status < 300:
        return CheckResult(
            False,
            "HTTPS_STATUS",
            f"expected HTTPS 2xx, got {https.status}",
            http,
            https,
        )
    return CheckResult(True, "OK", "site reachable", http, https)


def _is_webblock(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return (
        parsed.hostname == "dnspod.qcloud.com"
        and parsed.path == "/static/webblock.html"
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url: str, timeout: float, follow_redirects: bool) -> Observation:
    opener = (
        urllib.request.build_opener()
        if follow_redirects
        else urllib.request.build_opener(_NoRedirect)
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "huangque-domain-check/1"},
    )
    started = time.monotonic()
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error

    elapsed_ms = round((time.monotonic() - started) * 1000)
    try:
        return Observation(
            status=response.getcode(),
            url=response.geturl(),
            location=response.headers.get("Location"),
            elapsed_ms=elapsed_ms,
        )
    finally:
        response.close()


def run_check(domain: str, timeout: float, fetcher=fetch) -> CheckResult:
    try:
        http = fetcher(f"http://{domain}/", timeout, False)
        early = classify(domain, http, None)
        if early.code in {
            "DNSPOD_WEBBLOCK",
            "HTTP_STATUS",
            "UNEXPECTED_REDIRECT",
        }:
            return early
        https = fetcher(http.location, timeout, True)
        return classify(domain, http, https)
    except Exception as error:
        return CheckResult(False, "NETWORK_ERROR", str(error))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one read-only HTTP/HTTPS access check "
            "(no repair or continuous monitoring)"
        )
    )
    parser.add_argument("--domain", default="huangquechuanmei.com")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    result = run_check(args.domain, args.timeout)
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
