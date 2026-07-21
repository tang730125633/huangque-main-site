"""Small HTTP helpers shared by provider clients."""


def api_url(base, path):
    """Join OpenAI-compatible bases without producing /v1/v1/... paths."""
    base = str(base or "").rstrip("/")
    path = "/" + str(path or "").lstrip("/")
    if base.endswith("/v1") and path.startswith("/v1/"):
        path = path[3:]
    return base + path
