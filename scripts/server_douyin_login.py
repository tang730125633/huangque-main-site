#!/usr/bin/env python3
"""服务器侧：走青果住宅代理打开抖音登录，截图二维码给 Tang 扫，轮询登录成功。
- 截图存 /tmp/douyin_qr.png（拉到 Mac 给 Tang 看）
- 登录成功后会话持久化进 dy_user_data_dir，之后爬取直接用
- 进度写 /tmp/douyin_login.log
"""
import asyncio
import json
import re
import sys
import urllib.request
from playwright.async_api import async_playwright

QG_KEY = "55DC9F7E"
UD = "/home/ubuntu/MediaCrawler/browser_data/dy_user_data_dir"
QR_PATH = "/tmp/douyin_qr.png"


def log(m):
    print(m, flush=True)


def extract_proxy():
    url = f"https://share.proxy.qg.net/get?key={QG_KEY}&num=1&format=json"
    d = json.loads(urllib.request.urlopen(url, timeout=15).read())
    it = d["data"][0]
    return it["server"], it["proxy_ip"], it["area"], it["isp"]


async def main():
    server, pxip, area, isp = extract_proxy()
    log(f"代理网关 {server} | 出口 {pxip} {area} {isp}")
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            UD, headless=True,
            proxy={"server": f"http://{server}"},
            viewport={"width": 1280, "height": 900},
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(5000)

            # 已登录则直接返回
            body = await page.evaluate("() => document.body.innerText")
            m = re.search(r"抖音号[:：]\s*([0-9A-Za-z_.]+)", body)
            if m:
                log(f"✅ 已是登录态，抖音号 {m.group(1)}，无需扫码")
                return

            # 触发登录弹窗
            for sel in ["text=登录", "button:has-text('登录')", "[data-e2e='login-button']"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        break
                except Exception:
                    pass
            await page.wait_for_timeout(4000)
            await page.screenshot(path=QR_PATH, full_page=False)
            log(f"QR_SAVED {QR_PATH}")
            log("=> 已截图二维码，等待扫码（最多 180 秒）…")

            # 轮询登录成功
            for i in range(36):  # 36*5=180s
                await page.wait_for_timeout(5000)
                try:
                    b = await page.evaluate("() => document.body.innerText")
                except Exception:
                    continue
                mm = re.search(r"抖音号[:：]\s*([0-9A-Za-z_.]+)", b)
                # 抖音登录后首页右上出现头像、'登录'消失
                if mm or ("我的" in b and "登录" not in b[:50]):
                    # 进一步确认：访问个人页读抖音号
                    try:
                        await page.goto("https://www.douyin.com/user/self",
                                        wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(5000)
                        b2 = await page.evaluate("() => document.body.innerText")
                        m2 = re.search(r"抖音号[:：]\s*([0-9A-Za-z_.]+)", b2)
                        if m2:
                            log(f"✅✅ 登录成功！抖音号 {m2.group(1)} —— 会话已存盘")
                            return
                    except Exception:
                        pass
                log(f"…等待中 {(i+1)*5}s")
            log("⌛ 超时未检测到登录成功（二维码可能过期/没扫）")
        finally:
            await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
