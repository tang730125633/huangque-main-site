#!/usr/bin/env python3
"""Run the real IP12 page locally with Codex subscription and a fictional owner."""

import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--data-dir", default="/tmp/ip12-persona-agent-v1")
    args = parser.parse_args()

    os.environ["HERMES_AI_TRANSPORT"] = "codex-cli"
    os.environ["HERMES_DATA_DIR"] = str(Path(args.data_dir).resolve())
    os.environ["HERMES_HOME"] = str(Path(args.data_dir).resolve())
    os.environ["HERMES_ENABLE_INTERNAL_TOOLS"] = "0"
    os.environ["HERMES_MASTER_AGENT_MODE"] = "off"
    os.environ["HERMES_SEMANTIC_ROUTER_MODE"] = "live"
    os.environ["HERMES_SEMANTIC_DEBUG"] = "0"
    os.environ.setdefault("HERMES_CODEX_MODEL", "gpt-5.6-terra")
    for key in list(os.environ):
        if key.upper().endswith(("_API_KEY", "_ACCESS_TOKEN", "_SECRET")):
            os.environ.pop(key, None)

    # Agents SDK 本地预览（仅本地旁路，生产门不受影响）：
    # 默认全量 SDK；仍可设置 CANARY_PROJECT_ID 只放行一个 Project。
    if True:
        os.environ["HERMES_COGNITIVE_ENGINE"] = "agents_sdk"
        os.environ["HERMES_AGENTS_SDK_ENABLED"] = "1"
        os.environ["HERMES_AGENTS_SDK_LOCAL_BYPASS"] = "1"
        os.environ.setdefault("HERMES_AGENTS_SDK_MODEL", os.environ.get("HERMES_AGENTS_SDK_MODEL") or "deepseek-v4-pro")
        os.environ.setdefault("HERMES_AGENTS_SDK_PROVIDER", os.environ.get("HERMES_AGENTS_SDK_PROVIDER") or "deepseek")
        keychain_key = os.environ.get("HERMES_AGENTS_SDK_OPENAI_API_KEY")
        if not keychain_key:
            import subprocess as _sp
            try:
                keychain_key = _sp.run(
                    ["security", "find-generic-password", "-a", "openai",
                     "-s", "pi-openai-official", "-w"],
                    capture_output=True, text=True, timeout=10,
                ).stdout.strip()
            except Exception:
                keychain_key = ""
        if keychain_key:
            os.environ["HERMES_AGENTS_SDK_OPENAI_API_KEY"] = keychain_key

    hermes = Path(__file__).parents[1] / "server" / "hermes_ip12"
    sys.path.insert(0, str(hermes))
    user_site = Path.home() / "Library" / "Python" / "3.9" / "lib" / "python" / "site-packages"
    if user_site.is_dir() and str(user_site) not in sys.path:
        sys.path.append(str(user_site))
    bundled_python = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "python" / "lib"
    for site_packages in bundled_python.glob("python*/site-packages"):
        if str(site_packages) not in sys.path:
            sys.path.append(str(site_packages))
    import security
    import server

    identity = {"account_id": "local_persona_v1", "username": "local-persona", "role": "admin"}
    security._token_from_request = lambda: "local-preview"
    security._validate_token = lambda _token: dict(identity)
    security.RATE_REQUESTS = 1000
    server.current_account_id = lambda: identity["account_id"]

    print("IP12 人设 Agent 本地预览：http://127.0.0.1:%s/" % args.port)
    server.app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
