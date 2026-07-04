# -*- coding: utf-8 -*-
from . import core as _core
globals().update({k: getattr(_core, k) for k in dir(_core) if not k.startswith("__")})

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
