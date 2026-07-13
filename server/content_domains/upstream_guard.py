# -*- coding: utf-8 -*-
"""上游额度熔断器：上游没钱了，就别再让用户提交了。

## 为什么需要它 —— 告警拦不住用户

余额哨兵（scripts/balance_sentinel.py）每 10 分钟查一次各家余额，低于阈值就往飞书群里报。
它是有效的：近 14 天它确实为 WaveSpeed 报过警。

**但告警只叫醒了我们，拦不住用户。** 从「余额见底」到「有人看到告警并充上钱」这段时间里，
用户照样点生成、照样被扣点、照样等几分钟，然后看到一句天书：

    "视频接口失败: HTTP 400 {"code":400,"message":"积分余额不足，请先充值"}"      × 25
    "WaveSpeed接口失败: HTTP 400 {"code":400,"message":"Insufficient credits..."}" × 23

近 14 天 **48 条**任务是这么死的 —— 纯粹的运营事故，零技术含量，但用户体感是「这网站又崩了」。

## 做法：用【上游自己的拒绝】当信号，而不是猜余额

不去各家查余额（很多渠道根本没有余额 API，比如果肉/泽龙），而是看**它们刚刚是不是在因为
没钱而拒绝我们**：

    某个功能，最近 30 分钟内
      * 有 ≥2 条任务因为「余额不足」被上游拒绝
      * 且期间【没有任何一条成功】
    → 判定该功能的上游没额度了 → 新的提交【当场拒掉】，不扣点、不排队、不让用户等

一旦有一条成功（说明充上钱了），熔断自动解除 —— 不需要人工干预，也不需要重启。

## ⚠️ 必须 fail-open

这是个监控性质的组件。它自己出任何问题（查库失败、表结构变了、逻辑抛异常），都必须
**放行**。绝不能因为一个熔断器把整站的生成堵死 —— 那比它想防的问题严重得多。
所以整个判定包在 try/except 里，任何异常一律返回「没熔断」。
"""

import re
import time

from .core import closing, jdb

try:
    import func_names as _func_names          # 生产：content_api.py 直接跑，server/ 是 sys.path[0]
except ModuleNotFoundError:                   # 测试：以包的形式 import server.content_domains.*
    from .. import func_names as _func_names

# 各家「没钱了」的说法五花八门 —— 这是从线上真实报错里抄出来的。
# 新接一家渠道，第一次撞到余额不足时，把它的措辞加进来。
BALANCE_EXHAUSTED_RE = re.compile(
    r"余额不足|积分.{0,4}不足|额度.{0,4}不足|请先充值|请充值"
    r"|insufficient\s+(credits?|balance|funds)|top\s*up\s+your\s+account",
    re.I,
)

WINDOW_SECONDS = int(30 * 60)   # 只看最近 30 分钟 —— 再久就把「已经充过钱」的旧事故也算进来了
MIN_HITS = 2                    # 至少 2 条，避免一次偶发的 400 就把功能熔断
SCAN_LIMIT = 12                 # 每个功能最多回看这么多条终态任务


def _func_key(kind, payload):
    """熔断的粒度 = 用户看到的功能名（果肉/豆姐/欧米 分开，五个作图引擎分开）。

    复用 func_names —— 和运营后台的日志、统计、用户消费明细是同一份映射。
    一家渠道没钱，不该把别家一起熔断。
    """
    return _func_names.func_name(kind, payload)


def _job_payload(raw):
    import json
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        # payload 只取了前缀，截断的 JSON 解析不了 —— 这里不做正则兜底：
        # 兜底失败最多是把功能名认成上一级（例如「作图」而不是「作图 · 泽龙2生图」），
        # 熔断粒度变粗一点，不会误伤到别的渠道。
        return {}


def exhausted_reason(kind, payload):
    """这个功能的上游是不是没额度了？是 → 返回给用户看的话；不是 → None。

    ⚠️ fail-open：任何异常都返回 None（放行）。
    """
    try:
        key = _func_key(kind, payload)
        since = int(time.time()) - WINDOW_SECONDS
        with closing(jdb()) as c:
            rows = c.execute(
                # 按【时间】倒序，不是按 id —— 我们要的是「最近的」，而不是「id 最大的」。
                # 生产里两者恰好同序，但那是巧合，不是语义。
                """SELECT status, error, substr(payload, 1, 4096) AS payload
                   FROM jobs
                   WHERE kind = ? AND created_at >= ? AND status IN ('done', 'error')
                   ORDER BY created_at DESC, id DESC LIMIT ?""",
                (kind, since, SCAN_LIMIT * 4),
            ).fetchall()

        hits = 0
        seen = 0
        for r in rows:
            # 同一个 kind 下可能有多个渠道（xiaole_video 有果肉/豆姐/欧米）—— 只看同一个功能的
            if _func_key(kind, _job_payload(r["payload"])) != key:
                continue
            seen += 1
            if seen > SCAN_LIMIT:
                break
            if r["status"] == "done":
                return None          # 期间有成功的 → 充上钱了，熔断解除
            if BALANCE_EXHAUSTED_RE.search(r["error"] or ""):
                hits += 1
                if hits >= MIN_HITS:
                    return ("「%s」的上游额度已用尽，我们正在处理。"
                            "请稍后再试，或先换一个引擎。（未扣点）" % key)
        return None
    except Exception as e:
        # 熔断器自己挂了，绝不能把整站生成堵死
        print("[upstream_guard] 判定失败，放行: %s" % str(e)[:120], flush=True)
        return None
