#!/usr/bin/env python3
# 黄雀外部供应商余额哨兵：直查余额API(WaveSpeed/RunningHub/HeyGen/TikHub) + 扫content日志"余额不足"(兜底覆盖小乐等无余额API的)
# 低于阈值 → 飞书告警(复用 agent-metrics 的通道:「父OpenClaw开发测试」群)。同一项 2h 冷却。
# 用法: python3 balance_sentinel.py          巡检(cron每10分钟)
#       python3 balance_sentinel.py --test   自检通道
import json, os, re, subprocess, sys, time, urllib.request

STATE = os.path.expanduser("~/hq-monitor/.balance_sentinel_state.json")
COOLDOWN = 7200

def env(k):
    for f in ("/home/ubuntu/content-api/content.env",):
        try:
            for ln in open(f):
                m = re.match(r"\s*%s=(.*)" % re.escape(k), ln)
                if m: return m.group(1).strip().strip('"')
        except Exception: pass
    return os.environ.get(k, "")

def http_json(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def check_wavespeed():
    k = env("WAVESPEED_API_KEY")
    if not k: return None
    d = http_json("https://api.wavespeed.ai/api/v3/balance", headers={"Authorization": "Bearer " + k})
    bal = float(d["data"]["balance"])
    return ("WaveSpeed(线路二:动作模仿/换装)", bal, 10.0, "$%.2f" % bal, "https://wavespeed.ai/billing")

def check_runninghub():
    k = ""
    try:
        k = subprocess.run(["sudo", "grep", "-oP", "(?<=RUNNINGHUB_API_KEY=).*", "/etc/huangque/runninghub.env"],
                           capture_output=True, text=True, timeout=10).stdout.strip().strip('"')
    except Exception: pass
    if not k: k = os.environ.get("RUNNINGHUB_API_KEY", "")
    if not k: return None
    d = http_json("https://www.runninghub.cn/uc/openapi/accountStatus",
                  data=json.dumps({"apikey": k}).encode(), headers={"Content-Type": "application/json"})
    bal = float(d["data"]["remainMoney"])
    return ("RunningHub(换装线一/换背景)", bal, 20.0, "¥%.2f" % bal, "https://www.runninghub.cn/call-api")

HEYGEN_PLAN_MIN = 60.0   # plan_credit 低于它就告警。实测真实用户约 8 条/半小时 → 60 条≈4 小时余量
HEYGEN_MCP_CREDENTIALS = os.environ.get(
    "HEYGEN_MCP_CREDENTIALS", "/home/ubuntu/.config/huangque/heygen-mcp.json")
HEYGEN_OAUTH_WARN_SECONDS = 3 * 86400


def heygen_quota(d):
    """从 /v2/user/remaining_quota 的响应里拆出两个池，返回 (plan_credit, api)。

    HeyGen 有两个独立额度池，**单价差 420 倍**（2026-07-11 生成前后读余额实测）：

        plan_credit  一条 cinematic_avatar 扣 1        ← 优先扣这个，是真正在供片的池
        api 钱包     同一条扣 420 quota = $7.00        ← plan_credit 归零后【静默】落到这里

    而顶层的 remaining_quota **等于 api，完全不含 plan_credit**：
        {"remaining_quota": 69, "details": {"api": 69, "plan_credit": 390}}

    原来这里读 remaining_quota，等于在为「$7 的应急钱包快没钱了」报警，却对
    「1 credit 的主力池快见底了」一无所知 —— 而真正的事故恰恰是后者：
    plan_credit 归零 → 无任何提示地按 $7/条 从钱包扣 → $15 两条烧光 → 之后全员 402。
    """
    det = (d.get("data") or {}).get("details") or {}
    return float(det.get("plan_credit") or 0), float(det.get("api") or 0)


def check_heygen():
    k = env("HEYGEN_API_KEY")
    if not k: return None
    proxy = env("HEYGEN_DIRECT_PROXY") or "http://127.0.0.1:7897"
    h = urllib.request.ProxyHandler({"https": proxy, "http": proxy})
    op = urllib.request.build_opener(h)
    d = json.load(op.open(urllib.request.Request("https://api.heygen.com/v2/user/remaining_quota",
                                                 headers={"X-Api-Key": k}), timeout=25))
    return heygen_alerts(*heygen_quota(d))


def check_heygen_oauth(now=None):
    """MCP OAuth 到期前 3 天告警；只读过期时间，不读取或输出 token。"""
    try:
        expires_at = float(json.load(open(HEYGEN_MCP_CREDENTIALS))["expires_at"])
    except Exception:
        return ("HeyGen MCP OAuth", 0.0, 1.0, "凭据缺失或损坏", "https://developers.heygen.com/mcp/overview",
                "🚨 HeyGen MCP OAuth 凭据缺失或损坏，套餐 Credits 视频会在创建前失败并自动退点。请立即重新授权。")
    remaining = max(0.0, expires_at - (time.time() if now is None else now))
    expires = time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_at))
    return ("HeyGen MCP OAuth", remaining, HEYGEN_OAUTH_WARN_SECONDS,
            "有效期还剩 %.1f 天（%s）" % (remaining / 86400, expires),
            "https://developers.heygen.com/mcp/overview",
            "🚨 HeyGen MCP OAuth 将于 %s 到期。供应商当前 refresh token 实测只能使用一次，"
            "且刷新响应不返回替代 token；请在到期前重新授权，否则口播/剧情视频会在创建前失败并自动退点。" % expires)


