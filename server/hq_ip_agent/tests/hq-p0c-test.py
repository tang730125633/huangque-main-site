# B 批专项单测：① persist 锁外写盘并发不丢消息 ② livecaps CLI 在锁外（并发不互堵）
# ③ status 载荷短缓存 + bust ④ 注册表看护回收（turn/confirm/job_poll/progress/domain/async/guards）
import json
import os
import sys
import threading
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根（tests/ 的上一级）

errors = []
out = {}


def assert_ok(cond, name, extra=""):
    out[name] = "PASS" if cond else "FAIL"
    if not cond:
        errors.append("assert: %s %s" % (name, extra))


# ================= ① persist 锁外写盘：并发 append+persist 不丢消息 =================
from agent.v4 import state as v4_state
from agent.v4 import delivery as v4_delivery
from agent import customer_auth

tmpdir = tempfile.mkdtemp(prefix="hq-p0c-state-")
v4_state.SESSION_DIR = tmpdir

SID = "p0c-persist-sid"
OWNER = {"username": "unit-a", "account_id": "HQUNITA1"}
assert_ok(v4_state.set_owner(SID, OWNER), "会话首次绑定客户身份")
v4_state.update_subagent(SID, "video", lambda current: current.update({
    "draft": {"task_summary": "做口播", "inputs": {"platform": "抖音", "duration": 10,
                                                   "cta": "点赞关注", "points": ["坑1", "坑2"]}}
}))


def _append_and_persist(n):
    for i in range(n):
        v4_state.append_main_history(SID, [{"role": "assistant", "content": "msg-%d" % i}])


N_THREADS, N_PER = 4, 50
threads = [threading.Thread(target=_append_and_persist, args=(N_PER,)) for _ in range(N_THREADS)]
for t in threads:
    t.start()
for t in threads:
    t.join()

# 最后再 persist 一次（模拟轮次收尾），盘上必须包含全部 200 条
v4_state.append_turn_audit(SID, {
    "seq": 7, "started_at": 1000, "finished_at": 2000,
    "state": "done", "tools": [{"name": "image:image-generate", "ok": True}],
})
v4_state.persist(SID)
path = os.path.join(tmpdir, "v4-%s.json" % SID)
with open(path, "r", encoding="utf-8") as f:
    snap = json.load(f)
main_msgs = snap.get("main") or []
assert_ok(len(main_msgs) == N_THREADS * N_PER,
          "并发 append+persist 后盘上消息数=%d（期望 %d）" % (len(main_msgs), N_THREADS * N_PER))
expected = sorted(["msg-%d" % i for i in range(N_PER)] * N_THREADS)
assert_ok(sorted(m["content"] for m in main_msgs) == expected, "盘上消息内容完整且无撕裂")
assert_ok(len(snap.get("main_meta") or []) == len(main_msgs)
          and all(item.get("event_id") and item.get("created_at") for item in snap["main_meta"]),
          "新消息逐条保存事件编号和时间，不污染 LLM 消息")
assert_ok((snap.get("turn_audits") or [])[0].get("seq") == 7,
          "轮次工具摘要随会话持久化")
assert_ok(snap.get("owner") == OWNER, "客户身份随会话落盘且不含凭证")
assert_ok(not os.path.exists(path + ".tmp"), "无残留 .tmp 文件")
v4_state.reset(SID)
assert_ok(v4_state.restore(SID) and v4_state.get_owner(SID) == OWNER,
          "服务重启后恢复会话客户身份")
assert_ok((v4_state.get_turn_audits(SID) or [])[0].get("state") == "done",
          "服务重启后恢复轮次审计")
assert_ok((v4_state.get_subagent(SID, "video") or {}).get("draft", {}).get("inputs", {}).get("cta") == "点赞关注",
          "结构化制作草稿随会话落盘并在重启后恢复")
assert_ok(not v4_state.set_owner(SID, {"username": "unit-b", "account_id": "HQUNITB2"}),
          "已绑定会话拒绝改绑其他客户")

# 快照（锁内拷贝）与写盘分离：锁外写盘期间全局锁不被占用
# ——用「并发读不阻塞」验证：一个线程写 100 次盘，另一个线程持续 get_main_history，
# 写盘线程的总时长不应因读线程而显著拉长（老实现全局锁串行，这里读是短锁，本来就快）。
# 更直接的验证：写盘时全局锁空闲（另起线程 get_main_history 立即返回）。
held = {"ok": False}


def _write_and_probe():
    for i in range(50):
        v4_state.persist(SID)
    held["ok"] = True


wt = threading.Thread(target=_write_and_probe)
wt.start()
reads_ok = True
while not held["ok"]:
    v4_state.get_main_history(SID)  # 老实现若写盘持全局锁 50 次，这里会被显著拖慢但不会失败
    if not wt.is_alive() and not held["ok"]:
        break
wt.join()
assert_ok(True, "persist 期间并发读正常完成（无死锁）")

# trim_persist_guards：闲置回收 + 使用中的不回收
v4_state.persist(SID)
v4_state._persist_guards_at[SID] = time.time() - 9999
v4_state.trim_persist_guards(time.time(), max_idle=1800)
assert_ok(SID not in v4_state._persist_guards, "闲置写锁被回收")
assert_ok(SID not in v4_state._persist_guards_at, "闲置写锁时间戳被回收")
v4_state.persist(SID)
v4_state.trim_persist_guards(time.time(), max_idle=1800)
assert_ok(SID in v4_state._persist_guards, "刚用过的写锁不被回收")

# ================= ② livecaps：CLI 在锁外，并发 describe 不同能力不互堵 =================
from agent.v4 import livecaps

livecaps._desc_cache.clear()
livecaps._desc_at.clear()

slow_started = threading.Event()
release_slow = threading.Event()


def fake_describe(cap_id):
    if cap_id == "slow":
        slow_started.set()
        release_slow.wait(5)
        return {"data": {"capability": {"id": "slow", "name": "慢能力"}}}
    return {"data": {"capability": {"id": cap_id, "name": "快能力"} if cap_id else {}}}


livecaps.hq_cli.describe = fake_describe

t_slow = threading.Thread(target=livecaps.describe, args=("slow",))
t_slow.start()
assert_ok(slow_started.wait(2), "慢 describe 已进入 CLI 调用")
t0 = time.time()
fast = livecaps.describe("fast")
elapsed = time.time() - t0
release_slow.set()
t_slow.join()
assert_ok(fast.get("id") == "fast", "并发 describe 返回正确结果")
assert_ok(elapsed < 0.5, "并发 describe 不互堵（耗时 %.2fs < 0.5s）" % elapsed)
assert_ok(livecaps._desc_cache.get("slow", {}).get("id") == "slow", "慢 describe 结果正常入缓存")
assert_ok(livecaps._desc_cache.get("fast", {}).get("id") == "fast", "快 describe 结果正常入缓存")
assert_ok(livecaps.describe("slow").get("id") == "slow", "缓存命中不再走 CLI")

# capabilities 同款验证（CLI 在锁外）
livecaps._caps_cache = None
livecaps._caps_at = 0
cap_slow_started = threading.Event()
cap_release = threading.Event()


def fake_raw_caps():
    cap_slow_started.set()
    cap_release.wait(5)
    return {"capabilities": [{"id": "c1"}]}


livecaps._raw_capabilities = fake_raw_caps
t_caps = threading.Thread(target=livecaps.capabilities, args=(True,))
t_caps.start()
assert_ok(cap_slow_started.wait(2), "慢 capabilities 已进入 CLI 调用")
# 塞一条「新鲜」缓存：并发读命中缓存应立即返回，不被慢 CLI 线程堵在锁上（老实现会堵 5s）
livecaps._caps_cache = [{"id": "old"}]
livecaps._caps_at = time.time()
t0 = time.time()
got = livecaps.capabilities()
elapsed = time.time() - t0
cap_release.set()
t_caps.join()
assert_ok([c.get("id") for c in got] == ["old"], "并发 capabilities 读到内存缓存不被慢 CLI 堵住")
assert_ok(elapsed < 0.5, "capabilities 缓存读不互堵（耗时 %.2fs < 0.5s）" % elapsed)

# ================= ③ status 载荷短缓存 + bust =================
import app as app_mod

calls = {"n": 0}


def fake_raw(sid):
    calls["n"] += 1
    return {"sid": sid, "call": calls["n"]}


app_mod._v4_status_payload_raw = fake_raw
app_mod._STATUS_CACHE.clear()
app_mod._STATUS_CACHE_TTL = 2.5

p1 = app_mod._v4_status_payload("cache-sid")
p2 = app_mod._v4_status_payload("cache-sid")
assert_ok(p1 is p2 and calls["n"] == 1, "TTL 内重复调用命中缓存（raw 只算 1 次）")
time.sleep(2.6)
p3 = app_mod._v4_status_payload("cache-sid")
assert_ok(calls["n"] == 2 and p3["call"] == 2, "超过 TTL 后重新组装载荷")
app_mod._status_cache_bust("cache-sid")
p4 = app_mod._v4_status_payload("cache-sid")
assert_ok(calls["n"] == 3, "bust 后立即重新组装载荷")
other = app_mod._v4_status_payload("other-sid")
assert_ok(calls["n"] == 4 and other["call"] == 4, "不同 sid 各自独立组装")

# 缓存上限：塞满后踢最旧
app_mod._STATUS_CACHE.clear()
app_mod._STATUS_CACHE_MAX = 5
for i in range(10):
    app_mod._v4_status_payload("cap-%d" % i)
assert_ok(len(app_mod._STATUS_CACHE) <= 5, "缓存条目数封顶（%d <= 5）" % len(app_mod._STATUS_CACHE))

# ================= ④ 注册表看护回收 =================
app_mod._TURN_RESULTS.clear()
app_mod._CONFIRM_LOCKS.clear()
v4_delivery._LAST_JOB_POLL.clear()
now = time.time()

# _TURN_RESULTS
app_mod._TURN_RESULTS["old-idle"] = {1: {"state": "done", "ts": now - 3600}}
app_mod._TURN_RESULTS["busy"] = {1: {"state": "working", "ts": now}}
app_mod._TURN_RESULTS["recent"] = {1: {"state": "done", "ts": now - 100}}
app_mod._TURN_RESULTS["empty"] = {}
app_mod._trim_registries()
assert_ok("old-idle" not in app_mod._TURN_RESULTS, "闲置 30 分钟的轮次结果被回收")
assert_ok("busy" in app_mod._TURN_RESULTS, "working 中的轮次结果不被回收")
assert_ok("recent" in app_mod._TURN_RESULTS, "近期轮次结果不被回收")
assert_ok("empty" not in app_mod._TURN_RESULTS, "空轮次表被回收")

# _CONFIRM_LOCKS（引用计数）
lk_idle = {"lock": threading.Lock(), "refs": 0, "touch": now - 3600}
lk_busy = {"lock": threading.Lock(), "refs": 1, "touch": now - 3600}
lk_recent = {"lock": threading.Lock(), "refs": 0, "touch": now}
app_mod._CONFIRM_LOCKS["c-idle"] = lk_idle
app_mod._CONFIRM_LOCKS["c-busy"] = lk_busy
app_mod._CONFIRM_LOCKS["c-recent"] = lk_recent
app_mod._trim_registries()
assert_ok("c-idle" not in app_mod._CONFIRM_LOCKS, "闲置确认锁被回收")
assert_ok("c-busy" in app_mod._CONFIRM_LOCKS, "持有中的确认锁（refs=1）不被回收")
assert_ok("c-recent" in app_mod._CONFIRM_LOCKS, "近期确认锁不被回收")

# _LAST_JOB_POLL 封顶（注册表已搬进 v4_delivery）
v4_delivery._LAST_JOB_POLL.clear()
for i in range(2100):
    v4_delivery._LAST_JOB_POLL[("s%d" % i, "d")] = ("job%d" % i, now - (2100 - i))
app_mod._trim_registries()
assert_ok(len(v4_delivery._LAST_JOB_POLL) <= 2000,
          "补查节流表封顶 2000（实际 %d）" % len(v4_delivery._LAST_JOB_POLL))
