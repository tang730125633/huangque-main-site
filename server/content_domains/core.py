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
import os, re, sqlite3, json, time, threading, queue, base64, pathlib, urllib.request, urllib.error, urllib.parse, subprocess, uuid, sys
from contextlib import closing
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tikhub  # 同目录 TikHub 客户端（抖音/小红书/视频号 采集+获客）
import mimetypes; from . import assets_store, jobs_store, startup_recovery, submission_idempotency, miniprogram_security  # 领域存储模块均无反向依赖
try:
    from . import asset_batch, feature_flags
except ImportError:  # Running core.py directly during local checks.
    import asset_batch
    import feature_flags

PORT       = int(os.environ.get("CONTENT_API_PORT", "8096"))
AUTH_BASE  = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095")
AUTH_INTERNAL_TOKEN = os.environ.get("HQ_INTERNAL_TOKEN", "")
try:
    VERIFY_CACHE_TTL = max(0.0, float(os.environ.get("VERIFY_CACHE_TTL", "8") or 8)); VERIFY_CACHE_MAX = max(1, int(os.environ.get("VERIFY_CACHE_MAX", "2048") or 2048))
except Exception:
    VERIFY_CACHE_TTL = 8; VERIFY_CACHE_MAX = 2048
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
DL_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}

def _download_content_type_ext(headers):
    ctype = (headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0].strip().lower()
    return ctype or "application/octet-stream", DL_MIME_EXT.get(ctype, ".mp4")

def _out_path(rel):
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p and p not in {".", ".."}]
    if not parts:
        raise ValueError("文件路径不能为空")
    return OUT_DIR.joinpath(*parts)

def _file_url(rel):
    return "/api/gen/file/" + str(rel or "").replace("\\", "/").lstrip("/")

_cos_disabled_warned = False
def _warn_cos_disabled_once():
    # COS 未启用(通常是 content 进程没加载 COS_* env)会让所有产出回退本地/api/gen/file(私有需鉴权)。
    # 每进程只告警一次，避免刷屏；出现即说明该重启 content 或检查 content.env 的 COS 配置。
    global _cos_disabled_warned
    if not _cos_disabled_warned:
        _cos_disabled_warned = True
        print("[cos] 未启用：COS_SECRET_ID/KEY/REGION/BUCKET 有缺失，本进程所有产出回退本地 /api/gen/file（音频/图片/视频不会走 COS）。检查 content.env 并重启 content。", flush=True)
def public_url(rel, content_type=None, private=False):
    """产出文件的对外链接：COS 已配置且文件存在 → 上传 COS 返回直链；未配置/失败 → 回退本地 /api/gen/file/。
    只在"产出入库"这类一次性点调用；别放进资产列表端点（否则每次刷新都会重复上传）。"""
    local = _file_url(rel)
    if not rel:
        return local
    try:
        from . import cos
        if cos.enabled():
            fp = _out_path(rel)
            if fp.is_file():
                import mimetypes
                ctype = content_type or mimetypes.guess_type(str(rel))[0]
                # 只上传、不删本地：部分产出(如配音)会被下游(口播视频)复用，删了会断链。
                return cos.upload(fp, str(rel), ctype, private=private)
            else:
                # COS 已启用却因文件不在预期路径跳过上传 → 产出会回退本地私有链(需鉴权)。记录便于定位。
                print("[cos] 跳过上传(产出文件不在预期路径)，回退本地: rel=%s fp=%s" % (rel, fp), flush=True)
        else:
            _warn_cos_disabled_once()
    except Exception as e:
        print("[cos] 上传失败，回退本地: %s -> %s" % (rel, e), flush=True)
    return local
COS_COLLECT = os.environ.get("COS_COLLECT", "1").strip().lower() not in ("0", "false", "no")
def public_url_from_remote(remote_url, rel_key, content_type=None):
    """把一个远程 URL(如抖音 CDN 直链)的字节转存到 COS，返回 COS 永久直链。
    COS 已启用且 remote_url 非空 → urllib 拉字节(带 UA/超时) → cos put → 返回直链；
    未配置 / COS_COLLECT=0 / 拉取失败 / 上传失败 → 返回原 remote_url（回退，绝不因转存失败中断采集）。"""
    remote_url = (remote_url or "").strip()
    if not remote_url or not COS_COLLECT:
        return remote_url
    try:
        from . import cos
        if not cos.enabled():
            return remote_url
        data = tikhub._http_get(remote_url)  # 带 UA + 绕代理直连，限 26MB
        if not data:
            return remote_url
        return cos.put_bytes(data, str(rel_key), content_type)
    except Exception as e:
        print("[cos] 采集转存失败，回退原链接: %s -> %s" % (rel_key, e), flush=True)
        return remote_url
def _collect_cos_play_url(platform, vid_id, play_url):
    """采集视频 play_url → COS 永久直链。图集/无 play_url 跳过、保持原样。
    视频号(channels)加密流也跳过 COS 转存——它是 encfilekey 加密流(需 decode_key 解密)，
    转存 COS 会存成加密数据且丢失 decode_key，前端拿到不可播放。保持原 wxapp.tc.qq.com 直链 +
    decode_key，由前端下载代理 /api/gen/dl?dk= 解密。"""
    if not play_url or platform == "channels":
        return play_url
    ident = re.sub(r"[^A-Za-z0-9_.-]", "", str(vid_id or "")) or "v"
    key = "collect/%s/%s.mp4" % ((platform or "x"), ident)
    return public_url_from_remote(play_url, key, "video/mp4")
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
def _sensitive_output_file(rel):
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    name = os.path.basename(rel)
    return (rel.startswith("video/") or
            rel.startswith("audio/voice_preview_") or
            rel.startswith("audio/clone_") or
            rel.startswith("audio/vid_aud_") or
            name.startswith("vid_img_") or
            name.startswith("tryon_cloth_") or
            name.startswith("tryon_bg_"))

def _user_owns_output_file(username, rel):
    """敏感本地文件只允许其资产归属用户读取；删除后的资产不再放行。"""
    if not username or not rel:
        return False
    with closing(adb()) as c:
        # 配音资产(audio_assets)：生成的配音 / 克隆试听样音归属其用户。
        # 缺这一张表会让 voice_preview_*/aud_* 等敏感音频过不了归属校验→404(试听/下载"需要授权")。
        try:
            row = c.execute("""SELECT 1 FROM audio_assets
                WHERE username=? AND COALESCE(deleted,0)=0 AND file=? LIMIT 1""",
                (username, rel)).fetchone()
            if row:
                return True
        except Exception:
            pass
        row = c.execute("""SELECT 1 FROM video_assets
            WHERE username=? AND status!='deleted'
              AND ? IN (image_file,audio_file,reference_video_file,video_file)
            LIMIT 1""", (username, rel)).fetchone()
        if row:
            return True
        row = c.execute("""SELECT 1 FROM avatars
            WHERE username=? AND status!='deleted' AND image_file=? LIMIT 1""",
            (username, rel)).fetchone()
        if row:
            return True
        row = c.execute("""SELECT 1 FROM audio_voices
            WHERE username=? AND scope='personal' AND preview_file=? LIMIT 1""",
            (username, rel)).fetchone()
        return bool(row)

# ---- 能力定义：成本(点数) + 处理函数 ----
def _env_positive_int(name, default):
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except Exception:
        value = default
    return max(1, value)

