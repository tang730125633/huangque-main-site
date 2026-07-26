# -*- coding: utf-8 -*-
"""统一资产表 assets —— 让每个模块的产物都稳定入库、可分类、可收藏。

背景：产物入库此前只覆盖 audio(audio_assets) 和 video/tryon/xiaole_video(video_assets)。
image 是半入库（无表，靠 jobs + asset_marks 以 job_id 挂靠），
copy/collect/leads 完全没有资产表——产物只躺在 jobs.result 里，资产库页面不展示，
连收藏/打标签都做不到(ASSET_MARK_KINDS 只认 image/audio/video/avatar)。

本模块只负责新增的 assets 表，不动 audio_assets / video_assets / avatars——
那三张表被 video.html / audio.html / _user_owns_output_file 直接依赖，改结构会波及一片。

stage 维度落地 DESIGN.md 的「素材 / 作品 / 交付分开」原则：
  material 素材：采集来的别人的内容，给 AI 和运营当输入
  work     作品：我们生成的东西，给运营筛
  delivery 交付：给客户看的成果（客户名单等）

三个服务共用本模块：content_api(image/copy)、leadgen_api(collect/leads)、imggen_api(image)。
写入是「次要副作用」——任何异常都不该影响任务状态或点数，调用方一律 try/except 包住。
"""
import json
import os
import pathlib
import sqlite3
import time
import urllib.parse
from contextlib import closing

BASE = pathlib.Path(__file__).resolve().parents[1]
ASSET_DB = os.environ.get("CONTENT_ASSET_DB", str(BASE / "audio_assets.db"))

MATERIAL, WORK, DELIVERY = "material", "work", "delivery"
STAGES = {MATERIAL, WORK, DELIVERY}

# 每个 kind 的默认 stage。收藏/标签仍复用 asset_marks，asset_key 用 str(job_id)。
#
# 为什么没有 image：作图产物早就有自己的展示链路（jobs.result → /api/gen/history），
# assets.html 的图片分类读的是那条。曾经这里也写 image，但写进来的行没有任何读路径 ——
# 纯粹的死写：每次出图多一次 SQLite 写入，soft_delete 对它生效却影响不到 UI 看到的数据。
# 要把 image 也收进来，得先让前端图片分类改读 /api/gen/assets 并把 meta 摊平，
# 那是一次独立的迁移，不该顺手夹带。
KIND_STAGE = {"copy": WORK, "collect": MATERIAL, "leads": DELIVERY, "breakdown": WORK}
KINDS = set(KIND_STAGE)

_initialized = False