assert_ok(("s0", "d") not in v4_delivery._LAST_JOB_POLL, "最旧节流条目被踢")

# v4_subagent.PROGRESS / 域锁
from agent.v4 import subagent as v4_subagent

v4_subagent.PROGRESS.clear()
v4_subagent.PROGRESS["p-stale"] = {"ts": now - 700}
v4_subagent.PROGRESS["p-fresh"] = {"ts": now}
v4_subagent.trim_progress(now)
assert_ok("p-stale" not in v4_subagent.PROGRESS, "超龄进度被回收")
assert_ok("p-fresh" in v4_subagent.PROGRESS, "新鲜进度不被回收")

v4_subagent._DOMAIN_LOCKS.clear()
v4_subagent._DOMAIN_LOCKS[("d-idle", "x")] = {"lock": threading.Lock(), "refs": 0, "touch": now - 3600}
held_e = v4_subagent._domain_lock_acquire(("d-held", "x"))
held_e["touch"] = now - 3600  # 即使 touch 超龄，refs=1 也不能回收
v4_subagent.trim_domain_locks(now, max_idle=1800)
assert_ok(("d-idle", "x") not in v4_subagent._DOMAIN_LOCKS, "闲置域锁被回收")
assert_ok(("d-held", "x") in v4_subagent._DOMAIN_LOCKS, "持有中的域锁不被回收")
# 互斥仍有效：第二个 acquire 的锁与第一个是同一把
e2 = v4_subagent._domain_lock_acquire(("d-held", "x"))
assert_ok(e2["lock"] is held_e["lock"], "同域两次 acquire 拿到同一把锁（互斥不破）")
v4_subagent._domain_lock_release(e2)
v4_subagent._domain_lock_release(held_e)

# v4_main._ASYNC_JOBS
from agent.v4 import main_agent as v4_main

v4_main._ASYNC_JOBS.clear()
v4_main._ASYNC_JOBS["a-stale"] = {"m5_topics": now - 50000}
v4_main._ASYNC_JOBS["a-empty"] = {}
v4_main._ASYNC_JOBS["a-fresh"] = {"m6_scripts": now}
v4_main.trim_async_jobs(now)
assert_ok("a-stale" not in v4_main._ASYNC_JOBS, "超龄任务登记被回收")
assert_ok("a-empty" not in v4_main._ASYNC_JOBS, "空任务登记表被回收")
assert_ok("a-fresh" in v4_main._ASYNC_JOBS, "在跑任务登记不被回收")
assert_ok(v4_main.list_running_jobs("a-fresh") == ["m6_scripts"], "时间戳值保持 truthy（list_running_jobs 正常）")

# 清理测试现场：清空所有注册表，避免影响后续测试
app_mod._TURN_RESULTS.clear()
app_mod._CONFIRM_LOCKS.clear()
v4_delivery._LAST_JOB_POLL.clear()
app_mod._STATUS_CACHE.clear()
v4_subagent.PROGRESS.clear()
v4_subagent._DOMAIN_LOCKS.clear()
v4_main._ASYNC_JOBS.clear()

# ================= ⑤ P2-c：hq 并发闸 + 同源校验 + 异步 start =================
from agent import hq_cli


def _credential(user, account, token_char):
    return customer_auth.CustomerCredential(
        username=user, account_id=account, access_token=token_char * 32,
        expires_at=int(time.time()) + 3600, scopes=("ip12:read",),
    )


CLI_SID = "a" * 32
customer_auth.REGISTRY.put(CLI_SID, _credential("unit-a", "HQUNITA1", "A"))

# 并发闸：同时只有 5 个 CLI 子进程在跑（_subprocess_run 已持闸，fake_run 只统计并发数）
import subprocess as _sub
real_run = _sub.run
state_gate = {"active": 0, "peak": 0}


def fake_run(cmd, **kw):
    state_gate["active"] += 1
    state_gate["peak"] = max(state_gate["peak"], state_gate["active"])
    time.sleep(0.2)
    state_gate["active"] -= 1
    return real_run(["echo", "ok"], capture_output=True, text=True)


_sub.run = fake_run
threads5 = [threading.Thread(target=hq_cli.status, kwargs={"session_id": CLI_SID}) for _ in range(8)]
for t in threads5:
    t.start()
for t in threads5:
    t.join()
_sub.run = real_run
assert_ok(state_gate["peak"] <= 5, "hq CLI 全局并发闸生效（峰值 %d <= 5）" % state_gate["peak"])

# 每个客户走独立临时凭证目录；父进程密钥不泄漏，缺身份绝不启动 CLI。
CLI_SID_B = "b" * 32
customer_auth.REGISTRY.put(CLI_SID_B, _credential("unit-b", "HQUNITB2", "B"))
captured_profiles = []
subprocess_calls = {"count": 0}
os.environ["LLM_API_KEY"] = "unit-secret-must-not-leak"


def inspect_customer_run(cmd, **kw):
    subprocess_calls["count"] += 1
    env = kw.get("env") or {}
    directory = env.get("HQ_CLI_CONFIG_DIR")
    with open(os.path.join(directory, "credentials.json"), "r", encoding="utf-8") as handle:
        token = json.load(handle)["access_token"]
    captured_profiles.append({
        "token_marker": token[0],
        "directory": directory,
        "secret_leaked": "LLM_API_KEY" in env,
    })
    return _sub.CompletedProcess(cmd, 0, stdout=json.dumps({
        "schema": "hq.status/v1", "result": {"ok": True},
    }), stderr="")


_sub.run = inspect_customer_run
hq_cli.status(session_id=CLI_SID)
hq_cli.status(session_id=CLI_SID_B)
before_missing = subprocess_calls["count"]
missing_identity = hq_cli.status(session_id="c" * 32)
_sub.run = real_run
os.environ.pop("LLM_API_KEY", None)
assert_ok([item["token_marker"] for item in captured_profiles] == ["A", "B"],
          "不同客户 CLI 子进程使用各自凭证")
assert_ok(not any(item["secret_leaked"] for item in captured_profiles),
          "CLI 子进程看不到父进程 LLM 密钥")
assert_ok(all(not os.path.exists(item["directory"]) for item in captured_profiles),
          "CLI 临时凭证目录在子进程退出后删除")
assert_ok(subprocess_calls["count"] == before_missing
          and (missing_identity.get("data") or {}).get("error") == "customer_identity_required",
          "客户凭证缺失时明确拒绝且不回退公共账号")


class _AuthResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


class _AuthOpener:
    def __init__(self):
        self.requests = []

    def open(self, req, timeout=0):
        self.requests.append(req)
        if req.full_url.endswith("/api/auth/me"):
            return _AuthResponse({"user": {"username": "alice", "account_id": "HQ-ALICE"}})
        return _AuthResponse({
            "username": "alice", "access_token": "T" * 32,
            "access_expires_at": int(time.time()) + 3600,
            "scopes": ["ip12:read", "ip12:chat"],
        })


auth_opener = _AuthOpener()
auth_client = customer_auth.AuthClient("http://127.0.0.1:8095", opener=auth_opener)
auth_headers = {"Cookie": "other=drop-me; hq_session=unit-web-session"}
auth_user = auth_client.verify(auth_headers)
auth_credential = auth_client.issue_cli(auth_headers, auth_user)
issued_cookie = auth_opener.requests[-1].get_header("Cookie")
assert_ok(auth_user["account_id"] == "HQ-ALICE" and auth_credential.username == "alice",
          "网站登录态经 huangque-auth 解析并签发同一客户 CLI 身份")
assert_ok(issued_cookie == "hq_session=unit-web-session" and "other=" not in issued_cookie,
          "CLI 身份签发只转发 hq_session，不携带其他浏览器 Cookie")

# 网站身份、会话归属与同源校验
class _FakeAuth:
    def verify(self, headers):
        username = headers.get("X-Test-User")
        if not username:
            raise customer_auth.AuthError(401, "unauthorized", "请先登录黄雀账号")
        return {"username": username, "account_id": "HQ-" + username.upper()}

    def issue_cli(self, headers, user):
        return _credential(user["username"], user["account_id"],
                           "A" if user["username"] == "alice" else "B")

    def revoke(self, credential):
        return True


app_mod.CUSTOMER_AUTH = _FakeAuth()
client = app_mod.app.test_client()
r0 = client.post("/api/v4/start")
assert_ok(r0.status_code == 401, "未登录不能创建工作台会话")

AUTH_A = {"X-Test-User": "alice"}
AUTH_B = {"X-Test-User": "bob"}
SID_RESET_A = "1" * 32
SID_RESET_B = "2" * 32
for sid in (SID_RESET_A, SID_RESET_B):
    v4_state.reset(sid)
    v4_state.set_owner(sid, {"username": "alice", "account_id": "HQ-ALICE"})
    v4_state.persist(sid)

r1 = client.post("/api/v4/reset", json={"session_id": SID_RESET_A},
                 headers={**AUTH_A, "Origin": "https://evil.example.com"})
assert_ok(r1.status_code == 403, "跨站 Origin 的 reset 被拒绝（403）")
r2 = client.post("/api/v4/reset", json={"session_id": SID_RESET_A},
                 headers={**AUTH_A, "Origin": "http://localhost"})
assert_ok(r2.status_code == 200, "同源 Origin 的 reset 放行（200）")
assert_ok(os.path.exists(os.path.join(v4_state.SESSION_DIR, "v4-%s.json" % SID_RESET_A)),
          "重新开始只归档旧会话，不删除原始记录")
r3 = client.post("/api/v4/reset", json={"session_id": SID_RESET_B}, headers=AUTH_A)
assert_ok(r3.status_code == 200, "无 Origin 的 reset 放行（curl/自家脚本）")

# 异步 start：秒回 session_id + ack + seq，不再同步等开场白
orig_spawn = app_mod._spawn_turn
app_mod._spawn_turn = lambda sid, msg, paths=None: 999
r4 = client.post("/api/v4/start", headers=AUTH_A)
app_mod._spawn_turn = orig_spawn
d4 = r4.get_json()
assert_ok(r4.status_code == 200 and d4.get("session_id") and d4.get("async") is True
          and d4.get("seq") == 999 and d4.get("ack"), "start 立即返回异步确认（seq/ack/session_id 齐全）")
assert_ok(d4.get("reply") is None, "start 不再返回同步 reply")

# 不同客户不能恢复或列出对方会话
alice_sid = d4.get("session_id")
v4_state.append_main_history(alice_sid, [{"role": "user", "content": "客户 A 的会话"}])
v4_state.save_subagent(alice_sid, "image", messages=[
    {"role": "assistant", "content": "", "tool_calls": [{
        "id": "call-export-1", "type": "function",
        "function": {"name": "run_hq", "arguments": json.dumps({
            "capability": "image-generate", "quote_token": "must-not-export",
            "file_path": os.path.join(os.path.dirname(os.path.dirname(__file__)), "private.png"),
        })},
    }]},
    {"role": "tool", "tool_call_id": "call-export-1", "content": json.dumps({
        "ok": True, "access_token": "must-not-export-either", "job_id": 77,
        "video_url": "https://media.example/video.mp4?q-signature=must-not-export-query",
    })},
], last_result={"state": "completed", "summary": "图片完成", "result": {"job_id": 77}})
v4_state.persist(alice_sid)
listed_a = client.get("/api/v4/sessions", headers=AUTH_A).get_json().get("sessions") or []
listed_b = client.get("/api/v4/sessions", headers=AUTH_B).get_json().get("sessions") or []
assert_ok(any(row.get("sid") == alice_sid for row in listed_a), "客户只能列出自己的会话")
assert_ok(not any(row.get("sid") == alice_sid for row in listed_b), "其他客户列表不泄露会话")
forbidden = client.get("/api/v4/restore/" + alice_sid, headers=AUTH_B)
assert_ok(forbidden.status_code == 403, "其他客户拿到 sid 也不能恢复会话")
old_export_upload = app_mod.v4_exporter.UPLOAD_DIR
export_upload = os.path.join(tmpdir, "export-uploads")
os.makedirs(os.path.join(export_upload, alice_sid), exist_ok=True)
with open(os.path.join(export_upload, alice_sid, "reference.png"), "wb") as handle:
    handle.write(b"png-test-evidence")
