#!/usr/bin/env python3
"""画布模块接口黑盒测试（测试站 127.0.0.1:8095，Bearer 模式）"""
import json, time, urllib.request, urllib.error, sqlite3, sys

BASE = "http://127.0.0.1:8095"
DB = "/opt/huangque-test-server/server/users.db"
RESULTS = []

def call(method, path, token=None, body=None, raw_headers=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", "Bearer " + token)
    for k, v in (raw_headers or {}).items(): req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode() or "{}")
        except Exception: return e.code, {}

def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))

def login(u, p):
    s, d = call("POST", "/api/auth/miniprogram-login", body={"username": u, "password": p},
                raw_headers={"Origin": "http://8.138.143.64"})
    return (d.get("token") or d.get("session") or ""), s, d

# ---------- 0. 建测试用户（直接建库行,模拟 create-user CLI 等价物） ----------
import hashlib, secrets, os
PW = os.environ.get("CVSTEST_PW")
if not PW:
    print("缺少测试密码: 请先设置环境变量 CVSTEST_PW (仅测试站临时账号使用)")
    sys.exit(2)
sys.path.insert(0, "/opt/huangque-test-server/server")
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
for u in ("cvs_alice", "cvs_bob", "cvs_eve"):
    c.execute("DELETE FROM tokens WHERE username=?", (u,))
    c.execute("DELETE FROM users WHERE username=?", (u,))
c.commit()
import subprocess
for u, pts in (("cvs_alice", 100), ("cvs_bob", 100), ("cvs_eve", 100)):
    r = subprocess.run(["/usr/bin/python3", "/opt/huangque-test-server/server/auth_server.py",
                        "create-user", u, PW, str(pts), "member"],
                       capture_output=True, text=True)
    if r.returncode != 0: print("create-user fail", u, r.stderr.strip())
c.close()

ta, sa, da = login("cvs_alice", PW)
tb, sb, db_ = login("cvs_bob", PW)
te, se, de = login("cvs_eve", PW)
print("login status:", sa, sb, se)
if not (ta and tb and te):
    print("登录响应样例:", da); sys.exit(1)

# ---------- A. 认证与存在性 ----------
s, _ = call("GET", "/api/auth/canvas/boards")
check("A1 未登录访问→401", s == 401, s)

# ---------- B. 画板 CRUD ----------
s, d = call("POST", "/api/auth/canvas/boards", ta, {"name": "测试板A", "data": {"nodes": [], "edges": []}})
check("B1 创建画板→200+v1", s == 200 and d.get("board", {}).get("version") == 1, s)
BID = d.get("board", {}).get("id", "")
print("   board_id =", BID)

s, d = call("POST", "/api/auth/canvas/boards", ta, {"name": "坏\x01名字"})
check("B2 控制字符名→400", s == 400, s)
s, d = call("POST", "/api/auth/canvas/boards", ta, {"data": {"nodes": "不是数组"}})
check("B3 非法数据→400", s == 400, (s, d.get("detail")))

s, d = call("GET", "/api/auth/canvas/boards", ta)
mc = [b.get("members_count") for b in d.get("boards", []) if b.get("id") == BID]
check("B4 boards 列表包含新板", bool(mc), mc)
check("B4a [修复] members_count 含 owner→1", mc and mc[0] == 1, mc)

s, d = call("GET", f"/api/auth/canvas/boards/{BID}", ta)
check("B5 读单板含 data+members", s == 200 and "members" in d.get("board", {}), s)
s, d = call("GET", f"/api/auth/canvas/boards/{BID}", te)
check("A2 非成员读板→404(不泄露存在性)", s == 404, s)

# ---------- C. 好友 + 权限 ----------
s, d = call("POST", "/api/auth/friends/request", ta, {"account_id": None})
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
bob_acc = c.execute("SELECT account_id FROM users WHERE username='cvs_bob'").fetchone()["account_id"]
eve_acc = c.execute("SELECT account_id FROM users WHERE username='cvs_eve'").fetchone()["account_id"]
c.close()
call("POST", "/api/auth/friends/request", ta, {"account_id": bob_acc})
s, d = call("GET", "/api/auth/friend-requests", tb)
rid = None
for r in (d.get("incoming") or d.get("requests") or []):
    rid = r.get("id") or r.get("request_id")