def heygen_alerts(plan, api):
    """(plan_credit, api) → 告警项列表。纯函数，好测。"""
    out = [("HeyGen 套餐额度(口播/动作模仿/剧情视频)", plan, HEYGEN_PLAN_MIN,
            "plan_credit 剩 %.0f 条" % plan, "https://app.heygen.com")]
    # 致命：主力池空了、而应急钱包还有钱 → HeyGen 会【静默】按 $7/条 从钱包扣（平时 1 credit）。
    # 这不是「余额不足」，是「正在以 420 倍价格烧钱」，所以要自定义文案：不是叫你充值，是叫你停手。
    if plan <= 0 and api > 0:
        out.append((
            "HeyGen 静默跳价", 0.0, 1.0, "plan_credit 已空 · API 钱包尚余 %.0f" % api,
            "https://app.heygen.com",
            "🚨 HeyGen 套餐额度已耗尽！此刻每条视频正按 $7 从 API 钱包扣费"
            "（平时只扣 1 个 plan_credit，相当于 420 倍价格），且【没有任何提示】。"
            "请立即续订套餐；在续上之前，建议把 API 钱包清空 —— 宁可让它响亮地失败，也别静默烧钱。",
        ))
    return out

def check_tikhub():
    k = env("TIKHUB_KEY") or env("TIKHUB_API_KEY")
    if not k: return None
    d = http_json("https://api.tikhub.io/api/v1/tikhub/user/get_user_info",
                  headers={"Authorization": "Bearer " + k, "User-Agent": "Mozilla/5.0 hq-monitor"})
    bal = float(d.get("api_key_data", {}).get("balance") or d.get("user_data", {}).get("balance") or 0)
    return ("TikHub(采集/获客)", bal, 3.0, "$%.2f" % bal, "https://tikhub.io")

def scan_journal():
    """扫近20分钟content日志的余额类报错(兜底:小乐等无余额API)"""
    try:
        out = subprocess.run(["sudo", "journalctl", "-u", "huangque-content", "--since", "20 min ago", "--no-pager"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return []
    pat = re.compile(r"预扣费额度失败|Insufficient credits|余额不足|insufficient_quota|企业版余额", re.I)
    hits = set()
    for ln in out.splitlines():
        if pat.search(ln):
            if "xiaole" in ln or "预扣费" in ln or "zz1cc" in ln: hits.add("小乐xiaolevideo(果肉/豆姐/欧米) 日志出现『预扣费额度失败』")
            elif "Insufficient credits" in ln: hits.add("WaveSpeed 日志出现『Insufficient credits』")
            elif "企业版余额" in ln: hits.add("RunningHub 日志出现『企业版余额不足』")
            else: hits.add("日志出现余额类报错: " + ln.strip()[-120:])
    return list(hits)

# —— 飞书发送(复用 agent-metrics 通道) ——
AM = os.path.expanduser("~/agent-metrics")
def feishu_send(text):
    gid = None
    try:
        for b in json.load(open(AM + "/bot_groups.json")):
            for g in b["in_groups"]:
                if g["name"] == "父OpenClaw开发测试": gid = g["id"]
    except Exception: pass
    try:
        fe = json.load(open(os.path.expanduser("~/.openclaw/openclaw.json")))["channels"]["feishu"]
    except Exception:
        return False
    try:
        t = http_json("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                      data=json.dumps({"app_id": fe["appId"], "app_secret": fe["appSecret"]}).encode(),
                      headers={"Content-Type": "application/json"}).get("tenant_access_token")
        if not (t and gid): return False
        body = json.dumps({"receive_id": gid, "msg_type": "text", "content": json.dumps({"text": text})}).encode()
        r = http_json("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                      data=body, headers={"Authorization": "Bearer " + t, "Content-Type": "application/json"})
        return r.get("code") == 0
    except Exception as e:
        print("feishu err", e); return False

CHECKS = (check_wavespeed, check_runninghub, check_heygen, check_heygen_oauth, check_tikhub)


def collect_alerts(checks, state, now, journal=None):
    """跑一遍所有检查，返回 (告警文案列表, 更新后的 state)。纯逻辑，不打网、不发飞书，好测。

    check 函数可以返回单条 (name, bal, thresh, disp, url[, msg])，也可以返回一个列表 ——
    HeyGen 要报两件事（套餐额度低 / 正在静默按 420 倍价格烧钱），一条不够。
    """
    alerts = []
    for fn in checks:
        try:
            r = fn()
        except Exception as e:
            print(fn.__name__, "查询失败:", str(e)[:80])
            continue
        if not r:
            continue
        for item in (r if isinstance(r, list) else [r]):
            name, bal, thresh, disp, url = item[:5]
            custom = item[5] if len(item) > 5 else None
            print("%-30s %s (阈值%s)" % (name, disp, thresh))
            if bal < thresh and now - state.get(name, 0) > COOLDOWN:
                alerts.append(custom or ("⚠️ %s 余额 %s 低于阈值，功能将断供！充值: %s" % (name, disp, url)))
                state[name] = now
    for hit in (journal if journal is not None else scan_journal()):
        if now - state.get(hit, 0) > COOLDOWN:
            alerts.append("🚨 " + hit + "（近20分钟）请立即检查充值")
            state[hit] = now
    return alerts, state


def main():
    if "--test" in sys.argv:
        print("自检:", feishu_send("【黄雀余额哨兵】通道自检 OK。真告警格式:『⚠️ WaveSpeed 余额 $0.18 < $10，线路二将停，充值: wavespeed.ai/billing』"))
        return 0
    state = {}
    try:
        state = json.load(open(STATE))
    except Exception:
        pass
    now = int(time.time())
    alerts, state = collect_alerts(CHECKS, state, now)
    if alerts:
        msg = "【黄雀余额哨兵】\n" + "\n".join(alerts)
        print("告警发送:", feishu_send(msg), "\n" + msg)
    else:
        print("无告警")
    json.dump(state, open(STATE, "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
