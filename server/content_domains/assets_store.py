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
from contextlib import closing

BASE = pathlib.Path(__file__).resolve().parents[1]
ASSET_DB = os.environ.get("CONTENT_ASSET_DB", str(BASE / "audio_assets.db"))

MATERIAL, WORK, DELIVERY = "material", "work", "delivery"
STAGES = {MATERIAL, WORK, DELIVERY}

# 每个 kind 的默认 stage。收藏/标签仍复用 asset_marks，asset_key 用 str(job_id)。
KIND_STAGE = {"image": WORK, "copy": WORK, "collect": MATERIAL, "leads": DELIVERY}
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


def _project(kind, result):
    """把各 kind 形状各异的 result 投影成 (title, file, url, meta)。

    meta 刻意不收大块数据：collect 的 comments、leads 的 leads 名单都在 jobs.result 里，
    往这儿复制一份只会让资产库跟着 jobs 表一起膨胀（jobs_gc 清的是 payload，不清 result）。
    """
    r = result or {}
    if kind == "image":
        return (_clip(r.get("prompt")), r.get("file"), r.get("url"), {
            "mode": r.get("mode"), "provider": r.get("provider"), "model": r.get("model"),
            "count": r.get("count"), "ratio": r.get("ratio"), "quality": r.get("quality"),
            "image_size": r.get("image_size"), "width": r.get("width"), "height": r.get("height"),
            "files": r.get("files"), "urls": r.get("urls"),
        })
    if kind == "copy":
        return (_clip(r.get("prompt")), None, None, {
            "ctype": r.get("ctype"), "mode": r.get("mode"), "platform": r.get("platform"),
            "style": r.get("style"), "dur": r.get("dur"),
            "text": r.get("text"), "scenes": r.get("scenes"),
        })
    if kind == "collect":
        video = r.get("video") or {}
        copy = r.get("copy") or {}
        transcript = r.get("transcript") or {}
        return (_clip(video.get("title") or copy.get("title")),
                None,
                video.get("play_url") or r.get("url"),   # 优先可播放直链，回退封面
                {
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
        })
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
        out.append(d)
    return out


def soft_delete(username, asset_id):
    """软删除，与 audio_assets/video_assets 的 deleted 语义一致。"""
    _ensure()
    with closing(adb()) as c:
        cur = c.execute("UPDATE assets SET deleted=1 WHERE id=? AND username=? AND deleted=0",
                        (asset_id, username))
        c.commit()
        return cur.rowcount >= 1
