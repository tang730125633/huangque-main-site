#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黄雀 AI · 内容生成后端 API（能力中心）
=====================================================
架构：能力集中在后端，网页 + 飞书 bot 都来调；点数/额度统一在这里扣。
- 鉴权：复用现有认证服务(:8095)，前端带 Bearer <hq_token>；本服务调 /api/auth/me 校验 + 取 username/points/role。
- 异步任务模型：/api/gen/<能力> 提交 → job_id → 轮询 /api/gen/job/{id}（与 leadgen 同套路）。
- 点数：提交即预扣（够才受理），失败自动退点。点数落在 auth 的 users.db。

端口 127.0.0.1:8096，nginx 把 /api/gen/ 路由过来。零第三方依赖外只用 requests(已在 venv)。

P1：图片(gpt-image-2)。P2 文案 / P3 视频按同样的 register_capability 往里加。
"""
import os, re, sqlite3, json, time, threading, base64, pathlib, urllib.request, urllib.error, urllib.parse, subprocess, uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tikhub  # 同目录 TikHub 客户端（抖音/小红书/视频号 采集+获客）
import mimetypes  # 文件服务按扩展名识别 mime（png / mp3 …）

PORT       = int(os.environ.get("CONTENT_API_PORT", "8096"))
AUTH_BASE  = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095")
AUTH_INTERNAL_TOKEN = os.environ.get("HQ_INTERNAL_TOKEN", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
BASE       = pathlib.Path(__file__).resolve().parents[1]
JOB_DB     = str(BASE / "content_jobs.db")
AUDIO_DB   = str(BASE / "audio_assets.db")
OUT_DIR    = pathlib.Path(os.environ.get("CONTENT_OUT", str(BASE / "content_out")))
OUT_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_OUT_DIR = OUT_DIR / "audio"
VIDEO_OUT_DIR = OUT_DIR / "video"
AUDIO_OUT_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_OUT_DIR.mkdir(parents=True, exist_ok=True)

def _out_path(rel):
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p and p not in {".", ".."}]
    if not parts:
        raise ValueError("文件路径不能为空")
    return OUT_DIR.joinpath(*parts)

def _file_url(rel):
    return "/api/gen/file/" + str(rel or "").replace("\\", "/").lstrip("/")

def _resolve_out_file(rel):
    rel = urllib.parse.unquote(str(rel or "")).replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    fp = OUT_DIR / rel
    if fp.exists() and fp.is_file():
        return fp
    legacy = OUT_DIR / os.path.basename(rel)
    if legacy.exists() and legacy.is_file():
        return legacy
    name = os.path.basename(rel)
    for folder in (AUDIO_OUT_DIR, VIDEO_OUT_DIR):
        fp = folder / name
        if fp.exists() and fp.is_file():
            return fp
    return None

# ---- 能力定义：成本(点数) + 处理函数 ----
COST = {"image": 12, "copy": 3, "audio": 10, "video": 0}  # 视频任务壳暂不扣点；collect/leads 走 cost_of() 动态算
OPENAI_BASE = os.environ.get("OPENAI_BASE", "https://api.openai.com")
ZELONG_KEY  = os.environ.get("ZELONG_KEY", "")                              # 泽龙Ai 中转站(OpenAI 兼容)
ZELONG_BASE = os.environ.get("ZELONG_BASE", "https://api.xiaoleai.team")
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))     # 直连(绕过 HTTPS_PROXY)，给国内中转用
COPY_MODEL  = os.environ.get("COPY_MODEL", "gpt-4o")
TTS_MODEL   = os.environ.get("TTS_MODEL", "gpt-4o-mini-tts")  # 配音(同事的 audio 能力)
DOUBAO_APPID = os.environ.get("DOUBAO_APPID", "")
DOUBAO_TOKEN = os.environ.get("DOUBAO_TOKEN", "")
DOUBAO_CLONE_RESOURCE = os.environ.get("DOUBAO_CLONE_RESOURCE", "volc.megatts.voiceclone")
DOUBAO_CLONE_MODEL_TYPE = int(os.environ.get("DOUBAO_CLONE_MODEL_TYPE", "4"))
DOUBAO_TTS_RESOURCE = os.environ.get("DOUBAO_TTS_RESOURCE", "seed-icl-2.0")
HEYGEN_API_KEY = os.environ.get("HEYGEN_API_KEY", "")
HEYGEN_API_BASE = os.environ.get("HEYGEN_API_BASE", "https://api.heygen.com/v3")
HEYGEN_POLL_INTERVAL = max(3, int(os.environ.get("HEYGEN_POLL_INTERVAL", "8")))
HEYGEN_TIMEOUT = max(60, int(os.environ.get("HEYGEN_TIMEOUT", "1200")))

# Domain handlers are assembled by content_domains.registry at startup.
HANDLERS = {}

# ============ 任务库 ============
def jdb():
    c = sqlite3.connect(JOB_DB, timeout=10); c.row_factory = sqlite3.Row; return c

def adb():
    c = sqlite3.connect(AUDIO_DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with closing(jdb()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, username TEXT, cost INTEGER,
            status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
            created_at INTEGER, updated_at INTEGER)""")
        c.commit()
    init_audio_db()