VIDEO_COST = _env_positive_int("VIDEO_COST", 20)
JOB_WORKERS, FAST_JOB_WORKERS = _env_positive_int("CONTENT_JOB_WORKERS", 3), _env_positive_int("CONTENT_FAST_JOB_WORKERS", 3)  # 慢队列(换装/果肉video)/快队列(图片/音频等)各自worker数，分开防视频堵死快任务
TALKING_JOB_WORKERS = _env_positive_int("CONTENT_TALKING_JOB_WORKERS", 10)  # 口播(video mode=text/audio)专用池：HeyGen口播能扛高并发(50并发实测无429)
CINEMATIC_JOB_WORKERS = _env_positive_int("CONTENT_CINEMATIC_JOB_WORKERS", 10)  # AI剧情视频池(HeyGen)。20路并发实测(10口播+10剧情同时生成)：20/20全成、零降速(口播114s vs 单条基线104s)——HeyGen 的渲染容量远大于20，文档说的「Max Concurrent Video Jobs=10」不是硬限制。唯一的真限制是【提交突发】，由 _heygen_retry_429 兜住
AVATAR_JOB_WORKERS = _env_positive_int("CONTENT_AVATAR_JOB_WORKERS", 5)        # 建形象池。5 路是实测的干净档位(2026-07-12)：5并发 5/5成功、0×429、零降速(就绪中位19.7s vs 单条基线19.8s)；10并发 HeyGen 侧照样零429不降速，但【我们的出境隧道】开始丢包(1条TLS握手超时、1条提交花了57s)。所以瓶颈是隧道不是HeyGen，隧道扩容后可再往上调。串行(1)的吞吐只有144个/小时——500人集中建形象要排3.5小时，而建形象是电影化身的【入口】，堵在这里等于整个功能没法用；5路→约900个/小时，排队压到35分钟
IMAGE_JOB_WORKERS = _env_positive_int("CONTENT_IMAGE_JOB_WORKERS", 10)       # 生图专用池(生图慢90~450s，从快池拆出别拖死秒级任务)。10=500用户高峰约150张/时所需6.3个+60%余量；1worker≈24张/时(实测中位149s)
JOB_QUEUE_MAX = _env_positive_int("CONTENT_JOB_QUEUE_MAX", 32)
MAX_USER_ACTIVE_JOBS = _env_positive_int("MAX_USER_ACTIVE_JOBS", 5)                  # 单用户可同时提交(pending+running)的任务上限，超了提交即 429
MAX_USER_ACTIVE_XIAOLE_VIDEO = _env_positive_int("MAX_USER_ACTIVE_XIAOLE_VIDEO", 3)  # 单用户果肉/豆姐/欧米视频 active 上限：别让单一渠道吃满全部任务位
MAX_USER_ACTIVE_TRYON = _env_positive_int("MAX_USER_ACTIVE_TRYON", 1)                # 单用户换装视频 active 上限：最重链路，默认一次只放 1 条
MAX_USER_ACTIVE_CINEMATIC = _env_positive_int("MAX_USER_ACTIVE_CINEMATIC", 2)        # 单用户剧情视频 active 上限：重任务(约8分钟)，别让一个人占满 10 个 worker
MAX_USER_ACTIVE_AVATAR = _env_positive_int("MAX_USER_ACTIVE_AVATAR", 2)              # 单用户建形象 active 上限。池已不再串行(5路)，但仍留 2：池只有 5 个槽，一个人占满就把别人挡在门外——而建形象是电影化身的入口
MAX_USER_RUNNING_TALKING = _env_positive_int("MAX_USER_RUNNING_TALKING", 2)          # 单用户口播「运行中」并发上限：最多同时生成2条，多提交的留 pending 排队
MAX_USER_RUNNING_IMAGE = _env_positive_int("MAX_USER_RUNNING_IMAGE", 3)              # 单用户生图「运行中」并发上限=每人可并行3个。闸数全表 kind='image'，imggen也写这表→两服务合计3个，不是各3个
MAX_GLOBAL_RUNNING_BREAKDOWN = _env_positive_int("MAX_GLOBAL_RUNNING_BREAKDOWN", 2)  # 爆款拆解会跑下载+ffmpeg+ASR+多模态，全局限 2 条防慢任务挤爆机器
SERVICE_OWNER = "content"   # 本服务在 jobs.owner 的署名(#579)；两处全表扫描必须按它过滤，缘由见 jobs_store.ensure_owner_column
# reaper 各 kind 的超时宽限(秒)，默认 360。tryon 两段式+心跳刷新；xiaole_video 内部轮询600s+转存；
# image 多图/中转慢；collect 下载+ffmpeg抽音轨+ASR 且转写全站串行(实测成功平均88s)。video 按 mode 另算。
# 【生成死线】从 worker 真正开始干活算起(不含排队)：口播/果肉/豆姐/欧米等视频引擎统一 15 分钟。
# 各引擎轮询死线都用它，到点抛明确的「生成超时」并退点。
VIDEO_GEN_DEADLINE = _env_positive_int("VIDEO_GEN_DEADLINE", 900)
# reaper 宽限必须【大于】引擎死线：引擎到点抛明确的「生成超时」并退点，reaper 只兜底(worker 整个
# 卡死、连 updated_at 都不刷时才轮到它)。反过来 reaper 先杀 = 用户拿到没头没脑的超时、而 worker
# 还在跑上游照样收钱(口播原来就这样：中转死线 1200s、reaper 宽限却 540s)。多的 300s 给轮询之外的
# 上传/下载/烧字幕/混 BGM —— 那些阶段不刷 updated_at。
VIDEO_REAPER_GRACE = VIDEO_GEN_DEADLINE + 300

# 【电影化身单独一条死线】30 分钟(kongli 2026-07-17，原 20 分钟→更早 15 分钟)。它是唯一「提交即扣费」
# 的引擎($7/条，收钱在提交那一刻)：别的引擎超时顶多白等，它超时【钱已经花了】。线上真出现过我们
# 20 分钟判超时退点、HeyGen 那边其实还在渲染/已 completed 出片 —— 片子被扔、$7 照付。宁可多等。
# ⚠️ 宽限用加法钉死，别拆成两个字面量各写各的(口播就栽过：死线 1200s、宽限却 540s，reaper 先杀)。
CINEMATIC_GEN_DEADLINE = _env_positive_int("HEYGEN_MOTION_DEADLINE", 1800)
CINEMATIC_REAPER_GRACE = CINEMATIC_GEN_DEADLINE + 300
# 没登记的 kind 用它 —— 绝不能是 0（见 reaper 里的注释：0 的语义是「立刻杀」）。
KIND_GRACE_DEFAULT = _env_positive_int("KIND_GRACE_DEFAULT", 900)
KIND_GRACE = {"tryon": 2400, "xiaole_video": 1200, "image": 900, "collect": 1200,
              "cinematic": CINEMATIC_REAPER_GRACE, "avatar": 300, "breakdown": 600}
# ⚠️ tryon 【不】跟着 15 分钟走：线上实测线路一中位 909s、**p90 1612s(27 分钟)**。
#    砍到 15 分钟会把超过一成的换装任务判成失败。要改它得先把那条链路本身提速。
AVATAR_COST = _env_positive_int("AVATAR_COST", 5)   # 建形象：象征性收费防刷，失败自动退点
# ⚠️ cost_of() 回落到 COST.get(kind, 0) —— 新增 kind 忘了在这里登记，就是【免费】。
COST = {"image": 12, "copy": 3, "audio": 10, "video": VIDEO_COST, "tryon": 40,
        "cinematic": VIDEO_COST, "avatar": AVATAR_COST, "breakdown": 8}  # collect/leads/cinematic 走 cost_of() 动态算
# cinematic 的这条已经不生效了 —— 电影化身按成片秒数计费（video.cinematic_cost），
# cost_of() 里有它自己的分支、必定先 return。留在这里只当保险：万一哪天分支被绕过，
# 也是按 VIDEO_COST 收费，而不是回落到 0（=免费送 $7 一条的视频）。
OPENAI_BASE = os.environ.get("OPENAI_BASE", "https://api.openai.com")
ZELONG_KEY  = os.environ.get("ZELONG_KEY", "")                              # 泽龙Ai 中转站(OpenAI 兼容)
ZELONG_BASE = os.environ.get("ZELONG_BASE", "https://api.xiaoleai.team")
ZELONG2_KEY  = os.environ.get("ZELONG2_KEY", "")                            # 泽龙2 专供生图号池(chatgpt2api，OpenAI 兼容)
ZELONG2_BASE = os.environ.get("ZELONG2_BASE", "https://api.zelong.vip/image-pool")
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
        _ensure_column(c, "jobs", "deleted", "INTEGER DEFAULT 0")
        _ensure_column(c, "jobs", "refunded", "INTEGER DEFAULT 0")  # 退点幂等键(#187)
        _ensure_column(c, "jobs", "owner", "TEXT")                  # 归属服务(#511)，见 SERVICE_OWNER
        submission_idempotency.ensure_table(c)
        c.commit()
    feature_flags.init_db()
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
            background_file TEXT,
            tryon_mode TEXT,
            model TEXT,
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
        c.execute("""CREATE TABLE IF NOT EXISTS asset_marks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            asset_kind TEXT NOT NULL,
            asset_key TEXT NOT NULL,
            favorite INTEGER NOT NULL DEFAULT 0,
            tags TEXT,
            updated_at INTEGER,
            UNIQUE(username, asset_kind, asset_key)
        )""")
        _ensure_column(c, "audio_voices", "slot_id", "TEXT")
        _ensure_column(c, "audio_assets", "deleted", "INTEGER DEFAULT 0")
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
        _ensure_column(c, "video_assets", "background_file", "TEXT")
        _ensure_column(c, "video_assets", "tryon_mode", "TEXT")
        _ensure_column(c, "video_assets", "model", "TEXT")
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

ASSET_MARK_KINDS = {"image", "audio", "video", "avatar"} | assets_store.KINDS  # 新三类的 asset_key 同样用 str(job_id)

def _clean_asset_kind(kind):
    kind = str(kind or "").strip().lower()
    if kind not in ASSET_MARK_KINDS:
        raise ValueError("不支持的资产类型")
    return kind

def _clean_asset_key(key):
    key = str(key or "").strip()
    if not key:
        raise ValueError("缺少资产标识")
    return key[:500]

def _clean_asset_tags(tags):
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise ValueError("标签格式应为数组")
    out = []
    seen = set()
    for tag in tags:
        tag = re.sub(r"\s+", " ", str(tag or "").strip())[:24]
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= 8:
            break
    return out

def _upsert_asset_mark(username, kind, key, favorite=None, tags=None):
    kind = _clean_asset_kind(kind)
    key = _clean_asset_key(key)
    now = int(time.time())
    with closing(adb()) as c:
        row = c.execute("""SELECT favorite,tags FROM asset_marks
                           WHERE username=? AND asset_kind=? AND asset_key=?""",
                        (username, kind, key)).fetchone()
        fav = int(row["favorite"]) if row else 0
        tag_list = []
        if row and row["tags"]:
            try:
                tag_list = json.loads(row["tags"])
            except Exception:
                tag_list = [t.strip() for t in str(row["tags"]).split(",") if t.strip()]
        if favorite is not None:
            fav = 1 if favorite else 0
        if tags is not None:
            tag_list = _clean_asset_tags(tags)
        c.execute("""INSERT INTO asset_marks(username,asset_kind,asset_key,favorite,tags,updated_at)
                     VALUES(?,?,?,?,?,?)
                     ON CONFLICT(username,asset_kind,asset_key) DO UPDATE SET
                       favorite=excluded.favorite,
                       tags=excluded.tags,
                       updated_at=excluded.updated_at""",
                  (username, kind, key, fav, json.dumps(tag_list, ensure_ascii=False), now))
        c.commit()
    return {"kind": kind, "key": key, "favorite": bool(fav), "tags": tag_list, "updated_at": now}

