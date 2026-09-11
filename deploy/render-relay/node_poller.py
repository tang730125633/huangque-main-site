#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU 渲染节点的轮询器（pull 模式）

循环：向中转器取任务 → 交给本机渲染服务 → 等完成 → 把成品回传中转器。
全部是出站请求，节点在 NAT 后也能工作。
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

RELAY = os.environ["NODE_RELAY_URL"].rstrip("/")
NODE_TOKEN = os.environ["NODE_RELAY_TOKEN"].strip()
NODE_NAME = os.environ.get("NODE_NAME", "gpu-node").strip()
LOCAL = os.environ.get("NODE_LOCAL_RENDER", "http://127.0.0.1:8212").rstrip("/")
LOCAL_TOKEN = os.environ["NODE_LOCAL_TOKEN"].strip()
POLL_IDLE = float(os.environ.get("NODE_POLL_IDLE_SECONDS", "5"))
# 并发度：同时进行「领取→渲染→回传」的条数。应与渲染服务的
# MATRIX_TEMPLATE_HYPERFRAMES_CONCURRENCY 一致，超了只会互相排队反而更慢。
CONCURRENCY = max(1, int(os.environ.get("NODE_CONCURRENCY", "2")))
JOB_TIMEOUT = float(os.environ.get("NODE_JOB_TIMEOUT_SECONDS", "1800"))
RENDER_TIMEOUT = float(os.environ.get("NODE_RENDER_TIMEOUT_SECONDS", "900"))


def _call(url, token, method="GET", body=None, raw=None, headers=None, timeout=60):
    h = {"Authorization": "Bearer " + token}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    elif raw is not None:
        data = raw
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ct = resp.headers.get("Content-Type") or ""
        payload = resp.read()
        return json.loads(payload) if "json" in ct else payload


def claim():
    try:
        out = _call(RELAY + "/v1/claim", NODE_TOKEN, "POST",
                    body={"node": NODE_NAME}, timeout=30)
        return out.get("job")
    except Exception as exc:
        print("[poller] claim failed: %s" % exc, flush=True)
        return None


def run_local(payload, job_id=""):
    """交给本机渲染服务，轮询到终态，返回 (result, error)。"""
    # 本机渲染服务要求 X-Request-Id（幂等键），格式必须匹配其 REQUEST_RE
    req_id = ("relay" + str(job_id))[:64]
    job = _call(LOCAL + "/v1/jobs", LOCAL_TOKEN, "POST", body=payload,
                headers={"X-Request-Id": req_id}, timeout=30)
    jid = job.get("job_id")
    if not jid:
        return None, "本机渲染服务未返回 job_id"
    deadline = time.time() + JOB_TIMEOUT
    while time.time() < deadline:
        time.sleep(3)
        cur = _call(LOCAL + "/v1/jobs/" + jid, LOCAL_TOKEN, timeout=30)
        st = cur.get("status")
        if st == "completed":
            return cur.get("result") or {}, None
        if st == "failed":
            return None, str(cur.get("error") or "渲染失败")
    return None, "本机渲染超时"


def report(job_id, ok, result=None, error=None):
    _call(RELAY + "/v1/report", NODE_TOKEN, "POST",
          body={"job_id": job_id, "ok": ok, "result": result, "error": error}, timeout=30)


def upload_result(job_id, file_url, result):
    """下载本机成品并回传中转器。"""
    full = file_url if file_url.startswith("http") else LOCAL + file_url
    data = _call(full, LOCAL_TOKEN, timeout=RENDER_TIMEOUT + 120)
    headers = {
        "Content-Type": "video/mp4",
        "X-HQ-Duration": str(result.get("duration") or 0),
        "X-HQ-Width": str(result.get("width") or 1080),
        "X-HQ-Height": str(result.get("height") or 1920),
        "X-HQ-Template": str(result.get("template_id") or ""),
        "X-HQ-Engine": str(result.get("engine") or ""),
    }
    return _call(RELAY + "/v1/result/" + job_id, NODE_TOKEN, "POST",
                 raw=data, headers=headers, timeout=RENDER_TIMEOUT + 120)


def worker(slot):
    """一个并发槽位：独立地「领取→渲染→回传」，循环不停。"""
    while True:
        job = claim()
        if not job:
            time.sleep(POLL_IDLE)
            continue
        jid = job.get("job_id")
        payload = job.get("payload") or {}
        print("[poller] #%d 领取 %s template=%s" % (slot, jid, payload.get("template_id")), flush=True)
        started = time.time()
        try:
            result, error = run_local(payload, jid)
            if error:
                print("[poller] #%d 渲染失败 %s: %s" % (slot, jid, error[:150]), flush=True)
                report(jid, False, error=error)
                continue
            # /v1/result 已落盘并标记完成（file_url 由中转器生成，指向它自己），
            # 绝不能再调 /v1/report 覆盖——那会用节点本地路径盖掉对外的正确地址。
            out = upload_result(jid, result.get("file_url") or "", result)
            report(jid, True, result=result)
            print("[poller] #%d 完成 %s，用时 %.1fs，COS=%s"
                  % (slot, jid, time.time() - started, out.get("cos_uploaded")), flush=True)
        except Exception as exc:
            print("[poller] #%d 异常 %s: %s" % (slot, jid, exc), flush=True)
            try:
                report(jid, False, error=str(exc)[:300])
            except Exception:
                pass


def main():
    print("[poller] node=%s relay=%s local=%s 并发=%d"
          % (NODE_NAME, RELAY, LOCAL, CONCURRENCY), flush=True)
    threads = []
    for slot in range(1, CONCURRENCY + 1):
        t = threading.Thread(target=worker, args=(slot,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