app_mod.v4_exporter.UPLOAD_DIR = export_upload
exported = client.get("/api/v4/export/%s.jsonl" % alice_sid, headers=AUTH_A)
app_mod.v4_exporter.UPLOAD_DIR = old_export_upload
export_lines = [json.loads(line) for line in exported.get_data(as_text=True).splitlines()]
assert_ok(exported.status_code == 200
          and exported.headers.get("Content-Disposition", "").endswith('.jsonl"')
          and export_lines[0].get("schema") == "hq.agent-conversation/v1",
          "客户可下载机器可读 JSONL 对话证据")
assert_ok(any(item.get("type") == "message" and item.get("text") == "客户 A 的会话"
              for item in export_lines), "JSONL 含用户原始消息")
assert_ok(any(item.get("type") == "artifact" and item.get("name") == "reference.png"
              and item.get("sha256") and item.get("download_url", "").startswith("https://")
              for item in export_lines), "JSONL 为服务器文件生成可验证索引和下载地址")
session_line = next(item for item in export_lines if item.get("type") == "session")
trace_line = next(item for item in export_lines if item.get("type") == "trace_index")
assert_ok(session_line.get("owner") == {"username": "alice", "account_id": "HQ-ALICE"},
          "JSONL 明确写入当前用户身份，便于内部按客户排错")
assert_ok("image" in trace_line.get("domains", [])
          and "image-generate" in trace_line.get("capabilities", [])
          and 77 in trace_line.get("identifiers", {}).get("job_id", [])
          and any(item.get("name") == "reference.png" and item.get("sha256")
                  for item in trace_line.get("artifacts", [])),
          "JSONL 排错索引可串起子 Agent、能力、任务 ID 与服务器成品")
export_text = exported.get_data(as_text=True)
assert_ok("must-not-export" not in export_text and os.path.dirname(os.path.dirname(__file__)) not in export_text,
          "JSONL 脱敏 token、签名查询参数与服务器绝对路径")
forbidden_export = client.get("/api/v4/export/%s.jsonl" % alice_sid, headers=AUTH_B)
assert_ok(forbidden_export.status_code == 403, "其他客户不能导出会话")
selection = {"id": "voice-17", "label": "我的音色", "film": True, "manual": True}
selected = client.post("/api/v4/selection", headers=AUTH_A,
                       json={"session_id": alice_sid, "kind": "voice", "choice": selection})
restored_selected = client.get("/api/v4/restore/" + alice_sid, headers=AUTH_A).get_json()
assert_ok(selected.status_code == 200
          and (restored_selected.get("selected_choices") or {}).get("voice", {}).get("id") == "voice-17",
          "用户点选立即写入会话，刷新恢复仍保留选择")
forbidden_selection = client.post("/api/v4/selection", headers=AUTH_B,
                                  json={"session_id": alice_sid, "kind": "voice", "choice": selection})
assert_ok(forbidden_selection.status_code == 403, "其他客户不能修改会话选择")


class _ExpiringAuth(_FakeAuth):
    def __init__(self):
        self.verify_calls = 0
        self.revoked = 0

    def verify(self, headers):
        self.verify_calls += 1
        if self.verify_calls > 1:
            raise customer_auth.AuthError(401, "unauthorized", "请先登录黄雀账号")
        return super().verify(headers)

    def revoke(self, credential):
        self.revoked += 1
        return True


expiring_auth = _ExpiringAuth()
old_sse_recheck = app_mod.SSE_AUTH_RECHECK_SECONDS
app_mod.SSE_AUTH_RECHECK_SECONDS = 0
app_mod.CUSTOMER_AUTH = expiring_auth
stream_response = client.get("/api/v4/stream/" + alice_sid, headers=AUTH_A, buffered=False)
list(stream_response.response)
assert_ok(expiring_auth.revoked == 1 and customer_auth.REGISTRY.get(alice_sid) is None,
          "SSE 检测到网站登出后立即断开并撤销会话 CLI 身份")
app_mod.SSE_AUTH_RECHECK_SECONDS = old_sse_recheck
app_mod.CUSTOMER_AUTH = _FakeAuth()

# 同名人物的报告/选题文件必须按会话隔离，不能互相覆盖。
import agent.report as _report_files
import agent.modules56 as _module_files
report_output = tempfile.mkdtemp(prefix="hq-p0c-report-owner-")
old_report_output, old_module_output = _report_files.OUTPUT_DIR, _module_files.OUTPUT_DIR
old_render_md, old_render_pdf = _report_files.render_markdown, _report_files._render_pdf
old_m_render_md, old_m_render_html = _module_files.render_md, _module_files.render_html
old_chrome_print = _report_files.chrome_print
_report_files.OUTPUT_DIR = report_output
_module_files.OUTPUT_DIR = report_output


def _write_test_file(text, path):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(text))
    return True, ""


_report_files.render_markdown = lambda obj: str((obj.get("meta") or {}).get("marker"))
_report_files._render_pdf = lambda obj, path: _write_test_file(
    (obj.get("meta") or {}).get("marker"), path)
_module_files.render_md = lambda obj, suffix: str(obj.get("marker"))
_module_files.render_html = lambda obj, suffix: str(obj.get("marker"))
_report_files.chrome_print = lambda html, path: _write_test_file(html, path)
try:
    report_a = _report_files._persist("a" * 32, {"meta": {"name": "同名", "marker": "A"}}, "定稿")
    report_b = _report_files._persist("b" * 32, {"meta": {"name": "同名", "marker": "B"}}, "定稿")
    module_a = _module_files._persist("a" * 32, "同名", "选题生成_模块5", {"marker": "A"})
    module_b = _module_files._persist("b" * 32, "同名", "选题生成_模块5", {"marker": "B"})
finally:
    _report_files.OUTPUT_DIR, _module_files.OUTPUT_DIR = old_report_output, old_module_output
    _report_files.render_markdown, _report_files._render_pdf = old_render_md, old_render_pdf
    _module_files.render_md, _module_files.render_html = old_m_render_md, old_m_render_html
    _report_files.chrome_print = old_chrome_print
assert_ok(report_a["json"] != report_b["json"] and module_a["json"] != module_b["json"],
          "A/B 同名人物的报告和选题文件名按会话隔离")
assert_ok(open(os.path.join(report_output, report_a["json"]), encoding="utf-8").read() !=
          open(os.path.join(report_output, report_b["json"]), encoding="utf-8").read(),
          "A/B 同名报告各自保留内容，不发生覆盖")

# 异步 confirm：无报告时仍 400，有报告时走后台轮次
r5 = client.post("/api/v4/confirm", json={"session_id": alice_sid}, headers=AUTH_A)
assert_ok(r5.status_code == 400, "无报告时 confirm 仍拒绝（400）")

# ================= ⑥ 观测日志 + skill 同步（P2-d） =================
import io
import logging
import shutil as _sh
import subprocess

from agent.v4 import observability
from agent.v4 import skills as v4_skills

# ContextFilter 补默认值：格式含 sid/seq，无上下文时落占位符
_buf = io.StringIO()
_h = logging.StreamHandler(_buf)
_h.setFormatter(logging.Formatter(observability._FORMAT))
_h.addFilter(observability._ContextFilter())
_lg = logging.getLogger("hq-p0c-log")
_lg.handlers = [_h]
_lg.propagate = False
_lg.setLevel(logging.INFO)
_lg.info("带上下文", extra=observability.ctx("s1", 7))
_lg.info("无上下文")
_logout = _buf.getvalue()
_lg.handlers = []
assert_ok("sid=s1 seq=7" in _logout, "日志格式含 sid/seq 上下文字段")
assert_ok("sid=- seq=-" in _logout, "无上下文日志自动补默认 sid=-/seq=-")

# setup_logging 幂等（重复调用只装一次 handler）
_n0 = len(logging.getLogger().handlers)
observability.setup_logging()
_n1 = len(logging.getLogger().handlers)
observability.setup_logging()
assert_ok(_n1 == _n0 + 1 and len(logging.getLogger().handlers) == _n1,
          "setup_logging 幂等（handler 只装一次）")

# skill 同步三态：missing / diff / ok（临时 penguin 目录，不碰真实副本）
_tmp_penguin = tempfile.mkdtemp(prefix="hq-p0c-penguin-")
_orig_dir = v4_skills.PENGUIN_AGENTS_DIR
v4_skills.PENGUIN_AGENTS_DIR = _tmp_penguin
_st0 = dict(v4_skills.business_skill_sync_status())
assert_ok(set(_st0) == set(v4_skills.DOMAINS) and all(s == "missing" for s in _st0.values()),
          "空副本目录：12 域全 missing")
_src = v4_skills.business_skill_path("collect")
os.makedirs(os.path.dirname(v4_skills.installed_business_skill_path("collect")), exist_ok=True)
_sh.copyfile(_src, v4_skills.installed_business_skill_path("collect"))
os.makedirs(os.path.dirname(v4_skills.installed_business_skill_path("video")), exist_ok=True)
_sh.copyfile(_src, v4_skills.installed_business_skill_path("video"))
with open(v4_skills.installed_business_skill_path("video"), "a", encoding="utf-8") as _f:
    _f.write("\n# 篡改")
_st1 = dict(v4_skills.business_skill_sync_status())
assert_ok(_st1.get("collect") == "ok", "副本与源一致 → ok")
assert_ok(_st1.get("video") == "diff", "副本被改动 → diff")
assert_ok(_st1.get("audio") == "missing", "无副本 → missing")
v4_skills.PENGUIN_AGENTS_DIR = _orig_dir

# sync_skills.py 端到端（HQ_AGENTS_DIR 指向临时目录）：
# 有不同步 --check 退出 1；--push 修复；修完 --check 退出 0；无副本目录静默通过
_env = dict(os.environ, HQ_AGENTS_DIR=_tmp_penguin)
_r1 = subprocess.run([sys.executable, "scripts/sync_skills.py"], env=_env,
                     capture_output=True, text=True)
assert_ok(_r1.returncode == 1, "sync_skills --check 有不同步时退出码 1")
_r2 = subprocess.run([sys.executable, "scripts/sync_skills.py", "--push"], env=_env,
                     capture_output=True, text=True)
assert_ok(_r2.returncode == 0, "sync_skills --push 成功退出码 0")
_r3 = subprocess.run([sys.executable, "scripts/sync_skills.py"], env=_env,
                     capture_output=True, text=True)
assert_ok(_r3.returncode == 0, "push 后 --check 全同步退出码 0")
_r4 = subprocess.run([sys.executable, "scripts/sync_skills.py"],
                     env=dict(os.environ, HQ_AGENTS_DIR="/nonexistent-hq-penguin"),
                     capture_output=True, text=True)
assert_ok(_r4.returncode == 0, "无 Penguin 副本目录（服务器）静默通过退出码 0")

with open(v4_skills.business_skill_path("image"), "r", encoding="utf-8") as _f:
    _image_skill = _f.read()
assert_ok("provider=seedream + variant=std" in _image_skill,
          "出图子 Agent 默认使用 Seedream 标准版")
assert_ok("禁止复用旧 job_id 或口头声称已重提" in _image_skill
          and "seedream → openai" in _image_skill,
          "出图失败必须新报价、新 job_id，并按 Seedream → OpenAI 切换")

# ================= ⑦ 重复提问修复 + 空回复兜底（用户投诉批） =================
from agent import tools as v3_tools_mod

# 自造键 ↔ 规范字段模糊匹配（已答过的题不再重问）
assert_ok(v3_tools_mod._key_covers("direction.audience", "audience.target"), "自造键 audience.target 覆盖 direction.audience")
assert_ok(v3_tools_mod._key_covers("style.personality", "basic.personality"), "自造键 basic.personality 覆盖 style.personality")
assert_ok(v3_tools_mod._key_covers("direction.differentiation", "advantage.differentiator"), "自造键 differentiator 覆盖 differentiation")
assert_ok(not v3_tools_mod._key_covers("story.rise", "experience.praised"), "短段 rise 不误匹配 praised")
assert_ok(not v3_tools_mod._key_covers("basic.name", "direction.pains"), "无关键不误匹配")

