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
    "openai":   {"std": 20, "hd": 35},
    "xiaole":   {"std": 12, "hd": 16},
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

# Sora 2 限时 Beta 售价（点/秒）。官方标准价分别为 $0.10 / $0.30 / $0.50 / $0.70
# 每秒；按 7.1 汇率与 1 元=10 点，裸成本约 7.1 / 21.3 / 35.5 / 49.7 点/秒。
# 这里延续现有 Grok 30 点/秒约 4.2x 的安全垫，覆盖失败、存储、运维与汇率波动。
SORA_VIDEO_RATE = {
    ("sora-2", "720p"): 30,
    ("sora-2-pro", "720p"): 90,
    ("sora-2-pro", "1024p"): 150,
    ("sora-2-pro", "1080p"): 210,
}
# 数量上限必须与 image.gen_image 里的 cap 逐字一致，否则按 N 扣点却只出 cap 张 = 超收。
_IMAGE_CAP_2 = {"zelong", "zelong2", "xiaole", "seedream"}


def cost_of(kind, body):
    """动态点数：TikHub 按次计费，采集/获客调用数随参数变。约 5x buff 折算成点。"""
    if kind == "collect":
        # 提取文案（want 含 transcript）保留 6 点；其余即「内容爬取」，固定 30 点
        # （kongli 2026-07-15，原为 3 点）。前端两个动作共用这一个 collect 接口，靠 want 区分：
        #   主爬取   want=['comments'] 或 ['video']  → 30
        #   提取文案 want=['transcript']              → 6
        if "transcript" in (body.get("want") or []):
            return 6
        return 30
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
        # 电影化身按成片秒数计费；各玩法价格由 video.CINEMATIC_RATE_PER_SEC 管理。
        # 秒数在 validate_cinematic_payload 里已经落定成整数（「自适应」在那里就探测过参考视频），
        # 所以这里不存在「还不知道多长」的情况 —— 一次扣准，不需要预扣退差。
        from . import video as video_domain
        return video_domain.cinematic_cost(body)
    if kind == "video":
        # 口播每 30 秒一档、每档 30 点。这里算的是【预扣 hold】：audio 模式 ffprobe 拿精确时长扣准；
        # text 模式 TTS 还没跑，按文本长度偏保守估算预扣，跑完由 run_job 按成片真实时长结算多退。
        from . import video as video_domain
        return video_domain.video_cost(body)
    if kind == "tryon":
        has_clothes = bool(body.get("clothes_data"))
        has_bg = bool(body.get("background_data"))
        return 40 if (has_clothes and has_bg) else 25  # 两段(换装+换背景)40/单段25
        # TODO: 上线前与 kongli 确认点数
    if kind == "xiaole_video":
        # 果肉生成按模型与分辨率分别定价；参考图不额外收取用户点数。
        if body.get("operation") == "edit":
            raise ValueError("果肉视频编辑维护中")
        duration = min(15, max(1, int(body.get("duration") or 10)))
        if str(body.get("channel") or "grok").lower() != "grok":
            # 泽龙测试期官方 Omni / Seedance 统一 30 点/秒；生产功能旗默认关闭。
            return duration * 30
        model = str(body.get("model") or "grok-imagine-video")
        resolution = str(body.get("resolution") or "720p").lower()
        rates = {
            "grok-imagine-video": {"480p": 10, "720p": 12},
            "grok-imagine-video-1.5": {"480p": 15, "720p": 25, "1080p": 44},
        }
        rate = (rates.get(model) or rates["grok-imagine-video"]).get(resolution)
        if rate is None:
            raise ValueError("%s 不支持分辨率 %s" % (model, resolution))
        return duration * rate
    if kind == "sora_video":
        model = str(body.get("model") or "sora-2").strip().lower()
        resolution = str(body.get("resolution") or "720p").strip().lower()
        # 未知组合用最高档兜底；正常请求会在 validate_sora_video_payload 先被拒绝，
        # 这里的目标是即使未来接线漏校验，也绝不能回落成 0 点免费送高价 Pro。
        rate = SORA_VIDEO_RATE.get((model, resolution), max(SORA_VIDEO_RATE.values()))
        try:
            seconds = int(body.get("seconds") or 4)
        except (TypeError, ValueError):
            seconds = 4
        seconds = max(4, min(12, seconds))
        return rate * seconds
    if kind == "script_to_video":
        style = (body.get("style") or "口播").strip()
        if style == "剧情":
            try:
                duration = min(15, int(float(body.get("duration") or 10)))
            except (TypeError, ValueError):
                duration = 10
            return cost_of("xiaole_video", {
                "channel": "grok",
                "model": body.get("model") or "grok-imagine-video",
                "resolution": body.get("resolution") or "720p",
                "duration": max(1, int(math.ceil(duration))),
            })
        from . import video as video_domain
        lines = [(s.get("line") or "").strip() for s in (body.get("scenes") or []) if isinstance(s, dict)]
        talking = video_domain.video_cost({"text": "\n\n".join(line for line in lines if line)})
        generated = max(0, min(8, int(body.get("material_generate_count") or 0)))
        images = generated * cost_of("image", {
            "provider": "openai", "quality": "standard", "count": 1,
        })
        body["cost_breakdown"] = {
            "talking": talking,
            "material_images": images,
            "material_generate_count": generated,
            "material_reused_count": max(0, len(body.get("material_plan") or []) - generated),
            "total": talking + images,
        }
        return talking + images
    if kind == "breakdown":
        urls = body.get("urls")
        if isinstance(urls, list):
            count = max(1, min(5, len([url for url in urls if isinstance(url, str) and url.strip()])))
            return 20 * count
        return 20
    return COST.get(kind, 0)


