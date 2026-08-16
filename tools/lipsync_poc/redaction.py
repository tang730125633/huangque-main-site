"""Small deterministic scrubber for PoC reports and provider errors."""

import re
from urllib.parse import urlsplit, urlunsplit


SENSITIVE_FRAGMENTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
)


def _sensitive_key(key):
    lowered = str(key).lower()
    return any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS)


def _safe_url(value):
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return value
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        port_value = parsed.port
        if not hostname:
            return "[REDACTED_URL]"
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{port_value}" if port_value is not None else ""
        return urlunsplit(
            (parsed.scheme, hostname + port, parsed.path, "", "")
        )
    except Exception:
        # Provider error strings are untrusted. Redaction must be total:
        # malformed ports, brackets or Unicode netlocs must never prevent the
        # failure state/report from being persisted.
        return "[REDACTED_URL]"


def _safe_string(value):
    value = re.sub(
        (
            r"(?im)\b("
            r"proxy-authorization|authorization|set-cookie|cookie|"
            r"x-api-key|x-auth-token"
            r")\s*:\s*[^\r\n]*"
        ),
        lambda match: f"{match.group(1)}: [REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)\bBearer\s+[^\s,;]+",
        "Bearer [REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)\b(token|api[_-]?key|secret|password)=([^&\s]+)",
        lambda match: f"{match.group(1)}=[REDACTED]",
        value,
    )
    return re.sub(
        r"https?://[^\s]+",
        lambda match: _safe_url(match.group(0).rstrip(".,);"))
        + match.group(0)[len(match.group(0).rstrip(".,);")):],
        value,
    )


def redact(value):
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _sensitive_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _safe_string(value)
    return value
