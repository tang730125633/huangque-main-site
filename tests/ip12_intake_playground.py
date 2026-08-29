#!/usr/bin/env python3
"""Local-only playground for tuning the IP12 intake Skill."""

import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4323)
    parser.add_argument("--data-dir", default="/tmp/ip12-intake-playground")
    args = parser.parse_args()

    os.environ["HERMES_AI_TRANSPORT"] = "codex-cli"
    os.environ["HERMES_IP12_SKILL_PIPELINE_DEFAULT"] = "v1"
    os.environ["HERMES_DATA_DIR"] = str(Path(args.data_dir).resolve())
    os.environ["HERMES_HOME"] = str(Path(args.data_dir).resolve())
    os.environ["HERMES_ENABLE_INTERNAL_TOOLS"] = "0"
    os.environ["HERMES_MASTER_AGENT_MODE"] = "off"
    os.environ["HERMES_SEMANTIC_ROUTER_MODE"] = "off"
    os.environ["HERMES_AGENT_RUNTIME_WORKER_ENABLED"] = "0"
    os.environ.setdefault("HERMES_CODEX_MODEL", "gpt-5.6-terra")
    for key in list(os.environ):
        if key.upper().endswith(("_API_KEY", "_ACCESS_TOKEN", "_SECRET")):
            os.environ.pop(key, None)

    hermes = Path(__file__).parents[1] / "server" / "hermes_ip12"
    sys.path.insert(0, str(hermes))
    import security
    import server
    from flask import render_template

    identity = {"account_id": "local_intake_lab", "username": "intake-lab", "role": "admin"}
    security._token_from_request = lambda: "local-intake-lab"
    security._validate_token = lambda _token: dict(identity)
    security.RATE_REQUESTS = 1000
    server.current_account_id = lambda: identity["account_id"]

    @server.app.route("/intake-lab")
    def intake_lab():
        return render_template("intake_lab.html")

    print("IP12 基础访谈练习场：http://127.0.0.1:%s/intake-lab" % args.port)
    server.app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