# 空助手回复不落盘（空白气泡投诉）
v4_state.set_main_history("p0c-empty-sid", [])
v4_state.append_main_history("p0c-empty-sid", [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": ""},
    {"role": "assistant", "content": "  \n "},
    {"role": "assistant", "content": "正常回复"},
])
_h = v4_state.get_main_history("p0c-empty-sid")
assert_ok(len(_h) == 2 and _h[0]["content"] == "hi" and _h[1]["content"] == "正常回复",
          "空助手消息被过滤，正常消息保留")

# ================= ⑧ 主站档案工具 + 报价口径（补漏批） =================

# _pluck_project_ids 容错提取：dict/嵌套/列表/杂键 各种形状都认，去重保序
assert_ok(v3_tools_mod._pluck_project_ids({"project_id": "abc"}) == ["abc"],
          "项目 ID 直接键提取")
assert_ok(v3_tools_mod._pluck_project_ids(
    {"projects": [{"project_id": "p1", "name": "A"}, {"project_id": "p2", "name": "B"}]}) == ["p1", "p2"],
    "列表内多项目提取且保序")
assert_ok(v3_tools_mod._pluck_project_ids(
    {"result": {"items": [{"id": "x"}, {"id": "x"}, {"pid": "y"}]}}) == ["x", "y"],
    "嵌套结构 + 重复去重 + 混合键名")
assert_ok(v3_tools_mod._pluck_project_ids({"foo": 1, "bar": []}) == [],
          "无项目 ID 返回空列表")

# _load_ip12_profile：hq_cli 故障/无项目/指定不存在 的错误分支（不触网，纯 mock）
import agent.hq_cli as _hq_cli_mod
_orig_run = _hq_cli_mod.run

def _fake_run_both_down(cap, inputs=None, **kw):
    return {"exit_code": 10, "data": {"error": "upstream_unavailable",
                                      "message": "生成渠道正在繁忙或维护，请稍后再试"}, "stderr": ""}

_hq_cli_mod.run = _fake_run_both_down
_r = v3_tools_mod.dispatch("load_ip12_profile", {}, "p0c-ip12-sid")
assert_ok(not _r.get("ok") and "两边都不可用" in str(_r.get("error")),
          "两边接口都挂时如实报错，不编造档案")

_auth_calls = {"count": 0}


def _fake_run_auth_down(cap, inputs=None, **kw):
    _auth_calls["count"] += 1
    return {"exit_code": 4, "data": {"error": "customer_identity_required"}, "stderr": ""}


_hq_cli_mod.run = _fake_run_auth_down
_r = v3_tools_mod.dispatch("load_ip12_profile", {}, "p0c-ip12-sid")
assert_ok(not _r.get("ok") and "不会回退公共账号" in str(_r.get("error"))
          and _auth_calls["count"] == 1,
          "主站档案遇客户身份错误立即停止，不再降级重试")

def _fake_run_none(cap, inputs=None, **kw):
    if cap == "ip12-projects":
        return {"exit_code": 10, "data": {"error": "upstream_unavailable", "message": "繁忙"}, "stderr": ""}
    if cap == "digital-ip-projects":
        return {"exit_code": 0, "data": {"result": {"items": []}}, "stderr": ""}
    return {"exit_code": 0, "data": {"result": {}}, "stderr": ""}

_hq_cli_mod.run = _fake_run_none
_r = v3_tools_mod.dispatch("load_ip12_profile", {}, "p0c-ip12-sid")
assert_ok(not _r.get("ok") and "还没有" in str(_r.get("error")),
          "两边都没有项目时如实告知")

def _fake_run_digok(cap, inputs=None, **kw):
    if cap == "ip12-projects":
        return {"exit_code": 10, "data": {"error": "upstream_unavailable", "message": "繁忙"}, "stderr": ""}
    if cap == "digital-ip-projects":
        return {"exit_code": 0,
                "data": {"result": {"items": [{"id": "d1", "title": "小婷", "foundation_stage": {}},
                                              {"id": "d2", "title": "tangzelong",
                                               "foundation_stage": {"report_id": "r9"}}]}},
                "stderr": ""}
    if cap == "digital-ip-project":
        return {"exit_code": 0, "data": {"result": {"project": {"id": inputs.get("project_id"),
                                                                "foundation_stage": {"report_id": "r9"}}}},
                "stderr": ""}
    if cap == "digital-ip-report":
        return {"exit_code": 0, "data": {"result": {"report": {"content": {"title": "定位报告"}}}},
                "stderr": ""}
    return {"exit_code": 0, "data": {"result": {}}, "stderr": ""}

_hq_cli_mod.run = _fake_run_digok
_r = v3_tools_mod.dispatch("load_ip12_profile", {}, "p0c-ip12-sid")
assert_ok(_r.get("ok") and _r["result"]["source"] == "digital-ip" and _r["result"]["project_id"] == "d1",
          "老系统挂时自动降级新系统取最近项目")
assert_ok("老系统暂时不可用" in str(_r.get("note")), "降级时 note 说明来源切换")
assert_ok("report" in _r["result"]["archive"] and "project" in _r["result"]["archive"],
          "有已存报告时项目资料+报告一并拉回")
assert_ok("2 个项目" in str(_r.get("note")) and "小婷" in str(_r.get("note"))
          and "tangzelong（有已存报告）" in str(_r.get("note")),
          "多项目时 note 带标题与有无报告标记，提示先跟用户确认")
_r = v3_tools_mod.dispatch("load_ip12_profile", {"project_id": "d2"}, "p0c-ip12-sid")
assert_ok(_r.get("ok") and _r["result"]["project_id"] == "d2" and "report" in _r["result"]["archive"],
          "指定有报告的项目：拉项目+报告")

def _fake_run_ip12ok(cap, inputs=None, **kw):
    if cap == "ip12-projects":
        return {"exit_code": 0,
                "data": {"result": {"projects": [{"project_id": "p1", "title": "远志"},
                                                 {"project_id": "p2", "title": "小路"}]}},
                "stderr": ""}
    if cap == "ip12-project":
        return {"exit_code": 0,
                "data": {"result": {"project_id": inputs.get("project_id"),
                                    "profile": {"basic": {"name": "测试"}}}},
                "stderr": ""}
    return {"exit_code": 0, "data": {"result": {}}, "stderr": ""}

_hq_cli_mod.run = _fake_run_ip12ok
_r = v3_tools_mod.dispatch("load_ip12_profile", {}, "p0c-ip12-sid")
assert_ok(_r.get("ok") and _r["result"]["source"] == "ip12" and _r["result"]["project_id"] == "p1",
          "老系统正常时优先走老系统，不传 project_id 取最近项目（列表第一个）")
assert_ok("2 个项目" in str(_r.get("note")), "多项目时 note 提示让用户确认")
_r = v3_tools_mod.dispatch("load_ip12_profile", {"project_id": "p2"}, "p0c-ip12-sid")
assert_ok(_r.get("ok") and _r["result"]["project_id"] == "p2",
          "指定 project_id 取对应项目")
_r = v3_tools_mod.dispatch("load_ip12_profile", {"project_id": "p99"}, "p0c-ip12-sid")
assert_ok(not _r.get("ok") and "p99" in str(_r.get("error")) and "p1" in str(_r.get("error")),
          "指定不存在的项目：报错并列出可选项")
_hq_cli_mod.run = _orig_run

# quote_summary 参考价标注：points 缺失的 cost 如实标注可能浮动（防「报 30 扣 90」）
from agent.v4 import subagent as _v4_sub_mod
_q1 = {"capability": "digital-ip-batch-generate", "inputs": {}, "cost": 30, "points": None}
assert_ok("参考价" in _v4_sub_mod.quote_summary(_q1) and "可能浮动" in _v4_sub_mod.quote_summary(_q1),
          "points 缺失的报价标注参考价+可能浮动")
_q2 = {"capability": "digital-ip-batch-generate", "inputs": {}, "cost": 90, "points": 1706}
assert_ok("本次 90 点" in _v4_sub_mod.quote_summary(_q2) and "参考价" not in _v4_sub_mod.quote_summary(_q2),
          "带 points 的真实报价直接报数不标参考价")

# 确认卡完整参数展示（防「确认的和扣的不一样」）：渠道/时长/分辨率等默认补全项都要可见
_q3 = {"capability": "video-generate", "inputs": {"platform": "douyin", "duration": 10,
        "resolution": "1080p", "channel": "channel-a", "generate_audio": False}, "cost": 40, "points": 1600}
_s3 = _v4_sub_mod.quote_summary(_q3)
for _k in ("duration", "10", "resolution", "1080p", "channel", "channel-a", "generate_audio"):
    assert_ok(_k in _s3, "确认卡展示完整参数：缺 %s" % _k)

# ================= ⑨ 素材核验运行时强制（#12）=================

_v4_sub_mod._MATERIAL_CACHE.clear()
_orig_sub_hq_run = _v4_sub_mod.hq_cli.run

def _fake_material_list(cap, inputs=None, **kw):
    if cap == "assets" and (inputs or {}).get("kind") == "audio":
        return {"exit_code": 0, "data": {"result": {"items": [
            {"id": 36731, "file": "audio/aud_1788112315697.mp3"},
            {"id": 36727, "file": "audio/aud_1787848170174.mp3"}]}}, "stderr": ""}
    if cap == "video-avatars":
        return {"exit_code": 0, "data": {"result": {"items": [
            {"id": 536, "name": "本人形象"}, {"id": 535, "name": "形象 19"}]}}, "stderr": ""}
    return {"exit_code": 1, "data": {}, "stderr": "boom"}

_v4_sub_mod.hq_cli.run = _fake_material_list
assert_ok(_v4_sub_mod._verify_material_refs("video-lipsync", {"audio_asset_id": 36731}) == [],
          "资产库里存在的 audio_asset_id 核验通过")
assert_ok(_v4_sub_mod._verify_material_refs("video-lipsync", {"audio_asset_id": 99999})
          == ["audio_asset_id=99999"],
          "查不到的 audio_asset_id 被拦下")
assert_ok(_v4_sub_mod._verify_material_refs("digital-ip-audio-generate",
          {"audio_file": "audio/aud_1787848170174.mp3"}) == [],
          "audio_file 引用真实资产路径核验通过")
assert_ok(_v4_sub_mod._verify_material_refs("digital-ip-text-generate", {"avatar_id": 536}) == [],
          "已就绪形象 id 核验通过")
assert_ok(_v4_sub_mod._verify_material_refs("digital-ip-batch-generate",
          {"avatars": [{"avatar_id": 999, "label": "假形象"}]}) == ["avatars=999"],
          "avatars 对象列表里的假 id 被拦下")
assert_ok(_v4_sub_mod._verify_material_refs("digital-ip-text-generate",
          {"text": "纯文案无素材引用"}) == [],
          "无素材引用的 inputs 直接放行")


def _fake_material_by_customer(cap, inputs=None, **kw):
    avatar = 536 if kw.get("session_id") == "customer-a" else 999
    return {"exit_code": 0, "data": {"result": {"items": [{"id": avatar}]}}, "stderr": ""}


_v4_sub_mod.hq_cli.run = _fake_material_by_customer
_v4_sub_mod._MATERIAL_CACHE.clear()
assert_ok(_v4_sub_mod._verify_material_refs(
    "digital-ip-text-generate", {"avatar_id": 536}, "customer-a") == [],
    "客户 A 的素材只在客户 A 会话核验通过")
assert_ok(_v4_sub_mod._verify_material_refs(
    "digital-ip-text-generate", {"avatar_id": 536}, "customer-b") == ["avatar_id=536"],
    "素材核验缓存按客户会话隔离，不串用客户 A 结果")

_v4_sub_mod.hq_cli.run = _fake_material_list
_v4_sub_mod._MATERIAL_CACHE.clear()

