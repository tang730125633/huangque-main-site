# -*- coding: utf-8 -*-
"""jobs 表的状态机与退点幂等 —— 三个服务共用的安全网。

content_jobs.db 的 jobs 表被三个进程共写：
    content_api  (8096)  image/copy/audio/video/tryon/xiaole_video
    leadgen_api  (8100)  collect/leads
    imggen_api   (8101)  Nano Banana 作图
而 reaper（超时回收）只在 content_api 里跑。

这意味着任何一个服务写终态时不做 CAS，都会出现：
    reaper 在第 360s 判超时 → 退点 → worker 在第 3686s 跑完 → 无条件写 done
    → 用户既拿到结果又拿回点数（线上 jobs 1170/1164/1118…共 21 条，280 点）

这段逻辑此前在三个文件里各抄了一份，连注释措辞都一样，只有最后调用的退点函数不同。
同一个资金 bug 因此在 core → imggen → leadgen 上依次踩过三次。抽到这里统一维护。

本模块只依赖标准库，不 import core —— 三个服务都能安全 import（leadgen/imggen 本来
就在 `from content_domains import cos / assets_store`）。
"""
import json
import time
from contextlib import closing


def set_terminal(jdb, job_id, status, result=None, error=None, from_states=("running",)):
    """CAS 抢终态：仅当当前状态在 from_states 内才迁移，返回是否抢到(rowcount>=1)。

    败者不写状态、不做副作用 —— 谁先抢到谁定终态，reaper 与 worker 之间无竞态。

    from_states 默认只认 running（与 reaper 竞争的正常路径）。run_job 的 except 分支要传
    ("pending","running")：若异常发生在把任务改成 running 之前(如 SQLite 锁冲突)，任务还停在
    pending，只认 running 会让 CAS 失败 → 不退点 → 预扣的点永久丢失，而 reaper 只扫 running、
    从不回收 pending，没人能救它。

    jdb: 返回 sqlite3 连接的工厂函数（各服务连的是同一个 content_jobs.db）。
    """
    now = int(time.time())
    holes = ",".join("?" * len(from_states))
    with closing(jdb()) as c:
        if status == "done":
            cur = c.execute(
                "UPDATE jobs SET status='done', result=?, updated_at=? WHERE id=? AND status IN (%s)" % holes,
                (json.dumps(result, ensure_ascii=False), now, job_id) + tuple(from_states))
        else:
            cur = c.execute(
                "UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=? AND status IN (%s)" % holes,
                (str(error or "")[:300], now, job_id) + tuple(from_states))
        c.commit()
        return cur.rowcount >= 1


def claim_running(jdb, job_id):
    """CAS 认领：只有 pending 才能被本次执行接管。返回是否抢到。

    防同一个 job 被两个 worker 跑两遍（重启恢复 + 正常入队可能撞车）。
    """
    with closing(jdb()) as c:
        cur = c.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=? AND status='pending'",
                        (int(time.time()), job_id))
        c.commit()
        return cur.rowcount >= 1


def refund_once(jdb, job_id, username, cost, refund):
    """退点 job 级幂等：refunded 列 CAS，仅第一次真正加回点数(#187)。

    先置位再退点，保证「最多退一次」；但退点若真的失败，必须把 refunded 放回 0，
    否则这条 job 被永久标记「已退过」，用户的点再也拿不回来。

    refund(username, cost) -> 真值表示退点成功。调用方各自决定怎么退：
        content_api  points.safe_refund_points（吞异常，永远算成功）
        imggen_api   auth 的 /api/auth/points/refund（无兜底，失败要回滚）
        leadgen_api  auth 优先 + 直写 users.db 兜底
    """
    try:
        cost = int(cost or 0)
    except (TypeError, ValueError):
        cost = 0
    if cost <= 0:
        return False
    with closing(jdb()) as c:
        # 双重保险：仅当终态确为 error 且尚未退过，才置位并退点
        cur = c.execute("UPDATE jobs SET refunded=1 WHERE id=? AND refunded=0 AND status='error'", (job_id,))
        c.commit()
        if cur.rowcount < 1:
            return False   # 已退过 / 非 error 终态，跳过
    if refund(username, cost):
        return True
    with closing(jdb()) as c:   # 退点没成功，把幂等锁放回去，留给下次重试
        c.execute("UPDATE jobs SET refunded=0 WHERE id=? AND refunded=1", (job_id,))
        c.commit()
    return False
