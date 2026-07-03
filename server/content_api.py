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
import os, re, sqlite3, json, time, threading, base64, pathlib, urllib.request, urllib.error, urllib.parse, subprocess
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tikhub  # 同目录 TikHub 客户端（抖音/小红书/视频号 采集+获客）
import mimetypes  # 文件服务按扩展名识别 mime（png / mp3 …）

PORT       = int(os.environ.get("CONTENT_API_PORT", "8096"))
AUTH_BASE  = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095")
AUTH_DB    = os.environ.get("AUTH_DB", "/home/ubuntu/auth-service/users.db")  # 点数扣减直接落这
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
BASE       = pathlib.Path(__file__).resolve().parent
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
COST = {"image": 12, "copy": 3, "audio": 4, "video": 0}  # 视频任务壳暂不扣点；collect/leads 走 cost_of() 动态算
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

def cost_of(kind, body):
    """动态点数：TikHub 按次计费，采集/获客调用数随参数变。约 5x buff 折算成点。"""
    if kind == "collect":
        return 3 + (3 if "transcript" in (body.get("want") or []) else 0)
    if kind == "leads":
        n = max(1, min(30, int(body.get("count") or 12)))
        p = max(1, min(3, int(body.get("pages") or 1)))
        return 6 + (n * p) // 4
    if kind == "image":
        base = 12 if (body.get("quality") or "hd") == "hd" else 8  # 高清12/标准8(gpt-image2)
        cap = 2 if (body.get("provider") or "").strip().lower() == "zelong" else 4
        cnt = 1 if body.get("mask") else max(1, min(cap, int(body.get("count") or 1)))
        return base * cnt  # 质量基价 × 数量
    return COST.get(kind, 0)

# ============ 任务库 ============
def jdb():
    c = sqlite3.connect(JOB_DB, timeout=10); c.row_factory = sqlite3.Row; return c

def adb():
    c = sqlite3.connect(AUDIO_DB, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.execute("ATTACH DATABASE ? AS auth", (AUTH_DB,))
    except Exception:
        pass
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
        public = [
            ("public", "", "dapeng", "\u5927\u9e4f IVC", VOICE_MAP.get("dapeng", "alloy")),
            ("public", "", "zelong", "\u6cfd\u9f99 IVC", VOICE_MAP.get("zelong", "onyx")),
            ("public", "", "paul", "Paul \u7537\u58f0", VOICE_MAP.get("paul", "echo")),
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
    backfill_audio_assets()

def _ensure_column(c, table, column, spec):
    cols = [r["name"] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()]
    if column not in cols:
        c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, spec))

def get_user_id(username):
    try:
        with closing(sqlite3.connect(AUTH_DB, timeout=10)) as c:
            r = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            return r[0] if r else None
    except Exception:
        return None

def assign_audio_voice_slot(username):
    username = (username or "").strip()
    if not username:
        raise ValueError("missing username")
    user_id = get_user_id(username)
    now = int(time.time())
    with closing(adb()) as c:
        c.execute("BEGIN IMMEDIATE")
        slot = c.execute("""SELECT slot_id FROM voice_slot_pool
            WHERE status='available'
            ORDER BY created_at, slot_id
            LIMIT 1""").fetchone()
        if not slot:
            c.rollback()
            raise ValueError("\u6682\u65e0\u53ef\u5206\u914d\u7684\u97f3\u8272\u69fd\u4f4d")
        slot_id = slot["slot_id"]
        cur = c.execute("""UPDATE voice_slot_pool
            SET status='assigned', assigned_user_id=?, assigned_username=?, assigned_at=?
            WHERE slot_id=? AND status='available'""", (user_id, username, now, slot_id))
        if cur.rowcount != 1:
            c.rollback()
            raise ValueError("\u69fd\u4f4d\u5206\u914d\u51b2\u7a81\uff0c\u8bf7\u91cd\u8bd5")
        c.execute("""INSERT INTO audio_voice_slots
            (username, user_id, slot_id, status, created_at, updated_at)
            VALUES(?,?,?,?,?,?)""", (username, user_id, slot_id, "active", now, now))
        c.commit()
        return {"slot_id": slot_id, "username": username, "user_id": user_id, "status": "active"}

def redeem_audio_voice_slot(username, code):
    username = (username or "").strip()
    code = (code or "").strip()
    if not username:
        raise ValueError("missing username")
    if not code:
        raise ValueError("\u8bf7\u8f93\u5165\u5151\u6362\u7801")
    user_id = get_user_id(username)
    now = int(time.time())
    with closing(adb()) as c:
        c.execute("BEGIN IMMEDIATE")
        rc = c.execute("""SELECT code, status FROM voice_slot_codes
            WHERE code=?""", (code,)).fetchone()
        if not rc:
            c.rollback()
            raise ValueError("\u5151\u6362\u7801\u4e0d\u5b58\u5728")
        if rc["status"] != "unused":
            c.rollback()
            raise ValueError("\u5151\u6362\u7801\u5df2\u4f7f\u7528\u6216\u5df2\u5931\u6548")
        slot = c.execute("""SELECT slot_id FROM voice_slot_pool
            WHERE status='available'
            ORDER BY created_at, slot_id
            LIMIT 1""").fetchone()
        if not slot:
            c.rollback()
            raise ValueError("\u6682\u65e0\u53ef\u5206\u914d\u7684\u97f3\u8272\u69fd\u4f4d")
        slot_id = slot["slot_id"]
        cur = c.execute("""UPDATE voice_slot_pool
            SET status='assigned', assigned_user_id=?, assigned_username=?, assigned_at=?
            WHERE slot_id=? AND status='available'""", (user_id, username, now, slot_id))
        if cur.rowcount != 1:
            c.rollback()
            raise ValueError("\u69fd\u4f4d\u5206\u914d\u51b2\u7a81\uff0c\u8bf7\u91cd\u8bd5")
        c.execute("""INSERT INTO audio_voice_slots
            (username, user_id, slot_id, status, created_at, updated_at)
            VALUES(?,?,?,?,?,?)""", (username, user_id, slot_id, "active", now, now))
        cur = c.execute("""UPDATE voice_slot_codes
            SET status='used', assigned_slot_id=?, used_user_id=?, used_username=?, used_at=?
            WHERE code=? AND status='unused'""", (slot_id, user_id, username, now, code))
        if cur.rowcount != 1:
            c.rollback()
            raise ValueError("\u5151\u6362\u7801\u72b6\u6001\u66f4\u65b0\u5931\u8d25")
        c.commit()
        return {"slot_id": slot_id, "username": username, "user_id": user_id, "status": "active"}

def list_user_audio_voice_slots(username):
    with closing(adb()) as c:
        rows = c.execute("""SELECT s.id, s.username, s.user_id, s.slot_id, s.status, s.voice_id, COALESCE(s.reclone_count, 0) AS reclone_count,
                   s.created_at, s.updated_at, s.clone_started_at, s.clone_upload_at, s.clone_error,
                   s.clone_upload_speaker_id, s.clone_upload_response,
                   v.display_name AS voice_name, v.preview_file, v.preview_url, v.updated_at AS voice_updated_at
            FROM audio_voice_slots s
            LEFT JOIN audio_voices v ON v.id = s.voice_id
            WHERE s.username=?
            ORDER BY s.id DESC""", (username,)).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        if d.get("slot_id") and d.get("status") in ("training", "ready") and not d.get("preview_url"):
            try:
                synced = check_clone_status(username, d["slot_id"])
                if synced.get("status"):
                    d["status"] = synced["status"]
                if synced.get("preview_url"):
                    d["preview_url"] = synced["preview_url"]
            except Exception as e:
                print("[list_user_audio_voice_slots] sync failed username=%s slot_id=%s error=%s" %
                      (username, d.get("slot_id"), str(e)[:240]), flush=True)
        if d.get("preview_url") and d.get("voice_id") and d.get("status") == "training":
            d["status"] = "ready"
        items.append(d)
    return items

def generate_doubao_preview(speaker_id, text=None, speech_rate=0, loudness_rate=0, pitch_rate=0):
    text = (text or "\u4f60\u597d\uff0c\u8fd9\u662f\u6211\u7684\u4e13\u5c5e\u590d\u523b\u97f3\u8272\u8bd5\u542c\u3002\u58f0\u97f3\u6e05\u6670\u81ea\u7136\uff0c\u9002\u5408\u7528\u4e8e\u77ed\u89c6\u9891\u53e3\u64ad\u548c\u6587\u6848\u914d\u97f3\u3002").strip()
    reqid = "hq_preview_%d" % int(time.time() * 1000)
    body = json.dumps({
        "user": {"uid": "huangque"},
        "req_params": {
            "text": text,
            "speaker": speaker_id,
            "audio_params": {
                "format": "mp3",
                "sample_rate": 24000,
                "speech_rate": int(speech_rate or 0),
                "loudness_rate": int(loudness_rate or 0),
                "pitch_rate": int(pitch_rate or 0),
            },
            "additions": json.dumps({"explicit_language": "zh", "disable_markdown_filter": True}),
        },
    }, ensure_ascii=False).encode()
    req = urllib.request.Request("https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Api-App-Id": DOUBAO_APPID,
            "X-Api-Access-Key": DOUBAO_TOKEN,
            "X-Api-Resource-Id": DOUBAO_TTS_RESOURCE,
            "X-Api-Request-Id": reqid,
        },
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ValueError("\u8bd5\u542c\u97f3\u9891\u751f\u6210\u5931\u8d25: " + detail)
    chunks = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(b"data:"):
            line = line[5:].strip()
        elif line.startswith(b"event:"):
            continue
        try:
            d = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            continue
        if d.get("code") == 20000000:
            break
        data = d.get("data") or d.get("audio") or d.get("audio_data")
        if isinstance(data, str) and data:
            try:
                chunks.append(base64.b64decode(data))
            except Exception:
                pass
        if d.get("code") not in (None, 0, 20000000) or d.get("error") or d.get("message") == "error":
            raise ValueError(json.dumps(d, ensure_ascii=False)[:200])
    if not chunks:
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
            data = d.get("data") or d.get("audio") or d.get("audio_data")
            if isinstance(data, str) and data:
                chunks.append(base64.b64decode(data))
        except Exception:
            pass
    if not chunks:
        raise ValueError("\u8bd5\u542c\u97f3\u9891\u751f\u6210\u8fd4\u56de\u4e3a\u7a7a")
    fn = "audio/voice_preview_%d.mp3" % int(time.time() * 1000)
    _out_path(fn).write_bytes(b"".join(chunks))
    return {"file": fn, "url": _file_url(fn), "text": text}

