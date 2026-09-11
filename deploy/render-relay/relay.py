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
import hmac
import json
import os
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


def _priority_has_room(now):
    """高优先级线路是否还有空位（最近 PRIORITY_WINDOW 秒内来过）。"""
    if not PRIORITY_NODES:
        return False
    return any(
        now - ts <= PRIORITY_WINDOW and not _node_blocked(name, now)
        for name, ts in list(_LAST_CLAIM.items())
        if name in PRIORITY_NODES
    )
MAX_BODY = 256 * 1024 * 1024
OUT_DIR = os.environ.get("RELAY_OUT_DIR", "/home/ubuntu/render-relay/out")

# COS 上传片段：复用 content-api 的 cos 模块（凭证只留在中转器上）
_COS_UPLOAD_SNIPPET = """
import sys
sys.path.insert(0, "/home/ubuntu/content-api")
import os
os.chdir("/home/ubuntu/content-api")
from content_domains import cos
cos.upload(sys.argv[1], sys.argv[2], "video/mp4")
"""


def _db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


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
        conn.commit()


def _now():
    return int(time.time())


_NODE_RESULT_METADATA_FIELDS = {
    "duration", "width", "height", "template_id", "engine", "font_mode",
    "font_selection", "font_files", "private_font_bundle_sha256",
    "material_selection_contract_version", "material_clip_contract_version",
    "material_manifest", "editing_plan", "bgm_mode", "nine_grid_visuals",
    "fixed_duration_seconds", "fixed_skill_template",
}


def _merge_completed_result(existing, incoming):
    """Merge renderer evidence without replacing relay-owned delivery fields."""
    result = dict(existing or {})
    if not isinstance(incoming, dict):
        return result
    for key in _NODE_RESULT_METADATA_FIELDS:
        if key in incoming:
            result[key] = incoming[key]
    return result


