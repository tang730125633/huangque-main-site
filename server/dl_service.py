#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黄雀 · 视频下载微服务 —— 故意独立于 content_api.py。
监听 127.0.0.1:8097；nginx 用 `location = /api/gen/dl` 精确路由过来(优先级高于 ^~ /api/gen/)。
要求有效登录态，并严格限定视频 CDN 域名防 SSRF。纯标准库。

视频号(wxapp.tc.qq.com)特殊：直链是加密流，需调 :3001 Isaac64 解密服务(传 decode_key)解成可播放 mp4。
前端对视频号下载会带 &dk=<decode_key>，代理识别后走解密路径。
"""
import os, re, json, tempfile, urllib.request, urllib.parse, subprocess
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8097
AUTH_BASE = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095")
AUTH_COOKIE_NAME = os.environ.get("HQ_AUTH_COOKIE_NAME", "hq_session")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连，绕过环境代理
ALLOW = (
    ".zjcdn.com", ".douyinvod.com", ".douyinstatic.com", ".douyinpic.com", ".amemv.com",
    ".bytecdn.cn", ".ixigua.com", ".pstatp.com", ".snssdk.com", ".byteimg.com",
    ".xhscdn.com", ".rednotecdn.com", ".xiaohongshu.com",
    ".bytedance.net", ".lf-douyin.com", ".365yg.com",
    ".cos.ap-guangzhou.myqcloud.com",  # 采集视频转存 COS 后的直链下载(COS-COLLECT #113)
    "video.huangquechuanmei.com",  # 采集视频转存 COS 后走 CDN 域名(COS_DOMAIN)的直链下载
    "wxapp.tc.qq.com",
)  # 抖音(TikHub play_addr)/小红书/视频号 等直链 CDN 域名；防 SSRF。覆盖 collect 解析 + 生成产出下载。
DECRYPT_API = "http://127.0.0.1:3001/api/decrypt"  # Isaac64 WASM 解密服务(Evil0ctal)
MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}


def content_type_ext(headers):
    ctype = (headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0].strip().lower()
    return ctype or "application/octet-stream", MIME_EXT.get(ctype, ".mp4")


def verify_token(token):
    if not token:
        return False
    try:
        req = urllib.request.Request(AUTH_BASE + "/api/auth/me",
                                     headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=6) as r:
            return bool(json.loads(r.read()).get("user"))
    except Exception:
        return False

def request_token(headers):
    auth = headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        parts = auth.split(None, 1)
        token = parts[1].strip() if len(parts) == 2 else ""
        if token and token != "__cookie__":
            return token
    try:
        jar = cookies.SimpleCookie()
        jar.load(headers.get("Cookie") or "")
        morsel = jar.get(AUTH_COOKIE_NAME)
        return morsel.value.strip() if morsel and morsel.value else ""
    except Exception:
        return ""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _err(self, code, msg):
        b = ('{"detail":"%s"}' % msg).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _stream_decrypt(self, url, dk, ascii_name, raw):
        """视频号：下载加密流 → 调 :3001 解密 → 流回解密 mp4。"""
        enc = tempfile.NamedTemporaryFile(suffix=".enc", delete=False)
        try:
            up = OPENER.open(urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://channels.weixin.qq.com/"}), timeout=180)
            while True:
                c = up.read(65536)
                if not c:
                    break
                enc.write(c)
            up.close()
            enc.close()
            # 调解密服务(multipart: decode_key + video 文件) → 直接 curl 流式返回，避免占内存
            r = subprocess.run(
                ["curl", "-sS", "-X", "POST", DECRYPT_API,
                 "-F", "decode_key=" + dk,
                 "-F", "video=@%s" % enc.name,
                 "-o", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
            if r.returncode != 0 or not r.stdout:
                return self._err(502, "解密失败:" + (r.stderr.decode("u8", "ignore")[:80] or "解密服务无响应"))
            data = r.stdout
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition",
                             "attachment; filename=\"%s.mp4\"; filename*=UTF-8''%s" % (ascii_name, urllib.parse.quote(raw + ".mp4")))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            try:
                return self._err(502, "视频号下载失败:" + str(e)[:80])
            except Exception:
                pass
        finally:
            try:
                os.unlink(enc.name)
            except Exception:
                pass

    def do_GET(self):
        pr = urllib.parse.urlparse(self.path)
        if pr.path != "/api/gen/dl":
            return self._err(404, "not found")
        token = request_token(self.headers)
        if not verify_token(token):
            return self._err(401, "未登录")
        q = urllib.parse.parse_qs(pr.query)
        url = (q.get("url", [""])[0]).strip()
        raw = ((q.get("name", ["video"])[0])[:40]) or "video"
        dk = (q.get("dk", [""])[0]).strip()
        ascii_name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", raw).strip("_") or "video"
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        allowed_host = any(host == h or (h.startswith(".") and host.endswith(h)) for h in ALLOW)
        if not (url.startswith("http") and allowed_host):
            return self._err(400, "不支持的下载地址")
        # 视频号加密流：必须走解密路径(有 dk 才能解)
        if host == "wxapp.tc.qq.com":
            if not dk:
                return self._err(400, "视频号下载缺少解密密钥(请重新爬取获取新链接)")
            return self._stream_decrypt(url, dk, ascii_name, raw)
        try:
            up = OPENER.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=120)
        except Exception:
            return self._err(502, "下载失败(地址可能已过期，请重新爬取)")
        ctype, ext = content_type_ext(up.headers)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition",
                         "attachment; filename=\"%s%s\"; filename*=UTF-8''%s" % (ascii_name, ext, urllib.parse.quote(raw + ext)))
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