if rid: call("POST", "/api/auth/friend-requests/respond", tb, {"request_id": rid, "action": "accept"})
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/members", ta, {"account_id": bob_acc, "role": "editor"})
check("C1 邀请好友为 editor→200", s == 200, (s, d.get("detail")))
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/members", ta, {"account_id": eve_acc, "role": "editor"})
check("C2 邀请非好友→403 not_friend", s == 403, (s, d.get("detail")))
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/members", tb, {"account_id": eve_acc, "role": "viewer"})
check("C3 非 owner 邀请→403", s == 403, (s, d.get("detail")))

# 补加 alice↔eve 好友, 用于成员上限验证(服务以 HQ_CANVAS_MAX_MEMBERS_PER_BOARD=1 运行)
call("POST", "/api/auth/friends/request", ta, {"account_id": eve_acc})
s, d = call("GET", "/api/auth/friend-requests", te)
rid = None
for r in (d.get("incoming") or d.get("requests") or []):
    rid = r.get("id") or r.get("request_id")
if rid: call("POST", "/api/auth/friend-requests/respond", te, {"request_id": rid, "action": "accept"})
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/members", ta, {"account_id": eve_acc, "role": "viewer"})
check("C4 [P0-3] 已有 1 成员时再邀新人→429", s == 429, (s, d.get("detail")))
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/members", ta, {"account_id": bob_acc, "role": "editor"})
check("C5 [P0-3] 已达上限时老成员改角色(upsert)→200", s == 200, (s, d.get("detail")))

# ---------- D. ops / sync ----------
s, d = call("GET", f"/api/auth/canvas/boards/{BID}", ta)
v0 = d["board"]["version"]

def ops_body(base, ops, op_id, client="client-a"):
    return {"op_id": op_id, "client_id": client, "base_version": base, "ops": ops}

s, d = call("POST", f"/api/auth/canvas/boards/{BID}/ops", tb,
            ops_body(v0, [{"type": "node.create", "node": {"id": "n1", "type": "text", "x": 1, "y": 2}}], "op-001", "client-bob"))
check("D1 editor 提交 ops→200 版本+1", s == 200 and d.get("version") == v0 + 1, (s, d.get("detail")))
v1 = d.get("version", v0)

# 【修复验证 1】过期 base_version 必须被 409 拒绝,版本不变
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/ops", ta,
            ops_body(1, [{"type": "board.rename", "name": "过期基准改名"}], "op-002"))
check("D2 [修复] base_version 过期→409 并回当前版本", s == 409 and d.get("version") == v1, (s, d.get("version")))
v2 = v1

# 【修复验证 2】同基线并发写:先到者成功,后到者 409,不再静默覆盖
s1, d1 = call("POST", f"/api/auth/canvas/boards/{BID}/ops", ta, ops_body(v2, [{"type": "board.rename", "name": "甲的名字"}], "op-003a", "client-A"))
s2, d2 = call("POST", f"/api/auth/canvas/boards/{BID}/ops", tb, ops_body(v2, [{"type": "board.rename", "name": "乙的名字"}], "op-003b", "client-B"))
check("D3 [修复] 同基线双写→200+409", s1 == 200 and s2 == 409, (s1, s2))
s3, d3 = call("GET", f"/api/auth/canvas/boards/{BID}", ta)
final_name = d3.get("board", {}).get("name")
check("D3a [修复] 先到者改动不再被覆盖", final_name == "甲的名字", final_name)
v3 = d1.get("version", v2 + 1)
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/ops", tb, ops_body(v3, [{"type": "board.rename", "name": "乙同步后改名"}], "op-003c", "client-B"))
check("D3b [修复] 409 后带新版本重试→200", s == 200 and d.get("version") == v3 + 1, (s, d.get("detail")))

# 幂等重放
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/ops", tb,
            ops_body(v0, [{"type": "node.create", "node": {"id": "n1", "type": "text", "x": 1, "y": 2}}], "op-001", "client-bob"))
check("D4 相同 op_id 重放→幂等返回(版本不再涨)", s == 200 and d.get("batch", {}).get("op_id") == "op-001", (s, d.get("version")))

# 非法 ops
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/ops", ta,
            ops_body(1, [{"type": "node.create", "node": {"id": "n9", "type": "不存在类型"}}], "op-004"))
check("D5 非法节点类型→400 bad_op", s == 400, (s, d.get("detail")))
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/ops", ta,
            ops_body(1, [{"type": "node.delete", "id": f"n{i}"} for i in range(201)], "op-005"))
check("D6 超 200 条→413 too_many_ops", s == 413, (s, d.get("detail")))
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/ops", ta, {"ops": []})
check("D7 缺 op_id→400", s == 400, (s, d.get("detail")))
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/ops", te,
            ops_body(1, [{"type": "node.delete", "id": "n1"}], "op-006"))