def _upstream(method, path, body=None, timeout=30):
    """转发只读接口给云端渲染服务。"""
    if not UPSTREAM:
        raise RuntimeError("upstream not configured")
    data = None
    headers = {"Authorization": "Bearer " + UPSTREAM_TOKEN}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(UPSTREAM + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _cos_url(key):
    return "https://%s.cos.%s.myqcloud.com/%s" % (
        COS_BUCKET, os.environ.get("RELAY_COS_REGION", "ap-guangzhou"), key.lstrip("/"))


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

        if p == "/health":
            with _db() as conn:
                pending = conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]
                running = conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
            # templates 必须如实反映上游：黄雀的 availability() 要求
            # int(health["templates"]) 落在 TRANSITION_TEMPLATE_COUNTS = {2,15,19,20}，
            # 缺这个字段会被算成 0 而判定整条渠道「未就绪」——正是它导致 Agent
            # 一直收到「生成渠道正在繁忙或维护」。
            templates = 0
            upstream_ok = False
            try:
                code, raw = _upstream("GET", "/health", timeout=5)
                up = json.loads(raw or b"{}")
                templates = int(up.get("templates") or 0)
                upstream_ok = up.get("ok") is True
            except Exception as exc:
                print("[render-relay] health upstream failed: %s" % exc, flush=True)
            ok = upstream_ok and templates in (2, 15, 19, 20, 22)
            return self._send(200 if ok else 503, {
                "ok": ok, "templates": templates,
                "worker_alive": True, "worker_count": 1,
                "cleanup_worker_alive": True, "worker_degraded": False,
                "degraded_jobs": 0, "pending_jobs": pending, "running_jobs": running,
                "material_library_ready": True, "pexels_material_ready": True,
                "material_selection_contract_version": 2,
                "material_clip_contract_version": 2,
                "material_source_policy": "huangque-bookends-pexels-middle-v1",
            })

        if p.startswith("/v1/jobs/"):
            jid = p[len("/v1/jobs/"):].strip()
            with _db() as conn:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                return self._send(404, {"error": "not_found"})
            out = {"job_id": row["id"], "status": row["status"],
                   "created_at": row["created_at"], "updated_at": row["updated_at"]}
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
            jid = uuid.uuid4().hex
            now = _now()
            with _db() as conn:
                conn.execute(
                    "INSERT INTO jobs(id,payload,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (jid, json.dumps(body, ensure_ascii=False), "pending", now, now))
                conn.commit()
            return self._send(202, {"job_id": jid, "status": "pending",
                                    "created_at": now, "updated_at": now})

        if p == "/v1/user-assets":
            # 用户素材：转发给云端渲染服务（当前生产路径）
            if not self._auth(RELAY_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            n = int(self.headers.get("Content-Length") or "0")
            if n <= 0 or n > MAX_BODY:
                return self._send(400, {"error": "invalid_request", "detail": "素材大小无效"})
            data = self.rfile.read(n)
            headers = {
                "Authorization": "Bearer " + UPSTREAM_TOKEN,
                "Content-Type": self.headers.get("Content-Type") or "application/octet-stream",
                "X-HQ-Asset-Sha256": self.headers.get("X-HQ-Asset-Sha256") or "",
            }
            try:
                req = urllib.request.Request(UPSTREAM + "/v1/user-assets", data=data,
                                             headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=120) as resp:
                    self._send(resp.status, json.loads(resp.read() or b"{}"))
            except urllib.error.HTTPError as exc:
                self._send(exc.code, {"error": "upstream_rejected",
                                      "detail": exc.read().decode("utf-8", "replace")[:200]})
            except Exception as exc:
                self._send(503, {"error": "upstream_failed", "detail": str(exc)[:120]})
            return

        # ---- 节点侧 ----
        if p == "/v1/claim":
            if not self._auth(NODE_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            body = self._body() or {}
            node = str(body.get("node") or "").strip()[:64] or "unknown"
            now = _now()
            if _node_blocked(node, now):
                # 熔断中：不派活，也不记「还在轮询」——否则优先线路判空会误判成它还有空位
                return self._send(200, {"job": None, "deferred": "node_cooldown"})
            # 先记时间戳（被拒也要记，否则高优先级节点的「还在轮询」永远学不到）
            _LAST_CLAIM[node] = now
            if node not in PRIORITY_NODES and _priority_has_room(now):
                # 高优先级线路还有空位：本节点这次不领，让给它。
                # 返回 200 + job=None（与「暂时没活」同形），轮询器会照常隔几秒再来问。
                return self._send(200, {"job": None, "deferred": "priority"})
            with _db() as conn:
                # 回收失联节点的任务
                conn.execute(
                    "UPDATE jobs SET status='pending', node=NULL, updated_at=?"
                    " WHERE status='running' AND claimed_at < ?",
                    (now, now - CLAIM_TIMEOUT))
                row = conn.execute(
                    "SELECT id,payload FROM jobs WHERE status='pending'"
                    " ORDER BY created_at LIMIT 1").fetchone()
                if not row:
                    conn.commit()
                    return self._send(200, {"job": None})
                cur = conn.execute(
                    "UPDATE jobs SET status='running', node=?, claimed_at=?, updated_at=?"
                    " WHERE id=? AND status='pending'",
                    (node, now, now, row["id"]))
                conn.commit()
                if cur.rowcount != 1:
                    return self._send(200, {"job": None})
            return self._send(200, {"job": {"job_id": row["id"],
                                            "payload": json.loads(row["payload"])}})

        if p.startswith("/v1/result/"):
            # 节点回传成品视频（原始字节）。中转器落盘 + 上传 COS，避免节点持有 COS 凭证。
            if not self._auth(NODE_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            jid = p[len("/v1/result/"):].strip()
            with _db() as conn:
                row = conn.execute(
                    "SELECT status, node FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                return self._send(404, {"error": "not_found"})
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
                _row = conn.execute(
                    "SELECT node,status,result FROM jobs WHERE id=?", (jid,)).fetchone()
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