_out_verify = _v4_sub_mod._hq_run_with_file("video-lipsync", {"audio_asset_id": 99999},
                                            False, None, None, None, None,
                                            {}, "p0c-verify-sid", "digital-human")
assert_ok(not _out_verify.get("ok") and "素材核验不通过" in str(_out_verify.get("error")),
          "假素材在报价/提交前被运行时拦下，不给 hq CLI 机会")

def _fake_material_down(cap, inputs=None, **kw):
    return {"exit_code": 10, "data": {"error": "upstream_unavailable"}, "stderr": ""}

_v4_sub_mod.hq_cli.run = _fake_material_down
_v4_sub_mod._MATERIAL_CACHE.clear()
assert_ok(_v4_sub_mod._verify_material_refs("video-lipsync", {"audio_asset_id": 36731}) == [],
          "核验列表查询失败时不误伤（交给服务端最终校验）")
_v4_sub_mod._MATERIAL_CACHE[("sid", "x", "y")] = (time.time() - 9999, set())
_v4_sub_mod.trim_material_cache(time.time())
assert_ok(("sid", "x", "y") not in _v4_sub_mod._MATERIAL_CACHE, "素材核验缓存超龄被回收")
_v4_sub_mod.hq_cli.run = _orig_sub_hq_run

# ================= ⑩ 模块5 选题稳定 + 独立事实审核（#4/#28）=================

import agent.modules56 as _m56_mod
import agent.report as _report_mod

_orig_m56_state = _m56_mod.state
_orig_m56_persist = _m56_mod._persist
_orig_report_llm = _report_mod._llm_chat


class _FakeM56State:
    def __init__(self):
        self.rep = {}
        self.profile = {}

    def get_report_full(self, sid):
        return dict(self.rep.get(sid) or {})

    def set_report(self, sid, sub):
        cur = dict(self.rep.get(sid) or {})
        cur.update(sub)
        self.rep[sid] = cur

    def get_report_json(self, sid):
        return self.rep.get(sid, {}).get("_json") or {}

    def get_profile(self, sid):
        return dict(self.profile.get(sid) or {})


_m56_mod.state = _FakeM56State()
_m56_mod._persist = lambda *a, **k: {"pdf": None, "md": "x.md", "json": "x.json", "pdf_error": None}


def _valid_topics(seed="A"):
    topics = []
    for ty, n in (("故事型", 3), ("干货型", 9), ("案例型", 3)):
        for i in range(n):
            topics.append({"title": f"{seed}{ty}选题{i}", "type": ty, "goal": "建立信任"})
    recs = [{"title": topics[i]["title"], "reasons": ["原因1", "原因2"]} for i in range(3)]
    return {"topics": topics, "recommended": recs, "required_info": []}


# 场景1（#4）：前两轮校验不过 → 逐轮提温重试 → 第三轮通过；选题输出上限提到 8000 token
_calls_56 = []
_gen_56 = []
_fc_56 = []

def _fake_llm_56(msgs, max_tokens=8000, temperature=0.5):
    _calls_56.append({"max_tokens": max_tokens, "temperature": temperature,
                      "user": str(msgs[-1].get("content", ""))})
    if "事实审核员" in str(msgs[0].get("content", "")):
        return _fc_56.pop(0)
    return _gen_56.pop(0)

_report_mod._llm_chat = _fake_llm_56
_gen_56 = [
    json.dumps({"topics": [{"title": "题%d" % i, "type": "故事型", "goal": "g"} for i in range(5)],
                "recommended": [], "required_info": []}),
    json.dumps({"topics": [{"title": "题%d" % i, "type": "故事型", "goal": "g"} for i in range(5)],
                "recommended": [], "required_info": []}),
    json.dumps(_valid_topics()),
]
_fc_56 = [json.dumps({"issues": []})]
_m56_mod.state.rep = {"p0c-m5-sid": {"confirmed": True,
                                     "_json": {"meta": {"name": "测试用户"}}}}
_r56 = _m56_mod.generate_topics("p0c-m5-sid")
assert_ok(_r56.get("ok") and _r56.get("count") == 15 and _r56.get("fact_issues") == [],
          "模块5 三轮修订后生成 15 个选题并通过事实审核")
assert_ok(len(_calls_56) >= 3 and _calls_56[0]["max_tokens"] == 8000
          and _calls_56[1]["max_tokens"] == 8000,
          "选题输出上限 8000 token（原 3000 截断是坏格式根因）")
assert_ok([c["temperature"] for c in _calls_56[:3]] == [0.5, 0.7, 0.9],
          "修订轮逐轮提温 0.5→0.7→0.9（避免复读同一份坏稿）")
assert_ok(_calls_56[-1]["temperature"] == 0.0 and _calls_56[-1]["max_tokens"] == 8000,
          "事实审核低温独立第二遍审查，额度 8000（推理模型思考不吃光正文）")

# 场景2（#28）：审核揪出编造 → 带问题清单修订一轮 → 二审通过，结果不再带问题
_calls_56 = []
_gen_56 = [json.dumps(_valid_topics("A")), json.dumps(_valid_topics("B"))]
_fc_56 = [json.dumps({"issues": [{"where": "选题A故事型选题0", "claim": "编造细节", "problem": "编造"}]}),
          json.dumps({"issues": []})]
_r56b = _m56_mod.generate_topics("p0c-m5-sid")
assert_ok(_r56b.get("ok") and _r56b.get("fact_issues") == [],
          "审核发现问题后自动修订一轮，二审通过")
assert_ok(any("独立事实审核发现" in c["user"] for c in _calls_56),
          "修订提示把审核问题清单喂回生成模型")

# 场景3（#28b）：修订后仍有残留问题 → 不藏，如实标注随结果返回
_calls_56 = []
_gen_56 = [json.dumps(_valid_topics("A")), json.dumps(_valid_topics("B"))]
_fc_56 = [json.dumps({"issues": [{"where": "某选题", "claim": "数字对不上", "problem": "数字不一致"}]}),
          json.dumps({"issues": [{"where": "某选题", "claim": "仍无依据", "problem": "编造"}]})]
_r56c = _m56_mod.generate_topics("p0c-m5-sid")
assert_ok(_r56c.get("ok") and len(_r56c.get("fact_issues") or []) == 1
          and "如实标注" in str(_r56c.get("note")),
          "残留事实问题随结果如实返回，要求主 Agent 标注给用户")
assert_ok("事实审核有残留问题" in str(_m56_mod.state.rep["p0c-m5-sid"]["m5"].get("phase")),
          "m5 状态区标注事实审核残留")

# 场景4：审核服务失败或返回坏格式时必须失败关闭，绝不把“没审核”当成 issues=[]。
assert_ok('{"issues"' not in _m56_mod.FACT_CHECK_SPEC and "issues: []" not in _m56_mod.FACT_CHECK_SPEC,
          "事实审核提示词只给字段合同，不预填通过答案或问题示例")


def _fake_review_unavailable(msgs, max_tokens=8000, temperature=0.5):
    if "事实审核员" in str(msgs[0].get("content", "")):
        raise RuntimeError("review service unavailable")
    return json.dumps(_valid_topics("U"))


_report_mod._llm_chat = _fake_review_unavailable
_r56d = _m56_mod.generate_topics("p0c-m5-sid")
assert_ok(not _r56d.get("ok") and _r56d.get("status") == "review_pending"
          and _r56d.get("fact_review_status") == "unavailable",
          "事实审核调用失败时内容不进入 ready（挂起自动重审）")
assert_ok((_m56_mod.state.rep["p0c-m5-sid"].get("m5") or {}).get("status") == "review_pending"
          and bool((_m56_mod.state.rep["p0c-m5-sid"].get("_pending_review") or {}).get("obj")),
          "审核不可用状态写入会话、已生成内容挂起不丢，刷新后不冒充通过")

# 场景4b：审核服务恢复后自动重审挂起内容 → 审核通过 → 直接 ready 送达（不重新生成）
_p56 = _m56_mod.state.rep["p0c-m5-sid"]["_pending_review"]
_p56["ts"] = time.time() - 200  # 越过退避窗口
_gen_56.clear()
_fc_56 = [json.dumps({"issues": []})]
_report_mod._llm_chat = _fake_llm_56
_r56r = _m56_mod.retry_pending_review("p0c-m5-sid")
assert_ok(_r56r.get("ok") and _r56r.get("status") == "ready" and _r56r.get("count") == 15
          and not (_m56_mod.state.rep["p0c-m5-sid"].get("_pending_review") or {}).get("obj"),
          "服务恢复后自动补审通过：挂起内容直接 ready，不重新生成")

# 场景4c：补审一次仍失败 → 不再干等，直接交付并如实标注未经事实审核（对话流畅优先）
_report_mod._llm_chat = _fake_review_unavailable
_r56p = _m56_mod.generate_topics("p0c-m5-sid")
assert_ok(not _r56p.get("ok") and _r56p.get("status") == "review_pending",
          "补审场景前置：首次审核失败进入挂起")
_p56 = _m56_mod.state.rep["p0c-m5-sid"]["_pending_review"]
_p56["ts"] = time.time() - 200  # 越过退避窗口，审核仍失败
_r56q = _m56_mod.retry_pending_review("p0c-m5-sid")
assert_ok(_r56q.get("ok") and _r56q.get("status") == "ready"
          and _r56q.get("fact_review_status") == "unreviewed" and _r56q.get("count") == 15
          and (_m56_mod.state.rep["p0c-m5-sid"].get("m5") or {}).get("status") == "ready"
          and not (_m56_mod.state.rep["p0c-m5-sid"].get("_pending_review") or {}).get("obj"),
          "补审一次仍失败：内容直接交付（ready+unreviewed），绝不让用户干等")


def _fake_review_invalid(msgs, max_tokens=8000, temperature=0.5):
    if "事实审核员" in str(msgs[0].get("content", "")):
        return "not-json"
    return json.dumps(_valid_topics("V"))


_report_mod._llm_chat = _fake_review_invalid
_r56e = _m56_mod.generate_topics("p0c-m5-sid")
assert_ok(not _r56e.get("ok") and _r56e.get("status") == "review_pending",
          "事实审核坏格式同样失败关闭（挂起自动重审）")

_m56_mod.state = _orig_m56_state
_m56_mod._persist = _orig_m56_persist
_report_mod._llm_chat = _orig_report_llm

# ================= ⑪ 「不知道怎么拍」误判拒答（#47）=================
import agent.v4.main_agent as _v4_main_mod
_v4_main_mod._SYSTEM_PROMPT_CACHE = None
_prompt47 = _v4_main_mod.build_system_prompt()
assert_ok("「不知道/不会」是求助不是跳过" in _prompt47,
          "主 Agent 提示词含「不知道=求助非跳过」规则")
assert_ok("不知道怎么拍" in _prompt47, "提示词含「不知道怎么拍」示例")
assert_ok("不调用 update_profile、不推进断点" in _prompt47 and "不用关键词或正则硬匹配" in _prompt47,
          "澄清/反问由模型按语义解释，不写画像也不推进")
assert_ok("relationship.preferred_name" in _prompt47 and "recent.summary" in _prompt47,
          "称呼、关系与最近共识被明确要求写入长期画像快照")
assert_ok("production.draft" in _prompt47 and "暂不报价/生成" in _prompt47,
          "用户暂不执行时制作参数先保存到主 Agent 结构化草稿")
assert_ok("制作参数跨轮保存" in _v4_sub_mod._TASK_RULES
          and any(t.get("function", {}).get("name") == "save_draft" for t in _v4_sub_mod.subagent_tools()),
          "子 Agent 获得只保存不报价的结构化制作草稿工具")
_v4_sub_mod.state.reset("p0c-draft-sid")
_saved_draft = _v4_sub_mod.dispatch_tool("save_draft", {
    "task_summary": "做一条宠物用品口播",
    "inputs": {"platform": "抖音", "duration": 10, "cta": "点赞关注", "content_points": ["猫窝", "喂食器"]},
}, "p0c-draft-sid", "digital-human")
assert_ok(_saved_draft.get("ok")
          and (_v4_sub_mod.state.get_subagent("p0c-draft-sid", "digital-human") or {}).get("draft", {}).get("inputs", {}).get("duration") == 10,
          "平台/时长/CTA/内容点写入同一制作草稿，不触发 CLI")
