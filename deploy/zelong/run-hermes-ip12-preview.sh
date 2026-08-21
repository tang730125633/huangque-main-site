#!/usr/bin/env bash
set -euo pipefail

set -a
# shellcheck disable=SC1091
. /home/ubuntu/content-api/content.env
set +a

: "${COPY_BASE:?COPY_BASE is required}"
: "${COPY_API_KEY:?COPY_API_KEY is required}"
export OPENAI_API_BASE="$COPY_BASE"
export OPENAI_API_KEY="$COPY_API_KEY"
export HERMES_MODEL="${HERMES_PREVIEW_MODEL:-gpt-5.4}"
export HERMES_HOME=/home/ubuntu/hermes-preview
export HERMES_DATA_DIR=/home/ubuntu/hermes-preview-data
export HERMES_DATA_QUOTA_MB=512
export HERMES_AUTH_BASE=http://127.0.0.1:8095
export HQ_AUTH_BASE=http://127.0.0.1:8095
export HERMES_ENABLE_INTERNAL_TOOLS=0
export PYTHONPATH="/home/ubuntu/hermes-preview-deps${PYTHONPATH:+:$PYTHONPATH}"

cd "$HERMES_HOME"

if test "${1:-}" = --check; then
  exec /usr/bin/python3 -c '
import os
import requests

base = os.environ["OPENAI_API_BASE"].rstrip("/")
models_url = base[:-17] + "/models" if base.endswith("/chat/completions") else base + "/models"
response = requests.get(
    models_url,
    headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
    timeout=20,
)
response.raise_for_status()
models = {
    str(item.get("id"))
    for item in (response.json().get("data") or [])
    if isinstance(item, dict) and item.get("id")
}
if os.environ["HERMES_MODEL"] not in models:
    raise SystemExit("configured Hermes preview model is unavailable")

from server import app

routes = {rule.rule for rule in app.url_map.iter_rules() if rule.endpoint != "static"}
required = {
    "/",
    "/healthz",
    "/api/chat",
    "/api/conversations",
    "/api/conversations/import",
    "/api/conversations/<cid>/export",
}
missing = sorted(required - routes)
if missing:
    raise SystemExit("missing required preview routes: " + ", ".join(missing))
'
fi

exec /usr/bin/python3 -c \
  "import server; server.app.run(host='127.0.0.1', port=3102, debug=False)"