def _list_asset_marks(username, kind):
    kind = _clean_asset_kind(kind)
    with closing(adb()) as c:
        rows = c.execute("""SELECT asset_key,favorite,tags,updated_at FROM asset_marks
                            WHERE username=? AND asset_kind=? ORDER BY updated_at DESC""",
                         (username, kind)).fetchall()
    marks = {}
    for row in rows:
        try:
            tags = json.loads(row["tags"] or "[]")
        except Exception:
            tags = [t.strip() for t in str(row["tags"] or "").split(",") if t.strip()]
        marks[row["asset_key"]] = {
            "favorite": bool(row["favorite"]),
            "tags": tags,
            "updated_at": row["updated_at"],
        }
    return marks
def _delete_asset_mark(username, kind, key):
    try:
        key = _clean_asset_key(key)
        kind = _clean_asset_kind(kind)
    except Exception:
        return
    with closing(adb()) as c:
        c.execute("DELETE FROM asset_marks WHERE username=? AND asset_kind=? AND asset_key=?",
                  (username, kind, key))
        c.commit()
def delete_user_asset(username, kind, asset_id):
    kind = str(kind or "").strip().lower()
    if kind in assets_store.KINDS: return assets_store.soft_delete(username, int(asset_id))  # copy/collect/leads 在统一 assets 表
    if kind not in {"image", "audio", "video"}:
        raise ValueError("不支持的资产类型")
    try:
        asset_id = int(asset_id)
    except Exception:
        raise ValueError("缺少资产标识")
    now = int(time.time())
    if kind == "image":
        with closing(jdb()) as c:
            _ensure_column(c, "jobs", "deleted", "INTEGER DEFAULT 0")
            cur = c.execute("""UPDATE jobs SET deleted=1, updated_at=?
                               WHERE id=? AND username=? AND kind='image' AND COALESCE(deleted,0)=0""",
                            (now, asset_id, username))
            c.commit()
        if cur.rowcount < 1:
            raise LookupError("资产不存在或不属于当前账号")
        _delete_asset_mark(username, "image", str(asset_id))
        return {"kind": kind, "id": asset_id, "deleted": True}
    if kind == "audio":
        with closing(adb()) as c:
            _ensure_column(c, "audio_assets", "deleted", "INTEGER DEFAULT 0")
            cur = c.execute("""UPDATE audio_assets SET deleted=1
                               WHERE id=? AND username=? AND COALESCE(deleted,0)=0""",
                            (asset_id, username))
            c.commit()
        if cur.rowcount < 1:
            raise LookupError("资产不存在或不属于当前账号")
        _delete_asset_mark(username, "audio", str(asset_id))
        return {"kind": kind, "id": asset_id, "deleted": True}
    with closing(adb()) as c:
        cur = c.execute("""UPDATE video_assets SET status='deleted', updated_at=?
                           WHERE id=? AND username=? AND status!='deleted'""",
                        (now, asset_id, username))
        c.commit()
    if cur.rowcount < 1:
        raise LookupError("资产不存在或不属于当前账号")
    _delete_asset_mark(username, "video", str(asset_id))
    return {"kind": kind, "id": asset_id, "deleted": True}
# ============ 鉴权（向 auth 服务核验 token） ============
_verify_cache = {}; _verify_cache_lock = threading.Lock()
AUTH_COOKIE_NAME = os.environ.get("HQ_AUTH_COOKIE_NAME", "hq_session")

def _cookie_token(header):
    try:
        jar = cookies.SimpleCookie()
        jar.load(header or "")
        morsel = jar.get(AUTH_COOKIE_NAME)
        return morsel.value.strip() if morsel and morsel.value else ""
    except Exception:
        return ""

def _request_token(headers):
    auth = headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        if token and token != "__cookie__":
            return token
    return _cookie_token(headers.get("Cookie"))

def verify(token):
    if not token: return None
    now = time.time()
    if VERIFY_CACHE_TTL:
        with _verify_cache_lock:
            item = _verify_cache.get(token)
            if item and item[0] > now: return dict(item[1])
            if item: _verify_cache.pop(token, None)
    try:
        req = urllib.request.Request(AUTH_BASE + "/api/auth/me",
                                     headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=6) as r:
            user = json.loads(r.read()).get("user")
    except Exception:
        if VERIFY_CACHE_TTL:
            with _verify_cache_lock: _verify_cache.pop(token, None)
        return None
    if not isinstance(user, dict): return None
    if VERIFY_CACHE_TTL:
        with _verify_cache_lock:
            if len(_verify_cache) >= VERIFY_CACHE_MAX:
                for k, v in list(_verify_cache.items()):
                    if v[0] <= now: _verify_cache.pop(k, None)
                if len(_verify_cache) >= VERIFY_CACHE_MAX: _verify_cache.pop(next(iter(_verify_cache)), None)
            _verify_cache[token] = (now + VERIFY_CACHE_TTL, dict(user))
    return dict(user)

def _domains():
    from . import audio, points, video
    return audio, points, video
def _leads_domain():
    from . import leads
    return leads
def _must_change_password(user):
    return bool(user and user.get("must_change"))

_job_public_dict, _idempotency_key = jobs_store.public_dict, submission_idempotency.clean_key
def _idempotency_begin(username, endpoint, key, body): return submission_idempotency.begin(jdb, username, endpoint, key, body)
def _idempotency_complete(username, endpoint, key, response): submission_idempotency.complete(jdb, username, endpoint, key, response)
def _idempotency_abort(username, endpoint, key): submission_idempotency.abort(jdb, username, endpoint, key)
# ============ 图片能力：gpt-image-2 ============
# 三种模式同一入口：无图=文生图(generations)；有图无蒙版=图生图(edits)；有图有蒙版=局部修改(edits+mask)
# 老表把 9:16 和 3:4 都映射成 1024x1536 —— 那是 2:3，两个按钮出的是同一张图，谁都没拿到自己选的比例。
# 实测(2026-07-10)：gpt-image-2 并不只支持三个预设，唯一约束是「宽高都必须是 16 的倍数」
#   （传 123x456 报 "Width and height must both be divisible by 16"）。
# 下列尺寸已真实出图并读 PNG 头核对，generations 与 edits 两个端点都接受、都精确回显该尺寸。
# 成本几乎不变（image output token 不与像素成正比）：9:16 $0.0412→$0.0424(+3%)，3:4 →$0.0508(+23%)。
SIZES = {"1:1": "1024x1024", "9:16": "1152x2048", "16:9": "2048x1152", "3:4": "1200x1600"}

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

def _post(path, data, ctype, base=None, key=None, proxy=True, timeout=300):
    """timeout 可由调用方按剩余预算收紧/放宽（如泽龙2号池要压在总死线内）。默认 300 保持原行为。"""
    req = urllib.request.Request((base or OPENAI_BASE) + path, data=data,
                                 headers={"Authorization": "Bearer " + (key or OPENAI_KEY), "Content-Type": ctype}, method="POST")
    if proxy:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    with _NOPROXY.open(req, timeout=timeout) as r:  # 国内中转直连，不走 mihomo
        return json.loads(r.read())

