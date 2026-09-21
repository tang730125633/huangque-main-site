#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU 渲染节点的轮询器（pull 模式）

循环：向中转器取任务 → 交给本机渲染服务 → 等完成 → 把成品回传中转器。
全部是出站请求，节点在 NAT 后也能工作。
"""
import csv
import base64
import hashlib
import http.client
import json
import math
import os
import random
import subprocess
import sys
import tempfile
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
TELEMETRY_INTERVAL = max(5.0, float(os.environ.get("NODE_TELEMETRY_SECONDS", "5")))
LOCAL_SUBMIT_RETRY_SECONDS = 30.0
LOCAL_SUBMIT_MAX_ATTEMPTS = 5


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
                    body={"node": NODE_NAME, "gpu_render": _renderer_capability()}, timeout=30)
        return out.get("job")
    except Exception as exc:
        print("[poller] claim failed: %s" % exc, flush=True)
        return None


def _renderer_capability():
    try:
        health = _call(LOCAL + "/health", LOCAL_TOKEN, timeout=5)
        value = health.get("gpu_render") if isinstance(health, dict) and health.get("ok") is True else None
        return value if isinstance(value, dict) and value.get("ready") is True else None
    except Exception:
        return None


def _gpu_snapshot():
    """读取一张 NVIDIA 卡的轻量遥测；不可用时不影响领活。"""
    try:
        process = subprocess.run([
            "nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
            "temperature.gpu,power.draw", "--format=csv,noheader,nounits",
        ], capture_output=True, text=True, timeout=3)
        if process.returncode:
            return None
        row = next(csv.reader([process.stdout.strip()], skipinitialspace=True))
        if len(row) < 6:
            return None
        values = [float(value.strip()) for value in row[1:6]]
        encoder = None
        dmon = subprocess.run(
            ["nvidia-smi", "dmon", "-s", "u", "-c", "1"],
            capture_output=True, text=True, timeout=3,
        )
        for line in dmon.stdout.splitlines():
            fields = line.split()
            if line.lstrip().startswith("#") or len(fields) < 4:
                continue
            encoder = float(fields[3])
            break
        return {
            "name": row[0].strip()[:96],
            "utilization": values[0],
            "encoder": encoder,
            "memory_used": int(values[1] * 1024 * 1024),
            "memory_total": int(values[2] * 1024 * 1024),
            "temperature": values[3],
            "power": values[4],
            "sampled_at": int(time.time()),
        }
    except (OSError, StopIteration, ValueError, subprocess.SubprocessError):
        return None


def heartbeat():
    while True:
        started = time.monotonic()
        try:
            _call(RELAY + "/v1/heartbeat", NODE_TOKEN, "POST", body={
                "node": NODE_NAME, "gpu": _gpu_snapshot(),
                "gpu_render": _renderer_capability(),
            }, timeout=8)
        except Exception as exc:
            print("[poller] telemetry failed: %s" % exc, flush=True)
        time.sleep(max(1.0, TELEMETRY_INTERVAL - (time.monotonic() - started)))


class NodeSubmissionError(RuntimeError):
    def __init__(self, status, code, reason, retryable=False, detail_hash=""):
        self.status, self.code, self.reason = status, code, reason
        self.retryable, self.detail_hash = retryable, detail_hash
        self.attempts = 1
        super().__init__()

    def __str__(self):
        return ("node_submission_failed http=%s code=%s reason=%s attempts=%s%s" % (
            self.status, self.code, self.reason, self.attempts,
            " detail_sha256=" + self.detail_hash if self.detail_hash else ""))


def _submission_http_error(error):
    try:
        raw = error.read(16385)
        value = json.loads(raw) if len(raw) <= 16384 else {}
    except Exception:
        value = {}
    finally:
        error.close()
    if not isinstance(value, dict):
        value = {}
    codes = {"material_library_unavailable", "submission_failed", "invalid_request", "unauthorized", "not_found"}
    reasons = {"probe_failed", "probe_timeout", "auth_failed", "probe_rejected", "contract_invalid",
               "queue_capacity", "disk_capacity", "admission_failed", "idempotency_conflict"}
    code = value.get("error")
    code = code if isinstance(code, str) and code in codes else "unrecognized_error"
    reason = value.get("reason_code")
    reason = reason if isinstance(reason, str) and reason in reasons else "unclassified"
    detail = value.get("detail")
    detail_hash = hashlib.sha256(detail.encode()).hexdigest() if isinstance(detail, str) else ""
    retryable = (error.code == 503 and code == "material_library_unavailable"
                 and value.get("retryable") is True and reason in {"probe_failed", "probe_timeout"})
    # Rolling deployment: old servers only provide this exact pre-admission detail.
    if error.code == 409 and code == "submission_failed" and detail == "素材库切片能力暂不可用":
        retryable, reason = True, "legacy_library_unavailable"
    return NodeSubmissionError(error.code, code, reason, retryable, detail_hash)


def _submit_local(payload, req_id):
    frozen = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    deadline = time.monotonic() + LOCAL_SUBMIT_RETRY_SECONDS
    last = NodeSubmissionError(503, "material_library_unavailable", "retry_budget_exhausted")
    for attempt in range(1, LOCAL_SUBMIT_MAX_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            return _call(LOCAL + "/v1/jobs", LOCAL_TOKEN, "POST", body=frozen,
                         headers={"X-Request-Id": req_id}, timeout=min(30, remaining))
        except urllib.error.HTTPError as error:
            last = _submission_http_error(error)
            last.attempts = attempt
            if not last.retryable or attempt == LOCAL_SUBMIT_MAX_ATTEMPTS:
                raise last from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            print("[poller] admission_wait request_id=%s http=%s reason=%s attempt=%s"
                  % (req_id, last.status, last.reason, attempt), flush=True)
            time.sleep(min(remaining, (2 ** (attempt - 1)) * random.uniform(0.8, 1.2)))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            # An acknowledgement may be lost after acceptance. Do not blindly POST again.
            raise NodeSubmissionError(0, "transport_error", type(error).__name__) from None
    raise last from None


def run_local(payload, job_id=""):
    """交给本机渲染服务，轮询到终态，返回 (result, error)。"""
    # 本机渲染服务要求 X-Request-Id（幂等键），格式必须匹配其 REQUEST_RE
    req_id = ("relay" + str(job_id))[:64]
    job = _submit_local(payload, req_id)
    jid = job.get("job_id")
    if not jid:
        return None, "本机渲染服务未返回 job_id"
    deadline = time.monotonic() + JOB_TIMEOUT
    failures = 0
    while time.monotonic() < deadline:
        time.sleep(min(3 * (2 ** min(failures, 2)), max(0, deadline - time.monotonic())))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            cur = _call(LOCAL + "/v1/jobs/" + jid, LOCAL_TOKEN, timeout=min(30, remaining))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            if not _transient_local_read(exc):
                raise RuntimeError("node_status_read_failed " + _read_error_code(exc)) from None
            failures += 1
            print("[poller] status_read_retry request_id=%s attempt=%s error=%s"
                  % (req_id, failures, _read_error_code(exc)), flush=True)
            continue
        failures = 0
        if not isinstance(cur, dict):
            raise RuntimeError("node_status_response_invalid")
        st = cur.get("status")
        if st == "completed":
            return cur.get("result") or {}, None
        if st == "failed":
            return None, str(cur.get("error") or "渲染失败")
    return None, "本机任务状态跟踪超时（未重新提交渲染）"


def _read_error_code(exc):
    # Never log a URL, HTTP body, or exception message from an authenticated read.
    return "http_%s" % exc.code if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__


def _transient_local_read(exc):
    if isinstance(exc, urllib.error.HTTPError):
        exc.close()
        return exc.code in {408, 429, 500, 502, 503, 504}
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason
    return isinstance(exc, (TimeoutError, ConnectionError, http.client.IncompleteRead,
                            http.client.RemoteDisconnected))


def _download_local_result(url):
    """Retry only idempotent local GETs, never rendering or the Relay upload POST."""
    deadline = time.monotonic() + RENDER_TIMEOUT + 120
    for attempt in range(1, 4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            return _call(url, LOCAL_TOKEN, timeout=remaining)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            if not _transient_local_read(exc) or attempt == 3:
                raise RuntimeError("node_result_read_failed " + _read_error_code(exc)) from None
            print("[poller] result_read_retry attempt=%s error=%s"
                  % (attempt, _read_error_code(exc)), flush=True)
            time.sleep(min(attempt * 2, max(0, deadline - time.monotonic())))
    raise RuntimeError("node_result_read_deadline")


def _image_to_video(data, content_type, duration):
    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
        str(content_type or "").lower()
    )
    seconds = float(duration or 0)
    if not suffix or not math.isfinite(seconds) or not 7 <= seconds <= 20:
        raise RuntimeError("用户图片转视频参数无效")
    frames = math.ceil((seconds + 0.2) * 30)
    with tempfile.TemporaryDirectory(prefix="hq-user-image-") as temp:
        source = os.path.join(temp, "source" + suffix)
        output = os.path.join(temp, "clip.mp4")
        with open(source, "wb") as handle:
            handle.write(data)
        process = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-loop", "1", "-i", source, "-map", "0:v:0", "-an", "-vf",
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,setsar=1,fps=30,format=yuv420p",
            "-frames:v", str(frames), "-c:v", "libx264", "-preset", "fast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-threads", "2",
            "-map_metadata", "-1", "-movflags", "+faststart", output,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        if process.returncode or not os.path.isfile(output) or os.path.getsize(output) < 1024:
            raise RuntimeError("用户图片转视频失败")
        with open(output, "rb") as handle:
            return handle.read()


def sync_user_assets(payload, job_id):
    """把本任务引用的用户素材从中转器拉到当前渲染节点。"""
    for item in payload.get("user_materials") or []:
        sha = str(item.get("sha256") or "").strip().lower()
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise RuntimeError("任务包含无效的用户素材校验值")
        req = urllib.request.Request(
            RELAY + "/v1/job-assets/" + job_id + "/" + sha,
            headers={"Authorization": "Bearer " + NODE_TOKEN, "X-HQ-Node": NODE_NAME},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0]
            data = resp.read()
        if item.get("media_type") == "image":
            data = _image_to_video(data, content_type, payload.get("duration"))
            sha = hashlib.sha256(data).hexdigest()
            content_type = "video/mp4"
            item.update({
                "sha256": sha, "media_type": "video", "clip_start_seconds": 0,
            })
        _call(
            LOCAL + "/v1/user-assets", LOCAL_TOKEN, "POST", raw=data,
            headers={"Content-Type": content_type, "X-HQ-Asset-Sha256": sha},
            timeout=120,
        )


def report(job_id, ok, result=None, error=None):
    _call(RELAY + "/v1/report", NODE_TOKEN, "POST",
          body={"job_id": job_id, "ok": ok, "result": result, "error": error, "node": NODE_NAME}, timeout=30)


def upload_result(job_id, file_url, result):
    """下载本机成品并回传中转器。"""
    full = file_url if file_url.startswith("http") else LOCAL + file_url
    data = _download_local_result(full)
    headers = {
        "Content-Type": "video/mp4",
        "X-HQ-Duration": str(result.get("duration") or 0),
        "X-HQ-Width": str(result.get("width") or 1080),
        "X-HQ-Height": str(result.get("height") or 1920),
        "X-HQ-Template": str(result.get("template_id") or ""),
        "X-HQ-Engine": str(result.get("engine") or ""),
        "X-HQ-Node": NODE_NAME,
    }
    if isinstance(result.get("gpu_render"), dict):
        headers["X-HQ-GPU-Render"] = base64.b64encode(
            json.dumps(result["gpu_render"], separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
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
            sync_user_assets(payload, jid)
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
    threads = [threading.Thread(target=heartbeat, daemon=True)]
    threads[0].start()
    for slot in range(1, CONCURRENCY + 1):
        t = threading.Thread(target=worker, args=(slot,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
