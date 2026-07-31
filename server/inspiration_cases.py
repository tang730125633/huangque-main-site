#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small CRUD store for operator-managed inspiration cases."""

from contextlib import closing
import json
import pathlib
import sqlite3
import tempfile
import time
import urllib.parse
import uuid

try:
    from content_domains import cos
except ImportError:
    from .content_domains import cos


PUBLIC_ID_BASE = 1_000_000
TARGETS = {"nb2", "pro", "gpt", "seedream", "grok", "micro", "omni"}
IMAGE_TARGETS = {"nb2", "pro", "gpt", "seedream"}
STATUSES = {"draft", "published", "unpublished"}
RIGHTS = {"original", "authorized"}
MEDIA = {
    "image/png": ("image", "png", 10 * 1024 * 1024),
    "image/jpeg": ("image", "jpg", 10 * 1024 * 1024),
    "image/webp": ("image", "webp", 10 * 1024 * 1024),
    "video/mp4": ("video", "mp4", 200 * 1024 * 1024),
    "video/webm": ("video", "webm", 200 * 1024 * 1024),
}


def _db(path):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path):
    with closing(_db(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS inspiration_cases(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                prompt TEXT NOT NULL DEFAULT '',
                media_type TEXT NOT NULL DEFAULT 'image',
                media_url TEXT NOT NULL DEFAULT '',
                cover_url TEXT NOT NULL DEFAULT '',
                target TEXT NOT NULL DEFAULT 'nb2',
                status TEXT NOT NULL DEFAULT 'draft',
                featured INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 100,
                source_platform TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                rights_status TEXT NOT NULL DEFAULT '',
                rights_note TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                published_at INTEGER,
                impressions INTEGER NOT NULL DEFAULT 0,
                clicks INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_inspiration_cases_public
              ON inspiration_cases(status, featured DESC, sort_order, published_at DESC);
            """
        )
        conn.commit()


def _text(value, limit, field, required=False, plain=False):
    value = str(value or "").strip()
    if required and not value:
        raise ValueError("请填写%s" % field)
    if len(value) > limit:
        raise ValueError("%s不能超过 %d 个字" % (field, limit))
    if plain and any(ch in value for ch in "<>"):
        raise ValueError("%s不能包含尖括号" % field)
    return value


def _url(value, field, required=False):
    value = _text(value, 2000, field, required=required)
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or any(ch in value for ch in "\r\n\"'"):
        raise ValueError("%s必须是有效的 http(s) 地址" % field)
    return value


def _tags(value):
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise ValueError("标签格式无效")
    result = []
    for item in value[:12]:
        item = _text(item, 20, "标签", plain=True)
        if item and item not in result:
            result.append(item)
    return result


def _normalized(body):
    media_type = str(body.get("media_type") or "image").strip()
    target = str(body.get("target") or "nb2").strip()
    if media_type not in {"image", "video"}:
        raise ValueError("素材类型无效")
    if target not in TARGETS or (media_type == "image") != (target in IMAGE_TARGETS):
        raise ValueError("做同款目标与素材类型不匹配")
    media_url = _url(body.get("media_url"), "案例素材地址")
    cover_url = _url(body.get("cover_url"), "封面地址") or (media_url if media_type == "image" else "")
    rights = str(body.get("rights_status") or "").strip()
    if rights and rights not in RIGHTS:
        raise ValueError("授权状态无效")
    return {
        "title": _text(body.get("title"), 80, "标题", required=True, plain=True),
        "category": _text(body.get("category"), 30, "分类", plain=True),
        "tags": json.dumps(_tags(body.get("tags") or []), ensure_ascii=False),
        "prompt": _text(body.get("prompt"), 5000, "提示词"),
        "media_type": media_type,
        "media_url": media_url,
        "cover_url": cover_url,
        "target": target,
        "featured": 1 if body.get("featured") else 0,
        "sort_order": max(0, min(int(body.get("sort_order") or 100), 9999)),
        "source_platform": _text(body.get("source_platform"), 40, "素材来源", plain=True),
        "source_url": _url(body.get("source_url"), "原链接"),
        "rights_status": rights,
        "rights_note": _text(body.get("rights_note"), 300, "授权备注", plain=True),
    }


def _validate_publish(item):
    for key, label in (("category", "分类"), ("prompt", "提示词"), ("media_url", "案例素材"), ("cover_url", "封面")):
        if not item.get(key):
            raise ValueError("发布前请补充%s" % label)
    if item.get("rights_status") not in RIGHTS:
        raise ValueError("发布前请选择原创或已获授权")
    if item.get("rights_status") == "authorized" and not item.get("source_url"):
        raise ValueError("授权素材发布前请填写原链接")


def save_case(path, body, actor, publish=False):
    data = _normalized(body)
    if publish:
        _validate_publish(data)
    now = int(time.time())
    case_id = int(body.get("id") or 0)
    with closing(_db(path)) as conn:
        old = conn.execute("SELECT * FROM inspiration_cases WHERE id=?", (case_id,)).fetchone() if case_id else None
        if case_id and old is None:
            raise ValueError("案例不存在")
        if old is not None and old["status"] == "published" and not publish:
            _validate_publish(data)
        status = "published" if publish else (old["status"] if old is not None else "draft")
        published_at = now if publish and (old is None or old["status"] != "published") else (old["published_at"] if old is not None else None)
        fields = list(data)
        if old is None:
            cols = fields + ["status", "created_by", "updated_by", "created_at", "updated_at", "published_at"]
            vals = [data[x] for x in fields] + [status, actor, actor, now, now, published_at]
            marks = ",".join("?" for _ in cols)
            cur = conn.execute("INSERT INTO inspiration_cases(%s) VALUES(%s)" % (",".join(cols), marks), vals)
            case_id = int(cur.lastrowid)
        else:
            assigns = ",".join("%s=?" % x for x in fields)
            vals = [data[x] for x in fields] + [status, actor, now, published_at, case_id]
            conn.execute("UPDATE inspiration_cases SET %s,status=?,updated_by=?,updated_at=?,published_at=? WHERE id=?" % assigns, vals)
        conn.commit()
    return get_case(path, case_id)


def set_status(path, case_id, status, actor):
    if status not in {"published", "unpublished"}:
        raise ValueError("状态操作无效")
    case_id = int(case_id or 0)
    with closing(_db(path)) as conn:
        row = conn.execute("SELECT * FROM inspiration_cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise ValueError("案例不存在")
        if status == "published":
            _validate_publish(dict(row))
        now = int(time.time())
        published_at = now if status == "published" and row["status"] != "published" else row["published_at"]
        conn.execute(
            "UPDATE inspiration_cases SET status=?,updated_by=?,updated_at=?,published_at=? WHERE id=?",
            (status, actor, now, published_at, case_id),
        )
        conn.commit()
    return get_case(path, case_id)


def _item(row):
    item = dict(row)
    try:
        item["tags"] = json.loads(item.get("tags") or "[]")
    except Exception:
        item["tags"] = []
    item["featured"] = bool(item.get("featured"))
    item["public_id"] = PUBLIC_ID_BASE + int(item["id"])
    return item


def get_case(path, case_id):
    with closing(_db(path)) as conn:
        row = conn.execute("SELECT * FROM inspiration_cases WHERE id=?", (int(case_id),)).fetchone()
    if row is None:
        raise ValueError("案例不存在")
    return _item(row)


def _job_metrics(job_db, days=30):
    path = pathlib.Path(job_db)
    result = {}
    if not path.exists():
        return result
    since = int(time.time()) - max(1, min(int(days), 90)) * 86400
    try:
        with closing(sqlite3.connect(str(path), timeout=10)) as conn:
            rows = conn.execute(
                """SELECT cost,CASE WHEN json_valid(payload)
                   THEN json_extract(payload,'$.source_inspiration_id') END
                   FROM jobs WHERE status='done' AND created_at>=?""",
                (since,),
            ).fetchall()
    except sqlite3.Error:
        return result
    for cost, source_id in rows:
        try:
            public_id = int(source_id or 0)
        except Exception:
            continue
        if public_id < PUBLIC_ID_BASE:
            continue
        metric = result.setdefault(public_id - PUBLIC_ID_BASE, {"success_count": 0, "points_spent": 0})
        metric["success_count"] += 1
        metric["points_spent"] += int(cost or 0)
    return result


def list_admin(path, job_db, days=30):
    with closing(_db(path)) as conn:
        rows = conn.execute("SELECT * FROM inspiration_cases ORDER BY updated_at DESC,id DESC").fetchall()
    metrics = _job_metrics(job_db, days)
    items = []
    for row in rows:
        item = _item(row)
        item.update(metrics.get(item["id"], {"success_count": 0, "points_spent": 0}))
        items.append(item)
    return {"items": items, "days": days}


def list_public(path):
    with closing(_db(path)) as conn:
        rows = conn.execute(
            "SELECT * FROM inspiration_cases WHERE status='published' ORDER BY featured DESC,sort_order ASC,published_at DESC,id DESC"
        ).fetchall()
    items = []
    for row in rows:
        data = _item(row)
        public = {
            "id": data["public_id"], "title": data["title"], "category": data["category"],
            "tags": data["tags"], "prompt": data["prompt"], "type": data["media_type"],
            "image": data["cover_url"], "model": data["target"], "target": data["target"],
            "count": 0, "managed": True,
        }
        if data["media_type"] == "video":
            public["video"] = data["media_url"]
        items.append(public)
    return {"items": items}


def record_events(path, body):
    event = str(body.get("event") or "").strip()
    ids = body.get("ids") or []
    if event not in {"impression", "click"} or not isinstance(ids, list) or not 1 <= len(ids) <= 50:
        raise ValueError("事件格式无效")
    internal = sorted({int(x) - PUBLIC_ID_BASE for x in ids if str(x).isdigit() and int(x) >= PUBLIC_ID_BASE})
    if not internal:
        raise ValueError("案例编号无效")
    column = "impressions" if event == "impression" else "clicks"
    with closing(_db(path)) as conn:
        marks = ",".join("?" for _ in internal)
        conn.execute(
            "UPDATE inspiration_cases SET %s=%s+1 WHERE status='published' AND id IN (%s)" % (column, column, marks),
            internal,
        )
        conn.commit()
    return {"ok": True}


def _valid_magic(content_type, head):
    return {
        "image/png": head.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": head.startswith(b"\xff\xd8\xff"),
        "image/webp": head.startswith(b"RIFF") and head[8:12] == b"WEBP",
        "video/mp4": len(head) >= 12 and head[4:8] == b"ftyp",
        "video/webm": head.startswith(b"\x1aE\xdf\xa3"),
    }.get(content_type, False)


def upload_media(stream, length, content_type, requested_kind):
    content_type = str(content_type or "").split(";", 1)[0].strip().lower()
    spec = MEDIA.get(content_type)
    if not spec or spec[0] != requested_kind:
        raise ValueError("只支持 PNG/JPG/WebP 图片或 MP4/WebM 视频")
    length = int(length or 0)
    if length < 1 or length > spec[2]:
        raise ValueError("图片最大 10MB，视频最大 200MB")
    if not cos.enabled():
        raise RuntimeError("COS 未配置，暂时无法上传素材")
    temp = tempfile.NamedTemporaryFile(prefix="hq-inspiration-", suffix="." + spec[1], delete=False)
    path = pathlib.Path(temp.name)
    head = b""
    left = length
    try:
        with temp:
            while left:
                chunk = stream.read(min(left, 1024 * 1024))
                if not chunk:
                    raise ValueError("上传中断，请重试")
                if len(head) < 16:
                    head += chunk[:16 - len(head)]
                temp.write(chunk)
                left -= len(chunk)
        if not _valid_magic(content_type, head):
            raise ValueError("文件内容与格式不一致")
        key = "inspirations/%s/%s.%s" % (time.strftime("%Y/%m"), uuid.uuid4().hex, spec[1])
        url = cos.put_file(str(path), key, content_type=content_type)
        return {"ok": True, "url": url, "key": key, "media_type": spec[0], "size": length}
    finally:
        path.unlink(missing_ok=True)