def adb():
    c = sqlite3.connect(ASSET_DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_assets():
    """建表（幂等）。三个服务谁先起来谁建，不依赖 content_api 的 init_audio_db。"""
    global _initialized
    with closing(adb()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS assets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            kind TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'work',
            username TEXT NOT NULL,
            title TEXT,
            file TEXT,
            url TEXT,
            meta TEXT,
            created_at INTEGER,
            deleted INTEGER NOT NULL DEFAULT 0,
            UNIQUE(kind, job_id)
        )""")
        # 资产库按 (用户, 类型) 倒序翻页，未删的在前
        c.execute("""CREATE INDEX IF NOT EXISTS idx_assets_user_kind
                     ON assets(username, kind, deleted, created_at DESC)""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_assets_user_stage
                     ON assets(username, stage, deleted, created_at DESC)""")
        c.commit()
    _initialized = True


def _ensure():
    if not _initialized:
        init_assets()


def _clip(text, n=120):
    s = str(text or "").strip().replace("\n", " ")
    return s[:n] if s else None


def _permanent_hosts():
    """自有存储域名。每次读 env，不做模块级快照——COS_DOMAIN 可能在进程起来后才注入。"""
    custom = urllib.parse.urlparse(os.environ.get("COS_DOMAIN", "").strip().rstrip("/")).hostname \
        or os.environ.get("COS_DOMAIN", "").strip().rstrip("/").split("//")[-1].split("/")[0]
    return tuple(h.lower() for h in (custom, "myqcloud.com") if h)


def _is_permanent_url(url):
    """链接是否长期有效。

    两个坑：
    1. 私有桶(COS_PUBLIC=0)时 cos.py 返回的是 get_presigned_url(...) 签名链接，默认 7 天到期，
       但 host 同样是 *.myqcloud.com —— 只看域名会把它误判成永久。签名链接必带 query
       (q-sign-algorithm / Signature / Expires)，据此排除。
    2. 域名必须做后缀匹配。原来用子串包含，notmyqcloud.com.evil.net 会被判成永久。
    """
    parsed = urllib.parse.urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if not any(host == h or host.endswith("." + h) for h in _permanent_hosts()):
        return False
    q = parsed.query.lower()
    return not any(k in q for k in ("q-sign-algorithm", "signature", "expires", "sign="))


def _copy_body(result):
    text = str((result or {}).get("text") or "").strip()
    if text:
        return text
    blocks = []
    for index, scene in enumerate((result or {}).get("scenes") or [], 1):
        if not isinstance(scene, dict):
            continue
        duration = str(scene.get("dur") or "").strip()
        visual = str(scene.get("scene") or "").strip()
        line = str(scene.get("line") or "").strip()
        if not (duration or visual or line):
            continue
        blocks.append("镜号%02d（%s）\n画面：%s\n口播：%s" %
                      (index, duration, visual, line))
    return "\n\n".join(blocks)


_LEAD_META_FIELDS = (
    "lead_id", "nickname", "user_unique_id", "ip_location", "content",
    "title", "platform", "profile_url", "video_url", "red_id", "intent",
    "intent_score", "intent_reason", "follow_status", "follow_note",
)


def _lead_details(items):
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        out.append({key: item.get(key) for key in _LEAD_META_FIELDS
                    if item.get(key) not in (None, "")})
    return out


def _project_breakdown_item(result):
    r = result or {}
    return {
        "type": r.get("type") or "breakdown",
        "source_type": r.get("source_type"),
        "source_url": r.get("source_url"),
        "source_title": r.get("source_title"),
        "source_platform": r.get("source_platform"),
        "duration": r.get("duration"),
        "sections": r.get("sections"),
        "scenes": r.get("scenes"),
        "analysis": r.get("analysis"),
        "prompt": r.get("prompt"),
        "frame_thumbnails": r.get("frame_thumbnails") or [],
    }



def _project(kind, result):
    """把各 kind 形状各异的 result 投影成 (title, file, url, meta)。

    collect 评论仍只留计数；copy 正文和 leads 查看字段需要在资产库直接展示，
    因此保留经过投影的内容。历史记录由前端按 job_id 回源 jobs.result。
    """
    r = result or {}
    if kind == "copy":
        return (_clip(r.get("prompt")), None, None, {
            "ctype": r.get("ctype"), "mode": r.get("mode"), "platform": r.get("platform"),
            "style": r.get("style"), "dur": r.get("dur"),
            "text": r.get("text"), "scenes": r.get("scenes"), "body": _copy_body(r),
        })
    if kind == "collect":
        video = r.get("video") or {}
        copy = r.get("copy") or {}
        transcript = r.get("transcript") or {}
        play = video.get("play_url") or r.get("url")
        return (_clip(video.get("title") or copy.get("title")),
                None,
                play,   # 优先可播放直链，回退封面
                {
                    # COS 转存失败时 leadgen 会静默回退第三方 CDN 直链，那种链接会过期。
                    # 标出来，前端才能提示用户「趁早下载」，而不是几天后点开发现是死链。
                    "permanent": _is_permanent_url(play),
                    "platform": r.get("platform"), "source": r.get("source"),
                    "author": video.get("author"), "profile_url": video.get("profile_url"),
                    "cover": video.get("cover"), "duration": video.get("duration"),
                    "publish_time": video.get("publish_time"), "stats": video.get("stats"),
                    "desc": copy.get("desc"), "tags": copy.get("tags"),
                    "images": r.get("images") or [],
                    "has_transcript": bool(transcript.get("text") if isinstance(transcript, dict) else transcript),
                    "comments_count": len(r.get("comments") or []),
                })
    if kind == "leads":
        return (_clip(r.get("keyword")), None, None, {
            "keyword": r.get("keyword"), "platforms": r.get("platforms"),
            "leads_count": r.get("leads_count"), "spam": r.get("spam"),
            "chat": r.get("chat"), "total": r.get("total"),
            "leads": _lead_details(r.get("leads")),
        })
    if kind == "breakdown":
        if r.get("type") == "breakdown_batch":
            results = [_project_breakdown_item(item) for item in (r.get("results") or []) if isinstance(item, dict)]
            first = results[0] if results else {}
            title = _clip((first or {}).get("source_title")) or "批量视频拆解"
            return (title, None, (first or {}).get("source_url"), {
                "type": "breakdown_batch",
                "results": results,
                "errors": r.get("errors") or [],
                "total": r.get("total"),
            })
        item = _project_breakdown_item(r)
        return (_clip(r.get("source_title")), None, r.get("source_url"), item)
    return (None, None, None, {})


def record_asset(job_id, username, kind, result, stage=None, created_at=None):
    """把一次成功生成的产物写进 assets 表。幂等：UNIQUE(kind, job_id) + INSERT OR IGNORE。

    只在 CAS 抢到 done 终态之后调用——否则会给 reaper 判死的任务留下资产。
    """
    if kind not in KINDS or not username:
        return False
    _ensure()
    stage = stage if stage in STAGES else KIND_STAGE[kind]
    title, file, url, meta = _project(kind, result)
    with closing(adb()) as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO assets(job_id, kind, stage, username, title, file, url, meta, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (job_id, kind, stage, username, title, file, url,
             json.dumps(meta, ensure_ascii=False), int(created_at or time.time())))
        c.commit()
        return cur.rowcount >= 1


def list_assets(username, kind=None, stage=None, limit=60, offset=0):
    """资产库读取。kind / stage 均可选，未删的按时间倒序。"""
    _ensure()
    sql = "SELECT * FROM assets WHERE username=? AND deleted=0"
    args = [username]
    if kind:
        sql += " AND kind=?"; args.append(kind)
    if stage:
        sql += " AND stage=?"; args.append(stage)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    args += [max(1, min(200, int(limit or 60))), max(0, int(offset or 0))]
    with closing(adb()) as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except Exception:
            d["meta"] = {}
        d["meta"] = _slim_meta_for_list(d["meta"])
        out.append(d)
    return out


def _slim_meta_for_list(meta):
    """列表视图瘦身：关键帧缩略图每条只保留 1 张（原是 ≤4 张 base64，批量资产 5×4 张，
    limit=60 时响应可到数 MB），同时给 frame_count 让前端知道原来有几帧。"""
    if not isinstance(meta, dict):
        return meta
    thumbs = meta.get("frame_thumbnails") or []
    if len(thumbs) > 1:
        meta = dict(meta)
        meta["frame_thumbnails"] = thumbs[:1]
        meta["frame_count"] = len(thumbs)
    results = meta.get("results")
    if isinstance(results, list):
        slimmed = []
        changed = False
        for b in results:
            if isinstance(b, dict) and len(b.get("frame_thumbnails") or []) > 1:
                orig = len(b.get("frame_thumbnails") or [])
                b = dict(b)
                b["frame_thumbnails"] = (b.get("frame_thumbnails") or [])[:1]
                b["frame_count"] = orig
                changed = True
            slimmed.append(b)
        if changed:
            meta = dict(meta)
            meta["results"] = slimmed
    return meta


def list_assets_response(username, query):
    """给 HTTP 层用：解析 query dict → (status_code, body)。

    校验和取值留在 domain 里，core.py 只负责鉴权 + 路由 —— tests/test_content_domains.py
    有条护栏断言 core.py 不超过 1300 行，逻辑不该往那儿堆。
    """
    kind = (query.get("kind") or [""])[0].strip() or None
    stage = (query.get("stage") or [""])[0].strip() or None
    if kind and kind not in KINDS:
        return 400, {"detail": "kind 仅支持: " + ", ".join(sorted(KINDS))}
    if stage and stage not in STAGES:
        return 400, {"detail": "stage 仅支持: " + ", ".join(sorted(STAGES))}
    try:
        limit = int((query.get("limit") or ["60"])[0])
        offset = int((query.get("offset") or ["0"])[0])
    except (TypeError, ValueError):
        return 400, {"detail": "limit / offset 必须是整数"}
    try:
        return 200, {"items": list_assets(username, kind=kind, stage=stage, limit=limit, offset=offset)}
    except Exception as e:
        return 400, {"detail": str(e)[:160]}


def soft_delete(username, asset_id):
    """软删除，与 audio_assets/video_assets 的 deleted 语义一致。"""
    _ensure()
    with closing(adb()) as c:
        cur = c.execute("UPDATE assets SET deleted=1 WHERE id=? AND username=? AND deleted=0",
                        (asset_id, username))
        c.commit()
        return cur.rowcount >= 1