check("D8 非成员提交 ops→404", s == 404, s)

# sync
s, d = call("GET", f"/api/auth/canvas/boards/{BID}", ta)
vc = d["board"]["version"]
s, d = call("GET", f"/api/auth/canvas/boards/{BID}/sync?since={vc}", tb)
check("D9 sync 到最新→batches 空 reset=false", s == 200 and d.get("batches") == [] and d.get("reset") is False, (s, d.get("reset")))
s, d = call("GET", f"/api/auth/canvas/boards/{BID}/sync?since={vc-2}", tb)
check("D10 sync 增量→拿到 2 个批次", s == 200 and len(d.get("batches", [])) == 2, (s, len(d.get("batches", [])), d.get("reset")))
s, d = call("GET", f"/api/auth/canvas/boards/{BID}/sync?since=0", ta)
check("D11 since=0→400", s == 400, s)
s, d = call("GET", f"/api/auth/canvas/boards/{BID}/sync?since=abc", ta)
check("D12 since 非数字→400", s == 400, s)
s, d = call("GET", f"/api/auth/canvas/boards/{BID}/sync?since={vc+99}", ta)
check("D13 since 超当前→reset=true", s == 200 and d.get("reset") is True and "board" in d, (s, d.get("reset")))
s, d = call("GET", f"/api/auth/canvas/boards/{BID}/sync?since=1", te)
check("D14 非成员 sync→404", s == 404, s)

# 【修复验证 3】/save 写检查点批次,sync 走增量快照而非全量 reset
s, d = call("GET", f"/api/auth/canvas/boards/{BID}", ta)
vs = d["board"]["version"]
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/save", ta, {"version": vs, "data": d["board"]["data"], "name": "整存一次"})
check("D15 /save→200 版本+1", s == 200 and d.get("board", {}).get("version") == vs + 1, (s, d.get("detail")))
s, d = call("GET", f"/api/auth/canvas/boards/{BID}/sync?since={vs}", tb)
batches = d.get("batches", [])
is_ckpt = len(batches) == 1 and (batches[0].get("ops") or [{}])[0].get("type") == "board.snapshot"
check("D16 [修复] /save 后 sync→增量 board.snapshot 检查点", s == 200 and d.get("reset") is False and is_ckpt, (s, d.get("reset"), len(batches)))

# /save 版本冲突
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/save", tb, {"version": 1, "data": {}})
check("D17 /save 过期版本→409 并回最新板", s == 409 and "board" in d, (s, d.get("detail")))

# viewer 权限
call("POST", f"/api/auth/canvas/boards/{BID}/members", ta, {"account_id": bob_acc, "role": "viewer"})
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/save", tb, {"version": 999, "data": {}})
check("D18 viewer 保存→403", s == 403, (s, d.get("detail")))
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/ops", tb, ops_body(1, [{"type": "node.delete", "id": "n1"}], "op-007"))
check("D19 viewer 提交 ops→403", s == 403, (s, d.get("detail")))
call("POST", f"/api/auth/canvas/boards/{BID}/members", ta, {"account_id": bob_acc, "role": "editor"})

# ---------- L. sync 长轮询 ----------
import threading
s, d = call("GET", f"/api/auth/canvas/boards/{BID}", ta)
vl = d["board"]["version"]
t0 = time.time()
s, d = call("GET", f"/api/auth/canvas/boards/{BID}/sync?since={vl}&wait=2", ta)
el = time.time() - t0
check("L1 [P1] 无变化 hold 到超时(~2s)再回空", s == 200 and el >= 1.5 and d.get("batches") == [] and d.get("reset") is False, (s, round(el, 1)))
s, d = call("GET", f"/api/auth/canvas/boards/{BID}", ta)
vl = d["board"]["version"]
holder = {}
def waiter():
    tt = time.time()
    ss, dd = call("GET", f"/api/auth/canvas/boards/{BID}/sync?since={vl}&wait=10", tb)
    holder.update(s=ss, d=dd, el=time.time() - tt)
th = threading.Thread(target=waiter)
th.start()
time.sleep(1.0)
s2, d2 = call("POST", f"/api/auth/canvas/boards/{BID}/ops", tb, ops_body(vl, [{"type": "board.rename", "name": "唤醒长轮询"}], "lp-001", "client-bob"))
th.join(timeout=15)
check("L2 [P1] hold 期间新 ops→提前返回带批次", holder.get("s") == 200 and holder.get("el", 99) < 8 and len(holder.get("d", {}).get("batches", [])) == 1, (holder.get("s"), round(holder.get("el", -1), 1)))
s, d = call("GET", f"/api/auth/canvas/boards/{BID}/sync?since={vl}&wait=abc", ta)
check("L3 [P1] wait 非法→400", s == 400, s)

