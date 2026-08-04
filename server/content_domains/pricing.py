"""Shared, live point pricing for every billable Huangque capability."""

from contextlib import closing
import os
import pathlib
import sqlite3
import time


BASE = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = pathlib.Path(
    os.environ.get("PRICING_DB")
    or os.environ.get("FEATURE_FLAGS_DB")
    or str(BASE / "feature_flags.db")
)
_CACHE = {"loaded_at": 0, "items": {}}
_TTL = 5


def _rule(key, category, name, variant, points, unit, desc=""):
    return {
        "key": key,
        "category": category,
        "name": name,
        "variant": variant,
        "default_points": points,
        "unit": unit,
        "desc": desc,
    }


CATALOG = [
    _rule("invite.card_trial_reward", "用户权益", "名片有效邀请奖励", "每位新用户", 100, "点/人"),
    _rule("membership.experience.price_yuan", "用户权益", "体验官会员", "一年售价", 499, "元/年"),
    _rule("membership.experience.bonus_points", "用户权益", "体验官会员", "首购赠点", 1000, "点/次"),
    _rule("image.banana.nb2.std", "图片生成", "纳米香蕉 2", "标准", 18, "点/张"),
    _rule("image.banana.nb2.hd", "图片生成", "纳米香蕉 2", "高清", 35, "点/张"),
    _rule("image.banana.pro.std", "图片生成", "纳米香蕉 Pro", "标准", 35, "点/张"),
    _rule("image.banana.pro.hd", "图片生成", "纳米香蕉 Pro", "高清", 44, "点/张"),
    _rule("image.openai.std", "图片生成", "黄雀引擎 2", "标准", 20, "点/张"),
    _rule("image.openai.hd", "图片生成", "黄雀引擎 2", "高清", 35, "点/张"),
    _rule("image.seedream.std.std", "图片生成", "黄雀引擎 1", "标准版 · 标准", 8, "点/张"),
    _rule("image.seedream.std.hd", "图片生成", "黄雀引擎 1", "标准版 · 高清", 12, "点/张", "短剧关键帧固定生成 2 张"),
    _rule("image.seedream.pro.std", "图片生成", "黄雀引擎 1", "Pro · 标准", 15, "点/张"),
    _rule("image.seedream.pro.hd", "图片生成", "黄雀引擎 1", "Pro · 高清", 20, "点/张"),
    _rule("image.xiaole.std", "图片生成", "果肉生图", "标准", 12, "点/张"),
    _rule("image.xiaole.hd", "图片生成", "果肉生图", "高清", 16, "点/张"),
    _rule("image.zelong.std", "图片生成", "泽龙生图", "标准", 8, "点/张"),
    _rule("image.zelong.hd", "图片生成", "泽龙生图", "高清", 12, "点/张"),
    _rule("image.zelong2.std", "图片生成", "泽龙 2 生图", "标准", 8, "点/张"),
    _rule("image.zelong2.hd", "图片生成", "泽龙 2 生图", "高清", 12, "点/张"),
    _rule("image.reverse", "图片生成", "图片提示词反推", "单张", 2, "点/次"),
    _rule("text.copy", "文案与编导", "文案生成 / 短剧策划", "每次生成", 3, "点/次"),
    _rule("breakdown.item", "文案与编导", "素材拆解 / 提示词反推", "每个链接、视频或图片", 20, "点/个"),
    _rule("canvas.agent", "文案与编导", "AI 画布助手", "每次 AI 操作", 3, "点/次"),
    _rule("audio.tts", "音频与数字人", "配音 / 短剧配音", "每条提交", 10, "点/条"),
    _rule("audio.voice_slot", "音频与数字人", "个人音色槽位", "每个槽位", 50, "点/个"),
    _rule("video.talking.block", "音频与数字人", "数字人口播", "每 30 秒，不足 30 秒按 30 秒", 30, "点/30秒"),
    _rule("avatar.create", "音频与数字人", "创建数字人形象", "每个形象", 2, "点/次"),
    _rule("video.cinematic.motion", "视频生成", "电影化身 · 动作模仿", "按成片时长", 10, "点/秒"),
    _rule("video.cinematic.duo", "视频生成", "电影化身 · 双人互动", "按成片时长", 30, "点/秒"),
    _rule("video.cinematic.open", "视频生成", "电影化身 · 开放式生成", "按成片时长", 10, "点/秒", "短剧逐镜视频复用此价格"),
    _rule("video.tryon.single", "视频生成", "AI 换装", "只换衣服或只换背景", 25, "点/次"),
    _rule("video.tryon.double", "视频生成", "AI 换装 + 换背景", "两项同时处理", 40, "点/次"),
    _rule("video.grok.v1.480p", "视频生成", "果肉视频 Grok 1.0", "480p", 10, "点/秒"),
    _rule("video.grok.v1.720p", "视频生成", "果肉视频 Grok 1.0", "720p", 12, "点/秒", "编导剧情视频默认使用此价格"),
    _rule("video.grok.v1_5.480p", "视频生成", "果肉视频 Grok 1.5", "480p", 15, "点/秒"),
    _rule("video.grok.v1_5.720p", "视频生成", "果肉视频 Grok 1.5", "720p", 25, "点/秒"),
    _rule("video.grok.v1_5.1080p", "视频生成", "果肉视频 Grok 1.5", "1080p", 44, "点/秒"),
    _rule("video.seedance", "视频生成", "Seedance 视频", "按成片时长", 30, "点/秒"),
    _rule("video.omni", "视频生成", "Gemini Omni 视频", "按成片时长", 30, "点/秒"),
    _rule("video.minimax_h3.768p", "视频生成", "麦克视频", "768P", 6, "点/秒", "人物参考剧情视频"),
    _rule("video.sora.standard.720p", "视频生成", "Sora 2", "720p", 30, "点/秒"),
    _rule("video.sora.pro.720p", "视频生成", "Sora 2 Pro", "720p", 90, "点/秒"),
    _rule("video.sora.pro.1024p", "视频生成", "Sora 2 Pro", "1024p", 150, "点/秒"),
    _rule("video.sora.pro.1080p", "视频生成", "Sora 2 Pro", "1080p", 210, "点/秒"),
    _rule("collect.search", "采集与获客", "内容搜索", "每次搜索", 1, "点/次"),
    _rule("collect.base", "采集与获客", "内容抓取", "每个链接", 3, "点/次"),
    _rule("collect.transcript_extra", "采集与获客", "文案 / 字幕提取", "在抓取价上加收", 3, "点/次"),
    _rule("leads.base", "采集与获客", "获客线索", "每次任务基础价", 6, "点/次"),
    _rule("leads.per_four", "采集与获客", "获客线索", "每 4 条×页数加收", 1, "点"),
]
CATALOG_MAP = {item["key"]: item for item in CATALOG}


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS pricing_rules(
                rule TEXT PRIMARY KEY,
                points INTEGER NOT NULL CHECK(points > 0 AND points <= 100000),
                updated_by TEXT,
                updated_at INTEGER NOT NULL
            )"""
        )
        conn.commit()


def _load_rows():
    init_db()
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM pricing_rules").fetchall()
    return {row["rule"]: dict(row) for row in rows if row["rule"] in CATALOG_MAP}


def _cached_rows():
    now = time.time()
    if now - _CACHE["loaded_at"] > _TTL:
        try:
            _CACHE["items"] = _load_rows()
            _CACHE["loaded_at"] = now
        except Exception as exc:
            print("[pricing] read failed, using safe cache: %s" % exc, flush=True)
    return _CACHE["items"]


def invalidate_cache():
    _CACHE["loaded_at"] = 0


def get_price(key):
    meta = CATALOG_MAP.get(str(key or "").strip())
    if not meta:
        raise KeyError("unknown pricing rule: %s" % key)
    row = _cached_rows().get(meta["key"]) or {}
    return int(row.get("points") or meta["default_points"])


def get_rule(key, rows=None):
    meta = dict(CATALOG_MAP[str(key or "").strip()])
    row = (rows if rows is not None else _cached_rows()).get(meta["key"]) or {}
    meta.update({
        "points": int(row.get("points") or meta["default_points"]),
        "custom": bool(row),
        "updated_by": row.get("updated_by"),
        "updated_at": row.get("updated_at"),
    })
    return meta


def list_prices():
    rows = _cached_rows()
    return [get_rule(item["key"], rows) for item in CATALOG]


def set_price(key, points, actor):
    key = str(key or "").strip()
    if key not in CATALOG_MAP:
        raise ValueError("unknown pricing rule")
    if isinstance(points, bool):
        raise ValueError("points must be an integer")
    try:
        value = int(points)
    except (TypeError, ValueError):
        raise ValueError("points must be an integer")
    if value != points and str(points).strip() != str(value):
        raise ValueError("points must be an integer")
    if value < 1 or value > 100000:
        raise ValueError("points must be between 1 and 100000")
    now = int(time.time())
    init_db()
    with closing(db()) as conn:
        conn.execute(
            """INSERT INTO pricing_rules(rule,points,updated_by,updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(rule) DO UPDATE SET
                   points=excluded.points,
                   updated_by=excluded.updated_by,
                   updated_at=excluded.updated_at""",
            (key, value, actor or "admin", now),
        )
        conn.commit()
    invalidate_cache()
    return get_rule(key)


def public_catalog():
    items = []
    for rule in list_prices():
        items.append({
            key: rule.get(key)
            for key in (
                "key", "category", "name", "variant", "points", "unit",
                "desc", "updated_at",
            )
        })
    return {
        "items": items,
        "currency": "黄雀点数",
        "points_per_yuan": 10,
        "cache_seconds": _TTL,
    }
