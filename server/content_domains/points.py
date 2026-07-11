# -*- coding: utf-8 -*-
import math
import os
import time

from .core import AUTH_BASE, AUTH_INTERNAL_TOKEN, COST, closing, jdb, json, urllib, _ensure_column

# 各引擎的质量基价（点）。**1 点 = 0.1 元**，按上游官网价折算（汇率 7.1）。
# gpt-image-2 按官方 $30/M image output token 实测（2026-07-10，读 API 返回的 usage）：
#   标准(medium)  1024x1024 1756 tok=$0.0527 ¥0.37 | 1152x2048 1413 tok=$0.0424 ¥0.30 | 1200x1600 1694 tok=$0.0508 ¥0.36
#   高清(high) 恒为 medium 的 4 倍：¥1.20 ~ ¥1.50
# 取各比例里的最贵档定价，避免倒挂：标准 4 点、高清 15 点。
#   ⚠ 已知缺口：1:1 高清 + 图生图 还要 +1024 image input token($8/M)，实为 ¥1.554 ≈ 16 点。
# 其余引擎沿用原 8/12，待逐个测准后再调（Seedream 实际成本仅 2~6 点，偏高）。
IMAGE_BASE_COST = {
    "openai":   {"std": 4, "hd": 15},
    "seedream": {"std": 8, "hd": 12},
    "xiaole":   {"std": 8, "hd": 12},
    "zelong":   {"std": 8, "hd": 12},
    "zelong2":  {"std": 8, "hd": 12},
}
_IMAGE_DEFAULT_COST = {"std": 8, "hd": 12}
# 数量上限必须与 image.gen_image 里的 cap 逐字一致，否则按 N 扣点却只出 cap 张 = 超收。
_IMAGE_CAP_2 = {"zelong", "zelong2", "xiaole", "seedream"}


def cost_of(kind, body):
    """动态点数：TikHub 按次计费，采集/获客调用数随参数变。约 5x buff 折算成点。"""
    if kind == "collect":
        return 3 + (3 if "transcript" in (body.get("want") or []) else 0)
    if kind == "leads":
        n = max(1, min(30, int(body.get("count") or 12)))
        p = max(1, min(3, int(body.get("pages") or 1)))
        return 6 + (n * p) // 4
    if kind == "image":
        # 质量基价按引擎分档（IMAGE_BASE_COST）。gen_image 里 provider 缺省是 openai，这里保持一致。
        provider = (body.get("provider") or "openai").strip().lower()
        tier = "hd" if (body.get("quality") or "hd") == "hd" else "std"
        base = (IMAGE_BASE_COST.get(provider) or _IMAGE_DEFAULT_COST)[tier]
        # cap 必须与 image.gen_image 里的数量上限逐字一致，否则按 N 扣点却只出 cap 张 = 超收。
        cap = 2 if provider in _IMAGE_CAP_2 else 4
        cnt = 1 if body.get("mask") else max(1, min(cap, int(body.get("count") or 1)))
        return base * cnt  # 质量基价 × 数量
    if kind == "tryon":
        has_clothes = bool(body.get("clothes_data"))
        has_bg = bool(body.get("background_data"))
        return 40 if (has_clothes and has_bg) else 25  # 两段(换装+换背景)40/单段25
        # TODO: 上线前与 kongli 确认点数
    if kind == "xiaole_video":
        if (body.get("channel") or "grok") == "grok" and os.environ.get("GROK_VIDEO_PROVIDER", "xai").lower() != "xiaole":
            if body.get("operation") == "edit":
                duration = max(0.1, min(8.7, float(body.get("source_duration") or 0.1)))
                usd_cny = float(os.environ.get("XAI_USD_CNY", "7.3") or 7.3)
                buffer = float(os.environ.get("XAI_PRICE_BUFFER", "1.2") or 1.2)
                # 官方输入视频 $0.01/秒，编辑输出继承输入且最高 720p，按 $0.07/秒计。
                return max(1, int(math.ceil(duration * (0.01 + 0.07) * usd_cny * buffer * 10)))
            model = body.get("model") or "grok-imagine-video"
            resolution = (body.get("resolution") or "720p").lower()
            duration = max(1, min(15, int(body.get("duration") or 10)))
            per_second = {
                "grok-imagine-video": {"480p": 0.05, "720p": 0.07},
                "grok-imagine-video-1.5": {"480p": 0.08, "720p": 0.14, "1080p": 0.25},
            }.get(model, {}).get(resolution)
            if per_second is None:
                raise ValueError("果肉官方模型与分辨率不匹配")
            image_input = 0.01 if (model == "grok-imagine-video-1.5" and body.get("reference_images")) \
                else (0.002 if body.get("reference_images") else 0.0)
            usd_cny = float(os.environ.get("XAI_USD_CNY", "7.3") or 7.3)
            buffer = float(os.environ.get("XAI_PRICE_BUFFER", "1.2") or 1.2)
            # 1点=0.1元；默认按保守汇率+20%波动/运营缓冲向上取整，可用env调整。
            return max(1, int(math.ceil((duration * per_second + image_input) * usd_cny * buffer * 10)))
        # 豆姐/欧米及果肉小乐回滚线保留原定价。
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

def deduct_points(username, amount, reason=""):
    """预扣点。reason 落 points_audit，供对账。

    注意：三个服务都是「先扣点、后 INSERT jobs 行」，所以扣点这一刻还没有 job_id，
    reason 只能到 'job:<kind>' 这一层。退点时 job 行已存在，reason 会带上 '#<id>'。
    要让扣点也带 id，得把 INSERT 挪到扣点前面 —— 那样两步之间崩溃会留下一个没付钱的
    pending 任务被 worker 捡走白跑，代价大于收益，故不改。
    """
    amount = int(amount or 0)
    if amount <= 0:
        return get_points(username)
    res = _auth_points_request("/api/auth/points/deduct",
                               {"username": username, "amount": amount, "reason": reason})
    return int(res.get("points") or 0)

def refund_points(username, amount, reason=""):
    amount = int(amount or 0)
    if amount <= 0:
        return get_points(username)
    res = _auth_points_request("/api/auth/points/refund",
                               {"username": username, "amount": amount, "reason": reason})
    return int(res.get("points") or 0)

def safe_refund_points(username, amount, reason=""):
    try:
        return refund_points(username, amount, reason)
    except Exception:
        return get_points(username)

def add_points(username, delta, reason=""):
    try:
        delta = int(delta or 0)
        if delta >= 0:
            return refund_points(username, delta, reason)
        return deduct_points(username, -delta, reason)
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
        if provider == "seedream":
            return "作图 · Seedream" + (" Pro" if str(payload.get("variant") or "").lower() == "pro" else "")
        if provider == "xiaole":
            return "作图 · 果肉生图"
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
