#!/usr/bin/env bash
set -euo pipefail

set -a
# shellcheck disable=SC1091
. /home/ubuntu/content-api/content.env
set +a

: "${OPENAI_API_KEY:?OPENAI_API_KEY is required}"
export OPENAI_API_BASE="${HERMES_PREVIEW_BASE:-https://api.openai.com/v1}"
export OPENAI_API_KEY="${HERMES_PREVIEW_API_KEY:-$OPENAI_API_KEY}"
export HERMES_MODEL="${HERMES_PREVIEW_MODEL:-gpt-5.6-terra}"
export HTTP_PROXY="${HERMES_PREVIEW_HTTP_PROXY:-http://127.0.0.1:10810}"
export HTTPS_PROXY="${HERMES_PREVIEW_HTTPS_PROXY:-http://127.0.0.1:10810}"
export NO_PROXY="${HERMES_PREVIEW_NO_PROXY:-127.0.0.1,localhost,::1}"
export HERMES_MASTER_AGENT_MODE="${HERMES_PREVIEW_MASTER_AGENT_MODE:-live}"
export HERMES_HOME=/home/ubuntu/hermes-preview
export HERMES_DATA_DIR=/home/ubuntu/hermes-preview-data
export HERMES_DATA_QUOTA_MB=512
export HERMES_AUTH_BASE=http://127.0.0.1:8095
export HQ_AUTH_BASE=http://127.0.0.1:8095
export HERMES_ENABLE_INTERNAL_TOOLS=1
export HERMES_AGENT_RUNTIME_WORKER_ENABLED=1
export HERMES_AGENT_RUNTIME_WORKER_INTERVAL=3
export PYTHONPATH="/home/ubuntu/hermes-preview-deps${PYTHONPATH:+:$PYTHONPATH}"
export PATH="/home/ubuntu/hermes-preview/bin:$PATH"
PYTHON=/usr/bin/python3

cd "$HERMES_HOME"

if test "${1:-}" = --check; then
  exec "$PYTHON" -c '
import importlib.util
import os
from pathlib import Path
import shutil
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
    if models:
        raise SystemExit("configured Hermes preview model is unavailable")
    chat_url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    probe = requests.post(
        chat_url,
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
        json={
            "model": os.environ["HERMES_MODEL"],
            "messages": [{"role": "user", "content": "reply OK"}],
            "max_tokens": 1,
            "temperature": 0,
        },
        timeout=30,
    )
    probe.raise_for_status()

from server import app

routes = {rule.rule for rule in app.url_map.iter_rules() if rule.endpoint != "static"}
required = {
    "/",
    "/healthz",
    "/api/chat",
    "/api/conversations",
    "/api/conversations/import",
    "/api/conversations/<cid>/export",
    "/agnes-lab",
    "/api/agnes/status",
    "/team-workbench",
    "/api/team-workbench/status",
}
missing = sorted(required - routes)
if missing:
    raise SystemExit("missing required preview routes: " + ", ".join(missing))

modules = [
    "PIL", "playwright", "pypdf", "edge_tts", "faster_whisper",
    "requests", "yt_dlp",
]
missing_modules = [name for name in modules if importlib.util.find_spec(name) is None]
if missing_modules:
    raise SystemExit("missing preview media modules: " + ", ".join(missing_modules))
missing_commands = [
    name for name in ("ffmpeg", "ffprobe", "edge-tts", "yt-dlp")
    if shutil.which(name) is None
]
if missing_commands:
    raise SystemExit("missing preview media commands: " + ", ".join(missing_commands))
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    executable = Path(playwright.chromium.executable_path)
    if not executable.is_file():
        raise SystemExit("missing preview Chromium")
    browser = playwright.chromium.launch(headless=True, executable_path=str(executable))
    browser.close()
'
fi

exec "$PYTHON" -c \
  "import server; server.app.run(host='127.0.0.1', port=3102, debug=False)"
