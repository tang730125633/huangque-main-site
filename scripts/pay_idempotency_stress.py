#!/usr/bin/env python3
"""支付回调幂等压测（STAGING ONLY）。

老板 09-16 晚点名：auth 域（身份+账本）切写生产 PostgreSQL 后，充值/退点等账务
路径写 ``ledger`` schema，同一笔支付回调重放或并发重复到达时必须「只入账一次」。

本脚本在 staging（``huangque_staging``）上驱动**真实代码路径**：

* ``auth_server.process_virtual_pay_message``  → ``confirm_virtual_pay_order``（虚拟支付加点）
* ``auth_server.confirm_virtual_pay_order``    → 客户端主动确认/兜底查单走的同一个函数
* ``auth_server.review_recharge_order``        → 微信支付回调与管理员审批共用的入账边界
* ``auth_server.deduct_points`` / ``refund_points`` → 内部点数接口（带 transaction_key 幂等键）

微信网络调用被替换为本地假响应（``code_to_session`` / ``query_order`` /
``notify_provide_goods``），因此压测不发任何真实支付请求、不碰真实密钥；
``pay_env`` 固定为沙箱枚举 1。

安全门禁（fail closed，不可绕过）：
* 连接库名必须**精确等于** ``--expect-database``（默认 ``huangque_staging``），否则立刻退出；
* 只允许 localhost / unix socket 连接（禁止远程库）；
* 所有测试数据用 ``zzpaystress_`` 前缀，跑完自动清理，可重复执行。

用法（服务器 dapeng-server，以 postgres 身份走 unix socket peer 免密）::

    sudo -u postgres env HQ_DATABASE_URL=postgresql:///huangque_staging \\
        HQ_AUTH_DB_POOL_MAX=4 /usr/bin/python3 scripts/pay_idempotency_stress.py \\
        --scenario all --replays 20 --workers 6 --out /tmp/paystress.json

退出码：0 = 全部场景 PASS；1 = 有场景 FAIL（发现幂等漏洞）；2 = 门禁/环境错误。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(ROOT, "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

EXPECTED_DATABASE = "huangque_staging"
USER_PREFIX = "zzpaystress_"
BASE_POINTS = 1000          # 压测账号建档余额：账实不变量的基准
STRESS_ACTOR = "zz-stress-admin"

_STATE = {}


# ---------------------------------------------------------------------------
# 引导：环境 + 微信假客户端
# ---------------------------------------------------------------------------

def _bootstrap_env() -> None:
    """压测进程环境：PG 权威开关 + 假微信凭证（绝不使用真实密钥/现网环境）。"""
    os.environ.setdefault("HQ_IDENTITY_STORE", "postgres")
    os.environ.setdefault("HQ_LEDGER_STORE", "postgres")
    os.environ.setdefault("WX_MP_APPID", "wxstressappid000000")
    os.environ.setdefault("WX_MP_APPSECRET", "stress-app-secret")
    os.environ.setdefault("WX_VIRTUAL_PAY_OFFER_ID", "1450000000")
    os.environ.setdefault("WX_VIRTUAL_PAY_APP_KEY_PROD", "stress-app-key-prod")
    os.environ.setdefault("WX_VIRTUAL_PAY_APP_KEY_SANDBOX", "stress-app-key-sandbox")
    os.environ.setdefault("WX_VIRTUAL_PAY_ENV", "1")      # 1 = 沙箱枚举，压测绝不取现网密钥
    os.environ.setdefault("HQ_AUTH_DB_POOL_MAX", "4")


def auth():
    """惰性 import auth_server 并把微信客户端换成假实现。"""
    if "auth" not in _STATE:
        _bootstrap_env()
        import auth_server

        _patch_wechat(auth_server)
        _STATE["auth"] = auth_server
    return _STATE["auth"]


def _patch_wechat(auth_server) -> None:
    vpay = auth_server.wechat_vpay

    def _code_to_session(code, *args, **kwargs):
        return {"openid": "stress-openid", "session_key": "S" * 24}

    def _query_order(openid, order_id, env=None):
        row = _fetch_one("SELECT amount_fen FROM virtual_pay_orders WHERE order_id=?", (order_id,))
        if not row:
            return {"order": {}}
        now = int(time.time())
        return {"order": {
            "order_id": order_id,
            "status": 2,                       # 2 = 已支付
            "order_fee": int(row["amount_fen"] or 0),
            "paid_time": now,
            "wx_order_id": "WXSTRESS" + order_id,
            "wxpay_order_id": "PAYSTRESS" + order_id,
        }}

    def _notify_provide_goods(order_id, env=None):
        return {"errcode": 0, "errmsg": "ok"}

    vpay.code_to_session = _code_to_session
    vpay.query_order = _query_order
    vpay.notify_provide_goods = _notify_provide_goods


# ---------------------------------------------------------------------------
# 数据库helpers（走 auth_server.db() → auth_store PG 适配层，与生产同一条路）
# ---------------------------------------------------------------------------

def _query(sql, params=()):
    c = auth().db()
    try:
        return [dict(row) for row in c.execute(sql, params).fetchall()]
    finally:
        c.close()


def _fetch_one(sql, params=()):
    rows = _query(sql, params)
    return rows[0] if rows else None


def _exec(sql, params=()):
    c = auth().db()
    try:
        cursor = c.execute(sql, params)
        rowcount = cursor.rowcount
        c.commit()
        return rowcount
    finally:
        c.close()


def _database_name() -> str:
    row = _fetch_one("SELECT current_database() AS db")
    return str(row["db"]) if row else ""


def require_staging_target(expected: str) -> str:
    url = (os.environ.get("HQ_DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("REFUSED: HQ_DATABASE_URL 未配置")
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower()
    if host not in ("", "localhost", "127.0.0.1", "::1"):
        raise SystemExit(
            "REFUSED: 数据库主机 %r 不是本机，压测只允许 localhost/unix socket" % host)
    name = _database_name()
    if name != expected:
        raise SystemExit(
            "REFUSED: 当前连接库是 %r，压测只允许 %r（生产库 huangque 绝不触碰）" % (name, expected))
    return name


# ---------------------------------------------------------------------------
# 测试数据（zzpaystress_ 前缀，跑完清理）
# ---------------------------------------------------------------------------

def new_username(tag: str) -> str:
    _STATE["seq"] = _STATE.get("seq", 0) + 1
    return "%s%s_%d_%s" % (USER_PREFIX, _STATE["run"], _STATE["seq"], tag)


def ensure_user(username: str, points: int) -> None:
    _exec(
        """INSERT INTO users(
               username,pw_hash,pw_salt,display_name,points,role,must_change,
               created_at,account_status,membership_tier,card_initial_password
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (username, "x" * 64, "s" * 16, username, int(points), "member", 0,
         "2026-09-16 00:00:00", "active", "", 0),
    )