def query_doubao_clone_status(slot_id):
    body = json.dumps({"appid": DOUBAO_APPID, "speaker_id": slot_id}).encode()
    req = urllib.request.Request("https://openspeech.bytedance.com/api/v1/mega_tts/status",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer;" + DOUBAO_TOKEN,
            "Resource-Id": DOUBAO_CLONE_RESOURCE,
        },
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            resp = json.loads(raw or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ValueError("\u8c46\u5305\u590d\u523b\u72b6\u6001\u67e5\u8be2\u5931\u8d25: " + detail)
    base = resp.get("BaseResp") or resp.get("base_resp") or {}
    code = base.get("StatusCode", base.get("status_code", resp.get("code", 0)))
    try:
        code_i = int(code)
    except Exception:
        code_i = 0 if code in ("0", "OK", "ok", None) else -1
    if code_i not in (0,):
        msg = base.get("StatusMessage") or base.get("status_message") or resp.get("message") or json.dumps(resp, ensure_ascii=False)[:200]
        raise ValueError("\u8c46\u5305\u590d\u523b\u72b6\u6001\u5f02\u5e38: " + str(msg)[:200])
    return resp

def finalize_ready_voice(username, slot_id, display_name=None, demo_audio=None, preview_file=None):
    now = int(time.time())
    voice_key = "vip_" + re.sub(r"[^a-zA-Z0-9_\\-]", "_", slot_id)
    name = (display_name or "\u6211\u7684VIP\u590d\u523b\u97f3\u8272").strip()[:40]
    with closing(adb()) as c:
        c.execute("""INSERT OR IGNORE INTO audio_voices
            (username, scope, voice_key, display_name, provider_voice, preview_file, preview_url, slot_id, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (username, "personal", voice_key, name, slot_id, preview_file, demo_audio, slot_id, now, now))
        c.execute("""UPDATE audio_voices
            SET display_name=?, provider_voice=?, preview_file=?, preview_url=?, slot_id=?, updated_at=?
            WHERE username=? AND scope='personal' AND voice_key=?""",
            (name, slot_id, preview_file, demo_audio, slot_id, now, username, voice_key))
        r = c.execute("SELECT id FROM audio_voices WHERE username=? AND scope='personal' AND voice_key=?",
                      (username, voice_key)).fetchone()
        voice_id = r["id"] if r else None
        c.execute("""UPDATE audio_voice_slots SET voice_id=?, status='ready', clone_started_at=NULL, previous_preview_url=NULL, clone_error=NULL, updated_at=?
            WHERE username=? AND slot_id=?""", (voice_id, now, username, slot_id))
        c.commit()
    return {"voice_id": voice_id, "voice_key": voice_key, "display_name": name, "preview_file": preview_file, "preview_url": demo_audio, "status": "ready"}

def clear_voice_preview(username, slot_id):
    username = (username or "").strip()
    slot_id = (slot_id or "").strip()
    if not username or not slot_id:
        return 0
    removed = 0
    with closing(adb()) as c:
        rows = c.execute("""SELECT id, preview_file, preview_url FROM audio_voices
            WHERE username=? AND slot_id=?""", (username, slot_id)).fetchall()
        for r in rows:
            refs = []
            if r["preview_file"]:
                refs.append(str(r["preview_file"]))
            url = r["preview_url"] or ""
            if url.startswith("/api/gen/file/"):
                refs.append(url[len("/api/gen/file/"):])
            for ref in refs:
                name = os.path.basename(str(ref))
                if name.startswith("voice_preview_") and name.endswith(".mp3"):
                    fp = _resolve_out_file(ref)
                    try:
                        if fp and fp.exists():
                            fp.unlink()
                            removed += 1
                    except Exception as e:
                        print("[clear_voice_preview] delete failed file=%s error=%s" % (name, str(e)[:200]), flush=True)
        c.execute("""UPDATE audio_voices SET preview_file=NULL, preview_url=NULL, updated_at=?
            WHERE username=? AND slot_id=?""", (int(time.time()), username, slot_id))
        c.commit()
    return removed

def check_clone_status(username, slot_id):
    username = (username or "").strip()
    slot_id = (slot_id or "").strip()
    with closing(adb()) as c:
        slot = c.execute("""SELECT id, slot_id, status, voice_id, clone_started_at, clone_upload_at, clone_error,
                   clone_baseline_version, clone_baseline_icl_speaker_id, clone_baseline_demo_audio
            FROM audio_voice_slots
            WHERE username=? AND slot_id=?""", (username, slot_id)).fetchone()
        voice = c.execute("""SELECT display_name, preview_url FROM audio_voices
            WHERE username=? AND slot_id=? ORDER BY id DESC LIMIT 1""", (username, slot_id)).fetchone()
    if not slot:
        raise ValueError("\u97f3\u8272\u69fd\u4f4d\u4e0d\u5b58\u5728\u6216\u4e0d\u5c5e\u4e8e\u5f53\u524d\u8d26\u53f7")
    if slot["status"] == "failed":
        return {"status": "failed", "clone_error": slot["clone_error"] or "\u8c46\u5305\u590d\u523b\u5931\u8d25", "doubao_status": None}
    if slot["status"] == "ready" and voice and voice["preview_url"] and not slot["clone_started_at"]:
        return {"status": "ready", "preview_url": voice["preview_url"], "doubao_status": 2}
    try:
        resp = query_doubao_clone_status(slot_id)
    except Exception:
        if slot["status"] == "training":
            return {"status": "training", "doubao_status": None}
        raise
    st = resp.get("status")
    demo = resp.get("demo_audio")
    version = str(resp.get("version") or "")
    icl_speaker_id = str(resp.get("icl_speaker_id") or "")
    create_time = resp.get("create_time") or resp.get("createTime") or resp.get("created_at")
    try:
        create_time_i = int(create_time or 0)
    except Exception:
        create_time_i = 0
    clone_started_at = int(slot["clone_started_at"] or 0)
    clone_upload_at = int(slot["clone_upload_at"] or 0)
    baseline_version = str(slot["clone_baseline_version"] or "")
    baseline_icl = str(slot["clone_baseline_icl_speaker_id"] or "")
    baseline_demo = str(slot["clone_baseline_demo_audio"] or "")
    same_as_baseline = bool(baseline_version or baseline_icl or baseline_demo) and (
        (not baseline_version or version == baseline_version) and
        (not baseline_icl or icl_speaker_id == baseline_icl) and
        (not baseline_demo or str(demo or "") == baseline_demo)
    )
    if st == 2 and same_as_baseline:
        return {
            "status": "training",
            "doubao_status": st,
            "doubao_create_time": create_time_i,
            "doubao_version": version,
            "doubao_icl_speaker_id": icl_speaker_id,
            "clone_started_at": clone_started_at,
            "clone_upload_at": clone_upload_at,
            "stale_result": True,
        }
    if st == 2:
        try:
            preview = generate_doubao_preview(slot_id)
            preview_url = preview.get("url")
            preview_file = preview.get("file")
        except Exception as e:
            err = "\u6d4b\u8bd5\u97f3\u9891\u751f\u6210\u5931\u8d25: " + str(e)[:220]
            print("[check_clone_status] preview tts failed username=%s slot_id=%s error=%s" %
                  (username, slot_id, str(e)[:240]), flush=True)
            with closing(adb()) as c:
                c.execute("UPDATE audio_voice_slots SET status='failed', clone_error=?, updated_at=? WHERE username=? AND slot_id=?",
                          (err, int(time.time()), username, slot_id))
                c.commit()
            return {"status": "failed", "clone_error": err, "doubao_status": st, "doubao_demo_audio": demo}
        if not preview_url:
            err = "\u6d4b\u8bd5\u97f3\u9891\u751f\u6210\u8fd4\u56de\u4e3a\u7a7a"
            with closing(adb()) as c:
                c.execute("UPDATE audio_voice_slots SET status='failed', clone_error=?, updated_at=? WHERE username=? AND slot_id=?",
                          (err, int(time.time()), username, slot_id))
                c.commit()
            return {"status": "failed", "clone_error": err, "doubao_status": st, "doubao_demo_audio": demo}
        v = finalize_ready_voice(username, slot_id, voice["display_name"] if voice else None, preview_url, preview_file)
        return {"status": "ready", "preview_url": preview_url, "voice": v, "doubao_status": st, "doubao_demo_audio": demo, "doubao_create_time": create_time_i}
    if st == 3:
        with closing(adb()) as c:
            c.execute("UPDATE audio_voice_slots SET status='failed', updated_at=? WHERE username=? AND slot_id=?",
                      (int(time.time()), username, slot_id))
            c.commit()
        return {"status": "failed", "doubao_status": st}
    return {"status": "training", "doubao_status": st}

def mark_clone_training(username, slot_id, name):
    username = (username or "").strip()
    slot_id = (slot_id or "").strip()
    name = (name or "\u6211\u7684VIP\u590d\u523b\u97f3\u8272").strip()[:40]
    now = int(time.time())
    voice_key = "vip_" + re.sub(r"[^a-zA-Z0-9_\\-]", "_", slot_id)
    with closing(adb()) as c:
        slot = c.execute("""SELECT id, status, voice_id, COALESCE(reclone_count, 0) AS reclone_count, updated_at, clone_upload_at FROM audio_voice_slots
            WHERE username=? AND slot_id=?""",
            (username, slot_id)).fetchone()
        if not slot:
            raise ValueError("\u97f3\u8272\u69fd\u4f4d\u4e0d\u5b58\u5728\u6216\u4e0d\u5c5e\u4e8e\u5f53\u524d\u8d26\u53f7")
        if slot["status"] == "training":
            last_at = int(slot["clone_upload_at"] or slot["updated_at"] or 0)
            if last_at and now - last_at < 600:
                raise ValueError("\u97f3\u8272\u6b63\u5728\u590d\u523b\u4e2d\uff0c\u8bf7\u7b49\u5f85\u5b8c\u6210")
        is_reclone = slot["status"] == "ready" and bool(slot["voice_id"])
        reclone_count = int(slot["reclone_count"] or 0)
        if is_reclone and reclone_count >= 10:
            raise ValueError("\u8be5\u97f3\u8272\u5df2\u8fbe\u5230\u6700\u9ad810\u6b21\u91cd\u65b0\u590d\u523b\u4e0a\u9650")
        next_reclone_count = reclone_count + 1 if is_reclone else reclone_count
        c.execute("""INSERT OR IGNORE INTO audio_voices
            (username, scope, voice_key, display_name, provider_voice, slot_id, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (username, "personal", voice_key, name, slot_id, slot_id, now, now))
        c.execute("""UPDATE audio_voices
            SET display_name=?, provider_voice=?, slot_id=?, updated_at=?
            WHERE username=? AND scope='personal' AND voice_key=?""",
            (name, slot_id, slot_id, now, username, voice_key))
        r = c.execute("SELECT id FROM audio_voices WHERE username=? AND scope='personal' AND voice_key=?",
                      (username, voice_key)).fetchone()
        voice_id = r["id"] if r else None
        c.execute("""UPDATE audio_voice_slots SET voice_id=?, status='training', reclone_count=?, clone_started_at=?, clone_upload_at=NULL, clone_error=NULL, updated_at=?
            WHERE username=? AND slot_id=?""", (voice_id, next_reclone_count, now, now, username, slot_id))
        c.commit()
    clear_voice_preview(username, slot_id)
    return {"voice_id": voice_id, "voice_key": voice_key, "display_name": name, "status": "training", "reclone_count": next_reclone_count, "reclone_remaining": max(0, 10 - next_reclone_count)}

def clone_vip_voice_background(username, payload):
    try:
        clone_vip_voice(username, payload)
    except Exception as e:
        slot_id = (payload.get("slot_id") or "").strip()
        print("[clone_vip_voice_background] failed username=%s slot_id=%s error=%s" % (username, slot_id, str(e)[:300]), flush=True)
        if slot_id:
            try:
                with closing(adb()) as c:
                    c.execute("UPDATE audio_voice_slots SET status='failed', updated_at=? WHERE username=? AND slot_id=?",
                              (int(time.time()), username, slot_id))
                    c.commit()
            except Exception:
                pass

def prepare_clone_audio(audio_b64, audio_format):
    raw = base64.b64decode(audio_b64)
    ts = int(time.time() * 1000)
    safe_format = re.sub(r"[^a-zA-Z0-9]", "", audio_format or "mp3")[:8] or "mp3"
    src = _out_path("audio/clone_src_%d.%s" % (ts, safe_format))
    dst = _out_path("audio/clone_60s_%d.mp3" % ts)
    src.write_bytes(raw)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-t", "60",
        "-ac", "1",
        "-ar", "16000",
        "-b:a", "48k",
        str(dst),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        data = dst.read_bytes()
    except Exception:
        data = raw
        dst = src
    finally:
        try:
            if src.exists() and src != dst:
                src.unlink()
        except Exception:
            pass
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("\u6837\u97f3\u6587\u4ef6\u8fc7\u5927\uff0c\u8bf7\u4e0a\u4f20\u66f4\u77ed\u6216\u66f4\u4f4e\u7801\u7387\u7684\u97f3\u9891")
    return base64.b64encode(data).decode(), "mp3"

def clone_vip_voice(username, payload):
    username = (username or "").strip()
    slot_id = (payload.get("slot_id") or "").strip()
    name = (payload.get("name") or "\u6211\u7684VIP\u590d\u523b\u97f3\u8272").strip()[:40]
    audio_b64 = payload.get("audio") or ""
    audio_format = (payload.get("audio_format") or "mp3").strip().lower().lstrip(".")
    if audio_format not in {"mp3", "wav", "m4a", "aac", "ogg"}:
        audio_format = "mp3"
    if not slot_id:
        raise ValueError("\u7f3a\u5c11\u97f3\u8272\u69fd\u4f4d")
    if not audio_b64:
        raise ValueError("\u8bf7\u5148\u4e0a\u4f20\u6837\u97f3")
    if not DOUBAO_APPID or not DOUBAO_TOKEN:
        raise ValueError("\u8c46\u5305\u58f0\u97f3\u590d\u523b\u914d\u7f6e\u672a\u5b8c\u6210")
    with closing(adb()) as c:
        slot = c.execute("""SELECT id, slot_id, voice_id FROM audio_voice_slots
            WHERE username=? AND slot_id=? AND status IN ('active','training','failed','ready')""", (username, slot_id)).fetchone()
    if not slot:
        raise ValueError("\u97f3\u8272\u69fd\u4f4d\u4e0d\u5b58\u5728\u6216\u4e0d\u5c5e\u4e8e\u5f53\u524d\u8d26\u53f7")
    audio_b64, audio_format = prepare_clone_audio(audio_b64, audio_format)
    baseline_version = baseline_icl = baseline_demo = ""
    try:
        baseline = query_doubao_clone_status(slot_id)
        baseline_version = str(baseline.get("version") or "")
        baseline_icl = str(baseline.get("icl_speaker_id") or "")
        baseline_demo = str(baseline.get("demo_audio") or "")
    except Exception as e:
        print("[clone_vip_voice] baseline status skipped username=%s slot_id=%s error=%s" %
              (username, slot_id, str(e)[:200]), flush=True)
    body = json.dumps({
        "appid": DOUBAO_APPID,
        "speaker_id": slot_id,
        "audios": [{"audio_bytes": audio_b64, "audio_format": audio_format}],
        "source": 2,
        "language": 0,
        "model_type": DOUBAO_CLONE_MODEL_TYPE,
    }).encode()
    req = urllib.request.Request("https://openspeech.bytedance.com/api/v1/mega_tts/audio/upload",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer;" + DOUBAO_TOKEN,
            "Resource-Id": DOUBAO_CLONE_RESOURCE,
        },
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read().decode("utf-8", "replace")
            resp = json.loads(raw or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ValueError("\u8c46\u5305VIP\u590d\u523b\u63a5\u53e3\u5931\u8d25: " + detail)
    except Exception as e:
        raise ValueError("\u8c46\u5305VIP\u590d\u523b\u8bf7\u6c42\u5931\u8d25: " + str(e)[:160])
    base = resp.get("BaseResp") or resp.get("base_resp") or {}
    code = base.get("StatusCode", base.get("status_code", resp.get("code", 0)))
    try:
        code_i = int(code)
    except Exception:
        code_i = 0 if code in ("0", "OK", "ok", None) else -1
    if code_i not in (0,):
        msg = base.get("StatusMessage") or base.get("status_message") or resp.get("message") or json.dumps(resp, ensure_ascii=False)[:200]
        raise ValueError("\u8c46\u5305VIP\u590d\u523b\u5931\u8d25: " + str(msg)[:200])
    returned_speaker_id = (resp.get("speaker_id") or resp.get("speakerId") or "").strip()
    if returned_speaker_id and returned_speaker_id != slot_id:
        raise ValueError("\u8c46\u5305VIP\u590d\u523b\u8fd4\u56de\u7684\u97f3\u8272ID\u4e0e\u69fd\u4f4dID\u4e0d\u4e00\u81f4")
    upload_resp = json.dumps({
        "BaseResp": base,
        "speaker_id": returned_speaker_id,
        "message": resp.get("message"),
        "code": resp.get("code"),
    }, ensure_ascii=False)[:1000]
    now = int(time.time())
    voice_key = "vip_" + re.sub(r"[^a-zA-Z0-9_\\-]", "_", slot_id)
    with closing(adb()) as c:
        c.execute("""INSERT OR IGNORE INTO audio_voices
            (username, scope, voice_key, display_name, provider_voice, slot_id, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (username, "personal", voice_key, name, slot_id, slot_id, now, now))
        c.execute("""UPDATE audio_voices
            SET display_name=?, provider_voice=?, preview_file=NULL, preview_url=NULL, slot_id=?, updated_at=?
            WHERE username=? AND scope='personal' AND voice_key=?""",
            (name, slot_id, slot_id, now, username, voice_key))
        r = c.execute("SELECT id FROM audio_voices WHERE username=? AND scope='personal' AND voice_key=?",
                      (username, voice_key)).fetchone()
        voice_id = r["id"] if r else None
        c.execute("""UPDATE audio_voice_slots
            SET voice_id=?, status='training', clone_started_at=?, clone_upload_at=?, clone_error=NULL,
                clone_upload_speaker_id=?, clone_upload_response=?,
                clone_baseline_version=?, clone_baseline_icl_speaker_id=?, clone_baseline_demo_audio=?,
                updated_at=?
            WHERE username=? AND slot_id=?""",
            (voice_id, now, now, returned_speaker_id, upload_resp,
             baseline_version, baseline_icl, baseline_demo, now, username, slot_id))
        c.commit()
    print("[clone_vip_voice] upload ok username=%s slot_id=%s returned_speaker_id=%s response=%s" %
          (username, slot_id, returned_speaker_id or "", upload_resp[:500]), flush=True)
    return {"voice_id": voice_id, "voice_key": voice_key, "display_name": name, "status": "training", "speaker_id": returned_speaker_id or slot_id}

def ensure_audio_voice(username, voice_key):
    username = (username or "").strip()
    voice_key = (voice_key or "dapeng").strip()
    public_keys = {"dapeng", "zelong", "paul"}
    public_key = voice_key.lower()
    now = int(time.time())
    with closing(adb()) as c:
        r = c.execute("SELECT id FROM audio_voices WHERE scope='public' AND username='' AND voice_key=?",
                      (voice_key,)).fetchone()
        if r: return r["id"]
    if public_key in public_keys:
        voice_key = public_key
        with closing(adb()) as c:
            r = c.execute("SELECT id FROM audio_voices WHERE scope='public' AND username='' AND voice_key=?",
                          (voice_key,)).fetchone()
            if r: return r["id"]
            display = {"dapeng": "\u5927\u9e4f IVC", "zelong": "\u6cfd\u9f99 IVC", "paul": "Paul \u7537\u58f0"}.get(voice_key, voice_key)
            cur = c.execute("""INSERT INTO audio_voices
                (scope, username, voice_key, display_name, provider_voice, created_at, updated_at)
                VALUES('public','',?,?,?,?,?)""",
                (voice_key, display, VOICE_MAP.get(voice_key, "alloy"), now, now))
            c.commit()
            return cur.lastrowid
    with closing(adb()) as c:
        r = c.execute("SELECT id FROM audio_voices WHERE scope='personal' AND username=? AND voice_key=?",
                      (username, voice_key)).fetchone()
        if r: return r["id"]
        display = "\u6211\u7684\u590d\u523b\u97f3\u8272" if voice_key == "personal" else voice_key
        cur = c.execute("""INSERT INTO audio_voices
            (scope, username, voice_key, display_name, provider_voice, created_at, updated_at)
            VALUES('personal',?,?,?,?,?,?)""",
            (username, voice_key, display, VOICE_MAP.get(voice_key, VOICE_MAP.get("personal", "alloy")), now, now))
        c.commit()
        return cur.lastrowid

def resolve_audio_provider_voice(username, voice_key):
    username = (username or "").strip()
    voice_key = (voice_key or "dapeng").strip()
    public_keys = {"dapeng", "zelong", "paul"}
    public_key = voice_key.lower()
    with closing(adb()) as c:
        r = c.execute("""SELECT provider_voice FROM audio_voices
            WHERE scope='public' AND username='' AND voice_key=?""",
            (voice_key,)).fetchone()
    if r:
        return r["provider_voice"]
    if public_key in public_keys:
        ensure_audio_voice(username, public_key)
        return VOICE_MAP.get(public_key, VOICE_MAP["dapeng"])
    if voice_key == "personal":
        ensure_audio_voice(username, voice_key)
        return VOICE_MAP.get("personal", VOICE_MAP["dapeng"])
    with closing(adb()) as c:
        r = c.execute("""SELECT provider_voice FROM audio_voices
            WHERE scope='personal' AND username=? AND voice_key=?""",
            (username, voice_key)).fetchone()
    if not r:
        raise ValueError("个人音色不存在或不属于当前账号")
    return r["provider_voice"]

def record_audio_asset(job_id, username, result):
    if not result or result.get("type") != "audio":
        return
    now = int(time.time())
    raw_voice_key = (result.get("voice") or "dapeng").strip()
    voice_key = raw_voice_key.lower() if raw_voice_key.lower() in {"dapeng", "zelong", "paul"} else raw_voice_key
    voice_id = ensure_audio_voice(username, voice_key)
    with closing(adb()) as c:
        c.execute("""INSERT OR REPLACE INTO audio_assets
            (job_id, username, voice_id, voice_key, file, url, text, speed, pitch, volume, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, username, voice_id, voice_key, result.get("file"), result.get("url"),
             result.get("text") or result.get("prompt"), result.get("speed"), result.get("pitch"),
             result.get("volume"), now))
        c.commit()

def backfill_audio_assets():
    try:
        with closing(jdb()) as c:
            rows = c.execute("""SELECT id, username, result FROM jobs
                WHERE kind='audio' AND status='done' AND result IS NOT NULL""").fetchall()
        for r in rows:
            try:
                record_audio_asset(r["id"], r["username"], json.loads(r["result"]))
            except Exception:
                pass
    except Exception:
        pass

def list_audio_voices(username):
    with closing(adb()) as c:
        rows = c.execute("""SELECT id, scope, username, voice_key, display_name, provider_voice, preview_file, preview_url, slot_id, created_at, updated_at
            FROM audio_voices
            WHERE scope='public' OR (scope='personal' AND username=?)
            ORDER BY CASE scope WHEN 'public' THEN 0 ELSE 1 END, id""", (username,)).fetchall()
    return [dict(r) for r in rows]

def rename_audio_voice(username, slot_id, display_name):
    slot_id = (slot_id or "").strip()
    name = (display_name or "").strip()
    if not slot_id:
        raise Exception("缺少音色槽位")
    if not name:
        raise Exception("请输入音色名称")
    name = name[:40]
    now = int(time.time())
    with closing(adb()) as c:
        slot = c.execute("""SELECT voice_id FROM audio_voice_slots
            WHERE username=? AND slot_id=?""", (username, slot_id)).fetchone()
        if not slot or not slot["voice_id"]:
            raise Exception("音色不存在")
        cur = c.execute("""UPDATE audio_voices
            SET display_name=?, updated_at=?
            WHERE id=? AND username=? AND scope='personal'""",
            (name, now, slot["voice_id"], username))
        if cur.rowcount < 1:
            raise Exception("音色不存在")
        c.execute("UPDATE audio_voice_slots SET updated_at=? WHERE username=? AND slot_id=?",
                  (now, username, slot_id))
        c.commit()
    return {"slot_id": slot_id, "display_name": name, "updated_at": now}

def list_audio_assets(username, limit=120):
    limit = max(1, min(120, int(limit or 120)))
    with closing(adb()) as c:
        rows = c.execute("""SELECT a.id, a.job_id, a.username, a.voice_id, a.voice_key, a.file, a.url, a.text,
                   a.speed, a.pitch, a.volume, a.created_at, v.display_name AS voice_name, v.preview_url
            FROM audio_assets a
            LEFT JOIN audio_voices v ON v.id = a.voice_id
            WHERE a.username=?
            ORDER BY a.id DESC LIMIT ?""", (username, limit)).fetchall()
    return [dict(r) for r in rows]

def record_video_asset(job_id, username, result):
    now = int(time.time())
    with closing(adb()) as c:
        c.execute("""INSERT INTO video_assets
            (job_id, username, mode, image_file, audio_file, reference_video_file, video_file, video_url, text, voice_key,
             resolution, ratio, motion, phase, image_asset_id, audio_asset_id, reference_asset_id, provider_video_id,
             provider_avatar_id, provider_avatar_group_id, source_video_url, status, error, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
                mode=COALESCE(excluded.mode, video_assets.mode),
                image_file=COALESCE(excluded.image_file, video_assets.image_file),
                audio_file=COALESCE(excluded.audio_file, video_assets.audio_file),
                reference_video_file=COALESCE(excluded.reference_video_file, video_assets.reference_video_file),
                video_file=COALESCE(excluded.video_file, video_assets.video_file),
                video_url=COALESCE(excluded.video_url, video_assets.video_url),
                text=COALESCE(excluded.text, video_assets.text),
                voice_key=COALESCE(excluded.voice_key, video_assets.voice_key),
                resolution=COALESCE(excluded.resolution, video_assets.resolution),
                ratio=COALESCE(excluded.ratio, video_assets.ratio),
                motion=COALESCE(excluded.motion, video_assets.motion),
                phase=COALESCE(excluded.phase, video_assets.phase),
                image_asset_id=COALESCE(excluded.image_asset_id, video_assets.image_asset_id),
                audio_asset_id=COALESCE(excluded.audio_asset_id, video_assets.audio_asset_id),
                reference_asset_id=COALESCE(excluded.reference_asset_id, video_assets.reference_asset_id),
                provider_video_id=COALESCE(excluded.provider_video_id, video_assets.provider_video_id),
                provider_avatar_id=COALESCE(excluded.provider_avatar_id, video_assets.provider_avatar_id),
                provider_avatar_group_id=COALESCE(excluded.provider_avatar_group_id, video_assets.provider_avatar_group_id),
                source_video_url=COALESCE(excluded.source_video_url, video_assets.source_video_url),
                status=COALESCE(excluded.status, video_assets.status),
                error=excluded.error,
                updated_at=excluded.updated_at""",
            (job_id, username, result.get("mode"), result.get("image_file"), result.get("audio_file"),
             result.get("reference_video_file"), result.get("video_file"), result.get("video_url"), result.get("text"), result.get("voice"),
             result.get("resolution"), result.get("ratio"), result.get("motion"), result.get("phase"),
             result.get("image_asset_id"), result.get("audio_asset_id"), result.get("reference_asset_id"),
             result.get("provider_video_id") or result.get("video_id"), result.get("provider_avatar_id") or result.get("avatar_item_id"),
             result.get("provider_avatar_group_id") or result.get("avatar_group_id"), result.get("source_video_url"),
             result.get("status") or "pending", result.get("error"), now, now))
        c.commit()

def update_video_asset_phase(job_id, phase, **fields):
    if not job_id:
        return
    now = int(time.time())
    allowed = {
        "mode", "image_file", "audio_file", "reference_video_file", "video_file", "video_url",
        "text", "voice_key", "resolution", "ratio", "motion", "image_asset_id",
        "audio_asset_id", "reference_asset_id", "provider_video_id", "provider_avatar_id",
        "provider_avatar_group_id", "source_video_url", "status", "error"
    }
    if "voice" in fields and "voice_key" not in fields:
        fields["voice_key"] = fields.pop("voice")
    updates = {"phase": phase, "status": fields.pop("status", "running")}
    if "error" in fields:
        updates["error"] = fields.pop("error")
    for k, v in fields.items():
        if k in allowed and v is not None:
            updates[k] = v
    sets = ", ".join("%s=?" % k for k in updates)
    vals = list(updates.values()) + [now, job_id]
    try:
        with closing(adb()) as c:
            c.execute("UPDATE video_assets SET %s, updated_at=? WHERE job_id=?" % sets, vals)
            c.commit()
    except Exception:
        pass
    try:
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET updated_at=? WHERE id=? AND status='running'", (now, job_id))
            c.commit()
    except Exception:
        pass

def record_video_pending_asset(job_id, username, payload):
    record_video_asset(job_id, username, {
        "mode": payload.get("mode") or "text",
        "text": payload.get("text") or "",
        "voice": payload.get("voice") or "",
        "resolution": payload.get("resolution") or "1080p",
        "ratio": payload.get("ratio") or "9:16",
        "motion": payload.get("motion") or "medium",
        "phase": "queued",
        "status": "running",
    })

def list_video_assets(username, limit=120):
    limit = max(1, min(120, int(limit or 120)))
    with closing(adb()) as c:
        rows = c.execute("""SELECT id, job_id, username, mode, image_file, audio_file, reference_video_file, video_file, video_url,
                   text, voice_key, resolution, ratio, motion, phase, image_asset_id, audio_asset_id, reference_asset_id,
                   provider_video_id, provider_avatar_id, provider_avatar_group_id, source_video_url,
                   status, error, created_at, updated_at
            FROM video_assets
            WHERE username=?
            ORDER BY id DESC LIMIT ?""", (username, limit)).fetchall()
    return [dict(r) for r in rows]

def get_points(username):
    try:
        with closing(sqlite3.connect(AUTH_DB, timeout=10)) as c:
            r = c.execute("SELECT points FROM users WHERE username=?", (username,)).fetchone()
            return r[0] if r else 0
    except Exception:
        return 0

def add_points(username, delta):
    try:
        with closing(sqlite3.connect(AUTH_DB, timeout=10)) as c:
            c.execute("UPDATE users SET points = MAX(0, points + ?) WHERE username=?", (delta, username))
            c.commit()
    except Exception:
        pass

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

def gen_image(payload):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    ratio = payload.get("ratio") or "1:1"
    size  = SIZES.get(ratio, "1024x1024")
    img   = payload.get("image")   # base64(无 data: 前缀) — 上传参考图 → 图生图 / 局部修改
    mask  = payload.get("mask")    # base64 — 蒙版(透明处=要重绘的区域) → 局部修改
    quality = "high" if (payload.get("quality") or "hd") == "hd" else "medium"  # 标准=medium/高清=high
    provider = (payload.get("provider") or "openai").strip().lower()
    if provider == "zelong":
        base, key, proxy = ZELONG_BASE, ZELONG_KEY, False   # 泽龙Ai：国内中转，直连不走代理
        if not key:
            raise ValueError("泽龙Ai(中转站)未配置 key")
    else:
        base, key, proxy = OPENAI_BASE, OPENAI_KEY, True
    cap = 2 if provider == "zelong" else 4                   # 中转出图慢，数量上限低
    count = 1 if mask else max(1, min(cap, int(payload.get("count") or 1)))  # 局部修改只出 1 张
    if img:
        files = [("image", "in.png", base64.b64decode(img))]
        if mask:
            files.append(("mask", "mask.png", base64.b64decode(mask)))
        body, ct = _multipart({"model": "gpt-image-2", "prompt": prompt, "size": size, "quality": quality, "n": str(count)}, files)
        d = _post("/v1/images/edits", body, ct, base=base, key=key, proxy=proxy)
        mode = "inpaint" if mask else "img2img"
    else:
        body = json.dumps({"model": "gpt-image-2", "prompt": prompt, "size": size, "quality": quality, "n": count}).encode()
        d = _post("/v1/images/generations", body, "application/json", base=base, key=key, proxy=proxy)
        mode = "text2img"
    files_out, urls = [], []
    for i, item in enumerate(d.get("data") or []):
        fn = "img_%d_%d.png" % (int(time.time() * 1000), i)
        if item.get("b64_json"):
            (OUT_DIR / fn).write_bytes(base64.b64decode(item["b64_json"]))
        elif item.get("url"):                                # 部分中转返回 url 而非 b64
            opener = urllib.request.urlopen if proxy else _NOPROXY.open
            with opener(item["url"], timeout=120) as rr:
                (OUT_DIR / fn).write_bytes(rr.read())
        else:
            continue
        files_out.append(fn); urls.append("/api/gen/file/" + fn)
    if not files_out:
        raise ValueError("出图返回为空")
    return {"type": "image", "mode": mode, "provider": provider, "count": len(files_out),
            "file": files_out[0], "url": urls[0], "files": files_out, "urls": urls, "ratio": ratio, "prompt": prompt}

# ============ 文案能力：LLM（chat completions，走同一代理） ============
def _chat(sysmsg, usermsg, temp):
    body = json.dumps({"model": COPY_MODEL,
                       "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": usermsg}],
                       "temperature": temp}).encode()
    d = _post("/v1/chat/completions", body, "application/json")
    return (d.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

def gen_copy(payload):
    brief = (payload.get("prompt") or "").strip()
    if not brief:
        raise ValueError("请输入文案需求")
    ctype = (payload.get("ctype") or payload.get("type") or "通用").strip()
    # 编导：结构化分镜脚本（返回 scenes 数组）
    if (payload.get("format") or "") == "script":
        style = payload.get("style") or "口播"; dur = payload.get("dur") or "30s"; plat = payload.get("platform") or "抖音"
        raw = _chat("你是黄雀传媒资深短视频编导。只输出 JSON 本身，不要解释、不要 markdown 代码块。",
                    ("为以下选题生成一套可拍的%s短视频分镜脚本（平台%s，总时长约%s）。\n选题/卖点：%s\n"
                     "严格输出 JSON：{\"scenes\":[{\"dur\":\"3s\",\"scene\":\"画面描述\",\"line\":\"口播台词\"}]}，"
                     "3-4 个分镜，各 dur 之和≈总时长，口播口语化有钩子可直接念。" % (style, plat, dur, brief)), 0.85)
        s, e = raw.find("{"), raw.rfind("}"); scenes = []
        if s >= 0 and e > s:
            try: scenes = json.loads(raw[s:e+1]).get("scenes", [])
            except Exception: scenes = []
        if not scenes: raise ValueError("脚本解析失败，请重试")
        return {"type": "copy", "mode": "script", "scenes": scenes, "ctype": ctype,
                "style": style, "dur": dur, "platform": plat, "prompt": brief}
    # 通用文案（多条，--- 分隔）
    try: n = max(1, min(3, int(payload.get("n") or 2)))
    except Exception: n = 2
    text = _chat("你是黄雀传媒资深美业/电商营销文案。输出简体中文，口语化、有钩子、能转化。直接给文案本身，不要任何解释说明、不要前后缀。",
                 ("文案类型：%s\n需求/主题：%s\n请给 %d 条不同风格的文案，每条之间用单独一行「---」分隔；可适当用 emoji 和话题标签。" % (ctype, brief, n)), 0.9)
    if not text: raise ValueError("文案生成为空")
    return {"type": "copy", "ctype": ctype, "text": text, "prompt": brief}

# ============ 采集能力：TikHub 单条视频 → 视频+文案+口播+评论 ============
def gen_collect(payload):
    platform = (payload.get("platform") or "douyin").strip()
    raw = (payload.get("url") or payload.get("id") or "").strip()
    if not raw:
        raise ValueError("缺少链接或 id")
    note_type = payload.get("note_type") or "video"
    if payload.get("url") and not payload.get("id"):   # 贴链接：解析出平台+id+类型（短链也认）
        info = tikhub.parse_link(payload["url"])
        platform = info.get("platform") or platform
        ident = info.get("id")
        note_type = info.get("note_type")
        if not ident:
            raise ValueError("链接无法解析，请检查链接或改用关键词搜索")
    else:
        ident = raw
    if platform not in tikhub.PLATFORMS:
        raise ValueError("未知平台")
    want = payload.get("want") or ["copy", "comments"]
    det = tikhub.detail(platform, ident, note_type=note_type)
    if not (det.get("title") or det.get("desc") or det.get("images")):
        # 内容全空 = TikHub 偶发限流/抽风 或 私密/已删 → 报错退点，让前端提示重试，别甩空卡片
        raise ValueError("内容获取失败（可能是上游限流或内容私密/已删），请重试")
    au = det.get("author") or {}
    out = {
        "type": "collect", "platform": platform, "source": det.get("url") or ident,
        "video": {"title": det.get("title"), "author": au.get("name"), "authorAvatar": None,
                  "profile_url": au.get("profile_url"),
                  "cover": det.get("cover"), "play_url": det.get("play_url"), "url": det.get("url"),
                  "duration": det.get("duration"), "publish_time": det.get("publish_time"),
                  "stats": det.get("stats")},
        "copy": {"title": det.get("title"), "desc": det.get("desc"), "tags": det.get("tags")},
        "images": det.get("images") or [],   # 图文笔记的全部图片
        "transcript": None, "comments": [], "comments_more": False,
        "url": det.get("cover"), "prompt": det.get("title"),  # 给通用 history 用（封面+标题）
    }
    if "comments" in want:
        cm = tikhub.comments(platform, det.get("id") or ident, count=int(payload.get("comment_count") or 20))
        out["comments"] = cm["items"]; out["comments_more"] = bool(cm.get("has_more"))
    if "transcript" in want:
        try:
            out["transcript"] = tikhub.transcript(det)
        except tikhub.TikHubError as e:
            out["transcript"] = {"text": None, "error": str(e)[:120]}
    return out

# ============ 获客能力：关键词→搜视频→扒评论→意图过滤→客户名单 ============
# 意图规则镜像 scripts/leads_filter.py（调词两边同步）。
_SPAM = ["需要我推荐", "推荐给你", "先帮店做出业绩", "做出业绩再合作", "做出业绩再分润",
         "不需要店家出成本", "不需要我先出成本", "W的业绩", "万的业绩", "免费送模式",
         "0成本启动", "感兴趣的老板", "一起交流交流", "下店来打版"]
_HIGH = ["怎么拓客", "怎么收费", "怎么弄", "怎么做", "怎么操作", "怎么整", "怎么合作", "怎么矩阵",
         "多少钱", "价位", "求带", "带带", "带一带", "想学", "有偿", "预算", "求助", "求推荐",
         "靠谱的拓客", "有没有靠谱", "哪里下载", "谁能帮我", "我也想", "没开单", "怎么收费的",
         "想找", "教一下", "怎么回", "我该怎么", "到底", "求带带", "也想",
         "有效果吗", "效果怎么样", "会反弹", "反弹吗", "能瘦", "痛吗", "维持多久", "做一次",
         "几次", "安全吗", "在哪做", "怎么预约", "约一个", "想做", "想咨询", "哪家好",
         "怎么联系", "贵吗", "价格", "多少钱一次", "可以瘦吗", "有用吗", "求地址"]
def _is_spam(t): return any(k in t for k in _SPAM)
def _is_high(t): return any(k in t for k in _HIGH)

def gen_leads(payload):
    keyword   = (payload.get("keyword") or "").strip()
    platforms = payload.get("platforms") or ["douyin"]
    nvid      = max(1, min(30, int(payload.get("count") or 12)))
    pages     = max(1, min(3, int(payload.get("pages") or 1)))
    targets   = payload.get("channels_targets") or []   # 视频号盯号：sph 短号 / finder username 列表
    raw = []   # 评论汇总（字段对齐 _is_spam/_is_high 过滤）

    def pull(platform, vid_id, title):
        for pg in range(pages):
            try:
                cm = tikhub.comments(platform, vid_id, cursor=(pg * 20 if platform == "douyin" else None), count=20)
            except tikhub.TikHubError:
                break
            for c in cm["items"]:
                raw.append({"content": c.get("text"), "user_id": c.get("user_id"), "nickname": c.get("user"),
                            "ip_location": c.get("ip"), "like_count": c.get("likes") or 0,
                            "profile_url": c.get("profile_url"), "platform": platform, "source": title})
            if not cm.get("has_more"):
                break

    for platform in platforms:
        if platform == "channels":
            continue  # 视频号无全网搜，走下面盯号
        if not keyword:
            continue
        try:
            sr = tikhub.search(platform, keyword)
        except tikhub.TikHubError:
            continue
        for v in sr["items"][:nvid]:
            pull(platform, v["id"], v.get("title"))

    if "channels" in platforms:
        for tgt in targets:
            try:
                uname = tgt if "@finder" in tgt else (tikhub.ch_id_to_username(tgt).get("username"))
                if not uname:
                    continue
                for v in tikhub.ch_user_videos(uname)["items"][:nvid]:
                    pull("channels", v["id"], v.get("title"))
            except tikhub.TikHubError:
                continue

    leads, spam, chat, seen = [], 0, 0, set()
    for c in raw:
        t = (c.get("content") or "").strip()
        if not t:
            continue
        if _is_spam(t):
            spam += 1; continue
        if len(re.sub(r"\[[^\]]+\]", "", t).strip()) < 2:
            chat += 1; continue
        if _is_high(t):
            k = (c.get("user_id"), t)
            if k in seen:
                continue
            seen.add(k); leads.append(c)
        else:
            chat += 1
    leads.sort(key=lambda c: (len(c.get("content", "")), c.get("like_count", 0)), reverse=True)
    out_leads = [{"nickname": c.get("nickname"), "user_unique_id": c.get("user_id"),
                  "ip_location": c.get("ip_location"), "content": c.get("content"),
                  "title": c.get("source"), "platform": c.get("platform"),
                  "profile_url": c.get("profile_url")} for c in leads]
    return {"type": "leads", "keyword": keyword, "platforms": platforms,
            "leads_count": len(out_leads), "spam": spam, "chat": chat, "total": len(raw),
            "leads": out_leads, "url": None, "prompt": keyword}

# ============ 配音能力：OpenAI TTS（同事的 audio 能力，合并保留） ============
VOICE_MAP = {
    "dapeng": os.environ.get("VOICE_DAPENG", "alloy"),
    "zelong": os.environ.get("VOICE_ZELONG", "onyx"),
    "paul": os.environ.get("VOICE_PAUL", "echo"),
    "personal": os.environ.get("VOICE_PERSONAL", "alloy"),
    "alloy": "alloy", "ash": "ash", "ballad": "ballad", "coral": "coral", "echo": "echo",
    "fable": "fable", "nova": "nova", "onyx": "onyx", "sage": "sage", "shimmer": "shimmer",
}
SPEED_MAP = {"slow": 0.88, "normal": 1.0, "fast": 1.12, "偏慢": 0.88, "正常": 1.0, "偏快": 1.12}

def gen_audio(payload):
    text = (payload.get("text") or payload.get("prompt") or "").strip()
    if not text:
        raise ValueError("配音文案不能为空")
    if len(text) > 1200:
        raise ValueError("配音文案过长，请控制在 1200 字以内")
    username = (payload.get("_username") or "").strip()
    raw_voice_key = (payload.get("voice") or "dapeng").strip()
    voice_key = raw_voice_key.lower() if raw_voice_key.lower() in {"dapeng", "zelong", "paul"} else raw_voice_key
    voice = resolve_audio_provider_voice(username, voice_key)
    raw_speed = payload.get("speed")
    if isinstance(raw_speed, (int, float)):
        speed = max(0.5, min(2.0, round(float(raw_speed), 1)))
    else:
        speed = SPEED_MAP.get(raw_speed or "normal", 1.0)
    def knob(name, minv, maxv, default):
        try:
            return max(minv, min(maxv, int(float(payload.get(name, default)))))
        except Exception:
            return default
    pitch = knob("pitch", -12, 12, 0)
    volume = knob("volume", -50, 100, 0)
    if str(voice).startswith("S_"):
        speech_rate = int(round((speed - 1.0) * 100))
        preview = generate_doubao_preview(voice, text, speech_rate=speech_rate, loudness_rate=volume, pitch_rate=pitch)
        fn = preview.get("file")
        return {"type": "audio", "file": fn, "url": preview.get("url"), "voice": voice_key,
                "speed": speed, "pitch": pitch, "volume": volume, "text": text, "prompt": text}
    instructions = "中文短视频口播配音，语气自然，吐字清晰，节奏适合美业/本地生活转化。"
    body = json.dumps({
        "model": TTS_MODEL, "voice": voice, "input": text,
        "instructions": instructions, "response_format": "mp3", "speed": speed,
    }, ensure_ascii=False).encode()
    data = _post_bytes("/v1/audio/speech", body, "application/json")
    fn = "audio/aud_%d.mp3" % int(time.time() * 1000)
    _out_path(fn).write_bytes(data)
    return {"type": "audio", "file": fn, "url": _file_url(fn), "voice": voice_key,
            "speed": speed, "pitch": pitch, "volume": volume, "text": text, "prompt": text}

def _save_data_file(data_url, prefix, allowed_ext):
    raw = (data_url or "").strip()
    if not raw:
        return None
    if "," in raw and raw.lower().startswith("data:"):
        meta, raw = raw.split(",", 1)
        mime = meta.split(";", 1)[0].replace("data:", "").lower()
    else:
        mime = ""
    ext = ""
    for k, v in {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
        "audio/x-wav": ".wav", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
        "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"
    }.items():
        if mime == k:
            ext = v
            break
    if not ext:
        ext = allowed_ext[0]
    if ext not in allowed_ext:
        raise ValueError("不支持的文件格式")
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        raise ValueError("文件内容解析失败")
    max_size = (250 if ext in {".mp4", ".mov", ".webm"} else 35) * 1024 * 1024
    if len(data) > max_size:
        raise ValueError("文件过大，请压缩后再上传")
    folder = "audio/" if ext in {".mp3", ".wav", ".m4a"} else ("video/" if ext in {".mp4", ".mov", ".webm"} else "")
    fn = "%s%s_%d%s" % (folder, prefix, int(time.time() * 1000), ext)
    _out_path(fn).write_bytes(data)
    return fn

def _heygen_request_json(method, path, body=None, headers=None, timeout=180):
    if not HEYGEN_API_KEY:
        raise ValueError("视频生成服务未配置")
    h = {"x-api-key": HEYGEN_API_KEY}
    if headers:
        h.update(headers)
    req = urllib.request.Request(HEYGEN_API_BASE + path, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        raise RuntimeError("HeyGen接口失败: HTTP %s %s" % (e.code, detail))
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("HeyGen返回解析失败: %s" % raw[:300].decode("utf-8", "replace"))

def _heygen_upload_asset(file_path):
    path = pathlib.Path(file_path)
    if not path.is_file():
        raise ValueError("视频素材文件不存在")
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    boundary = "----huangque-heygen-%d" % int(time.time() * 1000)
    head = (
        "--%s\r\n"
        'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
        "Content-Type: %s\r\n\r\n"
    ) % (boundary, path.name.replace('"', ''), mime)
    body = head.encode() + path.read_bytes() + ("\r\n--%s--\r\n" % boundary).encode()
    data = _heygen_request_json("POST", "/assets", body, {
        "Content-Type": "multipart/form-data; boundary=%s" % boundary,
        "Content-Length": str(len(body)),
    }, timeout=240)
    asset_id = ((data.get("data") or {}).get("asset_id") or "").strip()
    if not asset_id:
        raise RuntimeError("HeyGen素材上传未返回asset_id: %s" % json.dumps(data, ensure_ascii=False)[:500])
    return asset_id

def _ensure_heygen_audio_mp3(audio_path):
    path = pathlib.Path(audio_path)
    if path.suffix.lower() == ".mp3":
        return path
    out = AUDIO_OUT_DIR / ("heygen_audio_%d.mp3" % int(time.time() * 1000))
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-vn", "-acodec", "libmp3lame", "-ar", "24000", "-ac", "1", "-b:a", "128k",
        str(out),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=180, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise ValueError("服务器未安装 ffmpeg，无法转换上传音频格式")
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode("utf-8", "replace")[:220]
        raise ValueError("音频格式转换失败，请重新上传 mp3 音频" + (": " + detail if detail else ""))
    except subprocess.TimeoutExpired:
        raise ValueError("音频格式转换超时，请重新上传更短的 mp3 音频")
    if not out.exists() or out.stat().st_size <= 0:
        raise ValueError("音频格式转换失败，请重新上传 mp3 音频")
    return out

def _heygen_create_video(image_asset_id, audio_asset_id, resolution, ratio, motion):
    title = "huangque video %d" % int(time.time())
    body = json.dumps({
        "title": title,
        "type": "image",
        "image": {"type": "asset_id", "asset_id": image_asset_id},
        "audio_asset_id": audio_asset_id,
        "resolution": resolution,
        "aspect_ratio": ratio,
        "fit": "cover",
        "expressiveness": motion,
        "output_format": "mp4",
    }, ensure_ascii=False).encode()
    data = _heygen_request_json("POST", "/videos", body, {
        "Content-Type": "application/json",
    }, timeout=90)
    video_id = ((data.get("data") or {}).get("video_id") or "").strip()
    if not video_id:
        raise RuntimeError("HeyGen未返回video_id: %s" % json.dumps(data, ensure_ascii=False)[:500])
    return video_id

def _find_nested_dict(obj, pred):
    if isinstance(obj, dict):
        if pred(obj):
            return obj
        for v in obj.values():
            got = _find_nested_dict(v, pred)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_nested_dict(v, pred)
            if got:
                return got
    return None

def _heygen_create_photo_avatar(image_asset_id):
    body = json.dumps({
        "type": "photo",
        "name": "huangque_photo_avatar_%d" % int(time.time()),
        "file": {"type": "asset_id", "asset_id": image_asset_id},
    }, ensure_ascii=False).encode()
    data = _heygen_request_json("POST", "/avatars", body, {
        "Content-Type": "application/json",
    }, timeout=90)
    root = data.get("data") or {}
    avatar_item_id = (((root.get("avatar_item") or {}).get("id")) or "").strip()
    avatar_group_id = (((root.get("avatar_group") or {}).get("id")) or "").strip()
    if not avatar_item_id:
        raise RuntimeError("HeyGen未返回avatar_item_id: %s" % json.dumps(data, ensure_ascii=False)[:500])
    return avatar_item_id, avatar_group_id

def _avatar_ready_from_payload(data, avatar_item_id):
    def is_item(d):
        return str(d.get("id") or "") == avatar_item_id
    item = _find_nested_dict(data, is_item)
    if not item:
        return False
    status = str(item.get("status") or item.get("state") or "").lower()
    return bool(item.get("preview_image_url") or status in {"completed", "ready", "success"})

def _heygen_wait_photo_avatar(avatar_item_id, avatar_group_id=""):
    deadline = time.time() + min(HEYGEN_TIMEOUT, 900)
    last_status = ""
    while time.time() < deadline:
        payloads = []
        if avatar_group_id:
            try:
                payloads.append(_heygen_request_json("GET", "/avatars/" + urllib.parse.quote(avatar_group_id), timeout=20))
            except Exception as e:
                last_status = str(e)[:120]
        try:
            payloads.append(_heygen_request_json("GET", "/avatars", timeout=20))
        except Exception as e:
            last_status = str(e)[:120]
        for data in payloads:
            if _avatar_ready_from_payload(data, avatar_item_id):
                return True
            item = _find_nested_dict(data, lambda d: str(d.get("id") or "") == avatar_item_id)
            if item:
                status = str(item.get("status") or item.get("state") or "processing")
                if status != last_status:
                    print("[heygen] avatar_id=%s status=%s" % (avatar_item_id, status), flush=True)
                    last_status = status
        time.sleep(HEYGEN_POLL_INTERVAL)
    raise TimeoutError("HeyGen Photo Avatar处理超时")

def _heygen_create_cinematic_video(avatar_item_id, reference_asset_id, ratio, resolution, duration):
    prompt = (
        "Create a realistic cinematic vertical video of the same person from the avatar photo. "
        "Follow the uploaded reference video closely for body movement, pose, timing, gestures, "
        "facial expression, framing and camera motion. Keep the person's identity, face, hairstyle, "
        "body proportions and outfit consistent. Smooth realistic motion, no text, no logo, no extra people."
    )
    body = json.dumps({
        "type": "cinematic_avatar",
        "title": "follow_reference_motion",
        "prompt": prompt,
        "avatar_id": [avatar_item_id],
        "references": [{"type": "asset_id", "asset_id": reference_asset_id}],
        "aspect_ratio": ratio,
        "resolution": resolution,
        "duration": duration,
        "enhance_prompt": False,
    }, ensure_ascii=False).encode()
    data = _heygen_request_json("POST", "/videos", body, {
        "Content-Type": "application/json",
    }, timeout=90)
    video_id = ((data.get("data") or {}).get("video_id") or "").strip()
    if not video_id:
        raise RuntimeError("HeyGen未返回video_id: %s" % json.dumps(data, ensure_ascii=False)[:500])
    return video_id

def _heygen_poll_video(video_id):
    deadline = time.time() + HEYGEN_TIMEOUT
    last_status = ""
    while time.time() < deadline:
        data = _heygen_request_json("GET", "/videos/" + urllib.parse.quote(video_id), timeout=90)
        info = data.get("data") or {}
        status = str(info.get("status") or "").lower()
        if status != last_status:
            print("[heygen] video_id=%s status=%s" % (video_id, status), flush=True)
            last_status = status
        if status == "completed":
            if not info.get("video_url"):
                raise RuntimeError("HeyGen完成但未返回video_url")
            return info
        if status in {"failed", "error"}:
            raise RuntimeError("HeyGen视频生成失败: %s" % json.dumps(info, ensure_ascii=False)[:500])
        time.sleep(HEYGEN_POLL_INTERVAL)
    raise TimeoutError("HeyGen视频生成超时")

def _download_video_file(url, prefix="vid"):
    req = urllib.request.Request(url, headers={"User-Agent": "huangque-content/1.0"})
    with urllib.request.urlopen(req, timeout=360) as r:
        data = r.read()
    if not data:
        raise RuntimeError("视频下载失败")
    fn = "video/%s_%d.mp4" % (prefix, int(time.time() * 1000))
    _out_path(fn).write_bytes(data)
    return fn

def generate_heygen_video(image_file, audio_file, resolution, ratio, motion):
    image_fp = _resolve_out_file(image_file)
    audio_fp = _resolve_out_file(audio_file)
    if not image_fp or not audio_fp:
        raise ValueError("视频素材文件不存在")
    audio_fp = _ensure_heygen_audio_mp3(audio_fp)
    image_asset_id = _heygen_upload_asset(image_fp)
    audio_asset_id = _heygen_upload_asset(audio_fp)
    video_id = _heygen_create_video(image_asset_id, audio_asset_id, resolution, ratio, motion)
    info = _heygen_poll_video(video_id)
    video_file = _download_video_file(info["video_url"], "heygen")
    return {
        "video_id": video_id,
        "image_asset_id": image_asset_id,
        "audio_asset_id": audio_asset_id,
        "video_file": video_file,
        "video_url": _file_url(video_file),
        "source_video_url": info.get("video_url"),
        "thumbnail_url": info.get("thumbnail_url"),
        "duration": info.get("duration"),
    }

def generate_heygen_motion_video(image_file, reference_video_file, resolution, ratio, duration, job_id=None):
    image_fp = _resolve_out_file(image_file)
    reference_fp = _resolve_out_file(reference_video_file)
    if not image_fp or not reference_fp:
        raise ValueError("动作模仿素材文件不存在")
    update_video_asset_phase(job_id, "uploading_image_asset")
    image_asset_id = _heygen_upload_asset(image_fp)
    update_video_asset_phase(job_id, "uploading_reference_asset", image_asset_id=image_asset_id)
    reference_asset_id = _heygen_upload_asset(reference_fp)
    update_video_asset_phase(job_id, "creating_photo_avatar", image_asset_id=image_asset_id,
                             reference_asset_id=reference_asset_id)
    avatar_item_id, avatar_group_id = _heygen_create_photo_avatar(image_asset_id)
    update_video_asset_phase(job_id, "waiting_photo_avatar", image_asset_id=image_asset_id,
                             reference_asset_id=reference_asset_id, provider_avatar_id=avatar_item_id,
                             provider_avatar_group_id=avatar_group_id)
    _heygen_wait_photo_avatar(avatar_item_id, avatar_group_id)
    update_video_asset_phase(job_id, "creating_cinematic_video", image_asset_id=image_asset_id,
                             reference_asset_id=reference_asset_id, provider_avatar_id=avatar_item_id,
                             provider_avatar_group_id=avatar_group_id)
    video_id = _heygen_create_cinematic_video(avatar_item_id, reference_asset_id, ratio, resolution, duration)
    update_video_asset_phase(job_id, "polling_video", image_asset_id=image_asset_id,
                             reference_asset_id=reference_asset_id, provider_avatar_id=avatar_item_id,
                             provider_avatar_group_id=avatar_group_id, provider_video_id=video_id)
    info = _heygen_poll_video(video_id)
    update_video_asset_phase(job_id, "downloading_video", provider_video_id=video_id,
                             source_video_url=info.get("video_url"))
    video_file = _download_video_file(info["video_url"], "cinematic")
    return {
        "video_id": video_id,
        "image_asset_id": image_asset_id,
        "reference_asset_id": reference_asset_id,
        "avatar_item_id": avatar_item_id,
        "avatar_group_id": avatar_group_id,
        "video_file": video_file,
        "video_url": _file_url(video_file),
        "source_video_url": info.get("video_url"),
        "thumbnail_url": info.get("thumbnail_url"),
        "duration": info.get("duration") or duration,
    }

def gen_video(payload):
    job_id = payload.get("_job_id")
    mode = (payload.get("mode") or "text").strip()
    if mode not in {"text", "audio", "motion"}:
        raise ValueError("生成方式不正确")
    image_file = _save_data_file(payload.get("image_data"), "vid_img", [".jpg", ".png", ".webp"])
    if not image_file:
        raise ValueError("请先上传人物形象图片")
    text = (payload.get("text") or "").strip()
    voice = (payload.get("voice") or "").strip()
    audio_file = None
    audio_url = None
    reference_video_file = None
    if mode == "motion":
        reference_video_file = _save_data_file(payload.get("reference_video_data"), "motion_ref", [".mp4", ".mov", ".webm"])
        if not reference_video_file:
            raise ValueError("请先上传参考动作视频")
        text = text or "动作模仿"
        update_video_asset_phase(job_id, "files_saved", mode=mode, image_file=image_file,
                                 reference_video_file=reference_video_file, text=text,
                                 voice=voice)
    elif mode == "text":
        if not text:
            raise ValueError("请先输入口播文案")
        if not voice:
            raise ValueError("请先选择音色")
        audio_result = gen_audio({
            "_username": (payload.get("_username") or "").strip(),
            "text": text,
            "voice": voice,
            "speed": payload.get("speed", 1.0),
            "pitch": payload.get("pitch", 0),
            "volume": payload.get("volume", 0),
        })
        audio_file = audio_result.get("file")
        audio_url = audio_result.get("url")
        if not audio_file:
            raise ValueError("口播音频生成失败")
    else:
        audio_file = _save_data_file(payload.get("audio_data"), "vid_aud", [".mp3", ".wav", ".m4a"])
        if not audio_file:
            raise ValueError("请先选择口播音频")
        audio_url = _file_url(audio_file)
    resolution = (payload.get("resolution") or "1080p").strip()
    ratio = (payload.get("ratio") or "9:16").strip()
    motion = (payload.get("motion") or "medium").strip()
    if resolution not in {"720p", "1080p", "4k"}:
        resolution = "1080p"
    if ratio not in {"9:16", "16:9", "1:1", "4:5", "5:4"}:
        ratio = "9:16"
    if motion not in {"low", "medium", "high"}:
        motion = "medium"
    try:
        duration = int(payload.get("duration") or 10)
    except Exception:
        duration = 10
    duration = max(5, min(30, duration))
    if mode == "motion":
        if resolution not in {"720p", "1080p"}:
            resolution = "720p"
        update_video_asset_phase(job_id, "motion_parameters_ready", resolution=resolution,
                                 ratio=ratio, motion=motion)
        video_result = generate_heygen_motion_video(image_file, reference_video_file, resolution, ratio, duration, job_id)
    else:
        video_result = generate_heygen_video(image_file, audio_file, resolution, ratio, motion)
    return {
        "type": "video", "status": "done", "mode": mode,
        "image_file": image_file, "image_url": _file_url(image_file),
        "audio_file": audio_file, "audio_url": audio_url,
        "reference_video_file": reference_video_file,
        "reference_video_url": _file_url(reference_video_file) if reference_video_file else None,
        "text": text, "voice": voice,
        "video_file": video_result.get("video_file"), "video_url": video_result.get("video_url"),
        "provider_video_id": video_result.get("video_id"),
        "provider_avatar_id": video_result.get("avatar_item_id"),
        "provider_avatar_group_id": video_result.get("avatar_group_id"),
        "image_asset_id": video_result.get("image_asset_id"),
        "audio_asset_id": video_result.get("audio_asset_id"),
        "reference_asset_id": video_result.get("reference_asset_id"),
        "source_video_url": video_result.get("source_video_url"),
        "thumbnail_url": video_result.get("thumbnail_url"), "duration": video_result.get("duration"),
        "resolution": resolution, "ratio": ratio, "motion": motion,
        "phase": "done",
        "message": "视频生成完成"
    }

HANDLERS = {"image": gen_image, "copy": gen_copy, "collect": gen_collect, "leads": gen_leads, "audio": gen_audio, "video": gen_video}

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
        if kind == "audio":
            record_audio_asset(job_id, r["username"], result)
        if kind == "video":
            record_video_asset(job_id, r["username"], result)
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET status='done', result=?, updated_at=? WHERE id=?",
                      (json.dumps(result, ensure_ascii=False), int(time.time()), job_id)); c.commit()
    except Exception as e:
        if kind == "video":
            try:
                failed = dict(payload)
                failed.update({"status": "failed", "error": str(e)[:300]})
                record_video_asset(job_id, r["username"], failed)
            except Exception:
                pass
        add_points(r["username"], r["cost"])  # 失败退点
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
                    add_points(r["username"], r["cost"])  # 退点
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
        if p == "/api/gen/audio/redeem-slot":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            body = self._json_body()
            try:
                slot = redeem_audio_voice_slot(user["username"], body.get("code"))
                return self._send(200, {"ok": True, "slot": slot})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p == "/api/gen/audio/voice-name":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            body = self._json_body()
            try:
                voice = rename_audio_voice(user["username"], body.get("slot_id"), body.get("name"))
                return self._send(200, {"ok": True, "voice": voice})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p == "/api/gen/audio/clone-vip":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            body = self._json_body()
            try:
                voice = mark_clone_training(user["username"], body.get("slot_id"), body.get("name"))
                threading.Thread(target=clone_vip_voice_background, args=(user["username"], body), daemon=True).start()
                return self._send(200, {"ok": True, "voice": voice})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:220]})
        if p.startswith("/api/gen/") and p[9:] in HANDLERS:
            kind = p[9:]
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录或登录已过期"})
            body = self._json_body()
            cost = cost_of(kind, body)
            if get_points(user["username"]) < cost:
                return self._send(402, {"detail": "点数不足", "need": cost})
            add_points(user["username"], -cost)  # 预扣
            now = int(time.time())
            with closing(jdb()) as c:
                cur = c.execute("INSERT INTO jobs(kind,username,cost,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                (kind, user["username"], cost, json.dumps(body, ensure_ascii=False), now, now))
                c.commit(); jid = cur.lastrowid
            if kind == "video":
                record_video_pending_asset(jid, user["username"], body)
            threading.Thread(target=run_job, args=(jid,), daemon=True).start()
            return self._send(200, {"job_id": jid, "cost": cost, "points_left": get_points(user["username"])})
        self._send(404, {"detail": "not found"})

    def do_GET(self):
        p = self.path.split("?")[0]
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
            return self._send(200, {"items": list_audio_voices(user["username"])})
        if p == "/api/gen/audio/assets":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "???"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try: lim = int((q.get("limit") or ["120"])[0])
            except Exception: lim = 120
            return self._send(200, {"items": list_audio_assets(user["username"], lim)})
        if p == "/api/gen/video/assets":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try: lim = int((q.get("limit") or ["120"])[0])
            except Exception: lim = 120
            return self._send(200, {"items": list_video_assets(user["username"], lim)})
        if p == "/api/gen/audio/slots":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            return self._send(200, {"items": list_user_audio_voice_slots(user["username"])})
        if p == "/api/gen/audio/clone-status":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                return self._send(200, {"ok": True, "result": check_clone_status(user["username"], (q.get("slot_id") or [""])[0])})
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
            if get_points(user["username"]) < 1: return self._send(402, {"detail": "点数不足", "need": 1})
            try:
                r = tikhub.search(platform, keyword, page=page, video_only=False)  # 含图文
            except tikhub.TikHubError as e:
                return self._send(502, {"detail": str(e)[:160]})
            add_points(user["username"], -1)
            items = [{"id": it.get("id"), "platform": it.get("platform"), "title": it.get("title"),
                      "cover": it.get("cover"), "author": it.get("author"), "url": it.get("url"),
                      "note_type": it.get("note_type"),
                      "stats": {"like": it.get("like"), "comment": it.get("comment")}} for it in (r.get("items") or [])]
            return self._send(200, {"items": items, "cost": 1, "points_left": get_points(user["username"])})
        if p == "/api/gen/health":
            return self._send(200, {"ok": True, "service": "huangque-content", "caps": list(HANDLERS),
                                    "has_openai": bool(OPENAI_KEY), "has_tikhub": bool(tikhub.KEY), "tikhub_base": tikhub.BASE})
        self._send(404, {"detail": "not found"})

if __name__ == "__main__":
    init_db()
    threading.Thread(target=reaper, daemon=True).start()  # 僵尸任务清道夫
    print("huangque-content-api on 127.0.0.1:%d  caps=%s" % (PORT, list(HANDLERS)))
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
