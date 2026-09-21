#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纳米香蕉2 三条候选渠道的真实可用性测试。

对每条候选：发布映射(走后台同一接口) → 真实下单 → 轮询 → 记录渠道绑定与成品。
测试结束后恢复原始映射。
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = "https://huangquechuanmei.com"
USER = "yuelei"
PASS = os.environ.get("HQ_TEST_PASS", "")
OP = "image.banana.nb2.text"
REPORT = []

ctx = ssl.create_default_context()


def call(method, path, body=None, cookie=None, timeout=90):
    r = urllib.request.Request(BASE + path, data=(json.dumps(body).encode() if body is not None else None), method=method)
    r.add_header("Content-Type", "application/json")
    r.add_header("User-Agent", "hq-acceptance/1.0")
    if cookie:
        r.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "ERR %s: %s" % (type(e).__name__, e)


def login():
    s, b = call("POST", "/api/auth/login", {"username": USER, "password": PASS})
    if s != 200:
        print("登录失败 HTTP %s %s" % (s, b[:200]))
        sys.exit(1)
    r = urllib.request.Request(BASE + "/api/auth/me")
    # 用 urllib 拿 Set-Cookie
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(BASE + "/api/auth/login",
                                 data=json.dumps({"username": USER, "password": PASS}).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    with op.open(req, timeout=60) as resp:
        resp.read()
    ck = "; ".join("%s=%s" % (c.name, c.value) for c in cj)
    return ck


def admin(path, body, cookie):
    return call("POST", path, body, cookie=cookie, timeout=90)


def get_mapping(cookie):
    s, b = call("GET", "/api/admin/channel-manager", cookie=cookie, timeout=120)
    if s != 200:
        return None
    d = json.loads(b)
    for m in d.get("operation_mappings") or []:
        if m.get("operation_id") == OP:
            return m
    return None


def publish(channels, cookie, rev, state="managed"):
    body = {"operation_id": OP, "state": state, "channels": channels, "expected_revision": rev}
    s, b = admin("/api/admin/channel-manager/operation-mapping", body, cookie)
    return s, b


def create_task(cookie, tag):
    payload = {
        "provider": "banana",
        "model": "nb2",
        "prompt": "一张纯白背景上的红色圆点，极简风格，用于渠道可用性测试，无文字",
        "ratio": "1:1",
        "quality": "std",
        "count": 1,
        "source_page": "banana",
    }
    s, b = call("POST", "/api/gen/banana", payload, cookie=cookie, timeout=180)
    try:
        d = json.loads(b)
    except Exception:
        d = {"raw": b[:300]}
    return s, d


def poll(cookie, job_id, limit=60):
    for _ in range(limit):
        s, b = call("GET", "/api/gen/job/%s" % job_id, cookie=cookie, timeout=60)
        if s == 200:
            try:
                d = json.loads(b)
            except Exception:
                d = {}
            st = str(d.get("status") or d.get("state") or "")
            if st in ("done", "error", "failed", "canceled", "refunded"):
                return st, d
        time.sleep(6)
    return "timeout", {}


def main():
    cookie = login()
    print("已登录 %s" % USER)

    m0 = get_mapping(cookie)
    print("原始映射: state=%s r%s channels=%s" % (m0.get("state"), m0.get("revision"), m0.get("channels")))
    rev = int(m0.get("revision") or 0)

    cases = [
        ("乐创 GPT Image 2 生图", "managed", ["xlw-image-2"]),
        ("乐创 GPT Image 2.5 生图", "managed", ["xlw-image-25"]),
        ("官方 Gemini（原厂线路）", "legacy", []),
    ]

    for name, state, chans in cases:
        print()
        print("=" * 66)
        print("候选: %s   state=%s channels=%s" % (name, state, chans))
        s, b = publish(chans, cookie, rev, state)
        print("  发布映射: HTTP %s %s" % (s, b[:160]))
        if s != 200:
            REPORT.append((name, "发布失败", "", "", "", ""))
            continue
        rev += 1
        m = get_mapping(cookie)
        print("  服务端确认: r%s state=%s channels=%s" % (m.get("revision"), m.get("state"), m.get("channels")))

        s, d = create_task(cookie, name)
        print("  下单: HTTP %s  %s" % (s, json.dumps(d, ensure_ascii=False)[:220]))
        job_id = d.get("job_id") or d.get("id")
        if not job_id:
            REPORT.append((name, "下单失败", "", "", "", ""))
            continue
        st, dd = poll(cookie, job_id)
        bind = ""
        # 从任务详情里取渠道绑定
        s, b = call("GET", "/api/gen/job/%s" % job_id, cookie=cookie, timeout=60)
        try:
            jd = json.loads(b)
            bind = json.dumps(jd.get("channel") or jd.get("_channel_binding") or jd.get("provider") or "", ensure_ascii=False)[:80]
            files = jd.get("files") or jd.get("urls") or []
        except Exception:
            bind, files = "", []
        print("  任务 %s 状态=%s  绑定=%s" % (job_id, st, bind))
        print("  成品: %s" % (str(files)[:160]))
        REPORT.append((name, "HTTP %s" % s, job_id, st, bind, str(files)[:60]))
        time.sleep(3)

    print()
    print("=" * 66)
    print("恢复原始映射")
    s, b = publish(m0.get("channels") or [], cookie, rev, m0.get("state") or "managed")
    print("  HTTP %s %s" % (s, b[:160]))


if __name__ == "__main__":
    main()