def make_virtual_order(username: str, package_id: str = "points_1000"):
    """走真实下单代码 create_virtual_pay_order（含定价/折扣/INSERT）。"""
    result, err = auth().create_virtual_pay_order(username, package_id, "stress-wx-code")
    if err or not result:
        raise RuntimeError("create_virtual_pay_order failed: %s" % err)
    return result["order"]


def make_recharge_order(username: str, points: int = 100, amount: float = 1.0):
    order, err = auth().create_recharge_order(username, amount, points, note="zz-paystress")
    if err or not order:
        raise RuntimeError("create_recharge_order failed: %s" % err)
    return order


def snapshot(username: str) -> dict:
    user = _fetch_one("SELECT points FROM users WHERE username=?", (username,))
    rows = _query(
        """SELECT id,delta,before_points,after_points,reason,transaction_key,created_at
             FROM points_audit WHERE username=? ORDER BY id""",
        (username,),
    )
    return {
        "points": int(user["points"] or 0) if user else None,
        "audit": rows,
    }


def order_row(order_id: str) -> dict:
    return _fetch_one("SELECT * FROM virtual_pay_orders WHERE order_id=?", (order_id,)) or {}


def recharge_row(order_id: str) -> dict:
    return _fetch_one("SELECT * FROM recharge_orders WHERE order_id=?", (order_id,)) or {}


def table_counts() -> dict:
    out = {}
    for label, sql in (
        ("identity.users", "SELECT count(*) AS n FROM users"),
        ("ledger.points_audit", "SELECT count(*) AS n FROM points_audit"),
        ("ledger.recharge_orders", "SELECT count(*) AS n FROM recharge_orders"),
        ("ledger.virtual_pay_orders", "SELECT count(*) AS n FROM virtual_pay_orders"),
    ):
        out[label] = int(_fetch_one(sql)["n"])
    return out


def purge_leftovers() -> dict:
    like = USER_PREFIX + "%"
    removed = {}
    for table, label in (
        ("points_audit", "ledger.points_audit"),
        ("virtual_pay_orders", "ledger.virtual_pay_orders"),
        ("recharge_orders", "ledger.recharge_orders"),
        ("membership_recharge_records", "ledger.membership_recharge_records"),
        ("users", "identity.users"),
    ):
        removed[label] = _exec("DELETE FROM %s WHERE username LIKE ?" % table, (like,))
    return removed


# ---------------------------------------------------------------------------
# 断言 / 证据
# ---------------------------------------------------------------------------

def ledger_evidence(label: str, base_points: int, before: dict, after: dict, reasons,
                    expected_delta=None, expected_rows: int = 1) -> tuple:
    """返回 (observations, checks)。

    base_points：该账号建档时的余额（本脚本创建用户时给定），所以
    「sum(全部流水 delta) == 当前余额 - base_points」是该账号的账实不变量。
    reasons：用于在流水里定位「本次压测产生的记账行」的 reason 子串。
    expected_delta：本次场景**应当**发生的余额净变动（None 表示跳过该校验）。
    expected_rows：本次场景**应当**产生的记账行数（单笔支付=1，同用户多笔=2），
    超过它就是重复入账。
    """
    rows = after["audit"]
    needles = reasons if isinstance(reasons, (list, tuple)) else [reasons]
    hits = [r for r in rows if any(n in (r["reason"] or "") for n in needles)]

    chain_ok, breaks, cursor = True, [], None
    for row in rows:
        if cursor is None:
            cursor = int(row["before_points"])
        if int(row["before_points"]) != cursor or \
                int(row["after_points"]) != cursor + int(row["delta"]):
            chain_ok = False
            breaks.append(int(row["id"]))
        cursor = int(row["after_points"])

    observations = {
        "label": label,
        "balance_base": base_points,
        "balance_before": before["points"],
        "balance_after": after["points"],
        "balance_delta": (after["points"] - before["points"])
        if before["points"] is not None and after["points"] is not None else None,
        "expected_balance_delta": expected_delta,
        "ledger_rows_total": len(rows),
        "ledger_rows_for_this_payment": len(hits),
        "expected_ledger_rows": expected_rows,
        "ledger_delta_sum_for_this_payment": sum(int(r["delta"]) for r in hits),
        "ledger_delta_sum_all": sum(int(r["delta"]) for r in rows),
        "ledger_chain_ok": chain_ok,
        "ledger_chain_break_ids": breaks[:10],
        "ledger_last_after_points": cursor,
        "ledger_rows_detail": [
            {"id": int(r["id"]), "delta": int(r["delta"]),
             "before": int(r["before_points"]), "after": int(r["after_points"]),
             "reason": (r["reason"] or "")[:60], "transaction_key": r["transaction_key"]}
            for r in hits
        ],
    }
    checks = {
        "ledger_chain_continuous": chain_ok,
        "ledger_reaches_balance": cursor == after["points"],
        "ledger_sum_matches_balance": (
            observations["ledger_delta_sum_all"] == after["points"] - base_points),
    }
    if expected_delta is not None:
        checks["balance_moved_expected"] = observations["balance_delta"] == expected_delta
    return observations, checks


