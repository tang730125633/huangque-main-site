#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄雀服务健康监控（#193/#241）。

每分钟 cron：查各服务 systemd is-active + HTTP 可连 + 重启计数，任一异常/恢复推飞书。
状态化(state.json)——只在 健康↔异常 状态翻转时告警一次，不每分钟刷屏。
另查 jobs 近 1 小时失败率和 xiaotan 持续高 CPU，超阈值告警一次。复用漂移哨兵飞书通道。

用法：python3 health_check.py            # cron 每分钟
      python3 health_check.py --selftest  # 打印各服务当前状态 + 测飞书，不改 state
"""
import os, sys, json, subprocess, urllib.request, urllib.error, sqlite3, time

HOME = "/home/ubuntu"
STATE = os.path.join(HOME, "hq-monitor", "state.json")
JOB_DB = os.path.join(HOME, "content-api", "content_jobs.db")
FAIL_RATE_MAX = 0.6          # 近1h失败率超此值告警
FAIL_MIN_SAMPLE = 10         # 样本太少不判失败率
CPU_LIMITS = {"xiaotan": 30.0}
CPU_SUSTAIN_SECONDS = 30 * 60

# (systemd 单元, 端口, 探测路径)
SERVICES = [
    ("huangque-auth",         8095, "/"),
    ("huangque-content",      8096, "/api/gen/health"),
    ("huangque-dl",           8097, "/"),
    ("huangque-admin",        8098, "/api/admin/health"),
    ("huangque-leadgen-api",  8100, "/"),
    ("huangque-imggen-api",   8101, "/"),
    ("xiaotan",               8501, "/docs"),
]


def _alert(text):
    try:
        sys.path.insert(0, os.path.join(HOME, "hq-drift"))
        from drift_sentinel import _feishu_send
        return _feishu_send(text)
    except Exception as e:
        print("飞书告警失败:", e); return False


def _is_active(unit):
    try:
        out = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() == "active"
    except Exception:
        return False


def _http_ok(port, path):
    # 任何 HTTP 响应(含 4xx)=服务活着；仅连接拒绝/超时=挂
    try:
        urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=4)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _nrestarts(unit):
    try:
        out = subprocess.run(["systemctl", "show", "-p", "NRestarts", "--value", unit],
                             capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip() or 0)
    except Exception:
        return 0


def _cpu_usage_ns(unit):
    try:
        out = subprocess.run(["systemctl", "show", "-p", "CPUUsageNSec", "--value", unit],
                             capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip() or 0)
    except Exception:
        return 0


def _next_cpu_state(previous, cpu_ns, now, limit):
    previous = previous or {}
    prev_ns = int(previous.get("cpu_ns") or 0)
    prev_at = int(previous.get("sample_at") or 0)
    elapsed = now - prev_at
    percent = ((cpu_ns - prev_ns) / (elapsed * 1_000_000_000) * 100.0
               if prev_ns and cpu_ns >= prev_ns and elapsed > 0 else 0.0)
    high_since = int(previous.get("high_since") or 0)
    alerted = bool(previous.get("alerted"))
    if percent >= limit:
        high_since = high_since or now
    else:
        high_since = 0
        if percent < limit * 0.7:
            alerted = False
    should_alert = bool(high_since and now - high_since >= CPU_SUSTAIN_SECONDS and not alerted)
    return {"cpu_ns": cpu_ns, "sample_at": now, "percent": round(percent, 1),
            "high_since": high_since, "alerted": alerted or should_alert}, should_alert


def probe():
    down = []
    detail = {}
    for unit, port, path in SERVICES:
        active = _is_active(unit)
        http = _http_ok(port, path)
        healthy = active and http
        detail[unit] = {"active": active, "http": http, "restarts": _nrestarts(unit),
                        "cpu_ns": _cpu_usage_ns(unit)}
        if not healthy:
            down.append(unit)
    return down, detail


def _fail_rate():
    try:
        c = sqlite3.connect(JOB_DB)
        since = int(time.time()) - 3600
        rows = list(c.execute("SELECT status, COUNT(*) FROM jobs WHERE created_at>=? GROUP BY status", (since,)))
        c.close()
        tot = sum(n for _, n in rows)
        err = sum(n for s, n in rows if s == "error")
        if tot < FAIL_MIN_SAMPLE:
            return None, tot, err
        return err / tot, tot, err
    except Exception:
        return None, 0, 0


def _load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"down": [], "fail_alerted": False, "cpu": {}}


def _save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(st, open(STATE, "w"))


def main():
    selftest = "--selftest" in sys.argv
    down, detail = probe()
    rate, tot, err = _fail_rate()
    if selftest:
        for u, d in detail.items():
            print("%s active=%s http=%s restarts=%d" % (u, d["active"], d["http"], d["restarts"]))
        print("近1h失败率:", ("%.0f%% (%d/%d)" % (rate*100, err, tot)) if rate is not None else "样本不足")
        print("飞书自检:", _alert("【健康监控自检】黄雀服务健康监控通道正常，可忽略 🙏"))
        return

    st = _load_state()
    now = int(time.time())
    prev_down = set(st.get("down", []))
    now_down = set(down)
    new_down = now_down - prev_down
    recovered = prev_down - now_down
    if new_down:
        _alert("🔴【服务异常】黄雀以下服务不可用：\n" +
               "\n".join("· %s (active=%s, http=%s, 重启%d次)" %
                         (u, detail[u]["active"], detail[u]["http"], detail[u]["restarts"]) for u in sorted(new_down)))
    if recovered:
        _alert("🟢【服务恢复】黄雀以下服务已恢复：\n" + "\n".join("· " + u for u in sorted(recovered)))

    fail_alerted = st.get("fail_alerted", False)
    if rate is not None and rate >= FAIL_RATE_MAX:
        if not fail_alerted:
            _alert("⚠️【任务失败率高】黄雀近1小时任务失败率 %.0f%% (%d/%d)，请排查。" % (rate*100, err, tot))
            fail_alerted = True
    elif rate is not None and rate < FAIL_RATE_MAX * 0.7:
        fail_alerted = False   # 回落后复位，下次超阈值再告警

    cpu_state = {}
    for unit, limit in CPU_LIMITS.items():
        state, should_alert = _next_cpu_state((st.get("cpu") or {}).get(unit),
                                              detail.get(unit, {}).get("cpu_ns", 0), now, limit)
        cpu_state[unit] = state
        if should_alert:
            _alert("⚠️【服务CPU持续过高】%s 已连续30分钟超过 %.0f%%，当前 %.1f%%，请排查。" %
                   (unit, limit, state["percent"]))

    _save_state({"down": sorted(now_down), "fail_alerted": fail_alerted, "cpu": cpu_state})
    print("probe done. down=%s fail_rate=%s" % (sorted(now_down), ("%.2f" % rate) if rate is not None else "n/a"))


if __name__ == "__main__":
    main()
