"""Shared platform feature flags.

The admin service writes these flags; content/imggen read them before accepting
new generation work. Missing rows use each catalog entry's default: legacy
entries remain enabled, while new features may opt into fail-closed behavior
with ``default_enabled=False``.
"""

from contextlib import closing
import os
import pathlib
import sqlite3
import time


BASE = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = pathlib.Path(os.environ.get("FEATURE_FLAGS_DB", str(BASE / "feature_flags.db")))
MAINTENANCE_DETAIL = "该功能维护中，暂不可用"
_CACHE = {"loaded_at": 0, "items": {}}
_TTL = 5

CATALOG = [
    {
        "key": "short_drama_lipsync_multi_v1",
        "name": "短剧多人对白口型",
        "desc": "项目内人物跟踪、人工确认与多人轮流对白口型",
        "service": "content",
        "default_enabled": False,
    },
    {
        "key": "short_drama_lipsync_v1",
        "name": "短剧真实口型",
        "desc": "短剧真实口型灰度、付费任务与合成交付",
        "service": "content",
        "default_enabled": False,
    },
    {"key": "image", "name": "图片生成", "desc": "黄雀引擎 1 / 2 图片生成入口", "service": "content"},
    {"key": "banana", "name": "纳米香蕉作图", "desc": "纳米香蕉 2 / Pro 图片生成入口", "service": "imggen"},
    {"key": "audio", "name": "配音生成", "desc": "文案配音与音色复刻", "service": "content"},
    {"key": "video", "name": "视频口播", "desc": "数字化 IP 视频生成", "service": "content"},
    {"key": "digital_presenter", "name": "数字人口播", "desc": "画布数字人口播项目", "service": "content", "default_enabled": False},
    {"key": "sora_video", "name": "Sora 2 限时测试", "desc": "OpenAI Sora 2 / Pro 非真人通用视频（2026-09-24 下线）", "service": "content", "default_enabled": False},
    {"key": "omni_video", "name": "Omni 视频", "desc": "Gemini Omni Flash 官方视频生成", "service": "content", "default_enabled": False},
    {"key": "seedance_video", "name": "Seedance 视频", "desc": "火山方舟 Seedance 2.0 官方视频生成", "service": "content", "default_enabled": False},
    {"key": "minimax_h3_video", "name": "麦克视频", "desc": "768P 人物参考剧情视频", "service": "content", "default_enabled": False},
    {"key": "avatar", "name": "数字人形象", "desc": "上传照片创建可复用的数字人形象", "service": "content"},
    {"key": "cinematic", "name": "AI 剧情视频", "desc": "选 1~3 个形象 + 提示词生成剧情视频", "service": "content"},
    {"key": "tryon", "name": "换装换背景", "desc": "视频换装与背景生成", "service": "content"},
    {"key": "collect", "name": "内容采集", "desc": "内容抓取与素材采集", "service": "content"},
    {"key": "breakdown", "name": "爆款拆解", "desc": "竞品视频分镜拆解", "service": "content"},
    {"key": "leads", "name": "获客分析", "desc": "线索抓取与评论分析", "service": "content"},
    {"key": "dl", "name": "下载代理", "desc": "无水印视频下载代理", "service": "dl"},
    {"key": "copy", "name": "文案生成", "desc": "营销文案生成", "service": "content"},
    {"key": "canvas_agent", "name": "画布 Agent", "desc": "读取画布并生成需确认的操作建议", "service": "content", "default_enabled": False},
]
CATALOG_MAP = {item["key"]: item for item in CATALOG}


class FeatureDisabled(Exception):
    pass


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS feature_flags(
                feature TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_by TEXT,
                updated_at INTEGER NOT NULL
            )"""
        )
        c.commit()


def _load_rows():
    init_db()
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM feature_flags").fetchall()
    return {
        row["feature"]: {
            "enabled": bool(row["enabled"]),
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    }


def _cached_rows():
    now = time.time()
    if now - _CACHE["loaded_at"] > _TTL:
        try:
            _CACHE["items"] = _load_rows()
        except Exception as e:
            print("[feature_flags] read failed, using safe cache: %s" % e, flush=True)
            items = dict(_CACHE.get("items") or {})
            for key, meta in CATALOG_MAP.items():
                if not meta.get("default_enabled", True):
                    items.pop(key, None)
            return items
        _CACHE["loaded_at"] = now
    return _CACHE["items"]


def invalidate_cache():
    _CACHE["loaded_at"] = 0


def is_enabled(feature):
    meta = CATALOG_MAP.get(str(feature or "").strip())
    if not meta:
        return True
    row = _cached_rows().get(meta["key"])
    return bool(meta.get("default_enabled", True)) if row is None else bool(row.get("enabled"))


def require_enabled(feature):
    if not is_enabled(feature):
        raise FeatureDisabled(MAINTENANCE_DETAIL)


def set_enabled(feature, enabled, actor):
    key = str(feature or "").strip()
    if key not in CATALOG_MAP:
        raise ValueError("unknown feature")
    now = int(time.time())
    with closing(db()) as c:
        c.execute(
            """INSERT INTO feature_flags(feature, enabled, updated_by, updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(feature) DO UPDATE SET
                   enabled=excluded.enabled,
                   updated_by=excluded.updated_by,
                   updated_at=excluded.updated_at""",
            (key, 1 if enabled else 0, actor or "admin", now),
        )
        c.commit()
    invalidate_cache()
    return get_feature(key)


def _safe_rows():
    try:
        return _load_rows()
    except Exception as e:
        print("[feature_flags] read failed, using catalog defaults: %s" % e, flush=True)
        return {}


def get_feature(feature):
    rows = _safe_rows()
    key = str(feature or "").strip()
    meta = dict(CATALOG_MAP[key])
    row = rows.get(key) or {}
    meta.update(
        {
            "enabled": bool(row.get("enabled", meta.get("default_enabled", True))),
            "updated_by": row.get("updated_by"),
            "updated_at": row.get("updated_at"),
        }
    )
    return meta


def list_features(services=None):
    rows = _safe_rows()
    service_map = {}
    for svc in services or []:
        service_map[svc.get("key")] = svc
    out = []
    for item in CATALOG:
        row = rows.get(item["key"]) or {}
        svc = service_map.get(item.get("service")) or {}
        merged = dict(item)
        merged.update(
            {
                "enabled": bool(row.get("enabled", item.get("default_enabled", True))),
                "updated_by": row.get("updated_by"),
                "updated_at": row.get("updated_at"),
                "online": bool(svc.get("online")) if svc else None,
                "service_name": svc.get("name") or item.get("service"),
                "service_status": svc.get("status"),
            }
        )
        out.append(merged)
    return out
