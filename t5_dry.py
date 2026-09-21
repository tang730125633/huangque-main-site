#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读预检：确认后台接口路径、当前映射、任务查询接口。不发布映射、不下单。"""
import http.cookiejar
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

BASE = "https://huangquechuanmei.com"
USER = "yuelei"
PASS = os.environ.get("HQ_TEST_PASS", "")
ctx = ssl.create_default_context()


def opener_with_cookie():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                     urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(BASE + "/api/auth/login",
                                 data=json.dumps({"username": USER, "password": PASS}).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    with op.open(req, timeout=60) as resp:
        body = json.loads(resp.read().decode())
    return op, cj, body


def get(op, path, timeout=120):
    r = urllib.request.Request(BASE + path)
    r.add_header("User-Agent", "hq-acceptance/1.0")
    try:
        with op.open(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "ERR %s: %s" % (type(e).__name__, e)


op, cj, login_body = opener_with_cookie()
u = login_body.get("user") or {}
print("登录: username=%s role=%s points=%s" % (u.get("username"), u.get("role"), u.get("points")))
print("Cookie 项:", [c.name for c in cj])

print()
print("=== 1. 后台渠道总览接口 ===")
s, b = get(op, "/api/admin/channel-manager")
print("   GET /api/admin/channel-manager → HTTP %s  %d 字节" % (s, len(b)))
if s == 200:
    d = json.loads(b)
    print("   顶层键:", sorted(d.keys()))
    for m in d.get("operation_mappings") or []:
        print("   映射 %-24s state=%-8s r%-3s channels=%s" % (
            m.get("operation_id"), m.get("state"), m.get("revision"), m.get("channels")))
else:
    print("   %s" % b[:200])

print()
print("=== 2. 任务查询接口 ===")
for p in ("/api/gen/job/9719", "/api/gen/job/1", "/api/job/9719"):
    s, b = get(op, p)
    print("   %-22s HTTP %s  %s" % (p, s, b[:120]))

print()
print("=== 3. 生图接口（HEAD 式探测，用空 body 看校验反应）===")
r = urllib.request.Request(BASE + "/api/gen/banana", data=b"{}", method="POST")
r.add_header("Content-Type", "application/json")
try:
    with op.open(r, timeout=60) as resp:
        print("   HTTP %s %s" % (resp.status, resp.read().decode()[:200]))
except urllib.error.HTTPError as e:
    print("   HTTP %s %s" % (e.code, e.read().decode()[:240]))
except Exception as e:
    print("   ERR %s %s" % (type(e).__name__, e))
