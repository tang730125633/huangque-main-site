#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU 渲染节点的轮询器（pull 模式）

循环：向中转器取任务 → 交给本机渲染服务 → 等完成 → 把成品回传中转器。
全部是出站请求，节点在 NAT 后也能工作。
"""
import csv
import base64
import hashlib
import hmac
import http.client
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from contextlib import contextmanager

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
                    body={"node": NODE_NAME, "gpu_render": _renderer_capability(),
                          "delivery_protocol": 2,"slots":CONCURRENCY}, timeout=30)
        job = out.get("job")
        if job and not re.fullmatch('[a-f0-9]{32}', str(job.get('claim_token') or '')):
            raise RuntimeError('durable_claim_not_supported')
        return job
    except Exception as exc:
        print("[poller] claim failed: %s" % exc, flush=True)
        return None


def _renderer_capability():
    try:
        health = _call(LOCAL + "/health", LOCAL_TOKEN, timeout=5)
        value = health.get("gpu_render") if isinstance(health, dict) and health.get("ok") is True else None
        if isinstance(value, dict) and value.get("ready") is True:
            return dict(value, text_style_delivery_protocol=2) if value.get("text_style_contract") else value
        return None
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


def run_local(payload, job_id="", *, local_id=None, on_accepted=None, started_at=None, durable=False):
    """交给本机渲染服务，轮询到终态，返回 (result, error)。"""
    # 本机渲染服务要求 X-Request-Id（幂等键），格式必须匹配其 REQUEST_RE
    req_id = ("relay" + str(job_id))[:64]
    job = {'job_id':local_id} if local_id else _submit_local(payload, req_id)
    jid = job.get("job_id")
    if not jid:
        if durable:raise RuntimeError('local_submission_unconfirmed')
        return None, "本机渲染服务未返回 job_id"
    if on_accepted:
        on_accepted(jid)
    remaining_budget = max(0, JOB_TIMEOUT - max(0, time.time() - started_at)) if started_at else JOB_TIMEOUT
    deadline = time.monotonic() + remaining_budget
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
    if durable:
        # After the execution budget, read the existing job once per recovery pass.
        # Unknown/running is NOT proof of a failed render and must not trigger a new POST.
        cur = _call(LOCAL + '/v1/jobs/' + jid, LOCAL_TOKEN, timeout=30)
        if cur.get('status') == 'completed': return cur.get('result') or {}, None
        if cur.get('status') == 'failed': return None, str(cur.get('error') or 'render_failed')
        raise RuntimeError('local_completion_unknown')
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
            if isinstance(data, bytes):
                handle.write(data)
            else:
                shutil.copyfileobj(data, handle, 64 * 1024)
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
        with tempfile.TemporaryFile() as source:
            total, digest = 0, hashlib.sha256()
            with urllib.request.urlopen(req, timeout=3600) as resp:
                content_type = (resp.headers.get("Content-Type") or "").split(";")[0]
                while chunk := resp.read(64 * 1024):
                    total += len(chunk)
                    if total > 2 * 1024 * 1024 * 1024:
                        raise RuntimeError("素材超出账号传输空间预算")
                    source.write(chunk)
                    digest.update(chunk)
            if not total or not hmac.compare_digest(digest.hexdigest(), sha):
                raise RuntimeError("用户素材传输校验失败")
            source.seek(0)
            data = source
            if item.get("media_type") == "image":
                data = _image_to_video(source, content_type, payload.get("duration"))
                sha = hashlib.sha256(data).hexdigest()
                total = len(data)
                content_type = "video/mp4"
                item.update({
                    "sha256": sha, "media_type": "video", "clip_start_seconds": 0,
                })
            _call(
                LOCAL + "/v1/user-assets", LOCAL_TOKEN, "POST", raw=data,
                headers={"Content-Type": content_type, "X-HQ-Asset-Sha256": sha,
                         "Content-Length": str(total)},
                timeout=3600,
            )


def report(job_id, ok, result=None, error=None, claim_token=None):
    _call(RELAY + "/v1/report", NODE_TOKEN, "POST",
          body={"job_id": job_id, "ok": ok, "result": result, "error": error, "node": NODE_NAME,
                **({'claim_token':claim_token} if claim_token else {})}, timeout=30)


def upload_result(job_id, file_url, result, *, claim_token=None, data=None):
    """下载本机成品并回传中转器。"""
    full = file_url if file_url.startswith("http") else LOCAL + file_url
    data = _download_local_result(full) if data is None else data
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
    if claim_token:
        headers.update({'X-HQ-Claim-Token':claim_token,'X-HQ-Artifact-Sha256':hashlib.sha256(data).hexdigest()})
    return _call(RELAY + "/v1/result/" + job_id, NODE_TOKEN, "POST",
                 raw=data, headers=headers, timeout=RENDER_TIMEOUT + 120)


class DeliveryStore:
    """Private atomic outbox. Payload/result never go into logs or source control."""
    def __init__(self, root):
        if Path(root).is_symlink():raise ValueError('delivery_root_linked')
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if os.name != 'nt': os.chmod(self.root,0o700)
        self.finished=self.root/'finished'
        if self.finished.is_symlink():raise ValueError('delivery_archive_linked')
        self.finished.mkdir(exist_ok=True,mode=0o700)
        self.lock = threading.RLock()
        self.active = set()
        self.retry_at = {}
        self.claim_lock = threading.Lock()

    def path(self, jid, suffix='.json'):
        if not re.fullmatch('[a-f0-9]{32}',jid): raise ValueError('delivery_id_invalid')
        p = self.root/(jid+suffix)
        if p.is_symlink(): raise ValueError('delivery_path_linked')
        return p

    def write(self, path, data):
        fd, name = tempfile.mkstemp(prefix='.delivery-',dir=self.root)
        try:
            with os.fdopen(fd,'wb') as f:
                f.write(data); f.flush(); os.fsync(f.fileno())
            os.replace(name,path)
        finally:
            if os.path.exists(name): os.unlink(name)

    def get(self,jid):
        with self.lock:
            p=self.path(jid)
            if not p.exists():p=self.finished/p.name
            if p.is_symlink():raise ValueError('delivery_path_linked')
            return json.loads(p.read_text(encoding='utf-8'))

    def identity(self,token,payload):
        return {'token_sha256':hashlib.sha256(token.encode()).hexdigest(),
                'payload_sha256':hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()}

    def ensure(self,job):
        jid=job['job_id']; token=job['claim_token']
        if not re.fullmatch('[a-f0-9]{32}',token): raise ValueError('delivery_token_invalid')
        with self.lock:
            if self.path(jid).exists() or (self.finished/(jid+'.json')).exists():
                old=self.get(jid)
                previous=old.get('identity') if old['phase']=='complete' else self.identity(old['claim_token'],old['payload'])
                if previous != self.identity(token,job['payload']):
                    raise ValueError('delivery_identity_conflict')
                return
            value={'job_id':jid,'claim_token':token,'payload':job['payload'],'phase':'claimed','started_at':time.time(),'result':{}}
            self.write(self.path(jid),json.dumps(value,ensure_ascii=False,allow_nan=False).encode())

    def update(self,jid,**fields):
        with self.lock:
            r=self.get(jid);r.update(fields)
            if 'phase' in fields:
                r.setdefault('phase_times',{}).setdefault(fields['phase'],time.time())
            if r['phase']=='complete':
                compact={'job_id':jid,'phase':'complete','phase_times':r.get('phase_times',{}),
                         'identity':r.get('identity') or self.identity(r['claim_token'],r['payload'])}
                self.write(self.finished/(jid+'.json'),json.dumps(compact).encode())
                self.path(jid).unlink(missing_ok=True)
                return compact
            self.write(self.path(jid),json.dumps(r,ensure_ascii=False,allow_nan=False).encode())
            return r

    def next_ready(self):
        with self.lock:
            for p in sorted(self.root.glob('*.json')):
                jid=p.stem
                if jid in self.active or self.retry_at.get(jid,0)>time.monotonic():continue
                r=self.get(jid)
                if r['phase']=='complete':continue
                self.active.add(jid);return r
        return None

    def release(self,jid):
        with self.lock:
            self.active.discard(jid);self.retry_at[jid]=time.monotonic()+10

    def outstanding(self):
        with self.lock:
            return sum(self.get(p.stem)['phase']!='complete' for p in self.root.glob('*.json'))

    @contextmanager
    def process_lock(self):
        path=self.root/'poller.lock'
        if path.is_symlink():raise ValueError('delivery_lock_linked')
        with path.open('a+b') as f:
            f.seek(0);f.write(b'0');f.flush();f.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield


def delivery_status(record):
    return _call(RELAY+'/v1/delivery-status',NODE_TOKEN,'POST',body={
        'node':NODE_NAME,'job_id':record['job_id'],'claim_token':record['claim_token']},timeout=30)


def process_delivery(store,record):
    jid=record['job_id'];token=record['claim_token']
    remote=delivery_status(record)
    if remote['status']=='failed':
        store.update(jid,phase='complete');return
    if remote['status']=='completed':
        if record.get('sha256') and remote.get('artifact_sha256') and record['sha256']!=remote['artifact_sha256']:
            raise RuntimeError('delivery_remote_artifact_conflict')
        if not remote.get('metadata_done'):
            report(jid,True,result=record.get('result') or {},claim_token=token)
        store.update(jid,phase='complete')
        store.path(jid,'.mp4').unlink(missing_ok=True)
        return
    if record['phase']=='claimed':
        payload=json.loads(json.dumps(record['payload']))
        try:
            sync_user_assets(payload, jid)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code not in {400,401,403,404,410,422}:raise
            record=store.update(jid,phase='failed',error='material_prepare_rejected_http_%s'%exc.code)
        except (RuntimeError,ValueError,subprocess.SubprocessError) as exc:
            record=store.update(jid,phase='failed',error='material_prepare_failed_%s'%type(exc).__name__)
        else:
            record=store.update(jid,phase='prepared',prepared_payload=payload)
    if record['phase'] in {'prepared','running'}:
        payload=record['prepared_payload']
        try:
            result, error = run_local(payload, jid,local_id=record.get('local_id'),
                on_accepted=lambda local_id:store.update(jid,phase='running',local_id=local_id),
                started_at=record['started_at'],durable=True)
        except NodeSubmissionError as exc:
            if exc.status not in {400,401,403,404,422} or exc.retryable:raise
            result,error=None,str(exc)
        record=store.update(jid,phase='failed' if error else 'rendered',result=result or {},error=error)
    if record['phase']=='failed':
        report(jid,False,error=record['error'],claim_token=token)
        store.update(jid,phase='complete');return
    result=record['result']
    if remote['status'] != 'completed':
        if record['phase']=='rendered':
            url=result.get('file_url') or ''
            if not re.fullmatch(r'/v1/files/[A-Za-z0-9_-]+\.mp4',url):
                raise ValueError('local_artifact_url_invalid')
            data=_download_local_result(LOCAL+url)
            if not isinstance(data,bytes) or not 0<len(data)<=256*1024*1024:raise ValueError('local_artifact_invalid')
            store.write(store.path(jid,'.mp4'),data)
            record=store.update(jid,phase='ready',sha256=hashlib.sha256(data).hexdigest())
        data=store.path(jid,'.mp4').read_bytes()
        if hashlib.sha256(data).hexdigest()!=record['sha256']:raise ValueError('delivery_spool_corrupt')
        out = upload_result(jid,result.get('file_url') or '',result,claim_token=token,data=data)
        if not out.get('ok'):raise RuntimeError('delivery_upload_unconfirmed')
    elif record.get('sha256') and remote.get('artifact_sha256') and record['sha256'] != remote['artifact_sha256']:
        raise RuntimeError('delivery_remote_artifact_conflict')
    report(jid, True, result=result,claim_token=token)
    store.update(jid,phase='complete')
    store.path(jid,'.mp4').unlink(missing_ok=True)


def recover_deliveries(store):
    out=_call(RELAY+'/v1/recover',NODE_TOKEN,'POST',body={'node':NODE_NAME},timeout=30)
    for job in out['jobs']:store.ensure(job)


def recovery_worker(store):
    while True:
        try:recover_deliveries(store)
        except Exception as exc:print('[poller] recovery_scan_error=%s'%type(exc).__name__,flush=True)
        time.sleep(15)


def recover_before_claims(store):
    while True:
        try:
            recover_deliveries(store)
            return
        except Exception as exc:
            print('[poller] startup_recovery_pending=%s'%type(exc).__name__,flush=True)
            time.sleep(15)


def worker(slot,store):
    """一个并发槽位：独立地「领取→渲染→回传」，循环不停。"""
    while True:
        try:
            record=store.next_ready()
        except Exception as exc:
            print('[poller] journal_read_error=%s'%type(exc).__name__,flush=True)
            time.sleep(15);continue
        if record is None:
            try:
                with store.claim_lock:
                    if store.outstanding() < CONCURRENCY:
                        job=claim()
                        if job:store.ensure(job)
                record=store.next_ready()
            except Exception as exc:
                print('[poller] journal_claim_error=%s'%type(exc).__name__,flush=True)
            if record is None:
                time.sleep(POLL_IDLE)
                continue
        jid=record['job_id']
        try:
            process_delivery(store,record)
            print('[poller] delivery_confirmed job=%s slot=%s'%(jid,slot),flush=True)
        except Exception as exc:
            # Transport/acknowledgement failure is not renderer failure.
            print('[poller] delivery_pending job=%s phase=%s error=%s'%(jid,store.get(jid)['phase'],type(exc).__name__),flush=True)
        finally:store.release(jid)


def main():
    print("[poller] node=%s relay=%s local=%s 并发=%d"
          % (NODE_NAME, RELAY, LOCAL, CONCURRENCY), flush=True)
    store=DeliveryStore(os.environ.get('NODE_STATE_DIR',str(Path.home()/'.huangque-render-poller')))
    with store.process_lock():
        recover_before_claims(store)
        threads = [threading.Thread(target=heartbeat,daemon=True),threading.Thread(target=recovery_worker,args=(store,),daemon=True)]
        threads += [threading.Thread(target=worker,args=(slot,store),daemon=True) for slot in range(1,CONCURRENCY+1)]
        for t in threads:t.start()
        for t in threads:t.join()


if __name__ == "__main__":
    main()