def _post_bytes(path, data, ctype):  # 返回原始字节(TTS 拿 mp3 二进制)
    req = urllib.request.Request(OPENAI_BASE + path, data=data,
                                 headers={"Authorization": "Bearer " + OPENAI_KEY, "Content-Type": ctype}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()
# ============ 后台 worker（有界队列 + 固定 worker，失败退点；慢/快队列分开防视频堵死作图） ============
_job_queue = queue.Queue(maxsize=JOB_QUEUE_MAX)          # 慢队列(tryon/xiaole_video + video兜底)
_fast_job_queue = queue.Queue(maxsize=JOB_QUEUE_MAX)     # 快队列(audio/copy/collect/leads等秒级任务)
_talking_job_queue = queue.Queue(maxsize=JOB_QUEUE_MAX)  # 口播队列(video mode=text/audio)
_image_job_queue = queue.Queue(maxsize=JOB_QUEUE_MAX)    # 生图队列(kind=image，从快池拆出防拖死快任务)
_cinematic_job_queue = queue.Queue(maxsize=JOB_QUEUE_MAX)  # AI剧情视频队列(kind=cinematic，HeyGen，约8分钟/条)
_avatar_job_queue = queue.Queue(maxsize=JOB_QUEUE_MAX)     # 建形象队列(kind=avatar，串行池)
_queued_job_ids = set()
_job_queue_lock = threading.Lock()
_run_gate_lock = threading.Lock()  # 单用户口播运行闸：count+抢running 在此锁内原子，防多worker同时超发
_submission_lock = threading.Lock()  # 活跃数检查+扣点+入队串行，批量与单条不能一起冲破单用户上限
_workers_started = False

# ============ 优雅停机（graceful drain）============
#
# 线上：近 14 天 53 条任务死于「服务重启中断，已退点，请重新提交」—— 涉及 8 个功能。
# 每次上线，正在生成的任务全部被判失败。用户等了几分钟，什么都没拿到。
#
# 根因：hardening.conf 的注释写着「优雅停机」，实际配的是 TimeoutStopSec=15 ——
# 而视频任务要跑 5~15 分钟。systemd 发 SIGTERM，进程【根本不理】（代码里一个 signal
# handler 都没有），15 秒后 SIGKILL，在飞任务全部猝死。
#
# 真正的优雅停机是两件事：
#   1. 收到 SIGTERM → 立刻【停止收新任务】（提交返回 503，不扣点）
#   2. 等在飞的任务【跑完】再退出（systemd 的 TimeoutStopSec 要给够）
#
# 代价：部署变慢（最坏要等最后一条视频跑完）。这是对的取舍 —— 一次部署慢几分钟，
# 换的是用户的任务不被杀。急着退出的话，Ctrl-C 两次 / systemctl kill 仍然能强杀。
_shutting_down = threading.Event()
_inflight = 0                      # 正在 run_job 里跑着的任务数
_inflight_lock = threading.Lock()
# 排空最长等多久。最长任务是电影化身 30 分钟（CINEMATIC_GEN_DEADLINE=1800s）+ 上传下载余量。
# 必须 ≥ 最长死线，否则重启时会把跑到一半的 30 分钟剧情视频砍掉（$7 已扣，片子丢）。
DRAIN_TIMEOUT = _env_positive_int("CONTENT_DRAIN_TIMEOUT", 1800)


def is_shutting_down():
    return _shutting_down.is_set()

# CAS 抢终态 / 退点幂等：实现在 content_domains/jobs_store.py，三个共写 jobs 表的服务共用一份。
def _set_terminal(job_id, status, result=None, error=None, from_states=("running",)):
    return jobs_store.set_terminal(jdb, job_id, status, result, error, from_states)
def _refund_once(job_id, username, cost, transaction_key=""):
    transaction_key = transaction_key or jobs_store.refund_transaction_key(job_id, username)
    return jobs_store.refund_once(jdb, job_id, username, cost, lambda u, c: (
        _domains()[1].refund_points(u, c, "job#%d" % job_id, transaction_key=transaction_key), True)[1])
def _pick_job_queue(kind, mode=None):
    # kind缺省(旧调用/测试)保守走慢队列；生图(慢90~450s)走生图池；秒级快任务(音频/文案/采集/名单)走快队列；
    # video(口播 text/audio)走口播池；tryon/xiaole_video 走慢池。
    if kind is None:
        return _job_queue
    if kind == "image":
        return _image_job_queue
    if kind == "breakdown":
        return _job_queue               # 下载+ffmpeg+ASR+多模态，走慢池别堵快任务
    if kind == "cinematic":
        return _cinematic_job_queue     # HeyGen 剧情视频，约 8 分钟/条，10 个 worker
    if kind == "avatar":
        return _avatar_job_queue        # 建形象，串行 1 个 worker
    if kind not in {"video", "tryon", "xiaole_video"}:
        return _fast_job_queue
    if kind == "video":
        return _talking_job_queue
    return _job_queue

def enqueue_jobs(job_ids, kind=None, mode=None):
    try:
        ids = [int(job_id) for job_id in job_ids]
    except Exception:
        return False
    q = _pick_job_queue(kind, mode)
    with _job_queue_lock:
        fresh = [job_id for job_id in ids if job_id not in _queued_job_ids]
        if q.maxsize > 0 and q.qsize() + len(fresh) > q.maxsize:
            return False
        for job_id in fresh:
            q.put_nowait(job_id)
            _queued_job_ids.add(job_id)
        return True

def enqueue_job(job_id, kind=None, mode=None):
    return enqueue_jobs([job_id], kind, mode)

def _user_active_job_count(username):
    if not username:
        return 0
    with closing(jdb()) as c:
        row = c.execute("""SELECT COUNT(*) AS n FROM jobs
                           WHERE username=? AND status IN ('pending','running')
                             AND COALESCE(deleted,0)=0""",
                        (username,)).fetchone()
    return int(row["n"] if row else 0)

def _user_active_kind_count(username, kind):
    if not username or not kind:
        return 0
    with closing(jdb()) as c:
        row = c.execute("""SELECT COUNT(*) AS n FROM jobs
                           WHERE username=? AND kind=? AND status IN ('pending','running')
                             AND COALESCE(deleted,0)=0""",
                        (username, kind)).fetchone()
    return int(row["n"] if row else 0)

def _user_video_submit_limit(kind, body, username, cost):
    if kind == "xiaole_video":
        active = _user_active_kind_count(username, "xiaole_video")
        if active >= MAX_USER_ACTIVE_XIAOLE_VIDEO:
            return {"detail": "当前果肉/豆姐/欧米视频最多同时排队或生成 %d 个任务，请等待部分完成后再继续" % MAX_USER_ACTIVE_XIAOLE_VIDEO,
                    "code": "xiaole_active_cap", "active_jobs": active, "max_active_jobs": MAX_USER_ACTIVE_XIAOLE_VIDEO,
                    "retry_after_ms": 4000, "need": cost}
    elif kind == "tryon":
        active = _user_active_kind_count(username, "tryon")
        if active >= MAX_USER_ACTIVE_TRYON:
            return {"detail": "当前换装视频最多同时排队或生成 %d 个任务，请等待任务完成后再继续" % MAX_USER_ACTIVE_TRYON,
                    "code": "tryon_active_cap", "active_jobs": active, "max_active_jobs": MAX_USER_ACTIVE_TRYON,
                    "retry_after_ms": 4000, "need": cost}
    elif kind == "cinematic":
        active = _user_active_kind_count(username, "cinematic")
        if active >= MAX_USER_ACTIVE_CINEMATIC:
            return {"detail": "当前剧情视频最多同时排队或生成 %d 个任务，请等待部分完成后再继续" % MAX_USER_ACTIVE_CINEMATIC,
                    "code": "cinematic_active_cap", "active_jobs": active, "max_active_jobs": MAX_USER_ACTIVE_CINEMATIC,
                    "retry_after_ms": 4000, "need": cost}
    elif kind == "avatar":
        active = _user_active_kind_count(username, "avatar")
        if active >= MAX_USER_ACTIVE_AVATAR:
            return {"detail": "当前最多同时创建 %d 个形象，请等待完成后再继续" % MAX_USER_ACTIVE_AVATAR,
                    "code": "avatar_active_cap", "active_jobs": active, "max_active_jobs": MAX_USER_ACTIVE_AVATAR,
                    "retry_after_ms": 4000, "need": cost}
    return None

def _user_running_talking_count(username):
    """该用户「运行中」的口播条数(kind=video，即 text/audio 口播)。"""
    if not username:
        return 0
    with closing(jdb()) as c:
        row = c.execute("SELECT COUNT(*) AS n FROM jobs WHERE username=? AND status='running' AND kind='video'",
                        (username,)).fetchone()
    return row["n"] if row else 0

def _user_running_image_count(username):
    """该用户「运行中」的生图条数(kind=image)。"""
    if not username:
        return 0
    with closing(jdb()) as c:
        row = c.execute("SELECT COUNT(*) AS n FROM jobs WHERE username=? AND status='running' AND kind='image'",
                        (username,)).fetchone()
    return int(row["n"] if row else 0)


def _global_running_breakdown_count():   # 全局运行中的爆款拆解数（按 owner 过滤，仅本服务）
    with closing(jdb()) as c:
        row = c.execute("SELECT COUNT(*) AS n FROM jobs WHERE status='running' AND kind='breakdown' AND COALESCE(owner,?)=?",
                        (SERVICE_OWNER, SERVICE_OWNER)).fetchone()
    return int(row["n"] if row else 0)

def _reject_pending_job(job_id, username, cost, reason):
    claimed = _set_terminal(job_id, "error", error=reason, from_states=("pending",))
    if claimed:
        _refund_once(job_id, username, cost)
    return claimed

def _job_worker_loop(q):
    global _inflight
    while True:
        try:
            # 带超时地取 —— 否则停机时 worker 会永远阻塞在 q.get() 上，排空检测不到它已经空了
            job_id = q.get(timeout=1.0)
        except queue.Empty:
            if _shutting_down.is_set():
                return          # 停机中且队列已空 → 这个 worker 可以退了
            continue
        with _inflight_lock:
            _inflight += 1
        try:
            run_job(job_id)
        finally:
            with _job_queue_lock:
                _queued_job_ids.discard(job_id)
            with _inflight_lock:
                _inflight -= 1
            q.task_done()

def _recover_pending_jobs(limit=None):
    limit = int(limit or JOB_QUEUE_MAX)
    with closing(jdb()) as c:
        rows = c.execute("SELECT id, kind, payload FROM jobs WHERE status='pending' AND COALESCE(owner,?)=? ORDER BY id ASC LIMIT ?",
                         (SERVICE_OWNER, SERVICE_OWNER, limit)).fetchall()
    recovered = 0
    for row in rows:
        try:
            mode = (json.loads(row["payload"] or "{}") or {}).get("mode", "")
        except Exception:
            mode = ""
        if not enqueue_job(row["id"], row["kind"], mode):
            break
        recovered += 1
    return recovered

def _pending_job_scanner():
    while True:
        try:
            _recover_pending_jobs(JOB_QUEUE_MAX)
            jobs_store.retry_failed_refunds(jdb, _refund_once, JOB_QUEUE_MAX)
        except Exception:
            pass
        time.sleep(30)

_ALL_JOB_QUEUES = (_job_queue, _fast_job_queue, _talking_job_queue,
                   _image_job_queue, _cinematic_job_queue, _avatar_job_queue)


def start_job_workers():
    global _workers_started
    with _job_queue_lock:
        if _workers_started:
            return
        _workers_started = True
    for count, q, prefix in ((JOB_WORKERS, _job_queue, "content-job-worker"), (FAST_JOB_WORKERS, _fast_job_queue, "content-fast-worker"),
                             (TALKING_JOB_WORKERS, _talking_job_queue, "content-talking-worker"),
                             (IMAGE_JOB_WORKERS, _image_job_queue, "content-image-worker"),
                             (CINEMATIC_JOB_WORKERS, _cinematic_job_queue, "content-cinematic-worker"),
                             (AVATAR_JOB_WORKERS, _avatar_job_queue, "content-avatar-worker")):
        for i in range(count):
            threading.Thread(target=_job_worker_loop, args=(q,), name="%s-%d" % (prefix, i + 1), daemon=True).start()
    threading.Thread(target=_pending_job_scanner, name="content-job-recover", daemon=True).start()
    _recover_pending_jobs(JOB_QUEUE_MAX)
def drain_and_exit(signum=None, frame=None):
    """SIGTERM → 停止收新任务 → 等在飞的跑完 → 退出。

    ⚠️ 跑在【信号处理器】里（主线程），【绝不能在这里阻塞等待】——主线程一卡，serve_forever
    的 accept 就停摆，排空那几分钟里【读接口(形象/资产/任务状态/文件)全部拒连】。2026-07-15
    事故：一条卡住的任务把排空拖满 ~19 分钟，整个 content API 随之下线，形象/资产刷不出来。

    正确做法：立刻置 _shutting_down（do_POST 据此 503 拒新提交、不扣点），HTTP 服务照常 serve
    读接口、绝不在排空期间关它；等待丢给后台线程，排空完/超时才退出（进程退出端口才释放，
    systemd 随即拉起新进程，只有毫秒级切换空档）。第二次信号 → 立刻退出（急着回滚用）。
    """
    if _shutting_down.is_set():
        print("[drain] 再次收到停机信号，立刻退出（在飞任务会被判失败退点）", flush=True)
        os._exit(1)
    _shutting_down.set()
    # 等待放到后台线程 —— 主线程立刻返回，serve_forever 继续 accept，读接口不受影响。
    threading.Thread(target=_drain_then_exit, args=(time.time(),), daemon=True).start()


def _drain_then_exit(t0):
    """后台线程：等在飞任务跑完（或超时）再退。期间 HTTP 服务不停，读接口照常。"""
    while time.time() - t0 < DRAIN_TIMEOUT:
        queued = sum(q.qsize() for q in _ALL_JOB_QUEUES)
        with _inflight_lock:
            running = _inflight
        if queued == 0 and running == 0:
            print("[drain] 排空完成，用时 %.0fs" % (time.time() - t0), flush=True)
            os._exit(0)
        print("[drain] 等在飞任务：排队 %d、执行中 %d（已等 %.0fs / 上限 %ds，期间读接口正常）"
              % (queued, running, time.time() - t0, DRAIN_TIMEOUT), flush=True)
        time.sleep(3)

    print("[drain] 超过 %ds 仍未排空，强制退出 —— 剩下的由 reclaim_orphaned_running 判失败退点"
          % DRAIN_TIMEOUT, flush=True)
    os._exit(0)


def install_signal_handlers():
    import signal
    signal.signal(signal.SIGTERM, drain_and_exit)
    signal.signal(signal.SIGINT, drain_and_exit)


def _mark_video_asset_failed(job_id, kind, error):
    """判失败时同步 video_asset 到失败终态(否则前端历史卡片读 video_assets 一直「生成中」)。⚠️用 update_video_asset_phase(UPDATE)非 record_video_asset(INSERT):mode 有 NOT NULL，cinematic/xiaole 失败路径无 mode→IntegrityError 被吞→卡 running。"""
    if kind not in {"video", "tryon", "xiaole_video", "cinematic"}:
        return
    try:
        _, _, video_domain = _domains()
        video_domain.update_video_asset_phase(job_id, "failed", status="failed", error=str(error)[:300])
    except Exception:
        pass

# ============ 任务心跳：让 reaper 的信号是真的 ============
#
# reaper 判的是「多久没心跳」（jobs.updated_at）。但我们的代码在长操作期间【根本不发心跳】：
#   * HeyGen 轮询（最长 900s，循环里一次 UPDATE 都没有）
#   * 烧字幕（whisper 跑 CPU，几分钟）
#   * 生图的 HTTP 调用、成片下载
# 于是 reaper 看到「这么久没动静」，就当 worker 死了，把【还在正常干活】的任务杀掉。
#
# 线上近 30 天被 reaper 误判「生成超时」的：video 45、xiaole_video 25、image 17、
# collect 7、tryon 3 —— 用户为此白等了 2655 分钟（44 小时），然后看到「生成超时，已退点」。
#
# 修法【不是】把 grace 调宽（那只是让误杀晚一点发生），而是让 worker 真的发心跳 ——
# 这样「没心跳」才真的等于「worker 死了」。
#
# ⚠️ 心跳只证明【worker 还活着】，不证明【任务会成功】。任务的时间上限仍然由各引擎自己的
# 死线兜住（VIDEO_GEN_DEADLINE / WS_DEADLINE / 各种 IMG_DEADLINE）—— 那些到点会抛一个
# 说得清的错。别因为有了心跳就把死线删了，否则一个真的卡死的上游会让任务永远挂着。
JOB_HEARTBEAT_INTERVAL = _env_positive_int("CONTENT_JOB_HEARTBEAT", 30)


def _start_job_heartbeat(job_id):
    """开一个后台线程，任务跑着的时候每 30 秒刷一次 jobs.updated_at。返回 stop()。"""
    stop = threading.Event()

    def beat():
        while not stop.wait(JOB_HEARTBEAT_INTERVAL):
            try:
                with closing(jdb()) as c:
                    c.execute("UPDATE jobs SET updated_at=? WHERE id=? AND status='running'",
                              (int(time.time()), job_id))
                    c.commit()
            except Exception:
                pass   # 心跳失败不该影响任务本身 —— 最坏是 reaper 把它当成死了
    threading.Thread(target=beat, name="job-heartbeat-%s" % job_id, daemon=True).start()
    return stop.set


def run_job(job_id):
    with closing(jdb()) as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r: return
    kind = r["kind"]; payload = json.loads(r["payload"] or "{}")
    username = r["username"]; cost = r["cost"]
    mode = str(payload.get("mode") or "").lower()
    is_talking = (kind == "video")   # 口播=video(text/audio)
    is_image = (kind == "image")
    stop_heartbeat = None
    is_breakdown = (kind == "breakdown")
    try:
        # 单用户口播/生图「运行中」并发闸 + 原子抢 running：同进程锁内 count+claim，防多 worker 同时超发。
        # 整段放进 try：抢 running 那句 UPDATE 自己抛异常(SQLite 锁冲突/磁盘满)时，任务还停在 pending，
        # 下面的 except 用 from_states 含 pending 把它判死退点 —— 否则预扣的点永久丢失，
        # 因为 reaper 只扫 running、从不回收 pending。
        with _run_gate_lock:
            if is_talking and _user_running_talking_count(username) >= MAX_USER_RUNNING_TALKING:
                return  # 超运行闸→不启动，任务留 pending(worker finally 会移出 _queued_job_ids)，等口播完成事件/30s 扫描重排
            if is_image and _user_running_image_count(username) >= MAX_USER_RUNNING_IMAGE:
                return  # 单用户生图运行闸：多的留 pending 排队
            if is_breakdown and _global_running_breakdown_count() >= MAX_GLOBAL_RUNNING_BREAKDOWN:
                return  # 爆款拆解全局运行闸：多的留 pending 排队
            if not jobs_store.claim_running(jdb, job_id):
                return  # CAS 认领失败：已被别的 worker 接管或已是终态
        # 抢到 running 才开心跳（前面几个 return 都还没认领，不该有心跳）。
        # 有了它，reaper 的「没心跳」才真的等于「worker 死了」—— 而不是「正在轮询/烧字幕」。
        stop_heartbeat = _start_job_heartbeat(job_id)
        if kind in {"audio", "video", "tryon", "xiaole_video", "leads", "cinematic", "avatar", "breakdown"}:
            payload["_username"] = username   # 少一个 kind，handler 就拿不到用户名/job_id：
            payload["_job_id"] = job_id       # gen_avatar 记不了形象归属，gen_cinematic 查不到用户的形象
        result = HANDLERS[kind](payload)
        # 先 CAS 抢 done 终态：仅当仍是 running 才写 done，防 reaper 已判 error 又被无条件覆盖(既出片又退点)
        if not _set_terminal(job_id, "done", result=result):
            return  # 已被 reaper 接管为 error+退点：放弃成功副作用(不入库、不覆盖状态)
        # 口播按成片真实时长结算：预扣(cost)是 hold，跑完多退少不补。只在抢到 done 后调 —— done CAS
        # 互斥 + reaper/reclaim 不碰 done → 每 job 至多结算一次，不重复退。结算失败不影响出片。
        if kind == "video":
            try:
                actual = _domains()[2].talking_actual_cost(result)
                if actual and int(cost or 0) > actual:
                    _domains()[1].safe_refund_points(username, int(cost) - actual, "job#%d 口播结算" % job_id)
            except Exception:
                pass
        # 已确认拿到 done 终态；入库是次要副作用，失败也不改状态、不退点
        try:
            audio_domain, _, video_domain = _domains()
            if kind == "audio":
                audio_domain.record_audio_asset(job_id, username, result)
            if kind in {"video", "tryon", "xiaole_video", "cinematic"}:
                video_domain.record_video_asset(job_id, username, result)
            assets_store.record_asset(job_id, username, kind, result)  # 只有 copy 会入统一 assets 表；其余 kind 内部忽略
        except Exception:
            pass
    except Exception as e:
        # 生成失败：CAS 抢 error 终态；抢到才记失败资产。退点走幂等(reaper 若已退则跳过)
        # from_states 含 pending：抢 running 那句自己抛异常时任务还停在 pending，只认 running 会不退点
        claimed = _set_terminal(job_id, "error", error=str(e), from_states=("pending", "running"))
        if claimed:
            _mark_video_asset_failed(job_id, kind, e)
        _refund_once(job_id, username, cost)  # 幂等：最多退一次
    finally:
        if stop_heartbeat:
            stop_heartbeat()   # ⚠️ 必须停 —— 否则每跑一个任务泄漏一个线程，而且它会一直把已终态的任务刷成「活着」
        if is_talking or is_image or is_breakdown:
            try:
                _recover_pending_jobs()  # 口播/生图/拆解跑完→腾出运行槽，立刻重排排队中的同类(+30s 扫描兜底)
            except Exception:
                pass

# ============ 超时清道夫：running 超 6 分钟的僵尸任务自动判失败 + 退点 ============
def reaper():
    while True:
        try:
            now = int(time.time()); cutoff = now - 360
            with closing(jdb()) as c:
                stuck = c.execute("SELECT id, username, cost, kind, payload, updated_at FROM jobs WHERE status='running' AND updated_at < ?", (cutoff,)).fetchall()
            for r in stuck:
                # ⚠️ 默认值【不能是 0】—— grace=0 会走到下面的 `if grace and ...` 判假，
                # 直接按 360s 的 cutoff 把任务杀掉。也就是说：一个新 kind 忘了在 KIND_GRACE 里
                # 登记，它就只有 6 分钟寿命。（audio/copy/leads/dl 至今都不在表里，只是它们跑得快，
                # 够不着 6 分钟才没出事 —— 这是个潜伏雷。）
                grace = KIND_GRACE.get(r["kind"], KIND_GRACE_DEFAULT)
                if r["kind"] == "video":
                    # 口播/动作模仿统一到 VIDEO_REAPER_GRACE。原「motion 40 分钟/口播 9 分钟」两套数：
                    # motion 40 分钟是当年必回退泽龙(20~37 分钟)时定的，去线路化走 WaveSpeed 后已不需要；
                    # 口播 9 分钟又比中转轮询死线(1200s)还短、会先杀。
                    grace = VIDEO_REAPER_GRACE
                if grace and r["updated_at"] >= now - grace:
                    continue
                # CAS 抢 error 终态；抢到(说明 worker 尚未写 done)才退点，退点本身再幂等一层
                if _set_terminal(r["id"], "error", error="生成超时自动结束，退款处理中"):
                    _refund_once(r["id"], r["username"], r["cost"])
                    _mark_video_asset_failed(r["id"], r["kind"], "生成超时自动结束，退款处理中")
        except Exception:
            pass
        time.sleep(60)

def _requeue_running_job(job_id):
    return startup_recovery.requeue_running_job(jdb, job_id)


def reclaim_orphaned_running():
    return startup_recovery.reclaim_orphaned_running(
        jdb=jdb,
        service_owner=SERVICE_OWNER,
        domains=_domains,
        set_terminal=_set_terminal,
        refund_once=_refund_once,
        mark_video_asset_failed=_mark_video_asset_failed,
        requeue_job=_requeue_running_job,
    )


# ============ HTTP ============
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _method_not_allowed(self):
        b = json.dumps({"detail": "Method Not Allowed"}, ensure_ascii=False).encode()
        self.send_response(405); self.send_header("Allow", "POST")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _token(self):
        return _request_token(self.headers)
    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception: return {}
    def _json_body_strict(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) or b"{}"
            return json.loads(raw)
        except Exception:
            raise ValueError("请求体不是合法 JSON")

    def do_POST(self):
        p = self.path.split("?")[0]
        audio_domain, points_domain, video_domain = _domains()
        if p == "/api/gen/asset/favorite":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            body = self._json_body()
            try:
                mark = _upsert_asset_mark(user["username"], body.get("kind"), body.get("key"), favorite=bool(body.get("favorite")))
                return self._send(200, {"ok": True, "mark": mark})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p == "/api/gen/asset/tags":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            body = self._json_body()
            try:
                mark = _upsert_asset_mark(user["username"], body.get("kind"), body.get("key"), tags=body.get("tags"))
                return self._send(200, {"ok": True, "mark": mark})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p == "/api/gen/asset/delete":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            body = self._json_body()
            try:
                deleted = delete_user_asset(user["username"], body.get("kind"), body.get("id"))
                return self._send(200, {"ok": True, "asset": deleted})
            except LookupError as e:
                return self._send(404, {"detail": str(e)[:160]})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p == "/api/gen/asset/batch-delete":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            try:
                result = asset_batch.batch_delete_user_assets(sys.modules[__name__], user["username"], self._json_body())
                return self._send(200, {"ok": True, **result})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p == "/api/gen/asset/batch-download":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            try:
                data, meta = asset_batch.build_asset_zip(sys.modules[__name__], user["username"], self._json_body())
            except LookupError as e:
                return self._send(404, {"detail": str(e)[:160]})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
            name = "huangque-assets-%s.zip" % time.strftime("%Y%m%d-%H%M%S")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (name, urllib.parse.quote(name)))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Asset-Count", str(meta.get("count", 0)))
            self.send_header("X-Asset-Skipped", str(meta.get("skipped", 0)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/api/gen/audio/buy-slot":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            if _must_change_password(user): return self._send(403, {"detail": "请先修改初始密码"})
            try:
                feature_flags.require_enabled("audio")
                slot = audio_domain.purchase_audio_voice_slot(user["username"])
                return self._send(200, {"ok": True, "slot": slot, "cost": slot["cost"], "points_left": slot["points_left"]})
            except feature_flags.FeatureDisabled as e: return self._send(503, {"detail": str(e)})
            except audio_domain.VoiceSlotError as e: return self._send(e.status, {"detail": str(e)})
            except points_domain.AuthPointsError as e:
                return self._send(402 if e.status == 402 else 502, {"detail": e.detail, "need": audio_domain.VOICE_SLOT_COST})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p == "/api/gen/audio/redeem-slot":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "\u672a\u767b\u5f55"})
            return self._send(410, {"detail": "音色槽位已改为点数购买，请使用购买入口"})
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
            if not user: return self._send(401, {"detail": "未登录"})
            if _must_change_password(user): return self._send(403, {"detail": "请先修改初始密码"})
            try:
                feature_flags.require_enabled("audio")
            except feature_flags.FeatureDisabled as e:
                return self._send(503, {"detail": str(e)})
            try:
                body = self._json_body_strict()
                body = audio_domain.validate_clone_vip_payload(user["username"], body)
                voice = audio_domain.mark_clone_training(user["username"], body.get("slot_id"), body.get("name"))
                threading.Thread(target=audio_domain.clone_vip_voice_background, args=(user["username"], body), daemon=True).start()
                return self._send(200, {"ok": True, "voice": voice})
            except audio_domain.CloneVipValidationError as e:
                return self._send(e.status, {"detail": e.detail})
            except ValueError as e:
                return self._send(400, {"detail": str(e)[:220]})
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
        if p == "/api/gen/leads/crm":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            try:
                crm = _leads_domain().upsert_crm(user["username"], self._json_body())
                return self._send(200, {"ok": True, "crm": crm})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p == "/api/gen/video/batch":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录或登录已过期"})
            if _must_change_password(user): return self._send(403, {"detail": "请先修改初始密码"})
            try:
                feature_flags.require_enabled("video")
                request_body = self._json_body_strict()
                payloads = video_domain.validate_video_batch_payload(
                    request_body, user["username"], min(video_domain.VIDEO_BATCH_MAX, MAX_USER_ACTIVE_JOBS))
                idem_key = _idempotency_key(self.headers.get("Idempotency-Key"))
            except feature_flags.FeatureDisabled as e:
                return self._send(503, {"detail": str(e)})
            except ValueError as e:
                return self._send(400, {"detail": str(e)[:220]})
            costs = [points_domain.cost_of("video", body) for body in payloads]
            total = sum(costs)
            with _submission_lock:
                idem_state, idem_response = _idempotency_begin(user["username"], p, idem_key, request_body)
                if idem_state == "replay":
                    return self._send(200, idem_response)
                if idem_state == "conflict":
                    return self._send(409, {"detail": "同一个 Idempotency-Key 不能用于不同请求", "code": "idempotency_conflict"})
                if idem_state == "processing":
                    return self._send(409, {"detail": "相同请求正在受理，请稍后查询", "code": "idempotency_in_progress", "retry_after_ms": 1000})
                active_jobs = _user_active_job_count(user["username"])
                if active_jobs + len(payloads) > MAX_USER_ACTIVE_JOBS:
                    _idempotency_abort(user["username"], p, idem_key)
                    return self._send(429, {"detail": "当前仅剩 %d 个任务位，无法提交 %d 条批量视频" %
                        (max(0, MAX_USER_ACTIVE_JOBS - active_jobs), len(payloads)), "active_jobs": active_jobs,
                        "available_slots": max(0, MAX_USER_ACTIVE_JOBS - active_jobs), "requested": len(payloads)})
                batch_id = uuid.uuid4().hex
                for body in payloads:
                    body["batch_id"] = batch_id
                job_ids = []
                try:
                    job_ids, points_left = jobs_store.create_paid_jobs(
                        jdb, points_domain.deduct_points, points_domain.refund_points, "video",
                        user["username"], zip(costs, payloads), SERVICE_OWNER, "video_batch")
                    for jid, body in zip(job_ids, payloads):
                        video_domain.record_video_pending_asset(jid, user["username"], body)
                except points_domain.AuthPointsError as e:
                    _idempotency_abort(user["username"], p, idem_key)
                    return self._send(402 if e.status == 402 else 502, {"detail": e.detail, "need": total})
                except jobs_store.PaidJobInsertError as e:
                    _idempotency_abort(user["username"], p, idem_key)
                    return self._send(500, {"detail": {"refunded": "批量任务创建失败，点数已退回",
                        "queued": "批量任务创建失败，退款正在自动重试"}.get(e.compensation,
                        "批量任务创建失败，退款需人工核对"), "submission_ref": e.submission_ref})
                except Exception:
                    for jid, cost in zip(job_ids, costs):
                        _reject_pending_job(jid, user["username"], cost, "批量任务创建失败")
                        try:
                            video_domain.update_video_asset_phase(jid, "failed", status="failed", error="批量任务创建失败")
                        except Exception:
                            pass
                    _idempotency_abort(user["username"], p, idem_key)
                    return self._send(500, {"detail": "批量任务创建失败，退款正在自动处理"})
                if not enqueue_jobs(job_ids, "video", "text"):
                    for jid, cost in zip(job_ids, costs):
                        _reject_pending_job(jid, user["username"], cost, "任务队列已满，请稍后再试")
                        video_domain.update_video_asset_phase(jid, "failed", status="failed", error="任务队列已满，请稍后再试")
                    _idempotency_abort(user["username"], p, idem_key)
                    return self._send(429, {"detail": "任务队列已满，批量任务未受理，退款正在处理", "need": total})
            jobs = [{"job_id": jid, "label": body.get("batch_label"), "index": body.get("batch_index")}
                    for jid, body in zip(job_ids, payloads)]
            response = {"batch_id": batch_id, "job_ids": job_ids, "jobs": jobs,
                        "count": len(job_ids), "cost": total, "cost_per_job": costs[0], "points_left": points_left}
            _idempotency_complete(user["username"], p, idem_key, response)
            return self._send(200, response)
        if p.startswith("/api/gen/") and p[9:] in HANDLERS:
            kind = p[9:]
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录或登录已过期"})
            if _must_change_password(user): return self._send(403, {"detail": "请先修改初始密码"})
            try:
                feature_flags.require_enabled(kind)
            except feature_flags.FeatureDisabled as e:
                return self._send(503, {"detail": str(e)})
            try:
                body = self._json_body_strict() if kind in {"video", "tryon", "cinematic", "avatar"} else self._json_body()
                request_body = dict(body) if isinstance(body, dict) else body
                # 微信小程序内容安全：必须在校验、扣点和入队之前完成。违规内容不扣点；
                # 微信服务异常时不收单，避免网络故障成为绕过审核的通道。
                miniprogram_security.check_payload(body)
                if kind == "video":
                    body = video_domain.validate_video_payload(body, user["username"])
                elif kind == "tryon":
                    body = video_domain.validate_tryon_payload(body)
                elif kind == "cinematic":
                    body = video_domain.validate_cinematic_payload(body, user["username"])
                elif kind == "avatar":
                    body = video_domain.validate_avatar_payload(body)
                elif kind == "xiaole_video":
                    body = video_domain.validate_xiaole_video_payload(body)
                elif kind == "image":
                    from . import image as image_domain
                    body = image_domain.validate_image_payload(body)
                # cinematic 也纳入：它提交即扣 $7，是最该防重复提交的一档（同一单任务路径，无额外风险）
                idem_key = _idempotency_key(self.headers.get("Idempotency-Key")) if kind in {"video", "tryon", "xiaole_video", "cinematic"} else ""
            except miniprogram_security.ContentRejected as e:
                return self._send(400, {"detail": str(e), "code": "content_rejected"})
            except miniprogram_security.SecurityUnavailable as e:
                return self._send(503, {"detail": str(e), "code": "content_security_unavailable", "retry_after_ms": 5000})
            except ValueError as e:
                return self._send(400, {"detail": str(e)[:220]})
            # 正在停机（部署中）→ 不收新活。⚠️ 必须在【扣点之前】。
            # 否则用户被扣了点、任务入了队，进程下一秒就退了 —— 又是一条「服务重启中断」。
            if is_shutting_down():
                return self._send(503, {"detail": "服务正在更新，请稍等几秒后重试（未扣点）",
                                        "code": "shutting_down", "retry_after_ms": 5000})

            # 上游没额度就当场拒 —— ⚠️ 必须在【扣点之前】。
            # 余额哨兵每 10 分钟告警一次，但告警只叫醒我们、拦不住用户：从「余额见底」到
            # 「有人充上钱」这段时间里，用户照样点生成、照样被扣点、照样等几分钟，然后看到
            # 一句天书（"积分余额不足，请先充值" / "Insufficient credits"）。近 14 天 48 条
            # 任务是这么死的。这里把它们挡在门外：不扣点、不排队、不让用户等。
            # fail-open：熔断器自己出问题一律放行（见 upstream_guard）。
            from . import upstream_guard
            blocked = upstream_guard.exhausted_reason(kind, body)
            if blocked:
                return self._send(503, {"detail": blocked, "code": "upstream_exhausted",
                                        "retry_after_ms": 60000})
            cost = points_domain.cost_of(kind, body)
            with _submission_lock:
                idem_state, idem_response = _idempotency_begin(user["username"], p, idem_key, request_body)
                if idem_state == "replay":
                    return self._send(200, idem_response)
                if idem_state == "conflict":
                    return self._send(409, {"detail": "同一个 Idempotency-Key 不能用于不同请求", "code": "idempotency_conflict"})
                if idem_state == "processing":
                    return self._send(409, {"detail": "相同请求正在受理，请稍后查询", "code": "idempotency_in_progress", "retry_after_ms": 1000})
                limit_hit = _user_video_submit_limit(kind, body, user["username"], cost)
                if limit_hit:
                    _idempotency_abort(user["username"], p, idem_key)
                    return self._send(429, limit_hit)
                active_jobs = _user_active_job_count(user["username"])
                if active_jobs >= MAX_USER_ACTIVE_JOBS:
                    _idempotency_abort(user["username"], p, idem_key)
                    return self._send(429, {"detail": "您有 %d 个任务正在排队/生成，完成后再提交" % active_jobs,
                        "code": "active_job_cap", "active_jobs": active_jobs, "max_active_jobs": MAX_USER_ACTIVE_JOBS,
                        "retry_after_ms": 4000, "need": cost})
                try:
                    jid, points_left = jobs_store.create_paid_job(
                        jdb, points_domain.deduct_points, points_domain.refund_points,
                        kind, user["username"], cost, body, SERVICE_OWNER)
                except points_domain.AuthPointsError as e:
                    _idempotency_abort(user["username"], p, idem_key)
                    return self._send(402 if e.status == 402 else 502, {"detail": e.detail, "need": cost})
                except jobs_store.PaidJobInsertError as e:
                    _idempotency_abort(user["username"], p, idem_key)
                    return self._send(500, {"detail": {"refunded": "任务创建失败，点数已退回",
                        "queued": "任务创建失败，退款正在自动重试"}.get(e.compensation, "任务创建失败，退款需人工核对"),
                        "submission_ref": e.submission_ref})
                if kind in {"video", "tryon", "xiaole_video", "cinematic"}:
                    try: video_domain.record_video_pending_asset(jid, user["username"], body)
                    except Exception:
                        _reject_pending_job(jid, user["username"], cost, "视频资产登记失败"); _idempotency_abort(user["username"], p, idem_key)
                        return self._send(500, {"detail": "任务创建失败，退款正在自动处理", "job_id": jid})
                if not enqueue_job(jid, kind, body.get("mode")):
                    _reject_pending_job(jid, user["username"], cost, "任务队列已满，请稍后再试")
                    if kind in {"video", "tryon", "xiaole_video", "cinematic"}:
                        video_domain.update_video_asset_phase(jid, "failed", status="failed", error="任务队列已满，请稍后再试")
                    _idempotency_abort(user["username"], p, idem_key)
                    return self._send(429, {"detail": "任务队列已满，请稍后再试", "code": "queue_full", "retry_after_ms": 4000, "need": cost})
            response = {"job_id": jid, "cost": cost, "points_left": points_left}
            _idempotency_complete(user["username"], p, idem_key, response)
            return self._send(200, response)
        self._send(404, {"detail": "not found"})

    def do_GET(self):
        p = self.path.split("?")[0]
        audio_domain, points_domain, video_domain = _domains()
        if p == "/api/gen/audio/clone-vip":
            return self._method_not_allowed()
        if p == "/api/gen/asset/marks":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                marks = _list_asset_marks(user["username"], (q.get("kind") or ["image"])[0])
                return self._send(200, {"items": marks})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p == "/api/gen/assets":   # 统一资产表：image/copy/collect/leads，按 kind / stage 过滤
            if not (user := verify(self._token())): return self._send(401, {"detail": "未登录"})
            return self._send(*assets_store.list_assets_response(user["username"], urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)))
        if p == "/api/gen/leads/crm":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            lead_ids = []
            for raw in q.get("lead_id") or q.get("ids") or []:
                lead_ids.extend([x.strip() for x in str(raw).split(",") if x.strip()])
            try:
                return self._send(200, {"items": _leads_domain().list_crm(user["username"], lead_ids)})
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p.startswith("/api/gen/job/"):
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            try: jid = int(p.rsplit("/", 1)[1])
            except Exception: return self._send(400, {"detail": "bad id"})
            with closing(jdb()) as c:
                r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r: return self._send(404, {"detail": "任务不存在"})
            if r["username"] != user.get("username"):
                return self._send(404, {"detail": "任务不存在"})
            phase = video_domain.get_video_job_phase(jid) if r["kind"] in {"video", "tryon", "xiaole_video", "cinematic"} else None
            if phase is None and r["kind"] == "breakdown":
                try:
                    phase = (json.loads(r["payload"] or "{}") or {}).get("phase")
                except Exception:
                    pass
            d = _job_public_dict(r, phase)
            return self._send(200, d)
        if p == "/api/gen/points/history":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                data = points_domain.history(
                    user["username"],
                    (q.get("days") or ["30"])[0],
                    (q.get("kind") or [""])[0],
                    (q.get("page") or ["1"])[0],
                    (q.get("page_size") or ["20"])[0],
                )
                data["points"] = points_domain.get_points(user["username"])
                return self._send(200, data)
            except Exception as e:
                return self._send(400, {"detail": str(e)[:160]})
        if p == "/api/gen/dl":   # 无水印视频下载代理：直连拉 CDN → 附件流回(强制下载)
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            try:
                feature_flags.require_enabled("dl")
            except feature_flags.FeatureDisabled as e:
                return self._send(503, {"detail": str(e)})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            url = (q.get("url", [""])[0]).strip()
            raw_name = ((q.get("name", ["video"])[0])[:40]) or "video"
            ascii_name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", raw_name).strip("_") or "video"  # header 必须 ASCII
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            ALLOW = (
                ".zjcdn.com", ".douyinvod.com", ".douyinstatic.com", ".douyinpic.com", ".amemv.com",
                ".bytecdn.cn", ".ixigua.com", ".pstatp.com", ".snssdk.com", ".byteimg.com",
                ".xhscdn.com", ".rednotecdn.com", ".xiaohongshu.com",
                ".bytedance.net", ".lf-douyin.com", ".365yg.com",
                ".cos.ap-guangzhou.myqcloud.com",  # 采集视频转存 COS 后的直链下载(COS-COLLECT #113)
                "video.huangquechuanmei.com",  # 自有 COS 加速域名：生成图片、音视频资产下载
            )  # 抖音(TikHub play_addr)/小红书 等直链 CDN；防 SSRF。覆盖 collect 解析视频下载。
            if not (url.startswith("http") and any(host == h or (h.startswith(".") and host.endswith(h)) for h in ALLOW)):
                return self._send(400, {"detail": "不支持的下载地址"})
            try:
                req = urllib.request.Request(url, headers={"User-Agent": tikhub.UA})
                up = tikhub._OPENER.open(req, timeout=120)  # 直连，绕过环境代理
            except Exception as e:
                return self._send(502, {"detail": "下载失败:" + str(e)[:80]})
            ctype, ext = _download_content_type_ext(up.headers)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition",
                             "attachment; filename=\"%s%s\"; filename*=UTF-8''%s" % (ascii_name, ext, urllib.parse.quote(raw_name + ext)))
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
            try:
                canonical_rel = fp.resolve().relative_to(OUT_DIR.resolve()).as_posix()
            except Exception:
                return self._send(404, {"detail": "no file"})
            sensitive = _sensitive_output_file(canonical_rel)
            if sensitive:
                user = verify(self._token())
                if not user: return self._send(401, {"detail": "未登录"})
                if not _user_owns_output_file(user.get("username"), canonical_rel):
                    return self._send(404, {"detail": "no file"})
            fn = fp.name
            data = fp.read_bytes()
            ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
            self.send_response(200); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if sensitive or fn.startswith("voice_preview_"):
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
            items = audio_domain.list_user_audio_voice_slots(user["username"])
            return self._send(200, {"items": items,
                "slot_count": sum(1 for item in items if item.get("status") in audio_domain.VALID_VOICE_SLOT_STATUSES),
                "slot_max": audio_domain.VOICE_SLOT_MAX_PER_USER, "slot_cost": audio_domain.VOICE_SLOT_COST,
                "points": user.get("points")})
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
                _ensure_column(c, "jobs", "deleted", "INTEGER DEFAULT 0")
                rows = c.execute("""SELECT id,result,created_at FROM jobs
                                 WHERE username=? AND status='done' AND kind=? AND COALESCE(deleted,0)=0
                                 ORDER BY id DESC LIMIT ?""",
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
            if _must_change_password(user): return self._send(403, {"detail": "请先修改初始密码"})
            try:
                feature_flags.require_enabled("collect")
            except feature_flags.FeatureDisabled as e:
                return self._send(503, {"detail": str(e)})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            platform = (q.get("platform", ["douyin"])[0]).strip()
            keyword  = (q.get("keyword", [""])[0]).strip()
            try: page = int(q.get("page", ["1"])[0] or 1)
            except Exception: page = 1
            if not keyword: return self._send(400, {"detail": "缺少关键词"})
            try:
                points_left = points_domain.deduct_points(user["username"], 1, "search:" + platform)
            except points_domain.AuthPointsError as e:
                code = 402 if e.status == 402 else 502
                return self._send(code, {"detail": e.detail, "need": 1})
            try:
                r = tikhub.search(platform, keyword, page=page, video_only=False)  # 含图文
            except tikhub.TikHubError as e:
                points_domain.safe_refund_points(user["username"], 1, "search:" + platform + ":refund")
                return self._send(502, {"detail": str(e)[:160]})
            items = [{"id": it.get("id"), "platform": it.get("platform"), "title": it.get("title"),
                      "cover": it.get("cover"), "author": it.get("author"), "url": it.get("url"),
                      "note_type": it.get("note_type"),
                      "stats": {"like": it.get("like"), "comment": it.get("comment")}} for it in (r.get("items") or [])]
            return self._send(200, {"items": items, "cost": 1, "points_left": points_left})
        if p == "/api/gen/health":
            return self._send(200, {"ok": True, "service": "huangque-content", "caps": list(HANDLERS), "job_workers": JOB_WORKERS, "fast_job_workers": FAST_JOB_WORKERS, "talking_job_workers": TALKING_JOB_WORKERS, "image_job_workers": IMAGE_JOB_WORKERS,
                                    "max_user_active_jobs": MAX_USER_ACTIVE_JOBS, "max_user_active_xiaole_video": MAX_USER_ACTIVE_XIAOLE_VIDEO, "max_user_active_tryon": MAX_USER_ACTIVE_TRYON, "max_user_active_cinematic": MAX_USER_ACTIVE_CINEMATIC,
                                    "max_user_running_talking": MAX_USER_RUNNING_TALKING, "max_user_running_image": MAX_USER_RUNNING_IMAGE, "video_cost": VIDEO_COST, "video_batch_max": min(video_domain.VIDEO_BATCH_MAX, MAX_USER_ACTIVE_JOBS), "has_openai": bool(OPENAI_KEY), "has_tikhub": bool(tikhub.KEY), "tikhub_base": tikhub.BASE})
        self._send(404, {"detail": "not found"})

    def do_PUT(self):
        if self.path.split("?")[0] == "/api/gen/audio/clone-vip": return self._method_not_allowed()
        self._send(404, {"detail": "not found"})
    def do_PATCH(self):
        if self.path.split("?")[0] == "/api/gen/audio/clone-vip": return self._method_not_allowed()
        self._send(404, {"detail": "not found"})
    def do_DELETE(self):
        if self.path.split("?")[0] == "/api/gen/audio/clone-vip": return self._method_not_allowed()
        self._send(404, {"detail": "not found"})
if __name__ == "__main__":
    init_db(); reclaim_orphaned_running()  # 回收上次重启遗留的 running 孤儿→秒退点
    start_job_workers(); threading.Thread(target=reaper, daemon=True).start()
    print("huangque-content-api on 127.0.0.1:%d  caps=%s" % (PORT, list(HANDLERS))); ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
