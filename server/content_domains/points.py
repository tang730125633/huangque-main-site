# -*- coding: utf-8 -*-
import time

from .core import AUTH_BASE, AUTH_INTERNAL_TOKEN, COST, closing, jdb, json, urllib, _ensure_column

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
    if kind == "tryon":
        has_clothes = bool(body.get("clothes_data"))
        has_bg = bool(body.get("background_data"))
        return 40 if (has_clothes and has_bg) else 25  # 两段(换装+换背景)40/单段25
        # TODO: 上线前与 kongli 确认点数
    if kind == "xiaole_video":
        # 果肉(Grok)/豆姐(Seedance) 视频。Grok 上游约 15 积分/条，含毛利暂定 30 点。
        # TODO: 上线前与业务确认点数
        return 30
    return COST.get(kind, 0)

class AuthPointsError(Exception):
    def __init__(self, status, detail, data=None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.data = data or {}

def _auth_points_request(path, payload=None, method="POST"):
    if not AUTH_INTERNAL_TOKEN:
        raise AuthPointsError(500, "未配置内部点数接口密钥")
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        AUTH_BASE + path,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-HQ-Internal-Token": AUTH_INTERNAL_TOKEN,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
        raise AuthPointsError(e.code, body.get("detail") or "点数接口调用失败", body)
    except AuthPointsError:
        raise
    except Exception as e:
        raise AuthPointsError(502, "点数接口不可用: " + str(e)[:120])

def get_points(username):
    username = urllib.parse.quote(str(username or ""), safe="")
    try:
        res = _auth_points_request("/api/auth/points?username=" + username, method="GET")
        return int(res.get("points") or 0)
    except Exception:
        return 0

def deduct_points(username, amount):
    amount = int(amount or 0)
    if amount <= 0:
        return get_points(username)
    res = _auth_points_request("/api/auth/points/deduct", {"username": username, "amount": amount})
    return int(res.get("points") or 0)

def refund_points(username, amount):
    amount = int(amount or 0)
    if amount <= 0:
        return get_points(username)
    res = _auth_points_request("/api/auth/points/refund", {"username": username, "amount": amount})
    return int(res.get("points") or 0)

def safe_refund_points(username, amount):
    try:
        return refund_points(username, amount)
    except Exception:
        return get_points(username)

def add_points(username, delta):
    try:
        delta = int(delta or 0)
        if delta >= 0:
            return refund_points(username, delta)
        return deduct_points(username, -delta)
    except Exception:
        return get_points(username)

def _job_payload(raw):
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

def _history_func_name(kind, payload):
    kind = kind or "unknown"
    if kind == "image":
        model = str(payload.get("model") or "").strip().lower()
        provider = str(payload.get("provider") or "").strip().lower()
        if model == "nb2":
            return "作图 · Nano Banana 2"
        if model == "pro":
            return "作图 · Nano Banana Pro"
        if provider == "openai":
            return "作图 · GPT Image"
        if provider.startswith("zelong"):
            return "作图 · 泽龙"
        return "作图"
    if kind == "video":
        mode = str(payload.get("mode") or "").strip().lower()
        if mode == "text":
            return "视频 · 文案口播"
        if mode == "audio":
            return "视频 · 音频口播"
        if mode == "motion":
            return "视频 · 动作模仿"
        return "视频生成"
    if kind == "collect":
        if str(payload.get("keyword") or "").strip():
            return "内容采集 · 关键词搜索"
        if str(payload.get("url") or "").strip():
            return "内容采集 · 贴链接"
        return "内容采集"
    names = {
        "tryon": "换装换背景",
        "xiaole_video": "果肉/微艺视频",
        "audio": "配音生成",
        "leads": "获客分析",
        "leadgen": "获客分析",
        "copy": "文案生成",
        "dl": "无水印下载",
    }
    return names.get(kind, kind)

def _history_status_label(status, refunded):
    status = str(status or "").lower()
    if refunded:
        return "已退点"
    if status == "done":
        return "已完成"
    if status in {"error", "failed"}:
        return "失败"
    if status == "running":
        return "生成中"
    if status == "pending":
        return "排队中"
    return status or "未知"

def history(username, days=30, kind="", page=1, page_size=20):
    days = max(1, min(int(days or 30), 365))
    page = max(1, int(page or 1))
    page_size = max(5, min(int(page_size or 20), 50))
    kind = str(kind or "").strip()
    since = int(time.time()) - days * 86400
    where = ["username=?", "created_at>=?"]
    params = [username, since]
    if kind:
        where.append("kind=?")
        params.append(kind)
    where_sql = " AND ".join(where)
    with closing(jdb()) as c:
        _ensure_column(c, "jobs", "refunded", "INTEGER DEFAULT 0")
        total = c.execute("SELECT COUNT(*) AS n FROM jobs WHERE " + where_sql, params).fetchone()["n"]
        rows = c.execute("""SELECT id, kind, cost, status, payload, error, created_at, updated_at, refunded
                         FROM jobs WHERE %s
                         ORDER BY created_at DESC, id DESC
                         LIMIT ? OFFSET ?""" % where_sql,
                         params + [page_size, (page - 1) * page_size]).fetchall()
        kinds = c.execute("""SELECT kind, COUNT(*) AS n FROM jobs
                          WHERE username=? AND created_at>=?
                          GROUP BY kind ORDER BY n DESC""", (username, since)).fetchall()
    items = []
    for row in rows:
        payload = _job_payload(row["payload"])
        refunded = bool(row["refunded"])
        cost = int(row["cost"] or 0)
        items.append({
            "task_id": row["id"],
            "kind": row["kind"] or "unknown",
            "func": _history_func_name(row["kind"], payload),
            "cost": cost,
            "amount": -cost,
            "status": row["status"] or "unknown",
            "status_label": _history_status_label(row["status"], refunded),
            "refunded": refunded,
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
            "error": (row["error"] or "")[:160],
        })
    total = int(total or 0)
    return {
        "days": days,
        "kind": kind,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "kinds": [{"kind": r["kind"], "label": _history_func_name(r["kind"], {}), "count": r["n"]} for r in kinds],
        "items": items,
    }
