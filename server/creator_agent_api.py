#!/usr/bin/env python3
"""Entrypoint for the standalone Huangque Creator Agent service."""

import argparse
import os

from creator_agent.service import serve


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("CREATOR_AGENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CREATOR_AGENT_PORT", "8114")))
    args = parser.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