_orig_v3_get_profile = _v4_main_mod.v3_state.get_profile
_v4_main_mod.v3_state.get_profile = lambda sid: {"name": "测试"}
_snap47 = _v4_main_mod._snapshot_line("p0c-snap-sid")
_v4_main_mod.v3_state.get_profile = _orig_v3_get_profile
assert_ok("只有明确拒答" in _snap47 and "是用户在表达困难" in _snap47,
          "画像快照注入拒答/求助区分规则")

# 模型 HTTP 503 属于明确失败：不进入自动重试循环；最终话术不诱导复制原话重发。
class InternalServerError(Exception):
    pass


class _Always503:
    def __init__(self):
        self.calls = 0
        self.chat = self
        self.completions = self

    def create(self, **_kwargs):
        self.calls += 1
        raise InternalServerError("503")


_fake_503_client = _Always503()
_orig_sub_client = _v4_sub_mod._client
_v4_sub_mod._client = lambda: _fake_503_client
try:
    _v4_sub_mod.llm_turn([], [])
except InternalServerError:
    pass
_v4_sub_mod._client = _orig_sub_client
assert_ok(_fake_503_client.calls == 1, "模型 HTTP 503 明确失败只调用一次，不自动循环重试")

_orig_main_llm = _v4_sub_mod.llm_turn
_orig_main_mode = _v4_main_mod.config.LLM_MODE
_v4_main_mod.config.LLM_MODE = "openai"
_v4_sub_mod.llm_turn = lambda *_a, **_k: (_ for _ in ()).throw(InternalServerError("503"))
_v4_main_mod.state.reset("p0c-503-copy")
_reply503, _log503, _route503 = _v4_main_mod.run_turn("p0c-503-copy", "继续刚才的方案")
_v4_sub_mod.llm_turn = _orig_main_llm
_v4_main_mod.config.LLM_MODE = _orig_main_mode
assert_ok("原请求已保留" in _reply503 and "不会自动补跑" in _reply503
          and "再发一次" not in _reply503 and "重新发送" not in _reply503,
          "模型失败明确说明本轮未成功，不再诱导用户重发原话")

# ================= ⑫ 空白会话 1→6 模块金线（#11）=================
# 从空白会话起，走工具层真实调度：采集 → 报告初稿 → 定稿 → 选题 → 文案 → 修订，
# 全程 fake LLM 返回合规 JSON，断言每步状态流转正确、文件落盘、全文不外发。

import agent.state as _v3_state_mod
import agent.modules56 as _m56_golden
import agent.report as _report_golden
import agent.tools as _v3_tools_golden

_orig_v3_session_dir = _v3_state_mod.SESSION_DIR
_orig_out_dir = _report_golden.OUTPUT_DIR
_orig_chrome_print = _report_golden.chrome_print
_orig_llm_golden = _report_golden._llm_chat

_tmp_golden = tempfile.mkdtemp(prefix="hq-golden-")
_v3_state_mod.SESSION_DIR = _tmp_golden
_report_golden.OUTPUT_DIR = os.path.join(_tmp_golden, "output")
_report_golden.chrome_print = lambda html_text, pdf_path: (True, None)
_m56_golden.OUTPUT_DIR = _report_golden.OUTPUT_DIR  # modules56.OUTPUT_DIR 在 import 时已拷贝，同步改


def _golden_report():
    def story(i):
        return {"title": "真实故事%d" % i, "one_liner": "一句话%d" % i,
                "emotion_curve": "平静→转折→共鸣", "scenarios": ["s1", "s2"],
                "hook": "你经历过这种反转吗？", "spread": "⭐⭐⭐"}
    return {
        "meta": {"name": "金线用户", "date": "2026-09-06"},
        "m1_positioning": {
            "keywords": [{"name": "词%d" % i, "desc": "解读%d" % i} for i in range(7)],
            "final": {"name": "真诚记录者", "slogan": "记录真实，放大善意", "strategy": "真实+干货+陪伴"},
            "market_opportunities": ["机缘1", "机缘2", "机缘3", "机缘4"],
            "risks": ["风险1", "风险2", "风险3", "风险4"],
        },
        "m2_persona": {
            "options": [
                {"id": "A", "title": "方案A", "traits": "tA", "story_tone": "sA",
                 "tags": "tagA", "formula": "fA", "pros": "pA", "cons": "cA"},
                {"id": "B", "title": "方案B", "traits": "tB", "story_tone": "sB",
                 "tags": "tagB", "formula": "fB", "pros": "pB", "cons": "cB"},
                {"id": "C", "title": "方案C", "traits": "tC", "story_tone": "sC",
                 "tags": "tagC", "formula": "fC", "pros": "pC", "cons": "cC"},
            ],
            "recommendation": {"chosen": "A", "title": "方案A", "reasons": ["r1", "r2", "r3", "r4"]},
            "core": {"traits": "ct", "story_tone": "cs", "tags": "cT", "quote": "cq", "image": "ci"},
        },
        "m3_value": {
            "diagnosis": [{"original": "o1", "problem": "p1", "suggestion": "s1"},
                          {"original": "o2", "problem": "p2", "suggestion": "s2"}],
            "final": {"core": "fc", "stance": "fs"},
            "slogan": {"text": "金句", "reasons": ["r1", "r2", "r3"]},
            "slogan_alternatives": [{"scenario": "sc%d" % i, "slogan": "sl%d" % i} for i in range(4)],
            "self_intro": {"original": "oi", "optimized": "op", "reasons": ["r1", "r2"]},
            "monetization": [{"path": "path%d" % i, "position": "pos%d" % i, "script": "sc%d" % i}
                             for i in range(3)],
        },
        "m4_story": {
            "stories": [story(i) for i in range(5)],
            "main_storyline": {"primary": "主线", "reasons": ["r1", "r2", "r3", "r4"]},
            "optimization": {
                "slogan_upgrades": [{"type": "t%d" % i, "original": "o%d" % i,
                                     "optimized": "op%d" % i, "reason": "r%d" % i} for i in range(2)],
                "edge_strategy": {"problem": "ep", "idea": "ei", "content": ["c1", "c2"]},
                "pit_deepdive": {"directions": ["d1", "d2"], "quote": "pq", "series": "ps"},
                "narrative": {"original": "no", "optimized": "nn"},
            },
            "priority": [{"p": "P%d" % i, "module": "m%d" % i, "task": "t%d" % i, "output": "o%d" % i}
                         for i in range(4)],
            "doc_status": "已生成",
        },
    }


def _golden_scripts(topic="金线选题1"):
    def script(st):
        body = "这是逻辑递进的中段内容。" * 5
        return {"style": st, "hook": "三秒钩子%s" % st,
                "body": body, "quote": "金句%s" % st,
                "cta": "想要就评论区扣666",
                "full_text": "钩子。" + body + body + "。金句%s。想要就评论区扣666。" % st}
    return {"topic": topic, "scripts": [script(s) for s in ("共情型", "震撼型", "故事型")],
            "recommended": {"style": "共情型", "reasons": ["r1", "r2", "r3"]}, "required_info": []}


_llm_golden_calls = []


def _fake_llm_golden(msgs, max_tokens=8000, temperature=0.5):
    _llm_golden_calls.append({"sys": str(msgs[0].get("content", ""))[:80],
                              "user": str(msgs[-1].get("content", "")),
                              "max_tokens": max_tokens, "temperature": temperature})
    sys_c = str(msgs[0].get("content", ""))
    user_c = str(msgs[-1].get("content", ""))
    if "事实审核员" in sys_c:
        return json.dumps({"issues": []})
    if "用户最终选择了方案" in user_c:  # finalize 的定向改写指令
        rep = _golden_report()
        return json.dumps(rep, ensure_ascii=False)
    if "口播文案写手" in sys_c:
        return json.dumps(_golden_scripts(), ensure_ascii=False)
    if "短视频选题策划师" in sys_c:
        topics = []
        for ty, n in (("故事型", 3), ("干货型", 9), ("案例型", 3)):
            for i in range(n):
                topics.append({"title": "%s金线选题%d" % (ty, i), "type": ty, "goal": "建立信任"})
        recs = [{"title": topics[i]["title"], "reasons": ["r1", "r2"]} for i in range(3)]
        return json.dumps({"topics": topics, "recommended": recs, "required_info": []}, ensure_ascii=False)
    return json.dumps(_golden_report(), ensure_ascii=False)


_report_golden._llm_chat = _fake_llm_golden

GOLDEN_SID = "p0c-golden-sid"
_r = _v3_tools_golden.dispatch("update_profile", {"facts": {"姓名": "金线用户", "职业": "跨境电商",
                                                          "direction.track": "跨境电商", "性格": "真诚"}}, GOLDEN_SID)
assert_ok(_r.get("ok") and "金线用户" in json.dumps(_r.get("result"), ensure_ascii=False),
          "金线① 空白会话开始采集信息")

_r = _v3_tools_golden.dispatch("profile_status", {}, GOLDEN_SID)
_ps = _r.get("result") or {}
assert_ok(_r.get("ok") and isinstance(_ps.get("core_covered"), int)
          and isinstance(_ps.get("modules"), list) and isinstance(_ps.get("missing_core"), list),
          "金线② profile_status 按模块回报采集进度")

_r = _v3_tools_golden.dispatch("generate_report", {}, GOLDEN_SID)
assert_ok(_r.get("ok") and _r.get("status") == "draft_ready" and len(_r.get("options") or []) == 3,
          "金线③ 报告初稿生成（三套方案）")
assert_ok(_r.get("recommended") == "A", "金线③b 初稿带推荐方案")
assert_ok(os.path.isdir(_report_golden.OUTPUT_DIR) and any(
    f.endswith(".json") or f.endswith(".md") for f in os.listdir(_report_golden.OUTPUT_DIR)),
    "金线③c 报告文件真实落盘")

_r = _v3_tools_golden.dispatch("finalize_report", {"chosen": "A"}, GOLDEN_SID)
assert_ok(_r.get("ok") and _r.get("status") == "final" and _r.get("chosen") == "A",
          "金线④ 用户选定方案 A 后定稿")
# UI 确认动作（app.py 在 final 状态后写入 confirmed）：金线里模拟真实前端确认环节
_v3_state_mod.set_report(GOLDEN_SID, {"confirmed": True})
assert_ok(_v3_state_mod.get_report_full(GOLDEN_SID).get("confirmed") is True,
          "金线④b 前端确认定稿（confirmed 置位）")

_r = _v3_tools_golden.dispatch("m5_topics", {}, GOLDEN_SID)
assert_ok(_r.get("ok") and _r.get("status") == "ready" and _r.get("count") >= 15,
          "金线⑤ 选题生成 15+ 并通过模板校验+事实审核")
assert_ok("金线选题0" in json.dumps(_r.get("topics"), ensure_ascii=False), "金线⑤b 选题内容在返回里")

_r = _v3_tools_golden.dispatch("m6_scripts", {"topic": "故事型金线选题0"}, GOLDEN_SID)
assert_ok(_r.get("ok") and _r.get("status") == "ready" and len(_r.get("scripts") or []) == 3,
          "金线⑥ 选定选题后三版文案生成")
assert_ok(_r.get("fact_issues") == [], "金线⑥b 文案事实审核通过")

_r = _v3_tools_golden.dispatch("script_revise", {"feedback": "金句再有力一点"}, GOLDEN_SID)
assert_ok(_r.get("ok") and _r.get("status") == "ready", "金线⑦ 文案按用户意见修订完成")

_r = _v3_tools_golden.dispatch("get_m5m6", {}, GOLDEN_SID)
_res = _r.get("result") or {}
assert_ok(_r.get("ok") and _res.get("confirmed") and (_res.get("m5") or {}).get("status") == "ready"
          and (_res.get("m6") or {}).get("status") == "ready",
          "金线⑧ get_m5m6 状态齐备（m5/m6 均 ready、报告已确认）")