def breakdown_batch_refund(cost, total, failed):
    try:
        cost, total, failed = int(cost or 0), int(total or 0), int(failed or 0)
    except (TypeError, ValueError):
        return 0
    if cost <= 0 or total <= 0 or failed <= 0:
        return 0
    failed = min(failed, total)
    return cost if failed >= total else min(cost, 20 * failed)


def settle_breakdown_batch(username, cost, result, job_id):
    if prepare_breakdown_batch_refund(username, cost, result, job_id):
        return reconcile_breakdown_refund(job_id)
    return None


def _ensure_breakdown_refund_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS breakdown_partial_refunds(
        job_id INTEGER PRIMARY KEY,
        username TEXT NOT NULL,
        amount INTEGER NOT NULL,
        transaction_key TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )""")


def prepare_breakdown_batch_refund(username, cost, result, job_id):
    """Persist partial-refund intent before the job is allowed to become done."""
    if (result or {}).get("type") != "breakdown_batch":
        return False
    amount = breakdown_batch_refund(
        cost, (result or {}).get("total"), len((result or {}).get("errors") or []))
    if amount <= 0:
        return False
    job_id = int(job_id)
    username = str(username or "")
    key = "breakdown-partial-refund:%s:%s" % (job_id, username)
    now = int(time.time())
    with closing(jdb()) as connection:
        _ensure_breakdown_refund_table(connection)
        connection.execute(
            """INSERT OR IGNORE INTO breakdown_partial_refunds(
                job_id,username,amount,transaction_key,state,created_at,updated_at
            ) VALUES(?,?,?,?,'pending',?,?)""",
            (job_id, username, amount, key, now, now),
        )
        row = connection.execute(
            "SELECT username,amount FROM breakdown_partial_refunds WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not row or row["username"] != username or int(row["amount"]) != amount:
            raise RuntimeError("批量拆解退款记录冲突")
        connection.commit()
    return True


def cancel_breakdown_refund(job_id):
    with closing(jdb()) as connection:
        _ensure_breakdown_refund_table(connection)
        connection.execute(
            "UPDATE breakdown_partial_refunds SET state='cancelled',updated_at=?"
            " WHERE job_id=? AND state='pending'",
            (int(time.time()), int(job_id)),
        )
        connection.commit()


def reconcile_breakdown_refund(job_id):
    with closing(jdb()) as connection:
        _ensure_breakdown_refund_table(connection)
        row = connection.execute(
            """SELECT r.*,j.status AS job_status
               FROM breakdown_partial_refunds r
               LEFT JOIN jobs j ON j.id=r.job_id WHERE r.job_id=?""",
            (int(job_id),),
        ).fetchone()
    if not row or row["state"] != "pending":
        return row["state"] if row else None
    if row["job_status"] != "done":
        if row["job_status"] in {"error", "failed"} or row["job_status"] is None:
            cancel_breakdown_refund(job_id)
            return "cancelled"
        return "pending"
    try:
        refund_points(
            row["username"], row["amount"],
            "job#%d 批量拆解失败退点" % int(job_id),
            transaction_key=row["transaction_key"],
        )
    except Exception as exc:
        with closing(jdb()) as connection:
            _ensure_breakdown_refund_table(connection)
            connection.execute(
                "UPDATE breakdown_partial_refunds SET attempts=attempts+1,last_error=?,updated_at=?"
                " WHERE job_id=? AND state='pending'",
                (str(exc)[:240], int(time.time()), int(job_id)),
            )
            connection.commit()
        return "pending"
    with closing(jdb()) as connection:
        _ensure_breakdown_refund_table(connection)
        connection.execute(
            "UPDATE breakdown_partial_refunds SET state='refunded',attempts=attempts+1,"
            " last_error=NULL,updated_at=? WHERE job_id=? AND state='pending'",
            (int(time.time()), int(job_id)),
        )
        connection.commit()
    return "refunded"


def retry_breakdown_refunds(limit=100):
    with closing(jdb()) as connection:
        _ensure_breakdown_refund_table(connection)
        rows = connection.execute(
            "SELECT job_id FROM breakdown_partial_refunds WHERE state='pending'"
            " ORDER BY updated_at ASC LIMIT ?",
            (max(1, int(limit or 100)),),
        ).fetchall()
        connection.commit()
    recovered = 0
    for row in rows:
        if reconcile_breakdown_refund(row["job_id"]) == "refunded":
            recovered += 1
    return recovered


class AuthPointsError(Exception):
    def __init__(self, status, detail, data=None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.data = data or {}


def public_error_body(error, need=None):
    body = {"detail": getattr(error, "detail", str(error))}
    source = getattr(error, "data", {}) or {}
    for key in ("code", "membership_url", "membership_enforcement_enabled"):
        if source.get(key) is not None:
            body[key] = source[key]
    if need is not None and getattr(error, "status", 0) == 402:
        body["need"] = need
    return body


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

def deduct_points(username, amount, reason="", transaction_key=""):
    """预扣点。reason 落 points_audit，供对账。

    注意：三个服务都是「先扣点、后 INSERT jobs 行」，所以扣点这一刻还没有 job_id，
    reason 只能到 'job:<kind>' 这一层。退点时 job 行已存在，reason 会带上 '#<id>'。
    要让扣点也带 id，得把 INSERT 挪到扣点前面 —— 那样两步之间崩溃会留下一个没付钱的
    pending 任务被 worker 捡走白跑，代价大于收益，故不改。
    """
    amount = int(amount or 0)
    if amount <= 0:
        return get_points(username)
    payload = {"username": username, "amount": amount, "reason": reason}
    if transaction_key:
        payload["transaction_key"] = str(transaction_key)
    res = _auth_points_request("/api/auth/points/deduct", payload)
    return int(res.get("points") or 0)

def refund_points(username, amount, reason="", transaction_key=""):
    amount = int(amount or 0)
    if amount <= 0:
        return get_points(username)
    payload = {"username": username, "amount": amount, "reason": reason}
    if transaction_key:
        payload["transaction_key"] = str(transaction_key)
    res = _auth_points_request("/api/auth/points/refund",
                               payload)
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
        refunded = int(row["refunded"] or 0) == 1
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