def init_audio_db():
    now = int(time.time())
    with closing(adb()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS audio_voices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT 'personal',
            voice_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            provider_voice TEXT NOT NULL,
            preview_file TEXT,
            preview_url TEXT,
            created_at INTEGER,
            updated_at INTEGER,
            UNIQUE(scope, username, voice_key)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS audio_assets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER UNIQUE,
            username TEXT NOT NULL,
            voice_id INTEGER,
            voice_key TEXT,
            file TEXT,
            url TEXT,
            text TEXT,
            speed REAL,
            pitch INTEGER,
            volume INTEGER,
            created_at INTEGER
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS voice_slot_pool(
            slot_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'available',
            assigned_user_id INTEGER,
            assigned_username TEXT,
            assigned_at INTEGER,
            created_at INTEGER NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS audio_voice_slots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            user_id INTEGER,
            slot_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            voice_id INTEGER,
            created_at INTEGER,
            updated_at INTEGER,
            UNIQUE(username, slot_id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS voice_slot_codes(
            code TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'unused',
            assigned_slot_id TEXT,
            used_user_id INTEGER,
            used_username TEXT,
            used_at INTEGER,
            created_at INTEGER NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS video_assets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER UNIQUE,
            username TEXT NOT NULL,
            mode TEXT NOT NULL,
            image_file TEXT,
            audio_file TEXT,
            reference_video_file TEXT,
            video_file TEXT,
            video_url TEXT,
            text TEXT,
            voice_key TEXT,
            resolution TEXT,
            ratio TEXT,
            motion TEXT,
            phase TEXT,
            image_asset_id TEXT,
            audio_asset_id TEXT,
            reference_asset_id TEXT,
            provider_video_id TEXT,
            provider_avatar_id TEXT,
            provider_avatar_group_id TEXT,
            source_video_url TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS avatars(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            name TEXT NOT NULL,
            image_file TEXT NOT NULL,
            provider_avatar_id TEXT NOT NULL,
            provider_avatar_group_id TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            created_at INTEGER,
            updated_at INTEGER,
            UNIQUE(username, provider_avatar_id)
        )""")
        _ensure_column(c, "audio_voices", "slot_id", "TEXT")
        _ensure_column(c, "audio_voice_slots", "reclone_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(c, "audio_voice_slots", "clone_started_at", "INTEGER")
        _ensure_column(c, "audio_voice_slots", "previous_preview_url", "TEXT")
        _ensure_column(c, "audio_voice_slots", "clone_upload_at", "INTEGER")
        _ensure_column(c, "audio_voice_slots", "clone_error", "TEXT")
        _ensure_column(c, "audio_voice_slots", "clone_upload_speaker_id", "TEXT")
        _ensure_column(c, "audio_voice_slots", "clone_upload_response", "TEXT")
        _ensure_column(c, "audio_voice_slots", "clone_baseline_version", "TEXT")
        _ensure_column(c, "audio_voice_slots", "clone_baseline_icl_speaker_id", "TEXT")
        _ensure_column(c, "audio_voice_slots", "clone_baseline_demo_audio", "TEXT")
        _ensure_column(c, "video_assets", "reference_video_file", "TEXT")
        _ensure_column(c, "video_assets", "phase", "TEXT")
        _ensure_column(c, "video_assets", "image_asset_id", "TEXT")
        _ensure_column(c, "video_assets", "audio_asset_id", "TEXT")
        _ensure_column(c, "video_assets", "reference_asset_id", "TEXT")
        _ensure_column(c, "video_assets", "provider_video_id", "TEXT")
        _ensure_column(c, "video_assets", "provider_avatar_id", "TEXT")
        _ensure_column(c, "video_assets", "provider_avatar_group_id", "TEXT")
        _ensure_column(c, "video_assets", "source_video_url", "TEXT")
        _ensure_column(c, "avatars", "provider_avatar_group_id", "TEXT")
        _ensure_column(c, "avatars", "status", "TEXT NOT NULL DEFAULT 'ready'")
        public = [
            ("public", "", "S_d21F8OR62", "\u516c\u5171\u97f3\u8272 1", "S_d21F8OR62"),
            ("public", "", "S_l8wE8OR62", "\u516c\u5171\u97f3\u8272 2", "S_l8wE8OR62"),
            ("public", "", "S_pa0E8OR62", "\u516c\u5171\u97f3\u8272 3", "S_pa0E8OR62"),
            ("public", "", "S_xaUB8OR62", "\u516c\u5171\u97f3\u8272 4", "S_xaUB8OR62"),
        ]
        for scope, username, voice_key, display_name, provider_voice in public:
            c.execute("""INSERT OR IGNORE INTO audio_voices
                (scope, username, voice_key, display_name, provider_voice, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?)""",
                (scope, username, voice_key, display_name, provider_voice, now, now))
        c.commit()
    _domains()[0].backfill_audio_assets()

def _ensure_column(c, table, column, spec):
    cols = [r["name"] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()]
    if column not in cols:
        c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, spec))




# ============ 鉴权（向 auth 服务核验 token） ============
def verify(token):
    if not token: return None
    try:
        req = urllib.request.Request(AUTH_BASE + "/api/auth/me",
                                     headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read()).get("user")
    except Exception:
        return None

def _domains():
    from . import audio, points, video
    return audio, points, video

# ============ 图片能力：gpt-image-2 ============
# 三种模式同一入口：无图=文生图(generations)；有图无蒙版=图生图(edits)；有图有蒙版=局部修改(edits+mask)
SIZES = {"1:1": "1024x1024", "9:16": "1024x1536", "16:9": "1536x1024", "3:4": "1024x1536"}

def _multipart(fields, files):
    """手搓 multipart/form-data；files=[(name, filename, bytes)]"""
    b = "----hqcontent7e3f"
    out = []
    for k, v in fields.items():
        out.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode())
    for name, fn, data in files:
        out.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: image/png\r\n\r\n' % (b, name, fn)).encode())
        out.append(data); out.append(b"\r\n")
    out.append(("--%s--\r\n" % b).encode())
    return b"".join(out), "multipart/form-data; boundary=" + b

def _post(path, data, ctype, base=None, key=None, proxy=True):
    req = urllib.request.Request((base or OPENAI_BASE) + path, data=data,
                                 headers={"Authorization": "Bearer " + (key or OPENAI_KEY), "Content-Type": ctype}, method="POST")
    if proxy:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    with _NOPROXY.open(req, timeout=300) as r:  # 国内中转直连，不走 mihomo
        return json.loads(r.read())

def _post_bytes(path, data, ctype):  # 返回原始字节(TTS 拿 mp3 二进制)
    req = urllib.request.Request(OPENAI_BASE + path, data=data,
                                 headers={"Authorization": "Bearer " + OPENAI_KEY, "Content-Type": ctype}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()




# ============ 后台 worker（串行跑任务，失败退点） ============
def run_job(job_id):
    with closing(jdb()) as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r: return
    kind = r["kind"]; payload = json.loads(r["payload"] or "{}")
    if kind in {"audio", "video"}:
        payload["_username"] = r["username"]
        payload["_job_id"] = job_id
    try:
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (int(time.time()), job_id)); c.commit()
        result = HANDLERS[kind](payload)
        audio_domain, _, video_domain = _domains()
        if kind == "audio":
            audio_domain.record_audio_asset(job_id, r["username"], result)
        if kind == "video":
            video_domain.record_video_asset(job_id, r["username"], result)
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET status='done', result=?, updated_at=? WHERE id=?",
                      (json.dumps(result, ensure_ascii=False), int(time.time()), job_id)); c.commit()
    except Exception as e:
        if kind == "video":
            try:
                failed = dict(payload)
                failed.update({"status": "failed", "error": str(e)[:300]})
                _, _, video_domain = _domains()
                video_domain.record_video_asset(job_id, r["username"], failed)
            except Exception:
                pass
        points_domain = _domains()[1]
        points_domain.safe_refund_points(r["username"], r["cost"])  # 失败退点
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?",
                      (str(e)[:300], int(time.time()), job_id)); c.commit()

# ============ 超时清道夫：running 超 6 分钟的僵尸任务自动判失败 + 退点 ============
def reaper():
    while True:
        try:
            cutoff = int(time.time()) - 360
            with closing(jdb()) as c:
                stuck = c.execute("SELECT id, username, cost, kind, updated_at FROM jobs WHERE status='running' AND updated_at < ?", (cutoff,)).fetchall()
                for r in stuck:
                    if r["kind"] == "video" and r["updated_at"] >= int(time.time()) - 1800:
                        continue
                    if r["kind"] == "image" and r["updated_at"] >= int(time.time()) - 900:
                        continue  # 多图/中转出图慢，给 image 15 分钟余量
                    points_domain = _domains()[1]
                    points_domain.safe_refund_points(r["username"], r["cost"])  # ??
                    c.execute("UPDATE jobs SET status='error', error='生成超时自动结束(>6分钟)，已退点', updated_at=? WHERE id=?",
                              (int(time.time()), r["id"]))
                if stuck: c.commit()
        except Exception:
            pass
        time.sleep(60)

# ============ HTTP ============
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _token(self):
        a = self.headers.get("Authorization") or ""
        return a[7:].strip() if a.startswith("Bearer ") else ""
    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception: return {}

    def do_POST(self):
        p = self.path.split("?")[0]
        audio_domain, points_domain, video_domain = _domains()
        if p == "/api/gen/audio/redeem-slot":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            body = self._json_body()
            try:
                slot = audio_domain.redeem_audio_voice_slot(user["username"], body.get("code"))
                return self._send(200, {"ok": True, "slot": slot})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p == "/api/gen/audio/voice-name":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            body = self._json_body()
            try:
                voice = audio_domain.rename_audio_voice(user["username"], body.get("slot_id"), body.get("name"))
                return self._send(200, {"ok": True, "voice": voice})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p == "/api/gen/audio/clone-vip":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            body = self._json_body()
            try:
                voice = audio_domain.mark_clone_training(user["username"], body.get("slot_id"), body.get("name"))
                threading.Thread(target=audio_domain.clone_vip_voice_background, args=(user["username"], body), daemon=True).start()
                return self._send(200, {"ok": True, "voice": voice})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:220]})
        if p == "/api/gen/video/avatar-name":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            body = self._json_body()
            try:
                avatar = video_domain.rename_video_avatar(user["username"], body.get("id"), body.get("name"))
                return self._send(200, {"ok": True, "avatar": avatar})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p == "/api/gen/video/avatar-delete":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            body = self._json_body()
            try:
                return self._send(200, {"ok": True, "avatar": video_domain.delete_video_avatar(user["username"], body.get("id"))})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p.startswith("/api/gen/") and p[9:] in HANDLERS:
            kind = p[9:]
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录或登录已过期"})
            body = self._json_body()
            cost = points_domain.cost_of(kind, body)
            try:
                points_left = points_domain.deduct_points(user["username"], cost)  # 原子预扣
            except points_domain.AuthPointsError as e:
                code = 402 if e.status == 402 else 502
                return self._send(code, {"detail": e.detail, "need": cost})
            now = int(time.time())
            with closing(jdb()) as c:
                cur = c.execute("INSERT INTO jobs(kind,username,cost,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                (kind, user["username"], cost, json.dumps(body, ensure_ascii=False), now, now))
                c.commit(); jid = cur.lastrowid
            if kind == "video":
                video_domain.record_video_pending_asset(jid, user["username"], body)
            threading.Thread(target=run_job, args=(jid,), daemon=True).start()
            return self._send(200, {"job_id": jid, "cost": cost, "points_left": points_left})
        self._send(404, {"detail": "not found"})

    def do_GET(self):
        p = self.path.split("?")[0]
        audio_domain, points_domain, video_domain = _domains()
        if p.startswith("/api/gen/job/"):
            try: jid = int(p.rsplit("/", 1)[1])
            except Exception: return self._send(400, {"detail": "bad id"})
            with closing(jdb()) as c:
                r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r: return self._send(404, {"detail": "任务不存在"})
            d = dict(r)
            if d.get("result"):
                try: d["result"] = json.loads(d["result"])
                except Exception: pass
            if d.get("kind") == "video":
                d["phase"] = video_domain.get_video_job_phase(jid)
            return self._send(200, d)
        if p == "/api/gen/dl":   # 无水印视频下载代理：直连拉 CDN → 附件流回(强制下载)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            url = (q.get("url", [""])[0]).strip()
            raw_name = ((q.get("name", ["video"])[0])[:40]) or "video"
            ascii_name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", raw_name).strip("_") or "video"  # header 必须 ASCII
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            ALLOW = (".zjcdn.com", ".douyinvod.com", ".douyinstatic.com", ".douyinpic.com", ".amemv.com",
                     ".bytecdn.cn", ".ixigua.com", ".pstatp.com", ".snssdk.com", ".byteimg.com",
                     ".xhscdn.com", ".rednotecdn.com", ".xiaohongshu.com")  # 防 SSRF：只允许已知视频 CDN
            if not (url.startswith("http") and any(host.endswith(h) for h in ALLOW)):
                return self._send(400, {"detail": "不支持的下载地址"})
            try:
                req = urllib.request.Request(url, headers={"User-Agent": tikhub.UA})
                up = tikhub._OPENER.open(req, timeout=120)  # 直连，绕过环境代理
            except Exception as e:
                return self._send(502, {"detail": "下载失败:" + str(e)[:80]})
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition",
                             "attachment; filename=\"%s.mp4\"; filename*=UTF-8''%s" % (ascii_name, urllib.parse.quote(raw_name + ".mp4")))
            clen = up.headers.get("Content-Length")
            if clen: self.send_header("Content-Length", clen)
            self.end_headers()
            try:
                while True:
                    chunk = up.read(65536)
                    if not chunk: break
                    self.wfile.write(chunk)
            except Exception:
                pass
            finally:
                up.close()
            return
        if p.startswith("/api/gen/file/"):
            rel = p[len("/api/gen/file/"):]
            fp = _resolve_out_file(rel)
            if not fp: return self._send(404, {"detail": "no file"})
            fn = fp.name
            data = fp.read_bytes()
            ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
            self.send_response(200); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if fn.startswith("voice_preview_"):
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
            else:
                self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers(); self.wfile.write(data); return
        if p == "/api/gen/audio/voices":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "???"})
            return self._send(200, {"items": audio_domain.list_audio_voices(user["username"])})
        if p == "/api/gen/audio/assets":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "???"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try: lim = int((q.get("limit") or ["120"])[0])
            except Exception: lim = 120
            return self._send(200, {"items": audio_domain.list_audio_assets(user["username"], lim)})
        if p == "/api/gen/video/avatars":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try: lim = int((q.get("limit") or ["120"])[0])
            except Exception: lim = 120
            return self._send(200, {"items": video_domain.list_video_avatars(user["username"], lim)})
        if p == "/api/gen/video/assets":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try: lim = int((q.get("limit") or ["120"])[0])
            except Exception: lim = 120
            return self._send(200, {"items": video_domain.list_video_assets(user["username"], lim)})
        if p == "/api/gen/audio/slots":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            return self._send(200, {"items": audio_domain.list_user_audio_voice_slots(user["username"])})
        if p == "/api/gen/audio/clone-status":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                return self._send(200, {"ok": True, "result": audio_domain.check_clone_status(user["username"], (q.get("slot_id") or [""])[0])})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:220]})
        if p == "/api/gen/history":   # 本人生成历史（资产/最近作品都读这）
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            try: lim = min(120, int(self.path.split("limit=")[1].split("&")[0])) if "limit=" in self.path else 60
            except Exception: lim = 60
            kind = self.path.split("kind=")[1].split("&")[0] if "kind=" in self.path else "image"
            if kind not in HANDLERS: kind = "image"
            with closing(jdb()) as c:
                rows = c.execute("SELECT id,result,created_at FROM jobs WHERE username=? AND status='done' AND kind=? ORDER BY id DESC LIMIT ?",
                                 (user["username"], kind, lim)).fetchall()
            items = []
            for r in rows:
                try: res = json.loads(r["result"])
                except Exception: continue
                items.append({"job_id": r["id"], "url": res.get("url"), "mode": res.get("mode"),
                              "prompt": res.get("prompt"), "text": res.get("text"), "ctype": res.get("ctype"),
                              "voice": res.get("voice"), "speed": res.get("speed"), "pitch": res.get("pitch"),
                              "volume": res.get("volume"), "emotion": res.get("emotion"),
                              "created_at": r["created_at"]})
            return self._send(200, {"items": items})
        if p == "/api/gen/collect/search":   # 关键词搜（即时，扣 1 点）— 采集页选片用
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            platform = (q.get("platform", ["douyin"])[0]).strip()
            keyword  = (q.get("keyword", [""])[0]).strip()
            try: page = int(q.get("page", ["1"])[0] or 1)
            except Exception: page = 1
            if not keyword: return self._send(400, {"detail": "缺少关键词"})
            try:
                points_left = points_domain.deduct_points(user["username"], 1)
            except points_domain.AuthPointsError as e:
                code = 402 if e.status == 402 else 502
                return self._send(code, {"detail": e.detail, "need": 1})
            try:
                r = tikhub.search(platform, keyword, page=page, video_only=False)  # 含图文
            except tikhub.TikHubError as e:
                points_domain.safe_refund_points(user["username"], 1)
                return self._send(502, {"detail": str(e)[:160]})
            items = [{"id": it.get("id"), "platform": it.get("platform"), "title": it.get("title"),
                      "cover": it.get("cover"), "author": it.get("author"), "url": it.get("url"),
                      "note_type": it.get("note_type"),
                      "stats": {"like": it.get("like"), "comment": it.get("comment")}} for it in (r.get("items") or [])]
            return self._send(200, {"items": items, "cost": 1, "points_left": points_left})
        if p == "/api/gen/health":
            return self._send(200, {"ok": True, "service": "huangque-content", "caps": list(HANDLERS),
                                    "has_openai": bool(OPENAI_KEY), "has_tikhub": bool(tikhub.KEY), "tikhub_base": tikhub.BASE})
        self._send(404, {"detail": "not found"})

if __name__ == "__main__":
    init_db()
    threading.Thread(target=reaper, daemon=True).start()  # 僵尸任务清道夫
    print("huangque-content-api on 127.0.0.1:%d  caps=%s" % (PORT, list(HANDLERS)))
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
