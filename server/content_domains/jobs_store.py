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
import uuid
from contextlib import closing


def refund_transaction_key(job_id, username=""):
    """跨 content/auth 重试时保持稳定的任务退款键。"""
    return "job-refund:%s:%d" % (str(username or "unknown")[:64], int(job_id))


class PaidJobInsertError(Exception):
    def __init__(self, compensation, submission_ref):
        super().__init__("paid job insert failed")
        self.compensation = compensation
        self.submission_ref = submission_ref


class PaidJobDeductError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = int(status or 500)
        self.detail = str(detail or "点数扣除失败")


def public_dict(row, phase=None):
    data = {key: row[key] for key in (
        "id", "kind", "username", "cost", "status", "result", "error", "created_at", "updated_at")}
    data["refunded"] = int(row["refunded"] or 0) == 1 if "refunded" in row.keys() else False
    if data.get("result"):
        try:
            data["result"] = json.loads(data["result"])
        except Exception:
            pass
    terminal_phase = {"done": "done", "error": "failed", "failed": "failed"}.get(data["status"])
    if terminal_phase is not None or phase is not None:
        data["phase"] = terminal_phase or phase
    return data


def ensure_owner_column(jdb):
    """保证 jobs.owner 存在（#511）。三个服务启动时各调一次，谁先起谁建，与部署顺序无关。

    没有这列时，content 的 pending 重排/孤儿回收会把 imggen、leadgen 的任务当成自己的：
    重排会用 content 的处理器去跑别家的 payload，重启回收会把别家正在飞的任务判失败退点。
    历史行 owner 为 NULL —— 那时只有 content 会留 pending/被回收，故 content 侧用
    COALESCE(owner,'content') 把它们仍归自己，语义与建列前完全一致。
    """
    with closing(jdb()) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        if "owner" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN owner TEXT")
            c.commit()


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
                """UPDATE jobs SET status='error', error=?, updated_at=?,
                   refunded=CASE WHEN COALESCE(cost,0)>0 AND COALESCE(refunded,0)=0 THEN 2 ELSE refunded END
                   WHERE id=? AND status IN (%s)""" % holes,
                (str(error or "")[:300], now, job_id) + tuple(from_states))
        c.commit()
        return cur.rowcount >= 1


VIDEO_NOTIFICATION_KINDS = {"video", "tryon", "xiaole_video", "sora_video", "cinematic"}