def result(label: str, observations: dict, checks: dict, notes=None) -> dict:
    verdict = "PASS" if all(checks.values()) else "FAIL"
    return {
        "scenario": label,
        "verdict": verdict,
        "checks": {k: bool(v) for k, v in checks.items()},
        "observations": observations,
        "notes": notes or [],
    }


def summarize_worker_output(block: str) -> dict:
    stats = {"workers": 0, "calls": 0, "outcomes": [], "errors": []}
    for line in (block or "").splitlines():
        if not line.startswith("STRESS_WORKER "):
            continue
        try:
            payload = json.loads(line[len("STRESS_WORKER "):])
        except Exception:
            continue
        stats["workers"] += 1
        stats["calls"] += int(payload.get("calls") or 0)
        stats["errors"].extend(payload.get("errors") or [])
        for item in payload.get("outcomes") or []:
            text = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if len(stats["outcomes"]) < 12 and text not in stats["outcomes"]:
                stats["outcomes"].append(text)
    return stats


# ---------------------------------------------------------------------------
# worker 模式（独立进程，用于跨进程并发）
# ---------------------------------------------------------------------------

def _act_virtual_notify(a, args):
    return a.process_virtual_pay_message({
        "Event": "xpay_goods_deliver_notify",
        "order_id": args["order_id"],
    })


def _act_virtual_confirm(a, args):
    order, err = a.confirm_virtual_pay_order(args["username"], args["order_id"])
    return {"status": (order or {}).get("status"), "err": err}


def _act_recharge_approve(a, args):
    order, err = a.review_recharge_order(
        STRESS_ACTOR, args["order_id"], "approve", "zz-paystress",
        transaction_id=args.get("transaction_id") or "", pay_channel="wxpay",
    )
    return {"status": (order or {}).get("status"), "err": err}


def _act_recharge_refund(a, args):
    refund = {
        "refund_status": "SUCCESS",
        "amount": {"total": int(args["amount_fen"]), "refund": int(args["amount_fen"])},
        "transaction_id": args["transaction_id"],
    }
    order, err = a.refund_recharge_order(args["order_id"], refund)
    return {"status": (order or {}).get("status"), "err": err}


def _act_virtual_refund(a, args):
    return a.process_virtual_pay_message({
        "Event": "xpay_refund_notify",
        "order_id": args["order_id"],
    })


def _act_points_refund(a, args):
    points, err = a.refund_points(
        args["username"], int(args["amount"]), args["reason"],
        args["transaction_key"], apply_balance=True,
    )
    return {"points": (points or {}).get("points"), "err": err}


def _act_points_deduct(a, args):
    points, err = a.deduct_points(
        args["username"], int(args["amount"]), args["reason"],
        args["transaction_key"], apply_balance=True,
    )
    return {"points": (points or {}).get("points"), "err": err}


ACTIONS = {
    "virtual_notify": _act_virtual_notify,
    "virtual_confirm": _act_virtual_confirm,
    "recharge_approve": _act_recharge_approve,
    "recharge_refund": _act_recharge_refund,
    "virtual_refund": _act_virtual_refund,
    "points_refund": _act_points_refund,
    "points_deduct": _act_points_deduct,
}


def _barrier_wait(barrier_dir: str) -> None:
    if not barrier_dir:
        return
    with open(os.path.join(barrier_dir, "ready.%d" % os.getpid()), "w"):
        pass
    go = os.path.join(barrier_dir, "go")
    deadline = time.time() + 60
    while not os.path.exists(go):
        if time.time() > deadline:
            raise SystemExit("barrier timeout")
        time.sleep(0.0005)


def worker_main(payload: dict) -> int:
    _STATE["run"] = payload.get("run") or "w"
    _bootstrap_env()
    action = payload["action"]
    replays = max(1, int(payload.get("replays") or 1))
    if payload.get("pool_max"):
        os.environ["HQ_AUTH_DB_POOL_MAX"] = str(payload["pool_max"])
    a = auth()
    _barrier_wait(payload.get("barrier") or "")
    outcomes, errors = [], []
    for _ in range(replays):
        try:
            outcomes.append(ACTIONS[action](a, payload.get("args") or {}))
        except Exception as exc:                      # noqa: BLE001 —— 记录所有异常作为证据
            errors.append("%s: %s" % (type(exc).__name__, str(exc)[:200]))
    print("STRESS_WORKER " + json.dumps(
        {"action": action, "calls": replays, "outcomes": outcomes, "errors": errors},
        ensure_ascii=False, default=str), flush=True)
    return 0


