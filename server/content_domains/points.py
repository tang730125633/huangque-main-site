# -*- coding: utf-8 -*-
import math
import os
import time

from .core import AUTH_BASE, AUTH_INTERNAL_TOKEN, COST, closing, jdb, json, urllib, _ensure_column

# 各引擎的质量基价（点）。**1 点 = 0.1 元**，按上游官网价折算（汇率 7.1）。
# gpt-image-2 按官方 $30/M image output token 实测（2026-07-10，读 API 返回的 usage）：
#   标准(medium)  1024x1024 1756 tok=$0.0527 ¥0.37 | 1152x2048 1413 tok=$0.0424 ¥0.30 | 1200x1600 1694 tok=$0.0508 ¥0.36
#   高清(high) 恒为 medium 的 4 倍：¥1.20 ~ ¥1.50
# 实测成本：标准约 ¥0.3~0.37（≈4 点）、高清约 ¥1.2~1.5（≈15 点）。定价上浮到 标准 20 点、
# 高清 30 点（kongli 2026-07-15 调价，含利润空间，不再贴成本走）。
#   ⚠ 已知缺口：1:1 高清 + 图生图 还要 +1024 image input token($8/M)，实为 ¥1.554 ≈ 16 点。
# 其余引擎沿用原 8/12，待逐个测准后再调（Seedream 实际成本仅 2~6 点，偏高）。
IMAGE_BASE_COST = {
    "openai":   {"std": 20, "hd": 30},
    "xiaole":   {"std": 8, "hd": 12},
    "zelong":   {"std": 8, "hd": 12},
    "zelong2":  {"std": 8, "hd": 12},
}
_IMAGE_DEFAULT_COST = {"std": 8, "hd": 12}
# Seedream 按【型号】(5.0 标准 / 5.0 pro，payload.variant) 再分【清晰度】(标准 std / 高清 hd) 定价
# （kongli 2026-07-15）。此前两个型号同价 {std:8,hd:12}，现在 pro 型号更贵。
SEEDREAM_VARIANT_COST = {
    "std": {"std": 8,  "hd": 12},   # 5.0 标准
    "pro": {"std": 15, "hd": 20},   # 5.0 Pro
}
# 数量上限必须与 image.gen_image 里的 cap 逐字一致，否则按 N 扣点却只出 cap 张 = 超收。
_IMAGE_CAP_2 = {"zelong", "zelong2", "xiaole", "seedream"}


def cost_of(kind, body):
    """动态点数：TikHub 按次计费，采集/获客调用数随参数变。约 5x buff 折算成点。"""
    if kind == "collect":
        return 3 + (3 if "transcript" in (body.get("want") or []) else 0)
    if kind == "leads":
        return 30   # 获客固定 30 点/次（采集量前端固定 20 视频）；与 leads.html 成本徽章一致，防"消耗点数对不上"
    if kind == "image":
        # 质量基价按引擎分档（IMAGE_BASE_COST）。gen_image 里 provider 缺省是 openai，这里保持一致。
        provider = (body.get("provider") or "openai").strip().lower()
        tier = "hd" if (body.get("quality") or "hd") == "hd" else "std"
        if provider == "seedream":
            variant = (body.get("variant") or "std").strip().lower()   # 5.0 标准 / 5.0 pro
            base = (SEEDREAM_VARIANT_COST.get(variant) or SEEDREAM_VARIANT_COST["std"])[tier]
        else:
            base = (IMAGE_BASE_COST.get(provider) or _IMAGE_DEFAULT_COST)[tier]
        # cap 必须与 image.gen_image 里的数量上限逐字一致，否则按 N 扣点却只出 cap 张 = 超收。
        cap = 2 if provider in _IMAGE_CAP_2 else 4
        cnt = 1 if body.get("mask") else max(1, min(cap, int(body.get("count") or 1)))
        return base * cnt  # 质量基价 × 数量
    if kind == "cinematic":
        # 电影化身：按成片秒数计费（单人动作模仿/开放式 3 点/秒，双人动作模仿 5 点/秒）。
        # 秒数在 validate_cinematic_payload 里已经落定成整数（「自适应」在那里就探测过参考视频），
        # 所以这里不存在「还不知道多长」的情况 —— 一次扣准，不需要预扣退差。
        from . import video as video_domain
        return video_domain.cinematic_cost(body)
    if kind == "video":
        # 口播 10 点/秒 × 输出时长。这里算的是【预扣 hold】：audio 模式 ffprobe 拿精确时长扣准；
        # text 模式 TTS 还没跑，按文本长度偏保守估算预扣，跑完由 run_job 按成片真实时长结算多退。
        from . import video as video_domain
        return video_domain.video_cost(body)
    if kind == "tryon":
        has_clothes = bool(body.get("clothes_data"))
        has_bg = bool(body.get("background_data"))
        return 40 if (has_clothes and has_bg) else 25  # 两段(换装+换背景)40/单段25
        # TODO: 上线前与 kongli 确认点数
    if kind == "xiaole_video":
        # 果肉视频统一 30 点/秒 × 时长（kongli 2026-07-15）。此前是按 xAI 官方成本×汇率动态算/回滚线扁平 30。
        # 编辑走 source_duration(上限 8.7s)，生成走 duration(上限 15s)；认不出时长兜底 10s。
        if body.get("operation") == "edit":
            duration = min(8.7, float(body.get("source_duration") or 0.1))
        else:
            duration = min(15, int(body.get("duration") or 10))
        return max(30, int(math.ceil(duration)) * 30)
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

# 功能名映射抽到了 server/func_names.py（唯一事实来源，和运营后台的日志/统计共用一份）。
# 原来这里和 admin_api.call_func_name 是两份拷贝，已经各自漂移 —— 见 func_names 的模块注释。
try:
    import func_names as _func_names     # 生产：content_api.py 直接跑，server/ 就是 sys.path[0]
except ModuleNotFoundError:              # 测试：以包的形式 import server.content_domains.points
    from .. import func_names as _func_names

_history_func_name = _func_names.func_name

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
