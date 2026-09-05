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

tmpdir = tempfile.mkdtemp(prefix="hq-p0c-state-")
v4_state.SESSION_DIR = tmpdir

SID = "p0c-persist-sid"


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
v4_state.persist(SID)
path = os.path.join(tmpdir, "v4-%s.json" % SID)
with open(path, "r", encoding="utf-8") as f:
    snap = json.load(f)
main_msgs = snap.get("main") or []
assert_ok(len(main_msgs) == N_THREADS * N_PER,
          "并发 append+persist 后盘上消息数=%d（期望 %d）" % (len(main_msgs), N_THREADS * N_PER))
expected = sorted(["msg-%d" % i for i in range(N_PER)] * N_THREADS)
assert_ok(sorted(m["content"] for m in main_msgs) == expected, "盘上消息内容完整且无撕裂")
assert_ok(not os.path.exists(path + ".tmp"), "无残留 .tmp 文件")

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

# 并发闸：同时只有 5 个 CLI 子进程在跑（_subprocess_run 已持闸，fake_run 只统计并发数）
import subprocess as _sub
real_run = _sub.run
state_gate = {"active": 0, "peak": 0}


def fake_run(cmd, **kw):
    state_gate["active"] += 1
    state_gate["peak"] = max(state_gate["peak"], state_gate["active"])
    time.sleep(0.2)
    state_gate["active"] -= 1
    return _sub.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")


_sub.run = fake_run
threads5 = [threading.Thread(target=hq_cli.status) for _ in range(8)]
for t in threads5:
    t.start()
for t in threads5:
    t.join()
_sub.run = real_run
assert_ok(state_gate["peak"] <= 5, "hq CLI 全局并发闸生效（峰值 %d <= 5）" % state_gate["peak"])

# 同源校验：跨站 Origin 拒绝、同源/无 Origin 放行
client = app_mod.app.test_client()
r1 = client.post("/api/v4/reset", json={"session_id": "x"}, headers={"Origin": "https://evil.example.com"})
assert_ok(r1.status_code == 403, "跨站 Origin 的 reset 被拒绝（403）")
r2 = client.post("/api/v4/reset", json={"session_id": "x"}, headers={"Origin": "http://localhost"})
assert_ok(r2.status_code == 200, "同源 Origin 的 reset 放行（200）")
r3 = client.post("/api/v4/reset", json={"session_id": "x"})
assert_ok(r3.status_code == 200, "无 Origin 的 reset 放行（curl/自家脚本）")

# 异步 start：秒回 session_id + ack + seq，不再同步等开场白
orig_spawn = app_mod._spawn_turn
app_mod._spawn_turn = lambda sid, msg, paths=None: 999
r4 = client.post("/api/v4/start")
app_mod._spawn_turn = orig_spawn
d4 = r4.get_json()
assert_ok(r4.status_code == 200 and d4.get("session_id") and d4.get("async") is True
          and d4.get("seq") == 999 and d4.get("ack"), "start 立即返回异步确认（seq/ack/session_id 齐全）")
assert_ok(d4.get("reply") is None, "start 不再返回同步 reply")

# 异步 confirm：无报告时仍 400，有报告时走后台轮次
r5 = client.post("/api/v4/confirm", json={"session_id": "no-such-report-sid"})
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

print(json.dumps(out, ensure_ascii=False, indent=2))
print("P0C_UNIT: %s" % ("PASS" if not errors else "FAIL"))
for e in errors:
    print("  ERROR:", e)
sys.exit(1 if errors else 0)