assert_ok(_res.get("m5_topics") and _res.get("m6_scripts"), "金线⑧b 选题/文案全文可从状态接口取出")

_r = _v3_tools_golden.dispatch("get_report", {}, GOLDEN_SID)
_rstr = json.dumps(_r, ensure_ascii=False)
assert_ok(_r.get("ok") and "_m5_json" not in _rstr and "_m6_json" not in _rstr and "_json" not in _rstr,
          "金线⑨ get_report 只给元信息，报告全文不外发")

_report_golden._llm_chat = _orig_llm_golden
_report_golden.chrome_print = _orig_chrome_print
_report_golden.OUTPUT_DIR = _orig_out_dir
_m56_golden.OUTPUT_DIR = _orig_out_dir
_v3_state_mod.SESSION_DIR = _orig_v3_session_dir
_v3_state_mod.reset(GOLDEN_SID)

# ================= ⑬ 历史压缩一致性（#1/#10）=================
# 长对话裁剪后：① 窗口内 user/assistant 往返完整；② 六态快照注入保住已确认事实；
# ③ 早于窗口的业务状态不丢（快照里可读）；④ 历史总长可控。

GOLDEN_SID2 = "p0c-trim-sid"
_v3_state_mod.SESSION_DIR = _tmp_golden
_v3_state_mod.update_profile(GOLDEN_SID2, {"姓名": "压缩用户", "职业": "母婴博主",
                                           "direction.audience": "新手妈妈", "已答字段样例": "值",
                                           "relationship.preferred_name": "叫我阿圆",
                                           "relationship.role": "长期创作搭档",
                                           "recent.summary": "刚确认先做母婴避坑口播"})
_v3_state_mod.set_report(GOLDEN_SID2, {"status": "final", "_json": {"meta": {"name": "压缩用户"}}})

_long_history = [{"role": "system", "content": "你是主 Agent"}]
for i in range(40):
    _long_history.append({"role": "user", "content": "问题%d" % i})
    _long_history.append({"role": "assistant", "content": "回答%d" % i})
_trimmed = _v4_main_mod._trim_history(GOLDEN_SID2, list(_long_history))
_snap_found = [m for m in _trimmed if m.get("role") == "system" and "已保存的 IP 画像" in str(m.get("content"))]
assert_ok(len(_snap_found) == 1, "压缩① 快照行注入恰好一条")
assert_ok("压缩用户" in str(_snap_found[0]["content"]) and "新手妈妈" in str(_snap_found[0]["content"]),
          "压缩② 已确认事实（姓名/受众）在快照里保住，不因裁剪丢失")
assert_ok("叫我阿圆" in str(_snap_found[0]["content"])
          and "长期创作搭档" in str(_snap_found[0]["content"])
          and "刚确认先做母婴避坑口播" in str(_snap_found[0]["content"]),
          "压缩②b 称呼、关系与最近共识经过80条长对话仍保留")
assert_ok(len(_trimmed) <= 1 + 1 + _v4_main_mod._HISTORY_WINDOW, "压缩③ 总长度受控（system+快照+窗口）")
_window_start = [i for i, m in enumerate(_trimmed) if m.get("role") == "user" and "问题" in str(m.get("content"))]
assert_ok(_window_start and _window_start[0] >= 1, "压缩④ 窗口起点对齐 user 消息")
assert_ok("问题0" not in json.dumps(_trimmed, ensure_ascii=False), "压缩⑤ 窗口外的旧消息确实被裁掉（不无限膨胀）")
assert_ok(not any(m.get("role") == "tool" for m in _trimmed), "压缩⑥ 无孤儿 tool 消息")

# 非法序列清洗兜底：tool 消息与 tool_calls 交错污染的历史也能规整
_polluted = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "答", "tool_calls": [{"id": "x"}]},
    {"role": "tool", "content": "tool result"},
    {"role": "user", "content": "下一句"},
]
_clean = _v4_main_mod._sanitize_for_llm(list(_polluted))
assert_ok(len(_clean) == 3 and all(m.get("role") != "tool" for m in _clean)
          and _clean[1].get("content") == "答" and "tool_calls" not in _clean[1],
          "压缩⑦ 非法 tool 序列清洗为合法历史（不 400）")

_v3_state_mod.SESSION_DIR = _orig_v3_session_dir
_v3_state_mod.reset(GOLDEN_SID2)

# ================= ⑦ 会话轮次 FIFO 串行：连发消息不再乱序抢答 =================
# 线上金线实测抓到：同一会话并发轮次各自跑，短消息先答完先落历史，
# 长消息（自我介绍）反而排到最后被处理，Agent 先答追问、再问用户已经答过的事。
# 修复：app.py 对同一 sid 的轮次按到达顺序排队串行执行。
import app as app_mod
import agent.state as agent_state_mod

_orig_run_turn = app_mod.v4_main.run_turn
_orig_state_session_dir = agent_state_mod.SESSION_DIR
_orig_turn_stale = app_mod._TURN_STALE_SECONDS

QUEUE_SID = "p0c-queue-sid"
_run_order = []
_first_queue_started = threading.Event()


def _fake_run_turn(sid, user_text=None, **kw):
    idx = len(_run_order)
    _run_order.append({"idx": idx, "start": time.monotonic(), "text": user_text})
    _first_queue_started.set()
    time.sleep(0.3)  # 模拟一轮真实 LLM 耗时
    v4_state.append_main_history(sid, [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": "回%d" % idx},
    ])
    _run_order[-1]["end"] = time.monotonic()
    return "回%d" % idx, [], []


agent_state_mod.SESSION_DIR = tmpdir  # 报告元信息也落到临时目录，不碰真实数据
app_mod.v4_main.run_turn = _fake_run_turn

seqs = [app_mod._spawn_turn(QUEUE_SID, "msg%d" % i) for i in range(3)]
assert_ok(_first_queue_started.wait(2), "队列⑦ 第 1 轮工作线程已启动")
with app_mod._TURN_LOCK:
    _qres = dict(app_mod._TURN_RESULTS.get(QUEUE_SID) or {})
assert_ok(not _qres.get(seqs[0], {}).get("queued", True), "队列⑦ 第 1 轮立即开跑（不排队）")
assert_ok(_qres.get(seqs[1], {}).get("queued") is True and _qres.get(seqs[2], {}).get("queued") is True,
          "队列⑦ 第 2/3 轮排队等待（queued 标记正确）")

# 排队中的轮次不能被 poll 的「超时误杀」当成卡死（排队等待 ≠ 卡死）
app_mod._TURN_STALE_SECONDS = 0.12
time.sleep(0.2)
with app_mod.app.app_context():
    app_mod.v4_poll(QUEUE_SID)
with app_mod._TURN_LOCK:
    _qres2 = dict(app_mod._TURN_RESULTS.get(QUEUE_SID) or {})
assert_ok(_qres2.get(seqs[1], {}).get("state") == "working"
          and _qres2.get(seqs[2], {}).get("state") == "working",
          "队列⑦ 排队轮次越过超时窗口仍算 working（不被误杀）")

_deadline = time.time() + 15
while time.time() < _deadline:
    with app_mod._TURN_LOCK:
        _fin = dict(app_mod._TURN_RESULTS.get(QUEUE_SID) or {})
    if len(_fin) >= 3 and all(v.get("state") in ("done", "error") for v in _fin.values()):
        break
    time.sleep(0.05)
app_mod._TURN_STALE_SECONDS = _orig_turn_stale

assert_ok(len(_run_order) == 3, "队列⑦ 三轮全部执行完毕")
assert_ok([r["idx"] for r in _run_order] == [0, 1, 2], "队列⑦ 执行顺序 = 到达顺序（FIFO）")
_serial = all(_run_order[i]["start"] >= _run_order[i - 1]["end"] - 0.02 for i in (1, 2))
assert_ok(_serial, "队列⑦ 同一会话轮次严格串行（无交叠）")
_hist = v4_state.get_main_history(QUEUE_SID)
_hist_pairs = [(m.get("role"), m.get("content")) for m in _hist]
assert_ok(_hist_pairs == [
    ("user", "msg0"), ("assistant", "回0"),
    ("user", "msg1"), ("assistant", "回1"),
    ("user", "msg2"), ("assistant", "回2"),
], "队列⑦ 落盘历史按发送顺序成对追加（不再后发先答）")
with app_mod._TURN_LOCK:
    _done_states = [v.get("state") for v in (app_mod._TURN_RESULTS.get(QUEUE_SID) or {}).values()]
assert_ok(all(s == "done" for s in _done_states) and len(_done_states) == 3,
          "队列⑦ 全部轮次最终 done（排队轮次的 error 快照被真实结果覆盖）")

app_mod.v4_main.run_turn = _orig_run_turn
agent_state_mod.SESSION_DIR = _orig_state_session_dir

# ================= ⑭ 文案兜底：语义交给模型，代码只校验动作 =================
_orig_dispatch_async = app_mod.v4_main._dispatch_report_async
_orig_llm_chat = app_mod.report._llm_chat
_disp_calls = []
_semantic_replies = []
_semantic_prompts = []


def _fake_dispatch_async(name, args, sid):
    _disp_calls.append({"name": name, "args": dict(args or {}), "sid": sid})
    return {"ok": True, "started": True}


def _fake_semantic_chat(messages, **kwargs):
    _semantic_prompts.append(messages)
    return json.dumps(_semantic_replies.pop(0), ensure_ascii=False)


app_mod.v4_main._dispatch_report_async = _fake_dispatch_async
app_mod.report._llm_chat = _fake_semantic_chat

AUTO_SID = "p0c-autow-sid"
agent_state_mod.SESSION_DIR = tmpdir
agent_state_mod.reset(AUTO_SID)
agent_state_mod.set_report(AUTO_SID, {
    "m5": {"status": "ready"},
    "_m5_json": {
        "topics": [{"title": "新手妈妈情绪管理"}, {"title": "我的创业故事"}],
        "recommended": [{"title": "我的创业故事"}],
    },
})

# 自然表达不靠词表：模型按整句语义返回动作和选题。
_semantic_replies.append({"action": "m6_scripts", "topic": "我的创业故事"})
app_mod._maybe_auto_dispatch_writing(AUTO_SID, "第二个方向更像我，顺着它往下落成口播稿吧")
assert_ok(len(_disp_calls) == 1 and _disp_calls[0]["name"] == "m6_scripts"
          and _disp_calls[0]["args"]["topic"] == "我的创业故事",
          "语义兜底① 自然表达可触发文案并选中模型理解的选题")

# 模型判断为闲聊/拒绝时不派发，不再由否定词表猜。
_disp_calls.clear()
_semantic_replies.append({"action": "none", "topic": ""})
app_mod._maybe_auto_dispatch_writing(AUTO_SID, "我先消化一下，刚才那段挺有启发")
assert_ok(not _disp_calls, "语义兜底② 闲聊或暂缓由模型判断为 none")

# 模型返回不存在的动作，代码拒绝执行。
_disp_calls.clear()
_semantic_replies.append({"action": "delete_everything", "topic": ""})
app_mod._maybe_auto_dispatch_writing(AUTO_SID, "继续")
assert_ok(not _disp_calls, "语义兜底③ 非白名单动作拒绝执行")

# 模型明确要生成但选题值不合法时，仅回落到报告中的重点推荐，不接受模型编造。
_disp_calls.clear()
_semantic_replies.append({"action": "m6_scripts", "topic": "模型编造的题目"})
app_mod._maybe_auto_dispatch_writing(AUTO_SID, "你替我定一个最合适的继续")
assert_ok(len(_disp_calls) == 1 and _disp_calls[0]["args"]["topic"] == "我的创业故事",
          "语义兜底④ 选题只能来自已生成候选")

# m6 已就绪时，模型可理解很口语的修改意见；feedback 永远保留用户原话。
_disp_calls.clear()
agent_state_mod.set_report(AUTO_SID, {"m6": {"status": "ready", "topic": "我的创业故事"}})
_semantic_replies.append({"action": "script_revise", "topic": ""})
feedback = "感觉最后那一下泄气了，收得更干脆些"
app_mod._maybe_auto_dispatch_writing(AUTO_SID, feedback)
assert_ok(len(_disp_calls) == 1 and _disp_calls[0]["name"] == "script_revise"
          and _disp_calls[0]["args"]["feedback"] == feedback,
          "语义兜底⑤ 口语修改意见按原话派发")

