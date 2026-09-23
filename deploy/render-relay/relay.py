#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄雀渲染中转器（pull 模式）

作用：让办公室内网的 GPU 节点以"全出站"方式参与渲染，黄雀无需任何改动。

数据流：
  黄雀 --POST /v1/jobs--> 中转器（入队）
  GPU 节点 --POST /v1/claim--> 取任务（出站，NAT 拦不住）
  GPU 节点 本地渲染 --> 上传 COS --> --POST /v1/report--> 回报
  黄雀 --GET /v1/jobs/<id>--> 查状态
  黄雀 --GET /v1/files/<id>.mp4--> 中转器从 COS 流式取回（同源，满足黄雀校验）

只读接口（health/templates/preflight/user-assets）直接转给云端渲染服务，
保证黄雀看到的能力目录与线上完全一致。
"""
import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH = os.environ.get("RELAY_DB", "/home/ubuntu/render-relay/relay.db")
UPSTREAM = os.environ.get("RELAY_UPSTREAM", "").rstrip("/")
UPSTREAM_TOKEN = os.environ.get("RELAY_UPSTREAM_TOKEN", "").strip()
NODE_TOKEN = os.environ.get("RELAY_NODE_TOKEN", "").strip()
RELAY_TOKEN = os.environ.get("RELAY_API_TOKEN", "").strip()
COS_BUCKET = os.environ.get("RELAY_COS_BUCKET", "").strip()
CLAIM_TIMEOUT = int(os.environ.get("RELAY_CLAIM_TIMEOUT", "2400"))   # 超过则以节点失联处理
REQUIRE_GPU = os.environ.get("RELAY_REQUIRE_GPU", "0").strip() == "1"
MAX_GPU_PENDING = max(1, int(os.environ.get("RELAY_GPU_QUEUE_MAX", "500")))
# /v1/files/ 流式转发的块大小：太大等于整包进内存，太小 syscall 太多
_STREAM_CHUNK = max(64 * 1024, int(os.environ.get("RELAY_STREAM_CHUNK", str(256 * 1024))))
# 超过这个秒数的请求打一行耗时日志（默认 5 秒；设 0 可关掉）
SLOW_LOG_SECONDS = float(os.environ.get("RELAY_SLOW_LOG_SECONDS", "5") or 0)

# ---- 线路优先级（2026-09-11：小方那台优先，它忙不过来才启用 GPU 机器）----
# 判断依据天然成立：轮询器只有在「有空位」时才会来 /v1/claim 问，所以
# 「高优先级节点最近 PRIORITY_WINDOW 秒内来问过」==「它还有空位」。
# 留空 RELAY_PRIORITY_NODES 即关闭优先级，回到先到先得。
PRIORITY_NODES = {
    n.strip() for n in os.environ.get("RELAY_PRIORITY_NODES", "").split(",") if n.strip()
}
PRIORITY_WINDOW = max(1, int(os.environ.get("RELAY_PRIORITY_WINDOW", "20")))
_LAST_CLAIM = {}          # node -> 最近一次来领活的时间（内存态，重启后重新学习）
_LAST_HEARTBEAT = {}      # node -> 最近一次独立遥测心跳，不参与“是否有空位”判断
_NODE_GPU = {}            # node -> 最近一次 GPU 遥测
_NODE_RENDER = {}         # node -> verified renderer contract plus server receipt time
_DELIVERY_LOCKS = tuple(threading.RLock() for _ in range(1024))
# 节点心跳：轮询器空闲时每 POLL_IDLE(默认 5) 秒来问一次，所以「90 秒没来过」= 掉线。
# 以前中转器只能靠 PRIORITY_WINDOW(20 秒) 猜「它还有没有空位」，**看不出节点死活** ——
# 节点挂了，任务就静静躺在队列里，没有任何信号。现在 /health 直接报每台节点的
# 在线状态和正在跑几条。
NODE_ONLINE_SECONDS = max(30, int(os.environ.get("RELAY_NODE_ONLINE_SECONDS", "90")))


# ---- 节点失败熔断（2026-09-11：fang 磁盘满，坏节点被继续派活，70 条全废）----
FAIL_THRESHOLD = max(1, int(os.environ.get("RELAY_NODE_FAIL_THRESHOLD", "3")))
FAIL_COOLDOWN = max(30, int(os.environ.get("RELAY_NODE_FAIL_COOLDOWN", "600")))
_NODE_FAIL = {}          # node -> {"n": 连续失败数, "until": 冷却截止时间戳}


def _node_blocked(node, now):
    """该节点是否处于熔断冷却期。"""
    st = _NODE_FAIL.get(node or "")
    return bool(st and st.get("until", 0) > now)


def _node_record(node, ok, now, detail=""):
    """记一次节点的成功/失败；连续失败到阈值就熔断。"""
    if not node:
        return
    if ok:
        if _NODE_FAIL.get(node, {}).get("n"):
            print("[render-relay] 节点 %s 恢复正常，连续失败计数清零" % node, flush=True)
        _NODE_FAIL.pop(node, None)
        return
    st = _NODE_FAIL.setdefault(node, {"n": 0, "until": 0})
    st["n"] += 1
    if st["n"] >= FAIL_THRESHOLD and st["until"] <= now:
        st["until"] = now + FAIL_COOLDOWN
        print("[render-relay] ⚠️ 节点 %s 连续失败 %d 次，熔断 %d 秒不再派活：%s"
              % (node, st["n"], FAIL_COOLDOWN, str(detail)[:160]), flush=True)


def _clean_gpu(value):
    if not isinstance(value, dict):
        return None
    try:
        out = {
            "name": str(value.get("name") or "")[:96],
            "utilization": max(0.0, min(100.0, float(value["utilization"]))),
            "memory_used": max(0, int(value["memory_used"])),
            "memory_total": max(0, int(value["memory_total"])),
            "temperature": float(value["temperature"]),
            "power": max(0.0, float(value["power"])),
            "sampled_at": int(value["sampled_at"]),
        }
        encoder = value.get("encoder")
        out["encoder"] = None if encoder is None else max(0.0, min(100.0, float(encoder)))
        return out
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _clean_render_contract(value):
    if not isinstance(value, dict) or value.get("ready") is not True:
        return None
    evidence = _clean_render_evidence(value)
    templates = value.get("templates")
    if (evidence is None or not isinstance(templates, list) or not 1 <= len(templates) <= 64
            or any(not isinstance(t, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", t) for t in templates)):
        return None
    result = {**evidence, "ready": True, "templates": sorted(set(templates))}
    if (value.get("material_adaptation_contract") == "auto-v1"
            and type(value.get("material_adaptation_delivery_protocol")) is int
            and value["material_adaptation_delivery_protocol"] == 2):
        result["material_adaptation_contract"] = "auto-v1"
        result["material_adaptation_delivery_protocol"] = 2
    text = value.get("text_style_contract")
    if (value.get("text_style_delivery_protocol") == 2 and isinstance(text, dict)
            and type(text.get("version")) is int and text["version"] == 1
            and isinstance(text.get("templates"), dict) and 1 <= len(text["templates"]) <= 64
            and all(t in templates and isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{64}", revision)
                    for t, revision in text["templates"].items())):
        result["text_style_contract"] = {"version": 1, "templates": dict(text["templates"])}
    return result


def _clean_render_evidence(value):
    if not isinstance(value, dict):
        return None
    adapter = value.get("adapter")
    sha = value.get("runtime_sha256")
    if (type(value.get("contract_version")) is not int or value.get("contract_version") != 1 or value.get("compositor") != "webgpu-native"
            or value.get("encoder") not in {"hevc_nvenc", "h264_nvenc"}
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or not isinstance(adapter, dict) or adapter.get("isFallbackAdapter") is not False
            or adapter.get("vendor") != "nvidia"):
        return None
    return {"contract_version": 1, "compositor": "webgpu-native", "encoder": value["encoder"],
            "runtime_sha256": sha, "adapter": {k: adapter.get(k) for k in
                ("vendor", "device", "architecture", "isFallbackAdapter")}}


def _record_render_contract(node, value, now):
    contract = _clean_render_contract(value)
    if contract is None:
        _NODE_RENDER.pop(node, None)
    else:
        _NODE_RENDER[node] = (now, contract)


def _gpu_capable(node, template, now, text_revision=None):
    recorded = _NODE_RENDER.get(node)
    return bool(recorded and 0 <= now - recorded[0] <= NODE_ONLINE_SECONDS
                and not _node_blocked(node, now)
                and (template is None or template in recorded[1]["templates"])
                and (text_revision is None or (recorded[1].get("text_style_contract") or {}).get("templates", {}).get(template) == text_revision))


def _gpu_available(template, now, text_revision=None):
    return any(_gpu_capable(node, template, now, text_revision) for node in list(_NODE_RENDER))


def _should_yield_to_idler(node, now, template=None, gpu_only=False, text_revision=None):
    """负载均衡：本节点在跑的活比别的**在线**节点多，就让给更空的那台。

    「谁空谁先拿」—— 最少的那台永远不让（否则会互相让到没人干活）。
    只跟**在线**节点比（NODE_ONLINE_SECONDS 内来领过活的）：掉线/摘出池的机器
    不该被算进分母，否则剩下的节点全都不敢接活。
    任何异常一律返回 False —— 均衡坏了也不能把派活搞停。
    """
    try:
        with _db() as conn:
            running = {
                str(row[0]): int(row[1]) for row in conn.execute(
                    "SELECT node, COUNT(*) FROM jobs WHERE status='running'"
                    " AND node IS NOT NULL AND node != '' GROUP BY node")
            }
        online = [
            name for name, ts in list(_LAST_CLAIM.items())
            if now - ts <= NODE_ONLINE_SECONDS and not _node_blocked(name, now)
            and (not (REQUIRE_GPU or gpu_only or text_revision) or _gpu_capable(name, template, now, text_revision))
            # A primary must balance against its own tier, not wait for an idle
            # standby which the priority gate intentionally prevents from claiming.
            and (node not in PRIORITY_NODES or name in PRIORITY_NODES)
        ]
        if len(online) < 2:
            return False          # 只有自己在线，没什么可让的
        return running.get(node, 0) > min(running.get(name, 0) for name in online)
    except Exception:
        return False


def _priority_has_room(now, template=None, gpu_only=False, text_revision=None):
    """高优先级线路是否还有空位（最近 PRIORITY_WINDOW 秒内来过）。"""
    if not PRIORITY_NODES:
        return False
    return any(
        now - ts <= PRIORITY_WINDOW and not _node_blocked(name, now)
        for name, ts in list(_LAST_CLAIM.items())
        if name in PRIORITY_NODES and (not (REQUIRE_GPU or gpu_only or text_revision) or _gpu_capable(name, template, now, text_revision))
    )
MAX_BODY = 256 * 1024 * 1024
USER_ASSET_BUDGET = 2 * 1024 * 1024 * 1024  # Matches upstream account aggregate space.
_USER_ASSET_LOCK = threading.Lock()
OUT_DIR = os.environ.get("RELAY_OUT_DIR", "/home/ubuntu/render-relay/out")
USER_ASSET_DIR = Path(os.environ.get(
    "RELAY_USER_ASSET_DIR", "/home/ubuntu/render-relay/user-assets"
))
USER_ASSET_RETENTION_SECONDS = max(
    3600, int(os.environ.get("RELAY_USER_ASSET_RETENTION_SECONDS", "259200"))
)
USER_ASSET_SUFFIXES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "video/mp4": ".mp4", "video/quicktime": ".mov",
}

# COS 上传片段：复用 content-api 的 cos 模块（凭证只留在中转器上）
_COS_UPLOAD_SNIPPET = """
import sys
sys.path.insert(0, "/home/ubuntu/content-api")
import os
os.chdir("/home/ubuntu/content-api")
from content_domains import cos
cos.upload(sys.argv[1], sys.argv[2], "video/mp4")
"""


@contextlib.contextmanager
def _db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with _db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            node TEXT,
            claimed_at INTEGER,
            result TEXT,
            error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at)")
        if "gpu_contract" not in {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}:
            conn.execute("ALTER TABLE jobs ADD COLUMN gpu_contract TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS delivery_claims(
            job_id TEXT PRIMARY KEY, node TEXT NOT NULL, token TEXT NOT NULL,
            created_at INTEGER NOT NULL, metadata_done INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute('CREATE INDEX IF NOT EXISTS idx_delivery_owner ON delivery_claims(node,metadata_done,created_at)')
        conn.commit()


def _now():
    return int(time.time())


def _delivery_lock(jid):
    return _DELIVERY_LOCKS[int(hashlib.sha256(jid.encode()).hexdigest()[:8],16) % len(_DELIVERY_LOCKS)]


def _delivery_claim(conn, jid):
    return conn.execute("SELECT * FROM delivery_claims WHERE job_id=?", (jid,)).fetchone()


def _delivery_owner(claim, node, token):
    return bool(claim and node == claim['node'] and isinstance(token, str)
                and hmac.compare_digest(token, claim['token']))


def _durable_upload(handler, jid):
    """Return False for legacy claims; durable claims never cross nodes implicitly."""
    with _db() as conn:
        lease = _delivery_claim(conn, jid)
    if lease is None:
        return False
    with _delivery_lock(jid):
        if not _delivery_owner(lease, handler.headers.get('X-HQ-Node'), handler.headers.get('X-HQ-Claim-Token')):
            handler._send(409, {'error':'delivery_owner_mismatch'}); return True
        with _db() as conn:
            row = conn.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        if not row or row['node'] != lease['node'] or row['status'] not in {'running','completed'}:
            handler._send(409, {'error':'delivery_state_conflict'}); return True
        sha = handler.headers.get('X-HQ-Artifact-Sha256', '')
        try:
            n = int(handler.headers.get('Content-Length', '0'))
            if not re.fullmatch('[a-f0-9]{64}', sha) or not 0 < n <= MAX_BODY:
                raise ValueError()
        except ValueError:
            handler._send(400, {'error':'delivery_artifact_invalid'}); return True
        data = handler.rfile.read(n)
        if len(data) != n or hashlib.sha256(data).hexdigest() != sha:
            handler._send(400, {'error':'delivery_artifact_invalid'}); return True
        existing = json.loads(row['result'] or '{}')
        if row['status'] == 'completed':
            if existing.get('artifact_sha256') != sha:
                handler._send(409, {'error':'delivery_artifact_conflict'}); return True
            handler._send(200, {'ok':True,'cos_uploaded':bool(existing.get('cos_key')), 'replayed':True}); return True
        try:
            encoded = handler.headers.get('X-HQ-GPU-Render', '')
            if len(encoded) > 8192: raise ValueError()
            expected = json.loads(row['gpu_contract']) if row['gpu_contract'] else None
            gpu = _clean_render_evidence(json.loads(base64.b64decode(encoded, validate=True))) if encoded else None
            if expected and (gpu is None or gpu['runtime_sha256'] != expected['runtime_sha256']):
                raise ValueError()
            duration = float(handler.headers.get('X-HQ-Duration') or 0)
            width = int(handler.headers.get('X-HQ-Width') or 1080)
            height = int(handler.headers.get('X-HQ-Height') or 1920)
            if not 0 <= duration <= 3600 or not 1 <= width <= 8192 or not 1 <= height <= 8192:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            handler._send(409, {'error':'gpu_evidence_required'}); return True
        out = Path(OUT_DIR); out.mkdir(parents=True,exist_ok=True)
        target = out/(jid+'.mp4'); temp = out/(jid+'.'+uuid.uuid4().hex+'.part')
        try:
            with temp.open('xb') as f:
                f.write(data); f.flush(); os.fsync(f.fileno())
            os.replace(temp,target)
        finally:
            temp.unlink(missing_ok=True)
        cos_key = 'huangque/render/%s.mp4' % jid
        uploaded = False
        try:
            import subprocess
            uploaded = subprocess.run(['/usr/bin/python3','-c',_COS_UPLOAD_SNIPPET,str(target),cos_key],timeout=300).returncode == 0
        except Exception as exc:
            print('[render-relay] durable COS transfer error=%s' % type(exc).__name__,flush=True)
        result = {'duration':duration,'width':width,'height':height,
                  'template_id':handler.headers.get('X-HQ-Template') or '',
                  'engine':handler.headers.get('X-HQ-Engine') or '',
                  'file_url':'/v1/files/%s.mp4' % jid,'file_size':n,
                  'artifact_sha256':sha,'cos_key':cos_key if uploaded else ''}
        if gpu: result['gpu_render'] = gpu
        with _db() as conn:
            conn.execute("UPDATE jobs SET status='completed',result=?,updated_at=? WHERE id=? AND node=? AND status='running'",
                         (json.dumps(result),_now(),jid,lease['node']))
        _node_record(lease['node'],True,_now())
        handler._send(200,{'ok':True,'cos_uploaded':uploaded}); return True


_NODE_RESULT_METADATA_FIELDS = {
    "duration", "width", "height", "template_id", "engine", "font_mode",
    "font_selection", "font_files", "private_font_bundle_sha256",
    "material_selection_contract_version", "material_clip_contract_version",
    "material_manifest", "editing_plan", "bgm_mode", "nine_grid_visuals",
    "fixed_duration_seconds", "fixed_skill_template", "color_profile", "gpu_render",
    "text_revision", "text_overrides", "material_adaptation",
}


def _merge_completed_result(existing, incoming, *, payload=None):
    """Merge renderer evidence without replacing relay-owned delivery fields."""
    result = dict(existing or {})
    if payload is not None:
        expected = payload.get("material_adaptation")
        actual = incoming.get("material_adaptation") if isinstance(incoming, dict) else None
        if expected != actual or (expected is not None and expected != "auto-v1"):
            raise ValueError("material_adaptation_mismatch")
    if not isinstance(incoming, dict):
        return result
    for key in _NODE_RESULT_METADATA_FIELDS:
        if key in incoming:
            result[key] = incoming[key]
    return result


# 上游 /health 里这些字段是**上游的真实状态**（素材库、契约版本、worker 池）。
# 2026-09-12：以前这里在 health 响应里写死常量，和真相对不上 ——
# clip 契约写 2（实际 3）、worker_count 写 1（实际 5）、素材策略串还停在 v1。
# 主站虽然不读它们，但排查渠道故障的人第一眼看的就是这几行，假值会把方向带偏。
# 现在如实透传：上游给了就报上游的，上游没给就不报（绝不编）。
_UPSTREAM_HEALTH_FIELDS = (
    "color_contract_version", "hdr_master_output",
    "worker_alive", "worker_count", "cleanup_worker_alive",
    "worker_degraded", "degraded_jobs",
    "material_library_ready", "pexels_material_ready", "pexels_material_optional",
    "material_source_policy",
    "material_selection_contract_version", "material_clip_contract_version",
    "concurrency",
)


def _upstream(method, path, body=None, timeout=30, headers=None):
    """转发只读接口给云端渲染服务。"""
    if not UPSTREAM:
        raise RuntimeError("upstream not configured")
    data = None
    merged = {"Authorization": "Bearer " + UPSTREAM_TOKEN}
    if isinstance(headers, dict):
        merged.update(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        merged["Content-Type"] = "application/json"
    req = urllib.request.Request(UPSTREAM + path, data=data, headers=merged, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _cos_url(key):
    return "https://%s.cos.%s.myqcloud.com/%s" % (
        COS_BUCKET, os.environ.get("RELAY_COS_REGION", "ap-guangzhou"), key.lstrip("/"))


def _valid_sha(value):
    value = str(value or "").strip().lower()
    return value if len(value) == 64 and all(c in "0123456789abcdef" for c in value) else ""


def _store_user_asset(data, sha, content_type, length=None):
    sha = _valid_sha(sha)
    content_type = str(content_type or "").split(";")[0].strip().lower()
    suffix = USER_ASSET_SUFFIXES.get(content_type)
    if isinstance(data, bytes):
        length, data = len(data), io.BytesIO(data)
    if not sha or not suffix or not isinstance(length, int) or not 0 < length <= USER_ASSET_BUDGET:
        raise ValueError("用户素材校验失败")
    # ponytail: serialize disk admission; use byte reservations if upload concurrency grows.
    with _USER_ASSET_LOCK:
        USER_ASSET_DIR.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(USER_ASSET_DIR).free - length < 512 * 1024 * 1024:
            raise OSError("素材临时存储空间不足")
        target = USER_ASSET_DIR / (sha + suffix)
        temporary = USER_ASSET_DIR / (target.name + "." + uuid.uuid4().hex + ".part")
        digest, remaining = hashlib.sha256(), length
        try:
            with temporary.open("xb") as handle:
                while remaining:
                    chunk = data.read(min(64 * 1024, remaining))
                    if not chunk:
                        raise ValueError("素材传输不完整")
                    handle.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            if not hmac.compare_digest(digest.hexdigest(), sha):
                raise ValueError("用户素材校验失败")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        cutoff = time.time() - USER_ASSET_RETENTION_SECONDS
        for old in USER_ASSET_DIR.iterdir():
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        return target


def _find_user_asset(sha):
    sha = _valid_sha(sha)
    if not sha:
        return None, ""
    for content_type, suffix in USER_ASSET_SUFFIXES.items():
        path = USER_ASSET_DIR / (sha + suffix)
        if path.is_file():
            return path, content_type
    return None, ""


class Handler(BaseHTTPRequestHandler):
    server_version = "HuangqueRenderRelay/1.0"

    def log_message(self, fmt, *args):
        print("[render-relay] " + fmt % args, flush=True)

    def handle_one_request(self):
        started = time.monotonic()
        try:
            return BaseHTTPRequestHandler.handle_one_request(self)
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= SLOW_LOG_SECONDS:
                print("[render-relay] 慢请求 %.1fs %s %s"
                      % (elapsed, getattr(self, "command", "-") or "-",
                         getattr(self, "path", "-") or "-"), flush=True)

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self, token):
        return bool(token) and hmac.compare_digest(
            (self.headers.get("Authorization") or "").replace("Bearer ", "").strip(), token)

    def _body(self):
        n = int(self.headers.get("Content-Length") or "0")
        if n <= 0 or n > MAX_BODY:
            return None
        return json.loads(self.rfile.read(n))

    def do_GET(self):
        p = urllib.parse.urlsplit(self.path).path

        if p.startswith("/v1/job-assets/"):
            if not self._auth(NODE_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            parts = p.split("/")
            if len(parts) != 5:
                return self._send(404, {"error": "not_found"})
            job_id, sha = parts[3], _valid_sha(parts[4])
            node = str(self.headers.get("X-HQ-Node") or "").strip()[:64]
            with _db() as conn:
                row = conn.execute(
                    "SELECT payload,status,node FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
            if not row or row["status"] != "running" or row["node"] != node or not sha:
                return self._send(404, {"error": "not_found"})
            payload = json.loads(row["payload"] or "{}")
            allowed = any(
                isinstance(item, dict) and _valid_sha(item.get("sha256")) == sha
                for item in payload.get("user_materials") or []
            )
            path, content_type = _find_user_asset(sha)
            if not allowed or path is None:
                return self._send(404, {"error": "not_found"})
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            with path.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile, _STREAM_CHUNK)
            return

        if p == "/health":
            with _db() as conn:
                pending = conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]
                running = conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
                delivery_waiting = conn.execute("SELECT COUNT(*) FROM delivery_claims d JOIN jobs j ON j.id=d.job_id WHERE j.status='completed' AND d.metadata_done=0").fetchone()[0]
                delivery_stalled = conn.execute("SELECT COUNT(*) FROM delivery_claims d JOIN jobs j ON j.id=d.job_id WHERE j.status='running' AND j.claimed_at < ?",(_now()-CLAIM_TIMEOUT,)).fetchone()[0]
                # 心跳的第二半：光知道「它最近来过」不够，还要看得出它在干活还是空转
                per_node = {
                    str(row[0]): int(row[1])
                    for row in conn.execute(
                        "SELECT node, COUNT(*) FROM jobs WHERE status='running'"
                        " AND node IS NOT NULL AND node != '' GROUP BY node")
                }
            # templates 必须如实反映上游：黄雀的 availability() 读这个字段判渠道就绪。
            # 但中转器**不拿模板数当判据** —— 原来写死 `templates in (2,15,19,20,22)`，
            # 模板一增减就会把整条渠道误判成不可用（2026-09-11 模板数 22→20 就差点踩到）。
            # 上游健不健康由上游自己在 ok 里说；这里只要求「上游 ok 且确实有模板」。
            templates = 0
            upstream_ok = False
            up = {}
            try:
                code, raw = _upstream("GET", "/health", timeout=5)
                parsed = json.loads(raw or b"{}")
                up = parsed if isinstance(parsed, dict) else {}
                templates = int(up.get("templates") or 0)
                upstream_ok = up.get("ok") is True
            except Exception as exc:
                print("[render-relay] health upstream failed: %s" % exc, flush=True)
            ok = upstream_ok and templates > 0
            # 节点心跳：在线 = 最近 NODE_ONLINE_SECONDS 内来领过活；running = 正在跑几条。
            # **只报告、不参与 ok 判定** —— 节点掉线时把整条渠道判成「未就绪」，
            # 用户会直接收到「渠道繁忙」，而让任务排队等节点回来往往才是对的。
            now = _now()
            nodes = {}
            for name in set(per_node) | set(_LAST_CLAIM) | set(_LAST_HEARTBEAT):
                seen = _LAST_HEARTBEAT.get(name, _LAST_CLAIM.get(name))
                age = None if seen is None else max(0, int(now - seen))
                nodes[name] = {
                    "online": age is not None and age <= NODE_ONLINE_SECONDS,
                    "last_seen_seconds": age,
                    "running": per_node.get(name, 0),
                    "gpu_ready": _gpu_capable(name, None, now),
                }
            gpu_nodes = sum(1 for value in nodes.values() if value["gpu_ready"])
            ok = ok and (not REQUIRE_GPU or gpu_nodes > 0)
            body = {
                "ok": ok, "templates": templates,
                "gpu_required": REQUIRE_GPU, "gpu_nodes_ready": gpu_nodes,
                "priority_nodes": sorted(PRIORITY_NODES),
                "priority_window_seconds": PRIORITY_WINDOW,
                "nodes": nodes,
                "nodes_online": sum(1 for v in nodes.values() if v["online"]),
                "nodes_total": len(nodes),
                # 这两个是中转器**自己**的真实队列长度，不是透传
                "pending_jobs": pending, "running_jobs": running,
                "delivery_protocol": 2,
                "delivery_metadata_pending": delivery_waiting,
                "delivery_recovery_required": delivery_stalled,
            }
            # 上游的真实状态如实透传；上游没给就不出现这个键，不编常量。
            for field in _UPSTREAM_HEALTH_FIELDS:
                if field in up:
                    body[field] = up[field]
            return self._send(200 if ok else 503, body)

        if p == "/v1/telemetry":
            if not self._auth(RELAY_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            with _db() as conn:
                per_node = {
                    str(row[0]): int(row[1])
                    for row in conn.execute(
                        "SELECT node, COUNT(*) FROM jobs WHERE status='running'"
                        " AND node IS NOT NULL AND node != '' GROUP BY node")
                }
            now = _now()
            nodes = {}
            for name in set(per_node) | set(_LAST_CLAIM) | set(_LAST_HEARTBEAT):
                seen = _LAST_HEARTBEAT.get(name, _LAST_CLAIM.get(name))
                age = None if seen is None else max(0, int(now - seen))
                node = {
                    "online": age is not None and age <= NODE_ONLINE_SECONDS,
                    "last_seen_seconds": age,
                    "running": per_node.get(name, 0),
                }
                gpu = dict(_NODE_GPU.get(name) or {})
                if gpu:
                    gpu["sample_age_seconds"] = max(0, int(now - gpu["sampled_at"]))
                    node["gpu"] = gpu
                nodes[name] = node
            return self._send(200, {"ok": True, "nodes": nodes})

        if p.startswith("/v1/jobs/"):
            jid = p[len("/v1/jobs/"):].strip()
            with _db() as conn:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                # 本地没有：可能是转发给上游（fang）的 preview_id 采纳任务，原样透传。
                try:
                    code, raw = _upstream("GET", "/v1/jobs/" + jid, timeout=30)
                except urllib.error.HTTPError as exc:
                    try:
                        raw = exc.read()
                    except Exception:
                        raw = b""
                    if not raw:
                        raw = json.dumps(
                            {"error": "not_found"}
                        ).encode("utf-8")
                    self.send_response(exc.code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                except Exception:
                    return self._send(404, {"error": "not_found"})
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            out = {"job_id": row["id"], "status": row["status"],
                   "created_at": row["created_at"], "updated_at": row["updated_at"]}
            if row["status"] == "completed" and json.loads(row["payload"]).get("material_adaptation"):
                # Binary upload is not complete delivery until its evidence is acknowledged.
                with _db() as conn:
                    lease = _delivery_claim(conn, jid)
                result = json.loads(row["result"] or "{}")
                if (not lease or not lease["metadata_done"]
                        or result.get("material_adaptation") != json.loads(row["payload"])["material_adaptation"]):
                    return self._send(200, {**out, "status": "running", "phase": "awaiting_metadata"})
            if row["result"]:
                out["result"] = json.loads(row["result"])
            if row["error"]:
                out["error"] = row["error"]
            return self._send(200, out)

        if p.startswith("/v1/files/"):
            # 从 COS 流式取回（黄雀要求同源地址）
            name = p[len("/v1/files/"):].strip()
            with _db() as conn:
                row = conn.execute(
                    "SELECT result FROM jobs WHERE id=? AND status='completed'",
                    (name.replace(".mp4", ""),)).fetchone()
            if not row or not row["result"]:
                # 本地没有：可能是转发给上游（fang）的 preview_id 采纳任务，文件在上游本机。
                try:
                    req = urllib.request.Request(
                        UPSTREAM + "/v1/files/" + name,
                        headers={"Authorization": "Bearer " + UPSTREAM_TOKEN},
                    )
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        declared = int(resp.headers.get("Content-Length") or 0)
                        self.send_response(resp.status)
                        self.send_header(
                            "Content-Type",
                            resp.headers.get("Content-Type") or "video/mp4",
                        )
                        if declared:
                            self.send_header("Content-Length", str(declared))
                        else:
                            self.send_header("Connection", "close")
                        self.end_headers()
                        shutil.copyfileobj(resp, self.wfile, _STREAM_CHUNK)
                    return
                except urllib.error.HTTPError as exc:
                    return self._send(exc.code, {"error": "not_found"})
                except Exception:
                    return self._send(404, {"error": "not_found"})
            result = json.loads(row["result"])
            local = Path(OUT_DIR) / name
            if local.is_file():
                # 流式发送：不把整个成片读进内存（2026-09-11 大任务超时事故）
                size = local.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                # 读源 / 写客户端 分开计时：写慢=对面收得慢，读慢=磁盘慢
                _s0 = time.monotonic()
                read_s = write_s = 0.0
                with local.open("rb") as handle:
                    while True:
                        _a = time.monotonic()
                        chunk = handle.read(_STREAM_CHUNK)
                        _b = time.monotonic()
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        _c = time.monotonic()
                        read_s += _b - _a
                        write_s += _c - _b
                total = time.monotonic() - _s0
                if total >= SLOW_LOG_SECONDS:
                    print("[render-relay] 慢文件 %.1fs 共%.1fMB 读盘%.2fs 发送%.2fs %s"
                          % (total, size / 1048576.0, read_s, write_s, name), flush=True)
                return
            key = result.get("cos_key") or ""
            if not key:
                return self._send(404, {"error": "no_output"})
            try:
                req = urllib.request.Request(_cos_url(key))
                with urllib.request.urlopen(req, timeout=120) as resp:
                    declared = int(resp.headers.get("Content-Length") or 0)
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    if declared:
                        self.send_header("Content-Length", str(declared))
                    else:
                        self.send_header("Connection", "close")
                    self.end_headers()
                    shutil.copyfileobj(resp, self.wfile, _STREAM_CHUNK)
            except Exception as exc:
                return self._send(502, {"error": "cos_fetch_failed", "detail": str(exc)[:120]})
            return

        if p == "/v1/templates":
            if not self._auth(RELAY_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            try:
                code, raw = _upstream("GET", "/v1/templates")
            except Exception as exc:
                return self._send(503, {"error": "upstream_failed", "detail": str(exc)[:120]})
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if p.startswith("/v1/preview-jobs/"):
            # 预览微调（2026-09-21）：纯透传上游，不落 jobs 表。
            if not self._auth(RELAY_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            try:
                code, raw = _upstream("GET", p, timeout=60)
            except urllib.error.HTTPError as exc:
                try:
                    raw = exc.read()
                except Exception:
                    raw = b""
                code = exc.code
                if not raw:
                    raw = json.dumps({"error": "upstream_error", "detail": str(exc)[:120]}).encode("utf-8")
            except Exception as exc:
                return self._send(503, {"error": "upstream_failed", "detail": str(exc)[:120]})
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if p.startswith("/v1/preview-files/"):
            # 预览微调（2026-09-21）：二进制原样回放，镜像上游的 inline/private 头。
            if not self._auth(RELAY_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            try:
                req = urllib.request.Request(
                    UPSTREAM + p, headers={"Authorization": "Bearer " + UPSTREAM_TOKEN})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read()
                    code = resp.status
                    ctype = resp.headers.get("Content-Type") or "application/octet-stream"
                    disposition = resp.headers.get("Content-Disposition") or "inline"
                    cache = resp.headers.get("Cache-Control") or "private, max-age=300"
            except urllib.error.HTTPError as exc:
                try:
                    raw = exc.read()
                except Exception:
                    raw = b""
                code = exc.code
                ctype = "application/json; charset=utf-8"
                disposition = None
                cache = None
                if not raw:
                    raw = json.dumps({"error": "upstream_error", "detail": str(exc)[:120]}).encode("utf-8")
            except Exception as exc:
                return self._send(503, {"error": "upstream_failed", "detail": str(exc)[:120]})
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            if disposition:
                self.send_header("Content-Disposition", disposition)
            if cache:
                self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(raw)
            return

        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        p = urllib.parse.urlsplit(self.path).path

        # ---- 黄雀侧 ----
        if p == "/v1/preflight":
            if not self._auth(RELAY_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            try:
                body = self._body()
                code, raw = _upstream("POST", "/v1/preflight", body, timeout=20)
                if (REQUIRE_GPU or body.get("text_overrides")) and 200 <= code < 300:
                    checked = json.loads(raw)
                    template = (checked.get("payload") or {}).get("template_id")
                    revision = body.get("text_revision") if body.get("text_overrides") else None
                    if body.get("text_overrides") and (not revision or (checked.get("payload") or {}).get("text_revision") != revision):
                        return self._send(409, {"error": "text_controls_unavailable", "detail": "上游尚未支持逐层文字微调"})
                    if not template or not _gpu_available(template, _now(), revision):
                        return self._send(503, {"error": "gpu_unavailable", "detail": "该模板暂时没有可用的 GPU 渲染节点"})
                    with _db() as conn:
                        if conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0] >= MAX_GPU_PENDING:
                            return self._send(429, {"error": "gpu_queue_full", "detail": "GPU 渲染队列已满，请稍后重试"})
            except urllib.error.HTTPError as exc:
                # 上游明确回的 4xx/5xx：**原样透传状态码和响应体**。
                # 绝不能兜成 503 —— 那会把「参数不对（400）」掩盖成「服务不可用」，
                # 用户拿不到真实原因（2026-09-11 实录：fang 回 400，用户看到"服务暂不可用"）。
                try:
                    raw = exc.read()
                except Exception:
                    raw = b""
                if not raw:
                    raw = json.dumps(
                        {"error": "upstream_error", "detail": str(exc)[:120]}
                    ).encode("utf-8")
                self.send_response(exc.code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            except Exception as exc:
                return self._send(503, {"error": "upstream_failed", "detail": str(exc)[:120]})
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if p == "/v1/jobs":
            if not self._auth(RELAY_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            body = self._body()
            if not isinstance(body, dict):
                return self._send(400, {"error": "invalid_request"})
            # preview_id 采纳：预览记录只存在 fang（上游）本机，正式任务也必须
            # 回 fang 渲染（GPU 节点本地没有预览记录会拒单）。原样透传上游响应。
            if body.get("preview_id"):
                forward_headers = {}
                request_id = self.headers.get("X-Request-Id") or ""
                if request_id:
                    # 渲染端按 X-Request-Id 做幂等；透传客户端的请求标识。
                    forward_headers["X-Request-Id"] = request_id
                try:
                    code, raw = _upstream(
                        "POST", "/v1/jobs", body, timeout=30,
                        headers=forward_headers,
                    )
                except urllib.error.HTTPError as exc:
                    try:
                        raw = exc.read()
                    except Exception:
                        raw = b""
                    if not raw:
                        raw = json.dumps(
                            {"error": "upstream_error", "detail": str(exc)[:120]}
                        ).encode("utf-8")
                    self.send_response(exc.code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                except Exception as exc:
                    return self._send(503, {"error": "upstream_failed", "detail": str(exc)[:120]})
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            jid = uuid.uuid4().hex
            now = _now()
            text_revision = body.get("text_revision") if body.get("text_overrides") else None
            if body.get("text_overrides") and (not isinstance(text_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", text_revision)):
                return self._send(400, {"error": "invalid_text_revision"})
            if REQUIRE_GPU and not isinstance(body.get("template_id"), str):
                return self._send(400, {"error": "invalid_template"})
            if (REQUIRE_GPU or text_revision) and not _gpu_available(body.get("template_id"), now, text_revision):
                return self._send(503, {"error": "gpu_unavailable", "detail": "GPU 渲染节点暂不可用"})
            if body.get("material_adaptation"):
                if body["material_adaptation"] != "auto-v1":
                    return self._send(400, {"error": "invalid_material_adaptation"})
                if not any(_gpu_capable(n, body.get("template_id"), now, text_revision)
                        and c[1].get("material_adaptation_contract") == "auto-v1"
                        for n, c in list(_NODE_RENDER.items())):
                    return self._send(503, {"error": "material_adaptation_unavailable", "detail": "暂无兼容素材适配协议的节点"})
            with _db() as conn:
                if REQUIRE_GPU or text_revision or body.get("material_adaptation"):
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0] >= MAX_GPU_PENDING:
                        return self._send(429, {"error": "gpu_queue_full", "detail": "GPU 渲染队列已满，请稍后重试"})
                conn.execute(
                    "INSERT INTO jobs(id,payload,status,created_at,updated_at,gpu_contract) VALUES(?,?,?,?,?,?)",
                    (jid, json.dumps(body, ensure_ascii=False), "pending", now, now, "required" if REQUIRE_GPU or text_revision or body.get("material_adaptation") else ""))
                conn.commit()
            return self._send(202, {"job_id": jid, "status": "pending",
                                    "created_at": now, "updated_at": now})

        if p == "/v1/user-assets":
            if not self._auth(RELAY_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            headers = {
                "Authorization": "Bearer " + UPSTREAM_TOKEN,
                "Content-Type": self.headers.get("Content-Type") or "application/octet-stream",
                "X-HQ-Asset-Sha256": self.headers.get("X-HQ-Asset-Sha256") or "",
            }
            try:
                n = int(self.headers.get("Content-Length") or "0")
                path = _store_user_asset(self.rfile, headers["X-HQ-Asset-Sha256"],
                                         headers["Content-Type"], length=n)
                headers["Content-Length"] = str(n)
                # A local cache/marker cannot prove the upstream still has the file.
                with path.open("rb") as source:
                    req = urllib.request.Request(UPSTREAM + "/v1/user-assets", data=source,
                                                 headers=headers, method="POST")
                    with urllib.request.urlopen(req, timeout=3600) as resp:
                        code = resp.status
                        response = json.loads(resp.read(1024 * 1024) or b"{}")
                self._send(code, response)
            except urllib.error.HTTPError as exc:
                self._send(exc.code, {"error": "upstream_rejected",
                                      "detail": exc.read(200).decode("utf-8", "replace")})
            except ValueError as exc:
                self._send(400, {"error": "invalid_request", "detail": str(exc)})
            except Exception as exc:
                self._send(503, {"error": "upstream_failed", "detail": str(exc)[:120]})
            return

        if p == "/v1/preview-jobs":
            # 预览微调（2026-09-21）：纯透传上游，不落本地队列、不参与 claim/report/COS。
            if not self._auth(RELAY_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            body = self._body()
            if not isinstance(body, dict):
                return self._send(400, {"error": "invalid_request"})
            try:
                code, raw = _upstream("POST", "/v1/preview-jobs", body, timeout=60)
            except urllib.error.HTTPError as exc:
                try:
                    raw = exc.read()
                except Exception:
                    raw = b""
                code = exc.code
                if not raw:
                    raw = json.dumps({"error": "upstream_error", "detail": str(exc)[:120]}).encode("utf-8")
            except Exception as exc:
                return self._send(503, {"error": "upstream_failed", "detail": str(exc)[:120]})
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        # ---- 节点侧 ----
        if p in {'/v1/recover','/v1/delivery-status'}:
            if not self._auth(NODE_TOKEN):
                return self._send(401, {'error':'unauthorized'})
            body = self._body() or {}
            node = body.get('node')
            if not isinstance(node,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',node):
                return self._send(400, {'error':'invalid_request'})
            with _db() as conn:
                if p == '/v1/recover':
                    rows = conn.execute("SELECT j.id,j.payload,d.token FROM jobs j JOIN delivery_claims d ON d.job_id=j.id WHERE d.node=? AND j.node=d.node AND (j.status='running' OR (j.status='completed' AND d.metadata_done=0)) ORDER BY d.created_at LIMIT 100",(node,)).fetchall()
                    return self._send(200, {'jobs':[{'job_id':r['id'],'payload':json.loads(r['payload']),'claim_token':r['token']} for r in rows]})
                jid = str(body.get('job_id') or '')
                lease = _delivery_claim(conn,jid)
                if not _delivery_owner(lease,node,body.get('claim_token')):
                    return self._send(409,{'error':'delivery_owner_mismatch'})
                row = conn.execute('SELECT status,node,result FROM jobs WHERE id=?',(jid,)).fetchone()
                if not row or row['node'] != node:
                    return self._send(409,{'error':'delivery_state_conflict'})
                result = json.loads(row['result'] or '{}')
                return self._send(200,{'status':row['status'],'metadata_done':bool(lease['metadata_done']), 'artifact_sha256':result.get('artifact_sha256')})

        if p == "/v1/heartbeat":
            if not self._auth(NODE_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            body = self._body() or {}
            node = str(body.get("node") or "").strip()[:64]
            if not node:
                return self._send(400, {"error": "invalid_request"})
            _LAST_HEARTBEAT[node] = _now()
            gpu = _clean_gpu(body.get("gpu"))
            if gpu is not None:
                _NODE_GPU[node] = gpu
            _record_render_contract(node, body.get("gpu_render"), _now())
            return self._send(200, {"ok": True})

        if p == "/v1/claim":
            if not self._auth(NODE_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            body = self._body() or {}
            node = str(body.get("node") or "").strip()[:64] or "unknown"
            now = _now()
            _record_render_contract(node, body.get("gpu_render"), now)
            if REQUIRE_GPU and not _gpu_capable(node, None, now):
                return self._send(200, {"job": None, "deferred": "gpu_unverified"})
            if _node_blocked(node, now):
                # 熔断中：不派活，也不记「还在轮询」——否则优先线路判空会误判成它还有空位
                return self._send(200, {"job": None, "deferred": "node_cooldown"})
            # 先记时间戳（被拒也要记，否则高优先级节点的「还在轮询」永远学不到）
            _LAST_CLAIM[node] = now
            _LAST_HEARTBEAT[node] = now
            with _db() as conn:
                # 回收失联节点的任务
                conn.execute(
                    "UPDATE jobs SET status='pending', node=NULL, updated_at=?,"
                    " gpu_contract=CASE WHEN gpu_contract!='' THEN 'required' ELSE '' END"
                    " WHERE status='running' AND claimed_at < ?"
                    " AND NOT EXISTS (SELECT 1 FROM delivery_claims d WHERE d.job_id=jobs.id)",
                    (now, now - CLAIM_TIMEOUT))
                if body.get('delivery_protocol') == 2:
                    slots=body.get('slots',5)
                    if type(slots) is not int or not 1 <= slots <= 20:
                        return self._send(400,{'error':'invalid_slots'})
                    owned=conn.execute("SELECT COUNT(*) FROM delivery_claims d JOIN jobs j ON j.id=d.job_id WHERE d.node=? AND (j.status='running' OR (j.status='completed' AND d.metadata_done=0))",(node,)).fetchone()[0]
                    if owned >= slots:
                        return self._send(200,{'job':None,'deferred':'delivery_capacity'})
                rows = conn.execute(
                    "SELECT id,payload,gpu_contract FROM jobs WHERE status='pending'"
                    " ORDER BY created_at LIMIT ?", (MAX_GPU_PENDING,)).fetchall()
                def can_claim(row):
                    payload = json.loads(row["payload"])
                    if payload.get("material_adaptation"):
                        capability = (_NODE_RENDER.get(node) or (None, {}))[1]
                        if (payload["material_adaptation"] != "auto-v1"
                                or capability.get("material_adaptation_contract") != "auto-v1"
                                or body.get("delivery_protocol") != 2):
                            return False
                    revision = payload.get("text_revision") if payload.get("text_overrides") else None
                    if payload.get("text_overrides") and (not revision or body.get("delivery_protocol") != 2):
                        return False
                    return not (REQUIRE_GPU or row["gpu_contract"] or revision) or _gpu_capable(node, payload.get("template_id"), now, revision)
                row = next((r for r in rows if can_claim(r)), None)
                if not row:
                    conn.commit()
                    return self._send(200, {"job": None})
                template = json.loads(row["payload"]).get("template_id")
                selected_payload = json.loads(row["payload"])
                text_revision = selected_payload.get("text_revision") if selected_payload.get("text_overrides") else None
                needs_gpu = REQUIRE_GPU or bool(row["gpu_contract"])
                # Honor the selected job's contract even after admission enforcement is disabled.
                if not selected_payload.get("material_adaptation") and node not in PRIORITY_NODES and _priority_has_room(now, template, needs_gpu, text_revision):
                    return self._send(200, {"job": None, "deferred": "priority"})
                if not selected_payload.get("material_adaptation") and _should_yield_to_idler(node, now, template, needs_gpu, text_revision):
                    return self._send(200, {"job": None, "deferred": "load"})
                contract = json.dumps(_NODE_RENDER[node][1]) if needs_gpu else ""
                cur = conn.execute(
                    "UPDATE jobs SET status='running', node=?, claimed_at=?, updated_at=?,gpu_contract=?"
                    " WHERE id=? AND status='pending'",
                    (node, now, now, contract, row["id"]))
                token = None
                if cur.rowcount == 1 and body.get('delivery_protocol') == 2:
                    token = uuid.uuid4().hex
                    conn.execute('INSERT INTO delivery_claims(job_id,node,token,created_at) VALUES(?,?,?,?)',
                                 (row['id'],node,token,now))
                conn.commit()
                if cur.rowcount != 1:
                    return self._send(200, {"job": None})
            return self._send(200, {"job": {"job_id": row["id"],
                                            "payload": json.loads(row["payload"]),
                                            **({'claim_token':token} if token else {})}})

        if p.startswith("/v1/result/"):
            # 节点回传成品视频（原始字节）。中转器落盘 + 上传 COS，避免节点持有 COS 凭证。
            if not self._auth(NODE_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            jid = p[len("/v1/result/"):].strip()
            if _durable_upload(self,jid):
                return
            with _db() as conn:
                row = conn.execute(
                    "SELECT status, node, gpu_contract FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                return self._send(404, {"error": "not_found"})
            gpu_evidence = None
            if row["gpu_contract"]:
                try:
                    encoded = self.headers.get("X-HQ-GPU-Render", "")
                    if len(encoded) > 8192:
                        raise ValueError("GPU header too long")
                    gpu_evidence = _clean_render_evidence(json.loads(base64.b64decode(encoded, validate=True)))
                    expected = json.loads(row["gpu_contract"])
                    if (gpu_evidence is None or gpu_evidence["runtime_sha256"] != expected["runtime_sha256"]
                            or self.headers.get("X-HQ-Node") != row["node"]
                            or row["status"] not in {"running", "completed"}):
                        raise ValueError("GPU evidence does not match claim")
                except (ValueError, TypeError, KeyError):
                    return self._send(409, {"error": "gpu_evidence_required"})
            _node_record(row["node"] or "", True, _now())
            n = int(self.headers.get("Content-Length") or "0")
            if n <= 0 or n > MAX_BODY:
                return self._send(400, {"error": "invalid_request", "detail": "文件大小无效"})
            data = self.rfile.read(n)
            out_dir = Path(OUT_DIR)
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / (jid + ".mp4")
            target.write_bytes(data)
            cos_key = "huangque/render/%s.mp4" % jid
            uploaded = False
            try:
                import subprocess
                rc = subprocess.run(
                    ["/usr/bin/python3", "-c", _COS_UPLOAD_SNIPPET, str(target), cos_key],
                    timeout=300).returncode
                uploaded = rc == 0
            except Exception as exc:
                print("[render-relay] cos upload failed: %s" % exc, flush=True)
            meta = dict(self.headers)
            result = {
                "duration": float(self.headers.get("X-HQ-Duration") or 0),
                "width": int(self.headers.get("X-HQ-Width") or 1080),
                "height": int(self.headers.get("X-HQ-Height") or 1920),
                "template_id": self.headers.get("X-HQ-Template") or "",
                "engine": self.headers.get("X-HQ-Engine") or "",
                "file_url": "/v1/files/%s.mp4" % jid,
                "file_size": len(data),
                "cos_key": cos_key if uploaded else "",
            }
            if gpu_evidence:
                result["gpu_render"] = gpu_evidence
            now = _now()
            with _db() as conn:
                conn.execute("UPDATE jobs SET status='completed', result=?, updated_at=?"
                             " WHERE id=?", (json.dumps(result, ensure_ascii=False), now, jid))
                conn.commit()
            return self._send(200, {"ok": True, "cos_uploaded": uploaded})

        if p == "/v1/report":
            if not self._auth(NODE_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            body = self._body() or {}
            jid = str(body.get("job_id") or "").strip()
            ok = bool(body.get("ok"))
            now = _now()
            with _db() as conn:
                lease = _delivery_claim(conn,jid)
            if lease is not None:
                with _delivery_lock(jid), _db() as conn:
                    if not _delivery_owner(lease,body.get('node'),body.get('claim_token')):
                        return self._send(409,{'error':'delivery_owner_mismatch'})
                    row = conn.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
                    if not row or row['node'] != lease['node']:
                        return self._send(409,{'error':'delivery_state_conflict'})
                    if ok:
                        if row['status'] != 'completed':
                            return self._send(409,{'error':'delivery_output_not_received'})
                        existing = json.loads(row['result'] or '{}')
                        incoming = body.get('result') or {}
                        try:
                            merged = _merge_completed_result(existing, incoming, payload=json.loads(row['payload']))
                        except ValueError as exc:
                            return self._send(409, {'error': str(exc)})
                        if row['gpu_contract']:
                            evidence = _clean_render_evidence(incoming.get('gpu_render') or existing.get('gpu_render'))
                            if not evidence or evidence['runtime_sha256'] != json.loads(row['gpu_contract'])['runtime_sha256']:
                                return self._send(409,{'error':'gpu_output_not_verified'})
                        conn.execute('UPDATE jobs SET result=? WHERE id=?',
                                     (json.dumps(merged),jid))
                        conn.execute('UPDATE delivery_claims SET metadata_done=1 WHERE job_id=?',(jid,))
                    elif row['status'] == 'running':
                        conn.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?",
                                     (str(body.get('error') or '')[:500],now,jid))
                        _node_record(lease['node'],False,now,'durable_render_failed')
                return self._send(200,{'ok':True})
            with _db() as conn:
                _row = conn.execute(
                    "SELECT node,status,result,gpu_contract,payload FROM jobs WHERE id=?", (jid,)).fetchone()
                if ok and _row:
                    try:
                        _merge_completed_result({}, body.get("result"), payload=json.loads(_row["payload"]))
                    except ValueError as exc:
                        return self._send(409, {"error": str(exc)})
                if _row and _row["gpu_contract"]:
                    if body.get("node") != _row["node"]:
                        return self._send(409, {"error": "node_mismatch"})
                    if ok:
                        if _row["status"] != "completed":
                            return self._send(409, {"error": "gpu_output_not_verified"})
                        existing = json.loads(_row["result"] or "{}")
                        incoming = body.get("result") if isinstance(body.get("result"), dict) else {}
                        evidence = _clean_render_evidence(incoming.get("gpu_render") or existing.get("gpu_render"))
                        expected = json.loads(_row["gpu_contract"])
                        if (_row["status"] != "completed" or evidence is None
                                or evidence["runtime_sha256"] != expected["runtime_sha256"]):
                            return self._send(409, {"error": "gpu_output_not_verified"})
                        body["result"] = {**incoming, "gpu_render": evidence}
                _node_record(_row["node"] if _row else "", ok, now,
                             body.get("error") or "")
                if ok:
                    existing = json.loads(_row["result"] or "{}") if _row else {}
                    result = _merge_completed_result(
                        existing, body.get("result") or {},
                    )
                    conn.execute(
                        "UPDATE jobs SET status='completed', result=?, updated_at=?"
                        " WHERE id=?", (json.dumps(result, ensure_ascii=False), now, jid))
                else:
                    if not _row or _row["status"] != "completed":
                        conn.execute(
                            "UPDATE jobs SET status='failed', error=?, updated_at=?"
                            " WHERE id=?", (str(body.get("error") or "")[:500], now, jid))
                conn.commit()
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not_found"})


class _Server(ThreadingHTTPServer):
    """连接队列默认只有 5（socketserver 的默认值）。

    慢下载一多就把 accept 队列占满，节点回传成品时连不上 → nginx 502 →
    **已经渲好的任务被判失败**（2026-09-11「提交 10 条死 2 条」的真凶）。
    必须在实例化前设好：server_bind() 里就会拿这个值调 socket.listen()。
    """
    request_queue_size = max(16, int(os.environ.get("RELAY_BACKLOG", "128")))


def main():
    if not NODE_TOKEN or not RELAY_TOKEN:
        raise SystemExit("RELAY_NODE_TOKEN 和 RELAY_API_TOKEN 必填")
    init_db()
    host = os.environ.get("RELAY_HOST", "127.0.0.1")
    port = int(os.environ.get("RELAY_PORT", "8213"))
    srv = _Server((host, port), Handler)
    print("[render-relay] listening on %s:%d, upstream=%s, backlog=%d"
          % (host, port, UPSTREAM, _Server.request_queue_size), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
