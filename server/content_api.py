#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Huangque content API entrypoint.

Business logic lives under ``content_domains``. This file intentionally stays
small so systemd keeps launching ``content_api.py`` while domain code can be
reviewed and evolved independently.
"""

import threading
from http.server import ThreadingHTTPServer

try:
    from .content_domains import core, registry
except ImportError:  # Running as /home/ubuntu/content-api/content_api.py
    from content_domains import core, registry


PORT = core.PORT
HANDLERS = registry.HANDLERS

# Keep the legacy core handler behavior while exposing the domain-assembled
# handler registry to request handling and health responses.
core.HANDLERS = HANDLERS


def main():
    core.init_db()
    core.start_job_workers()
    threading.Thread(target=core.reaper, daemon=True).start()
    print("huangque-content-api on 127.0.0.1:%d  caps=%s" % (PORT, list(HANDLERS)))
    ThreadingHTTPServer(("127.0.0.1", PORT), core.H).serve_forever()


if __name__ == "__main__":
    main()
