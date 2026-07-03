#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黄雀 · 视频下载微服务 —— 故意独立于 content_api.py。
背景：content_api.py 被多人共改(我的采集/获客 vs 同事的豆包音频)，对方反复绕过 git 直怼服务器，
把我加进 content_api 的 /api/gen/dl 下载路由一次次覆盖掉。把下载拆成独立服务 + 独立端口 + 独立
systemd 单元后，无论 content_api 怎么被覆盖重启，下载都不受影响。

监听 127.0.0.1:8097；nginx 用 `location = /api/gen/dl` 精确路由过来(优先级高于 ^~ /api/gen/)。
无鉴权(只是公开 CDN 视频的下载代理)，但严格限定视频 CDN 域名防 SSRF。纯标准库。
"""
import re, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8097
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连，绕过环境代理(服务器有 HTTPS_PROXY 给 OpenAI 用)
ALLOW = (".zjcdn.com", ".douyinvod.com", ".douyinstatic.com", ".douyinpic.com", ".amemv.com",
         ".bytecdn.cn", ".ixigua.com", ".pstatp.com", ".snssdk.com", ".byteimg.com",
         ".xhscdn.com", ".rednotecdn.com", ".xiaohongshu.com",
         "wxapp.tc.qq.com")  # 视频号视频 CDN(加密时效直链)；防 SSRF 只放精确域名不放泛 .qq.com


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _err(self, code, msg):
        b = ('{"detail":"%s"}' % msg).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        pr = urllib.parse.urlparse(self.path)
        if pr.path != "/api/gen/dl":
            return self._err(404, "not found")
        q = urllib.parse.parse_qs(pr.query)
        url = (q.get("url", [""])[0]).strip()
        raw = ((q.get("name", ["video"])[0])[:40]) or "video"
        ascii_name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", raw).strip("_") or "video"  # header 必须 ASCII
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if not (url.startswith("http") and any(host.endswith(h) for h in ALLOW)):
            return self._err(400, "不支持的下载地址")
        try:
            up = OPENER.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=120)
        except Exception:
            return self._err(502, "下载失败(地址可能已过期，请重新爬取)")
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Disposition",
                         "attachment; filename=\"%s.mp4\"; filename*=UTF-8''%s" % (ascii_name, urllib.parse.quote(raw + ".mp4")))
        clen = up.headers.get("Content-Length")
        if clen:
            self.send_header("Content-Length", clen)
        self.end_headers()
        try:
            while True:
                c = up.read(65536)
                if not c:
                    break
                self.wfile.write(c)
        except Exception:
            pass
        finally:
            up.close()


if __name__ == "__main__":
    print("huangque-dl on 127.0.0.1:%d" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
