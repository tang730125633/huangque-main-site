#!/usr/bin/env python3
"""号池体检：逐个 cookie 注入→访问 user/self→返回 抖音号/昵称/粉丝/有效性 + 静态信息。
顺序执行(省内存)。默认走青果住宅代理(降风控)，可用 HEALTH_PROXY 覆盖、=direct 直连。
用法:
  pool_health.py                       # 体检整池，文本表
  pool_health.py --json [--out F]      # JSON(可写文件)
  pool_health.py --json /path/a.json   # 只体检指定 cookie
  pool_health.py --json --out F --alert  # 体检整池 + 有失效则飞书告警 Tang
环境变量:
  HEALTH_PROXY  空=青果住宅(默认) / "direct"=直连 / "http://..."=指定代理
  QG_KEY        青果 key (默认 55DC9F7E)
  TANG_OPENID   告警接收人 open_id
  ALERT_ACCOUNT 飞书发送子账号(默认 default=小冬)"""
import json, asyncio, re, sys, glob, hashlib, datetime, urllib.parse, urllib.request, os, subprocess
from playwright.async_api import async_playwright

QG_KEY = os.environ.get("QG_KEY", "55DC9F7E")
HEALTH_PROXY = os.environ.get("HEALTH_PROXY", "")  # 空=青果
TANG_OPENID = os.environ.get("TANG_OPENID", "ou_d45fa42f49c14bc85abf18cc37f70391")
ALERT_ACCOUNT = os.environ.get("ALERT_ACCOUNT", "")
POOL_GLOB = os.environ.get("NUMBER_POOL", "/home/ubuntu/number_pool") + "/cookie_*.json"


def get_proxy():
    """返回 playwright proxy dict 或 None(直连)。默认每次取一个青果住宅IP。"""
    if HEALTH_PROXY == "direct":
        return None
    if HEALTH_PROXY:
        return {"server": HEALTH_PROXY}
    try:
        url = "https://share.proxy.qg.net/get?key=%s&num=1&format=json" % QG_KEY
        d = json.loads(urllib.request.urlopen(url, timeout=15).read())
        srv = d["data"][0]["server"]
        return {"server": "http://" + srv}
    except Exception:
        return None  # 青果取不到则直连兜底(也别让体检整个挂)


def static_info(d):
    uid = hashlib.md5(d.get("uid_tt", "").encode()).hexdigest()[:10] if d.get("uid_tt") else None
    lt = d.get("login_time")
    lt_s = datetime.datetime.fromtimestamp(int(lt) / 1000).strftime("%Y-%m-%d %H:%M") if lt and lt.isdigit() else None
    sg = urllib.parse.unquote(d.get("sid_guard", ""))
    exp = sg.split("|")[-1].replace("+", " ") if "|" in sg else None
    return uid, lt_s, exp


async def _attempt(cookies, proxy):
    """单次尝试。返回 (valid, dyid, nick, fans, err)。"""
    async with async_playwright() as p:
        kw = {"headless": True, "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"]}
        if proxy:
            kw["proxy"] = proxy
        br = await p.chromium.launch(**kw)
        try:
            ctx = await br.new_context(viewport={"width": 1280, "height": 900})
            await ctx.add_cookies(cookies)
            page = await ctx.new_page()
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=40000)
            await page.evaluate("() => { try{localStorage.setItem('HasUserLogin','1');}catch(e){} }")
            await page.goto("https://www.douyin.com/user/self", wait_until="domcontentloaded", timeout=40000)
            await page.wait_for_timeout(6000)
            body = await page.evaluate("() => document.body.innerText")
            title = await page.title()
            m = re.search(r"抖音号[:：]\s*([0-9A-Za-z_.]+)", body)
            if not m:
                return (False, None, None, None, "登录态无效(读不到抖音号)")
            tm = re.match(r"(.+?)的主页", title) or re.match(r"(.+?)\s*[-|]", title)
            nick = tm.group(1).strip()[:20] if tm else None
            fm = re.search(r"关注\s*([\d.万亿]+)\s*粉丝\s*([\d.万亿]+)", body)
            return (True, m.group(1), nick, fm.group(2) if fm else None, None)
        finally:
            await br.close()


async def check_one(path):
    cookies = json.load(open(path))
    d = {c["name"]: c["value"] for c in cookies}
    uid, lt_s, exp = static_info(d)
    n = re.search(r"cookie_(\d+)", path)
    r = {"file": os.path.basename(path), "n": int(n.group(1)) if n else None,
         "uid": uid, "login": lt_s, "expire": exp, "valid": False,
         "dyid": None, "nick": None, "fans": None, "err": None}
    # 两次尝试：先青果(住宅)，失败再直连复验 → 排除青果抽风误报，只有两次都死才判失效
    attempts = [get_proxy(), None]  # None = 直连
    last_err = None
    for proxy in attempts:
        try:
            valid, dyid, nick, fans, err = await _attempt(cookies, proxy)
        except Exception as e:
            last_err = str(e)[:60]
            continue
        if valid:
            r.update(valid=True, dyid=dyid, nick=nick, fans=fans, err=None)
            return r
        last_err = err
    r["err"] = last_err or "两次尝试均失败"
    return r


def alert_feishu(bad_rows):
    """有号失效 → openclaw 飞书 DM Tang。"""
    lines = ["⚠️ 号池体检告警：%d 个号失效，需更换 cookie" % len(bad_rows)]
    for r in bad_rows:
        who = r.get("dyid") or ("cookie_%s" % r.get("n"))
        lines.append("• cookie_%s（%s）：%s" % (r.get("n"), who, r.get("err") or "登录态失效"))
    lines.append("\n→ 去管理后台 /admin 重新导入这些号的最新 cookie。")
    msg = "\n".join(lines)
    cmd = ["openclaw", "message", "send", "--channel", "feishu", "--target", TANG_OPENID, "--message", msg]
    if ALERT_ACCOUNT:
        cmd += ["--account", ALERT_ACCOUNT]
    try:
        subprocess.run(cmd, env={**os.environ, "XDG_RUNTIME_DIR": "/run/user/%d" % os.getuid()},
                       capture_output=True, timeout=40)
    except Exception:
        pass


async def main():
    args = [a for a in sys.argv[1:] if a not in ("--json", "--alert")]
    as_json = "--json" in sys.argv
    do_alert = "--alert" in sys.argv
    out_file = None
    if "--out" in args:
        i = args.index("--out"); out_file = args[i + 1]; args = args[:i] + args[i + 2:]
    paths = args if args else sorted(glob.glob(POOL_GLOB), key=lambda p: int(re.search(r"cookie_(\d+)", p).group(1)))
    rows = []
    for p in paths:
        rows.append(await check_one(p))
    bad = [r for r in rows if not r["valid"]]
    result = {"rows": rows, "ok": sum(1 for r in rows if r["valid"]), "total": len(rows)}
    if out_file:
        json.dump(result, open(out_file, "w", encoding="utf-8"), ensure_ascii=False)
    if do_alert and bad:
        alert_feishu(bad)
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for r in rows:
            print("  %-22s | %s | 抖音号=%-14s 昵称=%-14s | uid=%s 过期=%s%s" % (
                r["file"], "✅有效" if r["valid"] else "❌失效",
                r["dyid"] or "-", r["nick"] or "-", r["uid"], r["expire"],
                "  " + r["err"] if r["err"] else ""))
        print("\n小结: %d/%d 有效%s" % (result["ok"], result["total"],
              "，已飞书告警" if (do_alert and bad) else ""))


asyncio.run(main())
