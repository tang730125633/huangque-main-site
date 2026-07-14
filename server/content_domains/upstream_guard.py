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

# ============ 两类「提交前就该拦住」的失败 ============
#
# 共同点：它们都是【上游当前不接活】，重试没用、等下去也没用，而用户已经被扣了点、
# 已经等了几分钟。与其让他等到最后看一句天书，不如提交这一刻就拒掉。

# 一、上游账户没钱。各家措辞完全不同 —— 从线上真实报错里抄的。
#     新接一家渠道，第一次撞到余额不足时，把它的措辞加进来。
BALANCE_EXHAUSTED_RE = re.compile(
    r"余额不足|积分.{0,4}不足|额度.{0,4}不足|请先充值|请充值"
    r"|insufficient\s+(credits?|balance|funds)|top\s*up\s+your\s+account",
    re.I,
)

# 二、这个【比例/尺寸】当前没有可用渠道。
#
# ⚠️ 这【不是】一个静态的支持矩阵 —— 别去前端写死「果肉只支持 16:9」。
# 上游是个聚合中转，报的是「【当前】暂无支持该视频参数的【可用渠道】」，渠道是动态上下线的。
# 线上证据：grok + 9:16 + 720p 有 5 成 0 败，而另一批 grok 9:16 却 27 条全挂。
# 同一个比例，不同时间，结果不同。写死矩阵会把本来能用的组合也禁掉。
#
# 所以按「最近有没有连续挂」来判断，和余额熔断同一套机制 —— 只是键里多了个比例。
RATIO_UNAVAILABLE_RE = re.compile(
    r"无可用渠道|当前模型暂无|暂无支持该视频参数|渠道不支持当前视频尺寸"
    r"|仅部分比例可用|不支持参数.{0,3}aspect_ratio",
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


def _ratio(payload):
    return str((payload or {}).get("ratio") or (payload or {}).get("aspect_ratio") or "").strip()


def exhausted_reason(kind, payload):
    """这个功能（或这个功能的这个比例）现在能不能提交？

    不能 → 返回给用户看的话；能 → None。

    两类熔断共用一次扫描：
      * 余额熔断   键 = 功能名           （果肉没钱，不影响欧米）
      * 比例熔断   键 = 功能名 + 比例     （果肉的 9:16 不可用，不影响果肉的 16:9）

    ⚠️ fail-open：任何异常都返回 None（放行）。
    """
    try:
        key = _func_key(kind, payload)
        ratio = _ratio(payload)
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

        # 两个熔断的【解除条件不一样】，必须分开跟踪：
        #   余额：任意一条成功就证明账户有钱了 —— 不管它是什么比例
        #   比例：只有【同比例】的成功才证明这个比例的渠道活了
        #         （果肉的 16:9 成功了，完全不代表果肉的 9:16 也有渠道）
        money_hits, money_cleared = 0, False
        ratio_hits, ratio_cleared = 0, not ratio   # payload 里没有比例 → 这个维度不适用
        seen = 0

        for r in rows:
            if money_cleared and ratio_cleared:
                break
            pl = _job_payload(r["payload"])
            # 同一个 kind 下可能有多个渠道（xiaole_video 有果肉/豆姐/欧米）—— 只看同一个功能的
            if _func_key(kind, pl) != key:
                continue
            seen += 1
            if seen > SCAN_LIMIT:
                break

            same_ratio = bool(ratio) and _ratio(pl) == ratio
            if r["status"] == "done":
                money_cleared = True
                if same_ratio:
                    ratio_cleared = True
                continue

            err = r["error"] or ""
            if not money_cleared and BALANCE_EXHAUSTED_RE.search(err):
                money_hits += 1
                if money_hits >= MIN_HITS:
                    return ("「%s」的上游额度已用尽，我们正在处理。"
                            "请稍后再试，或先换一个引擎。（未扣点）" % key)
            elif not ratio_cleared and same_ratio and RATIO_UNAVAILABLE_RE.search(err):
                ratio_hits += 1
                if ratio_hits >= MIN_HITS:
                    return ("「%s」当前没有支持 %s 的可用渠道（上游渠道是动态上下线的）。"
                            "请换一个画面比例，或稍后再试。（未扣点）" % (key, ratio))
        return None
    except Exception as e:
        # 熔断器自己挂了，绝不能把整站生成堵死
        print("[upstream_guard] 判定失败，放行: %s" % str(e)[:120], flush=True)
        return None
