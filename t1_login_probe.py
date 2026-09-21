#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：确认测试账号可登录主站、余额充足、并定位生图入口。"""
import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://huangquechuanmei.com"
USER = "yuelei"
PASS = os.environ.get("HQ_TEST_PASS", "")

ctx = ssl.create_default_context()


def req(method, path, body=None, cookie=None, timeout=40):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    r.add_header("User-Agent", "hq-acceptance/1.0")
    if cookie:
        r.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers
    except Exception as e:
        return None, "ERR %s: %s" % (type(e).__name__, e), None


print("=== 1. 未带凭据访问 /api/auth/me（应 401）===")
s, b, _ = req("GET", "/api/auth/me")
print("   HTTP %s  %s" % (s, b[:120]))

print()
print("=== 2. 登录 %s ===" % USER)
s, b, h = req("POST", "/api/auth/login", {"username": USER, "password": PASS})
print("   HTTP %s" % s)
print("   响应: %s" % b[:200])
cookie = None
if h:
    sc = h.get_all("Set-Cookie") or []
    if sc:
        cookie = "; ".join(x.split(";")[0] for x in sc)
        names = [x.split("=")[0] for x in sc]
        print("   Set-Cookie 项: %s" % names)
    else:
        print("   （无 Set-Cookie）")

if not cookie:
    print("   ❌ 登录未拿到 Cookie，无法继续")
    sys.exit(1)

print()
print("=== 3. 带 Cookie 再查 /api/auth/me ===")
s, b, _ = req("GET", "/api/auth/me", cookie=cookie)
print("   HTTP %s" % s)
try:
    d = json.loads(b)
    u = d.get("user") or d
    for k in ("username", "role", "points", "balance", "credits"):
        if k in u:
            print("   %-10s = %s" % (k, u[k]))
    print("   全部键:", sorted(u.keys())[:24])
except Exception:
    print("   %s" % b[:200])

print()
print("=== 4. 点数余额 ===")
for p in ("/api/me/points", "/api/user/points", "/api/points", "/api/me/balance", "/api/user/profile"):
    s, b, _ = req("GET", p, cookie=cookie)
    if s == 200:
        print("   %-24s HTTP 200  %s" % (p, b[:160]))
        break
    else:
        print("   %-24s HTTP %s" % (p, s))

print()
print("=== 5. 生图相关入口（探测）===")
for p in ("/api/gen/banana/health", "/api/gen/models", "/api/gen/banana"):
    s, b, _ = req("GET", p, cookie=cookie)
    print("   %-26s HTTP %s  %s" % (p, s, b[:150]))