# ---------------------------------------------------------------------------
# 并发编排：子进程 + 统一发令枪
# ---------------------------------------------------------------------------

def _spawn(ctx, payloads, pool_max=4):
    barrier = tempfile.mkdtemp(prefix="paystress-barrier-")
    script = os.path.abspath(__file__)
    env = os.environ.copy()
    env["HQ_AUTH_DB_POOL_MAX"] = str(pool_max)
    procs = []
    for payload in payloads:
        payload = dict(payload, barrier=barrier, run=_STATE["run"], pool_max=pool_max)
        procs.append(subprocess.Popen(
            [sys.executable, script, "--worker", json.dumps(payload, ensure_ascii=False)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            cwd=ROOT,
        ))
    deadline = time.time() + 60
    while time.time() < deadline:
        ready = [name for name in os.listdir(barrier) if name.startswith("ready.")]
        if len(ready) >= len(procs):
            break
        time.sleep(0.005)
    with open(os.path.join(barrier, "go"), "w"):
        pass
    chunks = []
    for proc in procs:
        out, err = proc.communicate(timeout=300)
        chunks.append((out or "") + ("\n[stderr] " + err if err else ""))
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

def sc_virtual_serial_replay(ctx) -> dict:
    """场景 A：同一笔虚拟支付回调串行重放 N 次（同一进程）。"""
    username = new_username("ser")
    ensure_user(username, 1000)
    order = make_virtual_order(username)
    before = snapshot(username)
    for _ in range(ctx["replays"]):
        auth().process_virtual_pay_message(
            {"Event": "xpay_goods_deliver_notify", "order_id": order["order_id"]})
    after = snapshot(username)
    row = order_row(order["order_id"])
    obs, checks = ledger_evidence("虚拟支付串行重放", BASE_POINTS, before, after,
                                  "微信虚拟支付: " + order["order_id"], int(order["points"]))
    obs.update({
        "replays": ctx["replays"],
        "order_points": int(order["points"]),
        "order_status": row.get("status"),
        "order_credited_at_set": bool(row.get("credited_at")),
        "order_delivered_at_set": bool(row.get("delivered_at")),
    })
    checks["credited_exactly_once"] = obs["ledger_rows_for_this_payment"] == 1
    checks["order_status_credited"] = row.get("status") == "credited"
    return result("A 虚拟支付回调串行重放", obs, checks)


def sc_virtual_thread_replay(ctx) -> dict:
    """场景 B：同一笔回调单进程多线程并发重放（进程内 RLock 应串行化）。"""
    username = new_username("thr")
    ensure_user(username, 1000)
    order = make_virtual_order(username)
    before = snapshot(username)
    threads, threads_n = [], ctx["threads"]
    start = threading.Barrier(threads_n)
    errors = []

    def _worker():
        start.wait()
        for _ in range(ctx["replays"]):
            try:
                auth().process_virtual_pay_message(
                    {"Event": "xpay_goods_deliver_notify", "order_id": order["order_id"]})
            except Exception as exc:                  # noqa: BLE001
                errors.append("%s: %s" % (type(exc).__name__, str(exc)[:160]))

    for _ in range(threads_n):
        threads.append(threading.Thread(target=_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    after = snapshot(username)
    row = order_row(order["order_id"])
    obs, checks = ledger_evidence("虚拟支付单进程多线程重放", BASE_POINTS, before, after,
                                  "微信虚拟支付: " + order["order_id"], int(order["points"]))
    obs.update({
        "threads": threads_n,
        "replays_per_thread": ctx["replays"],
        "total_calls": threads_n * ctx["replays"],
        "errors": errors[:10],
        "order_points": int(order["points"]),
        "order_status": row.get("status"),
    })
    checks["credited_exactly_once"] = obs["ledger_rows_for_this_payment"] == 1
    checks["no_worker_errors"] = not errors
    return result("B 虚拟支付单进程多线程重放", obs, checks)


def sc_virtual_process_replay(ctx) -> dict:
    """场景 C：同一笔回调跨进程并发重放（PG MVCC，进程内 RLock 不覆盖）。"""
    username = new_username("proc")
    ensure_user(username, 1000)
    order = make_virtual_order(username)
    before = snapshot(username)
    payloads = [{"action": "virtual_notify", "replays": ctx["replays"],
                 "args": {"order_id": order["order_id"]}}
                for _ in range(ctx["workers"])]
    block = _spawn(ctx, payloads, pool_max=2)
    stats = summarize_worker_output(block)
    after = snapshot(username)
    row = order_row(order["order_id"])
    obs, checks = ledger_evidence("虚拟支付跨进程并发重放", BASE_POINTS, before, after,
                                  "微信虚拟支付: " + order["order_id"], int(order["points"]))
    obs.update({
        "processes": ctx["workers"],
        "replays_per_process": ctx["replays"],
        "total_calls": stats["calls"],
        "worker_errors": stats["errors"][:10],
        "worker_outcomes": stats["outcomes"],
        "order_points": int(order["points"]),
        "order_status": row.get("status"),
    })
    checks["credited_exactly_once"] = obs["ledger_rows_for_this_payment"] == 1
    return result("C 虚拟支付跨进程并发重放", obs, checks)


def sc_virtual_cross_order_same_user(ctx) -> dict:
    """场景 D：同一用户两笔不同订单被跨进程同时入账（读-改-写丢更新探测）。"""
    username = new_username("cross")
    ensure_user(username, 1000)
    first = make_virtual_order(username, "points_1000")
    second = make_virtual_order(username, "points_2000")
    expected = int(first["points"]) + int(second["points"])
    before = snapshot(username)
    payloads = [
        {"action": "virtual_notify", "replays": ctx["replays"],
         "args": {"order_id": first["order_id"]}},
        {"action": "virtual_notify", "replays": ctx["replays"],
         "args": {"order_id": second["order_id"]}},
    ]
    block = _spawn(ctx, payloads, pool_max=2)
    stats = summarize_worker_output(block)
    after = snapshot(username)
    obs, checks = ledger_evidence(
        "同用户两笔订单跨进程并发入账", BASE_POINTS, before, after,
        ["微信虚拟支付: " + first["order_id"], "微信虚拟支付: " + second["order_id"]], expected,
        expected_rows=2)
    obs.update({
        "expected_points_total": expected,
        "order_a_points": int(first["points"]),
        "order_b_points": int(second["points"]),
        "order_a_rows": len([r for r in after["audit"]
                             if first["order_id"] in (r["reason"] or "")]),
        "order_b_rows": len([r for r in after["audit"]
                             if second["order_id"] in (r["reason"] or "")]),
        "worker_errors": stats["errors"][:10],
        "total_calls": stats["calls"],
    })
    checks["each_order_credited_once"] = (obs["order_a_rows"] == 1 and obs["order_b_rows"] == 1)
    return result("D 同用户多订单跨进程并发入账", obs, checks)


def sc_virtual_mixed_ordering(ctx) -> dict:
    """场景 E：乱序竞争 —— 同一订单重放 + 同用户另一订单入账 + 无关用户在途账务。"""
    username = new_username("mix")
    ensure_user(username, 1000)
    other_user = new_username("mixother")
    ensure_user(other_user, 500)
    order = make_virtual_order(username, "points_1000")
    side = make_virtual_order(username, "points_2000")
    before = snapshot(username)
    before_other = snapshot(other_user)
    payloads = [
        {"action": "virtual_notify", "replays": ctx["replays"], "args": {"order_id": order["order_id"]}},
        {"action": "virtual_notify", "replays": ctx["replays"], "args": {"order_id": side["order_id"]}},
        {"action": "points_deduct", "replays": ctx["replays"],
         "args": {"username": other_user, "amount": 5, "reason": "zz-tool",
                  "transaction_key": "zzpaystress-tool:deduct:%s" % other_user}},
        {"action": "points_refund", "replays": ctx["replays"],
         "args": {"username": other_user, "amount": 7, "reason": "zz-tool-refund",
                  "transaction_key": "zzpaystress-tool:refund:%s" % other_user}},
    ]
    block = _spawn(ctx, payloads, pool_max=2)
    stats = summarize_worker_output(block)
    after = snapshot(username)
    after_other = snapshot(other_user)
    obs, checks = ledger_evidence(
        "乱序竞争（重放+同用户另一单+无关账务）", BASE_POINTS, before, after,
        ["微信虚拟支付: " + order["order_id"], "微信虚拟支付: " + side["order_id"]],
        int(order["points"]) + int(side["points"]), expected_rows=2)
    other_hits = [r for r in after_other["audit"] if "zz-tool" in (r["reason"] or "")]
    obs.update({
        "main_order_rows": len([r for r in after["audit"] if order["order_id"] in (r["reason"] or "")]),
        "side_order_rows": len([r for r in after["audit"] if side["order_id"] in (r["reason"] or "")]),
        "expected_points_total": int(order["points"]) + int(side["points"]),
        "other_user_points_before": before_other["points"],
        "other_user_points_after": after_other["points"],
        "other_user_expected": before_other["points"] + 7 - 5,
        "other_user_ledger_rows": len(other_hits),
        "other_user_ledger_deltas": sorted({int(r["delta"]) for r in other_hits}),
        "worker_errors": stats["errors"][:10],
    })
    checks["each_order_credited_once"] = (obs["main_order_rows"] == 1 and obs["side_order_rows"] == 1)
    checks["other_user_exactly_once"] = (
        obs["other_user_points_after"] == obs["other_user_expected"]
        and len(other_hits) == 2)
    return result("E 乱序竞争（重放+同用户多单+无关账务）", obs, checks)


def sc_recharge_approve_serial(ctx) -> dict:
    """场景 F：同一充值单重复触发审批（串行，同一 transaction_id）。"""
    username = new_username("rcgser")
    ensure_user(username, 1000)
    order = make_recharge_order(username, points=250, amount=25.0)
    txn = "ZZSTRESSTXN%s" % order["order_id"][-8:]
    before = snapshot(username)
    errors = []
    for _ in range(ctx["replays"]):
        try:
            auth().review_recharge_order(STRESS_ACTOR, order["order_id"], "approve",
                                         "zz-paystress", transaction_id=txn, pay_channel="wxpay")
        except Exception as exc:                      # noqa: BLE001
            errors.append("%s: %s" % (type(exc).__name__, str(exc)[:160]))
    after = snapshot(username)
    row = recharge_row(order["order_id"])
    obs, checks = ledger_evidence("充值审批串行重复触发", BASE_POINTS, before, after,
                                  "充值审批: " + order["order_id"], int(order["points"]))
    obs.update({
        "replays": ctx["replays"],
        "order_points": int(order["points"]),
        "order_status": row.get("status"),
        "errors": errors[:5],
        "transaction_id_duplicates": _transaction_id_duplicates(txn),
    })
    checks["credited_exactly_once"] = obs["ledger_rows_for_this_payment"] == 1
    return result("F 充值审批串行重复触发", obs, checks)


def sc_recharge_approve_process(ctx) -> dict:
    """场景 G：同一充值单跨进程并发触发审批（同一 transaction_id）。"""
    username = new_username("rcgproc")
    ensure_user(username, 1000)
    order = make_recharge_order(username, points=250, amount=25.0)
    txn = "ZZSTRESSTXN%s" % order["order_id"][-8:]
    before = snapshot(username)
    payloads = [{"action": "recharge_approve", "replays": ctx["replays"],
                 "args": {"order_id": order["order_id"], "transaction_id": txn}}
                for _ in range(ctx["workers"])]
    block = _spawn(ctx, payloads, pool_max=2)
    stats = summarize_worker_output(block)
    after = snapshot(username)
    row = recharge_row(order["order_id"])
    obs, checks = ledger_evidence("充值审批跨进程并发触发", BASE_POINTS, before, after,
                                  "充值审批: " + order["order_id"], int(order["points"]))
    obs.update({
        "processes": ctx["workers"],
        "replays_per_process": ctx["replays"],
        "total_calls": stats["calls"],
        "order_points": int(order["points"]),
        "order_status": row.get("status"),
        "worker_errors": stats["errors"][:10],
        "worker_outcomes": stats["outcomes"],
        "transaction_id_duplicates": _transaction_id_duplicates(txn),
    })
    checks["credited_exactly_once"] = obs["ledger_rows_for_this_payment"] == 1
    checks["transaction_id_unique"] = obs["transaction_id_duplicates"] == 0
    return result("G 充值审批跨进程并发触发", obs, checks)


def sc_recharge_same_txn_two_orders(ctx) -> dict:
    """场景 H：同一微信流水号被两张不同充值单跨进程同时审批。"""
    username = new_username("rcgdup")
    ensure_user(username, 1000)
    first = make_recharge_order(username, points=100, amount=10.0)
    second = make_recharge_order(username, points=100, amount=10.0)
    txn = "ZZSTRESSDUPTXN%s" % first["order_id"][-8:]
    before = snapshot(username)
    payloads = [
        {"action": "recharge_approve", "replays": 1,
         "args": {"order_id": first["order_id"], "transaction_id": txn}},
        {"action": "recharge_approve", "replays": 1,
         "args": {"order_id": second["order_id"], "transaction_id": txn}},
    ]
    block = _spawn(ctx, payloads, pool_max=2)
    stats = summarize_worker_output(block)
    after = snapshot(username)
    obs, checks = ledger_evidence(
        "同一微信流水号审批两张单", BASE_POINTS, before, after,
        ["充值审批: " + first["order_id"], "充值审批: " + second["order_id"]],
        int(first["points"]))
    obs.update({
        "first_status": recharge_row(first["order_id"]).get("status"),
        "second_status": recharge_row(second["order_id"]).get("status"),
        "transaction_id_duplicates": _transaction_id_duplicates(txn),
        "worker_outcomes": stats["outcomes"],
    })
    checks["duplicate_transaction_id_rejected"] = obs["transaction_id_duplicates"] == 0
    return result("H 同一微信流水审批两张单", obs, checks)


def _transaction_id_duplicates(txn: str) -> int:
    rows = _query("SELECT order_id FROM recharge_orders WHERE transaction_id=?", (txn,))
    return max(0, len(rows) - 1)


def sc_points_key_replay(ctx) -> dict:
    """场景 I：同一 transaction_key 的退点跨进程重放（唯一约束护栏）。"""
    username = new_username("key")
    ensure_user(username, 1000)
    key = "zzpaystress-refund:%s" % username
    before = snapshot(username)
    payloads = [{"action": "points_refund", "replays": ctx["replays"],
                 "args": {"username": username, "amount": 30, "reason": "zz-key-replay",
                          "transaction_key": key}}
                for _ in range(ctx["workers"])]
    block = _spawn(ctx, payloads, pool_max=2)
    stats = summarize_worker_output(block)
    after = snapshot(username)
    obs, checks = ledger_evidence("transaction_key 退点跨进程重放", BASE_POINTS, before, after,
                                  "zz-key-replay", 30)
    obs.update({
        "processes": ctx["workers"],
        "total_calls": stats["calls"],
        "distinct_transaction_keys": sorted({r["transaction_key"] for r in after["audit"]
                                             if r["transaction_key"]}),
        "worker_errors": stats["errors"][:8],
        "worker_outcomes": stats["outcomes"],
    })
    checks["credited_exactly_once"] = obs["ledger_rows_for_this_payment"] == 1
    return result("I transaction_key 退点跨进程重放", obs, checks)


def sc_points_key_conflict(ctx) -> dict:
    """场景 J：同一 transaction_key 换金额重放 —— 必须被拒且不改余额。"""
    username = new_username("conf")
    ensure_user(username, 1000)
    key = "zzpaystress-conflict:%s" % username
    auth().refund_points(username, 40, "zz-conflict", key, apply_balance=True)
    before = snapshot(username)
    errors, outcomes = [], []
    try:
        points, err = auth().refund_points(username, 999, "zz-conflict-other", key, apply_balance=True)
        outcomes.append({"err": err, "points": (points or {}).get("points")})
    except Exception as exc:                          # noqa: BLE001
        errors.append("%s: %s" % (type(exc).__name__, str(exc)[:160]))
    after = snapshot(username)
    obs, checks = ledger_evidence("transaction_key 换金额重放", BASE_POINTS, before, after,
                                  "zz-conflict", 0)
    obs.update({"second_call_outcomes": outcomes, "second_call_errors": errors})
    checks["no_extra_ledger_row"] = obs["ledger_rows_for_this_payment"] == 1
    checks["second_call_rejected"] = bool(errors) or any(
        item.get("err") for item in outcomes)
    return result("J transaction_key 换金额重放", obs, checks)


def sc_recharge_refund_replay(ctx) -> dict:
    """场景 K：微信支付退款回调重复到达（跨进程并发）。"""
    username = new_username("rfdr")
    ensure_user(username, BASE_POINTS)
    order = make_recharge_order(username, points=250, amount=25.0)
    txn = "ZZSTRESSRFDTXN%s" % order["order_id"][-8:]
    auth().review_recharge_order(STRESS_ACTOR, order["order_id"], "approve", "zz-paystress",
                                 transaction_id=txn, pay_channel="wxpay")
    before = snapshot(username)
    payloads = [{"action": "recharge_refund", "replays": ctx["replays"],
                 "args": {"order_id": order["order_id"], "amount_fen": 2500,
                          "transaction_id": txn}}
                for _ in range(ctx["workers"])]
    block = _spawn(ctx, payloads, pool_max=2)
    stats = summarize_worker_output(block)
    after = snapshot(username)
    row = recharge_row(order["order_id"])
    obs, checks = ledger_evidence(
        "微信支付退款回调跨进程重放", BASE_POINTS, before, after,
        "微信支付退款: " + order["order_id"], -int(order["points"]))
    obs.update({
        "processes": ctx["workers"],
        "total_calls": stats["calls"],
        "order_points": int(order["points"]),
        "order_status": row.get("status"),
        "worker_errors": stats["errors"][:8],
        "worker_outcomes": stats["outcomes"],
    })
    checks["refunded_exactly_once"] = obs["ledger_rows_for_this_payment"] == 1
    checks["order_status_refunded"] = row.get("status") == "refunded"
    return result("K 微信支付退款回调跨进程重放", obs, checks)


def sc_virtual_refund_replay(ctx) -> dict:
    """场景 L：虚拟支付退款通知重复到达（跨进程并发）。"""
    username = new_username("rfdp")
    ensure_user(username, BASE_POINTS)
    order = make_virtual_order(username, "points_1000")
    auth().process_virtual_pay_message(
        {"Event": "xpay_goods_deliver_notify", "order_id": order["order_id"]})
    before = snapshot(username)
    payloads = [{"action": "virtual_refund", "replays": ctx["replays"],
                 "args": {"order_id": order["order_id"]}}
                for _ in range(ctx["workers"])]
    block = _spawn(ctx, payloads, pool_max=2)
    stats = summarize_worker_output(block)
    after = snapshot(username)
    row = order_row(order["order_id"])
    obs, checks = ledger_evidence(
        "虚拟支付退款通知跨进程重放", BASE_POINTS, before, after,
        "微信虚拟支付退款: " + order["order_id"], -int(order["points"]))
    obs.update({
        "processes": ctx["workers"],
        "total_calls": stats["calls"],
        "order_points": int(order["points"]),
        "order_status": row.get("status"),
        "worker_errors": stats["errors"][:8],
        "worker_outcomes": stats["outcomes"],
    })
    checks["refunded_exactly_once"] = obs["ledger_rows_for_this_payment"] == 1
    checks["order_status_refunded"] = row.get("status") == "refunded"
    checks["balance_back_to_base"] = obs["balance_after"] == BASE_POINTS
    return result("L 虚拟支付退款通知跨进程重放", obs, checks)


SCENARIOS = [
    ("A", sc_virtual_serial_replay),
    ("B", sc_virtual_thread_replay),
    ("C", sc_virtual_process_replay),
    ("D", sc_virtual_cross_order_same_user),
    ("E", sc_virtual_mixed_ordering),
    ("F", sc_recharge_approve_serial),
    ("G", sc_recharge_approve_process),
    ("H", sc_recharge_same_txn_two_orders),
    ("I", sc_points_key_replay),
    ("J", sc_points_key_conflict),
    ("K", sc_recharge_refund_replay),
    ("L", sc_virtual_refund_replay),
]
SCENARIO_MAP = {name: fn for name, fn in SCENARIOS}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def aggregate_repeats(name: str, outcomes: list, elapsed_ms: int) -> dict:
    """把同一场景的多次重复（每次全新账号/订单）聚合成一条结果。

    重复的意义：跨进程竞态不是每次都必现，单次 PASS 不能证明幂等；只有
    N 次重复全部只入账一次才算通过。失败次数与「出现重复流水行的次数」是证据。
    """
    checks = {}
    for outcome in outcomes:
        for key, value in outcome["checks"].items():
            checks[key] = bool(checks.get(key, True)) and bool(value)
    dup_runs = sum(
        1 for o in outcomes
        if (o["observations"].get("ledger_rows_for_this_payment") or 0)
        > (o["observations"].get("expected_ledger_rows") or 1))
    failed = [o for o in outcomes if o["verdict"] != "PASS"]
    per_repeat = [{
        "verdict": o["verdict"],
        "ledger_rows_for_this_payment": o["observations"].get("ledger_rows_for_this_payment"),
        "expected_ledger_rows": o["observations"].get("expected_ledger_rows"),
        "balance_before": o["observations"].get("balance_before"),
        "balance_after": o["observations"].get("balance_after"),
        "balance_delta": o["observations"].get("balance_delta"),
        "expected_balance_delta": o["observations"].get("expected_balance_delta"),
        "ledger_sum_all": o["observations"].get("ledger_delta_sum_all"),
        "ledger_chain_ok": o["observations"].get("ledger_chain_ok"),
    } for o in outcomes]
    observations = {
        "label": outcomes[0]["scenario"],
        "repeats": len(outcomes),
        "failed_repeats": len(failed),
        "excess_ledger_row_repeats": dup_runs,
        "per_repeat": per_repeat,
        "sample_failure": failed[0]["observations"] if failed else None,
        "sample_pass_checks": outcomes[0]["checks"] if outcomes else {},
    }
    return {
        "scenario": outcomes[0]["scenario"],
        "verdict": "PASS" if not failed else "FAIL",
        "checks": checks,
        "observations": observations,
        "notes": [],
        "name": name,
        "elapsed_ms": elapsed_ms,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="支付幂等压测（staging-only）")
    parser.add_argument("--scenario", default="all",
                        help="all 或场景名（A/B/.../J，也可用逗号分隔）")
    parser.add_argument("--replays", type=int, default=20, help="每个调用方的重放次数")
    parser.add_argument("--workers", type=int, default=6, help="跨进程场景的并发进程数")
    parser.add_argument("--threads", type=int, default=16, help="单进程线程场景的线程数")
    parser.add_argument("--repeat", type=int, default=1, help="每个场景重复次数（每次全新账号/订单）")
    parser.add_argument("--expect-database", default=EXPECTED_DATABASE)
    parser.add_argument("--run-id", default="", help="本次压测标记（默认时间戳）")
    parser.add_argument("--out", default="", help="证据 JSON 输出路径")
    parser.add_argument("--keep", action="store_true", help="保留测试数据（默认跑完清理）")
    parser.add_argument("--worker", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker:
        return worker_main(json.loads(args.worker))

    _STATE["run"] = args.run_id or time.strftime("%H%M%S")
    _bootstrap_env()
    database = require_staging_target(args.expect_database)

    report = {
        "harness": "pay_idempotency_stress",
        "scope": "staging-only",
        "database": database,
        "run_id": _STATE["run"],
        "started_at": int(time.time()),
        "params": {"replays": args.replays, "workers": args.workers, "threads": args.threads},
        "purged_before": purge_leftovers(),
        "table_counts_before": table_counts(),
        "scenarios": [],
    }

    if args.scenario == "all":
        selected = list(SCENARIOS)
    else:
        wanted = [part.strip().upper() for part in args.scenario.split(",") if part.strip()]
        selected = [(name, fn) for name, fn in SCENARIOS if name in wanted]
        unknown = [name for name in wanted if name not in SCENARIO_MAP]
        if unknown:
            raise SystemExit("unknown scenario: %s" % ", ".join(unknown))

    ctx = {"replays": args.replays, "workers": args.workers, "threads": args.threads}
    ctx["repeat"] = max(1, args.repeat)
    report["params"]["repeat"] = ctx["repeat"]
    for name, fn in selected:
        started = time.time()
        outcomes = []
        for _ in range(ctx["repeat"]):
            try:
                outcomes.append(fn(ctx))
            except Exception as exc:                  # noqa: BLE001
                outcomes.append({
                    "scenario": name, "verdict": "ERROR", "checks": {},
                    "observations": {"exception": "%s: %s" % (type(exc).__name__, exc)},
                    "notes": [],
                })
        elapsed = int((time.time() - started) * 1000)
        outcome = outcomes[0] if len(outcomes) == 1 else aggregate_repeats(name, outcomes, elapsed)
        outcome.setdefault("name", name)
        outcome["elapsed_ms"] = elapsed
        report["scenarios"].append(outcome)
        extra = ""
        if outcome["observations"].get("repeats"):
            extra = " repeats=%d failed=%d excess_rows=%d" % (
                outcome["observations"]["repeats"],
                outcome["observations"]["failed_repeats"],
                outcome["observations"]["excess_ledger_row_repeats"])
        print("[%s] %-40s %-5s %5dms%s  %s" % (
            name, outcome["scenario"], outcome["verdict"], outcome["elapsed_ms"], extra,
            json.dumps(outcome["checks"], ensure_ascii=False)), flush=True)

    if not args.keep:
        report["purged_after"] = purge_leftovers()
    report["table_counts_after"] = table_counts()
    report["finished_at"] = int(time.time())

    failed = [s for s in report["scenarios"] if s["verdict"] != "PASS"]
    report["summary"] = {
        "scenarios": len(report["scenarios"]),
        "passed": len(report["scenarios"]) - len(failed),
        "failed": len(failed),
        "failed_names": [s["scenario"] for s in failed],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print("evidence written to %s" % args.out)
    else:
        print(text)
    print("SUMMARY " + json.dumps(report["summary"], ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
