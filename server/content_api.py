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
    from .content_domains import core, digital_presenter, registry, video_compose
except ImportError:  # Running as /home/ubuntu/content-api/content_api.py
    from content_domains import core, digital_presenter, registry, video_compose


PORT = core.PORT
HANDLERS = registry.HANDLERS

# Keep the legacy core handler behavior while exposing the domain-assembled
# handler registry to request handling and health responses.
core.HANDLERS = HANDLERS
DigitalPresenterH = digital_presenter.make_handler(core.H, core)


class H(DigitalPresenterH):
    def _dispatch_video_compose(self, method):
        return video_compose.dispatch_http(
            self, method, core.verify, core._must_change_password, core.adb,
            core._resolve_out_file, core.OUT_DIR,
        )

    def do_POST(self):
        if self._dispatch_video_compose("POST"):
            return
        return super().do_POST()

    def do_GET(self):
        if self._dispatch_video_compose("GET"):
            return
        return super().do_GET()


def main():
    core.init_db()
    digital_presenter.init_db(core.jdb)
    # 回收上次遗留的 running 孤儿 → 秒退点。
    # 优雅停机（drain）之后这里应该【一条都收不到】—— 收到就说明上次是崩溃/被 SIGKILL 了，
    # 它现在是【兜底】，不再是常态。常态下的部署不该再产生孤儿。
    core.reclaim_orphaned_running()
    core.start_job_workers()
    threading.Thread(target=core.reaper, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    core._http_server = srv          # drain 时要 shutdown 它，停止收新提交
    core.install_signal_handlers()   # SIGTERM → 停止收活 → 等在飞的跑完 → 退出

    print("huangque-content-api on 127.0.0.1:%d  caps=%s  drain=%ds"
          % (PORT, list(HANDLERS), core.DRAIN_TIMEOUT))
    srv.serve_forever()


if __name__ == "__main__":
    main()
