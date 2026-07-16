#!/usr/bin/env python3
"""服务器侧：走青果住宅代理 + 现有登录目录，验证 登录态+代理 是否双通。
读到抖音号 = 登录有效 且 代理能带着已登录会话访问抖音。
"""
import asyncio
import json
import re
import urllib.request
from playwright.async_api import async_playwright

QG_KEY = "55DC9F7E"
UD = "/home/ubuntu/MediaCrawler/browser_data/dy_user_data_dir"


def extract_proxy():
    url = f"https://share.proxy.qg.net/get?key={QG_KEY}&num=1&format=json"
    d = json.loads(urllib.request.urlopen(url, timeout=15).read())
    it = d["data"][0]
    return it["server"], it["proxy_ip"], it["area"], it["isp"]


async def main():
    server, pxip, area, isp = extract_proxy()
    print(f"代理网关 {server} | 出口 {pxip} {area} {isp}")
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            UD, headless=True,
            proxy={"server": f"http://{server}"},
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        page = await ctx.new_page()
        try:
            # 先确认浏览器出口IP确实是住宅代理
            try:
                await page.goto("https://myip.ipip.net", wait_until="domcontentloaded", timeout=30000)
                ipline = (await page.evaluate("() => document.body.innerText"))[:120].replace("\n", " ")
                print("浏览器出口:", ipline)
            except Exception as e:
                print("出口IP检查跳过:", str(e)[:80])

            await page.goto("https://www.douyin.com/user/self",
                            wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(7000)
            body = await page.evaluate("() => document.body.innerText")
            m = re.search(r"抖音号[:：]\s*([0-9A-Za-z_.]+)", body)
            if m:
                print("✅ 登录态有效，抖音号:", m.group(1), "—— 登录+代理 双通！")
            elif "验证" in body or "captcha" in body.lower():
                print("⚠️ 撞验证码中间页。片段:", body[:150].replace("\n", " "))
            else:
                print("⚠️ 没读到抖音号（登录态可能失效）。片段:", body[:180].replace("\n", " "))
        finally:
            await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