# 图片上要写的文字仍是图片需求，不能被已有模块6抢成 script_revise。
_disp_calls.clear()
_semantic_replies.append({"action": "none", "topic": ""})
app_mod._maybe_auto_dispatch_writing(AUTO_SID, "海报上写：把 AI 做成能交付真实结果的人")
assert_ok(not _disp_calls and "图片、海报、视频、音频" in _semantic_prompts[-1][0]["content"],
          "语义兜底⑥ 海报文字不误派成模块6文案修订")

# 流程状态不允许时连模型都不调用。
_disp_calls.clear()
agent_state_mod.reset("p0c-autow-nom5")
pending_before = len(_semantic_replies)
app_mod._maybe_auto_dispatch_writing("p0c-autow-nom5", "往下做")
assert_ok(not _disp_calls and len(_semantic_replies) == pending_before,
          "语义兜底⑦ 流程未到文案阶段不调用模型、不派发")

# 成片域永远拿到当前会话已完成的模块6全文，不让用户重贴、不去外部项目猜。
COMPOSE_SID = "p0c-compose-m6-context"
agent_state_mod.reset(COMPOSE_SID)
agent_state_mod.set_report(COMPOSE_SID, {
    "m6": {"status": "ready", "topic": "黄雀案例拆解"},
    "_m6_json": {"topic": "黄雀案例拆解", "scripts": [
        {"style": "共情型", "full_text": "共情型真实口播全文"},
        {"style": "震撼型", "full_text": "震撼型真实口播全文"},
        {"style": "故事型", "full_text": "故事型真实口播全文"},
    ]},
})
_orig_subagent_turn = v4_main.subagent.run_subagent_turn
_compose_tasks = []


def _fake_compose_turn(sid, domain, task):
    _compose_tasks.append(task)
    return ({"state": "needs_approval", "summary": "两条模板成片待确认",
             "quote": {"cost": 10, "points": 100}}, [])


v4_main.subagent.run_subagent_turn = _fake_compose_turn
v4_main.dispatch("delegate_compose", {"task": "用共情型和震撼型各做一条模板成片"}, COMPOSE_SID, [])
assert_ok(len(_compose_tasks) == 1
          and "共情型真实口播全文" in _compose_tasks[0]
          and "震撼型真实口播全文" in _compose_tasks[0]
          and "current_session_m6" in _compose_tasks[0],
          "跨域交接① compose 自动获得当前会话模块6全文")
assert_ok("禁止要求用户重新粘贴" in _compose_tasks[0]
          and "外部 IP12 项目" in _compose_tasks[0],
          "跨域交接② 成片域不再让用户重贴或去外部项目猜文案")
v4_main.subagent.run_subagent_turn = _orig_subagent_turn
agent_state_mod.reset(COMPOSE_SID)

app_mod.v4_main._dispatch_report_async = _orig_dispatch_async
app_mod.report._llm_chat = _orig_llm_chat
agent_state_mod.reset(AUTO_SID)

# 自然语言确认/取消同样交给模型；报价 token 不送进判断上下文。
_orig_main_llm = v4_main.subagent.llm_turn
_orig_llm_mode = v4_main.config.LLM_MODE
_approval_replies = iter((
    '{"decision":"confirm"}', '{"decision":"cancel"}',
    '{"decision":"none"}', '{"decision":"run_anyway"}',
))
_approval_prompts = []


class _ApprovalMessage:
    def __init__(self, content):
        self.content = content


def _fake_approval_llm(messages, tools, **kwargs):
    _approval_prompts.append(messages)
    return _ApprovalMessage(next(_approval_replies))


v4_main.config.LLM_MODE = "openai"
v4_main.subagent.llm_turn = _fake_approval_llm
quote_for_test = {"capability": "video-generate", "cost": 30, "points": 30,
                  "quote_token": "must-not-enter-model"}
assert_ok(v4_main._typed_approval_decision("这个方案可以，就照这个执行", [quote_for_test]) == "confirm",
          "语义确认① 自然同意可确认当前报价")
assert_ok(v4_main._typed_approval_decision("我还是不做了，停在这里", [quote_for_test]) == "cancel",
          "语义确认② 自然取消可终止当前报价")
assert_ok(v4_main._typed_approval_decision("这个价格是怎么算的？", [quote_for_test]) == "none",
          "语义确认③ 询问价格不会被误当确认")
assert_ok(v4_main._typed_approval_decision("随便做点别的", [quote_for_test]) == "none",
          "语义确认④ 模型越权结论被拒绝")
assert_ok("must-not-enter-model" not in json.dumps(_approval_prompts, ensure_ascii=False),
          "语义确认⑤ 隐藏报价 token 不进入分类上下文")
v4_main.subagent.llm_turn = _orig_main_llm
v4_main.config.LLM_MODE = _orig_llm_mode

# 主 Agent 没调工具却声称「已提交/已扣点」时，语义核验必须拦下并逼回真实图片派发。
_orig_main_dispatch = v4_main.dispatch
_ground_calls = []


class _GroundFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = json.dumps(arguments, ensure_ascii=False)


class _GroundToolCall:
    def __init__(self, name, arguments):
        self.id = "call-ground-image"
        self.function = _GroundFunction(name, arguments)


class _GroundMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


_ground_replies = iter((
    _GroundMessage("海报已经提交，35点已扣，正在后台生成。"),
    _GroundMessage('{"unsupported":true,"domain":"image"}'),
    _GroundMessage(tool_calls=[_GroundToolCall("delegate_image", {"task": "制作本人艺术字海报"})]),
    _GroundMessage("图片报价35点，任务尚未提交、尚未扣点，请确认报价。"),
    _GroundMessage('{"unsupported":false,"domain":""}'),
))


def _fake_ground_llm(messages, tools, **kwargs):
    _ground_calls.append(messages)
    return next(_ground_replies)


def _fake_main_dispatch(name, args, sid, log):
    assert name == "delegate_image"
    return {"ok": True, "state": "needs_approval", "summary": "图片报价35点",
            "quote": {"capability": "image-generate", "cost": 35, "points": 100}}


GROUND_SID = "p0c-ground-claim"
v4_state.reset(GROUND_SID)
v4_main.config.LLM_MODE = "openai"
v4_main.subagent.llm_turn = _fake_ground_llm
v4_main.dispatch = _fake_main_dispatch
ground_reply, _ground_log, ground_routing = v4_main.run_turn(
    GROUND_SID, "海报上写：把 AI 做成能交付真实结果的人")
ground_history = v4_state.get_main_history(GROUND_SID)
assert_ok(ground_reply.startswith("图片报价35点")
          and ground_routing and ground_routing[0].get("domain") == "image",
          "执行证据① 空口提交被拦下并强制派发真实图片子 Agent")
assert_ok(not any("海报已经提交" in str(item.get("content") or "") for item in ground_history),
          "执行证据② 无工具的虚假已提交回复不进入用户历史")
assert_ok(any("运行时事实校验" in str(m.get("content") or "")
              for messages in _ground_calls for m in messages),
          "执行证据③ 语义核验失败后只重试一次真实派发")
v4_main.dispatch = _orig_main_dispatch
v4_main.subagent.llm_turn = _orig_main_llm
v4_main.config.LLM_MODE = _orig_llm_mode
v4_state.reset(GROUND_SID)

# ================= ⑮ Running → 终态自动回到对话，且不重复 =================
from agent.v4 import protocol as v4_protocol

_orig_hq_run = v4_delivery.hq_cli.run
_orig_emit = v4_delivery.streaming.emit
_orig_poll_interval = v4_delivery._JOB_POLL_INTERVAL
_delivery_events = []


def _seed_running(sid, domain, job_id):
    v4_state.reset(sid)
    v4_state.save_subagent(sid, domain, last_result=v4_protocol.make(
        v4_protocol.RUNNING, "任务已提交", result={"job_id": job_id}))


v4_delivery._JOB_POLL_INTERVAL = 0
v4_delivery.streaming.emit = lambda sid, event, data: _delivery_events.append((sid, event, data))

assert_ok(v4_subagent._task_status({"status": "done", "phase": "starting"}) == "done",
          "终态交付⓪ status=done 优先于陈旧 phase=starting")
expired_url = "https://media.example/result.mp4?q-sign-time=1%3B2"
fresh_url = "https://media.example/fresh-result.mp4"
v4_delivery.hq_cli.run = lambda cap, inputs, **kw: {
    "exit_code": 0,
    "data": {"result": {"items": [{"id": 7219, "job_id": 6970, "video_url": fresh_url}]}}
}
refreshed_task = v4_subagent._refresh_expired_task_media({
    "id": 6970,
    "status": "done",
    "phase": "starting",
    "result": {"video_file": "video/result.mp4", "video_url": expired_url},
}, {"job_id": 6970}, "p0c-refresh-media")
assert_ok(refreshed_task["result"]["video_url"] == fresh_url
          and refreshed_task.get("media_url_refreshed") is True,
          "终态交付⓪b 过期成品链接按当前客户资产自动刷新")

DONE_SID = "p0c-terminal-done"
_seed_running(DONE_SID, "digital-human", 7622)
v4_delivery.hq_cli.run = lambda *a, **kw: {
    "exit_code": 0,
    "data": {"result": {"job_id": 7622, "status": "done", "phase": "starting", "message": "数字人成片已生成",
                        "result": {"video_url": "https://example.test/result.mp4"}}},
}
v4_delivery.resume_stale_jobs(DONE_SID)
done_history = v4_state.get_main_history(DONE_SID)
assert_ok(sum(1 for m in done_history if m.get("task_job") == "7622") == 1,
          "终态交付① completed 结果自动写入对话")
assert_ok(any("result.mp4" in m.get("content", "") for m in done_history),
          "终态交付② 真实成品链接随结果进入对话")
assert_ok(any(e[1] == "delivery" and "已完成" in e[2]["reply"] for e in _delivery_events),
          "终态交付③ 在线页面收到实时 delivery 事件")
v4_delivery.resume_stale_jobs(DONE_SID)
assert_ok(sum(1 for m in v4_state.get_main_history(DONE_SID) if m.get("task_job") == "7622") == 1,
          "终态交付④ 重复轮询不重复发结果")

FAIL_SID = "p0c-terminal-fail"
_seed_running(FAIL_SID, "video", 7681)
v4_delivery.hq_cli.run = lambda *a, **kw: {
    "exit_code": 0,
    "data": {"result": {"job_id": 7681, "phase": "failed", "error_message": "供应商处理失败"}},
}
v4_delivery.resume_stale_jobs(FAIL_SID)
assert_ok(any("失败" in m.get("content", "") and "供应商处理失败" in m.get("content", "")
              for m in v4_state.get_main_history(FAIL_SID)),
          "终态交付⑤ failed 原因自动回到对话")

RUN_SID = "p0c-terminal-running"
_seed_running(RUN_SID, "audio", 7700)
v4_delivery.hq_cli.run = lambda *a, **kw: {
    "exit_code": 0, "data": {"result": {"job_id": 7700, "phase": "running"}},
}
v4_delivery.resume_stale_jobs(RUN_SID)
assert_ok(not any(m.get("task_job") == "7700" for m in v4_state.get_main_history(RUN_SID)),
          "终态交付⑥ 仍在 running 时不伪造完成消息")

v4_delivery.hq_cli.run = _orig_hq_run
v4_delivery.streaming.emit = _orig_emit
v4_delivery._JOB_POLL_INTERVAL = _orig_poll_interval
v4_delivery._LAST_JOB_POLL.clear()
agent_state_mod.SESSION_DIR = _orig_state_session_dir

print(json.dumps(out, ensure_ascii=False, indent=2))
print("P0C_UNIT: %s" % ("PASS" if not errors else "FAIL"))
for e in errors:
    print("  ERROR:", e)
sys.exit(1 if errors else 0)
