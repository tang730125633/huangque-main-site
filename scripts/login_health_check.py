#!/usr/bin/env python3
"""
爬虫登录态健康检查 —— 定时跑，抖音登录态失效就飞书私信告警 Tang。
- 有爬取在跑时跳过（正在爬 = 登录态本来就正常）
- 失效会重试一次再告警，避免页面抽风误报
"""
import os
import re
import asyncio
import subprocess

MC = os.path.expanduser("~/code/MediaCrawler")
UD = os.path.join(MC, "browser_data/dy_user_data_dir")
TANG = os.environ.get("LEADGEN_TANG_OPENID", "ou_0e1e2e6eeafa9abe7f28588b131f0850")


def crawl_running():
    return subprocess.run(["pgrep", "-f", "main.py --platform dy"],
                          capture_output=True).returncode == 0


def alert(msg):
    subprocess.run(["lark-cli", "im", "+messages-send", "--profile", "xiaoqiu", "--as", "bot",
                    "--user-id", TANG, "--text", msg], timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def check_once():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(UD, headless=True, args=["--no-sandbox"])
        page = await ctx.new_page()
        try:
            await page.goto("https://www.douyin.com/user/self",
                            wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)
            body = await page.evaluate("() => document.body.innerText")
            m = re.search(r"抖音号[:：]\s*([0-9A-Za-z_]+)", body)
            return m.group(1) if m else None
        finally:
            await ctx.close()


def main():
    if crawl_running():
        print("crawl running → skip (login OK)")
        return
    dyid = None
    for attempt in range(2):  # 失败重试一次
        try:
            dyid = asyncio.run(check_once())
        except Exception as e:
            print("check error:", e)
            dyid = None
        if dyid:
            break
    if dyid:
        print(f"✅ 登录态正常：抖音号 {dyid}")
    else:
        print("❌ 登录态失效 → 飞书告警")
        alert("⚠️ 抖音获客系统：爬虫登录态失效了（抖音号读不到），需要重新扫码登录。\n"
              "跟 Claude/小秋 说一句「重新扫码」即可触发二维码，几分钟搞定。")


if __name__ == "__main__":
    main()