def ensure_video_notification_outbox(jdb):
    with closing(jdb()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS video_notification_outbox(
            job_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_until INTEGER NOT NULL DEFAULT 0,
            next_retry_at INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            sent_at INTEGER
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_video_notify_ready ON video_notification_outbox(status,next_retry_at,job_id)")
        c.commit()


def set_done_with_video_outbox(jdb, job_id, username, kind, result=None, from_states=("running",)):
    """Atomically win jobs.done and enqueue only supported video notifications."""
    if kind not in VIDEO_NOTIFICATION_KINDS:
        return set_terminal(jdb, job_id, "done", result=result, from_states=from_states)
    ensure_video_notification_outbox(jdb)
    now = int(time.time())
    holes = ",".join("?" * len(from_states))
    with closing(jdb()) as c:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            "UPDATE jobs SET status='done', result=?, updated_at=? WHERE id=? AND status IN (%s)" % holes,
            (json.dumps(result, ensure_ascii=False), now, job_id) + tuple(from_states),
        )
        if cur.rowcount:
            c.execute(
                """INSERT OR IGNORE INTO video_notification_outbox(
                   job_id,username,kind,status,created_at,updated_at)
                   VALUES(?,?,?,'pending',?,?)""",
                (int(job_id), str(username or ""), str(kind), now, now),
            )
        c.commit()
        return cur.rowcount >= 1


def set_terminal_with_video_outbox(jdb, job_id, status, result=None, error=None, from_states=("running",)):
    if status != "done":
        return set_terminal(jdb, job_id, status, result, error, from_states)
    with closing(jdb()) as c:
        row = c.execute("SELECT username,kind FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return set_terminal(jdb, job_id, status, result, error, from_states)
    return set_done_with_video_outbox(
        jdb, job_id, row["username"], row["kind"], result, from_states)


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
    """确认待退款任务：2=待确认，1=Auth 已确认，0=历史未知/未发起。

    refund(username, cost) 只有在幂等 Auth 明确确认后才返回真；未知结果保持 2。
    """
    try:
        cost = int(cost or 0)
    except (TypeError, ValueError):
        cost = 0
    if cost <= 0:
        return False
    with closing(jdb()) as c:
        row = c.execute("SELECT 1 FROM jobs WHERE id=? AND status='error' AND refunded=2",
                        (job_id,)).fetchone()
    if not row:
        return False
    try:
        refunded = bool(refund(username, cost))
    except Exception:
        refunded = False
    if refunded:
        with closing(jdb()) as c:
            cur = c.execute("UPDATE jobs SET refunded=1,updated_at=? WHERE id=? AND refunded=2",
                            (int(time.time()), job_id))
            c.commit()
            return cur.rowcount > 0 or bool(c.execute(
                "SELECT 1 FROM jobs WHERE id=? AND refunded=1", (job_id,)).fetchone())
    with closing(jdb()) as c:
        c.execute("UPDATE jobs SET updated_at=? WHERE id=? AND refunded=2",
                  (int(time.time()), job_id))
        c.commit()
    return False


def retry_failed_refunds(jdb, refund_job, limit=100):
    """轮转补扫明确处于待确认态的退款；历史 refunded=0 永远不自动处理。"""
    with closing(jdb()) as c:
        import sqlite3
        c.row_factory = sqlite3.Row
        has_attempts = bool(c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='short_drama_charge_attempts'"
        ).fetchone())
        attempt_exclusion = (
            "AND NOT EXISTS (SELECT 1 FROM short_drama_charge_attempts a WHERE a.job_id=jobs.id)"
            if has_attempts else ""
        )
        rows = c.execute(
            """SELECT id,username,cost FROM jobs
               WHERE status='error' AND refunded=2 AND COALESCE(cost,0)>0
               %s ORDER BY updated_at ASC,id ASC LIMIT ?""" % attempt_exclusion,
            (max(1, int(limit or 100)),),
        ).fetchall()
    recovered = 0
    for row in rows:
        if refund_job(row["id"], row["username"], row["cost"]):
            recovered += 1
    return recovered


def _compensate_failed_insert(jdb, refund, username, cost, kind, submission_ref, error, owner):
    if int(cost or 0) <= 0:
        return "refunded"
    fallback_key = "job-insert-refund:%s" % submission_ref
    reason = "job:%s:insert_failed submit:%s" % (kind, submission_ref)
    now = int(time.time())
    payload = json.dumps({"_submission_ref": submission_ref}, ensure_ascii=False)
    try:
        with closing(jdb()) as c:
            cur = c.execute(
                """INSERT INTO jobs(kind,username,cost,status,payload,error,created_at,updated_at,owner,refunded)
                   VALUES(?,?,?,'error',?,?,?,?,?,2)""",
                (kind, username, int(cost), payload,
                 "任务创建失败，退款待确认: %s" % str(error or "")[:180], now, now, owner),
            )
            c.commit()
            retry_job_id = cur.lastrowid
    except Exception as record_error:
        try:
            if refund(username, cost, reason, transaction_key=fallback_key) is False:
                raise RuntimeError("refund not confirmed")
            return "refunded"
        except Exception as refund_error:
            print("[points-critical] job insert/refund record both failed submit=%s user=%s cost=%s "
                  "insert=%s refund=%s record=%s" % (
                      submission_ref, username, cost, str(error)[:120],
                      str(refund_error)[:120], str(record_error)[:120]), flush=True)
            return "untracked"
    transaction_key = refund_transaction_key(retry_job_id, username)
    confirmed = refund_once(
        jdb, retry_job_id, username, cost,
        lambda u, c: refund(u, c, reason, transaction_key=transaction_key))
    return "refunded" if confirmed else "queued"


def create_paid_jobs(jdb, deduct, refund, kind, username, items, owner, reason_kind="",
                     before_commit=None, charge_transaction_key=""):
    """一次预扣并原子写入一个或多个任务；失败补偿只维护这一处。"""
    items = [(int(cost or 0), payload) for cost, payload in items]
    total = sum(cost for cost, _ in items)
    submission_ref = uuid.uuid4().hex
    reason = "job:%s submit:%s" % (reason_kind or kind, submission_ref)
    points_left = (deduct(username, total, reason, charge_transaction_key)
                   if charge_transaction_key else deduct(username, total, reason))
    now = int(time.time())
    try:
        with closing(jdb()) as c:
            try:
                job_ids = []
                for cost, payload in items:
                    cur = c.execute(
                        "INSERT INTO jobs(kind,username,cost,payload,created_at,updated_at,owner) VALUES(?,?,?,?,?,?,?)",
                        (kind, username, cost, json.dumps(payload, ensure_ascii=False), now, now, owner),
                    )
                    job_ids.append(cur.lastrowid)
                if before_commit is not None:
                    before_commit(c, tuple(job_ids))
                c.commit()
                return job_ids, points_left
            except Exception:
                c.rollback()
                raise
    except Exception as error:
        state = _compensate_failed_insert(
            jdb, refund, username, total, kind, submission_ref, error, owner)
        raise PaidJobInsertError(state, submission_ref) from error


def create_paid_job(jdb, deduct, refund, kind, username, cost, payload, owner,
                    before_commit=None, charge_transaction_key=""):
    batch_callback = None
    if before_commit is not None:
        batch_callback = lambda connection, job_ids: before_commit(connection, job_ids[0])
    job_ids, points_left = create_paid_jobs(
        jdb, deduct, refund, kind, username, [(cost, payload)], owner,
        before_commit=batch_callback, charge_transaction_key=charge_transaction_key)
    return job_ids[0], points_left


def create_job_after_charge(jdb, kind, username, cost, payload, owner, before_commit=None):
    """Insert a job whose durable charge attempt was already reconciled.

    This intentionally has no billing side effect.  Its caller owns the persisted
    compensation state and must record refund intent before contacting Auth.
    """
    now = int(time.time())
    with closing(jdb()) as connection:
        try:
            cursor = connection.execute(
                "INSERT INTO jobs(kind,username,cost,payload,created_at,updated_at,owner) "
                "VALUES(?,?,?,?,?,?,?)",
                (kind, username, int(cost), json.dumps(payload, ensure_ascii=False),
                 now, now, owner),
            )
            job_id = int(cursor.lastrowid)
            if before_commit is not None:
                before_commit(connection, job_id)
            connection.commit()
            return job_id
        except Exception:
            connection.rollback()
            raise