# ---------- D20. ops 限频(默认 10 批/10 秒/人/板) ----------
s, d = call("POST", "/api/auth/canvas/boards", ta, {"name": "限频板", "data": {"nodes": [], "edges": []}})
rb = d["board"]["id"]
ok200, got429 = 0, False
for i in range(12):
    s, d = call("POST", f"/api/auth/canvas/boards/{rb}/ops", ta,
                ops_body(1 + i, [{"type": "board.rename", "name": f"名{i}"}], f"rl-{i}"))
    if s == 200:
        ok200 += 1
    elif s == 429:
        got429 = True
        break
check("D20 [P0-1] 窗口内 10 批放行", ok200 == 10, ok200)
check("D21 [P0-1] 第 11 批→429 rate_limited", got429, got429)
call("DELETE", f"/api/auth/canvas/boards/{rb}", ta)

# ---------- E. presence ----------
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/presence", ta, {"client_id": "alice-pc"})
check("E1 owner 心跳→online=1", s == 200 and d.get("online_count") == 1, (s, d))
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/presence", ta, {"client_id": "alice-pc"})
check("E1b [P0-2] 3秒内同端心跳→deduped 不写库", s == 200 and d.get("deduped") is True and d.get("online_count") == 1, (s, d))
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/presence", ta, {"client_id": "alice-phone"})
check("E2 [修复] 同一人第二个客户端→online 仍=1(按人去重)", s == 200 and d.get("online_count") == 1, (s, d))
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/presence", tb, {"client_id": "bob-pc"})
check("E3 editor 心跳→online=2", s == 200 and d.get("online_count") == 2, (s, d))
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/presence", te, {"client_id": "eve-pc"})
check("E4 非成员心跳→404", s == 404, s)
call("POST", f"/api/auth/canvas/boards/{BID}/members", ta, {"account_id": bob_acc, "role": "viewer"})
time.sleep(3.2)  # 避开 3 秒心跳去重窗口, 确保 E5 真实写库
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/presence", tb, {"client_id": "bob-pc"})
check("E5 [修复] viewer 心跳也计入在线数", s == 200 and d.get("online_count") == 2, (s, d))
s, d = call("POST", f"/api/auth/canvas/boards/{BID}/presence", ta, {"client_id": "  "})
check("E6 空 client_id→400", s == 400, (s, d.get("detail")))

# ---------- F. 成员管理 ----------
s, d = call("DELETE", f"/api/auth/canvas/boards/{BID}/members/cvs_bob", None, raw_headers={"Authorization": "Bearer " + tb})
check("F1 非 owner 移除成员→403", s == 403, (s, d.get("detail")))
s, d = call("DELETE", f"/api/auth/canvas/boards/{BID}/members/cvs_bob", ta)
check("F2 owner 移除成员→200", s == 200 and all(m.get("username") != "cvs_bob" for m in d.get("members", [])), (s, d))

# ---------- G. 建板上限(服务以 HQ_CANVAS_MAX_BOARDS_PER_USER=3 运行时有效) ----------
tmp_boards = []
created = 0
limited = False
for i in range(5):
    s, d = call("POST", "/api/auth/canvas/boards", ta, {"name": f"批量{i}", "data": {"nodes": [], "edges": []}})
    if s == 200:
        created += 1
        tmp_boards.append(d["board"]["id"])
    elif s == 429:
        limited = True
        break
check("G1 [修复] 已有 1 板时再建 2 块成功", created == 2, created)
check("G2 [修复] 达到上限→429", limited, limited)
for bid_tmp in tmp_boards:
    call("DELETE", f"/api/auth/canvas/boards/{bid_tmp}", ta)

# ---------- H. 删板 ----------
s, d = call("DELETE", f"/api/auth/canvas/boards/{BID}", tb)
check("H1 非 owner 删板→403/404", s in (403, 404), s)
s, d = call("DELETE", f"/api/auth/canvas/boards/{BID}", ta)
check("H2 owner 删板→200", s == 200, s)
s, d = call("GET", f"/api/auth/canvas/boards/{BID}", ta)
check("H3 删后读→404", s == 404, s)

print("\n===== 汇总 =====")
fails = [r for r in RESULTS if not r[1]]
print(f"共 {len(RESULTS)} 项, PASS {len(RESULTS)-len(fails)}, FAIL {len(fails)}")
for f_ in fails: print("  FAIL:", f_)
